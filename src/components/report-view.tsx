import { useState, type ReactNode } from "react";
import { Check, Copy, PenLine, Printer } from "lucide-react";
import { Button } from "@/components/ui/button";
import { OpportunityMeter, ScoreBars } from "@/components/score-bars";
import { STAGES } from "@/lib/playbook";
import { buildReport, reportMarkdown, snapshotForModel } from "@/lib/report";
import type { Engagement } from "@/lib/store";

export function ReportView({
  engagement,
  onNarrative,
}: {
  engagement: Engagement;
  onNarrative: (text: string) => void;
}) {
  const report = buildReport(engagement);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const copy = async () => {
    const md = reportMarkdown(engagement, report);
    await navigator.clipboard.writeText(md);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  };

  const polish = async () => {
    if (import.meta.env.VITE_PAGES === "true") {
      setError("AI polish is not available on the static GitHub Pages build. The report above is complete without it.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { polishNarrative } = await import("@/lib/ai");
      const result = await polishNarrative({ data: { snapshot: snapshotForModel(engagement) } });
      if (!result.ok) setError(result.error);
      else onNarrative(result.text);
    } catch {
      setError("Could not reach the writing assistant.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="mx-auto max-w-4xl px-4 py-8 sm:px-6 sm:py-10">
      <header className="border-b border-line pb-8">
        <p className="font-mono text-xs tracking-widest text-accent uppercase">
          Channel SE discovery report
        </p>
        <h1 className="mt-3 font-display text-4xl font-semibold tracking-tight text-fg sm:text-5xl">
          {engagement.customerName}
        </h1>
        <p className="mt-3 text-sm text-muted">
          {engagement.industry || "Industry tbd"} · {engagement.region || "Region tbd"} · Prepared by{" "}
          {engagement.seName || "Channel SE"}
        </p>
        <div className="mt-8 flex flex-wrap items-end justify-between gap-6">
          <OpportunityMeter value={report.opportunity} />
          <p className="max-w-sm text-sm leading-relaxed text-muted">{report.motionTitle}.</p>
        </div>
        <div className="no-print mt-6 flex flex-wrap gap-2">
          <Button variant="secondary" onClick={copy}>
            {copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy markdown"}
          </Button>
          <Button variant="secondary" onClick={() => window.print()}>
            <Printer /> Print
          </Button>
          <Button variant="outline" onClick={polish} disabled={busy}>
            <PenLine /> {busy ? "Writing…" : "Polish targeting memo"}
          </Button>
        </div>
        {error ? <p className="mt-3 text-sm text-danger">{error}</p> : null}
      </header>

      {engagement.narrative ? (
        <Section code="00" title="Targeting memo">
          <div className="space-y-3 text-sm leading-relaxed text-fg">
            {engagement.narrative.split("\n").filter(Boolean).map((p, i) => (
              <p key={i}>{p}</p>
            ))}
          </div>
        </Section>
      ) : null}

      <Section code="01" title="AI estate as heard">
        <ul className="space-y-2">
          {report.estate.map((line) => (
            <li key={line} className="text-sm leading-relaxed text-fg">
              {line}
            </li>
          ))}
        </ul>
      </Section>

      <Section code="02" title="Risk if they do nothing">
        <ul className="space-y-3">
          {report.risks.map((line) => (
            <li key={line} className="border-l-2 border-danger/50 pl-3 text-sm leading-relaxed text-fg">
              {line}
            </li>
          ))}
        </ul>
      </Section>

      <Section code="03" title="Fit scores">
        <ScoreBars scores={report.scores} />
      </Section>

      <Section code="04" title="Why Nutanix private inference">
        <ul className="space-y-3">
          {report.inferenceLead.map((line) => (
            <li key={line} className="text-sm leading-relaxed text-fg">
              {line}
            </li>
          ))}
        </ul>
      </Section>

      <Section code="05" title="Why Nutanix Agent Gateway">
        <ul className="space-y-3">
          {report.gatewayLead.map((line) => (
            <li key={line} className="text-sm leading-relaxed text-fg">
              {line}
            </li>
          ))}
        </ul>
      </Section>

      <Section code="06" title="Recommended POC">
        <h3 className="font-display text-lg font-medium text-fg">{report.poc.title}</h3>
        <ol className="mt-4 space-y-2">
          {report.poc.steps.map((step, i) => (
            <li key={step} className="flex gap-3 text-sm leading-relaxed text-fg">
              <span className="font-mono text-xs text-accent">{String(i + 1).padStart(2, "0")}</span>
              <span>{step}</span>
            </li>
          ))}
        </ol>
        <p className="mt-5 text-xs font-medium tracking-wide text-subtle uppercase">Success looks like</p>
        <ul className="mt-2 space-y-1.5">
          {report.poc.success.map((s) => (
            <li key={s} className="text-sm text-muted">
              {s}
            </li>
          ))}
        </ul>
      </Section>

      <Section code="07" title="Talk track">
        <div className="space-y-3">
          {report.talkTrack.map((line) => (
            <p key={line} className="text-sm leading-relaxed text-fg">
              {line}
            </p>
          ))}
        </div>
      </Section>

      <Section code="08" title="Next meeting">
        <ol className="space-y-2">
          {report.nextMeeting.map((line, i) => (
            <li key={line} className="flex gap-3 text-sm leading-relaxed text-fg">
              <span className="font-mono text-xs text-accent">{String(i + 1).padStart(2, "0")}</span>
              <span>{line}</span>
            </li>
          ))}
        </ol>
      </Section>

      <Section code="09" title="Handle these objections">
        <div className="space-y-4">
          {report.objections.map((o) => (
            <div key={o.objection} className="rounded-lg border border-line bg-surface p-4">
              <p className="text-sm font-medium text-fg">{o.objection}</p>
              <p className="mt-2 text-sm leading-relaxed text-muted">{o.reply}</p>
            </div>
          ))}
        </div>
      </Section>

      {report.gaps.length ? (
        <Section code="10" title="Still unknown">
          <p className="mb-3 text-sm text-muted">
            Core questions not captured. Do not pretend the estate map is complete.
          </p>
          <ul className="space-y-1.5">
            {report.gaps.map((g) => (
              <li key={g.prompt} className="text-sm text-fg">
                <span className="text-subtle">{g.stage} · </span>
                {g.prompt}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {STAGES.some((s) => engagement.stageNotes[s.id]?.trim()) ? (
        <Section code="11" title="SE notes">
          <div className="space-y-4">
            {STAGES.map((s) => {
              const n = engagement.stageNotes[s.id]?.trim();
              if (!n) return null;
              return (
                <div key={s.id}>
                  <p className="text-xs tracking-wide text-subtle uppercase">{s.title}</p>
                  <p className="mt-1 text-sm leading-relaxed text-fg">{n}</p>
                </div>
              );
            })}
          </div>
        </Section>
      ) : null}
    </article>
  );
}

function Section({
  code,
  title,
  children,
}: {
  code: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="border-b border-line py-8">
      <div className="mb-4 flex items-baseline gap-3">
        <span className="font-mono text-xs text-accent">{code}</span>
        <h2 className="font-display text-xl font-medium text-fg">{title}</h2>
      </div>
      {children}
    </section>
  );
}
