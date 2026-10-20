import sys
import textwrap

from ambientq.checks import load_checks, run_checks
from ambientq.models import Conversation, Turn


def conv(*turns):
    return Conversation("c", agent_name="nkp-ops-assistant",
                        turns=[Turn(index=i, **t) for i, t in enumerate(turns)])


def names(findings):
    return sorted(f.source for f in findings)


def test_clean_conversation_has_no_findings(harness):
    c = conv({"role": "user", "content": "hi"},
             {"role": "assistant", "tool_name": "get_cluster_status", "tool_call_id": "1", "tool_args": {"cluster": "a"}},
             {"role": "tool", "tool_call_id": "1", "content": "{}"},
             {"role": "assistant", "content": "ok"})
    assert run_checks(c, harness, load_checks()) == []


def test_unknown_tool_and_schema(harness):
    c = conv({"role": "user", "content": "x"},
             {"role": "assistant", "tool_name": "restart_cluster", "tool_call_id": "1", "tool_args": {}},
             {"role": "tool", "tool_call_id": "1", "content": "{}"},
             {"role": "assistant", "tool_name": "scale_nodepool", "tool_call_id": "2",
              "tool_args": {"cluster": "a", "size": 3}},
             {"role": "tool", "tool_call_id": "2", "content": "{}"},
             {"role": "assistant", "content": "done"})
    fs = run_checks(c, harness, load_checks())
    assert names(fs) == ["check:tool_args_schema", "check:unknown_tool"]
    schema = next(f for f in fs if f.source == "check:tool_args_schema")
    assert "nodepool" in schema.actual and "size" in schema.actual and schema.rubric_id == "R-T-scale_nodepool"


def test_error_ignored_unanswered_empty_final(harness):
    c = conv({"role": "user", "content": "x"},
             {"role": "assistant", "tool_name": "get_cluster_status", "tool_call_id": "1", "tool_args": {"cluster": "a"}},
             {"role": "tool", "tool_call_id": "1", "content": "{\"error\": 1}", "is_error": True},
             {"role": "assistant", "content": "All good!"},
             {"role": "assistant", "tool_name": "search_docs", "tool_call_id": "9", "tool_args": {"query": "q"}})
    assert names(run_checks(c, harness, load_checks())) == [
        "check:empty_final_answer", "check:tool_error_ignored", "check:unanswered_tool_call"]


def test_acknowledged_error_is_fine(harness):
    c = conv({"role": "user", "content": "x"},
             {"role": "assistant", "tool_name": "get_cluster_status", "tool_call_id": "1", "tool_args": {"cluster": "a"}},
             {"role": "tool", "tool_call_id": "1", "content": "err", "is_error": True},
             {"role": "assistant", "content": "The status call failed; retry later."})
    assert run_checks(c, harness, load_checks()) == []


def test_guardrail_pattern_and_max_turns(harness):
    harness.config["max_turns"] = 1
    c = conv({"role": "user", "content": "x"}, {"role": "assistant", "content": "a"},
             {"role": "assistant", "content": "key nai-ABCDEFGHIJKLMNOPQRST"})
    fs = run_checks(c, harness, load_checks())
    assert names(fs) == ["check:guardrail_patterns", "check:max_turns"]
    g = next(f for f in fs if f.source == "check:guardrail_patterns")
    assert g.rubric_id == "R-G1" and g.evidence[0].quote.startswith("nai-")


def test_plugins_and_disable(harness, tmp_path, monkeypatch):
    (tmp_path / "myplug.py").write_text(textwrap.dedent('''
        from ambientq.models import Finding
        def no_sorry(conv, harness):
            for t in conv.turns:
                if "sorry" in (t.content or "").lower():
                    yield Finding(conversation_id=conv.conversation_id, source="", category="output",
                                  expected="no apologies", actual="said sorry")
        def broken(conv, harness):
            raise RuntimeError("bug")
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    checks = load_checks(["myplug:no_sorry", "myplug:broken"], disabled=["empty_final_answer"])
    assert "empty_final_answer" not in checks
    c = conv({"role": "user", "content": "x"}, {"role": "assistant", "content": "Sorry!"})
    fs = run_checks(c, harness, checks)
    assert names(fs) == ["check:broken", "check:no_sorry"]
    assert next(f for f in fs if f.source == "check:broken").verified is False
    sys.modules.pop("myplug", None)
