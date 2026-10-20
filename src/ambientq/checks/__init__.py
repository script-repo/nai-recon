"""Deterministic checks: free, run on every conversation, pluggable.

A check is ``check(conv, harness) -> Iterable[Finding]``. Register one with the
``@check("name")`` decorator, an ``ambientq.checks`` entry point, or a
``module:function`` path in ``checks.plugins``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from importlib.metadata import entry_points

from ambientq.harness import Harness
from ambientq.models import Conversation, Finding

CheckFn = Callable[[Conversation, Harness], Iterable[Finding]]
REGISTRY: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        REGISTRY[name] = fn
        fn.check_name = name  # type: ignore[attr-defined]
        return fn
    return deco


def load_checks(plugins: Iterable[str] = (), disabled: Iterable[str] = ()) -> dict[str, CheckFn]:
    from ambientq.checks import builtin  # noqa: F401  (registers built-ins)

    found = dict(REGISTRY)
    for ep in entry_points(group="ambientq.checks"):
        found[ep.name] = ep.load()
    for path in plugins:
        mod, _, attr = path.partition(":")
        fn = getattr(importlib.import_module(mod), attr or "check")
        found[getattr(fn, "check_name", attr or mod)] = fn
    for name in disabled:
        found.pop(name, None)
    return found


def run_checks(conv: Conversation, harness: Harness, checks: dict[str, CheckFn]) -> list[Finding]:
    out: list[Finding] = []
    for name, fn in checks.items():
        try:
            for f in fn(conv, harness):
                if not f.source.startswith("check:"):
                    f.source = f"check:{name}"
                f.agent_name = f.agent_name or conv.agent_name
                out.append(f)
        except Exception as exc:  # a broken plugin must not stop the run
            out.append(Finding(conversation_id=conv.conversation_id, source=f"check:{name}",
                               category="configuration", expected="check runs cleanly",
                               actual=f"check raised {type(exc).__name__}: {exc}", severity="low",
                               verified=False, verifier_note="check error"))
    return out
