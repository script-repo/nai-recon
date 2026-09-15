import type { Question } from "./playbook";

export const QUESTIONS_B: Question[] = [
  {
    id: "inference.self",
    stageId: "inference",
    depth: "core",
    prompt: "If they self-host, how production-grade is it?",
    seScript:
      "Is that local model a researcher's Ollama box, a vLLM on a dusty GPU server, NVIDIA NIM, or something platform engineering will page on?",
    why: "DIY self-host is your displacement of toil, not of the idea. They already believe in private inference. They are failing on operations, multi-model catalog, RBAC, and a unified gateway. That is NAI.",
    listenFor: ["Ollama on a workstation", "One vLLM, no HA", "NIM interest but no operating model", "OpenShift AI comparison"],
    followUps: ["Who is on call when the GPU node dies at 2am?"],
    nutanixHook:
      "NAI productizes private inference: Hugging Face and NVIDIA NIM catalog, endpoint management, API keys, RBAC, preflight, plus Agent Gateway in front. Multi-GPU / multi-node inference and LoRA fine-tune in current NAI releases.",
    type: "single",
    showIf: [{ questionId: "inference.providers", optionIds: ["self"] }],
    options: [
      { id: "ollama", label: "Lab / Ollama / workstation", tags: ["ollama", "self-hosted"], score: { privateInferenceFit: 14, urgency: 6 } },
      { id: "vllm", label: "vLLM / TGI, not really a platform", tags: ["vllm", "self-hosted"], score: { privateInferenceFit: 12, agentGatewayFit: 8 } },
      { id: "nim", label: "NVIDIA NIM in some form", tags: ["nim", "self-hosted"], score: { privateInferenceFit: 10 } },
      { id: "platform", label: "An actual internal MaaS platform", tags: ["self-hosted", "internal-maas"], score: { agentGatewayFit: 10, privateInferenceFit: 6 } },
    ],
  },
  {
    id: "inference.gpu",
    stageId: "inference",
    depth: "core",
    prompt: "What does the GPU story look like?",
    seScript:
      "Do they have NVIDIA GPUs on-prem or on Nutanix today? Ordered? Rented in a cloud? Or is 'we will use APIs forever' the current plan?",
    why: "No GPUs does not kill private inference — it changes the first step to a capacity conversation. Idle GPUs are a utilization story. Ordered GPUs with no software plane are a 'don't let this become another science project' story.",
    listenFor: ["A100/H100 sitting underutilized", "DGX that only research can touch", "Cloud GPU sticker shock"],
    followUps: ["Are those GPUs attached to Nutanix already, or a separate island?"],
    nutanixHook:
      "NAI turns GPU capacity into Model-as-a-Service. kvCache offload, speculative decoding, and multi-node inference exist to make scarce GPUs serve more than one demo.",
    type: "single",
    options: [
      { id: "onprem-used", label: "On-prem GPUs, already serving models", tags: ["gpu-onprem"], score: { privateInferenceFit: 16 } },
      { id: "onprem-idle", label: "On-prem GPUs, idle or research-only", tags: ["gpu-onprem", "gpu-idle"], score: { privateInferenceFit: 18, urgency: 8 } },
      { id: "ordered", label: "GPUs ordered or in budget", tags: ["gpu-planned"], score: { privateInferenceFit: 12, urgency: 6 } },
      { id: "cloud", label: "Cloud GPUs only", tags: ["gpu-cloud"], score: { privateInferenceFit: 6, agentGatewayFit: 6 } },
      { id: "none", label: "No GPU plan — APIs only", tags: ["gpu-none"], score: { agentGatewayFit: 10, privateInferenceFit: 4 } },
    ],
  },
  {
    id: "inference.spend",
    stageId: "inference",
    depth: "core",
    prompt: "What is the order of magnitude of monthly model spend?",
    seScript:
      "I do not need the invoice. Are we talking hundreds, tens of thousands, or a number that made finance write a Slack message in all caps?",
    why: "Spend is the private-inference ROI math. Agent Gateway's token quotas are the control that prevents the next all-caps message. 'We don't know' is a governance finding equal to high spend.",
    listenFor: ["Scattered credit cards", "A single Azure EA line item", "Agents looping and burning tokens"],
    followUps: ["Can they attribute spend to a team, an app, or an agent today?"],
    nutanixHook:
      "Gateway token-based rate limiting and unified observability. Shift repetitive or sensitive workloads to private open-weight models where you pay for infrastructure, not every token.",
    type: "single",
    options: [
      { id: "unknown", label: "Unknown / not attributed", tags: ["token-blind"], score: { governanceGap: 14, agentGatewayFit: 14, urgency: 8 } },
      { id: "low", label: "Under ~$5k / month", tags: ["low-spend"], score: { privateInferenceFit: 4, agentGatewayFit: 6 } },
      { id: "mid", label: "Roughly $5k–$50k / month", tags: ["mid-spend"], score: { privateInferenceFit: 10, agentGatewayFit: 8, urgency: 4 } },
      { id: "high", label: "North of ~$50k / month", tags: ["high-spend", "bill-shock"], score: { privateInferenceFit: 18, agentGatewayFit: 10, urgency: 12 } },
    ],
  },
  {
    id: "inference.egress",
    stageId: "inference",
    depth: "deep",
    prompt: "How do they feel about prompts leaving the premises?",
    seScript:
      "For the workloads that matter, is sending the prompt to a US or EU public API acceptable, uncomfortable, or forbidden?",
    why: "This is the sovereignty axis. Forbidden means private inference is not optional. Uncomfortable means hybrid with a default-private policy. Acceptable means lead with Gateway cost and control, and still plant a private model for the next policy change.",
    listenFor: ["Legal opinion in draft", "EU-only", "Air-gap already required for other apps"],
    followUps: ["Does that policy differ for internal vs. customer-facing apps?"],
    nutanixHook:
      "NAI private inference, dark-site deployment, and air-gapped NVIDIA NIM are the product answers when 'forbidden' is the real policy.",
    type: "single",
    options: [
      { id: "ok", label: "Acceptable with a BAA / DPA", tags: [], score: { agentGatewayFit: 6 } },
      { id: "uncomfortable", label: "Uncomfortable; want an alternative", tags: ["sensitive-data"], score: { privateInferenceFit: 14, urgency: 6 } },
      { id: "forbidden", label: "Forbidden for important workloads", tags: ["sensitive-data", "airgap"], score: { privateInferenceFit: 20, urgency: 10, governanceGap: 8 } },
      { id: "unsettled", label: "Legal has not ruled", tags: ["gap"], score: { privateInferenceFit: 8, governanceGap: 6 } },
    ],
  },
  {
    id: "agents.maturity",
    stageId: "agents",
    depth: "core",
    prompt: "Where are they on agents — slides, POCs, or production?",
    seScript:
      "When they say agentic, do they mean a chatbot with a tool, a multi-step LangGraph, or something that already runs unattended against production systems?",
    why: "Maturity sets Gateway urgency. Slides get education. POCs get a control plane before they scale. Production agents without MCP governance are the highest-urgency Gateway motion in the playbook.",
    listenFor: ["A demo on a laptop", "An internal 'agent platform' with three users", "Something that already opens tickets or merges PRs"],
    followUps: ["How many agents, and how many humans own them?"],
    nutanixHook:
      "Agent Gateway is the front door between agents, models, and MCP tools — identity, tool-level filtering, quotas, failover, and an audit trail.",
    type: "single",
    options: [
      { id: "none", label: "Mostly conversation / slides", tags: ["agents-none"], score: { agentGatewayFit: 4 } },
      { id: "poc", label: "POCs and experiments", tags: ["agents-poc"], score: { agentGatewayFit: 12, urgency: 6 } },
      { id: "limited", label: "Limited production (one or two agents)", tags: ["agents-prod"], score: { agentGatewayFit: 16, urgency: 8 } },
      { id: "scaled", label: "Multiple production agents", tags: ["agents-prod", "agents-scaled"], score: { agentGatewayFit: 20, urgency: 12, governanceGap: 8 } },
    ],
  },
  {
    id: "agents.frameworks",
    stageId: "agents",
    depth: "core",
    prompt: "What are they building agents with?",
    seScript:
      "LangChain or LangGraph, CrewAI, Semantic Kernel, AutoGen, n8n, custom Python, something from a vendor — what is actually in Git?",
    why: "Framework tells you the integration story. Almost all of them can point at an OpenAI-compatible endpoint. That is why Gateway's unified API matters — you do not ask them to rewrite agents.",
    listenFor: ["LangGraph in production", "n8n glued to OpenAI", "A homegrown loop they are scared to touch"],
    followUps: ["Are those agents using structured tools, MCP, or ad-hoc HTTP?"],
    nutanixHook:
      "Do not fight the framework. Point it at Agent Gateway. Swap models and apply policy without a rewrite.",
    type: "multi",
    showIf: [{ questionId: "agents.maturity", optionIds: ["poc", "limited", "scaled"] }],
    options: [
      { id: "langchain", label: "LangChain / LangGraph", tags: ["langchain"], score: { agentGatewayFit: 8 } },
      { id: "crew", label: "CrewAI / AutoGen / multi-agent kits", tags: ["multi-agent"], score: { agentGatewayFit: 10 } },
      { id: "semantic", label: "Semantic Kernel / Microsoft stack", tags: ["msft"], score: { agentGatewayFit: 6 } },
      { id: "n8n", label: "n8n / automation tools", tags: ["automation"], score: { agentGatewayFit: 8 } },
      { id: "custom", label: "Custom orchestration", tags: ["custom-agent"], score: { agentGatewayFit: 8 } },
      { id: "vendor", label: "Vendor agent platform", tags: ["vendor-agent"], score: { agentGatewayFit: 6 } },
    ],
  },
  {
    id: "agents.mcp",
    stageId: "agents",
    depth: "core",
    prompt: "Are they using MCP or otherwise giving agents tools?",
    seScript:
      "Can the agent do things — GitHub, Slack, Jira, Salesforce, a database, internal APIs — and is that going through MCP or a pile of one-off integrations?",
    why: "This is the heart of Nutanix Agent Gateway in NAI 2.7/2.8. MCP without a gateway is a new class of identity problem. One-off tools without MCP will become MCP. Either way you want to be the control plane they adopt before the sprawl calcifies.",
    listenFor: ["MCP servers on laptops", "Shared GitHub tokens in agent configs", "Plans to 'add MCP next quarter'"],
    followUps: ["Who decides which tools an agent is allowed to call, today, in practice?"],
    nutanixHook:
      "Agent Gateway MCP: local MCP servers inside NAI, remote MCP, tool-level filtering, API-key injection at the gateway, and a full audit log of every MCP request.",
    type: "single",
    showIf: [{ questionId: "agents.maturity", optionIds: ["poc", "limited", "scaled"] }],
    options: [
      { id: "mcp-prod", label: "MCP in use, no central control", tags: ["mcp", "no-mcp-governance"], score: { agentGatewayFit: 22, governanceGap: 14, urgency: 12 } },
      { id: "mcp-early", label: "MCP experiments", tags: ["mcp", "no-mcp-governance"], score: { agentGatewayFit: 16, governanceGap: 8, urgency: 8 } },
      { id: "tools", label: "Tools / function-calling, not MCP yet", tags: ["tools-no-mcp"], score: { agentGatewayFit: 12, urgency: 6 } },
      { id: "none", label: "Chat-only agents, no tools", tags: [], score: { agentGatewayFit: 4 } },
    ],
  },
  {
    id: "agents.identity",
    stageId: "agents",
    depth: "deep",
    prompt: "How do agents authenticate to models and tools?",
    seScript:
      "When an agent calls a model or a tool, whose identity is it? A shared key, a human's OAuth token, a service principal, or 'it depends'?",
    why: "Shared keys are how you get bill-shock and how you fail an audit. Per-agent identity with revocable keys is table stakes, and it is a Gateway feature rather than a science project.",
    listenFor: [".env files in repos", "One org-wide OpenAI key", "No revocation story"],
    followUps: ["If a contractor leaves, do their agents die the same day?"],
    nutanixHook:
      "NAI API key management plus Gateway-side injection. Keys are issued, scoped, rate-limited, and revoked without touching every MCP server.",
    type: "single",
    showIf: [{ questionId: "agents.maturity", optionIds: ["poc", "limited", "scaled"] }],
    options: [
      { id: "shared", label: "Shared keys / secrets in config", tags: ["shared-keys", "no-mcp-governance"], score: { agentGatewayFit: 16, governanceGap: 14, urgency: 10 } },
      { id: "human", label: "Runs as a human's identity", tags: ["human-identity"], score: { agentGatewayFit: 12, governanceGap: 10 } },
      { id: "sp", label: "Service principals, inconsistent", tags: ["partial-iam"], score: { agentGatewayFit: 10, governanceGap: 6 } },
      { id: "mature", label: "Per-agent identity, revocable", tags: ["mature-iam"], score: { agentGatewayFit: 6 } },
    ],
  },
  {
    id: "agents.runaway",
    stageId: "agents",
    depth: "deep",
    prompt: "Any runaway loop, surprise bill, or agent acting outside intent?",
    seScript:
      "Have they already had an agent retry itself into a four-figure night, spam a ticket queue, or call a write tool it should not have had?",
    why: "These stories sell Gateway better than any datasheet. Token quotas, tool-level filtering, and audit exist because this class of failure is now common. Get the story in their words for the report.",
    listenFor: ["Infinite tool loop", "Write access that should have been read", "A weekend bill"],
    followUps: ["What control would have stopped it — a quota, a tool filter, or an approval step?"],
    nutanixHook:
      "Granular token-based rate limits per user, team, or agent. Tool-level MCP filtering. Audit logs for every MCP request. Fallback so a bad provider does not get hammered.",
    type: "single",
    showIf: [{ questionId: "agents.maturity", optionIds: ["poc", "limited", "scaled"] }],
    options: [
      { id: "yes", label: "Yes — cost, loop, or rogue action", tags: ["runaway", "bill-shock"], score: { agentGatewayFit: 18, urgency: 14, governanceGap: 10 } },
      { id: "near", label: "Near-miss or they are worried", tags: ["runaway-risk"], score: { agentGatewayFit: 12, urgency: 8 } },
      { id: "no", label: "Not yet", tags: [], score: { agentGatewayFit: 4 } },
    ],
  },
  {
    id: "gov.policy",
    stageId: "governance",
    depth: "core",
    prompt: "Is there an AI policy, and is it enforced in technical controls?",
    seScript:
      "I have seen beautiful acceptable-use PDFs. I am asking whether anything technical actually stops the wrong model, the wrong tool, or the wrong data.",
    why: "Policy without a control plane is the gap NAI fills. If they have a PDF and ChatGPT Plus, the report should say so plainly. If they have a real CASB/DLP but no model/tool identity, Gateway still has a job.",
    listenFor: ["Policy in draft", "Training only", "A CoE that reviews use cases by email"],
    followUps: ["Who is allowed to stand up a new model endpoint without a ticket?"],
    nutanixHook:
      "NAI RBAC, model access control, API keys, Gateway quotas, and MCP policy are enforcement, not documentation.",
    type: "single",
    options: [
      { id: "enforced", label: "Policy + technical enforcement", tags: ["policy-ok"], score: { governanceGap: 2, agentGatewayFit: 4 } },
      { id: "paper", label: "Policy on paper, little enforcement", tags: ["no-policy", "policy-unenforced"], score: { governanceGap: 14, agentGatewayFit: 12, urgency: 6 } },
      { id: "none", label: "No real policy yet", tags: ["no-policy"], score: { governanceGap: 16, agentGatewayFit: 10, urgency: 6 } },
      { id: "draft", label: "Being written now", tags: ["no-policy"], score: { governanceGap: 10, agentGatewayFit: 8 } },
    ],
  },
  {
    id: "gov.reg",
    stageId: "governance",
    depth: "core",
    prompt: "Which regulatory or audit overlays actually apply to AI here?",
    seScript:
      "If internal audit walked in tomorrow, what framework would they hold this against — HIPAA, PCI, SOC2, ISO, FedRAMP, GDPR, DORA, something sector-specific?",
    why: "Audit is how Gateway's logs and private inference's perimeter get budgeted. Name the overlay in the report so the CISO sees themselves.",
    listenFor: ["DORA in EMEA banks", "HIPAA + state privacy", "Fed/SLED air-gap", "SOC2 customer questionnaires they cannot answer"],
    followUps: ["Have customers already asked about where prompts are processed?"],
    nutanixHook:
      "Audit logs of MCP and LLM activity, RBAC, model scanning with Prisma AIRS (OWASP/NIST mapping), and private inference that never leaves the perimeter.",
    type: "multi",
    options: [
      { id: "hipaa", label: "HIPAA / health privacy", tags: ["regulated", "phi-pci"], score: { privateInferenceFit: 12, governanceGap: 8 } },
      { id: "pci", label: "PCI / financial data", tags: ["regulated", "phi-pci"], score: { privateInferenceFit: 10, governanceGap: 6 } },
      { id: "privacy", label: "GDPR / DORA / privacy regimes", tags: ["regulated"], score: { privateInferenceFit: 10, governanceGap: 8 } },
      { id: "fed", label: "FedRAMP / public sector / air-gap", tags: ["regulated", "airgap"], score: { privateInferenceFit: 16, governanceGap: 10 } },
      { id: "soc", label: "SOC2 / ISO customer pressure", tags: ["regulated"], score: { governanceGap: 6, agentGatewayFit: 6 } },
      { id: "none", label: "No specific overlay named", tags: [], score: {} },
    ],
  },
  {
    id: "gov.airgap",
    stageId: "governance",
    depth: "deep",
    prompt: "Do they need dark-site or air-gapped inference?",
    seScript:
      "Are there environments that cannot reach Hugging Face, NVIDIA NGC, or a public model API — now or in the next year?",
    why: "Air-gap changes the POC and the SKU conversation. NAI dark-site and air-gapped NIM are a differentiator against 'just use Azure OpenAI'. Do not discover this in week six of a POC.",
    listenFor: ["Classified / ITAR", "OT networks", "Sovereign cloud requirements"],
    followUps: ["How do they patch and update software in that environment today?"],
    nutanixHook:
      "NAI dark-site deployment and air-gapped NVIDIA NIM support (with NVAIE licenses) are the explicit product answers.",
    type: "single",
    options: [
      { id: "required", label: "Required today", tags: ["airgap"], score: { privateInferenceFit: 20, urgency: 10 } },
      { id: "planned", label: "Likely in 12 months", tags: ["airgap"], score: { privateInferenceFit: 12, urgency: 6 } },
      { id: "no", label: "Not required", tags: [], score: {} },
      { id: "unknown", label: "Unknown", tags: ["gap"], score: {} },
    ],
  },
  {
    id: "gov.observe",
    stageId: "governance",
    depth: "core",
    prompt: "Can they see token usage, tool calls, and who used which model?",
    seScript:
      "If I asked for last month's usage by team, by application, by agent — could someone produce it without a scavenger hunt?",
    why: "Blindness is the most common finding. Unified observability is not a nice-to-have; it is how they prevent the next bill-shock and how they answer audit. This is Gateway, not a Grafana side quest.",
    listenFor: ["Cloud bill only", "No tool-call logs", "Data science has a notebook, IT has nothing"],
    followUps: ["Where would a CISO look after a bad agent action today?"],
    nutanixHook:
      "Agent Gateway centralized visibility into token usage, MCP access, and LLM activity — plus audit logs of every MCP request.",
    type: "single",
    options: [
      { id: "full", label: "Yes — attributed and reviewable", tags: ["observe-ok"], score: { governanceGap: 0 } },
      { id: "partial", label: "Cloud bills only, no agent/tool view", tags: ["token-blind"], score: { governanceGap: 12, agentGatewayFit: 12 } },
      { id: "none", label: "Essentially blind", tags: ["token-blind"], score: { governanceGap: 18, agentGatewayFit: 14, urgency: 8 } },
    ],
  },
  {
    id: "path.pains",
    stageId: "path",
    depth: "core",
    prompt: "What is actually hurting them right now?",
    seScript:
      "If we only fix one thing this quarter, what would they pick — cost, control, a production use case, GPU operations, or getting off a vendor they no longer trust?",
    why: "Pain is the lead of the targeting narrative. Your report should open with their words, not a product list. Multi-select, then force a ranking in the notes.",
    listenFor: ["We cannot go to production", "We cannot explain spend", "We cannot hire enough ML platform people"],
    followUps: ["Which pain has a date attached to it?"],
    nutanixHook:
      "Map pain to motion: cost and sovereignty → private inference. Control, MCP, spend attribution → Agent Gateway. Operational toil on DIY models → NAI as MaaS.",
    type: "multi",
    options: [
      { id: "cost", label: "Cost / token bills", tags: ["bill-shock"], score: { privateInferenceFit: 12, urgency: 8 } },
      { id: "control", label: "No control or audit of agents/tools", tags: ["no-mcp-governance"], score: { agentGatewayFit: 14, urgency: 10 } },
      { id: "prod", label: "Cannot get a use case to production", tags: ["failed-poc"], score: { privateInferenceFit: 8, agentGatewayFit: 8, urgency: 8 } },
      { id: "ops", label: "GPU / model ops toil", tags: ["ops-toil"], score: { privateInferenceFit: 12, urgency: 6 } },
      { id: "risk", label: "Shadow AI / leakage risk", tags: ["shadow-ai"], score: { agentGatewayFit: 10, privateInferenceFit: 8, urgency: 8 } },
      { id: "vendor", label: "Vendor lock-in or displacement", tags: ["displacement"], score: { agentGatewayFit: 8, urgency: 6 } },
    ],
  },
  {
    id: "path.timing",
    stageId: "path",
    depth: "core",
    prompt: "What is the realistic timeline for a decision or POC?",
    seScript:
      "Are we trying to put something on a GPU this month, this quarter, or is this a next-FY architecture conversation?",
    why: "Timeline changes the recommended POC. This quarter: one model, one agent path, Gateway in front. Next FY: include NKP landing zone and Files corpus. Do not over-scope a this-month conversation.",
    listenFor: ["Board in six weeks", "Budget expires", "Waiting on GPUs"],
    followUps: ["What would make this slip a quarter?"],
    nutanixHook:
      "A two-week POC on existing Nutanix: deploy one NIM or Hugging Face model, put Agent Gateway in front of one existing bot or agent, show quotas and an audit log.",
    type: "single",
    options: [
      { id: "now", label: "This month", tags: ["time-now"], score: { urgency: 16 } },
      { id: "quarter", label: "This quarter", tags: ["time-quarter"], score: { urgency: 12 } },
      { id: "fy", label: "This fiscal year", tags: ["time-fy"], score: { urgency: 6 } },
      { id: "explore", label: "Exploratory, no date", tags: ["exploratory"], score: { urgency: 2 } },
    ],
  },
  {
    id: "path.budget",
    stageId: "path",
    depth: "deep",
    prompt: "Is there budget, and whose budget is it?",
    seScript:
      "If a POC goes well, who pays — platform, security, a LoB, or 'we will figure it out'? Is there already a line for AI infrastructure?",
    why: "Budget owner is the close plan. Platform budget loves NAI on existing clusters. Security budget loves Gateway. LoB budget loves a use case. 'Figure it out' means you need an exec sponsor in the report.",
    listenFor: ["Unspent GPU capex", "Cloud AI overage they want to repatriate", "No budget until FY start"],
    followUps: ["Can they use existing Nutanix capacity to start so software is the only ask?"],
    nutanixHook:
      "Lead with existing capacity. NAI makes current Nutanix GPUs and NKP clusters into an AI platform without a new island.",
    type: "single",
    options: [
      { id: "platform", label: "Platform / infra budget ready", tags: ["budget-platform"], score: { privateInferenceFit: 8, urgency: 6 } },
      { id: "security", label: "Security / risk budget ready", tags: ["budget-security"], score: { agentGatewayFit: 8, urgency: 6 } },
      { id: "lob", label: "Line-of-business budget", tags: ["budget-lob"], score: { urgency: 6 } },
      { id: "none", label: "No budget identified", tags: ["budget-gap"], score: { urgency: 2 } },
    ],
  },
  {
    id: "path.compete",
    stageId: "path",
    depth: "core",
    prompt: "Who else is in the conversation?",
    seScript:
      "What are they also looking at — Azure AI Foundry, Bedrock, OpenShift AI, VMware Private AI, Databricks, a DIY vLLM on Kubernetes, or 'nothing yet'?",
    why: "Competitive context shapes the talk track. Vs. hyperscalers: data gravity, hybrid, no lock-in, Gateway across providers. Vs. OpenShift AI: operational simplicity and Agent Gateway MCP. Vs. DIY: the control plane they will not finish.",
    listenFor: ["Already on Azure OpenAI, looking for private", "Red Hat in the DC", "Broadcom fatigue"],
    followUps: ["What would make them pick a platform vs. staying on raw APIs?"],
    nutanixHook:
      "NAI is hybrid by design: private NIM/HF models plus public providers through one Gateway. Dual-native with VMs and Kubernetes. You are not asking them to pick a single cloud's AI stack.",
    type: "multi",
    options: [
      { id: "azure", label: "Azure AI / OpenAI", tags: ["eval-hyperscaler", "azure-openai"], score: { agentGatewayFit: 6 } },
      { id: "aws", label: "Bedrock / SageMaker", tags: ["eval-hyperscaler", "bedrock"], score: { agentGatewayFit: 6 } },
      { id: "gcp", label: "Vertex AI", tags: ["eval-hyperscaler"], score: { agentGatewayFit: 4 } },
      { id: "redhat", label: "OpenShift AI / RHEL AI", tags: ["eval-redhat"], score: { privateInferenceFit: 8 } },
      { id: "vmware", label: "VMware Private AI / Broadcom", tags: ["eval-vmware", "displacement"], score: { privateInferenceFit: 8, urgency: 6 } },
      { id: "diy", label: "DIY vLLM / K8s", tags: ["eval-diy"], score: { privateInferenceFit: 10, agentGatewayFit: 8 } },
      { id: "data", label: "Databricks / Snowflake Cortex", tags: ["eval-data"], score: { agentGatewayFit: 4 } },
      { id: "none", label: "No active bake-off", tags: [], score: { urgency: 2 } },
    ],
  },
];
