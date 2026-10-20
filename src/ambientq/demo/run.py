"""End-to-end demo: three days of synthetic traffic, evaluations, and a fix proposal."""

from __future__ import annotations

import shutil
import subprocess
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

HARNESS_SRC = Path(__file__).parent / "harness"


@dataclass
class DemoResult:
    workdir: Path
    config_path: Path
    runs: list[dict[str, Any]] = field(default_factory=list)
    insights: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
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
    try:
        plan = [(1, ()), (2, ("tool_error_ignored",)), (3, ("tool_error_ignored",))]
        for day, exclude in plan:
            records, truth = synthetic.generate(day, conversations_per_day, exclude=exclude)
            res.truth.update(truth)
            f = workdir / f"traces-day{day}.jsonl"
            synthetic.write_jsonl(str(f), records)
            parsed = parse_files([f])
            store.ingest_spans(parsed.spans)
            for conv in parsed.conversations:
                store.upsert_conversation(conv)
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
        target = next((i for i in store.insights(status="RECURRING") if i.rubric_id.startswith("R-P")), None)
        if target:
            log(f"investigating: {target.title}")
            res.proposal = propose(store, cfg, target.insight_id, harness, llm, branch=make_branch)
            rep = res.proposal.get("replay") or {}
            log(f"proposal {res.proposal['proposal_id']}: {res.proposal['status']} "
                f"(replay fixed {rep.get('fixed')}, still failing {rep.get('still_failing')}, "
                f"regressions {rep.get('regressions')}); branch {res.proposal.get('branch')}")
    finally:
        store.close()
        if srv:
            srv.shutdown()
    return res
