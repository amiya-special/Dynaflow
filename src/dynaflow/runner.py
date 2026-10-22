"""Direct scheduling and recovery. Model payloads contain only question fields."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
from pathlib import Path
import time
import threading

from .data import verify
from .protocol import prompt, request, delivered, PROTOCOL
from .storage import atomic, append, read, run_lock, now
from . import transport
from .reporting import summarize, usage

def calls_for(root, number):
    return [read(p) for p in sorted((Path(root) / "calls" / f"question-{number:03d}").glob("*.end.json"))]

def recover(root):
    """Interrupted requests retain unknown consumption, never guessed zero."""
    ledger = root / "token-usage.jsonl"
    known = set()
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                known.add(json.loads(line)["call_id"])
            except (ValueError, KeyError):
                continue
    for start in sorted((root / "calls").glob("question-*/*.start.json")):
        end = start.with_name(start.name.replace(".start.json", ".end.json"))
        record = read(end if end.exists() else start)
        if not end.exists():
            record.update(error_type="Interrupted", error="Owner exited before usage receipt; consumption unknown",
                          usage=None, usage_reported=False, finished_at=now())
            atomic(end, record)
        if record["call_id"] not in known:
            append(ledger, {k: v for k, v in record.items() if k != "request"})
            known.add(record["call_id"])

def conversation(root, task, server, model, template, key, diagnostic=False, sleep=time.sleep, stop_event=None, config=None):
    body = request(model, task["number"], prompt(template, task), config=config)
    for attempt in range(1, 4):
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Run stopped")
        try:
            text, receipt = transport.call(root, task["number"], server, body, key, diagnostic, cancel_event=stop_event)
            break
        except transport.InferenceError as error:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Run stopped") from error
            if not error.retryable or attempt == 3:
                raise
            if stop_event is not None:
                if stop_event.wait(2 ** attempt):
                    raise InterruptedError("Run stopped")
            else:
                sleep(2 ** attempt)
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("Run stopped")
    return delivered(text, receipt), text

def preflight(root, manifest, config, template, key, stop_event=None):
    saved = root / "diagnostics/preflight.json"
    checks = []
    workflow = manifest.get("runtime_backend") == "portable-workflow"
    if workflow:
        from . import workflow as wf
        runtime = wf.check_runtime(config)
        transport.resolve_key(config["workflow"]["cloud_key_env"], config["workflow"]["cloud_url"])
        atomic(root / "diagnostics/workflow-runtime.json", runtime)
    if saved.exists() and read(saved).get("passed"):
        return
    for index, server in enumerate(config["servers"]):
        task = {"number": 9001 + index, "task_id": f"preflight-{server['id']}",
                "Question": "Compute 2 + 2. Give the integer as the final answer."}
        if workflow:
            message, _ = wf.chat(root, task, server, config, [{"role": "user", "content": "Compute 2 + 2. Reply FINAL ANSWER: 4"}],
                                 key, "main", None, wf.Cancellation(stop_event), diagnostic=True)
            from .protocol import extract
            answer = extract(message["content"])
        else:
            answer, _ = conversation(root, task, server, config["model"], template, key, True, stop_event=stop_event, config=config)
        if answer != "4":
            raise ValueError("API preflight failed")
        checks.append({"server_id": server["id"], "answer": answer, "model": config["model"], "max_tokens": config["parameters"]["max_tokens"]})
    atomic(saved, {"passed": True, "tests": checks, "at": now()})

def evaluate(root, task, server, config, template, key, stop_event=None):
    path = root / "results" / f"question-{task['number']:03d}.json"
    if path.exists():
        return
    start = time.monotonic()
    workflow = config.get("profile") in {"dynamic", "forced"}
    extra = {"selected_route": "direct"} if not workflow else {}
    parent_stop, timer = stop_event, None
    if config.get("question_deadline_seconds"):
        from .workflow import Cancellation
        stop_event = Cancellation(parent_stop)
        timer = threading.Timer(config["question_deadline_seconds"], stop_event.set)
        timer.daemon = True
        timer.start()
    try:
        if workflow:
            from .workflow import conversation as workflow_conversation
            answer, text, extra = workflow_conversation(root, task, server, config, template, key, stop_event)
        else:
            answer, text = conversation(root, task, server, config["model"], template, key, stop_event=stop_event, config=config)
        valid, error = True, None
    except InterruptedError:
        if timer is None or (parent_stop is not None and parent_stop.is_set()):
            raise
        answer, text, valid, error = "", "", False, "Question deadline exceeded"
    except Exception as exc:
        answer, text, valid = "", "", False
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if timer is not None:
            timer.cancel()
            stop_event.set()
    calls = calls_for(root, task["number"])
    atomic(path, {
        "number": task["number"], "dataset": task["dataset"], "task_id": task["task_id"],
        "method": config.get("mode", "Direct") if workflow else "Direct", "model": config["model"], "server_id": server["id"],
        "protocol_version": "workflow-api-v1" if workflow else PROTOCOL, "evaluation_valid": valid, "exit_code": 0 if valid else 1,
        "Final answer": answer, "visible_output": text, "error": error, "finished_at": now(),
        "duration_seconds": round(time.monotonic() - start, 3),
        "token_usage": usage(calls), "call_ids": [c["call_id"] for c in calls],
        "infrastructure_affected": False, "search_calls": 0, "search_errors": 0,
        "delivery_recovery_attempts": 0, "initial_delivery_failed": False,
        **extra,
    })

def run(root, *, preflight_only=False):
    from .managed_tools import session
    root = Path(root).resolve()
    with run_lock(root):
        manifest = verify(root)
        config = read(root / "config.json")
        questions = read(root / "questions.json")
        template = (root / manifest.get("prompt_file", "direct_prompt.txt")).read_text(encoding="utf-8")
        key = transport.api_key(manifest, config["servers"])
        stop = threading.Event()
        try:
            if (root / "STOP_BY_USER").exists():
                return summarize(root, "stopped")
            with session(root, questions, config, stop) as runtime_config:
                return _run_prepared(root, manifest, runtime_config, questions, template, key, stop, preflight_only)
        finally:
            stop.set()
            transport.cancel_active(root)


def _run_prepared(root, manifest, config, questions, template, key, stop_event, preflight_only):
    recover(root)
    if (root / "STOP_BY_USER").exists():
        return summarize(root, "stopped")
    atomic(root / "status.json", {"phase": "preflight", "at": now()})
    preflight(root, manifest, config, template, key, stop_event)
    if preflight_only:
        return summarize(root, "preflight_passed")
    by_number = {q["number"]: q for q in questions}
    queues = {s["id"]: [] for s in config["servers"]}
    for i, n in enumerate(config["question_order"]):
        if not (root / "results" / f"question-{n:03d}.json").exists():
            server_id = config["servers"][i % len(config["servers"])]["id"]
            queues[server_id].append(n)
    pools = {s["id"]: ThreadPoolExecutor(max_workers=s["concurrency"]) for s in config["servers"]}
    active = {}
    phase = "running"
    try:
        while any(queues.values()) or active:
            if (root / "STOP_BY_USER").exists():
                phase = "stopped"
                stop_event.set()
                transport.cancel_active(root)
                for q in queues.values():
                    q.clear()
            for server in config["servers"]:
                server_id = server["id"]
                count = sum(g == server_id for g in active.values())
                while queues[server_id] and count < server["concurrency"]:
                    n = queues[server_id].pop(0)
                    future = pools[server_id].submit(evaluate, root, by_number[n], server, config, template, key, stop_event)
                    active[future] = server_id
                    count += 1
            if active:
                done, _ = wait(active, timeout=2, return_when=FIRST_COMPLETED)
                for future in done:
                    active.pop(future)
                    try:
                        future.result()
                    except InterruptedError:
                        if not stop_event.is_set():
                            raise
            summarize(root, phase)
        if phase != "stopped":
            phase = "finished"
    except BaseException:
        phase = "interrupted"
        stop_event.set()
        transport.cancel_active(root)
        raise
    finally:
        # Interrupted attempts retain receipts but do not commit final answers.
        for pool in pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        summarize(root, phase)
    return read(root / "comparison/summary.json")
