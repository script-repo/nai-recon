"""Monthly compliance report (SPEC §11.6): one dict with everything, rendered as JSON or HTML.

Months are calendar months in UTC. With ``team`` set, every section is scoped to
that team except the audit event counts, which are install-wide (audit rows
carry no team).
"""

from __future__ import annotations

import html
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from ambientq import __version__
from ambientq.cost import Pricing, format_cost, summarize
from ambientq.models import SEVERITIES
from ambientq.store import SQLStore

DECISIONS = ("approved", "rejected")


# -- periods -----------------------------------------------------------------------
def month_bounds(month: str) -> tuple[float, float]:
    """``"2026-10"`` -> (start, end) epoch seconds, UTC, end exclusive."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", month or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"--month must look like 2026-10, got {month!r}")
    y, mo = int(m.group(1)), int(m.group(2))
    start = datetime(y, mo, 1, tzinfo=UTC)
    end = datetime(y + mo // 12, mo % 12 + 1, 1, tzinfo=UTC)
    return start.timestamp(), end.timestamp()


def month_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _next_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


_TS_SOURCES = [("conversations", "started_ns", 1e-9), ("runs", "started_at", 1.0), ("usage", "ts", 1.0),
               ("audit", "ts", 1.0), ("insights", "created_at", 1.0), ("proposals", "created_at", 1.0)]


def _scaled(ts: float, scale: float) -> float | int:
    return ts if scale == 1.0 else int(round(ts / scale))


def report_months(store: SQLStore) -> list[str]:
    """Months (``YYYY-MM``, newest first) that have any conversations, runs, usage, audit or insights."""
    lo, hi = None, None
    for table, col, scale in _TS_SOURCES:
        r = store.query(f"SELECT MIN({col}) AS lo, MAX({col}) AS hi FROM {table} WHERE {col} > 0")[0]
        if r["lo"] is not None:
            lo = min(lo, float(r["lo"]) * scale) if lo is not None else float(r["lo"]) * scale
            hi = max(hi, float(r["hi"]) * scale) if hi is not None else float(r["hi"]) * scale
    if lo is None or hi is None:
        return []
    out, month, last = [], month_of(lo), month_of(hi)
    while month <= last:
        start, end = month_bounds(month)
        if any(store.query(f"SELECT 1 AS x FROM {t} WHERE {c} >= ? AND {c} < ? LIMIT 1",
                           (_scaled(start, s), _scaled(end, s))) for t, c, s in _TS_SOURCES):
            out.append(month)
        month = _next_month(month)
    return out[::-1]


# -- stats -------------------------------------------------------------------------
def duration_stats(values: list[float]) -> dict[str, Any]:
    """count, mean, median and p90 (nearest rank) of durations in seconds."""
    if not values:
        return {"count": 0, "mean_s": None, "median_s": None, "p90_s": None}
    v = sorted(values)
    p90 = v[max(0, -(-len(v) * 9 // 10) - 1)]
    return {"count": len(v), "mean_s": round(statistics.fmean(v), 1), "median_s": round(statistics.median(v), 1),
            "p90_s": round(p90, 1)}


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    s = float(seconds)
    if s < 60:
        return "< 1 min"
    if s < 3600:
        return f"{s / 60:.0f} min"
    if s < 86400 * 2:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


# -- sections ----------------------------------------------------------------------
def _team_sql(team: str | None, col: str = "team") -> tuple[str, list[Any]]:
    return (f" AND {col} = ?", [team]) if team else ("", [])


def _conversations(store: SQLStore, start: float, end: float, team: str | None) -> dict[str, Any]:
    t_sql, t_par = _team_sql(team)
    rows = store.query(
        "SELECT agent_name, team, COUNT(*) AS n FROM conversations WHERE started_ns >= ? AND started_ns < ?"
        f"{t_sql} GROUP BY agent_name, team ORDER BY n DESC", [int(start * 1e9), int(end * 1e9), *t_par])
    by_agent: list[dict[str, Any]] = [{"agent": r["agent_name"] or "", "team": r["team"] or "", "count": int(r["n"])} for r in rows]
    by_team: Counter[str] = Counter()
    for r in by_agent:
        by_team[r["team"]] += r["count"]
    return {"total": sum(r["count"] for r in by_agent), "by_agent": by_agent,
            "by_team": [{"team": k, "count": v} for k, v in by_team.most_common()]}


def _grading(store: SQLStore, start: float, end: float, team: str | None) -> dict[str, Any]:
    t_sql, t_par = _team_sql(team)
    runs = store.query(f"SELECT * FROM runs WHERE started_at >= ? AND started_at < ?{t_sql} ORDER BY started_at",
                       [start, end, *t_par])

    def total(col: str) -> int:
        return sum(int(r.get(col) or 0) for r in runs)

    return {"runs": len(runs), "candidates": total("candidates"), "sampled": total("sampled"),
            "judged": total("evaluated"), "skipped_budget": total("skipped_budget"),
            "findings_kept": total("findings"), "findings_rejected": total("rejected"),
            "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "tokens": total("input_tokens") + total("output_tokens"),
            "by_status": dict(Counter(str(r.get("status") or "") for r in runs))}


def _insight_events(ins: dict[str, Any], occ: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Seen / resolved / regressed timestamps of one insight, from its occurrence history."""
    seen, resolved, regressed = [], [], []
    prev = None
    for o in occ:
        ts = float(o["created_at"] or 0)
        if int(o["count"] or 0) > 0:
            seen.append(ts)
            if prev == "RESOLVED":
                regressed.append(ts)
        if o["status_after"] == "RESOLVED" and int(o["count"] or 0) == 0:
            resolved.append(ts)
        prev = o["status_after"]
    ra = ins.get("resolved_at")
    if ra and not any(abs(t - float(ra)) < 60 for t in resolved):
        resolved.append(float(ra))
    return {"seen": seen, "resolved": sorted(resolved), "regressed": regressed}


def _insights(store: SQLStore, start: float, end: float, team: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    t_sql, t_par = _team_sql(team)
    rows = store.query(f"SELECT * FROM insights WHERE 1=1{t_sql}", t_par)
    occ: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in store.query("SELECT * FROM occurrences ORDER BY created_at"):
        occ[o["insight_id"]].append(o)

    def brief(i: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"insight_id": i["insight_id"], "title": i["title"] or "", "severity": i["severity"] or "",
                "category": i["category"] or "", "agent": i["agent_name"] or "", **extra}

    new, recurring, resolved, regressions, open_end = [], [], [], [], []
    fixes: list[tuple[str, float]] = []
    for i in rows:
        created = float(i["created_at"] or 0)
        ev = _insight_events(i, occ.get(i["insight_id"], []))
        if start <= created < end:
            new.append(brief(i))
        elif created < start and any(start <= t < end for t in ev["seen"]):
            recurring.append(brief(i))
        for t in ev["regressed"]:
            if start <= t < end:
                regressions.append(brief(i, at=iso(t)))
        for t in ev["resolved"]:
            if start <= t < end:
                # An episode starts at first sight, or at the latest regression before this fix.
                began = max([created, *[r for r in ev["regressed"] if r <= t]])
                fixes.append((i["severity"] or "", t - began))
                resolved.append(brief(i, resolved_at=iso(t), time_to_fix_s=round(t - began, 1)))
        if created < end:
            last_res = max((t for t in ev["resolved"] if t < end), default=None)
            last_seen = max((t for t in ev["seen"] if t < end), default=created)
            if last_res is None or last_seen > last_res:
                open_end.append(brief(i))

    def by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        c = Counter(x[key] for x in items)
        order = list(SEVERITIES) if key == "severity" else sorted(c)
        return {k: c[k] for k in order + sorted(set(c) - set(order)) if c[k]}

    ttf_by_sev: dict[str, list[float]] = defaultdict(list)
    for sev, d in fixes:
        ttf_by_sev[sev].append(d)
    ttf = {**duration_stats([d for _, d in fixes]),
           "by_severity": {s: duration_stats(ttf_by_sev[s]) for s in [*SEVERITIES, *sorted(set(ttf_by_sev) - set(
               SEVERITIES))] if ttf_by_sev.get(s)}}
    insights = {
        "new": len(new), "recurring": len(recurring), "resolved": len(resolved), "regressions": len(regressions),
        "open_at_end": len(open_end),
        "new_by_severity": by(new, "severity"), "new_by_category": by(new, "category"),
        "open_by_severity": by(open_end, "severity"), "open_by_category": by(open_end, "category"),
        "new_list": new, "resolved_list": resolved, "regression_list": regressions,
    }
    return insights, ttf


def _proposals(store: SQLStore, start: float, end: float, team: str | None) -> dict[str, Any]:
    rows = [p for p in store.proposals(team=team) if start <= float(p.get("created_at") or 0) < end]
    return {"total": len(rows), "by_status": dict(Counter(str(p["status"]) for p in rows).most_common()),
            "by_kind": dict(Counter(str(p["kind"]) for p in rows).most_common())}


def _decisions(store: SQLStore, start: float, end: float, team: str | None) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM audit WHERE ts >= ? AND ts < ? AND action IN (?, ?) ORDER BY ts",
                       [start, end, *DECISIONS])
    out = []
    for a in rows:
        p = store.get_proposal(a["subject"] or "") if a["subject"] else None
        if team and (p is None or (p.get("team") or "") != team):
            continue
        try:
            detail = json.loads(a["detail"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        ins = store.get_insight(p["insight_id"]) if p and p.get("insight_id") else None
        out.append({"at": iso(a["ts"]), "action": a["action"], "actor": a["actor"] or "",
                    "proposal": a["subject"] or "", "kind": (p or {}).get("kind") or "",
                    "insight": ins.title if ins else str(detail.get("insight") or ""),
                    "note": str(detail.get("note") or "") if isinstance(detail, dict) else ""})
    return out


def _usage(store: SQLStore, start: float, end: float, team: str | None, pricing: Pricing) -> dict[str, Any]:
    rows = store.usage_rows(since=start, until=end, team=team)
    agent = [r for r in rows if r.get("source") == "agent"]
    own = [r for r in rows if r.get("source") == "ambientq"]
    return {
        "currency": pricing.currency,
        "agent": {"by_model": summarize(agent, pricing, "model"), "by_agent": summarize(agent, pricing, "agent")},
        "ambientq": {"by_model": summarize(own, pricing, "model"), "by_agent": summarize(own, pricing, "agent"),
                     "by_purpose": _by_purpose(own, pricing)},
        "totals": summarize(rows, pricing, "source")["totals"],
    }


def _by_purpose(rows: list[dict[str, Any]], pricing: Pricing) -> dict[str, Any]:
    tagged = [{**r, "source": r.get("purpose") or "unknown"} for r in rows]
    s = summarize(tagged, pricing, "source")
    s["by"] = "purpose"
    return s


def _audit_counts(store: SQLStore, start: float, end: float) -> dict[str, Any]:
    rows = store.query("SELECT action, COUNT(*) AS n FROM audit WHERE ts >= ? AND ts < ? GROUP BY action",
                       [start, end])
    by_action = {r["action"]: int(r["n"]) for r in sorted(rows, key=lambda r: (-int(r["n"]), r["action"]))}
    return {"total": sum(by_action.values()), "by_action": by_action}


def build_report(store: SQLStore, cfg: Any, month: str, team: str | None = None) -> dict[str, Any]:
    """Everything SPEC §11.6 lists for one UTC calendar month, as plain JSON-able data."""
    start, end = month_bounds(month)
    pricing = Pricing.from_config(cfg)
    insights, ttf = _insights(store, start, end, team)
    report: dict[str, Any] = {
        "report": "ambientq-monthly", "ambientq_version": __version__,
        "generated_at": iso(datetime.now(UTC).timestamp()),
        "period": {"month": month, "start": iso(start), "end": iso(end), "team": team or None},
        "conversations": _conversations(store, start, end, team),
        "grading": _grading(store, start, end, team),
        "insights": insights,
        "time_to_fix": ttf,
        "proposals": _proposals(store, start, end, team),
        "decisions": _decisions(store, start, end, team),
        "usage": _usage(store, start, end, team, pricing),
        "audit": _audit_counts(store, start, end),
    }
    report["summary"] = {
        "conversations": report["conversations"]["total"], "judged": report["grading"]["judged"],
        "findings_kept": report["grading"]["findings_kept"], "insights_new": insights["new"],
        "insights_resolved": insights["resolved"], "open_at_end": insights["open_at_end"],
        "median_time_to_fix_s": ttf["median_s"], "proposals": report["proposals"]["total"],
        "decisions": len(report["decisions"]), "cost": report["usage"]["totals"]["cost"],
        "currency": pricing.currency,
    }
    return report


def report_basename(month: str, team: str | None = None) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", team) if team else ""
    return f"ambientq-report-{month}" + (f"-{safe}" if safe else "")


# -- HTML --------------------------------------------------------------------------
CSS = """
:root{--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--bg:#f6f8fa;--accent:#0b5cad;--high:#b42318;
--medium:#b54708;--low:#475467}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 Inter,"Segoe UI",Roboto,system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
color:var(--fg);background:#fff}
main{max-width:1040px;margin:0 auto;padding:32px 28px 48px}
header{border-bottom:2px solid var(--fg);padding-bottom:12px;margin-bottom:24px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;margin:32px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--line)}
h3{font-size:14px;margin:18px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.meta{color:var(--muted);margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:0 0 8px}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--bg)}
.card .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.card .k{font-size:12px;color:var(--muted)}
table{border-collapse:collapse;width:100%;margin:4px 0 12px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--bg)}
td.n,th.n{text-align:right}
.pill{display:inline-block;padding:0 7px;border-radius:9px;font-size:12px;border:1px solid currentColor}
.sev-high{color:var(--high)}.sev-medium{color:var(--medium)}.sev-low{color:var(--low)}
.act-approved{color:#067647}.act-rejected{color:var(--high)}
.empty{color:var(--muted);font-style:italic}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:8px}
@media (max-width:720px){.grid2{grid-template-columns:1fr}}
@page{size:A4;margin:16mm 14mm}
@media print{
body{font-size:11px}main{max-width:none;padding:0}nav{display:none}
.card{background:none}th{background:none}
h2,h3{break-after:avoid;page-break-after:avoid}
table,.cards,.card{break-inside:avoid;page-break-inside:avoid}
tr{break-inside:avoid;page-break-inside:avoid}
section.page{break-before:page;page-break-before:always}
a{color:inherit;text-decoration:none}
}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _num(v: Any) -> str:
    return f"{v:,}" if isinstance(v, int) else _e(v)


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None,
           empty: str = "None this month.") -> str:
    """``rows`` cells are already-escaped HTML."""
    if not rows:
        return f"<p class='empty'>{_e(empty)}</p>"
    numeric = numeric or set()
    head = "".join(f"<th{' class=n' if i in numeric else ''}>{_e(h)}</th>" for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f"<td{' class=n' if i in numeric else ''}>{c}</td>"
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _kv(d: dict[str, Any], key_label: str, empty: str = "None this month.") -> str:
    return _table([key_label, "Count"], [[_e(k), _num(v)] for k, v in d.items()], {1}, empty)


def _sev(s: str) -> str:
    return f"<span class='pill sev-{_e(s)}'>{_e(s)}</span>"


def _usage_table(s: dict[str, Any], label: str) -> str:
    cur = s["currency"]
    rows = [[_e(r["key"] or "–"), _num(r["calls"]), _num(r["input_tokens"]), _num(r["output_tokens"]),
             _e(format_cost(r["cost"], cur))] for r in s["rows"]]
    if rows:
        t = s["totals"]
        rows.append(["<b>Total</b>", _num(t["calls"]), _num(t["input_tokens"]), _num(t["output_tokens"]),
                     f"<b>{_e(format_cost(t['cost'], cur))}</b>"])
    return _table([label, "Calls", "Input tokens", "Output tokens", "Cost"], rows, {1, 2, 3, 4},
                  "No usage recorded.")


def render_html(report: dict[str, Any]) -> str:
    """A self-contained, print-friendly page (no JavaScript); every value is HTML-escaped."""
    per, sm, g, ins = report["period"], report["summary"], report["grading"], report["insights"]
    ttf, props, us = report["time_to_fix"], report["proposals"], report["usage"]
    month_name = datetime.strptime(per["month"], "%Y-%m").strftime("%B %Y")
    title = f"ambientq quality report · {month_name}" + (f" · team {per['team']}" if per.get("team") else "")
    cards = [("Conversations", _num(sm["conversations"])), ("Judged", _num(sm["judged"])),
             ("Findings kept", _num(sm["findings_kept"])), ("New insights", _num(sm["insights_new"])),
             ("Resolved", _num(sm["insights_resolved"])), ("Open at month end", _num(sm["open_at_end"])),
             ("Median time to fix", _e(human_duration(sm["median_time_to_fix_s"]))),
             ("Decisions", _num(sm["decisions"])), ("Model cost", _e(format_cost(sm["cost"], sm["currency"])))]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>",
        f"<header><h1>{_e(title)}</h1><p class='meta'>Period {_e(per['start'])} – {_e(per['end'])} (UTC, end "
        f"exclusive) · generated {_e(report['generated_at'])} · ambientq {_e(report['ambientq_version'])}</p>"
        "</header>",
        "<section><div class='cards'>" + "".join(
            f"<div class='card'><div class='v'>{v}</div><div class='k'>{_e(k)}</div></div>" for k, v in cards)
        + "</div></section>",
    ]
    conv = report["conversations"]
    parts.append("<section><h2>Conversations ingested</h2><div class='grid2'><div><h3>By agent</h3>" + _table(
        ["Agent", "Team", "Conversations"],
        [[_e(r["agent"] or "–"), _e(r["team"] or "–"), _num(r["count"])] for r in conv["by_agent"]], {2})
        + "</div><div><h3>By team</h3>" + _table(
        ["Team", "Conversations"], [[_e(r["team"] or "–"), _num(r["count"])] for r in conv["by_team"]], {1})
        + "</div></div></section>")
    parts.append("<section><h2>Grading</h2>" + _table(
        ["Runs", "Candidates", "Sampled", "Judged", "Skipped (budget)", "Findings kept", "Findings rejected",
         "Judge tokens"],
        [[_num(g[k]) for k in ("runs", "candidates", "sampled", "judged", "skipped_budget", "findings_kept",
                               "findings_rejected", "tokens")]], set(range(8))) + "</section>")
    parts.append(
        "<section><h2>Insights</h2>" + _table(
            ["New", "Recurring", "Resolved", "Regressions", "Open at month end"],
            [[_num(ins[k]) for k in ("new", "recurring", "resolved", "regressions", "open_at_end")]], set(range(5)))
        + "<div class='grid2'><div><h3>New by severity</h3>" + _kv(ins["new_by_severity"], "Severity")
        + "<h3>New by category</h3>" + _kv(ins["new_by_category"], "Category")
        + "</div><div><h3>Open at month end by severity</h3>" + _kv(ins["open_by_severity"], "Severity", "None open.")
        + "<h3>Open at month end by category</h3>" + _kv(ins["open_by_category"], "Category", "None open.")
        + "</div></div><h3>New this month</h3>" + _table(
            ["Insight", "Severity", "Category", "Agent"],
            [[_e(i["title"]), _sev(i["severity"]), _e(i["category"]), _e(i["agent"])] for i in ins["new_list"]])
        + "<h3>Resolved this month</h3>" + _table(
            ["Insight", "Severity", "Resolved", "Time to fix"],
            [[_e(i["title"]), _sev(i["severity"]), _e(i["resolved_at"]), _e(human_duration(i["time_to_fix_s"]))]
             for i in ins["resolved_list"]], {3})
        + "<h3>Regressions</h3>" + _table(
            ["Insight", "Severity", "Recurred"],
            [[_e(i["title"]), _sev(i["severity"]), _e(i["at"])] for i in ins["regression_list"]])
        + "</section>")
    ttf_rows = [["<b>All</b>", _num(ttf["count"]), *(_e(human_duration(ttf[k])) for k in
                                                    ("mean_s", "median_s", "p90_s"))]] if ttf["count"] else []
    ttf_rows += [[_sev(s), _num(v["count"]), *(_e(human_duration(v[k])) for k in ("mean_s", "median_s", "p90_s"))]
                 for s, v in ttf["by_severity"].items()]
    parts.append("<section><h2>Time to fix</h2><p class='meta'>From first seen (or the latest regression) to "
                 "resolved, for insights resolved this month.</p>" + _table(
                     ["Severity", "Fixed", "Mean", "Median", "p90"], ttf_rows, {1, 2, 3, 4},
                     "No insights resolved this month.") + "</section>")
    parts.append("<section class='page'><h2>Proposals and decisions</h2><div class='grid2'><div><h3>By status"
                 "</h3>" + _kv(props["by_status"], "Status") + "</div><div><h3>By kind</h3>"
                 + _kv(props["by_kind"], "Kind") + "</div></div><h3>Every decision</h3>" + _table(
                     ["When (UTC)", "Decision", "By", "Proposal", "Kind", "Insight", "Note"],
                     [[_e(d["at"]), f"<span class='pill act-{_e(d['action'])}'>{_e(d['action'])}</span>",
                       _e(d["actor"]), _e(d["proposal"]), _e(d["kind"]), _e(d["insight"]), _e(d["note"])]
                      for d in report["decisions"]], empty="No decisions this month.") + "</section>")
    parts.append(
        f"<section><h2>Model usage and cost</h2><p class='meta'>Prices per 1k tokens in {_e(us['currency'])} "
        "from <code>pricing</code> in the config.</p>"
        "<h3>Agent traffic by model</h3>" + _usage_table(us["agent"]["by_model"], "Model")
        + "<h3>Agent traffic by agent</h3>" + _usage_table(us["agent"]["by_agent"], "Agent")
        + "<h3>ambientq's own calls by model</h3>" + _usage_table(us["ambientq"]["by_model"], "Model")
        + "<h3>ambientq's own calls by purpose</h3>" + _usage_table(us["ambientq"]["by_purpose"], "Purpose")
        + "<h3>ambientq's own calls by agent graded</h3>" + _usage_table(us["ambientq"]["by_agent"], "Agent")
        + "</section>")
    parts.append("<section><h2>Audit events</h2><p class='meta'>Install-wide counts by action.</p>"
                 + _kv(report["audit"]["by_action"], "Action", "No audit events this month.") + "</section>")
    parts.append(f"<footer>{_e(title)} · data as JSON: {_e(report_basename(per['month'], per.get('team')))}.json"
                 "</footer></main></body></html>")
    return "".join(parts)
