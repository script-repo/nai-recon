import { Link, createFileRoute, useParams } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { ReportView } from "@/components/report-view";
import { Button } from "@/components/ui/button";
import { useEngagements } from "@/lib/store";

export const Route = createFileRoute("/e/$id/report")({ component: ReportPage });

export function ReportPage() {
  const { id } = useParams({ from: "/e/$id/report" });
  const engagement = useEngagements((s) => s.engagements.find((e) => e.id === id));
  const setNarrative = useEngagements((s) => s.setNarrative);
  const hydrated = useEngagements((s) => s.hydrated);

  if (!engagement && !hydrated) {
    return (
      <AppShell>
        <main className="px-4 py-16 text-sm text-muted">Loading report…</main>
      </AppShell>
    );
  }

  if (!engagement) {
    return (
      <AppShell>
        <main className="mx-auto max-w-lg px-4 py-16">
          <h1 className="font-display text-2xl">Report not found</h1>
          <Button asChild className="mt-6">
            <Link to="/">Back to Recon</Link>
          </Button>
        </main>
      </AppShell>
    );
  }

  return (
    <AppShell
      trail={
        <span className="hidden sm:inline">
          {engagement.customerName}
          <span className="text-subtle"> · report</span>
        </span>
      }
      actions={
        <Button asChild variant="secondary" size="sm">
          <Link to="/e/$id" params={{ id: engagement.id }}>
            <ArrowLeft /> Discovery
          </Link>
        </Button>
      }
    >
      <ReportView
        engagement={engagement}
        onNarrative={(text) => setNarrative(engagement.id, text)}
      />
    </AppShell>
  );
}
