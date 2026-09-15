import { create } from "zustand";
import { persist } from "zustand/middleware";
import { uid } from "@/lib/utils";
import type { Depth } from "@/lib/playbook";
import { STAGES } from "@/lib/playbook";

export type Answer = {
  optionIds: string[];
  text?: string;
  skipped?: boolean;
};

export type Engagement = {
  id: string;
  createdAt: string;
  updatedAt: string;
  seName: string;
  customerName: string;
  industry: string;
  region: string;
  meetingType: string;
  nutanixFootprint: string[];
  depth: Depth;
  answers: Record<string, Answer>;
  stageNotes: Record<string, string>;
  currentStageId: string;
  narrative?: string;
};

type State = {
  engagements: Engagement[];
  hydrated: boolean;
  setHydrated: (v: boolean) => void;
  createEngagement: (input: {
    seName: string;
    customerName: string;
    industry: string;
    region: string;
    meetingType: string;
    nutanixFootprint: string[];
    depth: Depth;
  }) => Engagement;
  importEngagement: (engagement: Engagement) => void;
  updateEngagement: (id: string, patch: Partial<Engagement>) => void;
  removeEngagement: (id: string) => void;
  setAnswer: (id: string, questionId: string, answer: Answer) => void;
  setStageNotes: (id: string, stageId: string, notes: string) => void;
  setCurrentStage: (id: string, stageId: string) => void;
  setNarrative: (id: string, narrative: string) => void;
  get: (id: string) => Engagement | undefined;
};

function touch(e: Engagement, patch: Partial<Engagement>): Engagement {
  return { ...e, ...patch, updatedAt: new Date().toISOString() };
}

function scrubLegacySample(e: Engagement): Engagement {
  const meridian =
    e.id === "sample-meridian-health" || /meridian/i.test(e.customerName ?? "");
  if (!meridian) return e;
  return {
    ...e,
    id: e.id === "sample-meridian-health" ? "sample-briarhaven-health" : e.id,
    customerName: "Briarhaven Health",
    seName: e.seName === "Alex Rivera" || e.id === "sample-meridian-health" ? "Casey Lang" : e.seName,
  };
}

function scrubEngagements(engagements: Engagement[]): Engagement[] {
  const seen = new Set<string>();
  const next: Engagement[] = [];
  for (const raw of engagements) {
    const e = scrubLegacySample(raw);
    if (seen.has(e.id)) continue;
    seen.add(e.id);
    next.push(e);
  }
  return next;
}

export const useEngagements = create<State>()(
  persist(
    (set, get) => ({
      engagements: [],
      hydrated: false,
      setHydrated: (v) => set({ hydrated: v }),
      createEngagement: (input) => {
        const now = new Date().toISOString();
        const engagement: Engagement = {
          id: uid(),
          createdAt: now,
          updatedAt: now,
          seName: input.seName.trim(),
          customerName: input.customerName.trim(),
          industry: input.industry,
          region: input.region,
          meetingType: input.meetingType,
          nutanixFootprint: input.nutanixFootprint,
          depth: input.depth,
          answers: {},
          stageNotes: {},
          currentStageId: STAGES[0].id,
        };
        set((s) => ({ engagements: [engagement, ...s.engagements] }));
        return engagement;
      },
      importEngagement: (engagement) => {
        set((s) => ({
          engagements: [engagement, ...s.engagements.filter((e) => e.id !== engagement.id)],
        }));
      },
      updateEngagement: (id, patch) => {
        set((s) => ({
          engagements: s.engagements.map((e) => (e.id === id ? touch(e, patch) : e)),
        }));
      },
      removeEngagement: (id) => {
        set((s) => ({ engagements: s.engagements.filter((e) => e.id !== id) }));
      },
      setAnswer: (id, questionId, answer) => {
        set((s) => ({
          engagements: s.engagements.map((e) =>
            e.id === id
              ? touch(e, { answers: { ...e.answers, [questionId]: answer } })
              : e,
          ),
        }));
      },
      setStageNotes: (id, stageId, notes) => {
        set((s) => ({
          engagements: s.engagements.map((e) =>
            e.id === id
              ? touch(e, { stageNotes: { ...e.stageNotes, [stageId]: notes } })
              : e,
          ),
        }));
      },
      setCurrentStage: (id, stageId) => {
        set((s) => ({
          engagements: s.engagements.map((e) =>
            e.id === id ? touch(e, { currentStageId: stageId }) : e,
          ),
        }));
      },
      setNarrative: (id, narrative) => {
        set((s) => ({
          engagements: s.engagements.map((e) =>
            e.id === id ? touch(e, { narrative }) : e,
          ),
        }));
      },
      get: (id) => get().engagements.find((e) => e.id === id),
    }),
    {
      name: "nai-recon-engagements",
      skipHydration: true,
      version: 2,
      partialize: (s) => ({ engagements: s.engagements }),
      migrate: (persisted) => {
        const prev = persisted as { engagements?: Engagement[] };
        return { engagements: scrubEngagements(prev.engagements ?? []) };
      },
      merge: (persisted, current) => {
        const prev = persisted as { engagements?: Engagement[] } | undefined;
        return {
          ...current,
          ...prev,
          engagements: scrubEngagements(prev?.engagements ?? current.engagements),
        };
      },
    },
  ),
);
