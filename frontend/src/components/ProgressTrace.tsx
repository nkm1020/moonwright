"use client";
/**
 * The planning trace: one row per LoopStep as they stream in. A failed validate / schedule
 * step renders as a full-width core block (red bar, conflict chips, the hint, and the numbers
 * behind it as a subject / budget / demanded table, the failed schedule step's own budget rows
 * first) whose title says what became of the core (sent to Nemotron for repair, no repair
 * round left, repairs off); a rejected model answer shows its hint and one line per ask. The
 * final failed step of a failed run carries the solver-proven advice in its payload and shows
 * it right under its core block ("What would make it feasible"), before the result event
 * arrives; once the console shows that advice in its own card beside the trace (`adviceShown`)
 * the step keeps one line that points there, so the advice is printed in full once. The bottom
 * notice keeps only its verdict line and the token count, so the core is printed in full once. A validated repair shows what that round changed as diff chips with
 * a scope-drift chip: from its own diff only (format.ts describeRepairDiff), named and given
 * units by the graph that round produced (format.ts repairGraphs: the plan's final graph for
 * the repair that produced it, a graph rebuilt from the later diffs for an earlier round of a
 * recording that can rebuild it, nothing otherwise). The rate model's advisories render muted,
 * except a volume advisory (planned m³ against the dimensions), which takes the amber of the
 * scope-drift chip; every model call carries its model, tokens and latency; the live row names
 * the stage the planner is expected to run next.
 */
import { useEffect, useState } from "react";
import type {
  Failure,
  LoopStep,
  PlanResult,
  ReplanOutcomePayload,
  Stage,
  StructurePayload,
  Suggestion,
  Task,
  TaskGraphPayload,
  Usage,
  VerificationReceipt,
} from "@/lib/types";
import {
  SUGGESTION_COPY,
  conflictPrefix,
  repairDiff,
  replanOutcome,
  scheduleReceipt,
  stepAdvisories,
  stepBudget,
  structurePayload,
  taskGraphPayload,
  type BudgetRow,
} from "@/lib/types";
import { STAGE_LABELS } from "@/lib/palette";
import {
  ADVISORY_KIND_LABELS,
  answerFailure,
  budgetRows,
  conflictMeaning,
  describeRepairDiff,
  failureStep,
  fmtCompact,
  fmtHours,
  fmtNum,
  fmtPercent,
  fmtSeconds,
  fmtSignedPct,
  fmtUsage,
  isFixtureModel,
  lastFailedStep,
  modelDisplayName,
  modelTitle,
  parseAdvisory,
  parseAskLine,
  planSuggestions,
  repairGraphs,
  repairRounds,
  plural,
  stepSuggestions,
  suggestionsTimeBase,
  tidyDetail,
  tidyHint,
  tidyMessage,
  totalUsage,
  type BudgetTableRow,
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
  /** The plan's final task graph (PlanResult.task_graph.tasks: names, units and robot types
   *  for the diff chips); pass it once the run is done and [] while the trace still streams.
   *  Only the repair that produced it reads it directly; earlier rounds get the graph they
   *  produced, rebuilt from the later diffs where that is exact (format.ts repairGraphs). */
  tasks?: Task[];
  /** Repair rounds the run was allowed (PlanRequest.max_repairs): 0 words the final failed
   *  core as "repairs off". Inferred from the trace and the result when not given. */
  maxRepairs?: number | null;
  /** The console shows this run's solver-proven advice in its own card next to the trace: the
   *  final failed step then points to it in one line and the bottom notice leaves it out. */
  adviceShown?: boolean;
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

const NO_TASKS: Task[] = [];

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
  adviceShown = false,
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
  const repairsOff = maxRepairs !== null ? maxRepairs === 0 : repairs === 0 && result !== null && answeredByRepair(result.failure);
  // the graph each repair produced: the plan's final graph for the last one, a graph rebuilt
  // from the later diffs for an earlier round where that is exact, none otherwise (and `tasks`
  // belongs to a finished result, never to the previous plan of a run still streaming)
  const graphs = result ? repairGraphs(steps, tasks) : new Map<number, Task[]>();

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
            rounds={repairRounds(steps.slice(0, i + 1))}
            tasks={graphs.get(i) ?? NO_TASKS}
            horizonH={horizonH}
            adviceShown={adviceShown}
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
                      {next === "repair" ? ` · round ${reasking ? repairs : repairs + 1}` : ""}
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
          <Outcome result={result} error={error} horizonH={horizonH} adviceShown={adviceShown} />
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
  rounds,
  tasks,
  horizonH,
  adviceShown,
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
  /** repair rounds up to and including this step (repairRounds of the trace so far): a repair
   *  row's round, and how a later answer came to be ("graph v3") for any other row; the
   *  backend's `attempt` counts every answer, re-asks included */
  rounds: number;
  tasks: Task[];
  horizonH: number | null;
  /** the console shows the advice in its own card: point there instead of repeating it */
  adviceShown: boolean;
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
            {step.stage === "repair" ? (
              <Badge mono title="repair rounds so far">
                round {rounds}
              </Badge>
            ) : step.attempt > 1 ? (
              <Badge mono title={answerTitle(step.attempt, rounds)}>
                graph v{step.attempt}
              </Badge>
            ) : null}
            {step.message && <span className="text-muted">{tidyMessage(step.message)}</span>}
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
          <CoreBlock
            failure={step.failure}
            horizonH={horizonH}
            budget={stepBudget(step)}
            outcome={coreOutcome}
            className="mt-2"
          />
        ) : (
          <p className="mt-1 pl-7 text-xs text-dim">The step failed without a failure record.</p>
        ))}
      {advice.length > 0 &&
        (adviceShown ? (
          <AdvicePointer suggestions={advice} className="mt-1.5" />
        ) : (
          <Suggestions
            variant="trace"
            suggestions={advice}
            timeBase={suggestionsTimeBase(step)}
            offsetH={offsetOf(step)}
            className="mt-1.5"
          />
        ))}
    </li>
  );
}

/** The "graph vN" badge's tooltip: which answer of the run this graph is and what came before
 *  it, "answer 3 from Nemotron, after 1 repair round and 1 re-ask" (`attempt` counts every
 *  answer; a rejected answer asked again is a re-ask, not a repair round). */
function answerTitle(attempt: number, rounds: number): string {
  const reasks = Math.max(0, attempt - 1 - rounds);
  const before = [
    rounds > 0 ? plural(rounds, "repair round") : null,
    reasks > 0 ? plural(reasks, "re-ask") : null,
  ].filter((s): s is string => s !== null);
  return `answer ${attempt} from Nemotron${before.length > 0 ? `, after ${before.join(" and ")}` : ""}`;
}

/** The final failed step's advice as one line, when the console shows it in full in its own
 *  card: how many changes CP-SAT proved and where they are. */
function AdvicePointer({ suggestions, className }: { suggestions: Suggestion[]; className?: string }) {
  const proven = suggestions.filter((s) => s.verified).length;
  const rest = suggestions.length - proven;
  const what =
    proven > 0
      ? `${plural(proven, "proven change")}${rest > 0 ? ` and ${plural(rest, "scope cut")}` : ""}`
      : plural(rest, "scope cut");
  return (
    <div
      className={cx(
        "flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-r-md border-l-[3px] py-1.5 pl-3.5 pr-3 text-xs",
        proven > 0 ? "border-ok bg-ok/[0.06]" : "border-white/25 bg-white/[0.03]",
        className,
      )}
    >
      <span className={cx("text-[11px] font-medium uppercase tracking-wider", proven > 0 ? "text-ok" : "text-muted")}>
        {SUGGESTION_COPY.heading}
      </span>
      <span className="text-muted">
        {what}
        {suggestions.length > 1 ? ", best first" : ""} · in the advice card
      </span>
    </div>
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
  "budget",
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

/** Chip label per structuring-note class (RequirementsCard classifyWarning): operations
 *  salvaged from a scrambled answer, a structure without dimensions, a lift count, a
 *  footprint, a smallest size and a distance read as a size corrected to the brief, an
 *  operation the brief's entry states that the structuring omitted (restored since phase 3l;
 *  amber: check before planning), an operation the fleet cannot staff (dropped since phase
 *  3j), one the brief line does not ask for, zone / depot geometry, anything else. */
const WARNING_CHIP: Record<WarningShape, string> = {
  recovered: "operations recovered",
  no_dimensions: "no dimensions",
  unstaffed: "unstaffed operation",
  dropped: "operation dropped",
  restored: "operation restored",
  unsupported: "operation not in brief",
  lifts: "lift count corrected",
  footprint: "footprint corrected",
  minimum: "smallest size read",
  distance: "distance dropped",
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
                  <span className="mt-px shrink-0 rounded border border-white/10 px-1 font-mono text-[11px]">
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
  replaced: "border-white/10 bg-white/5 text-muted",
  split: "border-info/30 bg-info/10 text-info",
  changed: "border-white/10 bg-white/5 text-muted",
  renumbered: "border-dashed border-white/15 text-dim",
};

/** Chips shown before "+N more": the changed tasks give way, never the added / removed ones. */
const DIFF_CHIP_LIMIT = 10;

/** The diff's chips as the block shows them: the added ids as one chip ("T7a, T7b, T9 added")
 *  and the removed ids as one, so a long diff never hides what came and went; the per-task
 *  chips otherwise as describeRepairDiff gives them. */
function groupedChips(chips: DiffChip[]): DiffChip[] {
  const out: DiffChip[] = [];
  const grouped = new Set<DiffChip["kind"]>();
  for (const c of chips) {
    if (c.kind !== "added" && c.kind !== "removed") {
      out.push(c);
      continue;
    }
    // each group stands where its first chip stood
    if (grouped.has(c.kind)) continue;
    grouped.add(c.kind);
    const group = chips.filter((x) => x.kind === c.kind);
    out.push(
      group.length === 1
        ? c
        : { task: group.map((x) => x.task).join(", "), kind: c.kind, text: c.kind, title: group.map((x) => x.title).join("; ") },
    );
  }
  return out;
}

/** What the repair changed: one chip per task (the added and the removed ids grouped), the
 *  scope-drift chip and the totals. */
function RepairDiffBlock({ diff, tasks }: { diff: NonNullable<ReturnType<typeof repairDiff>>; tasks: Task[] }) {
  const [open, setOpen] = useState(false);
  const summary = describeRepairDiff(diff, tasks);
  const chips = groupedChips(summary.chips);
  // the changed tasks give way when the row is long: everything else stays in view
  const others = chips.filter((c) => c.kind !== "changed").length;
  let room = Math.max(4, DIFF_CHIP_LIMIT - others);
  const visible = open
    ? chips
    : chips.filter((c) => {
        if (c.kind !== "changed") return true;
        room -= 1;
        return room >= 0;
      });
  const hidden = chips.length - visible.length;
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
              "inline-flex max-w-full items-baseline gap-1 rounded border px-1.5 py-px text-[11px] leading-[18px]",
              DIFF_CHIP_TONE[c.kind],
            )}
          >
            {/* a grouped chip's id list may wrap; a single id never breaks */}
            <span className={cx("font-mono font-semibold text-fg", c.task.includes(", ") ? "min-w-0 break-words" : "shrink-0")}>
              {c.task}
            </span>
            {c.was && <span className="shrink-0 font-mono text-dim">(was {c.was})</span>}
            <span className="min-w-0 break-words">{c.text}</span>
          </span>
        ))}
        {(hidden > 0 || open) && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="rounded border border-white/10 px-1.5 text-[11px] leading-[18px] text-muted hover:text-fg"
          >
            {open ? "show fewer" : `+${plural(hidden, "more change", "more changes")}`}
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
/** Conflict chips: the id in mono (a chain drawn with the console's arrow, "horizon_path:
 *  T1→T2→T7"), its meaning next to it, the raw id and the prefix's meaning as tooltip. */
export function ConflictChips({ conflicts, className }: { conflicts: string[]; className?: string }) {
  if (conflicts.length === 0) return null;
  return (
    <div className={cx("flex flex-wrap gap-1.5", className)}>
      {conflicts.map((c) => {
        const meaning = conflictMeaning(c);
        return (
          <span
            key={c}
            title={meaning ? `${c} · ${conflictPrefix(c)}: ${meaning}` : c}
            className="inline-flex max-w-full items-center gap-1.5 rounded border border-fail/40 bg-fail/10 px-1.5 py-px text-[11px] leading-[18px] text-fail"
          >
            <span className="break-all font-mono font-semibold">{c.replace(/->/g, "→")}</span>
            {meaning && <span className="text-fail/80">{meaning}</span>}
          </span>
        );
      })}
    </div>
  );
}

const SUBJECT_LABEL: Record<BudgetTableRow["kind"], string> = {
  robot: "Robot",
  type: "Robot type",
  task: "Task",
  chain: "Chain",
  zone: "Zone",
};

/** Decimals for one row's two figures: Wh whole, as the hints print them ("9,930 Wh"), more
 *  only where two figures that differ would otherwise print equal ("1,784.39 Wh against
 *  1,784.38 Wh"); hours with one decimal at most. */
function rowDigits(r: BudgetTableRow): number {
  if (r.unit !== "Wh") return 1;
  for (let d = 0; d < 2; d += 1) if (r.budget === r.demanded || fmtNum(r.budget, d) !== fmtNum(r.demanded, d)) return d;
  return 2;
}

/** The numbers behind a failure: subject / budget / demanded. */
function BudgetTable({ rows }: { rows: BudgetTableRow[] }) {
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
          {rows.map((r) => {
            // red marks what breaks the plan; a figure within its budget is context, unless the
            // row is the conflict itself, which then says why next to the figure
            const binding = r.binding ?? r.demanded > r.budget;
            const digits = rowDigits(r);
            return (
              <tr key={`${r.kind}-${r.subject}-${r.unit}`} className="border-t border-white/5 align-top">
                <td className="px-2.5 py-1.5">
                  <span className="font-mono font-medium text-fg">{r.subject}</span>
                  {r.note && <span className="ml-1.5 text-dim">{r.note}</span>}
                </td>
                <td className="whitespace-nowrap px-2.5 py-1.5 font-mono tabular-nums text-fg">
                  {fmtNum(r.budget, digits)} {r.unit}
                </td>
                <td className="px-2.5 py-1.5 font-mono tabular-nums">
                  <span className="whitespace-nowrap">
                    <span className={cx("font-medium", binding ? "text-fail" : "text-fg")}>
                      {fmtNum(r.demanded, digits)} {r.unit}
                    </span>
                    {r.budget > 0 && <span className="ml-1.5 text-dim">{fmtPercent(r.demanded / r.budget)} of budget</span>}
                  </span>
                  {binding && r.why && <div className="font-sans text-[11px] text-fail/85">{r.why}</div>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** A rejected model answer (kind schema / llm_error): the backend's one line per ask ("ask 1:
 *  cut off at 32,768 tokens", "ask 3 (reasoning off): task T3: 'after' must be a list") or the
 *  request's own account, model ids read as names, as a short list under the hint; a line the
 *  hint already says (an offline miss: "no recording for brief 'x'") is not repeated. */
function AskList({ details, hint }: { details: string[]; hint: string }) {
  const said = hint.toLowerCase();
  const lines = details.map(parseAskLine).filter((l) => l.text && (l.ask !== null || !said.includes(l.text.toLowerCase())));
  if (lines.length === 0) return null;
  const shown = lines.slice(0, ASK_LINES);
  return (
    <ul className="space-y-0.5 text-xs text-muted">
      {shown.map((l, i) => (
        <li key={i} className="flex items-baseline gap-2">
          {l.ask && <span className="shrink-0 font-mono text-[11px] text-dim">{l.ask}</span>}
          <span className="min-w-0 break-words">{l.text}</span>
        </li>
      ))}
      {lines.length > shown.length && (
        <li className="text-dim">and {plural(lines.length - shown.length, "more line")}</li>
      )}
    </ul>
  );
}

const ASK_LINES = 6;

/** What each failure kind means (the kind badge's tooltip). */
const FAILURE_KIND_TITLES: Record<string, string> = {
  validation: "The validator rejected the task graph before the solver ran",
  infeasible: "CP-SAT proved the task graph infeasible; the conflicts are the minimal core",
  solver_timeout: "CP-SAT ran out of time before proving the schedule",
  schema: "The model's answer did not validate as a task graph",
  llm_error: "The model call failed or returned nothing usable",
};

/** The kind badge's words: a verdict's kind as it is ("validation", "infeasible"), the others
 *  in words ("solver timeout"); an llm_error as format.ts answerFailure reads it: "model
 *  request" (the request failed), "empty answer" (the model answered with nothing), "offline"
 *  (an offline backend without a recording of the brief: no model was asked). */
const FAILURE_KIND_LABELS: Record<string, string> = {
  solver_timeout: "solver timeout",
  llm_error: "model request",
};

function failureKindLabel(failure: Failure): string {
  const a = answerFailure(failure);
  if (a === "offline") return "offline";
  if (a === "empty") return "empty answer";
  return FAILURE_KIND_LABELS[failure.kind] ?? failure.kind.replace(/_/g, " ");
}

function failureKindTitle(failure: Failure): string | undefined {
  switch (answerFailure(failure)) {
    case "offline":
      return "This backend replays recorded Nemotron answers and has none for this brief";
    case "empty":
      return "The model answered with nothing on every ask";
    case "request":
      return "The request to the model failed before an answer came back";
    default:
      return FAILURE_KIND_TITLES[failure.kind];
  }
}

/** Kind badge, conflict chips (meaning as tooltip), the hint, its numbers as a table (the
 *  failed schedule step's own budget rows first, `budget`), and the details: a rejected
 *  answer's per-ask lines as a short list, a verdict's raw lines behind a disclosure. Shared by
 *  the trace, the outcome notice and the red verification receipt; `collapsed` keeps the badge
 *  and the chips and folds the rest behind "Show the core", for a second copy of a core the
 *  trace shows in full. */
export function FailureDetails({
  failure,
  horizonH = null,
  budget = null,
  collapsed = false,
  className,
}: {
  failure: Failure;
  horizonH?: number | null;
  /** the failed schedule step's payload.budget (types.ts stepBudget), when it has one */
  budget?: readonly BudgetRow[] | null;
  collapsed?: boolean;
  className?: string;
}) {
  const rows = budgetRows(failure, horizonH, budget);
  const answer = failure.kind === "schema" || failure.kind === "llm_error";
  // per-ask lines (a rejected answer) rather than a failed request's own account
  const asks = answer && failure.details.some((d) => parseAskLine(d).ask !== null);
  const hasBody = Boolean(failure.hint) || rows.length > 0 || failure.details.length > 0;
  const body = (
    <>
      {failure.hint && <p className="text-[13px] leading-relaxed text-fg/90">{tidyHint(failure.hint)}</p>}
      {rows.length > 0 && <BudgetTable rows={rows} />}
      {answer ? (
        <AskList details={failure.details} hint={tidyHint(failure.hint)} />
      ) : (
        failure.details.length > 0 && (
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
        )
      )}
    </>
  );
  return (
    <div className={cx("space-y-2", className)}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="fail" mono title={failureKindTitle(failure)}>
          {failureKindLabel(failure)}
        </Badge>
        <ConflictChips conflicts={failure.conflicts} />
      </div>
      {collapsed && hasBody ? (
        <details className="group text-xs">
          <summary className="cursor-pointer select-none text-muted hover:text-fg">
            <span className="group-open:hidden">
              {answer
                ? asks
                  ? "Show what went wrong: the hint and each ask"
                  : "Show what went wrong"
                : !isVerdict(failure)
                  ? "Show what CP-SAT reported"
                  : rows.length > 0
                    ? "Show the core: the hint and the numbers behind it"
                    : "Show the core: the hint"}
            </span>
            <span className="hidden group-open:inline">{isVerdict(failure) ? "Hide the core" : "Hide the details"}</span>
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
 *  repair) asked again within its round, or a request to the model that failed and was sent
 *  again (an empty answer asked again, a failed request sent again); the run still streaming;
 *  or the step that ended the run: no repair round left, repairs off (max_repairs 0), a
 *  rejected answer, an empty answer or a failed request with no re-ask left, no recorded
 *  answer at all (an offline backend without a recording for the brief: its step message
 *  starts with "offline:"), or a run that ended without a result. "continued" makes no claim.
 *  Answers are read with format.ts answerFailure, as the receipt and the console read them. */
export type CoreOutcome =
  | "repaired"
  | "reasked"
  | "retried"
  | "emptyAgain"
  | "pending"
  | "stopped"
  | "off"
  | "rejected"
  | "requestFailed"
  | "empty"
  | "unavailable"
  | "aborted"
  | "continued";

const CORE_OUTCOME_LABEL: Record<CoreOutcome, string> = {
  repaired: "sent to Nemotron for repair",
  reasked: "answer rejected, Nemotron asked again",
  retried: "request failed, Nemotron asked again",
  emptyAgain: "empty answer, Nemotron asked again",
  pending: "",
  stopped: "no repair round left, planning stopped",
  off: "repairs off, planning stopped",
  rejected: "answer rejected, planning stopped",
  requestFailed: "request failed, planning stopped",
  empty: "empty answer, planning stopped",
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
  const how = answerFailure(step.failure);
  if (next) {
    if (answer && next.stage === step.stage) return how === "request" ? "retried" : how === "empty" ? "emptyAgain" : "reasked";
    if (verdict && next.stage === "repair") return "repaired";
    return "continued";
  }
  if (pending || runEnd === "running") return "pending";
  if (runEnd === "aborted") return "aborted";
  if (answer) {
    if (/^offline:/i.test(step.message ?? "") || how === "offline") return "unavailable";
    return how === "request" ? "requestFailed" : how === "empty" ? "empty" : "rejected";
  }
  return repairsOff ? "off" : "stopped";
}

/** A verdict failure (the validator or CP-SAT said no), as opposed to a broken model answer. */
export function isVerdict(failure: Failure | null | undefined): boolean {
  return failure?.kind === "infeasible" || failure?.kind === "validation";
}

/** A failure the planner answers with a repair round while one is left (backend loop.py): a
 *  verdict, or CP-SAT running out of time, whose note goes back to Nemotron like a core. */
export function answeredByRepair(failure: Failure | null | undefined): boolean {
  return isVerdict(failure) || failure?.kind === "solver_timeout";
}

/** "after 3 repair rounds", or "with repairs off" when the first verdict (or solver time-out)
 *  was final (PlanResult.repairs is 0 with such a failure only then); a run that never got a
 *  task graph to judge says why instead ("because Nemotron's answer was rejected", "because
 *  Nemotron returned an empty answer", "because the model request failed", "because no answer
 *  is recorded for this brief": format.ts answerFailure), so no title counts zero rounds. Read
 *  after "No verified plan" / "Not proved". */
export function roundsPhrase(plan: Pick<PlanResult, "repairs" | "failure">): string {
  const f = plan.failure;
  if (plan.repairs === 0 && answeredByRepair(f)) return "with repairs off";
  if (plan.repairs === 0 && f && !answeredByRepair(f)) {
    switch (answerFailure(f)) {
      case "offline":
        return "because no answer is recorded for this brief";
      case "request":
        return "because the model request failed";
      case "empty":
        return "because Nemotron returned an empty answer";
      default:
        return "because Nemotron's answer was rejected";
    }
  }
  return `after ${plural(plan.repairs, "repair round")}`;
}

/** A failed step as a full-width block with a red bar: the infeasibility core the solver (or
 *  the validator) extracted, which is what goes back to Nemotron when a repair round is left. */
export function CoreBlock({
  failure,
  horizonH = null,
  budget = null,
  outcome = "pending",
  className,
}: {
  failure: Failure;
  horizonH?: number | null;
  /** the step's own budget rows (types.ts stepBudget) */
  budget?: readonly BudgetRow[] | null;
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
      <FailureDetails failure={failure} horizonH={horizonH} budget={budget} />
    </div>
  );
}

// ------------------------------------------------------------------ outcome
function Outcome({
  result,
  error,
  horizonH,
  adviceShown,
}: {
  result: PlanResult | null;
  error: string | null;
  horizonH: number | null;
  /** the console's own card shows the advice */
  adviceShown: boolean;
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
      : answerFailure(result.failure) === "request" || answerFailure(result.failure) === "empty"
        ? "no tokens recorded"
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
  // the advice sits under the final failed step's core block just above, or in the console's
  // own card; a recording whose steps do not carry it (PlanResult.suggestions only) shows it
  // here instead
  const failedStep = lastFailedStep(result.steps);
  const advice = adviceShown || (failedStep && stepSuggestions(failedStep).length > 0) ? [] : planSuggestions(result);
  // the step that produced the verdict shows this core in full just above (chips, hint,
  // numbers): the notice then keeps only its verdict line and the token count, so one screen
  // holds one full core. A recording whose steps lack it gets the core here.
  const verdictStep = failureStep(result.steps, result.failure);
  return (
    <Notice tone="fail" title={`No verified plan ${roundsPhrase(result)}`}>
      {result.failure ? (
        verdictStep ? null : (
          <FailureDetails
            failure={result.failure}
            horizonH={horizonH ?? result.requirements.horizon_h}
            className="mt-1"
          />
        )
      ) : (
        "the planner stopped without a failure record"
      )}
      {advice.length > 0 && <Suggestions variant="outcome" suggestions={advice} className="mt-2" />}
      <div className={cx("text-xs", verdictStep && advice.length === 0 ? "mt-0.5" : "mt-1.5")}>{tokens}</div>
    </Notice>
  );
}
