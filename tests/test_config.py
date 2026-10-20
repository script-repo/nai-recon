import pytest

from ambientq.config import Config


def test_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("AMBIENTQ_SAMPLING_RATE", "0.75")
    monkeypatch.setenv("AMBIENTQ_LLM_BASE_URL", "https://nai.local/api/v1")
    cfg = Config.load()
    assert cfg.get("sampling", "rate") == 0.75
    assert cfg.get("llm", "base_url") == "https://nai.local/api/v1"
    assert cfg.get("budget", "max_tokens_per_run") == 400_000


def test_file_relative_harness_and_secret_env(tmp_path, monkeypatch):
    (tmp_path / "ambientq.yaml").write_text("harness: {path: agent}\nllm: {api_key_env: MY_KEY}\n")
    monkeypatch.setenv("MY_KEY", "s3cr3t")
    cfg = Config.load(tmp_path / "ambientq.yaml")
    assert cfg.get("harness", "path") == str((tmp_path / "agent").resolve())
    assert cfg.secret("llm", "api_key_env") == "s3cr3t"


def test_inline_secret_rejected(tmp_path):
    (tmp_path / "c.yaml").write_text("llm: {api_key: abc}\n")
    with pytest.raises(ValueError, match="api_key_env"):
        Config.load(tmp_path / "c.yaml")
