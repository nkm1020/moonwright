"use client";
/**
 * 2D replay of a SimResult: the site grid with zones, depot and structures, robots from the
 * current (interpolated) frame with status ring, battery bar and task caption, transport
 * controls, a scrubber with event ticks and replan markers, and a live task readout.
 *
 * The map fits the action, not the whole grid: the zones, the depot and every place a robot
 * rests, with a long empty stretch between them drawn short and marked as a break (the demo's
 * zones sit at the north edge, 30 cells from the depot). Captions that would collide with
 * another robot's marker or label, or a zone's caption, are left out, the failed robots' last
 * (the tooltip and the task list still say them).
 *
 * The climax: around each event a banner in the event colour fills the band at the top of the
 * canvas, where the site / time line sits otherwise, so it never covers the map ("10.0 h · EX1
 * failed on T3 (78% done) → CP-SAT rescheduled 10 tasks in 0.01 s → verified, 72 h → 79 h"),
 * and autoplay holds for HOLD_MS (4 s) at every robot failure and replan marker (click the map
 * or press space to continue). Failed robots keep a red X at their last position with "failed
 * at 10 h"; robots queued behind a busy zone read "waiting · zone Z1".
 *
 * Playback state (time, hold, hover, cluster offsets) lives in refs and is drawn by
 * requestAnimationFrame; React state is only updated ~12 times per second for the readouts.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { EventKind, PlanResult, RobotStatus, SimEvent, SimFrame, SimResult, Task, Zone } from "@/lib/types";
import { EVENT_COLORS, STATUS_COLORS, robotTypeColor, zoneColor } from "@/lib/palette";
import { zoneOrder } from "@/lib/tasks";
import { eventKindLabel, eventPhrase, formatHours, replanOutcome, replanSegments, withAlpha } from "./EventList";

export interface SimReplayProps {
  sim: SimResult;
  plan: PlanResult;
  /** All tasks ever planned (see lib/tasks.ts allTasks). */
  tasks: Task[];
  autoPlay?: boolean;
  /** Additive: pass a NEW object to jump the playhead to t_h (the play/pause state is kept). */
  seekRequest?: { t_h: number } | null;
  /** Additive: playhead time while playing or scrubbing, throttled to ~12 calls per second. */
  onTimeChange?: (t_h: number) => void;
  /** Additive: cap on the canvas height in CSS px (default 640). */
  maxHeight?: number;
  /** Additive: autoplay holds HOLD_MS at each robot failure and replan marker; a click on the
   *  map or the space bar continues (default true). */
  holdOnEvents?: boolean;
  /** Additive: where the playhead starts when a new SimResult loads (default 0 h). */
  startAtH?: number;
  /** Additive: a heading rendered above the map, level with the top of `aside`. */
  heading?: ReactNode;
  /** Additive: content for a right-hand column (the event list): the map, transport and
   *  scrubber take the left 3/5 from xl, the aside, the live task list and the legend stack
   *  on the right, so the replay's climax has no empty column next to it. */
  aside?: ReactNode;
}

/** Playback speeds; 1x replays one simulated hour per real second. */
export const REPLAY_SPEEDS = [1, 4, 16] as const;
const HOURS_PER_SECOND = 1;
const PUBLISH_MS = 80;
/** Window around an event (simulated hours, either side) in which its banner shows and its
 *  ring keeps expanding. */
export const PULSE_H = 1.5;
const BANNER_FADE_H = 0.3; // banner opacity ramps over the last 0.3 h of the window
/** Real time the autoplay holds on an event when holdOnEvents is set: long enough to read the
 *  banner aloud at any speed (click the map or press space to continue earlier). */
export const HOLD_MS = 4000;
/** The band across the top of the canvas: the site / time line, and the event banner in its
 *  place while an event shows (two lines fit, three on a narrow canvas); a run without events
 *  gets the line alone. */
const HEADER_H = 48;
const HEADER_NARROW_H = 66;
const HEADER_QUIET_H = 30;
/** What the map fits (cells around each thing it must show): a zone's radius plus
 *  ZONE_MARGIN_CELLS (its robots sit at its centre), MARGIN_CELLS around the depot and every
 *  place a robot rests. An empty stretch between them longer than MIN_BREAK_CELLS is drawn
 *  BREAK_CELLS wide and marked as a break. */
const ZONE_MARGIN_CELLS = 1;
const MARGIN_CELLS = 2;
const MIN_BREAK_CELLS = 8;
const BREAK_CELLS = 2;
const R = 7; // marker radius
const RING = R + 4.5; // status ring radius
const PAD = 18;
/** Room left of the map area for the y tick labels. */
const AXIS_W = 28;

/** Canvas font families: system faces on the server and before the first draw, the console's
 *  Geist faces (next/font sets --font-geist-sans / --font-geist-mono on <html>) from the first
 *  draw on, so the map's labels match the page. A family list the canvas cannot parse is
 *  ignored (the fallback stays). */
let FONT_SANS = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
let FONT_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
let fontsResolved = false;
function resolveFonts() {
  if (fontsResolved || typeof document === "undefined") return;
  fontsResolved = true;
  const style = getComputedStyle(document.documentElement);
  const probe = document.createElement("canvas").getContext("2d");
  const accept = (family: string): boolean => {
    if (!family || !probe) return false;
    probe.font = "10px serif";
    probe.font = `10px ${family}`;
    return probe.font !== "10px serif";
  };
  const sans = style.getPropertyValue("--font-geist-sans").trim();
  const mono = style.getPropertyValue("--font-geist-mono").trim();
  if (accept(`${sans}, ${FONT_SANS}`)) FONT_SANS = `${sans}, ${FONT_SANS}`;
  if (accept(`${mono}, ${FONT_MONO}`)) FONT_MONO = `${mono}, ${FONT_MONO}`;
}

/** The canvas is a map of the lunar site: always a dark surface, whatever the page theme. */
const C = {
  bg: "#0b0f17",
  grid: "rgba(148,163,184,0.07)",
  gridMajor: "rgba(148,163,184,0.15)",
  border: "rgba(148,163,184,0.35)",
  text: "#e2e8f0",
  muted: "#94a3b8",
  faint: "#64748b",
  halo: "rgba(11,15,23,0.85)",
  track: "rgba(148,163,184,0.28)",
  tooltipBg: "rgba(15,23,42,0.94)",
  tooltipBorder: "rgba(148,163,184,0.35)",
  batteryOk: "#cbd5e1",
};

// ------------------------------------------------------------------ model & snapshot
interface RobotMeta {
  id: string;
  type: string;
  cap: number;
  index: number;
}

/** One event's banner, built once from the SimEvent and the ReplanRecord it triggered. */
interface Banner {
  t: number;
  kind: EventKind;
  color: string;
  /** "EX1 failed on T3 (78% done)" */
  lead: string;
  /** clauses drawn after arrows; the last one carries the verdict glyph when set */
  outcome: string[];
  verdict: "ok" | "fail" | null;
  /** tie-break when two events share a time: failures first */
  rank: number;
}

interface Model {
  siteName: string;
  grid: [number, number];
  zones: Zone[];
  zoneIds: string[];
  depot: [number, number] | null;
  structureLabels: Record<string, string[]>;
  robots: Record<string, RobotMeta>;
  robotList: RobotMeta[];
  taskMap: Record<string, Task>;
  total: number;
  frames: SimFrame[];
  events: SimEvent[];
  replanTimes: number[];
  banners: Banner[];
  /** sorted, distinct: robot failures and replan markers */
  holdTimes: number[];
  /** robot id -> hour it failed (the event's time; a frame's time when no event names it) */
  failedAt: Record<string, number>;
  /** robot id -> the failure event's detail (for the tooltip) */
  failedDetail: Record<string, string>;
  tMax: number;
  horizonH: number;
  /** what the map must show, per axis: [from, to] in cells (fitAxis merges them) */
  spans: { x: [number, number][]; y: [number, number][] };
}

const KIND_RANK: Record<EventKind, number> = { robot_failure: 3, battery_low: 2, task_overrun: 1 };

function sameEvent(a: SimEvent, b: SimEvent): boolean {
  return a.t_h === b.t_h && a.kind === b.kind && a.robot === b.robot && a.task === b.task;
}

function buildModel(sim: SimResult, plan: PlanResult, tasks: Task[]): Model {
  const req = plan.requirements;
  const structureLabels: Record<string, string[]> = {};
  for (const s of req.structures) {
    if (!s.zone) continue;
    (structureLabels[s.zone] ??= []).push(`${s.id} ${s.type.replace(/_/g, " ")}`);
  }
  const robotList: RobotMeta[] = req.robots.map((r, index) => ({
    id: r.id,
    type: r.type,
    cap: r.battery_wh,
    index,
  }));
  const robots = Object.fromEntries(robotList.map((r) => [r.id, r]));
  const frames = sim.frames;

  const failedAt: Record<string, number> = {};
  const failedDetail: Record<string, string> = {};
  for (const e of sim.events) {
    if (e.kind !== "robot_failure" || !e.robot || e.robot in failedAt) continue;
    failedAt[e.robot] = e.t_h;
    if (e.detail) failedDetail[e.robot] = e.detail;
  }
  for (const f of frames) {
    for (const r of f.robots) if (r.status === "failed" && !(r.id in failedAt)) failedAt[r.id] = f.t_h;
  }

  const banners: Banner[] = sim.events.map((e) => {
    const record = sim.replans.find((r) => sameEvent(r.event, e));
    const outcome = record ? replanOutcome(record) : null;
    return {
      t: e.t_h,
      kind: e.kind,
      color: EVENT_COLORS[e.kind] ?? "#94a3b8",
      lead: eventPhrase(e),
      outcome: outcome ? replanSegments(outcome) : [],
      verdict: outcome ? (outcome.ok ? "ok" : "fail") : null,
      rank: (KIND_RANK[e.kind] ?? 0) + (record ? 10 : 0),
    };
  });
  const holdTimes = Array.from(
    new Set([
      ...sim.events.filter((e) => e.kind === "robot_failure").map((e) => e.t_h),
      ...sim.replans.map((r) => r.t_h),
    ]),
  ).sort((a, b) => a - b);

  // the zones, the depot and wherever a robot stands still (a zone the simulator laid out for
  // a task included), never the road between them
  const spans: Model["spans"] = { x: [], y: [] };
  const show = (x: number, y: number, r: number) => {
    spans.x.push([x - r, x + r]);
    spans.y.push([y - r, y + r]);
  };
  for (const z of req.site.zones) show(z.center[0], z.center[1], z.radius + ZONE_MARGIN_CELLS);
  if (req.site.depot) show(req.site.depot[0], req.site.depot[1], MARGIN_CELLS);
  for (const f of frames) for (const r of f.robots) if (r.status !== "moving") show(r.pos[0], r.pos[1], MARGIN_CELLS);

  return {
    siteName: req.site.name,
    grid: req.site.grid,
    zones: req.site.zones,
    zoneIds: zoneOrder(plan, tasks),
    depot: req.site.depot,
    structureLabels,
    robots,
    robotList,
    taskMap: Object.fromEntries(tasks.map((t) => [t.id, t])),
    total: tasks.length,
    frames,
    events: sim.events,
    replanTimes: sim.replans.map((r) => r.t_h),
    banners,
    holdTimes,
    failedAt,
    failedDetail,
    tMax: frames.length ? frames[frames.length - 1].t_h : 0,
    horizonH: req.horizon_h,
    spans,
  };
}

interface SnapRobot {
  id: string;
  type: string;
  cap: number;
  pos: [number, number];
  battery: number;
  status: RobotStatus;
  task: string | null;
  /** "working on T3", "→ T4", "waiting · zone Z1", "waiting · T9 after T3", "failed at 10 h" */
  caption: string;
}

interface Snapshot {
  t: number;
  robots: SnapRobot[];
  done: string[];
  active: string[];
  progress: Record<string, number>;
}

const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
const clamp01 = (x: number) => Math.max(0, Math.min(1, x));

function captionFor(
  r: Omit<SnapRobot, "caption">,
  model: Model,
  workingIn: Map<string, string>,
  done: Set<string>,
): string {
  switch (r.status) {
    case "working":
      return r.task ? `working on ${r.task}` : "working";
    case "moving":
      return r.task ? `→ ${r.task}` : "moving";
    case "idle": {
      if (!r.task) return "idle";
      const task = model.taskMap[r.task];
      const holder = task ? workingIn.get(task.zone) : undefined;
      if (task && holder && holder !== r.id) return `waiting · zone ${task.zone}`;
      const pending = task?.after.filter((a) => !done.has(a)) ?? [];
      return pending.length ? `waiting · ${r.task} after ${pending[0]}` : `waiting · ${r.task}`;
    }
    case "failed": {
      const at = model.failedAt[r.id];
      return at !== undefined ? `failed at ${formatHours(at)}` : "failed";
    }
    case "done":
      return "done";
    default:
      return r.status;
  }
}

/** Frame state at time t: positions, batteries and progress interpolated between the thinned
 *  frames; statuses sample-and-hold, except that a robot whose position changes is "moving". */
function snapshotAt(model: Model, t: number): Snapshot | null {
  const fr = model.frames;
  if (fr.length === 0) return null;
  let lo = 0;
  let hi = fr.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (fr[mid].t_h <= t) lo = mid;
    else hi = mid - 1;
  }
  const fi = fr[lo];
  const fj = fr[Math.min(lo + 1, fr.length - 1)];
  const span = fj.t_h - fi.t_h;
  const alpha = span > 0 ? Math.max(0, Math.min(1, (t - fi.t_h) / span)) : 0;
  const next = new Map(fj.robots.map((r) => [r.id, r]));
  const raw: Omit<SnapRobot, "caption">[] = fi.robots.map((r) => {
    const meta = model.robots[r.id];
    const n = alpha > 0 ? next.get(r.id) : undefined;
    const base: Omit<SnapRobot, "caption"> = {
      id: r.id,
      type: meta?.type ?? "",
      cap: meta?.cap ?? r.battery_wh,
      pos: [r.pos[0], r.pos[1]],
      battery: r.battery_wh,
      status: r.status,
      task: r.current_task,
    };
    if (!n) return base;
    const moved = Math.hypot(n.pos[0] - r.pos[0], n.pos[1] - r.pos[1]) > 1e-6;
    const moving = moved && alpha < 1 && r.status !== "failed";
    return {
      ...base,
      pos: [lerp(r.pos[0], n.pos[0], alpha), lerp(r.pos[1], n.pos[1], alpha)],
      battery: lerp(r.battery_wh, n.battery_wh, alpha),
      status: moving ? "moving" : alpha >= 1 ? n.status : r.status,
      task: moving ? (n.current_task ?? r.current_task) : alpha >= 1 ? n.current_task : r.current_task,
    };
  });
  // zone -> robot working there (one robot per zone: the others queue outside as "idle")
  const workingIn = new Map<string, string>();
  for (const r of raw) {
    if (r.status !== "working" || !r.task) continue;
    const zone = model.taskMap[r.task]?.zone;
    if (zone) workingIn.set(zone, r.id);
  }
  const done = new Set(fi.done_tasks);
  const robots: SnapRobot[] = raw.map((r) => ({ ...r, caption: captionFor(r, model, workingIn, done) }));
  const progress: Record<string, number> = {};
  for (const k of new Set([...Object.keys(fi.progress), ...Object.keys(fj.progress)])) {
    const a = fi.progress[k] ?? 0;
    const b = fj.progress[k] ?? a;
    progress[k] = lerp(a, b, alpha);
  }
  return { t, robots, done: fi.done_tasks, active: fi.active_tasks, progress };
}

// ------------------------------------------------------------------ geometry & marks
/** One axis of the map: which cells it shows and which empty stretches it draws short. */
interface Axis {
  /** the cell at the map area's left (x) or bottom (y) edge */
  lo: number;
  /** the map area's length in drawn cells (a break counts BREAK_CELLS) */
  len: number;
  /** stretches drawn BREAK_CELLS wide: [from, to] in cells, ascending */
  breaks: [number, number][];
  /** the grid's extent on this axis */
  size: number;
}

interface Geo {
  w: number;
  h: number;
  /** px per drawn cell */
  s: number;
  /** the map area's left and top edges in px (the header band is above it) */
  ox: number;
  oy: number;
  /** height of the header band */
  top: number;
  x: Axis;
  y: Axis;
}

/** What one axis must show (the spans merged) and the empty stretches between them longer than
 *  MIN_BREAK_CELLS; the whole grid when nothing is known. */
function fitAxis(spans: [number, number][], size: number): { from: number; to: number; breaks: [number, number][] } {
  const merged: [number, number][] = [];
  for (const [a, b] of [...spans].sort((p, q) => p[0] - q[0])) {
    const last = merged[merged.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else merged.push([a, b]);
  }
  if (merged.length === 0) return { from: 0, to: size, breaks: [] };
  const breaks: [number, number][] = [];
  for (let i = 1; i < merged.length; i += 1) {
    const from = merged[i - 1][1];
    const to = merged[i][0];
    if (to - from > MIN_BREAK_CELLS) breaks.push([from, to]);
  }
  return { from: merged[0][0], to: merged[merged.length - 1][1], breaks };
}

/** An axis's length in drawn cells (a break counts BREAK_CELLS). */
function drawnLength(f: ReturnType<typeof fitAxis>): number {
  return Math.max(1, f.to - f.from - f.breaks.reduce((acc, [a, b]) => acc + (b - a - BREAK_CELLS), 0));
}

/** Width over height of what the map shows, for the canvas box: a run whose work stands in a
 *  column gets a taller canvas (up to the box's max height) instead of a smaller scale. Kept
 *  between 1:2 and 5:2. */
function mapAspect(model: Model): number {
  const x = drawnLength(fitAxis(model.spans.x, model.grid[0]));
  const y = drawnLength(fitAxis(model.spans.y, model.grid[1]));
  return Math.min(2.5, Math.max(0.5, x / y));
}

/** The map area under the header band, scaled to what the run uses (fitAxis on both axes, one
 *  scale for both so zones stay round) and centred on it; the room left over shows more of the
 *  site around it. */
function geometry(w: number, h: number, model: Model): Geo {
  const top = model.banners.length === 0 ? HEADER_QUIET_H : w < COMPACT_W ? HEADER_NARROW_H : HEADER_H;
  const availW = Math.max(1, w - AXIS_W - PAD);
  const availH = Math.max(1, h - top - 2 * PAD);
  const fx = fitAxis(model.spans.x, model.grid[0]);
  const fy = fitAxis(model.spans.y, model.grid[1]);
  const lenX = drawnLength(fx);
  const lenY = drawnLength(fy);
  const s = Math.max(0.5, Math.min(availW / lenX, availH / lenY));
  const axis = (f: ReturnType<typeof fitAxis>, len: number, avail: number, size: number): Axis => ({
    lo: f.from - (avail / s - len) / 2,
    len: avail / s,
    breaks: f.breaks,
    size,
  });
  return {
    w,
    h,
    s,
    ox: AXIS_W,
    oy: top + PAD,
    top,
    x: axis(fx, lenX, availW, model.grid[0]),
    y: axis(fy, lenY, availH, model.grid[1]),
  };
}

/** Drawn cells from the axis's first cell to `v`: a break counts BREAK_CELLS however long. */
function drawnCells(a: Axis, v: number): number {
  let u = v - a.lo;
  for (const [from, to] of a.breaks) {
    if (v <= from) break;
    u -= (Math.min(v, to) - from) * (1 - BREAK_CELLS / (to - from));
  }
  return u;
}

/** Whether a cell line falls inside a break (it is not drawn there). */
function inBreak(a: Axis, v: number): boolean {
  return a.breaks.some(([from, to]) => v > from && v < to);
}

/** Grid y grows northward (the depot sits on the south edge), so the canvas flips it. */
function toPx(g: Geo, x: number, y: number): [number, number] {
  return [g.ox + drawnCells(g.x, x) * g.s, g.oy + (g.y.len - drawnCells(g.y, y)) * g.s];
}

/** Marker outline by robot type, as polygon points around (0,0); null means a circle. */
export function markerPolygon(type: string, r: number): [number, number][] | null {
  const regular = (n: number, radius: number, rot = -Math.PI / 2): [number, number][] =>
    Array.from({ length: n }, (_, i) => {
      const a = rot + (2 * Math.PI * i) / n;
      return [Math.cos(a) * radius, Math.sin(a) * radius];
    });
  switch (type) {
    case "excavator":
      return [
        [-r, -r],
        [r, -r],
        [r, r],
        [-r, r],
      ];
    case "hauler":
      return regular(3, r * 1.25);
    case "compactor":
      return regular(4, r * 1.25);
    case "printer":
      return regular(6, r * 1.12);
    case "crane":
      return regular(5, r * 1.15);
    default:
      return null;
  }
}

function markerPath(ctx: CanvasRenderingContext2D, type: string, cx: number, cy: number, r: number) {
  const poly = markerPolygon(type, r);
  ctx.beginPath();
  if (!poly) {
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    return;
  }
  poly.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(cx + x, cy + y) : ctx.lineTo(cx + x, cy + y)));
  ctx.closePath();
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.arcTo(x + w, y, x + w, y + rr, rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.arcTo(x + w, y + h, x + w - rr, y + h, rr);
  ctx.lineTo(x + rr, y + h);
  ctx.arcTo(x, y + h, x, y + h - rr, rr);
  ctx.lineTo(x, y + rr);
  ctx.arcTo(x, y, x + rr, y, rr);
  ctx.closePath();
}

function label(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  font: string,
  color: string,
  align: CanvasTextAlign = "center",
  baseline: CanvasTextBaseline = "alphabetic",
) {
  ctx.font = font;
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  ctx.lineWidth = 3;
  ctx.lineJoin = "round";
  ctx.strokeStyle = C.halo;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

/** Text in a fill: no halo, left aligned at (x, y); returns the advance. */
function plainText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, font: string, color: string): number {
  ctx.font = font;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
  return ctx.measureText(text).width;
}

function measure(ctx: CanvasRenderingContext2D, text: string, font: string): number {
  ctx.font = font;
  return ctx.measureText(text).width;
}

/** Ellipsise `text` (in the context's current font) to fit maxW. */
function fitText(ctx: CanvasRenderingContext2D, text: string, font: string, maxW: number): string {
  ctx.font = font;
  if (ctx.measureText(text).width <= maxW) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(text.slice(0, mid).trimEnd() + "…").width <= maxW) lo = mid;
    else hi = mid - 1;
  }
  return lo === 0 ? "" : text.slice(0, lo).trimEnd() + "…";
}

/** WCAG relative luminance of a 6-digit hex colour (0 = black, 1 = white). */
function luminance(hex: string): number {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return 0;
  const n = parseInt(m[1], 16);
  const ch = (v: number) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * ch((n >> 16) & 255) + 0.7152 * ch((n >> 8) & 255) + 0.0722 * ch(n & 255);
}

/** Battery bar fill: neutral when the charge is fine, amber under 20%, red under 10%. */
function batteryColor(frac: number): string {
  return frac < 0.1 ? STATUS_COLORS.failed : frac < 0.2 ? EVENT_COLORS.battery_low : C.batteryOk;
}

interface Placed extends SnapRobot {
  x: number;
  y: number;
  /** the robot's own map point, before the cluster spread it on a ring */
  bx: number;
  by: number;
  dir: [number, number];
  /** the cluster it was placed in (robots sharing a spot) */
  cluster: string;
}

// ------------------------------------------------------------------ label layout
interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

function overlapArea(a: Box, b: Box): number {
  return Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
}

/** The box of `text` at (x, baseline y) in a `size` px font, placed as `align` places it. */
function textBox(
  ctx: CanvasRenderingContext2D,
  text: string,
  font: string,
  size: number,
  x: number,
  y: number,
  align: CanvasTextAlign,
): Box {
  const w = measure(ctx, text, font);
  const x0 = align === "left" ? x : align === "right" ? x - w : x - w / 2;
  return { x0, y0: y - size, x1: x0 + w, y1: y + size * 0.3 };
}

/** A box slid inside `bounds` (as far as it fits). */
function slideInto(b: Box, bounds: Box): Box {
  const dx = b.x0 < bounds.x0 ? bounds.x0 - b.x0 : b.x1 > bounds.x1 ? Math.max(bounds.x0 - b.x0, bounds.x1 - b.x1) : 0;
  const dy = b.y0 < bounds.y0 ? bounds.y0 - b.y0 : b.y1 > bounds.y1 ? Math.max(bounds.y0 - b.y0, bounds.y1 - b.y1) : 0;
  return { x0: b.x0 + dx, y0: b.y0 + dy, x1: b.x1 + dx, y1: b.y1 + dy };
}

/** Where the canvas has room for labels: all of it under the header band, 4 px in. */
function labelBounds(geo: Geo): Box {
  return { x0: 4, y0: geo.top + 4, x1: geo.w - 4, y1: geo.h - 4 };
}

/** The first spot (a box) that overlaps nothing in `taken`, else the one that overlaps it least,
 *  each slid inside `bounds` first. */
function bestSpot(spots: Box[], taken: Box[], bounds: Box): Box {
  let best = slideInto(spots[0], bounds);
  let bestCost = Infinity;
  for (const spot of spots) {
    const b = slideInto(spot, bounds);
    const cost = taken.reduce((acc, t) => acc + overlapArea(b, t), 0);
    if (cost === 0) return b;
    if (cost < bestCost) {
      best = b;
      bestCost = cost;
    }
  }
  return best;
}

/** A zone's caption ("Z1 S1 landing pad"): the id at (x, baseline y), the structures after it. */
interface ZoneCaption {
  id: string;
  idW: number;
  extra: string;
  x: number;
  y: number;
  box: Box;
}

/** Each zone's caption beside its circle: above it, else below, left or right of it, the first
 *  spot clear of the other zones' circles and the captions placed before it. It depends on the
 *  geometry only, so it holds still while the robots move. */
function zoneCaptions(ctx: CanvasRenderingContext2D, geo: Geo, model: Model): ZoneCaption[] {
  const idFont = `600 12px ${FONT_SANS}`;
  const extraFont = `11px ${FONT_SANS}`;
  const bounds = labelBounds(geo);
  const circles = model.zones.map((z) => {
    const [cx, cy] = toPx(geo, z.center[0], z.center[1]);
    const r = z.radius * geo.s;
    return { id: z.id, cx, cy, r, box: { x0: cx - r, y0: cy - r, x1: cx + r, y1: cy + r } };
  });
  const out: ZoneCaption[] = [];
  for (const c of circles) {
    let extra = (model.structureLabels[c.id] ?? []).join(", ");
    if (extra.length > 30) extra = extra.slice(0, 29) + "…";
    const idW = measure(ctx, c.id, idFont);
    const width = idW + (extra ? measure(ctx, extra, extraFont) + 6 : 0);
    // the box of a caption whose baseline starts at (x, y): 12 px of text above it, 4 below
    const at = (x: number, y: number): Box => ({ x0: x - 2, y0: y - 12, x1: x + width + 2, y1: y + 4 });
    const spots = [
      at(c.cx - width / 2, c.cy - c.r - 7),
      at(c.cx - width / 2, c.cy + c.r + 15),
      at(c.cx - c.r - 8 - width, c.cy + 4),
      at(c.cx + c.r + 8, c.cy + 4),
    ];
    const taken = [...circles.filter((o) => o.id !== c.id).map((o) => o.box), ...out.map((o) => o.box)];
    const box = bestSpot(spots, taken, bounds);
    out.push({ id: c.id, idW, extra, x: box.x0 + 2, y: box.y0 + 12, box });
  }
  return out;
}

const idTagFont = () => `600 11px ${FONT_MONO}`;

/** A robot's id beside its marker, on the outside of its cluster; a robot alone has none (its
 *  callout row sits right next to it). */
interface IdTag {
  id: string;
  x: number;
  y: number;
  align: CanvasTextAlign;
  box: Box;
}

function idTag(ctx: CanvasRenderingContext2D, p: Placed): IdTag {
  const [dx, dy] = p.dir;
  const align: CanvasTextAlign = Math.abs(dx) > 0.3 ? (dx > 0 ? "left" : "right") : "center";
  const x = p.x + dx * (RING + 3);
  const y = dy > 0.3 ? p.y + RING + 13 : dy < -0.3 ? p.y - RING - 4 : p.y + 4;
  return { id: p.id, x, y, align, box: textBox(ctx, p.id, idTagFont(), 11, x, y, align) };
}

/** The robots that share a spot (placeRobots' clusters, a robot alone included) in one callout
 *  beside it: a row per robot in fleet order with its id, its battery bar and what it is doing.
 *  On a narrow canvas (compact) the rows keep the id and the bar, and the task list under the
 *  map says the rest. */
interface Callout {
  rows: Placed[];
  box: Box;
  idW: number;
  compact: boolean;
}

const CALLOUT_ROW_H = 15;
const CALLOUT_PAD = 6;
const CALLOUT_BAR_W = 22;
/** Canvas width under which callouts leave the captions out. */
const COMPACT_W = 520;

/** A callout row's words: what the robot is doing, after its battery share when that is low
 *  ("1% battery · done", so the figure never reads as progress). */
function rowText(p: Placed): string {
  const frac = p.cap > 0 ? p.battery / p.cap : 0;
  return p.status !== "failed" && frac < 0.2 ? `${Math.round(frac * 100)}% battery · ${p.caption}` : p.caption;
}

/** Lower first: whose cluster gets its callout placed first. */
const CALLOUT_RANK: Record<RobotStatus, number> = { failed: 0, working: 1, moving: 2, idle: 3, done: 4 };

/** The id tags and callouts of a frame, laid out so they keep clear of each other where the
 *  canvas has room: the zone captions, the markers and the id tags are placed first, then each
 *  cluster's callout (failed robots' clusters first) right of the cluster and its tags, else
 *  left, below or above them, the first spot clear of everything placed, else the least
 *  crowded one. */
function layoutLabels(
  ctx: CanvasRenderingContext2D,
  geo: Geo,
  model: Model,
  placed: Placed[],
  zones: ZoneCaption[],
): { tags: IdTag[]; callouts: Callout[] } {
  const bounds = labelBounds(geo);
  const compact = geo.w < COMPACT_W;
  const clusters = new Map<string, Placed[]>();
  for (const p of placed) (clusters.get(p.cluster) ?? clusters.set(p.cluster, []).get(p.cluster)!).push(p);
  const tags = placed.filter((p) => (clusters.get(p.cluster)?.length ?? 1) > 1).map((p) => idTag(ctx, p));
  const taken: Box[] = [
    ...zones.map((z) => z.box),
    ...placed.map((p) => ({ x0: p.x - RING - 1, y0: p.y - RING - 1, x1: p.x + RING + 1, y1: p.y + RING + 1 })),
    ...tags.map((t) => t.box),
  ];
  if (model.depot) {
    const [dx, dy] = toPx(geo, model.depot[0], model.depot[1]);
    taken.push({ x0: dx - 18, y0: dy - 32, x1: dx + 18, y1: dy + 18 }); // the pad and its "Depot" label
  }
  const rank = (rows: Placed[]) => Math.min(...rows.map((p) => CALLOUT_RANK[p.status]));
  const first = (rows: Placed[]) => Math.min(...rows.map((p) => model.robots[p.id]?.index ?? 99));
  const order = [...clusters.values()].sort((a, b) => rank(a) - rank(b) || first(a) - first(b));
  const idFont = `600 12px ${FONT_MONO}`;
  const capFont = `11px ${FONT_SANS}`;
  const callouts: Callout[] = [];
  for (const rows of order) {
    const idW = Math.max(...rows.map((p) => measure(ctx, p.id, idFont)));
    const capW = compact ? 0 : Math.max(0, ...rows.map((p) => measure(ctx, rowText(p), capFont)));
    const w = CALLOUT_PAD + idW + 6 + CALLOUT_BAR_W + (capW > 0 ? 6 + capW : 0) + CALLOUT_PAD;
    const h = rows.length * CALLOUT_ROW_H + 8;
    const cx = rows.reduce((acc, p) => acc + p.bx, 0) / rows.length;
    const cy = rows.reduce((acc, p) => acc + p.by, 0) / rows.length;
    // the cluster as drawn: its markers and its id tags
    const members = new Set(rows.map((p) => p.id));
    const parts = [
      ...rows.map((p) => ({ x0: p.x - RING, y0: p.y - RING, x1: p.x + RING, y1: p.y + RING })),
      ...tags.filter((t) => members.has(t.id)).map((t) => t.box),
    ];
    const ext: Box = {
      x0: Math.min(...parts.map((b) => b.x0)),
      y0: Math.min(...parts.map((b) => b.y0)),
      x1: Math.max(...parts.map((b) => b.x1)),
      y1: Math.max(...parts.map((b) => b.y1)),
    };
    const at = (x: number, y: number): Box => ({ x0: x, y0: y, x1: x + w, y1: y + h });
    const box = bestSpot(
      [at(ext.x1 + 6, cy - h / 2), at(ext.x0 - 6 - w, cy - h / 2), at(cx - w / 2, ext.y1 + 4), at(cx - w / 2, ext.y0 - 4 - h)],
      taken,
      bounds,
    );
    taken.push(box);
    callouts.push({ rows, box, idW, compact });
  }
  return { tags, callouts };
}

/** Robots sharing a cell are spread on a small ring (offsets eased between draws). */
function placeRobots(
  robots: SnapRobot[],
  model: Model,
  geo: Geo,
  offsets: Map<string, { x: number; y: number }>,
  ease: number,
): Placed[] {
  const groups = new Map<string, SnapRobot[]>();
  for (const r of robots) {
    const key = `${Math.round(r.pos[0] * 2)},${Math.round(r.pos[1] * 2)}`;
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(r);
  }
  const out: Placed[] = [];
  for (const [key, group] of groups) {
    group.sort((a, b) => (model.robots[a.id]?.index ?? 99) - (model.robots[b.id]?.index ?? 99));
    const n = group.length;
    const radius = n === 1 ? 0 : n <= 2 ? 14 : n <= 4 ? 19 : 12 + 2.7 * n;
    group.forEach((r, k) => {
      const a = -Math.PI / 2 + (2 * Math.PI * k) / n;
      const target = n === 1 ? { x: 0, y: 0 } : { x: Math.cos(a) * radius, y: Math.sin(a) * radius };
      const cur = offsets.get(r.id) ?? { ...target };
      cur.x += (target.x - cur.x) * ease;
      cur.y += (target.y - cur.y) * ease;
      offsets.set(r.id, cur);
      const [px, py] = toPx(geo, r.pos[0], r.pos[1]);
      const dir: [number, number] = n === 1 ? [0, 1] : [Math.cos(a), Math.sin(a)];
      out.push({ ...r, x: px + cur.x, y: py + cur.y, bx: px, by: py, dir, cluster: key });
    });
  }
  return out;
}

interface DrawInput {
  ctx: CanvasRenderingContext2D;
  geo: Geo;
  model: Model;
  snap: Snapshot;
  placed: Placed[];
  hover: string | null;
  pointer: { x: number; y: number } | null;
  /** fraction of the hold still to run while autoplay waits on an event, else null */
  hold: { frac: number } | null;
  /** the zone captions (zoneCaptions) */
  zoneCaps: ZoneCaption[];
  /** the robots' id tags (layoutLabels): the depot's caption gives way to them */
  tags: IdTag[];
}

/** A run of short teeth along a straight edge (a break's mark): horizontal at y, or vertical at
 *  x, between `from` and `to`. */
function zigzag(ctx: CanvasRenderingContext2D, horizontal: boolean, at: number, from: number, to: number) {
  ctx.beginPath();
  for (let t = from, k = 0; t <= to + 0.01; t += 4, k += 1) {
    const off = k % 2 === 0 ? -2 : 2;
    if (horizontal) {
      if (k === 0) ctx.moveTo(t, at + off);
      else ctx.lineTo(t, at + off);
    } else if (k === 0) ctx.moveTo(at + off, t);
    else ctx.lineTo(at + off, t);
  }
  ctx.stroke();
}

/** The breaks: each stretch drawn BREAK_CELLS wide on the map's surface with no grid lines,
 *  zigzag edges, and the cells it stands for ("21 cells"). Called inside the map area's clip. */
function drawBreaks(ctx: CanvasRenderingContext2D, geo: Geo, area: Box) {
  const font = `11px ${FONT_SANS}`;
  ctx.lineWidth = 1;
  ctx.strokeStyle = C.border;
  for (const [from, to] of geo.y.breaks) {
    const south = toPx(geo, 0, from)[1];
    const north = toPx(geo, 0, to)[1];
    ctx.fillStyle = C.bg;
    ctx.fillRect(area.x0, north, area.x1 - area.x0, south - north);
    zigzag(ctx, true, north, area.x0, area.x1);
    zigzag(ctx, true, south, area.x0, area.x1);
    label(ctx, `${Math.round(to - from)} cells`, area.x0 + 8, (north + south) / 2 + 4, font, C.faint, "left");
  }
  for (const [from, to] of geo.x.breaks) {
    const west = toPx(geo, from, 0)[0];
    const east = toPx(geo, to, 0)[0];
    ctx.fillStyle = C.bg;
    ctx.fillRect(west, area.y0, east - west, area.y1 - area.y0);
    zigzag(ctx, false, west, area.y0, area.y1);
    zigzag(ctx, false, east, area.y0, area.y1);
    ctx.save();
    ctx.translate((west + east) / 2 + 4, area.y1 - 8);
    ctx.rotate(-Math.PI / 2);
    label(ctx, `${Math.round(to - from)} cells`, 0, 0, font, C.faint, "left");
    ctx.restore();
  }
}

function drawSite({ ctx, geo, model, snap, placed, zoneCaps, tags }: DrawInput) {
  const { w, h, s } = geo;
  const [gx, gy] = model.grid;
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, w, h);

  // the map area under the header band: grid lines, the site's edge and the breaks stay in it
  const area: Box = { x0: geo.ox, y0: geo.oy, x1: geo.ox + geo.x.len * s, y1: geo.oy + geo.y.len * s };
  const [sx0, syTop] = toPx(geo, 0, gy);
  const [sx1, syBot] = toPx(geo, gx, 0);
  ctx.save();
  ctx.beginPath();
  ctx.rect(area.x0, area.y0, area.x1 - area.x0, area.y1 - area.y0);
  ctx.clip();
  // grid: hairlines every cell (when there is room) and every 5 cells, none inside a break
  ctx.lineWidth = 1;
  for (let x = 0; x <= gx; x++) {
    const major = x % 5 === 0;
    if ((!major && s < 9) || inBreak(geo.x, x)) continue;
    ctx.strokeStyle = major ? C.gridMajor : C.grid;
    const px = Math.round(toPx(geo, x, 0)[0]) + 0.5;
    ctx.beginPath();
    ctx.moveTo(px, syTop);
    ctx.lineTo(px, syBot);
    ctx.stroke();
  }
  for (let y = 0; y <= gy; y++) {
    const major = y % 5 === 0;
    if ((!major && s < 9) || inBreak(geo.y, y)) continue;
    ctx.strokeStyle = major ? C.gridMajor : C.grid;
    const py = Math.round(toPx(geo, 0, y)[1]) + 0.5;
    ctx.beginPath();
    ctx.moveTo(sx0, py);
    ctx.lineTo(sx1, py);
    ctx.stroke();
  }
  ctx.strokeStyle = C.border;
  ctx.strokeRect(Math.round(sx0) + 0.5, Math.round(syTop) + 0.5, Math.round(sx1 - sx0), Math.round(syBot - syTop));
  drawBreaks(ctx, geo, area);
  ctx.restore();
  // tick labels every 10 cells, where the map shows the line
  for (let x = 0; x <= gx; x += 10) {
    const px = toPx(geo, x, 0)[0];
    if (inBreak(geo.x, x) || px < area.x0 - 1 || px > area.x1 + 1) continue;
    label(ctx, String(x), px, area.y1 + 13, `11px ${FONT_MONO}`, C.faint, "center");
  }
  for (let y = 0; y <= gy; y += 10) {
    const py = toPx(geo, 0, y)[1];
    if (inBreak(geo.y, y) || py < area.y0 - 1 || py > area.y1 + 1) continue;
    label(ctx, String(y), area.x0 - 5, py + 4, `11px ${FONT_MONO}`, C.faint, "right");
  }

  // zones
  for (const z of model.zones) {
    const [cx, cy] = toPx(geo, z.center[0], z.center[1]);
    const r = z.radius * s;
    const color = zoneColor(z.id, model.zoneIds);
    const busy = snap.robots.some((rb) => rb.status === "working" && rb.task && model.taskMap[rb.task]?.zone === z.id);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = withAlpha(color, busy ? 0.2 : 0.11);
    ctx.fill();
    ctx.lineWidth = busy ? 2 : 1.25;
    ctx.strokeStyle = withAlpha(color, busy ? 0.95 : 0.5);
    ctx.stroke();
  }
  // captions beside the circles, where zoneCaptions found room
  for (const c of zoneCaps) {
    label(ctx, c.id, c.x, c.y, `600 12px ${FONT_SANS}`, C.text, "left");
    if (c.extra) label(ctx, c.extra, c.x + c.idW + 6, c.y, `11px ${FONT_SANS}`, C.muted, "left");
  }

  // depot: a dashed pad the parked robots sit on
  if (model.depot) {
    const [dx, dy] = toPx(geo, model.depot[0], model.depot[1]);
    const parked = snap.robots.filter(
      (rb) => Math.hypot(rb.pos[0] - model.depot![0], rb.pos[1] - model.depot![1]) < 1.5,
    ).length;
    const half = parked > 1 ? 14 + 2.7 * parked + RING : 16;
    ctx.save();
    ctx.setLineDash([3, 3]);
    roundRect(ctx, dx - half, dy - half, half * 2, half * 2, 5);
    ctx.lineWidth = 1;
    ctx.strokeStyle = withAlpha("#cbd5e1", 0.55);
    ctx.stroke();
    ctx.restore();
    // the caption gives way to a robot's id tag drawn over it (robots parked in a cluster on
    // the pad, a failed one among them), which would otherwise garble both
    const font = `11px ${FONT_SANS}`;
    const caption = textBox(ctx, "Depot", font, 11, dx, dy - half - 5, "center");
    if (!tags.some((t) => overlapArea(t.box, caption) > 0)) label(ctx, "Depot", dx, dy - half - 5, font, C.muted);
  }

  // heading lines for moving robots (under the markers)
  for (const p of placed) {
    if (p.status !== "moving" || !p.task) continue;
    const zoneId = model.taskMap[p.task]?.zone;
    const zone = model.zones.find((z) => z.id === zoneId);
    if (!zone) continue;
    const [zx, zy] = toPx(geo, zone.center[0], zone.center[1]);
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = withAlpha(robotTypeColor(p.type), 0.5);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(zx, zy);
    ctx.stroke();
    ctx.restore();
  }

  // event pulses: a ring expanding from the robot for PULSE_H after the event
  for (const e of model.events) {
    const age = snap.t - e.t_h;
    if (age < 0 || age > PULSE_H || !e.robot) continue;
    const p = placed.find((q) => q.id === e.robot);
    if (!p) continue;
    const phase = age / PULSE_H;
    ctx.beginPath();
    ctx.arc(p.x, p.y, RING + 4 + 34 * phase, 0, Math.PI * 2);
    ctx.lineWidth = 2;
    ctx.strokeStyle = withAlpha(EVENT_COLORS[e.kind] ?? "#94a3b8", (1 - phase) * 0.9);
    ctx.stroke();
  }
}

/** A robot's marker: type shape in the type colour, status ring, the red X once it failed. Its id
 *  and the rest are its id tag and its callout row (layoutLabels). */
function drawRobot(ctx: CanvasRenderingContext2D, p: Placed, hovered: boolean) {
  const color = robotTypeColor(p.type);
  const failed = p.status === "failed";
  const dim = failed ? 0.3 : p.status === "done" ? 0.6 : 1;

  if (hovered) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, RING + 4, 0, Math.PI * 2);
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "rgba(255,255,255,0.6)";
    ctx.stroke();
  }
  // status ring (a 2px surface gap separates it from the marker)
  ctx.beginPath();
  ctx.arc(p.x, p.y, RING, 0, Math.PI * 2);
  ctx.lineWidth = 2;
  ctx.strokeStyle = withAlpha(STATUS_COLORS[p.status] ?? "#94a3b8", p.status === "done" ? 0.7 : 1);
  ctx.stroke();
  // marker
  markerPath(ctx, p.type, p.x, p.y, R);
  ctx.fillStyle = withAlpha(color, dim);
  ctx.fill();
  if (failed) {
    // red X over the last position, with a surface halo so it reads on any zone fill
    const k = R + 3;
    ctx.lineCap = "round";
    for (const [width, stroke] of [
      [5, C.halo],
      [2.5, STATUS_COLORS.failed],
    ] as const) {
      ctx.lineWidth = width;
      ctx.strokeStyle = stroke;
      ctx.beginPath();
      ctx.moveTo(p.x - k, p.y - k);
      ctx.lineTo(p.x + k, p.y + k);
      ctx.moveTo(p.x + k, p.y - k);
      ctx.lineTo(p.x - k, p.y + k);
      ctx.stroke();
    }
    ctx.lineCap = "butt";
  }
}

function drawIdTag(ctx: CanvasRenderingContext2D, tag: IdTag, hovered: boolean) {
  label(ctx, tag.id, tag.x, tag.y, idTagFont(), hovered ? "#ffffff" : C.text, tag.align);
}

/** A cluster's callout: one row per robot, the id, the battery bar (neutral, amber under 20 %,
 *  red under 10 %) and what the robot is doing ("failed at 10 h" in red), on a dark panel. */
function drawCallout(ctx: CanvasRenderingContext2D, c: Callout, hover: string | null) {
  const { box } = c;
  roundRect(ctx, box.x0, box.y0, box.x1 - box.x0, box.y1 - box.y0, 5);
  ctx.fillStyle = "rgba(11,15,23,0.82)";
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(148,163,184,0.22)";
  ctx.stroke();
  c.rows.forEach((p, i) => {
    const failed = p.status === "failed";
    const base = box.y0 + 4 + CALLOUT_ROW_H * i + 11;
    let x = box.x0 + CALLOUT_PAD;
    plainText(ctx, p.id, x, base, `600 12px ${FONT_MONO}`, p.id === hover ? "#ffffff" : C.text);
    x += c.idW + 6;
    const frac = p.cap > 0 ? Math.max(0, Math.min(1, p.battery / p.cap)) : 0;
    roundRect(ctx, x, base - 5, CALLOUT_BAR_W, 3, 1.5);
    ctx.fillStyle = C.track;
    ctx.fill();
    if (frac > 0) {
      roundRect(ctx, x, base - 5, Math.max(2, CALLOUT_BAR_W * frac), 3, 1.5);
      ctx.fillStyle = withAlpha(batteryColor(frac), failed ? 0.5 : 1);
      ctx.fill();
    }
    if (!c.compact) {
      plainText(ctx, rowText(p), x + CALLOUT_BAR_W + 6, base, `11px ${FONT_SANS}`, failed ? STATUS_COLORS.failed : C.muted);
    }
  });
}

function drawTooltip({ ctx, geo, model, pointer, placed, hover }: DrawInput) {
  if (!hover || !pointer) return;
  const p = placed.find((q) => q.id === hover);
  if (!p) return;
  const task = p.task ? model.taskMap[p.task] : undefined;
  const state =
    p.status === "working" || p.status === "moving" || (p.status === "idle" && p.task)
      ? `${p.caption}${task ? ` · ${task.name}` : ""}`
      : p.caption;
  const pct = p.cap > 0 ? Math.round((100 * p.battery) / p.cap) : 0;
  const lines: { text: string; font: string; color: string }[] = [
    { text: `${p.id} · ${p.type || "robot"}`, font: `600 12px ${FONT_SANS}`, color: C.text },
    { text: state, font: `11px ${FONT_SANS}`, color: C.text },
    { text: `battery ${Math.round(p.battery)} / ${Math.round(p.cap)} Wh (${pct}%)`, font: `11px ${FONT_SANS}`, color: C.muted },
    { text: `position (${p.pos[0].toFixed(1)}, ${p.pos[1].toFixed(1)})`, font: `11px ${FONT_SANS}`, color: C.muted },
  ];
  const detail = p.status === "failed" ? model.failedDetail[p.id] : undefined;
  if (detail) lines.splice(2, 0, { text: detail, font: `11px ${FONT_SANS}`, color: C.muted });
  let width = 0;
  for (const l of lines) {
    ctx.font = l.font;
    width = Math.max(width, ctx.measureText(l.text).width);
  }
  const padX = 10;
  const lineH = 15;
  const bw = Math.min(geo.w - 8, width + padX * 2 + 2);
  const bh = lines.length * lineH + 12;
  let x = pointer.x + 14;
  let y = pointer.y + 14;
  if (x + bw > geo.w - 4) x = Math.max(4, pointer.x - bw - 14);
  if (y + bh > geo.h - 4) y = Math.max(4, pointer.y - bh - 14);
  roundRect(ctx, x, y, bw, bh, 6);
  ctx.fillStyle = C.tooltipBg;
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = C.tooltipBorder;
  ctx.stroke();
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  lines.forEach((l, i) => {
    ctx.font = l.font;
    ctx.fillStyle = l.color;
    ctx.fillText(l.text, x + padX, y + 6 + lineH * (i + 1) - 4, bw - padX * 2);
  });
}

/** The banner shown at time t: the nearest event inside its window; at equal distance the
 *  failure (and the event that triggered a replan) wins. */
function pickBanner(model: Model, t: number): Banner | null {
  let best: Banner | null = null;
  let bestD = Infinity;
  for (const b of model.banners) {
    const d = Math.abs(t - b.t);
    if (d > PULSE_H + 1e-9) continue;
    if (d < bestD - 1e-9 || (Math.abs(d - bestD) <= 1e-9 && best !== null && b.rank > best.rank)) {
      best = b;
      bestD = d;
    }
  }
  return best;
}

/** Verdict glyph (check or cross) centred at (cx, cy), 10 px tall. */
function drawVerdict(ctx: CanvasRenderingContext2D, verdict: "ok" | "fail", cx: number, cy: number, ink: string) {
  ctx.save();
  ctx.lineWidth = 2;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.strokeStyle = ink;
  ctx.beginPath();
  if (verdict === "ok") {
    ctx.moveTo(cx - 5, cy + 0.5);
    ctx.lineTo(cx - 1.5, cy + 4);
    ctx.lineTo(cx + 5.5, cy - 4);
  } else {
    ctx.moveTo(cx - 4.5, cy - 4.5);
    ctx.lineTo(cx + 4.5, cy + 4.5);
    ctx.moveTo(cx + 4.5, cy - 4.5);
    ctx.lineTo(cx - 4.5, cy + 4.5);
  }
  ctx.stroke();
  ctx.restore();
}

/**
 * Event banner in the header band, in the event colour: "10.0 h · EX1 failed on T3 (78% done)
 * → CP-SAT rescheduled 10 tasks in 0.01 s → ✓ verified, 72 h → 79 h". One line when it fits,
 * otherwise the outcome goes under the lead, a clause that does not fit taking the next line
 * while the band has one (two in all, three on a narrow canvas); what still overflows is
 * ellipsised. It covers the header line, never the map. While autoplay holds on the event a
 * countdown bar drains along the bottom edge and a hint says how to continue.
 */
function drawBanner({ ctx, geo, model, snap, hold }: DrawInput) {
  const b = pickBanner(model, snap.t);
  if (!b) return;
  const fade = clamp01((PULSE_H - Math.abs(snap.t - b.t)) / BANNER_FADE_H);
  if (fade <= 0) return;
  const darkInk = luminance(b.color) > 0.35; // amber and yellow bands take dark ink
  const ink = darkInk ? "#0b0f14" : "#ffffff";
  const inkMuted = darkInk ? "rgba(11,15,23,0.7)" : "rgba(255,255,255,0.78)";
  const padX = 12;
  const lineH = 18;
  const maxW = geo.w - padX * 2;
  const timeFont = `700 13px ${FONT_MONO}`;
  const leadFont = `600 13px ${FONT_SANS}`;
  const restFont = `500 13px ${FONT_SANS}`;
  const hintFont = `12px ${FONT_SANS}`;
  const time = `${b.t.toFixed(1)} h`;
  const sep = " · ";
  const arrow = "  →  ";
  const glyphW = 16;

  const timeW = measure(ctx, time, timeFont);
  const sepW = measure(ctx, sep, leadFont);
  const leadW = measure(ctx, b.lead, leadFont);
  const arrowW = measure(ctx, arrow, restFont);
  const outcomeW = b.outcome.reduce(
    (acc, text, i) => acc + arrowW + (i === b.outcome.length - 1 && b.verdict ? glyphW : 0) + measure(ctx, text, restFont),
    0,
  );
  const oneLine = b.outcome.length === 0 || timeW + sepW + leadW + outcomeW <= maxW;
  const H = geo.top;
  const maxLines = Math.max(2, Math.floor((H - 12) / lineH));

  ctx.save();
  ctx.globalAlpha = fade;
  ctx.fillStyle = b.color;
  ctx.fillRect(0, 0, geo.w, H);
  // a light tint on dark-ink bands, a shade on white-ink bands: keeps text contrast above 4.5:1
  ctx.fillStyle = darkInk ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.18)";
  ctx.fillRect(0, 0, geo.w, H);
  ctx.fillStyle = "rgba(0,0,0,0.35)";
  ctx.fillRect(0, H - 1, geo.w, 1);

  // line 1: time · lead
  const y1 = oneLine ? H / 2 + 4.5 : 8 + 13;
  let x = padX;
  x += plainText(ctx, time, x, y1, timeFont, ink);
  x += plainText(ctx, sep, x, y1, leadFont, inkMuted);
  x += plainText(ctx, fitText(ctx, b.lead, leadFont, Math.max(0, geo.w - padX - x)), x, y1, leadFont, ink);

  // outcome clauses, each after an arrow; the verdict glyph before the last one
  let y = y1;
  let line = 1;
  if (!oneLine) {
    x = padX;
    y = y1 + lineH;
    line = 2;
  }
  b.outcome.forEach((text, i) => {
    const last = i === b.outcome.length - 1;
    const need = arrowW + (last && b.verdict ? glyphW : 0) + measure(ctx, text, restFont);
    if (x > padX && x + need > geo.w - padX && line < maxLines) {
      x = padX;
      y += lineH;
      line += 1;
    }
    x += plainText(ctx, arrow, x, y, restFont, inkMuted);
    if (last && b.verdict) {
      drawVerdict(ctx, b.verdict, x + 6, y - 4.5, ink);
      x += glyphW;
    }
    x += plainText(ctx, fitText(ctx, text, restFont, Math.max(0, geo.w - padX - x)), x, y, restFont, ink);
  });

  // hold: a countdown bar drains along the banner's bottom edge; the hint sits in a pill at the
  // bottom right of the map, where there is always room
  if (hold) {
    ctx.fillStyle = inkMuted;
    ctx.fillRect(0, H - 3, geo.w * clamp01(hold.frac), 3);
    const hint = "paused on the event · click the map or press space to continue";
    const pw = Math.min(geo.w - 16, measure(ctx, hint, hintFont) + 20);
    const ph = 22;
    const px = geo.w - 8 - pw;
    const py = geo.h - 8 - ph;
    roundRect(ctx, px, py, pw, ph, 6);
    ctx.fillStyle = C.tooltipBg;
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = withAlpha(b.color, 0.7);
    ctx.stroke();
    plainText(ctx, fitText(ctx, hint, hintFont, pw - 20), px + 10, py + 15, hintFont, C.text);
  }
  ctx.restore();
}

/** The header band's own line: the site, the simulated time and day, and the tasks done (the
 *  event banner covers it while an event shows). */
function drawHeaderLine({ ctx, geo, model, snap }: DrawInput) {
  const day = Math.floor(snap.t / 24) + 1;
  const y = geo.top / 2 + 4.5;
  ctx.fillStyle = "rgba(148,163,184,0.18)";
  ctx.fillRect(0, geo.top - 1, geo.w, 1);
  label(ctx, `${model.siteName} · t = ${snap.t.toFixed(1)} h · day ${day}`, 12, y, `600 13px ${FONT_SANS}`, C.text, "left");
  label(ctx, `${snap.done.length}/${model.total} tasks done`, geo.w - 12, y, `12px ${FONT_SANS}`, C.muted, "right");
}

// ------------------------------------------------------------------ small SVG pieces for the DOM
function MarkerIcon({ type, color }: { type: string; color: string }) {
  const poly = markerPolygon(type, 5);
  return (
    <svg width="14" height="14" viewBox="-7 -7 14 14" aria-hidden className="shrink-0">
      {poly ? <polygon points={poly.map(([x, y]) => `${x},${y}`).join(" ")} fill={color} /> : <circle r="5" fill={color} />}
    </svg>
  );
}

function PlayIcon({ playing }: { playing: boolean }) {
  return playing ? (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" aria-hidden>
      <rect x="2" y="1.5" width="3.5" height="11" rx="1" />
      <rect x="8.5" y="1.5" width="3.5" height="11" rx="1" />
    </svg>
  ) : (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" aria-hidden>
      <path d="M3.5 1.8a1 1 0 0 1 1.5-.86l8 5.2a1 1 0 0 1 0 1.72l-8 5.2a1 1 0 0 1-1.5-.86z" />
    </svg>
  );
}

function FailedIcon() {
  return (
    <svg width="14" height="14" viewBox="-7 -7 14 14" aria-hidden className="shrink-0">
      <circle r="5.5" fill="none" stroke={STATUS_COLORS.failed} strokeWidth="1.5" />
      <path d="M-3 -3 L3 3 M3 -3 L-3 3" stroke={STATUS_COLORS.failed} strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

const mutedClass = "text-muted";

// ------------------------------------------------------------------ component
export function SimReplay({
  sim,
  plan,
  tasks,
  autoPlay,
  seekRequest,
  onTimeChange,
  maxHeight = 640,
  holdOnEvents = true,
  startAtH = 0,
  heading,
  aside,
}: SimReplayProps) {
  const model = useMemo(() => buildModel(sim, plan, tasks), [sim, plan, tasks]);
  const aspect = useMemo(() => mapAspect(model), [model]);

  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const modelRef = useRef(model);
  const sizeRef = useRef({ w: 0, h: 0, dpr: 1 });
  const tRef = useRef(0);
  const hoverRef = useRef<string | null>(null);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const hitRef = useRef<{ id: string; x: number; y: number }[]>([]);
  const offsetsRef = useRef(new Map<string, { x: number; y: number }>());
  const loopRef = useRef(false);
  const pendingRef = useRef(0);
  const lastPubRef = useRef(0);
  const lastSentRef = useRef<number | null>(null);
  const onTimeChangeRef = useRef(onTimeChange);
  /** the hold in progress: when it ends (performance.now ms) and the event time */
  const holdRef = useRef<{ until: number; t: number } | null>(null);
  const holdOnRef = useRef(holdOnEvents);
  const holdingRef = useRef<number | null>(null);

  const [playing, setPlaying] = useState(Boolean(autoPlay));
  const [speed, setSpeed] = useState<number>(REPLAY_SPEEDS[0]);
  const [uiT, setUiT] = useState(0);
  const [size, setSize] = useState({ w: 0, h: 0 });
  /** event time the autoplay is holding on (for the transport hint), else null */
  const [holding, setHolding] = useState<number | null>(null);

  useEffect(() => {
    modelRef.current = model;
  }, [model]);
  useEffect(() => {
    onTimeChangeRef.current = onTimeChange;
  }, [onTimeChange]);
  useEffect(() => {
    holdOnRef.current = holdOnEvents;
  }, [holdOnEvents]);

  const setHold = useCallback((value: { until: number; t: number } | null) => {
    holdRef.current = value;
    const t = value ? value.t : null;
    if (holdingRef.current !== t) {
      holdingRef.current = t;
      setHolding(t);
    }
  }, []);

  const draw = useCallback((ease: number) => {
    resolveFonts();
    const canvas = canvasRef.current;
    const { w, h, dpr } = sizeRef.current;
    if (!canvas || w < 20 || h < 20) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const m = modelRef.current;
    const snap = snapshotAt(m, tRef.current);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!snap) {
      ctx.fillStyle = C.bg;
      ctx.fillRect(0, 0, w, h);
      label(ctx, "No simulation frames", w / 2, h / 2, `12px ${FONT_SANS}`, C.muted);
      return;
    }
    const geo = geometry(w, h, m);
    const placed = placeRobots(snap.robots, m, geo, offsetsRef.current, ease);
    hitRef.current = placed.map((p) => ({ id: p.id, x: p.x, y: p.y }));
    const hd = holdRef.current;
    const hold = hd ? { frac: clamp01((hd.until - performance.now()) / HOLD_MS) } : null;
    const hover = hoverRef.current;
    const zoneCaps = zoneCaptions(ctx, geo, m);
    const { tags, callouts } = layoutLabels(ctx, geo, m, placed, zoneCaps);
    const input: DrawInput = { ctx, geo, model: m, snap, placed, hover, pointer: pointerRef.current, hold, zoneCaps, tags };
    drawSite(input);
    for (const p of placed) if (p.id !== hover) drawRobot(ctx, p, false);
    const hp = hover ? placed.find((p) => p.id === hover) : undefined;
    if (hp) drawRobot(ctx, hp, true);
    for (const tag of tags) drawIdTag(ctx, tag, tag.id === hover);
    for (const c of callouts) drawCallout(ctx, c, hover);
    drawHeaderLine(input);
    drawBanner(input);
    drawTooltip(input);
  }, []);

  const publish = useCallback((force: boolean) => {
    const now = performance.now();
    if (!force && now - lastPubRef.current < PUBLISH_MS) return;
    lastPubRef.current = now;
    const t = tRef.current;
    setUiT((prev) => (prev === t ? prev : t));
    if (lastSentRef.current !== t) {
      lastSentRef.current = t;
      onTimeChangeRef.current?.(t);
    }
  }, []);

  /** One coalesced draw while paused (the play loop draws on its own). */
  const requestDraw = useCallback(() => {
    if (loopRef.current || pendingRef.current) return;
    pendingRef.current = requestAnimationFrame(() => {
      pendingRef.current = 0;
      draw(1);
      publish(true);
    });
  }, [draw, publish]);

  useEffect(() => {
    return () => {
      if (pendingRef.current) cancelAnimationFrame(pendingRef.current);
    };
  }, []);

  // canvas sizing: CSS (aspect-ratio + max-height) decides the box, the observer rasterises it
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect) return;
      const w = Math.round(rect.width);
      const h = Math.round(rect.height);
      setSize((prev) => (prev.w === w && prev.h === h ? prev : { w, h }));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(size.w * dpr));
    canvas.height = Math.max(1, Math.round(size.h * dpr));
    sizeRef.current = { w: size.w, h: size.h, dpr };
    offsetsRef.current.clear();
    requestDraw();
  }, [size, requestDraw]);

  // a new SimResult rewinds to startAtH (pass a stable object; key the component per run if in doubt)
  useEffect(() => {
    const tMax = modelRef.current.tMax;
    tRef.current = Math.max(0, Math.min(tMax, Number.isFinite(startAtH) ? startAtH : 0));
    offsetsRef.current.clear();
    hoverRef.current = null;
    holdRef.current = null;
    const id = requestAnimationFrame(() => setPlaying(Boolean(autoPlay)));
    requestDraw();
    return () => cancelAnimationFrame(id);
  }, [sim, autoPlay, startAtH, requestDraw]);

  useEffect(() => {
    requestDraw();
  }, [model, requestDraw]);

  // external seek
  useEffect(() => {
    if (!seekRequest) return;
    const tMax = modelRef.current.tMax;
    tRef.current = Math.max(0, Math.min(tMax, seekRequest.t_h));
    offsetsRef.current.clear();
    holdRef.current = null;
    requestDraw();
  }, [seekRequest, requestDraw]);

  // play loop
  useEffect(() => {
    if (!playing) return;
    loopRef.current = true;
    if (pendingRef.current) {
      cancelAnimationFrame(pendingRef.current);
      pendingRef.current = 0;
    }
    let last = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const dt = Math.min(0.1, Math.max(0, (now - last) / 1000));
      last = now;
      const m = modelRef.current;
      const hd = holdRef.current;
      if (hd) {
        if (now < hd.until) {
          // holding on an event: time stands still, the countdown bar drains
          draw(1 - Math.exp(-dt * 12));
          raf = requestAnimationFrame(tick);
          return;
        }
        setHold(null);
      } else if (holdingRef.current !== null) {
        setHold(null);
      }
      const t0 = tRef.current;
      let t = t0 + dt * speed * HOURS_PER_SECOND;
      let held = false;
      if (holdOnRef.current) {
        const h = m.holdTimes.find((x) => x > t0 + 1e-9 && x <= t + 1e-9);
        if (h !== undefined) {
          t = h;
          held = true;
        }
      }
      const ended = t >= m.tMax;
      if (ended) t = m.tMax;
      tRef.current = t;
      if (held && !ended) setHold({ until: now + HOLD_MS, t });
      draw(1 - Math.exp(-dt * 12));
      publish(ended || held);
      if (ended) {
        setPlaying(false);
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      loopRef.current = false;
      holdRef.current = null;
      cancelAnimationFrame(raf);
    };
  }, [playing, speed, draw, publish, setHold]);

  const togglePlay = useCallback(() => {
    setHold(null);
    if (!playing && tRef.current >= modelRef.current.tMax - 1e-9) {
      tRef.current = 0;
      offsetsRef.current.clear();
    }
    setPlaying((p) => !p);
  }, [playing, setHold]);

  /** Ends a hold early; true when there was one. */
  const continueHold = useCallback(() => {
    if (!holdRef.current) return false;
    setHold(null);
    return true;
  }, [setHold]);

  const seek = useCallback(
    (t: number) => {
      const tMax = modelRef.current.tMax;
      tRef.current = Math.max(0, Math.min(tMax, t));
      setHold(null);
      setUiT(tRef.current);
      lastSentRef.current = tRef.current;
      onTimeChangeRef.current?.(tRef.current);
      requestDraw();
    },
    [requestDraw, setHold],
  );

  // space: continue a hold from anywhere outside a form control; play / pause when the map has focus
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key !== " " && e.code !== "Space") || e.repeat) return;
      const target = e.target as HTMLElement | null;
      const inWrap = Boolean(target && wrapRef.current?.contains(target));
      const onBody = !target || target === document.body || target === document.documentElement;
      if (!inWrap && !onBody) return;
      if (continueHold()) {
        e.preventDefault();
        return;
      }
      if (inWrap) {
        e.preventDefault();
        togglePlay();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [continueHold, togglePlay]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 6 : 1;
    switch (e.key) {
      case "ArrowRight":
        seek(tRef.current + step);
        break;
      case "ArrowLeft":
        seek(tRef.current - step);
        break;
      case "Home":
        seek(0);
        break;
      case "End":
        seek(modelRef.current.tMax);
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    pointerRef.current = { x, y };
    let best: string | null = null;
    let bestD = 18;
    for (const hit of hitRef.current) {
      const d = Math.hypot(hit.x - x, hit.y - y);
      if (d < bestD) {
        bestD = d;
        best = hit.id;
      }
    }
    hoverRef.current = best;
    e.currentTarget.style.cursor = best ? "pointer" : "default";
    requestDraw();
  };

  const onPointerLeave = (e: React.PointerEvent<HTMLCanvasElement>) => {
    hoverRef.current = null;
    pointerRef.current = null;
    e.currentTarget.style.cursor = "default";
    requestDraw();
  };

  // ---- readouts (React state, ~12 fps)
  const snap = useMemo(() => snapshotAt(model, uiT), [model, uiT]);
  const tMax = model.tMax;
  const day = Math.floor(uiT / 24) + 1;
  const horizonDays = Math.max(1, Math.ceil(model.horizonH / 24));
  const doneCount = snap?.done.length ?? 0;
  const active = snap?.active ?? [];
  const robotOnTask = (taskId: string) =>
    snap?.robots.find((r) => r.task === taskId && r.status === "working")?.id ??
    snap?.robots.find((r) => r.task === taskId)?.id ??
    "";
  const atEnd = uiT >= tMax - 1e-9;
  const banner = snap ? pickBanner(model, snap.t) : null;
  const summary = snap
    ? `${model.siteName} at ${uiT.toFixed(1)} hours: ${doneCount} of ${model.total} tasks done; ` +
      snap.robots.map((r) => `${r.id} ${r.caption}`).join(", ") +
      (banner ? `. Event at ${banner.t.toFixed(1)} h: ${[banner.lead, ...banner.outcome].join(", then ")}` : "")
    : "No simulation frames";
  const robotTypes = Array.from(new Set(model.robotList.map((r) => r.type)));
  const statuses: RobotStatus[] = ["idle", "moving", "working", "failed", "done"];
  const eventKinds = Object.keys(EVENT_COLORS).filter((k) => k !== "battery_low");

  const legend = (
    <div className={`flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] ${mutedClass}`}>
      {robotTypes.map((t) => (
        <span key={t} className="inline-flex items-center gap-1.5">
          <MarkerIcon type={t} color={robotTypeColor(t)} />
          {t}
        </span>
      ))}
      <span className="h-3 w-px bg-white/15" aria-hidden />
      {statuses.map((s) => (
        <span key={s} className="inline-flex items-center gap-1.5">
          {s === "failed" ? (
            <FailedIcon />
          ) : (
            <span className="h-3 w-3 rounded-full border-2" style={{ borderColor: STATUS_COLORS[s] }} aria-hidden />
          )}
          {s}
        </span>
      ))}
      <span className="h-3 w-px bg-white/15" aria-hidden />
      {eventKinds.map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <span className="h-3 w-0.5 rounded-full" style={{ backgroundColor: EVENT_COLORS[k] }} aria-hidden />
          {eventKindLabel(k)}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <span className="h-1.5 w-1.5 rotate-45 bg-info" aria-hidden />
        replan
      </span>
      <span className="h-3 w-px bg-white/15" aria-hidden />
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-flex h-1 w-5 overflow-hidden rounded-full bg-white/15" aria-hidden>
          <span className="h-full w-1/5" style={{ backgroundColor: EVENT_COLORS.battery_low }} />
        </span>
        battery &lt;20%
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="inline-flex h-1 w-5 overflow-hidden rounded-full bg-white/15" aria-hidden>
          <span className="h-full w-[10%]" style={{ backgroundColor: STATUS_COLORS.failed }} />
        </span>
        &lt;10%
      </span>
    </div>
  );

  const activeTasks = (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium tabular-nums">
          {doneCount}/{model.total} tasks done
        </span>
        <span className={`text-xs ${mutedClass}`}>{active.length} active</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-white/10">
        <div className="h-full rounded-full bg-info transition-[width]" style={{ width: `${model.total ? (100 * doneCount) / model.total : 0}%` }} />
      </div>
      <ul className="mt-2 space-y-1.5">
        {active.map((id) => {
          const task = model.taskMap[id];
          const robot = robotOnTask(id);
          const type = model.robots[robot]?.type ?? "";
          const pct = Math.round(100 * (snap?.progress[id] ?? 0));
          return (
            <li key={id}>
              <div className="flex items-baseline justify-between gap-2 text-xs">
                <span className="min-w-0 truncate">
                  <span className="font-mono font-semibold">{id}</span>{" "}
                  <span className={mutedClass}>{task?.name ?? ""}</span>
                </span>
                <span className={`shrink-0 font-mono tabular-nums ${mutedClass}`}>
                  {robot}
                  {robot ? " · " : ""}
                  {pct}%
                </span>
              </div>
              <div className="mt-0.5 h-1 w-full overflow-hidden rounded-full bg-white/10">
                <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: robotTypeColor(type) }} />
              </div>
            </li>
          );
        })}
        {active.length === 0 && (
          <li className={`text-xs ${mutedClass}`}>
            {atEnd
              ? sim.completed
                ? "All tasks finished."
                : "The run ended with unfinished tasks."
              : "No task in progress: robots are moving or waiting."}
          </li>
        )}
      </ul>
    </div>
  );

  return (
    <div className={aside ? "grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]" : "space-y-3"}>
    <div className="min-w-0 space-y-3">
      {heading}
      <div
        ref={wrapRef}
        role="group"
        tabIndex={0}
        aria-label="Simulation replay map. Space plays, pauses or continues past a held event; arrow keys step one hour, shift for six."
        aria-keyshortcuts="Space ArrowLeft ArrowRight Home End"
        onClick={() => {
          if (!continueHold()) togglePlay();
        }}
        onKeyDown={onKeyDown}
        className="relative w-full overflow-hidden rounded-xl border border-line focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
        style={{ aspectRatio: aspect, maxHeight, backgroundColor: C.bg }}
      >
        <canvas
          ref={canvasRef}
          className="absolute inset-0 block h-full w-full touch-none"
          role="img"
          aria-label={summary}
          onPointerMove={onPointerMove}
          onPointerLeave={onPointerLeave}
        />
      </div>

      {/* transport */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={togglePlay}
          aria-label={playing ? "Pause" : atEnd ? "Replay from the start" : "Play"}
          aria-pressed={playing}
          className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-accent text-[#0b0f14] shadow-sm transition hover:brightness-110"
        >
          <PlayIcon playing={playing} />
        </button>
        <div className="inline-flex overflow-hidden rounded-md border border-white/15" role="group" aria-label="Playback speed">
          {REPLAY_SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              aria-pressed={speed === s}
              onClick={() => setSpeed(s)}
              className={`px-2.5 py-1 text-xs font-medium tabular-nums transition-colors ${
                speed === s ? "bg-fg text-surface" : "hover:bg-white/10"
              }`}
            >
              {s}x
            </button>
          ))}
        </div>
        {/* the hold hint is drawn on the canvas (bottom-right pill); this copy is for screen
            readers only, so the transport row never reflows while the replay holds */}
        <span className="sr-only" role="status">
          {holding !== null && playing ? `holding at ${formatHours(holding)} · click the map or press space to continue` : ""}
        </span>
        <div className="ml-auto flex flex-wrap items-baseline gap-x-3 gap-y-0.5 tabular-nums">
          <span className="text-xl font-semibold leading-none">{uiT.toFixed(1)} h</span>
          <span className={`text-sm ${mutedClass}`}>
            day {day} / {horizonDays}
          </span>
          <span className={`text-sm ${mutedClass}`}>
            {doneCount}/{model.total} tasks
          </span>
          <span className={`text-xs ${mutedClass}`}>
            {sim.completed && sim.finished_at_h !== null
              ? `finishes at ${formatHours(sim.finished_at_h)}`
              : `does not finish (${formatHours(tMax)} simulated)`}
          </span>
        </div>
      </div>

      {/* scrubber: event ticks above the slider, replan markers below it */}
      <div className="relative h-14">
        <div className="absolute inset-x-2 top-0 h-5">
          {tMax > 0 &&
            model.events.map((e, i) => (
              <button
                key={`e${i}`}
                type="button"
                onClick={() => seek(e.t_h)}
                title={`${formatHours(e.t_h)} · ${eventKindLabel(e.kind)}${e.robot ? ` ${e.robot}` : ""}${e.detail ? ` — ${e.detail}` : ""}`}
                aria-label={`Seek to ${eventKindLabel(e.kind)} at ${formatHours(e.t_h)}`}
                className="absolute top-1.5 h-3.5 w-3 -translate-x-1/2 cursor-pointer"
                style={{ left: `${(100 * e.t_h) / tMax}%` }}
              >
                <span
                  className={`absolute left-1/2 -translate-x-1/2 rounded-full ${e.kind === "robot_failure" ? "inset-y-0 w-[3px]" : "inset-y-0.5 w-0.5"}`}
                  style={{ backgroundColor: EVENT_COLORS[e.kind] ?? "#94a3b8" }}
                />
              </button>
            ))}
        </div>
        <input
          type="range"
          aria-label="Simulation time"
          className="absolute inset-x-0 top-5 w-full accent-accent"
          min={0}
          max={tMax}
          step={0.05}
          value={uiT}
          onChange={(e) => seek(Number(e.target.value))}
        />
        <div className="absolute inset-x-2 bottom-0 h-4">
          {tMax > 0 &&
            model.replanTimes.map((t, i) => (
              <button
                key={`r${i}`}
                type="button"
                onClick={() => seek(t)}
                title={`Replan #${i + 1} at ${formatHours(t)}`}
                aria-label={`Seek to replan ${i + 1} at ${formatHours(t)}`}
                className="absolute top-0 flex -translate-x-1/2 cursor-pointer flex-col items-center"
                style={{ left: `${(100 * t) / tMax}%` }}
              >
                <span className="h-1.5 w-1.5 rotate-45 bg-info" />
                <span className="mt-0.5 text-[11px] font-semibold leading-none text-info">R{i + 1}</span>
              </button>
            ))}
        </div>
        <div className={`pointer-events-none absolute inset-x-0 bottom-0 flex justify-between text-[11px] tabular-nums ${mutedClass}`}>
          <span>0 h</span>
          <span>{formatHours(tMax)}</span>
        </div>
      </div>
      {!aside && (
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
          {legend}
          {activeTasks}
        </div>
      )}
    </div>
    {aside && (
      <div className="min-w-0 space-y-5">
        {aside}
        {activeTasks}
        <div className="border-t border-line pt-3">{legend}</div>
      </div>
    )}
    </div>
  );
}
