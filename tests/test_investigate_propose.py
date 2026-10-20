import random
import subprocess

import pytest

from ambientq.demo import synthetic
from ambientq.evaluate import evaluate
from ambientq.harness import Harness
from ambientq.ingest.jsonl import record_to_conversation
from ambientq.investigate import investigate, locate, make_diff
from ambientq.judge.rubric import build_rubric
from ambientq.models import Insight, InsightStatus, Location
from ambientq.propose import decide, open_pull_request, propose
from ambientq.replay import cut_index, turns_to_messages

from .conftest import git


def seed(store, scenarios, prefix="c"):
    for i, sc in enumerate(scenarios):
        store.upsert_conversation(record_to_conversation(
            synthetic.to_record(f"{prefix}{i}", sc(random.Random(i)), i)))


@pytest.fixture
def evaluated(store, cfg, harness, llm):
    seed(store, [synthetic.sc_health_no_status] * 3 + [synthetic.sc_health_ok, synthetic.sc_scale_no_confirm,
                                                       synthetic.sc_docs, synthetic.sc_endpoints_ok])
    evaluate(store, cfg, harness, llm)
    return store


def insight_for(store, needle):
    return next(i for i in store.insights() if needle in i.title)


def test_evaluate_produces_expected_insights(evaluated):
    titles = [i.title for i in evaluated.insights()]
    assert any("get_cluster_status" in t for t in titles)
    assert any("scale_nodepool" in t for t in titles)
    rejected = [f for f in evaluated.findings() if f.verified is False]
    assert rejected and all("quote not found" in f.verifier_note for f in rejected)
    run = evaluated.list_runs()[0]
    assert run.candidates == 7 and run.evaluated == 7 and run.input_tokens > 0


def test_locate_uses_rubric_location(evaluated, harness, llm):
    ins = insight_for(evaluated, "get_cluster_status")
    loc, reason = locate(ins, harness, build_rubric(harness), llm)
    assert loc == Location("prompts/system.md", 8, 8) and "R-P1" in reason


def test_locate_tool_and_unknown_tool_and_llm_fallback(harness, llm):
    rub = build_rubric(harness)
    base = dict(fingerprint="f", title="t", agent_name="a", status=InsightStatus.NEW, severity="high",
                first_seen_run="r", last_seen_run="r")
    tool_ins = Insight(insight_id="i1", category="tool_arguments", rubric_id="", tool_name="scale_nodepool", **base)
    loc, _ = locate(tool_ins, harness, rub, None)
    assert loc.file == "tools/tools.yaml" and "scale_nodepool" in harness.lines(loc.file, loc.start_line, loc.end_line)
    unk = Insight(insight_id="i2", category="tool_selection", rubric_id="CFG-TOOLS", tool_name="search_docs", **base)
    loc, reason = locate(unk, harness, rub, None)
    assert "search_docs" in harness.lines(loc.file, loc.start_line, loc.end_line)
    vague = Insight(insight_id="i3", category="completion", rubric_id="G-COMPLETION",
                    title="Defect about cluster health answers", **{k: v for k, v in base.items() if k != "title"})
    loc, reason = locate(vague, harness, rub, llm)
    assert harness.valid_location(loc)


def test_investigate_diff_applies_with_git(evaluated, harness_dir, harness, llm):
    ins = insight_for(evaluated, "get_cluster_status")
    inv = investigate(evaluated, ins.insight_id, harness, llm)
    assert inv.patch and "MANDATORY" in inv.patch.replacement
    assert inv.patch.diff.startswith("--- a/harness/prompts/system.md")
    repo = harness_dir.parent
    (repo / "p.diff").write_text(inv.patch.diff)
    subprocess.run(["git", "-C", str(repo), "apply", "--check", "p.diff"], check=True)
    assert inv.examples and inv.to_dict()["location"] == "prompts/system.md:8-8"


def test_propose_replay_branch_and_approval(evaluated, cfg, harness_dir, harness, llm):
    ins = insight_for(evaluated, "get_cluster_status")
    p = propose(evaluated, cfg, ins.insight_id, harness, llm, branch=True)
    rep = p["replay"]
    assert p["status"] == "PENDING_APPROVAL"
    assert rep["fixed"] == 3 and rep["still_failing"] == 0 and rep["regressions"] == 0
    assert any(r["kind"] == "control" for r in rep["rows"])
    repo = harness_dir.parent
    branch = p["branch"]
    assert "MANDATORY" in git(repo, "show", f"{branch}:harness/prompts/system.md")
    assert "MANDATORY" not in (harness_dir / "prompts" / "system.md").read_text()  # working tree untouched
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(repo, "status", "--porcelain").strip() == "" or "p.diff" in git(repo, "status", "--porcelain")
    with pytest.raises(ValueError):
        decide(evaluated, p["proposal_id"], " ", approve=True)
    d = decide(evaluated, p["proposal_id"], "alex", approve=True, note="lgtm")
    assert d["status"] == "APPROVED" and d["approved_by"] == "alex"
    with pytest.raises(RuntimeError):
        decide(evaluated, p["proposal_id"], "alex", approve=False)
    actions = [a["action"] for a in evaluated.audit_log()]
    assert "approved" in actions and "proposed" in actions


def test_patch_that_does_not_help_is_not_proposed(store, cfg, harness, llm):
    # Guardrail patterns are enforced on output, not in the prompt: the mock agent ignores
    # guardrails.yaml, so a guardrail-file patch cannot fix the leak in replay.
    seed(store, [synthetic.sc_endpoints_leak] * 2)
    evaluate(store, cfg, harness, llm)
    ins = next(i for i in store.insights() if i.rubric_id == "R-G1")
    p = propose(store, cfg, ins.insight_id, harness, llm, branch=True)
    assert p["status"] == "NOT_IMPROVED" and p["branch"] is None


def test_open_pr_requires_forge_config(cfg):
    with pytest.raises(RuntimeError, match="forge"):
        open_pull_request(cfg, "b", "t", "body")


def test_replay_helpers(harness):
    c = record_to_conversation(synthetic.to_record("x", synthetic.sc_scale_confirmed(random.Random(1)), 0))
    msgs = turns_to_messages("SYS", c.turns)
    assert msgs[0] == {"role": "system", "content": "SYS"}
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user", "assistant", "tool", "assistant"]
    assert msgs[4]["tool_calls"][0]["id"] == msgs[5]["tool_call_id"]
    assert cut_index(c, []) == 3


def test_make_diff_outside_git(tmp_path, harness):
    import shutil
    shutil.copytree(harness.root, tmp_path / "h")
    h = Harness.load(tmp_path / "h")
    d = make_diff(h, Location("prompts/system.md", 1, 1), "# Renamed")
    assert "--- a/prompts/system.md" in d and "+# Renamed" in d
