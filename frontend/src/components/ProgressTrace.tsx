"use client";
/**
 * The planning trace: one row per LoopStep as they stream in. A failed validate / schedule
 * step renders as a full-width core block (red bar, conflict chips, the hint, and the numbers
 * behind it as a robot / budget / demanded table); a validated repair shows what the model
 * changed as diff chips with a scope-drift chip; the rate model's advisories render muted;
 * every model call carries its model, tokens and latency; the live row names the stage the
 * planner is expected to run next.
 */
import { useEffect, useState } from "react";
import type {
  Failure,
  LoopStep,
  PlanResult,
  ReplanOutcomePayload,
  Stage,
  StructurePayload,
  Task,
  TaskGraphPayload,
  Usage,
  VerificationReceipt,
} from "@/lib/types";
import {
  conflictPrefix,
  repairDiff,
  replanOutcome,
  scheduleReceipt,
  stepAdvisories,
  structurePayload,
  taskGraphPayload,
} from "@/lib/types";
import { STAGE_LABELS } from "@/lib/palette";
import {
  budgetRows,
  conflictMeaning,
  describeRepairDiff,
  fmtCompact,
  fmtHours,
  fmtNum,
  fmtPercent,
  fmtSeconds,
  fmtSignedPct,
  fmtUsage,
  isFixtureModel,
  modelDisplayName,
  modelTitle,
  parseAdvisory,
  plural,
  tidyHint,
  tidyUnits,
  totalUsage,
  type Advisory,
  type BudgetRow,
  type DiffChip,
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
  /** Horizon of the requirements being planned: completes the budget table of robot / zone
   *  conflicts whose hint quotes hours but not the deadline. */
  horizonH?: number | null;
  /** The final task graph (names, units and robot types for the diff chips); empty while the
   *  trace is still streaming. */
  tasks?: Task[];
}

/** The stage the planner is expected to run next, given the steps seen so far. A plan started
 *  from requirements (what the console does) opens with the task graph; pass `first` =
 *  "structure" for a plan started from a brief. */
export function expectedNextStage(steps: LoopStep[], first: Stage = "taskgraph"): Stage {
  const last = steps[steps.length - 1];
  if (!last) return first;
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

/** What is happening while a stage runs (the live row). */
const WORKING: Record<Stage, string> = {
  structure: "Nemotron is structuring the brief: site, structures, fleet, horizon, constraints",
  taskgraph: "Nemotron is proposing the task graph with quantities; the rate model prices each task",
  validate: "the validator checks references, cycles, capability, per-type energy and the horizon",
  schedule: "CP-SAT is proving precedence, one task per robot, one robot per zone, battery budgets and the deadline",
  repair: "Nemotron is repairing the graph from the infeasibility core and its numbers",
  explain: "Nemotron is writing the explanation from the verified schedule",
  simulate: "the simulator is replaying the schedule",
  replan: "CP-SAT is rescheduling the remaining work",
};

export function ProgressTrace({
  steps,
  running,
  startedAt,
  result,
  error,
  onCancel,
  horizonH = null,
  tasks = [],
}: ProgressTraceProps) {
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
          <StepRow
            key={i}
            step={step}
            prev={i > 0 ? steps[i - 1] : null}
            next={i + 1 < steps.length ? steps[i + 1] : null}
            pending={running && i === steps.length - 1}
            round={step.stage === "repair" ? steps.slice(0, i + 1).filter((s) => s.stage === "repair").length : null}
            tasks={tasks}
            horizonH={horizonH}
          />
        ))}
        {running && (
          <li className="px-4 py-2.5 text-[13px]">
            <div className="flex items-center gap-3">
              <Dot color="var(--accent)" pulse className="size-2.5" />
              <div className="min-w-0 flex-1">
                <span className="font-medium text-accent">
                  Working: {STAGE_LABELS[next] ?? next}
                  {next === "repair" && repairs > 0 ? ` ${repairs + 1}` : ""}
                </span>
                <span className="ml-2 text-xs text-muted">{WORKING[next]}…</span>
              </div>
              <span className="shrink-0 font-mono text-xs tabular-nums text-dim">
                {elapsed !== null ? fmtSeconds(elapsed) : ""}
              </span>
              {onCancel && (
                <Button size="sm" variant="ghost" onClick={onCancel}>
                  Cancel
                </Button>
              )}
            </div>
          </li>
        )}
        {!running && steps.length === 0 && !error && (
          <li className="px-4 py-3 text-[13px] text-dim">No steps yet.</li>
        )}
      </ol>
      {(result || error) && (
        <div className="border-t border-line p-3">
          <Outcome result={result} error={error} horizonH={horizonH} />
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ rows
function StepRow({
  step,
  prev,
  next,
  pending,
  round,
  tasks,
  horizonH,
}: {
  step: LoopStep;
  prev: LoopStep | null;
  /** the step that followed, when the trace has one */
  next: LoopStep | null;
  /** the last step of a run still streaming: what follows is not known yet */
  pending: boolean;
  /** 1-based repair round for a repair row (the backend's `attempt` counts task graphs) */
  round: number | null;
  tasks: Task[];
  horizonH: number | null;
}) {
  const failed = !step.ok;
  // a failed core went to Nemotron only when a repair step followed it
  const coreOutcome: CoreOutcome = next?.stage === "repair" ? "repaired" : pending ? "pending" : "stopped";
  // a model row carries its latency in the usage badge, an ok schedule row the solver time
  const showDuration = !step.usage && !(step.stage === "schedule" && step.ok);
  return (
    <li className={cx("px-4 py-2.5", failed && "bg-fail/[0.04]")}>
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
            {round !== null ? (
              <Badge mono title="repair rounds so far">
                round {round}
              </Badge>
            ) : step.attempt > 1 ? (
              <Badge mono title={`the task graph after ${plural(step.attempt - 1, "repair round")}`}>
                graph v{step.attempt}
              </Badge>
            ) : null}
            {step.message && <span className="text-muted">{tidyUnits(step.message)}</span>}
            <span className="ml-auto flex items-center gap-2">
              {step.usage && <UsageBadge usage={step.usage} />}
              {showDuration && (
                <span className="font-mono text-[11px] tabular-nums text-dim">{fmtSeconds(step.duration_s)}</span>
              )}
            </span>
          </div>
          {!failed && <StepBody step={step} prev={prev} tasks={tasks} />}
        </div>
      </div>
      {failed &&
        (step.failure ? (
          <CoreBlock failure={step.failure} horizonH={horizonH} outcome={coreOutcome} className="mt-2" />
        ) : (
          <p className="mt-1 pl-7 text-xs text-dim">The step failed without a failure record.</p>
        ))}
    </li>
  );
}

/** Model, tokens and latency of one LLM call. */
function UsageBadge({ usage }: { usage: Usage }) {
  return (
    <Badge mono title={modelTitle(usage.model)} className="text-dim">
      {fmtUsage(usage)}
    </Badge>
  );
}

const KNOWN_PAYLOAD_KEYS = new Set([
  "advisories",
  "rate_model",
  "grounded",
  "diff",
  "zones",
  "grid",
  "depot",
  "warnings",
  "makespan_h",
  "status",
  "verified",
  "verification",
  "escalated",
  "offset_h",
  "time_base",
  "remaining_tasks",
  "before_makespan_h",
  "after_makespan_h",
  "solve_time_s",
]);

function StepBody({ step, prev, tasks }: { step: LoopStep; prev: LoopStep | null; tasks: Task[] }) {
  switch (step.stage) {
    case "structure": {
      const p = structurePayload(step);
      return p ? <StructureLine payload={p} /> : <GenericPayload step={step} />;
    }
    case "taskgraph":
    case "repair": {
      const tg = taskGraphPayload(step);
      const diff = repairDiff(step);
      const advisories = stepAdvisories(step);
      return (
        <>
          {diff && <RepairDiffBlock diff={diff} tasks={tasks} />}
          {tg && <RateModelLine payload={tg} />}
          {advisories.length > 0 && <AdvisoryList advisories={advisories} />}
          {!tg && !diff && <GenericPayload step={step} />}
        </>
      );
    }
    case "validate": {
      // the validate step repeats the producing step's advisories: list them once
      const advisories = stepAdvisories(step);
      const before = prev && (prev.stage === "taskgraph" || prev.stage === "repair") ? stepAdvisories(prev) : [];
      const same = advisories.length === before.length && advisories.every((a, i) => a === before[i]);
      return advisories.length > 0 && !same ? <AdvisoryList advisories={advisories} /> : null;
    }
    case "schedule": {
      const receipt = scheduleReceipt(step);
      return receipt ? <ReceiptLine receipt={receipt} /> : <GenericPayload step={step} />;
    }
    case "replan": {
      const outcome = replanOutcome(step);
      return outcome ? <ReplanLine outcome={outcome} /> : <GenericPayload step={step} />;
    }
    default:
      return <GenericPayload step={step} />;
  }
}

/** Scalars of a payload this console does not know yet (forward compatibility). */
function GenericPayload({ step }: { step: LoopStep }) {
  const entries = step.payload
    ? Object.entries(step.payload).filter(
        ([k, v]) =>
          !KNOWN_PAYLOAD_KEYS.has(k) &&
          (typeof v === "string" || typeof v === "number" || typeof v === "boolean"),
      )
    : [];
  if (entries.length === 0) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {entries.map(([k, v]) => (
        <Badge key={k} mono>
          {k}: {String(v)}
        </Badge>
      ))}
    </div>
  );
}

function StructureLine({ payload }: { payload: StructurePayload }) {
  return (
    <div className="mt-1 text-xs text-muted">
      <span className="font-mono tabular-nums">
        {plural(payload.zones, "zone")} · grid {payload.grid[0]}×{payload.grid[1]} ·{" "}
        {payload.depot ? `depot (${payload.depot[0]}, ${payload.depot[1]})` : "no depot"}
      </span>
      {payload.warnings.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-dim">
          {payload.warnings.map((w) => (
            <li key={w} className="flex gap-1.5">
              <span className="shrink-0 rounded border border-white/10 px-1 font-mono text-[11px] uppercase tracking-wider">
                geometry
              </span>
              <span>{w}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Only when the rate table priced something; an ungrounded graph says so in its advisory. */
function RateModelLine({ payload }: { payload: TaskGraphPayload }) {
  if (!payload.rate_model || payload.grounded <= 0) return null;
  return (
    <div className="mt-1 text-xs text-muted">
      rate model priced {plural(payload.grounded, "task")} from their quantities
    </div>
  );
}

/** Chip label per advisory kind, in sentence case: these are notes, not error codes. */
const ADVISORY_LABEL: Record<Advisory["kind"], string> = {
  estimate: "estimate",
  ungrounded: "no quantities",
  other: "advisory",
};

/** Advisories are warnings (estimate gaps, an ungrounded graph), never conflicts: muted. */
function AdvisoryList({ advisories }: { advisories: string[] }) {
  return (
    <ul className="mt-1.5 space-y-0.5 text-xs text-dim">
      {advisories.map((raw) => {
        const a = parseAdvisory(raw);
        return (
          <li key={raw} className="flex items-start gap-1.5">
            <span className="mt-px shrink-0 rounded border border-white/10 px-1 font-mono text-[11px]">
              {ADVISORY_LABEL[a.kind]}
            </span>
            {a.task && <span className="shrink-0 font-mono text-muted">{a.task}</span>}
            <span>{a.text}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** The ok schedule row's one-line receipt; the full receipt sits above the Gantt. */
function ReceiptLine({ receipt }: { receipt: VerificationReceipt }) {
  const tight = receipt.battery.find((b) => b.robot === receipt.tightest_robot) ?? null;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted">
      <span className="font-medium text-ok">proved</span>
      <span>{plural(receipt.precedence_edges, "precedence edge")}</span>
      <span aria-hidden="true">·</span>
      <span>{plural(receipt.robot_rows, "robot row")}</span>
      <span aria-hidden="true">·</span>
      <span>{plural(receipt.zone_groups, "zone group")}</span>
      <span aria-hidden="true">·</span>
      <span>
        {plural(receipt.battery.length, "battery budget")}
        {tight ? `, tightest ${tight.robot} ${fmtNum(tight.pct, 0)}%` : ""}
      </span>
      <span aria-hidden="true">·</span>
      <span>
        {fmtHours(receipt.horizon_used_h, 0)} of {fmtHours(receipt.horizon_h, 0)}
      </span>
      <span aria-hidden="true">·</span>
      <span>
        {receipt.status} in {fmtSeconds(receipt.solve_time_s)}
      </span>
      {receipt.recheck && (
        <>
          <span aria-hidden="true">·</span>
          <span>re-checked independently</span>
        </>
      )}
    </div>
  );
}

function ReplanLine({ outcome }: { outcome: ReplanOutcomePayload }) {
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
      <Badge tone={outcome.escalated ? "warn" : "ok"}>
        {outcome.escalated ? "escalated to Nemotron" : "CP-SAT only"}
      </Badge>
      <span className="font-mono tabular-nums">
        {plural(outcome.remaining_tasks, "task")} · {fmtHours(outcome.before_makespan_h, 0)} →{" "}
        {outcome.after_makespan_h === null ? "—" : fmtHours(outcome.after_makespan_h, 0)}
        {outcome.solve_time_s !== null && ` · ${fmtSeconds(outcome.solve_time_s)}`}
      </span>
    </div>
  );
}

// ------------------------------------------------------------------ repair diff
const DIFF_CHIP_TONE: Record<DiffChip["kind"], string> = {
  added: "border-ok/30 bg-ok/10 text-ok",
  removed: "border-fail/30 bg-fail/10 text-fail",
  split: "border-info/30 bg-info/10 text-info",
  changed: "border-white/10 bg-white/5 text-muted",
};

const DIFF_CHIP_LIMIT = 10;

/** What the repair changed: one chip per task, the scope-drift chip and the totals. */
function RepairDiffBlock({ diff, tasks }: { diff: NonNullable<ReturnType<typeof repairDiff>>; tasks: Task[] }) {
  const [open, setOpen] = useState(false);
  const summary = describeRepairDiff(diff, tasks);
  const visible = open ? summary.chips : summary.chips.slice(0, DIFF_CHIP_LIMIT);
  const hidden = summary.chips.length - visible.length;
  return (
    <div className="mt-1.5 space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {summary.scopeReview ? (
          <Badge tone="warn" title={summary.scopeReason ?? undefined}>
            scope changed, review
          </Badge>
        ) : (
          <Badge title="signed change of total energy against the graph the repair answered">
            scope {fmtSignedPct(summary.drift)} energy
          </Badge>
        )}
        {visible.map((c) => (
          <span
            key={`${c.kind}-${c.task}`}
            title={c.title}
            className={cx(
              "inline-flex max-w-full items-center gap-1 rounded border px-1.5 py-px text-[11px] leading-[18px]",
              DIFF_CHIP_TONE[c.kind],
            )}
          >
            <span className="font-mono font-semibold text-fg">{c.task}</span>
            <span className="truncate">{c.text}</span>
          </span>
        ))}
        {(hidden > 0 || open) && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="rounded border border-white/10 px-1.5 text-[11px] leading-[18px] text-muted hover:text-fg"
          >
            {open ? "show fewer" : `+${hidden} more`}
          </button>
        )}
      </div>
      <div className="text-[11px] text-dim">
        {summary.scopeReview && summary.scopeReason ? `${summary.scopeReason} · ` : ""}
        {summary.totals}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ failures
/** Conflict chips: the id in mono, its meaning next to it, the prefix label as tooltip. */
export function ConflictChips({ conflicts, className }: { conflicts: string[]; className?: string }) {
  if (conflicts.length === 0) return null;
  return (
    <div className={cx("flex flex-wrap gap-1.5", className)}>
      {conflicts.map((c) => {
        const meaning = conflictMeaning(c);
        return (
          <span
            key={c}
            title={meaning ? `${conflictPrefix(c)}: ${meaning}` : c}
            className="inline-flex max-w-full items-center gap-1.5 rounded border border-fail/40 bg-fail/10 px-1.5 py-px text-[11px] leading-[18px] text-fail"
          >
            <span className="font-mono font-semibold">{c}</span>
            {meaning && <span className="text-fail/80">{meaning}</span>}
          </span>
        );
      })}
    </div>
  );
}

const SUBJECT_LABEL: Record<BudgetRow["kind"], string> = {
  robot: "Robot",
  type: "Robot type",
  task: "Task",
  chain: "Chain",
  zone: "Zone",
};

/** The numbers behind a failure: robot / budget / demanded. */
function BudgetTable({ rows }: { rows: BudgetRow[] }) {
  const kinds = new Set(rows.map((r) => r.kind));
  const subject = kinds.size === 1 ? SUBJECT_LABEL[rows[0].kind] : "Subject";
  return (
    <div className="overflow-x-auto rounded-md border border-fail/20">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-fail/[0.06] text-left text-[11px] uppercase tracking-wider text-dim">
            <th className="px-2.5 py-1.5 font-medium">{subject}</th>
            <th className="px-2.5 py-1.5 font-medium">Budget</th>
            <th className="px-2.5 py-1.5 font-medium">Demanded</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.kind}-${r.subject}-${r.unit}`} className="border-t border-white/5 align-top">
              <td className="px-2.5 py-1.5">
                <span className="font-mono font-medium text-fg">{r.subject}</span>
                {r.note && <span className="ml-1.5 text-dim">{r.note}</span>}
              </td>
              <td className="whitespace-nowrap px-2.5 py-1.5 font-mono tabular-nums text-fg">
                {fmtNum(r.budget, 1)} {r.unit}
              </td>
              <td className="whitespace-nowrap px-2.5 py-1.5 font-mono tabular-nums">
                <span className="font-medium text-fail">
                  {fmtNum(r.demanded, 1)} {r.unit}
                </span>
                {r.budget > 0 && <span className="ml-1.5 text-dim">{fmtPercent(r.demanded / r.budget)} of budget</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Kind badge, conflict chips (prefix meaning as tooltip), the hint, its numbers as a table,
 *  and the raw details behind a disclosure. Shared by the trace, the outcome notice and the
 *  red verification receipt. */
export function FailureDetails({
  failure,
  horizonH = null,
  className,
}: {
  failure: Failure;
  horizonH?: number | null;
  className?: string;
}) {
  const rows = budgetRows(failure, horizonH);
  return (
    <div className={cx("space-y-2", className)}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="fail" mono>
          {failure.kind}
        </Badge>
        <ConflictChips conflicts={failure.conflicts} />
      </div>
      {failure.hint && <p className="text-[13px] leading-relaxed text-fg/90">{tidyHint(failure.hint)}</p>}
      {rows.length > 0 && <BudgetTable rows={rows} />}
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

/** What became of a failed step's core: a repair followed, the run is still streaming, or the
 *  planner stopped there (no repair round left). */
export type CoreOutcome = "repaired" | "pending" | "stopped";

const CORE_OUTCOME_LABEL: Record<CoreOutcome, string> = {
  repaired: "sent to Nemotron for repair",
  pending: "",
  stopped: "no repair round left, planning stopped",
};

/** A failed step as a full-width block with a red bar: the infeasibility core the solver (or
 *  the validator) extracted, which is what goes back to Nemotron when a repair round is left. */
export function CoreBlock({
  failure,
  horizonH = null,
  outcome = "pending",
  className,
}: {
  failure: Failure;
  horizonH?: number | null;
  outcome?: CoreOutcome;
  className?: string;
}) {
  const isCore = failure.kind === "infeasible" || failure.kind === "validation";
  const suffix = CORE_OUTCOME_LABEL[outcome];
  return (
    <div className={cx("rounded-r-md border-l-[3px] border-fail bg-fail/[0.07] py-2.5 pl-3.5 pr-3", className)}>
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-fail">
        {isCore
          ? `Infeasibility core · ${plural(failure.conflicts.length, "conflict")}${suffix ? ` · ${suffix}` : ""}`
          : "Step failed"}
      </div>
      <FailureDetails failure={failure} horizonH={horizonH} />
    </div>
  );
}

// ------------------------------------------------------------------ outcome
function Outcome({
  result,
  error,
  horizonH,
}: {
  result: PlanResult | null;
  error: string | null;
  horizonH: number | null;
}) {
  if (error) {
    return (
      <Notice tone="fail" title="Planning failed">
        {error}
      </Notice>
    );
  }
  if (!result) return null;
  const usage = totalUsage(result.steps);
  // recorded answers carry no token counts: say "2 recorded model answers"
  const tokens =
    usage.calls > 0
      ? usage.input + usage.output > 0
        ? `${fmtCompact(usage.input)} in / ${fmtCompact(usage.output)} out tokens${usage.model ? ` · ${modelDisplayName(usage.model)}` : ""}`
        : isFixtureModel(usage.model)
          ? plural(usage.calls, "recorded model answer")
          : `${plural(usage.calls, "model call")}${usage.model ? ` · ${modelDisplayName(usage.model)}` : ""}`
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
      {result.failure ? (
        <FailureDetails failure={result.failure} horizonH={horizonH ?? result.requirements.horizon_h} />
      ) : (
        "the planner stopped without a failure record"
      )}
      <div className="mt-1.5 text-xs">{tokens}</div>
    </Notice>
  );
}
