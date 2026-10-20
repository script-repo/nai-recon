"""End-to-end: the demo runs the whole loop with synthetic traces and the mock LLM."""

import json

import pytest

from ambientq.demo.run import APPROVER, run_demo
from ambientq.store import SQLStore

from .conftest import git

DEFECTS = {"health_no_status", "scale_no_confirm", "endpoints_leak", "tool_error_ignored"}


@pytest.mark.parametrize("http", [False, True], ids=["in-process", "http"])
def test_demo_end_to_end(tmp_path, http):
    res = run_demo(tmp_path / "w", http=http, log=lambda *a: None)
    # three simulated days, each with new conversations
    assert [r["candidates"] for r in res.runs] == [30, 30, 30]
    assert all(r["sampled"] < r["candidates"] for r in res.runs)  # sampling, not grade-everything
    assert all(r["tokens"] > 0 for r in res.runs)
    assert res.runs[0]["new"] > 0 and res.runs[1]["recurring"] > 0 and res.runs[2]["resolved"] > 0
    assert sum(r["rejected"] for r in res.runs) > 0  # the verifier removed unsupported claims

    statuses = {i["status"] for i in res.insights}
    assert {"RECURRING", "RESOLVED"} <= statuses
    resolved = [i for i in res.insights if i["status"] == "RESOLVED"]
    assert all("tool" in i["title"].lower() for i in resolved)  # the defect absent on days 2-3

    # day 3 arrived as AI-gateway proxy logs
    assert res.gateway and res.gateway["conversations"] == 30

    p = res.proposal
    assert p and p["status"] == "PENDING_APPROVAL" and p["kind"] == "prompt"
    assert p["replay"]["mode"] == "multi" and all(r["steps"] for r in p["replay"]["rows"])
    assert p["replay"]["fixed"] >= 1 and p["replay"]["regressions"] == 0 and p["replay"]["still_failing"] == 0
    repo = res.workdir / "agent-repo"
    assert p["branch"] in git(repo, "branch", "--list", "ambientq/*")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(repo, "status", "--porcelain").strip() == ""  # nothing applied to the working tree

    g = res.guardrail_proposal
    assert g and g["kind"] == "guardrail" and g["file"].endswith("guardrails.yaml")
    assert g["status"] == "PENDING_APPROVAL"
    # shadow test on day 4's new traffic
    assert res.shadow and res.shadow["conversations"] == 15 and res.shadow["verdict"] == "better"
    # approved through the dashboard, then reported
    assert res.approval and res.approval["code"] == 303 and res.approval["location"].endswith("m=approved")
    html_path, json_path = res.report_paths
    assert "<html" in html_path.read_text() and json.loads(json_path.read_text())["summary"]["decisions"] >= 1

    # every judged conversation with a planted defect produced at least one kept finding
    store = SQLStore(f"sqlite:///{res.workdir / 'ambientq.db'}")
    try:
        decided = store.get_proposal(p["proposal_id"])
        assert decided["status"] == "APPROVED" and decided["approved_by"] == APPROVER
        assert any(a["action"] == "approved" and "dashboard" in str(a["detail"]) for a in store.audit_log(500))
        assert store.usage_rows() and any(u["source"] != "ambientq" for u in store.usage_rows())
        flagged = {f.conversation_id for f in store.findings() if f.verified}
        # defects the deterministic checks can see are caught on every conversation...
        always = {cid for cid, sc in res.truth.items() if sc in ("endpoints_leak", "tool_error_ignored")}
        assert always <= flagged
        # ...judge-only defects are caught when sampled (sampling trades recall for cost)
        judged = store.judged_ids()
        assert 0 < len(judged) < len(res.truth)
        judge_only = {cid for cid, sc in res.truth.items() if sc in ("health_no_status", "scale_no_confirm")}
        assert judge_only & judged and (judge_only & judged) <= flagged
        clean = {cid for cid, sc in res.truth.items() if sc not in DEFECTS}
        assert not (clean & flagged)  # no false positives survive the verifier
    finally:
        store.close()
