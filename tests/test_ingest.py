import json

from ambientq.conversation import assemble, build_conversation
from ambientq.demo import synthetic
from ambientq.ingest.jsonl import (
    langchain_run_to_span,
    parse_files,
    parse_records,
    record_to_conversation,
)
from ambientq.ingest.otlp import decode_export_request
from ambientq.models import Span


def _span(sid, start, attrs, **kw):
    return Span(span_id=sid, trace_id=kw.pop("trace", "t1"), name=kw.pop("name", "s"), start_ns=start,
                end_ns=start + 1, attributes=attrs, **kw)


def test_semconv_structured_messages_dedup_history():
    u = {"role": "user", "parts": [{"type": "text", "content": "Is prod-east healthy?"}]}
    call = {"role": "assistant", "parts": [{"type": "tool_call", "id": "c1", "name": "get_cluster_status",
                                             "arguments": {"cluster": "prod-east"}}]}
    res = {"role": "tool", "parts": [{"type": "tool_call_response", "id": "c1", "response": "{\"health\":\"ok\"}"}]}
    ans = {"role": "assistant", "parts": [{"type": "text", "content": "Healthy."}]}
    spans = [
        _span("a", 1, {"gen_ai.operation.name": "chat", "gen_ai.conversation.id": "conv1",
                       "gen_ai.system_instructions": json.dumps([{"type": "text", "content": "SYS"}]),
                       "gen_ai.input.messages": json.dumps([u]), "gen_ai.output.messages": json.dumps([call]),
                       "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 2}),
        _span("b", 2, {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "get_cluster_status",
                       "gen_ai.tool.call.id": "c1", "gen_ai.tool.call.result": "{\"health\":\"ok\"}"}),
        _span("c", 3, {"gen_ai.operation.name": "chat", "gen_ai.input.messages": json.dumps([u, call, res]),
                       "gen_ai.output.messages": json.dumps([ans])}),
    ]
    convs = assemble(spans)
    assert len(convs) == 1
    c = convs[0]
    assert c.conversation_id == "conv1"
    assert [(t.role, t.tool_name) for t in c.turns] == [
        ("user", None), ("assistant", "get_cluster_status"), ("tool", "get_cluster_status"), ("assistant", None)]
    assert c.turns[1].tool_args == {"cluster": "prod-east"}
    assert c.metadata["system_prompt"] == "SYS"
    assert c.input_tokens == 10 and c.output_tokens == 2


def test_legacy_flat_genai_attributes():
    s = _span("a", 1, {"gen_ai.system": "openai", "gen_ai.prompt.0.role": "user", "gen_ai.prompt.0.content": "hi",
                       "gen_ai.completion.0.role": "assistant", "gen_ai.completion.0.content": "hello",
                       "gen_ai.completion.0.tool_calls.0.name": "search_docs",
                       "gen_ai.completion.0.tool_calls.0.arguments": "{\"query\": \"x\"}"})
    c = build_conversation("x", [s])
    assert [(t.role, t.content, t.tool_name) for t in c.turns] == [
        ("user", "hi", None), ("assistant", "hello", None), ("assistant", "", "search_docs")]
    assert c.turns[2].tool_args == {"query": "x"}


def test_event_based_messages():
    s = _span("a", 1, {"gen_ai.operation.name": "chat"}, events=[
        {"name": "gen_ai.user.message", "attributes": {"content": "q?"}},
        {"name": "gen_ai.choice", "attributes": {"message": json.dumps({"content": "a!"})}},
    ])
    c = build_conversation("x", [s])
    assert [(t.role, t.content) for t in c.turns] == [("user", "q?"), ("assistant", "a!")]


def test_openinference_tool_span_pairs_with_call_without_id():
    rec = synthetic.to_otlp("oi1", synthetic.sc_health_ok(__import__("random").Random(1)), 10**18,
                            __import__("random").Random(2), style="openinference")
    c = assemble(decode_export_request(rec))[0]
    assert c.conversation_id == "oi1"
    roles = [(t.role, t.tool_name) for t in c.turns]
    assert roles == [("user", None), ("assistant", "get_cluster_status"), ("tool", "get_cluster_status"),
                     ("assistant", None)]
    assert c.turns[1].tool_call_id == c.turns[2].tool_call_id


def test_tool_error_flag_from_span_status():
    rec = synthetic.to_otlp("e1", synthetic.sc_tool_error_ignored(__import__("random").Random(1)), 10**18,
                            __import__("random").Random(3))
    c = assemble(decode_export_request(rec))[0]
    assert c.has_error and any(t.role == "tool" and t.is_error for t in c.turns)


def test_simple_record():
    rec = synthetic.to_record("r1", synthetic.sc_scale_confirmed(__import__("random").Random(1)), 5)
    c = record_to_conversation(rec)
    assert c.agent_name == synthetic.AGENT
    assert [t.role for t in c.turns] == ["user", "assistant", "user", "assistant", "tool", "assistant"]
    assert c.turns[3].tool_name == "scale_nodepool" and c.turns[4].tool_name == "scale_nodepool"


def test_langchain_runs():
    runs = [
        {"id": "r1", "trace_id": "T", "run_type": "llm", "start_time": "2026-01-01T00:00:00Z",
         "end_time": "2026-01-01T00:00:01Z", "extra": {"metadata": {"thread_id": "th1"}},
         "inputs": {"messages": [[{"lc": 1, "type": "constructor", "id": ["x", "HumanMessage"],
                                   "kwargs": {"content": "list endpoints"}}]]},
         "outputs": {"generations": [[{"message": {"type": "ai", "content": "",
                                                   "tool_calls": [{"id": "c9", "name": "list_nai_endpoints",
                                                                   "args": {}}]}}]]}},
        {"id": "r2", "trace_id": "T", "parent_run_id": "r0", "run_type": "tool", "name": "list_nai_endpoints",
         "start_time": "2026-01-01T00:00:02Z", "end_time": "2026-01-01T00:00:03Z",
         "extra": {"metadata": {"thread_id": "th1", "tool_call_id": "c9"}},
         "inputs": {"input": {}}, "outputs": {"output": "[]"}},
    ]
    parsed = parse_records(runs)
    assert not parsed.errors
    c = assemble(parsed.spans)[0]
    assert c.conversation_id == "th1"
    assert [(t.role, t.tool_name) for t in c.turns] == [
        ("user", None), ("assistant", "list_nai_endpoints"), ("tool", "list_nai_endpoints")]
    assert langchain_run_to_span(runs[1]).status_code == "OK"


def test_parse_files_mixed_and_errors(tmp_path):
    records, truth = synthetic.generate(1, 12)
    f = tmp_path / "t.jsonl"
    synthetic.write_jsonl(str(f), records + [{"weird": 1}])
    parsed = parse_files([tmp_path])
    convs = assemble(parsed.spans) + parsed.conversations
    assert {c.conversation_id for c in convs} == set(truth)
    assert len(parsed.errors) == 1 and "unrecognized" in parsed.errors[0]


def test_trace_without_session_attr_follows_root():
    spans = [_span("r", 1, {"gen_ai.conversation.id": "C"}, trace="T"),
             _span("x", 2, {"gen_ai.operation.name": "chat", "gen_ai.input.messages": "[]"}, trace="T")]
    assert [c.conversation_id for c in assemble(spans)] == ["C"]
