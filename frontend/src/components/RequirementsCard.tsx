"use client";
/**
 * Step 02: what the model understood from the brief, editable. The site is drawn as a tiny
 * map (grid, zone circles, depot) so geometry mistakes are visible at a glance, with red kept
 * for what the backend would reject (a zone centre or the depot off the grid, a radius out of
 * bounds) and a muted note for a disc that merely reaches past the edge; every structure shows
 * the operations the brief asks for as chips (toggled from the validator's closed vocabulary)
 * and reads as prose ("20 m diameter · min diameter 12 m · 0.4 m deep", the smallest size a
 * brief allows editable like any dimension) with the task graph's work under it once a plan
 * exists, one figure per operation; the structuring stage's notes (operations recovered from a
 * scrambled answer, an operation the brief's entry states that the structuring omitted and
 * was restored, a structure without dimensions, an operation the fleet cannot staff and
 * was dropped, a size read from the brief, zone geometry) head the card; validation mirrors
 * the backend's pydantic limits. An operation chip agrees with the coverage check: one the
 * current fleet has no robot type for reads "not required", one the structuring stage
 * dropped for that reason stays visible, dashed, as "dropped", and one it restored from the
 * structure's own entry in the brief reads "restored". The static demo shows the card
 * read-only: the fields keep their values at full contrast, the add / remove / edit
 * affordances are gone, and the plan button replays the recorded run.
 */
import { createContext, useContext, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { LoopStep, Operation, Requirements, Robot, Site, Structure, Task, Unit } from "@/lib/types";
import { ID_PATTERN, LIMITS } from "@/lib/types";
import { ROBOT_TYPE_COLORS, robotTypeColor, zoneColor } from "@/lib/palette";
import {
  OPERATIONS,
  dimLabel,
  dimPhrase,
  fmtCompact,
  fmtHorizon,
  fmtNum,
  fmtSeconds,
  fmtWh,
  humanizeFields,
  isFixtureModel,
  isOperation,
  minDimension,
  operationLabel,
  plural,
  modelDisplayName,
  modelTitle,
  structureDims,
  structureOperations,
} from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  Dot,
  Field,
  IconX,
  Notice,
  NumberInput,
  Section,
  TextInput,
  cx,
  inputClass,
} from "./Section";

export interface RequirementsCardProps {
  value: Requirements;
  onChange: (next: Requirements) => void;
  /** The structure step that produced the requirements (model, tokens, duration, warnings). */
  structureStep: LoopStep | null;
  source: "model" | "sample" | null;
  edited: boolean;
  maxRepairs: number;
  onMaxRepairsChange: (n: number) => void;
  onPlan: () => void;
  planning: boolean;
  onRestructure: () => void;
  restructuring: boolean;
  canRestructure: boolean;
  /** Additive: the task graph of the current plan; shows per-structure quantities, hours and Wh. */
  tasks?: Task[] | null;
  /** Additive: the brief in the textarea is not the one these requirements were structured
   *  from (another example loaded, or the text edited): planning waits for a re-structure. */
  staleBrief?: boolean;
  /** Additive: why this backend cannot plan the card at all (a recorded-answers backend with no
   *  recording of the brief the card came from); the plan button is off and the reason shows
   *  above it. Null or absent: planning is possible. */
  planBlocked?: string | null;
  /** Additive: the static demo replays a recording: the card is read-only with this reason in
   *  its footer, Re-structure is not offered, and the plan button replays the recorded plan.
   *  Null or absent: the card is an editor. */
  recorded?: string | null;
}

/** The card is read-only (the static demo): the pieces that edit it render nothing. */
const LockedContext = createContext(false);

/** Keys a dimension can never take: the identity fields and the operations list. */
const RESERVED_KEYS = ["id", "type", "zone", "operations"];
const COMMON_DIMENSIONS = [
  "diameter_m",
  "length_m",
  "width_m",
  "depth_m",
  "height_m",
  "thickness_m",
  "trench_width_m",
  "trench_depth_m",
  // the smallest size a brief allows ("any diameter between 12 m and 20 m"): the planner may
  // shrink the work down to it (backend rates.py Shape.floor_m3)
  "min_diameter_m",
  "min_length_m",
  "min_width_m",
  "min_depth_m",
  "min_height_m",
];
/** One-line caption of the structures group, shown once above the rows. */
const OPERATIONS_CAPTION =
  "Operations the brief asks for; the solver requires a task of the right robot type for each one";

const isPosInt = (n: number) => Number.isInteger(n) && n > 0;
const isPos = (n: number) => Number.isFinite(n) && n > 0;
/** A dimension the backend takes: any finite number, and a 0 is an absent part (a 0 m key
 *  trench is no trench, as the structuring model writes it); a negative one is a typo. */
const isDim = (n: number) => Number.isFinite(n) && n >= 0;
/** Ids must match the backend's ID_PATTERN (letters, digits, _ . -): no spaces, no "#". */
const isId = (id: string) => ID_PATTERN.test(id);
const fmtLimit = (n: number) => n.toLocaleString("en-US");

/** Client-side checks mirroring the backend's pydantic constraints: numbers > 0 and within the
 *  MAX_* limits, ids unique and matching ID_PATTERN (what the server would answer 422 to), and
 *  structure dimensions of 0 or more (the server takes any finite one; a negative is a typo). */
export function validateRequirements(req: Requirements): string[] {
  const problems: string[] = [];
  if (!req.site.name.trim()) problems.push("site name is empty");
  if (!isPosInt(req.site.grid[0]) || !isPosInt(req.site.grid[1])) {
    problems.push("grid size must be positive whole numbers");
  }
  if (!isPosInt(req.horizon_h)) problems.push("horizon must be a positive whole number of hours");
  else if (req.horizon_h > LIMITS.horizonH) problems.push(`horizon must be at most ${fmtLimit(LIMITS.horizonH)} h`);

  if (req.robots.length === 0) problems.push("at least one robot is required");
  const robotIds = new Set<string>();
  req.robots.forEach((r, i) => {
    const label = r.id.trim() || `robot #${i + 1}`;
    if (!r.id.trim()) problems.push(`robot #${i + 1} has no id`);
    else if (!isId(r.id)) problems.push(`robot id "${r.id}": letters, digits, _ . - only`);
    else if (robotIds.has(r.id)) problems.push(`duplicate robot id ${r.id}`);
    robotIds.add(r.id);
    if (!r.type.trim()) problems.push(`${label} has no type`);
    if (!isPos(r.battery_wh)) problems.push(`${label}: battery must be > 0 Wh`);
    else if (r.battery_wh > LIMITS.energyWh) problems.push(`${label}: battery must be at most ${fmtLimit(LIMITS.energyWh)} Wh`);
    if (!isPos(r.speed_cells_per_h)) problems.push(`${label}: speed must be > 0`);
    else if (r.speed_cells_per_h > LIMITS.speedCellsPerH) problems.push(`${label}: speed must be at most ${fmtLimit(LIMITS.speedCellsPerH)} cells/h`);
  });

  const zoneIds = new Set(req.site.zones.map((z) => z.id));
  const structureIds = new Set<string>();
  req.structures.forEach((s, i) => {
    const label = s.id.trim() || `structure #${i + 1}`;
    if (!s.id.trim()) problems.push(`structure #${i + 1} has no id`);
    else if (!isId(s.id)) problems.push(`structure id "${s.id}": letters, digits, _ . - only`);
    else if (structureIds.has(s.id)) problems.push(`duplicate structure id ${s.id}`);
    structureIds.add(s.id);
    if (!s.type.trim()) problems.push(`${label} has no type`);
    if (s.zone && !zoneIds.has(s.zone)) problems.push(`${label}: zone ${s.zone} is not on the site`);
    // operations are a closed vocabulary server-side (a 422 otherwise); toggling a chip rewrites
    // the list normalised, so only a hand-edited or foreign list can get here
    const ops: unknown = s.operations;
    if (ops !== undefined && !Array.isArray(ops)) problems.push(`${label}: operations must be a list`);
    else if (Array.isArray(ops)) {
      const unknown = ops.filter((op) => !isOperation(op)).map(String);
      if (unknown.length > 0) {
        problems.push(`${label}: unknown operation ${unknown.join(", ")} (one of ${OPERATIONS.join(", ")})`);
      }
    }
    for (const [k, v] of structureDims(s)) {
      if (!isDim(v)) problems.push(`${label}: ${dimLabel(k)} must be 0 or more`);
    }
  });
  return problems;
}

// ------------------------------------------------------------------ prose helpers
const DIM_PHRASES: Record<string, (v: string) => string> = {
  diameter_m: (v) => `${v} m diameter`,
  radius_m: (v) => `${v} m radius`,
  length_m: (v) => `${v} m long`,
  width_m: (v) => `${v} m wide`,
  depth_m: (v) => `${v} m deep`,
  height_m: (v) => `${v} m high`,
  thickness_m: (v) => `${v} m thick`,
  trench_width_m: (v) => `trench ${v} m wide`,
  trench_depth_m: (v) => `trench ${v} m deep`,
  lifts: (v) => (v === "1" ? "1 lift" : `${v} lifts`),
};

/** "20 m diameter · min diameter 12 m · 0.4 m deep · 0.3 m thick" from a structure's numeric
 *  extras; a berm's key trench reads as one phrase, "trench 1.2 m wide × 0.8 m deep", when both
 *  its numbers exist; the smallest size a brief allows (min_<dimension>_m) follows the
 *  dimension it bounds. */
export function structureSummary(s: Structure): string {
  const dims = structureDims(s);
  const byKey = new Map(dims);
  const trenchWidth = byKey.get("trench_width_m");
  const trenchDepth = byKey.get("trench_depth_m");
  const trenchPair = trenchWidth !== undefined && trenchDepth !== undefined;
  let trenchDone = false;
  const parts: string[] = [];
  for (const [k, v] of dims) {
    const bounds = minDimension(k);
    if (bounds) {
      // printed right after the dimension it bounds; on its own when that one is missing
      if (!byKey.has(bounds)) parts.push(dimPhrase(k, v) ?? `${fmtNum(v, 2)} ${k}`);
      continue;
    }
    if (trenchPair && (k === "trench_width_m" || k === "trench_depth_m")) {
      // a 0 × 0 trench is no trench (the structuring model writes the keys either way)
      if (!trenchDone && (trenchWidth !== 0 || trenchDepth !== 0)) {
        parts.push(`trench ${fmtNum(trenchWidth, 2)} m wide × ${fmtNum(trenchDepth, 2)} m deep`);
      }
      trenchDone = true;
      continue;
    }
    const num = fmtNum(v, 2);
    const phrase = DIM_PHRASES[k];
    if (phrase) parts.push(phrase(num));
    else {
      const m = /^(.*)_(m|m2|m3|cm|mm|kg|t|wh|kwh|h|deg)$/i.exec(k);
      const base = (m ? m[1] : k).replace(/_/g, " ");
      parts.push(m ? `${num} ${m[2]} ${base}` : `${num} ${base}`);
    }
    const least = byKey.get(`min_${k}`);
    const floor = least === undefined ? null : dimPhrase(`min_${k}`, least);
    if (floor) parts.push(floor);
  }
  return parts.join(" · ");
}

const UNIT_LABELS: Record<Unit, string> = { m3: "m³", m2: "m²", m: "m", ea: "ea" };

/** The operation a task performs, read as the validator reads it (backend validate.py
 *  task_phases): by its robot type, and an excavator by its unit (m³ or m of cut is
 *  excavation, m² is grading); null for a robot type outside the closed vocabulary. */
export function taskOperation(t: Task): Operation | null {
  switch (t.robot_type) {
    case "surveyor":
      return "survey";
    case "hauler":
      return "haul";
    case "compactor":
      return "compact";
    case "printer":
      return "sinter";
    case "excavator":
      return t.unit === "m2" ? "grade" : "excavate";
    default:
      return null;
  }
}

/** "6 tasks · 31 robot-hours · 2,882 Wh · survey 160 m² · haul 2 × 60 m³ · compact 160 m² ·
 *  sinter 145 m²" for the tasks of one structure: the work summed over its tasks, as the task
 *  table counts it, so it never reads like the makespan. Quantities are grouped by operation
 *  and unit in work order, so excavated m³ and surveyed or compacted m² never add up into one
 *  figure; equal quantities read as a count (two passes over a pad are not summed into an area
 *  larger than the pad), unequal ones of one operation are summed. */
export function structureWork(tasks: Task[]): string | null {
  if (tasks.length === 0) return null;
  const hours = tasks.reduce((a, t) => a + t.duration_h, 0);
  const wh = tasks.reduce((a, t) => a + t.energy_wh, 0);
  const groups = new Map<string, { label: string; rank: number; unit: Unit; quantities: number[] }>();
  for (const t of tasks) {
    if (t.quantity === null || !t.unit) continue;
    const op = taskOperation(t);
    const key = `${op ?? t.robot_type}|${t.unit}`;
    const g = groups.get(key) ?? {
      label: op ? operationLabel(op) : t.robot_type,
      rank: op ? OPERATIONS.indexOf(op) : OPERATIONS.length,
      unit: t.unit,
      quantities: [],
    };
    g.quantities.push(t.quantity);
    groups.set(key, g);
  }
  const quantities = [...groups.values()]
    .sort((a, b) => a.rank - b.rank)
    .map(({ label, unit, quantities: qs }) => {
      const same = qs.length > 1 && qs.every((q) => Math.abs(q - qs[0]) < 0.05);
      const figure = same ? `${qs.length} × ${fmtNum(qs[0], 1)}` : fmtNum(qs.reduce((a, q) => a + q, 0), 1);
      return `${label} ${figure} ${UNIT_LABELS[unit]}`;
    })
    .join(" · ");
  const robotHours = `${fmtNum(hours, 1)}\u00a0${hours === 1 ? "robot-hour" : "robot-hours"}`;
  return `${plural(tasks.length, "task")} · ${robotHours} · ${fmtWh(wh)}${quantities ? ` · ${quantities}` : ""}`;
}

// ------------------------------------------------------------------ geometry
export interface GeometryNote {
  /** fail: the backend's hard rule (POST /plan answers 422), red; warn: for information, muted */
  level: "warn" | "fail";
  text: string;
}

/** Geometry checks drawn from the current site. "fail" mirrors the backend's hard rules
 *  (schemas.py Site: a zone centre or the depot outside the grid, a radius over a quarter of
 *  the grid's shorter side); everything else is a muted note: a zone disc that reaches past
 *  the grid edge (the planner and the simulator use its centre, so the backend plans it
 *  without a word), overlapping zones, structures without a declared zone, a missing depot.
 *  The backend reports the muted kinds on the structure step too. */
export function geometryNotes(site: Site, structures: Structure[]): GeometryNote[] {
  const notes: GeometryNote[] = [];
  const [gx, gy] = site.grid;
  const zones = site.zones;
  const maxRadius = Math.max(1, Math.floor(Math.min(gx, gy) / 4));
  const inside = (x: number, y: number) => x >= 0 && y >= 0 && x < gx && y < gy;
  for (const z of zones) {
    const [cx0, cy0] = z.center;
    if (!inside(cx0, cy0)) {
      notes.push({ level: "fail", text: `zone ${z.id} centre (${cx0}, ${cy0}) is outside the ${gx} × ${gy} grid` });
      continue;
    }
    if (!(z.radius >= 1 && z.radius <= maxRadius)) {
      notes.push({
        level: "fail",
        text: `zone ${z.id} radius ${z.radius} is outside 1–${maxRadius} cells (at most a quarter of the grid's shorter side)`,
      });
      continue;
    }
    // y grows northward (the map draws north up)
    const over = [
      [cy0 + z.radius - gy, "north"],
      [z.radius - cy0, "south"],
      [cx0 + z.radius - gx, "east"],
      [z.radius - cx0, "west"],
    ]
      .filter((e): e is [number, string] => typeof e[0] === "number" && e[0] > 0)
      .map(([n, edge]) => `${plural(n, "cell")} past the ${edge} edge`);
    if (over.length > 0) notes.push({ level: "warn", text: `zone ${z.id} extends ${over.join(" and ")}; the planner uses its centre` });
  }
  for (let i = 0; i < zones.length; i++) {
    for (let j = i + 1; j < zones.length; j++) {
      const a = zones[i];
      const b = zones[j];
      const d = Math.hypot(a.center[0] - b.center[0], a.center[1] - b.center[1]);
      if (d < a.radius + b.radius) notes.push({ level: "warn", text: `zones ${a.id} and ${b.id} overlap` });
    }
  }
  const unzoned = structures.filter((s) => s.zone === null).map((s) => s.id || "?");
  if (unzoned.length > 0) {
    notes.push({ level: "warn", text: `${unzoned.join(", ")} ${unzoned.length === 1 ? "has" : "have"} no zone: the planner will auto-place it` });
  }
  if (!site.depot) notes.push({ level: "warn", text: "no depot: robots start at the bottom centre of the grid" });
  else if (!inside(site.depot[0], site.depot[1])) {
    notes.push({ level: "fail", text: `depot (${site.depot[0]}, ${site.depot[1]}) is outside the ${gx} × ${gy} grid` });
  }
  return notes;
}

/** Warnings the structure step carried (StructurePayload.warnings), if any. */
function stepWarnings(step: LoopStep | null): string[] {
  const w = step?.payload?.warnings;
  return Array.isArray(w) ? w.filter((x): x is string => typeof x === "string") : [];
}

// ------------------------------------------------------------------ structuring notes
/** The shapes of StructurePayload.warnings, told apart by their opening words: operations
 *  salvaged from a scrambled structuring answer, a structure without a single dimension, an
 *  operation no robot type can staff (a recording older than phase 3j) or one dropped for
 *  that reason (phase 3j), an operation the structure's own entry in the brief states that
 *  the structuring omitted, restored (phase 3l), an operation the brief line does not ask for,
 *  a lift count corrected to the brief (phase 3g), a recorded diameter corrected to the
 *  rectangle the brief states (phase 3i), a smaller size the brief allows and a figure it
 *  gives only as a distance (phase 3j), zone / depot geometry; anything else is "other". */
export type WarningShape =
  | "recovered"
  | "no_dimensions"
  | "unstaffed"
  | "dropped"
  | "restored"
  | "unsupported"
  | "lifts"
  | "footprint"
  | "minimum"
  | "distance"
  | "geometry"
  | "other";

export interface CardWarning {
  shape: WarningShape;
  /** warn (amber): act before planning; muted: for information, nothing to do */
  tone: "warn" | "muted";
  /** the short imperative an amber note leads with */
  lead: string | null;
  /** the structure the note names ("structure S2 ..."), null for a site-wide one */
  structure: string | null;
  /** the operations an unstaffed / dropped / restored / unsupported note is about */
  operations: Operation[];
  /** the sentence as shown, field names read as words and a length kept with its unit
   *  ("diameter 12 m -> 12 m x 12 m"): an amber note drops the clause its lead already says */
  text: string;
  raw: string;
}

export const WARNING_LEADS = {
  recovered: "Check these operations against the brief",
  restored: "Check the restored operations against the brief",
  no_dimensions: "Add the brief's dimensions before planning",
  lifts: "Check the lift count against the brief",
  footprint: "Check the footprint against the brief",
  minimum: "Check the smallest size against the brief",
  distance: "Check the dimensions against the brief",
} as const;

const WARNING_SHAPES: [WarningShape, RegExp][] = [
  ["recovered", /^structure \S+: operations \[[^\]]*\] recovered from malformed keys/],
  ["no_dimensions", /^structure \S+ (?:\([^)]*\) )?has no dimensions\b/],
  ["unstaffed", /^structure \S+ lists \S+ but the fleet has no /],
  ["dropped", /^structure \S+: operations .+? dropped: the fleet has no /],
  ["restored", /^structure \S+: operations .+? restored: its entry states /],
  ["unsupported", /^structure \S+ lists \S+ but the brief line about it asks for /],
  ["lifts", /^structure \S+: (?:lifts|lift_count|compaction_lifts) \S+ -> \d+: /],
  ["footprint", /^structure \S+: (?:min_)?diameter_m [^:]*-> \S+ m x \S+ m(?:, at least \S+ m x \S+ m)?: /],
  ["minimum", /^structure \S+: min_\w+_m none -> \S+: /],
  ["distance", /^structure \S+: \w+_m \S+ dropped: the brief's entry for \S+ states \S+ m only as a distance/],
  ["geometry", /^(?:zones \S+ and \S+ overlap\b|structure \S+ (?:has no zone\b|names zone )|no depot\b)/],
];

/** One warning line of the structure step, classified by its prefix. */
export function classifyWarning(raw: string): CardWarning {
  const text = raw.trim();
  const shape = WARNING_SHAPES.find(([, re]) => re.test(text))?.[0] ?? "other";
  const structure = /^structure (\S+?):?(?:\s|$)/.exec(text)?.[1] ?? null;
  // "lists sinter but ..." (unstaffed, unsupported); "operations survey and sinter dropped";
  // "operations haul and compact restored"
  const named =
    /^structure \S+ lists (\S+) but /.exec(text)?.[1] ??
    /^structure \S+: operations (.+?) dropped: /.exec(text)?.[1] ??
    /^structure \S+: operations (.+?) restored: /.exec(text)?.[1] ??
    "";
  const operations = named.split(/,\s*|\s+and\s+/).filter(isOperation);
  const lead =
    shape === "recovered"
      ? WARNING_LEADS.recovered
      : shape === "restored"
        ? WARNING_LEADS.restored
        : shape === "no_dimensions"
          ? WARNING_LEADS.no_dimensions
          : shape === "lifts"
            ? WARNING_LEADS.lifts
            : shape === "footprint"
              ? WARNING_LEADS.footprint
              : shape === "minimum"
                ? WARNING_LEADS.minimum
                : shape === "distance"
                  ? WARNING_LEADS.distance
                  : null;
  const shown =
    shape === "recovered"
      ? text.replace(/;\s*check them against the brief\.?$/, "")
      : shape === "no_dimensions"
        ? text.replace(/,\s*so add the brief's numbers before planning\.?$/, "")
        : text;
  // a narrow column wraps a note often: never between a length and its unit ("12 / m")
  const words = humanizeFields(shown).replace(/(\d) (m|m2|m3)\b/g, "$1\u00a0$2");
  return { shape, tone: lead ? "warn" : "muted", lead, structure, operations, text: words, raw };
}

/** The structure step's warnings as the card lists them: amber notes first, order kept. */
export function cardWarnings(step: LoopStep | null): CardWarning[] {
  const all = stepWarnings(step).map(classifyWarning);
  return [...all.filter((w) => w.tone === "warn"), ...all.filter((w) => w.tone === "muted")];
}

/** The structure with `op` added to (or removed from) its operations: every other key is kept
 *  (dimensions and unknown extras round-trip to POST /plan) and the list is written normalised
 *  (closed vocabulary, no repeats, order of mention kept), which is what the backend accepts. */
export function toggleOperation(s: Structure, op: Operation): Structure {
  const ops = structureOperations(s);
  return { ...s, operations: ops.includes(op) ? ops.filter((o) => o !== op) : [...ops, op] };
}

function nextId(prefix: string, existing: string[]): string {
  for (let n = 1; ; n++) {
    const id = `${prefix}${n}`;
    if (!existing.includes(id)) return id;
  }
}

function withDim(s: Structure, key: string, value: number): Structure {
  const next: Structure = { ...s };
  next[key] = value;
  return next;
}

function omitKey(s: Structure, key: string): Structure {
  return Object.fromEntries(Object.entries(s).filter(([k]) => k !== key)) as Structure;
}

export function RequirementsCard({
  value,
  onChange,
  structureStep,
  source,
  edited,
  maxRepairs,
  onMaxRepairsChange,
  onPlan,
  planning,
  onRestructure,
  restructuring,
  canRestructure,
  tasks,
  staleBrief = false,
  planBlocked = null,
  recorded = null,
}: RequirementsCardProps) {
  const locked = recorded !== null;
  const problems = validateRequirements(value);
  const repairsOk = Number.isInteger(maxRepairs) && maxRepairs >= 0 && maxRepairs <= 10;
  const zoneIds = value.site.zones.map((z) => z.id);
  const robotIds = value.robots.map((r) => r.id);
  const duplicateRobots = new Set(robotIds.filter((id, i) => robotIds.indexOf(id) !== i));
  const structureIds = value.structures.map((s) => s.id);
  const duplicateStructures = new Set(structureIds.filter((id, i) => structureIds.indexOf(id) !== i));
  const notes = geometryNotes(value.site, value.structures);
  const warnings = cardWarnings(structureStep);
  const warningsByStructure = new Map<string, CardWarning[]>();
  for (const w of warnings) {
    if (w.structure) warningsByStructure.set(w.structure, [...(warningsByStructure.get(w.structure) ?? []), w]);
  }
  const tasksByStructure = useMemo(() => {
    const m = new Map<string, Task[]>();
    for (const t of tasks ?? []) {
      if (!t.structure) continue;
      const list = m.get(t.structure) ?? [];
      list.push(t);
      m.set(t.structure, list);
    }
    return m;
  }, [tasks]);

  const update = (patch: Partial<Requirements>) => onChange({ ...value, ...patch });
  const updateSite = (patch: Partial<Site>) => update({ site: { ...value.site, ...patch } });
  const setRobot = (i: number, patch: Partial<Robot>) =>
    update({ robots: value.robots.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const removeRobot = (i: number) => update({ robots: value.robots.filter((_, j) => j !== i) });
  const addRobot = () =>
    update({
      robots: [
        ...value.robots,
        { id: nextId("R", robotIds), type: "excavator", battery_wh: 3000, speed_cells_per_h: 12 },
      ],
    });
  const setStructure = (i: number, next: Structure) =>
    update({ structures: value.structures.map((s, j) => (j === i ? next : s)) });
  const removeStructure = (i: number) =>
    update({ structures: value.structures.filter((_, j) => j !== i) });
  const addStructure = () =>
    update({
      structures: [
        ...value.structures,
        { id: nextId("S", structureIds), type: "", zone: zoneIds[0] ?? null },
      ],
    });
  const setConstraint = (i: number, text: string) =>
    update({ constraints: value.constraints.map((c, j) => (j === i ? text : c)) });
  const removeConstraint = (i: number) =>
    update({ constraints: value.constraints.filter((_, j) => j !== i) });
  const addConstraint = () => update({ constraints: [...value.constraints, ""] });

  const usage = structureStep?.usage ?? null;
  // a sample recorded from a live run shows its structuring call like a fresh one
  const recordedLive = source === "sample" && Boolean(usage?.model) && !isFixtureModel(usage?.model);
  const badges = (
    <>
      {source === "sample" && <Badge tone="info">{recordedLive ? "recorded live run" : "from the bundled sample"}</Badge>}
      {(source === "model" || recordedLive) && structureStep && (
        <Badge title={modelTitle(usage?.model)}>
          {usage?.model ? modelDisplayName(usage.model) : "recorded answer"}
          {" · "}
          {/* the model's own latency, as the brief line and the trace quote it */}
          {fmtSeconds(usage?.latency_s ?? structureStep.duration_s)}
          {usage && usage.input_tokens + usage.output_tokens > 0 && (
            <>
              {" · "}
              {fmtCompact(usage.input_tokens)}/{fmtCompact(usage.output_tokens)} tok
            </>
          )}
        </Badge>
      )}
      {edited && <Badge tone="info">edited by you</Badge>}
    </>
  );

  const typeCounts = new Map<string, number>();
  for (const r of value.robots) typeCounts.set(r.type || "?", (typeCounts.get(r.type || "?") ?? 0) + 1);
  // exact, as the validator compares robot types
  const fleetTypes = new Set(value.robots.map((r) => r.type));

  return (
    <Section
      id="requirements"
      step="02"
      title="Requirements"
      subtitle={
        locked
          ? "What the model understood from the brief, as recorded: the card the planner was given. With the backend you correct ids, dimensions, batteries or the horizon here before planning."
          : "What the model understood from the brief. Correct ids, dimensions, batteries or the horizon here: the planner only ever sees this card."
      }
      actions={badges}
    >
      <Card bodyClassName="p-0">
        <datalist id="robot-types">
          {Object.keys(ROBOT_TYPE_COLORS).map((t) => (
            <option key={t} value={t} />
          ))}
        </datalist>
        <datalist id="dimension-keys">
          {COMMON_DIMENSIONS.map((k) => (
            <option key={k} value={k} />
          ))}
        </datalist>

        {warnings.length > 0 && (
          <div className="border-b border-line px-4 py-2.5 md:px-5">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                Structuring notes · {warnings.length}
              </h3>
              <span className="text-[11px] text-dim">what the structuring stage flagged; none of these stops planning</span>
            </div>
            <ul className="mt-1.5 space-y-1 text-[12px] leading-snug">
              {warnings.map((w, i) => (
                <li key={i} className="flex items-start gap-2">
                  <Dot color={w.tone === "warn" ? "var(--warn)" : "var(--fg-dim)"} className="mt-1" />
                  <span className={w.tone === "warn" ? "text-muted" : "text-dim"}>
                    {w.lead && (
                      <>
                        <span className="font-medium text-warn">{w.lead}</span>
                        {" · "}
                      </>
                    )}
                    {w.text}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* read-only: the fieldset disables every control at once and mw-locked keeps them at
            full contrast (they are the content); the edit affordances read LockedContext */}
        <LockedContext.Provider value={locked}>
        <fieldset disabled={locked} className={cx("min-w-0", locked && "mw-locked")}>
        <div className="grid grid-cols-[minmax(0,1fr)] gap-6 p-4 md:p-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <div className="space-y-6">
            <Group title="Site">
              {/* the map goes under the fields on a phone, where beside them it leaves the
                  grid inputs too narrow to show their values */}
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-[minmax(0,1fr)_auto]">
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Site name" className="col-span-2">
                    <TextInput
                      value={value.site.name}
                      onChange={(e) => updateSite({ name: e.target.value })}
                      invalid={!value.site.name.trim()}
                    />
                  </Field>
                  <Field label="Grid (cells)" as="div">
                    <div className="flex items-center gap-1.5">
                      <NumberInput
                        aria-label="grid width"
                        value={value.site.grid[0]}
                        min={1}
                        step={1}
                        invalid={!isPosInt(value.site.grid[0])}
                        onChange={(n) => updateSite({ grid: [n, value.site.grid[1]] })}
                      />
                      <span className="text-dim">×</span>
                      <NumberInput
                        aria-label="grid height"
                        value={value.site.grid[1]}
                        min={1}
                        step={1}
                        invalid={!isPosInt(value.site.grid[1])}
                        onChange={(n) => updateSite({ grid: [value.site.grid[0], n] })}
                      />
                    </div>
                  </Field>
                  <Field
                    label="Horizon (h)"
                    hint={isPosInt(value.horizon_h) ? fmtHorizon(value.horizon_h) : undefined}
                    error={
                      !isPosInt(value.horizon_h)
                        ? "positive whole hours"
                        : value.horizon_h > LIMITS.horizonH
                          ? `at most ${fmtLimit(LIMITS.horizonH)} h`
                          : undefined
                    }
                  >
                    <NumberInput
                      value={value.horizon_h}
                      min={1}
                      max={LIMITS.horizonH}
                      step={1}
                      invalid={!isPosInt(value.horizon_h) || value.horizon_h > LIMITS.horizonH}
                      onChange={(n) => update({ horizon_h: n })}
                    />
                  </Field>
                </div>
                <SiteMap site={value.site} structures={value.structures} notes={notes} />
              </div>
              {notes.length > 0 && (
                <ul className="mt-2 space-y-0.5 text-[11px]">
                  {notes.map((n, i) => (
                    <li key={`n${i}`} className={n.level === "fail" ? "text-fail" : "text-dim"}>
                      geometry · {n.text}
                    </li>
                  ))}
                </ul>
              )}
            </Group>

            <Group
              title={`Constraints · ${value.constraints.length}`}
              action={
                <Button size="sm" variant="ghost" onClick={addConstraint}>
                  + Add
                </Button>
              }
            >
              {value.constraints.length === 0 && (
                <p className="text-xs text-dim">No free-text constraints.</p>
              )}
              <ul className="space-y-1.5">
                {value.constraints.map((c, i) => (
                  <li key={i} className="flex items-start gap-1.5">
                    <ConstraintInput value={c} onChange={(text) => setConstraint(i, text)} label={`constraint ${i + 1}`} />
                    <RemoveButton onClick={() => removeConstraint(i)} label={`remove constraint ${i + 1}`} />
                  </li>
                ))}
              </ul>
            </Group>
          </div>

          <div className="space-y-6">
            <Group
              title={`Structures · ${value.structures.length}`}
              caption={value.structures.length > 0 ? OPERATIONS_CAPTION : undefined}
              action={
                <Button size="sm" variant="ghost" onClick={addStructure}>
                  + Add structure
                </Button>
              }
            >
              {value.structures.length === 0 && (
                <p className="text-xs text-dim">No structures: the plan would have nothing to build.</p>
              )}
              <ul className="space-y-2">
                {value.structures.map((s, i) => (
                  <StructureRow
                    key={i}
                    structure={s}
                    index={i}
                    zoneIds={zoneIds}
                    fleetTypes={fleetTypes}
                    duplicate={duplicateStructures.has(s.id)}
                    work={tasks ? structureWork(tasksByStructure.get(s.id) ?? []) : null}
                    hasPlan={Boolean(tasks)}
                    warnings={warningsByStructure.get(s.id) ?? []}
                    onChange={(next) => setStructure(i, next)}
                    onRemove={() => removeStructure(i)}
                  />
                ))}
              </ul>
            </Group>

            <Group
              title={
                <>
                  Robots · {value.robots.length}
                  {typeCounts.size > 0 && (
                    <span className="ml-2 font-normal normal-case tracking-normal text-dim">
                      {[...typeCounts].map(([t, n]) => `${n} ${t}`).join(" · ")}
                    </span>
                  )}
                </>
              }
              action={
                <Button size="sm" variant="ghost" onClick={addRobot}>
                  + Add robot
                </Button>
              }
            >
              {/* narrower columns on a phone, so the read-only card fits its width (an
                  editable one adds the remove column and may scroll) */}
              <div className="overflow-x-auto">
                <table className="w-full border-separate border-spacing-y-1 text-xs sm:min-w-[520px]">
                  <thead>
                    <tr className="text-left text-[11px] uppercase tracking-wider text-dim">
                      <th className="px-1 font-medium">Id</th>
                      <th className="px-1 font-medium">Type</th>
                      <th className="px-1 font-medium">Battery (Wh)</th>
                      <th className="px-1 font-medium">Speed (cells/h)</th>
                      {!locked && <th className="px-1" />}
                    </tr>
                  </thead>
                  <tbody>
                    {value.robots.map((r, i) => (
                      <tr key={i}>
                        <td className="w-14 px-1 sm:w-24">
                          <TextInput
                            value={r.id}
                            onChange={(e) => setRobot(i, { id: e.target.value })}
                            aria-label={`robot ${i + 1} id`}
                            className="font-mono"
                            invalid={!r.id.trim() || !isId(r.id) || duplicateRobots.has(r.id)}
                            title={isId(r.id) || !r.id.trim() ? undefined : "letters, digits, _ . - only"}
                          />
                        </td>
                        <td className="px-1">
                          <div className="flex items-center gap-2">
                            <Dot color={robotTypeColor(r.type)} />
                            <TextInput
                              value={r.type}
                              list="robot-types"
                              onChange={(e) => setRobot(i, { type: e.target.value })}
                              aria-label={`robot ${i + 1} type`}
                              invalid={!r.type.trim()}
                            />
                          </div>
                        </td>
                        <td className="w-[4.5rem] px-1 sm:w-32">
                          <NumberInput
                            value={r.battery_wh}
                            min={1}
                            max={LIMITS.energyWh}
                            step={100}
                            invalid={!isPos(r.battery_wh) || r.battery_wh > LIMITS.energyWh}
                            onChange={(n) => setRobot(i, { battery_wh: n })}
                            aria-label={`robot ${i + 1} battery in Wh`}
                          />
                        </td>
                        <td className="w-16 px-1 sm:w-32">
                          <NumberInput
                            value={r.speed_cells_per_h}
                            min={0.1}
                            max={LIMITS.speedCellsPerH}
                            step={0.5}
                            invalid={!isPos(r.speed_cells_per_h) || r.speed_cells_per_h > LIMITS.speedCellsPerH}
                            onChange={(n) => setRobot(i, { speed_cells_per_h: n })}
                            aria-label={`robot ${i + 1} speed in cells per hour`}
                          />
                        </td>
                        {!locked && (
                          <td className="w-8 px-1">
                            <RemoveButton onClick={() => removeRobot(i)} label={`remove robot ${r.id || i + 1}`} />
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Group>
          </div>
        </div>
        </fieldset>
        </LockedContext.Provider>

        {/* a recorded card is not sent anywhere: the client's checks do not apply to it (the
            backend took it as it is) */}
        {problems.length > 0 && !locked && (
          <div className="border-t border-line px-4 py-3 md:px-5">
            <Notice tone="warn" title={`${plural(problems.length, "problem")} to fix before planning`}>
              {problems.slice(0, 4).join(" · ")}
              {problems.length > 4 ? ` · +${problems.length - 4} more` : ""}
            </Notice>
          </div>
        )}

        {staleBrief && (
          <div className="border-t border-line px-4 py-3 md:px-5">
            <Notice tone="warn" title="The brief changed since these requirements were structured">
              Re-structure the brief to refresh this card. Planning waits until then, so the planner never
              gets a card that came from another brief.
            </Notice>
          </div>
        )}

        {planBlocked && !staleBrief && (
          <div className="border-t border-line px-4 py-3 md:px-5">
            <Notice tone="neutral" title="Planning these requirements needs a live backend">
              {planBlocked}
            </Notice>
          </div>
        )}

        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3 md:px-5">
          <div className="flex flex-wrap items-center gap-3">
            <Field label="Repair rounds" className={cx("w-24", locked && "mw-locked")}>
              <NumberInput
                value={maxRepairs}
                min={0}
                max={10}
                step={1}
                invalid={!repairsOk}
                disabled={locked}
                onChange={onMaxRepairsChange}
              />
            </Field>
            <p className="max-w-xs text-[11px] leading-snug text-dim">
              {recorded ??
                "Infeasibility cores from the validator or CP-SAT go back to Nemotron up to this many times (0–10)."}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {/* a recorded card has nothing to re-structure (the reason line says what needs the
                backend), so the button is not offered at all */}
            {!locked && (
              <Button
                variant="secondary"
                onClick={onRestructure}
                loading={restructuring}
                disabled={!canRestructure || planning}
                title="Run the structuring stage again on the current brief (discards your edits)"
              >
                Re-structure
              </Button>
            )}
            <Button
              variant="primary"
              onClick={onPlan}
              loading={planning}
              disabled={
                (!locked && (problems.length > 0 || !repairsOk)) || restructuring || staleBrief || Boolean(planBlocked)
              }
              title={
                staleBrief
                  ? "Re-structure the brief first: these requirements came from another brief"
                  : (planBlocked ?? (locked ? "Replay the recorded run's planning: its trace, cores and repairs" : undefined))
              }
            >
              {locked ? "Replay planning" : planning ? "Planning…" : "Plan with these requirements"}
            </Button>
          </div>
        </footer>
      </Card>
    </Section>
  );
}

// ------------------------------------------------------------------ site map
const MAP_W = 176;
const MAP_MAX_H = 176;

/** The site as the simulator lays it out: grid cells, zone circles in the zone colours with
 *  the structures they hold, the depot as a diamond; north is up (y grows northward). */
function SiteMap({ site, structures, notes }: { site: Site; structures: Structure[]; notes: GeometryNote[] }) {
  const [gx, gy] = site.grid;
  if (!isPosInt(gx) || !isPosInt(gy)) {
    return (
      <div className="flex h-24 w-44 items-center justify-center rounded-md border border-dashed border-fail/40 text-center text-[11px] text-fail">
        grid must be positive whole numbers to draw the site
      </div>
    );
  }
  const s = Math.min(MAP_W / gx, MAP_MAX_H / gy);
  const W = Math.max(96, Math.round(gx * s));
  const H = Math.max(96, Math.round(gy * s));
  const sx = W / gx;
  const sy = H / gy;
  const px = (x: number) => x * sx;
  const py = (y: number) => H - y * sy;
  const zoneIds = site.zones.map((z) => z.id);
  const bad = new Set(
    notes
      .filter((n) => n.level === "fail")
      .flatMap((n) => zoneIds.filter((id) => n.text.includes(`zone ${id} `))),
  );
  const gridStep = gx <= 20 && gy <= 20 ? 5 : 10;
  const byZone = new Map<string, string[]>();
  for (const st of structures) {
    if (!st.zone) continue;
    byZone.set(st.zone, [...(byZone.get(st.zone) ?? []), st.id || "?"]);
  }
  const summary = `${gx} × ${gy} cells · ${plural(site.zones.length, "zone")}${
    site.depot ? ` · depot (${site.depot[0]}, ${site.depot[1]})` : " · no depot"
  }`;

  // Label layout. A zone's id (and the structures it holds, one line below) sit on its centre,
  // kept inside the map; when the depot sits on that spot the labels move beside the disc. The
  // depot's own label takes the free side of its diamond: right, else left, else below.
  const CHAR_W = 6.7; // 11 px Geist Mono
  const depot = site.depot ? { x: px(site.depot[0]), y: py(site.depot[1]) } : null;
  const clampY = (y: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, y));
  type Box = { x0: number; x1: number; y0: number; y1: number };
  const boxOf = (x: number, anchor: "start" | "middle" | "end", w: number, y0: number, y1: number): Box => {
    const x0 = anchor === "start" ? x : anchor === "end" ? x - w : x - w / 2;
    return { x0, x1: x0 + w, y0, y1 };
  };
  const overlaps = (a: Box, b: Box) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
  const zoneLabels = site.zones.map((z) => {
    const held = (byZone.get(z.id) ?? []).join(" ");
    const cx = px(z.center[0]);
    const cy = py(z.center[1]);
    const r = Math.max(z.radius * sx, 2);
    const idY = held ? clampY(cy - 5, 8, H - 17) : clampY(cy, 8, H - 6);
    const heldY = idY + 11;
    const w = Math.max(z.id.length, held.length) * CHAR_W;
    const bottom = (held ? heldY : idY) + 6;
    let x = cx;
    let anchor: "start" | "middle" | "end" = "middle";
    if (depot && overlaps(boxOf(cx, "middle", w, idY - 6, bottom), { x0: depot.x - 6, x1: depot.x + 6, y0: depot.y - 6, y1: depot.y + 6 })) {
      if (cx - r - 4 - w >= 0) {
        x = cx - r - 4;
        anchor = "end";
      } else {
        x = cx + r + 4;
        anchor = "start";
      }
    }
    return { id: z.id, held, x, anchor, idY, heldY, box: boxOf(x, anchor, w, idY - 6, bottom) };
  });
  const depotLabel = (() => {
    if (!depot) return null;
    const w = 5 * CHAR_W;
    const free = (b: Box) => b.x0 >= 0 && b.x1 <= W && b.y1 <= H && !zoneLabels.some((l) => overlaps(l.box, b));
    const y = clampY(depot.y, 8, H - 6);
    const right = { x: depot.x + 7, y, anchor: "start" as const };
    const left = { x: depot.x - 7, y, anchor: "end" as const };
    if (free(boxOf(right.x, "start", w, y - 6, y + 6))) return right;
    if (free(boxOf(left.x, "end", w, y - 6, y + 6))) return left;
    return { x: depot.x, y: Math.min(H - 4, depot.y + 14), anchor: "middle" as const };
  })();
  return (
    <figure className="shrink-0">
      <svg
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Site map: ${summary}; zones ${site.zones.map((z) => `${z.id} at (${z.center[0]}, ${z.center[1]}) radius ${z.radius}`).join(", ") || "none"}`}
        className="block rounded-md border border-white/10 bg-surface"
      >
        {Array.from({ length: Math.floor(gx / gridStep) }, (_, i) => (i + 1) * gridStep)
          .filter((v) => v < gx)
          .map((v) => (
            <line key={`vx${v}`} x1={px(v)} x2={px(v)} y1={0} y2={H} stroke="rgba(255,255,255,0.06)" />
          ))}
        {Array.from({ length: Math.floor(gy / gridStep) }, (_, i) => (i + 1) * gridStep)
          .filter((v) => v < gy)
          .map((v) => (
            <line key={`hy${v}`} x1={0} x2={W} y1={py(v)} y2={py(v)} stroke="rgba(255,255,255,0.06)" />
          ))}
        {site.zones.map((z, i) => {
          const color = zoneColor(z.id, zoneIds);
          const held = byZone.get(z.id) ?? [];
          const r = Math.max(z.radius * sx, 2);
          const label = zoneLabels[i];
          return (
            <g key={z.id}>
              <title>{`${z.id} · centre (${z.center[0]}, ${z.center[1]}) · radius ${z.radius}${held.length ? ` · ${held.join(", ")}` : " · no structure"}`}</title>
              <circle
                cx={px(z.center[0])}
                cy={py(z.center[1])}
                r={r}
                fill={color}
                fillOpacity={0.28}
                stroke={bad.has(z.id) ? "#ef4444" : color}
                strokeWidth={bad.has(z.id) ? 1.5 : 1}
                strokeDasharray={bad.has(z.id) ? "3 2" : undefined}
              />
              <text
                x={label.x}
                y={label.idY}
                textAnchor={label.anchor}
                dominantBaseline="central"
                fontSize={11}
                fontWeight={600}
                fill="#e6edf3"
                fontFamily="var(--font-mono)"
              >
                {z.id}
              </text>
              {held.length > 0 && (
                <text
                  x={label.x}
                  y={label.heldY}
                  textAnchor={label.anchor}
                  dominantBaseline="central"
                  fontSize={11}
                  fill="#9aa7b4"
                  fontFamily="var(--font-mono)"
                >
                  {label.held}
                </text>
              )}
            </g>
          );
        })}
        {site.depot && depot && depotLabel && (
          <g>
            <title>{`depot (${site.depot[0]}, ${site.depot[1]})`}</title>
            <rect
              x={depot.x - 4}
              y={depot.y - 4}
              width={8}
              height={8}
              transform={`rotate(45 ${depot.x} ${depot.y})`}
              fill="#0b0f14"
              stroke="#e6edf3"
              strokeWidth={1.5}
            />
            <text
              x={depotLabel.x}
              y={depotLabel.y}
              textAnchor={depotLabel.anchor}
              dominantBaseline={depotLabel.anchor === "middle" ? undefined : "central"}
              fontSize={11}
              fill="#9aa7b4"
              fontFamily="var(--font-mono)"
            >
              depot
            </text>
          </g>
        )}
        <text x={4} y={13} fontSize={11} fill="#7b8896" fontFamily="var(--font-mono)">
          N ↑
        </text>
      </svg>
      <figcaption className="mt-1.5 font-mono text-[11px] leading-snug text-dim" style={{ maxWidth: W }}>
        {summary}
      </figcaption>
    </figure>
  );
}

// ------------------------------------------------------------------ pieces
function Group({
  title,
  caption,
  action,
  children,
}: {
  title: ReactNode;
  /** one line under the title, e.g. what the operation chips mean */
  caption?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  const locked = useContext(LockedContext);
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
        {!locked && action}
      </div>
      {caption && <p className="-mt-1 mb-2 text-[11px] leading-snug text-dim">{caption}</p>}
      {children}
    </div>
  );
}

/** A constraint is a sentence: its field wraps and grows with it instead of cutting it off at
 *  the edge (the card is read whole, on a phone as well); Enter adds no line break, since the
 *  planner reads each constraint as one line. */
function ConstraintInput({ value, onChange, label }: { value: string; onChange: (text: string) => void; label: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => {
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
    };
    fit();
    // the column narrows on a phone or a resize: fit again when the width changes
    let width = el.clientWidth;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [value]);
  return (
    <textarea
      ref={ref}
      rows={1}
      value={value}
      onChange={(e) => onChange(e.target.value.replace(/\s*\n\s*/g, " "))}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.preventDefault();
      }}
      aria-label={label}
      className={cx(inputClass.replace("h-8 ", ""), "block resize-none overflow-hidden py-[5px] leading-5")}
    />
  );
}

function RemoveButton({ onClick, label }: { onClick: () => void; label: string }) {
  const locked = useContext(LockedContext);
  if (locked) return null;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="flex size-7 shrink-0 items-center justify-center rounded-md text-dim transition-colors hover:bg-fail/10 hover:text-fail"
    >
      <IconX className="size-3" />
    </button>
  );
}

const OP_CHIP = "inline-flex items-center rounded border px-1.5 py-px font-mono text-[11px] leading-[18px]";

/** The robot type an operation needs (backend validate.py _CANONICAL_TYPE): the coverage
 *  check never requires an operation the fleet has no robot of this type for. */
const OPERATION_ROBOT: Record<Operation, string> = {
  survey: "surveyor",
  excavate: "excavator",
  grade: "excavator",
  haul: "hauler",
  compact: "compactor",
  sinter: "printer",
};

function StructureRow({
  structure,
  index,
  zoneIds,
  fleetTypes,
  duplicate,
  work,
  hasPlan,
  warnings,
  onChange,
  onRemove,
}: {
  structure: Structure;
  index: number;
  zoneIds: string[];
  /** the robot types of the fleet as the card holds it now */
  fleetTypes: Set<string>;
  duplicate: boolean;
  /** "5 tasks · 46 h · 5,800 Wh · 126 m³" when a task graph exists. */
  work: string | null;
  hasPlan: boolean;
  /** the structuring notes that name this structure */
  warnings: CardWarning[];
  onChange: (next: Structure) => void;
  onRemove: () => void;
}) {
  const locked = useContext(LockedContext);
  const dims = structureDims(structure);
  const ops = structureOperations(structure);
  const [editingOps, setEditingOps] = useState(false);
  const unknownZone = structure.zone !== null && !zoneIds.includes(structure.zone);
  const invalidDim = dims.some(([, v]) => !isDim(v));
  const summary = structureSummary(structure);
  const name = structure.id || `structure ${index + 1}`;
  const recovered = warnings.some((w) => w.shape === "recovered");
  /** operation -> why no task will be required of it (no robot type of the fleet can staff
   *  it, or the brief line asks for another operation): the chip reads dashed with the note as
   *  tooltip. The fleet is read as the card holds it now, so the chip agrees with the coverage
   *  check after an edit (a printer removed, an operation listed again). */
  const notRequired = new Map<Operation, string>();
  for (const w of warnings) {
    if (w.shape === "unstaffed" || w.shape === "unsupported") {
      for (const op of w.operations) notRequired.set(op, w.text);
    }
  }
  for (const op of ops) {
    const robot = OPERATION_ROBOT[op];
    if (!fleetTypes.has(robot)) {
      notRequired.set(op, `the fleet has no ${robot}: no ${op} task will be required of it`);
    }
  }
  /** operations the structuring stage dropped (the fleet had no robot for them) and the card
   *  does not list again: shown dashed, the note as tooltip */
  const dropped = new Map<Operation, string>();
  for (const w of warnings) {
    if (w.shape !== "dropped") continue;
    for (const op of w.operations) if (!ops.includes(op)) dropped.set(op, w.text);
  }
  /** operations the structuring stage restored from the structure's own entry in the brief
   *  (phase 3l: the structuring had omitted them): the chip says so, the note as tooltip */
  const restored = new Map<Operation, string>();
  for (const w of warnings) {
    if (w.shape !== "restored") continue;
    for (const op of w.operations) restored.set(op, w.text);
  }
  return (
    <li className="rounded-md border border-white/10 bg-surface/60 p-3">
      {/* the zone column narrows on a phone, where the type needs the room */}
      <div className="grid grid-cols-[4rem_minmax(0,1fr)_5.5rem_auto] items-center gap-2 sm:grid-cols-[4rem_minmax(0,1fr)_7rem_auto]">
        <TextInput
          value={structure.id}
          onChange={(e) => onChange({ ...structure, id: e.target.value })}
          aria-label={`structure ${index + 1} id`}
          invalid={!structure.id.trim() || !isId(structure.id) || duplicate}
          title={isId(structure.id) || !structure.id.trim() ? undefined : "letters, digits, _ . - only"}
          className="font-mono"
        />
        <TextInput
          value={structure.type}
          onChange={(e) => onChange({ ...structure, type: e.target.value })}
          aria-label={`structure ${index + 1} type`}
          placeholder="type, e.g. landing_pad"
          invalid={!structure.type.trim()}
        />
        <div className="flex items-center gap-1.5">
          {structure.zone && !unknownZone && <Dot color={zoneColor(structure.zone, zoneIds)} />}
          <select
            aria-label={`structure ${index + 1} zone`}
            value={structure.zone ?? ""}
            onChange={(e) => onChange({ ...structure, zone: e.target.value || null })}
            aria-invalid={unknownZone || undefined}
            className={cx(inputClass, "font-mono")}
          >
            <option value="">no zone</option>
            {zoneIds.map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
            {unknownZone && structure.zone && <option value={structure.zone}>{structure.zone} (unknown)</option>}
          </select>
        </div>
        <RemoveButton onClick={onRemove} label={`remove structure ${structure.id || index + 1}`} />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1" role="group" aria-label={`operations of ${name}`}>
        <span className="mr-0.5 text-[11px] font-medium uppercase tracking-wider text-dim">operations</span>
        {editingOps
          ? OPERATIONS.map((op) => {
              const on = ops.includes(op);
              return (
                <button
                  key={op}
                  type="button"
                  aria-pressed={on}
                  onClick={() => onChange(toggleOperation(structure, op))}
                  className={cx(
                    OP_CHIP,
                    "transition-colors",
                    on
                      ? "border-white/30 bg-white/10 text-fg"
                      : "border-dashed border-white/15 text-dim hover:border-white/30 hover:text-fg",
                  )}
                >
                  {operationLabel(op)}
                </button>
              );
            })
          : ops.map((op) => {
              const note = notRequired.get(op);
              const from = restored.get(op);
              return (
                <span
                  key={op}
                  title={note ?? from}
                  className={cx(OP_CHIP, note ? "border-dashed border-white/15 text-dim" : "border-white/15 bg-white/[0.06] text-fg")}
                >
                  {operationLabel(op)}
                  {note ? " · not required" : from ? " · restored" : null}
                </span>
              );
            })}
        {!editingOps &&
          [...dropped].map(([op, note]) => (
            <span key={`dropped-${op}`} title={note} className={cx(OP_CHIP, "border-dashed border-white/15 text-dim")}>
              {operationLabel(op)} · dropped
            </span>
          ))}
        {!editingOps && ops.length === 0 && dropped.size === 0 && (
          <span className="text-[11px] text-dim">none stated in the brief</span>
        )}
        {!locked && (
          <button
            type="button"
            onClick={() => setEditingOps((v) => !v)}
            aria-expanded={editingOps}
            className="ml-0.5 text-[11px] text-dim hover:text-fg"
          >
            {editingOps ? "done" : "edit operations"}
          </button>
        )}
        {recovered && !editingOps && (
          <span className="ml-1 text-[11px] font-medium text-warn">{WARNING_LEADS.recovered}</span>
        )}
      </div>
      {summary ? (
        <p className="mt-2 text-[12.5px] text-fg">{summary}</p>
      ) : (
        <p className="mt-2 text-[12px] leading-snug">
          <span className="font-medium text-warn">{WARNING_LEADS.no_dimensions}</span>
          <span className="text-dim">
            {" · "}the planner sizes its tasks from numeric fields such as diameter, length, width, depth or
            height and never sees the brief
          </span>
        </p>
      )}
      {hasPlan && (
        <p className="mt-0.5 font-mono text-[11px] text-muted">
          {work ?? "no task in the graph builds this structure"}
        </p>
      )}
      {/* the summary line above already reads the dimensions out; the editor is for edits */}
      {!locked && (
        <details className="group mt-1.5" open={invalidDim || dims.length === 0 || undefined}>
          <summary className="cursor-pointer select-none text-[11px] text-dim hover:text-fg">
            {dims.length === 0 ? "add dimensions" : "edit dimensions"}
            {invalidDim ? <span className="ml-1 text-fail">· a dimension must be 0 or more</span> : null}
          </summary>
          <div className="mt-2 flex flex-wrap items-end gap-2">
            {dims.map(([key, v]) => (
              <Field key={key} label={dimLabel(key)} className="w-28" error={isDim(v) ? undefined : "must be 0 or more"}>
                <div className="flex items-center gap-0.5">
                  <NumberInput
                    value={v}
                    min={0}
                    step="any"
                    invalid={!isDim(v)}
                    onChange={(n) => onChange(withDim(structure, key, n))}
                  />
                  <RemoveButton onClick={() => onChange(omitKey(structure, key))} label={`remove ${dimLabel(key)}`} />
                </div>
              </Field>
            ))}
            <DimAdder existing={dims.map(([k]) => k)} onAdd={(key) => onChange(withDim(structure, key, 1))} />
          </div>
        </details>
      )}
    </li>
  );
}

function DimAdder({ existing, onAdd }: { existing: string[]; onAdd: (key: string) => void }) {
  const [text, setText] = useState("");
  const key = text
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  const ok = key.length > 0 && !existing.includes(key) && !RESERVED_KEYS.includes(key);
  const submit = () => {
    if (!ok) return;
    onAdd(key);
    setText("");
  };
  return (
    <div className="flex items-center gap-1">
      <TextInput
        value={text}
        list="dimension-keys"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        }}
        placeholder="add dimension, e.g. depth_m"
        aria-label="new dimension key"
        className="w-48"
      />
      <Button size="sm" variant="ghost" onClick={submit} disabled={!ok}>
        Add
      </Button>
    </div>
  );
}
