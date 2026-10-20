"""Configuration: a YAML file plus ``AMBIENTQ_*`` environment overrides.

Secrets are never read from the file itself, only from environment variables
named in it (``api_key_env``, ``token_env``), which is how Kubernetes Secrets
are mounted.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "store": {"url": "sqlite:///ambientq.db"},
    "harness": {"path": "."},
    "llm": {
        "base_url": "http://localhost:9999/v1",
        "api_key_env": "NAI_API_KEY",
        "model": "judge",
        "verifier_model": None,
        "ca_bundle": None,
        "insecure_skip_verify": False,
        "timeout_s": 120,
        "temperature": 0.0,
        "max_retries": 2,
        "json_mode": True,
    },
    "sampling": {"rate": 0.2, "seed": "ambientq", "max_priority": 50},
    "budget": {"max_tokens_per_run": 400_000, "max_conversations_per_run": 200},
    "judge": {"max_findings": 8, "min_confidence": 0.5, "goal": None},
    "verifier": {"enabled": True, "llm": True},
    "insights": {"similarity": 0.5, "resolve_after_runs": 2},
    "replay": {"enabled": True, "model": None, "max_conversations": 5, "control_sample": 2},
    "forge": {"kind": None, "api_url": None, "repo": None, "token_env": "FORGE_TOKEN", "base_branch": "main"},
    "server": {"host": "127.0.0.1", "port": 8080, "ingest_token_env": "AMBIENTQ_INGEST_TOKEN"},
    "checks": {"disabled": [], "plugins": []},
    "ingest": {"redact": []},
}

ENV_MAP = {
    "AMBIENTQ_STORE_URL": ("store", "url"),
    "AMBIENTQ_HARNESS_PATH": ("harness", "path"),
    "AMBIENTQ_LLM_BASE_URL": ("llm", "base_url"),
    "AMBIENTQ_LLM_MODEL": ("llm", "model"),
    "AMBIENTQ_LLM_VERIFIER_MODEL": ("llm", "verifier_model"),
    "AMBIENTQ_LLM_CA_BUNDLE": ("llm", "ca_bundle"),
    "AMBIENTQ_SAMPLING_RATE": ("sampling", "rate"),
    "AMBIENTQ_BUDGET_MAX_TOKENS": ("budget", "max_tokens_per_run"),
    "AMBIENTQ_SERVER_HOST": ("server", "host"),
    "AMBIENTQ_SERVER_PORT": ("server", "port"),
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(old: Any, value: str) -> Any:
    if isinstance(old, bool):
        return value.lower() in ("1", "true", "yes")
    if isinstance(old, int):
        return int(value)
    if isinstance(old, float):
        return float(value)
    return value


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path | None = None

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)

    def secret(self, section: str, env_key: str) -> str | None:
        """Read a secret from the environment variable the config names."""
        env_name = self.get(section, env_key)
        if not env_name:
            return None
        return os.environ.get(env_name) or None

    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
        data = copy.deepcopy(DEFAULTS)
        p: Path | None = None
        if path is None and os.environ.get("AMBIENTQ_CONFIG"):
            path = os.environ["AMBIENTQ_CONFIG"]
        if path is not None:
            p = Path(path)
            loaded = yaml.safe_load(p.read_text()) or {}
            _reject_inline_secrets(loaded)
            data = _deep_merge(data, loaded)
            # Relative paths in the file are relative to the file.
            hp = data["harness"].get("path")
            if hp and not Path(hp).is_absolute():
                data["harness"]["path"] = str((p.parent / hp).resolve())
        for env, (sec, key) in ENV_MAP.items():
            if env in os.environ:
                data[sec][key] = _coerce(data[sec].get(key), os.environ[env])
        if overrides:
            data = _deep_merge(data, overrides)
        return cls(data=data, path=p)


_SECRET_KEYS = {"api_key", "token", "password", "secret"}


def _reject_inline_secrets(d: Any, trail: str = "") -> None:
    if isinstance(d, dict):
        for k, v in d.items():
            if k in _SECRET_KEYS and v:
                raise ValueError(
                    f"config key '{trail}{k}' holds a secret inline; put it in an environment "
                    f"variable and reference it with '{k}_env' instead"
                )
            _reject_inline_secrets(v, f"{trail}{k}.")
