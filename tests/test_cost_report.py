import json
from datetime import UTC, datetime

import pytest

from ambientq.cli import main
from ambientq.config import Config
from ambientq.cost import Pricing, summarize
from ambientq.demo import synthetic
from ambientq.ingest.jsonl import record_to_conversation
from ambientq.ingest.otlp import decode_export_request
from ambientq.llm import LLMClient
from ambientq.models import Insight, InsightStatus, Run
from ambientq.report import build_report, duration_stats, month_bounds, render_html, report_months
from ambientq.store import SQLStore


def ts(*a: int) -> float:
    return datetime(*a, tzinfo=UTC).timestamp()


OCT = ts(2026, 10, 1)
DAY = 86400.0

PRICING = {"pricing": {"currency": "EUR", "default": {"input_per_1k": 0.1, "output_per_1k": 0.2},
                       "models": {"big": {"input_per_1k": 1.0, "output_per_1k": 2.0},
                                  "llama-3-1-*": {"input_per_1k": 0.5, "output_per_1k": 0.5},
                                  "llama-3-1-8b": {"input_per_1k": 0.01, "output_per_1k": 0.01}}}}


# -- pricing -------------------------------------------------------------------------
def test_pricing_exact_glob_default():
    p = Pricing.from_config(Config.load(overrides=PRICING))
    assert p.currency == "EUR"
    assert p.cost("big", 1000, 1000) == pytest.approx(3.0)
    assert p.cost("llama-3-1-70b", 2000, 0) == pytest.approx(1.0)  # glob
    assert p.cost("llama-3-1-8b", 1000, 1000) == pytest.approx(0.02)  # exact beats glob
    assert p.cost("other", 1000, 1000) == pytest.approx(0.3)  # default
    assert Pricing.from_config(Config.load()).cost("x", 10**6, 10**6) == 0.0


def test_summarize_groups_and_totals():
    p = Pricing.from_config(Config.load(overrides=PRICING))
    rows = [{"agent_name": "a", "model": "big", "source": "agent", "team": "t1", "input_tokens": 1000,
             "output_tokens": 0},
            {"agent_name": "a", "model": "small", "source": "ambientq", "team": "t1", "input_tokens": 1000,
             "output_tokens": 1000},
            {"agent_name": "b", "model": None, "source": "agent", "team": "t2", "input_tokens": 0,
             "output_tokens": 0}]
    s = summarize(rows, p, "agent")
    assert [r["key"] for r in s["rows"]] == ["a", "b"]
    assert s["rows"][0]["calls"] == 2 and s["rows"][0]["cost"] == pytest.approx(1.3)
    assert s["totals"] == {"calls": 3, "input_tokens": 2000, "output_tokens": 1000, "tokens": 3000,
                           "cost": pytest.approx(1.3)}
    assert {r["key"] for r in summarize(rows, p, "model")["rows"]} == {"big", "small", "unknown"}
    with pytest.raises(ValueError):
        summarize(rows, p, "colour")


# -- usage capture ---------------------------------------------------------------------
def test_span_usage_recorded_once(store):
    records, _ = synthetic.generate(1, 4)
    spans = [s for r in records if "resourceSpans" in r for s in decode_export_request(r)]
    store.ingest_spans(spans)
    rows = store.usage_rows()
    assert rows and all(r["source"] == "agent" and r["id"].startswith("span:") for r in rows)
    assert all(r["conversation_id"] and r["agent_name"] == synthetic.AGENT for r in rows)
    first = sum(r["input_tokens"] for r in rows)
    assert first > 0
    store.ingest_spans(spans)  # idempotent
    assert sum(r["input_tokens"] for r in store.usage_rows()) == first


def test_record_usage_from_simple_record(store):
    rec = {"conversation_id": "r1", "agent": "bot", "model": "big", "started_ns": int(OCT * 1e9),
           "usage": {"input_tokens": 100, "output_tokens": 20},
           "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}
    store.upsert_conversation(record_to_conversation(rec))
    store.upsert_conversation(record_to_conversation(rec))
    rows = store.usage_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == "conv:r1" and rows[0]["model"] == "big" and rows[0]["ts"] == OCT
    store.upsert_conversation(record_to_conversation({**rec, "conversation_id": "r2", "usage": None}))
    assert len(store.usage_rows()) == 1


def test_llm_usage_hook(store, mock):
    llm = LLMClient("http://mock/v1", "mock-judge", transport=mock.transport, on_usage=store.usage_recorder())
    llm.usage_context.update(agent="bot", team="blue", run_id="run_9")
    verifier = llm.with_model("mock-verifier")
    llm.chat([{"role": "user", "content": "hello"}], purpose="judge")
    verifier.chat([{"role": "user", "content": "hello"}], purpose="verify")
    rows = store.usage_rows()
    assert [(r["model"], r["purpose"], r["source"], r["agent_name"], r["team"], r["run_id"]) for r in rows] == [
        ("mock-judge", "judge", "ambientq", "bot", "blue", "run_9"),
        ("mock-verifier", "verify", "ambientq", "bot", "blue", "run_9")]

    def boom(_):
        raise RuntimeError("x")

    llm.on_usage = boom  # a broken hook never fails the call
    assert llm.chat([{"role": "user", "content": "hi"}], purpose="judge") is not None


# -- report ----------------------------------------------------------------------------
def test_month_bounds_utc():
    assert month_bounds("2026-10") == (OCT, ts(2026, 11, 1))
    assert month_bounds("2026-12")[1] == ts(2027, 1, 1)
    for bad in ("2026-13", "2026/10", "", "26-10"):
        with pytest.raises(ValueError):
            month_bounds(bad)


def test_duration_stats():
    assert duration_stats([]) == {"count": 0, "mean_s": None, "median_s": None, "p90_s": None}
    s = duration_stats([float(x) for x in range(1, 11)])
    assert (s["count"], s["mean_s"], s["median_s"], s["p90_s"]) == (10, 5.5, 5.5, 9.0)


def _audit(store: SQLStore, at: float, actor: str, action: str, subject: str, detail: dict) -> None:
    store.execute("INSERT INTO audit(id, ts, actor, action, subject, detail) VALUES (?,?,?,?,?,?)",
                  (f"aud_{at}_{action}", at, actor, action, subject, json.dumps(detail)))


def _occ(store: SQLStore, ins: str, run: str, count: int, status: str, at: float) -> None:
    store.save_occurrence(ins, run, count, [], status)
    store.execute("UPDATE occurrences SET created_at=? WHERE insight_id=? AND run_id=?", (at, ins, run))


def _insight(store: SQLStore, iid: str, sev: str, cat: str, created: float, status: InsightStatus,
             resolved_at: float | None = None, team: str = "blue", title: str = "") -> None:
    store.save_insight(Insight(insight_id=iid, fingerprint=iid, title=title or iid, category=cat, rubric_id="R",
                               agent_name="bot", status=status, severity=sev, first_seen_run="r",
                               last_seen_run="r", created_at=created, team=team, resolved_at=resolved_at))


@pytest.fixture
def populated(store):
    s = store
    # conversations: one just before, two inside, one at the exclusive end of October (UTC)
    for cid, at, team in (("c0", OCT - 1, "blue"), ("c1", OCT, "blue"), ("c2", OCT + 5 * DAY, "red"),
                          ("c3", ts(2026, 11, 1), "blue")):
        conv = record_to_conversation({"conversation_id": cid, "agent": "bot", "team": team,
                                       "started_ns": int(at * 1e9), "messages": []})
        s.upsert_conversation(conv)
    s.save_run(Run(run_id="run_a", started_at=OCT + DAY, candidates=10, sampled=4, evaluated=4, findings=3,
                   rejected=1, input_tokens=900, output_tokens=100, status="ok", team="blue"))
    s.save_run(Run(run_id="run_old", started_at=OCT - DAY, candidates=99, team="blue"))
    # i_old: from September, recurs in October, resolved after 3 days of October
    _insight(s, "i_old", "medium", "procedure", OCT - 10 * DAY, InsightStatus.RESOLVED, OCT + 3 * DAY)
    _occ(s, "i_old", "r1", 2, "NEW", OCT - 10 * DAY)
    _occ(s, "i_old", "r2", 1, "RECURRING", OCT + DAY)
    _occ(s, "i_old", "r3", 0, "RESOLVED", OCT + 3 * DAY)
    # i_new: new in October, still open
    _insight(s, "i_new", "high", "safety", OCT + 2 * DAY, InsightStatus.NEW, title="<b>leak</b>")
    _occ(s, "i_new", "r2", 1, "NEW", OCT + 2 * DAY)
    # i_fix: new in October, fixed in 1 day
    _insight(s, "i_fix", "high", "output", OCT + 4 * DAY, InsightStatus.RESOLVED, OCT + 5 * DAY)
    _occ(s, "i_fix", "r4", 1, "NEW", OCT + 4 * DAY)
    _occ(s, "i_fix", "r5", 0, "RESOLVED", OCT + 5 * DAY)
    # i_reg: resolved in September, regressed in October (open at month end)
    _insight(s, "i_reg", "low", "procedure", OCT - 20 * DAY, InsightStatus.RECURRING)
    _occ(s, "i_reg", "r0", 1, "NEW", OCT - 20 * DAY)
    _occ(s, "i_reg", "r1", 0, "RESOLVED", OCT - 15 * DAY)
    _occ(s, "i_reg", "r6", 1, "RECURRING", OCT + 6 * DAY)
    # i_red: another team
    _insight(s, "i_red", "high", "safety", OCT + DAY, InsightStatus.NEW, team="red")
    s.commit()
    # proposals and decisions
    s.save_proposal({"proposal_id": "prp_1", "insight_id": "i_old", "status": "APPROVED", "team": "blue",
                     "created_at": OCT + 2 * DAY})
    s.save_proposal({"proposal_id": "prp_2", "insight_id": "i_new", "status": "REJECTED", "team": "blue",
                     "kind": "guardrail", "created_at": OCT + 3 * DAY})
    s.save_proposal({"proposal_id": "prp_3", "insight_id": "i_new", "status": "PENDING_APPROVAL", "team": "blue",
                     "kind": "tool_schema", "created_at": ts(2026, 11, 1)})
    _audit(s, OCT + 2.5 * DAY, "alice@example.com", "approved", "prp_1", {"note": "lgtm", "insight": "i_old"})
    _audit(s, OCT + 3.5 * DAY, "<script>alert(1)</script>", "rejected", "prp_2",
           {"note": "no & <never>", "insight": "i_new"})
    _audit(s, OCT - 1, "bob", "approved", "prp_0", {})
    _audit(s, OCT + DAY, "ambientq", "llm_call", "judge", {})
    _audit(s, OCT + DAY + 1, "ambientq", "llm_call", "judge", {})
    s.commit()
    # usage: agent traffic and ambientq's own calls; one row outside the month
    s.record_usage(agent="bot", model="big", source="agent", input_tokens=1000, output_tokens=500, team="blue",
                   ts=OCT + DAY, usage_id="u1")
    s.record_usage(agent="bot", model="llama-3-1-70b", source="ambientq", purpose="judge", input_tokens=2000,
                   output_tokens=0, team="blue", ts=OCT + DAY, usage_id="u2")
    s.record_usage(agent="bot", model="big", source="agent", input_tokens=10**6, output_tokens=0, team="blue",
                   ts=ts(2026, 11, 1), usage_id="u3")
    s.record_usage(agent="other", model="big", source="agent", input_tokens=1000, output_tokens=0, team="red",
                   ts=OCT + DAY, usage_id="u4")
    return s


def test_report_numbers(populated):
    cfg = Config.load(overrides=PRICING)
    r = build_report(populated, cfg, "2026-10", team="blue")
    assert r["period"] == {"month": "2026-10", "start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z",
                           "team": "blue"}
    assert r["conversations"]["total"] == 1  # c1 only (c0 Sept, c2 red, c3 Nov)
    g = r["grading"]
    assert (g["runs"], g["candidates"], g["judged"], g["findings_kept"], g["findings_rejected"], g["tokens"]) == (
        1, 10, 4, 3, 1, 1000)
    ins = r["insights"]
    assert (ins["new"], ins["recurring"], ins["resolved"], ins["regressions"], ins["open_at_end"]) == (2, 2, 2, 1, 2)
    assert ins["new_by_severity"] == {"high": 2}
    assert ins["open_by_severity"] == {"high": 1, "low": 1}
    assert {i["insight_id"] for i in ins["regression_list"]} == {"i_reg"}
    ttf = r["time_to_fix"]
    assert ttf["count"] == 2 and ttf["mean_s"] == 7 * DAY and ttf["median_s"] == 7 * DAY
    assert ttf["by_severity"]["high"]["median_s"] == DAY
    assert ttf["by_severity"]["medium"]["median_s"] == 13 * DAY
    p = r["proposals"]
    assert p["total"] == 2 and p["by_status"] == {"APPROVED": 1, "REJECTED": 1}
    assert p["by_kind"] == {"prompt": 1, "guardrail": 1}  # NULL kind counts as prompt
    assert [(d["action"], d["actor"], d["proposal"], d["note"]) for d in r["decisions"]] == [
        ("approved", "alice@example.com", "prp_1", "lgtm"),
        ("rejected", "<script>alert(1)</script>", "prp_2", "no & <never>")]
    assert r["decisions"][0]["at"] == "2026-10-03T12:00:00Z"
    u = r["usage"]
    assert u["currency"] == "EUR"
    assert u["agent"]["by_model"]["totals"]["cost"] == pytest.approx(2.0)  # big: 1.0 + 1.0
    assert u["ambientq"]["by_model"]["rows"][0]["key"] == "llama-3-1-70b"
    assert u["ambientq"]["by_model"]["totals"]["cost"] == pytest.approx(1.0)
    assert u["ambientq"]["by_purpose"]["rows"][0]["key"] == "judge"
    assert u["totals"]["cost"] == pytest.approx(3.0)
    assert r["audit"]["by_action"] == {"llm_call": 2, "approved": 1, "rejected": 1}
    assert r["summary"]["cost"] == pytest.approx(3.0)
    json.dumps(r)  # JSON-able

    whole = build_report(populated, cfg, "2026-10")
    assert whole["conversations"]["total"] == 2
    assert whole["insights"]["new"] == 3
    assert whole["usage"]["totals"]["calls"] == 3
    nov = build_report(populated, cfg, "2026-11")
    assert nov["conversations"]["total"] == 1 and nov["proposals"]["total"] == 1
    assert nov["insights"]["open_at_end"] == 3 and nov["insights"]["new"] == 0


def test_report_html_escapes_and_prints(populated):
    html = render_html(build_report(populated, Config.load(overrides=PRICING), "2026-10"))
    assert html.startswith("<!doctype html>") and "@media print" in html and "page-break" in html
    assert "<script" not in html.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "no &amp; &lt;never&gt;" in html and "&lt;b&gt;leak&lt;/b&gt;" in html
    assert "October 2026" in html and "EUR" in html


def test_report_months(populated):
    assert report_months(populated) == ["2026-11", "2026-10", "2026-09"]
    assert report_months(SQLStore(":memory:")) == []


def test_cli_usage_report_audit_export(tmp_path, capsys):
    db = tmp_path / "aq.db"
    cfgf = tmp_path / "aq.yaml"
    cfgf.write_text(json.dumps({"store": {"url": f"sqlite:///{db}"}, **PRICING}))
    s = SQLStore(f"sqlite:///{db}")
    s.record_usage(agent="bot", model="big", source="agent", input_tokens=1000, output_tokens=0, team="blue")
    s.audit("alice", "approved", "prp_1", {"note": "ok"})
    s.close()
    c = ["-c", str(cfgf)]
    assert main(c + ["usage", "--by", "model", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rows"][0]["key"] == "big" and data["totals"]["cost"] == pytest.approx(1.0)
    assert main(c + ["usage", "--team", "red"]) == 0
    assert "TOTAL" in capsys.readouterr().out
    month = datetime.now(UTC).strftime("%Y-%m")
    out = tmp_path / "reports"
    assert main(c + ["report", "--month", month, "--team", "blue", "--out", str(out)]) == 0
    printed = capsys.readouterr().out.split()
    assert sorted(p.rsplit("/", 1)[1] for p in printed) == [f"ambientq-report-{month}-blue.html",
                                                          f"ambientq-report-{month}-blue.json"]
    assert json.loads((out / f"ambientq-report-{month}-blue.json").read_text())["usage"]["totals"]["calls"] == 1
    assert main(c + ["report", "--month", "2026-99"]) == 2
    assert main(c + ["audit-export", "--format", "jsonl"]) == 0
    ev = json.loads(capsys.readouterr().out.strip())
    assert ev["actor"] == "alice" and ev["detail"] == {"note": "ok"} and ev["ts"].endswith("Z")
    f = tmp_path / "audit.cef"
    assert main(c + ["audit-export", "--format", "cef", "--since", "2000-01-01", "--out", str(f)]) == 0
    assert f.read_text().startswith("CEF:0|ambientq|ambientq|")
    assert main(c + ["audit-export", "--format", "syslog", "--until", "2000-01-01"]) == 0
    assert capsys.readouterr().out == ""


def test_evaluate_attributes_own_usage(store, cfg, harness, mock):
    from ambientq.evaluate import evaluate

    records, _ = synthetic.generate(1, 4)
    store.ingest_spans([s for r in records if "resourceSpans" in r for s in decode_export_request(r)])
    llm = LLMClient("http://mock/v1", "mock-judge", transport=mock.transport, on_usage=store.usage_recorder())
    run = evaluate(store, cfg, harness, llm).run
    own = [r for r in store.usage_rows() if r["source"] == "ambientq"]
    assert own and {(r["agent_name"], r["run_id"]) for r in own} == {(harness.agent, run.run_id)}
    assert sum(r["input_tokens"] for r in own) == run.input_tokens
