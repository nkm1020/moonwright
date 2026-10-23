"use client";
/**
 * One replan: the schedule in force before the event next to the replanned schedule, both in
 * absolute simulator hours on the same axis, plus the outcome (CP-SAT alone, or escalated to
 * Nemotron), the makespan delta, the re-verification summary, the replanning trace and the
 * context the planner was given. A failed replan shows its verdict in place of the "After"
 * chart, with the solver-proven advice the replanner attached (a later deadline, one more
 * robot; absolute hours against the brief's deadline) under the failure line.
 */
import { useMemo } from "react";
import type {
  Failure,
  FailureSpec,
  LoopStep,
  PlanResult,
  ReplanOutcomePayload,
  ReplanRecord,
  Requirements,
  Schedule,
  Task,
  VerificationReceipt,
} from "@/lib/types";
import { conflictPrefix } from "@/lib/types";
import { EVENT_COLORS, STAGE_LABELS, robotTypeColor, zoneColor } from "@/lib/palette";
import {
  conflictMeaning,
  fmtHours,
  fmtSeconds,
  isFixtureModel,
  lastFailedStep,
  modelDisplayName,
  planSuggestions,
  repairRounds,
  plural,
  suggestionsTimeBase,
  tidyDetail,
  tidyHint,
  tidyMessage,
} from "@/lib/format";
import { Gantt, defaultZoneIds } from "./Gantt";
import { ESCALATION_MODEL, KindPill, escalationText, eventKindLabel, formatHours, replanOutcome as outcomeOf } from "./EventList";
import { Badge } from "./Section";
import { Suggestions } from "./Suggestions";

export interface BeforeAfterProps {
  record: ReplanRecord;
  requirements: Requirements;
  /** All tasks ever planned (see lib/tasks.ts allTasks). */
  tasks: Task[];
  /** 1-based replan number for the heading. */
  index: number;
  /** Additive: forwarded to both Gantts. */
  onSelectTask?: (taskId: string | null) => void;
  /** Additive: open the context panel initially. */
  defaultOpen?: boolean;
}

interface Change {
  task: string;
  robotBefore: string | null;
  robotAfter: string;
  /** start_h after - before (end_h after - before for a task that continues); null for a task
   *  the old schedule did not have. */
  shiftH: number | null;
  /** The task was underway at the event and carries on with its robot: the replanned entry
   *  is its remaining portion, so only a moved end counts as a change. */
  continues?: boolean;
}

/** Tasks whose robot or timing changed between the two schedules (after's task order). A task
 *  that had started before `atH` and keeps its robot is re-expressed by the replan as its
 *  remaining portion from the replan time; comparing starts would report it as shifted, so it
 *  is compared on its end and listed as `continues` only when that end moved. */
export function diffSchedules(before: Schedule, after: Schedule, atH?: number): Change[] {
  const prev = new Map(before.entries.map((e) => [e.task, e]));
  const out: Change[] = [];
  for (const e of after.entries) {
    const p = prev.get(e.task);
    if (!p) {
      out.push({ task: e.task, robotBefore: null, robotAfter: e.robot, shiftH: null });
      continue;
    }
    if (atH !== undefined && p.start_h < atH && p.robot === e.robot) {
      const shift = e.end_h - p.end_h;
      if (shift !== 0) {
        out.push({ task: e.task, robotBefore: p.robot, robotAfter: e.robot, shiftH: shift, continues: true });
      }
      continue;
    }
    const shift = e.start_h - p.start_h;
    if (p.robot !== e.robot || shift !== 0) {
      out.push({ task: e.task, robotBefore: p.robot, robotAfter: e.robot, shiftH: shift });
    }
  }
  return out;
}

function signed(h: number): string {
  return `${h > 0 ? "+" : h < 0 ? "−" : "±"}${formatHours(Math.abs(h))}`;
}

/** The closing `replan` step's payload (ReplanOutcomePayload), when the trace has one. */
function replanOutcome(steps: LoopStep[]): { step: LoopStep; payload: ReplanOutcomePayload | null } | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    const s = steps[i];
    if (s.stage !== "replan") continue;
    const p = s.payload;
    const payload = p && typeof p.escalated === "boolean" ? (p as unknown as ReplanOutcomePayload) : null;
    return { step: s, payload };
  }
  return null;
}

/** The model that answered the last repair prompt of a replan trace, for the headline. */
function repairModel(steps: LoopStep[]): string {
  for (let i = steps.length - 1; i >= 0; i--) {
    const s = steps[i];
    if (s.stage !== "repair" || !s.usage?.model) continue;
    return isFixtureModel(s.usage.model) ? "Nemotron (recorded answer)" : modelDisplayName(s.usage.model);
  }
  return ESCALATION_MODEL;
}

/** The one-line story of the replan, built from the closing step's payload when it parses
 *  ("CP-SAT rescheduled 13 tasks in <0.01 s, Nemotron not needed · makespan 89 h → 91 h");
 *  for older recordings and failed replans the backend message, tidied ("89 h -> 91 h" reads
 *  "89 h → 91 h"). Recordings made before phase 3d end the message in a time-base flag,
 *  "(absolute)", which is dropped; current ones carry none, so the strip is a no-op. */
function outcomeHeadline(
  outcome: { step: LoopStep; payload: ReplanOutcomePayload | null } | null,
  after: PlanResult,
  escalated: boolean,
): string | null {
  if (!outcome) return null;
  const p = outcome.payload;
  const ok = after.success && after.schedule !== null;
  if (p && ok && p.after_makespan_h !== null) {
    const n = plural(p.remaining_tasks, "task");
    const solve = p.solve_time_s !== null ? ` in ${fmtSeconds(p.solve_time_s)}` : "";
    const span = `makespan ${fmtHours(p.before_makespan_h, 0)} → ${fmtHours(p.after_makespan_h, 0)}`;
    if (!escalated) return `CP-SAT rescheduled ${n}${solve}, Nemotron not needed · ${span}`;
    return `CP-SAT could not finish ${n}; ${repairModel(after.steps)} repaired the graph and CP-SAT re-verified it${solve} · ${span}`;
  }
  const msg = outcome.step.message;
  return msg ? tidyMessage(msg.replace(/\s*\((absolute|relative)\)\s*$/, "")) : null;
}

/** The verification receipt of the last ok `schedule` step of the replan trace. */
function replanReceipt(steps: LoopStep[]): VerificationReceipt | null {
  for (let i = steps.length - 1; i >= 0; i--) {
    const s = steps[i];
    if (s.stage !== "schedule" || !s.ok) continue;
    const v = s.payload?.verification;
    if (v && typeof v === "object" && "battery" in (v as object)) return v as VerificationReceipt;
    return null;
  }
  return null;
}

const cardClass = "rounded-lg border border-line bg-raised p-4 md:p-5";
const mutedClass = "text-muted";

export function BeforeAfter({ record, requirements, tasks, index, onSelectTask, defaultOpen }: BeforeAfterProps) {
  const { event, before, after } = record;
  const horizonH = requirements.horizon_h;
  const afterSchedule = after.success ? after.schedule : null;
  const kindColor = EVENT_COLORS[event.kind] ?? "#94a3b8";

  const changes = useMemo(
    () => (afterSchedule ? diffSchedules(before, afterSchedule, record.t_h) : []),
    [before, afterSchedule, record.t_h],
  );
  const reassigned = changes.filter((c) => c.robotBefore !== null && c.robotBefore !== c.robotAfter);
  const highlightAfter = (reassigned.length > 0 ? reassigned : changes).map((c) => c.task);
  // robots the replan could no longer use: the one that failed now (with its hour) and any
  // lost earlier (hour unknown here)
  const failedNow: FailureSpec[] = useMemo(
    () => (event.kind === "robot_failure" && event.robot ? [{ robot: event.robot, t_h: event.t_h }] : []),
    [event],
  );
  const failedEarlier = useMemo(() => {
    const alive = new Set(after.requirements.robots.map((r) => r.id));
    return requirements.robots
      .map((r) => r.id)
      .filter((id) => !alive.has(id) && !failedNow.some((f) => f.robot === id));
  }, [requirements, after.requirements, failedNow]);
  const eventMarker = {
    t_h: event.t_h,
    label: `${eventKindLabel(event.kind)}${event.robot ? ` ${event.robot}` : ""}`,
    color: kindColor,
  };

  const zoneIds = useMemo(() => defaultZoneIds(requirements, tasks), [requirements, tasks]);
  const legendZones = zoneIds.filter((z) => tasks.some((t) => t.zone === z));
  const robotTypes = [...new Set(requirements.robots.map((r) => r.type))];

  const outcome = replanOutcome(after.steps);
  const summary = outcomeOf(record);
  const escalated = record.escalated || outcome?.payload?.escalated === true;
  const headline = outcomeHeadline(outcome, after, escalated);
  const receipt = afterSchedule ? replanReceipt(after.steps) : null;
  const tightest = receipt?.battery.find((b) => b.robot === receipt.tightest_robot) ?? null;

  const delta = afterSchedule ? afterSchedule.makespan_h - before.makespan_h : null;
  // same time axis on both charts: the later of the two makespans (the Gantt adds the horizon
  // itself when it is close enough to be drawn)
  const sharedSpanH = Math.max(
    before.makespan_h,
    afterSchedule?.makespan_h ?? 0,
    ...before.entries.map((e) => e.end_h),
    ...(afterSchedule?.entries.map((e) => e.end_h) ?? []),
  );
  const deltaTone =
    delta === null || delta === 0
      ? "border-white/20 text-fg"
      : delta > 0
        ? "border-warn/50 bg-warn/10 text-warn"
        : "border-ok/50 bg-ok/10 text-ok";

  const outcomeChip = afterSchedule ? (
    escalated ? (
      <Badge
        tone="info"
        title={`CP-SAT alone could not finish the remaining work: a repair prompt with the infeasibility core went to ${ESCALATION_MODEL} and the solver re-verified its answer`}
      >
        {escalationText(summary)}
      </Badge>
    ) : (
      <Badge
        tone="neutral"
        title={`The remaining ${plural(record.remaining_tasks.length, "task")} were rescheduled by CP-SAT alone${
          outcome?.payload?.solve_time_s !== null && outcome?.payload?.solve_time_s !== undefined
            ? ` in ${fmtSeconds(outcome.payload.solve_time_s)}`
            : ""
        }; no model call was needed`}
      >
        CP-SAT rescheduled, Nemotron not needed
      </Badge>
    )
  ) : escalated ? (
    <Badge tone="warn" title="CP-SAT alone could not finish the remaining work, so a repair prompt was due to Nemotron">
      {escalationText(summary)}
    </Badge>
  ) : null;

  return (
    <section className={`${cardClass} space-y-4`} aria-labelledby={`replan-${index}-heading`}>
      <header className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id={`replan-${index}-heading`} className="text-base font-semibold tracking-tight">
            Replan #{index} at {formatHours(record.t_h)} after {eventKindLabel(event.kind)}
            {event.robot ? ` ${event.robot}` : ""}
          </h3>
          <Badge tone={afterSchedule ? "ok" : "fail"}>{afterSchedule ? "re-verified" : "replan failed"}</Badge>
          {outcomeChip}
        </div>
        <div className={`flex flex-wrap items-center gap-x-2 gap-y-1 text-sm ${mutedClass}`}>
          <KindPill kind={event.kind} />
          {event.task && <span className="font-mono text-xs">{event.task}</span>}
          <span>{event.detail ?? "No detail recorded."}</span>
        </div>
        {headline ? <p className="text-[13px] text-fg">{headline}</p> : null}
        <p className={`text-xs ${mutedClass}`}>
          {plural(record.remaining_tasks.length, "task")} remaining · {formatHours(horizonH - record.t_h)} of the{" "}
          {formatHours(horizonH)} horizon left · {plural(after.requirements.robots.length, "robot")} available
        </p>
      </header>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="min-w-0">
          <Gantt
            schedule={before}
            tasks={tasks}
            requirements={requirements}
            horizonH={horizonH}
            title="Before"
            nowH={record.t_h}
            markers={[eventMarker]}
            highlightTasks={event.task ? [event.task] : []}
            compact
            minSpanH={sharedSpanH}
            showLegend={false}
            failures={failedNow}
            failedRobots={failedEarlier}
            dimBeforeNow
            zoneIds={zoneIds}
            onSelectTask={onSelectTask}
          />
        </div>
        <div className="min-w-0">
          {afterSchedule ? (
            <Gantt
              schedule={afterSchedule}
              tasks={tasks}
              requirements={requirements}
              horizonH={horizonH}
              title="After"
              nowH={record.t_h}
              markers={[eventMarker]}
              highlightTasks={highlightAfter}
              compact
              minSpanH={sharedSpanH}
              showLegend={false}
              failures={failedNow}
              failedRobots={failedEarlier}
              dimBeforeNow
              zoneIds={zoneIds}
              onSelectTask={onSelectTask}
            />
          ) : (
            <FailurePanel after={after} atH={record.t_h} />
          )}
        </div>
      </div>

      {/* one legend for both charts */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
        <span className="text-dim">bars = zone · dot and stripe = robot type</span>
        {legendZones.map((z) => (
          <span key={z} className="inline-flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: zoneColor(z, zoneIds) }} />
            zone {z}
          </span>
        ))}
        <span className="text-dim" aria-hidden>
          ·
        </span>
        {robotTypes.map((t) => (
          <span key={t} className="inline-flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: robotTypeColor(t) }} />
            {t}
          </span>
        ))}
        <span className="ml-auto inline-flex items-center gap-1">
          <span className="inline-block w-4 border-t-2 border-white" />
          now
        </span>
        {failedNow.length > 0 ? (
          <span className="inline-flex items-center gap-1 text-fail">
            <span aria-hidden className="font-bold">
              ✕
            </span>
            robot failed
          </span>
        ) : null}
        <span className="text-dim">left of now = done</span>
      </div>

      <dl className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <div className="flex items-center gap-2">
          <dt className={mutedClass}>Makespan</dt>
          <dd className="flex items-center gap-2 tabular-nums">
            <span>{formatHours(before.makespan_h)}</span>
            <span aria-hidden>→</span>
            <span className="font-semibold">{afterSchedule ? formatHours(afterSchedule.makespan_h) : "—"}</span>
            {delta !== null && (
              <span className={`rounded-full border px-2 py-0.5 text-sm font-semibold ${deltaTone}`}>
                {signed(delta)}
              </span>
            )}
            {afterSchedule && afterSchedule.makespan_h > horizonH && (
              <span className="text-xs text-fail">exceeds the horizon</span>
            )}
          </dd>
        </div>
        <div className="flex items-center gap-2">
          <dt className={mutedClass}>Repairs used</dt>
          <dd className="tabular-nums">
            {summary.repairsAnswered}
            {summary.noAnswer && <span className="ml-1.5 text-xs text-muted">offline, no recorded repair</span>}
          </dd>
        </div>
        {afterSchedule && (
          <div className="flex items-center gap-2">
            <dt className={mutedClass}>Solver</dt>
            <dd className="tabular-nums">
              {afterSchedule.status} · {fmtSeconds(afterSchedule.solve_time_s)}
            </dd>
          </div>
        )}
        {receipt && (
          <div className="flex items-center gap-2">
            <dt className={mutedClass}>Re-verified</dt>
            <dd className="tabular-nums text-xs" title={receipt.recheck}>
              {plural(receipt.precedence_edges, "precedence edge")} · {plural(receipt.robot_rows, "robot row")} ·{" "}
              {plural(receipt.zone_groups, "zone group")}
              {tightest ? ` · tightest battery ${tightest.robot} ${Math.round(tightest.pct)}%` : ""}
            </dd>
          </div>
        )}
        <div className="flex min-w-0 items-center gap-2">
          <dt className={mutedClass}>Trace</dt>
          <dd>
            <TraceChips steps={after.steps} />
          </dd>
        </div>
      </dl>

      {changes.length > 0 && (
        <div className="text-xs">
          <span className={`${mutedClass} mr-2`}>Changes ({changes.length})</span>
          <span className="inline-flex flex-wrap gap-1.5 align-middle">
            {changes.map((c) => (
              <span
                key={c.task}
                className="inline-flex items-center gap-1 rounded-md border border-white/15 px-1.5 py-0.5 font-mono tabular-nums"
                title={
                  c.shiftH === null
                    ? `${c.task} is new in the replanned schedule`
                    : c.continues
                      ? `${c.task} continues on ${c.robotAfter}; its end moves ${signed(c.shiftH)}`
                      : `${c.task}: ${c.robotBefore} → ${c.robotAfter}, start ${signed(c.shiftH)}`
                }
              >
                <span className="font-semibold">{c.task}</span>
                {c.robotBefore !== null && c.robotBefore !== c.robotAfter ? (
                  <span>
                    {c.robotBefore}
                    <span aria-hidden> → </span>
                    {c.robotAfter}
                  </span>
                ) : (
                  <span className={mutedClass}>{c.robotAfter}</span>
                )}
                {c.shiftH === null ? (
                  <span className={mutedClass}>new</span>
                ) : c.continues ? (
                  <span className={mutedClass}>continues, ends {signed(c.shiftH)}</span>
                ) : (
                  c.shiftH !== 0 && <span className={mutedClass}>{signed(c.shiftH)}</span>
                )}
              </span>
            ))}
          </span>
        </div>
      )}

      {after.explanation && <p className={`text-sm leading-6 ${mutedClass}`}>{after.explanation}</p>}

      <details className="group rounded-lg border border-line" open={defaultOpen}>
        <summary className={`cursor-pointer select-none px-3 py-2 text-xs font-medium ${mutedClass} hover:text-fg`}>
          Replanning context given to the planner
        </summary>
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap border-t border-line px-3 py-2 font-mono text-[11px] leading-5">
          {record.context}
        </pre>
      </details>
    </section>
  );
}

function TraceChips({ steps }: { steps: LoopStep[] }) {
  if (steps.length === 0) return <span className={`text-xs ${mutedClass}`}>no steps recorded</span>;
  return (
    <ol className="flex flex-wrap items-center gap-1">
      {steps.map((s, i) => {
        // repair rounds count from the first repair; the backend's `attempt` counts task graphs
        const round = s.stage === "repair" ? repairRounds(steps.slice(0, i + 1)) : null;
        return (
        <li key={i} className="flex items-center gap-1">
          <span
            title={[tidyMessage(s.message), s.failure ? tidyHint(s.failure.hint) : null].filter(Boolean).join(" — ") || undefined}
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 ${
              s.ok
                ? "border-ok/40 bg-ok/10"
                : "border-fail/50 bg-fail/10"
            }`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${s.ok ? "bg-ok" : "bg-fail"}`} aria-hidden />
            {STAGE_LABELS[s.stage] ?? s.stage}
            {round !== null ? (
              <span className={mutedClass}>round {round}</span>
            ) : s.attempt > 1 ? (
              <span className={mutedClass}>graph v{s.attempt}</span>
            ) : null}
          </span>
          {i < steps.length - 1 && (
            <span className={`text-[11px] ${mutedClass}`} aria-hidden>
              →
            </span>
          )}
        </li>
        );
      })}
    </ol>
  );
}

/** The "After" cell of a failed replan: the verdict (kind, conflicts, hint, details), then the
 *  advice the replanner attached (ReplanRecord.after.suggestions, or the final failed step's
 *  payload): absolute simulator hours against the brief's deadline, unless an older recording's
 *  step says its hours are planner-relative, in which case the note names the offset. */
function FailurePanel({ after, atH }: { after: PlanResult; atH: number }) {
  const failure: Failure | null = after.failure;
  const advice = planSuggestions(after);
  const failedStep = lastFailedStep(after.steps);
  // the PlanResult-level list of a replan is rebased to absolute hours by the backend; only a
  // step payload recorded without the flag is relative
  const own = Array.isArray(after.suggestions) && after.suggestions.length > 0;
  const timeBase = own || !failedStep ? "absolute" : suggestionsTimeBase(failedStep);
  const offset = failedStep?.payload?.offset_h;
  return (
    <div className="flex h-full min-h-40 flex-col gap-2 rounded-lg border border-fail/40 bg-fail/5 p-3 text-sm">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-full bg-fail" aria-hidden />
        <span className="font-semibold">Replan failed{failure ? `: ${failure.kind}` : ""}</span>
      </div>
      {failure ? (
        <>
          {failure.conflicts.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {failure.conflicts.map((c) => (
                <span
                  key={c}
                  title={conflictMeaning(c) ?? conflictPrefix(c)}
                  className="rounded-md border border-fail/50 px-1.5 py-0.5 font-mono text-[11px]"
                >
                  {c}
                </span>
              ))}
            </div>
          )}
          {failure.hint && <p className={mutedClass}>{tidyHint(failure.hint)}</p>}
          {failure.details.length > 0 && (
            <ul className={`list-disc space-y-0.5 pl-5 text-xs ${mutedClass}`}>
              {failure.details.slice(0, 6).map((d, i) => (
                <li key={i}>{tidyDetail(d)}</li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <p className={mutedClass}>The planner returned no schedule and no failure record.</p>
      )}
      {advice.length > 0 && (
        <Suggestions
          variant="replan"
          suggestions={advice}
          timeBase={timeBase}
          offsetH={typeof offset === "number" && Number.isFinite(offset) ? offset : atH}
        />
      )}
      <p className={`mt-auto text-xs ${mutedClass}`}>The previous schedule stays in force.</p>
    </div>
  );
}
