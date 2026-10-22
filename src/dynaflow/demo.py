"""Small real-API runs through the shared preparation and runner."""
from collections import Counter
import json
from pathlib import Path
import tempfile

from . import configuration, data, managed_tools, runner
from .storage import atomic, read

ROOT = Path(__file__).resolve().parents[2]
MODES = ("direct", "dynamic", "forced")


def add_arguments(parser):
    parser.add_argument("--mode", choices=MODES, default="forced")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--config", type=Path, help="Explicit API configuration; takes precedence over model environment settings")
    parser.add_argument("--deadline", type=int, default=1200, help="Demo-only per-question deadline in seconds")
    parser.add_argument("--preflight-only", action="store_true", help="Run environment checks without solving the selected questions")
    parser.add_argument("--search-provider", choices=["deepseek", "exa", "tavily"])
    parser.add_argument("--fetch-provider", choices=["deepseek", "tavily"])


def prepare(args):
    if args.deadline <= 0:
        raise ValueError("Demo deadline must be positive")
    originals = data.load_questions(ROOT / "examples/demo/questions.json")
    questions = [{"number": i, "original_number": q.get("number"),
                  **{k: q[k] for k in ("dataset", "task_id", "Question")}}
                 for i, q in enumerate(originals, 1)]
    explicit = read(args.config) if getattr(args, "config", None) else None
    cfg = configuration.load(args.mode, explicit)
    cfg["question_order"] = list(range(1, len(questions) + 1))
    cfg["question_deadline_seconds"] = args.deadline
    cfg["servers"] = [dict(cfg["servers"][0], concurrency=1)]
    if explicit is None:
        for field, adapter in (("parameters", cfg["model_adapter"]),
                               ("subagent_parameters", cfg.get("subagent_adapter"))):
            if adapter == "deepseek":
                cfg[field] = {"max_tokens": 65536, "top_p": 0.95, "thinking": True, "reasoning_effort": "high"}
    model = cfg["model"]
    if args.mode != "direct":
        tools = managed_tools.options(preflight_tools=True, config=cfg)
        if args.search_provider:
            tools["search_provider"] = args.search_provider
        if args.fetch_provider:
            tools["fetch_provider"] = args.fetch_provider
        if tools["search_provider"] != "tavily":
            tools["search_endpoint"] = None
        if tools["fetch_provider"] != "tavily":
            tools["fetch_endpoint"] = "https://api.deepseek.com/v1/tools/fetch"
        cfg["workflow"].update(tools)
    out = (args.out or ROOT / "runs" / ("demo-" + args.mode)).resolve()
    temporary_root = ROOT / "runs"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="demo-prepare-", dir=temporary_root) as directory:
        temp = Path(directory).resolve()
        if not temp.is_relative_to(temporary_root.resolve()):
            raise ValueError("Temporary preparation path escaped run directory")
        atomic(temp / "questions.json", questions)
        atomic(temp / "config.json", cfg)
        data.prepare(out, temp / "questions.json", temp / "config.json",
                     profile=args.mode, demo=True)
    atomic(out / "demo.json", {"mode": args.mode, "model": model, "questions": len(questions)})
    return out


def run(args):
    out = prepare(args)
    print(json.dumps({"output": str(out), "mode": args.mode}, ensure_ascii=False), flush=True)
    report = {"mode": args.mode, "preflight_only": args.preflight_only, "passed": False}
    try:
        summary = runner.run(out, preflight_only=args.preflight_only)
        data.verify(out)
        report["summary"] = summary
        report["passed"] = (summary["phase"] == "preflight_passed" if args.preflight_only else
                            summary["overall"]["valid_answers"] == summary["overall"]["expected"])
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        report.update(error_type=type(error).__name__, error=str(error))
    finally:
        calls = [read(p) for p in (out / "calls").rglob("*.end.json")]
        report["calls_by_role"] = dict(Counter(c["role"] for c in calls))
        entries = [json.loads(line) for p in (out / "tools").glob("*.jsonl")
                   for line in p.read_text(encoding="utf-8").splitlines()]
        formal = [t for t in entries if t["dataset"] != "tool-preflight"]
        report["question_tool_calls"] = dict(Counter(t["name"] for t in formal))
        report["question_tool_errors"] = sum(bool(t["result"].get("is_error")) for t in formal)
        report["tool_preflight_calls"] = len(entries) - len(formal)
        atomic(out / "verification.json", report)
    print(json.dumps({"output": str(out), **report}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2
