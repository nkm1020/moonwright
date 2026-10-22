"use client";
import { useState, type ReactNode } from "react";
import type { LoopStep, Requirements, Robot, Site, Structure } from "@/lib/types";
import { ID_PATTERN, LIMITS } from "@/lib/types";
import { ROBOT_TYPE_COLORS, robotTypeColor, zoneColor } from "@/lib/palette";
import {
  dimLabel,
  fmtCompact,
  fmtHorizon,
  fmtSeconds,
  plural,
  shortModel,
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
  /** The structure step that produced the requirements (model, tokens, duration). */
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
}: RequirementsCardProps) {
  const problems = validateRequirements(value);
  const repairsOk = Number.isInteger(maxRepairs) && maxRepairs >= 0 && maxRepairs <= 10;
  const zoneIds = value.site.zones.map((z) => z.id);
  const robotIds = value.robots.map((r) => r.id);
  const duplicateRobots = new Set(robotIds.filter((id, i) => robotIds.indexOf(id) !== i));
  const structureIds = value.structures.map((s) => s.id);
  const duplicateStructures = new Set(structureIds.filter((id, i) => structureIds.indexOf(id) !== i));

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
        <Badge title={usage?.model || "no model call recorded"}>
          {usage?.model ? shortModel(usage.model) : "offline fixture"}
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
      {edited && <Badge tone="accent">edited by you</Badge>}
    </>
  );

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
              <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs">
                <span className="mr-1 text-dim">Zones</span>
                {value.site.zones.map((z) => (
                  <Badge key={z.id} mono title={`centre (${z.center[0]}, ${z.center[1]}), radius ${z.radius}`}>
                    <Dot color={zoneColor(z.id, zoneIds)} />
                    {z.id}
                    <span className="text-dim">
                      ({z.center[0]},{z.center[1]}) r{z.radius}
                    </span>
                  </Badge>
                ))}
                {value.site.zones.length === 0 && <span className="text-dim">none declared</span>}
                {value.site.depot && (
                  <Badge mono title="robot depot">
                    depot ({value.site.depot[0]},{value.site.depot[1]})
                  </Badge>
                )}
              </div>
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
                    onChange={(next) => setStructure(i, next)}
                    onRemove={() => removeStructure(i)}
                  />
                ))}
              </ul>
            </Group>

            <Group
              title={`Robots · ${value.robots.length}`}
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
              disabled={problems.length > 0 || !repairsOk || restructuring}
            >
              {planning ? "Planning…" : "Plan with these requirements"}
            </Button>
          </div>
        </footer>
      </Card>
    </Section>
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
  onChange,
  onRemove,
}: {
  structure: Structure;
  index: number;
  zoneIds: string[];
  duplicate: boolean;
  onChange: (next: Structure) => void;
  onRemove: () => void;
}) {
  const dims = structureDims(structure);
  const unknownZone = structure.zone !== null && !zoneIds.includes(structure.zone);
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
        <RemoveButton onClick={onRemove} label={`remove structure ${structure.id || index + 1}`} />
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        {dims.map(([key, v]) => (
          <Field key={key} label={dimLabel(key)} className="w-32" error={isPos(v) ? undefined : "must be > 0"}>
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
