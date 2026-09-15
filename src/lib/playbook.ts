export type ScoreAxis =
  | "shadowRisk"
  | "privateInferenceFit"
  | "agentGatewayFit"
  | "urgency"
  | "dataGravity"
  | "governanceGap";

export type QuestionType = "single" | "multi";

export type Depth = "core" | "deep";

export type Option = {
  id: string;
  label: string;
  hint?: string;
  tags: string[];
  score?: Partial<Record<ScoreAxis, number>>;
};

export type ShowIf = {
  questionId: string;
  optionIds?: string[];
  unlessOptionIds?: string[];
};

export type Question = {
  id: string;
  stageId: string;
  depth: Depth;
  prompt: string;
  seScript: string;
  why: string;
  listenFor: string[];
  followUps: string[];
  nutanixHook: string;
  type: QuestionType;
  options: Option[];
  showIf?: ShowIf[];
};

export type Stage = {
  id: string;
  index: number;
  code: string;
  title: string;
  short: string;
  intent: string;
  briefing: string;
  wedge: string;
};

export const STAGES: Stage[] = [
  {
    id: "account",
    index: 1,
    code: "01",
    title: "Account frame",
    short: "Who, where, why now",
    intent: "Set the terrain before you hunt tooling. Industry, Nutanix footprint, and who owns AI decide which NAI motion you lead with.",
    briefing:
      "Do not start with models. Start with gravity: where the data lives, who is politically accountable for AI, and whether this is a net-new conversation or an install-base expansion. A bank on AHV with Files is a different play than a retailer on VMware shopping Bedrock.",
    wedge: "Install-base Nutanix plus regulated data is the shortest path to a private-inference POC.",
  },
  {
    id: "shadow",
    index: 2,
    code: "02",
    title: "Shadow AI",
    short: "What people actually use",
    intent: "Surface unsanctioned ChatGPT, Copilot, Claude, and Gemini use. Shadow AI is the political proof that demand already exists.",
    briefing:
      "Ask what employees use, not what IT approved. If they blocked ChatGPT at the proxy, ask what happens on phones and in personal tenants. Shadow AI rarely dies — it relocates. That relocation is your Agent Gateway opening: a sanctioned path faster than the workaround.",
    wedge: "Agent Gateway as the sanctioned front door. Private inference for anything that must not leave.",
  },
  {
    id: "chatbots",
    index: 3,
    code: "03",
    title: "Chatbots & copilots",
    short: "Assistants in the business",
    intent: "Find production assistants — helpdesk, HR, customer support, sales copilots — and who owns them.",
    briefing:
      "Line-of-business bots are often built outside IT with a SaaS LLM behind them. Map owners, systems of record, and hallucination pain. A bot that already talks to ServiceNow or Salesforce is one MCP policy away from being governable.",
    wedge: "Put those bots on a unified endpoint with auth, quotas, and an audit trail.",
  },
  {
    id: "rag",
    index: 4,
    code: "04",
    title: "RAG & knowledge",
    short: "Where context comes from",
    intent: "Discover retrieval stacks, document corpora, and whether chunks of sensitive data are leaving the premises.",
    briefing:
      "RAG is where data gravity becomes visible. If SharePoint, file shares, or Nutanix Files are the corpus, and embeddings or prompts go to a public API, you have a sovereignty and cost conversation. Listen for vector DBs that live in someone else's cloud while the source files sit in their DC.",
    wedge: "Private inference next to Files and Objects. Do not ship the corpus to a frontier API.",
  },
  {
    id: "inference",
    index: 5,
    code: "05",
    title: "Inference estate",
    short: "Models, GPUs, spend",
    intent: "Inventory every path tokens take — public APIs, Azure OpenAI, Bedrock, NIM, vLLM, Ollama — plus GPU reality and monthly spend.",
    briefing:
      "You are mapping the bill and the blast radius. High public-token spend with idle or planned on-prem GPUs is a private-inference ROI story. A zoo of SDKs with no failover is an Agent Gateway story. Most accounts are both.",
    wedge: "NAI private inference for NVIDIA NIM and Hugging Face models, with Agent Gateway routing cheap or sensitive work private and bursting to public.",
  },
  {
    id: "agents",
    index: 6,
    code: "06",
    title: "Agents & MCP",
    short: "Tools, identity, runaway risk",
    intent: "Find agent frameworks, MCP servers, tool credentials, and any cost or rogue-behavior incident.",
    briefing:
      "This is the Agent Gateway stage. Agents that can write to GitHub, query a database, or call Stripe without a control plane are a CISO conversation waiting to happen. Ask who holds the API keys the agent uses. If the answer is 'in the .env', you have a discovery win.",
    wedge: "Nutanix Agent Gateway: MCP governance, tool-level filtering, per-agent quotas, unified API, audit logs.",
  },
  {
    id: "governance",
    index: 7,
    code: "07",
    title: "Governance & sovereignty",
    short: "Policy, audit, air-gap",
    intent: "Test whether AI policy is real, which regulations apply, and whether they need dark-site or air-gapped inference.",
    briefing:
      "Policy decks are not enforcement. Ask who can see token usage by team, whether there is an audit trail of tool calls, and if models are scanned before they run. Air-gap and data-residency answers change the entire POC shape — NIM on a dark site vs. hybrid fallback.",
    wedge: "RBAC, API keys, Prisma AIRS model scanning, MCP audit trail, air-gapped NIM, dark-site NAI.",
  },
  {
    id: "path",
    index: 8,
    code: "08",
    title: "Pain & path",
    short: "Budget, timeline, next step",
    intent: "Lock the buying motion: what is broken, who pays, when they need a win, and who else is in the bake-off.",
    briefing:
      "Close the loop. Convert the estate map into a single next action — usually a two-week POC that deploys one private model and puts Agent Gateway in front of an existing agent or chatbot. Name the executive sponsor out loud.",
    wedge: "A scoped POC on existing Nutanix: one model, one agent path, visible token control.",
  },
];

export const INDUSTRIES = [
  "Financial services",
  "Healthcare & life sciences",
  "Insurance",
  "Public sector",
  "Education",
  "Manufacturing",
  "Retail & CPG",
  "Energy & utilities",
  "Telecom",
  "Technology",
  "Media & entertainment",
  "Legal & professional services",
  "Other",
] as const;

export const REGIONS = ["North America", "EMEA", "APJ", "LATAM"] as const;

export const MEETING_TYPES = [
  { id: "first", label: "First discovery" },
  { id: "deep", label: "Technical deep dive" },
  { id: "exec", label: "Executive briefing" },
  { id: "install", label: "Install-base expansion" },
  { id: "competitive", label: "Competitive displacement" },
] as const;

export const NUTANIX_FOOTPRINT = [
  { id: "ahv", label: "NCI / AHV" },
  { id: "nkp", label: "NKP (Kubernetes)" },
  { id: "files", label: "Nutanix Files" },
  { id: "objects", label: "Nutanix Objects" },
  { id: "ndb", label: "Nutanix Database Service" },
  { id: "mine", label: "Mine / backup" },
  { id: "none", label: "No Nutanix yet" },
  { id: "unknown", label: "Unknown" },
] as const;

import { QUESTIONS_A } from "./playbook-questions-a";
import { QUESTIONS_B } from "./playbook-questions-b";

export const QUESTIONS = [...QUESTIONS_A, ...QUESTIONS_B];

export function stageById(id: string) {
  return STAGES.find((s) => s.id === id);
}

export function questionsForStage(stageId: string, depth: Depth) {
  return QUESTIONS.filter((q) => q.stageId === stageId && (depth === "deep" || q.depth === "core"));
}

export function questionById(id: string) {
  return QUESTIONS.find((q) => q.id === id);
}

export function isQuestionVisible(
  question: Question,
  answers: Record<string, { optionIds: string[] }>,
) {
  if (!question.showIf || question.showIf.length === 0) return true;
  return question.showIf.every((rule) => {
    const current = answers[rule.questionId];
    if (!current || current.optionIds.length === 0) return false;
    if (rule.unlessOptionIds?.some((id) => current.optionIds.includes(id))) return false;
    if (!rule.optionIds || rule.optionIds.length === 0) return true;
    return rule.optionIds.some((id) => current.optionIds.includes(id));
  });
}

export function visibleQuestions(depth: Depth, answers: Record<string, { optionIds: string[] }>) {
  return QUESTIONS.filter(
    (q) => (depth === "deep" || q.depth === "core") && isQuestionVisible(q, answers),
  );
}
