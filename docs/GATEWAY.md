# Gateway ingest

ambientq can grade an agent's traffic from the logs of the OpenAI-compatible
gateway in front of the model, with no change to the agent's code. This page
records what the gateways in front of Nutanix Enterprise AI (NAI) publish
(research done **October 2026**), what the adapter assumes, and how to configure it.

## Research summary (Oct 2026)

### Nutanix Enterprise AI and the Nutanix Agent Gateway

**Short answer: Nutanix does not publicly document a per-request log, audit
log or trace export format for LLM traffic through NAI or its Agent Gateway.**
Here is what the public sources do say:

| What | Documented? | Source |
|---|---|---|
| **Nutanix Agent Gateway**: GA in NAI 2.7 (announced 26 May 2026). The NAI 2.7 post ties it to the earlier "AI Gateway" / "Unified Endpoints" feature. One OpenAI-compatible endpoint for self-hosted and public models, with auth, token rate limits, failover and "unified observability" of token use | Product announcement only. No log schema | [Nutanix blog, 2026-05-26](https://www.nutanix.com/blog/introducing-nutanix-agent-gateway), [NAI 2.7 community post](https://next.nutanix.com/nutanix-agentic-ai-180/nutanix-enterprise-ai-nai-2-7-is-now-available-45620) |
| MCP requests: "Record every MCP request with a comprehensive audit trail"; 2.7: "Every request is logged, audited, and rate-limited" | Yes, as a statement. No format or export path given | same |
| NAI 2.8 (26 Aug 2026): MCP gateway GA, downloadable MCP server logs in the console, header-based rate limiting (Tech Preview) that identifies clients "by API client key or request header" | Console features. No export format | [NAI 2.8 product update](https://next.nutanix.com/product-updates/nutanix-enterprise-ai-2-8-45849) |
| **Audit log API** (NAI 2.5): `POST /api/enterpriseai/v1/auditlogs` returns `{createdAt, description, entityAffected, entityType, eventType, id, userName}`. These are **admin/config events** (e.g. a user was created), not inference requests | Yes | [NAI 2.5 API reference](https://www.nutanix.dev/api_reference/apis/nai2.5.html) |
| Remote syslog: `cluster/config` → `rsyslogServer {serverAddress, port, protocol, logConfigs[{logType: "audit", logSeverity}]}`. Ships the audit log above | Yes | same |
| Endpoint logs: `GET /api/enterpriseai/v1/logs` (`data.logs[].message`, tail lines) and `/logs/download`. These are inference-server container logs (plain text), not structured request/response records | Yes | same |
| Metrics: `POST /api/enterpriseai/v1/metrics/query` (`requestCount`, `inputTokenCount`, latency, TTFT, GPU …); NAI 2.3+ can export metrics to an OpenTelemetry collector (vLLM/NIM, API server, DCGM, node exporter) | Yes, **metrics only**. No per-request content, no session id | [NAI 2.3 blog](https://www.nutanix.com/tech-center/blog/introducing-nutanix-enterprise-ai-23), API reference |
| "OpenInference distributed tracing and OpenTelemetry GenAI semantic conventions" for the Agent Gateway | **Trade press only** (one article, 2026-07-03). Not in any Nutanix doc we could read. Treat as unconfirmed | [Security Storage und Channel Germany](https://security-storage-und-channel-germany.de/language/en/nutanix-launches-agent-gateway-to-tame-ai-costs-and-governance/) |
| Built on Envoy AI Gateway? | **Unconfirmed**. A press article was only tagged "Envoy AI Gateway"; Nutanix doesn't say | press |

The portal.nutanix.com documentation pages (NAI 2.6+ admin guide, Helm
parameters) need JavaScript or a login and could not be read for this research.
The 2.6 `nai-core` chart is known (from search snippets) to ship an
OpenTelemetry collector (`naiMonitoring.opentelemetry`, image
`nutanix/nai-opentelemetry-collector-contrib`) and an
`rsyslogAuditLogsExport` component. Neither is documented as exporting
request/response logs. **Re-check this when a live NAI 2.7+ cluster is at hand.**

**What the adapter therefore assumes:** `--format nai-gateway` is an **alias
of the generic preset**. If an NAI cluster turns out to export OTLP traces with
GenAI or OpenInference attributes (as the press claims), send those to
`POST /v1/traces` / `ambientq ingest` (auto/otlp), which already understands
both conventions. Don't use the proxy-log adapter for that. If it exports a
JSON request log, map its fields with `gateway.mappings` (no code needed) and
let us know so a real `nai-gateway` preset can replace the alias.

### OpenAI-compatible proxies people put in front of NAI

**LiteLLM proxy** ([logging spec](https://docs.litellm.ai/docs/proxy/logging_spec),
[request headers](https://docs.litellm.ai/docs/proxy/request_headers)).

* `StandardLoggingPayload` (what logging callbacks / custom loggers / S3 / GCS buckets receive):
  `id`, `trace_id`, `session_id`, `call_type`, `model`, `model_group`, `api_base`,
  `status` (`success`/`failure`), `prompt_tokens`, `completion_tokens`, `startTime`,
  `endTime` (epoch floats), `messages` (the request messages), `response` (the
  ChatCompletion object), `error_str`, `end_user`, and `metadata`
  (`user_api_key_alias`, `user_api_key_team_alias`, `user_api_key_user_id`,
  `requester_custom_headers` = the client's `x-` headers, …).
* `LiteLLM_SpendLogs` rows (DB / `GET /spend/logs`): `request_id`, `call_type`,
  `model`, `model_group`, `startTime`/`endTime` (timestamps), `user`, `team_id`,
  `end_user`, `metadata`, `messages`, `response`, `proxy_server_request`,
  `session_id`, `status`, token counts. Depending on the LiteLLM version the prompt
  sits in `messages` or only in `proxy_server_request` (issue #23636, fixed by PR
  #34793, Jul 2026). The preset reads both.
* Sessions: `x-litellm-trace-id` / `x-litellm-session-id` headers, or
  `litellm_session_id` / `metadata.session_id` in the body, end up in
  `session_id`. **When the client sends none, LiteLLM fills `session_id` with a
  fresh UUID per request**, which would make every call its own conversation.
  Several 2026 bugs also lost the caller's id (#40851, #40379, #43540). The
  preset therefore tries the agent's own header
  (`metadata.requester_custom_headers.x-session-id`) and `metadata.session_id`
  before the `session_id` column. If your agent sends no session id at all, set
  `gateway.mappings: {"=session": []}` so the fallback hash groups calls.

**Envoy AI Gateway**, now "Agent Router", an Agentic AI Foundation project; docs v1.2
([access logs](https://theagentrouter.ai/docs/capabilities/observability/accesslogs/),
[tracing](https://theagentrouter.ai/docs/capabilities/observability/tracing/)).

* The documented JSON access log has flat dotted keys:
  `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.provider.name`,
  `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens`, `start_time`,
  `duration` (ms), `response_code`, `x-request-id`, … (MCP variant: `mcp.session.id`,
  `mcp.method.name`, `mcp.tool.name`). **It has no request or response bodies**,
  so access logs alone can't be graded. The adapter skips such records and
  says so.
* Tracing is where the content is: OTLP spans in **OpenInference** (default,
  full request/response, `OPENINFERENCE_HIDE_*` to redact) or **OTel GenAI**
  (`AI_GATEWAY_TRACING_SEMCONV=gen_ai`, v1.1+; content needs
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`). The request header
  `agent-session-id` is mapped to `session.id` on spans and logs by default
  (`controller.spanRequestHeaderAttributes` / `logRequestHeaderAttributes`).
  **Recommended:** point the gateway's OTLP exporter at ambientq's
  `/v1/traces`. Both conventions are ingested as-is.
* The `envoy-ai-gateway` preset is for access logs to which you have added the
  bodies yourself (`request_body` / `response_body`, e.g. via a Lua or ext_proc
  filter) and a `session.id` / `agent-session-id` / `x-session-id` field.

**Kong AI Gateway** ([AI audit log reference](https://developer.konghq.com/ai-gateway/ai-audit-log-reference/)).
With `config.logging.log_statistics` and `log_payloads` on, any Kong log plugin
(http-log, file-log, …) writes `ai.proxy.payload.request` / `.response` (the
bodies, as JSON strings), `ai.proxy.usage.prompt_tokens` / `completion_tokens`,
`ai.proxy.meta.request_model` / `response_model` / `provider_name` /
`llm_latency`, plus Kong's usual `request.headers`, `response.status`,
`started_at` (epoch ms), `latencies.request` and `consumer`. There is no session
id unless the client sends a header. The `kong` preset reads `x-session-id` /
`agent-session-id`.

## How the adapter works

`ambientq ingest --format {auto,otlp,record,langchain,proxy-log,litellm,envoy-ai-gateway,kong,nai-gateway} FILE...`,
`POST /v1/gateway/logs`, and `ambientq.ingest.gateway` in code.

* **Input:** one JSON object per request/response pair, as JSON lines or a JSON
  array (POST bodies may also be gzip and `application/x-ndjson`).
* **Output:** one OTel span per pair: `gen_ai.operation.name=chat`,
  `gen_ai.conversation.id`, `gen_ai.agent.name`, `gen_ai.request.model`,
  `gen_ai.response.model`, `gen_ai.input.messages` (the request's messages, OpenAI
  chat format), `gen_ai.output.messages` (the first response choice, `tool_calls`
  included), `gen_ai.usage.input_tokens` / `output_tokens`, `ambientq.team`,
  `ambientq.source=gateway`, status `ERROR` + `error.type` on failure (`status`
  `failure`/`error` or ≥ 400, or an `error` field). Span ids are stable hashes,
  so re-ingesting a log is idempotent.
* The rest of the pipeline is unchanged: the history re-sent on every call is
  de-duplicated by `conversation.build_conversation`. A tool call in one response
  and its result in the next request become an assistant tool-call turn and a
  `tool` turn, so checks, the judge and multi-step replay's tool-result tape work
  on gateway traffic too.
* **Tool errors:** OpenAI tool messages carry no error flag. A tool result whose
  JSON body has a top-level `error` key is marked `is_error` (this needed a
  two-line pass-through in `normalize.message_from_any` and
  `conversation.build_conversation`).
* **Conversation id:** the first non-empty `session` path (`gateway.session_fields`
  first, then `gateway.mappings.session`, then the preset's). If there is none,
  `gw-` + a hash of (agent, system prompt, first user message). That is stable as
  long as the agent re-sends the history, but two conversations that open
  with the same message from the same agent and system prompt merge. Send a
  session header if you can.
* **Agent:** a configured field or header (`x-agent-name`), else the user /
  API key alias, else `unknown`. **Team:** an optional field → `ambientq.team`.
* `auto` treats a record as a proxy log only if it has `response` with
  `request`/`messages`/`proxy_server_request`, or `request_body`+`response_body`,
  or `choices`+`messages`, or Kong's `ai.proxy.payload`. OTLP exports, simple
  `{conversation_id, messages}` records and LangChain runs are still recognized
  as before. The preset is then detected per record (Kong, LiteLLM, Envoy, else
  generic). Metadata-only access logs are only read with an explicit `--format`.

### Field mappings

Logical fields: `request`, `response`, `messages`, `model`, `response_model`,
`session`, `agent`, `team`, `start_time`, `end_time`, `duration_ms`, `usage_in`,
`usage_out`, `status`, `error`, `request_id`. Each preset maps each one to a list
of dotted paths, tried in order (`src/ambientq/ingest/gateway.py: PRESETS`).
Paths:

* `a.b.0.c`: dict keys and list indexes. Keys match case-insensitively (headers).
* A key may itself contain dots (`gen_ai.usage.input_tokens` in Envoy logs).
  The longest matching key wins.
* A JSON string met on the way is parsed (Kong's payloads).
* Times may be epoch s/ms/µs/ns or ISO-8601. `duration_ms` is used when there is no end time.

`gateway.mappings` (per field, one path or a list) are tried **before** the
preset's paths for that field. Write the key as `"=field"` to replace them.
`gateway.session_fields` go first of all. See
[`examples/gateway/ambientq-gateway.yaml`](../examples/gateway/ambientq-gateway.yaml)
and the sample logs next to it.

### HTTP log sink

`POST /v1/gateway/logs` takes the same bearer token as `/v1/traces`
(`server.ingest_token_env`). The body is a JSON array, one object, or JSON lines
(optionally gzip). The response is `{records, spans, conversations, skipped, errors}`.
The preset is detected per record and the server's `gateway` config section
applies. LiteLLM's generic API logger / webhook callbacks, Kong's http-log and
any gateway with an HTTP log sink can post here.

### Demo

`ambientq.demo.synthetic.generate_proxy_logs(day, n, seed, style="litellm"|"generic")`
emits the demo scenarios as gateway logs (one entry per LLM call, history
re-sent, session id in `metadata` and `x-session-id`), with the same
conversation ids and scenario truth as `generate(day, n, seed)`.
