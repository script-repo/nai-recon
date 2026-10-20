"""Example deterministic check plugin: list it in ``checks.plugins`` as ``my_checks:no_competitor_names``."""

from collections.abc import Iterator

from ambientq.harness import Harness
from ambientq.models import Conversation, Evidence, Finding

COMPETITORS = ("acme cloud", "globex ai")


def no_competitor_names(conv: Conversation, harness: Harness) -> Iterator[Finding]:
    for t in conv.turns:
        if t.role == "assistant" and any(c in (t.content or "").lower() for c in COMPETITORS):
            yield Finding(conversation_id=conv.conversation_id, source="", category="output",
                          rubric_id="", severity="low", expected="do not recommend competitor products",
                          actual="assistant named a competitor", evidence=[Evidence(t.index, "")])
