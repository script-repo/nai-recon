import { AXES, type Scores } from "@/lib/scoring";
import { cn } from "@/lib/utils";

export function ScoreBars({ scores, compact }: { scores: Scores; compact?: boolean }) {
  return (
    <div className={cn("grid gap-3", compact ? "gap-2" : "gap-3")}>
      {AXES.map((axis) => (
        <div key={axis.id}>
          <div className="mb-1 flex items-baseline justify-between gap-3">
            <span className={cn("text-xs font-medium text-fg")}>{axis.label}</span>
            <span className="font-mono text-xs tabular-nums text-muted">{scores[axis.id]}</span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-surface-2">
            <div
              className="h-full rounded-full bg-accent transition-[width] duration-500 ease-out"
              style={{ width: `${scores[axis.id]}%` }}
            />
          </div>
          {!compact ? <p className="mt-1 text-xs text-subtle">{axis.blurb}</p> : null}
        </div>
      ))}
    </div>
  );
}

export function OpportunityMeter({ value }: { value: number }) {
  const tone = value >= 70 ? "text-ok" : value >= 40 ? "text-warn" : "text-muted";
  return (
    <div className="flex items-end gap-3">
      <div>
        <p className="text-xs tracking-wide text-subtle uppercase">Opportunity</p>
        <p className={cn("font-display text-4xl font-semibold tabular-nums leading-none", tone)}>
          {value}
          <span className="ml-1 text-lg text-subtle">/100</span>
        </p>
      </div>
    </div>
  );
}
