"""Command line: ingest, evaluate, insights, investigate, propose, approve, serve, demo, mock-llm."""

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
    return LLMClient.from_config(cfg, transport=transport, audit=store.audit)


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))


def cmd_ingest(args: argparse.Namespace) -> int:
    from ambientq.ingest.gateway import mappings_from_config
    from ambientq.ingest.jsonl import parse_files

    cfg, store = _ctx(args)
    parsed = parse_files(args.paths, fmt=args.format, mappings=mappings_from_config(cfg.section("gateway")))
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
                      cfg.get("judge", "goal"))
    if args.json:
        _print(inv.to_dict(), True)
        return 0
    print(f"insight:  {inv.insight.title}")
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
    p = propose(store, cfg, args.insight, harness, _llm(cfg, store), do_replay=not args.no_replay,
                branch=args.branch, open_pr=args.open_pr)
    if args.json:
        _print(p, True)
        return 0
    print(f"proposal {p['proposal_id']}: {p['status']}")
    print(f"target:   {p['file']}:{p['start_line']}-{p['end_line']}")
    if p.get("replay"):
        r = p["replay"]
        print(f"replay:   fixed {r['fixed']}, still failing {r['still_failing']}, regressions {r['regressions']}")
    if p.get("branch"):
        print(f"branch:   {p['branch']}")
    if p.get("pr_url"):
        print(f"PR:       {p['pr_url']}")
    print(f"\n{p['diff']}")
    print("Nothing was applied. Approve or reject with: ambientq approve "
          f"{p['proposal_id']} --by <name> [--reject]")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from ambientq.propose import decide

    cfg, store = _ctx(args)
    p = decide(store, args.proposal, args.by, approve=not args.reject, note=args.note or "")
    print(f"proposal {p['proposal_id']} {p['status']} by {p['approved_by']} (recorded in the audit log)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from ambientq.web import App, make_server

    cfg, store = _ctx(args)
    host = args.host or cfg.get("server", "host")
    port = args.port or int(cfg.get("server", "port"))
    token = cfg.secret("server", "ingest_token_env")
    from ambientq.ingest.gateway import mappings_from_config

    srv = make_server(App(store, token, gateway_mappings=mappings_from_config(cfg.section("gateway"))), host, port)
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
    s.add_argument("--format", default="auto",
                   choices=["auto", "otlp", "record", "langchain", "proxy-log", "litellm", "envoy-ai-gateway",
                            "kong", "nai-gateway"],
                   help="input format (default: detect per record); proxy-log formats read gateway.mappings")
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
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_investigate)

    s = sub.add_parser("propose", help="patch + replay + (optional) branch/PR; never applied")
    s.add_argument("insight")
    s.add_argument("--harness")
    s.add_argument("--no-replay", action="store_true")
    s.add_argument("--branch", action="store_true", help="commit the patch to a new branch in the harness repo")
    s.add_argument("--open-pr", action="store_true", help="push the branch and open a PR/MR on the configured forge")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("approve", help="record a human decision on a proposal")
    s.add_argument("proposal")
    s.add_argument("--by", required=True)
    s.add_argument("--reject", action="store_true")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_approve)

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
