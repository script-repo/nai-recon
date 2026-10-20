"""Map framework-specific span attributes onto one message shape.

Supported conventions:

* OpenTelemetry GenAI semantic conventions: structured ``gen_ai.input.messages``
  / ``gen_ai.output.messages`` / ``gen_ai.system_instructions`` (>=1.37), the
  older flattened ``gen_ai.prompt.N.*`` / ``gen_ai.completion.N.*`` attributes,
  and ``gen_ai.*.message`` / ``gen_ai.choice`` events.
* OpenInference (Arize/Phoenix): ``llm.input_messages.N.message.*``,
  ``openinference.span.kind``, ``tool.name``, ``input.value``/``output.value``.
* OpenAI chat-format messages, used by the simple JSONL record and LangChain.

The common message is ``{"role", "content", "tool_calls": [{"id","name","args"}],
"tool_call_id", "name"}``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ambientq.models import Span

Message = dict[str, Any]

LLM_OPERATIONS = {"chat", "text_completion", "generate_content", "completion"}
TOOL_OPERATIONS = {"execute_tool"}


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "[{":
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # content parts
        out = []
        for p in value:
            if isinstance(p, dict):
                out.append(str(p.get("content", p.get("text", ""))) if p.get("type", "text") == "text" else "")
            else:
                out.append(str(p))
        return "".join(out)
    return json.dumps(value, sort_keys=True)


def _args(value: Any) -> dict[str, Any]:
    v = _loads(value)
    if isinstance(v, dict):
        return v
    if v in (None, ""):
        return {}
    return {"_raw": v}


def message_from_any(m: dict[str, Any]) -> Message:
    """Normalize one message in OpenAI chat or GenAI-semconv ``parts`` form."""
    role = str(m.get("role", "user")).lower()
    if role in ("human",):
        role = "user"
    if role in ("ai", "model"):
        role = "assistant"
    msg: Message = {"role": role, "content": "", "tool_calls": []}
    if "parts" in m:  # GenAI semconv structured message
        texts = []
        for p in m.get("parts") or []:
            ptype = p.get("type", "text")
            if ptype == "text":
                texts.append(str(p.get("content", "")))
            elif ptype == "tool_call":
                msg["tool_calls"].append(
                    {"id": p.get("id"), "name": p.get("name"), "args": _args(p.get("arguments"))}
                )
            elif ptype == "tool_call_response":
                msg["tool_call_id"] = p.get("id")
                resp = p.get("response", p.get("result"))
                texts.append(resp if isinstance(resp, str) else json.dumps(resp, sort_keys=True))
                msg["role"] = "tool"
        msg["content"] = "".join(texts)
    else:
        msg["content"] = _text(m.get("content"))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", tc)
            msg["tool_calls"].append(
                {"id": tc.get("id"), "name": fn.get("name"), "args": _args(fn.get("arguments", fn.get("args")))}
            )
        if m.get("tool_call_id"):
            msg["tool_call_id"] = m["tool_call_id"]
    if m.get("name"):
        msg["name"] = m["name"]
    if m.get("is_error"):  # set by the gateway adapter on failed tool results
        msg["is_error"] = True
    return msg


def _indexed(attrs: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """Collect ``prefix.N.rest`` flat attributes into a list of nested dicts."""
    pat = re.compile(re.escape(prefix) + r"\.(\d+)\.(.+)$")
    rows: dict[int, dict[str, Any]] = {}
    for k, v in attrs.items():
        mt = pat.match(k)
        if mt:
            rows.setdefault(int(mt.group(1)), {})[mt.group(2)] = v
    return [rows[i] for i in sorted(rows)]


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in flat.items():
        cur = out
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
            if not isinstance(cur, dict):
                break
        else:
            cur[parts[-1]] = v
    return out


def _listify(d: Any) -> list[Any]:
    if isinstance(d, list):
        return d
    if isinstance(d, dict) and all(k.isdigit() for k in d):
        return [d[k] for k in sorted(d, key=int)]
    return []


def _openinference_messages(attrs: dict[str, Any], key: str) -> list[Message]:
    msgs = []
    for row in _indexed(attrs, key):
        nested = _nest(row).get("message", {})
        tcs = []
        for tc in _listify(nested.get("tool_calls")):
            t = tc.get("tool_call", tc)
            fn = t.get("function", {})
            tcs.append({"id": t.get("id"), "function": {"name": fn.get("name"), "arguments": fn.get("arguments")}})
        m = {"role": nested.get("role", "user"), "content": nested.get("content", ""), "tool_calls": tcs}
        if nested.get("tool_call_id"):
            m["tool_call_id"] = nested["tool_call_id"]
        if nested.get("name"):
            m["name"] = nested["name"]
        msgs.append(message_from_any(m))
    return msgs


def _legacy_genai_messages(attrs: dict[str, Any], key: str) -> list[Message]:
    msgs = []
    for row in _indexed(attrs, key):
        nested = _nest(row)
        tcs = [
            {"id": tc.get("id"), "function": {"name": tc.get("name"), "arguments": tc.get("arguments")}}
            for tc in _listify(nested.get("tool_calls"))
        ]
        m = {"role": nested.get("role", "assistant" if "completion" in key else "user"),
             "content": nested.get("content", ""), "tool_calls": tcs}
        if nested.get("tool_call_id"):
            m["tool_call_id"] = nested["tool_call_id"]
        msgs.append(message_from_any(m))
    return msgs


def _event_messages(span: Span) -> tuple[list[Message], list[Message]]:
    inputs: list[Message] = []
    outputs: list[Message] = []
    for ev in span.events:
        name, a = ev.get("name", ""), ev.get("attributes", {})
        role_map = {
            "gen_ai.system.message": "system",
            "gen_ai.user.message": "user",
            "gen_ai.assistant.message": "assistant",
            "gen_ai.tool.message": "tool",
        }
        if name in role_map:
            m = {"role": role_map[name], "content": a.get("content", ""),
                 "tool_calls": _loads(a.get("tool_calls")) or []}
            if a.get("id"):
                m["tool_call_id"] = a["id"]
            inputs.append(message_from_any(m))
        elif name == "gen_ai.choice":
            body = _loads(a.get("message")) or {}
            if not isinstance(body, dict):
                body = {"content": body}
            body.setdefault("role", "assistant")
            if "tool_calls" in a and "tool_calls" not in body:
                body["tool_calls"] = _loads(a["tool_calls"])
            outputs.append(message_from_any(body))
    return inputs, outputs


def span_kind(span: Span) -> str:
    """Classify a span as ``llm``, ``tool``, ``agent`` or ``other``."""
    a = span.attributes
    op = str(a.get("gen_ai.operation.name", "")).lower()
    oi = str(a.get("openinference.span.kind", "")).upper()
    if op in TOOL_OPERATIONS or oi == "TOOL" or a.get("run_type") == "tool":
        return "tool"
    if op in LLM_OPERATIONS or oi == "LLM" or a.get("run_type") == "llm":
        return "llm"
    if op in ("invoke_agent", "create_agent") or oi in ("AGENT", "CHAIN"):
        return "agent"
    if any(k.startswith(("gen_ai.input.messages", "gen_ai.prompt.", "llm.input_messages")) for k in a):
        return "llm"
    if a.get("gen_ai.tool.name") or a.get("tool.name"):
        return "tool"
    return "other"


def llm_messages(span: Span) -> tuple[list[Message], list[Message], str]:
    """Return (input messages, output messages, system instructions) of an LLM span."""
    a = span.attributes
    system = ""
    si = _loads(a.get("gen_ai.system_instructions"))
    if si:
        system = _text(si) if isinstance(si, list) else str(si)
    inputs: list[Message] = []
    outputs: list[Message] = []
    raw_in = _loads(a.get("gen_ai.input.messages"))
    raw_out = _loads(a.get("gen_ai.output.messages"))
    if isinstance(raw_in, list):
        inputs = [message_from_any(m) for m in raw_in if isinstance(m, dict)]
    if isinstance(raw_out, list):
        outputs = [message_from_any(m) for m in raw_out if isinstance(m, dict)]
    if not inputs:
        inputs = _openinference_messages(a, "llm.input_messages") or _legacy_genai_messages(a, "gen_ai.prompt")
    if not outputs:
        outputs = _openinference_messages(a, "llm.output_messages") or _legacy_genai_messages(a, "gen_ai.completion")
    if not inputs and not outputs:
        inputs, outputs = _event_messages(span)
    if not system:
        sys_msgs = [m["content"] for m in inputs if m["role"] == "system"]
        system = "\n".join(sys_msgs)
    return inputs, outputs, system


def tool_execution(span: Span) -> dict[str, Any]:
    """Return ``{name, call_id, args, result, is_error}`` for a tool span."""
    a = span.attributes
    name = a.get("gen_ai.tool.name") or a.get("tool.name") or span.name.replace("execute_tool ", "")
    args = a.get("gen_ai.tool.call.arguments", a.get("tool.parameters", a.get("input.value")))
    result = a.get("gen_ai.tool.call.result", a.get("output.value"))
    if isinstance(result, (dict, list)):
        result = json.dumps(result, sort_keys=True)
    return {
        "name": name,
        "call_id": a.get("gen_ai.tool.call.id") or a.get("tool_call.id") or a.get("tool.call_id"),
        "args": _args(args),
        "result": "" if result is None else str(result),
        "is_error": span.is_error or bool(a.get("error.type")),
    }


def conversation_key(span: Span) -> str:
    a = span.attributes
    for k in ("gen_ai.conversation.id", "session.id", "conversation.id", "thread_id", "langsmith.trace.session_id"):
        if a.get(k):
            return str(a[k])
    return span.trace_id


def agent_name(span: Span) -> str:
    a = span.attributes
    return str(a.get("gen_ai.agent.name") or a.get("agent.name") or span.resource.get("service.name") or "")


def harness_version(span: Span) -> str:
    return str(
        span.attributes.get("ambientq.harness.version")
        or span.resource.get("ambientq.harness.version")
        or span.resource.get("service.version")
        or ""
    )


def team(span: Span) -> str:
    return str(span.attributes.get("ambientq.team") or span.resource.get("ambientq.team") or "")
