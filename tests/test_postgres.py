"""The store against a real PostgreSQL server.

Skipped unless ``AMBIENTQ_TEST_PG_URL`` points at a database the tests may
create schemas in (CI runs a ``postgres:16`` service container). Each test gets
a fresh schema, selected with ``search_path``, and drops it afterwards.
"""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from ambientq.demo import synthetic
from ambientq.evaluate import evaluate
from ambientq.ingest.jsonl import record_to_conversation
from ambientq.ingest.otlp import decode_export_request
from ambientq.insights import correlate
from ambientq.models import Evidence, Finding, Insight, InsightStatus, Run
from ambientq.propose import decide, propose
from ambientq.store import COLUMNS, SQLStore

PG_URL = os.environ.get("AMBIENTQ_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="AMBIENTQ_TEST_PG_URL is not set")


def _with_search_path(url: str, schema: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


@pytest.fixture
def pg_url() -> Iterator[str]:
    """A URL whose connections live in a fresh, empty schema."""
    import psycopg

    schema = f"aq_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    try:
        yield _with_search_path(PG_URL, schema)
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def pg(pg_url: str) -> Iterator[SQLStore]:
    s = SQLStore(pg_url, team_of=lambda agent: "platform")
    yield s
    s.close()


def test_dialect_and_migrations_are_idempotent(pg_url):
    s1 = SQLStore(pg_url)
    assert s1.dialect == "postgres"
    for table, col, _ in COLUMNS:
        assert col in s1.columns(table)
    s1.close()
    s2 = SQLStore(pg_url)  # second migrate on the same schema is a no-op
    s2.migrate()
    assert "team" in s2.columns("insights")
    s2.close()


def test_columns_are_scoped_to_the_current_schema(pg_url):
    """A table of the same name in another schema must not hide a missing column."""
    import psycopg

    other = f"aq_other_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {other}")
        admin.execute(f"CREATE TABLE {other}.insights (insight_id TEXT, team TEXT, resolved_at DOUBLE PRECISION)")
    try:
        with psycopg.connect(pg_url, autocommit=True) as c:
            c.execute("CREATE TABLE insights (insight_id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, title TEXT, "
                      "category TEXT, rubric_id TEXT, agent_name TEXT, tool_name TEXT, expected TEXT, "
                      "status TEXT, severity TEXT, first_seen_run TEXT, last_seen_run TEXT, "
                      "occurrence_count INTEGER, conversation_count INTEGER, quiet_runs INTEGER, "
                      "harness_version_first TEXT, harness_version_last TEXT, created_at DOUBLE PRECISION, "
                      "updated_at DOUBLE PRECISION)")
        s = SQLStore(pg_url)
        s.close()
        with psycopg.connect(pg_url, autocommit=True) as c:
            rows = c.execute("SELECT column_name FROM information_schema.columns "
                             "WHERE table_schema = current_schema() AND table_name = 'insights'").fetchall()
        assert {"team", "resolved_at", "regressions"} <= {r[0] for r in rows}
    finally:
        with psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {other} CASCADE")


def test_v01_schema_upgrades_in_place(pg_url):
    import psycopg

    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute("CREATE TABLE proposals (proposal_id TEXT PRIMARY KEY, insight_id TEXT, status TEXT, "
                  "file TEXT, start_line INTEGER, end_line INTEGER, diff TEXT, rationale TEXT, replay TEXT, "
                  "branch TEXT, pr_url TEXT, approved_by TEXT, approved_at DOUBLE PRECISION, "
                  "created_at DOUBLE PRECISION)")
        c.execute("INSERT INTO proposals(proposal_id, status, created_at) VALUES ('prp_old', 'DRAFT', 1)")
    s = SQLStore(pg_url)
    try:
        assert {"team", "kind", "shadow", "decision_note"} <= s.columns("proposals")
        old = s.get_proposal("prp_old")
        assert old is not None and old["kind"] == "prompt" and old["shadow"] is None
    finally:
        s.close()


def test_ingest_roundtrip_and_rehome(pg):
    records, _ = synthetic.generate(1, 6)
    spans = [s for r in records if "resourceSpans" in r for s in decode_export_request(r)]
    touched = pg.ingest_spans(spans)
    assert touched and all(pg.get_conversation(c) for c in touched)
    conv = pg.get_conversation(sorted(touched)[0])
    assert conv.turns and conv.agent_name == synthetic.AGENT and conv.team == "platform"
    pg.ingest_spans(spans)  # idempotent
    assert pg.counts()["conversations"] == len(touched)
    assert pg.list_conversations(team="platform") and pg.list_conversations(team="nope") == []

    rec = synthetic.to_otlp("late", synthetic.sc_docs(random.Random(1)), 10**18, random.Random(1))
    late = decode_export_request(rec)
    for s in late[1:]:
        s.attributes.pop("gen_ai.conversation.id", None)
    pg.ingest_spans(late[1:])
    pg.ingest_spans([late[0]])
    assert pg.get_conversation(late[0].trace_id) is None
    assert len(pg.get_conversation("late").turns) == 4


def test_findings_insights_occurrences_and_prefix_lookup(pg):
    pg.save_run(Run(run_id="run_1", started_at=1.0, team="platform"))
    assert pg.get_run("run_1").team == "platform"
    assert [r.run_id for r in pg.list_runs(team="platform")] == ["run_1"]
    f = Finding(conversation_id="c", source="judge", category="procedure", expected="e", actual="a",
                evidence=[Evidence(1, "q")], run_id="run_1", verified=True)
    pg.save_finding(f)
    pg.save_finding(f)  # upsert
    pg.commit()
    got = pg.findings(run_id="run_1")
    assert len(got) == 1 and got[0].evidence[0].quote == "q" and got[0].verified is True
    ins = Insight("ins_abc", "fp", "t", "procedure", "R-P1", "a", InsightStatus.NEW, "high", "run_1", "run_1",
                  team="platform", resolved_at=12.5, regressions=1)
    pg.save_insight(ins)
    pg.save_insight(Insight("insXabd", "fp2", "t2", "procedure", "R-P2", "a", InsightStatus.NEW, "low",
                            "run_1", "run_1"))
    pg.save_occurrence("ins_abc", "run_1", 2, ["c", "c", "d"], "NEW")
    pg.commit()
    # "_" in a prefix is a literal, not a LIKE wildcard: "ins_ab" matches only ins_abc.
    assert pg.get_insight("ins_ab").insight_id == "ins_abc"
    assert pg.get_insight("ins%") is None
    back = pg.get_insight("ins_abc")
    assert back.resolved_at == 12.5 and back.regressions == 1 and back.team == "platform"
    assert pg.occurrences("ins_abc")[0]["conversation_ids"] == ["c", "d"]
    assert [i.insight_id for i in pg.insights(status="new", team="platform")] == ["ins_abc"]


def test_correlate_lifecycle(pg):
    def F(cid: str, **kw: object) -> Finding:
        base: dict = dict(conversation_id=cid, source="judge", category="procedure", actual="did not",
                          expected="Call get_cluster_status first", rubric_id="R-P1", agent_name="a",
                          verified=True)
        base.update(kw)
        return Finding(**base)

    r = correlate(pg, "run1", [F("c1"), F("c2"), F("c3", rubric_id="R-P3", expected="confirm")],
                  agents_evaluated={"a"})
    assert len(r.new) == 2
    correlate(pg, "run2", [F("c4")], agents_evaluated={"a"})
    r = correlate(pg, "run3", [F("c5")], agents_evaluated={"a"})
    assert len(r.resolved) == 1
    r = correlate(pg, "run4", [F("c6", rubric_id="R-P3", expected="confirm")], agents_evaluated={"a"})
    statuses = {i.rubric_id: i.status for i in pg.insights()}
    assert statuses == {"R-P1": InsightStatus.RECURRING, "R-P3": InsightStatus.RECURRING}


def test_proposals_roundtrip_and_filters(pg):
    pg.save_proposal({"proposal_id": "prp_1", "insight_id": "ins_1", "status": "DRAFT", "team": "platform",
                      "kind": "tool_schema", "replay": {"fixed": 1}, "shadow": {"verdict": "better"}})
    pg.save_proposal({"proposal_id": "prp_1", "insight_id": "ins_1", "status": "APPROVED", "team": "platform",
                      "kind": "tool_schema", "replay": {"fixed": 2}, "decision_note": "ok"})
    p = pg.get_proposal("prp_1")
    assert p["replay"] == {"fixed": 2} and p["status"] == "APPROVED" and p["kind"] == "tool_schema"
    assert p["shadow"] == {"verdict": "better"}  # not in the second save, kept
    assert pg.get_proposal("prp_") is not None and pg.get_proposal("prp%") is None
    assert len(pg.proposals(team="platform", status="APPROVED")) == 1
    assert pg.proposals(status="DRAFT") == []


def test_usage_and_audit_filters(pg):
    for i in range(3):
        pg.record_usage(agent="a", model="m", source="agent", input_tokens=10, output_tokens=5,
                        usage_id=f"u{i % 2}", ts=100.0 + i)
    rows = pg.usage_rows()
    assert len(rows) == 2 and {r["team"] for r in rows} == {"platform"}
    assert len(pg.usage_rows(since=100.5)) == 1 and pg.usage_rows(team="other") == []
    assert len(pg.usage_rows(until=101.0)) == 1
    for i, action in enumerate(("a1", "a2", "a3")):
        pg.audit("me", action, "x", {"i": i})
    assert [r["action"] for r in pg.audit_log()] == ["a3", "a2", "a1"]
    assert [r["action"] for r in pg.audit_log(ascending=True, limit=2)] == ["a1", "a2"]
    ts = [r["ts"] for r in pg.audit_log(ascending=True)]
    assert [r["action"] for r in pg.audit_log(since=ts[1], until=ts[2] + 1e-9)] in (["a3", "a2"], ["a2"])


def test_failed_statement_does_not_poison_the_connection(pg):
    with pytest.raises(Exception):  # noqa: B017 - psycopg error class depends on the server
        pg.query("SELECT no_such_column FROM runs")
    pg.save_run(Run(run_id="after_error", started_at=2.0))
    assert pg.get_run("after_error") is not None


def test_evaluate_and_propose_end_to_end(pg_url, cfg, harness, llm):
    store = SQLStore(pg_url, team_of=cfg.team_for)
    try:
        for i, sc in enumerate([synthetic.sc_health_no_status] * 3
                               + [synthetic.sc_health_ok, synthetic.sc_scale_no_confirm, synthetic.sc_docs]):
            store.upsert_conversation(record_to_conversation(synthetic.to_record(f"c{i}", sc(random.Random(i)), i)))
        res = evaluate(store, cfg, harness, llm)
        assert res.run.evaluated == 6 and res.run.input_tokens > 0
        ins = next(i for i in store.insights() if "get_cluster_status" in i.title)
        assert store.findings(insight_id=ins.insight_id)
        p = propose(store, cfg, ins.insight_id, harness, llm)
        assert p["status"] == "PENDING_APPROVAL" and p["replay"]["improved"]
        decided = decide(store, p["proposal_id"], "alice", approve=True)
        assert decided["status"] == "APPROVED" and decided["approved_by"] == "alice"
        assert {"run_started", "proposed"} <= {r["action"] for r in store.audit_log()}
    finally:
        store.close()
