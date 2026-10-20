"""Alerts (Slack, Teams, generic webhooks) and multi-agent, multi-team evaluation."""

from __future__ import annotations

import json
import random
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import yaml

from ambientq.alerts import deliver, payload
from ambientq.cli import main
from ambientq.config import Config
from ambientq.demo import synthetic
from ambientq.insights import CorrelationResult
from ambientq.models import Insight, InsightStatus
from ambientq.store import SQLStore

from .conftest import git


@pytest.fixture
def hook_server():
    received: list[tuple[str, dict[str, Any]]] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, body))
            self.send_response(500 if self.path == "/broken" else 200)
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", received
    srv.shutdown()


def _ins(store: SQLStore, iid: str, sev: str, team: str = "platform", status=InsightStatus.NEW) -> None:
    store.save_insight(Insight(insight_id=iid, fingerprint=iid, title=f"title {iid}", category="safety",
                               rubric_id="R-S1", agent_name="bot", status=status, severity=sev,
                               first_seen_run="r", last_seen_run="r", team=team, expected="ask first",
                               occurrence_count=3, conversation_count=2))


def test_payload_shapes():
    ins = Insight(insight_id="ins_1", fingerprint="f", title="Leaked endpoint", category="safety", rubric_id="R",
                  agent_name="bot", status=InsightStatus.NEW, severity="high", first_seen_run="r",
                  last_seen_run="r", team="platform")
    slack = payload("slack", ins, "new", "https://aq/insights/ins_1")
    assert "Leaked endpoint" in slack["text"] and slack["blocks"][0]["type"] == "section"
    teams = payload("teams", ins, "regression", "https://aq/insights/ins_1")
    card = teams["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and card["actions"][0]["url"].endswith("ins_1")
    assert card["body"][0]["text"].startswith("Regression")
    gen = payload("generic", ins, "new")
    assert gen["event"] == "new" and gen["team"] == "platform" and gen["url"] is None


def test_deliver_filters_by_severity_team_and_audits(store, hook_server, monkeypatch):
    base, received = hook_server
    monkeypatch.setenv("HOOK_SLACK", f"{base}/slack")
    monkeypatch.setenv("HOOK_ALL", f"{base}/all")
    monkeypatch.setenv("HOOK_BROKEN", f"{base}/broken")
    _ins(store, "i_high", "high")
    _ins(store, "i_low", "low")
    _ins(store, "i_fin", "high", team="finance")
    _ins(store, "i_back", "low", status=InsightStatus.RECURRING)
    cfg = Config.load(overrides={"auth": {"public_url": "https://aq.example.test"}, "alerts": {
        "min_severity": "high", "on_regression": True, "webhooks": [
            {"kind": "slack", "url_env": "HOOK_SLACK", "teams": ["platform"]},
            {"kind": "generic", "url_env": "HOOK_ALL", "min_severity": "low"},
            {"kind": "teams", "url_env": "HOOK_BROKEN"},
            {"kind": "slack", "url_env": "HOOK_UNSET"}]}})
    corr = CorrelationResult(new=["i_high", "i_low", "i_fin"], recurring=["i_back"], resolved=[],
                             regressions=["i_back"])
    sent = deliver(store, cfg, corr, "run_1")
    by = {(p, b.get("insight_id") or b.get("text", "")[:0]) for p, b in received}
    slack = [b for p, b in received if p == "/slack"]
    assert len(slack) == 2  # i_high (new, platform) and i_back (regression, any severity); not finance, not low
    assert any("https://aq.example.test/insights/i_high" in b["text"] for b in slack)
    generic = sorted(b["insight_id"] for p, b in received if p == "/all")
    assert generic == ["i_back", "i_fin", "i_high", "i_low"]
    assert by  # deliveries happened
    failed = [r for r in sent if "error" in r]
    assert {r["webhook"] for r in failed} == {"teams#2", "slack#3"}
    assert any("HTTP 500" in r["error"] for r in failed) and any("HOOK_UNSET" in r["error"] for r in failed)
    audit = store.audit_log(100)
    assert sum(a["action"] == "alert_sent" for a in audit) == 6
    assert sum(a["action"] == "alert_failed" for a in audit) == 6
    assert all(base not in str(a["detail"]) for a in audit)  # URLs are secrets


def test_regressions_off_and_unreachable_hook_never_raises(store, monkeypatch):
    monkeypatch.setenv("HOOK", "http://127.0.0.1:9/nothing-listens-here")
    _ins(store, "i_back", "high", status=InsightStatus.RECURRING)
    cfg = Config.load(overrides={"alerts": {"on_regression": False, "webhooks": [{"kind": "generic",
                                                                                   "url_env": "HOOK"}]}})
    assert deliver(store, cfg, CorrelationResult([], ["i_back"], [], ["i_back"])) == []
    out = deliver(store, cfg, CorrelationResult(["i_back"], [], [], []))
    assert len(out) == 1 and "error" in out[0]


def _second_harness(harness_dir, name: str):
    other = harness_dir.parent.parent / f"{name}-repo" / "harness"
    shutil.copytree(harness_dir, other)
    h = other / "harness.yaml"
    h.write_text(h.read_text().replace("agent: nkp-ops-assistant", f"agent: {name}"))
    git(other.parent, "init", "-q", "-b", "main")
    git(other.parent, "add", "-A")
    git(other.parent, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return other


def test_multi_agent_multi_team_evaluate_and_alerts(tmp_path, harness_dir, hook_server, monkeypatch, capsys):
    base, received = hook_server
    monkeypatch.setenv("AQ_HOOK", f"{base}/hook")
    billing = _second_harness(harness_dir, "billing-bot")
    cfgp = tmp_path / "ambientq.yaml"
    cfgp.write_text(yaml.safe_dump({
        "store": {"url": f"sqlite:///{tmp_path}/a.db"}, "llm": {"base_url": "mock", "model": "m"},
        "sampling": {"rate": 1.0},
        "agents": [{"name": "nkp-ops-assistant", "harness": str(harness_dir), "team": "platform"},
                   {"name": "billing-bot", "harness": str(billing), "team": "finance"}],
        "alerts": {"min_severity": "high", "webhooks": [{"kind": "generic", "url_env": "AQ_HOOK"}]}}))
    records, _ = synthetic.generate(1, 12)
    rng = random.Random(3)
    names = list(synthetic.SCENARIOS)
    other = [{**synthetic.to_record(f"b-{i}", synthetic.SCENARIOS[names[i % len(names)]][0](rng),
                                    1_760_200_000_000_000_000 + i), "agent": "billing-bot"} for i in range(12)]
    f = tmp_path / "t.jsonl"
    synthetic.write_jsonl(str(f), records + other)
    c = ["-c", str(cfgp)]
    assert main(c + ["ingest", str(f)]) == 0
    capsys.readouterr()
    assert main(c + ["evaluate", "--json"]) == 0
    runs = json.loads(capsys.readouterr().out)
    assert [(r["agent"], r["team"], r["candidates"]) for r in runs] == [
        ("nkp-ops-assistant", "platform", 12), ("billing-bot", "finance", 12)]
    store = SQLStore(f"sqlite:///{tmp_path}/a.db")
    teams = {i.team for i in store.insights()}
    assert teams == {"platform", "finance"}
    assert {c.team for c in store.list_conversations()} == {"platform", "finance"}
    assert {r.team for r in store.list_runs()} == {"platform", "finance"}
    assert {u["team"] for u in store.usage_rows()} >= {"platform", "finance"}
    # one alert per new high-severity insight, from both teams
    highs = [i for i in store.insights() if i.severity == "high"]
    assert sorted(b["insight_id"] for _, b in received) == sorted(i.insight_id for i in highs)
    assert {b["team"] for _, b in received} == {"platform", "finance"}
    # --agent and --team filters
    assert main(c + ["evaluate", "--agent", "billing-bot", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["candidates"] == 0
    assert main(c + ["evaluate", "--agent", "nope"]) == 2
    assert main(c + ["insights", "--team", "finance", "--json"]) == 0
    fin = json.loads(capsys.readouterr().out)
    assert fin and all(i["team"] == "finance" and i["agent_name"] == "billing-bot" for i in fin)
    # propose picks the insight's own agent harness
    target = next(i for i in fin if i["rubric_id"].startswith("R-"))
    assert main(c + ["propose", target["insight_id"], "--json"]) == 0
    prop = json.loads(capsys.readouterr().out)
    assert prop["team"] == "finance" and prop["agent_name"] == "billing-bot"
    assert prop["file"] and prop["status"] in ("PENDING_APPROVAL", "NOT_IMPROVED")
    assert main(c + ["proposals", "--team", "platform", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(c + ["proposals", "--team", "finance"]) == 0
    assert prop["proposal_id"] in capsys.readouterr().out
    store.close()
