"use client";
/**
 * Gantt chart of a verified schedule: one row per robot (requirements.robots order), one bar per
 * ScheduleEntry coloured by the task's zone. Pure SVG; the width is measured with a
 * ResizeObserver so the chart fills its container. The time axis always starts at 0 h
 * (absolute planner / simulator hours) so two charts side by side share a scale when their
 * spans agree (see `minSpanH`).
 *
 * Interaction: hover or focus a bar for its tooltip, click / Enter / Space to select it
 * (precedence arrows are drawn from its predecessors), Escape or a click on the background to
 * clear. Selection is controlled through `selectedTask` when given, otherwise kept internally.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { FocusEvent, KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import type { Requirements, Schedule, ScheduleEntry, Task } from "@/lib/types";
import { robotTypeColor, zoneColor } from "@/lib/palette";

export interface GanttMarker {
  t_h: number;
  label: string;
  color?: string;
  /** Additive: markers sharing a group keep their lines but, when more than three of them are
   *  drawn, share one label "<group> ×n" instead of stacking unreadable labels. */
  group?: string;
}

export interface GanttProps {
  schedule: Schedule;
  tasks: Task[];
  requirements: Requirements;
  /** Defaults to requirements.horizon_h; drawn as the right-hand deadline line. */
  horizonH?: number;
  title?: string;
  /** Vertical "now" line in absolute hours (simulation time). */
  nowH?: number;
  markers?: GanttMarker[];
  highlightTasks?: string[];
  /** Smaller rows for side-by-side use. */
  compact?: boolean;
  onSelectTask?: (taskId: string | null) => void;
  /** Controlled selection (ring + precedence arrows). Omit to let the chart keep its own;
   *  a single-entry `highlightTasks` then doubles as the selection. */
  selectedTask?: string | null;
  /** Zone order used for colours. Defaults to the site's zones followed by task-only zones,
   *  i.e. the same order as lib/tasks zoneOrder(). */
  zoneIds?: string[];
  /** Hide the zone legend, e.g. when two compact charts share one. Default true. */
  showLegend?: boolean;
  /** Rows visible before the body scrolls. Default 10. */
  maxVisibleRows?: number;
  /** Lower bound for the axis span in hours, to give before/after charts the same scale. */
  minSpanH?: number;
  /** Additive: robots out of service in this schedule (failed before it was made); their row
   *  is tagged "failed" instead of looking merely idle. */
  failedRobots?: string[];
}

// ---------------------------------------------------------------- palette (dark surface)
const INK = "#e5e7eb";
const MUTED = "#94a3b8";
const FAINT = "#64748b";
const GRID = "rgba(255,255,255,0.08)";
const BAND = "rgba(255,255,255,0.03)";
const DEADLINE = "#f87171";
const DEADLINE_TEXT = "#fca5a5";
const NOW = "#f59e0b";
const NOW_TEXT = "#fcd34d";
const MISSING = "#64748b";

const TICK_STEPS = [1, 2, 4, 8, 12, 24, 48, 72, 96, 168, 336, 720];

// ---------------------------------------------------------------- helpers
/** Mirrors lib/tasks zoneOrder() for callers that only have requirements + tasks. */
export function defaultZoneIds(requirements: Requirements, tasks: Task[]): string[] {
  const ids = requirements.site.zones.map((z) => z.id);
  for (const t of tasks) if (!ids.includes(t.zone)) ids.push(t.zone);
  return ids;
}

export function fmtH(h: number): string {
  return Number.isInteger(h) ? String(h) : h.toFixed(1);
}

export function fmtWh(wh: number): string {
  return Math.round(wh).toLocaleString("en-US");
}

/** Label ink for text drawn inside a coloured fill, picked by the fill's luminance. */
function inkFor(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return "#0b0f14";
  const n = parseInt(m[1], 16);
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const l = 0.2126 * lin(n >> 16) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
  return l > 0.3 ? "#0b0f14" : "#f8fafc";
}

function textWidth(s: string, font: number): number {
  return s.length * font * 0.6;
}

/** Smallest tick step whose labels still have `minPx` of room; the span rounds up to a multiple. */
function chooseScale(spanRaw: number, plotW: number, minPx: number): { step: number; span: number } {
  for (const step of TICK_STEPS) {
    const span = Math.max(step, Math.ceil(spanRaw / step) * step);
    if (plotW / (span / step) >= minPx) return { step, span };
  }
  const step = TICK_STEPS[TICK_STEPS.length - 1];
  return { step, span: Math.max(step, Math.ceil(spanRaw / step) * step) };
}

function arrowPath(x1: number, y1: number, x2: number, y2: number, lift: number): string {
  if (Math.abs(y1 - y2) < 0.5) {
    // same robot: arc above the bars so the arrow does not vanish under them
    const dx = Math.max(8, Math.min(24, Math.abs(x2 - x1) / 3));
    return `M${x1},${y1} C${x1 + dx},${y1 - lift} ${x2 - dx},${y2 - lift} ${x2},${y2}`;
  }
  if (Math.abs(x2 - x1) < 4) return `M${x1},${y1} L${x2},${y2}`;
  const dx = Math.max(10, Math.min(48, Math.abs(x2 - x1) / 2));
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

interface Row {
  id: string;
  type: string | null;
}

interface HoverState {
  key: string;
  x: number;
  y: number;
  wrapW: number;
  wrapH: number;
}

// ---------------------------------------------------------------- component
export function Gantt(props: GanttProps) {
  const {
    schedule,
    tasks,
    requirements,
    title,
    nowH,
    markers,
    highlightTasks,
    compact = false,
    onSelectTask,
    selectedTask,
    zoneIds: zoneIdsProp,
    showLegend = true,
    maxVisibleRows = 10,
    minSpanH,
    failedRobots,
  } = props;
  const horizonH = props.horizonH ?? requirements.horizon_h;

  const arrowId = "gantt-arrow-" + useId().replace(/\W/g, "");
  const wrapRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);
  // undefined = untouched (fall back to a single highlight), null = explicitly cleared
  const [innerSel, setInnerSel] = useState<string | null | undefined>(undefined);
  const [hover, setHover] = useState<HoverState | null>(null);
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const failedSet = useMemo(() => new Set(failedRobots ?? []), [failedRobots]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      const w = Math.floor(el.clientWidth);
      if (w > 0) setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const model = useMemo(() => {
    const taskMap = new Map(tasks.map((t) => [t.id, t]));
    const zoneIds = zoneIdsProp ?? defaultZoneIds(requirements, tasks);
    const rows: Row[] = requirements.robots.map((r) => ({ id: r.id, type: r.type }));
    const rowIndex = new Map(rows.map((r, i) => [r.id, i]));
    for (const e of schedule.entries) {
      if (!rowIndex.has(e.robot)) {
        rowIndex.set(e.robot, rows.length);
        rows.push({ id: e.robot, type: null });
      }
    }
    const entries = [...schedule.entries].sort((a, b) => a.start_h - b.start_h || a.end_h - b.end_h);
    const byTask = new Map<string, ScheduleEntry>();
    for (const e of entries) if (!byTask.has(e.task)) byTask.set(e.task, e);
    const busyRobots = new Set(entries.map((e) => e.robot));
    const makespan = Math.max(schedule.makespan_h, 0, ...entries.map((e) => e.end_h));
    const ref = Math.max(makespan, nowH ?? 0, 1, ...(markers ?? []).map((m) => m.t_h));
    // a deadline far beyond the work would squeeze the bars: show it as a badge instead
    const deadlineInPlot = horizonH > 0 && horizonH <= 3 * ref;
    // an empty schedule still gets a readable 0-8 h axis instead of a single 1 h tick
    const spanRaw = Math.max(deadlineInPlot ? horizonH : 0, ref, minSpanH ?? 0, entries.length ? 0 : 8);
    const hasMissing = entries.some((e) => !taskMap.has(e.task));
    const usedZones = zoneIds.filter((z) => tasks.some((t) => t.zone === z));
    return {
      taskMap,
      zoneIds,
      rows,
      rowIndex,
      entries,
      byTask,
      busyRobots,
      makespan,
      deadlineInPlot,
      spanRaw,
      hasMissing,
      legendZones: usedZones.length > 0 ? usedZones : zoneIds,
    };
  }, [tasks, requirements, schedule, zoneIdsProp, nowH, markers, horizonH, minSpanH]);

  const hasAnnot = nowH !== undefined || (markers?.length ?? 0) > 0;
  const M = compact
    ? { rowH: 22, barH: 14, labelW: 66, padR: 16, dayBand: 14, tickBand: 14, font: 10, minTickPx: 36 }
    : { rowH: 34, barH: 22, labelW: 110, padR: 24, dayBand: 18, tickBand: 18, font: 11, minTickPx: 44 };
  const annotBand = hasAnnot ? (compact ? 14 : 16) : 0;
  const headerH = M.dayBand + M.tickBand + annotBand;
  const bodyH = Math.max(model.rows.length, 1) * M.rowH;
  const scrollable = model.rows.length > maxVisibleRows;
  const maxBodyH = maxVisibleRows * M.rowH;

  const plotW = width === null ? 600 : Math.max(width - M.labelW - M.padR, 80);
  const { step, span } = chooseScale(model.spanRaw, plotW, M.minTickPx);
  const x = (t: number) => M.labelW + (t / span) * plotW;
  const plotRight = M.labelW + plotW;

  // ---- selection / emphasis
  const selected =
    selectedTask !== undefined
      ? selectedTask
      : innerSel !== undefined
        ? innerSel
        : highlightTasks && highlightTasks.length === 1
          ? highlightTasks[0]
          : null;
  const highlightSet = useMemo(() => new Set(highlightTasks ?? []), [highlightTasks]);
  const selTask = selected ? model.taskMap.get(selected) : undefined;
  const selEntry = selected ? model.byTask.get(selected) : undefined;
  const predIds = new Set(selTask?.after ?? []);
  const emphasis = highlightSet.size > 0 || selected !== null;

  const select = (id: string | null) => {
    if (selectedTask === undefined) setInnerSel(id);
    onSelectTask?.(id);
  };

  const showTip = (key: string, clientX: number, clientY: number) => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const r = wrap.getBoundingClientRect();
    setHover({ key, x: clientX - r.left, y: clientY - r.top, wrapW: r.width, wrapH: r.height });
  };

  // ---- axis pieces
  const ticks: number[] = [];
  for (let t = 0; t <= span + 1e-9; t += step) ticks.push(t);
  const dayPx = x(24) - x(0);
  const labelEvery = Math.max(1, Math.ceil((compact ? 30 : 46) / Math.max(dayPx, 1)));
  const days: { k: number; x0: number; x1: number; label: boolean }[] = [];
  for (let k = 0; k * 24 < span; k++) {
    const x0 = x(24 * k);
    const x1 = x(Math.min(24 * (k + 1), span));
    days.push({ k, x0, x1, label: k % labelEvery === 0 && x1 - x0 >= (compact ? 22 : 34) });
  }

  const deadlineX = x(horizonH);
  const deadlineLabel = `deadline ${fmtH(horizonH)} h`;
  const deadlineLabelW = textWidth(deadlineLabel, M.font - 1);
  const deadlineAnchorEnd = deadlineX - M.labelW > deadlineLabelW + 8;
  const deadlineBox: [number, number] | null = model.deadlineInPlot
    ? deadlineAnchorEnd
      ? [deadlineX - 4 - deadlineLabelW, deadlineX - 4]
      : [deadlineX + 4, deadlineX + 4 + deadlineLabelW]
    : null;

  type Annot = { t: number; label: string; line: string; text: string; group?: string };
  const annots: Annot[] = (markers ?? []).map((m) => ({
    t: m.t_h,
    label: m.label,
    line: m.color ?? INK,
    text: m.color ?? INK,
    group: m.group,
  }));
  if (nowH !== undefined) annots.push({ t: nowH, label: `now ${fmtH(nowH)} h`, line: NOW, text: NOW_TEXT });
  annots.sort((a, b) => a.t - b.t);
  // every marker gets its line; a group with more than three markers gets one label carrying
  // the count (at its first line), since a stack of "task overrun" labels reads as nothing
  const groupCounts = new Map<string, number>();
  for (const a of annots) if (a.group) groupCounts.set(a.group, (groupCounts.get(a.group) ?? 0) + 1);
  const labelled: Annot[] = [];
  const labelledGroups = new Set<string>();
  for (const a of annots) {
    const n = a.group ? (groupCounts.get(a.group) ?? 0) : 0;
    if (a.group && n > 3) {
      if (labelledGroups.has(a.group)) continue;
      labelledGroups.add(a.group);
      labelled.push({ ...a, label: `${a.group} ×${n}` });
    } else {
      labelled.push(a);
    }
  }
  // labels sit right of their line, are pushed right past the previous label and never past
  // the figure's right edge: there they hang left of their line when that is free, else they
  // are pinned to the edge
  const placedAnnots: (Annot & { lx: number; tx: number; anchor: "start" | "end" })[] = [];
  const rightEdge = (width ?? plotRight + M.padR) - 2;
  let lastRight = -Infinity;
  for (const a of labelled) {
    const lx = x(a.t);
    const w = textWidth(a.label, M.font - 1) + 6;
    let tx = Math.max(lx + 4, lastRight + 6);
    let anchor: "start" | "end" = "start";
    if (tx + w > rightEdge) {
      anchor = "end";
      if (lx - 4 - w >= lastRight + 6) {
        tx = lx - 4;
        lastRight = lx - 4;
      } else {
        tx = rightEdge;
        lastRight = rightEdge;
      }
    } else {
      lastRight = tx + w;
    }
    placedAnnots.push({ ...a, lx, tx, anchor });
  }

  // ---- tooltip
  const hoverEntry = hover ? model.entries.find((e) => entryKey(e) === hover.key) : undefined;
  const hoverTask = hoverEntry ? model.taskMap.get(hoverEntry.task) : undefined;
  const tipW = compact ? 230 : 280;
  const tipStyle = hover
    ? {
        left: Math.max(0, Math.min(hover.x + 14, hover.wrapW - tipW)),
        ...(hover.y > hover.wrapH * 0.6 ? { bottom: hover.wrapH - hover.y + 12 } : { top: hover.y + 14 }),
        width: tipW,
      }
    : undefined;

  const robotTypeOf = (id: string) => model.rows[model.rowIndex.get(id) ?? -1]?.type ?? null;

  const onBarKey = (e: KeyboardEvent<SVGGElement>, id: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      select(selected === id ? null : id);
    } else if (e.key === "Escape") {
      select(null);
      setHover(null);
      e.currentTarget.blur();
    }
  };

  const onBarFocus = (e: FocusEvent<SVGGElement>, key: string) => {
    const r = e.currentTarget.getBoundingClientRect();
    showTip(key, r.left + r.width / 2, r.bottom);
  };

  const onBarPointer = (e: ReactPointerEvent<SVGGElement>, key: string) => {
    showTip(key, e.clientX, e.clientY);
  };

  const visibleBodyH = scrollable ? maxBodyH : bodyH;

  return (
    <div
      ref={wrapRef}
      role="figure"
      aria-label={title ?? "Schedule Gantt chart"}
      className={`relative w-full min-w-0 text-slate-200 ${compact ? "text-[11px]" : "text-xs"}`}
      style={{ fontVariantNumeric: "tabular-nums" }}
    >
      {/* header: title + summary */}
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {title ? <h3 className={`font-medium ${compact ? "text-xs" : "text-sm"}`}>{title}</h3> : null}
        <div className="ml-auto flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-slate-400">
          <span>
            makespan <b className="font-semibold text-slate-200">{fmtH(model.makespan)} h</b>
          </span>
          <span>· {schedule.status}</span>
          <span>· {model.entries.length} {model.entries.length === 1 ? "task" : "tasks"}</span>
          {!model.deadlineInPlot ? (
            <span
              className="rounded border border-red-400/40 bg-red-400/10 px-1.5 py-px text-red-200"
              title="The deadline is more than three times the plotted span, so it is not drawn as a line."
            >
              deadline {fmtH(horizonH)} h · off chart
              {model.makespan > 0 ? ` (${(horizonH / model.makespan).toFixed(1)}× makespan)` : ""}
            </span>
          ) : null}
        </div>
      </div>

      {/* time axis (placeholder until the container has been measured) */}
      {width === null ? (
        <div style={{ height: headerH }} aria-hidden />
      ) : (
          <div className="overflow-hidden">
            <svg width={width} height={headerH} className="block" aria-hidden>
              {days.map((d) => (
                <g key={d.k}>
                  {d.k % 2 === 1 ? (
                    <rect x={d.x0} y={0} width={d.x1 - d.x0} height={headerH} fill={BAND} />
                  ) : null}
                  {d.label &&
                  !(deadlineBox && d.x0 + 4 < deadlineBox[1] && d.x0 + 4 + textWidth("Day 10", M.font - 1) > deadlineBox[0]) ? (
                    <text
                      x={d.x0 + 4}
                      y={M.dayBand / 2}
                      dominantBaseline="central"
                      fontSize={M.font - 1}
                      fill={MUTED}
                    >
                      {compact ? `D${d.k + 1}` : `Day ${d.k + 1}`}
                    </text>
                  ) : null}
                </g>
              ))}
              <line
                x1={M.labelW}
                x2={plotRight}
                y1={M.dayBand + M.tickBand - 0.5}
                y2={M.dayBand + M.tickBand - 0.5}
                stroke={GRID}
              />
              <text
                x={M.labelW - 6}
                y={M.dayBand + M.tickBand / 2}
                textAnchor="end"
                dominantBaseline="central"
                fontSize={M.font - 1}
                fill={FAINT}
              >
                h
              </text>
              {ticks.map((t) => (
                <g key={t}>
                  <line
                    x1={x(t)}
                    x2={x(t)}
                    y1={M.dayBand + M.tickBand - 4}
                    y2={M.dayBand + M.tickBand}
                    stroke={MUTED}
                  />
                  <text
                    x={x(t)}
                    y={M.dayBand + M.tickBand / 2 - 1}
                    textAnchor="middle"
                    dominantBaseline="central"
                    fontSize={M.font - 1}
                    fill={MUTED}
                  >
                    {t}
                  </text>
                </g>
              ))}
              {model.deadlineInPlot ? (
                <g>
                  <line
                    x1={deadlineX}
                    x2={deadlineX}
                    y1={0}
                    y2={headerH}
                    stroke={DEADLINE}
                    strokeDasharray="4 3"
                  />
                  <text
                    x={deadlineAnchorEnd ? deadlineX - 4 : deadlineX + 4}
                    y={M.dayBand / 2}
                    textAnchor={deadlineAnchorEnd ? "end" : "start"}
                    dominantBaseline="central"
                    fontSize={M.font - 1}
                    fill={DEADLINE_TEXT}
                  >
                    {deadlineLabel}
                  </text>
                </g>
              ) : null}
              {annots.map((a, i) => (
                <line
                  key={`${a.label}-${i}`}
                  x1={x(a.t)}
                  x2={x(a.t)}
                  y1={M.dayBand + M.tickBand}
                  y2={headerH}
                  stroke={a.line}
                  strokeWidth={a.line === NOW ? 1.5 : 1}
                />
              ))}
              {placedAnnots.map((a, i) => (
                <text
                  key={`${a.label}-${i}`}
                  x={a.tx}
                  y={M.dayBand + M.tickBand + annotBand / 2}
                  textAnchor={a.anchor}
                  dominantBaseline="central"
                  fontSize={M.font - 1}
                  fontWeight={600}
                  fill={a.text}
                >
                  {a.label}
                </text>
              ))}
            </svg>
          </div>
      )}

      {/* rows: this container is always mounted so the ResizeObserver can measure it */}
      <div
        ref={scrollRef}
        className={scrollable ? "overflow-y-auto overflow-x-hidden" : "overflow-hidden"}
        style={scrollable ? { maxHeight: maxBodyH } : undefined}
        onScroll={() => setHover(null)}
      >
        {width === null ? (
          <div style={{ height: visibleBodyH }} aria-hidden />
        ) : (
            <svg
              width={width}
              height={bodyH}
              className="block"
              role="group"
              aria-label={`${model.entries.length} scheduled tasks on ${model.rows.length} robots`}
              onKeyDown={(e) => {
                if (e.key === "Escape" && selected !== null) select(null);
              }}
            >
              <defs>
                <marker
                  id={arrowId}
                  viewBox="0 0 10 10"
                  refX="9"
                  refY="5"
                  markerWidth="7"
                  markerHeight="7"
                  orient="auto-start-reverse"
                >
                  <path d="M0,0 L10,5 L0,10 z" fill={INK} />
                </marker>
              </defs>

              {/* background: click clears the selection */}
              <rect
                x={0}
                y={0}
                width={width}
                height={bodyH}
                fill="transparent"
                onClick={() => {
                  if (selected !== null) select(null);
                }}
              />
              {days.map((d) =>
                d.k % 2 === 1 ? (
                  <rect
                    key={d.k}
                    x={d.x0}
                    y={0}
                    width={d.x1 - d.x0}
                    height={bodyH}
                    fill={BAND}
                    pointerEvents="none"
                  />
                ) : null,
              )}
              {ticks.map((t) => (
                <line key={t} x1={x(t)} x2={x(t)} y1={0} y2={bodyH} stroke={GRID} pointerEvents="none" />
              ))}
              <line x1={M.labelW - 0.5} x2={M.labelW - 0.5} y1={0} y2={bodyH} stroke={GRID} />

              {/* rows: label gutter + separators */}
              {model.rows.map((r, i) => {
                const cy = i * M.rowH + M.rowH / 2;
                const idle = !model.busyRobots.has(r.id);
                const failed = failedSet.has(r.id);
                return (
                  <g key={r.id} opacity={failed ? 0.55 : idle ? 0.6 : 1}>
                    <line
                      x1={0}
                      x2={width}
                      y1={(i + 1) * M.rowH - 0.5}
                      y2={(i + 1) * M.rowH - 0.5}
                      stroke={GRID}
                    />
                    <circle cx={10} cy={compact ? cy : cy - 5} r={compact ? 3.5 : 4} fill={robotTypeColor(r.type ?? "")} />
                    <text
                      x={19}
                      y={compact ? cy : cy - 5}
                      dominantBaseline="central"
                      fontSize={M.font}
                      fontWeight={600}
                      fill={failed ? DEADLINE_TEXT : INK}
                      textDecoration={failed ? "line-through" : undefined}
                    >
                      {r.id}
                    </text>
                    {!compact ? (
                      <text x={19} y={cy + 8} dominantBaseline="central" fontSize={9} fill={MUTED}>
                        {r.type ?? "unknown robot"}
                      </text>
                    ) : null}
                    {failed ? (
                      <text
                        x={M.labelW + 6}
                        y={cy}
                        dominantBaseline="central"
                        fontSize={M.font - 1}
                        fontWeight={600}
                        fill={DEADLINE_TEXT}
                      >
                        failed
                      </text>
                    ) : idle && !compact ? (
                      <text
                        x={M.labelW + 6}
                        y={cy}
                        dominantBaseline="central"
                        fontSize={M.font - 1}
                        fontStyle="italic"
                        fill={FAINT}
                      >
                        idle
                      </text>
                    ) : null}
                  </g>
                );
              })}

              {model.entries.length === 0 ? (
                <text
                  x={M.labelW + plotW / 2}
                  y={bodyH / 2}
                  textAnchor="middle"
                  dominantBaseline="central"
                  fontSize={M.font}
                  fontStyle="italic"
                  fill={MUTED}
                >
                  No scheduled tasks
                </text>
              ) : null}

              {/* bars */}
              {model.entries.map((e) => {
                const key = entryKey(e);
                const task = model.taskMap.get(e.task);
                const row = model.rowIndex.get(e.robot) ?? 0;
                const x0 = x(e.start_h) + 1;
                const w = Math.max(x(e.end_h) - 1 - x0, 2);
                const y0 = row * M.rowH + (M.rowH - M.barH) / 2;
                const fill = task ? zoneColor(task.zone, model.zoneIds) : MISSING;
                const fits = textWidth(e.task, M.font) + 10 <= w;
                const isSel = selected === e.task;
                const ringed = isSel || highlightSet.has(e.task);
                const focused = focusKey === key;
                const isPred = predIds.has(e.task);
                const opacity = !emphasis ? 1 : ringed || focused ? 1 : isPred ? 0.85 : 0.3;
                const hovered = hover?.key === key;
                const label = task
                  ? `${e.task} ${task.name}, ${e.robot}, ${fmtH(e.start_h)} to ${fmtH(e.end_h)} h`
                  : `${e.task} (not in the task graph), ${e.robot}, ${fmtH(e.start_h)} to ${fmtH(e.end_h)} h`;
                return (
                  <g
                    key={key}
                    role="button"
                    tabIndex={0}
                    aria-label={label}
                    aria-pressed={isSel}
                    className="cursor-pointer outline-none"
                    opacity={opacity}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      select(isSel ? null : e.task);
                    }}
                    onKeyDown={(ev) => onBarKey(ev, e.task)}
                    onPointerEnter={(ev) => onBarPointer(ev, key)}
                    onPointerMove={(ev) => onBarPointer(ev, key)}
                    onPointerLeave={() => setHover(null)}
                    onFocus={(ev) => {
                      setFocusKey(key);
                      onBarFocus(ev, key);
                    }}
                    onBlur={() => {
                      setFocusKey(null);
                      setHover(null);
                    }}
                  >
                    {/* hit target: full row height, at least 24 px wide */}
                    <rect
                      x={w >= 24 ? x0 : x0 + w / 2 - 12}
                      y={row * M.rowH}
                      width={Math.max(w, 24)}
                      height={M.rowH}
                      fill="transparent"
                    />
                    {ringed || focused ? (
                      // selection ring, or a dashed keyboard-focus ring until Enter selects
                      <rect
                        x={x0 - 2}
                        y={y0 - 2}
                        width={w + 4}
                        height={M.barH + 4}
                        rx={compact ? 3 : 4}
                        fill="none"
                        stroke={INK}
                        strokeWidth={2}
                        strokeDasharray={ringed ? undefined : "3 2"}
                      />
                    ) : null}
                    <rect
                      x={x0}
                      y={y0}
                      width={w}
                      height={M.barH}
                      rx={compact ? 2 : 3}
                      fill={fill}
                      stroke={hovered || isPred ? INK : "none"}
                      strokeWidth={1}
                      strokeOpacity={hovered ? 0.9 : 0.6}
                    />
                    {fits ? (
                      <text
                        x={x0 + w / 2}
                        y={y0 + M.barH / 2}
                        textAnchor="middle"
                        dominantBaseline="central"
                        fontSize={M.font}
                        fontWeight={600}
                        fill={inkFor(fill)}
                        pointerEvents="none"
                      >
                        {e.task}
                      </text>
                    ) : null}
                  </g>
                );
              })}

              {/* precedence arrows for the selected task */}
              {selEntry && selTask
                ? selTask.after.map((p) => {
                    const pe = model.byTask.get(p);
                    if (!pe) return null;
                    const r1 = model.rowIndex.get(pe.robot) ?? 0;
                    const r2 = model.rowIndex.get(selEntry.robot) ?? 0;
                    const d = arrowPath(
                      x(pe.end_h) - 1,
                      r1 * M.rowH + M.rowH / 2,
                      x(selEntry.start_h) + 1,
                      r2 * M.rowH + M.rowH / 2,
                      M.barH / 2 + 6,
                    );
                    return (
                      <path
                        key={p}
                        d={d}
                        fill="none"
                        stroke={INK}
                        strokeWidth={1.5}
                        strokeOpacity={0.95}
                        markerEnd={`url(#${arrowId})`}
                        pointerEvents="none"
                      />
                    );
                  })
                : null}

              {/* deadline / now / markers across the rows */}
              {model.deadlineInPlot ? (
                <line
                  x1={deadlineX}
                  x2={deadlineX}
                  y1={0}
                  y2={bodyH}
                  stroke={DEADLINE}
                  strokeDasharray="4 3"
                  pointerEvents="none"
                />
              ) : null}
              {annots.map((a, i) => (
                <line
                  key={`${a.label}-${i}`}
                  x1={x(a.t)}
                  x2={x(a.t)}
                  y1={0}
                  y2={bodyH}
                  stroke={a.line}
                  strokeWidth={a.line === NOW ? 1.5 : 1}
                  strokeOpacity={0.9}
                  pointerEvents="none"
                />
              ))}
            </svg>
        )}
      </div>

      {/* legend */}
      {showLegend ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-400">
          {model.legendZones.map((z) => (
            <span key={z} className="inline-flex items-center gap-1">
              <span
                className="inline-block h-2.5 w-2.5 rounded-sm"
                style={{ background: zoneColor(z, model.zoneIds) }}
              />
              zone {z}
            </span>
          ))}
          {model.hasMissing ? (
            <span className="inline-flex items-center gap-1">
              <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: MISSING }} />
              not in task graph
            </span>
          ) : null}
          {model.deadlineInPlot ? (
            <span className="ml-auto inline-flex items-center gap-1">
              <span className="inline-block w-4 border-t border-dashed" style={{ borderColor: DEADLINE }} />
              deadline
            </span>
          ) : null}
          {nowH !== undefined ? (
            <span className={`inline-flex items-center gap-1 ${model.deadlineInPlot ? "" : "ml-auto"}`}>
              <span className="inline-block w-4 border-t-2" style={{ borderColor: NOW }} />
              now
            </span>
          ) : null}
        </div>
      ) : null}

      {/* tooltip */}
      {hover && hoverEntry && tipStyle ? (
        <div
          role="tooltip"
          className="pointer-events-none absolute z-20 rounded-md border border-white/15 bg-[#111827]/95 px-2.5 py-2 text-[11px] leading-snug text-slate-200 shadow-lg shadow-black/40 backdrop-blur"
          style={tipStyle}
        >
          <div className={`flex items-baseline gap-1.5 ${compact ? "whitespace-nowrap" : ""}`}>
            <span className="font-mono font-semibold">{hoverEntry.task}</span>
            <span className={`font-medium ${compact ? "truncate" : ""}`}>
              {hoverTask ? hoverTask.name : "not in the task graph"}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 text-slate-400">
            <span className="inline-flex items-center gap-1">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ background: robotTypeColor(robotTypeOf(hoverEntry.robot) ?? "") }}
              />
              <span className="text-slate-200">{hoverEntry.robot}</span>
              {robotTypeOf(hoverEntry.robot) ? <span>{robotTypeOf(hoverEntry.robot)}</span> : null}
            </span>
            <span>
              <span className="text-slate-200">
                {fmtH(hoverEntry.start_h)}–{fmtH(hoverEntry.end_h)} h
              </span>{" "}
              ({fmtH(hoverEntry.end_h - hoverEntry.start_h)} h)
            </span>
          </div>
          {hoverTask ? (
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-slate-400">
              <span>
                <span className="text-slate-200">{fmtWh(hoverTask.energy_wh)}</span> Wh
              </span>
              <span className="inline-flex items-center gap-1">
                <span
                  className="inline-block h-2 w-2 rounded-sm"
                  style={{ background: zoneColor(hoverTask.zone, model.zoneIds) }}
                />
                zone <span className="text-slate-200">{hoverTask.zone}</span>
              </span>
              {hoverTask.duration_h !== hoverEntry.end_h - hoverEntry.start_h ? (
                <span>planned {fmtH(hoverTask.duration_h)} h</span>
              ) : null}
              <span className={compact ? "whitespace-nowrap" : ""}>
                after{" "}
                <span className="text-slate-200">
                  {hoverTask.after.length > 0 ? hoverTask.after.join(", ") : "none"}
                </span>
              </span>
              {hoverTask.structure && !compact ? <span>structure {hoverTask.structure}</span> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function entryKey(e: ScheduleEntry): string {
  return `${e.task}@${e.robot}@${e.start_h}-${e.end_h}`;
}
