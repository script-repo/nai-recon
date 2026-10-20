"""Fixes beyond the system prompt: tool descriptions, tool schemas, guardrails, config."""

import random
import subprocess

import pytest

from ambientq import patches
from ambientq.demo import synthetic
from ambientq.evaluate import evaluate
from ambientq.harness import Harness
from ambientq.ingest.jsonl import record_to_conversation
from ambientq.investigate import investigate
from ambientq.llm import LLMError
from ambientq.models import Insight, InsightStatus, PatchKind
from ambientq.propose import propose


def seed(store, scenarios):
    for i, sc in enumerate(scenarios):
        store.upsert_conversation(record_to_conversation(synthetic.to_record(f"c{i}", sc(random.Random(i)), i)))


@pytest.fixture
def graded(store, cfg, harness, llm):
    seed(store, [synthetic.sc_endpoints_leak] * 3 + [synthetic.sc_scale_no_confirm] * 3
         + [synthetic.sc_health_ok, synthetic.sc_docs])
    evaluate(store, cfg, harness, llm)
    return store


def applies(harness_dir, diff):
    top = subprocess.run(["git", "-C", str(harness_dir), "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, check=True).stdout.strip()
    r = subprocess.run(["git", "-C", top, "apply", "--check", "-"], input=diff, capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def manual_insight(store, **kw):
    base = dict(insight_id="ins_manual", fingerprint="fp-manual", title="t", category="procedure", rubric_id="",
                agent_name="nkp-ops-assistant", status=InsightStatus.RECURRING, severity="medium",
                first_seen_run="r", last_seen_run="r", occurrence_count=2, conversation_count=2)
    base.update(kw)
    ins = Insight(**base)
    store.save_insight(ins)
    store.commit()
    return ins


def test_choose_kind(harness):
    def ins(**kw):
        d = dict(insight_id="i", fingerprint="f", title="t", category="procedure", rubric_id="", agent_name="a",
                 status=InsightStatus.NEW, severity="high", first_seen_run="r", last_seen_run="r")
        d.update(kw)
        return Insight(**d)
    assert patches.choose_kind(ins(rubric_id="R-G1", category="safety"), harness) == PatchKind.GUARDRAIL
    assert patches.choose_kind(ins(rubric_id="CFG-MAX-TURNS", category="completion"), harness) == PatchKind.CONFIG
    assert patches.choose_kind(ins(rubric_id="R-T-scale_nodepool", category="tool_arguments",
                                   tool_name="scale_nodepool"), harness) == PatchKind.TOOL_SCHEMA
    assert patches.choose_kind(ins(rubric_id="R-T-search_docs", category="tool_selection"),
                               harness) == PatchKind.TOOL_DESCRIPTION
    assert patches.choose_kind(ins(rubric_id="R-P1"), harness) == PatchKind.PROMPT
    assert patches.choose_kind(ins(rubric_id="G-TOOL-RESULT", category="tool_results", tool_name="nope"),
                               harness) == PatchKind.PROMPT


def test_guardrail_patch_enforces_pattern_and_replay_proves_it(graded, cfg, harness, harness_dir, llm):
    g = next(i for i in graded.insights() if i.rubric_id.startswith("R-G"))
    p = propose(graded, cfg, g.insight_id, harness, llm)
    assert p["kind"] == "guardrail" and p["file"] == "guardrails.yaml"
    assert "+    action: redact" in p["diff"] and p["diff"].count("\n-") == 0  # minimal: one added line
    ok, err = applies(harness_dir, p["diff"])
    assert ok, err
    assert p["status"] == "PENDING_APPROVAL" and p["replay"]["fixed"] == 3  # the guardrail layer redacts in replay
    assert graded.get_proposal(p["proposal_id"])["kind"] == "guardrail"


def test_tool_description_patch_fixes_procedure_insight(graded, cfg, harness, harness_dir, llm):
    sc = next(i for i in graded.insights() if "scale_nodepool" in i.title)
    p = propose(graded, cfg, sc.insight_id, harness, llm, kind="tool_description")
    assert p["kind"] == "tool_description" and p["file"] == "tools/tools.yaml"
    assert "-    description: Change the replica count" in p["diff"]
    assert applies(harness_dir, p["diff"])[0]
    Harness.load(harness_dir)  # the original is untouched
    assert p["status"] == "PENDING_APPROVAL" and p["replay"]["fixed"] == 3
    # multi-step replay answered the agent's scale call from the recorded results and flagged arg mismatches
    assert p["replay"]["mode"] == "multi"


def test_tool_schema_patch(store, harness, harness_dir, llm):
    ins = manual_insight(store, category="tool_arguments", rubric_id="R-T-list_nai_endpoints",
                         tool_name="list_nai_endpoints", title="[tool_arguments] list_nai_endpoints args",
                         expected="call list_nai_endpoints with a namespace")
    inv = investigate(store, ins.insight_id, harness, llm)
    assert inv.kind == "tool_schema"
    assert "+      required:" in inv.patch.diff and "- namespace" in inv.patch.diff
    assert applies(harness_dir, inv.patch.diff)[0]
    patched = harness.with_replacement(inv.patch.location, inv.patch.replacement)
    assert patched.tool("list_nai_endpoints").parameters["required"] == ["namespace"]
    assert patched.tool("scale_nodepool").parameters == harness.tool("scale_nodepool").parameters


def test_config_patch(store, harness, harness_dir, llm):
    ins = manual_insight(store, category="completion", rubric_id="CFG-MAX-TURNS", title="[completion] limit",
                         expected="finish within 8 assistant steps")
    inv = investigate(store, ins.insight_id, harness, llm)
    assert inv.kind == "config"
    assert "-max_turns: 8" in inv.patch.diff and "+max_turns: 12" in inv.patch.diff
    assert applies(harness_dir, inv.patch.diff)[0]


def test_locate_only_reports_the_structured_target(graded, harness):
    g = next(i for i in graded.insights() if i.rubric_id.startswith("R-G"))
    inv = investigate(graded, g.insight_id, harness, None)
    assert inv.kind == "guardrail" and inv.location.file == "guardrails.yaml" and inv.patch is None


def test_validation_rejects_bad_fixes():
    with pytest.raises(LLMError):
        patches.validate_schema({"type": "object", "properties": {}}, {"properties": {"a": {}}})  # drops `a`
    with pytest.raises(LLMError):
        patches.validate_schema({"type": "object", "properties": {"a": {}}, "required": ["b"]}, {})
    with pytest.raises(LLMError):
        patches.validate_schema({"type": "object", "properties": {"a": {"pattern": "("}}}, {})
    with pytest.raises(LLMError):
        patches.validate_guardrail({"pattern": "(", "description": "x"})
    with pytest.raises(LLMError):
        patches.validate_guardrail({"description": "x", "action": "explode"})
    assert patches.validate_guardrail("Never say x") == {"description": "Never say x"}


def test_guardrail_layer_in_harness(harness):
    g = harness.guardrails[0]
    assert g.apply("key nai-AAAAAAAAAAAAAAAAAAAA") == "key nai-AAAAAAAAAAAAAAAAAAAA"  # detect-only by default
    g.action = "redact"
    assert g.apply("key nai-AAAAAAAAAAAAAAAAAAAA") == "key [REDACTED]"
    g.action = "block"
    assert g.apply("key nai-AAAAAAAAAAAAAAAAAAAA") == "I can't share that."
