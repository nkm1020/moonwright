"use client";
/**
 * Simulation set-up: failure injections (robot + time), the replan budget and the Run button.
 * The simulator step is fixed at SIM_DT_H (0.25 h). Pure controlled component: the parent owns
 * `SimulateOptions` and the request.
 */
import type { FailureSpec, PlanResult, Requirements, SimulateRequest } from "@/lib/types";
import { robotTypeColor } from "@/lib/palette";
import { formatHours } from "./EventList";
import { Button } from "./Section";

export interface SimulateOptions {
  failures: FailureSpec[];
  maxReplans: number;
}

export const DEFAULT_SIMULATE_OPTIONS: SimulateOptions = {
  failures: [],
  maxReplans: 2,
};

/** Fixed simulator step in hours; frame_stride keeps about one frame per hour. */
export const SIM_DT_H = 0.25;
export const SIM_FRAME_STRIDE = Math.max(1, Math.round(1 / SIM_DT_H));

export interface SimControlsProps {
  requirements: Requirements;
  horizonH: number;
  value: SimulateOptions;
  onChange: (value: SimulateOptions) => void;
  onRun: () => void;
  running: boolean;
  disabled?: boolean;
  /** Additive: makespan of the plan; a new failure row defaults to ~40 % of it (else of the horizon). */
  makespanH?: number;
  /** Additive: error text shown under the Run button (e.g. the last request failure). */
  error?: string | null;
}

export const MAX_REPLANS_RANGE = [0, 1, 2, 3, 4, 5] as const;

/** Error text for one failure row, or null when it is valid. */
export function failureRowError(f: FailureSpec, horizonH: number, requirements?: Requirements): string | null {
  if (requirements && !requirements.robots.some((r) => r.id === f.robot)) return `unknown robot ${f.robot}`;
  if (!Number.isFinite(f.t_h)) return "enter a time in hours";
  if (f.t_h < 0 || f.t_h > horizonH) return `time must be between 0 and ${horizonH} h`;
  return null;
}

/** All problems that should block a run (empty array = valid). */
export function validateSimulateOptions(value: SimulateOptions, horizonH: number, requirements?: Requirements): string[] {
  const errors: string[] = [];
  value.failures.forEach((f, i) => {
    const e = failureRowError(f, horizonH, requirements);
    if (e) errors.push(`failure ${i + 1}: ${e}`);
  });
  if (!Number.isInteger(value.maxReplans) || value.maxReplans < 0 || value.maxReplans > 5) {
    errors.push("max replans must be 0..5");
  }
  return errors;
}

/** Body for POST /simulate: the fixed 0.25 h step with one frame kept per hour. */
export function toSimulateRequest(
  plan: PlanResult,
  value: SimulateOptions,
  extra: Partial<Omit<SimulateRequest, "plan">> = {},
): SimulateRequest {
  return {
    plan,
    failures: value.failures.map((f) => ({ robot: f.robot, t_h: f.t_h })),
    max_replans: value.maxReplans,
    dt_h: SIM_DT_H,
    frame_stride: SIM_FRAME_STRIDE,
    ...extra,
  };
}

/** What the run will do, in one sentence. */
export function describeRun(value: SimulateOptions, requirements: Requirements): string {
  const valid = value.failures.filter((f) => Number.isFinite(f.t_h));
  if (valid.length === 0) {
    return "Replay the verified schedule with no injected failure. A verified plan runs clean: any battery-low or task-overrun event would be a genuine anomaly.";
  }
  const typeOf = Object.fromEntries(requirements.robots.map((r) => [r.id, r.type]));
  const list = [...valid]
    .sort((a, b) => a.t_h - b.t_h)
    .map((f) => `${f.robot}${typeOf[f.robot] ? ` (${typeOf[f.robot]})` : ""} failure at ${formatHours(f.t_h)}`)
    .join(" and ");
  if (value.maxReplans === 0) {
    return `Inject ${list}; replanning is off, so the run continues on the original schedule and the failed robot's work stays undone.`;
  }
  const budget = value.maxReplans === 1 ? "one replan" : `at most ${value.maxReplans} replans`;
  return `Inject ${list}; the agent reschedules the remaining work with CP-SAT and escalates to Nemotron only if the fleet cannot finish (${budget}).`;
}

const inputClass =
  "h-8 rounded-md border border-white/15 bg-surface px-2 text-[13px] tabular-nums outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20 disabled:opacity-50";

export function SimControls({
  requirements,
  horizonH,
  value,
  onChange,
  onRun,
  running,
  disabled = false,
  makespanH,
  error,
}: SimControlsProps) {
  const robots = requirements.robots;
  const errors = validateSimulateOptions(value, horizonH, requirements);
  const locked = disabled || running;
  const canRun = !locked && errors.length === 0 && robots.length > 0;

  const patch = (p: Partial<SimulateOptions>) => onChange({ ...value, ...p });
  const updateRow = (i: number, p: Partial<FailureSpec>) =>
    patch({ failures: value.failures.map((f, j) => (j === i ? { ...f, ...p } : f)) });
  const removeRow = (i: number) => patch({ failures: value.failures.filter((_, j) => j !== i) });
  const addRow = () => {
    if (robots.length === 0) return;
    const used = new Set(value.failures.map((f) => f.robot));
    const robot = robots.find((r) => !used.has(r.id)) ?? robots[0];
    const base = makespanH && makespanH > 0 ? makespanH : horizonH;
    // ~40 % into the work, on a 5 h grid (80 h makespan -> 30 h)
    const t = Math.min(horizonH, Math.max(0, Math.round((base * 0.4) / 5) * 5));
    patch({ failures: [...value.failures, { robot: robot.id, t_h: t }] });
  };

  return (
    <div className="space-y-4">
      <fieldset className="space-y-2" disabled={locked}>
        <legend className="text-[11px] font-medium uppercase tracking-wider text-dim">Failure injections</legend>
        {value.failures.length === 0 && (
          <p className="text-[13px] text-muted">None yet. Add one to watch the agent replan around a lost robot.</p>
        )}
        <ul className="space-y-2">
          {value.failures.map((f, i) => {
            const rowError = failureRowError(f, horizonH, requirements);
            const type = robots.find((r) => r.id === f.robot)?.type ?? "";
            return (
              <li key={i} className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: robotTypeColor(type) }}
                    aria-hidden
                  />
                  <select
                    aria-label={`Robot for failure ${i + 1}`}
                    className={`${inputClass} font-mono`}
                    value={f.robot}
                    onChange={(e) => updateRow(i, { robot: e.target.value })}
                  >
                    {robots.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.id} · {r.type}
                      </option>
                    ))}
                    {!robots.some((r) => r.id === f.robot) && <option value={f.robot}>{f.robot}</option>}
                  </select>
                  <span className="text-[13px] text-muted">fails at</span>
                  <input
                    type="number"
                    aria-label={`Failure ${i + 1} time in hours`}
                    aria-invalid={rowError ? true : undefined}
                    className={`${inputClass} w-24 font-mono ${rowError ? "border-fail/70 focus:border-fail/70 focus:ring-fail/20" : ""}`}
                    min={0}
                    max={horizonH}
                    step={0.5}
                    value={Number.isFinite(f.t_h) ? f.t_h : ""}
                    onChange={(e) => updateRow(i, { t_h: e.target.value === "" ? NaN : Number(e.target.value) })}
                  />
                  <span className="text-[13px] text-muted">h</span>
                  <button
                    type="button"
                    onClick={() => removeRow(i)}
                    aria-label={`Remove failure ${i + 1}`}
                    className="ml-auto rounded-md px-2 py-1 text-xs text-muted hover:bg-white/10 hover:text-fg disabled:opacity-50"
                  >
                    Remove
                  </button>
                </div>
                {rowError && <p className="pl-5 text-xs text-fail">{rowError}</p>}
              </li>
            );
          })}
        </ul>
        <button
          type="button"
          onClick={addRow}
          disabled={robots.length === 0}
          className="rounded-md border border-dashed border-white/20 px-2.5 py-1 text-xs font-medium text-muted hover:border-white/40 hover:text-fg disabled:opacity-50"
        >
          + Add failure
        </button>
      </fieldset>

      <fieldset disabled={locked}>
        <legend className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-dim">Max replans</legend>
        <div className="flex flex-wrap items-center gap-3">
          <div className="inline-flex overflow-hidden rounded-md border border-white/15" role="group">
            {MAX_REPLANS_RANGE.map((n) => {
              const on = value.maxReplans === n;
              return (
                <button
                  key={n}
                  type="button"
                  aria-pressed={on}
                  onClick={() => patch({ maxReplans: n })}
                  className={`px-2.5 py-1 text-[13px] tabular-nums transition-colors disabled:opacity-50 ${
                    on ? "bg-white/15 font-semibold text-fg" : "text-muted hover:bg-white/10 hover:text-fg"
                  }`}
                >
                  {n}
                </button>
              );
            })}
          </div>
          <span className="text-[11px] text-dim">
            per run · simulator step {formatHours(SIM_DT_H)}, one frame kept per hour
          </span>
        </div>
      </fieldset>

      <p className="text-[13px] leading-5 text-muted">{describeRun(value, requirements)}</p>

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={onRun} disabled={!canRun} loading={running}>
          {running ? "Simulating…" : "Run simulation"}
        </Button>
        {errors.length > 0 && !running && <span className="text-xs text-fail">{errors[0]}</span>}
        {error && !running && <span className="text-xs text-fail">{error}</span>}
      </div>
    </div>
  );
}
