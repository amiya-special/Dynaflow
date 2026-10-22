"""Shared environment adapter for source, module and installed CLI entrypoints."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
KEYS = {
    "DEEPSEEK_API_KEY",
    "DYNAFLOW_SEARCH_PROVIDER", "TAVILY_API_KEY", "EXA_API_KEY", "DYNAFLOW_TOOLS_IMAGE",
    "TAVILY_ENDPOINT_URL",
    "DYNAFLOW_FETCH_PROVIDER",
    "DYNAFLOW_BASE_URL", "DYNAFLOW_MODEL", "DYNAFLOW_MODEL_ADAPTER", "DYNAFLOW_CONCURRENCY", "DYNAFLOW_API_KEY",
    "DYNAFLOW_TRAINING_STAGE", "DYNAFLOW_CLOUD_URL", "DYNAFLOW_CLOUD_API_KEY",
    "DYNAFLOW_CLOUD_MODEL", "DYNAFLOW_CLOUD_ADAPTER",
    "DYNAFLOW_FETCH_MODEL", "DYNAFLOW_FETCH_BASE_URL", "DYNAFLOW_FETCH_ADAPTER", "DYNAFLOW_FETCH_API_KEY",
    "DYNAFLOW_CONTAINER_IMAGE", "DYNAFLOW_TOOL_API_KEY",
}


def load_env(path):
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in KEYS or key in values:
            raise ValueError(f"Invalid or duplicate env field at line {number}")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed env quote at line {number}")
            value = value[1:-1]
        values[key] = value
    # Validate the entire file before modifying the process environment.
    for key, value in values.items():
        os.environ.setdefault(key, value)


def main(argv=None):
    from dynaflow import cli
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--env-file", type=Path)
    options, arguments = bootstrap.parse_known_args(sys.argv[1:] if argv is None else argv)
    try:
        env_path = options.env_file or ROOT / ".env"
        if options.env_file or env_path.exists():
            load_env(env_path.resolve())
        return cli.main(arguments)
    except (OSError, ValueError, KeyError) as error:
        # Do not echo file contents or credential values on parsing failures.
        print(f"Environment setup failed ({type(error).__name__}); check env syntax, paths and profile.", file=sys.stderr)
        return 2
