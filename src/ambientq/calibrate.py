"""Judge calibration: measure the LLM judge against human labels before trusting it.

A labeled set is JSON Lines, one conversation per line: any record
``ingest.jsonl.parse_records`` understands (a simple conversation record with
OpenAI messages, or an OTLP export request holding one conversation) plus a
``labels`` object::

    {"labels": {"defects": [{"category": "procedure", "rubric_id": "R-P3", "note": "..."}],
                "clean": false, "labeler": "alice"}}

``calibrate`` runs the judge (and, unless disabled, the evidence check and the
LLM verifier) on every conversation and compares the verified findings with the
labels at conversation level: per category, per rubric item, overall
agreement with Cohen's kappa, a ``min_confidence`` sweep, and the judge's
tokens and latency, which size the NAI endpoint.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ambientq.checks import CheckFn, load_checks, run_checks
from ambientq.conversation import assemble
from ambientq.harness import Harness
from ambientq.ingest.jsonl import parse_records
from ambientq.judge.judge import judge_conversation
from ambientq.judge.rubric import Rubric, build_rubric
from ambientq.judge.verifier import check_evidence
from ambientq.judge.verifier import verify as verify_findings
from ambientq.llm import BudgetExceeded, LLMClient, LLMError
from ambientq.models import CATEGORIES, Conversation, Finding, Turn
from ambientq.sampling import hash_fraction

log = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


class LabelError(ValueError):
    """A labeled file has lines that cannot be used; the message lists every one."""


# --- labeled format -----------------------------------------------------------------


@dataclass
class LabelDefect:
    category: str
    rubric_id: str = ""
    note: str = ""


@dataclass
class LabeledConversation:
    conversation: Conversation
    defects: list[LabelDefect] = field(default_factory=list)
    clean: bool = True
    labeler: str = ""
    note: str = ""
    line: int = 0

    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id

    @property
    def flagged(self) -> bool:
        return not self.clean

    def categories(self) -> set[str]:
        return {d.category for d in self.defects}

    def rubric_ids(self) -> set[str]:
        return {d.rubric_id for d in self.defects if d.rubric_id}

    def rubric_resolved(self) -> bool:
        """True when every defect names its rubric item (or the conversation is clean)."""
        return self.clean or (bool(self.defects) and all(d.rubric_id for d in self.defects))

    def labels_dict(self) -> dict[str, Any]:
        return {"clean": self.clean, "labeler": self.labeler, "note": self.note,
                "defects": [{k: v for k, v in d.__dict__.items() if v} for d in self.defects]}


@dataclass
class LabelParse:
    items: list[LabeledConversation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unlabeled: list[str] = field(default_factory=list)  # export stubs nobody has labeled yet


def _parse_labels(raw: Any) -> tuple[list[LabelDefect], bool | None, str, str]:
    """Validate one ``labels`` object -> (defects, clean or None if unlabeled, labeler, note)."""
    if not isinstance(raw, dict):
        raise ValueError("'labels' must be an object")
    defects_raw = raw.get("defects") or []
    if not isinstance(defects_raw, list):
        raise ValueError("'labels.defects' must be a list")
    defects = []
    for j, d in enumerate(defects_raw):
        if not isinstance(d, dict):
            raise ValueError(f"labels.defects[{j}] must be an object")
        cat = str(d.get("category") or "").strip().lower()
        if cat not in CATEGORIES:
            raise ValueError(f"labels.defects[{j}].category {d.get('category')!r} is not one of "
                             f"{', '.join(CATEGORIES)}")
        rid = d.get("rubric_id") or ""
        if not isinstance(rid, str):
            raise ValueError(f"labels.defects[{j}].rubric_id must be a string")
        defects.append(LabelDefect(cat, rid.strip(), str(d.get("note") or "")))
    clean = raw.get("clean")
    if clean is not None and not isinstance(clean, bool):
        raise ValueError("'labels.clean' must be true, false or null")
    if clean is True and defects:
        raise ValueError("'labels.clean' is true but defects are listed")
    if clean is None and defects:
        clean = False
    return defects, clean, str(raw.get("labeler") or ""), str(raw.get("note") or "")


def _record_conversation(rec: dict[str, Any]) -> Conversation:
    parsed = parse_records([rec])
    if parsed.errors:
        raise ValueError(parsed.errors[0].removeprefix("record 0: "))
    convs = list(parsed.conversations) + assemble(parsed.spans)
    if len(convs) != 1:
        raise ValueError(f"record holds {len(convs)} conversations; a labeled line must hold exactly one")
    conv = convs[0]
    if not conv.turns:
        raise ValueError(f"conversation {conv.conversation_id} has no turns")
    return conv


def parse_labeled(lines: Iterable[str], source: str = "") -> LabelParse:
    """Parse labeled JSONL lines; every bad line is reported with its number, never skipped silently."""
    out = LabelParse()
    seen: dict[str, int] = {}
    where = f"{source}:" if source else "line "
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise ValueError("a line must be a JSON object")
            if "labels" not in rec:
                raise ValueError("no 'labels' object")
            defects, clean, labeler, note = _parse_labels(rec["labels"])
            conv = _record_conversation({k: v for k, v in rec.items() if k != "labels"})
            if conv.conversation_id in seen:
                raise ValueError(f"conversation {conv.conversation_id} already labeled on line "
                                 f"{seen[conv.conversation_id]}")
            seen[conv.conversation_id] = n
            if clean is None:
                out.unlabeled.append(conv.conversation_id)
                continue
            out.items.append(LabeledConversation(conv, defects, clean, labeler, note, n))
        except json.JSONDecodeError as exc:
            out.errors.append(f"{where}{n}: not valid JSON ({exc.msg})")
        except (ValueError, TypeError, KeyError) as exc:
            out.errors.append(f"{where}{n}: {exc}")
    return out


def read_labeled(path: str | Path) -> LabelParse:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        return parse_labeled(fh, p.name)


def load_labeled(path: str | Path) -> list[LabeledConversation]:
    """Load a labeled set. Raises ``LabelError`` listing every bad line; unlabeled stubs are skipped."""
    parsed = read_labeled(path)
    if parsed.errors:
        shown = "\n  ".join(parsed.errors[:50])
        more = f"\n  ... and {len(parsed.errors) - 50} more" if len(parsed.errors) > 50 else ""
        raise LabelError(f"{len(parsed.errors)} bad line(s) in {path}:\n  {shown}{more}")
    if parsed.unlabeled:
        log.warning("%d conversation(s) in %s have no labels yet and are skipped", len(parsed.unlabeled), path)
    return parsed.items


# --- metrics ----------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n (95% by default); (0, 1) when n == 0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def cohen_kappa(tp: int, fp: int, fn: int, tn: int) -> float:
    """Cohen's kappa for two binary raters (judge vs human)."""
    n = tp + fp + fn + tn
    if n == 0:
        return 0.0
    po = (tp + tn) / n
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (n * n)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


@dataclass
class Counts:
    """Conversation-level confusion counts for one label (a category, a rubric item, or "any defect")."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, human: bool, judge: bool) -> None:
        if human and judge:
            self.tp += 1
        elif judge:
            self.fp += 1
        elif human:
            self.fn += 1
        else:
            self.tn += 1

    def __iadd__(self, other: Counts) -> Counts:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.tn += other.tn
        return self

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def support(self) -> int:
        return self.tp + self.fn

    @property
    def predicted(self) -> int:
        return self.tp + self.fp

    @property
    def precision(self) -> float | None:
        return self.tp / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        return self.tp / self.support if self.support else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def agreement(self) -> float | None:
        return (self.tp + self.tn) / self.n if self.n else None

    @property
    def kappa(self) -> float:
        return cohen_kappa(self.tp, self.fp, self.fn, self.tn)

    def to_dict(self) -> dict[str, Any]:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn, "support": self.support,
                "predicted": self.predicted, "precision": _r(self.precision), "recall": _r(self.recall),
                "f1": _r(self.f1), "precision_ci95": [_r(x) for x in wilson(self.tp, self.predicted)],
                "recall_ci95": [_r(x) for x in wilson(self.tp, self.support)]}


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def counted(f: Finding, min_confidence: float) -> bool:
    """A finding counts as a prediction when the verifier did not reject it and it clears the threshold."""
    return f.verified is not False and f.confidence >= min_confidence


@dataclass
class Scores:
    flag: Counts
    categories: dict[str, Counts]
    rubric: dict[str, Counts]
    disagreements: list[dict[str, Any]]

    @property
    def micro(self) -> Counts:
        total = Counts()
        for c in self.categories.values():
            total += c
        return total

    @property
    def agreement(self) -> float | None:
        return self.flag.agreement

    @property
    def kappa(self) -> float:
        return self.flag.kappa


def _finding_dict(f: Finding, min_confidence: float) -> dict[str, Any]:
    return {"source": f.source, "category": f.category, "rubric_id": f.rubric_id,
            "confidence": round(f.confidence, 3), "verified": f.verified, "counted": counted(f, min_confidence),
            "actual": f.actual, "verifier_note": f.verifier_note}


def score(labeled: Sequence[LabeledConversation], predictions: dict[str, list[Finding]], *,
          min_confidence: float = 0.0) -> Scores:
    """Compare predictions with labels at conversation level.

    Categories and rubric items are scored over every category/item that a human
    or the judge used. Rubric-level counts only use conversations whose labels
    are rubric-resolved (clean, or every defect names its rubric item), so a
    category-only label never counts as a miss for a specific item.
    """
    kept = {cid: [f for f in fs if counted(f, min_confidence)] for cid, fs in predictions.items()}
    cats = sorted({c for lc in labeled for c in lc.categories()}
                  | {f.category for lc in labeled for f in kept.get(lc.conversation_id, [])},
                  key=lambda c: CATEGORIES.index(c) if c in CATEGORIES else len(CATEGORIES))
    rids = sorted({r for lc in labeled for r in lc.rubric_ids()}
                  | {f.rubric_id for lc in labeled for f in kept.get(lc.conversation_id, []) if f.rubric_id})
    flag, by_cat = Counts(), {c: Counts() for c in cats}
    by_rid = {r: Counts() for r in rids}
    disagreements = []
    for lc in labeled:
        found = kept.get(lc.conversation_id, [])
        jcats = {f.category for f in found}
        jrids = {f.rubric_id for f in found if f.rubric_id}
        hcats = lc.categories()
        flag.add(lc.flagged, bool(found))
        for c in cats:
            by_cat[c].add(c in hcats, c in jcats)
        if lc.rubric_resolved():
            for r in rids:
                by_rid[r].add(r in lc.rubric_ids(), r in jrids)
        if lc.flagged != bool(found) or hcats != jcats:
            disagreements.append({
                "conversation_id": lc.conversation_id, "line": lc.line, "human": lc.labels_dict(),
                "judge": [_finding_dict(f, min_confidence) for f in predictions.get(lc.conversation_id, [])],
                "missed": sorted(hcats - jcats), "extra": sorted(jcats - hcats),
                "flag": {"human": lc.flagged, "judge": bool(found)},
            })
    return Scores(flag, by_cat, by_rid, disagreements)


def sweep(labeled: Sequence[LabeledConversation], predictions: dict[str, list[Finding]],
          thresholds: Iterable[float] = DEFAULT_THRESHOLDS) -> list[dict[str, Any]]:
    """Precision/recall (micro over categories, and conversation flag) at each ``min_confidence``."""
    rows = []
    for t in sorted(set(thresholds)):
        s = score(labeled, predictions, min_confidence=t)
        m = s.micro
        rows.append({"threshold": round(t, 4), "precision": _r(m.precision), "recall": _r(m.recall),
                     "f1": _r(m.f1), "flag_precision": _r(s.flag.precision), "flag_recall": _r(s.flag.recall),
                     "agreement": _r(s.agreement), "kappa": _r(s.kappa),
                     "findings": sum(counted(f, t) for fs in predictions.values() for f in fs)})
    return rows


def recommend_threshold(rows: Sequence[dict[str, Any]]) -> float | None:
    """Best micro F1; ties go to higher precision, then to the lower threshold (keeps recall)."""
    usable = [r for r in rows if r["f1"] is not None]
    if not usable:
        return None
    best = max(usable, key=lambda r: (r["f1"], r["precision"] or 0.0, -r["threshold"]))
    return float(best["threshold"])


# --- running the judge -------------------------------------------------------------


@dataclass
class ConversationCost:
    conversation_id: str
    judge_prompt_tokens: int = 0
    judge_completion_tokens: int = 0
    judge_latency_s: float = 0.0
    verify_prompt_tokens: int = 0
    verify_completion_tokens: int = 0
    verify_latency_s: float = 0.0
    verify_calls: int = 0

    @property
    def tokens(self) -> int:
        return (self.judge_prompt_tokens + self.judge_completion_tokens + self.verify_prompt_tokens
                + self.verify_completion_tokens)


def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _stats(values: Sequence[float], nd: int = 3) -> dict[str, float]:
    return {"mean": round(sum(values) / len(values), nd) if values else 0.0, "p50": round(_pct(values, 0.5), nd),
            "p95": round(_pct(values, 0.95), nd), "max": round(max(values), nd) if values else 0.0}


@dataclass
class CalibrationReport:
    labeled: int
    judged: int
    model: str
    verifier_model: str | None
    verify: bool
    with_checks: bool
    min_confidence: float
    scores: Scores
    sweep: list[dict[str, Any]]
    recommended_threshold: float | None
    costs: list[ConversationCost]
    conversations_per_day: int
    sampling_rate: float
    errors: list[str] = field(default_factory=list)
    unknown_rubric_ids: list[str] = field(default_factory=list)
    labelers: dict[str, int] = field(default_factory=dict)

    def usage(self) -> dict[str, Any]:
        c = self.costs
        judged_per_day = self.conversations_per_day * self.sampling_rate
        mean_tokens = sum(x.tokens for x in c) / len(c) if c else 0.0
        mean_busy = sum(x.judge_latency_s + x.verify_latency_s for x in c) / len(c) if c else 0.0
        busy_s = judged_per_day * mean_busy
        return {
            "judge_prompt_tokens": _stats([x.judge_prompt_tokens for x in c], 1),
            "judge_completion_tokens": _stats([x.judge_completion_tokens for x in c], 1),
            "judge_latency_s": _stats([x.judge_latency_s for x in c]),
            "verify_tokens": _stats([x.verify_prompt_tokens + x.verify_completion_tokens for x in c], 1),
            "verify_calls": _stats([float(x.verify_calls) for x in c], 2),
            "verify_latency_s": _stats([x.verify_latency_s for x in c]),
            "tokens_per_conversation": round(mean_tokens, 1),
            "total_tokens": sum(x.tokens for x in c),
            "sizing": {
                "conversations_per_day": self.conversations_per_day, "sampling_rate": self.sampling_rate,
                "judged_per_day": round(judged_per_day, 1), "tokens_per_day": int(judged_per_day * mean_tokens),
                "busy_seconds_per_day": round(busy_s, 1),
                # Concurrent requests needed to keep up when calls run back to back at this latency.
                "min_concurrency": max(1, math.ceil(busy_s / 86_400)) if busy_s else 0,
            },
        }

    def summary(self) -> dict[str, Any]:
        """Headline numbers only (no conversation content): what the audit event records."""
        m = self.scores.micro
        return {"labeled": self.labeled, "judged": self.judged, "model": self.model,
                "verifier_model": self.verifier_model, "verify": self.verify, "with_checks": self.with_checks,
                "min_confidence": self.min_confidence, "agreement": _r(self.scores.agreement),
                "kappa": _r(self.scores.kappa), "precision": _r(m.precision), "recall": _r(m.recall),
                "f1": _r(m.f1), "recommended_threshold": self.recommended_threshold,
                "disagreements": len(self.scores.disagreements), "errors": len(self.errors),
                "total_tokens": sum(x.tokens for x in self.costs)}

    def to_dict(self) -> dict[str, Any]:
        s = self.scores
        return {
            "summary": self.summary(),
            "conversation": {**s.flag.to_dict(), "agreement": _r(s.agreement), "kappa": _r(s.kappa),
                             "confusion": {"human_flagged": {"judge_flagged": s.flag.tp, "judge_clean": s.flag.fn},
                                           "human_clean": {"judge_flagged": s.flag.fp, "judge_clean": s.flag.tn}}},
            "micro": s.micro.to_dict(),
            "categories": {k: v.to_dict() for k, v in s.categories.items()},
            "rubric": {k: {**v.to_dict(), "n": v.n} for k, v in s.rubric.items()},
            "sweep": self.sweep,
            "recommended_threshold": self.recommended_threshold,
            "usage": self.usage(),
            "per_conversation": [x.__dict__ for x in self.costs],
            "disagreements": s.disagreements,
            "labelers": self.labelers,
            "unknown_rubric_ids": self.unknown_rubric_ids,
            "errors": self.errors,
        }

    def to_text(self) -> str:
        return format_report(self)


def _fmt(x: float | None) -> str:
    return "   -  " if x is None else f"{x:6.2f}"


def _ci(lo_hi: tuple[float, float]) -> str:
    return f"[{lo_hi[0]:.2f}-{lo_hi[1]:.2f}]"


def _table(title: str, rows: dict[str, Counts]) -> list[str]:
    out = [f"{title:<24} {'tp':>4} {'fp':>4} {'fn':>4} {'tn':>4}  {'precision':<20} {'recall':<20} {'f1':>6}"]
    for k, c in rows.items():
        p = f"{_fmt(c.precision)} {_ci(wilson(c.tp, c.predicted))}"
        r = f"{_fmt(c.recall)} {_ci(wilson(c.tp, c.support))}"
        out.append(f"  {k[:22]:<22} {c.tp:>4} {c.fp:>4} {c.fn:>4} {c.tn:>4}  {p:<20} {r:<20} {_fmt(c.f1)}")
    return out


def _human(h: dict[str, Any]) -> str:
    who = f" ({h['labeler']})" if h.get("labeler") else ""
    if h["clean"]:
        return f"clean{who}"
    ds = ", ".join(d["category"] + (f" {d['rubric_id']}" if d.get("rubric_id") else "") for d in h["defects"])
    return (ds or "flagged, no category") + who


def format_report(rep: CalibrationReport) -> str:
    s, u = rep.scores, rep.usage()
    f = s.flag
    lines = [
        f"judge calibration: {rep.labeled} labeled conversation(s), {rep.judged} judged; model {rep.model}, "
        f"verifier {rep.verifier_model or '-' if rep.verify else 'off'}{', checks on' if rep.with_checks else ''}, "
        f"min_confidence {rep.min_confidence:.2f}",
        "",
        f"conversation level (judge flags iff a human did): agreement {_fmt(s.agreement).strip()} "
        f"({f.tp + f.tn}/{f.n}), Cohen's kappa {s.kappa:.2f}",
        f"  human flagged: judge flagged {f.tp:>3}, judge clean {f.fn:>3}",
        f"  human clean:   judge flagged {f.fp:>3}, judge clean {f.tn:>3}",
        "",
        *_table("per category", s.categories),
        *_table("  all categories (micro)", {"micro": s.micro})[1:],
    ]
    if s.rubric:
        lines += ["", *_table("per rubric item", s.rubric)]
    lines += ["", "min_confidence sweep    precision  recall      f1  findings"]
    for r in rep.sweep:
        mark = "  <- recommended" if r["threshold"] == rep.recommended_threshold else ""
        lines.append(f"  {r['threshold']:<20.2f} {_fmt(r['precision'])}  {_fmt(r['recall'])}  {_fmt(r['f1'])}"
                     f"  {r['findings']:>8}{mark}")
    jp, jc, lat = u["judge_prompt_tokens"], u["judge_completion_tokens"], u["judge_latency_s"]
    sz = u["sizing"]
    lines += [
        "",
        f"judge per conversation: prompt tokens mean {jp['mean']:.0f} (p95 {jp['p95']:.0f}), completion mean "
        f"{jc['mean']:.0f} (p95 {jc['p95']:.0f}); latency mean {lat['mean']:.3f}s, p50 {lat['p50']:.3f}s, "
        f"p95 {lat['p95']:.3f}s",
        f"verifier per conversation: {u['verify_calls']['mean']:.1f} call(s), {u['verify_tokens']['mean']:.0f} "
        f"tokens, {u['verify_latency_s']['mean']:.3f}s",
        f"sizing: {sz['conversations_per_day']:,} conversations/day x sampling {sz['sampling_rate']:.0%} = "
        f"{sz['judged_per_day']:,.0f} judged/day -> ~{sz['tokens_per_day']:,} tokens/day "
        f"({u['tokens_per_conversation']:,.0f} per conversation), at least {sz['min_concurrency']} concurrent "
        "request(s)",
    ]
    if s.disagreements:
        lines += ["", f"disagreements ({len(s.disagreements)}):"]
        for d in s.disagreements:
            judge = ", ".join(f"{x['category']} {x['rubric_id'] or '-'} ({x['confidence']:.2f}"
                              + ("" if x["counted"] else ", rejected" if x["verified"] is False else ", below")
                              + ")" for x in d["judge"]) or "no findings"
            lines.append(f"  {d['conversation_id']}: human {_human(d['human'])} | judge {judge}")
            if d["human"].get("note"):
                lines.append(f"      note: {d['human']['note']}")
    if rep.unknown_rubric_ids:
        lines += ["", f"labels name rubric ids the harness does not define: {', '.join(rep.unknown_rubric_ids)}"]
    if rep.errors:
        lines += ["", f"errors ({len(rep.errors)}):", *[f"  {e}" for e in rep.errors[:20]]]
    return "\n".join(lines)


def _tokens(llm: LLMClient) -> tuple[int, int, int]:
    return llm.budget.input_tokens, llm.budget.output_tokens, llm.budget.calls


def calibrate(labeled: Sequence[LabeledConversation], harness: Harness, judge_llm: LLMClient, *,
              verifier_llm: LLMClient | None = None, verify: bool = True, llm_verify: bool = True,
              with_checks: bool = False, checks: dict[str, CheckFn] | None = None,
              min_confidence: float = 0.0, thresholds: Iterable[float] = DEFAULT_THRESHOLDS,
              rubric: Rubric | None = None, goal: str | None = None, max_findings: int = 8,
              conversations_per_day: int = 1000, sampling_rate: float = 0.2,
              progress: Callable[[int, int], None] | None = None) -> CalibrationReport:
    """Judge every labeled conversation and compare the verified findings with the labels.

    The judge keeps every finding (``min_confidence`` 0) so the threshold sweep
    can be computed from one pass; the headline metrics use ``min_confidence``.
    With ``verify``, every finding passes the deterministic evidence check and,
    with ``llm_verify``, the LLM verifier (``verifier_llm``, default the judge).
    """
    rubric = rubric or build_rubric(harness, goal)
    vllm = (verifier_llm or judge_llm) if verify and llm_verify else None
    check_fns = (checks if checks is not None else load_checks()) if with_checks else {}
    predictions: dict[str, list[Finding]] = {}
    costs: list[ConversationCost] = []
    errors: list[str] = []
    for i, lc in enumerate(labeled):
        conv = lc.conversation
        cost = ConversationCost(conv.conversation_id)
        found: list[Finding] = []
        for f in run_checks(conv, harness, check_fns):
            if f.verified is None:
                f.verified, f.verifier_note = check_evidence(f, conv, rubric)
            found.append(f)
        try:
            p0, c0, _ = _tokens(judge_llm)
            t0 = time.perf_counter()
            judged = judge_conversation(judge_llm, conv, harness, rubric, max_findings=max_findings,
                                        min_confidence=0.0)
            cost.judge_latency_s = round(time.perf_counter() - t0, 4)
            p1, c1, _ = _tokens(judge_llm)
            cost.judge_prompt_tokens, cost.judge_completion_tokens = p1 - p0, c1 - c0
            if verify:
                vb = _tokens(vllm) if vllm is not None else (0, 0, 0)
                t0 = time.perf_counter()
                verify_findings(judged, conv, rubric, vllm)
                cost.verify_latency_s = round(time.perf_counter() - t0, 4)
                if vllm is not None:
                    va = _tokens(vllm)
                    cost.verify_prompt_tokens, cost.verify_completion_tokens = va[0] - vb[0], va[1] - vb[1]
                    cost.verify_calls = va[2] - vb[2]
        except BudgetExceeded as exc:
            errors.append(f"{conv.conversation_id}: {exc}; stopped, {len(labeled) - i} not judged")
            break
        except LLMError as exc:
            errors.append(f"{conv.conversation_id}: judge failed: {exc}")
            continue
        predictions[conv.conversation_id] = found + judged
        costs.append(cost)
        if progress:
            progress(i + 1, len(labeled))
    scored = [lc for lc in labeled if lc.conversation_id in predictions]
    rows = sweep(scored, predictions, thresholds)
    known = rubric.ids() | {f.rubric_id for fs in predictions.values() for f in fs if f.source != "judge"}
    labelers: dict[str, int] = {}
    for lc in labeled:
        labelers[lc.labeler or "-"] = labelers.get(lc.labeler or "-", 0) + 1
    return CalibrationReport(
        labeled=len(labeled), judged=len(scored), model=judge_llm.model,
        verifier_model=vllm.model if vllm is not None else None, verify=verify, with_checks=with_checks,
        min_confidence=min_confidence, scores=score(scored, predictions, min_confidence=min_confidence),
        sweep=rows, recommended_threshold=recommend_threshold(rows), costs=costs,
        conversations_per_day=conversations_per_day, sampling_rate=sampling_rate, errors=errors,
        unknown_rubric_ids=sorted({r for lc in labeled for r in lc.rubric_ids()} - known), labelers=labelers,
    )


# --- exporting conversations for labeling -----------------------------------------------


def conversation_to_record(conv: Conversation) -> dict[str, Any]:
    """A simple conversation record (OpenAI messages) that ``record_to_conversation`` reads back."""
    msgs: list[dict[str, Any]] = []
    open_calls: list[tuple[str, str]] = []  # (call id, tool name) not yet answered
    for t in conv.turns:
        if t.role == "assistant" and t.tool_name:
            cid = t.tool_call_id or f"call_{t.index}"
            open_calls.append((cid, t.tool_name))
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": cid, "type": "function",
                 "function": {"name": t.tool_name, "arguments": json.dumps(t.tool_args or {})}}]})
        elif t.role == "tool":
            cid = t.tool_call_id or next((c for c, n in reversed(open_calls) if n == t.tool_name), "")
            open_calls = [(c, n) for c, n in open_calls if c != cid]
            m: dict[str, Any] = {"role": "tool", "tool_call_id": cid, "content": t.content}
            if t.tool_name:
                m["name"] = t.tool_name
            if t.is_error:
                m["is_error"] = True
            msgs.append(m)
        else:
            msgs.append({"role": t.role, "content": t.content})
    rec: dict[str, Any] = {"conversation_id": conv.conversation_id, "agent": conv.agent_name}
    for key, val in (("team", conv.team), ("harness_version", conv.harness_version),
                     ("model", conv.metadata.get("model")), ("started_ns", conv.started_ns)):
        if val:
            rec[key] = val
    rec["messages"] = msgs
    return rec


def label_stub() -> dict[str, Any]:
    return {"defects": [], "clean": None, "labeler": "", "note": ""}


def select_for_labeling(convs: Sequence[Conversation], n: int, *, flagged: set[str] | frozenset[str] = frozenset(),
                        flagged_share: float = 0.5, seed: str = "ambientq") -> list[Conversation]:
    """Pick ``n`` conversations deterministically, ``flagged_share`` of them from ``flagged``.

    Defects are rare in real traffic, so a purely random sample gives recall
    almost nothing to measure; mixing in conversations ambientq already flagged
    fixes that (precision is then measured on a flag-enriched set, see
    docs/CALIBRATION.md). Shortfalls in one pool are filled from the other.
    """
    def order(cs: Iterable[Conversation]) -> list[Conversation]:
        return sorted(cs, key=lambda c: hash_fraction(c.conversation_id, seed))

    hit = order(c for c in convs if c.conversation_id in flagged)
    rest = order(c for c in convs if c.conversation_id not in flagged)
    want = min(len(hit), round(n * max(0.0, min(1.0, flagged_share))))
    pick = hit[:want] + rest[:n - want]
    if len(pick) < n:
        pick += hit[want:want + n - len(pick)]
    return sorted(pick, key=lambda c: (c.started_ns, c.conversation_id))


def export_for_labeling(store: Any, *, since_ns: int = 0, n: int = 50, agent: str | None = None,
                        team: str | None = None, flagged_share: float = 0.5,
                        seed: str = "ambientq") -> list[dict[str, Any]]:
    """Labeled-format records with an empty ``labels`` stub, for humans to fill in.

    Judge findings are deliberately left out: showing them to labelers anchors
    the labels to the judge and inflates agreement.
    """
    convs = [c for c in store.list_conversations(since_ns=since_ns, agent=agent, team=team) if c.turns]
    flagged = {r["conversation_id"] for r in store.query(
        "SELECT DISTINCT conversation_id FROM findings WHERE verified IS NULL OR verified <> 0")}
    return [{**conversation_to_record(c), "labels": label_stub()}
            for c in select_for_labeling(convs, n, flagged=flagged, flagged_share=flagged_share, seed=seed)]


def turns_equal(a: Sequence[Turn], b: Sequence[Turn]) -> bool:
    """Same roles, text, tool names/args and error flags (ids and timestamps may differ)."""
    def key(t: Turn) -> tuple[Any, ...]:
        return t.role, t.content or "", t.tool_name or "", json.dumps(t.tool_args or {}, sort_keys=True), t.is_error
    return len(a) == len(b) and all(key(x) == key(y) for x, y in zip(a, b, strict=True))
