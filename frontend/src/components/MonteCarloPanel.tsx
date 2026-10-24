"use client";
/**
 * Step 05, robustness: Monte Carlo over the verified plan (random robot failures and
 * task-duration jitter, CP-SAT rescheduling the remaining work after every event), the
 * outcome attribution with the single points of failure, and the spare-robot what-if.
 *
 * The baseline run goes through App.tsx (`onRun` -> `result`). The what-if re-posts
 * /montecarlo from here with the same seed and parameters against a PlanResult whose fleet
 * carries the extra robots, keeps the baseline untouched and renders both side by side, so
 * App.tsx keeps today's props. The static demo has no /montecarlo: the inputs show the
 * recorded run's, locked, and the what-if is the recorded one (one spare of each single-point
 * type) or none.
 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  HistogramBin,
  MonteCarloRequest,
  MonteCarloResult,
  PlanResult,
  Requirements,
  Robot,
} from "@/lib/types";
import { errorMessage, isAbortError, postMonteCarlo } from "@/lib/api";
import { clamp, fmtHours, fmtNum, fmtPercent, fmtSeconds, plural } from "@/lib/format";
import { robotTypeColor } from "@/lib/palette";
import {
  Button,
  Card,
  Dot,
  Field,
  IconCheck,
  Notice,
  NumberInput,
  Section,
  Spinner,
  Stat,
  cx,
  type Tone,
} from "./Section";
import { OutcomeBar, attributeOutcomes, describeOutcome, withinHorizonCount, type Attribution } from "./OutcomeBar";

export interface MonteCarloPanelProps {
  plan: PlanResult;
  briefId: string | null;
  result: MonteCarloResult | null;
  running: boolean;
  /** seconds the run in progress has taken so far (App's ticker); null while idle */
  elapsedS?: number | null;
  error: string | null;
  onRun: (req: MonteCarloRequest) => void;
  /** Optional: when given, the what-if column offers "Plan with these requirements" with the
   *  fleet that includes the spares (App.tsx can feed it to the requirements editor). */
  onAdoptRequirements?: (requirements: Requirements) => void;
  /** Additive: the static demo replays a recording: the inputs (the recorded run's parameters)
   *  are locked with this reason under them, Run replays the recorded result, and the spare
   *  chips cannot start a what-if of their own. */
  recorded?: string | null;
  /** Additive: the static demo's recorded what-if, the same runs with one spare of each single
   *  point of failure; offered when its fleet is the one spareRobots names (CP1 -> CP2). */
  recordedWhatIf?: MonteCarloResult | null;
  /** Additive: why the static demo's recorded what-if is missing ("could not load the recorded
   *  what-if (HTTP 404)"), said where its button would be. */
  recordedWhatIfError?: string | null;
}

const isInt = (n: number, lo: number, hi: number) => Number.isInteger(n) && n >= lo && n <= hi;
const inUnit = (n: number) => Number.isFinite(n) && n >= 0 && n <= 1;

/** "84%" / "37.5%": one decimal only when the rate needs it (200 runs resolve 0.5 %). */
export function fmtPct1(x: number | null | undefined): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${(x * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
}

/** "52–66%" for a Wilson interval. */
export function fmtCi(ci: [number, number] | null | undefined): string {
  if (!ci) return "—";
  return `${Math.round(ci[0] * 100)}–${Math.round(ci[1] * 100)}%`;
}

function rateTone(x: number): Tone {
  if (x >= 0.9) return "ok";
  if (x >= 0.6) return "warn";
  return "fail";
}

// ------------------------------------------------------------------ spares
const TRAILING_DIGITS = /^(.*?)(\d+)$/;

/** Id of a spare for `type`: the type's robot id with its number bumped to the next free one
 *  ("SV1" -> "SV2"; "SV1" + "SV2" -> "SV3"); an id without a number gets "2" appended. */
export function spareRobotId(robots: Robot[], type: string, taken: ReadonlySet<string>): string {
  const sample = robots.find((r) => r.type === type);
  const m = sample ? TRAILING_DIGITS.exec(sample.id) : null;
  const prefix = m && m[1] ? m[1] : sample ? sample.id : type.slice(0, 2).toUpperCase();
  let n = m ? Math.max(2, Number(m[2]) + 1) : 2;
  while (taken.has(`${prefix}${n}`)) n += 1;
  return `${prefix}${n}`;
}

/** One clone per type, same battery and speed as the type's first robot, in fleet order (the
 *  order the types' first robots appear in), whatever order `types` lists them in. Monte Carlo
 *  draws one failure time per robot in fleet order, so the same spares appended in another
 *  order are other draws: in fleet order they are the fleet the CLI's what-if replays
 *  (docs/reference.md, the recorded landing_pad.whatif.mc.json: CP2, PR2, SV2). */
export function spareRobots(robots: Robot[], types: string[]): Robot[] {
  const taken = new Set(robots.map((r) => r.id));
  const out: Robot[] = [];
  const fleetTypes = [...new Set(robots.map((r) => r.type))];
  for (const type of fleetTypes.filter((t) => types.includes(t))) {
    const sample = robots.find((r) => r.type === type);
    if (!sample) continue;
    const id = spareRobotId(robots, type, taken);
    taken.add(id);
    out.push({ id, type, battery_wh: sample.battery_wh, speed_cells_per_h: sample.speed_cells_per_h });
  }
  return out;
}

/** The PlanResult with the spares appended to its fleet; the schedule is untouched (a spare
 *  idles until a replan needs it). */
export function withSpares(plan: PlanResult, spares: Robot[]): PlanResult {
  if (spares.length === 0) return plan;
  return {
    ...plan,
    requirements: { ...plan.requirements, robots: [...plan.requirements.robots, ...spares] },
  };
}

/** True when every robot a recorded what-if's runs lost is in `fleet`: the recording used the
 *  spares this panel would name, so its result can stand for that fleet. */
export function whatIfFits(recorded: MonteCarloResult, fleet: Robot[]): boolean {
  const ids = new Set(fleet.map((r) => r.id));
  return recorded.samples.every((s) => s.failures.every((f) => ids.has(f.robot)));
}

/** The hazard the inputs imply, in numbers a judge can check: "0.002 failures per robot-hour
 *  = MTBF 500 h, about 16% per robot over the 89 h plan (71% that at least one of 7 robots
 *  fails); task durations jittered ±15%; up to 2 solver replans per run." */
export function hazardCopy(rate: number, jitter: number, maxReplans: number, sortieH: number, fleet: number): string {
  const jitterText = `task durations jittered ±${fmtPercent(jitter)}`;
  const replanText = `up to ${plural(maxReplans, "solver replan")} per run`;
  if (!(rate > 0)) return `No random failures (rate 0): only the ${jitterText} perturbs the plan; ${replanText}.`;
  const mtbf = 1 / rate;
  const perRobot = 1 - Math.exp(-rate * sortieH);
  const anyRobot = 1 - Math.exp(-rate * sortieH * fleet);
  return (
    `${fmtNum(rate, 4)} failures per robot-hour = MTBF ${fmtHours(mtbf, 0)}, about ${fmtPercent(perRobot)} per robot ` +
    `over the ${fmtHours(sortieH, 0)} plan (${fmtPercent(anyRobot)} that at least one of ${fleet} robots fails); ` +
    `${jitterText}; ${replanText}.`
  );
}

function finishTimes(result: MonteCarloResult): number[] {
  return result.samples.flatMap((s) => (s.finished_at_h === null ? [] : [s.finished_at_h]));
}

// ------------------------------------------------------------------ panel
interface WhatIf {
  /** the baseline result it was run against; stale (hidden) once `result` is another object */
  baseline: MonteCarloResult;
  types: string[];
  spares: Robot[];
  request: MonteCarloRequest;
  status: "running" | "done" | "error";
  /** the last finished what-if result; kept on screen (dimmed) while a new one runs */
  result: MonteCarloResult | null;
  error: string | null;
}

export function MonteCarloPanel({
  plan,
  briefId,
  result,
  running,
  elapsedS = null,
  error,
  onRun,
  onAdoptRequirements,
  recorded = null,
  recordedWhatIf = null,
  recordedWhatIfError = null,
}: MonteCarloPanelProps) {
  const replay = recorded !== null;
  const [runs, setRuns] = useState(200);
  const [rate, setRate] = useState(0.002);
  const [jitter, setJitter] = useState(0.15);
  const [seed, setSeed] = useState<number>(1);
  const [maxReplans, setMaxReplans] = useState(2);
  const [advanced, setAdvanced] = useState(false);
  const [lastReq, setLastReq] = useState<MonteCarloRequest | null>(null);
  const [whatIf, setWhatIf] = useState<WhatIf | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const holder = abortRef;
    return () => holder.current?.abort();
  }, []);

  const valid = {
    runs: isInt(runs, 1, 200),
    rate: inUnit(rate),
    jitter: inUnit(jitter),
    seed: !Number.isFinite(seed) || Number.isInteger(seed),
    maxReplans: isInt(maxReplans, 0, 5),
  };
  const allValid = Object.values(valid).every(Boolean);

  const robots = plan.requirements.robots;
  const makespanH = plan.schedule?.makespan_h ?? null;
  const sortieH = makespanH ?? plan.requirements.horizon_h;
  const hazard =
    valid.rate && valid.jitter && valid.maxReplans ? hazardCopy(rate, jitter, maxReplans, sortieH, robots.length) : null;

  const attribution = useMemo(() => (result ? attributeOutcomes(result, robots) : null), [result, robots]);
  const spofTypes = useMemo(() => attribution?.spof.map((r) => r.type) ?? [], [attribution]);
  const spareCatalog = useMemo(() => spareRobots(robots, spofTypes), [robots, spofTypes]);
  const active = whatIf && result && whatIf.baseline === result ? whatIf : null;
  const selected = active?.types ?? [];
  const baselineSeed = lastReq?.seed ?? result?.samples[0]?.seed ?? null;
  // the recorded what-if stands for "one spare of each" only when its runs lost no robot this
  // panel would not name
  const recordedFits =
    recordedWhatIf !== null && spareCatalog.length > 0 && whatIfFits(recordedWhatIf, [...robots, ...spareCatalog]);

  const cancelWhatIf = () => {
    abortRef.current?.abort();
    abortRef.current = null;
  };

  /** The request a what-if repeats: the baseline's own, else the inputs with its seed. */
  const baselineRequest = (baseline: MonteCarloResult): MonteCarloRequest =>
    lastReq ?? {
      plan,
      runs: baseline.runs,
      failure_rate_per_robot_h: rate,
      duration_jitter: jitter,
      max_replans: maxReplans,
      seed: baseline.samples[0]?.seed ?? 1,
      ...(briefId ? { brief_id: briefId } : {}),
    };

  /** The static demo's what-if: the recorded runs with one spare of each single-point type. */
  const showRecordedWhatIf = () => {
    if (!result || !recordedWhatIf) return;
    cancelWhatIf();
    const request: MonteCarloRequest = { ...baselineRequest(result), plan: withSpares(plan, spareCatalog) };
    setWhatIf({
      baseline: result,
      types: spofTypes,
      spares: spareCatalog,
      request,
      status: "done",
      result: recordedWhatIf,
      error: null,
    });
  };

  const submit = () => {
    if (!allValid) return;
    // A concrete seed always goes out so that a what-if can replay exactly the same draws.
    const req: MonteCarloRequest = {
      plan,
      runs,
      failure_rate_per_robot_h: rate,
      duration_jitter: jitter,
      max_replans: maxReplans,
      seed: Number.isFinite(seed) ? seed : Math.floor(Math.random() * 2 ** 31),
      ...(briefId ? { brief_id: briefId } : {}),
    };
    cancelWhatIf();
    setWhatIf(null);
    setLastReq(req);
    onRun(req);
  };

  const runWhatIf = (types: string[]) => {
    if (!result) return;
    cancelWhatIf();
    if (types.length === 0) {
      setWhatIf(null);
      return;
    }
    // a fleet of the static demo's own choosing has no recording to show
    if (replay) return;
    const baseline = result;
    const spares = spareCatalog.filter((r) => types.includes(r.type));
    const request: MonteCarloRequest = { ...baselineRequest(baseline), plan: withSpares(plan, spares) };
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setWhatIf((prev) => ({
      baseline,
      types,
      spares,
      request,
      status: "running",
      result: prev && prev.baseline === baseline ? prev.result : null,
      error: null,
    }));
    postMonteCarlo(request, ctrl.signal)
      .then((r) => {
        if (ctrl.signal.aborted) return;
        setWhatIf({ baseline, types, spares, request, status: "done", result: r, error: null });
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted || isAbortError(e)) return;
        setWhatIf((prev) => ({
          baseline,
          types,
          spares,
          request,
          status: "error",
          result: prev && prev.baseline === baseline ? prev.result : null,
          error: errorMessage(e),
        }));
      })
      .finally(() => {
        if (abortRef.current === ctrl) abortRef.current = null;
      });
  };

  const toggleSpare = (type: string) =>
    runWhatIf(selected.includes(type) ? selected.filter((t) => t !== type) : [...selected, type]);

  const seedText = Number.isFinite(seed) ? `seed ${seed}` : "random seed";

  return (
    <Section
      id="robustness"
      step="05"
      title="Robustness"
      subtitle="Monte Carlo: the verified plan is replayed many times with random robot failures and task-duration jitter; CP-SAT reschedules the remaining work after every event. The headline is the probability of finishing within the horizon with its 95% confidence interval, and where the misses come from."
    >
      <Card>
        {/* the static demo locks the inputs to the recorded run's at full contrast (mw-locked) */}
        <div
          className={cx(
            "grid grid-cols-2 gap-3 md:grid-cols-[repeat(3,minmax(0,13rem))_auto] md:items-end",
            replay && "mw-locked",
          )}
        >
          <Field label="Runs" hint="replays, 1–200" error={valid.runs ? undefined : "whole number 1–200"}>
            <NumberInput value={runs} min={1} max={200} step={1} invalid={!valid.runs} disabled={replay} onChange={setRuns} />
          </Field>
          <Field label="Failure rate" hint="per robot-hour, 0–1" error={valid.rate ? undefined : "0–1"}>
            <NumberInput value={rate} min={0} max={1} step={0.001} invalid={!valid.rate} disabled={replay} onChange={setRate} />
          </Field>
          <Field label="Duration jitter" hint="relative spread, 0–1" error={valid.jitter ? undefined : "0–1"}>
            <NumberInput value={jitter} min={0} max={1} step={0.05} invalid={!valid.jitter} disabled={replay} onChange={setJitter} />
          </Field>
          <div className="col-span-2 flex md:col-span-1 md:block">
            <Button variant="primary" onClick={submit} loading={running} disabled={!allValid} className="w-full md:w-auto">
              {running ? `Replaying ${runs} runs…` : replay ? "Replay Monte Carlo" : "Run Monte Carlo"}
            </Button>
          </div>
        </div>

        <div className="mt-2.5">
          <button
            type="button"
            aria-expanded={advanced}
            aria-controls="mc-advanced"
            onClick={() => setAdvanced((v) => !v)}
            className="inline-flex items-center gap-1.5 text-[11px] text-dim transition-colors hover:text-fg"
          >
            <Chevron open={advanced} />
            <span className="font-medium uppercase tracking-wider">Advanced</span>
            <span className="font-mono">
              · {seedText} · {plural(maxReplans, "replan")} per run
            </span>
          </button>
          {advanced && (
            <div id="mc-advanced" className={cx("mt-2 grid grid-cols-2 gap-3 md:max-w-md", replay && "mw-locked")}>
              <Field
                label="Seed"
                hint="same seed, same draws; empty = random"
                error={valid.seed ? undefined : "whole number"}
              >
                <NumberInput
                  value={seed}
                  step={1}
                  placeholder="random"
                  invalid={!valid.seed}
                  disabled={replay}
                  onChange={setSeed}
                />
              </Field>
              <Field label="Max replans" hint="per run, 0–5" error={valid.maxReplans ? undefined : "0–5"}>
                <NumberInput
                  value={maxReplans}
                  min={0}
                  max={5}
                  step={1}
                  invalid={!valid.maxReplans}
                  disabled={replay}
                  onChange={setMaxReplans}
                />
              </Field>
            </div>
          )}
        </div>

        {hazard && <p className="mt-3 text-xs text-muted">{hazard}</p>}
        {recorded && <p className="mt-1 text-xs text-dim">{recorded}</p>}

        {error && (
          <Notice tone="fail" title="Monte Carlo failed" className="mt-4">
            {error}
            {result && <div className="mt-1 text-xs">The previous result stays below.</div>}
          </Notice>
        )}

        {running && (
          <div
            role="status"
            className="mt-5 flex items-center gap-3 rounded-md border border-dashed border-white/10 px-4 py-6 text-[13px] text-muted"
          >
            <Spinner className="size-4 shrink-0 text-accent" />
            <span className="min-w-0 flex-1">
              Replaying {runs} runs with random failures and jitter; CP-SAT reschedules after every event, Nemotron is
              not called. A few seconds for most plans; one with many short tasks across far zones can take half a minute,
              since every replan is solved to optimality.
            </span>
            <span className="shrink-0 font-mono text-xs tabular-nums text-dim">{elapsedS === null ? "" : fmtSeconds(elapsedS)}</span>
          </div>
        )}

        {!result && !running && !error && (
          <p className="mt-4 text-xs text-dim">
            Each run draws failures from the rate above, stretches task durations by the jitter, and replans the
            remaining work with the same solver checks as the main loop.
          </p>
        )}

        {result && attribution && (
          <div className={running ? "opacity-50 transition-opacity" : "transition-opacity"} aria-busy={running || undefined}>
            <SpofStrip
              attribution={attribution}
              catalog={spareCatalog}
              selected={selected}
              runs={result.runs}
              seed={baselineSeed}
              onToggle={toggleSpare}
              onClear={() => runWhatIf([])}
              locked={recorded}
              onAddAll={recordedFits ? showRecordedWhatIf : undefined}
              missing={recordedWhatIfError}
            />
            {active ? (
              <Comparison
                baseline={result}
                baselineAttribution={attribution}
                whatIf={active}
                plan={plan}
                seed={baselineSeed}
                onRetry={() => runWhatIf(active.types)}
                onAdopt={onAdoptRequirements}
              />
            ) : (
              <ResultView
                heading="Result"
                note={resultNote(result, robots.length, baselineSeed)}
                result={result}
                plan={plan}
                attribution={attribution}
                layout="wide"
              />
            )}
          </div>
        )}
      </Card>
    </Section>
  );
}

function resultNote(result: MonteCarloResult, fleet: number, seed: number | null): string {
  return `${plural(fleet, "robot")} · ${plural(result.runs, "replay")} in ${fmtSeconds(result.elapsed_s)}${
    seed === null ? "" : ` · seed ${seed}`
  }`;
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={cx("size-3 transition-transform", open && "rotate-90")}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <path d="M6 3.5l4.5 4.5L6 12.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ------------------------------------------------------------------ single points of failure
/** The single points of failure as chips, each one a what-if toggle (a spare of that type).
 *  Locked (the static demo), the chips only report, and `onAddAll` shows the recorded what-if
 *  with one spare of each when there is one. */
function SpofStrip({
  attribution,
  catalog,
  selected,
  runs,
  seed,
  onToggle,
  onClear,
  locked = null,
  onAddAll,
  missing = null,
}: {
  attribution: Attribution;
  catalog: Robot[];
  selected: string[];
  runs: number;
  seed: number | null;
  onToggle: (type: string) => void;
  onClear: () => void;
  /** why a chip cannot start a what-if of its own */
  locked?: string | null;
  onAddAll?: () => void;
  /** why the recorded what-if is not there (its file did not load) */
  missing?: string | null;
}) {
  const { spof } = attribution;
  const sameDraws = `${seed === null ? "same seed" : `seed ${seed}`}, same failure rate and jitter`;
  return (
    <div className="mt-5 rounded-md border border-line bg-white/[0.02] px-3.5 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="text-[11px] font-medium uppercase tracking-wider text-dim">
          Single points of failure{" "}
          <span className="ml-1 font-normal normal-case tracking-normal">· robot types with one robot</span>
        </div>
        <div className="text-[11px] text-dim">
          {locked === null
            ? `What if: add a spare and the same ${runs} replays re-run with it (${sameDraws})`
            : onAddAll
              ? `What if, recorded: the same ${runs} replays with one spare of each (${sameDraws})`
              : missing
                ? `What if: ${missing}`
                : `What if: ${locked}`}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {spof.length === 0 && (
          <span className="text-xs text-muted">Every robot type has a backup: no single point of failure in this fleet.</span>
        )}
        {spof.map((row) => {
          const spare = catalog.find((r) => r.type === row.type);
          const on = selected.includes(row.type);
          return (
            <button
              key={row.type}
              type="button"
              aria-pressed={on}
              disabled={!spare || locked !== null}
              onClick={() => onToggle(row.type)}
              title={
                locked !== null
                  ? `${row.type}: ${locked}`
                  : on
                    ? `Remove the spare ${spare?.id ?? ""} and re-run`
                    : `Add a spare ${row.type} (${spare?.id ?? ""}, same battery and speed) and re-run the ${runs} replays`
              }
              className={cx(
                "inline-flex max-w-full items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors",
                // a locked chip reports, at full contrast; it is no dead button
                locked === null ? "disabled:cursor-not-allowed disabled:opacity-50" : "cursor-default",
                on
                  ? "border-fg/40 bg-white/10 text-fg"
                  : cx("border-white/10 bg-white/5 text-muted", locked === null && "hover:border-white/20 hover:text-fg"),
              )}
            >
              {on ? <IconCheck className="size-3 text-ok" /> : <Dot color={robotTypeColor(row.type)} />}
              <span className="font-mono font-medium text-fg">{row.robots.join("/") || row.type}</span>
              <span>{row.type}</span>
              <span className="font-mono tabular-nums text-dim">
                {row.misses === 0 ? "no misses" : `in ${plural(row.misses, "miss", "misses")}`}
              </span>
              {(on || locked === null) && (
                <span className={cx("font-medium", on ? "text-ok" : "text-fg")}>
                  {on ? `${spare?.id ?? "spare"} added` : `+ add ${spare?.id ?? "spare"}`}
                </span>
              )}
            </button>
          );
        })}
        {onAddAll && selected.length === 0 && (
          <Button
            size="sm"
            onClick={onAddAll}
            title={`Show the recorded re-run with ${catalog.map((r) => r.id).join(", ")} in the fleet`}
          >
            + Add one spare of each ({catalog.map((r) => r.id).join(", ")})
          </Button>
        )}
        {selected.length > 0 && (
          <Button variant="ghost" size="sm" onClick={onClear}>
            Clear spares
          </Button>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ result views
interface ResultViewProps {
  heading: ReactNode;
  note?: ReactNode;
  result: MonteCarloResult;
  plan: PlanResult;
  attribution: Attribution;
  /** wide: hero and stats beside the charts (a lone result); stacked: one column of a pair */
  layout: "wide" | "stacked";
  /** shared binning and y-range so two histograms compare on the same axes */
  axes?: SharedAxes | null;
  /** the baseline a what-if is measured against (adds the delta under the hero) */
  compareTo?: MonteCarloResult | null;
  className?: string;
}

function ResultView({ heading, note, result, plan, attribution, layout, axes, compareTo, className }: ResultViewProps) {
  const finishes = useMemo(() => finishTimes(result), [result]);
  const within = withinHorizonCount(result);
  // completed runs that finished after the horizon; with none, "completed" would repeat the hero
  const late = Math.max(0, result.completed - within);
  const delta = compareTo ? (result.within_horizon_rate - compareTo.within_horizon_rate) * 100 : null;
  const hero = (
    <Stat
      hero
      label="Finished within horizon"
      value={fmtPct1(result.within_horizon_rate)}
      tone={rateTone(result.within_horizon_rate)}
      sub={
        <>
          95% CI {fmtCi(result.within_horizon_ci)} · {within} of {result.runs} runs by {fmtHours(result.horizon_h, 0)}
          {delta !== null && (
            <>
              {" · "}
              <span className={cx("font-medium", delta > 0 ? "text-ok" : delta < 0 ? "text-fail" : "text-muted")}>
                {delta > 0 ? "+" : delta < 0 ? "−" : "±"}
                {fmtNum(Math.abs(delta), 1)} pts
              </span>{" "}
              vs baseline
            </>
          )}
        </>
      }
    />
  );
  const stats = (
    <div className={cx("mt-4 grid grid-cols-2 gap-x-4 gap-y-3", layout === "wide" ? "xl:grid-cols-4" : "sm:grid-cols-4")}>
      {late > 0 ? (
        <Stat label="Completed" value={fmtPct1(result.completion_rate)} sub={`${result.completed} of ${result.runs}, ${late} late`} />
      ) : (
        <Stat label="Did not finish" value={result.runs - result.completed} sub="none finished late" />
      )}
      <Stat label="p50 finish" value={fmtHours(result.finish_h.p50)} sub={`mean ${fmtHours(result.finish_h.mean)}`} />
      <Stat label="p90 finish" value={fmtHours(result.finish_h.p90)} sub={`max ${fmtHours(result.finish_h.max)}`} />
      <Stat label="Mean replans" value={fmtNum(result.replans_mean, 2)} sub={`failures ${fmtNum(result.failures_mean, 2)}/run`} />
    </div>
  );
  const outcomes = (
    <>
      <OutcomeBar result={result} />
      <p className="mt-2 text-[13px] leading-relaxed text-muted">
        <span className="text-fg">{attribution.lead}</span>
        {attribution.rest && ` ${attribution.rest}`}
      </p>
    </>
  );
  const histogram = (
    <Histogram
      finishes={finishes}
      horizonH={result.horizon_h}
      makespanH={plan.schedule?.makespan_h ?? null}
      runs={result.runs}
      axes={axes ?? null}
    />
  );
  return (
    <div className={cx(layout === "stacked" ? "rounded-md border border-line p-4" : "mt-5", className)}>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="text-[14px] font-medium">{heading}</h3>
        {note && <span className="text-[11px] text-dim">{note}</span>}
      </div>
      {layout === "wide" ? (
        // the answer, its breakdown and the misses on the left; the distribution alone on
        // the right, so the two columns end close together
        <div className="grid gap-x-10 gap-y-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div>
            {hero}
            {stats}
            <div className="mt-5">{outcomes}</div>
            <MissList result={result} />
          </div>
          <div>{histogram}</div>
        </div>
      ) : (
        <div>
          {hero}
          {stats}
          <div className="mt-5">{outcomes}</div>
          <div className="mt-4">{histogram}</div>
          <MissList result={result} />
        </div>
      )}
    </div>
  );
}

function MissList({ result }: { result: MonteCarloResult }) {
  const missed = result.samples.filter((s) => !s.within_horizon);
  if (missed.length === 0) return null;
  const shown = missed.slice(0, 6);
  return (
    <details className="mt-4 text-xs text-muted">
      <summary className="cursor-pointer select-none hover:text-fg">Misses run by run ({missed.length})</summary>
      <ul className="mt-2 space-y-1 font-mono tabular-nums">
        {shown.map((s) => (
          <li key={s.seed}>
            seed {s.seed}: {describeOutcome(s.outcome || (s.completed ? "late" : "stuck"))} ·{" "}
            {s.completed ? `finished ${fmtHours(s.finished_at_h)}` : "did not finish"} ·{" "}
            {plural(s.failures.length, "failure")}
            {s.failures.length > 0 && ` (${s.failures.map((f) => `${f.robot}@${fmtNum(f.t_h, 1)} h`).join(", ")})`} ·{" "}
            {plural(s.replans, "replan")}
          </li>
        ))}
        {missed.length > shown.length && <li>… and {missed.length - shown.length} more</li>}
      </ul>
    </details>
  );
}

// ------------------------------------------------------------------ what-if comparison
function Comparison({
  baseline,
  baselineAttribution,
  whatIf,
  plan,
  seed,
  onRetry,
  onAdopt,
}: {
  baseline: MonteCarloResult;
  baselineAttribution: Attribution;
  whatIf: WhatIf;
  plan: PlanResult;
  seed: number | null;
  onRetry: () => void;
  onAdopt?: (requirements: Requirements) => void;
}) {
  const { status, result, spares, request } = whatIf;
  const fleet = request.plan.requirements.robots;
  const finA = useMemo(() => finishTimes(baseline), [baseline]);
  const finB = useMemo(() => (result ? finishTimes(result) : []), [result]);
  const axes = useMemo(() => sharedAxes([finA, finB]), [finA, finB]);
  const attribution = useMemo(() => (result ? attributeOutcomes(result, fleet) : null), [result, fleet]);
  const ids = spares.map((r) => r.id);
  const heading = (
    <>
      What if <span className="font-mono">{ids.map((id) => `+${id}`).join(" ")}</span>
    </>
  );
  const note = `${plural(fleet.length, "robot")} · same ${seed === null ? "seed" : `seed ${seed}`}, failure rate and jitter${
    result ? ` · ${plural(result.runs, "replay")} in ${fmtSeconds(result.elapsed_s)}` : ""
  }`;
  const retry = (
    <Button size="sm" onClick={onRetry} className="mt-2">
      Retry
    </Button>
  );

  return (
    <div className="mt-5 grid gap-5 lg:grid-cols-2">
      <ResultView
        heading="Baseline"
        note={resultNote(baseline, plan.requirements.robots.length, seed)}
        result={baseline}
        plan={plan}
        attribution={baselineAttribution}
        layout="stacked"
        axes={axes}
      />
      {result && attribution ? (
        <div className="relative">
          <ResultView
            heading={heading}
            note={note}
            result={result}
            plan={request.plan}
            attribution={attribution}
            layout="stacked"
            axes={axes}
            compareTo={baseline}
            className={status === "running" ? "opacity-50 transition-opacity" : "transition-opacity"}
          />
          {status === "running" && (
            <div className="pointer-events-none absolute inset-x-0 top-14 flex justify-center" role="status">
              <span className="inline-flex items-center gap-2 rounded-full border border-line bg-raised px-3 py-1 text-xs text-muted shadow-lg">
                <Spinner className="size-3 text-accent" />
                re-running {plural(request.runs ?? baseline.runs, "replay")} with {ids.join(", ")}…
              </span>
            </div>
          )}
          {status === "error" && (
            <Notice tone="fail" title="What-if run failed" className="mt-3">
              {whatIf.error}
              <div>{retry}</div>
            </Notice>
          )}
          {status === "done" && onAdopt && (
            <div className="mt-3">
              <Button size="sm" onClick={() => onAdopt(request.plan.requirements)}>
                Plan with these requirements
              </Button>
            </div>
          )}
        </div>
      ) : (
        <div className="flex flex-col rounded-md border border-line p-4">
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <h3 className="text-[13px] font-medium">{heading}</h3>
            <span className="text-[11px] text-dim">{note}</span>
          </div>
          {status === "error" ? (
            <Notice tone="fail" title="What-if run failed">
              {whatIf.error}
              <div>{retry}</div>
            </Notice>
          ) : (
            <div
              role="status"
              className="flex min-h-72 flex-1 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-white/10 px-4 text-center text-[13px] text-muted"
            >
              <Spinner className="size-5 text-accent" />
              <span>
                Replaying {plural(request.runs ?? baseline.runs, "run")} with {ids.join(", ")} in the fleet…
              </span>
              <span className="text-[11px] text-dim">
                same {seed === null ? "seed" : `seed ${seed}`}, failure rate and jitter as the baseline; only the replans
                can use the spare
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ histogram (hand-rolled SVG)
const W = 640;
const H = 252;
const PAD = { l: 44, r: 16, t: 30, b: 46 };
/** viewBox units; the panel renders the 640-unit box at 560–630 CSS px, so this is 11–12 px */
const AXIS_FONT = 12.5;

function niceStep(range: number, maxTicks: number): number {
  const rough = range / Math.max(1, maxTicks);
  const pow = Math.pow(10, Math.floor(Math.log10(Math.max(rough, 1e-9))));
  for (const m of [1, 2, 2.5, 5, 10]) {
    const s = m * pow;
    if (s >= rough) return s;
  }
  return 10 * pow;
}

function ticks(lo: number, hi: number, step: number): number[] {
  const out: number[] = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) out.push(Number(t.toFixed(6)));
  return out;
}

const BIN_WIDTHS_H = [0.25, 0.5, 1, 2, 4, 6, 12, 24, 48, 168];

export interface BinGrid {
  start: number;
  width: number;
  n: number;
}

export interface SharedAxes {
  grid: BinGrid;
  maxCount: number;
}

/** Equal-width bins over the finish times themselves. The backend's bins are horizon / 12
 *  wide (28 h for a 336 h horizon), which puts every finish of a plan that ends in a quarter
 *  of its horizon into one bar; here the width is the nice hour step that gives roughly
 *  sqrt(n) bins between the earliest and the latest finish. */
export function binGrid(finishes: number[]): BinGrid | null {
  if (finishes.length === 0) return null;
  const lo = Math.min(...finishes);
  const hi = Math.max(...finishes);
  const target = clamp(Math.round(Math.sqrt(finishes.length) * 1.6), 6, 16);
  const rough = (hi - lo) / target;
  const width = BIN_WIDTHS_H.find((w) => w >= rough) ?? Math.ceil(rough / 168) * 168;
  const start = Math.floor(lo / width + 1e-9) * width;
  const n = Math.floor((hi - start) / width + 1e-9) + 1;
  return { start, width, n };
}

/** Counts per bin of `grid` (default: the grid of these finishes). Bins are [start, end)
 *  except the last, which includes the latest finish; a finish outside a shared grid lands in
 *  its edge bin. */
export function binFinishes(finishes: number[], grid: BinGrid | null = binGrid(finishes)): HistogramBin[] {
  if (!grid) return [];
  const { start, width, n } = grid;
  const counts = new Array<number>(n).fill(0);
  for (const f of finishes) counts[clamp(Math.floor((f - start) / width + 1e-9), 0, n - 1)] += 1;
  return counts.map((count, k) => ({
    start_h: Number((start + k * width).toFixed(3)),
    end_h: Number((start + (k + 1) * width).toFixed(3)),
    count,
  }));
}

/** One grid and one y-range for several finish-time sets, so their histograms share axes. */
export function sharedAxes(sets: number[][]): SharedAxes | null {
  const grid = binGrid(sets.flat());
  if (!grid) return null;
  const maxCount = Math.max(0, ...sets.map((s) => Math.max(0, ...binFinishes(s, grid).map((b) => b.count))));
  return { grid, maxCount };
}

function Histogram({
  finishes,
  horizonH,
  makespanH,
  runs,
  axes,
  className,
}: {
  finishes: number[];
  horizonH: number;
  makespanH: number | null;
  runs: number;
  axes: SharedAxes | null;
  className?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const uid = useId().replace(/[^A-Za-z0-9_-]/g, "");
  const bins = useMemo(() => binFinishes(finishes, axes?.grid ?? binGrid(finishes)), [finishes, axes]);
  const header = (
    <div className="mb-1 flex items-baseline justify-between gap-3">
      <h3 className="text-[13px] font-medium">Finish time distribution</h3>
      {bins.length > 0 && (
        <span className="text-[11px] text-dim">completed runs per {fmtNum(bins[0].end_h - bins[0].start_h, 2)} h bin</span>
      )}
    </div>
  );
  if (bins.length === 0) {
    return (
      <div className={className}>
        {header}
        <div className="flex min-h-40 items-center justify-center rounded-md border border-dashed border-white/10 text-xs text-dim">
          No run completed, so there are no finish times to plot.
        </div>
      </div>
    );
  }
  const dataMin = Math.min(bins[0].start_h, makespanH ?? Infinity);
  const finishMax = bins[bins.length - 1].end_h;
  // A horizon far beyond every finish time would squeeze the distribution into a corner:
  // then clip the axis and pin the horizon marker to the right edge with an arrow.
  const horizonOffChart = horizonH > finishMax * 1.6;
  const dataMax = horizonOffChart ? finishMax * 1.18 : Math.max(finishMax, horizonH);
  const span = Math.max(1, dataMax - dataMin);
  const x0 = Math.max(0, dataMin - span * 0.04);
  const x1 = dataMax + span * 0.06;
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const baseline = PAD.t + plotH;
  const xs = (h: number) => PAD.l + ((h - x0) / (x1 - x0)) * plotW;
  const maxCount = Math.max(1, axes?.maxCount ?? 0, ...bins.map((b) => b.count));
  const yStep = niceStep(maxCount, 4);
  const yMax = Math.max(yStep, Math.ceil(maxCount / yStep) * yStep);
  const ys = (c: number) => PAD.t + (1 - c / yMax) * plotH;
  const xStep = niceStep(x1 - x0, 7);
  const xTicks = ticks(x0, x1, xStep);
  const yTicks = ticks(0, yMax, yStep);
  const hovered = hover !== null ? bins[hover] : null;
  const horizonX = horizonOffChart ? PAD.l + plotW : xs(horizonH);
  const horizonLabelAtEnd = horizonX > PAD.l + plotW * 0.7;
  const makespanX = makespanH !== null ? xs(makespanH) : null;
  const makespanLabelAtEnd = makespanX !== null && makespanX > PAD.l + plotW * 0.7;
  const clipIn = `${uid}-in`;
  const clipOut = `${uid}-out`;

  const bars = bins.map((b) => {
    const left = xs(b.start_h);
    const right = xs(b.end_h);
    const bx = left + 1;
    const bw = Math.max(1, right - left - 2);
    const top = ys(b.count);
    const bh = Math.max(0, baseline - top);
    const r = Math.min(4, bw / 2, bh);
    const path =
      bh <= 0
        ? ""
        : `M${bx},${baseline} V${top + r} a${r},${r} 0 0 1 ${r},${-r} H${bx + bw - r} a${r},${r} 0 0 1 ${r},${r} V${baseline} Z`;
    return { left, right, path };
  });
  const binText = (b: HistogramBin) =>
    `${fmtNum(b.start_h, 1)}–${fmtNum(b.end_h, 1)} h: ${plural(b.count, "run")} (${fmtPercent(runs > 0 ? b.count / runs : 0)})${
      b.start_h < horizonH && b.end_h > horizonH ? " · straddles the horizon" : ""
    }`;
  const tickFill = "var(--fg-dim)";
  const mono = "var(--font-mono)";

  return (
    <div className={className}>
      {header}
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Histogram of finish times over ${runs} runs with the ${horizonH} hour horizon marked`}
        className="h-auto w-full select-none"
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <clipPath id={clipIn}>
            <rect x={0} y={0} width={Math.max(0, horizonX)} height={H} />
          </clipPath>
          <clipPath id={clipOut}>
            <rect x={horizonX} y={0} width={Math.max(0, W - horizonX)} height={H} />
          </clipPath>
        </defs>

        {yTicks.map((t) => (
          <g key={`y${t}`}>
            <line x1={PAD.l} x2={PAD.l + plotW} y1={ys(t)} y2={ys(t)} stroke="rgba(255,255,255,0.08)" strokeWidth={1} />
            <text x={PAD.l - 6} y={ys(t)} dy="0.32em" textAnchor="end" fontSize={AXIS_FONT} fill={tickFill} fontFamily={mono}>
              {t}
            </text>
          </g>
        ))}
        <line x1={PAD.l} x2={PAD.l + plotW} y1={baseline} y2={baseline} stroke="rgba(255,255,255,0.18)" strokeWidth={1} />
        {xTicks.map((t) => (
          <text key={`x${t}`} x={xs(t)} y={baseline + 14} textAnchor="middle" fontSize={AXIS_FONT} fill={tickFill} fontFamily={mono}>
            {t}
          </text>
        ))}
        <text x={PAD.l + plotW} y={H - 8} textAnchor="end" fontSize={AXIS_FONT} fill={tickFill} fontFamily={mono}>
          finish time (h)
        </text>
        <text x={PAD.l - 6} y={PAD.t - 12} textAnchor="end" fontSize={AXIS_FONT} fill={tickFill} fontFamily={mono}>
          runs
        </text>

        {/* bars inside the horizon in muted blue, past it in red; a bin that straddles the
            horizon is painted on each side of the marker by the two clips */}
        <g clipPath={`url(#${clipIn})`}>
          {bars.map(
            (b, i) =>
              b.path && (
                <path key={i} d={b.path} fill="var(--info)" fillOpacity={hover === null || hover === i ? 0.7 : 0.35} />
              ),
          )}
        </g>
        <g clipPath={`url(#${clipOut})`}>
          {bars.map(
            (b, i) =>
              b.path && (
                <path key={i} d={b.path} fill="var(--fail)" fillOpacity={hover === null || hover === i ? 0.85 : 0.4} />
              ),
          )}
        </g>
        {bars.map((b, i) => (
          <rect
            key={i}
            x={b.left}
            y={PAD.t}
            width={Math.max(1, b.right - b.left)}
            height={plotH}
            fill="transparent"
            stroke={hover === i ? "var(--fg)" : "none"}
            strokeWidth={1}
            strokeDasharray="3 2"
            tabIndex={0}
            role="img"
            aria-label={binText(bins[i])}
            className="outline-none"
            onMouseEnter={() => setHover(i)}
            onFocus={() => setHover(i)}
            onBlur={() => setHover(null)}
          >
            <title>{binText(bins[i])}</title>
          </rect>
        ))}

        {makespanX !== null && makespanH !== null && (
          <g>
            <line x1={makespanX} x2={makespanX} y1={PAD.t - 4} y2={baseline} stroke="var(--fg-muted)" strokeWidth={1} strokeDasharray="2 3" />
            <text
              x={makespanX + (makespanLabelAtEnd ? -4 : 4)}
              y={PAD.t - 12}
              textAnchor={makespanLabelAtEnd ? "end" : "start"}
              fontSize={AXIS_FONT}
              fill="var(--fg-muted)"
              fontFamily={mono}
            >
              plan {fmtNum(makespanH, 0)} h
            </text>
          </g>
        )}
        <g>
          <line x1={horizonX} x2={horizonX} y1={PAD.t - 4} y2={baseline} stroke="var(--fail)" strokeWidth={1.5} strokeDasharray="4 3" />
          <text
            x={horizonX + (horizonLabelAtEnd ? -5 : 5)}
            y={PAD.t + 2}
            textAnchor={horizonLabelAtEnd ? "end" : "start"}
            fontSize={AXIS_FONT}
            fontWeight={600}
            fill="var(--fg)"
            fontFamily={mono}
          >
            horizon {fmtNum(horizonH, 0)} h{horizonOffChart ? " →" : ""}
          </text>
        </g>
      </svg>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px] text-dim">
        <span className="font-mono tabular-nums text-muted">{hovered ? binText(hovered) : "hover or tab to a bar for its count"}</span>
        <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="inline-flex items-center gap-1">
            <i className="inline-block size-2 rounded-sm" style={{ background: "var(--info)", opacity: 0.7 }} aria-hidden="true" />
            inside the horizon
          </span>
          <span className="inline-flex items-center gap-1">
            <i className="inline-block size-2 rounded-sm" style={{ background: "var(--fail)", opacity: 0.85 }} aria-hidden="true" />
            past the horizon
          </span>
          <span>dashed: plan makespan · horizon</span>
        </span>
      </div>
    </div>
  );
}
