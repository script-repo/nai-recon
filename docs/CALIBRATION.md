# Calibrating the judge

ambientq grades conversations with an LLM judge. Before you trust its findings,
or change the model it runs on, measure it against people: label a few dozen
real conversations, run `ambientq calibrate`, and read precision and recall per
defect category. This page covers why, how to build a labeled set from your own
traffic, how to run it against a Nutanix Enterprise AI (NAI) endpoint, how to
read the numbers, and what they can't tell you.

## Why calibrate

* **The judge is a model.** A small model on a GPU you can spare may miss
  defects (low recall) or report defects that aren't there (low precision).
  Both cost something: misses hide real problems, false alarms turn into
  insights and proposed patches nobody needed.
* **It differs per category.** A judge can be excellent at catching leaked
  credentials and poor at spotting unsupported claims. One overall accuracy
  number hides that; calibration reports each category and each rubric item.
* **It sizes the endpoint.** The same run measures the judge's tokens and
  latency per conversation, so you can work out tokens/day and concurrency
  before you reserve GPU capacity on NAI.
* **It sets the knobs.** `judge.min_confidence`, whether to run the verifier,
  and which verifier model to use are decisions you can make from data.

Re-run it whenever the judge model, the verifier model, the judge prompt or the
harness rubric changes.

## Try it on the shipped sample

```bash
ambientq calibrate --sample --base-url mock
```

`--sample` uses `ambientq/demo/calibration/labeled.jsonl` (40 conversations of
the demo `nkp-ops-assistant`, mixed OTLP and plain records) and the demo
harness; `--base-url mock` uses the built-in rule-based mock model, so it runs
anywhere. The labels come from the demo scenarios' truth, by three fictional
labelers, except for three **planted judgement calls**, each with a `note`
that starts `planted ambiguity:`:

| conversation | label | why it disagrees with the scenario |
|---|---|---|
| a `scale_no_confirm` | clean | the user said "bump it to N" outright; the labeler read that as consent, while the harness demands an explicit confirmation step |
| a `docs` | grounding (`G-GROUNDING`) | the cited snippet never mentions GPU flags; the labeler counted "with the GPU flags" as unsupported |
| a `health_ok` | completion (`G-COMPLETION`) | the labeler expected per-node health, not just the cluster summary |

That is what real label sets look like: people disagree with the rubric and
with each other at the edges. Regenerate the file with
`python -m ambientq.demo.calibration` (a test checks it matches the generator).

## The labeled format

JSON Lines, one conversation per line. Each line is any record `ambientq
ingest` understands (a simple record with OpenAI chat messages, or an OTLP
`ExportTraceServiceRequest` holding one conversation) plus a `labels` object:

```json
{"conversation_id": "c-0142", "agent": "nkp-ops-assistant",
 "messages": [{"role": "user", "content": "Bump gpu-pool on prod-east to 8"}, "..."],
 "labels": {"defects": [{"category": "procedure", "rubric_id": "R-P3",
                         "note": "scaled without asking"}],
            "clean": false, "labeler": "alice", "note": ""}}
```

* `defects`: what is wrong, one entry per distinct defect. `category` is one of
  `procedure, output, tool_selection, tool_arguments, tool_results, completion,
  grounding, answer_quality, configuration, safety`. `rubric_id` is optional:
  category-level labels are fine and much faster to produce. Find rubric ids
  with the judge's rubric for your harness (`R-P<n>` prompt rules in order,
  `R-T-<tool>`, guardrail ids, and the generic `G-*` items).
* `clean: true` with no defects means a person checked the conversation and
  found it correct. `clean: false` with no defects means "something is wrong,
  category unknown": it counts for conversation-level agreement only.
* `clean: null` (or missing) with no defects means *not labeled yet*: the line
  is skipped with a note, so you can calibrate on a half-labeled file.
* `labeler`, `note`: free text, shown in the disagreement list.

Bad lines (invalid JSON, unknown category, `clean: true` with defects, a record
holding zero or several conversations, a duplicate conversation id) are listed
with their line numbers and the run stops, so a typo never silently shrinks
your set.

## Label your own traffic

1. **Export a sample** from the store:

   ```bash
   ambientq export-labeling --since 7d --n 50 --out to_label.jsonl
   ```

   This writes 50 conversations from the last 7 days as labeled-format records
   with an empty stub (`"labels": {"defects": [], "clean": null, ...}`).
   Defects are rare in production, so by default half the sample
   (`--flagged-share 0.5`) comes from conversations ambientq already flagged
   and half from the rest; a purely random sample would leave recall nothing to
   measure. `--agent`, `--team` and `--seed` narrow and fix the selection.
   The export contains conversation content: treat it like the traces it came
   from, and keep it inside the cluster.

2. **Label blind.** The export deliberately leaves the judge's findings out:
   labelers who see them anchor on them and agreement looks better than it is.
   Give each labeler the agent's system prompt and tool list (the harness) and
   ask, per conversation: did the agent break a rule? Which category? Which
   rule, if obvious? Two labelers on the same 20 conversations is a cheap way
   to learn how subjective your categories are (if they disagree with each
   other, don't expect the judge to agree with either).

3. **Run calibrate** on the file. Unlabeled lines are skipped, so you can run it
   as you go.

Because the sample is enriched with flagged conversations, precision measured
on it is optimistic compared with random traffic (positives are more common
than in production). Use it to compare models and settings with each other,
and confirm a final choice on a random sample (`--flagged-share 0`).

## Run against an NAI endpoint

NAI serves an OpenAI-compatible API at `https://<nai-host>/api/v1`; the model
name is the endpoint name, and the key is an endpoint API key created in the
NAI console.

```bash
export NAI_API_KEY=...            # never put the key in the config file
ambientq calibrate to_label.jsonl \
  --base-url https://<nai-host>/api/v1 \
  --model <judge-endpoint-name> \
  --api-key-env NAI_API_KEY \
  --verifier-model <verifier-endpoint-name> \
  --out calibration-$(date +%F).json
```

Without `--base-url` / `--model`, the config's `llm` section is used. A cluster
with a private CA needs the CA bundle in the config:

```yaml
llm:
  base_url: https://<nai-host>/api/v1
  model: <judge-endpoint-name>
  api_key_env: NAI_API_KEY
  ca_bundle: /etc/ambientq/nai-ca.pem
```

Other options: `--no-verify` scores the raw judge (no evidence check, no
verifier) so you can see what the verifier buys; `--with-checks` also counts the
deterministic checks' findings, i.e. what `evaluate` would report;
`--min-confidence` sets the threshold for the headline numbers (default
`judge.min_confidence`); `--per-day` and `--rate` feed the sizing estimate
(default 1000 conversations/day at `sampling.rate`); `--json` prints the full
report. Every run writes a `calibrated` audit event with the headline numbers
(never conversation content), and the judge's calls are recorded as usage
like any other ambientq run.

## Read the numbers

All counts are at **conversation level**: for category C, a true positive is a
conversation where a person labeled a C defect and the judge reported at least
one verified C finding (at or above the threshold).

* **Precision** (of the conversations the judge flagged for C, how many really
  had a C defect). Low precision means noise: insights nobody needs, patches
  that fix nothing. Raise `judge.min_confidence` (use the sweep), keep the
  verifier on, or give the verifier a larger model than the judge
  (`llm.verifier_model`); verification is one short call per finding, so a big
  verifier is cheap compared with a big judge.
* **Recall** (of the conversations people labeled C, how many the judge
  caught). Low recall means blind spots. Use a bigger judge model, lower the
  threshold, or make the harness rule more specific: the rubric is built from
  the prompt's normative lines, and a rule without "must / never / always /
  before / only ..." is not a rubric item at all.
* **F1** balances the two; **support** is how many conversations people
  labeled with C. A category with support 3 tells you very little (see limits).
* **Agreement and Cohen's kappa** compare "the judge flagged this conversation"
  with "a person flagged it". Kappa corrects for chance: with mostly clean
  traffic, a judge that never flags anything still agrees 90% of the time, but
  its kappa is 0. Rough reading: above 0.8 strong, 0.6-0.8 substantial,
  0.4-0.6 moderate, below 0.4 do not automate on it.
* **Per rubric item** uses only conversations whose labels name rubric items
  for every defect (or are clean), so a category-only label never counts as a
  miss for a specific rule.
* **The min_confidence sweep** recomputes micro precision/recall/F1 (all
  categories pooled) at thresholds 0.0-0.9 from one judge pass, and recommends
  the best-F1 threshold (ties go to higher precision, then the lower
  threshold). Set `judge.min_confidence` to it, or above it if false alarms
  cost you more than misses.
* **Disagreements** list every conversation where the judge and the label
  differ (the flag or any category), with the label, the note and every judge
  finding, including the ones the verifier rejected or the threshold dropped.
  Read them: half the time the label is what needs fixing.
* **Sizing**: mean/p50/p95 prompt and completion tokens and latency per judged
  conversation, verifier calls and tokens, and `tokens/day = conversations/day
  x sampling rate x tokens per conversation`. `min_concurrency` is how many
  requests must run in parallel to keep up if each takes the measured latency.
  Measure on the endpoint you will use: latency on a shared or cold endpoint
  can be several times the steady state.

## Worked example (the mock's real output)

`ambientq calibrate --sample --base-url mock`:

```
judge calibration: 40 labeled conversation(s), 40 judged; model judge, verifier judge, min_confidence 0.50

conversation level (judge flags iff a human did): agreement 0.93 (37/40), Cohen's kappa 0.85
  human flagged: judge flagged  16, judge clean   2
  human clean:   judge flagged   1, judge clean  21

per category               tp   fp   fn   tn  precision            recall                   f1
  procedure                 8    1    0   31    0.89 [0.56-0.98]     1.00 [0.68-1.00]     0.94
  tool_results              5    0    0   35    1.00 [0.57-1.00]     1.00 [0.57-1.00]     1.00
  completion                0    0    1   39     -   [0.00-1.00]     0.00 [0.00-0.79]      -
  grounding                 0    0    7   33     -   [0.00-1.00]     0.00 [0.00-0.35]      -
  safety                    3    0    0   37    1.00 [0.44-1.00]     1.00 [0.44-1.00]     1.00
  micro                    16    1    8  175    0.94 [0.73-0.99]     0.67 [0.47-0.82]     0.78
...
judge per conversation: prompt tokens mean 1875 (p95 1944), completion mean 46 (p95 87); ...
sizing: 1,000 conversations/day x sampling 20% = 200 judged/day -> ~411,685 tokens/day (2,058 per conversation), ...
```

How to read it:

* **Conversation level is strong** (kappa 0.85). The three misses are exactly
  the planted judgement calls: one false alarm (the "bump it" scale a labeler
  called clean) and two conversations only a person flagged (the docs answer
  and the per-node health expectation).
* **Safety and tool results are perfect, but look at the intervals.** Safety
  has support 3, so recall 1.00 comes with a 95% interval of 0.44-1.00. Three
  conversations prove little; label more leaks before you rely on it.
* **Grounding recall is 0.** Every `health_no_status` conversation is labeled
  with both the missing status call (`procedure`, R-P1) and the invented
  version and node count (`grounding`, R-P2). The judge is told to report a
  root cause once, so it reports R-P1 only. Either decision is defensible;
  calibration makes you choose: label root causes only, or change the judge
  prompt to report consequences too. This is the most common lesson from a
  first calibration: the categories need a shared definition.
* **The sweep is flat at 0.94 precision** because the verifier already rejects
  the mock's one low-confidence (0.6) unsupported claim about the docs. Run the
  same set with `--no-verify` and the sweep shows what the verifier was doing:

  ```
  min_confidence sweep    precision  recall      f1  findings
    0.00 ... 0.60          0.74    0.71    0.72        23
    0.70                   0.94    0.67    0.78        17  <- recommended
  ```

  Without the verifier, five clean docs conversations are flagged for
  grounding (precision 0.74); raising `judge.min_confidence` to 0.7 removes
  them at the cost of one lucky hit. With the verifier on, keep the default
  0.5. (The mock's latency is near zero; a real endpoint's is what sizes it.)

## Honest limits

* **Labels are opinions.** The judge can't agree with people more than people
  agree with each other. Measure that once (two labelers, same conversations)
  and write category definitions down.
* **Small sets have wide intervals.** Every precision and recall comes with a
  95% Wilson score interval: with 5 positives a perfect recall is only "at
  least ~0.57". Read the interval, not the point estimate, and prefer 20+
  labeled positives in any category you will act on. Kappa on 40 conversations
  is similarly rough (about +/-0.15).
* **Conversation level, not finding level.** Two separate procedure defects in
  one conversation count once; a finding with the right category but the wrong
  turn still counts as a hit. Use the disagreement list and the rubric-level
  table for finer reading.
* **The sample decides what you measured.** Enriched samples overstate
  precision; a week of traffic misses rare failure modes; a judge calibrated on
  one agent is not calibrated on another.
* **The mock is not a model.** The sample run shows the tooling; only a run on
  your endpoint, with your labels, says anything about your judge.
