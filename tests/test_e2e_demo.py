"""End-to-end: the demo runs the whole loop with synthetic traces and the mock LLM."""

import pytest

from ambientq.demo.run import run_demo
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

    p = res.proposal
    assert p and p["status"] == "PENDING_APPROVAL"
    assert p["replay"]["fixed"] >= 1 and p["replay"]["regressions"] == 0 and p["replay"]["still_failing"] == 0
    repo = res.workdir / "agent-repo"
    assert p["branch"] in git(repo, "branch", "--list", "ambientq/*")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(repo, "status", "--porcelain").strip() == ""  # nothing applied to the working tree

    # every judged conversation with a planted defect produced at least one kept finding
    store = SQLStore(f"sqlite:///{res.workdir / 'ambientq.db'}")
    try:
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
