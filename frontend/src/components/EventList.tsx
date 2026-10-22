"use client";
/**
 * Chronological list of simulator events interleaved with the replans they triggered.
 * Clicking a row seeks the replay (`onSeek(t_h)`); the row at or just before `nowH` is
 * highlighted. Also exports the small formatting helpers shared by the sim components.
 */
import type { EventKind, ReplanRecord, SimEvent } from "@/lib/types";
import { EVENT_COLORS } from "@/lib/palette";

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
  // the row(s) at or just before nowH: an event and the replan it triggered share a time
  let activeT: number | null = null;
  if (nowH !== undefined) {
    for (const row of rows) if (row.t <= nowH + 1e-6) activeT = row.t;
  }

  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-white/15 px-3 py-4 text-center text-sm text-muted">
        No events: the plan ran exactly as scheduled.
      </p>
    );
  }

  const seekable = typeof onSeek === "function";
  return (
    <ol className="space-y-1 overflow-y-auto pr-1" style={{ maxHeight }} aria-label="Simulation events">
      {rows.map((row) => {
        const active = activeT !== null && row.t === activeT;
        const base =
          "grid w-full grid-cols-[4.25rem_minmax(0,1fr)] gap-x-3 rounded-lg border px-3 py-2 text-left text-sm transition-colors";
        const tone = active
          ? "border-info/50 bg-info/10"
          : "border-transparent hover:bg-white/5";
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
  return (
    <span className="min-w-0">
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <KindPill kind={event.kind} />
        {event.robot && <span className="font-mono text-xs font-semibold">{event.robot}</span>}
        {event.task && (
          <span className="font-mono text-xs text-muted">{event.task}</span>
        )}
      </span>
      {event.detail && (
        <span className="mt-0.5 block truncate text-xs text-muted" title={event.detail}>
          {event.detail}
        </span>
      )}
    </span>
  );
}

function ReplanCells({ record, index }: { record: ReplanRecord; index: number }) {
  const after = record.after;
  const ok = after.success && after.schedule !== null;
  const summary = ok
    ? `replanned: ${formatHours(record.before.makespan_h)} → ${formatHours(after.schedule!.makespan_h)}`
    : `replan failed${after.failure ? ` · ${after.failure.kind}` : ""}`;
  return (
    <span className="min-w-0">
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span
          className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 ${
            ok
              ? "border-info/40 bg-info/10"
              : "border-fail/50 bg-fail/10"
          }`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-info" : "bg-fail"}`} aria-hidden />
          Replan #{index}
        </span>
        <span className="text-xs font-medium tabular-nums">{summary}</span>
      </span>
      <span className="mt-0.5 block truncate text-xs text-muted">
        {record.remaining_tasks.length} tasks remaining · {after.repairs} repair{after.repairs === 1 ? "" : "s"}
        {ok && after.schedule ? ` · ${after.schedule.status}` : ""}
        {!ok && after.failure?.hint ? ` · ${after.failure.hint}` : ""}
      </span>
    </span>
  );
}
