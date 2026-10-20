"""Replay failing conversations against a patched harness and re-grade them.

The conversation is cut just before the first assistant turn the insight's
evidence points at. From there the agent's model (on NAI) is asked to carry on
under the original harness (control) and the patched one, and both
continuations are graded with the same checks and judge.

Two modes:

* ``multi`` (default): the agent runs step by step until it gives a final
  answer (or ``max_steps``). Every tool call it makes is answered from the
  conversation's **tool-result tape**: the results recorded at ingest, matched
  by tool name and arguments. Calls with no exact match follow the
  ``unmatched`` policy (``nearest``, ``error`` or ``stop``) and are flagged.
  Real tools are never called.
* ``single``: only the agent's next response is regenerated (v0.1 behaviour).
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


UNMATCHED_POLICIES = ("nearest", "error", "stop")
NOT_RECORDED = json.dumps({"error": "not recorded: ambientq replay has no recorded result for this tool call"})


def canonical_args(args: dict[str, Any] | None) -> str:
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class TapeEntry:
    tool: str
    args: dict[str, Any]
    result: str
    is_error: bool
    used: bool = False


@dataclass
class TapeAnswer:
    content: str
    is_error: bool
    match: str  # exact | library | nearest | not_recorded | stop
    recorded_args: dict[str, Any] | None = None


@dataclass
class ToolTape:
    """Recorded tool results of one conversation, keyed by tool name and arguments."""

    entries: list[TapeEntry] = field(default_factory=list)
    # Results recorded in *other* conversations of the same agent, consulted (and flagged
    # ``library``) when the continuation calls a tool with arguments this conversation never
    # recorded. Shared and never consumed.
    library: list[TapeEntry] = field(default_factory=list)

    @classmethod
    def from_conversation(cls, conv: Conversation) -> ToolTape:
        results = {t.tool_call_id: t for t in conv.turns if t.role == "tool" and t.tool_call_id}
        loose = [t for t in conv.turns if t.role == "tool" and not t.tool_call_id]
        tape = cls()
        for call in conv.tool_calls:
            res = results.get(call.tool_call_id) if call.tool_call_id else None
            if res is None:
                # No call id: the next unclaimed result after the call (same tool name if known).
                res = next((t for t in loose if t.index > call.index
                            and (not t.tool_name or t.tool_name == call.tool_name)), None)
                if res is not None:
                    loose.remove(res)
            if res is not None:
                tape.entries.append(TapeEntry(call.tool_name or "", dict(call.tool_args or {}), res.content,
                                              res.is_error))
        return tape

    def fresh(self) -> ToolTape:
        return ToolTape([TapeEntry(e.tool, e.args, e.result, e.is_error) for e in self.entries], self.library)

    def answer(self, tool: str, args: dict[str, Any] | None, policy: str = "nearest") -> TapeAnswer:
        key = canonical_args(args)
        same = [e for e in self.entries if e.tool == tool]
        for e in same:
            if not e.used and canonical_args(e.args) == key:
                e.used = True
                return TapeAnswer(e.result, e.is_error, "exact", e.args)
        if policy == "stop":
            return TapeAnswer("", False, "stop")
        lib = next((e for e in reversed(self.library) if e.tool == tool and canonical_args(e.args) == key), None)
        if lib is not None:
            return TapeAnswer(lib.result, lib.is_error, "library", lib.args)
        if policy == "nearest" and same:
            fresh = [e for e in same if not e.used] or same
            best = max(fresh, key=lambda e: _arg_similarity(e.args, args or {}))
            best.used = True
            return TapeAnswer(best.result, best.is_error, "nearest", best.args)
        return TapeAnswer(NOT_RECORDED, True, "not_recorded")


def build_library(convs: list[Conversation]) -> list[TapeEntry]:
    """Successful tool results from these conversations, oldest first, for cross-conversation lookup."""
    out: list[TapeEntry] = []
    for c in convs:
        out.extend(e for e in ToolTape.from_conversation(c).entries if not e.is_error)
    return out


def _arg_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    same_vals = sum(1 for k in keys if k in a and k in b and a[k] == b[k])
    same_keys = len(set(a) & set(b))
    return (same_vals + 0.5 * same_keys) / len(keys)


def _append_response(turns: list[Turn], resp: Any, h: Harness) -> list[Turn]:
    """Append the model's response (through the harness's guardrail layer); return the new tool-call turns."""
    if resp.content:
        turns.append(Turn(index=len(turns), role="assistant", content=h.apply_guardrails(resp.content),
                          model=resp.model))
    calls = []
    for tc in resp.tool_calls:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        if not isinstance(args, dict):
            args = {"_raw": args}
        t = Turn(index=len(turns), role="assistant", tool_name=fn.get("name"), tool_call_id=tc.get("id"),
                 tool_args=args, model=resp.model)
        turns.append(t)
        calls.append(t)
    return calls


def continue_conversation(llm: LLMClient, h: Harness, conv: Conversation, cut: int, tape: ToolTape, *,
                          max_steps: int = 8, unmatched: str = "nearest") -> Conversation:
    """Multi-step replay from ``cut``: run the agent until it answers, feeding it recorded tool results."""
    if unmatched not in UNMATCHED_POLICIES:
        raise ValueError(f"replay.unmatched must be one of {UNMATCHED_POLICIES}, got {unmatched!r}")
    turns = [Turn.from_dict(t.to_dict()) for t in conv.turns[:cut]]
    tools = [t.openai() for t in h.tools] or None
    flags: list[dict[str, Any]] = []
    steps, reason = 0, "max_steps"
    while steps < max_steps:
        resp = llm.chat(turns_to_messages(h.system_prompt(), turns), purpose="replay", tools=tools,
                        max_tokens=1024)
        steps += 1
        calls = _append_response(turns, resp, h)
        if not calls:
            reason = "final_answer"
            break
        stop = False
        for call in calls:
            name = call.tool_name or ""
            ans = tape.answer(name, call.tool_args, unmatched)
            if name not in h.tool_names():
                flags.append({"tool": name, "args": call.tool_args, "match": ans.match, "undefined_tool": True})
            elif ans.match != "exact":
                flags.append({"tool": name, "args": call.tool_args, "match": ans.match,
                              "recorded_args": ans.recorded_args})
            if ans.match == "stop":
                stop = True
                break
            turns.append(Turn(index=len(turns), role="tool", content=ans.content, tool_name=name,
                              tool_call_id=call.tool_call_id, is_error=ans.is_error))
        if stop:
            reason = "unmatched_call"
            break
    return Conversation(conversation_id=f"{conv.conversation_id}~replay", agent_name=conv.agent_name,
                        turns=turns, team=conv.team,
                        metadata={"replay_of": conv.conversation_id, "cut": cut, "mode": "multi",
                                  "steps": steps, "stop_reason": reason, "unmatched_calls": flags})


def regenerate(llm: LLMClient, h: Harness, conv: Conversation, cut: int) -> Conversation:
    prefix = conv.turns[:cut]
    resp = llm.chat(turns_to_messages(h.system_prompt(), prefix), purpose="replay",
                    tools=[t.openai() for t in h.tools] or None, max_tokens=1024)
    turns = [Turn.from_dict(t.to_dict()) for t in prefix]
    _append_response(turns, resp, h)
    return Conversation(conversation_id=f"{conv.conversation_id}~replay", agent_name=conv.agent_name,
                        turns=turns, team=conv.team,
                        metadata={"replay_of": conv.conversation_id, "cut": cut, "mode": "single", "steps": 1,
                                  "stop_reason": "single_step", "unmatched_calls": []})


@dataclass
class Replayer:
    """How continuations are produced: mode, step limit and the unmatched-call policy."""

    mode: str = "multi"
    max_steps: int = 8
    unmatched: str = "nearest"
    library: list[TapeEntry] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: Any, store: Any = None, agent: str | None = None) -> Replayer:
        r = cls(mode=str(cfg.get("replay", "mode", "multi") or "multi"),
                max_steps=int(cfg.get("replay", "max_steps", 8) or 8),
                unmatched=str(cfg.get("replay", "unmatched", "nearest") or "nearest"))
        size = int(cfg.get("replay", "library_size", 200) or 0)
        if store is not None and size > 0 and r.mode == "multi":
            r.library = build_library(store.list_conversations(agent=agent)[-size:])
        return r

    def run(self, llm: LLMClient, h: Harness, conv: Conversation, cut: int,
            tape: ToolTape | None = None) -> Conversation:
        if self.mode == "single":
            return regenerate(llm, h, conv, cut)
        if self.mode != "multi":
            raise ValueError(f"replay.mode must be 'multi' or 'single', got {self.mode!r}")
        tape = (tape or ToolTape.from_conversation(conv)).fresh()
        if not tape.library:
            tape.library = self.library
        return continue_conversation(llm, h, conv, cut, tape, max_steps=self.max_steps, unmatched=self.unmatched)


def matches_insight(f: Finding, ins: Insight) -> bool:
    if f.verified is False or f.category != ins.category:
        return False
    if ins.category in TOOL_CATEGORIES and ins.tool_name:
        return f.tool_name == ins.tool_name
    return f.rubric_id == ins.rubric_id or jaccard(f.expected, ins.expected) >= 0.3 or not ins.rubric_id.startswith("R-P")


def _complete(conv: Conversation) -> bool:
    return conv.metadata.get("stop_reason") == "final_answer"


def grade(conv: Conversation, h: Harness, judge: LLMClient | None, checks: dict[str, Any]) -> list[Finding]:
    rubric = build_rubric(h)
    found = []
    # A continuation that didn't reach a final answer (single-step, step limit, unmatched stop)
    # ended early on purpose, so "conversation ended early" checks don't apply to it.
    skip = () if _complete(conv) else ("empty_final_answer", "unanswered_tool_call")
    for f in run_checks(conv, h, {k: v for k, v in checks.items() if k not in skip}):
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
    mode: str = "multi"

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

    @property
    def unmatched(self) -> int:
        return sum(len(r.get("unmatched_calls") or []) for r in self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "fixed": self.fixed, "still_failing": self.still_failing,
                "regressions": self.regressions, "improved": self.improved, "unmatched_calls": self.unmatched,
                "rows": self.rows, "errors": self.errors}



def _trace(before: Conversation, after: Conversation) -> dict[str, Any]:
    """Replay bookkeeping for a report row: steps taken, why it stopped, and flagged tool calls."""
    return {"steps": [before.metadata.get("steps"), after.metadata.get("steps")],
            "stop_reason": [before.metadata.get("stop_reason"), after.metadata.get("stop_reason")],
            "unmatched_calls": [{"side": side, **u} for side, c in (("original", before), ("patched", after))
                                for u in c.metadata.get("unmatched_calls") or []]}


def replay(insight: Insight, failing: list[tuple[Conversation, list[Finding]]], original: Harness,
           patched: Harness, agent_llm: LLMClient, judge_llm: LLMClient | None,
           controls: list[Conversation] | None = None, checks: dict[str, Any] | None = None,
           replayer: Replayer | None = None) -> ReplayReport:
    checks = checks if checks is not None else load_checks()
    replayer = replayer or Replayer()
    rep = ReplayReport(mode=replayer.mode)
    for conv, fs in failing:
        cut = cut_index(conv, fs)
        try:
            before = replayer.run(agent_llm, original, conv, cut)
            after = replayer.run(agent_llm, patched, conv, cut)
            bf = any(matches_insight(f, insight) for f in grade(before, original, judge_llm, checks))
            af = any(matches_insight(f, insight) for f in grade(after, patched, judge_llm, checks))
        except LLMError as exc:
            rep.errors.append(f"{conv.conversation_id}: {exc}")
            continue
        rep.rows.append({"kind": "failing", "conversation_id": conv.conversation_id, "cut": cut,
                         "before_fail": bf, "after_fail": af,
                         "before_reply": _reply(before, cut), "after_reply": _reply(after, cut),
                         **_trace(before, after)})
    for conv in controls or []:
        cut = cut_index(conv, [])
        try:
            before = replayer.run(agent_llm, original, conv, cut)
            after = replayer.run(agent_llm, patched, conv, cut)
            nb = sum(1 for f in grade(before, original, judge_llm, checks) if f.verified)
            na = sum(1 for f in grade(after, patched, judge_llm, checks) if f.verified)
        except LLMError as exc:
            rep.errors.append(f"{conv.conversation_id}: {exc}")
            continue
        rep.rows.append({"kind": "control", "conversation_id": conv.conversation_id, "cut": cut,
                         "before_new_findings": nb, "after_new_findings": na,
                         "before_reply": _reply(before, cut), "after_reply": _reply(after, cut),
                         **_trace(before, after)})
    return rep


def _reply(conv: Conversation, cut: int) -> str:
    parts = []
    for t in conv.turns[cut:]:
        if t.role == "tool":
            parts.append(f"<result {t.tool_name}: {t.content[:80]}>")
        else:
            parts.append(t.content if t.content else f"<call {t.tool_name}({json.dumps(t.tool_args or {})})>")
    return " ".join(parts)[:600]
