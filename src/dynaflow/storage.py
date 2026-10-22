"""Atomic artifacts and process-scoped run locks."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import threading
from datetime import datetime, timezone

_WRITE_LOCK = threading.Lock()

def now():
    return datetime.now(timezone.utc).isoformat()

def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def append(path, value):
    with _WRITE_LOCK:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")

@contextlib.contextmanager
def run_lock(root):
    """OS lock releases on process exit. The lock file is not a stale-PID flag."""
    lock_path = Path(root) / ".run.lock"
    handle = lock_path.open("a+b", buffering=0)
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another process owns this run directory") from None
    try:
        yield
    finally:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

