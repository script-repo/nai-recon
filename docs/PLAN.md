# Build plan

Milestones are committed and pushed as they land.

| # | Milestone | Done when |
|---|---|---|
| 0 | Spec + plan | `docs/SPEC.md`, `docs/PLAN.md` committed |
| 1 | Skeleton | `pyproject.toml`, package layout, config, models, CI workflow, lint |
| 2 | Ingest | OTLP JSON decode, GenAI semconv / OpenInference / LangChain normalizers, JSONL ingest, conversation assembly; unit tests |
| 3 | Store | SQLite store with schema + migrations; Postgres dialect behind optional extra; unit tests |
| 4 | Harness + rubric | harness loader with line maps, git version; rubric builder; unit tests |
| 5 | LLM client + mock | OpenAI-compatible client with budget/audit; mock LLM (in-process + HTTP server); unit tests |
| 6 | Evaluate | checks plugin API + built-ins, sampling, judge, verifier, evaluate pipeline; unit tests |
| 7 | Insights | fingerprinting, clustering, status transitions, history; unit tests |
| 8 | Investigate + propose | locator, patch generator (unified diff), replay, branch/PR, approval; unit tests |
| 9 | Surfaces | CLI, OTLP receiver, dashboard, demo mode; e2e test in demo mode |
| 10 | Deploy | Dockerfile, Helm chart, raw manifests, README quickstart |
| 11 | Harden | ruff + mypy clean, CI green, gaps documented |

## Design decisions

* **Stdlib-first.** HTTP servers (`http.server`), LLM client (`urllib`) and
  SQLite are standard library; the only hard dependency is PyYAML. Smaller
  supply chain, easier air-gapped installs on NKP.
* **Determinism for tests.** Sampling is hash-based; the mock LLM is
  rule-based and deterministic, so the e2e test asserts exact outcomes.
* **Every rubric item carries its source location**, which turns most
  investigations into a lookup instead of an LLM guess.
* **Propose, never apply.** The only write to an agent repo is a new branch.

## Risks

* Real judge quality depends on the NAI model chosen (≥70B instruct models
  recommended). JSON mode support varies by serving engine; the client asks
  for `response_format: json_object` and falls back to extracting the first
  JSON object from text.
* Single-step replay under-tests multi-step fixes (see SPEC §8).
