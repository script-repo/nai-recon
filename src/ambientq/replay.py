"""Replay failing conversations against a patched harness and re-grade them.

Single-step replay: the conversation is cut just before the first assistant
turn the insight's evidence points at; the agent's model (on NAI) is asked for
its next response under the original harness (control) and the patched one;
both are graded with the same checks and judge. Tool results earlier in the
conversation are replayed from the trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ambientq.checks import load_checks, run_checks
from ambientq.harness import Harness
from ambientq.insights import TOOL_CATEGORIES, jaccard
from ambientq.judge.judge import judge_conversation
from ambientq.judge.rubric import build_rubric
from ambientq.judge.verifier import check_evidence, verify
from ambientq.llm import LLMClient, LLMError
from ambientq.models import Conversation, Finding, Insight, Turn


def turns_to_messages(system: str, turns: list[Turn]) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    pending: list[str] = []
    n = 0
    for t in turns:
        if t.role == "user":
            msgs.append({"role": "user", "content": t.content})
        elif t.role == "assistant" and t.tool_name:
            n += 1
            cid = t.tool_call_id or f"call_{n}"
            pending.append(cid)
            call = {"id": cid, "type": "function",
                    "function": {"name": t.tool_name, "arguments": json.dumps(t.tool_args or {})}}
            last = msgs[-1] if msgs else None
            if last and last["role"] == "assistant":
                last.setdefault("tool_calls", []).append(call)
                last.setdefault("content", None)
            else:
                msgs.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif t.role == "assistant":
            msgs.append({"role": "assistant", "content": t.content})
        elif t.role == "tool":
            cid = t.tool_call_id or (pending.pop(0) if pending else f"call_{n}")
            if cid in pending:
                pending.remove(cid)
            msgs.append({"role": "tool", "tool_call_id": cid, "content": t.content})
    return msgs


def cut_index(conv: Conversation, findings: list[Finding]) -> int:
    """Index of the first assistant turn implicated by the findings."""
    idxs = sorted(e.turn for f in findings for e in f.evidence if 0 <= e.turn < len(conv.turns))
    for i in idxs:
        j = i
        while j >= 0 and conv.turns[j].role != "assistant":
            j -= 1
        if j < 0:
            continue
        # back up to the start of a run of assistant turns (content + tool calls of one response)
        while j > 0 and conv.turns[j - 1].role == "assistant":
            j -= 1
        return j
    last_user = max((t.index for t in conv.turns if t.role == "user"), default=0)
    return min(last_user + 1, len(conv.turns))


def regenerate(llm: LLMClient, h: Harness, conv: Conversation, cut: int) -> Conversation:
    prefix = conv.turns[:cut]
    resp = llm.chat(turns_to_messages(h.system_prompt(), prefix), purpose="replay",
                    tools=[t.openai() for t in h.tools] or None, max_tokens=1024)
    turns = [Turn.from_dict(t.to_dict()) for t in prefix]
    if resp.content:
        turns.append(Turn(index=len(turns), role="assistant", content=resp.content, model=resp.model))
    for tc in resp.tool_calls:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        turns.append(Turn(index=len(turns), role="assistant", tool_name=fn.get("name"),
                          tool_call_id=tc.get("id"), tool_args=args, model=resp.model))
    return Conversation(conversation_id=f"{conv.conversation_id}~replay", agent_name=conv.agent_name,
                        turns=turns, metadata={"replay_of": conv.conversation_id, "cut": cut})


def matches_insight(f: Finding, ins: Insight) -> bool:
    if f.verified is False or f.category != ins.category:
        return False
    if ins.category in TOOL_CATEGORIES and ins.tool_name:
        return f.tool_name == ins.tool_name
    return f.rubric_id == ins.rubric_id or jaccard(f.expected, ins.expected) >= 0.3 or not ins.rubric_id.startswith("R-P")


def grade(conv: Conversation, h: Harness, judge: LLMClient | None, checks: dict[str, Any]) -> list[Finding]:
    rubric = build_rubric(h)
    found = []
    # The replay ends at the regenerated turn, so "conversation ended early" checks don't apply.
    for f in run_checks(conv, h, {k: v for k, v in checks.items()
                                  if k not in ("empty_final_answer", "unanswered_tool_call")}):
        f.verified, f.verifier_note = check_evidence(f, conv, rubric)
        found.append(f)
    if judge is not None:
        jf = judge_conversation(judge, conv, h, rubric)
        found.extend(verify(jf, conv, rubric, None))
    return found


@dataclass
class ReplayReport:
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def fixed(self) -> int:
        return sum(1 for r in self.rows if r["kind"] == "failing" and r["before_fail"] and not r["after_fail"])

    @property
    def still_failing(self) -> int:
        return sum(1 for r in self.rows if r["kind"] == "failing" and r["after_fail"])

    @property
    def regressions(self) -> int:
        return sum(1 for r in self.rows if r["kind"] == "control" and r["after_new_findings"] > r["before_new_findings"])

    @property
    def improved(self) -> bool:
        return self.fixed > 0 and self.regressions == 0

    def to_dict(self) -> dict[str, Any]:
        return {"fixed": self.fixed, "still_failing": self.still_failing, "regressions": self.regressions,
                "improved": self.improved, "rows": self.rows, "errors": self.errors}


def replay(insight: Insight, failing: list[tuple[Conversation, list[Finding]]], original: Harness,
           patched: Harness, agent_llm: LLMClient, judge_llm: LLMClient | None,
           controls: list[Conversation] | None = None, checks: dict[str, Any] | None = None) -> ReplayReport:
    checks = checks if checks is not None else load_checks()
    rep = ReplayReport()
    for conv, fs in failing:
        cut = cut_index(conv, fs)
        try:
            before = regenerate(agent_llm, original, conv, cut)
            after = regenerate(agent_llm, patched, conv, cut)
            bf = any(matches_insight(f, insight) for f in grade(before, original, judge_llm, checks))
            af = any(matches_insight(f, insight) for f in grade(after, patched, judge_llm, checks))
        except LLMError as exc:
            rep.errors.append(f"{conv.conversation_id}: {exc}")
            continue
        rep.rows.append({"kind": "failing", "conversation_id": conv.conversation_id, "cut": cut,
                         "before_fail": bf, "after_fail": af,
                         "before_reply": _reply(before, cut), "after_reply": _reply(after, cut)})
    for conv in controls or []:
        cut = cut_index(conv, [])
        try:
            before = regenerate(agent_llm, original, conv, cut)
            after = regenerate(agent_llm, patched, conv, cut)
            nb = sum(1 for f in grade(before, original, judge_llm, checks) if f.verified)
            na = sum(1 for f in grade(after, patched, judge_llm, checks) if f.verified)
        except LLMError as exc:
            rep.errors.append(f"{conv.conversation_id}: {exc}")
            continue
        rep.rows.append({"kind": "control", "conversation_id": conv.conversation_id, "cut": cut,
                         "before_new_findings": nb, "after_new_findings": na,
                         "after_reply": _reply(after, cut)})
    return rep


def _reply(conv: Conversation, cut: int) -> str:
    parts = []
    for t in conv.turns[cut:]:
        parts.append(t.content if t.content else f"<call {t.tool_name}({json.dumps(t.tool_args or {})})>")
    return " ".join(parts)[:400]
