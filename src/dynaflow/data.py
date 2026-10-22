"""Prepare answer-free inputs and verify immutable run snapshots."""
from collections import Counter
from importlib.resources import files
from pathlib import Path
import shutil

from . import __version__
from .protocol import PROTOCOL
from . import configuration
from .storage import atomic, read, sha, now

def asset(name):
    return Path(str(files("dynaflow").joinpath("assets", name)))

def key(row):
    return row["dataset"], row["task_id"]

def unique(rows, label):
    result = {}
    for row in rows:
        k = key(row)
        if k in result:
            raise ValueError(f"Duplicate {label} identity: {k}")
        result[k] = row
    return result

def load_questions(path):
    questions = read(path)
    if not isinstance(questions, list) or not questions:
        raise ValueError("Questions must be a nonempty JSON list")
    answer_fields = {"answer", "answers", "gold", "final answer", "final_answer",
                     "reference_answer", "reference_answers", "gold_answer", "gold_answers"}
    for number, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            raise ValueError("Each question must be a JSON object")
        if any(name.lower() in answer_fields for name in q):
            raise ValueError("Question input contains answers")
        if type(q.get("number")) is not int or q["number"] != number:
            raise ValueError("Questions must have contiguous numbers 1..N in input order")
        if any(not isinstance(q.get(name), str) or not q[name].strip() for name in ("dataset", "Question")):
            raise ValueError("Each question requires nonempty dataset and Question strings")
        task_id = q.get("task_id")
        if not (type(task_id) is int or isinstance(task_id, str) and task_id.strip()):
            raise ValueError("Each question requires a string or integer task_id")
    unique(questions, "question")
    return questions

def validate_config(config, count):
    configuration.validate(config, count)


def prepare(out, questions_file, config_file=None,
            api_key_env=None, profile=None, workflow_options=None, *, demo=False):
    from . import workflow_protocol as wp
    explicit = read(config_file) if config_file else None
    mode = profile or (explicit or {}).get("mode", "direct")
    config = configuration.load(mode, explicit)
    workflow = mode in wp.PROFILES
    out = Path(out).resolve()
    if out.exists():
        raise ValueError("Output exists; resume its existing run instead")
    questions = load_questions(questions_file)
    if config.get("question_order") is None:
        config["question_order"] = [q["number"] for q in questions]
    if api_key_env:
        config["api_key_env"] = api_key_env
        if workflow and not (explicit or {}).get("workflow", {}).get("cloud_key_env"):
            config["workflow"]["cloud_key_env"] = api_key_env
    if workflow_options:
        if not workflow:
            raise ValueError("Direct cannot enable Workflow tools")
        config["workflow"].update(workflow_options)
    validate_config(config, len(questions))
    if workflow:
        wp.validate(config, len(questions))
        wp.built_runtime_dir()
    out.mkdir(parents=True)
    for name in ("calls", "results", "comparison", "diagnostics"):
        (out / name).mkdir()
    atomic(out / "questions.json", questions)
    prompt_name = "workflow_prompt.txt" if workflow else "direct_prompt.txt"
    shutil.copyfile(asset(mode + "_prompt.txt"), out / prompt_name)
    atomic(out / "config.json", config)
    names = ["questions.json", prompt_name, "config.json"]
    package = Path(__file__).parent
    code_hashes = {p.relative_to(package).as_posix(): sha(p) for p in package.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".json", ".txt"}}
    manifest = {
        "implementation": "dynaflow", "implementation_version": __version__,
        "protocol_version": wp.PROTOCOL if workflow else PROTOCOL, "created_at": now(),
        "method": mode,
        "training_stage": "demo" if demo else config.get("training_stage", "unspecified"),
        "purpose": "demo" if demo else "run", "model_adapter": config["model_adapter"],
        "profile": mode, "prompt_file": prompt_name,
        "runtime_backend": "portable-workflow" if workflow else "standalone-direct",
        "workflow_runtime_sha256": wp.runtime_hashes() if workflow else {},
        "expected": len(questions), "dataset_counts": dict(Counter(q["dataset"] for q in questions)),
        "input_sha256": {name: sha(out / name) for name in names},
        "code_sha256": code_hashes,
        "api_key_env": config["api_key_env"],
    }
    atomic(out / "manifest.json", manifest)
    return manifest

def verify(root):
    root = Path(root)
    manifest = read(root / "manifest.json")
    for name, expected in manifest["input_sha256"].items():
        p = (root / name).resolve()
        if not p.is_relative_to(root.resolve()) or sha(p) != expected:
            raise ValueError(f"Run input was modified: {name}")
    package = Path(__file__).parent
    for name, expected in manifest["code_sha256"].items():
        p = (package / name).resolve()
        if not p.is_relative_to(package.resolve()) or sha(p) != expected:
            raise ValueError(f"Implementation was modified since prepare: {name}")
    if manifest.get("workflow_runtime_sha256"):
        from .workflow_protocol import runtime_dir
        directory = runtime_dir().resolve()
        for name, expected in manifest["workflow_runtime_sha256"].items():
            p = (directory / name).resolve()
            if not p.is_relative_to(directory) or sha(p) != expected:
                raise ValueError("Workflow runtime modified since prepare: " + name)
    return manifest

