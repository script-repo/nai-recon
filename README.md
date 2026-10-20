# ambientq — on-prem ambient quality agent for LLM agents

**ambientq** runs next to your production agents and keeps grading them. It reads
their OpenTelemetry traces, grades a sample of conversations with an LLM served
on-prem (by default **Nutanix Enterprise AI**, or any OpenAI-compatible endpoint),
groups the failures into deduplicated *insights*, and finds the line in the
agent's harness (system prompt, tool definitions, guardrails) behind each one.
It then proposes a patch (to the prompt, a tool description or schema, a
guardrail or the agent config), replays the failing conversations against it
with their recorded tool results, shadow-tests it on new traffic, and opens it
as a branch or PR **for a human to approve**, in the dashboard or the CLI.

It is a vendor-neutral, fully on-prem take on Google's
[AQuA](https://github.com/google/adk-recipes/tree/main/core/python/ambient-quality-agent).
AQuA runs on BigQuery, Gemini and Cloud Run; ambientq uses OTLP, SQLite or Postgres,
and your own GPUs.

```
agents ──OTLP──┐
AI gateway logs ┴► ambientq ──► checks + sampled LLM judge ──► verifier ──► insights (NEW/RECURRING/RESOLVED/regression)
                     │                                                         │            └► Slack / Teams / webhook alerts
                     └── dashboard (sign-in, approvals) / API / monthly report  investigate ◄─┘
                                       └► patch (diff) ► multi-step replay ► shadow test ► branch/PR ► human approval
judge, verifier, fixer and replay models: OpenAI-compatible endpoints on NAI (inside your cluster)
```

## Why this matters for regulated, sovereign AI

- **Data never leaves your environment.** Traces, findings and patches stay in a local
  database. The only outbound calls go to your NAI endpoint and, if you enable
  it, your own git server. There is no telemetry and nothing phones home. The Helm chart ships a
  NetworkPolicy that enforces this.
- **Governance built in.** Every LLM call is recorded in an audit table with its
  purpose, model, token counts and a prompt SHA-256 (never the prompt content), and
  so is every proposal and every human decision. ambientq **cannot** merge or deploy: it only proposes.
- **Cost under control.** Deterministic checks run on every conversation and cost
  nothing. The LLM judge runs on a reproducible, risk-weighted sample, within a
  per-run token budget.
- **Small supply chain.** The only runtime dependency is PyYAML. The HTTP servers, the LLM client and SQLite all come
  from the standard library, which makes air-gapped installs on NKP easy.

## Quickstart (no cluster needed)

```bash
pip install -e .          # Python 3.11+
ambientq demo --workdir ./demo-work --serve
```

The demo builds a sample agent (an "NKP Ops Assistant" with 4 tools and 6 rules)
inside a local git repo. It simulates four days of production traffic with planted
defects: days 1–2 as traces (OTel GenAI semconv, OpenInference and plain JSONL), day 3
as AI-gateway proxy logs. It grades each day with a mock OpenAI-compatible model,
proposes a prompt fix and a guardrail fix (checked by multi-step replay), shadow-tests
the prompt fix on day 4's traffic, approves it through the dashboard as a signed-in
approver, and writes the monthly compliance report. Open http://127.0.0.1:8080.

```
day 1: 30 conversations, 20 sampled, 12 findings kept, 2 rejected by verifier; insights new=6 recurring=0 resolved=0
day 2: 30 conversations, 14 sampled, 10 findings kept, 0 rejected by verifier; insights new=0 recurring=4 resolved=0
day 3: 53 AI-gateway log records -> 30 conversations
day 3: 30 conversations, 15 sampled, 11 findings kept, 1 rejected by verifier; insights new=0 recurring=4 resolved=2
investigating: [procedure] Always call `get_cluster_status` before answering any question about a cluster's health.
proposal prp_… (prompt): PENDING_APPROVAL (multi replay fixed 3, still failing 0, regressions 0); branch ambientq/ins_…
proposal prp_… (guardrail): PENDING_APPROVAL on guardrails.yaml
shadow test on 15 new conversations: better (findings live 13 vs patched 5)
approved in the dashboard by sre-lead@example.com: HTTP 303
compliance report: demo-work/ambientq-report-2026-10.html
```

A 10-minute walkthrough for customer demos is in [`docs/SE-DEMO.md`](docs/SE-DEMO.md).

Add `--http` to run the mock model as a real HTTP server, or run it yourself with
`ambientq mock-llm --port 9999`.

## Use it with Nutanix Enterprise AI

1. **Pick a judge endpoint in NAI.** An instruct model of 70B or larger grades best. Copy its URL from
   *View Sample Request* (for example `https://nai.example.local/api/v1`). Leave off the
   `/chat/completions` suffix. NAI authenticates with `Authorization: Bearer <endpoint API key>`,
   and the `model` field is the endpoint name. **Calibrate it first** against human labels:
   `ambientq calibrate labeled.jsonl --base-url https://nai.example.local/api/v1 --model <endpoint>`
   reports precision, recall and agreement per category and recommends a confidence threshold
   ([`docs/CALIBRATION.md`](docs/CALIBRATION.md); `ambientq calibrate --sample --base-url mock` to try it).
2. **Describe the harness.** Put a `harness.yaml` in the agent's repo:

   ```yaml
   agent: nkp-ops-assistant
   system_prompt: prompts/system.md      # one file or a list
   tools: tools/tools.yaml               # OpenAI function-tool list (YAML/JSON)
   guardrails: guardrails.yaml           # forbidden_patterns (regex) and rules
   config: agent.yaml                    # model, max_turns, ...
   goal: "Grounded answers, never leak credentials, confirm before changing infrastructure"
   ```

3. **Configure** (see [`examples/ambientq.yaml`](examples/ambientq.yaml)) and export the key:

   ```bash
   export NAI_API_KEY=...            # never put secrets in the config file (it refuses them)
   ```

4. **Send traces.** Point your agent's OTel exporter at `http://<ambientq>:8080/v1/traces`
   (OTLP/HTTP with JSON encoding; protobuf works with `pip install 'ambientq[otlp-proto]'`), or route
   them through an OTel Collector ([`examples/otel-collector.yaml`](examples/otel-collector.yaml)).
   You can also ingest exported files instead. **No agent changes?** Ingest your AI gateway's
   request logs (LiteLLM, Envoy AI Gateway, Kong, or any OpenAI-compatible proxy via field mappings):
   `ambientq ingest --format proxy-log gateway.jsonl`, or have the gateway POST them to
   `/v1/gateway/logs` ([`docs/GATEWAY.md`](docs/GATEWAY.md)).
5. **Run the loop:**

   ```bash
   ambientq -c ambientq.yaml ingest traces/*.jsonl       # OTLP JSON, simple records, LangChain runs
   ambientq -c ambientq.yaml evaluate --since 24h        # checks + sampled judge + verifier + insights
   ambientq -c ambientq.yaml insights --status NEW
   ambientq -c ambientq.yaml investigate ins_1a2b        # file:lines + proposed diff + rationale
   ambientq -c ambientq.yaml propose ins_1a2b --branch   # replay, then commit to ambientq/<insight> branch
   ambientq -c ambientq.yaml propose ins_1a2b --open-pr  # ...and open a PR/MR (GitHub, GitLab, Gitea)
   ambientq -c ambientq.yaml shadow prp_9z8y --since 24h # compare live vs patched on new traffic
   ambientq -c ambientq.yaml approve prp_9z8y --by alex  # record the human decision (audit log)
   ambientq -c ambientq.yaml serve                       # receivers + dashboard (approvals with sign-in) + JSON API
   ambientq -c ambientq.yaml report --month 2026-10      # compliance report, HTML (print to PDF) + JSON
   ambientq -c ambientq.yaml usage --by model            # tokens and cost per agent/model/team
   ambientq -c ambientq.yaml audit-export --format cef   # audit log for your SIEM (jsonl, cef, syslog)
   ```

## Deploy beside agents on NKP

```bash
kubectl create namespace ai-quality
kubectl -n ai-quality create secret generic nai-judge-key --from-literal=apiKey=<NAI endpoint key>
helm install ambientq deploy/helm/ambientq -n ai-quality \
  --set nai.baseUrl=https://nai.example.local/api/v1 --set nai.model=llama-3-1-70b-judge \
  --set harness.repo=https://git.example.local/ai/nkp-ops-agent.git \
  --set networkPolicy.egressCIDRs='{10.20.0.0/24}'
```

The chart deploys the server (receiver, dashboard and API) and an hourly `evaluate` CronJob, with
a PVC for SQLite (or point it at Postgres), a non-root read-only container, a git
init container for the harness, an optional private CA for NAI, and a NetworkPolicy. Sign-in
(`auth.mode: oidc` or `proxy`), Ingress with TLS, alerts, several agents and teams, and pricing are
chart values; bad values fail at render time. The image defaults to the signed
`ghcr.io/script-repo/ambientq` release (verify it with cosign, or mirror it into an air-gapped
registry: [`docs/RELEASE.md`](docs/RELEASE.md)). Plain manifests are in
[`deploy/k8s/ambientq.yaml`](deploy/k8s/ambientq.yaml), and a `Dockerfile` is included.

## How grading works

- **Rubric built from the harness.** Every normative line in the prompt (*must*, *never*,
  *always*, *before*, *confirm* …) becomes a rubric item, and so do every tool and every guardrail. There are also four
  generic items: grounding, completion, tool-result handling and safety. Each item keeps its
  `file:line` location, so most investigations are a lookup, not a guess.
- **Deterministic checks**, run on every conversation: `unknown_tool`, `tool_args_schema`,
  `unanswered_tool_call`, `tool_error_ignored`, `guardrail_patterns`, `max_turns`,
  `empty_final_answer`. You can add your own as Python plugins (`module:function` or the `ambientq.checks`
  entry-point group); see [`examples/my_checks.py`](examples/my_checks.py).
- **LLM judge**: one call per sampled conversation. The conversation is passed as delimited JSON
  data, and every finding must cite a turn and quote it.
- **Verifier**: findings whose quotes don't appear in the transcript, or that cite an unknown rubric
  item, are rejected without a model call. An optional second model then confirms the rest.
  Rejected findings are kept for audit but never become insights.
- **Insights**: findings are fingerprinted by agent, category, rule and tool (or a normalized
  signature), with a similarity fallback. An insight is NEW on first sight, RECURRING when it shows up in a later
  run (including after it was resolved, which is a regression), and RESOLVED after N quiet runs.
- **Replay**: the failing conversation is cut before the offending assistant turn. The agent's own
  model continues it under the original harness and under the patched one, step by step, with each
  tool call answered from the conversation's recorded tool results (matched by name and arguments; a
  call never recorded there is answered from the same call in the agent's other conversations, then by
  the `replay.unmatched` policy, and flagged). Both continuations are graded. A proposal reaches
  `PENDING_APPROVAL` only if it fixes at least one failing conversation and causes no regressions on
  control conversations. Otherwise it is marked `NOT_IMPROVED`. `replay.mode: single` keeps v0.1's
  one-turn replay.
- **Shadow test**: before approving, compare the live and patched harness on conversations that
  arrived after the proposal was made (`ambientq shadow`, or `shadow.enabled` after every run).

Full design: [`docs/SPEC.md`](docs/SPEC.md). Build plan: [`docs/PLAN.md`](docs/PLAN.md).

## Development

```bash
pip install -e ".[dev]"
ruff check src tests && mypy && pytest
```

## What ships today (v0.2)

- **Grading loop**: OTLP/HTTP receiver (JSON; protobuf with an extra), file ingest (OTLP JSON, simple
  records, LangChain runs) and **AI-gateway log ingest** with presets for LiteLLM, Envoy AI Gateway
  and Kong plus configurable field mappings; deterministic checks on every conversation; a sampled,
  budgeted LLM judge; an evidence verifier; deduplicated insights with regressions counted.
- **Judge calibration**: `ambientq calibrate` (precision, recall, F1, kappa, threshold sweep, sizing)
  with a 40-conversation sample set, and `export-labeling` to label your own traffic.
- **Fixes**: prompt, tool description, tool argument schema, guardrail and agent config patches as
  minimal unified diffs; multi-step replay with recorded tool results; shadow testing on new traffic;
  branches and PRs (GitHub, GitLab, Gitea).
- **Dashboard**: server-rendered, no JavaScript. Optional sign-in by OIDC (authorization code + PKCE)
  or a trusted proxy (oauth2-proxy, Pomerium), roles viewer/approver/admin scoped by team, approve or
  reject next to the diff, replay and shadow results, CSRF and Origin checks, strict security headers.
  Read-only, with no write endpoints, when sign-in is off.
- **Operations**: several agents and teams per install, token usage and cost per agent/model/team,
  Slack/Teams/webhook alerts on new high-severity insights and regressions, monthly compliance
  report (HTML with print CSS, JSON), audit export (JSON lines, CEF, syslog).
- **Packaging**: SQLite or Postgres (both tested in CI), Helm chart with a values schema, Ingress and
  PDB, and a release workflow that pushes a multi-arch image to GHCR with a syft SBOM and cosign keyless
  signature on tags.

## Limits, honestly

- **Not yet run against a live NAI endpoint.** Everything is tested with a rule-based mock model. The
  calibration numbers in the docs are the mock's; real judge quality depends on the model you pick, so
  run `ambientq calibrate` on your own labeled traffic before trusting it.
- **Replay can't replay people.** User turns after the cut point are not simulated, so a fix that
  changes what the agent asks the user is only graded up to that question. A tool call that neither the
  conversation nor the agent's other conversations recorded gets the `replay.unmatched` fallback, which
  is flagged in the report but can still skew the grade.
- **Nutanix publishes no per-request AI-gateway log format** (checked October 2026; see
  `docs/GATEWAY.md`). `--format nai-gateway` is the generic adapter, to be mapped to whatever your
  gateway emits.
- OIDC ID-token signatures are checked only with the `oidc` extra (`cryptography`); without it ambientq
  relies on the direct TLS token exchange, as OIDC Core allows. Groups come from one ID-token claim.
- Insight clustering is fingerprint + word similarity, not embeddings. Alerts are fire-and-forget (no
  retries or digest). Costs are what you configure in `pricing`; ambientq does not read NAI billing.
- The release workflow and Docker image are verified in CI only; the first signed image appears with
  the first `v*` tag.

Apache-2.0.
