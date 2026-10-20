"""HTTP surface: OTLP/HTTP trace receiver, read-only dashboard and JSON API."""

from __future__ import annotations

import hmac
import html
import json
import logging
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ambientq.ingest.otlp import decode_body
from ambientq.store import SQLStore

log = logging.getLogger(__name__)
MAX_BODY = 32 * 1024 * 1024

CSS = """
body{font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;color:#1d2433;background:#f6f7f9}
header{background:#1d2433;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:baseline}
header a{color:#cfd6e4;text-decoration:none}header b{font-size:16px}
main{padding:20px 24px;max-width:1200px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e3e6eb}
th,td{padding:7px 10px;border-bottom:1px solid #eceef2;text-align:left;vertical-align:top}
th{background:#fafbfc;font-weight:600;color:#4a5468}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.NEW{background:#fde8e8;color:#9b1c1c}.RECURRING{background:#fdf3d8;color:#8a5a00}.RESOLVED{background:#def7ec;color:#03543f}
.high{color:#9b1c1c;font-weight:600}.medium{color:#8a5a00}.low{color:#4a5468}
.cards{display:flex;gap:12px;margin-bottom:18px}.card{background:#fff;border:1px solid #e3e6eb;padding:10px 16px;min-width:120px}
.card .n{font-size:22px;font-weight:600}.muted{color:#6b7385}
pre{background:#fff;border:1px solid #e3e6eb;padding:10px;overflow:auto;white-space:pre-wrap}
.turn{background:#fff;border:1px solid #e3e6eb;margin:6px 0;padding:8px 10px}
.turn.user{border-left:4px solid #3f83f8}.turn.assistant{border-left:4px solid #0e9f6e}.turn.tool{border-left:4px solid #9061f9}
.turn.err{border-left:4px solid #e02424}.flag{background:#fff8e6}
"""


def _ts(v: float | None) -> str:
    return datetime.fromtimestamp(v, UTC).strftime("%Y-%m-%d %H:%M UTC") if v else ""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{_e(title)} · ambientq</title>"
            f"<style>{CSS}</style></head><body><header><b>ambientq</b><a href='/'>Insights</a>"
            f"<a href='/runs'>Runs</a><a href='/proposals'>Proposals</a><a href='/audit'>Audit</a>"
            f"<span class=muted>on-prem · read-only</span></header><main>{body}</main></body></html>")


class App:
    def __init__(self, store: SQLStore, ingest_token: str | None = None,
                 gateway_mappings: dict[str, Any] | None = None) -> None:
        self.store = store
        self.ingest_token = ingest_token
        self.gateway_mappings = gateway_mappings

    # -- pages ------------------------------------------------------------------
    def insights_page(self, status: str | None) -> str:
        c = self.store.counts()
        ins = self.store.insights(status=status)
        allins = self.store.insights()
        by = {s: sum(1 for i in allins if i.status == s) for s in ("NEW", "RECURRING", "RESOLVED")}
        cards = "".join(f"<div class=card><div class=muted>{k}</div><div class=n>{v}</div></div>" for k, v in
                        [("Conversations", c["conversations"]), ("Runs", c["runs"]), ("NEW", by["NEW"]),
                         ("RECURRING", by["RECURRING"]), ("RESOLVED", by["RESOLVED"])])
        filt = " ".join(f"<a href='/?status={s}'>{s}</a>" for s in ("NEW", "RECURRING", "RESOLVED")) + " <a href='/'>all</a>"
        rows = "".join(
            f"<tr><td><span class='pill {i.status}'>{i.status}</span></td><td class={i.severity}>{i.severity}</td>"
            f"<td><a href='/insights/{i.insight_id}'>{_e(i.title)}</a><div class=muted>{_e(i.agent_name)} · "
            f"{_e(i.rubric_id)}</div></td><td>{i.occurrence_count}</td><td>{i.conversation_count}</td>"
            f"<td class=muted>{_e(i.last_seen_run)}</td></tr>" for i in ins)
        return page("Insights", f"<div class=cards>{cards}</div><p>Filter: {filt}</p><table><tr><th>Status</th>"
                    f"<th>Severity</th><th>Insight</th><th>Occurrences</th><th>Conversations</th><th>Last seen</th>"
                    f"</tr>{rows or '<tr><td colspan=6 class=muted>No insights yet.</td></tr>'}</table>")

    def insight_page(self, iid: str) -> str | None:
        ins = self.store.get_insight(iid)
        if ins is None:
            return None
        occ = self.store.occurrences(ins.insight_id)
        fs = self.store.findings(insight_id=ins.insight_id)
        props = self.store.proposals(ins.insight_id)
        hist = "".join(f"<tr><td>{_e(o['run_id'])}</td><td>{o['count']}</td><td><span class='pill {o['status_after']}'>"
                       f"{o['status_after']}</span></td><td>{_ts(o['created_at'])}</td></tr>" for o in occ)
        frows = "".join(
            f"<tr><td><a href='/conversations/{_e(f.conversation_id)}'>{_e(f.conversation_id)}</a></td>"
            f"<td>{_e(f.source)}</td><td>{_e(f.actual)}</td><td class=muted>{_e(f.verifier_note)}</td></tr>"
            for f in fs[:50])
        prow = "".join(f"<tr><td>{_e(p['proposal_id'])}</td><td>{_e(p['status'])}</td><td>{_e(p['file'])}:"
                       f"{p['start_line']}-{p['end_line']}</td><td>{_e(p.get('branch') or '')}</td></tr>"
                       f"<tr><td colspan=4><pre>{_e(p['diff'])}</pre><div>{_e(p['rationale'])}</div></td></tr>"
                       for p in props)
        return page(ins.title, f"<h2>{_e(ins.title)}</h2><p><span class='pill {ins.status}'>{ins.status}</span> "
                    f"<span class={ins.severity}>{ins.severity}</span> · {_e(ins.category)} · rule {_e(ins.rubric_id)} · "
                    f"agent {_e(ins.agent_name)} · harness {_e(ins.harness_version_first)} → {_e(ins.harness_version_last)}</p>"
                    f"<p><b>Expected:</b> {_e(ins.expected)}</p>"
                    f"<h3>History</h3><table><tr><th>Run</th><th>Count</th><th>Status after</th><th>When</th></tr>{hist}</table>"
                    f"<h3>Findings</h3><table><tr><th>Conversation</th><th>Source</th><th>Actual</th><th>Verifier</th></tr>"
                    f"{frows}</table><h3>Proposals</h3><table>{prow or '<tr><td class=muted>None yet. Run '}"
                    f"{'' if prow else '<code>ambientq propose ' + _e(ins.insight_id) + '</code></td></tr>'}</table>")

    def conversation_page(self, cid: str) -> str | None:
        conv = self.store.get_conversation(cid)
        if conv is None:
            return None
        flagged = {e.turn for f in self.store.findings(conversation_id=cid) if f.verified for e in f.evidence}
        turns = []
        for t in conv.turns:
            cls = f"turn {t.role}{' err' if t.is_error else ''}{' flag' if t.index in flagged else ''}"
            head = f"#{t.index} {t.role}" + (f" → {t.tool_name}" if t.tool_name else "")
            body = _e(t.content) if t.content else (f"<code>{_e(json.dumps(t.tool_args))}</code>" if t.tool_args is not None else "")
            turns.append(f"<div class='{cls}'><div class=muted>{_e(head)}</div>{body}</div>")
        fs = self.store.findings(conversation_id=cid)
        frows = "".join(f"<tr><td>{'✔' if f.verified else '✘'}</td><td>{_e(f.source)}</td><td>{_e(f.rubric_id)}</td>"
                        f"<td>{_e(f.actual)}</td><td class=muted>{_e(f.verifier_note)}</td></tr>" for f in fs)
        return page(cid, f"<h2>Conversation {_e(cid)}</h2><p class=muted>agent {_e(conv.agent_name)} · "
                    f"{len(conv.turns)} turns · tokens {conv.input_tokens}/{conv.output_tokens}</p>"
                    f"<h3>Findings</h3><table><tr><th>Kept</th><th>Source</th><th>Rule</th><th>Actual</th><th>Verifier</th></tr>"
                    f"{frows}</table><h3>Transcript</h3>{''.join(turns)}")

    def runs_page(self) -> str:
        rows = "".join(f"<tr><td>{_e(r.run_id)}</td><td>{_ts(r.started_at)}</td><td>{_e(r.harness_version)}</td>"
                       f"<td>{r.candidates}</td><td>{r.sampled}</td><td>{r.evaluated}</td><td>{r.findings}</td>"
                       f"<td>{r.rejected}</td><td>{r.input_tokens + r.output_tokens}/{r.budget_tokens or '∞'}</td>"
                       f"<td>{_e(r.status)}</td></tr>" for r in self.store.list_runs())
        return page("Runs", "<table><tr><th>Run</th><th>Started</th><th>Harness</th><th>Candidates</th><th>Sampled</th>"
                    "<th>Judged</th><th>Findings</th><th>Rejected</th><th>Tokens/budget</th><th>Status</th></tr>"
                    f"{rows}</table>")

    def proposals_page(self) -> str:
        rows = "".join(f"<tr><td>{_e(p['proposal_id'])}</td><td><a href='/insights/{_e(p['insight_id'])}'>"
                       f"{_e(p['insight_id'])}</a></td><td>{_e(p['status'])}</td><td>{_e(p['file'])}:{p['start_line']}</td>"
                       f"<td>{_e((p.get('replay') or {}).get('fixed', '-'))}</td><td>{_e(p.get('approved_by') or '')}</td></tr>"
                       for p in self.store.proposals())
        return page("Proposals", "<p class=muted>Proposals are never applied by ambientq. Approve with "
                    "<code>ambientq approve &lt;id&gt; --by &lt;name&gt;</code>, then merge through your normal process.</p>"
                    "<table><tr><th>Proposal</th><th>Insight</th><th>Status</th><th>Target</th><th>Fixed in replay</th>"
                    f"<th>Decided by</th></tr>{rows}</table>")

    def audit_page(self) -> str:
        rows = "".join(f"<tr><td>{_ts(a['ts'])}</td><td>{_e(a['actor'])}</td><td>{_e(a['action'])}</td>"
                       f"<td>{_e(a['subject'])}</td><td class=muted>{_e(a['detail'])[:200]}</td></tr>"
                       for a in self.store.audit_log())
        return page("Audit", f"<table><tr><th>When</th><th>Actor</th><th>Action</th><th>Subject</th><th>Detail</th></tr>{rows}</table>")

    # -- API --------------------------------------------------------------------
    def api(self, path: str) -> Any:
        parts = path.strip("/").split("/")[1:]
        if parts == ["insights"]:
            return [asdict(i) for i in self.store.insights()]
        if len(parts) == 2 and parts[0] == "insights":
            ins = self.store.get_insight(parts[1])
            if not ins:
                return None
            return {**asdict(ins), "occurrences": self.store.occurrences(ins.insight_id),
                    "findings": [asdict(f) for f in self.store.findings(insight_id=ins.insight_id)],
                    "proposals": self.store.proposals(ins.insight_id)}
        if parts == ["runs"]:
            return [asdict(r) for r in self.store.list_runs()]
        if len(parts) == 2 and parts[0] == "conversations":
            conv = self.store.get_conversation(parts[1])
            return asdict(conv) if conv else None
        if parts == ["proposals"]:
            return self.store.proposals()
        if parts == ["stats"]:
            return self.store.counts()
        return None

    def ingest(self, body: bytes, content_type: str, auth: str | None) -> tuple[int, dict[str, Any]]:
        if self.ingest_token and not hmac.compare_digest(auth or "", f"Bearer {self.ingest_token}"):
            return 401, {"error": "unauthorized"}
        try:
            spans = decode_body(body, content_type)
        except Exception as exc:
            return 400, {"error": f"bad OTLP payload: {exc}"}
        touched = self.store.ingest_spans(spans)
        return 200, {"partialSuccess": {}, "spans": len(spans), "conversations": len(touched)}

    def ingest_gateway(self, body: bytes, content_type: str, auth: str | None) -> tuple[int, dict[str, Any]]:
        """``POST /v1/gateway/logs``: proxy-log records (JSON array, object or JSON lines)."""
        if self.ingest_token and not hmac.compare_digest(auth or "", f"Bearer {self.ingest_token}"):
            return 401, {"error": "unauthorized"}
        from ambientq.ingest.gateway import ingest_gateway_payload
        try:
            return 200, ingest_gateway_payload(self.store, body, content_type, self.gateway_mappings)
        except ValueError as exc:
            return 400, {"error": str(exc)}


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ambientq"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8") -> None:
            raw = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"
            try:
                if path == "/healthz":
                    return self._json(200, {"ok": True})
                if path.startswith("/api/"):
                    data = app.api(path)
                    return self._json(200, data) if data is not None else self._json(404, {"error": "not found"})
                html_out: str | None
                if path == "/":
                    html_out = app.insights_page(q["status"][0] if q.get("status") else None)
                elif path.startswith("/insights/"):
                    html_out = app.insight_page(path.split("/", 2)[2])
                elif path.startswith("/conversations/"):
                    html_out = app.conversation_page(path.split("/", 2)[2])
                elif path == "/runs":
                    html_out = app.runs_page()
                elif path == "/proposals":
                    html_out = app.proposals_page()
                elif path == "/audit":
                    html_out = app.audit_page()
                else:
                    html_out = None
                if html_out is None:
                    return self._send(404, page("Not found", "<p>Not found.</p>"))
                return self._send(200, html_out)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("request failed")
                return self._send(500, page("Error", f"<pre>{_e(exc)}</pre>"))

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path.rstrip("/")
            if route not in ("/v1/traces", "/v1/gateway/logs"):
                return self._json(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                return self._json(413, {"error": "payload too large"})
            body = self.rfile.read(n)
            if self.headers.get("Content-Encoding", "") == "gzip":
                import gzip
                body = gzip.decompress(body)
            handler = app.ingest_gateway if route == "/v1/gateway/logs" else app.ingest
            code, data = handler(body, self.headers.get("Content-Type", "application/json"),
                                 self.headers.get("Authorization"))
            return self._json(code, data)

    return ThreadingHTTPServer((host, port), Handler)


def serve_background(app: App, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    srv = make_server(app, host, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
