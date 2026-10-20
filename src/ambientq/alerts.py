"""Alerts to Slack, Microsoft Teams or a generic webhook after each evaluation run.

One message per **new** insight at or above a webhook's ``min_severity``, and
per **regression** (a RESOLVED insight that came back), when
``alerts.on_regression`` is on. Webhook URLs are secrets: each webhook names
the environment variable that holds its URL (``url_env``). Every delivery and
every failure is audited, without the URL; a failed delivery never fails the run.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ambientq.config import Config
from ambientq.insights import CorrelationResult
from ambientq.models import Insight
from ambientq.store import SQLStore

log = logging.getLogger(__name__)
SEVERITY = {"low": 1, "medium": 2, "high": 3, "critical": 4}
PostFn = Callable[[str, bytes], int]


def http_post(url: str, body: bytes) -> int:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "ambientq"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - admin-configured webhook URL
            return int(r.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _text(ins: Insight, event: str, link: str) -> tuple[str, str]:
    head = ("Regression: a resolved insight is back" if event == "regression" else "New insight") + \
        f" [{ins.severity}] {ins.title}"
    body = (f"Agent {ins.agent_name or '-'} · team {ins.team or '-'} · {ins.category} · "
            f"{ins.occurrence_count} occurrence(s) in {ins.conversation_count} conversation(s)")
    if ins.expected:
        body += f"\nExpected: {ins.expected}"
    if link:
        body += f"\n{link}"
    return head, body


def payload(kind: str, ins: Insight, event: str, link: str = "") -> dict[str, Any]:
    head, body = _text(ins, event, link)
    if kind == "slack":
        return {"text": f"{head}\n{body}",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{head}*\n{body}"}}]}
    if kind == "teams":  # Adaptive Card, accepted by Teams Workflows and incoming webhooks
        facts = [{"title": k, "value": str(v)} for k, v in (
            ("Severity", ins.severity), ("Agent", ins.agent_name or "-"), ("Team", ins.team or "-"),
            ("Occurrences", ins.occurrence_count), ("Conversations", ins.conversation_count))]
        card: dict[str, Any] = {
            "type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "body": [
                {"type": "TextBlock", "text": head, "weight": "Bolder", "wrap": True,
                 "color": "Attention" if ins.severity in ("high", "critical") else "Default"},
                {"type": "FactSet", "facts": facts},
                *([{"type": "TextBlock", "text": f"Expected: {ins.expected}", "wrap": True}] if ins.expected else [])]}
        if link:
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in ambientq", "url": link}]
        return {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
    return {"event": event, "insight_id": ins.insight_id, "title": ins.title, "severity": ins.severity,
            "status": str(ins.status), "category": ins.category, "agent": ins.agent_name, "team": ins.team,
            "occurrences": ins.occurrence_count, "conversations": ins.conversation_count,
            "regressions": ins.regressions, "expected": ins.expected, "url": link or None}


def deliver(store: SQLStore, cfg: Config, corr: CorrelationResult, run_id: str = "",
            post: PostFn | None = None) -> list[dict[str, Any]]:
    """Send alerts for one run's correlation result; returns what was attempted."""
    a = cfg.section("alerts")
    hooks = a.get("webhooks") or []
    if not hooks:
        return []
    post = post or http_post
    base = str(cfg.get("auth", "public_url") or "").rstrip("/")
    regress = set(corr.regressions) if a.get("on_regression", True) else set()
    events = [(iid, "new") for iid in corr.new] + [(iid, "regression") for iid in corr.regressions if iid in regress]
    sent: list[dict[str, Any]] = []
    for idx, hook in enumerate(hooks):
        kind = str(hook.get("kind") or "generic")
        floor = SEVERITY.get(str(hook.get("min_severity") or a.get("min_severity") or "high"), 3)
        teams = set(hook.get("teams") or [])
        url = os.environ.get(str(hook.get("url_env") or ""), "")
        label = f"{kind}#{idx}"
        for iid, event in events:
            ins = store.get_insight(iid)
            if ins is None or (teams and ins.team not in teams):
                continue
            if event == "new" and SEVERITY.get(ins.severity, 0) < floor:
                continue
            rec = {"webhook": label, "insight": iid, "event": event, "run": run_id}
            if not url:
                rec["error"] = f"environment variable {hook.get('url_env')!r} is not set"
            else:
                body = json.dumps(payload(kind, ins, event, f"{base}/insights/{iid}" if base else "")).encode()
                try:
                    code = post(url, body)
                    if not 200 <= code < 300:
                        rec["error"] = f"HTTP {code}"
                except Exception as exc:  # network errors must never fail the run
                    rec["error"] = type(exc).__name__ + (f": {exc.reason}" if hasattr(exc, "reason") else "")
            store.audit("ambientq", "alert_failed" if "error" in rec else "alert_sent", iid, rec)
            if "error" in rec:
                log.warning("alert %s for %s failed: %s", label, iid, rec["error"])
            sent.append(rec)
    return sent
