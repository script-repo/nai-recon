import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { createFileRoute } from "@tanstack/react-router";
import { ArrowRight, Radar, Trash2 } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { STAGES } from "@/lib/playbook";
import { sampleEngagement } from "@/lib/sample";
import { answeredCount, opportunityScore, normalizedScores, computeScores } from "@/lib/scoring";
import { useEngagements } from "@/lib/store";
import { formatDate } from "@/lib/utils";

export const Route = createFileRoute("/")({ component: Home });

export function Home() {
  const navigate = useNavigate();
  const { engagements, importEngagement, removeEngagement } = useEngagements();
  const [openStage, setOpenStage] = useState(STAGES[0].id);
  const active = STAGES.find((s) => s.id === openStage) ?? STAGES[0];

  const loadSample = () => {
    removeEngagement("sample-meridian-health");
    const sample = sampleEngagement();
    importEngagement(sample);
    navigate({ to: "/e/$id/report", params: { id: sample.id } });
  };

  return (
    <AppShell
      actions={
        <Button asChild>
          <Link to="/new">
            New discovery <ArrowRight />
          </Link>
        </Button>
      }
    >
      <main>
        <section className="border-b border-line">
          <div className="mx-auto grid max-w-7xl gap-10 px-4 py-12 sm:px-6 lg:grid-cols-[1.15fr_0.85fr] lg:py-16">
            <div>
              <p className="font-mono text-xs tracking-widest text-accent uppercase">
                Channel SE field tool
              </p>
              <h1 className="mt-4 max-w-xl font-display text-4xl font-semibold tracking-tight text-fg sm:text-5xl">
                Uncover the AI estate. Then aim Nutanix at it.
              </h1>
              <p className="mt-5 max-w-xl text-base leading-relaxed text-muted">
                Recon walks a Nutanix channel SE through the eight conversations that surface
                shadow AI, chatbots, RAG, public inference, agents, and MCP sprawl — and turns the
                answers into a targeting report for Enterprise AI private inference and Agent
                Gateway.
              </p>
              <div className="mt-8 flex flex-wrap gap-3">
                <Button asChild size="lg">
                  <Link to="/new">Start a discovery</Link>
                </Button>
                <Button variant="secondary" size="lg" onClick={loadSample}>
                  <Radar /> Open sample report
                </Button>
              </div>
              <p className="mt-4 max-w-md text-xs leading-relaxed text-subtle">
                Built for live meetings. Ask the question on the left, read why it matters on the
                right, capture the answer, leave with a report the CISO can actually read.
              </p>
            </div>
            <MethodologyMap activeId={openStage} onSelect={setOpenStage} />
          </div>
        </section>

        <section className="border-b border-line bg-bg-elevated">
          <div className="mx-auto max-w-7xl px-4 py-10 sm:px-6">
            <p className="font-mono text-xs text-accent">{active.code}</p>
            <h2 className="mt-2 font-display text-2xl font-medium text-fg">{active.title}</h2>
            <p className="mt-3 max-w-3xl text-sm leading-relaxed text-muted">{active.briefing}</p>
            <p className="mt-4 max-w-3xl text-sm text-fg">
              <span className="text-subtle">Wedge · </span>
              {active.wedge}
            </p>
          </div>
        </section>

        <section className="mx-auto max-w-7xl px-4 py-12 sm:px-6">
          <div className="flex items-end justify-between gap-4">
            <div>
              <h2 className="font-display text-2xl font-medium">Engagements</h2>
              <p className="mt-1 text-sm text-muted">Stored on this device only.</p>
            </div>
          </div>
          {engagements.length === 0 ? (
            <div className="mt-8 rounded-xl border border-dashed border-line-strong px-6 py-14 text-center">
              <p className="font-display text-lg text-fg">No discoveries yet</p>
              <p className="mx-auto mt-2 max-w-md text-sm text-muted">
                Start a live capture, or open the Briarhaven Health sample to see a finished targeting
                report.
              </p>
            </div>
          ) : (
            <ul className="mt-8 grid gap-3">
              {engagements.map((e) => {
                const { done, total } = answeredCount(e);
                const score = opportunityScore(normalizedScores(computeScores(e)));
                return (
                  <li
                    key={e.id}
                    className="flex flex-col gap-4 rounded-xl border border-line bg-surface p-4 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0">
                      <p className="font-display text-lg font-medium text-fg">{e.customerName}</p>
                      <p className="mt-1 text-sm text-muted">
                        {e.industry || "Industry tbd"} · {formatDate(e.updatedAt)} · {done}/{total}{" "}
                        answered · {score}/100
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button asChild variant="secondary" size="sm">
                        <Link to="/e/$id" params={{ id: e.id }}>
                          Continue
                        </Link>
                      </Button>
                      <Button asChild size="sm">
                        <Link to="/e/$id/report" params={{ id: e.id }}>
                          Report
                        </Link>
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => removeEngagement(e.id)}
                        aria-label="Remove engagement"
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </main>
    </AppShell>
  );
}

function MethodologyMap({
  activeId,
  onSelect,
}: {
  activeId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="rounded-xl border border-line bg-surface p-4 sm:p-5">
      <p className="text-xs tracking-wide text-subtle uppercase">The eight conversations</p>
      <ol className="mt-4 space-y-1">
        {STAGES.map((stage) => {
          const active = stage.id === activeId;
          return (
            <li key={stage.id}>
              <button
                type="button"
                onClick={() => onSelect(stage.id)}
                className={`flex w-full items-start gap-3 rounded-lg px-2 py-2.5 text-left transition-colors duration-150 ${
                  active ? "bg-bg-elevated" : "hover:bg-bg-elevated/70"
                }`}
              >
                <span className="font-mono text-xs text-accent">{stage.code}</span>
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-fg">{stage.title}</span>
                  <span className="block text-xs text-subtle">{stage.short}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
