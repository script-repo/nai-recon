# Build plan

## v0.1

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

## v0.2

Everything on the roadmap that does not need a live NAI endpoint (SPEC §11).
Built on feature branches merged into `ambientq`; each milestone has unit
tests, and the end-to-end demo is extended to use it.

| # | Milestone | Done when |
|---|---|---|
| 12 | Foundations | SPEC/PLAN v0.2, config defaults for every new section, additive schema migrations (new columns and tables) |
| 13 | Multi-step replay | tool-result tape, match policies with flags, loop to final answer, `replay.mode` single/multi |
| 14 | Patch kinds + shadow | prompt / tool_description / tool_schema / guardrail / config fixers with validation; `ambientq shadow`, stored shadow results |
| 15 | Calibration | labeled format, `ambientq calibrate`, sample set, `docs/CALIBRATION.md` |
| 16 | Gateway ingest | research recorded in `docs/GATEWAY.md`; proxy-log adapter with field mappings; `POST /v1/gateway/logs` |
| 17 | Dashboard auth + approvals | OIDC (PKCE) and proxy-header sign-in, roles, CSRF-protected approve/reject, audit, new UI |
| 18 | Reports, SIEM, cost | `ambientq report`, `ambientq audit-export` (jsonl/CEF/syslog), usage table, pricing, `ambientq usage` |
| 19 | Alerts, multi-team | Slack/Teams/webhook alerts on new high-severity insights and regressions; `agents:` list, team scoping in store/CLI/UI |
| 20 | Packaging | release workflow (GHCR, cosign keyless, syft SBOM), Postgres CI job, Helm polish, `docs/SE-DEMO.md` |
| 21 | Release | README "What ships today", demo e2e covers new features, CI green, tag `v0.2.0` |

### v0.2 design decisions

* **Still stdlib-first.** OIDC, webhooks, CEF/syslog and HTML reports use the
  standard library. JWKS signature checks use the optional `cryptography`
  extra (`ambientq[oidc]`); without it, ID tokens are trusted only from the
  direct TLS token-endpoint exchange (OIDC Core §3.1.3.7).
* **Additive migrations only.** New columns are added with `ALTER TABLE ADD
  COLUMN` when missing, so v0.1 databases upgrade in place.
* **Replay never calls real tools.** Multi-step replay answers tool calls only
  from recorded results; anything not recorded is flagged, never executed.
* **Approval stays human.** The dashboard can record a decision; it still
  cannot merge or deploy.

### v0.2 risks

* Judge precision/recall on real traffic is unknown until calibrated on NAI.
* Gateway log shapes vary by product and version; field mappings cover the gap.
* Multi-step replay cannot replay user turns after the cut.
