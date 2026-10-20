"""Deduplicate findings into insights with NEW / RECURRING / RESOLVED status."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ambientq.models import Finding, Insight, InsightStatus, new_id, now_s
from ambientq.store import SQLStore

TOOL_CATEGORIES = {"tool_selection", "tool_arguments", "tool_results"}
_STOP = set(["a", "an", "the", "to", "of", "and", "or", "in", "on", "for", "with", "by", "is", "are", "be", "was", "were", "it", "its", "this", "that", "as", "at", "from", "should", "must", "agent", "user", "tool", "call", "called", "calls", "answer", "response", "not", "no", "do", "does", "did", "when", "only", "than", "then"])
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def signature(text: str, n: int = 8) -> str:
    toks = re.findall(r"[a-z][a-z_\-]+", (text or "").lower())
    sig = [t for t in toks if t not in _STOP and not re.search(r"\d", t)]
    return " ".join(sig[:n])


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z][a-z_\-]+", (text or "").lower()) if t not in _STOP}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def fingerprint(f: Finding) -> str:
    if f.category in TOOL_CATEGORIES and f.tool_name:
        key = f"tool:{f.tool_name}"
    elif f.rubric_id and not f.rubric_id.startswith("G-"):
        key = ""  # a specific rubric item is already a precise identity
    else:
        key = signature(f.expected)
    raw = "|".join([f.agent_name, f.category, f.rubric_id, key])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class CorrelationResult:
    new: list[str]
    recurring: list[str]
    resolved: list[str]


def correlate(store: SQLStore, run_id: str, findings: Iterable[Finding], *, agents_evaluated: Iterable[str],
              harness_version: str = "", similarity: float = 0.5, resolve_after_runs: int = 2,
              titles: dict[str, str] | None = None) -> CorrelationResult:
    """Attach verified findings to insights and update every insight's status."""
    titles = titles or {}
    res = CorrelationResult([], [], [])
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.verified is False:
            continue
        f.fingerprint = f.fingerprint or fingerprint(f)
        groups[f.fingerprint].append(f)

    open_insights = [i for i in store.insights() if i.status != InsightStatus.RESOLVED]
    touched: set[str] = set()
    for fp, fs in groups.items():
        head = min(fs, key=lambda x: _SEV_RANK.get(x.severity, 1))
        ins = store.insight_by_fingerprint(fp)
        if ins is None:
            # Similarity fallback: same agent/category/rubric with a near-identical expectation.
            ins = next((i for i in open_insights if i.agent_name == head.agent_name and i.category == head.category
                        and i.rubric_id == head.rubric_id and i.insight_id not in touched
                        and jaccard(i.expected, head.expected) >= similarity), None)
        convs = sorted({f.conversation_id for f in fs})
        if ins is None:
            title = titles.get(head.rubric_id) or head.expected
            ins = Insight(
                insight_id=new_id("ins"), fingerprint=fp, title=f"[{head.category}] {title}"[:160],
                category=head.category, rubric_id=head.rubric_id, agent_name=head.agent_name,
                status=InsightStatus.NEW, severity=head.severity, first_seen_run=run_id, last_seen_run=run_id,
                tool_name=head.tool_name, expected=head.expected, harness_version_first=harness_version,
                harness_version_last=harness_version,
            )
            res.new.append(ins.insight_id)
        else:
            if ins.last_seen_run != run_id:
                ins.status = InsightStatus.RECURRING
                res.recurring.append(ins.insight_id)
            ins.last_seen_run = run_id
            ins.harness_version_last = harness_version
            if _SEV_RANK.get(head.severity, 1) < _SEV_RANK.get(ins.severity, 1):
                ins.severity = head.severity
        ins.occurrence_count += len(fs)
        ins.conversation_count += len(convs)
        ins.quiet_runs = 0
        ins.updated_at = now_s()
        store.save_insight(ins)
        store.save_occurrence(ins.insight_id, run_id, len(fs), convs, str(ins.status))
        for f in fs:
            f.insight_id = ins.insight_id
            store.save_finding(f)
        touched.add(ins.insight_id)

    agents = set(agents_evaluated)
    for ins in store.insights():
        if ins.insight_id in touched or ins.status == InsightStatus.RESOLVED or ins.agent_name not in agents:
            continue
        ins.quiet_runs += 1
        if ins.quiet_runs >= resolve_after_runs:
            ins.status = InsightStatus.RESOLVED
            res.resolved.append(ins.insight_id)
            store.save_occurrence(ins.insight_id, run_id, 0, [], str(ins.status))
        ins.updated_at = now_s()
        store.save_insight(ins)
    store.commit()
    return res
