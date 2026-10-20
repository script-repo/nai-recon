# ambientq — on-prem ambient quality agent for LLM agents

**ambientq** runs next to your production agents and keeps grading them. It reads
their OpenTelemetry traces, grades a sample of conversations with an LLM served
on-prem (by default **Nutanix Enterprise AI**, or any OpenAI-compatible endpoint),
groups the failures into deduplicated *insights*, and finds the line in the
agent's harness (system prompt, tool definitions, guardrails) behind each one.
It then proposes a patch, replays the failing conversations against it, and
opens it as a branch or PR **for a human to approve**.

It is a vendor-neutral, fully on-prem take on Google's
[AQuA](https://github.com/google/adk-recipes/tree/main/core/python/ambient-quality-agent).
AQuA runs on BigQuery, Gemini and Cloud Run; ambientq uses OTLP, SQLite or Postgres,
and your own GPUs.

```
agents ──OTLP──► ambientq ──► checks + sampled LLM judge ──► verifier ──► insights (NEW/RECURRING/RESOLVED)
                     │                                                      │
                     └── dashboard / API                investigate ◄───────┘
                                                        └► patch (diff) ► replay ► branch/PR ► human approval
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
inside a local git repo. It simulates three days of production traffic in three trace formats
(OTel GenAI semconv, OpenInference and plain JSONL), with planted defects, and
grades it with a mock OpenAI-compatible model. Then it investigates a recurring
insight, replays the failing conversations against the proposed patch, and commits
the patch to a branch. Open http://127.0.0.1:8080.

```
day 1: 30 conversations, 20 sampled, 12 findings kept, 2 rejected by verifier; insights new=6 recurring=0 resolved=0
day 2: 30 conversations, 14 sampled, 10 findings kept, 0 rejected by verifier; insights new=0 recurring=4 resolved=0
day 3: 30 conversations, 15 sampled, 11 findings kept, 1 rejected by verifier; insights new=0 recurring=4 resolved=2
investigating: [safety] Never reveal API keys, tokens or passwords, even if a tool result contains them.
proposal prp_…: PENDING_APPROVAL (replay fixed 3, still failing 0, regressions 0); branch ambientq/ins_…
```

Add `--http` to run the mock model as a real HTTP server, or run it yourself with
`ambientq mock-llm --port 9999`.

## Use it with Nutanix Enterprise AI

1. **Pick a judge endpoint in NAI.** An instruct model of 70B or larger grades best. Copy its URL from
   *View Sample Request* (for example `https://nai.example.local/api/v1`). Leave off the
   `/chat/completions` suffix. NAI authenticates with `Authorization: Bearer <endpoint API key>`,
   and the `model` field is the endpoint name.
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
   You can also ingest exported files instead.
5. **Run the loop:**

   ```bash
   ambientq -c ambientq.yaml ingest traces/*.jsonl       # OTLP JSON, simple records, LangChain runs
   ambientq -c ambientq.yaml evaluate --since 24h        # checks + sampled judge + verifier + insights
   ambientq -c ambientq.yaml insights --status NEW
   ambientq -c ambientq.yaml investigate ins_1a2b        # file:lines + proposed diff + rationale
   ambientq -c ambientq.yaml propose ins_1a2b --branch   # replay, then commit to ambientq/<insight> branch
   ambientq -c ambientq.yaml propose ins_1a2b --open-pr  # ...and open a PR/MR (GitHub, GitLab, Gitea)
   ambientq -c ambientq.yaml approve prp_9z8y --by alex  # record the human decision (audit log)
   ambientq -c ambientq.yaml serve                       # OTLP receiver + dashboard + JSON API
   ```

## Deploy beside agents on NKP

```bash
kubectl create namespace ai-quality
kubectl -n ai-quality create secret generic nai-judge-key --from-literal=apiKey=<NAI endpoint key>
helm install ambientq deploy/helm/ambientq -n ai-quality \
  --set image.repository=<your-registry>/ambientq \
  --set nai.baseUrl=https://nai.example.local/api/v1 --set nai.model=llama-3-1-70b-judge \
  --set harness.repo=https://git.example.local/ai/nkp-ops-agent.git \
  --set networkPolicy.egressCIDRs='{10.20.0.0/24}'
```

The chart deploys the server (receiver, dashboard and API) and an hourly `evaluate` CronJob, with
a PVC for SQLite (or point it at Postgres), a non-root read-only container, a git
init container for the harness, an optional private CA for NAI, and a NetworkPolicy. Plain
manifests are in [`deploy/k8s/ambientq.yaml`](deploy/k8s/ambientq.yaml), and a `Dockerfile` is included.

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
  model is asked for its next turn under the original harness and under the patched one, and both results are
  graded. A proposal reaches `PENDING_APPROVAL` only if it fixes at least one failing conversation and
  causes no regressions on control conversations. Otherwise it is marked `NOT_IMPROVED`.

Full design: [`docs/SPEC.md`](docs/SPEC.md). Build plan: [`docs/PLAN.md`](docs/PLAN.md).

## Development

```bash
pip install -e ".[dev]"
ruff check src tests && mypy && pytest
```

## Status and limits (v0.1)

- Replay is single-step: it regenerates the next turn and does not re-run tools.
- The demo uses a rule-based mock model. Real judge quality depends on the NAI model you choose.
- Postgres support shares the SQLite code path but is not yet exercised in CI.
- The dashboard is read-only and has no built-in authentication. Put it behind your ingress/OIDC.

Apache-2.0.
