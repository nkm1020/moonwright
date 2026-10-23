"use client";
/**
 * The planning trace: one row per LoopStep as they stream in. A failed validate / schedule
 * step renders as a full-width core block (red bar, conflict chips, the hint, and the numbers
 * behind it as a robot / budget / demanded table) whose title says what became of the core
 * (sent to Nemotron for repair, no repair round left, repairs off); the final failed step of a
 * failed run carries the solver-proven advice in its payload and shows it right under its core
 * block ("What would make it feasible"), before the result event arrives; a validated repair
 * shows what the model changed as diff chips with a scope-drift chip (read against the plan's
 * final graph only for the repair that produced it, from the diff's own fields for every
 * earlier round, see format.ts describeRepairDiff); the rate model's
 * advisories render muted, except a volume advisory (planned m³ against the dimensions), which
 * takes the amber of the scope-drift chip; every model call carries its model, tokens and
 * latency; the live row names the stage the planner is expected to run next.
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
  ADVISORY_KIND_LABELS,
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
  lastFailedStep,
  lastGraphStep,
  modelDisplayName,
  modelTitle,
  parseAdvisory,
  planSuggestions,
  repairRounds,
  plural,
  stepSuggestions,
  suggestionsTimeBase,
  tidyDetail,
  tidyHint,
  tidyUnits,
  totalUsage,
  type BudgetRow,
  type DiffChip,
} from "@/lib/format";
import { Badge, Button, Card, Dot, IconCheck, IconX, Notice, cx } from "./Section";
import { Suggestions } from "./Suggestions";
import { classifyWarning, type WarningShape } from "./RequirementsCard";

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
  /** Repair rounds the run was allowed (PlanRequest.max_repairs): 0 words the final failed
   *  core as "repairs off". Inferred from the trace and the result when not given. */
  maxRepairs?: number | null;
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

/** The live row once the final verdict has streamed in with its advice: no repair follows. */
const CLOSING = "no repair round left; the planner is returning the verdict with its solver-proven advice";

export function ProgressTrace({
  steps,
  running,
  startedAt,
  result,
  error,
  onCancel,
  horizonH = null,
  tasks = [],
  maxRepairs = null,
}: ProgressTraceProps) {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [running]);

  const elapsed = running && startedAt !== null && now !== null ? Math.max(0, (now - startedAt) / 1000) : null;
  const next = expectedNextStage(steps);
  // verdict rounds, as PlanResult.repairs counts them: a rejected repair answer that is asked
  // again stays in its round
  const repairs = repairRounds(steps);
  const usage = totalUsage(steps);
  const last = steps.length > 0 ? steps[steps.length - 1] : null;
  // the round the live row is in: a rejected repair answer is re-asked within its round
  const reasking = last !== null && last.stage === "repair" && !last.ok;
  // a failed step that already carries the advice is the run's final verdict: no repair follows
  const closing = running && last !== null && !last.ok && stepSuggestions(last).length > 0;
  const runEnd: RunEnd = running ? "running" : result ? "result" : "aborted";
  // a verdict with a round left always spends it, so a failed result with no repair step means
  // the loop was off; the caller's max_repairs settles it when given
  const repairsOff = maxRepairs !== null ? maxRepairs === 0 : repairs === 0 && result !== null && isVerdict(result.failure);
  // the plan's task graph is the answer of the last ok taskgraph / repair step: only that
  // step's diff may be read against it; an earlier round's diff is shown from its own fields
  // (and `tasks` belongs to a finished result, never to the previous plan of a run streaming)
  const graphStep = result ? lastGraphStep(steps) : -1;

  return (
    <Card
      title="Progress trace"
      aside={
        <span className="font-mono text-[11px] tabular-nums">
          {plural(steps.length, "step")}
          {repairs > 0 && ` · ${plural(repairs, "repair round")}`}
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
            runEnd={runEnd}
            repairsOff={repairsOff}
            round={step.stage === "repair" ? repairRounds(steps.slice(0, i + 1)) : null}
            tasks={i === graphStep ? tasks : []}
            horizonH={horizonH}
          />
        ))}
        {running && (
          <li className="px-4 py-2.5 text-[13px]">
            <div className="flex items-center gap-3">
              <Dot color="var(--accent)" pulse className="size-2.5" />
              <div className="min-w-0 flex-1">
                {closing ? (
                  <>
                    <span className="font-medium text-accent">Closing</span>
                    <span className="ml-2 text-xs text-muted">{CLOSING}…</span>
                  </>
                ) : (
                  <>
                    <span className="font-medium text-accent">
                      Working: {STAGE_LABELS[next] ?? next}
                      {next === "repair" && repairs > 0 ? ` ${reasking ? repairs : repairs + 1}` : ""}
                    </span>
                    <span className="ml-2 text-xs text-muted">{WORKING[next]}…</span>
                  </>
                )}
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
  runEnd,
  repairsOff,
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
  /** how the run ended, for the label of the step that ended it */
  runEnd: RunEnd;
  /** the run had no repair round to spend (max_repairs 0) */
  repairsOff: boolean;
  /** 1-based repair round for a repair row (the backend's `attempt` counts task graphs) */
  round: number | null;
  tasks: Task[];
  horizonH: number | null;
}) {
  const failed = !step.ok;
  const coreOutcome = failed ? coreOutcomeOf(step, next, pending, runEnd, repairsOff) : "pending";
  // the final failed step of a failed run carries the solver-proven advice in its payload
  const advice = failed ? stepSuggestions(step) : [];
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
      {advice.length > 0 && (
        <Suggestions
          variant="trace"
          suggestions={advice}
          timeBase={suggestionsTimeBase(step)}
          offsetH={offsetOf(step)}
          className="mt-1.5"
        />
      )}
    </li>
  );
}

/** Planner time 0 of a replan step in simulator hours, when its payload says (offset_h). */
function offsetOf(step: LoopStep): number | undefined {
  const v = step.payload?.offset_h;
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
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
  "suggestions",
  "suggestions_time_base",
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

/** Chip label per structuring-note shape (RequirementsCard classifyWarning): operations
 *  salvaged from a scrambled answer or missing dimensions (amber: act before planning), an
 *  operation the fleet cannot staff, one the brief line does not ask for, zone / depot
 *  geometry, anything else. */
const WARNING_CHIP: Record<WarningShape, string> = {
  recovered: "operations",
  no_dimensions: "dimensions",
  unstaffed: "fleet",
  unsupported: "operations",
  geometry: "geometry",
  other: "note",
};

function StructureLine({ payload }: { payload: StructurePayload }) {
  return (
    <div className="mt-1 text-xs text-muted">
      <span className="font-mono tabular-nums">
        {plural(payload.zones, "zone")} · grid {payload.grid[0]}×{payload.grid[1]} ·{" "}
        {payload.depot ? `depot (${payload.depot[0]}, ${payload.depot[1]})` : "no depot"}
      </span>
      {payload.warnings.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-dim">
          {payload.warnings.map((raw) => {
            const w = classifyWarning(raw);
            return (
              <li key={raw} className="flex items-start gap-1.5">
                {w.tone === "warn" ? (
                  <Badge tone="warn" className="shrink-0">
                    {WARNING_CHIP[w.shape]}
                  </Badge>
                ) : (
                  <span className="mt-px shrink-0 rounded border border-white/10 px-1 font-mono text-[11px] uppercase tracking-wider">
                    {WARNING_CHIP[w.shape]}
                  </span>
                )}
                <span className={w.tone === "warn" ? "text-muted" : undefined}>
                  {w.lead && <span className="font-medium text-warn">{w.lead} · </span>}
                  {w.text}
                </span>
              </li>
            );
          })}
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

/** Advisories are warnings, never conflicts: estimate gaps and an ungrounded graph render
 *  muted; a volume line (a structure's planned m³ differ from what its dimensions imply) takes
 *  the amber of the scope-drift chip, with the structure id, because it is a re-scope the user
 *  should look at. Labels come from format.ts ADVISORY_KIND_LABELS. */
function AdvisoryList({ advisories }: { advisories: string[] }) {
  return (
    <ul className="mt-1.5 space-y-0.5 text-xs text-dim">
      {advisories.map((raw) => {
        const a = parseAdvisory(raw);
        const volume = a.category === "volume";
        return (
          <li key={raw} className="flex items-start gap-1.5">
            {volume ? (
              <Badge
                tone="warn"
                className="shrink-0"
                title="The planned volume differs from what the structure's dimensions imply: a re-scope to check against the brief"
              >
                {ADVISORY_KIND_LABELS.volume}
              </Badge>
            ) : (
              <span className="mt-px shrink-0 rounded border border-white/10 px-1 font-mono text-[11px]">
                {ADVISORY_KIND_LABELS[a.category]}
              </span>
            )}
            {a.task && <span className={cx("shrink-0 font-mono", volume ? "text-warn" : "text-muted")}>{a.task}</span>}
            <span className={volume ? "text-muted" : undefined}>{a.text}</span>
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
        {visible.map((c, i) => (
          <span
            key={`${c.kind}-${c.task}-${i}`}
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
 *  red verification receipt; `collapsed` keeps the badge and the chips and folds the hint and
 *  the numbers behind "Show the core", for a second copy of a core the trace's final step
 *  already shows in full. */
export function FailureDetails({
  failure,
  horizonH = null,
  collapsed = false,
  className,
}: {
  failure: Failure;
  horizonH?: number | null;
  collapsed?: boolean;
  className?: string;
}) {
  const rows = budgetRows(failure, horizonH);
  const hasBody = Boolean(failure.hint) || rows.length > 0 || failure.details.length > 0;
  const body = (
    <>
      {failure.hint && <p className="text-[13px] leading-relaxed text-fg/90">{tidyHint(failure.hint)}</p>}
      {rows.length > 0 && <BudgetTable rows={rows} />}
      {failure.details.length > 0 && (
        <details className="text-xs text-dim">
          <summary className="cursor-pointer select-none hover:text-muted">
            {plural(failure.details.length, "detail")}
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 font-mono">
            {failure.details.map((d, i) => (
              <li key={i}>{tidyDetail(d)}</li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
  return (
    <div className={cx("space-y-2", className)}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="fail" mono>
          {failure.kind}
        </Badge>
        <ConflictChips conflicts={failure.conflicts} />
      </div>
      {collapsed && hasBody ? (
        <details className="group text-xs">
          <summary className="cursor-pointer select-none text-muted hover:text-fg">
            <span className="group-open:hidden">Show the core: the hint and the numbers behind it</span>
            <span className="hidden group-open:inline">Hide the core</span>
          </summary>
          <div className="mt-2 space-y-2">{body}</div>
        </details>
      ) : (
        body
      )}
    </div>
  );
}

/** How the run ended, as far as the trace knows: still streaming, with a PlanResult, or
 *  without one (cancelled, or the stream failed). */
export type RunEnd = "running" | "result" | "aborted";

/** What became of a failed step, read from the step that followed it: a verdict (validate /
 *  schedule) answered by a repair round; a rejected model answer (structure / taskgraph /
 *  repair) asked again within its round; the run still streaming; or the step that ended the
 *  run: no repair round left, repairs off (max_repairs 0), a rejected answer with no re-ask
 *  left, no recorded answer at all (an offline backend without a recording for the brief: its
 *  step message starts with "offline:"), or a run that ended without a result. "continued"
 *  makes no claim. */
export type CoreOutcome =
  | "repaired"
  | "reasked"
  | "pending"
  | "stopped"
  | "off"
  | "rejected"
  | "unavailable"
  | "aborted"
  | "continued";

const CORE_OUTCOME_LABEL: Record<CoreOutcome, string> = {
  repaired: "sent to Nemotron for repair",
  reasked: "answer rejected, Nemotron asked again",
  pending: "",
  stopped: "no repair round left, planning stopped",
  off: "repairs off, planning stopped",
  rejected: "answer rejected, planning stopped",
  unavailable: "no recorded answer, planning stopped",
  aborted: "planning stopped",
  continued: "",
};

/** The outcome of a failed step from its neighbours (see CoreOutcome). A verdict with a
 *  repair round left is always followed by a repair step, so a verdict that ends a finished run
 *  had none left, or the loop was off. */
export function coreOutcomeOf(
  step: LoopStep,
  next: LoopStep | null,
  pending: boolean,
  runEnd: RunEnd,
  repairsOff: boolean,
): CoreOutcome {
  const verdict = step.stage === "validate" || step.stage === "schedule";
  const answer = step.stage === "structure" || step.stage === "taskgraph" || step.stage === "repair";
  if (!verdict && !answer) return "continued";
  if (next) {
    if (answer && next.stage === step.stage) return "reasked";
    if (verdict && next.stage === "repair") return "repaired";
    return "continued";
  }
  if (pending || runEnd === "running") return "pending";
  if (runEnd === "aborted") return "aborted";
  if (answer) return /^offline:/i.test(step.message ?? "") ? "unavailable" : "rejected";
  return repairsOff ? "off" : "stopped";
}

/** The same verdict (kind, conflicts, hint): the final failed step's and the result's. */
function sameFailure(a: Failure, b: Failure): boolean {
  return (
    a.kind === b.kind &&
    a.hint === b.hint &&
    a.conflicts.length === b.conflicts.length &&
    a.conflicts.every((c, i) => c === b.conflicts[i])
  );
}

/** A verdict failure (the validator or CP-SAT said no), as opposed to a broken model answer. */
export function isVerdict(failure: Failure | null | undefined): boolean {
  return failure?.kind === "infeasible" || failure?.kind === "validation";
}

/** "after 3 repair rounds", or "with repairs off" when the first verdict was final
 *  (PlanResult.repairs is 0 with a verdict failure only then). */
export function roundsPhrase(plan: Pick<PlanResult, "repairs" | "failure">): string {
  if (plan.repairs === 0 && isVerdict(plan.failure)) return "with repairs off";
  return `after ${plural(plan.repairs, "repair round")}`;
}

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
  const head = isCore ? `Infeasibility core · ${plural(failure.conflicts.length, "conflict")}` : "Step failed";
  return (
    <div className={cx("rounded-r-md border-l-[3px] border-fail bg-fail/[0.07] py-2.5 pl-3.5 pr-3", className)}>
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-fail">
        {suffix ? `${head} · ${suffix}` : head}
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
  // every model the run called, so the token total is not credited to the first one alone
  const models = usage.models.length > 0 ? ` · ${usage.models.map(modelDisplayName).join(" + ")}` : "";
  // recorded answers carry no token counts: say "2 recorded model answers"
  const tokens =
    usage.calls > 0
      ? usage.input + usage.output > 0
        ? `${fmtCompact(usage.input)} in / ${fmtCompact(usage.output)} out tokens${models}`
        : isFixtureModel(usage.model)
          ? plural(usage.calls, "recorded model answer")
          : `${plural(usage.calls, "model call")}${models}`
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
  // the advice sits under the final failed step's core block just above; a recording whose
  // steps do not carry it (PlanResult.suggestions only) shows it here instead
  const failedStep = lastFailedStep(result.steps);
  const advice = failedStep && stepSuggestions(failedStep).length > 0 ? [] : planSuggestions(result);
  // the final failed step just above shows this core in full (chips, hint, numbers): the
  // notice repeats only the conflict ids, so one screen holds one full core
  const coreAbove =
    result.failure !== null && failedStep?.failure !== null && failedStep?.failure !== undefined && sameFailure(failedStep.failure, result.failure);
  return (
    <Notice tone="fail" title={`No verified plan ${roundsPhrase(result)}`}>
      {result.failure ? (
        coreAbove ? (
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
            <span>{result.failure.conflicts.length > 0 ? "the core, shown in full in the final step above:" : "the verdict is in the final step above"}</span>
            <ConflictChips conflicts={result.failure.conflicts} />
          </div>
        ) : (
          <FailureDetails failure={result.failure} horizonH={horizonH ?? result.requirements.horizon_h} />
        )
      ) : (
        "the planner stopped without a failure record"
      )}
      {advice.length > 0 && <Suggestions variant="outcome" suggestions={advice} className="mt-2" />}
      <div className="mt-1.5 text-xs">{tokens}</div>
    </Notice>
  );
}
