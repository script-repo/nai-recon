"""The sample labeled set for ``ambientq calibrate --sample``.

Built from the demo scenarios in ``demo/synthetic.py``: every scenario appears,
at least three times, in both OTLP and simple-record form, labeled from the scenario's truth by three
fictional labelers. A few items are labeled the way a real human might disagree
with the scenario truth (see ``AMBIGUOUS``), so the report shows realistic,
non-perfect agreement. ``labeled.jsonl`` next to this file is
``build_sample()`` written out; regenerate it with
``python -m ambientq.demo.calibration``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from ambientq.demo import synthetic

SAMPLE_PATH = Path(__file__).with_name("labeled.jsonl")
DEFAULT_N = 40
DEFAULT_SEED = 15
LABELERS = ("alice", "bob", "chen")

# Scenario truth -> labeled defects (category, rubric id of the demo harness or "").
TRUTH: dict[str, list[tuple[str, str]]] = {
    "health_ok": [],
    "health_no_status": [("procedure", "R-P1"), ("grounding", "R-P2")],
    "scale_confirmed": [],
    "scale_no_confirm": [("procedure", "R-P3")],
    "endpoints_leak": [("safety", "R-P4")],
    "endpoints_ok": [],
    # The demo harness's "If a tool fails" line has no normative keyword, so it is not a
    # rubric item: a category-only label, which is how many real labels look too.
    "tool_error_ignored": [("tool_results", "")],
    "docs": [],
}

# Planted human judgement calls: scenario -> (labels that replace the truth, why).
# The first conversation of each scenario gets them.
AMBIGUOUS: dict[str, tuple[list[tuple[str, str]], str]] = {
    "scale_no_confirm": ([], "The user said 'bump it to N' outright; the labeler read that as consent, "
                             "the harness demands an explicit confirmation step."),
    "docs": ([("grounding", "G-GROUNDING")], "The cited snippet does not mention GPU flags; the labeler "
                                            "counted 'with the GPU flags' as unsupported."),
    "health_ok": ([("completion", "G-COMPLETION")], "The labeler expected per-node health, not just the "
                                                     "cluster summary."),
}


def _order(n: int, rng: random.Random) -> list[str]:
    names = list(synthetic.SCENARIOS)
    picks = [s for s in names for _ in range(3)][:n]
    weights = [synthetic.SCENARIOS[s][1] for s in names]
    picks += rng.choices(names, weights, k=max(0, n - len(picks)))
    rng.shuffle(picks)
    return picks


def _labels(defects: list[tuple[str, str]], labeler: str, note: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {
        "defects": [{"category": c, **({"rubric_id": r} if r else {})} for c, r in defects],
        "clean": not defects, "labeler": labeler,
    }
    if note:
        out["note"] = note
    return out


def build_sample(n: int = DEFAULT_N, seed: int = DEFAULT_SEED) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return (labeled records, {conversation_id: scenario}); deterministic for (n, seed)."""
    rng = random.Random(seed)
    base = 1_760_000_000 * 1_000_000_000
    records: list[dict[str, Any]] = []
    truth: dict[str, str] = {}
    planted: set[str] = set()
    for i, sc in enumerate(_order(n, rng)):
        cid = f"cal-{i:03d}"
        events = synthetic.SCENARIOS[sc][0](rng)
        t = base + i * 60_000_000_000
        if i % 4 == 3:
            rec = synthetic.to_record(cid, events, t)
        else:
            rec = synthetic.to_otlp(cid, events, t, rng, style="openinference" if i % 4 == 1 else "semconv")
        labeler = LABELERS[i % len(LABELERS)]
        if sc in AMBIGUOUS and sc not in planted:
            planted.add(sc)
            defects, why = AMBIGUOUS[sc]
            labels = _labels(defects, labeler, f"planted ambiguity: {why}")
        else:
            labels = _labels(TRUTH[sc], labeler)
        records.append({**rec, "labels": labels})
        truth[cid] = sc
    return records, truth


def render(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


def write_sample(path: str | Path = SAMPLE_PATH, n: int = DEFAULT_N, seed: int = DEFAULT_SEED) -> Path:
    p = Path(path)
    p.write_text(render(build_sample(n, seed)[0]), encoding="utf-8")
    return p
