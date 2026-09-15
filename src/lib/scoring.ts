import {
  QUESTIONS,
  STAGES,
  visibleQuestions,
  questionById,
  type ScoreAxis,
} from "@/lib/playbook";
import type { Engagement } from "@/lib/store";

export type Scores = Record<ScoreAxis, number>;

export const AXES: { id: ScoreAxis; label: string; blurb: string }[] = [
  { id: "shadowRisk", label: "Shadow AI", blurb: "Unsanctioned usage and leakage exposure" },
  { id: "privateInferenceFit", label: "Private inference", blurb: "Fit for NAI on-prem / hybrid models" },
  { id: "agentGatewayFit", label: "Agent Gateway", blurb: "Fit for unified control of models and MCP" },
  { id: "urgency", label: "Urgency", blurb: "Incident, mandate, bill, or timeline pressure" },
  { id: "dataGravity", label: "Data gravity", blurb: "Corpus and files already in the customer DC" },
  { id: "governanceGap", label: "Governance gap", blurb: "Policy, identity, and observability holes" },
];

const EMPTY: Scores = {
  shadowRisk: 0,
  privateInferenceFit: 0,
  agentGatewayFit: 0,
  urgency: 0,
  dataGravity: 0,
  governanceGap: 0,
};

export function collectTags(engagement: Engagement): string[] {
  const tags = new Set<string>();
  for (const fp of engagement.nutanixFootprint) tags.add(`fp-${fp}`);
  if (engagement.industry.toLowerCase().includes("health")) tags.add("regulated");
  for (const [qid, answer] of Object.entries(engagement.answers)) {
    const q = questionById(qid);
    if (!q) continue;
    for (const oid of answer.optionIds) {
      const opt = q.options.find((o) => o.id === oid);
      opt?.tags.forEach((t) => tags.add(t));
    }
  }
  return [...tags];
}

export function computeScores(engagement: Engagement): Scores {
  const scores: Scores = { ...EMPTY };
  const visible = visibleQuestions(engagement.depth, engagement.answers);
  for (const q of visible) {
    const answer = engagement.answers[q.id];
    if (!answer || answer.skipped) continue;
    for (const oid of answer.optionIds) {
      const opt = q.options.find((o) => o.id === oid);
      if (!opt?.score) continue;
      for (const [axis, value] of Object.entries(opt.score) as [ScoreAxis, number][]) {
        scores[axis] += value;
      }
    }
  }
  return scores;
}

function clamp100(n: number) {
  return Math.max(0, Math.min(100, Math.round(n)));
}

export function normalizedScores(raw: Scores): Scores {
  // Empirical caps from the playbook's point totals so the bars read as 0–100.
  const caps: Scores = {
    shadowRisk: 70,
    privateInferenceFit: 140,
    agentGatewayFit: 150,
    urgency: 80,
    dataGravity: 70,
    governanceGap: 80,
  };
  const out = { ...EMPTY };
  (Object.keys(raw) as ScoreAxis[]).forEach((k) => {
    out[k] = clamp100((raw[k] / caps[k]) * 100);
  });
  return out;
}

export type Motion = "gateway" | "inference" | "dual";

export function recommendedMotion(norm: Scores): Motion {
  if (norm.agentGatewayFit > norm.privateInferenceFit + 12) return "gateway";
  if (norm.privateInferenceFit > norm.agentGatewayFit + 12) return "inference";
  return "dual";
}

export function opportunityScore(norm: Scores): number {
  const weighted =
    norm.privateInferenceFit * 0.28 +
    norm.agentGatewayFit * 0.28 +
    norm.urgency * 0.18 +
    norm.shadowRisk * 0.1 +
    norm.dataGravity * 0.08 +
    norm.governanceGap * 0.08;
  return clamp100(weighted);
}

export function answeredCount(engagement: Engagement) {
  const visible = visibleQuestions(engagement.depth, engagement.answers);
  const done = visible.filter((q) => {
    const a = engagement.answers[q.id];
    return a && (a.skipped || a.optionIds.length > 0);
  }).length;
  return { done, total: visible.length };
}

export function stageStatus(engagement: Engagement, stageId: string) {
  const visible = visibleQuestions(engagement.depth, engagement.answers).filter(
    (q) => q.stageId === stageId,
  );
  const done = visible.filter((q) => {
    const a = engagement.answers[q.id];
    return a && (a.skipped || a.optionIds.length > 0);
  }).length;
  return {
    done,
    total: visible.length,
    state: (visible.length === 0
      ? "empty"
      : done === 0
        ? "idle"
        : done >= visible.length
          ? "complete"
          : "partial") as "empty" | "idle" | "partial" | "complete",
  };
}

export function optionLabels(engagement: Engagement, questionId: string): string[] {
  const q = QUESTIONS.find((x) => x.id === questionId);
  const a = engagement.answers[questionId];
  if (!q || !a) return [];
  return a.optionIds
    .map((id) => q.options.find((o) => o.id === id)?.label)
    .filter((x): x is string => Boolean(x));
}

export function hasTag(tags: string[], ...need: string[]) {
  return need.some((t) => tags.includes(t));
}

export { STAGES };
