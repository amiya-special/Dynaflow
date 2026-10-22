"""Authenticated loopback RPC; one isolated scientific container per question."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import threading
import uuid

from .data import asset, key
from .storage import read, append, now
from .research_web import WebTools


def definitions():
    return read(asset("research_tools.json"))


def validate(value, schema, location="arguments"):
    """Validate the JSON Schema subset used by the exposed tools."""
    supported = {"$schema", "type", "properties", "required", "additionalProperties", "description",
                 "items", "enum", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum",
                 "format", "default", "title"}
    if set(schema) - supported:
        raise ValueError("Unsupported schema keywords: " + str(set(schema) - supported))
    kind = schema.get("type")
    good = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
            "boolean": isinstance(value, bool), "number": type(value) in {int, float},
            "integer": type(value) is int, "null": value is None}
    if kind and not good.get(kind, False):
        raise ValueError(location + ": expected " + kind)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(location + ": unsupported enum value")
    if type(value) in {int, float}:
        if not math.isfinite(value) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(location + ": number outside schema bounds")
        if value <= schema.get("exclusiveMinimum", -math.inf):
            raise ValueError(location + ": number must exceed exclusiveMinimum")
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
        raise ValueError(location + ": string length outside schema bounds")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            raise ValueError(location + ": required field missing")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(location + ": unknown field")
        for field in value.keys() & properties.keys():
            validate(value[field], properties[field], location + "." + field)
    if isinstance(value, list) and "items" in schema:
        for item in value:
            validate(item, schema["items"], location + "[]")


class ToolService:
    def __init__(self, root, tasks, provider, summarize, image="dynaflow-tools:py311-v1", fetch_provider="tavily"):
        self.root, self.image = Path(root).resolve(), image
        (self.root / "tools").mkdir(parents=True, exist_ok=True)
        self.tasks = {key(t): t for t in tasks}
        self.schemas = {t["function"]["name"]: t["function"]["parameters"] for t in definitions()}
        self.locks = {k: threading.Lock() for k in self.tasks}
        self.containers, self.token = {}, secrets.token_urlsafe(32)
        self.web = WebTools(provider, summarize, fetch_provider)
        self.closed = False
        self.environment = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "COMSPEC") if k in os.environ}
        self.image_id = json.loads(self.docker(["image", "inspect", image]).stdout)[0]["Id"]
        self.http = None

    def docker(self, args, payload=None, timeout=660):
        cp = subprocess.run(["docker", *args], input=payload, capture_output=True, text=True,
                            encoding="utf-8", env=self.environment, timeout=timeout)
        if cp.returncode:
            raise RuntimeError("Tool container command failed: " + cp.stderr[:1000])
        return cp

    def container(self, task):
        k = key(task)
        if k not in self.containers:
            work = self.root / "tool-workspaces" / f"question-{task['number']:03d}"
            work.mkdir(parents=True, exist_ok=True)
            name = "dynaflow-tools-" + uuid.uuid4().hex
            self.docker(["run", "-d", "--rm", "--name", name, "--network", "none", "--read-only",
                         "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
                         "--memory", "2g", "--cpus", "2", "--tmpfs", "/tmp:rw,nosuid,size=256m,mode=1777",
                         "--mount", f"type=bind,source={work},target=/work", self.image_id])
            self.containers[k] = name
        return self.containers[k]

    def execute(self, request):
        k = request.get("dataset"), request.get("task_id")
        if k not in self.tasks:
            raise ValueError("Unregistered question identity")
        task, name, a = self.tasks[k], request["name"], request["arguments"]
        if name not in self.schemas:
            raise ValueError("Unavailable tool")
        validate(a, self.schemas[name])
        with self.locks[k]:
            if self.closed:
                raise ValueError("Tool service closed")
            entry = {"at": now(), "dataset": k[0], "task_id": k[1], "name": name, "arguments": a}
            try:
                if name == "WebSearch":
                    result = self.web.search(a)
                elif name == "WebFetch":
                    result = self.web.fetch(a, task)
                else:
                    result = json.loads(self.docker(["exec", "-i", self.container(task),
                        "python", "/opt/tools/execute.py"], json.dumps({"name": name, "arguments": a})).stdout)
            except Exception as error:
                result = {"is_error": True, "error_type": type(error).__name__, "error": str(error)}
            append(self.root / "tools" / f"question-{task['number']:03d}.jsonl", {**entry, "result": result})
            return result

    def start(self):
        service = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/execute" or not hmac.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + service.token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1_000_000:
                        raise ValueError("Invalid request size")
                    result = service.execute(json.loads(self.rfile.read(length)))
                    payload, status = json.dumps(result, ensure_ascii=False).encode(), 200
                except (ValueError, KeyError, TypeError) as error:
                    payload, status = json.dumps({"error_type": type(error).__name__}).encode(), 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.http.server_port}"

    def close(self):
        self.closed = True
        if self.http:
            self.http.shutdown()
            self.http.server_close()
            self.thread.join()
        for name in self.containers.values():
            subprocess.run(["docker", "rm", "-f", name], env=self.environment, capture_output=True, timeout=30)
