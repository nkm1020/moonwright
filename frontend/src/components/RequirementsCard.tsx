"use client";
/**
 * Step 02: what the model understood from the brief, editable. The site is drawn as a tiny
 * map (grid, zone circles, depot) so geometry mistakes are visible at a glance; every
 * structure reads as prose ("20 m diameter · 0.4 m deep") with the task-graph quantities
 * under it once a plan exists; validation mirrors the backend's pydantic limits.
 */
import { useMemo, useState, type ReactNode } from "react";
import type { LoopStep, Requirements, Robot, Site, Structure, Task, Unit } from "@/lib/types";
import { ID_PATTERN, LIMITS } from "@/lib/types";
import { ROBOT_TYPE_COLORS, robotTypeColor, zoneColor } from "@/lib/palette";
import {
  dimLabel,
  fmtCompact,
  fmtHorizon,
  fmtHours,
  fmtNum,
  fmtSeconds,
  fmtWh,
  plural,
  modelDisplayName,
  modelTitle,
  structureDims,
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
}

const RESERVED_KEYS = ["id", "type", "zone"];
const COMMON_DIMENSIONS = [
  "diameter_m",
  "length_m",
  "width_m",
  "depth_m",
  "height_m",
  "thickness_m",
  "trench_depth_m",
];

const isPosInt = (n: number) => Number.isInteger(n) && n > 0;
const isPos = (n: number) => Number.isFinite(n) && n > 0;
/** Ids must match the backend's ID_PATTERN (letters, digits, _ . -): no spaces, no "#". */
const isId = (id: string) => ID_PATTERN.test(id);
const fmtLimit = (n: number) => n.toLocaleString("en-US");

/** Client-side checks mirroring the backend's pydantic constraints: numbers > 0 and within the
 *  MAX_* limits, ids unique and matching ID_PATTERN (what the server would answer 422 to). */
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
    for (const [k, v] of structureDims(s)) {
      if (!isPos(v)) problems.push(`${label}: ${dimLabel(k)} must be > 0`);
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
  trench_depth_m: (v) => `${v} m trench`,
};

/** "20 m diameter · 0.4 m deep · 0.3 m thick" from a structure's numeric extras. */
export function structureSummary(s: Structure): string {
  const parts = structureDims(s).map(([k, v]) => {
    const num = fmtNum(v, 2);
    const phrase = DIM_PHRASES[k];
    if (phrase) return phrase(num);
    const m = /^(.*)_(m|m2|m3|cm|mm|kg|t|wh|kwh|h|deg)$/i.exec(k);
    const base = (m ? m[1] : k).replace(/_/g, " ");
    return m ? `${num} ${m[2]} ${base}` : `${num} ${base}`;
  });
  return parts.join(" · ");
}

const UNIT_LABELS: Record<Unit, string> = { m3: "m³", m2: "m²", m: "m", ea: "ea" };

/** "5 tasks · 46 h · 5,800 Wh · 126 m³ + 314 m²" for the tasks of one structure. */
export function structureWork(tasks: Task[]): string | null {
  if (tasks.length === 0) return null;
  const hours = tasks.reduce((a, t) => a + t.duration_h, 0);
  const wh = tasks.reduce((a, t) => a + t.energy_wh, 0);
  const byUnit = new Map<Unit, number>();
  for (const t of tasks) if (t.quantity !== null && t.unit) byUnit.set(t.unit, (byUnit.get(t.unit) ?? 0) + t.quantity);
  const quantities = [...byUnit].map(([u, q]) => `${fmtNum(q, 1)} ${UNIT_LABELS[u]}`).join(" + ");
  return `${plural(tasks.length, "task")} · ${fmtHours(hours, 1)} · ${fmtWh(wh)}${quantities ? ` · ${quantities}` : ""}`;
}

// ------------------------------------------------------------------ geometry
export interface GeometryNote {
  level: "warn" | "fail";
  text: string;
}

/** Soft geometry checks drawn from the current site: zones outside the grid or overlapping,
 *  structures without a declared zone, a missing depot. The backend reports the same kinds
 *  of warnings on the structure step. */
export function geometryNotes(site: Site, structures: Structure[]): GeometryNote[] {
  const notes: GeometryNote[] = [];
  const [gx, gy] = site.grid;
  const zones = site.zones;
  for (const z of zones) {
    const [cx0, cy0] = z.center;
    if (cx0 - z.radius < 0 || cy0 - z.radius < 0 || cx0 + z.radius > gx || cy0 + z.radius > gy) {
      notes.push({ level: "fail", text: `zone ${z.id} reaches outside the ${gx} × ${gy} grid` });
    }
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
  if (!site.depot) notes.push({ level: "warn", text: "no depot: robots start at the grid origin" });
  else if (site.depot[0] < 0 || site.depot[1] < 0 || site.depot[0] > gx || site.depot[1] > gy) {
    notes.push({ level: "fail", text: `depot (${site.depot[0]}, ${site.depot[1]}) is outside the grid` });
  }
  return notes;
}

/** Warnings the structure step carried (StructurePayload.warnings), if any. */
function stepWarnings(step: LoopStep | null): string[] {
  const w = step?.payload?.warnings;
  return Array.isArray(w) ? w.filter((x): x is string => typeof x === "string") : [];
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
}: RequirementsCardProps) {
  const problems = validateRequirements(value);
  const repairsOk = Number.isInteger(maxRepairs) && maxRepairs >= 0 && maxRepairs <= 10;
  const zoneIds = value.site.zones.map((z) => z.id);
  const robotIds = value.robots.map((r) => r.id);
  const duplicateRobots = new Set(robotIds.filter((id, i) => robotIds.indexOf(id) !== i));
  const structureIds = value.structures.map((s) => s.id);
  const duplicateStructures = new Set(structureIds.filter((id, i) => structureIds.indexOf(id) !== i));
  const notes = geometryNotes(value.site, value.structures);
  const warnings = stepWarnings(structureStep);
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
  const badges = (
    <>
      {source === "sample" && <Badge tone="info">from the bundled sample</Badge>}
      {source === "model" && structureStep && (
        <Badge title={modelTitle(usage?.model)}>
          {usage?.model ? modelDisplayName(usage.model) : "recorded answer"}
          {" · "}
          {fmtSeconds(structureStep.duration_s)}
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

  return (
    <Section
      id="requirements"
      step="02"
      title="Requirements"
      subtitle="What the model understood from the brief. Correct ids, dimensions, batteries or the horizon here: the planner only ever sees this card."
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

        <div className="grid grid-cols-[minmax(0,1fr)] gap-6 p-4 md:p-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <div className="space-y-6">
            <Group title="Site">
              <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-4">
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
              {(notes.length > 0 || warnings.length > 0) && (
                <ul className="mt-2 space-y-0.5 text-[11px]">
                  {notes.map((n, i) => (
                    <li key={`n${i}`} className={n.level === "fail" ? "text-fail" : "text-dim"}>
                      geometry · {n.text}
                    </li>
                  ))}
                  {warnings.map((w, i) => (
                    <li key={`w${i}`} className="text-dim">
                      structure step · {w}
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
                  <li key={i} className="flex items-center gap-1.5">
                    <TextInput
                      value={c}
                      onChange={(e) => setConstraint(i, e.target.value)}
                      aria-label={`constraint ${i + 1}`}
                    />
                    <RemoveButton onClick={() => removeConstraint(i)} label={`remove constraint ${i + 1}`} />
                  </li>
                ))}
              </ul>
            </Group>
          </div>

          <div className="space-y-6">
            <Group
              title={`Structures · ${value.structures.length}`}
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
                    duplicate={duplicateStructures.has(s.id)}
                    work={tasks ? structureWork(tasksByStructure.get(s.id) ?? []) : null}
                    hasPlan={Boolean(tasks)}
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
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] border-separate border-spacing-y-1 text-xs">
                  <thead>
                    <tr className="text-left text-[11px] uppercase tracking-wider text-dim">
                      <th className="px-1 font-medium">Id</th>
                      <th className="px-1 font-medium">Type</th>
                      <th className="px-1 font-medium">Battery (Wh)</th>
                      <th className="px-1 font-medium">Speed (cells/h)</th>
                      <th className="px-1" />
                    </tr>
                  </thead>
                  <tbody>
                    {value.robots.map((r, i) => (
                      <tr key={i}>
                        <td className="w-24 px-1">
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
                        <td className="w-32 px-1">
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
                        <td className="w-32 px-1">
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
                        <td className="w-8 px-1">
                          <RemoveButton onClick={() => removeRobot(i)} label={`remove robot ${r.id || i + 1}`} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Group>
          </div>
        </div>

        {problems.length > 0 && (
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

        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3 md:px-5">
          <div className="flex flex-wrap items-center gap-3">
            <Field label="Repair rounds" className="w-24">
              <NumberInput
                value={maxRepairs}
                min={0}
                max={10}
                step={1}
                invalid={!repairsOk}
                onChange={onMaxRepairsChange}
              />
            </Field>
            <p className="max-w-xs text-[11px] leading-snug text-dim">
              Infeasibility cores from the validator or CP-SAT go back to Nemotron up to this many
              times (0–10).
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              onClick={onRestructure}
              loading={restructuring}
              disabled={!canRestructure || planning}
              title="Run the structuring stage again on the current brief (discards your edits)"
            >
              Re-structure
            </Button>
            <Button
              variant="primary"
              onClick={onPlan}
              loading={planning}
              disabled={problems.length > 0 || !repairsOk || restructuring || staleBrief}
              title={staleBrief ? "Re-structure the brief first: these requirements came from another brief" : undefined}
            >
              {planning ? "Planning…" : "Plan with these requirements"}
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
        {site.zones.map((z) => {
          const color = zoneColor(z.id, zoneIds);
          const held = byZone.get(z.id) ?? [];
          const r = Math.max(z.radius * sx, 2);
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
                x={px(z.center[0])}
                y={py(z.center[1]) - (held.length ? 4 : 0)}
                textAnchor="middle"
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
                  x={px(z.center[0])}
                  y={py(z.center[1]) + 8}
                  textAnchor="middle"
                  dominantBaseline="central"
                  fontSize={10}
                  fill="#9aa7b4"
                  fontFamily="var(--font-mono)"
                >
                  {held.join(" ")}
                </text>
              )}
            </g>
          );
        })}
        {site.depot && (
          <g>
            <title>{`depot (${site.depot[0]}, ${site.depot[1]})`}</title>
            <rect
              x={px(site.depot[0]) - 4}
              y={py(site.depot[1]) - 4}
              width={8}
              height={8}
              transform={`rotate(45 ${px(site.depot[0])} ${py(site.depot[1])})`}
              fill="#0b0f14"
              stroke="#e6edf3"
              strokeWidth={1.5}
            />
            <text
              x={px(site.depot[0])}
              y={Math.min(H - 4, py(site.depot[1]) + 14)}
              textAnchor="middle"
              fontSize={10}
              fill="#9aa7b4"
              fontFamily="var(--font-mono)"
            >
              depot
            </text>
          </g>
        )}
        <text x={4} y={12} fontSize={10} fill="#66727f" fontFamily="var(--font-mono)">
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
function Group({ title, action, children }: { title: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-[12px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
        {action}
      </div>
      {children}
    </div>
  );
}

function RemoveButton({ onClick, label }: { onClick: () => void; label: string }) {
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

function StructureRow({
  structure,
  index,
  zoneIds,
  duplicate,
  work,
  hasPlan,
  onChange,
  onRemove,
}: {
  structure: Structure;
  index: number;
  zoneIds: string[];
  duplicate: boolean;
  /** "5 tasks · 46 h · 5,800 Wh · 126 m³" when a task graph exists. */
  work: string | null;
  hasPlan: boolean;
  onChange: (next: Structure) => void;
  onRemove: () => void;
}) {
  const dims = structureDims(structure);
  const unknownZone = structure.zone !== null && !zoneIds.includes(structure.zone);
  const invalidDim = dims.some(([, v]) => !isPos(v));
  const summary = structureSummary(structure);
  return (
    <li className="rounded-md border border-white/10 bg-surface/60 p-3">
      <div className="grid grid-cols-[4rem_minmax(0,1fr)_7rem_auto] items-center gap-2">
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
      <p className={cx("mt-2 text-[12.5px]", summary ? "text-fg" : "text-dim")}>
        {summary || "no dimensions stated: the rate model cannot price this structure"}
      </p>
      {hasPlan && (
        <p className="mt-0.5 font-mono text-[11px] text-muted">
          {work ?? "no task in the graph builds this structure"}
        </p>
      )}
      <details className="group mt-1.5" open={invalidDim || dims.length === 0 || undefined}>
        <summary className="cursor-pointer select-none text-[11px] text-dim hover:text-fg">
          {dims.length === 0 ? "add dimensions" : "edit dimensions"}
          {invalidDim ? <span className="ml-1 text-fail">· a dimension must be &gt; 0</span> : null}
        </summary>
        <div className="mt-2 flex flex-wrap items-end gap-2">
          {dims.map(([key, v]) => (
            <Field key={key} label={dimLabel(key)} className="w-28" error={isPos(v) ? undefined : "must be > 0"}>
              <div className="flex items-center gap-0.5">
                <NumberInput
                  value={v}
                  min={0}
                  step="any"
                  invalid={!isPos(v)}
                  onChange={(n) => onChange(withDim(structure, key, n))}
                />
                <RemoveButton onClick={() => onChange(omitKey(structure, key))} label={`remove ${dimLabel(key)}`} />
              </div>
            </Field>
          ))}
          <DimAdder existing={dims.map(([k]) => k)} onAdd={(key) => onChange(withDim(structure, key, 1))} />
        </div>
      </details>
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
