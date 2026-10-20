from ambientq.conversation import build_conversation
from ambientq.demo import synthetic
from ambientq.ingest.otlp import decode_export_request
from ambientq.models import Evidence, Finding, Insight, InsightStatus, Run
from ambientq.store import SQLStore


def test_ingest_and_roundtrip(store):
    records, truth = synthetic.generate(1, 6)
    spans = [s for r in records if "resourceSpans" in r for s in decode_export_request(r)]
    touched = store.ingest_spans(spans)
    assert touched and all(store.get_conversation(c) for c in touched)
    c = store.get_conversation(sorted(touched)[0])
    assert c.turns and c.agent_name == synthetic.AGENT
    # idempotent re-ingest
    store.ingest_spans(spans)
    assert store.counts()["conversations"] == len(touched)


def test_late_root_span_rehomes_trace(store):
    rec = synthetic.to_otlp("late", synthetic.sc_docs(__import__("random").Random(1)), 10**18,
                            __import__("random").Random(1))
    spans = decode_export_request(rec)
    root = spans[0]
    children = spans[1:]
    for s in children:  # strip session ids from children
        s.attributes.pop("gen_ai.conversation.id", None)
    store.ingest_spans(children)
    assert store.get_conversation(root.trace_id) is not None
    store.ingest_spans([root])
    assert store.get_conversation(root.trace_id) is None
    assert len(store.get_conversation("late").turns) == 4


def test_findings_insights_runs_proposals_audit(store):
    store.save_run(Run(run_id="run_1", started_at=1.0))
    assert store.get_run("run_1").run_id == "run_1"
    f = Finding(conversation_id="c", source="judge", category="procedure", expected="e", actual="a",
                evidence=[Evidence(1, "q")], run_id="run_1", verified=True)
    store.save_finding(f)
    got = store.findings(run_id="run_1")[0]
    assert got.evidence[0].quote == "q" and got.verified is True
    ins = Insight(insight_id="ins_abc", fingerprint="fp", title="t", category="procedure", rubric_id="R-P1",
                  agent_name="a", status=InsightStatus.NEW, severity="high", first_seen_run="run_1",
                  last_seen_run="run_1")
    store.save_insight(ins)
    store.save_occurrence("ins_abc", "run_1", 2, ["c", "c", "d"], "NEW")
    assert store.get_insight("ins_ab").insight_id == "ins_abc"  # prefix lookup
    assert store.occurrences("ins_abc")[0]["conversation_ids"] == ["c", "d"]
    store.save_proposal({"proposal_id": "prp_1", "insight_id": "ins_abc", "status": "DRAFT", "replay": {"fixed": 1}})
    assert store.get_proposal("prp_1")["replay"] == {"fixed": 1}
    store.audit("me", "did", "x", {"k": 1})
    assert store.audit_log()[0]["action"] == "did"


def test_mark_evaluated_filters(store):
    conv = build_conversation("c1", [])
    store.upsert_conversation(conv)
    assert len(store.list_conversations(not_evaluated_for="v1")) == 1
    store.mark_evaluated(["c1"], "run", "v1")
    assert store.list_conversations(not_evaluated_for="v1") == []
    assert len(store.list_conversations(not_evaluated_for="v2")) == 1


def test_postgres_sql_translation():
    s = SQLStore.__new__(SQLStore)
    s.dialect = "postgres"
    assert s._sql("SELECT * FROM t WHERE a=? AND b=?") == "SELECT * FROM t WHERE a=%s AND b=%s"
    s.dialect = "sqlite"
    assert "REAL" in s._sql("x DOUBLE PRECISION")


def test_file_store(tmp_path):
    s = SQLStore(f"sqlite:///{tmp_path}/sub/a.db")
    s.save_run(Run(run_id="r", started_at=1))
    s.close()
    assert SQLStore(f"sqlite:///{tmp_path}/sub/a.db").get_run("r")
