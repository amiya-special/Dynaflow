"""Explicit search adapters and public-page fetching; credentials stay on host."""
import ipaddress
import json
import os
import re
import socket
import time
import threading
from collections import OrderedDict
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from .data import asset
from .storage import read


DEFAULT_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def tavily_endpoint():
    endpoint = os.environ.get("TAVILY_ENDPOINT_URL") or DEFAULT_TAVILY_SEARCH_URL
    u = urlsplit(endpoint)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("TAVILY_ENDPOINT_URL must be a credential-free HTTPS URL")
    return endpoint if endpoint.endswith("/search") else endpoint.rstrip("/") + "/search"


def tavily_extract_endpoint():
    # Match the original shared tavilyEndpointUrl setting's /search → /extract rule.
    return tavily_endpoint().removesuffix("/search") + "/extract"


def secondary_prompt(content, instruction):
    settings = read(asset("webfetch.json"))
    # One-pass replacement prevents page text containing a template marker from being rewritten.
    values = {"markdownContent": content, "prompt": instruction,
              "guidelines": settings["guidelines"]}
    return re.sub(r"\$\{(markdownContent|prompt|guidelines)\}", lambda m: values[m[1]], settings["template"])


def truncate_markdown(content, limit):
    encoded = content.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return content, False
    # JS slice counts UTF-16 units; avoid emitting a dangling surrogate in UTF-8 requests.
    return encoded[:limit * 2].decode("utf-16-le", errors="ignore") + "\n\n[Content truncated due to length...]", True


def post(url, payload, key=None, timeout=90):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        with urlopen(Request(url, json.dumps(payload).encode(), headers), timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Provider response exceeds 8 MiB")
            text = raw.decode("utf-8")
            if text.lstrip().startswith("data:") or "\ndata:" in text:
                events = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]
                return next(e for e in reversed(events) if "result" in e or "error" in e)
            return json.loads(text)
    except HTTPError as error:
        # Never echo an arbitrary provider body containing possible credentials.
        raise RuntimeError(f"Web provider HTTP {error.code}") from None


def public_url(value):
    u = urlsplit(value)
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.port not in {None, 80, 443}:
        raise ValueError("Expected a public HTTP(S) URL without credentials or nonstandard port")
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Private/local addresses are not fetchable")
    return u._replace(scheme="https", fragment="").geturl()


def domain_matches(url, domain):
    host = (urlsplit(url).hostname or "").lower()
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


class WebTools:
    def __init__(self, provider, summarize, fetch_provider="tavily"):
        if provider not in {"deepseek", "exa", "tavily"} or fetch_provider not in {"deepseek", "tavily"}:
            raise ValueError("Unsupported explicit web provider")
        self.provider, self.summarize, self.fetch_provider = provider, summarize, fetch_provider
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.cache_lock = threading.Lock()
        self.settings = read(asset("webfetch.json"))

    def cache_get(self, url):
        with self.cache_lock:
            for k, (created, page) in list(self.cache.items()):
                if time.monotonic() - created >= self.settings["cache_ttl_seconds"]:
                    self.cache.pop(k)
                    self.cache_bytes -= page["bytes"]
            if url not in self.cache:
                return None
            self.cache.move_to_end(url)
            return self.cache[url][1]

    def cache_put(self, url, page):
        with self.cache_lock:
            old = self.cache.pop(url, None)
            if old:
                self.cache_bytes -= old[1]["bytes"]
            if page["bytes"] > self.settings["cache_max_bytes"]:
                return
            while self.cache and self.cache_bytes + page["bytes"] > self.settings["cache_max_bytes"]:
                _, (_, discarded) = self.cache.popitem(last=False)
                self.cache_bytes -= discarded["bytes"]
            self.cache[url] = (time.monotonic(), page)
            self.cache_bytes += page["bytes"]

    def search(self, a):
        query, count = a["query"], int(a.get("num_results", 8))
        if not 1 <= count <= 50:
            raise ValueError("num_results must be between 1 and 50")
        if self.provider != "exa":
            for field, default in (("livecrawl", "fallback"), ("search_type", "auto"), ("context_max_characters", 10000)):
                if field in a and a[field] != default:
                    raise ValueError(f"{field} is supported only by the Exa adapter")
        if self.provider == "deepseek":
            if len(query) > 128:
                raise ValueError("DeepSeek search accepts at most 128 characters per query")
            obj = post("https://api.deepseek.com/v1/tools/search", {"query": query, "count": count},
                       os.environ["DEEPSEEK_API_KEY"])
            rows = obj.get("results", [])
        elif self.provider == "tavily":
            endpoint = tavily_endpoint()
            payload = {"query": query, "search_depth": "basic", "max_results": count,
                       "include_domains": a.get("allowed_domains", []),
                       "exclude_domains": a.get("blocked_domains", [])}
            # Custom proxies receive no official API key.
            if urlsplit(endpoint).hostname == "api.tavily.com":
                if not os.environ.get("TAVILY_API_KEY"):
                    raise ValueError("Official Tavily endpoint requires TAVILY_API_KEY")
                payload["api_key"] = os.environ["TAVILY_API_KEY"]
            obj = post(endpoint, payload, timeout=30)
            rows = [{"title": r.get("title", ""), "url": r.get("url", ""),
                     "snippet": r.get("content", "")} for r in obj.get("results", [])]
        else:
            obj = post("https://mcp.exa.ai/mcp",
                       {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "web_search_exa", "arguments": {
                            "query": query, "type": a.get("search_type", "auto"), "numResults": count,
                            "livecrawl": a.get("livecrawl", "fallback"),
                            "contextMaxCharacters": a.get("context_max_characters", 10000)}}},
                       os.environ.get("EXA_API_KEY"))
            if obj.get("error") or obj.get("result", {}).get("isError"):
                raise ValueError("Exa returned a tool error")
            text = "\n".join(c.get("text", "") for c in obj.get("result", {}).get("content", []))
            rows = []
            for block in re.split(r"\n---+\n", text):
                title = re.search(r"Title:\s*(.+)", block)
                url = re.search(r"URL:\s*(https?://\S+)", block)
                content = re.search(r"Content:\s*([\s\S]*)", block)
                if url:
                    rows.append({"title": title[1] if title else url[1], "url": url[1],
                                 "snippet": content[1][:300] if content else ""})
            if not rows:
                rows = [{"title": title, "url": url, "snippet": ""}
                        for title, url in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", text)]
        rows = [r for r in rows if urlsplit(r.get("url", "")).scheme in {"http", "https"}]
        allowed, blocked = a.get("allowed_domains", []), a.get("blocked_domains", [])
        rows = [r for r in rows if (not allowed or any(domain_matches(r["url"], d) for d in allowed))
                and not any(domain_matches(r["url"], d) for d in blocked)][:count]
        return {"content": f'Web search results for query: "{query}"\nLinks:\n' +
                "\n".join(f'  - [{r.get("title", r["url"])}]({r["url"]}): {r.get("snippet", "")}' for r in rows),
                "provider": self.provider, "results": rows,
                "domain_filter": "client-side", "provider_fallback": False}

    def fetch(self, a, task):
        start = time.monotonic()
        url = public_url(a["url"])
        page = self.cache_get(url)
        hit = page is not None
        if not hit:
            if self.fetch_provider == "tavily":
                endpoint = tavily_extract_endpoint()
                payload = {"urls": [url]}
                if urlsplit(endpoint).hostname == "api.tavily.com":
                    if not os.environ.get("TAVILY_API_KEY"):
                        raise ValueError("Official Tavily endpoint requires TAVILY_API_KEY")
                    payload["api_key"] = os.environ["TAVILY_API_KEY"]
                response = post(endpoint, payload, timeout=self.settings["fetch_timeout_seconds"])
                content = response.get("raw_content") or ""
                if not content.strip():
                    rows = response.get("results", [])
                    content = rows[0].get("raw_content", "") if rows else ""
                page = {"content": content}
            else:
                page = post("https://api.deepseek.com/v1/tools/fetch", {"url": url},
                            os.environ["DEEPSEEK_API_KEY"])
            if not isinstance(page.get("content"), str) or not page["content"].strip():
                raise ValueError("Fetch provider returned no page content")
            page["bytes"] = len(page["content"].encode("utf8"))
            self.cache_put(url, page)
        content = page["content"]
        processed = bool(a["prompt"].strip())
        model_content, truncated = truncate_markdown(content, self.settings["max_markdown_utf16_units"])
        summary = self.summarize(task, a["prompt"], url, model_content) if processed else content
        return {"url": a["url"], "fetch_url": url, "bytes": page["bytes"], "code": 200, "codeText": "OK",
                "result": summary, "content": summary, "durationMs": round((time.monotonic() - start) * 1000),
                "provider": self.fetch_provider, "endpoint": tavily_extract_endpoint() if self.fetch_provider == "tavily" else "https://api.deepseek.com/v1/tools/fetch",
                "cache_hit": hit, "source_truncated": processed and truncated, "summary_applied": processed,
                "redirect_policy": "provider-managed; no synthesized redirect notification", "provider_fallback": False}
