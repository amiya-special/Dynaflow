"""Workflow host with model API and research tool adapters.

Production scripts run in a network-isolated container; only the Python host
owns credentials and sends model requests.
"""
from concurrent.futures import ThreadPoolExecutor, Future
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from . import transport, workflow_protocol as wp
from .protocol import prompt, extract
from .storage import atomic, append, read, now
from . import system_prompts


class Cancellation:
    def __init__(self, parent=None):
        self.event, self.parent = threading.Event(), parent

    def set(self):
        self.event.set()

    def is_set(self):
        return self.event.is_set() or bool(self.parent and self.parent.is_set())

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while not self.is_set() and time.monotonic() < until:
            self.event.wait(min(0.1, max(0, until - time.monotonic())))
        return self.is_set()


def chat(root, task, server, cfg, messages, key, role, tools, stop, context=None, diagnostic=False):
    body = wp.request(cfg, messages, role, tools)
    for attempt in range(1, 4):
        if stop.is_set():
            raise InterruptedError("Workflow stopped")
        try:
            text, receipt = transport.call(root, task["number"], server, body, key, diagnostic,
                role=role, context=context, cancel_event=Cancellation(stop), idle_timeout=300 if role == "subagent" else None)
            break
        except transport.InferenceError as error:
            if stop.is_set():
                raise InterruptedError("Workflow stopped")
            # Retry transient transport failures without duplicating completed requests.
            if not error.retryable or attempt == 3:
                raise
            if stop.wait(2 ** attempt):
                raise InterruptedError("Workflow stopped")
    if stop.is_set():
        raise InterruptedError("Workflow stopped")
    if receipt["truncated"] or receipt["finish_reasons"][-1] not in {"stop", "tool_calls"}:
        raise ValueError("Workflow response is incomplete")
    calls = receipt.get("tool_calls", [])
    if receipt["finish_reasons"][-1] == "tool_calls" and not calls:
        raise ValueError("Tool-call finish without a tool call")
    if calls and (len({t["id"] for t in calls}) != len(calls) or any(not t["id"] for t in calls)):
        raise ValueError("Missing or duplicate tool call identifier")
    message = {"role": "assistant", "content": text}
    if cfg.get("subagent_adapter" if role == "subagent" else "model_adapter") == "deepseek":
        message["reasoning_content"] = receipt.get("reasoning_content", "")
    if calls:
        message["tool_calls"] = calls
    return message, receipt


def check_runtime(cfg):
    directory = wp.runtime_dir()
    wp.built_runtime_dir()
    if any(not (directory / "node_modules" / name / "package.json").is_file() for name in ("ajv", "zod")):
        raise ValueError("Workflow runtime missing: run npm --prefix workflow_runtime ci --ignore-scripts and npm --prefix workflow_runtime run build from the project root")
    installed = {name: read(directory / "node_modules" / name / "package.json")["version"] for name in ("ajv", "zod")}
    if installed != read(directory / "package.json")["dependencies"]:
        raise ValueError("Workflow dependency versions differ from the pinned package")
    if cfg["workflow"]["engine_mode"] != "docker":
        raise ValueError("Workflow execution requires the Docker backend")
    if not shutil.which("docker"):
        raise ValueError("Workflow execution requires Docker with Linux containers")
    image = cfg["workflow"]["container_image"]
    cp = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True)
    if cp.returncode:
        raise ValueError("Workflow container image is not installed: " + image)
    return {"engine_mode": "docker", "image_id": json.loads(cp.stdout)[0]["Id"], "dependencies": installed}



class Engine:
    def __init__(self, work, run_id, invocation, cfg, callback, stop, resume=False, script_changed=False):
        self.work, self.run_id = Path(work).resolve(), run_id
        self.invocation, self.cfg, self.callback, self.stop = invocation, cfg, callback, stop
        self.resume, self.script_changed = resume, script_changed

    def run(self):
        self.work.mkdir(parents=True, exist_ok=True)
        runtime = wp.runtime_dir().resolve()
        if self.cfg["workflow"]["engine_mode"] != "docker":
            raise ValueError("Workflow execution requires the Docker backend")
        container = "dynaflow-" + uuid.uuid4().hex
        options = dict(invocation=self.invocation, runId=self.run_id, resume=self.resume,
                       scriptChanged=self.script_changed, workDir="/work",
                       subagentModel=wp.runtime_model(self.cfg))
        atomic(self.work / "invocation.json", options)
        command = ["docker", "run", "--rm", "--name", container, "-i", "--network", "none",
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--mount", f"type=bind,source={runtime},target=/runtime,readonly",
                   "--mount", f"type=bind,source={self.work},target=/work", "--workdir", "/work",
                   self.cfg["workflow"]["container_image"], "node", "/runtime/host.mjs", "/work/invocation.json"]
        # Engine scripts receive neither model credentials nor the parent environment.
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "COMSPEC") if k in os.environ}
        events, write_lock = queue.Queue(), threading.Lock()
        result = None
        valid_agents = set()
        with (self.work / "engine.stderr.log").open("a", encoding="utf-8") as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                       cwd=self.work, env=env, text=True, encoding="utf-8", bufsize=1)
            def read_lines():
                try:
                    for line in process.stdout:
                        events.put(json.loads(line))
                except Exception:
                    events.put({"type": "protocol_error"})
                finally:
                    events.put(None)
            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            def respond(event):
                try:
                    value = self.callback(event["params"], event["id"], self.stop)
                except InterruptedError:
                    value = {"kind": "dead", "reason": "incomplete-output", "detail": "Cancelled"}
                except Exception as error:
                    value = {"kind": "dead", "reason": "runagent-threw", "detail": type(error).__name__}
                with write_lock:
                    if process.poll() is None:
                        try:
                            process.stdin.write(json.dumps({"id": event["id"], "result": value}, ensure_ascii=False) + "\n")
                            process.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
            # The engine itself enforces the original 3/default, 16/max semaphore.
            pool = ThreadPoolExecutor(max_workers=16)
            try:
                while True:
                    if self.stop.is_set():
                        raise InterruptedError("Workflow engine stopped")
                    try:
                        event = events.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if event is None:
                        break
                    append(self.work / "events.jsonl", event)
                    if event["type"] == "agent_request":
                        pool.submit(respond, event)
                    elif event["type"] == "progress":
                        progress = event["event"]
                        if progress.get("type") == "agent_done" and progress.get("result", {}).get("kind") == "ok" and progress["result"].get("model") == wp.runtime_model(self.cfg):
                            valid_agents.add(progress["agentId"])
                    elif event["type"] == "result":
                        result = event["result"]
                        break
                    elif event["type"] == "protocol_error":
                        raise ValueError("Workflow engine protocol failed")
            finally:
                self.stop.set()
                if result is not None:
                    # Let a successful worker drain its stdio and close normally.
                    # Killing it immediately after the result races Windows pipe cleanup.
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    process.terminate()
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
                process.wait(timeout=30)
                pool.shutdown(wait=True)
                reader.join(timeout=2)
                process.stdin.close()
                process.stdout.close()
        if result is None:
            raise ValueError("Workflow engine exited without a result")
        result["valid_cloud_agents"] = len(valid_agents)
        atomic(self.work / "result.json", result)
        return result


class Session:
    def __init__(self, root, task, server, cfg, template, key, stop):
        self.root, self.task, self.server, self.cfg = Path(root), task, server, cfg
        self.template, self.key, self.stop = template, key, Cancellation(stop)
        self.work = self.root / "workflows" / f"question-{task['number']:03d}" / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        self.jobs, self.delivered = {}, set()
        self.workflow_attempted = False
        self.protocol_violation = False
        self.agent_successes, self.agent_lock = 0, threading.Lock()
        self.tool_calls = self.tool_errors = 0
        self.launchers = []

    def worker(self, params, request_id, stop):
        if set(params) - {"prompt", "schema", "model", "allowedTools", "label", "phase"}:
            return {"kind": "dead", "reason": "invalid-output", "detail": "Unsupported subagent option"}
        schema = params.get("schema")
        tools = list(self.cfg["workflow"]["tools"])
        if params.get("allowedTools") is not None:
            tools = [t for t in tools if t["function"]["name"] in params["allowedTools"]]
        if schema:
            tools.append({"type": "function", "function": {"name": "StructuredOutput", "description": "Deliver the final structured result.", "parameters": schema}})
        system = system_prompts.worker_prompt(self.cfg, tools, bool(schema))
        atomic(self.work / f"worker-{request_id}.system.json",
               {"version": system_prompts.VERSION, "content": system})
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": params["prompt"]}]
        server = {"id": self.server["id"], "url": self.cfg["workflow"]["cloud_url"]}
        cloud_key = transport.resolve_key(self.cfg["workflow"]["cloud_key_env"], server["url"])
        output_tokens, unknown = 0, False
        while True:
            message, receipt = chat(self.root, self.task, server, self.cfg, messages, cloud_key, "subagent", tools, stop,
                                    {"session": self.work.name, "agent_request": request_id})
            output_tokens += (receipt.get("usage") or {}).get("completion_tokens", 0)
            unknown |= not receipt["usage_reported"]
            messages.append(message)
            calls = message.get("tool_calls", [])
            if not calls:
                if schema or not message["content"].strip():
                    return {"kind": "dead", "reason": "invalid-output", "detail": "No final structured delivery" if schema else "Empty output"}
                output = message["content"]
                break
            structured = [c for c in calls if c["function"]["name"] == "StructuredOutput"]
            if structured:
                if not schema or len(calls) != 1:
                    return {"kind": "dead", "reason": "invalid-output", "detail": "Ambiguous StructuredOutput delivery"}
                try:
                    output = json.loads(structured[0]["function"]["arguments"])
                except ValueError:
                    return {"kind": "dead", "reason": "invalid-output", "detail": "Invalid StructuredOutput JSON"}
                break
            for call in calls:
                value = self.execute_tool(call, tools, stop)
                # Original WebSearch/WebFetch map their result to a plain text tool block.
                rendered = value.get("content") if call["function"]["name"] in {"WebSearch", "WebFetch"} and isinstance(value, dict) and not value.get("is_error") else None
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": rendered if isinstance(rendered, str) else json.dumps(value, ensure_ascii=False)})
        # The engine's unlimited budget requires a numeric counter. Ledger remains
        # authoritative and explicitly marks unknown usage; this is never a cost estimate.
        result = {"kind": "ok", "output": output, "model": wp.runtime_model(self.cfg), "usage": {"outputTokens": output_tokens},
                  "usageIncomplete": unknown, "delivery": {"rawText": message["content"], "deliveredOutput": output,
                  "finishReason": receipt["finish_reasons"][-1]}}
        with self.agent_lock:
            self.agent_successes += 1
        return result

    def execute_tool(self, call, schemas, stop):
        name = call["function"]["name"]
        if name not in {t["function"]["name"] for t in schemas}:
            raise ValueError("Worker requested an unavailable tool")
        arguments = json.loads(call["function"]["arguments"])
        url = self.cfg["workflow"].get("tool_service_url")
        if not url:
            raise ValueError("Research tool service is not configured")
        connection, base = transport.connect(url)
        tool_id = uuid.uuid4().hex
        append(self.work / "tools.jsonl", {"call_id": tool_id, "phase": "start", "name": name, "arguments": arguments, "at": now()})
        with self.agent_lock:
            self.tool_calls += 1
        try:
            import http.client
            connection.response_class = lambda sock, **kwargs: http.client.HTTPResponse(
                transport._ResponseSocket(sock, stop, 660), **kwargs)
            headers = {"Content-Type": "application/json"}
            key = os.environ.get(self.cfg["workflow"]["tool_key_env"])
            if key:
                headers["Authorization"] = "Bearer " + key
            connection.request("POST", base + "/execute", json.dumps({"name": name, "arguments": arguments,
                "task_id": self.task["task_id"], "dataset": self.task["dataset"]}).encode(), headers)
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("Independent tool service HTTP " + str(response.status))
            value = json.loads(response.read())
            if isinstance(value, dict) and value.get("is_error"):
                with self.agent_lock:
                    self.tool_errors += 1
            append(self.work / "tools.jsonl", {"call_id": tool_id, "phase": "end", "name": name, "result": value, "at": now()})
            return value
        except Exception as error:
            append(self.work / "tools.jsonl", {"call_id": tool_id, "phase": "end", "name": name, "error_type": type(error).__name__, "at": now()})
            with self.agent_lock:
                self.tool_errors += 1
            raise
        finally:
            connection.close()

    def launch(self, invocation):
        self.workflow_attempted = True
        wp.validate_invocation(invocation)
        previous = invocation.get("resumeFromRunId")
        if previous:
            if previous not in self.jobs or not self.jobs[previous]["future"].done():
                raise ValueError("Resume requires a terminal run in this question session")
            old = self.jobs[previous]
            saved = old["invocation"]
            invocation = {**saved, **invocation}
            # Retain previous journal and result before engine replay/invalidation.
            archive = self.work / "history" / (previous + "-" + uuid.uuid4().hex)
            shutil.copytree(old["directory"], archive)
            run_id, directory = previous, old["directory"]
            script_changed = saved["script"] != invocation["script"]
            self.delivered.discard(run_id)
        else:
            run_id = "wf-" + uuid.uuid4().hex
            directory, script_changed = self.work / run_id, False
        if not isinstance(invocation.get("script"), str) or not invocation["script"].strip():
            raise ValueError("A new Workflow requires a nonempty inline script")
        cancellation = Cancellation(self.stop)
        engine = Engine(directory, run_id, invocation, self.cfg, self.worker, cancellation, bool(previous), script_changed)
        future = Future()
        def launch_engine():
            try:
                future.set_result(engine.run())
            except BaseException as error:
                future.set_exception(error)
        thread = threading.Thread(target=launch_engine, daemon=True)
        self.launchers.append(thread)
        thread.start()
        self.jobs[run_id] = dict(future=future, stop=cancellation, directory=directory, invocation=invocation)
        return {"task_id": run_id, "task_type": "local_workflow", "status": "running"}

    def output(self, run_id, block=True):
        job = self.jobs[run_id]
        if not block and not job["future"].done():
            return {"task_id": run_id, "status": "running"}
        while not job["future"].done():
            if self.stop.wait(0.1):
                raise InterruptedError("Workflow stopped")
        try:
            result = job["future"].result()
        except InterruptedError:
            if self.stop.is_set():
                raise
            result = {"status": "killed", "resultStatus": "not_checked"}
        except Exception as error:
            result = {"status": "failed", "error": str(error)}
        self.delivered.add(run_id)
        return {"task_id": run_id, "task_type": "local_workflow", **result}

    def run(self):
        messages = [{"role": "system", "content": system_prompts.main_prompt(self.cfg, wp.main_tools())},
                    {"role": "user", "content": prompt(self.template, self.task)}]
        try:
            while True:
                message, _ = chat(self.root, self.task, self.server, self.cfg, messages, self.key, "main", wp.main_tools(), self.stop)
                messages.append(message)
                atomic(self.work / "main-messages.json", messages)
                calls = message.get("tool_calls", [])
                if calls:
                    for call in calls:
                        try:
                            args = json.loads(call["function"]["arguments"])
                            name = call["function"]["name"]
                            if name == "Workflow":
                                value = self.launch(args)
                            elif name == "TaskOutput":
                                value = self.output(args["task_id"], args.get("block", True))
                            elif name == "TaskStop":
                                self.jobs[args["task_id"]]["stop"].set()
                                value = self.output(args["task_id"])
                            else:
                                self.protocol_violation = True
                                raise ValueError("Main agent cannot execute task-solving tools")
                        except (ValueError, KeyError, TypeError) as error:
                            value = {"is_error": True, "error": str(error)}
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(value, ensure_ascii=False)})
                    continue
                undelivered = [run_id for run_id in self.jobs if run_id not in self.delivered]
                if undelivered:
                    # A candidate produced before delivery is never promoted.
                    for run_id in undelivered:
                        messages.append({"role": "user", "content": "Workflow completion notification:\n" + json.dumps(self.output(run_id), ensure_ascii=False)})
                    continue
                answer = extract(message["content"])
                if self.protocol_violation:
                    raise ValueError("Main agent violated the Workflow tool boundary")
                if not answer:
                    raise ValueError("Empty final answer")
                receipts = [self.output(run_id) for run_id in self.delivered]
                valid_receipts = [r for r in receipts if r.get("status") == "completed" and r.get("resultStatus") == "valid"]
                if self.cfg["profile"] == "forced" and not self.jobs:
                    raise ValueError("Forced Workflow requires a real workflow")
                if self.workflow_attempted and (not valid_receipts or not any(r.get("valid_cloud_agents", 0) for r in valid_receipts)):
                    raise ValueError("Workflow final answer lacks a valid result and successful cloud agent")
                result = {"selected_route": "workflow" if self.jobs else "direct", "workflow_result_receipts": receipts,
                          "completion_rule": "post_workflow_valid_delivery_v3" if self.jobs else "direct_submission",
                          "runtime_backend": "portable-workflow",
                          "system_prompt_version": system_prompts.VERSION,
                          "research_tool_calls": self.tool_calls, "research_tool_errors": self.tool_errors,
                          "workflow_session": str(self.work.relative_to(self.root))}
                atomic(self.work / "main-messages.json", messages)
                return answer, message["content"], result
        finally:
            self.stop.set()
            for job in self.jobs.values():
                job["stop"].set()
            for thread in self.launchers:
                thread.join()


def conversation(root, task, server, cfg, template, key, stop):
    return Session(root, task, server, cfg, template, key, stop).run()
