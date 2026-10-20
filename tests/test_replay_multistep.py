"""Multi-step replay: the tool-result tape, unmatched-call policies, and full continuations."""

import json
import random

import pytest

from ambientq.checks import load_checks
from ambientq.demo import synthetic
from ambientq.ingest.jsonl import record_to_conversation
from ambientq.judge.rubric import build_rubric
from ambientq.llm import LLMClient
from ambientq.models import Conversation, Evidence, Finding, Insight, InsightStatus, Turn
from ambientq.replay import NOT_RECORDED, Replayer, ToolTape, continue_conversation, grade, replay


def conv_from(sc, cid="c1", seed=1) -> Conversation:
    return record_to_conversation(synthetic.to_record(cid, sc(random.Random(seed)), 0))


def scripted(*replies):
    """An LLM transport that answers with the given messages in order (then repeats the last)."""
    calls = []

    def transport(url, headers, body):
        calls.append(body)
        msg = replies[min(len(calls) - 1, len(replies) - 1)]
        return 200, {"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    return LLMClient("http://x/v1", "agent", transport=transport), calls


def call(name, args, cid="call_x"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


def test_tape_pairs_results_with_calls():
    conv = conv_from(synthetic.sc_scale_confirmed)
    tape = ToolTape.from_conversation(conv)
    assert len(tape.entries) == 1
    e = tape.entries[0]
    assert e.tool == "scale_nodepool" and json.loads(e.result)["ok"] is True and e.args["replicas"] == 6


def test_tape_pairs_results_without_call_ids():
    conv = Conversation("x", turns=[
        Turn(0, "user", "hi"), Turn(1, "assistant", tool_name="search_docs", tool_args={"query": "a"}),
        Turn(2, "tool", "R1", tool_name="search_docs"),
        Turn(3, "assistant", tool_name="search_docs", tool_args={"query": "b"}), Turn(4, "tool", "R2")])
    tape = ToolTape.from_conversation(conv)
    assert [(e.args["query"], e.result) for e in tape.entries] == [("a", "R1"), ("b", "R2")]


def test_tape_policies():
    tape = ToolTape.from_conversation(Conversation("x", turns=[
        Turn(0, "user", "q"), Turn(1, "assistant", tool_name="t", tool_call_id="1", tool_args={"a": 1, "b": 2}),
        Turn(2, "tool", "first", tool_call_id="1"),
        Turn(3, "assistant", tool_name="t", tool_call_id="2", tool_args={"a": 9, "b": 2}),
        Turn(4, "tool", "second", tool_call_id="2", is_error=True)]))
    t = tape.fresh()
    exact = t.answer("t", {"b": 2, "a": 9})  # key order doesn't matter
    assert exact.match == "exact" and exact.content == "second" and exact.is_error
    near = t.answer("t", {"a": 1, "b": 3})
    assert near.match == "nearest" and near.content == "first"
    assert t.answer("other", {}).content == NOT_RECORDED
    assert tape.fresh().answer("t", {"a": 5}, "error").match == "not_recorded"
    assert tape.fresh().answer("t", {"a": 5}, "stop").match == "stop"
    # each recorded result is used once, in order, for repeated identical calls
    t2 = tape.fresh()
    assert t2.answer("t", {"a": 1, "b": 2}).match == "exact"
    assert t2.answer("t", {"a": 1, "b": 2}).match == "nearest"


def test_continue_runs_to_final_answer_with_recorded_results(harness):
    conv = conv_from(synthetic.sc_scale_confirmed)
    cut = 3  # after the user's "Yes, confirmed."
    args = conv.turns[cut].tool_args
    llm, calls = scripted(call("scale_nodepool", args), say("Done: scaled."))
    out = continue_conversation(llm, harness, conv, cut, ToolTape.from_conversation(conv))
    roles = [t.role for t in out.turns[cut:]]
    assert roles == ["assistant", "tool", "assistant"]
    assert json.loads(out.turns[cut + 1].content)["ok"] is True  # the recorded result, not a live call
    assert out.metadata["stop_reason"] == "final_answer" and out.metadata["steps"] == 2
    assert out.metadata["unmatched_calls"] == []
    # the model saw the recorded tool result on its second step
    assert calls[1]["messages"][-1]["role"] == "tool"


def test_continue_flags_unmatched_and_undefined_calls(harness):
    conv = conv_from(synthetic.sc_health_no_status)  # never called get_cluster_status
    llm, _ = scripted(call("get_cluster_status", {"cluster": "prod-east"}, "a"),
                      call("drop_database", {}, "b"), say("ok"))
    out = continue_conversation(llm, harness, conv, 1, ToolTape.from_conversation(conv))
    flags = out.metadata["unmatched_calls"]
    assert [f["match"] for f in flags] == ["not_recorded", "not_recorded"]
    assert flags[1]["undefined_tool"] is True
    assert out.turns[2].is_error and "not recorded" in out.turns[2].content

    llm, _ = scripted(call("get_cluster_status", {"cluster": "prod-east"}, "a"), say("ok"))
    stop = continue_conversation(llm, harness, conv, 1, ToolTape.from_conversation(conv), unmatched="stop")
    assert stop.metadata["stop_reason"] == "unmatched_call" and stop.turns[-1].role == "assistant"

    with pytest.raises(ValueError):
        continue_conversation(llm, harness, conv, 1, ToolTape(), unmatched="guess")


def test_continue_stops_at_max_steps(harness):
    conv = conv_from(synthetic.sc_docs)
    llm, calls = scripted(call("search_docs", {"query": "loop"}))
    out = continue_conversation(llm, harness, conv, 1, ToolTape.from_conversation(conv), max_steps=3)
    assert out.metadata["stop_reason"] == "max_steps" and len(calls) == 3


def test_grade_checks_completion_only_for_finished_continuations(harness):
    conv = conv_from(synthetic.sc_docs)
    unfinished = Conversation("u", turns=conv.turns[:2], metadata={"stop_reason": "max_steps"})
    finished = Conversation("f", turns=conv.turns[:2], metadata={"stop_reason": "final_answer"})
    checks = load_checks()
    assert not [f for f in grade(unfinished, harness, None, checks) if f.source == "check:empty_final_answer"]
    assert [f for f in grade(finished, harness, None, checks) if f.source == "check:empty_final_answer"]


def test_replay_multi_vs_single_on_tool_using_fix(harness, llm):
    """A fix that needs a tool call is only fully exercised by multi-step replay."""
    conv = conv_from(synthetic.sc_health_no_status)
    ins = Insight("ins_x", "fp", "status", "procedure", "R-P1", conv.agent_name, InsightStatus.RECURRING,
                  "high", "r", "r", expected="Call get_cluster_status before answering about cluster health")
    loc = build_rubric(harness).get("R-P1").location
    patched = harness.with_replacement(
        loc, "- Always call `get_cluster_status` before answering any question about a cluster's health. MANDATORY.")
    f = Finding(conv.conversation_id, "judge", "procedure", ins.expected, "no status call", rubric_id="R-P1",
                evidence=[Evidence(1, conv.turns[1].content[:20])])
    multi = replay(ins, [(conv, [f])], harness, patched, llm, llm, replayer=Replayer("multi"))
    single = replay(ins, [(conv, [f])], harness, patched, llm, llm, replayer=Replayer("single"))
    assert multi.fixed == 1 and single.fixed == 1
    m = multi.to_dict()
    assert m["mode"] == "multi" and m["rows"][0]["steps"][1] == 2  # call + answer under the patch
    assert m["unmatched_calls"] == 1  # the status call had no recorded result: flagged
    assert single.to_dict()["rows"][0]["steps"] == [1, 1]
    with pytest.raises(ValueError):
        Replayer("both").run(llm, harness, conv, 1)


def test_library_answers_calls_this_conversation_never_recorded(store, cfg):
    """A new tool call is answered from another conversation's recording with the same arguments, flagged."""
    from ambientq.replay import build_library

    with_status = conv_from(synthetic.sc_health_ok)
    call = next(t for t in with_status.turns if t.tool_name == "get_cluster_status" and t.role == "assistant")
    lib = build_library([with_status])
    assert lib and all(not e.is_error for e in lib)
    tape = ToolTape(library=lib).fresh()
    ans = tape.answer("get_cluster_status", call.tool_args, "error")
    assert ans.match == "library" and "health" in ans.content
    assert tape.answer("get_cluster_status", {"cluster": "nowhere"}, "error").match == "not_recorded"
    assert tape.answer("get_cluster_status", call.tool_args, "stop").match == "stop"  # stop stays strict
    store.upsert_conversation(with_status)
    r = Replayer.from_config(cfg, store, with_status.agent_name)
    assert r.library and Replayer.from_config(cfg).library == []
    cfg.data["replay"]["library_size"] = 0
    assert Replayer.from_config(cfg, store, with_status.agent_name).library == []
