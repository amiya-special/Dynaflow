"""Text/file/scientific tools executed only inside the per-question container."""
import glob
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

ROOT = Path("/work").resolve()
STATE = Path("/tmp/dynaflow-state.json")
LIMIT = 64000


def resolve(value, directory=False):
    p = Path(value)
    p = (ROOT / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError("Path is outside this question's /work directory")
    if directory and not p.is_dir():
        raise ValueError("Directory does not exist")
    return p


def command(args, timeout=120, cwd=ROOT):
    path = Path("/tmp") / ("output-" + uuid.uuid4().hex)
    with path.open("w+b") as output:
        process = subprocess.Popen(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output.seek(0)
        text = output.read(LIMIT * 4 + 1).decode("utf-8", errors="replace")
    path.unlink()
    truncated = len(text) > LIMIT
    return {"output": text[:LIMIT], "exit_code": process.returncode, "timed_out": timed_out,
            "truncated": truncated, "is_error": timed_out or process.returncode not in (0, 1)}


def execute(name, a, state):
    if name == "Bash":
        if a.get("run_in_background") or a.get("dangerouslyDisableSandbox"):
            raise ValueError("Background execution and sandbox bypass are not supported")
        cwd = resolve(state.get("cwd", "/work"), True)
        cwd_file = "/tmp/dynaflow-cwd"
        # A fixed trap records cwd; user command is an argument, never host-shell code.
        result = command(["bash", "-c", 'trap \'pwd -P > /tmp/dynaflow-cwd\' EXIT; eval "$1"', "bash", a["command"]],
                         min(max(a.get("timeout", 120000), 1), 600000) / 1000, cwd)
        if Path(cwd_file).exists():
            try:
                state["cwd"] = str(resolve(Path(cwd_file).read_text().strip(), True))
            except ValueError:
                state["cwd"] = "/work"
        result["is_error"] = result["timed_out"] or result["exit_code"] != 0
        return result
    if name in {"Read", "Write", "Edit"}:
        p = resolve(a["file_path"])
        if name == "Read":
            if a.get("pages") or p.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                raise ValueError("This backend reads text/notebooks; PDF/image rendering is unavailable")
            if p.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Text file exceeds 8 MiB read limit")
            text = p.read_text(encoding="utf-8")
            if p.suffix == ".ipynb":
                notebook = json.loads(text)
                text = "\n".join("".join(c.get("source", [])) + "\n" + json.dumps(c.get("outputs", []), ensure_ascii=False)
                                 for c in notebook["cells"])
            state.setdefault("read", {})[str(p)] = text
            offset = max(1, a.get("offset", 1))
            lines = text.splitlines()
            chosen = lines[offset - 1:offset - 1 + a.get("limit", 2000)]
            return {"content": "\n".join(f"{i}\t{line[:2000]}" for i,line in enumerate(chosen, offset)),
                    "total_lines": len(lines), "empty": not text}
        if name == "Write":
            if p.exists() and str(p) not in state.get("read", {}):
                raise ValueError("Read the existing file before overwriting it")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a["content"], encoding="utf-8")
            return {"file_path": str(p), "written": True}
        if str(p) not in state.get("read", {}):
            raise ValueError("Read the file before editing it")
        text = p.read_text(encoding="utf-8")
        old, new = a["old_string"], a["new_string"]
        if not old or old == new or old not in text:
            raise ValueError("old_string must exist and differ from new_string")
        if text != state["read"][str(p)]:
            raise ValueError("File changed since Read; read again before editing")
        if not a.get("replace_all") and text.count(old) != 1:
            raise ValueError("old_string is not unique")
        text = text.replace(old, new, -1 if a.get("replace_all") else 1)
        p.write_text(text, encoding="utf-8")
        state["read"][str(p)] = text
        return {"file_path": str(p), "edited": True}
    if name == "Glob":
        directory = resolve(a.get("path", "/work"), True)
        pattern = a["pattern"]
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("Glob pattern must stay within its directory")
        paths = [Path(p).resolve() for p in glob.glob(str(directory / pattern), recursive=True)]
        paths = [p for p in paths if p.is_relative_to(ROOT) and p.is_file()]
        return {"files": [str(p) for p in sorted(paths, key=lambda p:p.stat().st_mtime, reverse=True)]}
    if name == "Grep":
        path = resolve(a.get("path", "/work"))
        args = ["rg", "--no-follow"]
        mode = a.get("output_mode", "files_with_matches")
        if mode == "files_with_matches": args.append("-l")
        elif mode == "count": args.append("-c")
        elif mode != "content": raise ValueError("Unsupported Grep output mode")
        for field, flag in (("-i","-i"),("multiline","--multiline"),("-n","-n")):
            if a.get(field, field == "-n" and mode == "content"): args.append(flag)
        if a.get("multiline"): args.append("--multiline-dotall")
        for field, flag in (("-A","-A"),("-B","-B"),("-C","-C")):
            if field in a: args += [flag,str(int(a[field]))]
        for field, flag in (("glob","--glob"),("type","--type")):
            if a.get(field): args += [flag,a[field]]
        result = command(args + ["-e", a["pattern"], "--", str(path)])
        lines = result["output"].splitlines()
        offset, limit = int(a.get("offset", 0)), int(a.get("head_limit", 200))
        result["output"] = "\n".join(lines[offset:offset+limit] if limit else lines[offset:])
        result["truncated"] |= bool(limit and len(lines) > offset+limit)
        return result
    if name == "TodoWrite":
        state["todos"] = a["todos"]
        return {"todos": a["todos"]}
    raise ValueError("Unsupported container tool")


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        result = execute(request["name"], request["arguments"], state)
        STATE.write_text(json.dumps(state), encoding="utf-8")
        payload = json.dumps(result, ensure_ascii=False)
        if len(payload) > 100000:
            result = {"output": payload[:LIMIT], "truncated": True}
    except Exception as error:
        result = {"is_error": True, "error": str(error), "error_type": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
