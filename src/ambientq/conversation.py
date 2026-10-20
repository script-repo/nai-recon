"""Assemble spans into ordered, normalized conversations."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable

from ambientq.ingest import normalize as nz
from ambientq.models import Conversation, Span, Turn


def _key(role: str, content: str, extra: str = "") -> tuple[str, str, str]:
    return (role, " ".join(content.split()), extra)


def build_conversation(conversation_id: str, spans: list[Span]) -> Conversation:
    """Turn the spans of one conversation into a de-duplicated turn list.

    LLM spans re-send the whole history on every call, so each input message
    is appended only the first time it is seen. Tool spans add the tool
    result (and the tool call, if no LLM output recorded it).
    """
    spans = sorted(spans, key=lambda s: (s.start_ns, s.end_ns))
    conv = Conversation(conversation_id=conversation_id)
    seen: set[tuple[str, str, str]] = set()
    calls_seen: set[str] = set()
    results_seen: set[str] = set()
    turns: list[Turn] = []

    def add(role: str, content: str, span: Span, **kw: object) -> None:
        turns.append(Turn(index=len(turns), role=role, content=content, span_id=span.span_id,
                          ts_ns=span.start_ns, **kw))  # type: ignore[arg-type]

    def add_message(m: nz.Message, span: Span, model: str | None) -> None:
        role = m["role"]
        if role == "system":
            return
        if role == "tool":
            cid = m.get("tool_call_id") or ""
            k = _key("tool", m.get("content", ""), cid)
            if k in seen or (cid and cid in results_seen):
                return
            seen.add(k)
            if cid:
                results_seen.add(cid)
            add("tool", m.get("content", ""), span, tool_call_id=cid or None, tool_name=m.get("name"),
                is_error=bool(m.get("is_error")))
            if m.get("is_error"):
                conv.has_error = True
            return
        content = m.get("content", "")
        tcs = m.get("tool_calls") or []
        sig = json.dumps([(t.get("name"), t.get("args")) for t in tcs], sort_keys=True) if tcs else ""
        k = _key(role, content, sig)
        if k in seen:
            return
        seen.add(k)
        if content or not tcs:
            add(role, content, span, model=model if role == "assistant" else None)
        for tc in tcs:
            cid = tc.get("id") or ""
            if cid:
                if cid in calls_seen:
                    continue
                calls_seen.add(cid)
            add("assistant", "", span, tool_name=tc.get("name"), tool_call_id=cid or None,
                tool_args=tc.get("args") or {}, model=model)

    for span in spans:
        if not conv.agent_name:
            conv.agent_name = nz.agent_name(span)
        if not conv.harness_version:
            conv.harness_version = nz.harness_version(span)
        if not conv.team:
            conv.team = nz.team(span)
        if span.is_error:
            conv.has_error = True
        a = span.attributes
        conv.input_tokens += int(a.get("gen_ai.usage.input_tokens", a.get("llm.token_count.prompt", 0)) or 0)
        conv.output_tokens += int(a.get("gen_ai.usage.output_tokens", a.get("llm.token_count.completion", 0)) or 0)
        kind = nz.span_kind(span)
        if kind == "llm":
            model = a.get("gen_ai.response.model") or a.get("gen_ai.request.model") or a.get("llm.model_name")
            inputs, outputs, system = nz.llm_messages(span)
            if system and "system_prompt" not in conv.metadata:
                conv.metadata["system_prompt"] = system
            for m in inputs:
                add_message(m, span, None)
            for m in outputs:
                add_message(m, span, model)
        elif kind == "tool":
            te = nz.tool_execution(span)
            cid = te["call_id"] or ""
            if not cid:
                # OpenInference tool spans carry no call id: pair with the latest unanswered call.
                cand = next((t for t in reversed(turns) if t.role == "assistant" and t.tool_name == te["name"]
                             and t.tool_call_id and t.tool_call_id not in results_seen), None)
                if cand is not None:
                    cid = cand.tool_call_id or ""
            if not cid or cid not in calls_seen:
                if cid:
                    calls_seen.add(cid)
                add("assistant", "", span, tool_name=te["name"], tool_call_id=cid or None, tool_args=te["args"])
            if cid and cid in results_seen:
                # Result already recorded from the next LLM call's input; keep the error flag.
                if te["is_error"]:
                    for t in turns:
                        if t.role == "tool" and t.tool_call_id == cid:
                            t.is_error = True
                continue
            if cid:
                results_seen.add(cid)
            add("tool", te["result"], span, tool_name=te["name"], tool_call_id=cid or None,
                is_error=te["is_error"])
            seen.add(_key("tool", te["result"], cid))
            if te["is_error"]:
                conv.has_error = True

    # Fill in tool names on tool-result turns from their calls.
    names = {t.tool_call_id: t.tool_name for t in turns if t.role == "assistant" and t.tool_call_id}
    for t in turns:
        if t.role == "tool" and not t.tool_name and t.tool_call_id in names:
            t.tool_name = names[t.tool_call_id]
    conv.turns = turns
    if spans:
        conv.started_ns = spans[0].start_ns
        conv.ended_ns = max(s.end_ns for s in spans)
    return conv


def group_spans(spans: Iterable[Span]) -> dict[str, list[Span]]:
    """Group spans by conversation; spans of one trace follow its root's key."""
    spans = list(spans)
    by_trace: dict[str, str] = {}
    for s in spans:
        k = nz.conversation_key(s)
        if k != s.trace_id:
            by_trace.setdefault(s.trace_id, k)
    groups: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        k = nz.conversation_key(s)
        if k == s.trace_id:
            k = by_trace.get(s.trace_id, k)
        groups[k].append(s)
    return dict(groups)


def assemble(spans: Iterable[Span]) -> list[Conversation]:
    return [build_conversation(cid, ss) for cid, ss in group_spans(spans).items()]
