"use client";
/**
 * Chronological list of simulator events interleaved with the replans they triggered.
 * Clicking a row (or pressing Enter / Space on it) seeks the replay (`onSeek(t_h)`); the row at
 * or just before `nowH` is highlighted and kept in view. Also exports the small formatting
 * helpers shared by the sim components: hours, colours, the one-line event phrase and the
 * replan outcome read from a ReplanRecord and the closing `replan` step of its trace.
 */
import { useEffect, useRef } from "react";
import type { EventKind, Failure, ReplanOutcomePayload, ReplanRecord, SimEvent } from "@/lib/types";
import { EVENT_COLORS } from "@/lib/palette";
import { fmtSeconds, tidyHint } from "@/lib/format";

export interface EventListProps {
  events: SimEvent[];
  replans: ReplanRecord[];
  nowH?: number;
  onSeek?: (t_h: number) => void;
  /** Additive: CSS max-height of the scrolling list (default "26rem"). */
  maxHeight?: string;
}

export const EVENT_KIND_LABELS: Record<EventKind, string> = {
  robot_failure: "robot failure",
  battery_low: "battery low",
  task_overrun: "task overrun",
};

/** The model a solver-first replan escalates to (backend REPAIR routing). */
export const ESCALATION_MODEL = "Nemotron 3 Ultra";

export function eventKindLabel(kind: string): string {
  return EVENT_KIND_LABELS[kind as EventKind] ?? kind.replace(/_/g, " ");
}

/** Hours for captions: integers plain ("30"), otherwise up to two decimals ("59.25", "90.3"). */
export function formatHours(t: number, unit = " h"): string {
  if (!Number.isFinite(t)) return "—";
  const r = Math.round(t * 100) / 100;
  const text = Number.isInteger(r) ? String(r) : r.toFixed(2).replace(/0$/, "");
  return text + unit;
}

/** "#rrggbb" + alpha -> "rgba(r,g,b,a)" (palette colours are 6-digit hex). */
export function withAlpha(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/**
 * One short clause per event, built from the simulator's detail string:
 * "EX2 failed on T5 (44% done)", "HL2 ran out of battery heading to T8",
 * "HL2 battery low: 400 Wh left, 650 Wh needed", "T7 overran by 3 h on HL1".
 */
export function eventPhrase(e: SimEvent): string {
  const d = e.detail ?? "";
  const pct = /\((\d+)% done\)/.exec(d)?.[1];
  const done = pct !== undefined ? ` (${pct}% done)` : "";
  switch (e.kind) {
    case "robot_failure": {
      const who = e.robot ?? "a robot";
      const how = /ran out of battery/i.test(d) ? "ran out of battery" : "failed";
      if (!e.task) return `${who} ${how} while idle`;
      const heading = /heading to/i.test(d);
      return `${who} ${how} ${heading ? "heading to" : "on"} ${e.task}${done}`;
    }
    case "battery_low": {
      const m = /has (\d[\d,]*) Wh left.*?needs (\d[\d,]*) Wh/i.exec(d);
      const who = e.robot ?? "a robot";
      return m ? `${who} battery low: ${m[1]} Wh left, ${m[2]} Wh needed` : `${who} battery low`;
    }
    case "task_overrun": {
      const m = /\(([\d.]+) h over\)/.exec(d);
      const what = e.task ?? "a task";
      return `${what} overran${m ? ` by ${m[1]} h` : ""}${e.robot ? ` on ${e.robot}` : ""}`;
    }
    default:
      return d || eventKindLabel(e.kind);
  }
}

/** What a replan did: the record plus the closing `replan` step of its trace (payload
 *  ReplanOutcomePayload), with the record's own fields as fallback for older recordings. */
export interface ReplanOutcome {
  /** a verified schedule came out of it */
  ok: boolean;
  /** CP-SAT alone could not finish the remaining work: a repair prompt went to Nemotron */
  escalated: boolean;
  remaining: number;
  beforeH: number;
  afterH: number | null;
  solveS: number | null;
  /** PlanResult.repairs of the replan: every repair round, answered or not */
  repairs: number;
  /** repair steps that returned an answer; a failed repair without usage ("offline: no repair
   *  fixture …") is an escalation nobody answered */
  repairsAnswered: number;
  /** escalated, but no model answered (the offline client had no recorded repair) */
  noAnswer: boolean;
  failure: Failure | null;
}

export function replanOutcome(record: ReplanRecord): ReplanOutcome {
  const after = record.after;
  const closing = [...after.steps].reverse().find((s) => s.stage === "replan");
  const p = (closing?.payload ?? null) as Partial<ReplanOutcomePayload> | null;
  const schedule = after.success ? after.schedule : null;
  const escalated = typeof p?.escalated === "boolean" ? p.escalated : record.escalated;
  const repairsAnswered = after.steps.filter((s) => s.stage === "repair" && (s.ok || s.usage !== null)).length;
  return {
    ok: schedule !== null,
    escalated,
    repairsAnswered,
    noAnswer: escalated && repairsAnswered === 0,
    remaining: typeof p?.remaining_tasks === "number" ? p.remaining_tasks : record.remaining_tasks.length,
    beforeH: typeof p?.before_makespan_h === "number" ? p.before_makespan_h : record.before.makespan_h,
    afterH: schedule ? (typeof p?.after_makespan_h === "number" ? p.after_makespan_h : schedule.makespan_h) : null,
    solveS: typeof p?.solve_time_s === "number" ? p.solve_time_s : (schedule?.solve_time_s ?? null),
    repairs: after.repairs,
    failure: after.failure,
  };
}

/** "no verified replan: infeasible energy:EX1, capability:excavator" */
export function replanFailureText(o: ReplanOutcome): string {
  const f = o.failure;
  if (!f) return "no verified replan";
  const ids = f.conflicts.slice(0, 3).join(", ") + (f.conflicts.length > 3 ? ` +${f.conflicts.length - 3}` : "");
  return `no verified replan: ${f.kind}${ids ? ` ${ids}` : ""}`;
}

/** "escalated to Nemotron 3 Ultra (1 repair)", or the honest clause when no model answered
 *  (the offline client had no recorded repair for this event). */
export function escalationText(o: ReplanOutcome): string {
  if (o.noAnswer) return "Nemotron escalation needed, no recorded repair (offline)";
  const n = o.repairsAnswered;
  return `escalated to ${ESCALATION_MODEL}${n > 0 ? ` (${n} repair${n === 1 ? "" : "s"})` : ""}`;
}

/** The clauses that follow the event in the replay banner, in order; the last one carries the
 *  verdict: ["CP-SAT rescheduled 13 tasks in <0.01 s", "verified, 89 h → 91 h"] or
 *  ["CP-SAT could not finish 13 tasks", "escalated to Nemotron 3 Ultra (1 repair)",
 *  "verified, 89 h → 95 h"] or [..., "no verified replan: infeasible energy:EX1"]. */
export function replanSegments(o: ReplanOutcome): string[] {
  const n = `${o.remaining} task${o.remaining === 1 ? "" : "s"}`;
  const out: string[] = [];
  if (o.escalated) {
    out.push(`CP-SAT could not finish ${n}`);
    out.push(escalationText(o));
  } else if (o.ok) {
    out.push(`CP-SAT rescheduled ${n}${o.solveS !== null ? ` in ${fmtSeconds(o.solveS)}` : ""}`);
  }
  out.push(o.ok && o.afterH !== null ? `verified, ${formatHours(o.beforeH)} → ${formatHours(o.afterH)}` : replanFailureText(o));
  return out;
}

/** Small pill: coloured dot (the mark) + label in text colour. */
export function KindPill({ kind, className = "" }: { kind: string; className?: string }) {
  const color = EVENT_COLORS[kind] ?? "#94a3b8";
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 ${className}`}
      style={{ borderColor: withAlpha(color, 0.45), backgroundColor: withAlpha(color, 0.12) }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} aria-hidden />
      {eventKindLabel(kind)}
    </span>
  );
}

type Row =
  | { type: "event"; key: string; t: number; order: number; event: SimEvent }
  | { type: "replan"; key: string; t: number; order: number; record: ReplanRecord; index: number };

function buildRows(events: SimEvent[], replans: ReplanRecord[]): Row[] {
  const rows: Row[] = [];
  events.forEach((event, i) => rows.push({ type: "event", key: `e${i}`, t: event.t_h, order: 0, event }));
  replans.forEach((record, i) =>
    rows.push({ type: "replan", key: `r${i}`, t: record.t_h, order: 1, record, index: i + 1 }),
  );
  // stable sort: time, then events before the replan they triggered
  return rows.sort((a, b) => a.t - b.t || a.order - b.order);
}

export function EventList({ events, replans, nowH, onSeek, maxHeight = "26rem" }: EventListProps) {
  const rows = buildRows(events, replans);
  const listRef = useRef<HTMLOListElement>(null);
  // the row(s) at or just before nowH: an event and the replan it triggered share a time
  let activeT: number | null = null;
  if (nowH !== undefined) {
    for (const row of rows) if (row.t <= nowH + 1e-6) activeT = row.t;
  }

  // keep the highlighted row inside the list's own scroll box (never scrolls the page)
  useEffect(() => {
    const ol = listRef.current;
    if (!ol || activeT === null) return;
    const el = ol.querySelector<HTMLElement>('[aria-current="true"]');
    if (!el) return;
    const top = el.offsetTop - ol.offsetTop;
    const bottom = top + el.offsetHeight;
    if (top < ol.scrollTop) ol.scrollTop = top;
    else if (bottom > ol.scrollTop + ol.clientHeight) ol.scrollTop = bottom - ol.clientHeight;
  }, [activeT]);

  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-white/15 px-3 py-4 text-center text-sm text-muted">
        No events: the plan ran exactly as scheduled.
      </p>
    );
  }

  const seekable = typeof onSeek === "function";
  return (
    <ol ref={listRef} className="relative space-y-1 overflow-y-auto pr-1" style={{ maxHeight }} aria-label="Simulation events">
      {rows.map((row) => {
        const active = activeT !== null && row.t === activeT;
        const base =
          "grid w-full grid-cols-[4.25rem_minmax(0,1fr)] gap-x-3 rounded-lg border px-3 py-2 text-left text-sm transition-colors " +
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60";
        const tone = active ? "border-info/50 bg-info/10" : "border-transparent hover:bg-white/5";
        return (
          <li key={row.key}>
            <button
              type="button"
              onClick={seekable ? () => onSeek(row.t) : undefined}
              aria-current={active ? "true" : undefined}
              title={seekable ? `Seek the replay to ${formatHours(row.t)}` : undefined}
              className={`${base} ${tone} ${seekable ? "cursor-pointer" : "cursor-default"}`}
            >
              <span className="pt-0.5 font-mono text-xs tabular-nums text-muted">
                {formatHours(row.t)}
              </span>
              {row.type === "event" ? <EventCells event={row.event} /> : <ReplanCells record={row.record} index={row.index} />}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function EventCells({ event }: { event: SimEvent }) {
  const phrase = eventPhrase(event);
  return (
    <span className="min-w-0">
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <KindPill kind={event.kind} />
        {event.robot && <span className="font-mono text-xs font-semibold">{event.robot}</span>}
        {event.task && (
          <span className="font-mono text-xs text-muted">{event.task}</span>
        )}
      </span>
      <span className="mt-0.5 block truncate text-xs text-muted" title={event.detail ?? undefined}>
        {phrase}
      </span>
    </span>
  );
}

function ReplanCells({ record, index }: { record: ReplanRecord; index: number }) {
  const o = replanOutcome(record);
  const summary = o.ok && o.afterH !== null
    ? `replanned: ${formatHours(o.beforeH)} → ${formatHours(o.afterH)} · ${o.escalated ? escalationText(o) : "CP-SAT only"}`
    : `${replanFailureText(o)}${o.escalated ? ` · ${escalationText(o)}` : ""}`;
  const parts: string[] = [`${o.remaining} task${o.remaining === 1 ? "" : "s"} remaining`];
  if (o.ok && !o.escalated && o.solveS !== null) parts.push(`solved in ${fmtSeconds(o.solveS)}`);
  if (o.repairsAnswered > 0) parts.push(`${o.repairsAnswered} repair${o.repairsAnswered === 1 ? "" : "s"}`);
  if (o.ok && record.after.schedule) parts.push(record.after.schedule.status);
  if (!o.ok && o.failure?.hint) parts.push(tidyHint(o.failure.hint));
  return (
    <span className="min-w-0">
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span
          className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 ${
            o.ok ? "border-info/40 bg-info/10" : "border-fail/50 bg-fail/10"
          }`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${o.ok ? "bg-info" : "bg-fail"}`} aria-hidden />
          Replan #{index}
        </span>
        <span className="text-xs font-medium tabular-nums">{summary}</span>
      </span>
      <span className="mt-0.5 block truncate text-xs text-muted" title={parts.join(" · ")}>
        {parts.join(" · ")}
      </span>
    </span>
  );
}
