"use client";
/**
 * 2D replay of a SimResult: the site grid with zones, depot and structures, robots from the
 * current (interpolated) frame with status ring, battery bar and task caption, transport
 * controls, a scrubber with event ticks and replan markers, and a live task readout.
 *
 * The climax: around each event a banner in the event colour runs across the top of the map
 * ("30.0 h · EX2 failed on T4 (79% done) → CP-SAT rescheduled 11 tasks in <0.01 s →
 * verified, 80 h → 91 h"), and autoplay holds for HOLD_MS (4 s) at every robot failure and
 * replan marker (click the map or press space to continue). Failed robots keep a red X at their last
 * position with "failed at 30 h"; robots queued behind a busy zone read "waiting · zone Z1".
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
const BANNER_H = 38;
const R = 7; // marker radius
const RING = R + 4.5; // status ring radius
const BAR_W = 26;
const PAD = 18;

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
  /** "EX2 failed on T4 (79% done)" */
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
  /** "working on T3", "→ T4", "waiting · zone Z1", "waiting · T9 after T3", "failed at 30 h" */
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
interface Geo {
  w: number;
  h: number;
  s: number;
  ox: number;
  oy: number;
  gx: number;
  gy: number;
}

function geometry(w: number, h: number, grid: [number, number]): Geo {
  const [gx, gy] = grid;
  const s = Math.max(0.5, Math.min((w - 2 * PAD) / gx, (h - 2 * PAD) / gy));
  return { w, h, s, ox: (w - s * gx) / 2, oy: (h - s * gy) / 2, gx, gy };
}

/** Grid y grows northward (the depot sits on the south edge), so the canvas flips it. */
function toPx(g: Geo, x: number, y: number): [number, number] {
  return [g.ox + x * g.s, g.oy + (g.gy - y) * g.s];
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
  dir: [number, number];
}

/** Vertical extent of a robot's marker plus its label stack (mirrors drawRobot's layout). */
function labelTop(p: Placed): number {
  if (p.dir[1] < -0.3) return p.y - (RING + 5) - 3 - (p.caption ? 12 : 0) - 15 - 2;
  return p.y - RING - 2;
}
function labelBottom(p: Placed): number {
  if (p.dir[1] > 0.3) return p.y + RING + 5 + 10 + (p.caption ? 12 : 0) + 5 + 5;
  if (Math.abs(p.dir[1]) <= 0.3) return p.y + (p.caption ? 9 : -2) + 5 + 5;
  return p.y + RING + 2;
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
  for (const group of groups.values()) {
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
      out.push({ ...r, x: px + cur.x, y: py + cur.y, dir });
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
}

function drawSite({ ctx, geo, model, snap, placed }: DrawInput) {
  const { w, h, s, gx, gy } = geo;
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, w, h);

  // grid: hairlines every cell (when there is room) and every 5 cells
  const [x0, yTop] = toPx(geo, 0, gy);
  const [x1, yBot] = toPx(geo, gx, 0);
  ctx.lineWidth = 1;
  for (let x = 0; x <= gx; x++) {
    const major = x % 5 === 0;
    if (!major && s < 9) continue;
    ctx.strokeStyle = major ? C.gridMajor : C.grid;
    const px = Math.round(x0 + x * s) + 0.5;
    ctx.beginPath();
    ctx.moveTo(px, yTop);
    ctx.lineTo(px, yBot);
    ctx.stroke();
  }
  for (let y = 0; y <= gy; y++) {
    const major = y % 5 === 0;
    if (!major && s < 9) continue;
    ctx.strokeStyle = major ? C.gridMajor : C.grid;
    const py = Math.round(yBot - y * s) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x0, py);
    ctx.lineTo(x1, py);
    ctx.stroke();
  }
  ctx.strokeStyle = C.border;
  ctx.strokeRect(Math.round(x0) + 0.5, Math.round(yTop) + 0.5, Math.round(s * gx), Math.round(s * gy));
  // tick labels every 10 cells
  for (let x = 0; x <= gx; x += 10) {
    label(ctx, String(x), x0 + x * s, yBot + 13, `11px ${FONT_MONO}`, C.faint, "center");
  }
  for (let y = 0; y <= gy; y += 10) {
    label(ctx, String(y), x0 - 5, yBot - y * s + 4, `11px ${FONT_MONO}`, C.faint, "right");
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
    // caption outside the circle, pushed further out when a robot cluster's labels overshoot it
    const inZone = placed.filter((p) => Math.hypot(p.pos[0] - z.center[0], p.pos[1] - z.center[1]) <= z.radius + 0.5);
    const topMost = Math.min(cy - r, ...inZone.map(labelTop));
    const bottomMost = Math.max(cy + r, ...inZone.map(labelBottom));
    const above = topMost - 8 > yTop + 14;
    const capY = above ? topMost - 7 : bottomMost + 14;
    const structures = model.structureLabels[z.id] ?? [];
    let extra = structures.join(", ");
    if (extra.length > 30) extra = extra.slice(0, 29) + "…";
    const idFont = `600 12px ${FONT_SANS}`;
    const extraFont = `11px ${FONT_SANS}`;
    ctx.font = idFont;
    const idW = ctx.measureText(z.id).width;
    ctx.font = extraFont;
    const extraW = extra ? ctx.measureText(extra).width + 6 : 0;
    const startX = cx - (idW + extraW) / 2;
    label(ctx, z.id, startX, capY, idFont, C.text, "left");
    if (extra) label(ctx, extra, startX + idW + 6, capY, extraFont, C.muted, "left");
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
    label(ctx, "Depot", dx, dy - half - 5, `11px ${FONT_SANS}`, C.muted);
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

  // labels: id, caption, battery bar, laid out away from the cluster centre
  const [dx, dy] = p.dir;
  const ax = p.x + dx * (RING + 5);
  const ay = p.y + dy * (RING + 5);
  const align: CanvasTextAlign = Math.abs(dx) > 0.3 ? (dx > 0 ? "left" : "right") : "center";
  const vert = dy > 0.3 ? "down" : dy < -0.3 ? "up" : "middle";
  const cap = p.caption;
  const idFont = `600 12px ${FONT_MONO}`;
  const capFont = `11px ${FONT_SANS}`;
  let idY: number;
  let capY: number | null;
  let barY: number;
  if (vert === "down") {
    idY = ay + 11;
    capY = cap ? idY + 13 : null;
    barY = (capY ?? idY) + 5;
  } else if (vert === "up") {
    idY = ay - 3;
    capY = cap ? idY - 13 : null;
    barY = (capY ?? idY) - 16;
  } else {
    idY = p.y - 2;
    capY = cap ? idY + 12 : null;
    barY = (capY ?? idY) + 5;
  }
  label(ctx, p.id, ax, idY, idFont, hovered ? "#ffffff" : C.text, align);
  if (cap && capY !== null) label(ctx, cap, ax, capY, capFont, failed ? STATUS_COLORS.failed : C.muted, align);
  const frac = p.cap > 0 ? Math.max(0, Math.min(1, p.battery / p.cap)) : 0;
  const bx = align === "left" ? ax : align === "right" ? ax - BAR_W : ax - BAR_W / 2;
  roundRect(ctx, bx, barY, BAR_W, 3, 1.5);
  ctx.fillStyle = C.track;
  ctx.fill();
  if (frac > 0) {
    roundRect(ctx, bx, barY, Math.max(2, BAR_W * frac), 3, 1.5);
    ctx.fillStyle = withAlpha(batteryColor(frac), failed ? 0.5 : 1);
    ctx.fill();
  }
  // a low battery is quantified next to its bar (text in text colour; the bar carries the state)
  if (frac < 0.2 && !failed) {
    const pct = `${Math.round(frac * 100)}%`;
    const px = align === "right" ? bx - 4 : bx + BAR_W + 4;
    label(ctx, pct, px, barY + 4, `600 11px ${FONT_MONO}`, C.text, align === "right" ? "right" : "left");
  }
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
 * Event banner across the top of the canvas, in the event colour: "30.0 h · EX2 failed on T4
 * (79% done) → CP-SAT rescheduled 11 tasks in <0.01 s → ✓ verified, 80 h → 91 h". One line
 * when it fits, otherwise the outcome wraps to a second line; clauses that still overflow are
 * ellipsised. While autoplay holds on the event a countdown bar drains along the bottom edge
 * and a hint says how to continue. Returns the banner height (0 when none is showing).
 */
function drawBanner({ ctx, geo, model, snap, hold }: DrawInput): number {
  const b = pickBanner(model, snap.t);
  if (!b) return 0;
  const fade = clamp01((PULSE_H - Math.abs(snap.t - b.t)) / BANNER_FADE_H);
  if (fade <= 0) return 0;
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
  const twoLines = b.outcome.length > 0 && timeW + sepW + leadW + outcomeW > maxW;
  const H = twoLines ? lineH * 2 + 12 : BANNER_H;

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
  const y1 = twoLines ? 8 + 13 : H / 2 + 4.5;
  let x = padX;
  x += plainText(ctx, time, x, y1, timeFont, ink);
  x += plainText(ctx, sep, x, y1, leadFont, inkMuted);
  x += plainText(ctx, fitText(ctx, b.lead, leadFont, Math.max(0, geo.w - padX - x)), x, y1, leadFont, ink);

  // outcome clauses, each after an arrow; the verdict glyph before the last one
  let y = y1;
  if (twoLines) {
    x = padX;
    y = y1 + lineH;
  }
  b.outcome.forEach((text, i) => {
    const last = i === b.outcome.length - 1;
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
  return H;
}

function drawOverlay({ ctx, geo, model, snap }: DrawInput, topInset: number) {
  const day = Math.floor(snap.t / 24) + 1;
  const [x0, yTop] = toPx(geo, 0, geo.gy);
  const [x1] = toPx(geo, geo.gx, 0);
  const y = topInset > 0 ? topInset + 16 : yTop + 16;
  label(ctx, `${model.siteName} · t = ${snap.t.toFixed(1)} h · day ${day}`, x0 + 8, y, `600 13px ${FONT_SANS}`, C.text, "left");
  label(ctx, `${snap.done.length}/${model.total} tasks done`, x1 - 8, y, `12px ${FONT_SANS}`, C.muted, "right");
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
    const geo = geometry(w, h, m.grid);
    const placed = placeRobots(snap.robots, m, geo, offsetsRef.current, ease);
    hitRef.current = placed.map((p) => ({ id: p.id, x: p.x, y: p.y }));
    const hd = holdRef.current;
    const hold = hd ? { frac: clamp01((hd.until - performance.now()) / HOLD_MS) } : null;
    const input: DrawInput = { ctx, geo, model: m, snap, placed, hover: hoverRef.current, pointer: pointerRef.current, hold };
    drawSite(input);
    const hover = hoverRef.current;
    for (const p of placed) if (p.id !== hover) drawRobot(ctx, p, false);
    const hp = hover ? placed.find((p) => p.id === hover) : undefined;
    if (hp) drawRobot(ctx, hp, true);
    const bannerH = drawBanner(input);
    drawOverlay(input, bannerH);
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
  const [gx, gy] = model.grid;

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
        style={{ aspectRatio: `${gx} / ${gy}`, maxHeight, backgroundColor: C.bg }}
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
                <span className="mt-0.5 text-[10px] font-semibold leading-none text-info">R{i + 1}</span>
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
