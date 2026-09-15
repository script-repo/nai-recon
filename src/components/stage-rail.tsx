import { STAGES } from "@/lib/playbook";
import { stageStatus } from "@/lib/scoring";
import type { Engagement } from "@/lib/store";
import { cn } from "@/lib/utils";

export function StageRail({
  engagement,
  currentId,
  onSelect,
  orientation = "vertical",
}: {
  engagement: Engagement;
  currentId: string;
  onSelect: (id: string) => void;
  orientation?: "vertical" | "horizontal";
}) {
  if (orientation === "horizontal") {
    return (
      <nav
        aria-label="Discovery stages"
        className="no-print flex gap-2 overflow-x-auto pb-1 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {STAGES.map((stage) => {
          const status = stageStatus(engagement, stage.id);
          const active = stage.id === currentId;
          return (
            <button
              key={stage.id}
              type="button"
              onClick={() => onSelect(stage.id)}
              className={cn(
                "flex min-w-36 shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-left transition-colors duration-150",
                active
                  ? "border-accent/40 bg-surface text-fg"
                  : "border-line bg-bg-elevated text-muted hover:border-line-strong hover:text-fg",
              )}
            >
              <StatusDot state={status.state} />
              <span className="min-w-0">
                <span className="block font-mono text-xs text-subtle">{stage.code}</span>
                <span className="block truncate text-xs font-medium">{stage.title}</span>
              </span>
            </button>
          );
        })}
      </nav>
    );
  }

  return (
    <nav aria-label="Discovery stages" className="relative">
      <div className="absolute top-3 bottom-3 left-4 w-px bg-line" aria-hidden />
      <ol className="relative space-y-1">
        {STAGES.map((stage) => {
          const status = stageStatus(engagement, stage.id);
          const active = stage.id === currentId;
          return (
            <li key={stage.id}>
              <button
                key={stage.id}
                type="button"
                onClick={() => onSelect(stage.id)}
                className={cn(
                  "flex w-full items-start gap-3 rounded-lg px-1.5 py-2 text-left transition-colors duration-150",
                  active ? "bg-surface" : "hover:bg-surface/60",
                )}
              >
                <StatusDot state={active ? "active" : status.state} />
                <span className="min-w-0 flex-1">
                  <span className="flex items-baseline justify-between gap-2">
                    <span
                      className={cn(
                        "font-display text-sm font-medium",
                        active ? "text-fg" : "text-muted",
                      )}
                    >
                      {stage.title}
                    </span>
                    <span className="font-mono text-xs tabular-nums text-subtle">
                      {status.total ? `${status.done}/${status.total}` : "—"}
                    </span>
                  </span>
                  <span className="mt-0.5 block text-xs text-subtle">{stage.short}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function StatusDot({
  state,
}: {
  state: "empty" | "idle" | "partial" | "complete" | "active";
}) {
  return (
    <span
      className={cn(
        "relative z-10 mt-0.5 size-3.5 shrink-0 rounded-full border-2 bg-bg",
        state === "complete" && "border-ok bg-ok",
        state === "partial" && "border-accent bg-accent/40",
        state === "active" && "border-accent bg-accent",
        (state === "idle" || state === "empty") && "border-line-strong",
      )}
      aria-hidden
    />
  );
}
