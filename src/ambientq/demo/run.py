"""End-to-end demo: four days of synthetic traffic and the whole v0.2 loop.

Days 1-2 arrive as OTLP/JSON traces, day 3 as AI-gateway proxy logs. Each day is
graded; then ambientq proposes a prompt fix and a guardrail fix (both checked by
multi-step replay), shadow-tests the prompt fix on day 4's new traffic, an
approver approves it through the dashboard (trusted-proxy sign-in, CSRF), and
the monthly compliance report is written.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ambientq.config import Config
from ambientq.demo import mock_llm, synthetic
from ambientq.evaluate import evaluate
from ambientq.harness import Harness
from ambientq.ingest.jsonl import parse_files
from ambientq.llm import LLMClient
from ambientq.propose import propose
from ambientq.store import SQLStore

DAY_NS = 86_400 * 1_000_000_000
APPROVER = "sre-lead@example.com"

HARNESS_SRC = Path(__file__).parent / "harness"


@dataclass
class DemoResult:
    workdir: Path
    config_path: Path
    runs: list[dict[str, Any]] = field(default_factory=list)
    insights: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    guardrail_proposal: dict[str, Any] | None = None
    shadow: dict[str, Any] | None = None
    gateway: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    report_paths: list[Path] = field(default_factory=list)
    truth: dict[str, str] = field(default_factory=dict)


def _git(root: Path, *args: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def prepare(workdir: Path, base_url: str = "http://127.0.0.1:9999/v1") -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    hdir = workdir / "agent-repo" / "harness"
    if hdir.exists():
        shutil.rmtree(hdir.parent)
    shutil.copytree(HARNESS_SRC, hdir, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    repo = hdir.parent
    if _git(repo, "init", "-q", "-b", "main"):
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=demo", "-c", "user.email=demo@localhost", "commit", "-q", "-m", "demo agent harness")
    cfg = {
        "store": {"url": f"sqlite:///{(workdir / 'ambientq.db').resolve()}"},
        "harness": {"path": str(hdir.resolve())},
        "llm": {"base_url": base_url, "api_key_env": "NAI_API_KEY", "model": "mock-judge"},
        "sampling": {"rate": 0.5, "seed": "demo"},
        "budget": {"max_tokens_per_run": 500_000, "max_conversations_per_run": 100},
        "insights": {"resolve_after_runs": 2},
        "replay": {"enabled": True, "max_conversations": 3, "control_sample": 2},
        # illustrative prices so the usage page and report show costs; set your own
        "pricing": {"currency": "USD", "default": {"input_per_1k": 0.0005, "output_per_1k": 0.0015}},
    }
    path = workdir / "ambientq.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def run_demo(workdir: str | Path, *, http: bool = False, conversations_per_day: int = 30,
             make_branch: bool = True, log: Any = print) -> DemoResult:
    """Run the whole loop. ``http=True`` drives the mock through a real HTTP server."""
    workdir = Path(workdir)
    srv = None
    transport = None
    base_url = "http://127.0.0.1:9999/v1"
    if http:
        srv = mock_llm.serve(port=0)
        base_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    else:
        transport = mock_llm.MockLLM().transport
    cfg_path = prepare(workdir, base_url)
    cfg = Config.load(cfg_path)
    store = SQLStore(cfg.get("store", "url"))
    harness = Harness.load(cfg.get("harness", "path"))
    llm = LLMClient.from_config(cfg, transport=transport, audit=store.audit, on_usage=store.usage_recorder())
    res = DemoResult(workdir=workdir, config_path=cfg_path)
    now_ns = time.time_ns()
    try:
        plan = [(1, ()), (2, ("tool_error_ignored",)), (3, ("tool_error_ignored",))]
        for day, exclude in plan:
            t0 = now_ns - (5 - day) * DAY_NS
            if day == 3:
                entries, truth = synthetic.generate_proxy_logs(day, conversations_per_day, exclude=exclude, t0_ns=t0)
                res.gateway = _ingest_gateway(store, entries, http)
                log(f"day {day}: {len(entries)} AI-gateway log records -> {res.gateway.get('conversations')} "
                    f"conversations")
            else:
                records, truth = synthetic.generate(day, conversations_per_day, exclude=exclude, t0_ns=t0)
                f = workdir / f"traces-day{day}.jsonl"
                synthetic.write_jsonl(str(f), records)
                parsed = parse_files([f])
                store.ingest_spans(parsed.spans)
                for conv in parsed.conversations:
                    store.upsert_conversation(conv)
            res.truth.update(truth)
            out = evaluate(store, cfg, harness, llm, trigger=f"demo-day{day}")
            r = out.run
            res.runs.append({"day": day, "run_id": r.run_id, "candidates": r.candidates, "sampled": r.sampled,
                             "judged": r.evaluated, "findings": r.findings, "rejected": r.rejected,
                             "tokens": r.input_tokens + r.output_tokens, "new": len(out.correlation.new),
                             "recurring": len(out.correlation.recurring), "resolved": len(out.correlation.resolved)})
            log(f"day {day}: {r.candidates} conversations, {r.sampled} sampled, {r.findings} findings kept, "
                f"{r.rejected} rejected by verifier; insights new={len(out.correlation.new)} "
                f"recurring={len(out.correlation.recurring)} resolved={len(out.correlation.resolved)}")
        res.insights = [{"id": i.insight_id, "status": str(i.status), "title": i.title, "rubric_id": i.rubric_id,
                         "occurrences": i.occurrence_count} for i in store.insights()]
        target = next((i for i in sorted(store.insights(status="RECURRING"), key=lambda i: i.rubric_id)
                       if i.rubric_id.startswith("R-P")), None)
        if target:
            log(f"investigating: {target.title}")
            res.proposal = propose(store, cfg, target.insight_id, harness, llm, branch=make_branch)
            rep = res.proposal.get("replay") or {}
            log(f"proposal {res.proposal['proposal_id']} ({res.proposal['kind']}): {res.proposal['status']} "
                f"({rep.get('mode')} replay fixed {rep.get('fixed')}, still failing {rep.get('still_failing')}, "
                f"regressions {rep.get('regressions')}); branch {res.proposal.get('branch')}")
        guard = next((i for i in store.insights(status="RECURRING") if i.rubric_id.startswith("R-G")), None)
        if guard:
            res.guardrail_proposal = propose(store, cfg, guard.insight_id, harness, llm, branch=False)
            g = res.guardrail_proposal
            log(f"proposal {g['proposal_id']} ({g['kind']}): {g['status']} on {g['file']}")
        if res.proposal:
            # day 4: new traffic arrives after the proposal; shadow-test it there
            records, truth = synthetic.generate(4, conversations_per_day // 2, exclude=("tool_error_ignored",),
                                                t0_ns=now_ns - DAY_NS)
            res.truth.update(truth)
            parsed = parse_files([_write(workdir / "traces-day4.jsonl", records)])
            store.ingest_spans(parsed.spans)
            for conv in parsed.conversations:
                store.upsert_conversation(conv)
            from ambientq.shadow import shadow_test

            res.shadow = shadow_test(store, cfg, res.proposal["proposal_id"], harness,
                                     llm.with_model(cfg.get("replay", "model")), llm)
            log(f"shadow test on {res.shadow['conversations']} new conversations: {res.shadow['verdict']} "
                f"(findings live {res.shadow['live_findings']} vs patched {res.shadow['patched_findings']})")
            if res.proposal["status"] == "PENDING_APPROVAL":
                res.approval = _approve_in_dashboard(store, cfg, res.proposal["proposal_id"])
                log(f"approved in the dashboard by {APPROVER}: HTTP {res.approval['code']}")
        from ambientq.report import build_report, render_html, report_basename

        month = time.strftime("%Y-%m", time.gmtime(now_ns / 1e9))
        rep_data = build_report(store, cfg, month)
        base = workdir / report_basename(month)
        base.with_suffix(".json").write_text(json.dumps(rep_data, indent=1, default=str))
        base.with_suffix(".html").write_text(render_html(rep_data))
        res.report_paths = [base.with_suffix(".html"), base.with_suffix(".json")]
        log(f"compliance report: {res.report_paths[0]}")
    finally:
        store.close()
        if srv:
            srv.shutdown()
    return res


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    synthetic.write_jsonl(str(path), records)
    return path


def _ingest_gateway(store: SQLStore, entries: list[dict[str, Any]], http: bool) -> dict[str, Any]:
    """Day 3 arrives as gateway logs: POSTed to the log sink over HTTP, or ingested directly."""
    body = "\n".join(json.dumps(e) for e in entries).encode()
    if not http:
        from ambientq.ingest.gateway import ingest_gateway_payload

        return ingest_gateway_payload(store, body, "application/x-ndjson")
    from ambientq.web import App, serve_background

    srv = serve_background(App(store, ingest_token="demo-token"))
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/v1/gateway/logs", data=body,
                                     method="POST", headers={"Content-Type": "application/x-ndjson",
                                                             "Authorization": "Bearer demo-token"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return dict(json.loads(r.read()))
    finally:
        srv.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _approve_in_dashboard(store: SQLStore, cfg: Config, proposal_id: str) -> dict[str, Any]:
    """Sign in through a (simulated) oauth2-proxy and press Approve, as an approver would."""
    from ambientq.auth import Auth
    from ambientq.web import App, serve_background

    auth = Auth({"mode": "proxy", "roles": {"approver": [APPROVER], "viewer": ["*"]},
                 "proxy": {"trusted_proxies": ["127.0.0.1/32"]}}, secret=secrets.token_bytes(32))
    srv = serve_background(App(store, auth=auth, cfg=cfg))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    who = {"X-Forwarded-User": APPROVER.split("@")[0], "X-Forwarded-Email": APPROVER}
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/proposals/{proposal_id}", headers=who),
                                    timeout=30) as r:
            page = r.read().decode()
        csrf = page.split("name=csrf value='")[1].split("'")[0]
        form = urllib.parse.urlencode({"csrf": csrf, "decision": "approve",
                                       "note": "demo: replay and shadow test both improved"}).encode()
        req = urllib.request.Request(f"{base}/proposals/{proposal_id}/decision", data=form, method="POST",
                                     headers={**who, "Origin": base,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as r:
                code, location = r.status, r.headers.get("Location", "")
        except urllib.error.HTTPError as exc:
            code, location = exc.code, exc.headers.get("Location", "")
        return {"code": code, "location": location, "by": APPROVER}
    finally:
        srv.shutdown()
