# ambientq: a 10-minute SE demo

A script for showing ambientq to a customer running (or evaluating) Nutanix
Enterprise AI. It runs on a laptop with no cluster, no GPU and no network: the
demo uses a built-in mock model that behaves like an NAI endpoint.

**The story in one line:** your agents are in production; ambientq watches
every conversation on-prem, finds the recurring mistakes, traces each one to
the line in the agent's prompt or tools that causes it, proves a fix works on
real traffic, and waits for a human to approve it, with an audit trail your
compliance team can use.

## Before the meeting (5 minutes, once)

```bash
git clone -b ambientq https://github.com/script-repo/nai-recon.git ambientq && cd ambientq
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
ambientq demo --workdir ./demo-work          # dry run: about 10 seconds
```

To press **Approve** in the browser yourself (step 5), install a header
extension (ModHeader or similar) and set `X-Forwarded-Email: se@example.com`
for `127.0.0.1:8080`. Step 2 turns on trusted-proxy sign-in for the demo, and
only requests from `auth.proxy.trusted_proxies` (localhost by default) are
believed. In production the proxy is oauth2-proxy or your ingress, never a
browser extension.

## The demo

### 1. Set the scene (1 min)

> "You have an ops assistant on NKP. It answers questions about clusters and can
> scale node pools. It's been live for a few days. How do you know it's doing
> the right thing, on every conversation, without sending transcripts to a
> SaaS eval tool?"

Show `demo-work/agent-repo/harness/`: `prompts/system.md` (six rules),
`tools/tools.yaml` (four tools), `guardrails.yaml`. This is the agent's
"harness": everything you can change without retraining a model.

### 2. Run the loop (1 min)

```bash
rm -rf demo-work && ambientq demo --workdir ./demo-work
cat >> demo-work/ambientq.yaml <<'YAML'
auth:
  mode: proxy                  # identity from a trusted proxy: here, your browser on localhost
  cookie_insecure: true
  roles: {approver: [se@example.com], admin: [se@example.com], viewer: ["*"]}
YAML
ambientq -c demo-work/ambientq.yaml serve
```

(Skip the `auth` block to show the default read-only dashboard.)

Narrate the output as it scrolls:

- **Days 1–2 arrive as OpenTelemetry traces, day 3 as AI-gateway logs.** "No
  agent changes needed: if your traffic goes through LiteLLM, Envoy AI Gateway,
  Kong or any OpenAI-compatible proxy, ambientq reads its logs."
- **Sampled, not everything.** "Deterministic checks run on every conversation
  for free; the LLM judge sees a risk-weighted sample, within a token budget."
- **Rejected by verifier.** "Every finding must quote the transcript. Claims the
  judge can't back up are thrown out before anyone sees them."
- **resolved=2 on day 3.** "This defect stopped happening, and ambientq noticed."

### 3. Insights (2 min)

Open http://127.0.0.1:8080.

- The cards: conversations, new / recurring / resolved, regressions, awaiting approval.
- Click **"Always call get_cluster_status before answering…"**. Point at the
  rule, file and line in the header: "it doesn't just say *the agent is wrong*,
  it says *which line of your prompt it's ignoring*."
- Click a conversation in **Findings**: the flagged turn is highlighted. "Here
  the agent said the cluster was healthy without checking."

### 4. The proposed fix (3 min)

Go to **Proposals** and open the **prompt** proposal.

- **Change** (left): a one-line diff to `system.md`. "A normal git branch in
  the agent's repo; ambientq never merges or deploys anything."
- **Replay** (right): "We re-ran the failing conversations with the patched
  prompt, multi-step: the agent's own model, with the *recorded* tool results
  played back. 3 fixed, 0 still failing, 0 regressions on conversations that
  were fine."
- **Shadow test**: "Then we ran it on 15 conversations that arrived *after* the
  fix was written: findings went from 13 to 5 compared with the live prompt."
- **Replies before and after**: the agent now calls the tool before answering.
- Back on **Proposals**, open the **guardrail** proposal: "fixes aren't only
  prompts: tool descriptions, tool argument schemas, guardrails and agent
  config too, each validated before it's proposed."

### 5. Human approval and audit (2 min)

On the guardrail proposal (still pending), add a note ("CHG-1042") and press
**Approve** (needs the sign-in setup above; otherwise run
`ambientq -c demo-work/ambientq.yaml approve <id> --by se@example.com`).

- Open **Audit log**: the approval with who, when, the note and `via:
  dashboard`; sign-ins; every LLM call with model and token counts but no
  prompt content (a hash only).
- "Approvers only see their teams; viewers can't approve; every form is
  CSRF-protected."

### 6. Cost and compliance (1 min)

- **Usage & cost**: tokens per agent and model, production traffic and
  ambientq's own judge calls separately, priced with your numbers (GPU cost
  as a price per 1k tokens).
- **Reports** → this month → HTML: print to PDF. "This is the monthly evidence
  pack for your AI governance board: what was graded, what was found, how fast
  it was fixed, who approved what."
- Mention: `ambientq audit-export --format cef` for the SIEM, Slack/Teams alerts
  on new high-severity issues and regressions.

## Closing (30 s)

> "All of this ran on this laptop. In your environment it's one Helm chart on
> NKP next to your agents, grading with a model you serve on NAI. Nothing leaves
> the cluster: the NetworkPolicy allows only your NAI endpoint and your git server."

Next step to offer: label 50–100 real conversations and run `ambientq
calibrate` against their NAI endpoint, to pick the judge model size with
numbers ([`CALIBRATION.md`](CALIBRATION.md)).

## Questions you'll get

- **"Which model judges?"** Any OpenAI-compatible endpoint; on NAI an
  instruct model of 70B or larger. Calibrate to choose.
- **"Does it change my agent?"** Never automatically. It opens a branch or PR;
  your normal change process merges it.
- **"What does it cost to run?"** Checks are free; the judge runs on a sample
  (default 20%) within a per-run token budget. The usage page shows the real
  figure, and `calibrate` estimates tokens per day before you start.
- **"What about the Nutanix AI gateway?"** Nutanix doesn't publish a
  per-request log format yet; the generic proxy-log adapter maps whatever it
  emits ([`GATEWAY.md`](GATEWAY.md)).
- **"Is the demo model real?"** No, it's a rule-based mock so the demo is
  deterministic and offline. Say so.
