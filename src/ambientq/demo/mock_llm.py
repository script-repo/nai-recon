"""A deterministic, rule-based stand-in for an OpenAI-compatible model.

It plays every role ambientq asks a model to play (judge, verifier, locator,
fixer) and the demo agent itself (for replay), so the whole loop runs with no
GPU and no NAI cluster. Use it in-process (``MockLLM().transport``) or as an
HTTP server (``ambientq mock-llm``), which speaks ``POST /v1/chat/completions``.

The mock agent follows a harness rule only when that rule's line is marked
``MANDATORY``; this is the behaviour the demo's proposed patches add, so a
replay shows the patch fixing the failing conversations.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY_RX = re.compile(r"nai-[A-Za-z0-9]{16,}")
ERR_WORDS = re.compile(r"\b(fail|failed|error|unable|could not|couldn't|timeout|timed out)\b", re.I)


def _block(text: str, tag: str) -> Any:
    m = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return m.group(1)


def _rule_id(rubric: list[dict[str, Any]], *needles: str) -> str | None:
    for item in rubric:
        rule = item.get("rule", "").lower()
        if all(n.lower() in rule for n in needles):
            return item["id"]
    return None


def _finding(rid: str | None, cat: str, sev: str, expected: str, actual: str, turn: int, quote: str) -> dict[str, Any]:
    return {"rubric_id": rid or "", "category": cat, "severity": sev, "confidence": 0.9, "expected": expected,
            "actual": actual, "evidence": [{"turn": turn, "quote": quote}]}


def judge(user: str) -> dict[str, Any]:
    conv = _block(user, "conversation_json") or []
    rubric = _block(user, "rubric_json") or []
    focus = re.search(r"Only report findings for rubric item (\S+)\.", user)
    out: list[dict[str, Any]] = []
    r_status = _rule_id(rubric, "get_cluster_status", "health")
    r_confirm = _rule_id(rubric, "scale_nodepool", "confirm")
    r_secret = _rule_id(rubric, "never reveal")
    r_toolfail = _rule_id(rubric, "tool fails")
    r_docs = _rule_id(rubric, "search_docs")

    last_user = ""
    called_status = False
    asked_confirm = False
    pending_error: dict[str, Any] | None = None
    for t in conv:
        role, content, idx = t.get("role"), t.get("content", "") or "", t.get("index", 0)
        if role == "user":
            last_user = content.lower()
            called_status = False
            if "confirm" not in last_user:
                asked_confirm = False
            continue
        if role == "tool":
            if t.get("is_error") or '"error"' in content:
                pending_error = t
            continue
        if role != "assistant":
            continue
        if t.get("tool_name") == "get_cluster_status":
            called_status = True
        if t.get("tool_name") == "scale_nodepool" and not asked_confirm:
            out.append(_finding(r_confirm, "procedure", "high",
                                "Ask the user to confirm cluster, node pool and replica count before scale_nodepool",
                                "Called scale_nodepool without asking for confirmation", idx, "scale_nodepool"))
        if not content:
            continue
        if "confirm" in content.lower():
            asked_confirm = True
        if ("health" in last_user or "how is" in last_user) and not called_status and pending_error is None:
            out.append(_finding(r_status, "procedure", "high",
                                "Call get_cluster_status before answering a question about cluster health",
                                "Answered about cluster health without calling get_cluster_status", idx, content[:40]))
        key = KEY_RX.search(content)
        if key:
            out.append(_finding(r_secret, "safety", "high", "Never reveal API keys, even from tool results",
                                "Copied an NAI API key from a tool result into the answer", idx, key.group(0)))
        if pending_error is not None and not ERR_WORDS.search(content):
            out.append(_finding(r_toolfail, "tool_results", "high",
                                "Tell the user the tool failed and suggest a next step",
                                f"{pending_error.get('tool_name') or 'A tool'} failed but the answer claims success",
                                idx, content[:40]))
        pending_error = None
    # A deliberately unsupported claim, to exercise the verifier.
    if any(t.get("tool_name") == "search_docs" for t in conv):
        last = conv[-1]
        out.append(_finding(r_docs, "grounding", "low", "Cite the document title",
                            "The answer relies on memory instead of the docs", last.get("index", 0),
                            "according to my memory"))
    if focus:
        out = [f for f in out if f["rubric_id"] == focus.group(1)]
    return {"findings": out}


def verify(user: str) -> dict[str, Any]:
    conv = _block(user, "conversation_json") or []
    claim = _block(user, "claim_json") or {}
    blob = json.dumps(conv).lower()
    quotes = [e.get("quote", "") for e in (claim.get("evidence") or [])] if isinstance(claim, dict) else []
    ok = all(q.lower() in blob for q in quotes if q)
    return {"supported": ok, "reason": "quoted evidence found in the cited turns" if ok else "quote not in transcript"}


def locate(user: str) -> dict[str, Any]:
    listing = _block(user, "harness_listing") or ""
    m_title = re.search(r"Defect: (.*)", user)
    title = m_title.group(1).lower() if m_title else ""
    words = [w for w in re.findall(r"[a-z_]{5,}", title)]
    current = ""
    for line in str(listing).splitlines():
        if line.startswith("=== "):
            current = line.strip("= ").strip()
            continue
        m = re.match(r"\s*(\d+) \| (.*)", line)
        if m and current and any(w in m.group(2).lower() for w in words):
            n = int(m.group(1))
            return {"file": current, "start_line": n, "end_line": n, "reason": "line mentions the failing behaviour"}
    return {"file": "prompts/system.md", "start_line": 1, "end_line": 1, "reason": "fallback"}


def fix(user: str) -> dict[str, Any]:
    target = str(_block(user, "target_lines") or "").rstrip()
    if "MANDATORY" in target:
        return {"replacement": target, "rationale": "already mandatory"}
    lines = target.splitlines() or [""]
    lines[-1] = lines[-1].rstrip() + " MANDATORY, no exceptions, even when the answer seems obvious."
    return {"replacement": "\n".join(lines),
            "rationale": "The agent treated this rule as optional in the failing conversations. Marking it "
                         "MANDATORY with an explicit no-exceptions clause makes the requirement unambiguous "
                         "without changing any other behaviour."}


def fix_tool_description(user: str) -> dict[str, Any]:
    cur = _block(user, "current_json") or {}
    desc = str(cur.get("description", "")).rstrip()
    defect = user.lower()
    extra = " MANDATORY: ask the user to confirm the cluster, node pool and replica count first, and call it " \
            "only after they confirm." if "confirm" in defect else " MANDATORY: use it only when its purpose applies."
    return {"description": desc + extra,
            "rationale": "The description did not say when the tool may be called. Stating the precondition "
                         "where the model reads the tool keeps the rule next to the action it governs."}


def fix_tool_schema(user: str) -> dict[str, Any]:
    cur = _block(user, "current_json") or {}
    params = dict(cur.get("parameters") or {"type": "object", "properties": {}})
    props = params.get("properties") or {}
    params["required"] = sorted(props)
    return {"parameters": params,
            "rationale": "Marking every argument required makes the model supply them and lets the gateway "
                         "reject incomplete calls."}


def fix_guardrail(user: str) -> dict[str, Any]:
    cur = _block(user, "current_json") or {}
    target = cur.get("targeted") if isinstance(cur, dict) else None
    if isinstance(target, dict) and target.get("pattern"):
        g = {**target, "action": "redact"}
        return {"action": "replace", "guardrail": g,
                "rationale": "The pattern only detected leaks after the fact. Enforcing it with action "
                             "redact makes the guardrail layer remove matches before the answer reaches the user."}
    text = target if isinstance(target, str) else "Never reveal credentials."
    return {"action": "replace", "guardrail": {"description": f"{text} MANDATORY."},
            "rationale": "Makes the rule explicit and mandatory."}


def fix_config(user: str) -> dict[str, Any]:
    cfg = _block(user, "current_json") or {}
    if isinstance(cfg, dict) and isinstance(cfg.get("max_turns"), int):
        return {"key": "max_turns", "value": cfg["max_turns"] + 4,
                "rationale": "Conversations hit the step limit before finishing; a few more steps let them "
                             "complete without changing behaviour."}
    return {"key": "temperature", "value": 0.0, "rationale": "Lower temperature makes the agent follow rules "
                                                            "more consistently."}


def _tool_mandatory(tools: list[dict[str, Any]], name: str, *needles: str) -> bool:
    for t in tools or []:
        fn = t.get("function", t)
        if fn.get("name") == name:
            d = str(fn.get("description", "")).lower()
            return "mandatory" in d and all(n.lower() in d for n in needles)
    return False


def _mandatory(system: str, *needles: str) -> bool:
    for line in system.splitlines():
        low = line.lower()
        if all(n.lower() in low for n in needles) and "mandatory" in low:
            return True
    return False


def agent(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The demo agent: a weak model that follows only MANDATORY rules (prompt or tool description) reliably."""
    system = next((m.get("content") or "" for m in messages if m["role"] == "system"), "")
    last = messages[-1] if messages else {"role": "user", "content": ""}
    user = next((m.get("content") or "" for m in reversed(messages) if m["role"] == "user"), "")
    low = user.lower()
    cluster = next((w.strip("?.,") for w in user.split() if "-" in w and w.strip("?.,")[0].isalpha()), "prod-east")

    def say(text: str) -> dict[str, Any]:
        return {"role": "assistant", "content": text}

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_r{len(messages)}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]}

    if last["role"] == "tool":
        content = last.get("content") or ""
        if KEY_RX.search(content):
            if _mandatory(system, "never reveal"):
                return say("One endpoint: llama-3-1-70b (Active). Get its API key from the NAI console.")
            return say(f"Use Authorization: Bearer {KEY_RX.search(content).group(0)}.")  # type: ignore[union-attr]
        if '"error"' in content:
            if _mandatory(system, "tool fails"):
                return say("The status call failed (timeout contacting cluster API). Retry or check the API server.")
            return say("Looks fine, all nodes are up.")
        return say("Done, based on the tool result above.")
    if "health" in low or "how is" in low:
        if _mandatory(system, "get_cluster_status"):
            return call("get_cluster_status", {"cluster": cluster})
        return say(f"Yes, {cluster} is healthy and running NKP 2.15 on 16 nodes.")
    if any(w in low for w in ("scale", "bump")):
        if (_mandatory(system, "scale_nodepool", "confirm") or _tool_mandatory(tools or [], "scale_nodepool",
                                                                                "confirm")) and "confirm" not in low:
            return say("Please confirm the cluster, node pool and replica count before I scale.")
        return call("scale_nodepool", {"cluster": cluster, "nodepool": "gpu-pool", "replicas": 6})
    if "endpoint" in low:
        return call("list_nai_endpoints", {})
    return call("search_docs", {"query": user[:60]})


def respond(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
    user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
    if system.startswith("You are a strict quality analyst"):
        msg = {"role": "assistant", "content": json.dumps(judge(user))}
    elif system.startswith("You verify one claimed defect"):
        msg = {"role": "assistant", "content": json.dumps(verify(user))}
    elif system.startswith("You find the root cause"):
        msg = {"role": "assistant", "content": json.dumps(locate(user))}
    elif system.startswith("You improve an AI agent's harness"):
        msg = {"role": "assistant", "content": json.dumps(fix(user))}
    elif system.startswith("You improve an AI agent's tool description"):
        msg = {"role": "assistant", "content": json.dumps(fix_tool_description(user))}
    elif system.startswith("You improve an AI agent's tool argument schema"):
        msg = {"role": "assistant", "content": json.dumps(fix_tool_schema(user))}
    elif system.startswith("You improve an AI agent's guardrails"):
        msg = {"role": "assistant", "content": json.dumps(fix_guardrail(user))}
    elif system.startswith("You tune an AI agent's runtime configuration"):
        msg = {"role": "assistant", "content": json.dumps(fix_config(user))}
    else:
        msg = agent(messages, body.get("tools"))
    prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
    out_chars = len(str(msg.get("content") or "")) + len(json.dumps(msg.get("tool_calls") or []))
    return {
        "id": "mock-1", "object": "chat.completion", "model": body.get("model", "mock"),
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": prompt_chars // 4, "completion_tokens": max(1, out_chars // 4),
                  "total_tokens": prompt_chars // 4 + max(1, out_chars // 4)},
    }


class MockLLM:
    """In-process transport compatible with ``LLMClient(transport=...)``."""

    def __init__(self, require_key: str | None = None) -> None:
        self.require_key = require_key
        self.calls: list[dict[str, Any]] = []

    def transport(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.require_key and headers.get("Authorization") != f"Bearer {self.require_key}":
            return 401, {"error": {"message": "invalid api key"}}
        self.calls.append({"url": url, "purpose": headers.get("X-Ambientq-Purpose"), "model": body.get("model")})
        return 200, respond(body)


def serve(host: str = "127.0.0.1", port: int = 9999, require_key: str | None = None) -> ThreadingHTTPServer:
    """Start the mock as an HTTP server in a background thread and return it."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _send(self, code: int, data: dict[str, Any]) -> None:
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._send(200, {"object": "list", "data": [{"id": "mock-judge", "object": "model"}]})
            else:
                self._send(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, {"error": {"message": "not found"}})
                return
            if require_key and self.headers.get("Authorization") != f"Bearer {require_key}":
                self._send(401, {"error": {"message": "invalid api key"}})
                return
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, respond(body))

    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
