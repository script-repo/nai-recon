"""Token pricing and usage summaries (SPEC §11.7).

Prices are per 1k tokens in ``pricing.currency``. ``pricing.models`` keys are
model names or globs (``llama-3-1-*``); an exact name wins over a glob, and
globs are tried in config order. Unpriced models use ``pricing.default``. On-prem
GPU cost can be expressed as an effective price per 1k tokens.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

GROUP_BY = ("agent", "model", "team", "source")
_COLUMN = {"agent": "agent_name", "model": "model", "team": "team", "source": "source"}


@dataclass(frozen=True)
class Price:
    input_per_1k: float = 0.0
    output_per_1k: float = 0.0

    @classmethod
    def from_dict(cls, d: Any) -> Price:
        d = d if isinstance(d, dict) else {}
        return cls(float(d.get("input_per_1k") or 0.0), float(d.get("output_per_1k") or 0.0))


@dataclass
class Pricing:
    currency: str = "USD"
    default: Price = field(default_factory=Price)
    models: dict[str, Price] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Any) -> Pricing:
        sec = cfg.section("pricing") if hasattr(cfg, "section") else dict(cfg or {})
        return cls(currency=str(sec.get("currency") or "USD"), default=Price.from_dict(sec.get("default")),
                   models={str(k): Price.from_dict(v) for k, v in (sec.get("models") or {}).items()})

    def price(self, model: str) -> Price:
        if model in self.models:
            return self.models[model]
        for pattern, p in self.models.items():
            if any(c in pattern for c in "*?[") and fnmatch.fnmatchcase(model, pattern):
                return p
        return self.default

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        p = self.price(model or "unknown")
        return (input_tokens or 0) / 1000 * p.input_per_1k + (output_tokens or 0) / 1000 * p.output_per_1k


def summarize(rows: Iterable[dict[str, Any]], pricing: Pricing, by: str = "model") -> dict[str, Any]:
    """Group usage rows by ``agent``, ``model``, ``team`` or ``source``; tokens and cost per group.

    Returns ``{"by", "currency", "rows": [{key, calls, input_tokens, output_tokens, tokens, cost}],
    "totals": {...}}``, rows sorted by cost then tokens, descending.
    """
    if by not in GROUP_BY:
        raise ValueError(f"--by must be one of {', '.join(GROUP_BY)}, got {by!r}")
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r.get(_COLUMN[by]) or ("unknown" if by == "model" else ""))
        g = groups.setdefault(key, {"key": key, "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0})
        i, o = int(r.get("input_tokens") or 0), int(r.get("output_tokens") or 0)
        g["calls"] += 1
        g["input_tokens"] += i
        g["output_tokens"] += o
        g["cost"] += pricing.cost(str(r.get("model") or "unknown"), i, o)
    out = []
    for g in groups.values():
        g["tokens"] = g["input_tokens"] + g["output_tokens"]
        g["cost"] = round(g["cost"], 6)
        out.append(g)
    out.sort(key=lambda g: (-g["cost"], -g["tokens"], g["key"]))
    totals = {k: sum(g[k] for g in out) for k in ("calls", "input_tokens", "output_tokens", "tokens")}
    totals["cost"] = round(sum(g["cost"] for g in out), 6)
    return {"by": by, "currency": pricing.currency, "rows": out, "totals": totals}


def format_cost(value: float, currency: str) -> str:
    return f"{value:,.4f} {currency}" if 0 < value < 0.01 else f"{value:,.2f} {currency}"
