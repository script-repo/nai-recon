from ambientq.insights import correlate, fingerprint, jaccard, signature
from ambientq.models import Finding, InsightStatus


def F(cid, **kw):
    base = dict(conversation_id=cid, source="judge", category="procedure", expected="Call get_cluster_status first",
                actual="did not", rubric_id="R-P1", agent_name="a", verified=True)
    base.update(kw)
    return Finding(**base)


def status(store):
    return {i.rubric_id: i.status for i in store.insights()}


def test_fingerprint_rules():
    assert fingerprint(F("1")) == fingerprint(F("2", expected="something else entirely"))
    assert fingerprint(F("1", category="tool_arguments", tool_name="x", rubric_id="G-COMPLETION")) == \
        fingerprint(F("2", category="tool_arguments", tool_name="x", rubric_id="G-COMPLETION", expected="zzz"))
    g1 = F("1", rubric_id="G-GROUNDING", category="grounding", expected="Claims node count 16 without a source")
    g2 = F("2", rubric_id="G-GROUNDING", category="grounding", expected="Claims node count 9 without a source")
    assert fingerprint(g1) == fingerprint(g2)  # numbers are stripped from the signature
    assert signature("The agent MUST call 42 tools") == "tools"
    assert jaccard("a b c", "") == 0


def test_lifecycle_new_recurring_resolved_regression(store):
    r = correlate(store, "run1", [F("c1"), F("c2"), F("c3", rubric_id="R-P3", expected="confirm")],
                  agents_evaluated={"a"}, titles={"R-P1": "Always call status"})
    assert len(r.new) == 2 and status(store) == {"R-P1": InsightStatus.NEW, "R-P3": InsightStatus.NEW}
    ins = store.insight_by_fingerprint(fingerprint(F("x")))
    assert ins.title == "[procedure] Always call status" and ins.occurrence_count == 2 and ins.conversation_count == 2

    r = correlate(store, "run2", [F("c4")], agents_evaluated={"a"})
    assert r.recurring and status(store)["R-P1"] == InsightStatus.RECURRING
    assert status(store)["R-P3"] == InsightStatus.NEW  # quiet 1 run, not yet resolved

    r = correlate(store, "run3", [F("c5")], agents_evaluated={"a"})
    assert status(store)["R-P3"] == InsightStatus.RESOLVED and len(r.resolved) == 1

    r = correlate(store, "run4", [F("c6", rubric_id="R-P3", expected="confirm")], agents_evaluated={"a"})
    assert status(store)["R-P3"] == InsightStatus.RECURRING  # regression re-opens
    occ = store.occurrences(store.insight_by_fingerprint(fingerprint(F("x", rubric_id="R-P3"))).insight_id)
    assert [o["status_after"] for o in occ] == ["NEW", "RESOLVED", "RECURRING"]


def test_unverified_ignored_and_other_agents_untouched(store):
    correlate(store, "run1", [F("c1", verified=False)], agents_evaluated={"a"})
    assert store.insights() == []
    correlate(store, "run1", [F("c1", agent_name="b")], agents_evaluated={"b"})
    for run in ("run2", "run3"):
        correlate(store, run, [], agents_evaluated={"a"})
    assert store.insights()[0].status == InsightStatus.NEW  # agent b was not evaluated


def test_similarity_fallback(store):
    correlate(store, "run1", [F("c1", rubric_id="G-COMPLETION", category="completion",
                                expected="answer every part of the multi part request about clusters")],
              agents_evaluated={"a"})
    correlate(store, "run2", [F("c2", rubric_id="G-COMPLETION", category="completion",
                                expected="answer every single part of the request about clusters please")],
              agents_evaluated={"a"})
    assert len(store.insights()) == 1 and store.insights()[0].status == InsightStatus.RECURRING
