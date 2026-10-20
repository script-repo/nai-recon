# ambientq: on-prem ambient quality agent — specification

Status: v0.2 (implemented in this repo; §11 lists what v0.2 adds to v0.1)
Package: `ambientq` · CLI: `ambientq` · License: Apache-2.0

## 1. Purpose

`ambientq` runs next to production LLM agents and continuously answers three
questions, without any data leaving the customer's environment:

1. **What is going wrong?** Grade sampled production conversations against the
   agent's own *harness* (system prompt, tool definitions, guardrails, config)
   with an LLM judge served on-prem (Nutanix Enterprise AI, or any
   OpenAI-compatible endpoint), plus deterministic Python checks.
2. **Is it the same problem as last week?** Deduplicate findings into
   *insights* with `NEW` / `RECURRING` / `RESOLVED` status and occurrence
   history.
3. **What should change in the harness?** Locate the harness file and line
   range responsible, propose a unified-diff patch with a rationale, replay
   the failing conversations against the patched harness, and offer the patch
   as a branch / pull request. **A human always approves; nothing is ever
   auto-applied to production.**

It is inspired by Google's AQuA (Ambient Quality Agent, adk-recipes v0.1.0),
which does this loop on GCP (BigQuery, Gemini, Cloud Run). `ambientq` keeps the
loop and replaces every cloud dependency:

| Concern            | AQuA (GCP)                         | ambientq (on-prem)                                   |
|--------------------|------------------------------------|------------------------------------------------------|
| Trace source       | BigQuery (ADK analytics plugin)    | OpenTelemetry OTLP/HTTP, JSONL files, framework adapters |
| Storage            | BigQuery                           | SQLite (default) or PostgreSQL                       |
| Judge / verifier   | Gemini                             | Any OpenAI-compatible endpoint; NAI by default       |
| Deterministic checks | Python metrics published to GCS  | Python plugins (entry points or module paths)        |
| Root cause         | Source snapshot in GCS             | Harness files in a local git checkout                |
| Deploy             | Cloud Run + Terraform + IAP        | Docker image, Helm chart for NKP / any Kubernetes    |
| Fix                | Proposed, not applied              | Proposed, **replayed**, opened as branch/PR, never auto-merged |

## 2. Architecture

```
            agents (LangChain, OpenInference, ADK, custom, ...)
                     │ OTLP/HTTP (JSON or protobuf), or JSONL export
                     ▼
 ┌────────────── ambientq ───────────────────────────────────────────────┐
 │ ingest ──► normalize (GenAI semconv) ──► store (SQLite/Postgres)      │
 │                                             │                         │
 │ evaluate:  sample ─► checks (Python) ─┐     │                         │
 │                    ─► LLM judge ──────┼─► verifier ─► findings ─► insights (dedup, status, history)
 │                                       │                                   │
 │ investigate: insight ─► harness locator (file:lines) ─► LLM patch ─► unified diff + rationale
 │ propose:     patch ─► replay failing conversations ─► re-grade ─► branch/PR (human approval)
 │ serve:       OTLP receiver + read-only dashboard + JSON API            │
 └───────────────────────────────────────────────────────────────────────┘
                     │ OpenAI-compatible HTTPS (Bearer API key)
                     ▼
        Nutanix Enterprise AI endpoint on NKP  (judge / verifier / fixer model)
```

All components are in one Python package with no mandatory runtime dependency
beyond PyYAML; HTTP servers and the LLM client use the standard library. This
keeps the supply chain small for air-gapped installs. Optional extras:
`postgres` (psycopg), `otlp-proto` (opentelemetry-proto for protobuf OTLP).

### 2.1 Modules

| Module | Responsibility |
|---|---|
| `ambientq.config` | YAML config + env overrides (`AMBIENTQ_*`), secrets only from env/files |
| `ambientq.models` | Dataclasses: `Span`, `Turn`, `Conversation`, `Finding`, `Insight`, `Occurrence`, `Patch`, `Proposal`, `Run` |
| `ambientq.ingest.otlp` | Decode OTLP `ExportTraceServiceRequest` (JSON; protobuf if available) into `Span`s |
| `ambientq.ingest.normalize` | Map GenAI semconv, OpenInference, and LangChain/LangSmith-style attributes to a common span shape |
| `ambientq.ingest.jsonl` | File ingest: OTLP JSON lines, simple conversation records, LangChain run records |
| `ambientq.ingest.receiver` | `POST /v1/traces` OTLP/HTTP receiver |
| `ambientq.conversation` | Assemble spans into ordered conversations (by `gen_ai.conversation.id` / `session.id`, else trace id) |
| `ambientq.store` | `Store` interface; `SQLStore` for SQLite and PostgreSQL |
| `ambientq.harness` | Load a harness directory (manifest + prompt/tools/guardrail files), line-indexed, versioned by git SHA or content hash |
| `ambientq.llm` | OpenAI-compatible chat client (NAI), JSON-mode parsing, retries, token accounting, budget |
| `ambientq.judge.rubric` | Build a rubric from the harness; every item keeps its source location |
| `ambientq.judge.judge` | LLM judge prompt + parser |
| `ambientq.judge.verifier` | Second pass: deterministic evidence check + LLM verifier |
| `ambientq.checks` | Deterministic check plugin API and built-in checks |
| `ambientq.sampling` | Deterministic, risk-weighted sampling under budget |
| `ambientq.insights` | Fingerprinting, clustering, status transitions, history |
| `ambientq.investigate` | Insight → harness location → patch (unified diff) + rationale |
| `ambientq.replay` | Re-run failing conversations against a patched harness and re-grade |
| `ambientq.propose` | Write branch / open PR (GitHub, GitLab, Gitea), record proposals, approvals |
| `ambientq.web` | Read-only dashboard + JSON API |
| `ambientq.demo` | Synthetic traces, demo harness, mock OpenAI-compatible LLM (in-process and HTTP) |
| `ambientq.cli` | `ingest`, `evaluate`, `insights`, `investigate`, `propose`, `approve`, `serve`, `demo`, `mock-llm` |

## 3. Data model

SQL schema (portable across SQLite and PostgreSQL; JSON stored as text):

```
spans(span_id PK, trace_id, parent_span_id, conversation_id, name, kind,
      start_ns, end_ns, status_code, status_message, attributes JSON,
      events JSON, resource JSON, agent_name, ingested_at)

conversations(conversation_id PK, agent_name, harness_version, started_ns,
      ended_ns, span_count, turn_count, input_tokens, output_tokens,
      has_error, last_evaluated_run, updated_at)

runs(run_id PK, started_at, finished_at, trigger, harness_version,
     sampled, evaluated, findings, rejected, input_tokens, output_tokens,
     budget_tokens, status, notes)

findings(finding_id PK, run_id, conversation_id, source  -- 'judge'|'check:<name>'
     category, rubric_id, severity, confidence, expected, actual,
     evidence JSON, verified, verifier_note, fingerprint, insight_id, created_at)

insights(insight_id PK, fingerprint UNIQUE, title, category, rubric_id,
     status -- NEW|RECURRING|RESOLVED, severity, first_seen_run, last_seen_run,
     occurrence_count, conversation_count, quiet_runs, harness_version_first,
     harness_version_last, created_at, updated_at)

occurrences(insight_id, run_id, count, conversation_ids JSON, status_after,
     created_at, PK(insight_id, run_id))

proposals(proposal_id PK, insight_id, status -- DRAFT|REPLAYED|PENDING_APPROVAL|APPROVED|REJECTED|OPENED,
     file, start_line, end_line, diff, rationale, replay JSON, branch, pr_url,
     approved_by, approved_at, created_at)

audit(id PK, ts, actor, action, subject, detail JSON)
```

### 3.1 Normalized conversation

```
Turn(index, role: user|assistant|tool|system, content, tool_name?,
     tool_call_id?, tool_args?, is_error, span_id, ts_ns, model?)
Conversation(conversation_id, agent_name, harness_version, turns[], metadata)
```

### 3.2 Trace input mapping

| Source | Conversation id | Messages | Tool calls |
|---|---|---|---|
| OTel GenAI semconv (≥1.37) | `gen_ai.conversation.id` | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions` (JSON) and legacy `gen_ai.prompt.N.*` / `gen_ai.completion.N.*` and `gen_ai.*.message` events | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |
| OpenInference | `session.id` | `llm.input_messages.N.message.{role,content}`, `llm.output_messages.N...` | `openinference.span.kind=TOOL`, `tool.name`, `input.value`, `output.value` |
| LangChain / LangSmith run JSON (JSONL) | `session_id` / `thread_id` / root run id | `inputs.messages`, `outputs.generations` | `run_type=tool`, `name`, `inputs`, `outputs` |
| Simple record (JSONL) | `conversation_id` | `messages[]` in OpenAI chat format incl. `tool_calls` | from messages |

Agent identity: `gen_ai.agent.name`, else resource `service.name`.
Harness version: `ambientq.harness.version` or `service.version`.

## 4. Harness

A harness directory (usually inside the agent's git repo) contains
`harness.yaml`:

```yaml
agent: nkp-ops-assistant
system_prompt: prompts/system.md        # one or more files
tools: tools/tools.yaml                 # OpenAI function-tool list (YAML or JSON)
guardrails: guardrails.yaml             # optional: forbidden patterns, required behaviours
config: agent.yaml                      # optional: model, temperature, max_turns ...
goal: "Correct, grounded NKP answers; never leak credentials"   # evaluation focus
```

`ambientq.harness` loads every file with line numbers and reports a version
(`git rev-parse HEAD` when inside a repo and clean, else `sha256:` of
contents). Every rubric item and tool spec keeps `(file, start_line,
end_line)` so investigation can point at it.

## 5. Evaluation

### 5.1 Sampling and budget

* Candidates: conversations not yet graded for the current harness version,
  within `--since`.
* Always include (up to `sampling.max_priority`): conversations with span
  errors, tool errors, or failed deterministic checks.
* Fill the rest by deterministic hash sampling at `sampling.rate`
  (`sha256(conversation_id + seed) < rate`) so reruns are reproducible.
* Budget: `budget.max_tokens_per_run` and `budget.max_conversations_per_run`.
  Token cost is estimated before each call (≈ chars/4) and taken from the
  `usage` field after; evaluation stops cleanly when the budget is reached and
  the run records how many were skipped.

### 5.2 Deterministic checks

A check is any callable `check(conv: Conversation, harness: Harness) ->
Iterable[Finding]`, registered by the `ambientq.checks` entry-point group or
listed by module path in config (`checks.plugins: ["mypkg.checks:no_pii"]`).
Built-ins:

| name | finds |
|---|---|
| `unknown_tool` | a tool call to a name absent from the harness tool list |
| `tool_args_schema` | missing required args / unknown args against the tool JSON schema |
| `unanswered_tool_call` | a tool call with no recorded result |
| `tool_error_ignored` | a tool error followed by an assistant answer that does not acknowledge it |
| `guardrail_patterns` | assistant output matching `guardrails.forbidden_patterns` (regex), e.g. secrets |
| `max_turns` | conversation exceeding `config.max_turns` |
| `empty_final_answer` | conversation ending without an assistant answer |

Checks run on every candidate (they are free); only the LLM judge is sampled.

### 5.3 Judge rubric

The rubric is built from the harness:

1. **Instruction items**: each line/bullet of the system prompt containing a
   normative cue (`must`, `never`, `always`, `do not`, `only`, `should`,
   `required`, `before`, `confirm`) becomes `R-P<n>` with its location.
2. **Tool items**: one per tool (`R-T<name>`): call it only when its
   description applies, with schema-valid arguments; location is the tool
   block.
3. **Guardrail items**: each guardrail (`R-G<n>`).
4. **Generic items** (no location): `G-GROUNDING` (claims must be supported by
   tool results or context), `G-COMPLETION` (all parts of the request
   answered), `G-TOOL-RESULT` (tool errors / empty results handled honestly),
   `G-SAFETY` (no internal details or secrets exposed).
5. The **goal** focuses attention but never creates a finding on its own.

Categories (adapted from AQuA's checklist): `procedure`, `output`,
`tool_selection`, `tool_arguments`, `tool_results`, `completion`,
`grounding`, `answer_quality`, `configuration`, `safety`.

The judge returns strict JSON:

```json
{"findings": [{"rubric_id": "R-P3", "category": "procedure",
  "severity": "high|medium|low", "confidence": 0.0,
  "expected": "...", "actual": "...",
  "evidence": [{"turn": 4, "quote": "exact text from the turn"}]}]}
```

Rules given to the judge: ground every finding in a turn and quote it; judge
against the harness as given; a tool returning bad data is not the agent's
defect (handling it may be); report a root cause once; return an empty list
when nothing is wrong; at most `judge.max_findings` per conversation.

### 5.4 Verifier (false-positive filter)

1. **Deterministic evidence check**: each quoted evidence string must occur
   (whitespace/case-normalized) in the cited turn, and the `rubric_id` must
   exist. Findings failing this are rejected with no model call.
2. **LLM verifier** (optional, can use a different/larger NAI endpoint): sees
   the rubric item, the cited turns and the finding, answers
   `{"supported": bool, "reason": str}`. Unsupported findings are kept as
   `verified=0` (auditable) but never become insights.

## 6. Insights

* **Fingerprint** = `sha1(agent | category | rubric_id | key)` where `key`
  is the tool name for tool categories, else a normalized signature of the
  expected behaviour (lower-cased, stop-words and numbers/ids removed, first 8
  significant tokens).
* **Clustering fallback**: a finding whose fingerprint is unknown is merged
  into an existing open insight with the same agent/category/rubric if token
  Jaccard similarity of `expected` ≥ `insights.similarity` (default 0.5).
* **Status transitions** per run:
  * first occurrence → `NEW`
  * seen again in a later run → `RECURRING` (also `RESOLVED → RECURRING`,
    a regression)
  * not seen for `insights.resolve_after_runs` (default 2) consecutive runs
    that evaluated ≥1 conversation of that agent → `RESOLVED`
* Every run that touches an insight writes an `occurrences` row (history).

## 7. Investigate

1. **Locate**: if the insight's `rubric_id` maps to a harness item, use its
   file/line range; tool categories map to the tool block; otherwise ask the
   LLM to pick a location from a numbered listing of the harness files
   (`{"file":..., "start_line":..., "end_line":...}`), validated against real
   files and lines.
2. **Fix**: give the LLM the insight, 1–3 failing excerpts, and the located
   lines with ±10 lines of context; it returns
   `{"replacement": "<new text for start..end>", "rationale": "..."}`.
3. **Diff**: apply in memory and render a `git apply`-compatible unified diff
   with `difflib`. Validate it applies cleanly to the current file content.

## 8. Self-improvement loop (`propose`)

1. Load the patch; materialize a patched harness in a temp dir.
2. **Replay**: for each failing conversation (up to `replay.max_conversations`),
   rebuild the message prefix up to the first assistant turn implicated in the
   finding; call the *agent's* model (config `replay.model`, an NAI endpoint)
   with the patched system prompt and tool list; append the new assistant turn;
   re-run checks + judge restricted to the insight's rubric item. Repeat on the
   original harness as a control.
3. Score: `fixed = failing_before - failing_after`; record a regression if any
   previously passing sampled conversation (`replay.control_sample`) now fails.
4. If `fixed > 0` and no regressions → status `PENDING_APPROVAL`. Optionally
   (`--branch`) commit to `ambientq/<insight_id>` in the harness repo, and
   (`--open-pr`) push and open a PR via the configured forge API with the
   rationale and replay table. The PR is never merged by ambientq.
5. `ambientq approve <proposal> --by <name>` records the human decision in
   the audit log. Deployment is the customer's normal CD process.

Limits: replay is single-step (the agent's next response, not a full
multi-step re-execution with live tools); tool results are replayed from the
trace. This catches prompt/tool-description fixes, not tool backend bugs.

## 9. Interfaces

### CLI

```
ambientq ingest FILE...|--otlp-dir DIR     # JSONL / OTLP JSON files
ambientq evaluate [--since 24h] [--rate 0.2] [--max-tokens N] [--no-verify]
ambientq insights [--status NEW] [--json]
ambientq investigate INSIGHT_ID [--json]
ambientq propose INSIGHT_ID [--replay/--no-replay] [--branch] [--open-pr]
ambientq approve PROPOSAL_ID --by NAME [--reject]
ambientq serve [--host 0.0.0.0 --port 8080]    # OTLP receiver + dashboard
ambientq demo [--workdir DIR]                  # end-to-end with mock LLM
ambientq mock-llm [--port 9999]                # OpenAI-compatible mock server
```

### HTTP

* `POST /v1/traces` — OTLP/HTTP (application/json; application/x-protobuf when
  `opentelemetry-proto` is installed). Optional bearer token
  (`server.ingest_token`).
* `GET /` dashboard; `/insights/<id>`, `/conversations/<id>`, `/runs`.
* `GET /api/insights`, `/api/insights/<id>`, `/api/runs`, `/api/conversations/<id>`,
  `GET /healthz`.

### Configuration (`ambientq.yaml`)

```yaml
store: {url: "sqlite:///var/lib/ambientq/ambientq.db"}   # or postgresql://...
harness: {path: /harness}                                # git checkout
llm:
  base_url: https://nai.example.local/api/v1             # NAI endpoint (OpenAI-compatible)
  api_key_env: NAI_API_KEY                               # never in the file
  model: llama-3-1-70b-judge                             # NAI endpoint name
  verifier_model: null                                   # default: same as model
  ca_bundle: /etc/ssl/nai-ca.pem                         # NAI often uses a private CA
  timeout_s: 120
  temperature: 0
sampling: {rate: 0.2, seed: ambientq, max_priority: 50}
budget: {max_tokens_per_run: 400000, max_conversations_per_run: 200}
judge: {max_findings: 8, min_confidence: 0.5}
verifier: {enabled: true}
insights: {similarity: 0.5, resolve_after_runs: 2}
replay: {enabled: true, model: null, max_conversations: 5}
forge: {kind: github, api_url: https://api.github.com, repo: org/agent, token_env: FORGE_TOKEN}
server: {ingest_token_env: AMBIENTQ_INGEST_TOKEN}
checks: {disabled: [], plugins: []}
```

## 10. Security, governance and sovereignty

* **Data stays on-prem.** Traces, findings and patches live in the local
  store. The only outbound calls are to the configured OpenAI-compatible
  endpoint (an NAI endpoint inside the cluster) and, only if enabled, the
  customer's own forge. No telemetry, no phone-home.
* **Secrets** come only from environment variables / mounted Kubernetes
  Secrets (`api_key_env`, `token_env`). Config files never hold them; logs and
  the audit table never print them.
* **TLS**: custom CA bundle support for NAI's private PKI; `insecure_skip_verify`
  exists for labs and logs a warning on every start.
* **Human in the loop**: ambientq can only *propose*. Patches go to a branch/PR;
  `approve` records who approved what and when. There is no code path that
  merges or deploys.
* **Audit**: every LLM call (purpose, model, token counts, prompt SHA-256 —
  not content), proposal and approval is recorded in `audit`.
* **Least privilege**: the Helm chart runs as non-root, read-only root FS,
  drops all capabilities, and has an optional NetworkPolicy allowing egress
  only to the NAI endpoint and DNS.
* **Prompt injection**: conversation content is passed to the judge as JSON
  data inside delimiters, the judge is told to treat it as data, and the
  verifier's deterministic evidence check rejects findings with fabricated
  quotes. Proposed patches are text diffs reviewed by humans.
* **PII**: optional redaction regexes (`ingest.redact`) are applied before
  storage.

## 11. v0.2 additions

v0.2 builds everything on the roadmap that does not need a live NAI
endpoint. Each part works against any OpenAI-compatible endpoint and is tested
with the mock model. New config sections all have defaults that keep v0.1
behaviour, so a v0.1 config file still works unchanged.

### 11.1 Judge calibration (`ambientq calibrate`)

Before trusting a judge model on NAI, measure it against human labels.

* **Labeled set** (JSONL, one conversation per line): any conversation record
  ambientq can ingest (simple record, OTLP export) plus a `labels` object:
  `{"labels": {"defects": [{"category": "procedure", "rubric_id": "R-P3"}], "clean": false, "labeler": "..."}}`.
  `clean: true` with no defects marks a conversation a human found correct.
* `ambientq calibrate LABELED.jsonl [--model M] [--base-url URL] [--json] [--out report.json]`
  runs the judge (and, unless `--no-verify`, the verifier) on every labeled
  conversation and compares, per category and per rubric item:
  true/false positives and negatives, **precision**, **recall**, F1, and
  conversation-level **agreement** (judge flags a conversation iff a human did)
  with Cohen's kappa. It also reports judge tokens and latency per
  conversation, which sizes the NAI endpoint.
* A sample labeled set built from the demo scenarios ships in the package
  (`ambientq calibrate --sample`). `docs/CALIBRATION.md` explains how to label
  real traffic and run it against an NAI endpoint, and how to read the numbers
  (e.g. raise `judge.min_confidence` when precision is low).

### 11.2 Multi-step replay with recorded tool results

* Tool results are already stored as `tool` turns at ingest. v0.2 indexes them
  per conversation into a **tool-result tape**: `(tool name, canonical JSON of
  args) -> [results in order]`.
* `replay.mode: multi` (new default; `single` keeps v0.1 behaviour). From the
  cut point the agent's model is called in a loop under the patched harness
  (and under the original as control). Each tool call it makes is answered
  from the tape:
  1. exact match on name + canonical args (each recorded result used once, in order);
  2. else `replay.unmatched` policy: `nearest` (default: same tool, most
     argument keys/values in common), `error` (return a structured
     "not recorded" tool error), or `stop` (end the continuation there).
  Every non-exact answer is **flagged** in the replay row
  (`unmatched_calls: [...]`), and so is a call to a tool the harness doesn't define.
* The loop stops at a final assistant answer, at `replay.max_steps` (default 8)
  model calls, or on the `stop` policy. The whole continuation is graded with
  checks (now including `empty_final_answer` and `unanswered_tool_call`) and the
  judge focused on the insight's rubric item.
* User turns after the cut are not replayed (a replay can't know how the user
  would have answered a different reply); the continuation ends where the agent
  would hand back to the user. This is stated in every replay report (`mode`, `steps`).

### 11.3 Gateway ingest (no agent code changes)

* Research result (Oct 2026, recorded in `docs/GATEWAY.md`): what Nutanix
  documents publicly for NAI / its AI gateway, and which export formats exist.
* `ambientq ingest --format proxy-log FILE...` (also `--format nai-gateway`, and
  auto-detection): an adapter for OpenAI-compatible proxy logs: one JSON object
  per request/response pair (LiteLLM spend/request logs, Envoy AI Gateway
  access logs, generic API gateways). Pairs are grouped into conversations by
  a session header/field, falling back to a hash of the first user message, and
  converted into GenAI-semconv LLM spans, so the rest of the pipeline is
  unchanged. Tool calls in a response and tool results in the next request
  become tool turns, so multi-step replay works on gateway traffic too.
* Field mappings are configurable (`gateway.mappings`, dotted paths), so any
  gateway log shape can be ingested without code.
* The server also accepts `POST /v1/gateway/logs` (same token as OTLP) for
  gateways that can POST their logs (webhook / HTTP log sink).

### 11.4 Dashboard sign-in, roles and approvals

* `auth.mode`: `none` (default, read-only dashboard as in v0.1), `oidc`, or `proxy`.
* **OIDC**: authorization code flow with PKCE (S256), stdlib only. Discovery
  from `{issuer}/.well-known/openid-configuration`; the ID token from the
  token endpoint is validated on its claims (issuer, audience, expiry, nonce)
  after a direct TLS back-channel exchange (per OIDC Core §3.1.3.7, TLS from
  the token endpoint stands in for signature validation; RS256 signature
  verification against the JWKS is done when the optional `cryptography`
  extra is installed: `pip install 'ambientq[oidc]'`).
* **Proxy**: trust identity headers (`X-Forwarded-User`, `X-Forwarded-Email`,
  `X-Forwarded-Groups`, as set by oauth2-proxy / Pomerium / an ingress) only
  from `auth.proxy.trusted_proxies` CIDRs.
* **Roles**: `viewer` < `approver` < `admin`, granted by user, email or group
  (`auth.roles`). Sessions are HMAC-signed cookies (`HttpOnly`, `SameSite=Lax`,
  `Secure` unless `auth.cookie_insecure`), secret from `auth.session_secret_env`.
* **Approvals**: approvers see Approve / Reject on a proposal page that shows
  the diff and the replay (and shadow) results side by side; POST forms carry a
  per-session **CSRF token** (double-submit, constant-time compared) and the
  `Origin` header is checked. Every decision, sign-in and sign-in failure is
  written to the audit log. Admins can see the audit page and exports.
* With `auth.mode: none` the dashboard has no write endpoints at all.
* New UI look: system font stack (Inter / Segoe UI / Roboto fallbacks),
  sidebar navigation, cards, status pills, side-by-side diff/replay view,
  print-friendly; still one server-rendered page per view, no JavaScript needed.

### 11.5 Fixes beyond the system prompt; shadow testing

* Every proposal has a `kind`: `prompt`, `tool_description`, `tool_schema`,
  `guardrail` or `config`, picked from the insight (rubric item location, tool
  categories, check that fired). Each kind has its own fixer instructions:
  * `tool_description`: rewrite the tool's `description` (when to call it, what it returns);
  * `tool_schema`: change the tool's `parameters` JSON schema (add `required`,
    `enum`, `pattern`, `minimum`, descriptions) — validated as a JSON Schema
    object and that the tools file still loads;
  * `guardrail`: add or tighten a guardrail rule or `forbidden_patterns` regex
    (each regex must compile);
  * `config`: change agent config keys (e.g. `max_turns`, `temperature`) —
    only keys already present, values type-checked.
  Structured patches are applied with YAML node locations, so the diff is still
  a minimal `git apply`-able unified diff on the original file.
* **Shadow testing** (`ambientq shadow PROPOSAL [--since 24h] [--limit N]`, and
  `shadow.enabled` to run it after every `evaluate`): replays *new* incoming
  conversations (not the ones the fix was made from) under the live and the
  patched harness with the replay model, grades both, and stores per-proposal
  results: conversations compared, findings live vs. patched, wins, losses,
  and a verdict (`better`, `same`, `worse`). Approvers see it next to the replay.

### 11.6 Compliance reporting and SIEM export

* `ambientq report --month YYYY-MM [--team T] [--format html|json|both] [--out DIR]`:
  conversations ingested and graded, findings kept/rejected, insights
  new/recurring/resolved, mean and median **time to fix** (insight first seen →
  resolved), proposals by status and kind, every decision with who made it and
  when, model usage and cost per model and per agent (agent traffic and
  ambientq's own LLM calls separately), and the audit event counts. HTML has
  print CSS (save as PDF from the browser); JSON has the same data.
* `ambientq audit-export [--since 7d] --format jsonl|cef|syslog [--out FILE]`:
  audit events as JSON lines, ArcSight CEF, or RFC 5424 syslog lines, for a
  SIEM (Splunk, QRadar, Sentinel, Elastic). `--follow` is left to the SIEM's
  file collector.

### 11.7 Cost tracking

* A `usage` table records tokens per (time, team, agent, model, source), where
  source is `agent` (from ingested LLM spans / gateway logs) or `ambientq`
  (judge, verifier, fixer, replay calls).
* `pricing.models.<model>: {input_per_1k, output_per_1k}` and
  `pricing.default`, in `pricing.currency` (default USD). On-prem GPU cost can
  be expressed as an effective price per 1k tokens.
* `ambientq usage [--since 30d] [--by agent|model|team] [--json]`; also in the
  dashboard and the monthly report.

### 11.8 Alerts

* `alerts.webhooks: [{kind: slack|teams|generic, url_env: ENV, min_severity: high}]`
  — URLs are secrets, read from env vars. After each `evaluate`, one message per
  new insight at or above `min_severity`, and per **regression** (a RESOLVED
  insight that recurs). Slack incoming-webhook and Teams (Adaptive Card via
  Workflows/incoming webhook) payloads; `generic` posts JSON. Each delivery
  (and failure) goes to the audit log; failures never fail the run.

### 11.9 Multi-agent, multi-team

* `agents: [{name, harness, team}]` lists every agent this install grades
  (the v0.1 `harness.path` still works as a single agent). `evaluate`,
  `investigate`, `propose` take `--agent`; without it `evaluate` runs every agent.
* Each conversation, run, insight and proposal carries a `team`, taken from the
  `agents` entry, else the span/resource attribute `ambientq.team`, else
  `teams.default` (`default`).
* CLI `--team` filters lists and reports; the dashboard has a team switcher, and
  `auth.roles` entries can be scoped to teams (`approver: ["alice@x", {group: "sre", teams: ["platform"]}]`).

### 11.10 Packaging

* GitHub Actions `release.yml`: on tags `v*` or manual dispatch, build the
  image with Buildx, push to `ghcr.io/<owner>/ambientq`, generate an SBOM with
  syft (SPDX JSON, attached as an attestation and a workflow artifact), and sign
  the image keylessly with cosign (GitHub OIDC). Never on ordinary pushes.
* CI gains a `postgres` job with a PostgreSQL service container that runs the
  store and end-to-end tests against Postgres.
* Helm chart: values for auth, alerts, pricing, agents/teams, shadow; probes,
  pod security context, optional Ingress, ServiceMonitor-free (no extra CRDs).
* `docs/SE-DEMO.md`: a 10-minute demo script for Nutanix SEs.

## 12. Roadmap (after v0.2)

* Needs a live NAI endpoint: calibrate the judge on real labeled traffic per
  model size; tune default prompts; NAI management-API integration (discover
  endpoints, per-endpoint usage and cost).
* Native Nutanix AI Gateway export once a documented log/OTel format exists
  (the generic proxy-log adapter covers it meanwhile).
* LLM-based semantic clustering with embeddings served by NAI.
* Triggered runs after agent deploys (Argo/Flux webhook); dataset export for fine-tuning.
