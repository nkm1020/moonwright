"use client";
/**
 * One replan: the schedule in force before the event next to the replanned schedule, both in
 * absolute simulator hours on the same horizon, plus makespan delta, repair count, the
 * replanning trace and the context the planner was given.
 */
import { useMemo } from "react";
import type { Failure, LoopStep, ReplanRecord, Requirements, Schedule, Task } from "@/lib/types";
import { CONFLICT_PREFIXES, conflictPrefix } from "@/lib/types";
import { EVENT_COLORS, STAGE_LABELS } from "@/lib/palette";
import { fmtSeconds } from "@/lib/format";
import { Gantt } from "./Gantt";
import { KindPill, eventKindLabel, formatHours } from "./EventList";
import { Badge } from "./Section";

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

const cardClass =
  "rounded-xl border border-line bg-raised p-4";
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
  // robots the replan could no longer use (the failed one, and any lost earlier)
  const failedRobots = useMemo(() => {
    const alive = new Set(after.requirements.robots.map((r) => r.id));
    return requirements.robots.map((r) => r.id).filter((id) => !alive.has(id));
  }, [requirements, after.requirements]);
  const eventMarker = {
    t_h: event.t_h,
    label: `${eventKindLabel(event.kind)}${event.robot ? ` ${event.robot}` : ""}`,
    color: kindColor,
  };

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

  return (
    <section className={`${cardClass} space-y-4`} aria-labelledby={`replan-${index}-heading`}>
      <header className="space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id={`replan-${index}-heading`} className="text-base font-semibold tracking-tight">
            Replan #{index} at {formatHours(record.t_h)} after {eventKindLabel(event.kind)}
            {event.robot ? ` ${event.robot}` : ""}
          </h3>
          <Badge tone={afterSchedule ? "ok" : "fail"}>{afterSchedule ? "re-verified" : "replan failed"}</Badge>
        </div>
        <div className={`flex flex-wrap items-center gap-x-2 gap-y-1 text-sm ${mutedClass}`}>
          <KindPill kind={event.kind} />
          {event.task && <span className="font-mono text-xs">{event.task}</span>}
          <span>{event.detail ?? "No detail recorded."}</span>
        </div>
        <p className={`text-xs ${mutedClass}`}>
          {record.remaining_tasks.length} tasks remaining · horizon {formatHours(horizonH)} ·{" "}
          {after.requirements.robots.length} robots available
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
              failedRobots={failedRobots}
              onSelectTask={onSelectTask}
            />
          ) : (
            <FailurePanel failure={after.failure} />
          )}
        </div>
      </div>

      <dl className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <div className="flex items-center gap-2">
          <dt className={mutedClass}>Makespan</dt>
          <dd className="flex items-center gap-2 tabular-nums">
            <span>{formatHours(before.makespan_h)}</span>
            <span aria-hidden>→</span>
            <span className="font-semibold">{afterSchedule ? formatHours(afterSchedule.makespan_h) : "—"}</span>
            {delta !== null && (
              <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${deltaTone}`}>
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
          <dd className="tabular-nums">{after.repairs}</dd>
        </div>
        {afterSchedule && (
          <div className="flex items-center gap-2">
            <dt className={mutedClass}>Solver</dt>
            <dd className="tabular-nums">
              {afterSchedule.status} · {fmtSeconds(afterSchedule.solve_time_s)}
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
      {steps.map((s, i) => (
        <li key={i} className="flex items-center gap-1">
          <span
            title={[s.message, s.failure?.hint].filter(Boolean).join(" — ") || undefined}
            className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 ${
              s.ok
                ? "border-ok/40 bg-ok/10"
                : "border-fail/50 bg-fail/10"
            }`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${s.ok ? "bg-ok" : "bg-fail"}`} aria-hidden />
            {STAGE_LABELS[s.stage] ?? s.stage}
            {s.attempt > 1 && <span className={mutedClass}>#{s.attempt}</span>}
          </span>
          {i < steps.length - 1 && (
            <span className={`text-[10px] ${mutedClass}`} aria-hidden>
              →
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

function FailurePanel({ failure }: { failure: Failure | null }) {
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
                  title={CONFLICT_PREFIXES[conflictPrefix(c)] ?? c}
                  className="rounded-md border border-fail/50 px-1.5 py-0.5 font-mono text-[11px]"
                >
                  {c}
                </span>
              ))}
            </div>
          )}
          {failure.hint && <p className={mutedClass}>{failure.hint}</p>}
          {failure.details.length > 0 && (
            <ul className={`list-disc space-y-0.5 pl-5 text-xs ${mutedClass}`}>
              {failure.details.slice(0, 6).map((d, i) => (
                <li key={i}>{d}</li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <p className={mutedClass}>The planner returned no schedule and no failure record.</p>
      )}
      <p className={`mt-auto text-xs ${mutedClass}`}>The previous schedule stays in force.</p>
    </div>
  );
}
