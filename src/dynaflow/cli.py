"""Command-line entrypoints for preparing, running and inspecting inference."""
import argparse
import json
from pathlib import Path
import sys

from .data import prepare, verify
from .runner import run
from .reporting import summarize

MODES = {mode: mode for mode in ("direct", "dynamic", "forced")}


def resolve_profile(mode=None, profile=None, config=None):
    """Resolve the execution mode from CLI arguments and explicit configuration."""
    configured = (config or {}).get("mode", (config or {}).get("profile"))
    requested = MODES.get(mode) if mode else None
    selected = [value for value in (requested, profile, configured) if value]
    if mode and not requested or any(value not in MODES.values() for value in selected):
        raise ValueError("Unsupported execution mode")
    if len(set(selected)) > 1:
        raise ValueError("Mode and config refer to different experiments")
    return next(iter(selected), "direct")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dynaflow")
    parser.add_argument("--env-file", type=Path, help="Private env file (loaded by the shared entrypoint)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Prepare an immutable run; no model calls")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--questions", type=Path, required=True, help="Answer-free question JSON; see examples/demo/questions.json")
    p.add_argument("--config", type=Path)
    p.add_argument("--api-key-env", help="Override the name of the main API key environment variable")
    p.add_argument("--mode", choices=MODES, help="Execution mode; defaults to direct or the supplied config")
    p.add_argument("--workflow-options", type=Path, help="Portable Workflow backend/tool-service configuration JSON (no credentials)")
    p.add_argument("--managed-tools", action="store_true", help="Start the independent research tool service during run")
    from . import demo
    demo.add_arguments(sub.add_parser("demo", help="Run the included questions with direct, dynamic or forced mode"))
    for name in ("run", "summarize", "verify"):
        p = sub.add_parser(name)
        p.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            cfg = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
            profile = resolve_profile(args.mode, config=cfg)
            if args.managed_tools and (profile == "direct" or args.workflow_options):
                raise ValueError("--managed-tools requires Workflow and cannot be combined with --workflow-options")
            from .managed_tools import options as managed_options
            from .configuration import load as load_config
            workflow_options = (managed_options(config=load_config(profile, cfg)) if args.managed_tools else
                                json.loads(args.workflow_options.read_text(encoding="utf-8")) if args.workflow_options else None)
            result = prepare(args.out, args.questions, args.config,
                             api_key_env=args.api_key_env, profile=profile, workflow_options=workflow_options)
            result = {"prepared": str(args.out), "expected": result["expected"], "mode": result["profile"], "model_calls": 0}
        elif args.command == "demo":
            return demo.run(args)
        elif args.command == "run":
            result = run(args.run_dir)
        elif args.command == "verify":
            m = verify(args.run_dir)
            result = {"passed": True, "expected": m["expected"], "mode": m["profile"]}
        else:
            verify(args.run_dir)
            result = summarize(args.run_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

