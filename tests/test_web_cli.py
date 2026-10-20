import json
import urllib.error
import urllib.request

import pytest
import yaml

from ambientq.cli import main
from ambientq.demo import synthetic
from ambientq.web import App, serve_background


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def server(store):
    srv = serve_background(App(store, ingest_token="tok"), port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}", store
    srv.shutdown()


def test_otlp_receiver_and_dashboard(server):
    base, store = server
    records, truth = synthetic.generate(1, 8)
    otlp = [r for r in records if "resourceSpans" in r]
    code, _ = http("POST", base + "/v1/traces", json.dumps(otlp[0]).encode(), {"Content-Type": "application/json"})
    assert code == 401
    for r in otlp:
        code, body = http("POST", base + "/v1/traces", json.dumps(r).encode(),
                          {"Content-Type": "application/json", "Authorization": "Bearer tok"})
        assert code == 200 and json.loads(body)["conversations"] == 1
    code, body = http("POST", base + "/v1/traces", b"{not json", {"Authorization": "Bearer tok"})
    assert code == 400
    assert store.counts()["conversations"] == len(otlp)
    cid = sorted(store.list_conversations(), key=lambda c: c.conversation_id)[0].conversation_id
    for path in ("/", "/?status=NEW", "/runs", "/proposals", "/audit", f"/conversations/{cid}"):
        code, body = http("GET", base + path)
        assert code == 200 and "ambientq" in body, path
    assert http("GET", base + "/healthz")[0] == 200
    assert json.loads(http("GET", base + "/api/stats")[1])["conversations"] == len(otlp)
    assert json.loads(http("GET", base + f"/api/conversations/{cid}")[1])["conversation_id"] == cid
    assert http("GET", base + "/api/insights/nope")[0] == 404
    assert http("GET", base + "/nope")[0] == 404


def test_dashboard_escapes_html(server):
    base, store = server
    from ambientq.ingest.jsonl import record_to_conversation
    store.upsert_conversation(record_to_conversation(
        {"conversation_id": "x", "messages": [{"role": "user", "content": "<script>alert(1)</script>"}]}))
    body = http("GET", base + "/conversations/x")[1]
    assert "<script>alert" not in body and "&lt;script&gt;" in body


def test_cli_flow(tmp_path, harness_dir, capsys):
    cfgp = tmp_path / "ambientq.yaml"
    cfgp.write_text(yaml.safe_dump({"store": {"url": f"sqlite:///{tmp_path}/a.db"},
                                    "harness": {"path": str(harness_dir)},
                                    "llm": {"base_url": "mock", "model": "m"}, "sampling": {"rate": 1.0}}))
    records, _ = synthetic.generate(1, 15)
    f = tmp_path / "t.jsonl"
    synthetic.write_jsonl(str(f), records)
    c = ["-c", str(cfgp)]
    assert main(c + ["ingest", str(f)]) == 0
    assert "15 conversations" in capsys.readouterr().out
    assert main(c + ["evaluate", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["candidates"] == 15 and out["findings_kept"] > 0
    assert main(c + ["evaluate"]) == 0  # nothing new to grade
    assert "0 candidates" in capsys.readouterr().out
    assert main(c + ["insights", "--json"]) == 0
    ins = json.loads(capsys.readouterr().out)
    target = next(i for i in ins if i["rubric_id"].startswith("R-P"))
    assert main(c + ["insights", target["insight_id"]]) == 0
    assert main(c + ["investigate", target["insight_id"], "--locate-only"]) == 0
    assert "prompts/system.md" in capsys.readouterr().out
    assert main(c + ["propose", target["insight_id"], "--json"]) == 0
    prop = json.loads(capsys.readouterr().out)
    assert prop["status"] in ("PENDING_APPROVAL", "NOT_IMPROVED")
    assert main(c + ["approve", prop["proposal_id"], "--by", "sam", "--reject"]) == 0
    assert "REJECTED" in capsys.readouterr().out
    assert main(c + ["insights", "ins_doesnotexist"]) == 1
    assert main(c + ["evaluate", "--checks-only", "--all"]) == 0
    assert main(c + ["investigate", "ins_nope"]) == 2


def test_cli_since_parsing():
    from ambientq.cli import _since
    assert _since(None) == 0 and _since("1h") > 0
    with pytest.raises(SystemExit):
        _since("yesterday")
