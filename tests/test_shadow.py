"""Shadow testing a proposal on traffic that arrived after it."""

import json
import random

import pytest

from ambientq.cli import main
from ambientq.demo import synthetic
from ambientq.evaluate import evaluate
from ambientq.ingest.jsonl import record_to_conversation
from ambientq.propose import propose
from ambientq.shadow import shadow_pending, shadow_test, verdict


def seed(store, scenarios, prefix):
    for i, sc in enumerate(scenarios):
        store.upsert_conversation(record_to_conversation(
            synthetic.to_record(f"{prefix}{i}", sc(random.Random(i + 100)), i)))


@pytest.fixture
def proposal(store, cfg, harness, llm):
    seed(store, [synthetic.sc_endpoints_leak] * 3 + [synthetic.sc_health_ok], "old")
    evaluate(store, cfg, harness, llm)
    ins = next(i for i in store.insights() if i.rubric_id.startswith("R-P") and i.category == "safety")
    return propose(store, cfg, ins.insight_id, harness, llm)


def test_verdict():
    assert verdict(2, 0, 1, 0) == "better"
    assert verdict(0, 0, 0, 0) == "same"
    assert verdict(1, 2, 0, 0) == "worse"
    assert verdict(3, 0, 0, 1) == "worse"  # the targeted defect appears under the patch


def test_shadow_runs_on_new_conversations_only(store, cfg, harness, llm, proposal):
    assert proposal["status"] == "PENDING_APPROVAL"
    none = shadow_test(store, cfg, proposal["proposal_id"], harness, llm, llm)
    assert none["conversations"] == 0 and none["verdict"] == "no_data"
    seed(store, [synthetic.sc_endpoints_leak] * 2 + [synthetic.sc_docs, synthetic.sc_health_ok], "new")
    res = shadow_test(store, cfg, proposal["proposal_id"], harness, llm, llm)
    assert res["conversations"] == 4
    assert res["insight_live"] == 2 and res["insight_patched"] == 0
    assert res["wins"] >= 2 and res["losses"] == 0 and res["verdict"] == "better"
    stored = store.get_proposal(proposal["proposal_id"])
    assert stored["shadow"]["verdict"] == "better"
    assert any(a["action"] == "shadow_tested" for a in store.audit_log())
    assert shadow_test(store, cfg, proposal["proposal_id"], harness, llm, llm, limit=1)["conversations"] == 1


def test_shadow_refuses_stale_harness(store, cfg, harness, llm, proposal, harness_dir):
    from ambientq.harness import Harness
    f = harness_dir / proposal["file"]
    f.write_text("# changed\n" + f.read_text())
    with pytest.raises(RuntimeError, match="changed"):
        shadow_test(store, cfg, proposal["proposal_id"], Harness.load(harness_dir), llm, llm)


def test_shadow_pending(store, cfg, harness, llm, proposal):
    seed(store, [synthetic.sc_endpoints_leak], "new")
    out = shadow_pending(store, cfg, harness, llm, llm)
    assert out[proposal["proposal_id"]]["verdict"] == "better"


def test_cli_shadow(tmp_path, harness_dir, capsys):
    import yaml
    cfgp = tmp_path / "a.yaml"
    cfgp.write_text(yaml.safe_dump({"store": {"url": f"sqlite:///{tmp_path}/a.db"},
                                    "harness": {"path": str(harness_dir)}, "llm": {"base_url": "mock"},
                                    "sampling": {"rate": 1.0}, "shadow": {"enabled": True}}))
    c = ["-c", str(cfgp)]
    f = tmp_path / "t.jsonl"
    synthetic.write_jsonl(str(f), [synthetic.to_record(f"x{i}", synthetic.sc_endpoints_leak(random.Random(i)), i)
                                   for i in range(3)])
    assert main(c + ["ingest", str(f)]) == 0
    assert main(c + ["evaluate"]) == 0
    capsys.readouterr()
    assert main(c + ["insights", "--json"]) == 0
    target = next(i for i in json.loads(capsys.readouterr().out) if i["rubric_id"].startswith("R-P"))
    assert main(c + ["propose", target["insight_id"], "--json"]) == 0
    pid = json.loads(capsys.readouterr().out)["proposal_id"]
    f2 = tmp_path / "t2.jsonl"
    synthetic.write_jsonl(str(f2), [synthetic.to_record("y1", synthetic.sc_endpoints_leak(random.Random(9)), 9)])
    assert main(c + ["ingest", str(f2)]) == 0
    assert main(c + ["evaluate"]) == 0
    assert f"shadow {pid}: better" in capsys.readouterr().out
    assert main(c + ["shadow", pid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "better"
