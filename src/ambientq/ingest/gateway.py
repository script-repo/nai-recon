"""Gateway ingest: OpenAI-compatible proxy logs -> GenAI-semconv LLM spans.

A proxy log has one JSON object per request/response pair (LiteLLM spend /
standard logging payloads, Kong AI Proxy http-log entries, Envoy AI Gateway
JSON access logs, any API gateway that logs bodies). Each pair becomes one
``chat`` span carrying ``gen_ai.input.messages`` (the request's messages) and
``gen_ai.output.messages`` (the response choice), so the rest of the pipeline
(``store.ingest_spans``, ``conversation.build_conversation``) is unchanged:
the history an agent re-sends on every call is de-duplicated there, tool calls
in one response and tool results in the next request become tool turns.

Where each logical field lives is a list of dotted paths per preset
(``PRESETS``), overridable with ``gateway.mappings``. A path segment may
itself contain dots (Envoy logs keys like ``gen_ai.request.model``), list
indexes are numbers (``choices.0.message``), dict keys match case-insensitively
(headers), and a JSON string met on the way is parsed (Kong logs bodies as
strings). See ``docs/GATEWAY.md``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ambientq.models import Span

FIELDS = ("request", "response", "messages", "model", "response_model", "session", "agent", "team",
          "start_time", "end_time", "duration_ms", "usage_in", "usage_out", "status", "error", "request_id")

Mappings = dict[str, list[str]]

_GENERIC: Mappings = {
    "request": ["request", "request_body", "body.request"],
    "response": ["response", "response_body", "body.response"],
    "messages": ["request.messages", "request_body.messages", "messages"],
    "model": ["request.model", "request_body.model", "model"],
    "response_model": ["response.model", "response_body.model"],
    "session": ["session_id", "conversation_id", "metadata.session_id", "metadata.conversation_id",
                "request_headers.x-session-id", "headers.x-session-id", "request.headers.x-session-id",
                "request_headers.agent-session-id", "request.metadata.session_id"],
    "agent": ["request_headers.x-agent-name", "headers.x-agent-name", "request.headers.x-agent-name",
              "agent", "agent_name", "metadata.agent_name", "key_alias", "user", "request.user"],
    "team": ["team", "request_headers.x-team", "headers.x-team", "metadata.team"],
    "start_time": ["start_time", "startTime", "timestamp", "started_at", "time"],
    "end_time": ["end_time", "endTime"],
    "duration_ms": ["duration_ms", "latency_ms", "duration"],
    "usage_in": ["response.usage.prompt_tokens", "response.usage.input_tokens", "usage.prompt_tokens",
                 "usage.input_tokens", "prompt_tokens"],
    "usage_out": ["response.usage.completion_tokens", "response.usage.output_tokens",
                  "usage.completion_tokens", "usage.output_tokens", "completion_tokens"],
    "status": ["status", "status_code", "response_code", "response.status"],
    "error": ["error", "error_message", "response.error"],
    "request_id": ["request_id", "id", "response.id", "request_headers.x-request-id"],
}

# LiteLLM StandardLoggingPayload (logging callbacks) and LiteLLM_SpendLogs rows.
_LITELLM: Mappings = {
    "request": ["proxy_server_request"],
    "response": ["response"],
    "messages": ["messages", "proxy_server_request.messages"],
    "model": ["model_group", "model", "proxy_server_request.model"],
    "response_model": ["response.model"],
    "session": ["metadata.requester_custom_headers.x-session-id", "metadata.session_id",
                "litellm_session_id", "metadata.litellm_session_id", "session_id",
                "proxy_server_request.metadata.session_id", "proxy_server_request.litellm_session_id"],
    "agent": ["metadata.requester_custom_headers.x-agent-name", "metadata.agent_name",
              "metadata.user_api_key_alias", "metadata.user_api_key_user_id", "user", "end_user"],
    "team": ["metadata.requester_custom_headers.x-team", "metadata.user_api_key_team_alias",
             "metadata.user_api_key_team_id", "team_id"],
    "start_time": ["startTime", "start_time"],
    "end_time": ["endTime", "end_time"],
    "duration_ms": [],
    "usage_in": ["prompt_tokens", "response.usage.prompt_tokens"],
    "usage_out": ["completion_tokens", "response.usage.completion_tokens"],
    "status": ["status"],
    "error": ["error_str", "error_information.error_message"],
    "request_id": ["id", "request_id"],
}

# Envoy AI Gateway (now "Agent Router") JSON access log, per its documented format. The
# documented log has no bodies: request/response only resolve if you add them yourself.
_ENVOY: Mappings = {
    "request": ["request_body", "request"],
    "response": ["response_body", "response"],
    "messages": ["request_body.messages", "request.messages"],
    "model": ["gen_ai.request.model"],
    "response_model": ["gen_ai.response.model"],
    "session": ["session.id", "agent-session-id", "x-session-id"],
    "agent": ["x-agent-name", "gen_ai.agent.name"],
    "team": ["x-team"],
    "start_time": ["start_time"],
    "end_time": [],
    "duration_ms": ["duration"],
    "usage_in": ["gen_ai.usage.input_tokens"],
    "usage_out": ["gen_ai.usage.output_tokens"],
    "status": ["response_code"],
    "error": ["response_code_details"],
    "request_id": ["x-request-id"],
}

# Kong AI Proxy with logging.log_payloads + log_statistics, as written by http-log / file-log.
_KONG: Mappings = {
    "request": ["ai.proxy.payload.request"],
    "response": ["ai.proxy.payload.response"],
    "messages": ["ai.proxy.payload.request.messages"],
    "model": ["ai.proxy.meta.request_model"],
    "response_model": ["ai.proxy.meta.response_model"],
    "session": ["request.headers.x-session-id", "request.headers.agent-session-id"],
    "agent": ["request.headers.x-agent-name", "consumer.username", "consumer.custom_id"],
    "team": ["request.headers.x-team"],
    "start_time": ["started_at"],
    "end_time": [],
    "duration_ms": ["latencies.request"],
    "usage_in": ["ai.proxy.usage.prompt_tokens", "ai.proxy.usage.input_tokens"],
    "usage_out": ["ai.proxy.usage.completion_tokens", "ai.proxy.usage.output_tokens"],
    "status": ["response.status"],
    "error": [],
    "request_id": ["request.id", "request.headers.x-request-id"],
}

PRESETS: dict[str, Mappings] = {
    "generic": _GENERIC,
    "litellm": _LITELLM,
    "envoy-ai-gateway": _ENVOY,
    "kong": _KONG,
    # Nutanix publishes no per-request log export format for its Agent Gateway (Oct 2026):
    # an alias of the generic preset until it does (docs/GATEWAY.md).
    "nai-gateway": _GENERIC,
}
FORMATS = ("proxy-log", *PRESETS)

_MISSING = object()


# --- field access ----------------------------------------------------------------

def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def _child(cur: Any, key: str) -> Any:
    cur = _parse_json(cur)
    if isinstance(cur, list):
        if key.isdigit() and int(key) < len(cur):
            return cur[int(key)]
        return _MISSING
    if not isinstance(cur, Mapping):
        return _MISSING
    if key in cur:
        return cur[key]
    low = key.lower()
    for k, v in cur.items():
        if isinstance(k, str) and k.lower() == low:
            return v
    return _MISSING


def lookup(rec: Any, path: str) -> Any:
    """Resolve a dotted path; the longest key matching a prefix of the rest wins."""
    parts = path.split(".")

    def walk(cur: Any, i: int) -> Any:
        if i == len(parts):
            return cur
        for j in range(len(parts), i, -1):
            nxt = _child(cur, ".".join(parts[i:j]))
            if nxt is not _MISSING:
                found = walk(nxt, j)
                if found is not _MISSING:
                    return found
        return _MISSING

    out = walk(rec, 0)
    return None if out is _MISSING else _parse_json(out)


def first(rec: Any, paths: Iterable[str]) -> Any:
    """The first non-empty value among ``paths``."""
    for p in paths:
        v = lookup(rec, p)
        if v not in (None, "", [], {}):
            return v
    return None


# --- presets and mappings -------------------------------------------------------

def detect_preset(rec: Mapping[str, Any]) -> str:
    if isinstance(rec.get("ai"), Mapping) and "proxy" in rec["ai"]:
        return "kong"
    if any(k in rec for k in ("proxy_server_request", "call_type", "model_group", "litellm_session_id")):
        return "litellm"
    if "gen_ai.request.model" in rec or "response_code" in rec:
        return "envoy-ai-gateway"
    return "generic"


def is_proxy_log(rec: Mapping[str, Any]) -> bool:
    """True for records that look like one proxy-logged request/response pair.

    Conservative so it never claims an OTLP export, a simple conversation record
    (``messages`` without a ``response``) or a LangChain run.
    """
    if "resourceSpans" in rec or "resource_spans" in rec or "run_type" in rec:
        return False
    ai = rec.get("ai")
    if isinstance(ai, Mapping) and isinstance(ai.get("proxy"), Mapping) and "payload" in ai["proxy"]:
        return True
    if "response" in rec and any(k in rec for k in ("request", "messages", "proxy_server_request")):
        return True
    if "request_body" in rec and "response_body" in rec:
        return True
    return "choices" in rec and "messages" in rec


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else [str(x) for x in v]


def resolve_mappings(preset: str = "generic", mappings: Mapping[str, Any] | None = None) -> Mappings:
    """The preset's paths with ``mappings`` (field -> path or paths) placed first.

    A custom mapping is tried before the preset's own paths for that field. To replace
    a field's paths entirely, give the mapping under ``"=<field>"`` (``{"=session": []}``
    turns session lookup off, so every pair is grouped by the fallback hash).
    """
    if preset not in PRESETS:
        raise ValueError(f"unknown gateway preset {preset!r} (one of {', '.join(PRESETS)})")
    out = {f: list(PRESETS[preset].get(f, [])) for f in FIELDS}
    for key, paths in (mappings or {}).items():
        replace = key.startswith("=")
        f = key.lstrip("=")
        if f not in FIELDS:
            raise ValueError(f"unknown gateway mapping field {f!r} (one of {', '.join(FIELDS)})")
        out[f] = _as_list(paths) if replace else _as_list(paths) + [p for p in out[f] if p not in _as_list(paths)]
    return out


def mappings_from_config(section: Mapping[str, Any] | None) -> dict[str, Any]:
    """User mappings from the ``gateway`` config section, ``session_fields`` first in ``session``."""
    section = section or {}
    m: dict[str, Any] = {k: _as_list(v) for k, v in (section.get("mappings") or {}).items()}
    fields = _as_list(section.get("session_fields"))
    if fields:
        key = "=session" if "=session" in m else "session"
        m[key] = fields + [p for p in m.get(key, []) if p not in fields]
    return m


# --- conversion -----------------------------------------------------------------

def _ts_ns(value: Any) -> int:
    """Epoch seconds, milliseconds, microseconds or nanoseconds, or an ISO-8601 string."""
    if value in (None, ""):
        return 0
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9)
    v = float(value)
    for limit, scale in ((1e11, 1e9), (1e14, 1e6), (1e17, 1e3)):
        if v < limit:
            return int(v * scale)
    return int(v)


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _hash(*parts: Any, n: int = 32) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) if isinstance(p, Mapping) else str(p) for p in content)
    return "" if content is None else str(content)


def _looks_like_error(content: Any) -> bool:
    """A tool result whose JSON body has a top-level ``error`` key (gateways carry no error flag)."""
    body = _parse_json(content)
    return isinstance(body, Mapping) and bool(body.get("error"))


def _input_messages(raw: Any) -> list[dict[str, Any]]:
    msgs = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, Mapping):
            continue
        m = dict(m)
        if m.get("role") == "tool" and "is_error" not in m and _looks_like_error(m.get("content")):
            m["is_error"] = True
        msgs.append(m)
    return msgs


def _output_messages(resp: Any) -> list[dict[str, Any]]:
    """The response's first choice as an assistant message (chat completion, text or message)."""
    if resp in (None, "", {}):
        return []
    if isinstance(resp, str):
        return [{"role": "assistant", "content": resp}]
    if not isinstance(resp, Mapping):
        return []
    choices = resp.get("choices")
    if isinstance(choices, list) and choices:
        c = choices[0] if isinstance(choices[0], Mapping) else {}
        msg = c.get("message") or c.get("delta") or {"content": c.get("text", "")}
        return [{"role": "assistant", **dict(msg)}]
    if "role" in resp or "tool_calls" in resp:
        return [{"role": "assistant", **dict(resp)}]
    return []


def _is_error(status: Any, error: Any) -> bool:
    if error not in (None, "", {}, []):
        return True
    if isinstance(status, str):
        s = status.strip().lower()
        if s in ("failure", "failed", "error"):
            return True
        return s.isdigit() and int(s) >= 400
    return isinstance(status, (int, float)) and not isinstance(status, bool) and status >= 400


def fallback_session(agent: str, messages: list[dict[str, Any]]) -> str:
    """Stable id from (agent, system prompt, first user message) when no session id is logged."""
    system = next((_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")), "")
    user = next((_text(m.get("content")) for m in messages if m.get("role") == "user"), "")
    return "gw-" + _hash(agent, " ".join(system.split()), " ".join(user.split()), n=16)


def record_to_span(rec: Mapping[str, Any], m: Mappings, index: int = 0) -> Span | None:
    """One proxy-log record -> one GenAI ``chat`` span; ``None`` when it carries no messages."""
    messages = _input_messages(first(rec, m["messages"]) or lookup(first(rec, m["request"]), "messages"))
    resp = first(rec, m["response"])
    outputs = _output_messages(resp)
    if not messages and not outputs:
        return None
    agent = str(first(rec, m["agent"]) or "unknown")
    session = first(rec, m["session"])
    conv_id = str(session) if session else fallback_session(agent, messages)
    start = _ts_ns(first(rec, m["start_time"])) or index
    end = _ts_ns(first(rec, m["end_time"]))
    if not end:
        end = start + _int(first(rec, m["duration_ms"])) * 1_000_000
    model = first(rec, m["model"]) or lookup(first(rec, m["request"]), "model")
    resp_model = first(rec, m["response_model"]) or (resp.get("model") if isinstance(resp, Mapping) else None)
    usage: Mapping[str, Any] = {}
    if isinstance(resp, Mapping) and isinstance(resp.get("usage"), Mapping):
        usage = resp["usage"]
    tin = _int(first(rec, m["usage_in"]) or usage.get("prompt_tokens") or usage.get("input_tokens"))
    tout = _int(first(rec, m["usage_out"]) or usage.get("completion_tokens") or usage.get("output_tokens"))
    error = first(rec, m["error"])
    failed = _is_error(first(rec, m["status"]), error)
    rid = first(rec, m["request_id"])
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.conversation.id": conv_id,
        "gen_ai.agent.name": agent,
        "gen_ai.input.messages": json.dumps(messages, default=str),
        "gen_ai.output.messages": json.dumps(outputs, default=str),
        "gen_ai.usage.input_tokens": tin,
        "gen_ai.usage.output_tokens": tout,
        "ambientq.source": "gateway",
    }
    if model:
        attrs["gen_ai.request.model"] = str(model)
    if resp_model:
        attrs["gen_ai.response.model"] = str(resp_model)
    team = first(rec, m["team"])
    if team:
        attrs["ambientq.team"] = str(team)
    if rid:
        attrs["ambientq.gateway.request_id"] = str(rid)
    if failed:
        attrs["error.type"] = str(error or first(rec, m["status"]) or "error")[:200]
    span_id = _hash("gw", conv_id, str(rid) if rid else (start, messages, outputs), n=16)
    return Span(span_id=span_id, trace_id=_hash("gw-trace", conv_id), name=f"chat {model or ''}".strip(),
                kind="CLIENT", start_ns=start, end_ns=max(end, start),
                status_code="ERROR" if failed else "OK",
                status_message=_text(error)[:500] if failed and error else "", attributes=attrs)


def records_to_spans(records: Iterable[Mapping[str, Any]], preset: str = "proxy-log",
                     mappings: Mapping[str, Any] | None = None) -> tuple[list[Span], int, list[str]]:
    """Convert records; ``preset`` ``proxy-log`` (or ``auto``) detects one per record.

    Returns (spans, records skipped for carrying no messages, errors).
    """
    spans: list[Span] = []
    skipped = 0
    errors: list[str] = []
    cache: dict[str, Mappings] = {}
    for i, rec in enumerate(records):
        try:
            if not isinstance(rec, Mapping):
                raise ValueError("not a JSON object")
            name = detect_preset(rec) if preset in ("proxy-log", "auto") else preset
            if name not in cache:
                cache[name] = resolve_mappings(name, mappings)
            span = record_to_span(rec, cache[name], index=i)
        except Exception as exc:  # keep going; report per record
            errors.append(f"record {i}: {exc}")
            continue
        if span is None:
            skipped += 1
        else:
            spans.append(span)
    return spans, skipped, errors


def parse_payload(body: bytes, content_type: str = "application/json") -> list[Any]:
    """A gateway POST body: a JSON array, one JSON object, or JSON lines (gzip ok)."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    text = body.decode("utf-8").strip()
    if not text:
        return []
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in ("application/x-ndjson", "application/jsonl", "application/json-lines"):
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            pass
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def ingest_gateway_payload(store: Any, body: bytes, content_type: str,
                           mappings: Mapping[str, Any] | None = None, preset: str = "proxy-log") -> dict[str, Any]:
    """HTTP log sink: decode a POSTed batch of proxy-log records and ingest it.

    Raises ``ValueError`` on a body that isn't JSON / JSON lines.
    """
    try:
        records = parse_payload(body, content_type)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"bad gateway log payload: {exc}") from exc
    spans, skipped, errors = records_to_spans(records, preset, mappings)
    touched = store.ingest_spans(spans) if spans else set()
    return {"records": len(records), "spans": len(spans), "conversations": len(touched),
            "skipped": skipped, "errors": errors[:20]}
