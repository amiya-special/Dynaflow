"""Render system messages for the independent evaluator."""
import platform
from .data import asset
from .workflow_protocol import runtime_model

VERSION = "standalone-system-v1"


def render(role, substitutions):
    templates = {"main": "workflow_main.system.txt", "worker": "workflow_worker.system.txt"}
    path = asset(templates[role])
    text = path.read_text(encoding="utf-8")
    for name, value in substitutions.items():
        marker = "{{" + name + "}}"
        if marker not in text:
            raise ValueError("Missing system prompt substitution: " + name)
        text = text.replace(marker, value)
    return text


def main_prompt(cfg, tools):
    engine = cfg.get("workflow", {}).get("engine_mode", "unconfigured")
    execution = ("Linux Docker container, working directory /work; network disabled"
                 if engine == "docker" else "not configured")
    environment = (
        f" - Python evaluation host platform: {platform.system()}.\n"
        f" - Workflow JavaScript execution: {execution}.\n"
        " - No shell or filesystem tool is directly available to the main agent.\n"
        f" - You are powered by the model {cfg.get('model', 'unspecified')}.\n"
        f" - Workflow subagent model: {runtime_model(cfg) if cfg.get('subagent_model') else 'unspecified'}.\n"
        " - External capabilities are limited to the explicitly configured subagent tools.")
    return render("main", {"MAIN_TOOLS": ", ".join(t["function"]["name"] for t in tools),
                           "HOST_ENVIRONMENT": environment})


def worker_prompt(cfg, tools, structured=False):
    names = ", ".join(t["function"]["name"] for t in tools) or "none"
    environment = (
        "<env>\n"
        "Execution role: Workflow sub-agent called by the Python evaluation host\n"
        f"Model: {runtime_model(cfg)}\n"
        "Direct shell/filesystem access: not implied; consult the supplied tool definitions\n"
        "Tool execution environment: " + cfg.get("workflow", {}).get("research_environment",
            "determined by the configured tool service, if any") + "\n"
        "</env>")
    delivery = ("Delivery contract for this host: deliver your final result through the supplied StructuredOutput tool, "
                "matching its JSON Schema. Do not combine that final delivery with other tool calls."
                if structured else
                "Delivery contract for this host: return your final result as text.")
    return render("worker", {"WORKER_ENVIRONMENT": environment, "WORKER_TOOLS": names,
                             "DELIVERY_INSTRUCTION": delivery})
