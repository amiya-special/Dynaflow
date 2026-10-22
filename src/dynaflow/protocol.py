"""Question formatting, deterministic seeds and final-answer delivery checks."""
import hashlib
import json
import re

PROTOCOL = "direct-api-v1"
def seed(number, base_seed=42):
    job = f"run-01/Direct/question-{number:03d}"
    return int.from_bytes(hashlib.sha256(f"{base_seed}|{job}|1".encode()).digest()[:4], "big") % (2**31)

def prompt(template, task):
    prefix = template.split("# Question\n")[0]
    # Preserve insertion order exactly as in the original prompt builder.
    payload = {k: v for k, v in task.items() if k in {"task_id", "Question", "file_name", "file_path"}}
    return prefix + "# Question\n" + json.dumps(payload, ensure_ascii=False)

def request(model, number, text, config=None):
    from .configuration import default, request as api_request
    cfg = config or default("direct")
    cfg = dict(cfg, model=model)
    return api_request(cfg, [{"role": "user", "content": text}], number=number)

def extract(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    matches = re.findall(r"FINAL ANSWER:\s*(.*)", text)
    if matches:
        return matches[-1].strip()
    for match in re.finditer(r'\{[^{}]*"Final answer"[^{}]*\}', text, re.S):
        try:
            return str(json.loads(match.group())["Final answer"])
        except ValueError:
            pass
    raise ValueError("Direct response has no visible final answer record")

def delivered(text, receipt):
    if receipt["truncated"]:
        raise ValueError("Output reached token cap; delivery incomplete")
    if not receipt["finish_reasons"] or receipt["finish_reasons"][-1] != "stop":
        raise ValueError("Unexpected finish reason")
    answer = extract(text)
    if not answer:
        raise ValueError("Empty final answer")
    return answer

