"""Built-in deterministic checks."""

from __future__ import annotations

import re
from collections.abc import Iterator

from ambientq.checks import check
from ambientq.harness import Harness
from ambientq.models import Conversation, Evidence, Finding

_ACK = re.compile(r"\b(error|fail|failed|unable|couldn't|could not|problem|issue|unavailable|sorry|not able)\b", re.I)


def _f(conv: Conversation, **kw: object) -> Finding:
    return Finding(conversation_id=conv.conversation_id, source="", agent_name=conv.agent_name, **kw)  # type: ignore[arg-type]


@check("unknown_tool")
def unknown_tool(conv: Conversation, h: Harness) -> Iterator[Finding]:
    if not h.tools:
        return
    names = h.tool_names()
    for t in conv.tool_calls:
        if t.tool_name not in names:
            yield _f(conv, category="tool_selection", rubric_id="CFG-TOOLS", severity="high",
                     expected=f"call only tools defined in the harness ({', '.join(sorted(names))})",
                     actual=f"called undefined tool `{t.tool_name}`", tool_name=t.tool_name or "",
                     evidence=[Evidence(t.index, t.tool_name or "")])


@check("tool_args_schema")
def tool_args_schema(conv: Conversation, h: Harness) -> Iterator[Finding]:
    for t in conv.tool_calls:
        spec = h.tool(t.tool_name or "")
        if not spec:
            continue
        args = t.tool_args or {}
        props = spec.parameters.get("properties") or {}
        missing = [r for r in spec.parameters.get("required") or [] if r not in args]
        unknown = [a for a in args if props and a not in props and a != "_raw"]
        if missing or unknown:
            what = []
            if missing:
                what.append(f"missing required {', '.join(missing)}")
            if unknown:
                what.append(f"unknown argument(s) {', '.join(unknown)}")
            yield _f(conv, category="tool_arguments", rubric_id=f"R-T-{spec.name}", severity="medium",
                     tool_name=spec.name, expected=f"call `{spec.name}` with schema-valid arguments",
                     actual=f"`{spec.name}` called with {'; '.join(what)}",
                     evidence=[Evidence(t.index, spec.name)])


@check("unanswered_tool_call")
def unanswered_tool_call(conv: Conversation, h: Harness) -> Iterator[Finding]:
    answered = {t.tool_call_id for t in conv.tool_results if t.tool_call_id}
    for t in conv.tool_calls:
        if t.tool_call_id and t.tool_call_id not in answered:
            yield _f(conv, category="tool_results", rubric_id="G-TOOL-RESULT", severity="medium",
                     tool_name=t.tool_name or "", expected=f"`{t.tool_name}` returns a result before the agent relies on it",
                     actual=f"no result recorded for `{t.tool_name}` call {t.tool_call_id}",
                     evidence=[Evidence(t.index, t.tool_name or "")])


@check("tool_error_ignored")
def tool_error_ignored(conv: Conversation, h: Harness) -> Iterator[Finding]:
    for t in conv.tool_results:
        if not t.is_error:
            continue
        nxt = next((u for u in conv.turns[t.index + 1:] if u.role == "assistant" and u.content), None)
        if nxt and not _ACK.search(nxt.content):
            yield _f(conv, category="tool_results", rubric_id="G-TOOL-RESULT", severity="high",
                     tool_name=t.tool_name or "", expected="acknowledge the failed tool call to the user",
                     actual=f"`{t.tool_name}` failed but the answer does not mention it",
                     evidence=[Evidence(nxt.index, nxt.content[:80])])


@check("guardrail_patterns")
def guardrail_patterns(conv: Conversation, h: Harness) -> Iterator[Finding]:
    for g in h.guardrails:
        if not g.pattern:
            continue
        rx = re.compile(g.pattern)
        for t in conv.turns:
            if t.role != "assistant" or not t.content:
                continue
            m = rx.search(t.content)
            if m:
                yield _f(conv, category="safety", rubric_id=g.gid, severity="high",
                         expected=g.text, actual=f"assistant output matched forbidden pattern /{g.pattern}/",
                         evidence=[Evidence(t.index, m.group(0))])
                break


@check("max_turns")
def max_turns(conv: Conversation, h: Harness) -> Iterator[Finding]:
    limit = int(h.config.get("max_turns", 0) or 0)
    n = sum(1 for t in conv.turns if t.role == "assistant")
    if limit and n > limit:
        yield _f(conv, category="completion", rubric_id="CFG-MAX-TURNS", severity="low",
                 expected=f"finish within {limit} assistant steps", actual=f"took {n} assistant steps")


@check("empty_final_answer")
def empty_final_answer(conv: Conversation, h: Harness) -> Iterator[Finding]:
    if not conv.turns:
        return
    last = conv.turns[-1]
    if not (last.role == "assistant" and last.content.strip() and not last.tool_name):
        yield _f(conv, category="completion", rubric_id="G-COMPLETION", severity="medium",
                 expected="the conversation ends with an answer to the user",
                 actual=f"conversation ends with a {last.role}{' tool call' if last.tool_name else ''} turn",
                 evidence=[Evidence(last.index, "")])
