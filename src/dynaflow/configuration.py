"""Provider-neutral API configuration and request construction."""
from copy import deepcopy
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .storage import read

ROOT = Path(__file__).resolve().parents[2]
MODES = ("direct", "dynamic", "forced")
RESERVED = {"model", "messages", "tools", "stream", "stream_options"}


def default(mode):
    if mode not in MODES:
        raise ValueError("Unsupported evaluation mode")
    return read(ROOT / "configs" / (mode + ".json"))


def url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Expected a credential-free HTTP(S) base URL")
    return value


def normalize(cfg):
    cfg = deepcopy(cfg)
    mode = cfg.get("mode", cfg.get("profile"))
    if mode not in MODES or cfg.get("profile", mode) != mode:
        raise ValueError("Configuration mode and profile must match")
    cfg["profile"] = mode
    if mode != "direct":
        cfg["subagent_model"] = cfg.get("subagent_model") or cfg["model"]
        cfg["subagent_adapter"] = cfg.get("subagent_adapter") or cfg["model_adapter"]
        cfg["subagent_parameters"] = deepcopy(cfg.get("subagent_parameters") if cfg.get("subagent_parameters") is not None else cfg["parameters"])
        cfg["workflow"]["cloud_url"] = cfg["workflow"].get("cloud_url") or cfg["servers"][0]["url"]
        cfg["workflow"]["cloud_key_env"] = cfg["workflow"].get("cloud_key_env") or cfg["api_key_env"]
    return cfg


def load(mode, explicit=None):
    cfg = deepcopy(explicit) if explicit is not None else default(mode)
    if cfg.get("mode") != mode:
        raise ValueError("Mode and config refer to different execution modes")
    if explicit is None:
        for env, field in (("DYNAFLOW_MODEL", "model"), ("DYNAFLOW_MODEL_ADAPTER", "model_adapter"), ("DYNAFLOW_TRAINING_STAGE", "training_stage")):
            if os.environ.get(env):
                cfg[field] = os.environ[env]
        if os.environ.get("DYNAFLOW_BASE_URL"):
            cfg["servers"][0]["url"] = os.environ["DYNAFLOW_BASE_URL"]
        if os.environ.get("DYNAFLOW_CONCURRENCY"):
            cfg["servers"][0]["concurrency"] = int(os.environ["DYNAFLOW_CONCURRENCY"])
        if mode != "direct":
            for env, field in (("DYNAFLOW_CLOUD_MODEL", "subagent_model"), ("DYNAFLOW_CLOUD_ADAPTER", "subagent_adapter")):
                if os.environ.get(env):
                    cfg[field] = os.environ[env]
            for env, field in (("DYNAFLOW_CLOUD_URL", "cloud_url"), ("DYNAFLOW_CONTAINER_IMAGE", "container_image")):
                if os.environ.get(env):
                    cfg["workflow"][field] = os.environ[env]
            if os.environ.get("DYNAFLOW_CLOUD_API_KEY"):
                cfg["workflow"]["cloud_key_env"] = "DYNAFLOW_CLOUD_API_KEY"
    return normalize(cfg)


def validate(cfg, count):
    if cfg["model"] == "your-model-name" or not isinstance(cfg["model"], str) or not cfg["model"].strip():
        raise ValueError("Set DYNAFLOW_MODEL or model in the explicit configuration")
    for role in ("main", "subagent") if cfg["mode"] != "direct" else ("main",):
        adapter = cfg["model_adapter"] if role == "main" else cfg["subagent_adapter"]
        parameters = cfg["parameters"] if role == "main" else cfg["subagent_parameters"]
        extra = cfg.get("extra_body", {}) if role == "main" else cfg.get("subagent_extra_body", {})
        if adapter not in {"openai", "deepseek"}:
            raise ValueError("Supported model adapters: openai, deepseek")
        if not isinstance(parameters, dict) or type(parameters.get("max_tokens")) is not int or parameters["max_tokens"] < 1:
            raise ValueError("Each model requires a positive max_tokens parameter")
        if not isinstance(extra, dict) or RESERVED.intersection(parameters) or RESERVED.intersection(extra):
            raise ValueError("Model parameters cannot override model, messages, tools or streaming")
        if role == "subagent" and (not isinstance(cfg["subagent_model"], str) or not cfg["subagent_model"].strip()):
            raise ValueError("Subagent model must be specified")
        if adapter == "openai" and "thinking" in parameters:
            raise ValueError("Provider-specific thinking fields belong in extra_body for the openai adapter")
    servers = cfg.get("servers", [])
    if not servers or any(not isinstance(s.get("id"), str) or not s["id"] for s in servers) or len({s["id"] for s in servers}) != len(servers):
        raise ValueError("Expected distinct, nonempty API server identifiers")
    for server in servers:
        url(server["url"])
        if type(server.get("concurrency")) is not int or server["concurrency"] < 1:
            raise ValueError("Server concurrency must be a positive integer")
    key_names = [cfg["api_key_env"]]
    if cfg["mode"] != "direct":
        key_names.append(cfg["workflow"]["cloud_key_env"])
    for variable in key_names:
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise ValueError("API key configuration must name an environment variable")
    if cfg.get("seed_strategy") not in {"fixed", "per_question", None} or (cfg.get("seed") is not None and type(cfg["seed"]) is not int):
        raise ValueError("Invalid seed or seed strategy")
    deadline = cfg.get("question_deadline_seconds")
    if deadline is not None and (type(deadline) is not int or deadline <= 0):
        raise ValueError("Question deadline must be positive or null")
    order = cfg["question_order"]
    if len(order) != count or any(type(n) is not int for n in order) or set(order) != set(range(1, count + 1)):
        raise ValueError("Question order is not a permutation of the input")
    if cfg["mode"] == "direct" and (cfg.get("tools") or cfg.get("workflow")):
        raise ValueError("Direct does not use tools or a Workflow runtime")


def request(cfg, messages, role="main", tools=None, number=None):
    worker = role == "subagent"
    model = cfg["subagent_model"] if worker else cfg["model"]
    adapter = (cfg.get("subagent_adapter") or cfg["model_adapter"]) if worker else cfg["model_adapter"]
    params = deepcopy(cfg.get("subagent_parameters") or cfg["parameters"]) if worker else deepcopy(cfg["parameters"])
    extra = deepcopy(cfg.get("subagent_extra_body", {}) if worker else cfg.get("extra_body", {}))
    if RESERVED.intersection(params) or RESERVED.intersection(extra):
        raise ValueError("Reserved model request field")
    if adapter == "deepseek":
        thinking = params.pop("thinking", True)
        params["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if thinking:
            params.pop("temperature", None)
            params.pop("presence_penalty", None)
            params.setdefault("reasoning_effort", "high")
    elif adapter != "openai":
        raise ValueError("Unknown model adapter")
    if adapter != "deepseek":
        messages = [{k: v for k, v in message.items() if k not in {"reasoning", "reasoning_content", "reasoning_details"}} for message in messages]
    body = dict(params, **extra)
    body.update(model=model, messages=messages, stream=True, stream_options={"include_usage": True})
    if adapter == "openai" and not worker and cfg.get("seed") is not None:
        from .protocol import seed
        body["seed"] = seed(number, cfg["seed"]) if cfg.get("seed_strategy") == "per_question" and number is not None else cfg["seed"]
    if tools:
        body["tools"] = tools
    return body
