"""Managed research tools shared by formal runs and real-API demos."""
from contextlib import contextmanager
from copy import deepcopy
import os
import threading
from urllib.parse import urlsplit

from . import transport, configuration
from .research_tools import ToolService, definitions
from .research_web import secondary_prompt, tavily_endpoint, tavily_extract_endpoint
from .storage import atomic, read


def options(preflight_tools=False, config=None):
    config = config or configuration.load("forced")
    provider = os.environ.get("DYNAFLOW_SEARCH_PROVIDER") or "tavily"
    fetch = os.environ.get("DYNAFLOW_FETCH_PROVIDER") or "tavily"
    model = os.environ.get("DYNAFLOW_FETCH_MODEL") or config["model"]
    return {
        "tool_service_mode": "managed", "tools": definitions(), "tool_service_url": None,
        "tool_key_env": "DYNAFLOW_TOOL_API_KEY", "search_provider": provider,
        "fetch_provider": fetch, "tools_image": os.environ.get("DYNAFLOW_TOOLS_IMAGE") or "dynaflow-tools:py311-v1",
        "search_endpoint": tavily_endpoint() if provider == "tavily" else None,
        "fetch_endpoint": tavily_extract_endpoint() if fetch == "tavily" else "https://api.deepseek.com/v1/tools/fetch",
        "fetch_summary_model": model, "fetch_summary_url": os.environ.get("DYNAFLOW_FETCH_BASE_URL") or config["servers"][0]["url"],
        "fetch_summary_adapter": os.environ.get("DYNAFLOW_FETCH_ADAPTER") or config["model_adapter"],
        "fetch_summary_key_env": "DYNAFLOW_FETCH_API_KEY" if os.environ.get("DYNAFLOW_FETCH_API_KEY") else config["api_key_env"], "fetch_summary_max_tokens": 4096,
        "tools_preflight": preflight_tools,
        "research_environment": "Linux /work in a per-question container; Python 3.11, numpy 2.2.5, "
            "scipy 1.15.3, sympy 1.12.1, mpmath 1.3.0. Bash has no network. "
            "WebSearch: " + provider + "; WebFetch: " + fetch + " extraction + " + model + " summarization. "
            "Only supplied tools are available.",
    }


@contextmanager
def session(root, tasks, config, stop):
    cfg = deepcopy(config)
    w = cfg.get("workflow", {})
    if w.get("tool_service_mode") != "managed":
        yield cfg
        return
    providers = {w["search_provider"], w["fetch_provider"]}
    if "tavily" in providers and urlsplit(tavily_endpoint()).hostname == "api.tavily.com" and not os.environ.get("TAVILY_API_KEY"):
        raise ValueError("Set TAVILY_API_KEY in the private env file for web search/page extraction; see .env.example")
    if "deepseek" in providers and not os.environ.get("DEEPSEEK_API_KEY"):
        raise ValueError("Set DEEPSEEK_API_KEY for the selected web tools; DYNAFLOW_API_KEY configures model calls only")
    summary_key = transport.resolve_key(w["fetch_summary_key_env"], w["fetch_summary_url"])
    if w.get("search_endpoint") and w["search_endpoint"] != tavily_endpoint():
        raise ValueError("Search endpoint changed since prepare; create a new run")
    if w.get("fetch_endpoint") and w["fetch_provider"] == "tavily" and w["fetch_endpoint"] != tavily_extract_endpoint():
        raise ValueError("Fetch endpoint changed since prepare; create a new run")
    def summarize(task, instruction, page_url, content):
        adapter = w["fetch_summary_adapter"]
        params = {"max_tokens": w["fetch_summary_max_tokens"]}
        if adapter == "deepseek":
            params["thinking"] = False
        body = configuration.request({"model": w["fetch_summary_model"], "model_adapter": adapter,
            "parameters": params, "extra_body": {}, "seed": None},
            [{"role": "user", "content": secondary_prompt(content, instruction)}])
        text, receipt = transport.call(root, task["number"], {"id": "webfetch", "url": w["fetch_summary_url"]},
            body, summary_key, task["number"] == 0, role="webfetch", cancel_event=stop, idle_timeout=120)
        if receipt["truncated"] or not receipt["finish_reasons"] or receipt["finish_reasons"][-1] != "stop":
            raise ValueError("WebFetch summarization incomplete")
        return text
    probe = {"number": 0, "dataset": "tool-preflight", "task_id": "isolated-probe"}
    service = None
    variable = w["tool_key_env"]
    previous = os.environ.get(variable)
    try:
        service = ToolService(root, [probe, *tasks], w["search_provider"], summarize,
                              w["tools_image"], w["fetch_provider"])
        w["tool_service_url"] = service.start()
        os.environ[variable] = service.token
        atomic(root / "diagnostics/tool-runtime.json", {
            "service_mode": "managed", "tools_image_id": service.image_id,
            "search_provider": w["search_provider"], "fetch_provider": w["fetch_provider"],
            "fetch_summary_model": w["fetch_summary_model"],
        })
        saved = root / "diagnostics/tools-preflight.json"
        if w.get("tools_preflight") and not saved.exists():
            receipts = preflight(service, probe)
            atomic(saved, {"passed": True, "receipts": receipts})
        yield cfg
    finally:
        try:
            if service is not None:
                service.close()
        finally:
            if previous is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous


def preflight(service, task):
    receipts = []
    def execute(name, arguments, expect_error=False):
        result = service.execute({**task, "name": name, "arguments": arguments})
        receipts.append({"name": name, "arguments": arguments, "result": result})
        if bool(result.get("is_error")) != expect_error:
            raise ValueError("Preflight unexpected tool result: " + name)
        return result
    result = execute("Bash", {"command": "python -c \"import sys,numpy,scipy,sympy,mpmath,os; print(sys.version); print(numpy.__version__,scipy.__version__,sympy.__version__,mpmath.__version__); print(sympy.factorint(391)); assert not any(k in os.environ for k in ['DEEPSEEK_API_KEY','DYNAFLOW_CLOUD_API_KEY','DYNAFLOW_TOOL_API_KEY','TAVILY_API_KEY','EXA_API_KEY'])\""})
    assert "2.2.5 1.15.3 1.12.1 1.3.0" in result["output"]
    execute("Write", {"file_path": "/work/probe.txt", "content": "alpha\nbeta\n"})
    execute("Edit", {"file_path": "/work/probe.txt", "old_string": "beta", "new_string": "gamma"}, True)
    execute("Read", {"file_path": "/work/probe.txt"})
    execute("Edit", {"file_path": "/work/probe.txt", "old_string": "beta", "new_string": "gamma"})
    assert "/work/probe.txt" in execute("Glob", {"pattern": "*.txt"})["files"]
    assert "gamma" in execute("Grep", {"pattern": "gamma", "path": "/work", "output_mode": "content"})["output"]
    execute("Read", {"file_path": "/etc/passwd"}, True)
    execute("Bash", {"command": "python -c \"import os; os.symlink('/etc/passwd','/work/escape')\""})
    execute("Read", {"file_path": "/work/escape"}, True)
    execute("TodoWrite", {"todos": [{"content": "Check independent tools", "status": "completed",
                                    "activeForm": "Checking independent tools"}]})
    result = execute("WebSearch", {"query": "Python scientific computing NumPy", "num_results": 2})
    assert result["results"]
    result = execute("WebFetch", {"url": "https://www.python.org/", "prompt": "What programming language does this official website describe?"})
    assert "python" in result["content"].lower()
    return receipts

