"""Standard-library HTTP/SSE transport for model API calls."""
import gzip
import http.client
import io
import json
import os
from pathlib import Path
import socket
import ssl
import time
import threading
from urllib.parse import urlsplit
import uuid

from .storage import atomic, append, now

_ACTIVE = {}
_ACTIVE_LOCK = threading.Lock()

def cancel_active(root):
    with _ACTIVE_LOCK:
        cancellations = list(_ACTIVE.get(str(Path(root).resolve()), set()))
    for cancellation in cancellations:
        cancellation.set()

class _InterruptibleReader(io.RawIOBase):
    """Poll for cancellation without imposing a deadline on model generation.

    Use recv directly: socket.makefile's reader cannot resume after a timeout.
    The unused file handle retains the socket through HTTP/1.0 auto-close.
    """
    def __init__(self, sock, cancellation, idle_timeout=None):
        self.sock = sock
        self.hold = sock.makefile("rb", buffering=0)
        self.cancellation = cancellation
        self.idle_timeout = idle_timeout
        self.last_data = time.monotonic()
        sock.settimeout(0.25)

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.cancellation.is_set():
            try:
                count = self.sock.recv_into(buffer)
                self.last_data = time.monotonic()
                return count
            except socket.timeout:
                if self.idle_timeout and time.monotonic() - self.last_data >= self.idle_timeout:
                    raise TimeoutError("Upstream idle timeout")
                continue
        raise InterruptedError("Request stopped by user")

    def close(self):
        self.hold.close()
        super().close()

class _ResponseSocket:
    def __init__(self, sock, cancellation, idle_timeout=None):
        self.sock, self.cancellation = sock, cancellation
        self.idle_timeout = idle_timeout

    def makefile(self, mode):
        return io.BufferedReader(_InterruptibleReader(self.sock, self.cancellation, self.idle_timeout))

class InferenceError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable

def connect(url):
    u = urlsplit(url)
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    if u.scheme == "https":
        connection = http.client.HTTPSConnection(u.hostname, u.port, timeout=30, context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    connection.connect()
    # Match the original connect=30, total=None, sock_read=None policy.
    connection.sock.settimeout(None)
    return connection, u.path.rstrip("/")

def call(root, number, server, body, key, diagnostic=False, *, role="direct", context=None, cancel_event=None, idle_timeout=None):
    root = Path(root)
    directory = root / ("diagnostics/calls" if diagnostic else "calls") / f"question-{number:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    call_id = uuid.uuid4().hex
    prefix = directory / call_id
    start = time.monotonic()
    record = dict(call_id=call_id, number=number, role=role, model=body["model"],
                  server_id=server["id"], url=server["url"], at=now(), request=body,
                  usage=None, usage_reported=False, stream_done=False, finish_reasons=[],
                  diagnostic=diagnostic)
    record["context"] = context or {}
    atomic(str(prefix) + ".start.json", record)
    visible = []
    reasoning = []
    tool_calls = {}
    reasoning_chars = 0
    events = 0
    connection = None
    response = None
    cancellation = cancel_event if cancel_event is not None else threading.Event()

    def event(line):
        nonlocal reasoning_chars, events
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if payload == b"[DONE]":
            record["stream_done"] = True
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            record["unparsed_events"] = record.get("unparsed_events", 0) + 1
            return
        if isinstance(obj.get("usage"), dict):
            record["usage"] = obj["usage"]
        if obj.get("error"):
            record["provider_error"] = True
        for name in ("provider", "model", "id"):
            if name in obj:
                record["response_" + name] = obj[name]
        for choice in obj.get("choices", []):
            if choice.get("finish_reason"):
                record["finish_reasons"].append(choice["finish_reason"])
            delta = choice.get("delta") or {}
            for part in delta.get("tool_calls", []):
                index = part.get("index", 0)
                current = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    current["id"] = part["id"]
                for name in ("name", "arguments"):
                    current["function"][name] += part.get("function", {}).get(name, "")
            if isinstance(delta.get("content"), str):
                visible.append(delta["content"])
            for name in ("reasoning_content", "reasoning"):
                if isinstance(delta.get(name), str):
                    reasoning_chars += len(delta[name])
                    if body.get("thinking", {}).get("type") == "enabled" and body.get("tools") and name == "reasoning_content":
                        reasoning.append(delta[name])
        events += 1

    try:
        connection, base = connect(server["url"])
        with _ACTIVE_LOCK:
            _ACTIVE.setdefault(str(root.resolve()), set()).add(cancellation)
        connection.response_class = lambda sock, **kwargs: http.client.HTTPResponse(
            _ResponseSocket(sock, cancellation, idle_timeout), **kwargs)
        connection.request("POST", base + "/chat/completions",
                           body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                           headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        response = connection.getresponse()
        record["http_status"] = response.status
        if response.status != 200:
            # Do not persist arbitrary provider error bodies that might echo credentials.
            raise InferenceError(f"Local HTTP {response.status}", response.status >= 500 or response.status == 429)
        last_progress = start
        with gzip.open(str(prefix) + ".response.gz", "wb") as trace:
            while True:
                line = response.readline()
                if not line:
                    break
                trace.write(line)
                event(line.rstrip(b"\r\n"))
                if time.monotonic() - last_progress >= 5:
                    trace.flush()
                    atomic(str(prefix) + ".progress.json", {
                        "at": now(), "stream_events": events,
                        "visible_characters": sum(map(len, visible)), "reasoning_characters": reasoning_chars,
                        "note": "Character/event progress only; not token estimates",
                    })
                    last_progress = time.monotonic()
        record["transport_complete"] = True
        if record.get("provider_error"):
            raise InferenceError("Provider returned an error in the stream")
        if not record["stream_done"]:
            raise InferenceError("Stream ended before DONE", True)
        if not record["finish_reasons"]:
            raise InferenceError("Stream ended without finish_reason", True)
        record["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        if reasoning:
            record["reasoning_content"] = "".join(reasoning)
        return "".join(visible), record
    except (OSError, http.client.HTTPException) as error:
        record.update(error_type=type(error).__name__, error="Transport failure")
        raise InferenceError("Transport failure: " + type(error).__name__, True) from error
    except BaseException as error:
        record.update(error_type=type(error).__name__, error=str(error)[:1200])
        raise
    finally:
        if response is not None:
            response.close()
        if connection:
            with _ACTIVE_LOCK:
                _ACTIVE.get(str(root.resolve()), set()).discard(cancellation)
            connection.close()
        usage = record.get("usage")
        record.update(
            finished_at=now(), duration_seconds=round(time.monotonic() - start, 3),
            usage_reported=bool(usage and all(isinstance(usage.get(k), int) for k in ("prompt_tokens", "completion_tokens", "total_tokens"))),
            truncated="length" in record["finish_reasons"], visible_characters=sum(map(len, visible)),
            reasoning_characters=reasoning_chars,
        )
        atomic(str(prefix) + ".end.json", record)
        append(root / ("diagnostics/token-usage.jsonl" if diagnostic else "token-usage.jsonl"),
               {k: v for k, v in record.items() if k not in {"request", "reasoning_content"}})

def resolve_key(variable, url):
    value = os.environ.get(variable)
    if value:
        return value
    if urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}:
        return "local-api"
    raise ValueError("Set " + variable + " for the configured remote API")


def api_key(manifest, servers):
    keys = [resolve_key(manifest["api_key_env"], server["url"]) for server in servers]
    return keys[0]
