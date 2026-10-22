"""Workflow tool definitions and provider-neutral API configuration."""
from pathlib import Path
from .storage import read, sha

PROTOCOL = "workflow-api-v1"
PROFILES = ("dynamic", "forced")

def runtime_model(cfg):
    return cfg["subagent_model"]

def runtime_dir():
    return Path(__file__).resolve().parents[2] / "workflow_runtime"

def built_runtime_dir():
    directory = runtime_dir() / "generated"
    if any(not (directory / name).is_file() for name in ("engine/runWorkflow.js", "workflow-tool.json")):
        raise ValueError("Workflow runtime is not built: run npm --prefix workflow_runtime ci --ignore-scripts and npm --prefix workflow_runtime run build from the project root")
    return directory


def runtime_hashes():
    built_runtime_dir()
    root = runtime_dir()
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*"))
            if p.is_file() and "node_modules" not in p.parts and p.suffix in {".json", ".mjs", ".js", ".ts", ".txt"}}

def request(cfg, messages, role, tools=None):
    from .configuration import request as api_request
    return api_request(cfg, messages, role, tools)

def tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}

def main_tools():
    return [read(built_runtime_dir() / "workflow-tool.json"),
            tool("TaskOutput", "Wait for a launched Workflow and read its actual result.",
                 {"task_id": {"type": "string"}, "block": {"type": "boolean"}}, ["task_id"]),
            tool("TaskStop", "Stop an existing Workflow.", {"task_id": {"type": "string"}}, ["task_id"])]

def validate(cfg, count):
    w = cfg["workflow"]
    if w["engine_mode"] != "docker" or w["backend"] != "portable":
        raise ValueError("Unsupported Workflow backend")
    from urllib.parse import urlsplit
    for value in [w["cloud_url"], w.get("tool_service_url")]:
        if value:
            u = urlsplit(value)
            if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError("Workflow endpoints must be credential-free HTTP(S) URLs")
    names = [t["function"]["name"] for t in w["tools"]]
    if len(names) != len(set(names)) or "StructuredOutput" in names:
        raise ValueError("Duplicate or reserved worker tool name")
    if names and not w.get("tool_service_url") and w.get("tool_service_mode") != "managed":
        raise ValueError("Worker tools require an explicit independent tool service")


def validate_invocation(invocation):
    from .research_tools import validate as validate_arguments
    validate_arguments(invocation, main_tools()[0]["function"]["parameters"])

