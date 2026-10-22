"use client";
/**
 * Simulation set-up: failure injections (robot + time), replan budget, simulator step and the
 * Run button. Pure controlled component: the parent owns `SimulateOptions` and the request.
 */
import type { FailureSpec, PlanResult, Requirements, SimulateRequest } from "@/lib/types";
import { robotTypeColor } from "@/lib/palette";
import { formatHours } from "./EventList";

export interface SimulateOptions {
  failures: FailureSpec[];
  maxReplans: number;
  dtH: number;
}

export const DEFAULT_SIMULATE_OPTIONS: SimulateOptions = {
  failures: [],
  maxReplans: 2,
  dtH: 0.25,
};

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
export const DT_MIN_H = 0.05;
export const DT_MAX_H = 1;

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
  if (!(value.dtH >= DT_MIN_H && value.dtH <= DT_MAX_H)) errors.push(`dt must be ${DT_MIN_H}..${DT_MAX_H} h`);
  return errors;
}

/** Body for POST /simulate. frame_stride defaults so that about one frame per hour is kept. */
export function toSimulateRequest(
  plan: PlanResult,
  value: SimulateOptions,
  extra: Partial<Omit<SimulateRequest, "plan">> = {},
): SimulateRequest {
  return {
    plan,
    failures: value.failures.map((f) => ({ robot: f.robot, t_h: f.t_h })),
    max_replans: value.maxReplans,
    dt_h: value.dtH,
    frame_stride: Math.max(1, Math.round(1 / value.dtH)),
    ...extra,
  };
}

function describe(value: SimulateOptions, requirements: Requirements): string {
  const step = `${formatHours(value.dtH)} steps`;
  const valid = value.failures.filter((f) => Number.isFinite(f.t_h));
  if (valid.length === 0) {
    return `Replays the verified schedule in ${step} with no injected failures; battery-low and task-overrun events are still reported.`;
  }
  const typeOf = Object.fromEntries(requirements.robots.map((r) => [r.id, r.type]));
  const list = [...valid]
    .sort((a, b) => a.t_h - b.t_h)
    .map((f) => `${f.robot}${typeOf[f.robot] ? ` (${typeOf[f.robot]})` : ""} fails at ${formatHours(f.t_h)}`)
    .join(", ");
  const replan =
    value.maxReplans === 0
      ? "Replanning is off, so the run continues on the original schedule and the failed robot's work stays undone."
      : `After each failure the agent replans the remaining work from the task-graph stage and re-verifies it (at most ${value.maxReplans} time${value.maxReplans === 1 ? "" : "s"}).`;
  return `Replays the schedule in ${step}; ${list}. ${replan}`;
}

const inputClass =
  "rounded-md border border-white/15 bg-surface px-2 py-1 text-sm tabular-nums outline-none focus:border-accent/60 focus:ring-2 focus:ring-accent/20 disabled:opacity-50";

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
    const t = Math.min(horizonH, Math.max(0, Math.round(base * 0.4)));
    patch({ failures: [...value.failures, { robot: robot.id, t_h: t }] });
  };

  return (
    <div className="space-y-4">
      <fieldset className="space-y-2" disabled={locked}>
        <legend className="text-xs font-medium uppercase tracking-wide text-muted">
          Failure injections
        </legend>
        {value.failures.length === 0 && (
          <p className="text-sm text-muted">None. Add one to test the replanner.</p>
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
                  <span className="text-sm text-muted">fails at</span>
                  <input
                    type="number"
                    aria-label={`Failure ${i + 1} time in hours`}
                    aria-invalid={rowError ? true : undefined}
                    className={`${inputClass} w-24 ${rowError ? "border-fail/70 focus:border-fail/70 focus:ring-fail/20" : ""}`}
                    min={0}
                    max={horizonH}
                    step={0.5}
                    value={Number.isFinite(f.t_h) ? f.t_h : ""}
                    onChange={(e) => updateRow(i, { t_h: e.target.value === "" ? NaN : Number(e.target.value) })}
                  />
                  <span className="text-sm text-muted">h</span>
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

      <div className="flex flex-wrap gap-x-8 gap-y-4">
        <fieldset disabled={locked}>
          <legend className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted">
            Max replans
          </legend>
          <div className="inline-flex overflow-hidden rounded-md border border-white/15" role="group">
            {MAX_REPLANS_RANGE.map((n) => {
              const on = value.maxReplans === n;
              return (
                <button
                  key={n}
                  type="button"
                  aria-pressed={on}
                  onClick={() => patch({ maxReplans: n })}
                  className={`px-2 py-1 text-sm tabular-nums transition-colors disabled:opacity-50 ${
                    on
                      ? "bg-accent text-[#0b0f14]"
                      : "hover:bg-white/10"
                  }`}
                >
                  {n}
                </button>
              );
            })}
          </div>
        </fieldset>
        <fieldset disabled={locked} className="min-w-[12rem] flex-1">
          <legend className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted">
            Simulator step
          </legend>
          <div className="flex items-center gap-3">
            <input
              type="range"
              aria-label="Simulator step in hours"
              className="w-full accent-accent"
              min={DT_MIN_H}
              max={DT_MAX_H}
              step={0.05}
              value={value.dtH}
              onChange={(e) => patch({ dtH: Math.round(Number(e.target.value) * 100) / 100 })}
            />
            <span className="w-16 shrink-0 text-right text-sm tabular-nums">{formatHours(value.dtH)}</span>
          </div>
        </fieldset>
      </div>

      <p className="text-xs leading-5 text-muted">{describe(value, requirements)}</p>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onRun}
          disabled={!canRun}
          aria-busy={running || undefined}
          className="inline-flex items-center gap-2 rounded-md bg-accent px-3.5 py-1.5 text-sm font-semibold text-[#0b0f14] shadow-sm transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running && (
            <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden>
              <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.3" strokeWidth="3" />
              <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
            </svg>
          )}
          {running ? "Simulating…" : "Run simulation"}
        </button>
        {errors.length > 0 && !running && (
          <span className="text-xs text-fail">{errors[0]}</span>
        )}
        {error && !running && <span className="text-xs text-fail">{error}</span>}
      </div>
    </div>
  );
}
