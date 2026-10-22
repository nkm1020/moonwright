"use client";
import { useEffect, useState } from "react";
import type { Failure, LoopStep, PlanResult, Stage } from "@/lib/types";
import { STAGE_LABELS } from "@/lib/palette";
import {
  conflictMeaning,
  fmtCompact,
  fmtHours,
  fmtSeconds,
  plural,
  shortModel,
  totalUsage,
} from "@/lib/format";
import { Badge, Button, Card, Dot, IconCheck, IconX, Notice, cx } from "./Section";

export interface ProgressTraceProps {
  steps: LoopStep[];
  running: boolean;
  /** Date.now() when the run started; drives the live elapsed counter. */
  startedAt: number | null;
  result: PlanResult | null;
  error: string | null;
  onCancel?: () => void;
}

/** The stage the planner is expected to run next, given the steps seen so far. */
export function expectedNextStage(steps: LoopStep[]): Stage {
  const last = steps[steps.length - 1];
  if (!last) return "structure";
  switch (last.stage) {
    case "structure":
      return last.ok ? "taskgraph" : "structure";
    case "taskgraph":
      return last.ok ? "validate" : "taskgraph";
    case "repair":
      return last.ok ? "validate" : "repair";
    case "validate":
      return last.ok ? "schedule" : "repair";
    case "schedule":
      return last.ok ? "explain" : "repair";
    default:
      return last.stage;
  }
}

export function ProgressTrace({ steps, running, startedAt, result, error, onCancel }: ProgressTraceProps) {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [running]);

  const elapsed = running && startedAt !== null && now !== null ? Math.max(0, (now - startedAt) / 1000) : null;
  const next = expectedNextStage(steps);
  const repairs = steps.filter((s) => s.stage === "repair").length;
  const usage = totalUsage(steps);

  return (
    <Card
      title="Progress trace"
      aside={
        <span className="font-mono text-[11px] tabular-nums">
          {plural(steps.length, "step")}
          {repairs > 0 && ` · ${plural(repairs, "repair")}`}
          {usage.input + usage.output > 0 && ` · ${fmtCompact(usage.input + usage.output)} tok`}
        </span>
      }
      bodyClassName="p-0"
    >
      <ol aria-live="polite" className="divide-y divide-white/5">
        {steps.map((step, i) => (
          <StepRow key={i} step={step} />
        ))}
        {running && (
          <li className="flex items-center gap-3 px-4 py-2.5 text-[13px]">
            <Dot color="var(--accent)" pulse className="size-2.5" />
            <span className="text-accent">
              Working: {STAGE_LABELS[next] ?? next}
              {next === "repair" && repairs > 0 ? ` ${repairs + 1}` : ""}
            </span>
            <span className="ml-auto font-mono text-xs tabular-nums text-dim">
              {elapsed !== null ? fmtSeconds(elapsed) : ""}
            </span>
            {onCancel && (
              <Button size="sm" variant="ghost" onClick={onCancel}>
                Cancel
              </Button>
            )}
          </li>
        )}
        {!running && steps.length === 0 && !error && (
          <li className="px-4 py-3 text-[13px] text-dim">No steps yet.</li>
        )}
      </ol>
      {(result || error) && (
        <div className="border-t border-line p-3">
          <Outcome result={result} error={error} />
        </div>
      )}
    </Card>
  );
}

function StepRow({ step }: { step: LoopStep }) {
  const failed = !step.ok;
  const payload = step.payload
    ? Object.entries(step.payload).filter(
        ([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean",
      )
    : [];
  return (
    <li className="px-4 py-2.5">
      <div className="flex items-start gap-3">
        <span
          aria-label={failed ? "failed" : "ok"}
          className={cx(
            "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full",
            failed ? "bg-fail/15 text-fail" : "bg-ok/15 text-ok",
          )}
        >
          {failed ? <IconX className="size-2.5" /> : <IconCheck className="size-2.5" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
            <span className="font-medium">{STAGE_LABELS[step.stage] ?? step.stage}</span>
            {step.attempt > 1 && (
              <Badge tone="accent" mono>
                attempt {step.attempt}
              </Badge>
            )}
            {step.message && <span className="text-muted">{step.message}</span>}
          </div>
          {failed && step.failure && <FailureDetails failure={step.failure} />}
          {payload.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {payload.map(([k, v]) => (
                <Badge key={k} mono>
                  {k}: {String(v)}
                </Badge>
              ))}
            </div>
          )}
        </div>
        <div className="shrink-0 text-right font-mono text-[11px] tabular-nums text-dim">
          <div>{fmtSeconds(step.duration_s)}</div>
          {step.usage && step.usage.input_tokens + step.usage.output_tokens > 0 && (
            <div title={step.usage.model || "no model name recorded"}>
              {fmtCompact(step.usage.input_tokens)} in · {fmtCompact(step.usage.output_tokens)} out
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

/** Conflict chips (prefix meaning from CONFLICT_PREFIXES), the hint and the raw details. */
export function FailureDetails({ failure }: { failure: Failure }) {
  return (
    <div className="mt-1.5 space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="fail">{failure.kind}</Badge>
        {failure.conflicts.map((c) => {
          const meaning = conflictMeaning(c);
          return (
            <Badge key={c} tone="warn" title={meaning ?? c}>
              <span className="font-mono">{c}</span>
              {meaning && <span className="text-muted">{meaning}</span>}
            </Badge>
          );
        })}
      </div>
      {failure.hint && <p className="text-[13px] text-muted">{failure.hint}</p>}
      {failure.details.length > 0 && (
        <details className="text-xs text-dim">
          <summary className="cursor-pointer select-none hover:text-muted">
            {plural(failure.details.length, "detail")}
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 font-mono">
            {failure.details.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function Outcome({ result, error }: { result: PlanResult | null; error: string | null }) {
  if (error) {
    return (
      <Notice tone="fail" title="Planning failed">
        {error}
      </Notice>
    );
  }
  if (!result) return null;
  const usage = totalUsage(result.steps);
  // offline fixture answers carry no token counts: say "2 model calls · offline fixture"
  const tokens =
    usage.calls > 0
      ? `${usage.input + usage.output > 0 ? `${fmtCompact(usage.input)} in / ${fmtCompact(usage.output)} out tokens` : plural(usage.calls, "model call")}${usage.model ? ` · ${shortModel(usage.model)}` : ""}`
      : "no model calls";
  if (result.success && result.schedule) {
    const s = result.schedule;
    const bound =
      s.status !== "optimal" && s.objective_bound_h !== null ? ` (lower bound ${s.objective_bound_h} h)` : "";
    return (
      <Notice
        tone="ok"
        title={`Verified plan: makespan ${fmtHours(s.makespan_h, 0)} within the ${fmtHours(result.requirements.horizon_h, 0)} horizon`}
      >
        {plural(result.repairs, "repair round")} · {s.status}
        {bound} · solver {fmtSeconds(s.solve_time_s)} · {tokens}
      </Notice>
    );
  }
  return (
    <Notice tone="fail" title={`No verified plan after ${plural(result.repairs, "repair round")}`}>
      {result.failure ? <FailureDetails failure={result.failure} /> : "the planner stopped without a failure record"}
      <div className="mt-1.5 text-xs">{tokens}</div>
    </Notice>
  );
}
