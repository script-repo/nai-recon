"""Audit log export for a SIEM (SPEC §11.6): JSON lines, ArcSight CEF, RFC 5424 syslog.

Each format is one line per audit event, so a SIEM's file collector (Splunk
UF, Elastic Agent, QRadar/Sentinel syslog forwarders) can tail the output.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from ambientq import __version__

FORMATS = ("jsonl", "cef", "syslog")

# RFC 5424 private enterprise number used in structured data (IANA "example" PEN, as in the RFC).
SD_ID = "ambientq@32473"
FACILITY = 13  # log audit

_NAMES = {
    "approved": "Proposal approved", "rejected": "Proposal rejected", "proposed": "Proposal created",
    "run_started": "Evaluation run started", "run_finished": "Evaluation run finished",
    "llm_call": "LLM call",
}


def severity(action: str) -> int:
    """CEF severity 0-10: failures and denials > decisions > sign-ins > routine > LLM calls."""
    a = (action or "").lower()
    if any(w in a for w in ("fail", "denied", "forbidden", "error", "csrf")):
        return 8
    if a in ("approved", "rejected"):
        return 7
    if any(w in a for w in ("sign", "login", "logout", "role")):
        return 5
    if a == "llm_call":
        return 1
    return 3


def syslog_severity(action: str) -> int:
    """RFC 5424 severity: 4 warning, 5 notice, 6 informational, 7 debug."""
    s = severity(action)
    return 4 if s >= 8 else 5 if s >= 6 else 6 if s >= 3 else 7


def event_name(action: str) -> str:
    return _NAMES.get(action, (action or "event").replace("_", " ").capitalize())


def _detail(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"raw": str(raw)}


def iso_utc(ts: float) -> str:
    """RFC 3339 / ISO-8601 in UTC with milliseconds: ``2026-10-20T10:15:00.123Z``."""
    return datetime.fromtimestamp(float(ts or 0), UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_event(row: dict[str, Any]) -> dict[str, Any]:
    return {"ts": iso_utc(row.get("ts") or 0), "id": row.get("id") or "", "actor": row.get("actor") or "",
            "action": row.get("action") or "", "subject": row.get("subject") or "",
            "severity": severity(row.get("action") or ""), "detail": _detail(row.get("detail"))}


# -- CEF ---------------------------------------------------------------------------
def cef_header(value: Any) -> str:
    """Escape a CEF header field: backslash and pipe; no line breaks allowed."""
    s = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return re.sub(r"[\r\n]+", " ", s)


def cef_ext(value: Any) -> str:
    """Escape a CEF extension value: backslash, equals, and CR/LF as ``\\r`` / ``\\n``."""
    s = str(value).replace("\\", "\\\\").replace("=", "\\=")
    return s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def cef_line(row: dict[str, Any]) -> str:
    action = row.get("action") or ""
    detail = _detail(row.get("detail"))
    head = "|".join(cef_header(x) for x in ("CEF:0", "ambientq", "ambientq", __version__, action,
                                             event_name(action), severity(action)))
    note = detail.get("note") if isinstance(detail, dict) else None
    ext = [("rt", str(int(float(row.get("ts") or 0) * 1000))), ("suser", row.get("actor") or ""),
           ("act", action), ("externalId", row.get("id") or ""),
           ("cs1Label", "detail"), ("cs1", json.dumps(detail, sort_keys=True, default=str)),
           ("cs2Label", "subject"), ("cs2", row.get("subject") or ""),
           ("msg", note or f"{event_name(action)} {row.get('subject') or ''}".strip())]
    return head + "|" + " ".join(f"{k}={cef_ext(v)}" for k, v in ext)


# -- syslog ------------------------------------------------------------------------
def sd_param(value: Any) -> str:
    """Escape an RFC 5424 SD-PARAM value (``"``, ``\\``, ``]``); control characters become spaces."""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def _token(value: str, max_len: int) -> str:
    """RFC 5424 header token: printable US-ASCII without spaces, or ``-``."""
    s = re.sub(r"[^\x21-\x7e]", "_", value or "")[:max_len]
    return s or "-"


def syslog_line(row: dict[str, Any], hostname: str | None = None) -> str:
    action = row.get("action") or ""
    pri = FACILITY * 8 + syslog_severity(action)
    host = _token(hostname if hostname is not None else socket.gethostname(), 255)
    sd = (f'[{SD_ID} actor="{sd_param(row.get("actor") or "")}" subject="{sd_param(row.get("subject") or "")}"'
          f' id="{sd_param(row.get("id") or "")}" severity="{severity(action)}"]')
    msg = json.dumps(_detail(row.get("detail")), sort_keys=True, default=str, ensure_ascii=False)
    return f"<{pri}>1 {iso_utc(row.get('ts') or 0)} {host} ambientq - {_token(action, 32)} {sd} {msg}"


# -- export ------------------------------------------------------------------------
def iter_export(rows: Iterable[dict[str, Any]], fmt: str, *, hostname: str | None = None) -> Iterator[str]:
    if fmt not in FORMATS:
        raise ValueError(f"--format must be one of {', '.join(FORMATS)}, got {fmt!r}")
    for row in rows:
        if fmt == "jsonl":
            yield json.dumps(to_event(row), sort_keys=True, default=str, ensure_ascii=False)
        elif fmt == "cef":
            yield cef_line(row)
        else:
            yield syslog_line(row, hostname)


def export_audit(rows: Iterable[dict[str, Any]], fmt: str, *, hostname: str | None = None) -> str:
    """Audit rows (as ``SQLStore.audit_log`` returns them) as newline-terminated lines in ``fmt``."""
    return "".join(line + "\n" for line in iter_export(rows, fmt, hostname=hostname))
