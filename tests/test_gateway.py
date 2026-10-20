import gzip
import json
from pathlib import Path

import pytest
import yaml

from ambientq.cli import main
from ambientq.conversation import assemble
from ambientq.demo import synthetic
from ambientq.ingest import gateway as gw
from ambientq.ingest.jsonl import parse_files, parse_records
from ambientq.web import App, serve_background

from .test_web_cli import http

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "gateway"

USER = {"role": "user", "content": "Is prod-east healthy?"}
CALL = {"role": "assistant", "content": None, "tool_calls": [
    {"id": "call_1", "type": "function",
     "function": {"name": "get_cluster_status", "arguments": "{\"cluster\": \"prod-east\"}"}}]}
RESULT = {"role": "tool", "tool_call_id": "call_1", "content": "{\"health\": \"Healthy\"}"}
ANSWER = {"role": "assistant", "content": "prod-east is Healthy."}


def _resp(msg, tin=100, tout=10, model="llama-3-1-8b-instruct"):
    return {"id": "r", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": msg}],
            "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


def _litellm(msgs, out, t, session="s-1", **kw):
    rec = {"id": f"chatcmpl-{t}", "call_type": "acompletion", "model": "llama-3-1-8b-instruct",
           "startTime": t, "endTime": t + 0.5, "status": "success", "messages": msgs, "response": _resp(out),
           "prompt_tokens": 100, "completion_tokens": 10,
           "metadata": {"user_api_key_alias": "ops-key", "user_api_key_team_alias": "platform",
                        "requester_custom_headers": {"x-agent-name": "nkp-ops-assistant"}}}
    if session:
        rec["metadata"]["session_id"] = session
    rec.update(kw)
    return rec


def _pair():
    return [_litellm([USER], CALL, 1_760_000_000.0), _litellm([USER, CALL, RESULT], ANSWER, 1_760_000_001.0)]


def _one(records, fmt="auto", mappings=None):
    parsed = parse_records(records, fmt=fmt, mappings=mappings)
    convs = assemble(parsed.spans)
    assert len(convs) == 1, (parsed.errors, [c.conversation_id for c in convs])
    return convs[0], parsed


def test_litellm_tool_turns_usage_agent_team():
    conv, parsed = _one(_pair(), fmt="litellm")
    assert not parsed.errors
    assert conv.conversation_id == "s-1"
    assert [(t.role, t.tool_name, t.tool_call_id) for t in conv.turns] == [
        ("user", None, None), ("assistant", "get_cluster_status", "call_1"),
        ("tool", "get_cluster_status", "call_1"), ("assistant", None, None)]
    assert conv.turns[1].tool_args == {"cluster": "prod-east"}
    assert conv.turns[3].content == "prod-east is Healthy." and conv.turns[3].model == "llama-3-1-8b-instruct"
    assert conv.agent_name == "nkp-ops-assistant" and conv.team == "platform"
    assert (conv.input_tokens, conv.output_tokens) == (200, 20)
    span = parsed.spans[0]
    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert span.attributes["gen_ai.request.model"] == "llama-3-1-8b-instruct"
    assert span.start_ns == 1_760_000_000 * 10**9 and span.end_ns > span.start_ns


def test_litellm_spend_log_row_and_autodetect():
    row = {"request_id": "chatcmpl-9", "call_type": "acompletion", "model": "m", "session_id": "sess-9",
           "startTime": "2026-10-20T10:00:00Z", "endTime": "2026-10-20T10:00:01Z", "status": "success",
           "messages": {}, "proxy_server_request": {"model": "m", "messages": [USER]},
           "response": _resp(ANSWER), "prompt_tokens": 12, "completion_tokens": 3, "user": "svc-ops"}
    conv, parsed = _one([row])  # auto-detected as a LiteLLM proxy log
    assert conv.conversation_id == "sess-9" and conv.agent_name == "svc-ops"
    assert [t.role for t in conv.turns] == ["user", "assistant"]
    assert conv.started_ns == 1_792_490_400 * 10**9  # 2026-10-20T10:00Z


def test_envoy_access_log_dotted_keys_and_metadata_only_skipped():
    full = {"start_time": "2026-10-20T10:00:00.000Z", "duration": 850, "response_code": 200,
            "gen_ai.request.model": "gpt-4o-mini", "gen_ai.response.model": "gpt-4o-mini-2024",
            "gen_ai.usage.input_tokens": 15, "gen_ai.usage.output_tokens": 7, "session.id": "env-1",
            "x-agent-name": "helper", "request_body": json.dumps({"messages": [USER]}),
            "response_body": json.dumps(_resp(ANSWER))}
    meta_only = {k: v for k, v in full.items() if not k.endswith("_body")}
    conv, parsed = _one([full, meta_only], fmt="envoy-ai-gateway")
    assert conv.conversation_id == "env-1" and conv.agent_name == "helper"
    assert (conv.input_tokens, conv.output_tokens) == (15, 7)
    span = parsed.spans[0]
    assert span.attributes["gen_ai.response.model"] == "gpt-4o-mini-2024"
    assert span.end_ns - span.start_ns == 850 * 10**6
    assert any("no request/response messages" in e for e in parsed.errors)


def test_kong_payload_strings_and_case_insensitive_headers():
    rec = {"started_at": 1_760_000_000_000, "latencies": {"request": 900},
           "request": {"headers": {"X-Session-Id": "k-1", "X-Agent-Name": "kong-agent"}},
           "response": {"status": 200}, "consumer": {"username": "team-key"},
           "ai": {"proxy": {"payload": {"request": json.dumps({"model": "m", "messages": [USER]}),
                                        "response": json.dumps(_resp(ANSWER))},
                            "usage": {"prompt_tokens": 14, "completion_tokens": 21},
                            "meta": {"request_model": "m", "response_model": "m-1"}}}}
    conv, _ = _one([rec])  # auto-detects Kong
    assert conv.conversation_id == "k-1" and conv.agent_name == "kong-agent"
    assert (conv.input_tokens, conv.output_tokens) == (14, 21)
    assert conv.started_ns == 1_760_000_000 * 10**9
    rec["request"]["headers"] = {}
    assert _one([rec], fmt="kong")[0].agent_name == "team-key"


def test_generic_preset_and_custom_mappings():
    rec = {"ts": 1_760_000_000, "hdrs": {"conv": "c-7", "who": "custom-agent", "squad": "sre"},
           "req": {"model": "m", "msgs": [{"role": "system", "content": "SYS"}, USER]},
           "res": {"output": ANSWER, "tok": {"in": 5, "out": 2}}, "status": 200}
    mappings = {"messages": "req.msgs", "response": "res.output", "model": "req.model", "session": "hdrs.conv",
                "agent": ["hdrs.who"], "team": "hdrs.squad", "start_time": "ts", "usage_in": "res.tok.in",
                "usage_out": "res.tok.out"}
    conv, parsed = _one([rec], fmt="proxy-log", mappings=mappings)
    assert conv.conversation_id == "c-7" and conv.agent_name == "custom-agent" and conv.team == "sre"
    assert conv.metadata["system_prompt"] == "SYS"
    assert [(t.role, t.content) for t in conv.turns] == [("user", USER["content"]), ("assistant", ANSWER["content"])]
    assert (conv.input_tokens, conv.output_tokens) == (5, 2)
    generic = {"request": {"model": "m", "messages": [USER]}, "response": _resp(ANSWER),
               "request_headers": {"x-session-id": "g-1", "x-agent-name": "a"}}
    assert _one([generic])[0].conversation_id == "g-1"
    with pytest.raises(ValueError):
        gw.resolve_mappings("generic", {"nope": "x"})
    with pytest.raises(ValueError):
        gw.resolve_mappings("nope")


def test_session_fallback_hash_and_agent_fallbacks():
    recs = [_litellm([USER], CALL, 1.0, session=None), _litellm([USER, CALL, RESULT], ANSWER, 2.0, session=None)]
    conv, _ = _one(recs, fmt="litellm")
    assert conv.conversation_id.startswith("gw-") and len(conv.turns) == 4
    assert conv.conversation_id == _one(recs, fmt="litellm")[0].conversation_id  # stable
    other = _litellm([{"role": "user", "content": "List endpoints"}], ANSWER, 3.0, session=None)
    ids = {c.conversation_id for c in assemble(parse_records([*recs, other], fmt="litellm").spans)}
    assert len(ids) == 2
    # "=session" replaces the preset's session paths: everything falls back to the hash
    convs = assemble(parse_records(_pair(), fmt="litellm", mappings={"=session": []}).spans)
    assert len(convs) == 1 and convs[0].conversation_id.startswith("gw-")
    # agent: header, else key alias, else "unknown"
    rec = _litellm([USER], ANSWER, 1.0)
    rec["metadata"]["requester_custom_headers"] = {}
    assert _one([rec], fmt="litellm")[0].agent_name == "ops-key"
    bare = {"request": {"messages": [USER]}, "response": _resp(ANSWER)}
    assert _one([bare])[0].agent_name == "unknown"


def test_errors_mark_span_and_conversation():
    failed = _litellm([USER, CALL, RESULT], ANSWER, 2.0, status="failure", error_str="RateLimitError: 429")
    failed["response"] = {}
    conv, parsed = _one([_litellm([USER], CALL, 1.0), failed], fmt="litellm")
    bad = [s for s in parsed.spans if s.is_error]
    assert len(bad) == 1 and "RateLimitError" in bad[0].status_message
    assert conv.has_error and [t.role for t in conv.turns][-1] == "tool"
    http500 = {"request": {"messages": [USER]}, "response": "", "status": 500,
               "request_headers": {"x-session-id": "e"}}
    assert parse_records([http500], fmt="generic").spans[0].is_error


def test_tool_error_result_flagged():
    err = {**RESULT, "content": "{\"error\": \"timeout contacting cluster API\"}"}
    conv, _ = _one([_litellm([USER], CALL, 1.0), _litellm([USER, CALL, err], ANSWER, 2.0)], fmt="litellm")
    tool = next(t for t in conv.turns if t.role == "tool")
    assert tool.is_error and conv.has_error


def test_lookup_and_config_mappings():
    rec = {"a.b": {"c": [1, {"d": "x"}]}, "s": json.dumps({"k": 2}), "H": {"X-Id": "v"}}
    assert gw.lookup(rec, "a.b.c.1.d") == "x"
    assert gw.lookup(rec, "s.k") == 2
    assert gw.lookup(rec, "h.x-id") == "v"
    assert gw.lookup(rec, "a.b.c.5") is None
    m = gw.mappings_from_config({"session_fields": ["request_headers.x-conv"], "mappings": {"agent": "who"}})
    assert m == {"agent": ["who"], "session": ["request_headers.x-conv"]}
    resolved = gw.resolve_mappings("litellm", m)
    assert resolved["session"][0] == "request_headers.x-conv" and len(resolved["session"]) > 1
    assert resolved["agent"][0] == "who"


def test_autodetect_keeps_existing_shapes():
    records, truth = synthetic.generate(2, 21)
    parsed = parse_records(records)
    assert not parsed.errors
    assert len(parsed.conversations) == 3 and len(assemble(parsed.spans)) == 18
    lc = {"id": "r1", "run_type": "llm", "inputs": {"messages": [USER]}, "outputs": {}}
    assert len(parse_records([lc]).spans) == 1 and not parse_records([lc]).conversations
    assert not any(gw.is_proxy_log(r) for r in [*records, lc])
    # an explicit format refuses other shapes
    assert parse_records(records[:1], fmt="litellm").errors or not parse_records(records[:1], fmt="litellm").spans
    assert parse_records(_pair(), fmt="otlp").errors
    with pytest.raises(ValueError):
        parse_records([], fmt="nope")


@pytest.mark.parametrize("style", ["litellm", "generic"])
def test_synthetic_proxy_logs_match_scenarios(style):
    records, truth = synthetic.generate(1, 14)
    logs, truth2 = synthetic.generate_proxy_logs(1, 14, style=style)
    assert truth == truth2
    ref = {c.conversation_id: c for c in assemble(parse_records(records).spans)}
    ref.update({c.conversation_id: c for c in parse_records(records).conversations})
    parsed = parse_records(logs)
    assert not parsed.errors
    got = {c.conversation_id: c for c in assemble(parsed.spans)}
    assert set(got) == set(ref)
    for cid, c in ref.items():
        want = [(t.role, t.tool_name, t.content, t.tool_args or None, t.is_error) for t in c.turns]
        have = [(t.role, t.tool_name, t.content, t.tool_args or None, t.is_error) for t in got[cid].turns]
        assert have == want, cid
        assert got[cid].agent_name == synthetic.AGENT


def test_store_ingest_from_gateway(store):
    logs, truth = synthetic.generate_proxy_logs(3, 6)
    touched = store.ingest_spans(parse_records(logs).spans)
    assert touched == set(truth)
    conv = store.get_conversation(next(iter(truth)))
    assert conv and conv.agent_name == synthetic.AGENT and conv.input_tokens > 0 and conv.turns
    store.ingest_spans(parse_records(logs).spans)  # re-ingest is idempotent
    assert store.counts()["conversations"] == 6


@pytest.fixture
def gw_server(store):
    srv = serve_background(App(store, ingest_token="tok", gateway_mappings={"agent": "who"}), port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1/gateway/logs", store
    srv.shutdown()


def test_http_sink(gw_server):
    url, store = gw_server
    body = json.dumps(_pair()).encode()
    assert http("POST", url, body, {"Content-Type": "application/json"})[0] == 401
    assert http("POST", url, body, {"Authorization": "Bearer nope"})[0] == 401
    code, out = http("POST", url, body, {"Content-Type": "application/json", "Authorization": "Bearer tok"})
    assert code == 200 and json.loads(out) == {"records": 2, "spans": 2, "conversations": 1, "skipped": 0,
                                               "errors": []}
    assert len(store.get_conversation("s-1").turns) == 4
    lines = "\n".join(json.dumps({"request": {"messages": [USER]}, "response": _resp(ANSWER), "who": "posted",
                                  "session_id": f"h-{i}"}) for i in range(3)).encode()
    code, out = http("POST", url, gzip.compress(lines),
                     {"Content-Type": "application/x-ndjson", "Authorization": "Bearer tok"})
    assert code == 200 and json.loads(out)["conversations"] == 3
    assert store.get_conversation("h-0").agent_name == "posted"  # App's gateway_mappings apply
    assert http("POST", url, b"{nope", {"Authorization": "Bearer tok"})[0] == 400


def test_http_sink_without_token(store):
    srv = serve_background(App(store), port=0)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1/gateway/logs"
        code, out = http("POST", url, json.dumps(_pair()[0]).encode(), {"Content-Type": "application/json"})
        assert code == 200 and json.loads(out)["spans"] == 1
    finally:
        srv.shutdown()


def test_ingest_gateway_payload_direct(store):
    out = gw.ingest_gateway_payload(store, b"", "application/json")
    assert out["records"] == 0 and out["conversations"] == 0
    with pytest.raises(ValueError):
        gw.ingest_gateway_payload(store, b"\xff\xfe", "application/json")


def test_examples_parse():
    files = sorted(EXAMPLES.glob("*.json*"))
    assert files
    for f in files:
        fmt = {"litellm": "litellm", "envoy": "envoy-ai-gateway", "kong": "kong"}.get(f.stem.split("-")[0], "auto")
        parsed = parse_files([f], fmt=fmt)
        assert parsed.spans, f
        assert not [e for e in parsed.errors if "skipped" not in e], (f, parsed.errors)


def test_cli_format(tmp_path, capsys):
    cfgp = tmp_path / "ambientq.yaml"
    cfgp.write_text(yaml.safe_dump({"store": {"url": f"sqlite:///{tmp_path}/a.db"},
                                    "gateway": {"session_fields": ["metadata.conv"]}}))
    logs, truth = synthetic.generate_proxy_logs(1, 5)
    for e in logs:
        e["metadata"]["conv"] = "cli-" + e["metadata"]["session_id"]
    f = tmp_path / "gw.jsonl"
    synthetic.write_jsonl(str(f), logs)
    c = ["-c", str(cfgp)]
    assert main(c + ["ingest", "--format", "litellm", str(f)]) == 0
    assert "-> 5 conversations" in capsys.readouterr().out
    assert main(c + ["ingest", "--format", "otlp", str(f)]) == 1  # nothing in that format
    assert "unrecognized shape for --format otlp" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(c + ["ingest", "--format", "bogus", str(f)])
    from ambientq.store import SQLStore
    s = SQLStore(f"sqlite:///{tmp_path}/a.db")
    assert {cv.conversation_id for cv in s.list_conversations()} == {f"cli-{cid}" for cid in truth}
    s.close()
