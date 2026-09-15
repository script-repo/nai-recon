import { useMemo, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  FileText,
  SkipForward,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  STAGES,
  questionsForStage,
  isQuestionVisible,
  stageById,
  type Question,
} from "@/lib/playbook";
import type { Answer, Engagement } from "@/lib/store";
import { cn } from "@/lib/utils";

export function QuestionFlow({
  engagement,
  onAnswer,
  onNotes,
  onStage,
}: {
  engagement: Engagement;
  onAnswer: (questionId: string, answer: Answer) => void;
  onNotes: (stageId: string, notes: string) => void;
  onStage: (stageId: string) => void;
}) {
  const stage = stageById(engagement.currentStageId) ?? STAGES[0];
  const visible = useMemo(
    () =>
      questionsForStage(stage.id, engagement.depth).filter((q) =>
        isQuestionVisible(q, engagement.answers),
      ),
    [stage.id, engagement.depth, engagement.answers],
  );

  const [cursor, setCursor] = useState(0);
  const safeCursor = Math.min(cursor, Math.max(0, visible.length - 1));
  const question = visible[safeCursor];

  const go = (next: number) => {
    if (next < 0) {
      const idx = STAGES.findIndex((s) => s.id === stage.id);
      if (idx > 0) {
        onStage(STAGES[idx - 1].id);
        setCursor(99);
      }
      return;
    }
    if (next >= visible.length) {
      const idx = STAGES.findIndex((s) => s.id === stage.id);
      if (idx < STAGES.length - 1) {
        onStage(STAGES[idx + 1].id);
        setCursor(0);
      }
      return;
    }
    setCursor(next);
  };

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <div>
        <p className="font-mono text-xs tracking-widest text-accent uppercase">
          Stage {stage.code} · {stage.title}
        </p>
        <h1 className="mt-2 font-display text-2xl font-semibold text-fg sm:text-3xl">
          {stage.intent}
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-muted">{stage.briefing}</p>

        {visible.length === 0 ? (
          <p className="mt-8 text-sm text-muted">
            No questions in this stage for the current answers. Continue to the next stage.
          </p>
        ) : (
          <>
            <ol className="mt-6 flex flex-wrap gap-1.5">
              {visible.map((q, i) => {
                const a = engagement.answers[q.id];
                const done = Boolean(a && (a.skipped || a.optionIds.length > 0));
                return (
                  <li key={q.id}>
                    <button
                      type="button"
                      onClick={() => setCursor(i)}
                      className={cn(
                        "size-8 rounded-md border font-mono text-xs tabular-nums transition-colors duration-150",
                        i === safeCursor
                          ? "border-accent bg-accent text-accent-fg"
                          : done
                            ? "border-ok/40 bg-ok/15 text-ok"
                            : "border-line bg-surface text-muted hover:border-line-strong",
                      )}
                      aria-label={`Question ${i + 1}`}
                    >
                      {i + 1}
                    </button>
                  </li>
                );
              })}
            </ol>

            {question ? (
              <QuestionCard
                key={question.id}
                question={question}
                answer={engagement.answers[question.id]}
                index={safeCursor}
                total={visible.length}
                onAnswer={(answer) => onAnswer(question.id, answer)}
                onPrev={() => go(safeCursor - 1)}
                onNext={() => go(safeCursor + 1)}
              />
            ) : null}
          </>
        )}

        <div className="mt-8">
          <label className="text-xs font-medium tracking-wide text-subtle uppercase">
            SE notes for this stage
          </label>
          <Textarea
            className="mt-2"
            rows={3}
            value={engagement.stageNotes[stage.id] ?? ""}
            onChange={(e) => onNotes(stage.id, e.target.value)}
            placeholder="Quotes, names, political landmines, anything that should land in the report…"
          />
        </div>
      </div>

      {question ? <CoachPanel question={question} stageWedge={stage.wedge} /> : <StageCoach stageId={stage.id} />}
    </div>
  );
}

function QuestionCard({
  question,
  answer,
  index,
  total,
  onAnswer,
  onPrev,
  onNext,
}: {
  question: Question;
  answer?: Answer;
  index: number;
  total: number;
  onAnswer: (answer: Answer) => void;
  onPrev: () => void;
  onNext: () => void;
}) {
  const selected = answer?.optionIds ?? [];
  const toggle = (optionId: string) => {
    if (question.type === "single") {
      onAnswer({ optionIds: [optionId], text: answer?.text, skipped: false });
      return;
    }
    const next = selected.includes(optionId)
      ? selected.filter((id) => id !== optionId)
      : [...selected, optionId];
    onAnswer({ optionIds: next, text: answer?.text, skipped: false });
  };

  return (
    <section className="mt-8 rounded-xl border border-line bg-bg-elevated p-5 sm:p-6">
      <p className="font-mono text-xs text-subtle">
        {String(index + 1).padStart(2, "0")} / {String(total).padStart(2, "0")}
        {question.depth === "deep" ? " · Deep dive" : ""}
      </p>
      <h2 className="mt-2 font-display text-xl font-medium text-fg">{question.prompt}</h2>
      <p className="mt-3 border-l-2 border-accent/40 pl-3 text-sm leading-relaxed text-muted">
        {question.seScript}
      </p>

      <ul className="mt-5 grid gap-2">
        {question.options.map((opt) => {
          const on = selected.includes(opt.id);
          return (
            <li key={opt.id}>
              <button
                type="button"
                onClick={() => toggle(opt.id)}
                className={cn(
                  "flex min-h-12 w-full items-start gap-3 rounded-lg border px-3 py-3 text-left transition-colors duration-150",
                  on
                    ? "border-accent/50 bg-accent/10 text-fg"
                    : "border-line bg-surface text-muted hover:border-line-strong hover:text-fg",
                )}
              >
                <span
                  className={cn(
                    "mt-0.5 flex size-4 shrink-0 items-center justify-center border",
                    question.type === "single" ? "rounded-full" : "rounded-xs",
                    on ? "border-accent bg-accent" : "border-line-strong",
                  )}
                />
                <span>
                  <span className="block text-sm font-medium text-fg">{opt.label}</span>
                  {opt.hint ? <span className="mt-0.5 block text-xs text-subtle">{opt.hint}</span> : null}
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      <Textarea
        className="mt-4"
        rows={2}
        value={answer?.text ?? ""}
        onChange={(e) =>
          onAnswer({
            optionIds: selected,
            text: e.target.value,
            skipped: answer?.skipped,
          })
        }
        placeholder="Optional capture — names, product versions, exact quotes"
      />

      <div className="mt-5 flex flex-wrap items-center gap-2">
        <Button variant="secondary" onClick={onPrev}>
          <ChevronLeft /> Back
        </Button>
        <Button onClick={onNext}>
          Continue <ChevronRight />
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            onAnswer({ optionIds: selected, text: answer?.text, skipped: true });
            onNext();
          }}
        >
          <SkipForward /> Skip
        </Button>
      </div>
    </section>
  );
}

function CoachPanel({ question, stageWedge }: { question: Question; stageWedge: string }) {
  return (
    <aside className="rounded-xl border border-line bg-surface p-5 lg:sticky lg:top-20 lg:h-fit">
      <p className="flex items-center gap-2 text-xs font-medium tracking-wide text-subtle uppercase">
        <CircleHelp className="size-3.5" /> Why you are asking
      </p>
      <p className="mt-3 text-sm leading-relaxed text-fg">{question.why}</p>

      <p className="mt-5 text-xs font-medium tracking-wide text-subtle uppercase">Listen for</p>
      <ul className="mt-2 space-y-1.5">
        {question.listenFor.map((item) => (
          <li key={item} className="flex gap-2 text-sm text-muted">
            <span className="mt-2 size-1 shrink-0 rounded-full bg-accent" />
            <span>{item}</span>
          </li>
        ))}
      </ul>

      {question.followUps.length ? (
        <>
          <p className="mt-5 text-xs font-medium tracking-wide text-subtle uppercase">If they bite</p>
          <ul className="mt-2 space-y-1.5">
            {question.followUps.map((item) => (
              <li key={item} className="text-sm text-muted">
                {item}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <div className="mt-5 rounded-lg border border-accent/20 bg-accent/5 p-3">
        <p className="flex items-center gap-2 text-xs font-medium tracking-wide text-accent uppercase">
          <FileText className="size-3.5" /> Nutanix wedge
        </p>
        <p className="mt-2 text-sm leading-relaxed text-fg">{question.nutanixHook}</p>
        <p className="mt-2 text-xs text-subtle">{stageWedge}</p>
      </div>
    </aside>
  );
}

function StageCoach({ stageId }: { stageId: string }) {
  const stage = stageById(stageId);
  if (!stage) return null;
  return (
    <aside className="rounded-xl border border-line bg-surface p-5">
      <p className="text-xs font-medium tracking-wide text-subtle uppercase">Stage wedge</p>
      <p className="mt-3 text-sm leading-relaxed text-fg">{stage.wedge}</p>
    </aside>
  );
}
