import random

from ambientq.demo import synthetic
from ambientq.ingest.jsonl import record_to_conversation
from ambientq.judge.judge import build_judge_messages, judge_conversation, parse_findings
from ambientq.judge.rubric import build_rubric
from ambientq.judge.verifier import check_evidence, verify
from ambientq.models import Evidence, Finding


def make(sc, cid="c1"):
    return record_to_conversation(synthetic.to_record(cid, sc(random.Random(1)), 0))


def test_prompt_contains_harness_and_data_delimiters(harness):
    rub = build_rubric(harness)
    msgs = build_judge_messages(make(synthetic.sc_health_ok), harness, rub, focus_rubric="R-P1")
    assert msgs[0]["content"].startswith("You are a strict quality analyst")
    user = msgs[1]["content"]
    assert "<conversation_json>" in user and "<rubric_json>" in user and "get_cluster_status" in user
    assert "Only report findings for rubric item R-P1." in user


def test_parse_findings_normalizes(harness):
    rub = build_rubric(harness)
    c = make(synthetic.sc_health_ok)
    data = {"findings": [
        {"rubric_id": "R-T-scale_nodepool", "category": "bogus", "severity": "CRITICAL", "confidence": "x",
         "expected": "e", "actual": "a", "evidence": [{"turn": "1", "quote": "q"}, {"bad": 1}]},
        "junk",
    ]}
    fs = parse_findings(data, c, rub)
    assert len(fs) == 1
    f = fs[0]
    assert f.category == "tool_selection" and f.severity == "medium" and f.confidence == 0.7
    assert f.tool_name == "scale_nodepool" and f.evidence == [Evidence(1, "q")]
    assert parse_findings("nonsense", c, rub) == []


def test_mock_judge_flags_planted_defects(harness, llm):
    rub = build_rubric(harness)
    cases = {
        synthetic.sc_health_no_status: "procedure",
        synthetic.sc_scale_no_confirm: "procedure",
        synthetic.sc_endpoints_leak: "safety",
        synthetic.sc_tool_error_ignored: "tool_results",
    }
    for sc, cat in cases.items():
        fs = judge_conversation(llm, make(sc), harness, rub)
        assert [f.category for f in fs] == [cat], sc.__name__
    for sc in (synthetic.sc_health_ok, synthetic.sc_scale_confirmed, synthetic.sc_endpoints_ok):
        assert judge_conversation(llm, make(sc), harness, rub) == [], sc.__name__


def test_verifier_rejects_fabricated_quote(harness, llm):
    rub = build_rubric(harness)
    c = make(synthetic.sc_docs)
    fs = judge_conversation(llm, c, harness, rub)
    assert len(fs) == 1  # the mock's deliberately unsupported claim
    verify(fs, c, rub, llm)
    assert fs[0].verified is False and "quote not found" in fs[0].verifier_note


def test_evidence_checks(harness):
    rub = build_rubric(harness)
    c = make(synthetic.sc_health_no_status)

    def f(**kw):
        base = dict(conversation_id="c1", source="judge", category="procedure", expected="e", actual="a",
                    rubric_id="R-P1", evidence=[Evidence(1, c.turns[1].content[:20])])
        base.update(kw)
        return Finding(**base)

    assert check_evidence(f(), c, rub)[0]
    assert not check_evidence(f(rubric_id="R-NOPE"), c, rub)[0]
    assert not check_evidence(f(evidence=[]), c, rub)[0]
    assert not check_evidence(f(evidence=[Evidence(99, "x")]), c, rub)[0]
    assert check_evidence(f(evidence=[Evidence(0, c.turns[1].content[:20])]), c, rub)[0]  # off-by-one tolerated
    # LLM verifier pass on a real finding keeps it
    kept = verify([f()], c, rub, None)
    assert kept[0].verified is True
