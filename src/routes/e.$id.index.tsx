import { Link, createFileRoute, useParams } from "@tanstack/react-router";
import { FileBarChart, Layers } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { QuestionFlow } from "@/components/question-flow";
import { StageRail } from "@/components/stage-rail";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { answeredCount } from "@/lib/scoring";
import { useEngagements } from "@/lib/store";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/e/$id/")({ component: EngagementPage });

export function EngagementPage() {
  const { id } = useParams({ from: "/e/$id/" });
  const engagement = useEngagements((s) => s.engagements.find((e) => e.id === id));
  const setAnswer = useEngagements((s) => s.setAnswer);
  const setStageNotes = useEngagements((s) => s.setStageNotes);
  const setCurrentStage = useEngagements((s) => s.setCurrentStage);
  const updateEngagement = useEngagements((s) => s.updateEngagement);
  const hydrated = useEngagements((s) => s.hydrated);

  if (!engagement && !hydrated) {
    return (
      <AppShell>
        <main className="mx-auto max-w-7xl px-4 py-16 text-sm text-muted">Loading engagement…</main>
      </AppShell>
    );
  }

  if (!engagement) {
    return (
      <AppShell>
        <main className="mx-auto max-w-lg px-4 py-16">
          <h1 className="font-display text-2xl">Discovery not found</h1>
          <p className="mt-2 text-sm text-muted">It may have been removed from this device.</p>
          <Button asChild className="mt-6">
            <Link to="/">Back to Recon</Link>
          </Button>
        </main>
      </AppShell>
    );
  }

  const { done, total } = answeredCount(engagement);
  const pct = total ? Math.round((done / total) * 100) : 0;

  return (
    <AppShell
      trail={
        <span className="hidden sm:inline">
          {engagement.customerName}
          <span className="text-subtle"> · discovery</span>
        </span>
      }
      actions={
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() =>
              updateEngagement(engagement.id, {
                depth: engagement.depth === "core" ? "deep" : "core",
              })
            }
            className={cn(
              "hidden h-11 items-center gap-2 rounded-md border px-3 text-xs sm:inline-flex",
              engagement.depth === "deep"
                ? "border-accent/40 text-accent"
                : "border-line text-muted hover:text-fg",
            )}
          >
            <Layers className="size-3.5" />
            {engagement.depth === "deep" ? "Deep dive on" : "Core"}
          </button>
          <Button asChild variant="secondary" size="sm">
            <Link to="/e/$id/report" params={{ id: engagement.id }}>
              <FileBarChart /> Report
            </Link>
          </Button>
        </div>
      }
    >
      <div className="border-b border-line bg-bg-elevated">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-3 sm:px-6">
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-3">
              <p className="truncate font-display text-sm font-medium">{engagement.customerName}</p>
              <p className="font-mono text-xs tabular-nums text-muted">
                {done}/{total}
              </p>
            </div>
            <Progress className="mt-2" value={pct} />
          </div>
        </div>
      </div>
      <div className="mx-auto grid max-w-7xl gap-8 px-4 py-6 sm:px-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
        <div className="lg:hidden">
          <StageRail
            engagement={engagement}
            currentId={engagement.currentStageId}
            onSelect={(sid) => setCurrentStage(engagement.id, sid)}
            orientation="horizontal"
          />
        </div>
        <aside className="no-print hidden lg:block">
          <div className="sticky top-20">
            <StageRail
              engagement={engagement}
              currentId={engagement.currentStageId}
              onSelect={(sid) => setCurrentStage(engagement.id, sid)}
            />
          </div>
        </aside>
        <main className="min-w-0 pb-16">
          <QuestionFlow
            key={engagement.currentStageId}
            engagement={engagement}
            onAnswer={(qid, answer) => setAnswer(engagement.id, qid, answer)}
            onNotes={(sid, notes) => setStageNotes(engagement.id, sid, notes)}
            onStage={(sid) => setCurrentStage(engagement.id, sid)}
          />
        </main>
      </div>
    </AppShell>
  );
}
