import subprocess

from ambientq.harness import Harness
from ambientq.judge.rubric import build_rubric
from ambientq.models import Location


def test_load_harness(harness):
    assert harness.agent == "nkp-ops-assistant"
    assert harness.tool_names() == {"get_cluster_status", "list_nai_endpoints", "scale_nodepool", "search_docs"}
    assert "Always call `get_cluster_status`" in harness.system_prompt()
    assert harness.config["max_turns"] == 8
    g = harness.guardrails[0]
    assert g.gid == "R-G1" and g.pattern and g.location.file == "guardrails.yaml"


def test_tool_locations_point_at_blocks(harness):
    spec = harness.tool("scale_nodepool")
    block = harness.lines(spec.location.file, spec.location.start_line, spec.location.end_line)
    assert "name: scale_nodepool" in block
    assert "search_docs" not in block and "list_nai_endpoints" not in block


def test_rubric_items_have_locations(harness):
    rub = build_rubric(harness)
    status = next(i for i in rub.items if "get_cluster_status" in i.text and i.rid.startswith("R-P"))
    assert harness.lines(status.location.file, status.location.start_line, status.location.end_line).startswith(
        "- Always call")
    assert rub.get("R-T-scale_nodepool").tool == "scale_nodepool"
    assert "cluster, nodepool, replicas" in rub.get("R-T-scale_nodepool").text
    assert {"G-GROUNDING", "G-COMPLETION", "G-TOOL-RESULT", "G-SAFETY", "R-G1"} <= rub.ids()
    assert rub.goal.startswith("Correct, grounded")
    assert "R-P1" in rub.render()


def test_version_git_and_hash(harness_dir, tmp_path):
    h = Harness.load(harness_dir)
    assert h.version().startswith("git:")
    (harness_dir / "prompts" / "system.md").write_text("changed must\n")
    assert Harness.load(harness_dir).version().startswith("sha256:")  # dirty tree -> content hash
    subprocess.run(["git", "-C", str(harness_dir), "checkout", "--", "."], check=True)


def test_with_replacement(harness):
    loc = Location("prompts/system.md", 8, 8)
    patched = harness.with_replacement(loc, "- Always call it.\n- Second line must stay.")
    assert patched.files["prompts/system.md"][7:9] == ["- Always call it.", "- Second line must stay."]
    assert harness.files["prompts/system.md"][7].startswith("- Always call `get_cluster_status`")
    assert patched.valid_location(Location("prompts/system.md", 1, 2))
    assert not patched.valid_location(Location("nope.md", 1, 1))
