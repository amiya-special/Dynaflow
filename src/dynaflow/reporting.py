"""Run progress, model answers, routes and recorded API usage."""
import csv
from pathlib import Path
from .storage import read, atomic, now

def usage(calls):
    known = [c for c in calls if c.get("usage_reported")]
    return {
        "calls": len(calls), "usage_reported_calls": len(known),
        "usage_missing_calls": len(calls) - len(known),
        "input_tokens": sum(c["usage"]["prompt_tokens"] for c in known),
        "output_tokens": sum(c["usage"]["completion_tokens"] for c in known),
        "total_tokens": sum(c["usage"]["total_tokens"] for c in known),
        "truncated_calls": sum(bool(c.get("truncated")) for c in calls),
        "failed_transport_attempts": sum(bool(c.get("error")) for c in calls),
    }

def summarize(root, phase=None):
    root = Path(root)
    m = read(root / "manifest.json")
    questions = read(root / "questions.json")
    results = {read(p)["number"]: read(p) for p in (root / "results").glob("*.json")}
    calls = [read(p) for p in (root / "calls").glob("question-*/*.end.json")]

    def metrics(qs):
        numbers = [q["number"] for q in qs]
        recorded = sum(n in results for n in numbers)
        valid = sum(bool(results.get(n, {}).get("evaluation_valid")) for n in numbers)
        return {
            "expected": len(numbers), "recorded": recorded,
            "valid_answers": valid, "invalid": recorded - valid,
            "pending": len(numbers) - recorded, "complete": recorded == len(numbers),
        }
    current_phase = phase or (read(root / "status.json").get("phase", "prepared") if (root / "status.json").exists() else "prepared")
    summary = {
        "at": now(), "phase": current_phase,
        "implementation": m["implementation"], "protocol_version": m["protocol_version"],
        "method": m["method"], "training_stage": m["training_stage"],
        "runtime_backend": m.get("runtime_backend", "standalone-direct"),
        "purpose": m["purpose"], "overall": metrics(questions),
        "datasets": {ds: metrics([q for q in questions if q["dataset"] == ds]) for ds in m["dataset_counts"]},
        "tokens": dict(usage(calls), diagnostics_excluded=True, includes_all_formal_attempts=True),
        "tokens_by_role": {role: usage([c for c in calls if c.get("role") == role]) for role in sorted({c.get("role", "unknown") for c in calls})},
        "routes": {route: sum(r.get("selected_route") == route for r in results.values()) for route in ("direct", "workflow")},
    }
    atomic(root / "comparison/summary.json", summary)
    atomic(root / "status.json", {"phase": summary["phase"], "at": now(), **summary["overall"]})
    fields = ["number", "dataset", "task_id", "Final answer", "evaluation_valid", "error",
              "selected_route", "runtime_backend"]
    tmp = root / "comparison/answers.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for q in questions:
            n = q["number"]
            row = {**q, **results.get(n, {})}
            writer.writerow({k: row.get(k) for k in fields})
    tmp.replace(root / "comparison/answers.csv")
    return summary

