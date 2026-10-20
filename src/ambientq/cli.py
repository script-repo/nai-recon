"""Command line: ingest, evaluate, insights, investigate, propose, approve, usage, report, audit-export,
calibrate, export-labeling, serve, demo, mock-llm."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from typing import Any

from ambientq import __version__
from ambientq.config import Config


def _since(value: str | None) -> int:
    if not value:
        return 0
    m = re.fullmatch(r"(\d+)([smhd])", value)
    if not m:
        raise SystemExit(f"--since must look like 36h / 7d / 90m, got {value!r}")
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return int((time.time() - secs) * 1e9)


def _when(value: str | None) -> float | None:
    """``7d`` / ``36h`` (that long ago) or an ISO date/time (UTC unless it has an offset) -> epoch seconds."""
    if not value:
        return None
    if re.fullmatch(r"\d+[smhd]", value):
        return _since(value) / 1e9
    from datetime import UTC, datetime

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"expected a duration like 7d or an ISO date/time, got {value!r}") from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _ctx(args: argparse.Namespace) -> tuple[Config, Any]:
    from ambientq.store import SQLStore

    cfg = Config.load(args.config)
    return cfg, SQLStore(cfg.get("store", "url"), team_of=cfg.team_for)


def _llm(cfg: Config, store: Any) -> Any:
    from ambientq.llm import LLMClient

    transport = None
    if cfg.get("llm", "base_url") == "mock":
        from ambientq.demo.mock_llm import MockLLM
        transport = MockLLM().transport
    return LLMClient.from_config(cfg, transport=transport, audit=store.audit, on_usage=store.usage_recorder())


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))


def cmd_ingest(args: argparse.Namespace) -> int:
    from ambientq.ingest.jsonl import parse_files

    cfg, store = _ctx(args)
    parsed = parse_files(args.paths)
    touched = store.ingest_spans(parsed.spans) if parsed.spans else set()
    for conv in parsed.conversations:
        store.upsert_conversation(conv)
    print(f"ingested {len(parsed.spans)} spans and {len(parsed.conversations)} records "
          f"-> {len(touched) + len(parsed.conversations)} conversations")
    for e in parsed.errors[:20]:
        print(f"  warning: {e}", file=sys.stderr)
    return 1 if parsed.errors and not (parsed.spans or parsed.conversations) else 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from ambientq.evaluate import evaluate
    from ambientq.harness import Harness

    cfg, store = _ctx(args)
    harness = Harness.load(args.harness or cfg.get("harness", "path"))
    llm = None if args.checks_only else _llm(cfg, store)
    out = evaluate(store, cfg, harness, llm, since_ns=_since(args.since), rate=args.rate,
                   max_tokens=args.max_tokens, use_verifier=not args.no_verify, trigger=args.trigger,
                   reevaluate=args.all)
    r = out.run
    summary = {"run_id": r.run_id, "harness_version": r.harness_version, "candidates": r.candidates,
               "sampled": r.sampled, "judged": r.evaluated, "skipped_budget": r.skipped_budget,
               "findings_kept": r.findings, "findings_rejected": r.rejected,
               "tokens": r.input_tokens + r.output_tokens, "budget": r.budget_tokens,
               "insights": asdict(out.correlation), "status": r.status}
    if args.json:
        _print(summary, True)
    else:
        print(f"run {r.run_id} on harness {r.harness_version}: {r.candidates} candidates, {r.sampled} sampled, "
              f"{r.evaluated} judged, {r.findings} findings kept, {r.rejected} rejected, "
              f"{r.input_tokens + r.output_tokens} tokens")
        c = out.correlation
        print(f"insights: {len(c.new)} new, {len(c.recurring)} recurring, {len(c.resolved)} resolved")
    if llm is not None and cfg.get("shadow", "enabled", False):
        from ambientq.shadow import shadow_pending

        for pid, res in shadow_pending(store, cfg, harness, llm.with_model(cfg.get("replay", "model")),
                                       llm).items():
            if not args.json:
                print(f"shadow {pid}: {res.get('verdict')} ({res.get('conversations', 0)} conversation(s))")
    return 0


def cmd_insights(args: argparse.Namespace) -> int:
    cfg, store = _ctx(args)
    if args.id:
        ins = store.get_insight(args.id)
        if not ins:
            print(f"no insight {args.id}", file=sys.stderr)
            return 1
        data = {**asdict(ins), "occurrences": store.occurrences(ins.insight_id),
                "findings": [asdict(f) for f in store.findings(insight_id=ins.insight_id)][:20]}
        if args.json:
            _print(data, True)
        else:
            print(f"{ins.insight_id} [{ins.status}] {ins.severity} {ins.title}")
            print(f"  rule {ins.rubric_id}  agent {ins.agent_name}  occurrences {ins.occurrence_count}  "
                  f"conversations {ins.conversation_count}")
            for o in data["occurrences"]:
                print(f"  run {o['run_id']}: {o['count']} -> {o['status_after']}")
        return 0
    rows = store.insights(status=args.status)
    if args.json:
        _print([asdict(i) for i in rows], True)
        return 0
    if not rows:
        print("no insights")
    for i in rows:
        print(f"{i.insight_id}  {str(i.status):9}  {i.severity:6}  x{i.occurrence_count:<3} {i.title}")
    return 0


def cmd_investigate(args: argparse.Namespace) -> int:
    from ambientq.harness import Harness
    from ambientq.investigate import investigate

    cfg, store = _ctx(args)
    harness = Harness.load(args.harness or cfg.get("harness", "path"))
    inv = investigate(store, args.insight, harness, None if args.locate_only else _llm(cfg, store),
                      cfg.get("judge", "goal"), kind=args.kind)
    if args.json:
        _print(inv.to_dict(), True)
        return 0
    print(f"insight:  {inv.insight.title}")
    print(f"kind:     {inv.kind}")
    print(f"location: {inv.location}  ({inv.locate_reason})")
    if inv.patch:
        print(f"\nrationale: {inv.patch.rationale}\n")
        print(inv.patch.diff)
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    from ambientq.harness import Harness
    from ambientq.propose import propose

    cfg, store = _ctx(args)
    harness = Harness.load(args.harness or cfg.get("harness", "path"))
    if args.replay_mode:
        cfg.data["replay"]["mode"] = args.replay_mode
    p = propose(store, cfg, args.insight, harness, _llm(cfg, store), do_replay=not args.no_replay,
                branch=args.branch, open_pr=args.open_pr, kind=args.kind)
    if args.json:
        _print(p, True)
        return 0
    print(f"proposal {p['proposal_id']}: {p['status']}")
    print(f"target:   {p['file']}:{p['start_line']}-{p['end_line']} ({p.get('kind', 'prompt')})")
    if p.get("replay"):
        r = p["replay"]
        print(f"replay:   fixed {r['fixed']}, still failing {r['still_failing']}, regressions {r['regressions']}"
              f" ({r.get('mode', 'single')}-step, {r.get('unmatched_calls', 0)} unmatched tool call(s) flagged)")
    if p.get("branch"):
        print(f"branch:   {p['branch']}")
    if p.get("pr_url"):
        print(f"PR:       {p['pr_url']}")
    print(f"\n{p['diff']}")
    print("Nothing was applied. Approve or reject with: ambientq approve "
          f"{p['proposal_id']} --by <name> [--reject]")
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    from ambientq.harness import Harness
    from ambientq.shadow import shadow_test

    cfg, store = _ctx(args)
    harness = Harness.load(args.harness or cfg.get("harness", "path"))
    llm = _llm(cfg, store)
    since = _since(args.since) / 1e9 if args.since else None
    res = shadow_test(store, cfg, args.proposal, harness, llm.with_model(cfg.get("replay", "model")), llm,
                      limit=args.limit, since=since)
    if args.json:
        _print(res, True)
        return 0
    print(f"shadow test of {args.proposal}: {res['conversations']} new conversation(s), "
          f"findings live {res['live_findings']} vs patched {res['patched_findings']}, "
          f"wins {res['wins']}, losses {res['losses']} -> {res['verdict']}")
    for e in res["errors"][:10]:
        print(f"  warning: {e}", file=sys.stderr)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from ambientq.propose import decide

    cfg, store = _ctx(args)
    p = decide(store, args.proposal, args.by, approve=not args.reject, note=args.note or "")
    print(f"proposal {p['proposal_id']} {p['status']} by {p['approved_by']} (recorded in the audit log)")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    from ambientq.cost import Pricing, format_cost, summarize

    cfg, store = _ctx(args)
    pricing = Pricing.from_config(cfg)
    rows = store.usage_rows(since=_when(args.since) or 0.0, team=args.team)
    data = summarize(rows, pricing, args.by)
    data["since"] = args.since
    if args.json:
        _print(data, True)
        return 0
    print(f"{'' if not args.team else f'team {args.team}, '}last {args.since}, by {args.by} ({pricing.currency})")
    print(f"{args.by:<32} {'calls':>8} {'input':>12} {'output':>12} {'cost':>16}")
    for r in [*data["rows"], {"key": "TOTAL", **data["totals"]}]:
        print(f"{(r['key'] or '-')[:32]:<32} {r['calls']:>8,} {r['input_tokens']:>12,} {r['output_tokens']:>12,} "
              f"{format_cost(r['cost'], pricing.currency):>16}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    from ambientq.report import build_report, render_html, report_basename

    cfg, store = _ctx(args)
    month = args.month or time.strftime("%Y-%m", time.gmtime())
    report = build_report(store, cfg, month, team=args.team)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = out / report_basename(month, args.team)
    if args.format in ("json", "both"):
        base.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(base.with_suffix(".json"))
    if args.format in ("html", "both"):
        base.with_suffix(".html").write_text(render_html(report), encoding="utf-8")
        print(base.with_suffix(".html"))
    return 0


def cmd_audit_export(args: argparse.Namespace) -> int:
    from ambientq.siem import iter_export

    cfg, store = _ctx(args)
    rows = store.audit_log(limit=2**62, since=_when(args.since) or 0.0, until=_when(args.until), ascending=True)
    lines = iter_export(rows, args.format)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            n = sum(fh.write(line + "\n") > 0 for line in lines)
        print(f"wrote {n} audit events to {args.out}", file=sys.stderr)
    else:
        for line in lines:
            sys.stdout.write(line + "\n")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from ambientq.calibrate import calibrate, read_labeled
    from ambientq.harness import Harness

    if args.sample:
        from ambientq.demo.calibration import SAMPLE_PATH

        path = Path(args.labeled or SAMPLE_PATH)
        harness_path = args.harness or str(Path(SAMPLE_PATH).parents[1] / "harness")
    elif args.labeled:
        path, harness_path = Path(args.labeled), args.harness
    else:
        raise SystemExit("calibrate needs a labeled JSONL file, or --sample for the shipped demo set")
    cfg, store = _ctx(args)
    llm_cfg = cfg.data["llm"]
    for key, val in (("base_url", args.base_url), ("model", args.model), ("api_key_env", args.api_key_env),
                     ("verifier_model", args.verifier_model)):
        if val:
            llm_cfg[key] = val
    parsed = read_labeled(path)
    for e in parsed.errors[:50]:
        print(f"  bad line: {e}", file=sys.stderr)
    if parsed.errors:
        print(f"error: {len(parsed.errors)} bad line(s) in {path}; fix them and run again", file=sys.stderr)
        return 2
    if parsed.unlabeled:
        print(f"note: {len(parsed.unlabeled)} conversation(s) have no labels yet and are skipped", file=sys.stderr)
    if not parsed.items:
        print(f"error: no labeled conversations in {path}", file=sys.stderr)
        return 2
    harness = Harness.load(harness_path or cfg.get("harness", "path"))
    llm = _llm(cfg, store)
    llm.usage_context.update(agent=harness.agent, run_id=f"calibrate:{path.name}")
    verify = not args.no_verify and bool(cfg.get("verifier", "enabled", True))
    rep = calibrate(
        parsed.items, harness, llm, verifier_llm=llm.with_model(cfg.get("llm", "verifier_model")),
        verify=verify, llm_verify=bool(cfg.get("verifier", "llm", True)), with_checks=args.with_checks,
        checks=_checks(cfg) if args.with_checks else None,
        min_confidence=float(args.min_confidence if args.min_confidence is not None
                             else cfg.get("judge", "min_confidence", 0.0)),
        goal=cfg.get("judge", "goal"), max_findings=int(cfg.get("judge", "max_findings", 8)),
        conversations_per_day=args.per_day,
        sampling_rate=float(args.rate if args.rate is not None else cfg.get("sampling", "rate", 0.2)),
    )
    store.audit("ambientq", "calibrated", path.name, rep.summary())
    data = rep.to_dict()
    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    if args.json:
        _print(data, True)
    else:
        print(rep.to_text())
        if args.out:
            print(f"\nfull report: {args.out}")
    return 0 if rep.judged else 1


def _checks(cfg: Config) -> Any:
    from ambientq.checks import load_checks

    return load_checks(cfg.get("checks", "plugins", []), cfg.get("checks", "disabled", []))


def cmd_export_labeling(args: argparse.Namespace) -> int:
    from ambientq.calibrate import export_for_labeling

    cfg, store = _ctx(args)
    recs = export_for_labeling(store, since_ns=_since(args.since), n=args.n, agent=args.agent, team=args.team,
                               flagged_share=args.flagged_share, seed=args.seed)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    store.audit("ambientq", "exported_for_labeling", args.out, {"conversations": len(recs), "since": args.since,
                                                               "agent": args.agent, "team": args.team})
    print(f"wrote {len(recs)} conversation(s) to {args.out}; fill in each line's \"labels\" and run: "
          f"ambientq calibrate {args.out}")
    return 0 if recs else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from ambientq.web import App, make_server

    cfg, store = _ctx(args)
    host = args.host or cfg.get("server", "host")
    port = args.port or int(cfg.get("server", "port"))
    token = cfg.secret("server", "ingest_token_env")
    srv = make_server(App(store, token), host, port)
    print(f"ambientq {__version__}: dashboard http://{host}:{port}/  OTLP/HTTP http://{host}:{port}/v1/traces"
          f"{'  (ingest token required)' if token else ''}")
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from ambientq.demo.run import run_demo

    res = run_demo(args.workdir, http=args.http, conversations_per_day=args.conversations,
                   make_branch=not args.no_branch)
    print(f"\ndemo state in {res.workdir}; config {res.config_path}")
    print(f"next: ambientq -c {res.config_path} insights   |   ambientq -c {res.config_path} serve")
    if args.serve:
        args.config = str(res.config_path)
        args.host, args.port = "127.0.0.1", args.port
        return cmd_serve(args)
    return 0


def cmd_mock_llm(args: argparse.Namespace) -> int:
    from ambientq.demo.mock_llm import serve

    srv = serve(args.host, args.port)
    print(f"mock OpenAI-compatible LLM on http://{args.host}:{srv.server_address[1]}/v1")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ambientq", description="On-prem ambient quality agent for LLM agents")
    p.add_argument("-c", "--config", help="path to ambientq.yaml (or AMBIENTQ_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"ambientq {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="ingest JSONL / OTLP JSON trace files or directories")
    s.add_argument("paths", nargs="+")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("evaluate", help="grade sampled conversations and update insights")
    s.add_argument("--harness")
    s.add_argument("--since", help="only conversations newer than e.g. 24h, 7d")
    s.add_argument("--rate", type=float, help="sampling rate override (0..1)")
    s.add_argument("--max-tokens", type=int, help="token budget override for this run")
    s.add_argument("--no-verify", action="store_true", help="skip the verifier pass")
    s.add_argument("--checks-only", action="store_true", help="run only the deterministic checks (no LLM)")
    s.add_argument("--all", action="store_true", help="re-evaluate conversations already graded for this harness")
    s.add_argument("--trigger", default="manual", help="label for the run (manual, schedule, deploy)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("insights", help="list insights, or show one")
    s.add_argument("id", nargs="?")
    s.add_argument("--status", choices=["NEW", "RECURRING", "RESOLVED"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_insights)

    s = sub.add_parser("investigate", help="locate the harness lines behind an insight and draft a patch")
    s.add_argument("insight")
    s.add_argument("--harness")
    s.add_argument("--locate-only", action="store_true", help="no LLM: only map to file/lines")
    s.add_argument("--kind", choices=["prompt", "tool_description", "tool_schema", "guardrail", "config"], help="force what to fix (default: chosen from the insight)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_investigate)

    s = sub.add_parser("propose", help="patch + replay + (optional) branch/PR; never applied")
    s.add_argument("insight")
    s.add_argument("--harness")
    s.add_argument("--no-replay", action="store_true")
    s.add_argument("--replay-mode", choices=["multi", "single"],
                   help="multi: run to a final answer on recorded tool results (default); single: next turn only")
    s.add_argument("--branch", action="store_true", help="commit the patch to a new branch in the harness repo")
    s.add_argument("--open-pr", action="store_true", help="push the branch and open a PR/MR on the configured forge")
    s.add_argument("--kind", choices=["prompt", "tool_description", "tool_schema", "guardrail", "config"], help="force what to fix (default: chosen from the insight)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("shadow", help="run a proposal's patched harness beside the live one on new traffic")
    s.add_argument("proposal")
    s.add_argument("--harness")
    s.add_argument("--since", help="conversations ingested in the last e.g. 24h (default: since the proposal)")
    s.add_argument("--limit", type=int, help="at most N conversations (default shadow.limit)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_shadow)

    s = sub.add_parser("approve", help="record a human decision on a proposal")
    s.add_argument("proposal")
    s.add_argument("--by", required=True)
    s.add_argument("--reject", action="store_true")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("usage", help="token usage and cost from agent traffic and ambientq's own calls")
    s.add_argument("--since", default="30d", help="e.g. 30d, 24h, or an ISO date (default 30d)")
    s.add_argument("--by", choices=["agent", "model", "team", "source"], default="model")
    s.add_argument("--team")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("report", help="monthly compliance report (HTML with print CSS, and JSON)")
    s.add_argument("--month", help="YYYY-MM in UTC (default: the current month)")
    s.add_argument("--team")
    s.add_argument("--format", choices=["html", "json", "both"], default="both")
    s.add_argument("--out", default=".", help="output directory (default .)")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("audit-export", help="audit events for a SIEM: JSON lines, ArcSight CEF or RFC 5424 syslog")
    s.add_argument("--since", default="7d", help="e.g. 7d, 24h, or an ISO date/time (default 7d)")
    s.add_argument("--until", help="end (exclusive): a duration ago or an ISO date/time")
    s.add_argument("--format", choices=["jsonl", "cef", "syslog"], required=True)
    s.add_argument("--out", help="write to FILE instead of stdout")
    s.set_defaults(fn=cmd_audit_export)

    s = sub.add_parser("calibrate", help="measure the judge against human labels (precision, recall, kappa, cost)")
    s.add_argument("labeled", nargs="?", help="labeled JSONL (see docs/CALIBRATION.md)")
    s.add_argument("--sample", action="store_true", help="use the shipped demo labeled set and demo harness")
    s.add_argument("--base-url", help="OpenAI-compatible endpoint, e.g. https://<nai-host>/api/v1, or 'mock'")
    s.add_argument("--model", help="judge model (the NAI endpoint name)")
    s.add_argument("--api-key-env", help="environment variable holding the endpoint API key")
    s.add_argument("--verifier-model", help="verifier model (default: the judge model)")
    s.add_argument("--no-verify", action="store_true", help="score raw judge findings (no evidence check, no verifier)")
    s.add_argument("--with-checks", action="store_true", help="count deterministic check findings too")
    s.add_argument("--min-confidence", type=float, help="threshold for the headline numbers (default judge.min_confidence)")
    s.add_argument("--per-day", type=int, default=1000, help="conversations/day for the sizing estimate (default 1000)")
    s.add_argument("--rate", type=float, help="sampling rate for the sizing estimate (default sampling.rate)")
    s.add_argument("--harness")
    s.add_argument("--json", action="store_true")
    s.add_argument("--out", help="also write the full JSON report to FILE")
    s.set_defaults(fn=cmd_calibrate)

    s = sub.add_parser("export-labeling", help="write stored conversations as labeled-format records to label")
    s.add_argument("--since", default="7d", help="conversations newer than e.g. 7d, 24h (default 7d)")
    s.add_argument("--n", type=int, default=50, help="how many conversations (default 50)")
    s.add_argument("--agent")
    s.add_argument("--team")
    s.add_argument("--flagged-share", type=float, default=0.5,
                   help="share drawn from conversations ambientq already flagged (default 0.5)")
    s.add_argument("--seed", default="ambientq", help="selection seed (same seed, same sample)")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_export_labeling)

    s = sub.add_parser("serve", help="OTLP/HTTP receiver + read-only dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("demo", help="run the whole loop on synthetic traces with a mock LLM")
    s.add_argument("--workdir", default="./demo-work")
    s.add_argument("--conversations", type=int, default=30, help="conversations per simulated day")
    s.add_argument("--http", action="store_true", help="drive the mock LLM over real HTTP")
    s.add_argument("--no-branch", action="store_true")
    s.add_argument("--serve", action="store_true", help="start the dashboard afterwards")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("mock-llm", help="run the mock OpenAI-compatible LLM server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=9999)
    s.set_defaults(fn=cmd_mock_llm)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(args.fn(args) or 0)
    except (KeyError, RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
