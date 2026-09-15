import { STAGES, questionById, visibleQuestions } from "@/lib/playbook";
import type { Engagement } from "@/lib/store";
import {
  collectTags,
  computeScores,
  normalizedScores,
  opportunityScore,
  recommendedMotion,
  optionLabels,
  hasTag,
  type Motion,
  type Scores,
} from "@/lib/scoring";

export type ReportModel = {
  customer: string;
  seName: string;
  opportunity: number;
  motion: Motion;
  motionTitle: string;
  scores: Scores;
  tags: string[];
  estate: string[];
  risks: string[];
  inferenceLead: string[];
  gatewayLead: string[];
  poc: { title: string; steps: string[]; success: string[] };
  talkTrack: string[];
  nextMeeting: string[];
  gaps: { stage: string; prompt: string }[];
  objections: { objection: string; reply: string }[];
};

function motionTitle(motion: Motion) {
  if (motion === "gateway") return "Lead with Nutanix Agent Gateway";
  if (motion === "inference") return "Lead with Nutanix private inference";
  return "Dual motion: private inference + Agent Gateway";
}

export function buildReport(engagement: Engagement): ReportModel {
  const tags = collectTags(engagement);
  const scores = normalizedScores(computeScores(engagement));
  const motion = recommendedMotion(scores);
  const customer = engagement.customerName || "the customer";

  const estate: string[] = [];
  const providers = optionLabels(engagement, "inference.providers");
  if (providers.length) estate.push(`Model providers in use: ${providers.join("; ")}.`);
  const shadow = optionLabels(engagement, "shadow.tools");
  if (shadow.length) estate.push(`Consumer / desktop AI observed: ${shadow.join("; ")}.`);
  const bots = optionLabels(engagement, "chatbots.prod");
  if (bots.length) estate.push(`Assistant posture: ${bots.join("; ")}.`);
  const rag = optionLabels(engagement, "rag.exists");
  if (rag.length) estate.push(`Knowledge / RAG: ${rag.join("; ")}.`);
  const sources = optionLabels(engagement, "rag.sources");
  if (sources.length) estate.push(`Knowledge sources: ${sources.join("; ")}.`);
  const agents = optionLabels(engagement, "agents.maturity");
  if (agents.length) estate.push(`Agentic maturity: ${agents.join("; ")}.`);
  const mcp = optionLabels(engagement, "agents.mcp");
  if (mcp.length) estate.push(`Tool / MCP posture: ${mcp.join("; ")}.`);
  const gpu = optionLabels(engagement, "inference.gpu");
  if (gpu.length) estate.push(`GPU reality: ${gpu.join("; ")}.`);
  const spend = optionLabels(engagement, "inference.spend");
  if (spend.length) estate.push(`Spend visibility: ${spend.join("; ")}.`);
  if (engagement.nutanixFootprint.length) {
    estate.push(
      `Nutanix already in account: ${engagement.nutanixFootprint.join(", ") || "unspecified"}.`,
    );
  }
  if (estate.length === 0) {
    estate.push("Estate still being mapped — complete more stages for a sharper inventory.");
  }

  const risks: string[] = [];
  if (hasTag(tags, "shadow-ai", "bypass", "no-path", "blocked-no-alt")) {
    risks.push(
      `Shadow AI is already the default path. Blocking consumer tools without a sanctioned alternative will not hold. ${customer} needs a front door that is as easy as ChatGPT and actually governed.`,
    );
  }
  if (hasTag(tags, "incident", "phi-pci", "sensitive-data")) {
    risks.push(
      "Sensitive or regulated data is in the blast radius of current inference paths. Prompt logs at a public provider are a reportable-class problem, not a theoretical one.",
    );
  }
  if (hasTag(tags, "mcp", "no-mcp-governance", "shared-keys", "runaway")) {
    risks.push(
      "Agents can reach tools without a control plane — shared keys, MCP sprawl, or a runaway already in the folklore. This is a CISO finding whether or not they have used the word MCP.",
    );
  }
  if (hasTag(tags, "token-blind")) {
    risks.push(
      "Nobody can attribute token usage or tool calls by team, app, or agent. The next bill-shock will not have a post-mortem. It will have a guess.",
    );
  }
  if (hasTag(tags, "no-policy", "policy-unenforced")) {
    risks.push(
      "AI policy is documentation, not enforcement. There is no technical stop for the wrong model, the wrong tool, or the wrong data.",
    );
  }
  if (risks.length === 0) {
    risks.push(
      "No single screaming incident yet — which is the cheap time to put a control plane in. Waiting for the first rogue agent or paste event is how this conversation gets more expensive.",
    );
  }

  const inferenceLead: string[] = [];
  inferenceLead.push(
    `Nutanix Enterprise AI turns ${hasTag(tags, "nutanix-nkp", "fp-nkp") ? "existing NKP" : "Nutanix infrastructure"} into Model-as-a-Service: catalog of Hugging Face and NVIDIA NIM models, endpoint management, API keys, RBAC, and preflight testing.`,
  );
  if (hasTag(tags, "sensitive-data", "phi-pci", "airgap", "regulated")) {
    inferenceLead.push(
      "Private inference keeps prompts, KV-cache, and logs inside the perimeter. Dark-site deployment and air-gapped NIM exist for environments that cannot touch public model hubs.",
    );
  }
  if (hasTag(tags, "high-spend", "mid-spend", "bill-shock")) {
    inferenceLead.push(
      "Repetitive and internal workloads do not need a frontier API. Shift them to open-weight models on NAI so spend becomes infrastructure, not a metered surprise. Keep frontier models for the tasks that actually need them — through the same Gateway.",
    );
  }
  if (hasTag(tags, "gpu-idle", "gpu-onprem", "gpu-planned")) {
    inferenceLead.push(
      "GPUs without a productized inference plane become science projects. NAI is how those GPUs become an internal model service — including multi-GPU inference, speculative decoding, and LoRA fine-tunes on smaller models.",
    );
  }
  if (hasTag(tags, "rag", "data-gravity", "nutanix-files", "nutanix-objects", "fp-files", "fp-objects")) {
    inferenceLead.push(
      "The corpus already has gravity. Run the generator next to Nutanix Files and Objects instead of shipping chunks to a public completion API.",
    );
  }
  if (hasTag(tags, "self-hosted", "vllm", "ollama", "nim", "eval-diy")) {
    inferenceLead.push(
      "They already believe in private models. DIY vLLM/Ollama is the toil. NAI is the operating model: catalog, tenancy, keys, and a gateway in front so the science project becomes a platform.",
    );
  }

  const gatewayLead: string[] = [];
  gatewayLead.push(
    "Nutanix Agent Gateway is the control layer between agents, LLMs, and MCP tools — public hosted models and NAI private models behind one OpenAI-compatible API.",
  );
  if (hasTag(tags, "mcp", "no-mcp-governance", "tools-no-mcp")) {
    gatewayLead.push(
      "MCP governance is generally available: local MCP servers inside NAI, remote MCP, tool-level filtering (read vs write), and an audit trail of every MCP request. This is how a CISO sleeps once agents can touch GitHub, Stripe, or a database.",
    );
  }
  if (hasTag(tags, "shadow-ai", "bypass", "blocked-no-alt", "no-path")) {
    gatewayLead.push(
      "Give builders a sanctioned path that is faster than the workaround. SSO-backed keys, quotas, and the right model for the task beat a proxy block page every time.",
    );
  }
  if (hasTag(tags, "token-blind", "bill-shock", "runaway", "high-spend")) {
    gatewayLead.push(
      "Token-based rate limits per user, team, or agent, plus unified observability. The looping agent becomes a quota event, not a finance incident.",
    );
  }
  gatewayLead.push(
    "Fallback and unified endpoints: if Azure OpenAI throttles, traffic can fail over to a private NIM or another provider without rewriting the app. That is production, not a demo.",
  );

  const poc = buildPoc(customer, tags, motion);
  const talkTrack = buildTalkTrack(customer, tags, motion);
  const nextMeeting = [
    "Walk the estate map back to them in ten minutes. Confirm what you heard; do not add new product yet.",
    motion === "gateway"
      ? "Show Agent Gateway: one endpoint, two models (public + private), a quota, and an MCP tool filtered to read-only."
      : motion === "inference"
        ? "Show NAI: deploy one NIM or Hugging Face model on their Nutanix, hit it with an existing app's OpenAI SDK."
        : "Show the hybrid: one Gateway URL, a private model for the sensitive corpus, a public model for the rest, with failover.",
    "Leave behind a one-page POC plan with success criteria they can forward to the CISO and platform owner.",
    "Name the executive sponsor and the technical owner before you leave the room.",
  ];

  const gaps: { stage: string; prompt: string }[] = [];
  const visible = visibleQuestions(engagement.depth, engagement.answers);
  for (const q of visible) {
    const a = engagement.answers[q.id];
    if (!a || (a.optionIds.length === 0 && !a.skipped)) {
      const stage = STAGES.find((s) => s.id === q.stageId);
      gaps.push({ stage: stage?.title ?? q.stageId, prompt: q.prompt });
    }
  }

  const objections = [
    {
      objection: "We already have Azure OpenAI / Bedrock.",
      reply:
        "Keep it. Agent Gateway sits in front so builders use one API, you get quotas and failover, and sensitive or repetitive work can move to a private NIM without a rewrite. This is hybrid control, not a rip-and-replace.",
    },
    {
      objection: "We can just run vLLM on Kubernetes ourselves.",
      reply:
        "You can. The question is whether you also want catalog, RBAC, API keys, MCP governance, token attribution, and model scanning as a product — or as a two-year platform team. NAI is that product on the Nutanix you already operate.",
    },
    {
      objection: "We are not ready for agents.",
      reply:
        "The users already are. Shadow copilots and LoB bots are agents with worse identity. Gateway plus a sanctioned private model is how you get ready without pretending you can pause the market.",
    },
    {
      objection: "We do not have GPUs yet.",
      reply:
        "Start with Agent Gateway in front of the public models they already pay for — visibility and control this quarter. Land private inference when the GPUs arrive so the software plane is not a science project on day one of the hardware.",
    },
  ];

  return {
    customer,
    seName: engagement.seName || "Channel SE",
    opportunity: opportunityScore(scores),
    motion,
    motionTitle: motionTitle(motion),
    scores,
    tags,
    estate,
    risks,
    inferenceLead,
    gatewayLead,
    poc,
    talkTrack,
    nextMeeting,
    gaps,
    objections,
  };
}

function buildPoc(customer: string, tags: string[], motion: Motion) {
  const steps: string[] = [];
  if (motion !== "gateway") {
    steps.push(
      `Stand up NAI on existing ${hasTag(tags, "fp-nkp", "nutanix-nkp") ? "NKP" : "Nutanix"} capacity and deploy one approved model (NVIDIA NIM or Hugging Face) as a private endpoint.`,
    );
  }
  steps.push(
    "Place Agent Gateway in front of that endpoint and one public provider they already use. Same SDK, two backends, token quota on the key.",
  );
  if (hasTag(tags, "mcp", "tools-no-mcp", "chatbot-prod", "agents-prod", "agents-poc")) {
    steps.push(
      "Onboard one existing bot or agent. Attach one MCP or tool (read-only) through the Gateway. Show the audit log of the tool call.",
    );
  } else {
    steps.push(
      "Point one internal assistant or a simple RAG demo at the Gateway URL. Use a non-sensitive corpus first, then a crown-jewel document they would never send to a public API.",
    );
  }
  if (hasTag(tags, "rag", "nutanix-files", "fp-files")) {
    steps.push("Second slice: retrieve from the on-prem corpus (Files or the existing vector store) and generate with the private model only.");
  }
  steps.push("Review usage by key with the platform owner and CISO. Agree production guardrails: quotas, tool filters, model access.");

  const success = [
    "An existing app or bot completes against the Gateway with no SDK rewrite.",
    "A private model answers a question the customer would not send to a public API.",
    "A quota or tool filter demonstrably blocks a bad call.",
    "A named owner will sponsor the production landing zone.",
  ];

  const title =
    motion === "gateway"
      ? `${customer}: two-week Agent Gateway control-plane POC`
      : motion === "inference"
        ? `${customer}: two-week private inference POC on Nutanix`
        : `${customer}: two-week hybrid NAI + Agent Gateway POC`;

  return { title, steps, success };
}

function buildTalkTrack(customer: string, tags: string[], motion: Motion): string[] {
  const lines: string[] = [];
  lines.push(
    `${customer} already has AI. The question is whether it is an estate you can operate — or a collection of keys, copilots, and agents that IT finds after the fact.`,
  );
  if (hasTag(tags, "shadow-ai", "bypass", "token-blind")) {
    lines.push(
      "People found a path. It is the consumer one. Every week you wait, more of the business logic and more of the data lives in that path. A block page will not win. A sanctioned front door will.",
    );
  }
  if (motion === "inference" || motion === "dual") {
    lines.push(
      "Private inference on Nutanix Enterprise AI is how sensitive and repetitive work comes home. Hugging Face and NVIDIA NIM, on the infrastructure you already run, next to the files you already guard.",
    );
  }
  if (motion === "gateway" || motion === "dual") {
    lines.push(
      "Agent Gateway is how you let builders keep their frameworks — LangChain, custom, vendor bots — while you finally get identity, MCP policy, token limits, failover, and an audit trail across public and private models.",
    );
  }
  if (hasTag(tags, "eval-hyperscaler", "azure-openai", "bedrock")) {
    lines.push(
      "This is not a demand to leave Azure OpenAI or Bedrock. It is a demand to stop treating them as the control plane. Use them as providers behind a Gateway you own.",
    );
  }
  lines.push(
    "The POC is small on purpose: one model, one path, visible control. If that does not change the conversation in two weeks, we should not be having a platform discussion.",
  );
  return lines;
}

export function reportMarkdown(engagement: Engagement, report: ReportModel) {
  const notes = STAGES.map((s) => {
    const n = engagement.stageNotes[s.id]?.trim();
    return n ? `### ${s.title}\n${n}` : "";
  }).filter(Boolean);

  const answered = visibleQuestions(engagement.depth, engagement.answers)
    .map((q) => {
      const labels = optionLabels(engagement, q.id);
      const skipped = engagement.answers[q.id]?.skipped;
      const extra = engagement.answers[q.id]?.text;
      const value = skipped ? "_Skipped / not discussed_" : labels.join("; ") || "_Unanswered_";
      return `- **${q.prompt}** ${value}${extra ? ` — ${extra}` : ""}`;
    })
    .join("\n");

  return `# Recon report — ${engagement.customerName}

- SE: ${engagement.seName || "—"}
- Industry: ${engagement.industry || "—"} · Region: ${engagement.region || "—"}
- Meeting: ${engagement.meetingType || "—"}
- Opportunity: ${report.opportunity}/100
- Motion: ${report.motionTitle}

## Estate
${report.estate.map((l) => `- ${l}`).join("\n")}

## Risks
${report.risks.map((l) => `- ${l}`).join("\n")}

## Why Nutanix private inference
${report.inferenceLead.map((l) => `- ${l}`).join("\n")}

## Why Nutanix Agent Gateway
${report.gatewayLead.map((l) => `- ${l}`).join("\n")}

## Recommended POC
**${report.poc.title}**
${report.poc.steps.map((s, i) => `${i + 1}. ${s}`).join("\n")}

Success:
${report.poc.success.map((s) => `- ${s}`).join("\n")}

## Talk track
${report.talkTrack.map((l) => `- ${l}`).join("\n")}

## Next meeting
${report.nextMeeting.map((l) => `- ${l}`).join("\n")}

## Discovery gaps
${report.gaps.length ? report.gaps.map((g) => `- (${g.stage}) ${g.prompt}`).join("\n") : "- Core questions complete."}

${notes.length ? `## SE notes\n${notes.join("\n\n")}` : ""}

## Captured answers
${answered}
`;
}

export function snapshotForModel(engagement: Engagement) {
  const report = buildReport(engagement);
  return {
    customer: engagement.customerName,
    industry: engagement.industry,
    region: engagement.region,
    nutanixFootprint: engagement.nutanixFootprint,
    motion: report.motionTitle,
    opportunity: report.opportunity,
    scores: report.scores,
    estate: report.estate,
    risks: report.risks,
    tags: report.tags,
    notes: engagement.stageNotes,
    answers: Object.fromEntries(
      visibleQuestions(engagement.depth, engagement.answers).map((q) => [
        q.id,
        {
          prompt: q.prompt,
          labels: optionLabels(engagement, q.id),
          skipped: engagement.answers[q.id]?.skipped ?? false,
          text: engagement.answers[q.id]?.text ?? "",
        },
      ]),
    ),
  };
}

export function questionByIdSafe(id: string) {
  return questionById(id);
}
