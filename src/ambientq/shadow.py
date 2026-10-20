"""Shadow testing: run a proposed harness next to the live one on new traffic, before approval.

Replay proves a fix on the conversations it was made from. Shadow testing
checks it on conversations that arrived *after* the proposal: each one is
replayed from its last user message under the live harness and under the
patched one (same replay model, same recorded tool results), both are graded
with the same checks and judge, and the totals give a verdict an approver
can weigh next to the replay: ``better``, ``same`` or ``worse``.
Nothing is deployed; production traffic is only read.
"""

from __future__ import annotations

import shutil
from typing import Any

from ambientq.checks import load_checks
from ambientq.config import Config
from ambientq.harness import Harness
from ambientq.llm import LLMClient, LLMError
from ambientq.models import Insight, Location, ProposalStatus, now_s
from ambientq.replay import Replayer, cut_index, grade, matches_insight
from ambientq.store import SQLStore


def verdict(wins: int, losses: int, insight_live: int, insight_patched: int) -> str:
    if losses > wins or insight_patched > insight_live:
        return "worse"
    if wins > losses or insight_patched < insight_live:
        return "better"
    return "same"


def patched_harness(harness: Harness, p: dict[str, Any]) -> Harness:
    if p.get("replacement") is None:
        raise RuntimeError(f"proposal {p['proposal_id']} predates shadow testing (no stored replacement)")
    loc = Location(p["file"], int(p["start_line"]), int(p["end_line"]))
    if not harness.valid_location(loc) or (p.get("original") is not None
                                           and harness.lines(loc.file, loc.start_line, loc.end_line) != p["original"]):
        raise RuntimeError(f"the harness changed since proposal {p['proposal_id']}; propose again")
    return harness.with_replacement(loc, p["replacement"])


def shadow_test(store: SQLStore, cfg: Config, proposal_id: str, harness: Harness, agent_llm: LLMClient,
                judge_llm: LLMClient | None, *, limit: int | None = None, since: float | None = None,
                actor: str = "ambientq") -> dict[str, Any]:
    """Shadow-test one proposal on conversations ingested after it was made (or after ``since``)."""
    p = store.get_proposal(proposal_id)
    if p is None:
        raise KeyError(f"no proposal {proposal_id!r}")
    ins = store.get_insight(p["insight_id"])
    if ins is None:
        raise KeyError(f"proposal {proposal_id} has no insight")
    patched = patched_harness(harness, p)
    try:
        return _run(store, cfg, p, ins, harness, patched, agent_llm, judge_llm,
                    limit=int(limit if limit is not None else cfg.get("shadow", "limit", 20)),
                    since=float(since if since is not None else p["created_at"]), actor=actor)
    finally:
        shutil.rmtree(patched.root, ignore_errors=True)


def _run(store: SQLStore, cfg: Config, p: dict[str, Any], ins: Insight, live: Harness, patched: Harness,
         agent_llm: LLMClient, judge_llm: LLMClient | None, *, limit: int, since: float,
         actor: str) -> dict[str, Any]:
    # The conversations the fix was made from (findings that existed when it was proposed) don't count.
    source = {r["conversation_id"] for r in store.query(
        "SELECT conversation_id FROM findings WHERE insight_id=? AND created_at <= ?",
        (ins.insight_id, float(p["created_at"])))}
    fresh = [c for c in store.list_conversations(agent=ins.agent_name or None, ingested_after=since)
             if c.conversation_id not in source and any(t.role == "user" for t in c.turns)]
    fresh = fresh[-limit:] if limit > 0 else fresh
    replayer = Replayer.from_config(cfg)
    checks = load_checks(cfg.get("checks", "plugins", []), cfg.get("checks", "disabled", []))
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for conv in fresh:
        cut = cut_index(conv, [])
        try:
            a = replayer.run(agent_llm, live, conv, cut)
            b = replayer.run(agent_llm, patched, conv, cut)
            fa = [f for f in grade(a, live, judge_llm, checks) if f.verified]
            fb = [f for f in grade(b, patched, judge_llm, checks) if f.verified]
        except LLMError as exc:
            errors.append(f"{conv.conversation_id}: {exc}")
            continue
        rows.append({"conversation_id": conv.conversation_id, "live_findings": len(fa), "patched_findings": len(fb),
                     "live_insight": any(matches_insight(f, ins) for f in fa),
                     "patched_insight": any(matches_insight(f, ins) for f in fb),
                     "unmatched_calls": len(a.metadata.get("unmatched_calls") or [])
                     + len(b.metadata.get("unmatched_calls") or [])})
    wins = sum(1 for r in rows if r["patched_findings"] < r["live_findings"])
    losses = sum(1 for r in rows if r["patched_findings"] > r["live_findings"])
    il = sum(1 for r in rows if r["live_insight"])
    ip = sum(1 for r in rows if r["patched_insight"])
    result = {
        "ran_at": now_s(), "since": since, "conversations": len(rows), "mode": replayer.mode,
        "live_findings": sum(r["live_findings"] for r in rows),
        "patched_findings": sum(r["patched_findings"] for r in rows),
        "wins": wins, "losses": losses, "insight_live": il, "insight_patched": ip,
        "verdict": verdict(wins, losses, il, ip) if rows else "no_data", "rows": rows, "errors": errors,
    }
    p["shadow"] = result
    store.save_proposal(p)
    store.audit(actor, "shadow_tested", p["proposal_id"], {k: result[k] for k in (
        "conversations", "live_findings", "patched_findings", "wins", "losses", "verdict")})
    return result


def shadow_pending(store: SQLStore, cfg: Config, harness: Harness, agent_llm: LLMClient,
                   judge_llm: LLMClient | None) -> dict[str, dict[str, Any]]:
    """Shadow-test every proposal still awaiting approval for this harness's agent (``shadow.enabled``)."""
    out: dict[str, dict[str, Any]] = {}
    for p in store.proposals(status=str(ProposalStatus.PENDING_APPROVAL)):
        if p.get("agent_name") not in (None, "", harness.agent) or p.get("replacement") is None:
            continue
        try:
            out[p["proposal_id"]] = shadow_test(store, cfg, p["proposal_id"], harness, agent_llm, judge_llm)
        except RuntimeError as exc:
            out[p["proposal_id"]] = {"verdict": "error", "error": str(exc)}
    return out
