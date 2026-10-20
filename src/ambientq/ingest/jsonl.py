"""File ingest: JSON Lines (or JSON) files holding any of

* OTLP ``ExportTraceServiceRequest`` objects (what the OTel Collector's file
  exporter writes),
* simple conversation records ``{"conversation_id", "agent", "messages": [...]}``
  with OpenAI chat-format messages,
* LangChain / LangSmith run records (``run_type`` llm/tool/chain),
* OpenAI-compatible proxy logs, one request/response pair per object
  (LiteLLM, Envoy AI Gateway, Kong, generic gateways; see ``ingest.gateway``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ambientq.ingest import gateway as gw
from ambientq.ingest import normalize as nz
from ambientq.ingest.otlp import decode_export_request
from ambientq.models import Conversation, Span, Turn


@dataclass
class Parsed:
    spans: list[Span] = field(default_factory=list)
    conversations: list[Conversation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("[") or (stripped.startswith("{") and "\n{" not in stripped.strip()):
        try:
            data = json.loads(text)
            yield from (data if isinstance(data, list) else [data])
            return
        except json.JSONDecodeError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)


def record_to_conversation(rec: dict[str, Any]) -> Conversation:
    """Build a conversation from a simple record with OpenAI chat messages."""
    conv = Conversation(
        conversation_id=str(rec.get("conversation_id") or rec.get("id")),
        agent_name=str(rec.get("agent") or rec.get("agent_name") or ""),
        harness_version=str(rec.get("harness_version") or ""),
        metadata=dict(rec.get("metadata") or {}),
        team=str(rec.get("team") or ""),
    )
    if rec.get("model"):
        conv.metadata.setdefault("model", str(rec["model"]))
    turns: list[Turn] = []
    names: dict[str, str] = {}
    for raw in rec.get("messages", []):
        m = nz.message_from_any(raw)
        if m["role"] == "system":
            conv.metadata.setdefault("system_prompt", m["content"])
            continue
        if m["role"] == "tool":
            cid = m.get("tool_call_id")
            turns.append(Turn(index=len(turns), role="tool", content=m["content"], tool_call_id=cid,
                              tool_name=m.get("name") or names.get(cid or ""),
                              is_error=bool(raw.get("is_error"))))
            continue
        if m["content"] or not m["tool_calls"]:
            turns.append(Turn(index=len(turns), role=m["role"], content=m["content"]))
        for tc in m["tool_calls"]:
            if tc.get("id"):
                names[tc["id"]] = tc.get("name") or ""
            turns.append(Turn(index=len(turns), role="assistant", tool_name=tc.get("name"),
                              tool_call_id=tc.get("id"), tool_args=tc.get("args") or {}))
    conv.turns = turns
    conv.has_error = any(t.is_error for t in turns)
    usage = rec.get("usage") or {}
    conv.input_tokens = int(usage.get("input_tokens", 0) or 0)
    conv.output_tokens = int(usage.get("output_tokens", 0) or 0)
    ts = rec.get("started_ns") or 0
    conv.started_ns = conv.ended_ns = int(ts)
    return conv


# --- LangChain / LangSmith runs ------------------------------------------------

_LC_ROLE = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool",
            "HumanMessage": "user", "AIMessage": "assistant", "SystemMessage": "system",
            "ToolMessage": "tool", "AIMessageChunk": "assistant"}


def _lc_message(m: Any) -> dict[str, Any]:
    if isinstance(m, dict) and m.get("lc") == 1:
        kind = (m.get("id") or ["", "HumanMessage"])[-1]
        m = {**m.get("kwargs", {}), "type": kind}
    if not isinstance(m, dict):
        return {"role": "user", "content": str(m)}
    role = _LC_ROLE.get(str(m.get("type", m.get("role", "user"))), str(m.get("role", "user")))
    tcs = [{"id": tc.get("id"), "function": {"name": tc.get("name"), "arguments": tc.get("args", {})}}
           for tc in m.get("tool_calls") or []]
    out: dict[str, Any] = {"role": role, "content": m.get("content", ""), "tool_calls": tcs}
    if m.get("tool_call_id"):
        out["tool_call_id"] = m["tool_call_id"]
    if m.get("name"):
        out["name"] = m["name"]
    return out


def _ts_ns(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value * 1e9) if value < 1e12 else int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1e9)


def langchain_run_to_span(run: dict[str, Any]) -> Span:
    meta = (run.get("extra") or {}).get("metadata") or {}
    attrs: dict[str, Any] = {"run_type": run.get("run_type")}
    session = run.get("session_id") or meta.get("thread_id") or meta.get("session_id") or meta.get("conversation_id")
    if session:
        attrs["session.id"] = str(session)
    if meta.get("agent_name"):
        attrs["gen_ai.agent.name"] = meta["agent_name"]
    inputs, outputs = run.get("inputs") or {}, run.get("outputs") or {}
    rt = run.get("run_type")
    if rt == "llm":
        msgs = inputs.get("messages") or []
        if msgs and isinstance(msgs[0], list):
            msgs = msgs[0]
        attrs["gen_ai.input.messages"] = json.dumps([_lc_message(m) for m in msgs])
        gens = outputs.get("generations") or []
        if gens and isinstance(gens[0], list):
            gens = gens[0]
        outs = [_lc_message(g.get("message", {"type": "ai", "content": g.get("text", "")})) for g in gens]
        attrs["gen_ai.output.messages"] = json.dumps(outs)
        model = ((run.get("extra") or {}).get("invocation_params") or {}).get("model")
        if model:
            attrs["gen_ai.request.model"] = model
    elif rt == "tool":
        attrs["gen_ai.tool.name"] = run.get("name")
        attrs["gen_ai.tool.call.arguments"] = json.dumps(inputs.get("input", inputs))
        out = outputs.get("output", outputs)
        if isinstance(out, dict) and "content" in out:
            out = out["content"]
        attrs["gen_ai.tool.call.result"] = out if isinstance(out, str) else json.dumps(out)
        if meta.get("tool_call_id") or run.get("tool_call_id"):
            attrs["gen_ai.tool.call.id"] = meta.get("tool_call_id") or run.get("tool_call_id")
    return Span(
        span_id=str(run.get("id")),
        trace_id=str(run.get("trace_id") or run.get("id")),
        parent_span_id=str(run["parent_run_id"]) if run.get("parent_run_id") else None,
        name=str(run.get("name", rt)),
        start_ns=_ts_ns(run.get("start_time")),
        end_ns=_ts_ns(run.get("end_time")),
        status_code="ERROR" if run.get("error") else "OK",
        status_message=str(run.get("error") or ""),
        attributes=attrs,
    )


FORMATS = ("auto", "otlp", "record", "langchain", *gw.FORMATS)


def _is_otlp(rec: dict[str, Any]) -> bool:
    return "resourceSpans" in rec or "resource_spans" in rec


def _is_record(rec: dict[str, Any]) -> bool:
    return "messages" in rec and ("conversation_id" in rec or "id" in rec)


def parse_records(records: Iterable[dict[str, Any]], fmt: str = "auto",
                  mappings: dict[str, Any] | None = None) -> Parsed:
    """Parse records of one ``fmt`` (``auto`` detects each record's shape).

    ``mappings`` are ``gateway.mappings`` for the proxy-log formats.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r} (one of {', '.join(FORMATS)})")
    out = Parsed()
    skipped = 0
    for i, rec in enumerate(records):
        try:
            if fmt in gw.FORMATS or (fmt == "auto" and gw.is_proxy_log(rec)):
                spans, n, errs = gw.records_to_spans([rec], fmt if fmt in gw.FORMATS else "proxy-log", mappings)
                out.spans.extend(spans)
                skipped += n
                out.errors.extend(f"record {i}: {e.split(': ', 1)[-1]}" for e in errs)
            elif _is_otlp(rec) and fmt in ("auto", "otlp"):
                out.spans.extend(decode_export_request(rec))
            elif _is_record(rec) and fmt in ("auto", "record"):
                out.conversations.append(record_to_conversation(rec))
            elif "run_type" in rec and fmt in ("auto", "langchain"):
                out.spans.append(langchain_run_to_span(rec))
            else:
                want = "" if fmt == "auto" else f" for --format {fmt}"
                out.errors.append(f"record {i}: unrecognized shape{want} (keys: {sorted(rec)[:6]})")
        except Exception as exc:  # keep going; report per record
            out.errors.append(f"record {i}: {exc}")
    if skipped:
        out.errors.append(f"{skipped} proxy-log record(s) carry no request/response messages "
                          "(metadata-only access log?); skipped")
    return out


def parse_files(paths: Iterable[str | Path], fmt: str = "auto",
                mappings: dict[str, Any] | None = None) -> Parsed:
    total = Parsed()
    for p in paths:
        path = Path(p)
        files = sorted(path.rglob("*.json*")) if path.is_dir() else [path]
        for f in files:
            part = parse_records(iter_records(f), fmt=fmt, mappings=mappings)
            total.spans.extend(part.spans)
            total.conversations.extend(part.conversations)
            total.errors.extend(f"{f}: {e}" for e in part.errors)
    return total
