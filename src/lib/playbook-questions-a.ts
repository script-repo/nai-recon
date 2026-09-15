import type { Question } from "./playbook";

export const QUESTIONS_A: Question[] = [
  {
    id: "account.industry",
    stageId: "account",
    depth: "core",
    prompt: "Which industry vertical best describes this account?",
    seScript:
      "I want to make sure I am asking the right compliance questions. How do you describe the business to a regulator — bank, payer, hospital, public agency?",
    why: "Vertical drives sovereignty, model choice, and which NAI capabilities you lead with. Healthcare and FS jump to private inference and audit. A tech company may lead with developer experience and Agent Gateway.",
    listenFor: [
      "Regulated language: HIPAA, PCI, SOX, DORA, FedRAMP, ITAR",
      "Multi-national data residency ('EU data cannot leave')",
      "A named AI CoE vs. shadow projects in a single LoB",
    ],
    followUps: [
      "Which regulations actually bind production AI, not just the wish list?",
      "Is there a country or region that is a hard constraint for model hosting?",
    ],
    nutanixHook:
      "Regulated verticals are private-inference first. NAI supports dark-site deployment and air-gapped NVIDIA NIM for customers who cannot touch public APIs.",
    type: "single",
    options: [
      { id: "fs", label: "Financial services", tags: ["regulated", "fs"], score: { privateInferenceFit: 18, governanceGap: 8, urgency: 6 } },
      { id: "health", label: "Healthcare & life sciences", tags: ["regulated", "phi-pci"], score: { privateInferenceFit: 20, governanceGap: 10, urgency: 8 } },
      { id: "insure", label: "Insurance", tags: ["regulated"], score: { privateInferenceFit: 14, governanceGap: 6 } },
      { id: "public", label: "Public sector / education", tags: ["regulated", "airgap"], score: { privateInferenceFit: 16, governanceGap: 10 } },
      { id: "mfg", label: "Manufacturing / energy / utilities", tags: ["ot-data"], score: { privateInferenceFit: 12, dataGravity: 8 } },
      { id: "retail", label: "Retail, CPG, or other commercial", tags: ["commercial"], score: { agentGatewayFit: 8, privateInferenceFit: 6 } },
      { id: "tech", label: "Technology / media / professional services", tags: ["builder-heavy"], score: { agentGatewayFit: 12 } },
    ],
  },
  {
    id: "account.footprint",
    stageId: "account",
    depth: "core",
    prompt: "What Nutanix software is already in this environment?",
    seScript:
      "Before we talk models — what of ours is already running? AHV, NKP, Files, Objects? I want to land AI next to the data you already trust us with.",
    why: "Existing Nutanix is the time-to-value argument. Files + Objects mean the corpus may already sit on our stack. NKP means NAI is a natural Kubernetes-native layer rather than a new island.",
    listenFor: [
      "AHV vs. ESXi under Nutanix",
      "NKP or other Kubernetes (OpenShift, EKS, Rancher)",
      "Files/Objects used for unstructured content that RAG would need",
    ],
    followUps: [
      "Where do the file shares that knowledge workers live in actually reside?",
      "Is Kubernetes already a first-class platform, or still a side cluster?",
    ],
    nutanixHook:
      "NAI is Kubernetes-native and designed to sit on NKP. Private inference next to Files and Objects is the data-gravity story — bring the model to the data, not the other way around.",
    type: "multi",
    options: [
      { id: "ahv", label: "NCI / AHV", tags: ["nutanix-ahv"], score: { privateInferenceFit: 10 } },
      { id: "nkp", label: "NKP", tags: ["nutanix-nkp"], score: { privateInferenceFit: 14, agentGatewayFit: 6 } },
      { id: "files", label: "Nutanix Files", tags: ["nutanix-files"], score: { dataGravity: 16, privateInferenceFit: 10 } },
      { id: "objects", label: "Nutanix Objects", tags: ["nutanix-objects"], score: { dataGravity: 14, privateInferenceFit: 8 } },
      { id: "ndb", label: "NDB / databases on Nutanix", tags: ["nutanix-ndb"], score: { dataGravity: 8 } },
      { id: "none", label: "No Nutanix today", tags: ["greenfield"], score: { urgency: 2 } },
      { id: "unknown", label: "Not sure yet", tags: ["gap"], score: {} },
    ],
  },
  {
    id: "account.owner",
    stageId: "account",
    depth: "core",
    prompt: "Who is politically accountable for enterprise AI?",
    seScript:
      "When an agent does something dumb — or a token bill explodes — whose name is on the incident? CIO, CISO, a CoE, or a line of business that went around IT?",
    why: "The economic buyer and the veto live in different chairs. A CISO-led motion is Gateway + audit. A CIO/platform motion is private inference on existing infrastructure. A LoB-led motion needs IT a path that does not slow builders down.",
    listenFor: [
      "AI CoE that has a charter but no control plane",
      "CISO 'we are not ready' blocking production",
      "Platform team already running vLLM as a side project",
    ],
    followUps: [
      "Does the CoE have budget, or only a slideware mandate?",
      "Who would sign a POC on existing Nutanix capacity?",
    ],
    nutanixHook:
      "NAI gives platform and security a single control plane — RBAC, API keys, token quotas, MCP audit — without taking models away from builders.",
    type: "single",
    options: [
      { id: "cio", label: "CIO / infrastructure & platform", tags: ["buyer-cio"], score: { privateInferenceFit: 10, urgency: 4 } },
      { id: "ciso", label: "CISO / risk & security", tags: ["buyer-ciso"], score: { agentGatewayFit: 14, governanceGap: 10, urgency: 8 } },
      { id: "coe", label: "AI / data CoE", tags: ["buyer-coe"], score: { agentGatewayFit: 10, privateInferenceFit: 8 } },
      { id: "lob", label: "A line of business (went around IT)", tags: ["buyer-lob", "shadow-ai"], score: { shadowRisk: 12, agentGatewayFit: 10, urgency: 8 } },
      { id: "unclear", label: "Unclear / contested", tags: ["buyer-gap"], score: { governanceGap: 8, urgency: 6 } },
    ],
  },
  {
    id: "account.trigger",
    stageId: "account",
    depth: "deep",
    prompt: "What triggered this conversation now?",
    seScript:
      "Why are we talking this quarter and not last year? Board mandate, a leaked prompt, a token bill, a failed POC, Broadcom fatigue?",
    why: "Trigger determines urgency copy in the report. A board mandate wants a visible control plane. A bill-shock wants routing to private models. A failed DIY Kubernetes GPU cluster wants operational simplicity.",
    listenFor: [
      "Board or CEO 'we must do AI'",
      "Incident: data in ChatGPT, runaway agent, leaked key",
      "Vendor event: Broadcom, GPU lead times, Azure OpenAI quota",
    ],
    followUps: ["Is there a date on the calendar this has to show progress by?"],
    nutanixHook:
      "Match the trigger to the POC: control-plane demo for a CISO trigger, cost/failover demo for a bill-shock trigger, one-model-on-AHV for a 'just make it work' trigger.",
    type: "single",
    options: [
      { id: "board", label: "Executive or board AI mandate", tags: ["exec-mandate"], score: { urgency: 14 } },
      { id: "incident", label: "Security or data-leak incident", tags: ["incident"], score: { urgency: 16, shadowRisk: 10, governanceGap: 10 } },
      { id: "bill", label: "Token bill / cost shock", tags: ["bill-shock"], score: { urgency: 14, privateInferenceFit: 12 } },
      { id: "failed", label: "Failed or stalled DIY / POC", tags: ["failed-poc"], score: { urgency: 10, privateInferenceFit: 8 } },
      { id: "refresh", label: "Infra refresh or vendor displacement", tags: ["displacement"], score: { urgency: 8 } },
      { id: "explore", label: "Exploratory — no burning platform", tags: ["exploratory"], score: { urgency: 2 } },
    ],
  },
  {
    id: "shadow.tools",
    stageId: "shadow",
    depth: "core",
    prompt: "Which consumer or unsanctioned AI tools are people already using?",
    seScript:
      "Forget the official stack for a minute. If I walked the sales floor and engineering, what would I see in the browser? ChatGPT, Claude, Gemini, Copilot, Perplexity, something else?",
    why: "This is the demand signal. You cannot un-invent usage. The SE job is to make that usage visible, then offer a path that is safer and still fast. Multi-select — most enterprises have two or three.",
    listenFor: [
      "Personal ChatGPT Plus on corporate laptops",
      "GitHub Copilot or Cursor in engineering with no policy",
      "'We do not know' — that is itself a finding",
    ],
    followUps: [
      "Are those personal subscriptions or corporate tenants?",
      "Has anyone sampled what files are being pasted in?",
    ],
    nutanixHook:
      "Agent Gateway becomes the sanctioned alternative: one API, SSO-backed keys, quotas, and the option to serve a private model for anything sensitive.",
    type: "multi",
    options: [
      { id: "chatgpt", label: "ChatGPT (OpenAI consumer or Plus)", tags: ["shadow-ai", "public-openai"], score: { shadowRisk: 16, agentGatewayFit: 10, privateInferenceFit: 8 } },
      { id: "claude", label: "Claude.ai", tags: ["shadow-ai", "anthropic"], score: { shadowRisk: 12, agentGatewayFit: 8 } },
      { id: "gemini", label: "Gemini / Google consumer", tags: ["shadow-ai"], score: { shadowRisk: 10, agentGatewayFit: 6 } },
      { id: "copilot", label: "Microsoft Copilot (M365 or GitHub)", tags: ["sanctioned-copilot"], score: { agentGatewayFit: 6, shadowRisk: 6 } },
      { id: "cursor", label: "Cursor, Windsurf, or similar IDEs", tags: ["shadow-ai", "dev-tools"], score: { shadowRisk: 10, agentGatewayFit: 8 } },
      { id: "perplexity", label: "Perplexity or other research chat", tags: ["shadow-ai"], score: { shadowRisk: 8 } },
      { id: "unknown", label: "IT does not have a clear picture", tags: ["shadow-ai", "token-blind"], score: { shadowRisk: 14, governanceGap: 12, agentGatewayFit: 10 } },
      { id: "none", label: "Believed to be none / fully blocked", tags: ["blocked"], score: { shadowRisk: 2 } },
    ],
  },
  {
    id: "shadow.sanction",
    stageId: "shadow",
    depth: "core",
    prompt: "Is there an official sanctioned assistant, and do people actually use it?",
    seScript:
      "Is there an approved path — a corporate Copilot, an internal GPT, a ServiceNow VA — and if so, how does its usage compare to what people do on their own?",
    why: "A sanctioned tool that nobody uses is a failed control. It tells you the official path is slower, dumber, or blocked. That is the product requirement for Agent Gateway plus a capable private or hybrid model.",
    listenFor: [
      "Official Copilot with 10% adoption and ChatGPT with 70%",
      "Internal GPT that cannot see SharePoint",
      "Legal banned it; engineering ignored the ban",
    ],
    followUps: ["What would make the sanctioned path the path of least resistance?"],
    nutanixHook:
      "The winning control is not a block page. It is a unified endpoint that is as easy as the consumer tool, with private models for sensitive prompts and public models when they are the right fit.",
    type: "single",
    options: [
      { id: "used", label: "Sanctioned tool exists and is the default", tags: ["sanctioned-ok"], score: { agentGatewayFit: 6 } },
      { id: "ignored", label: "Sanctioned tool exists; people bypass it", tags: ["shadow-ai", "bypass"], score: { shadowRisk: 14, agentGatewayFit: 14, urgency: 8 } },
      { id: "none", label: "No sanctioned assistant at all", tags: ["shadow-ai", "no-path"], score: { shadowRisk: 12, agentGatewayFit: 12, urgency: 8 } },
      { id: "blocked", label: "Consumer tools blocked; no alternative", tags: ["blocked-no-alt"], score: { agentGatewayFit: 16, urgency: 10, shadowRisk: 8 } },
    ],
  },
  {
    id: "shadow.leak",
    stageId: "shadow",
    depth: "deep",
    prompt: "Any DLP alerts, paste incidents, or known data in consumer AI tools?",
    seScript:
      "Has security actually seen source code, customer data, or contracts land in a consumer model — or is this still a hypothetical risk?",
    why: "A real incident turns the conversation from enablement to emergency. It also names the CISO as a necessary stakeholder in the POC, which is good — Agent Gateway's audit trail is the demo they want.",
    listenFor: [
      "Code in ChatGPT",
      "PHI/PII paste",
      "DLP product that cannot see prompt payloads",
    ],
    followUps: ["What was the official response — training, a ban, or a project?"],
    nutanixHook:
      "Private inference keeps prompts inside the perimeter. Agent Gateway gives security an audit log of every model and MCP call they currently cannot see.",
    type: "single",
    options: [
      { id: "confirmed", label: "Confirmed incident or DLP hits", tags: ["incident", "sensitive-data"], score: { shadowRisk: 18, privateInferenceFit: 14, urgency: 14, governanceGap: 10 } },
      { id: "suspected", label: "Suspected but not measured", tags: ["token-blind"], score: { shadowRisk: 12, governanceGap: 10, agentGatewayFit: 8 } },
      { id: "none", label: "No known incidents", tags: [], score: { shadowRisk: 2 } },
      { id: "unknown", label: "Security cannot see prompt traffic", tags: ["token-blind"], score: { governanceGap: 14, agentGatewayFit: 12, shadowRisk: 10 } },
    ],
  },
  {
    id: "shadow.depts",
    stageId: "shadow",
    depth: "deep",
    prompt: "Which functions are the heaviest unsanctioned users?",
    seScript:
      "Where would I find the power users — engineering, sales, support, legal, finance, clinical, operations?",
    why: "Department tells you the first agent use case and the first corpus. Support plus a knowledge base is a RAG + chatbot motion. Engineering plus Copilot is a Gateway + private coding model motion.",
    listenFor: ["Support wanting a bot on the runbook", "Legal refusing any public model", "Devs already on Cursor with company code"],
    followUps: ["Which of those would you most want on a sanctioned path first?"],
    nutanixHook: "Pick one department for the POC. Breadth is how POCs die. Depth is how they become production.",
    type: "multi",
    options: [
      { id: "eng", label: "Engineering / product", tags: ["dept-eng"], score: { agentGatewayFit: 8 } },
      { id: "support", label: "Customer support / IT helpdesk", tags: ["dept-support"], score: { agentGatewayFit: 8, privateInferenceFit: 6 } },
      { id: "sales", label: "Sales / marketing", tags: ["dept-sales"], score: { shadowRisk: 6 } },
      { id: "legal", label: "Legal / compliance / finance", tags: ["dept-legal", "sensitive-data"], score: { privateInferenceFit: 10, governanceGap: 6 } },
      { id: "ops", label: "Operations / clinical / field", tags: ["dept-ops"], score: { privateInferenceFit: 8, dataGravity: 6 } },
      { id: "unknown", label: "Unknown", tags: ["gap"], score: {} },
    ],
  },
  {
    id: "chatbots.prod",
    stageId: "chatbots",
    depth: "core",
    prompt: "Are there chatbots or copilots already in production?",
    seScript:
      "Not a slide. Something a customer, employee, or operator actually talks to today — HR bot, ticket bot, product assistant, voice IVR.",
    why: "Production bots are attach points. They already have an owner, a channel, and a failure mode. Putting Agent Gateway in front of an existing bot is a lower-risk POC than inventing a new agent.",
    listenFor: [
      "ServiceNow VA, Salesforce Einstein, Genesys, custom RAG bot",
      "Multiple bots with different vendors and no shared identity",
      "A 'pilot' that has been in production for a year",
    ],
    followUps: ["Who gets paged when it hallucinates in front of a customer?"],
    nutanixHook:
      "Those bots can keep their UX. Change the inference and tool path: unified API, private model for internal knowledge, Gateway policy on what the bot is allowed to call.",
    type: "single",
    options: [
      { id: "prod", label: "Yes — in production, serving users", tags: ["chatbot-prod"], score: { agentGatewayFit: 12, urgency: 6 } },
      { id: "pilot", label: "Pilot / limited production", tags: ["chatbot-pilot"], score: { agentGatewayFit: 8 } },
      { id: "planned", label: "Planned, not built", tags: ["chatbot-planned"], score: { agentGatewayFit: 6 } },
      { id: "none", label: "None of note", tags: [], score: {} },
    ],
  },
  {
    id: "chatbots.stack",
    stageId: "chatbots",
    depth: "core",
    prompt: "What are those assistants built on?",
    seScript:
      "When they built it, what did they reach for — a SaaS bot platform, a hyperscaler studio, LangChain, or a vendor copilot that came with the CRM?",
    why: "Stack tells you how hard the swap is. A bot that already speaks OpenAI-compatible APIs can point at NAI Agent Gateway with almost no rewrite. A fully vendor-locked Einstein or Genesys path is a side-by-side motion, not a rip-and-replace.",
    listenFor: ["OpenAI API in a Python service", "Azure AI Studio", "Botpress / Voiceflow", "Homegrown"],
    followUps: ["Do they already use an OpenAI-compatible SDK? That is your migration gift."],
    nutanixHook:
      "Agent Gateway exposes a unified OpenAI-compatible API across public providers and self-hosted NAI models. Builders keep their SDK; IT gains the control plane.",
    type: "multi",
    showIf: [{ questionId: "chatbots.prod", optionIds: ["prod", "pilot", "planned"] }],
    options: [
      { id: "openai-api", label: "Direct OpenAI / Anthropic API", tags: ["public-openai"], score: { agentGatewayFit: 12, privateInferenceFit: 8 } },
      { id: "azure", label: "Azure OpenAI / AI Studio", tags: ["azure-openai"], score: { agentGatewayFit: 10 } },
      { id: "saas-bot", label: "SaaS bot platform (ServiceNow, Genesys, Botpress…)", tags: ["saas-bot"], score: { agentGatewayFit: 6 } },
      { id: "crm", label: "CRM / CCaaS copilot (Salesforce, Dynamics…)", tags: ["crm-copilot"], score: { agentGatewayFit: 4 } },
      { id: "custom", label: "Custom (LangChain, in-house)", tags: ["langchain"], score: { agentGatewayFit: 10, privateInferenceFit: 6 } },
      { id: "unknown", label: "Unknown", tags: ["gap"], score: {} },
    ],
  },
  {
    id: "chatbots.systems",
    stageId: "chatbots",
    depth: "deep",
    prompt: "Which systems of record do those assistants already touch?",
    seScript:
      "When the bot answers, is it only chatting, or can it look up a ticket, a policy, an order, an EMR note?",
    why: "Tool access is the Agent Gateway MCP conversation. A bot that already has credentials to a system of record is an ungoverned agent, even if they do not call it that.",
    listenFor: ["ServiceNow, Salesforce, SAP, home-grown APIs, databases, file shares"],
    followUps: ["Are those credentials shared, rotated, and scoped — or a service account in a vault nobody reviews?"],
    nutanixHook:
      "MCP server management in Agent Gateway lets you filter tools (read vs write), inject keys at the gateway, and audit every tool call.",
    type: "multi",
    showIf: [{ questionId: "chatbots.prod", optionIds: ["prod", "pilot"] }],
    options: [
      { id: "itsm", label: "ITSM (ServiceNow, Jira, etc.)", tags: ["mcp-candidate"], score: { agentGatewayFit: 10 } },
      { id: "crm", label: "CRM / ERP", tags: ["mcp-candidate"], score: { agentGatewayFit: 8 } },
      { id: "kb", label: "Knowledge base / files", tags: ["rag"], score: { dataGravity: 10, privateInferenceFit: 8 } },
      { id: "data", label: "Databases / warehouses", tags: ["mcp-candidate", "sensitive-data"], score: { agentGatewayFit: 10, privateInferenceFit: 8 } },
      { id: "none", label: "Chat only — no tools", tags: [], score: { privateInferenceFit: 4 } },
    ],
  },
  {
    id: "chatbots.quality",
    stageId: "chatbots",
    depth: "deep",
    prompt: "Are hallucination, latency, or outage complaints already a thing?",
    seScript:
      "When this goes wrong, what do they complain about — made-up answers, slow replies, or the vendor being down?",
    why: "Hallucination on internal knowledge is a RAG + private-model problem. Outages and quota limits are Gateway fallback. Latency is often a public-API routing problem you can fix by serving a smaller local model.",
    listenFor: ["Made-up policy answers", "Azure OpenAI 429s", "Cannot cite the source document"],
    followUps: ["Do they have evaluation or just anecdotal screenshots?"],
    nutanixHook:
      "Private models next to the corpus improve grounding. Agent Gateway fallback keeps the bot up when a public provider rate-limits. Speculative decoding and KV-cache work in NAI 2.8 target latency.",
    type: "multi",
    showIf: [{ questionId: "chatbots.prod", optionIds: ["prod", "pilot"] }],
    options: [
      { id: "hallucination", label: "Hallucinations / ungrounded answers", tags: ["hallucination"], score: { privateInferenceFit: 10, dataGravity: 6 } },
      { id: "latency", label: "Latency", tags: ["latency"], score: { privateInferenceFit: 8 } },
      { id: "outage", label: "Provider outage or quota limits", tags: ["outage"], score: { agentGatewayFit: 12, urgency: 6 } },
      { id: "cost", label: "Cost per conversation", tags: ["bill-shock"], score: { privateInferenceFit: 10 } },
      { id: "none", label: "No major complaints yet", tags: [], score: {} },
    ],
  },
  {
    id: "rag.exists",
    stageId: "rag",
    depth: "core",
    prompt: "Are they doing retrieval-augmented generation or document Q&A?",
    seScript:
      "Is anyone asking questions against SharePoint, file shares, Confluence, tickets, or a research corpus — not just chatting with a naked model?",
    why: "RAG is the data-gravity tell. If the corpus is on-prem and the model is in a public cloud, they are exporting their knowledge base one chunk at a time. That is the private-inference brief.",
    listenFor: ["'We built a GPT on our policies'", "Vector DB in Pinecone with files in the DC", "Pilots that died on permissions"],
    followUps: ["Which corpus would they least want leaving the building?"],
    nutanixHook:
      "Run inference next to Nutanix Files and Objects. The chunks never need a public API. Agent Gateway still lets them burst to a frontier model for tasks that do not touch that corpus.",
    type: "single",
    options: [
      { id: "prod", label: "Yes, in production", tags: ["rag", "rag-prod"], score: { dataGravity: 16, privateInferenceFit: 12 } },
      { id: "pilot", label: "Pilot / prototype", tags: ["rag", "rag-pilot"], score: { dataGravity: 12, privateInferenceFit: 10 } },
      { id: "planned", label: "Want it, have not built it", tags: ["rag-planned"], score: { dataGravity: 8, privateInferenceFit: 8 } },
      { id: "none", label: "Not doing RAG", tags: [], score: {} },
    ],
  },
  {
    id: "rag.vector",
    stageId: "rag",
    depth: "core",
    prompt: "Where does retrieval live?",
    seScript:
      "When they search that corpus, is it Pinecone, Azure AI Search, Elasticsearch, pgvector, Weaviate, something on-prem, or still 'grep and hope'?",
    why: "A cloud vector DB plus on-prem files is a split-brain architecture. It is also a cost and residency problem. You do not need to rip the vector DB on day one — you need to stop sending the retrieved chunks to a public LLM.",
    listenFor: ["Pinecone / Weaviate Cloud", "pgvector on NDB or Postgres", "Elastic already in the DC"],
    followUps: ["Who owns embedding generation, and which model does it?"],
    nutanixHook:
      "Keep retrieval where it is if it works. Move the generator. NAI private inference plus Gateway lets the RAG app keep its vector store and change only the completion endpoint.",
    type: "multi",
    showIf: [{ questionId: "rag.exists", optionIds: ["prod", "pilot", "planned"] }],
    options: [
      { id: "cloud-vector", label: "Cloud vector DB (Pinecone, Weaviate Cloud, etc.)", tags: ["vector-cloud"], score: { dataGravity: 8, privateInferenceFit: 8, governanceGap: 6 } },
      { id: "azure-search", label: "Azure AI Search / AWS / Vertex search", tags: ["vector-cloud"], score: { dataGravity: 6 } },
      { id: "onprem", label: "On-prem (Elastic, pgvector, OpenSearch…)", tags: ["vector-onprem"], score: { dataGravity: 12, privateInferenceFit: 10 } },
      { id: "none", label: "No real vector layer yet", tags: ["rag-early"], score: { privateInferenceFit: 6 } },
      { id: "unknown", label: "Unknown", tags: ["gap"], score: {} },
    ],
  },
  {
    id: "rag.sources",
    stageId: "rag",
    depth: "core",
    prompt: "What are the source systems for that knowledge?",
    seScript:
      "Where does the truth live — SharePoint, Confluence, Nutanix Files, a file server, a wiki nobody owns, an object store?",
    why: "If they say Nutanix Files or a DC file share, you have the punchline already. If they say SharePoint Online only, hybrid is still valid but the gravity argument is weaker — lean into governance and cost.",
    listenFor: ["Files on Nutanix", "Legacy file servers they want to leave", "Permissions nightmares"],
    followUps: ["Do they have a story for document-level ACL when the bot answers?"],
    nutanixHook:
      "Nutanix Files and Objects as the system of knowledge, NAI as the system of inference. One operational domain instead of three clouds.",
    type: "multi",
    showIf: [{ questionId: "rag.exists", optionIds: ["prod", "pilot", "planned"] }],
    options: [
      { id: "files-nx", label: "Nutanix Files or Objects", tags: ["nutanix-files", "data-gravity"], score: { dataGravity: 18, privateInferenceFit: 12 } },
      { id: "files-other", label: "On-prem file shares (not Nutanix)", tags: ["data-gravity"], score: { dataGravity: 14, privateInferenceFit: 10 } },
      { id: "sharepoint", label: "SharePoint / M365", tags: ["m365"], score: { dataGravity: 6, agentGatewayFit: 4 } },
      { id: "confluence", label: "Confluence / wiki / tickets", tags: ["kb"], score: { dataGravity: 6 } },
      { id: "object-cloud", label: "S3 / Azure Blob / GCS", tags: ["vector-cloud"], score: { dataGravity: 4 } },
    ],
  },
  {
    id: "rag.sensitivity",
    stageId: "rag",
    depth: "deep",
    prompt: "How sensitive is the corpus?",
    seScript:
      "If a chunk of that corpus showed up in a model provider's logs tomorrow, is that a shrug, a ticket, or a reportable event?",
    why: "Sensitivity is the private-inference closer. PHI, PCI, source code, M&A, and customer PII do not belong in a public prompt log. You need them to say this out loud so it appears in the report their CISO will read.",
    listenFor: ["PHI, PCI, source code, HR, unpublished financials", "'It's only policies' — still often confidential"],
    followUps: ["Has legal classified prompts and completions as data in motion?"],
    nutanixHook:
      "Private inference on NAI keeps prompts, KV cache, and logs inside the customer perimeter. Dark-site and air-gapped NIM exist for the 'reportable event' class.",
    type: "single",
    showIf: [{ questionId: "rag.exists", optionIds: ["prod", "pilot", "planned"] }],
    options: [
      { id: "high", label: "Regulated or crown-jewel (PHI, PCI, source, secrets)", tags: ["sensitive-data", "phi-pci"], score: { privateInferenceFit: 20, governanceGap: 10, urgency: 10, dataGravity: 10 } },
      { id: "medium", label: "Internal confidential, not regulated", tags: ["sensitive-data"], score: { privateInferenceFit: 12, dataGravity: 6 } },
      { id: "low", label: "Mostly public or low sensitivity", tags: [], score: { privateInferenceFit: 4 } },
      { id: "unknown", label: "Not classified", tags: ["gap", "governance"], score: { governanceGap: 8 } },
    ],
  },
  {
    id: "inference.providers",
    stageId: "inference",
    depth: "core",
    prompt: "Which model providers are in use today?",
    seScript:
      "Name every way a completion can happen here — OpenAI, Azure OpenAI, Anthropic, Bedrock, Vertex, Mistral, a local Llama, NIM, something on a researcher's GPU.",
    why: "This is the estate map. Multiple providers with no common auth, quota, or failover is the Agent Gateway pitch in one sentence. Heavy public usage plus any on-prem GPU plan is the hybrid routing pitch.",
    listenFor: [
      "Corporate Azure OpenAI plus personal OpenAI keys in apps",
      "Bedrock 'because procurement already has AWS'",
      "A data-science box running Ollama",
    ],
    followUps: ["Is there a single bill owner, or is this scattered across credit cards and cloud accounts?"],
    nutanixHook:
      "Agent Gateway: one API in front of public hosted models and NAI private models, with token-based rate limits, fallback, and unified observability.",
    type: "multi",
    options: [
      { id: "openai", label: "OpenAI (api.openai.com)", tags: ["public-openai"], score: { agentGatewayFit: 10, privateInferenceFit: 8 } },
      { id: "azure", label: "Azure OpenAI", tags: ["azure-openai"], score: { agentGatewayFit: 10 } },
      { id: "anthropic", label: "Anthropic", tags: ["anthropic"], score: { agentGatewayFit: 8 } },
      { id: "bedrock", label: "AWS Bedrock", tags: ["bedrock"], score: { agentGatewayFit: 8 } },
      { id: "vertex", label: "Google Vertex AI", tags: ["vertex"], score: { agentGatewayFit: 6 } },
      { id: "mistral", label: "Mistral / Cohere / other API", tags: ["public-other"], score: { agentGatewayFit: 6 } },
      { id: "self", label: "Self-hosted (vLLM, TGI, NIM, Ollama, llama.cpp)", tags: ["self-hosted"], score: { privateInferenceFit: 12 } },
      { id: "unknown", label: "Nobody has a complete list", tags: ["token-blind"], score: { agentGatewayFit: 12, governanceGap: 12 } },
    ],
  },
];
