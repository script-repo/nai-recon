"""HTTP surface: OTLP/HTTP and gateway-log receivers, the dashboard, approvals, and a JSON API.

Server-rendered HTML, no JavaScript. With ``auth.mode: none`` the dashboard is
read-only and has no write endpoints. With sign-in on (OIDC or a trusted
proxy), approvers can approve or reject proposals from the proposal page,
behind a per-session CSRF token and an Origin check; every decision, sign-in
and refused attempt goes to the audit log.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from ambientq import __version__
from ambientq.auth import (
    PENDING_COOKIE,
    SESSION_COOKIE,
    Auth,
    AuthError,
    Identity,
    check_csrf,
    parse_cookies,
    same_origin,
)
from ambientq.ingest.otlp import decode_body
from ambientq.models import ProposalStatus
from ambientq.store import SQLStore

log = logging.getLogger(__name__)
MAX_BODY = 32 * 1024 * 1024
MAX_FORM = 64 * 1024

CSS = """
:root{--bg:#f5f7fb;--panel:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--brand:#4f46e5;--brand-ink:#fff;
--side:#0b1220;--side-ink:#cbd5e1;--ok:#047857;--ok-bg:#d1fae5;--warn:#92400e;--warn-bg:#fef3c7;--bad:#b91c1c;
--bad-bg:#fee2e2;--info:#1d4ed8;--info-bg:#dbeafe;--radius:10px;
--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}html,body{margin:0}
body{font:14px/1.5 var(--sans);color:var(--ink);background:var(--bg);display:grid;grid-template-columns:232px 1fr;
min-height:100vh;-webkit-font-smoothing:antialiased}
aside{background:var(--side);color:var(--side-ink);padding:20px 14px;display:flex;flex-direction:column;gap:4px;
position:sticky;top:0;height:100vh}
aside .brand{color:#fff;font-weight:700;font-size:17px;letter-spacing:-.01em;padding:4px 10px 14px;display:flex;
align-items:center;gap:8px}
aside .brand i{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,#818cf8,#22d3ee);
display:inline-block}
aside a{color:var(--side-ink);text-decoration:none;padding:7px 10px;border-radius:7px;font-weight:500}
aside a:hover{background:#1e293b;color:#fff}aside a.on{background:#1e293b;color:#fff}
aside .sect{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;padding:14px 10px 4px}
aside .who{margin-top:auto;font-size:12px;padding:10px;border-top:1px solid #1e293b;color:#94a3b8}
aside .who b{color:#e2e8f0;display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis}
main{padding:28px 36px;max-width:1280px;width:100%}
h1{font-size:22px;letter-spacing:-.02em;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
h3{font-size:14px;margin:18px 0 8px;color:#334155}
.sub{color:var(--muted);margin:0 0 20px}
a{color:var(--brand)}a:hover{text-decoration:underline}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin:0 0 22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;font-weight:500}.card .n{font-size:24px;font-weight:650;margin-top:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px;margin:0 0 16px}
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--panel);border:1px solid var(--line);
border-radius:var(--radius);overflow:hidden}
th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}th{background:#f8fafc;font-weight:600;color:#475569;font-size:12px;
text-transform:uppercase;letter-spacing:.04em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.NEW,.REJECTED,.worse,.NOT_IMPROVED{background:var(--bad-bg);color:var(--bad)}
.RECURRING,.PENDING_APPROVAL,.same,.no_data{background:var(--warn-bg);color:var(--warn)}
.RESOLVED,.APPROVED,.better{background:var(--ok-bg);color:var(--ok)}
.DRAFT,.REPLAYED,.kind{background:var(--info-bg);color:var(--info)}
.high{color:var(--bad);font-weight:600}.medium{color:var(--warn);font-weight:600}.low{color:var(--muted)}
.muted{color:var(--muted)}.small{font-size:12px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}
.chips a{padding:4px 11px;border:1px solid var(--line);border-radius:999px;background:var(--panel);
text-decoration:none;color:#334155;font-size:13px}.chips a.on{background:var(--ink);color:#fff;border-color:var(--ink)}
code,pre{font-family:var(--mono);font-size:12.5px}
pre{background:#0b1220;color:#e2e8f0;border-radius:var(--radius);padding:12px 14px;overflow:auto;margin:0}
.diff{white-space:pre;line-height:1.45}.diff .a{color:#86efac;display:block}.diff .d{color:#fca5a5;display:block}
.diff .h{color:#93c5fd;display:block}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:1000px){.grid2{grid-template-columns:1fr}}
.turn{background:var(--panel);border:1px solid var(--line);border-left:4px solid #94a3b8;border-radius:8px;
margin:8px 0;padding:9px 12px}
.turn.user{border-left-color:#3b82f6}.turn.assistant{border-left-color:#10b981}.turn.tool{border-left-color:#8b5cf6}
.turn.err{border-left-color:#ef4444}.turn.flag{background:#fffbeb}
.reply{background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px}
.stat{display:flex;gap:18px;flex-wrap:wrap}.stat div{min-width:90px}.stat b{display:block;font-size:20px}
form.decide textarea{width:100%;min-height:64px;border:1px solid var(--line);border-radius:8px;padding:8px;
font:inherit;margin:8px 0}
button{font:inherit;font-weight:600;border:0;border-radius:8px;padding:8px 16px;cursor:pointer}
button.ok{background:var(--ok);color:#fff}button.bad{background:var(--bad);color:#fff}
button.plain{background:var(--brand);color:var(--brand-ink)}
.flash{background:var(--info-bg);color:var(--info);border-radius:8px;padding:10px 14px;margin:0 0 16px}
.flash.error{background:var(--bad-bg);color:var(--bad)}
select{font:inherit;padding:5px 8px;border-radius:7px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;
width:100%}
@media print{body{display:block;background:#fff}aside,.noprint,form.decide{display:none}main{padding:0}
.panel,table,.card{break-inside:avoid}}
"""


def _ts(v: float | None) -> str:
    return datetime.fromtimestamp(v, UTC).strftime("%Y-%m-%d %H:%M UTC") if v else ""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _pill(v: Any, extra: str = "") -> str:
    return f"<span class='pill {_e(v)} {extra}'>{_e(v)}</span>"


def render_diff(diff: str) -> str:
    out = []
    for ln in (diff or "").splitlines():
        cls = "a" if ln.startswith("+") and not ln.startswith("+++") else \
            "d" if ln.startswith("-") and not ln.startswith("---") else "h" if ln.startswith("@@") else ""
        out.append(f"<span class={cls}>{_e(ln) or ' '}</span>" if cls else _e(ln))
    return "<pre class=diff>" + "\n".join(o if o.startswith("<span") else o for o in out) + "</pre>"


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: Any = field(default_factory=dict)
    client_ip: str = "127.0.0.1"
    form: dict[str, str] = field(default_factory=dict)
    host_url: str = "http://localhost"


@dataclass
class Response:
    code: int
    body: str | bytes
    ctype: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


def redirect(location: str, *cookies: str) -> Response:
    return Response(303, "", headers=[("Location", location), *(("Set-Cookie", c) for c in cookies)])


class App:
    def __init__(self, store: SQLStore, ingest_token: str | None = None,
                 gateway_mappings: dict[str, Any] | None = None, *, auth: Auth | None = None,
                 cfg: Any = None) -> None:
        self.store = store
        self.ingest_token = ingest_token
        self.gateway_mappings = gateway_mappings
        self.auth = auth
        self.cfg = cfg

    @property
    def auth_on(self) -> bool:
        return bool(self.auth and self.auth.enabled)

    # -- layout -----------------------------------------------------------------
    def teams(self, ident: Identity | None) -> list[str]:
        rows = self.store.query("SELECT DISTINCT team FROM conversations WHERE team IS NOT NULL AND team <> '' "
                                "UNION SELECT DISTINCT team FROM insights WHERE team IS NOT NULL AND team <> ''")
        teams = sorted({str(r["team"]) for r in rows})
        vis = ident.visible_teams() if ident else None
        return [t for t in teams if vis is None or t in vis]

    def layout(self, title: str, body: str, ident: Identity | None, active: str = "", team: str | None = None,
               flash: str = "", flash_kind: str = "") -> str:
        nav = [("/", "Insights", "insights"), ("/proposals", "Proposals", "proposals"), ("/runs", "Runs", "runs"),
               ("/usage", "Usage & cost", "usage"), ("/reports", "Reports", "reports")]
        if not self.auth_on or (ident and ident.can("admin")):
            nav.append(("/audit", "Audit log", "audit"))
        tq = f"?team={quote(team)}" if team else ""
        links = "".join(f"<a href='{h}{tq}' class='{'on' if a == active else ''}'>{t}</a>" for h, t, a in nav)
        teams = self.teams(ident)
        switch = ""
        if len(teams) > 1:
            opts = "".join(f"<option value='{_e(t)}'{' selected' if t == team else ''}>{_e(t)}</option>"
                           for t in teams)
            switch = (f"<div class=sect>Team</div><form method=get action='/{active if active != 'insights' else ''}'>"
                      f"<select name=team><option value=''>All teams</option>{opts}</select>"
                      f"<button class='plain small noprint' style='margin-top:6px;width:100%'>Switch</button></form>")
        if not self.auth_on:
            who = "<b>Read-only</b>sign-in is off"
        elif ident:
            who = f"<b>{_e(ident.name)}</b>{_e(ident.role(team))} · <a href='/logout' style='padding:0'>sign out</a>"
        else:
            who = "<b>Signed out</b>"
        fl = f"<div class='flash {flash_kind}'>{_e(flash)}</div>" if flash else ""
        return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
                f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{_e(title)} · ambientq</title>"
                f"<style>{CSS}</style></head><body><aside><div class=brand><i></i>ambientq</div>"
                f"<div class=sect>Quality</div>{links}{switch}"
                f"<div class=who>{who}<div class=small style='margin-top:6px'>v{__version__} · on-prem</div></div>"
                f"</aside><main>{fl}{body}</main></body></html>")

    def _team_ok(self, ident: Identity | None, team: str | None) -> bool:
        if not self.auth_on:
            return True
        if ident is None:
            return False
        vis = ident.visible_teams()
        return vis is None or (team or "") in vis

    def _filter(self, ident: Identity | None, items: list[Any], key: Any) -> list[Any]:
        if not self.auth_on or ident is None or ident.visible_teams() is None:
            return items
        vis = ident.visible_teams() or set()
        return [i for i in items if (key(i) or "") in vis]

    # -- pages ------------------------------------------------------------------
    def insights_page(self, status: str | None, ident: Identity | None = None, team: str | None = None) -> str:
        allins = self._filter(ident, self.store.insights(team=team), lambda i: i.team)
        ins = [i for i in allins if not status or str(i.status) == status]
        by = {s: sum(1 for i in allins if i.status == s) for s in ("NEW", "RECURRING", "RESOLVED")}
        convs = self.store.query("SELECT COUNT(*) AS n FROM conversations" + (" WHERE team=?" if team else ""),
                                 (team,) if team else ())[0]["n"]
        pending = len([p for p in self.store.proposals(team=team, status=str(ProposalStatus.PENDING_APPROVAL))])
        regress = sum(i.regressions for i in allins)
        cards = "".join(f"<div class=card><div class=k>{k}</div><div class=n>{v}</div></div>" for k, v in
                        [("Conversations", convs), ("New", by["NEW"]), ("Recurring", by["RECURRING"]),
                         ("Resolved", by["RESOLVED"]), ("Regressions", regress), ("Awaiting approval", pending)])
        tq = f"&team={quote(team)}" if team else ""
        chips = "".join(f"<a href='/?status={s}{tq}' class='{'on' if status == s else ''}'>{s.title()}</a>"
                        for s in ("NEW", "RECURRING", "RESOLVED"))
        chips = f"<a href='/{'?team=' + quote(team) if team else ''}' class='{'' if status else 'on'}'>All</a>" + chips
        rows = "".join(
            f"<tr><td>{_pill(i.status)}</td><td class={i.severity}>{i.severity}</td>"
            f"<td><a href='/insights/{i.insight_id}'>{_e(i.title)}</a><div class='muted small'>{_e(i.agent_name)} · "
            f"{_e(i.rubric_id)}{' · team ' + _e(i.team) if i.team else ''}"
            f"{' · ' + str(i.regressions) + ' regression(s)' if i.regressions else ''}</div></td>"
            f"<td class=num>{i.occurrence_count}</td><td class=num>{i.conversation_count}</td>"
            f"<td class='muted small'>{_ts(i.updated_at)}</td></tr>" for i in ins)
        return self.layout("Insights", f"<h1>Insights</h1><p class=sub>Recurring defects found in production "
                           f"conversations, deduplicated across runs.</p><div class=cards>{cards}</div>"
                           f"<div class=chips>{chips}</div><table><tr><th>Status</th><th>Severity</th><th>Insight</th>"
                           f"<th class=num>Occurrences</th><th class=num>Conversations</th><th>Updated</th></tr>"
                           f"{rows or '<tr><td colspan=6 class=muted>No insights yet.</td></tr>'}</table>",
                           ident, "insights", team)

    def insight_page(self, iid: str, ident: Identity | None = None, team: str | None = None) -> str | None:
        ins = self.store.get_insight(iid)
        if ins is None or not self._team_ok(ident, ins.team):
            return None
        occ = self.store.occurrences(ins.insight_id)
        fs = self.store.findings(insight_id=ins.insight_id)
        props = self.store.proposals(ins.insight_id)
        hist = "".join(f"<tr><td>{_e(o['run_id'])}</td><td class=num>{o['count']}</td><td>{_pill(o['status_after'])}"
                       f"</td><td class='muted small'>{_ts(o['created_at'])}</td></tr>" for o in occ)
        frows = "".join(
            f"<tr><td><a href='/conversations/{quote(f.conversation_id)}'>{_e(f.conversation_id)}</a></td>"
            f"<td>{_e(f.source)}</td><td>{_e(f.actual)}</td><td class='muted small'>{_e(f.verifier_note)}</td></tr>"
            for f in fs[:50])
        prow = "".join(f"<tr><td><a href='/proposals/{_e(p['proposal_id'])}'>{_e(p['proposal_id'])}</a></td>"
                       f"<td>{_pill(p['status'])}</td><td>{_pill(p.get('kind') or 'prompt', 'kind')}</td>"
                       f"<td><code>{_e(p['file'])}:{p['start_line']}-{p['end_line']}</code></td></tr>" for p in props)
        none = f"<tr><td colspan=4 class=muted>None yet. Run <code>ambientq propose {_e(ins.insight_id)}</code></td></tr>"
        return self.layout(ins.title, f"<h1>{_e(ins.title)}</h1><p class=sub>{_pill(ins.status)} "
                           f"<span class={ins.severity}>{ins.severity}</span> · {_e(ins.category)} · rule "
                           f"{_e(ins.rubric_id)} · agent {_e(ins.agent_name)} · team {_e(ins.team or '-')} · harness "
                           f"{_e(ins.harness_version_first)} → {_e(ins.harness_version_last)}</p>"
                           f"<div class=panel><b>Expected:</b> {_e(ins.expected)}</div>"
                           f"<h2>Proposals</h2><table><tr><th>Proposal</th><th>Status</th><th>Kind</th><th>Target</th>"
                           f"</tr>{prow or none}</table>"
                           f"<h2>History</h2><table><tr><th>Run</th><th class=num>Count</th><th>Status after</th>"
                           f"<th>When</th></tr>{hist}</table>"
                           f"<h2>Findings</h2><table><tr><th>Conversation</th><th>Source</th><th>Actual</th>"
                           f"<th>Verifier</th></tr>{frows}</table>", ident, "insights", team)

    def conversation_page(self, cid: str, ident: Identity | None = None, team: str | None = None) -> str | None:
        conv = self.store.get_conversation(cid)
        if conv is None or not self._team_ok(ident, conv.team):
            return None
        fs = self.store.findings(conversation_id=cid)
        flagged = {e.turn for f in fs if f.verified for e in f.evidence}
        turns = []
        for t in conv.turns:
            cls = f"turn {t.role}{' err' if t.is_error else ''}{' flag' if t.index in flagged else ''}"
            head = f"#{t.index} {t.role}" + (f" → {t.tool_name}" if t.tool_name else "")
            body = _e(t.content) if t.content else (f"<code>{_e(json.dumps(t.tool_args))}</code>"
                                                     if t.tool_args is not None else "")
            turns.append(f"<div class='{cls}'><div class='muted small'>{_e(head)}</div>{body}</div>")
        frows = "".join(f"<tr><td>{'✔' if f.verified else '✘'}</td><td>{_e(f.source)}</td><td>{_e(f.rubric_id)}</td>"
                        f"<td>{_e(f.actual)}</td><td class='muted small'>{_e(f.verifier_note)}</td></tr>" for f in fs)
        return self.layout(cid, f"<h1>Conversation {_e(cid)}</h1><p class=sub>agent {_e(conv.agent_name)} · team "
                           f"{_e(conv.team or '-')} · {len(conv.turns)} turns · tokens {conv.input_tokens}/"
                           f"{conv.output_tokens}</p><h2>Findings</h2><table><tr><th>Kept</th><th>Source</th>"
                           f"<th>Rule</th><th>Actual</th><th>Verifier</th></tr>{frows}</table>"
                           f"<h2>Transcript</h2>{''.join(turns)}", ident, "insights", team)

    def runs_page(self, ident: Identity | None = None, team: str | None = None) -> str:
        runs = self._filter(ident, self.store.list_runs(team=team), lambda r: r.team)
        rows = "".join(f"<tr><td>{_e(r.run_id)}<div class='muted small'>{_e(r.agent_name)}</div></td>"
                       f"<td class=small>{_ts(r.started_at)}</td><td><code>{_e(r.harness_version)}</code></td>"
                       f"<td class=num>{r.candidates}</td><td class=num>{r.sampled}</td><td class=num>{r.evaluated}</td>"
                       f"<td class=num>{r.findings}</td><td class=num>{r.rejected}</td>"
                       f"<td class=num>{r.input_tokens + r.output_tokens:,}/{r.budget_tokens or '∞'}</td>"
                       f"<td>{_e(r.status)}</td></tr>" for r in runs)
        return self.layout("Runs", "<h1>Evaluation runs</h1><p class=sub>Each run grades new conversations with "
                           "deterministic checks and a sampled LLM judge.</p><table><tr><th>Run</th><th>Started</th>"
                           "<th>Harness</th><th class=num>Candidates</th><th class=num>Sampled</th>"
                           "<th class=num>Judged</th><th class=num>Findings</th><th class=num>Rejected</th>"
                           f"<th class=num>Tokens/budget</th><th>Status</th></tr>{rows}</table>", ident, "runs", team)

    def proposals_page(self, ident: Identity | None = None, team: str | None = None) -> str:
        props = self._filter(ident, self.store.proposals(team=team), lambda p: p.get("team"))
        rows = "".join(
            f"<tr><td><a href='/proposals/{_e(p['proposal_id'])}'>{_e(p['proposal_id'])}</a>"
            f"<div class='muted small'>{_ts(p.get('created_at'))}</div></td>"
            f"<td><a href='/insights/{_e(p['insight_id'])}'>{_e(p['insight_id'])}</a></td><td>{_pill(p['status'])}</td>"
            f"<td>{_pill(p.get('kind') or 'prompt', 'kind')}</td><td><code>{_e(p['file'])}:{p['start_line']}</code></td>"
            f"<td class=num>{_e((p.get('replay') or {}).get('fixed', '-'))}</td>"
            f"<td>{_pill((p.get('shadow') or {}).get('verdict')) if p.get('shadow') else '<span class=muted>-</span>'}"
            f"</td><td>{_e(p.get('approved_by') or '')}</td></tr>" for p in props)
        hint = ("Approve or reject on each proposal's page." if self.auth_on else
                "Sign-in is off, so this dashboard is read-only: record decisions with "
                "<code>ambientq approve &lt;id&gt; --by &lt;name&gt;</code>.")
        return self.layout("Proposals", f"<h1>Proposals</h1><p class=sub>Harness changes ambientq proposes. They are "
                           f"never applied by ambientq: a person approves, then merges through the normal change "
                           f"process. {hint}</p><table><tr><th>Proposal</th><th>Insight</th><th>Status</th><th>Kind</th>"
                           f"<th>Target</th><th class=num>Fixed in replay</th><th>Shadow</th><th>Decided by</th></tr>"
                           f"{rows or '<tr><td colspan=8 class=muted>No proposals yet.</td></tr>'}</table>",
                           ident, "proposals", team)

    def proposal_page(self, pid: str, ident: Identity | None = None, team: str | None = None,
                      flash: str = "", flash_kind: str = "") -> str | None:
        p = self.store.get_proposal(pid)
        if p is None or not self._team_ok(ident, p.get("team")):
            return None
        ins = self.store.get_insight(p["insight_id"])
        rep = p.get("replay") or {}
        sh = p.get("shadow") or {}
        replay_html = "<p class=muted>Not replayed.</p>"
        if rep:
            stats = "".join(f"<div><span class='muted small'>{k}</span><b>{_e(v)}</b></div>" for k, v in [
                ("Fixed", rep.get("fixed")), ("Still failing", rep.get("still_failing")),
                ("Regressions", rep.get("regressions")), ("Mode", rep.get("mode", "single")),
                ("Unmatched tool calls", rep.get("unmatched_calls", 0))])
            rrows = "".join(
                f"<tr><td><a href='/conversations/{quote(r['conversation_id'])}'>{_e(r['conversation_id'])}</a>"
                f"<div class='muted small'>{_e(r['kind'])}</div></td>"
                + (f"<td>{'fail' if r.get('before_fail') else 'pass'} → <b>{'fail' if r.get('after_fail') else 'pass'}"
                   f"</b></td>" if r["kind"] == "failing" else
                   f"<td>{r.get('before_new_findings')} → <b>{r.get('after_new_findings')}</b> findings</td>")
                + f"<td class=num>{len(r.get('unmatched_calls') or [])}</td></tr>" for r in rep.get("rows", []))
            replay_html = (f"<div class=stat>{stats}</div><h3>Per conversation</h3><table><tr><th>Conversation</th>"
                           f"<th>Before → after</th><th class=num>Flagged calls</th></tr>{rrows}</table>")
        shadow_html = "<p class=muted>Not shadow-tested yet (<code>ambientq shadow " + _e(p["proposal_id"]) + \
                      "</code>).</p>"
        if sh:
            shadow_html = (f"<div class=stat><div><span class='muted small'>Verdict</span><b>{_pill(sh.get('verdict'))}"
                           f"</b></div>" + "".join(
                               f"<div><span class='muted small'>{k}</span><b>{_e(v)}</b></div>" for k, v in [
                                   ("New conversations", sh.get("conversations")),
                                   ("Findings live → patched", f"{sh.get('live_findings')} → {sh.get('patched_findings')}"),
                                   ("Wins / losses", f"{sh.get('wins')} / {sh.get('losses')}")])
                           + f"</div><p class='muted small'>Ran {_ts(sh.get('ran_at'))} on conversations that arrived "
                             f"after the proposal.</p>")
        replies = "".join(
            f"<tr><td class=small><a href='/conversations/{quote(r['conversation_id'])}'>{_e(r['conversation_id'])}"
            f"</a></td><td><div class=reply>{_e(r.get('before_reply', ''))}</div></td>"
            f"<td><div class=reply>{_e(r.get('after_reply', ''))}</div></td></tr>"
            for r in rep.get("rows", []) if r.get("after_reply") is not None)
        decision = self._decision_panel(p, ident)
        pr = f" · <a href='{_e(p['pr_url'])}'>pull request</a>" if p.get("pr_url") else ""
        body = (f"<h1>Proposal {_e(p['proposal_id'])}</h1><p class=sub>{_pill(p['status'])} "
                f"{_pill(p.get('kind') or 'prompt', 'kind')} · insight <a href='/insights/{_e(p['insight_id'])}'>"
                f"{_e(ins.title if ins else p['insight_id'])}</a> · team {_e(p.get('team') or '-')} · created "
                f"{_ts(p.get('created_at'))}{' · branch <code>' + _e(p['branch']) + '</code>' if p.get('branch') else ''}"
                f"{pr}</p>"
                f"{decision}<div class=grid2><div><h2>Change</h2><p class=muted><code>{_e(p['file'])}:"
                f"{p['start_line']}-{p['end_line']}</code></p>{render_diff(p['diff'])}<h3>Rationale</h3>"
                f"<div class=panel>{_e(p['rationale'])}</div></div>"
                f"<div><h2>Replay</h2><div class=panel>{replay_html}</div><h2>Shadow test</h2>"
                f"<div class=panel>{shadow_html}</div></div></div>"
                + (f"<h2>Replies before and after</h2><table><tr><th>Conversation</th><th>Original harness</th>"
                   f"<th>Patched harness</th></tr>{replies}</table>" if replies else ""))
        return self.layout(f"Proposal {p['proposal_id']}", body, ident, "proposals", team, flash, flash_kind)

    def _decision_panel(self, p: dict[str, Any], ident: Identity | None) -> str:
        decided = p["status"] in (ProposalStatus.APPROVED, ProposalStatus.REJECTED)
        if decided:
            note = f" — “{_e(p['decision_note'])}”" if p.get("decision_note") else ""
            return (f"<div class=panel><b>{_e(p['status'].title())}</b> by {_e(p.get('approved_by'))} on "
                    f"{_ts(p.get('approved_at'))}{note}</div>")
        if not self.auth_on:
            return ("<div class=panel>Read-only dashboard (sign-in is off). Record a decision with "
                    f"<code>ambientq approve {_e(p['proposal_id'])} --by &lt;name&gt; [--reject]</code>.</div>")
        if not ident or not ident.can("approver", p.get("team")):
            return "<div class='panel muted'>You can view this proposal; approvers decide on it.</div>"
        warn = ("<p class=high>Replay did not show an improvement. Approve only if you have other evidence.</p>"
                if p["status"] == ProposalStatus.NOT_IMPROVED else "")
        sh = (p.get("shadow") or {}).get("verdict")
        if sh == "worse":
            warn += "<p class=high>The shadow test found it worse than the live harness on new traffic.</p>"
        return (f"<form class='panel decide' method=post action='/proposals/{_e(p['proposal_id'])}/decision'>"
                f"<b>Your decision</b> <span class='muted small'>(recorded in the audit log as {_e(ident.name)}; "
                f"nothing is merged or deployed)</span>{warn}"
                f"<input type=hidden name=csrf value='{_e(ident.csrf)}'>"
                f"<textarea name=note maxlength=2000 placeholder='Note (optional): why, ticket number, conditions'>"
                f"</textarea><button class=ok name=decision value=approve>Approve</button> "
                f"<button class=bad name=decision value=reject>Reject</button></form>")

    def audit_page(self, ident: Identity | None = None, team: str | None = None, action: str | None = None) -> str:
        rows_data = self.store.audit_log(500)
        actions = sorted({a["action"] for a in rows_data})
        if action:
            rows_data = [a for a in rows_data if a["action"] == action]
        chips = "<a href='/audit' class='" + ("" if action else "on") + "'>All</a>" + "".join(
            f"<a href='/audit?action={quote(a)}' class='{'on' if a == action else ''}'>{_e(a)}</a>" for a in actions)
        rows = "".join(f"<tr><td class=small>{_ts(a['ts'])}</td><td>{_e(a['actor'])}</td><td>{_e(a['action'])}</td>"
                       f"<td>{_e(a['subject'])}</td><td class='muted small'>{_e(a['detail'])[:240]}</td></tr>"
                       for a in rows_data)
        return self.layout("Audit", "<h1>Audit log</h1><p class=sub>Every LLM call (no prompt content), proposal, "
                           "decision and sign-in. Export for your SIEM with <code>ambientq audit-export</code>.</p>"
                           f"<div class=chips>{chips}</div><table><tr><th>When</th><th>Actor</th><th>Action</th>"
                           f"<th>Subject</th><th>Detail</th></tr>{rows}</table>", ident, "audit", team)

    def usage_page(self, ident: Identity | None = None, team: str | None = None, since: str = "30d") -> str:
        from ambientq.cost import Pricing, format_cost, summarize

        days = {"7d": 7, "30d": 30, "90d": 90}.get(since, 30)
        pricing = Pricing.from_config(self.cfg) if self.cfg is not None else Pricing()
        rows = self._filter(ident, self.store.usage_rows(since=time.time() - days * 86400, team=team),
                            lambda r: r.get("team"))
        tq = f"&team={quote(team)}" if team else ""
        chips = "".join(f"<a href='/usage?since={s}{tq}' class='{'on' if s == since else ''}'>Last {s}</a>"
                        for s in ("7d", "30d", "90d"))
        tables = []
        for by, label in (("source", "Production agents vs ambientq"), ("agent", "By agent"), ("model", "By model"),
                          ("team", "By team")):
            data = summarize(rows, pricing, by)
            trs = "".join(f"<tr><td>{_e(r['key'] or '-')}</td><td class=num>{r['calls']:,}</td>"
                          f"<td class=num>{r['input_tokens']:,}</td><td class=num>{r['output_tokens']:,}</td>"
                          f"<td class=num>{_e(format_cost(r['cost'], pricing.currency))}</td></tr>"
                          for r in data["rows"])
            tot = data["totals"]
            tables.append(f"<h2>{label}</h2><table><tr><th>{by.title()}</th><th class=num>Calls</th>"
                          f"<th class=num>Input tokens</th><th class=num>Output tokens</th><th class=num>Cost</th></tr>"
                          f"{trs}<tr><td><b>Total</b></td><td class=num><b>{tot['calls']:,}</b></td>"
                          f"<td class=num><b>{tot['input_tokens']:,}</b></td><td class=num><b>{tot['output_tokens']:,}"
                          f"</b></td><td class=num><b>{_e(format_cost(tot['cost'], pricing.currency))}</b></td></tr>"
                          f"</table>")
        return self.layout("Usage & cost", f"<h1>Usage &amp; cost</h1><p class=sub>Tokens from production agent "
                           f"traffic and from ambientq's own judge, verifier, fixer and replay calls, priced with "
                           f"<code>pricing</code> ({_e(pricing.currency)} per 1k tokens).</p>"
                           f"<div class=chips>{chips}</div>{''.join(tables)}", ident, "usage", team)

    def reports_page(self, ident: Identity | None = None, team: str | None = None) -> str:
        from ambientq.report import report_months

        tq = f"?team={quote(team)}" if team else ""
        rows = "".join(f"<tr><td><b>{_e(m)}</b></td><td><a href='/reports/{_e(m)}{tq}'>HTML</a> · "
                       f"<a href='/reports/{_e(m)}{tq}{'&' if tq else '?'}format=json'>JSON</a></td></tr>"
                       for m in report_months(self.store))
        return self.layout("Reports", "<h1>Compliance reports</h1><p class=sub>Monthly: conversations graded, "
                           "insights found and resolved, time to fix, proposals and who decided them, model usage "
                           "and cost. Print the HTML to PDF, or run <code>ambientq report --month YYYY-MM</code>.</p>"
                           f"<table><tr><th>Month</th><th>Download</th></tr>"
                           f"{rows or '<tr><td colspan=2 class=muted>No data yet.</td></tr>'}</table>",
                           ident, "reports", team)

    def report(self, month: str, team: str | None, fmt: str) -> Response | None:
        import re

        from ambientq.report import build_report, render_html

        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return None
        rep = build_report(self.store, self.cfg, month, team=team)
        if fmt == "json":
            return Response(200, json.dumps(rep, default=str, indent=1), "application/json")
        return Response(200, render_html(rep))

    def login_page(self, error: str = "") -> str:
        msg = f"<div class='flash error'>{_e(error)}</div>" if error else ""
        how = ("<p><a href='/login'><button class=plain>Sign in with your identity provider</button></a></p>"
               if self.auth and self.auth.mode == "oidc" else
               "<p>Sign-in is handled by the proxy in front of ambientq; your request carried no trusted identity.</p>")
        return self.layout("Sign in", f"<h1>Sign in</h1>{msg}<p class=sub>ambientq needs to know who you are to "
                           f"show this dashboard.</p>{how}", None)

    # -- API --------------------------------------------------------------------
    def api(self, path: str, ident: Identity | None = None) -> Any:
        parts = path.strip("/").split("/")[1:]
        if parts == ["insights"]:
            return [asdict(i) for i in self._filter(ident, self.store.insights(), lambda i: i.team)]
        if len(parts) == 2 and parts[0] == "insights":
            ins = self.store.get_insight(parts[1])
            if not ins or not self._team_ok(ident, ins.team):
                return None
            return {**asdict(ins), "occurrences": self.store.occurrences(ins.insight_id),
                    "findings": [asdict(f) for f in self.store.findings(insight_id=ins.insight_id)],
                    "proposals": self.store.proposals(ins.insight_id)}
        if parts == ["runs"]:
            return [asdict(r) for r in self._filter(ident, self.store.list_runs(), lambda r: r.team)]
        if len(parts) == 2 and parts[0] == "conversations":
            conv = self.store.get_conversation(parts[1])
            return asdict(conv) if conv and self._team_ok(ident, conv.team) else None
        if parts == ["proposals"]:
            return self._filter(ident, self.store.proposals(), lambda p: p.get("team"))
        if len(parts) == 2 and parts[0] == "proposals":
            p = self.store.get_proposal(parts[1])
            return p if p and self._team_ok(ident, p.get("team")) else None
        if parts == ["stats"]:
            return self.store.counts()
        if parts == ["usage"]:
            from ambientq.cost import Pricing, summarize
            pricing = Pricing.from_config(self.cfg) if self.cfg is not None else Pricing()
            rows = self._filter(ident, self.store.usage_rows(since=time.time() - 30 * 86400), lambda r: r.get("team"))
            return {by: summarize(rows, pricing, by) for by in ("source", "agent", "model", "team")}
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

    # -- request handling ---------------------------------------------------------
    def identify(self, req: Request) -> Identity | None:
        if not self.auth_on:
            return None
        assert self.auth is not None
        try:
            return self.auth.identity(req.headers, req.client_ip)
        except AuthError as exc:
            self.store.audit("anonymous", "sign_in_failed", req.client_ip, {"reason": str(exc), "path": req.path})
            return None

    def handle(self, req: Request) -> Response:
        path = req.path.rstrip("/") or "/"
        if path == "/healthz":
            return Response(200, json.dumps({"ok": True}), "application/json")
        if self.auth_on and path in ("/login", "/auth/callback", "/logout"):
            return self._auth_route(path, req)
        ident = self.identify(req)
        if self.auth_on and (ident is None or ident.level() == 0 and not ident.visible_teams()):
            if path.startswith("/api/"):
                return Response(401 if ident is None else 403, json.dumps({"error": "sign in required"}),
                                "application/json")
            if ident is None and self.auth and self.auth.mode == "oidc":
                return redirect("/login?" + urlencode({"next": req.path}))
            if ident is None:
                return Response(401, self.login_page())
            self.store.audit(ident.name, "access_denied", path, {"reason": "no role"})
            return Response(403, self.layout("Forbidden", "<h1>No access</h1><p>Your account has no ambientq role. "
                                             "Ask an admin to add you to <code>auth.roles</code>.</p>", ident))
        if req.method == "POST":
            return self._post(path, req, ident)
        team = req.query.get("team") or None
        if team and not self._team_ok(ident, team):
            team = None
        if path.startswith("/api/"):
            data = self.api(path, ident)
            return Response(200 if data is not None else 404,
                            json.dumps(data if data is not None else {"error": "not found"}, default=str),
                            "application/json")
        out: str | Response | None
        if path == "/":
            out = self.insights_page(req.query.get("status"), ident, team)
        elif path.startswith("/insights/"):
            out = self.insight_page(path.split("/", 2)[2], ident, team)
        elif path.startswith("/conversations/"):
            out = self.conversation_page(path.split("/", 2)[2], ident, team)
        elif path == "/runs":
            out = self.runs_page(ident, team)
        elif path == "/proposals":
            out = self.proposals_page(ident, team)
        elif path.startswith("/proposals/") and path.count("/") == 2:
            out = self.proposal_page(path.split("/", 2)[2], ident, team, *self._flash(req))
        elif path == "/usage":
            out = self.usage_page(ident, team, req.query.get("since", "30d"))
        elif path == "/reports":
            out = self.reports_page(ident, team)
        elif path.startswith("/reports/") and path.count("/") == 2:
            out = self.report(path.split("/", 2)[2], team, req.query.get("format", "html"))
        elif path == "/audit":
            if self.auth_on and not (ident and ident.can("admin")):
                return Response(403, self.layout("Forbidden", "<h1>Admins only</h1>", ident))
            out = self.audit_page(ident, team, req.query.get("action"))
        else:
            out = None
        if out is None:
            return Response(404, self.layout("Not found", "<h1>Not found</h1>", ident))
        return out if isinstance(out, Response) else Response(200, out)

    @staticmethod
    def _flash(req: Request) -> tuple[str, str]:
        msg = {"approved": ("Approved and recorded in the audit log.", ""),
               "rejected": ("Rejected and recorded in the audit log.", ""),
               "csrf": ("The form expired. Reload the page and try again.", "error"),
               "forbidden": ("You are not an approver for this team.", "error"),
               "decided": ("This proposal was already decided.", "error")}
        return msg.get(req.query.get("m", ""), ("", ""))

    def _post(self, path: str, req: Request, ident: Identity | None) -> Response:
        parts = path.strip("/").split("/")
        if not (self.auth_on and len(parts) == 3 and parts[0] == "proposals" and parts[2] == "decision"):
            return Response(404 if not self.auth_on else 405, json.dumps({"error": "not found"}), "application/json")
        assert self.auth is not None
        pid = parts[1]
        back = f"/proposals/{quote(pid)}"
        if not same_origin(req.headers, req.host_url, self.auth.public_url) or not check_csrf(
                ident, req.form.get("csrf", "")):
            self.store.audit(ident.name if ident else "anonymous", "decision_refused", pid, {"reason": "csrf"})
            return Response(403, self.layout("Forbidden", "<h1>Request refused</h1><p>The form expired or came from "
                                             f"another site. <a href='{back}'>Back to the proposal</a></p>", ident))
        p = self.store.get_proposal(pid)
        if p is None:
            return Response(404, self.layout("Not found", "<h1>Not found</h1>", ident))
        assert ident is not None
        if not ident.can("approver", p.get("team")):
            self.store.audit(ident.name, "decision_refused", p["proposal_id"], {"reason": "not an approver",
                                                                                "team": p.get("team")})
            return redirect(back + "?m=forbidden")
        choice = req.form.get("decision")
        if choice not in ("approve", "reject"):
            return redirect(back)
        from ambientq.propose import decide

        try:
            decide(self.store, p["proposal_id"], ident.name, approve=choice == "approve",
                   note=req.form.get("note", "")[:2000], via="dashboard")
        except RuntimeError:
            return redirect(back + "?m=decided")
        return redirect(back + ("?m=approved" if choice == "approve" else "?m=rejected"))

    def _auth_route(self, path: str, req: Request) -> Response:
        assert self.auth is not None
        a = self.auth
        if path == "/logout":
            ident = self.identify(req)
            if ident:
                self.store.audit(ident.name, "sign_out", "", {})
            return redirect("/", a.cookie(SESSION_COOKIE, "", 0))
        if a.mode != "oidc":
            return redirect("/")
        if path == "/login":
            try:
                url, pending = a.begin(req.host_url, req.query.get("next", "/"))
            except AuthError as exc:
                return Response(502, self.login_page(f"The identity provider is unreachable: {exc}"))
            return redirect(url, a.cookie(PENDING_COOKIE, pending, 600))
        try:
            ident, session, nxt = a.finish(req.host_url, req.query,
                                           parse_cookies(req.headers.get("Cookie")).get(PENDING_COOKIE, ""))
        except AuthError as exc:
            self.store.audit("anonymous", "sign_in_failed", req.client_ip, {"reason": str(exc)})
            return Response(401, self.login_page(f"Sign-in failed: {exc}"))
        self.store.audit(ident.name, "sign_in", ident.user, {"role": ident.role(), "groups": ident.groups[:20]})
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/",
                        a.cookie(SESSION_COOKIE, session, a.ttl), a.cookie(PENDING_COOKIE, "", 0))


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ambientq"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8",
                  headers: list[tuple[str, str]] | None = None) -> None:
            raw = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'")
            for k, v in headers or []:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str), "application/json")

        def _request(self, method: str, form: dict[str, str] | None = None) -> Request:
            u = urlparse(self.path)
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
            proto = self.headers.get("X-Forwarded-Proto") or "http"
            return Request(method=method, path=u.path, query={k: v[0] for k, v in parse_qs(u.query).items()},
                           headers=self.headers, client_ip=self.client_address[0], form=form or {},
                           host_url=f"{proto}://{host}")

        def _respond(self, req: Request) -> None:
            try:
                r = app.handle(req)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("request failed")
                r = Response(500, app.layout("Error", f"<h1>Error</h1><pre>{_e(exc)}</pre>", None))
            self._send(r.code, r.body, r.ctype, r.headers)

        def do_GET(self) -> None:  # noqa: N802
            self._respond(self._request("GET"))

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path.rstrip("/")
            n = int(self.headers.get("Content-Length", 0) or 0)
            if route in ("/v1/traces", "/v1/gateway/logs"):
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
            if n > MAX_FORM:
                return self._json(413, {"error": "form too large"})
            raw = self.rfile.read(n).decode("utf-8", errors="replace")
            form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
            self._respond(self._request("POST", form))

    return ThreadingHTTPServer((host, port), Handler)


def serve_background(app: App, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    srv = make_server(app, host, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
