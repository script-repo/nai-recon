import { useEffect, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useEngagements } from "@/lib/store";
import { cn } from "@/lib/utils";

export function Mark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      width="28"
      height="28"
      className={cn("mark size-7 text-surface-2", className)}
      aria-hidden="true"
    >
      <rect width="32" height="32" rx="8" fill="currentColor" />
      <g className="text-accent" fill="none" stroke="currentColor">
        <circle cx="16" cy="16" r="9" strokeWidth="1.6" />
        <path d="M16 4v4.5M16 23.5V28M4 16h4.5M23.5 16H28" strokeWidth="1.4" />
      </g>
      <circle cx="16" cy="16" r="2.2" className="fill-accent" />
    </svg>
  );
}

export function AppShell({
  children,
  trail,
  actions,
}: {
  children: ReactNode;
  trail?: ReactNode;
  actions?: ReactNode;
}) {
  useEffect(() => {
    let cancelled = false;
    const mark = () => {
      if (!cancelled) useEngagements.setState({ hydrated: true });
    };
    const unsub = useEngagements.persist.onFinishHydration(mark);
    try {
      void Promise.resolve(useEngagements.persist.rehydrate()).then(mark, mark);
    } catch {
      mark();
    }
    const timeout = window.setTimeout(mark, 400);
    return () => {
      cancelled = true;
      unsub();
      window.clearTimeout(timeout);
    };
  }, []);

  return (
    <div className="min-h-dvh bg-bg text-fg">
      <header className="no-print sticky top-0 z-30 border-b border-line bg-bg/90 backdrop-blur-sm">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-3 px-4 sm:px-6">
          <Link to="/" className="flex items-center gap-2.5 text-fg">
            <Mark />
            <span className="font-display text-sm font-semibold tracking-tight">Recon</span>
          </Link>
          <span className="hidden text-xs text-subtle sm:inline">Nutanix Enterprise AI</span>
          {trail ? (
            <div className="min-w-0 flex-1 truncate text-sm text-muted">{trail}</div>
          ) : (
            <div className="flex-1" />
          )}
          <div className="flex items-center gap-2">{actions}</div>
        </div>
      </header>
      {children}
    </div>
  );
}
