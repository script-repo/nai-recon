import { useState, type FormEvent, type ReactNode } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  INDUSTRIES,
  MEETING_TYPES,
  NUTANIX_FOOTPRINT,
  REGIONS,
  type Depth,
} from "@/lib/playbook";
import { useEngagements } from "@/lib/store";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/new")({ component: NewEngagement });

export function NewEngagement() {
  const navigate = useNavigate();
  const createEngagement = useEngagements((s) => s.createEngagement);
  const [seName, setSeName] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [industry, setIndustry] = useState("");
  const [region, setRegion] = useState("North America");
  const [meetingType, setMeetingType] = useState("first");
  const [footprint, setFootprint] = useState<string[]>([]);
  const [depth, setDepth] = useState<Depth>("core");

  const toggleFp = (id: string) => {
    setFootprint((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!customerName.trim()) return;
    const created = createEngagement({
      seName,
      customerName,
      industry,
      region,
      meetingType,
      nutanixFootprint: footprint,
      depth,
    });
    navigate({ to: "/e/$id", params: { id: created.id } });
  };

  return (
    <AppShell trail={<span>New discovery</span>}>
      <main className="mx-auto max-w-2xl px-4 py-10 sm:px-6">
        <p className="font-mono text-xs tracking-widest text-accent uppercase">Briefing</p>
        <h1 className="mt-2 font-display text-3xl font-semibold">Frame the account first</h1>
        <p className="mt-3 text-sm leading-relaxed text-muted">
          Two minutes of setup so the questions and the report carry the right names. You can change
          depth later if the meeting goes long.
        </p>

        <form onSubmit={submit} className="mt-8 space-y-6">
          <Field label="Customer / account" htmlFor="customer">
            <Input
              id="customer"
              required
              value={customerName}
              onChange={(e) => setCustomerName(e.target.value)}
              placeholder="Meridian Health"
            />
          </Field>
          <Field label="Your name" htmlFor="se">
            <Input
              id="se"
              value={seName}
              onChange={(e) => setSeName(e.target.value)}
              placeholder="Alex Rivera"
            />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Industry" htmlFor="industry">
              <Select id="industry" value={industry} onChange={setIndustry}>
                <option value="">Select…</option>
                {INDUSTRIES.map((i) => (
                  <option key={i} value={i}>
                    {i}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Region" htmlFor="region">
              <Select id="region" value={region} onChange={setRegion}>
                {REGIONS.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Field label="Meeting type" htmlFor="meeting">
            <Select id="meeting" value={meetingType} onChange={setMeetingType}>
              {MEETING_TYPES.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </Select>
          </Field>
          <div>
            <p className="text-sm font-medium text-fg">Nutanix already in the account</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {NUTANIX_FOOTPRINT.map((fp) => {
                const on = footprint.includes(fp.id);
                return (
                  <button
                    key={fp.id}
                    type="button"
                    onClick={() => toggleFp(fp.id)}
                    className={cn(
                      "min-h-11 rounded-full border px-3 text-sm transition-colors duration-150",
                      on
                        ? "border-accent/50 bg-accent/15 text-fg"
                        : "border-line bg-surface text-muted hover:text-fg",
                    )}
                  >
                    {fp.label}
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <p className="text-sm font-medium text-fg">Question depth</p>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              {(
                [
                  { id: "core" as const, label: "Core", hint: "About 20 questions. Live meeting default." },
                  { id: "deep" as const, label: "Deep dive", hint: "Full playbook, including incidents and budget." },
                ] as const
              ).map((d) => (
                <button
                  key={d.id}
                  type="button"
                  onClick={() => setDepth(d.id)}
                  className={cn(
                    "rounded-lg border px-4 py-3 text-left transition-colors duration-150",
                    depth === d.id
                      ? "border-accent/50 bg-accent/10"
                      : "border-line bg-surface hover:border-line-strong",
                  )}
                >
                  <span className="block text-sm font-medium text-fg">{d.label}</span>
                  <span className="mt-1 block text-xs text-subtle">{d.hint}</span>
                </button>
              ))}
            </div>
          </div>
          <Button type="submit" size="lg" className="w-full sm:w-auto">
            Open the workflow
          </Button>
        </form>
      </main>
    </AppShell>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}

function Select({
  id,
  value,
  onChange,
  children,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
}) {
  return (
    <select
      id={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="flex h-11 w-full rounded-md border border-line bg-bg-elevated px-3 text-sm text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70"
    >
      {children}
    </select>
  );
}
