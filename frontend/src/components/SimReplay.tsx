"use client";
/**
 * 2D replay of a SimResult: the site grid with zones, depot and structures, robots from the
 * current (interpolated) frame with status ring, battery bar and task caption, transport
 * controls, a scrubber with event ticks and replan markers, and a live task readout.
 *
 * Playback state (time, hover, cluster offsets) lives in refs and is drawn by
 * requestAnimationFrame; React state is only updated ~12 times per second for the readouts.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PlanResult, RobotStatus, SimEvent, SimFrame, SimResult, Task, Zone } from "@/lib/types";
import { EVENT_COLORS, STATUS_COLORS, robotTypeColor, zoneColor } from "@/lib/palette";
import { zoneOrder } from "@/lib/tasks";
import { eventKindLabel, formatHours, withAlpha } from "./EventList";

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
}

/** Playback speeds; 1x replays one simulated hour per real second. */
export const REPLAY_SPEEDS = [1, 4, 16] as const;
const HOURS_PER_SECOND = 1;
const PUBLISH_MS = 80;
const PULSE_H = 2.5; // how long an event ring keeps expanding, in simulated hours
const R = 7; // marker radius
const RING = R + 4.5; // status ring radius
const BAR_W = 26;
const PAD = 18;

const FONT_SANS = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
const FONT_MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

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
};

// ------------------------------------------------------------------ model & snapshot
interface RobotMeta {
  id: string;
  type: string;
  cap: number;
  index: number;
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
  tMax: number;
  horizonH: number;
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
}

interface Snapshot {
  t: number;
  robots: SnapRobot[];
  done: string[];
  active: string[];
  progress: Record<string, number>;
}

const lerp = (a: number, b: number, k: number) => a + (b - a) * k;

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
  const robots: SnapRobot[] = fi.robots.map((r) => {
    const meta = model.robots[r.id];
    const n = alpha > 0 ? next.get(r.id) : undefined;
    const base: SnapRobot = {
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

function batteryColor(frac: number): string {
  return frac > 0.5 ? STATUS_COLORS.working : frac > 0.2 ? EVENT_COLORS.battery_low : STATUS_COLORS.failed;
}

function caption(r: SnapRobot): string | null {
  switch (r.status) {
    case "working":
      return r.task ? `working on ${r.task}` : "working";
    case "moving":
      return r.task ? `→ ${r.task}` : "moving";
    case "idle":
      return r.task ? `waiting · ${r.task}` : "idle";
    case "failed":
      return "failed";
    case "done":
      return "done";
    default:
      return null;
  }
}

interface Placed extends SnapRobot {
  x: number;
  y: number;
  dir: [number, number];
}

/** Vertical extent of a robot's marker plus its label stack (mirrors drawRobot's layout). */
function labelTop(p: Placed): number {
  if (p.dir[1] < -0.3) return p.y - (RING + 5) - 3 - (caption(p) ? 12 : 0) - 15 - 2;
  return p.y - RING - 2;
}
function labelBottom(p: Placed): number {
  if (p.dir[1] > 0.3) return p.y + RING + 5 + 10 + (caption(p) ? 12 : 0) + 5 + 5;
  if (Math.abs(p.dir[1]) <= 0.3) return p.y + (caption(p) ? 9 : -2) + 5 + 5;
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
    label(ctx, String(x), x0 + x * s, yBot + 11, `9px ${FONT_MONO}`, C.faint, "center");
  }
  for (let y = 0; y <= gy; y += 10) {
    label(ctx, String(y), x0 - 4, yBot - y * s + 3, `9px ${FONT_MONO}`, C.faint, "right");
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
    const extraFont = `10px ${FONT_SANS}`;
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
    label(ctx, "Depot", dx, dy - half - 5, `10px ${FONT_SANS}`, C.muted);
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

  // event pulses
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
  const dim = p.status === "failed" ? 0.35 : p.status === "done" ? 0.6 : 1;

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
  if (p.status === "failed") {
    const k = R + 2;
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.strokeStyle = STATUS_COLORS.failed;
    ctx.beginPath();
    ctx.moveTo(p.x - k, p.y - k);
    ctx.lineTo(p.x + k, p.y + k);
    ctx.moveTo(p.x + k, p.y - k);
    ctx.lineTo(p.x - k, p.y + k);
    ctx.stroke();
    ctx.lineCap = "butt";
  }

  // labels: id, caption, battery bar, laid out away from the cluster centre
  const [dx, dy] = p.dir;
  const ax = p.x + dx * (RING + 5);
  const ay = p.y + dy * (RING + 5);
  const align: CanvasTextAlign = Math.abs(dx) > 0.3 ? (dx > 0 ? "left" : "right") : "center";
  const vert = dy > 0.3 ? "down" : dy < -0.3 ? "up" : "middle";
  const cap = caption(p);
  const idFont = `600 11px ${FONT_MONO}`;
  const capFont = `10px ${FONT_SANS}`;
  let idY: number;
  let capY: number | null;
  let barY: number;
  if (vert === "down") {
    idY = ay + 10;
    capY = cap ? idY + 12 : null;
    barY = (capY ?? idY) + 5;
  } else if (vert === "up") {
    idY = ay - 3;
    capY = cap ? idY - 12 : null;
    barY = (capY ?? idY) - 15;
  } else {
    idY = p.y - 2;
    capY = cap ? idY + 11 : null;
    barY = (capY ?? idY) + 5;
  }
  label(ctx, p.id, ax, idY, idFont, hovered ? "#ffffff" : C.text, align);
  if (cap && capY !== null) label(ctx, cap, ax, capY, capFont, C.muted, align);
  const frac = p.cap > 0 ? Math.max(0, Math.min(1, p.battery / p.cap)) : 0;
  const bx = align === "left" ? ax : align === "right" ? ax - BAR_W : ax - BAR_W / 2;
  roundRect(ctx, bx, barY, BAR_W, 3, 1.5);
  ctx.fillStyle = C.track;
  ctx.fill();
  if (frac > 0) {
    roundRect(ctx, bx, barY, Math.max(2, BAR_W * frac), 3, 1.5);
    ctx.fillStyle = withAlpha(batteryColor(frac), p.status === "failed" ? 0.5 : 1);
    ctx.fill();
  }
}

function drawTooltip({ ctx, geo, model, pointer, placed, hover }: DrawInput) {
  if (!hover || !pointer) return;
  const p = placed.find((q) => q.id === hover);
  if (!p) return;
  const task = p.task ? model.taskMap[p.task] : undefined;
  const state =
    p.status === "working" || p.status === "moving" || (p.status === "idle" && p.task)
      ? `${caption(p)}${task ? ` · ${task.name}` : ""}`
      : (caption(p) ?? p.status);
  const pct = p.cap > 0 ? Math.round((100 * p.battery) / p.cap) : 0;
  const lines: { text: string; font: string; color: string }[] = [
    { text: `${p.id} · ${p.type || "robot"}`, font: `600 12px ${FONT_SANS}`, color: C.text },
    { text: state, font: `11px ${FONT_SANS}`, color: C.text },
    { text: `battery ${Math.round(p.battery)} / ${Math.round(p.cap)} Wh (${pct}%)`, font: `11px ${FONT_SANS}`, color: C.muted },
    { text: `position (${p.pos[0].toFixed(1)}, ${p.pos[1].toFixed(1)})`, font: `11px ${FONT_SANS}`, color: C.muted },
  ];
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

function drawOverlay({ ctx, geo, model, snap }: DrawInput) {
  const day = Math.floor(snap.t / 24) + 1;
  const [x0, yTop] = toPx(geo, 0, geo.gy);
  const [x1] = toPx(geo, geo.gx, 0);
  label(ctx, `${model.siteName} · t = ${snap.t.toFixed(1)} h · day ${day}`, x0 + 8, yTop + 16, `600 12px ${FONT_SANS}`, C.text, "left");
  label(ctx, `${snap.done.length}/${model.total} tasks done`, x1 - 8, yTop + 16, `11px ${FONT_SANS}`, C.muted, "right");
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

const mutedClass = "text-muted";

// ------------------------------------------------------------------ component
export function SimReplay({ sim, plan, tasks, autoPlay, seekRequest, onTimeChange, maxHeight = 640 }: SimReplayProps) {
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

  const [playing, setPlaying] = useState(Boolean(autoPlay));
  const [speed, setSpeed] = useState<number>(REPLAY_SPEEDS[0]);
  const [uiT, setUiT] = useState(0);
  const [size, setSize] = useState({ w: 0, h: 0 });

  useEffect(() => {
    modelRef.current = model;
  }, [model]);
  useEffect(() => {
    onTimeChangeRef.current = onTimeChange;
  }, [onTimeChange]);

  const draw = useCallback((ease: number) => {
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
    const input: DrawInput = { ctx, geo, model: m, snap, placed, hover: hoverRef.current, pointer: pointerRef.current };
    drawSite(input);
    const hover = hoverRef.current;
    for (const p of placed) if (p.id !== hover) drawRobot(ctx, p, false);
    const hp = hover ? placed.find((p) => p.id === hover) : undefined;
    if (hp) drawRobot(ctx, hp, true);
    drawOverlay(input);
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

  // a new SimResult rewinds (pass a stable object; key the component per run if in doubt)
  useEffect(() => {
    tRef.current = 0;
    offsetsRef.current.clear();
    hoverRef.current = null;
    const id = requestAnimationFrame(() => setPlaying(Boolean(autoPlay)));
    requestDraw();
    return () => cancelAnimationFrame(id);
  }, [sim, autoPlay, requestDraw]);

  useEffect(() => {
    requestDraw();
  }, [model, requestDraw]);

  // external seek
  useEffect(() => {
    if (!seekRequest) return;
    const tMax = modelRef.current.tMax;
    tRef.current = Math.max(0, Math.min(tMax, seekRequest.t_h));
    offsetsRef.current.clear();
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
      const tMax = modelRef.current.tMax;
      let t = tRef.current + dt * speed * HOURS_PER_SECOND;
      const ended = t >= tMax;
      if (ended) t = tMax;
      tRef.current = t;
      draw(1 - Math.exp(-dt * 12));
      publish(ended);
      if (ended) {
        setPlaying(false);
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      loopRef.current = false;
      cancelAnimationFrame(raf);
    };
  }, [playing, speed, draw, publish]);

  const togglePlay = () => {
    if (!playing && tRef.current >= model.tMax - 1e-9) {
      tRef.current = 0;
      offsetsRef.current.clear();
    }
    setPlaying((p) => !p);
  };

  const seek = (t: number) => {
    tRef.current = Math.max(0, Math.min(model.tMax, t));
    setUiT(tRef.current);
    lastSentRef.current = tRef.current;
    onTimeChange?.(tRef.current);
    requestDraw();
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
  const summary = snap
    ? `${model.siteName} at ${uiT.toFixed(1)} hours: ${doneCount} of ${model.total} tasks done; ` +
      snap.robots.map((r) => `${r.id} ${caption(r) ?? r.status}`).join(", ")
    : "No simulation frames";
  const robotTypes = Array.from(new Set(model.robotList.map((r) => r.type)));
  const statuses: RobotStatus[] = ["idle", "moving", "working", "failed", "done"];
  const [gx, gy] = model.grid;

  return (
    <div className="space-y-3">
      <div
        ref={wrapRef}
        className="relative w-full overflow-hidden rounded-xl border border-line"
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
                <span className="absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 rounded-full" style={{ backgroundColor: EVENT_COLORS[e.kind] ?? "#94a3b8" }} />
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
                <span className="mt-0.5 text-[9px] font-semibold leading-none text-info">R{i + 1}</span>
              </button>
            ))}
        </div>
        <div className={`pointer-events-none absolute inset-x-0 bottom-0 flex justify-between text-[10px] tabular-nums ${mutedClass}`}>
          <span>0 h</span>
          <span>{formatHours(tMax)}</span>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
        {/* legend */}
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
              <span className="h-3 w-3 rounded-full border-2" style={{ borderColor: STATUS_COLORS[s] }} aria-hidden />
              {s}
            </span>
          ))}
          <span className="h-3 w-px bg-white/15" aria-hidden />
          {Object.keys(EVENT_COLORS).map((k) => (
            <span key={k} className="inline-flex items-center gap-1.5">
              <span className="h-3 w-0.5 rounded-full" style={{ backgroundColor: EVENT_COLORS[k] }} aria-hidden />
              {eventKindLabel(k)}
            </span>
          ))}
        </div>

        {/* active tasks */}
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
      </div>
    </div>
  );
}
