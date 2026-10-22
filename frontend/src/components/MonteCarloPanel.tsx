"use client";
import { useMemo, useState } from "react";
import type { HistogramBin, MonteCarloRequest, MonteCarloResult, PlanResult } from "@/lib/types";
import { clamp, fmtHours, fmtNum, fmtPercent, fmtSeconds, plural } from "@/lib/format";
import { Button, Card, Field, Notice, NumberInput, Section, Stat, type Tone } from "./Section";

export interface MonteCarloPanelProps {
  plan: PlanResult;
  briefId: string | null;
  result: MonteCarloResult | null;
  running: boolean;
  error: string | null;
  onRun: (req: MonteCarloRequest) => void;
}

const isInt = (n: number, lo: number, hi: number) => Number.isInteger(n) && n >= lo && n <= hi;
const inUnit = (n: number) => Number.isFinite(n) && n >= 0 && n <= 1;

export function MonteCarloPanel({ plan, briefId, result, running, error, onRun }: MonteCarloPanelProps) {
  const [runs, setRuns] = useState(50);
  const [rate, setRate] = useState(0.002);
  const [jitter, setJitter] = useState(0.15);
  const [seed, setSeed] = useState<number>(NaN);
  const [maxReplans, setMaxReplans] = useState(2);

  const valid = {
    runs: isInt(runs, 1, 200),
    rate: inUnit(rate),
    jitter: inUnit(jitter),
    seed: !Number.isFinite(seed) || Number.isInteger(seed),
    maxReplans: isInt(maxReplans, 0, 5),
  };
  const allValid = Object.values(valid).every(Boolean);

  const submit = () => {
    if (!allValid) return;
    onRun({
      plan,
      runs,
      failure_rate_per_robot_h: rate,
      duration_jitter: jitter,
      max_replans: maxReplans,
      ...(Number.isFinite(seed) ? { seed } : {}),
      ...(briefId ? { brief_id: briefId } : {}),
    });
  };

  return (
    <Section
      id="robustness"
      step="05"
      title="Robustness"
      subtitle="Monte Carlo: the verified plan is replayed many times with random robot failures and task-duration jitter, replanning on every event. The headline is the probability of finishing within the horizon."
    >
      <Card>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-[repeat(5,minmax(0,1fr))_auto] xl:items-end">
          <Field label="Runs" hint="1–200" error={valid.runs ? undefined : "whole number 1–200"}>
            <NumberInput value={runs} min={1} max={200} step={1} invalid={!valid.runs} onChange={setRuns} />
          </Field>
          <Field label="Failure rate" hint="per robot-hour" error={valid.rate ? undefined : "0–1"}>
            <NumberInput value={rate} min={0} max={1} step={0.001} invalid={!valid.rate} onChange={setRate} />
          </Field>
          <Field label="Duration jitter" hint="relative spread, 0–1" error={valid.jitter ? undefined : "0–1"}>
            <NumberInput value={jitter} min={0} max={1} step={0.05} invalid={!valid.jitter} onChange={setJitter} />
          </Field>
          <Field label="Seed" hint="optional" error={valid.seed ? undefined : "whole number"}>
            <NumberInput value={seed} step={1} placeholder="random" invalid={!valid.seed} onChange={setSeed} />
          </Field>
          <Field label="Max replans" hint="per run, 0–5" error={valid.maxReplans ? undefined : "0–5"}>
            <NumberInput value={maxReplans} min={0} max={5} step={1} invalid={!valid.maxReplans} onChange={setMaxReplans} />
          </Field>
          <div className="col-span-2 flex md:col-span-3 xl:col-span-1 xl:block">
            <Button variant="primary" onClick={submit} loading={running} disabled={!allValid} className="w-full xl:w-auto">
              {running ? `Running ${runs} replays…` : "Run Monte Carlo"}
            </Button>
          </div>
        </div>

        {error && (
          <Notice tone="fail" title="Monte Carlo failed" className="mt-4">
            {error}
          </Notice>
        )}

        {result ? (
          <Results result={result} plan={plan} />
        ) : (
          !running &&
          !error && (
            <p className="mt-4 text-xs text-dim">
              Each run draws failures from the rate above, stretches task durations by the jitter,
              and replans the remaining work with the same solver checks as the main loop.
            </p>
          )
        )}
      </Card>
    </Section>
  );
}

function rateTone(x: number): Tone {
  if (x >= 0.9) return "ok";
  if (x >= 0.6) return "warn";
  return "fail";
}

function Results({ result, plan }: { result: MonteCarloResult; plan: PlanResult }) {
  const within = result.samples.filter((s) => s.within_horizon).length;
  const notCompleted = result.runs - result.completed;
  const late = result.samples.filter((s) => s.completed && !s.within_horizon);
  const missed = result.samples.filter((s) => !s.within_horizon).slice(0, 6);
  return (
    <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <div>
        <Stat
          hero
          label="Finished within horizon"
          value={fmtPercent(result.within_horizon_rate)}
          tone={rateTone(result.within_horizon_rate)}
          sub={`${within} of ${result.runs} runs done by ${fmtHours(result.horizon_h, 0)}`}
        />
        <div className="mt-5 grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-3">
          <Stat label="Completed" value={fmtPercent(result.completion_rate)} sub={`${result.completed} of ${result.runs}`} />
          <Stat label="p50 finish" value={fmtHours(result.finish_h.p50)} sub={`mean ${fmtHours(result.finish_h.mean)}`} />
          <Stat label="p90 finish" value={fmtHours(result.finish_h.p90)} sub={`max ${fmtHours(result.finish_h.max)}`} />
          <Stat label="Mean replans" value={fmtNum(result.replans_mean, 2)} sub={`${fmtNum(result.failures_mean, 2)} failures per run`} />
          <Stat label="Late / unfinished" value={`${late.length} / ${notCompleted}`} sub="runs" />
          <Stat label="Elapsed" value={fmtSeconds(result.elapsed_s)} sub={`${plural(result.runs, "replay")}`} />
        </div>
        {missed.length > 0 && (
          <details className="mt-4 text-xs text-muted">
            <summary className="cursor-pointer select-none hover:text-fg">
              Runs that missed the horizon ({result.runs - within})
            </summary>
            <ul className="mt-2 space-y-1 font-mono tabular-nums">
              {missed.map((s) => (
                <li key={s.seed}>
                  seed {s.seed}: {s.completed ? `finished ${fmtHours(s.finished_at_h)}` : "did not finish"} ·{" "}
                  {plural(s.failures.length, "failure")} ({s.failures.map((f) => `${f.robot}@${fmtNum(f.t_h, 1)}h`).join(", ") || "none"}) ·{" "}
                  {plural(s.replans, "replan")}
                </li>
              ))}
              {result.runs - within > missed.length && <li>… and {result.runs - within - missed.length} more</li>}
            </ul>
          </details>
        )}
      </div>
      <Histogram
        finishes={result.samples.flatMap((s) => (s.finished_at_h === null ? [] : [s.finished_at_h]))}
        serverBins={result.histogram}
        horizonH={result.horizon_h}
        makespanH={plan.schedule?.makespan_h ?? null}
        runs={result.runs}
      />
    </div>
  );
}

// ------------------------------------------------------------------ histogram (hand-rolled SVG)
const W = 640;
const H = 240;
const PAD = { l: 40, r: 16, t: 28, b: 36 };

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

/** Equal-width bins over the finish times themselves. The backend's bins are horizon / 12
 *  wide (28 h for a 336 h horizon), which puts every finish of a plan that ends in a quarter
 *  of its horizon into one bar; here the width is the nice hour step that gives roughly
 *  sqrt(n) bins between the earliest and the latest finish. Bins are [start, end) except the
 *  last, which includes the latest finish. */
export function binFinishes(finishes: number[]): HistogramBin[] {
  if (finishes.length === 0) return [];
  const lo = Math.min(...finishes);
  const hi = Math.max(...finishes);
  const target = clamp(Math.round(Math.sqrt(finishes.length) * 1.6), 6, 16);
  const rough = (hi - lo) / target;
  const width = BIN_WIDTHS_H.find((w) => w >= rough) ?? Math.ceil(rough / 168) * 168;
  const start = Math.floor(lo / width + 1e-9) * width;
  const n = Math.floor((hi - start) / width + 1e-9) + 1;
  const counts = new Array<number>(n).fill(0);
  for (const f of finishes) counts[Math.min(n - 1, Math.floor((f - start) / width + 1e-9))] += 1;
  return counts.map((count, k) => ({
    start_h: Number((start + k * width).toFixed(3)),
    end_h: Number((start + (k + 1) * width).toFixed(3)),
    count,
  }));
}

function binColor(b: HistogramBin, horizonH: number): string {
  if (b.end_h <= horizonH) return "var(--accent)";
  if (b.start_h >= horizonH) return "var(--fail)";
  return "var(--warn)";
}

function Histogram({
  finishes,
  serverBins,
  horizonH,
  makespanH,
  runs,
}: {
  finishes: number[];
  /** The backend's horizon / 12 bins, listed in the details table for reference. */
  serverBins: HistogramBin[];
  horizonH: number;
  makespanH: number | null;
  runs: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const bins = useMemo(() => binFinishes(finishes), [finishes]);
  if (bins.length === 0) {
    return (
      <div className="flex min-h-40 items-center justify-center rounded-md border border-dashed border-white/10 text-xs text-dim">
        No run completed, so there are no finish times to plot.
      </div>
    );
  }
  const dataMin = Math.min(bins[0].start_h, makespanH ?? Infinity);
  const finishMax = bins[bins.length - 1].end_h;
  // A horizon far beyond every finish time would squeeze the distribution into a corner:
  // then clip the axis and pin the horizon marker to the right edge with an arrow.
  const horizonOffChart = horizonH > finishMax * 1.6;
  const dataMax = horizonOffChart ? finishMax * 1.25 : Math.max(finishMax, horizonH);
  const span = Math.max(1, dataMax - dataMin);
  const x0 = Math.max(0, dataMin - span * 0.04);
  const x1 = dataMax + span * 0.06;
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const baseline = PAD.t + plotH;
  const xs = (h: number) => PAD.l + ((h - x0) / (x1 - x0)) * plotW;
  const maxCount = Math.max(1, ...bins.map((b) => b.count));
  const yStep = niceStep(maxCount, 4);
  const yMax = Math.max(yStep, Math.ceil(maxCount / yStep) * yStep);
  const ys = (c: number) => PAD.t + (1 - c / yMax) * plotH;
  const xStep = niceStep(x1 - x0, 7);
  const xTicks = ticks(x0, x1, xStep);
  const yTicks = ticks(0, yMax, yStep);
  const hovered = hover !== null ? bins[hover] : null;
  const horizonX = horizonOffChart ? PAD.l + plotW : xs(horizonH);
  const labelAtEnd = horizonX > PAD.l + plotW * 0.7;

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] font-medium">Finish time distribution</h3>
        <span className="text-[11px] text-dim">
          completed runs per {fmtNum(bins[0].end_h - bins[0].start_h, 2)} h bin
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Histogram of finish times over ${runs} runs with the ${horizonH} hour horizon marked`}
        className="h-auto w-full select-none"
        onMouseLeave={() => setHover(null)}
      >
        {yTicks.map((t) => (
          <g key={`y${t}`}>
            <line x1={PAD.l} x2={PAD.l + plotW} y1={ys(t)} y2={ys(t)} stroke="rgba(255,255,255,0.08)" strokeWidth={1} />
            <text x={PAD.l - 6} y={ys(t)} dy="0.32em" textAnchor="end" fontSize={10} fill="var(--fg-dim)" fontFamily="var(--font-mono)">
              {t}
            </text>
          </g>
        ))}
        <line x1={PAD.l} x2={PAD.l + plotW} y1={baseline} y2={baseline} stroke="rgba(255,255,255,0.18)" strokeWidth={1} />
        {xTicks.map((t) => (
          <text key={`x${t}`} x={xs(t)} y={baseline + 14} textAnchor="middle" fontSize={10} fill="var(--fg-dim)" fontFamily="var(--font-mono)">
            {t} h
          </text>
        ))}

        {bins.map((b, i) => {
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
          const share = runs > 0 ? b.count / runs : 0;
          return (
            <g key={i} onMouseEnter={() => setHover(i)}>
              {path && <path d={path} fill={binColor(b, horizonH)} opacity={hover === null || hover === i ? 0.95 : 0.45} />}
              <rect x={left} y={PAD.t} width={Math.max(1, right - left)} height={plotH} fill="transparent">
                <title>{`${fmtNum(b.start_h, 1)}–${fmtNum(b.end_h, 1)} h: ${plural(b.count, "run")} (${fmtPercent(share)})`}</title>
              </rect>
            </g>
          );
        })}

        {makespanH !== null && (
          <g>
            <line x1={xs(makespanH)} x2={xs(makespanH)} y1={PAD.t - 4} y2={baseline} stroke="var(--fg-muted)" strokeWidth={1} strokeDasharray="2 3" />
            <text x={xs(makespanH) + (labelAtEnd ? -4 : 4)} y={PAD.t - 10} textAnchor={labelAtEnd ? "end" : "start"} fontSize={10} fill="var(--fg-muted)" fontFamily="var(--font-mono)">
              plan {fmtNum(makespanH, 0)} h
            </text>
          </g>
        )}
        <g>
          <line x1={horizonX} x2={horizonX} y1={PAD.t - 4} y2={baseline} stroke="var(--fail)" strokeWidth={1.5} strokeDasharray="4 3" />
          <text x={horizonX + (labelAtEnd ? -5 : 5)} y={PAD.t + 2} textAnchor={labelAtEnd ? "end" : "start"} fontSize={10} fontWeight={600} fill="var(--fg)" fontFamily="var(--font-mono)">
            horizon {fmtNum(horizonH, 0)} h{horizonOffChart ? " →" : ""}
          </text>
        </g>
      </svg>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-[11px] text-dim">
        <span className="font-mono tabular-nums text-muted">
          {hovered
            ? `${fmtNum(hovered.start_h, 1)}–${fmtNum(hovered.end_h, 1)} h: ${plural(hovered.count, "run")} (${fmtPercent(hovered.count / Math.max(1, runs))})`
            : "hover a bar for its count"}
        </span>
        <span>amber: bin inside the horizon · red: bin past the horizon · dashed: plan makespan</span>
      </div>
      <details className="mt-2 text-xs text-muted">
        <summary className="cursor-pointer select-none hover:text-fg">
          Bins as a table (backend bins, horizon / 12 wide)
        </summary>
        <table className="mt-2 w-full max-w-sm text-left font-mono tabular-nums">
          <thead>
            <tr className="text-[11px] uppercase tracking-wider text-dim">
              <th className="py-0.5 font-medium">from (h)</th>
              <th className="py-0.5 font-medium">to (h)</th>
              <th className="py-0.5 text-right font-medium">runs</th>
            </tr>
          </thead>
          <tbody>
            {serverBins.map((b, i) => (
              <tr key={i}>
                <td className="py-0.5">{fmtNum(b.start_h, 1)}</td>
                <td className="py-0.5">{fmtNum(b.end_h, 1)}</td>
                <td className="py-0.5 text-right">{b.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
