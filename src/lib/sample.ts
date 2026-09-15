import type { Engagement } from "@/lib/store";

/** Fictional healthcare discovery — shadow AI and ungoverned agents. */
export function sampleEngagement(): Engagement {
  const now = new Date().toISOString();
  return {
    id: "sample-briarhaven-health",
    createdAt: now,
    updatedAt: now,
    seName: "Casey Lang",
    customerName: "Briarhaven Health",
    industry: "Healthcare & life sciences",
    region: "North America",
    meetingType: "Technical deep dive",
    nutanixFootprint: ["ahv", "files", "nkp"],
    depth: "deep",
    currentStageId: "path",
    stageNotes: {
      shadow:
        "CISO confirmed nurses and care coordinators use ChatGPT on phones after the proxy block. A policy PDF exists. Enforcement does not.",
      agents:
        "Platform team has two LangGraph agents in limited production: prior-auth helper and a coding-assistant that can open Jira. Shared OpenAI key in a vault. No MCP gateway.",
      inference:
        "Azure OpenAI is the official path (~$70k/mo and climbing). A research group runs Llama on a single H100 that platform cannot observe.",
    },
    answers: {
      "account.industry": { optionIds: ["health"] },
      "account.footprint": { optionIds: ["ahv", "nkp", "files"] },
      "account.owner": { optionIds: ["ciso"] },
      "account.trigger": { optionIds: ["bill"] },
      "shadow.tools": { optionIds: ["chatgpt", "copilot", "unknown"] },
      "shadow.sanction": { optionIds: ["ignored"] },
      "shadow.leak": { optionIds: ["suspected"] },
      "shadow.depts": { optionIds: ["support", "ops", "eng"] },
      "chatbots.prod": { optionIds: ["prod"] },
      "chatbots.stack": { optionIds: ["azure", "custom"] },
      "chatbots.systems": { optionIds: ["itsm", "kb"] },
      "chatbots.quality": { optionIds: ["hallucination", "cost"] },
      "rag.exists": { optionIds: ["pilot"] },
      "rag.vector": { optionIds: ["azure-search"] },
      "rag.sources": { optionIds: ["files-nx", "sharepoint"] },
      "rag.sensitivity": { optionIds: ["high"] },
      "inference.providers": { optionIds: ["azure", "self"] },
      "inference.self": { optionIds: ["vllm"] },
      "inference.gpu": { optionIds: ["onprem-idle"] },
      "inference.spend": { optionIds: ["high"] },
      "inference.egress": { optionIds: ["uncomfortable"] },
      "agents.maturity": { optionIds: ["limited"] },
      "agents.frameworks": { optionIds: ["langchain"] },
      "agents.mcp": { optionIds: ["tools"] },
      "agents.identity": { optionIds: ["shared"] },
      "agents.runaway": { optionIds: ["near"] },
      "gov.policy": { optionIds: ["paper"] },
      "gov.reg": { optionIds: ["hipaa", "soc"] },
      "gov.airgap": { optionIds: ["planned"] },
      "gov.observe": { optionIds: ["partial"] },
      "path.pains": { optionIds: ["cost", "control", "risk"] },
      "path.timing": { optionIds: ["quarter"] },
      "path.budget": { optionIds: ["platform"] },
      "path.compete": { optionIds: ["azure", "diy"] },
    },
  };
}
