import pytest

from ambientq.demo.mock_llm import MockLLM, serve
from ambientq.llm import Budget, BudgetExceeded, LLMClient, LLMError, extract_json


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Sure! Here: {"a": {"b": "}"}} done') == {"a": {"b": "}"}}
    assert extract_json("[1, 2]") == [1, 2]
    with pytest.raises(LLMError):
        extract_json("no json here")


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, url, headers, body):
        self.seen.append((url, dict(headers), dict(body)))
        return self.replies.pop(0)


OK = (200, {"choices": [{"message": {"content": '{"x": 1}'}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def test_auth_header_url_and_json_mode():
    t = FakeTransport([OK])
    audit = []
    c = LLMClient("https://nai.local/api/v1/", "llama", "k123", transport=t,
                  audit=lambda *a: audit.append(a))
    assert c.chat_json([{"role": "user", "content": "hi"}], purpose="judge") == {"x": 1}
    url, headers, body = t.seen[0]
    assert url == "https://nai.local/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k123"
    assert body["model"] == "llama" and body["response_format"] == {"type": "json_object"}
    assert c.budget.input_tokens == 10 and c.budget.output_tokens == 5
    detail = audit[0][3]
    assert "prompt_sha256" in detail and "hi" not in str(detail)  # audit never stores prompt content


def test_json_mode_fallback_and_retry(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    t = FakeTransport([(400, {"error": "response_format unsupported"}), (503, {}), OK])
    c = LLMClient("http://x/v1", "m", transport=t)
    assert c.chat_json([{"role": "user", "content": "hi"}], purpose="p") == {"x": 1}
    assert "response_format" not in t.seen[-1][2]


def test_errors_raise():
    c = LLMClient("http://x/v1", "m", transport=FakeTransport([(401, {"error": "bad key"})]))
    with pytest.raises(LLMError, match="401"):
        c.chat([{"role": "user", "content": "x"}], purpose="p")


def test_budget():
    c = LLMClient("http://x/v1", "m", transport=FakeTransport([OK]), budget=Budget(max_tokens=100))
    with pytest.raises(BudgetExceeded):
        c.chat([{"role": "user", "content": "x" * 1000}], purpose="p", max_tokens=50)
    assert Budget(0).can_afford(10**9)


def test_with_model_shares_budget():
    c = LLMClient("http://x/v1", "m", transport=MockLLM().transport)
    v = c.with_model("verifier")
    assert v.model == "verifier" and v.budget is c.budget and c.with_model(None) is c


def test_real_http_against_mock_server():
    srv = serve(port=0, require_key="sekret")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        good = LLMClient(base, "mock", "sekret", max_retries=0)
        r = good.chat([{"role": "user", "content": "List our NAI endpoints."}], purpose="replay")
        assert r.tool_calls[0]["function"]["name"] == "list_nai_endpoints"
        bad = LLMClient(base, "mock", "wrong", max_retries=0)
        with pytest.raises(LLMError, match="401"):
            bad.chat([{"role": "user", "content": "x"}], purpose="p")
    finally:
        srv.shutdown()
