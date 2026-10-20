"""The evaluation run: checks -> sample -> judge -> verify -> insights."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ambientq.checks import load_checks, run_checks
from ambientq.config import Config
from ambientq.harness import Harness
from ambientq.insights import CorrelationResult, correlate
from ambientq.judge.judge import judge_conversation
from ambientq.judge.rubric import build_rubric
from ambientq.judge.verifier import check_evidence, verify
from ambientq.llm import Budget, BudgetExceeded, LLMClient, LLMError
from ambientq.models import Finding, Run, new_id, now_s
from ambientq.sampling import select
from ambientq.store import SQLStore

log = logging.getLogger(__name__)


@dataclass
class EvalResult:
    run: Run
    correlation: CorrelationResult
    findings: list[Finding] = field(default_factory=list)


def evaluate(store: SQLStore, cfg: Config, harness: Harness, llm: LLMClient | None, *, since_ns: int = 0,
             rate: float | None = None, max_tokens: int | None = None, use_verifier: bool | None = None,
             trigger: str = "manual", reevaluate: bool = False) -> EvalResult:
    version = harness.version()
    budget_tokens = int(max_tokens if max_tokens is not None else cfg.get("budget", "max_tokens_per_run", 0))
    run = Run(run_id=new_id("run"), started_at=now_s(), run_trigger=trigger, harness_version=version,
              budget_tokens=budget_tokens)
    store.save_run(run)
    store.audit("ambientq", "run_started", run.run_id, {"harness_version": version, "trigger": trigger})

    rubric = build_rubric(harness, cfg.get("judge", "goal"))
    candidates = [c for c in store.list_conversations(since_ns=since_ns,
                                                      not_evaluated_for=None if reevaluate else version)
                  if c.agent_name in ("", harness.agent)]
    run.candidates = len(candidates)

    checks = load_checks(cfg.get("checks", "plugins", []), cfg.get("checks", "disabled", []))
    all_findings: list[Finding] = []
    flagged: set[str] = set()
    for conv in candidates:
        for f in run_checks(conv, harness, checks):
            if f.verified is None:
                f.verified, f.verifier_note = check_evidence(f, conv, rubric)
            all_findings.append(f)
            if f.verified:
                flagged.add(conv.conversation_id)

    sample = select(candidates, rate=rate if rate is not None else float(cfg.get("sampling", "rate", 0.2)),
                    seed=str(cfg.get("sampling", "seed", "ambientq")), priority=flagged,
                    max_priority=int(cfg.get("sampling", "max_priority", 50)),
                    max_total=int(cfg.get("budget", "max_conversations_per_run", 0)))
    run.sampled = len(sample)
    sampled_ids = {c.conversation_id for c in sample}

    verifier_on = cfg.get("verifier", "enabled", True) if use_verifier is None else use_verifier
    judged: set[str] = set()
    if llm is not None:
        llm.budget = Budget(max_tokens=budget_tokens)
        vllm = llm.with_model(cfg.get("llm", "verifier_model")) if cfg.get("verifier", "llm", True) else None
        for i, conv in enumerate(sample):
            try:
                found = judge_conversation(llm, conv, harness, rubric,
                                           max_findings=int(cfg.get("judge", "max_findings", 8)),
                                           min_confidence=float(cfg.get("judge", "min_confidence", 0.0)))
                if verifier_on:
                    verify(found, conv, rubric, vllm)
                all_findings.extend(found)
                judged.add(conv.conversation_id)
            except BudgetExceeded:
                run.skipped_budget = len(sample) - i
                log.warning("token budget reached; %d sampled conversation(s) left for the next run",
                            run.skipped_budget)
                break
            except LLMError as exc:
                log.error("judge failed on %s: %s", conv.conversation_id, exc)
                run.notes += f"judge error on {conv.conversation_id}: {exc}\n"
        run.input_tokens, run.output_tokens = llm.budget.input_tokens, llm.budget.output_tokens
    run.evaluated = len(judged)

    for f in all_findings:
        f.run_id = run.run_id
        store.save_finding(f)
    store.commit()
    run.findings = sum(1 for f in all_findings if f.verified is not False)
    run.rejected = sum(1 for f in all_findings if f.verified is False)

    titles = {i.rid: i.text for i in rubric.items}
    corr = correlate(store, run.run_id, all_findings, agents_evaluated={harness.agent} if candidates else set(),
                     harness_version=version, similarity=float(cfg.get("insights", "similarity", 0.5)),
                     resolve_after_runs=int(cfg.get("insights", "resolve_after_runs", 2)), titles=titles)

    # Budget-skipped conversations stay eligible for the next run.
    done = [c.conversation_id for c in candidates if c.conversation_id not in sampled_ids or c.conversation_id in judged]
    if llm is None:
        done = [c.conversation_id for c in candidates]
    store.mark_evaluated(done, run.run_id, version)
    store.mark_judged(judged, run.run_id)

    run.finished_at = now_s()
    run.status = "budget_exhausted" if run.skipped_budget else "ok"
    store.save_run(run)
    store.audit("ambientq", "run_finished", run.run_id, {
        "findings": run.findings, "rejected": run.rejected, "new": len(corr.new),
        "recurring": len(corr.recurring), "resolved": len(corr.resolved),
        "tokens": run.input_tokens + run.output_tokens,
    })
    return EvalResult(run=run, correlation=corr, findings=all_findings)
