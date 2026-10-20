"""Deterministic, risk-weighted sampling of conversations for the LLM judge."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from ambientq.models import Conversation


def hash_fraction(conversation_id: str, seed: str) -> float:
    h = hashlib.sha256(f"{seed}:{conversation_id}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def select(conversations: Iterable[Conversation], *, rate: float, seed: str = "ambientq",
           priority: Iterable[str] = (), max_priority: int = 50, max_total: int = 0) -> list[Conversation]:
    """Pick conversations to judge.

    Priority conversations (errors, failed checks) come first, up to
    ``max_priority``; the rest are included when their stable hash falls under
    ``rate``. ``max_total`` caps the result (0 = no cap).
    """
    convs = list(conversations)
    prio_ids = set(priority)
    prio = [c for c in convs if c.conversation_id in prio_ids or c.has_error][:max_priority]
    chosen = {c.conversation_id for c in prio}
    rest = sorted(
        (c for c in convs if c.conversation_id not in chosen and hash_fraction(c.conversation_id, seed) < rate),
        key=lambda c: hash_fraction(c.conversation_id, seed),
    )
    out = prio + rest
    return out[:max_total] if max_total > 0 else out
