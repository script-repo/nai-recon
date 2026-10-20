"""OpenAI-compatible chat client for Nutanix Enterprise AI (or any compatible server).

NAI exposes inference at ``https://<nai-host>/api/v1`` (``/chat/completions``),
authenticated with ``Authorization: Bearer <endpoint API key>``; the ``model``
field is the NAI endpoint name. Clusters commonly use a private CA, so a CA
bundle can be configured. Standard library only.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ambientq.models import sha256_text

log = logging.getLogger(__name__)

Transport = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any]]]


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class Budget:
    """Token budget for one run. ``max_tokens`` <= 0 means unlimited."""

    max_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def used(self) -> int:
        return self.input_tokens + self.output_tokens

    def can_afford(self, estimate: int) -> bool:
        return self.max_tokens <= 0 or self.used + estimate <= self.max_tokens

    def charge(self, usage: dict[str, Any]) -> None:
        self.calls += 1
        self.input_tokens += int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        self.output_tokens += int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)


def estimate_tokens(messages: list[dict[str, Any]], max_out: int = 1024) -> int:
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return chars // 4 + max_out


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and prose around it."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(text)):
                c = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find(open_c, start + 1)
    raise LLMError(f"model reply is not JSON: {text[:200]!r}")


def urllib_transport(timeout_s: float, ca_bundle: str | None, insecure: bool) -> Transport:
    if insecure:
        ctx = ssl._create_unverified_context()  # noqa: S323 - explicit lab opt-in
    else:
        ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()

    def send(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"error": {"message": raw[:500]}}

    return send


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        temperature: float = 0.0,
        timeout_s: float = 120,
        ca_bundle: str | None = None,
        insecure_skip_verify: bool = False,
        max_retries: int = 2,
        json_mode: bool = True,
        transport: Transport | None = None,
        budget: Budget | None = None,
        audit: Callable[[str, str, str, dict[str, Any]], None] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.temperature = temperature
        self.max_retries = max_retries
        self.json_mode = json_mode
        self.budget = budget or Budget()
        self._audit = audit
        self.on_usage = on_usage
        # Attribution for ``on_usage`` rows (``agent``, ``team``, ``run_id``); callers update it in place.
        self.usage_context: dict[str, Any] = {}
        if insecure_skip_verify:
            log.warning("TLS verification to the LLM endpoint is DISABLED (insecure_skip_verify)")
        self.transport = transport or urllib_transport(timeout_s, ca_bundle, insecure_skip_verify)

    @classmethod
    def from_config(cls, cfg: Any, *, model: str | None = None, transport: Transport | None = None,
                    budget: Budget | None = None, audit: Any = None, on_usage: Any = None) -> LLMClient:
        llm = cfg.section("llm")
        return cls(
            base_url=llm["base_url"], model=model or llm["model"], api_key=cfg.secret("llm", "api_key_env"),
            temperature=float(llm.get("temperature", 0.0)), timeout_s=float(llm.get("timeout_s", 120)),
            ca_bundle=llm.get("ca_bundle"), insecure_skip_verify=bool(llm.get("insecure_skip_verify")),
            max_retries=int(llm.get("max_retries", 2)), json_mode=bool(llm.get("json_mode", True)),
            transport=transport, budget=budget, audit=audit, on_usage=on_usage,
        )

    def with_model(self, model: str | None) -> LLMClient:
        if not model or model == self.model:
            return self
        other = object.__new__(LLMClient)
        other.__dict__.update(self.__dict__)
        other.model = model  # shares budget, audit, on_usage and usage_context with self
        return other

    def chat(self, messages: list[dict[str, Any]], *, purpose: str, tools: list[dict[str, Any]] | None = None,
             json_reply: bool = False, max_tokens: int = 1024) -> LLMResponse:
        estimate = estimate_tokens(messages, max_tokens)
        if not self.budget.can_afford(estimate):
            raise BudgetExceeded(f"token budget reached ({self.budget.used}/{self.budget.max_tokens})")
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature,
                                "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        if json_reply and self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "X-Ambientq-Purpose": purpose}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self.base_url}/chat/completions"
        last = ""
        for attempt in range(self.max_retries + 1):
            status, data = self.transport(url, headers, body)
            if status == 200:
                break
            last = f"HTTP {status}: {json.dumps(data)[:300]}"
            if status == 400 and "response_format" in body:
                body.pop("response_format")  # serving engine without JSON mode
                continue
            if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise LLMError(last)
        else:
            raise LLMError(last)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        self.budget.charge(usage)
        if self._audit:
            self._audit("ambientq", "llm_call", purpose, {
                "model": self.model, "prompt_sha256": sha256_text(json.dumps(messages, sort_keys=True)),
                "input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
            })
        resp = LLMResponse(content=msg.get("content") or "", tool_calls=msg.get("tool_calls") or [],
                           usage=usage, model=data.get("model", self.model))
        if self.on_usage:
            try:
                self.on_usage({"model": self.model, "purpose": purpose,
                               "input_tokens": int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
                               "output_tokens": int(usage.get("completion_tokens",
                                                              usage.get("output_tokens", 0)) or 0),
                               **self.usage_context})
            except Exception:  # noqa: BLE001 - cost tracking must never fail a grading call
                log.exception("usage hook failed")
        return resp

    def chat_json(self, messages: list[dict[str, Any]], *, purpose: str, max_tokens: int = 2048) -> Any:
        resp = self.chat(messages, purpose=purpose, json_reply=True, max_tokens=max_tokens)
        return extract_json(resp.content)
