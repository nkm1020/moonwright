"use client";
/**
 * Dev harness for the simulation components, fed by the recorded landing_pad sample payloads.
 * The replay autoplays from 25 h so the 30 h failure, its banner and the hold come up within
 * seconds; two synthetic variants of the recorded replan (escalated to Nemotron, no verified
 * replan) exercise the other banner and event-list wordings without a backend. SimControls
 * keeps local state only (no backend call); the Run button fakes a 1.5 s run.
 */
import { useEffect, useRef, useState } from "react";
import type { Failure, LoopStep, PlanResult, ReplanRecord, SimResult } from "@/lib/types";
import { allTasks } from "@/lib/tasks";
import { BeforeAfter } from "@/components/BeforeAfter";
import { EventList, formatHours } from "@/components/EventList";
import { SimControls, DEFAULT_SIMULATE_OPTIONS, type SimulateOptions } from "@/components/SimControls";
import { HOLD_MS, PULSE_H, SimReplay } from "@/components/SimReplay";

interface Loaded {
  plan: PlanResult;
  sim: SimResult;
}

type Variant = "recorded" | "escalated" | "failed";

const VARIANTS: { id: Variant; label: string; blurb: string }[] = [
  { id: "recorded", label: "Recorded", blurb: "the recorded landing-pad simulation as served: CP-SAT only, 80 h → 95 h" },
  { id: "escalated", label: "Escalated (synthetic)", blurb: "same run, replan marked as escalated to Nemotron with one repair, 80 h → 99 h" },
  { id: "failed", label: "No verified replan (synthetic)", blurb: "same run, replan fails with an infeasibility core" },
];

const START_AT_H = 25;
const cardClass = "rounded-xl border border-line bg-raised p-4";

async function fetchJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

/** The recorded replan, re-told as an escalation: CP-SAT could not fit the remaining work
 *  (an energy core on EX1), Nemotron re-scoped it in one repair, CP-SAT re-verified a longer
 *  makespan. The trace reads like the backend's (validate, failed schedule with its budget
 *  row, repair, validate, schedule, closing replan step worded as sim/replan.py _outcome_step
 *  words it: no time-base flag, real plurals). Frames are left as recorded. */
function escalatedVariant(sim: SimResult): SimResult {
  const replans: ReplanRecord[] = sim.replans.map((r) => {
    const schedule = r.after.schedule ? { ...r.after.schedule, makespan_h: r.after.schedule.makespan_h + 4 } : null;
    const core: Failure = {
      kind: "infeasible",
      conflicts: ["energy:EX1"],
      hint: "EX1 would need at least 4,310 Wh, 25 Wh of it travel, but has 3,551 Wh.",
      details: [],
    };
    const steps: LoopStep[] = [];
    for (const st of r.after.steps) {
      if (st.stage === "schedule" && st.ok) {
        steps.push(
          {
            ...st,
            ok: false,
            failure: core,
            message: null,
            payload: {
              budget: [{ kind: "robot", subject: "EX1", robots: ["EX1"], budget_wh: 3551, demanded_wh: 4310, travel_wh: 25 }],
              offset_h: r.t_h,
              time_base: "relative",
            },
          },
          {
            stage: "repair",
            attempt: 2,
            ok: true,
            failure: null,
            duration_s: 21.4,
            usage: { model: "nvidia/Nemotron-3-Ultra-550b-a55b", input_tokens: 5200, output_tokens: 9800, latency_s: 21.4 },
            message: `${r.remaining_tasks.length} tasks`,
            payload: null,
          },
          {
            stage: "validate",
            attempt: 2,
            ok: true,
            failure: null,
            duration_s: 0.003,
            usage: null,
            message: `${r.remaining_tasks.length} tasks valid`,
            payload: { offset_h: r.t_h, time_base: "relative" },
          },
          {
            ...st,
            attempt: 2,
            message: `makespan ${schedule?.makespan_h ?? "—"}h (optimal)`,
            payload: st.payload && schedule ? { ...st.payload, makespan_h: schedule.makespan_h } : st.payload,
          },
        );
      } else if (st.stage === "replan") {
        steps.push({
          ...st,
          message: `infeasible (energy:EX1) -> escalated to Nemotron; makespan ${r.before.makespan_h} h -> ${schedule?.makespan_h ?? "—"} h, verified after 1 repair`,
          payload: { ...(st.payload ?? {}), escalated: true, after_makespan_h: schedule?.makespan_h ?? null, solve_time_s: 0.012 },
        });
      } else {
        steps.push(st);
      }
    }
    return { ...r, escalated: true, after: { ...r.after, repairs: 1, schedule, steps } };
  });
  return { ...sim, replans };
}

/** The recorded replan, re-told as a failure: two repairs, still infeasible, the run does not
 *  finish. Frames are left as recorded. The verdict and the closing message are worded like
 *  the backend's (schedule.py energy sentence with its budget row, replan.py _outcome_step). */
function failedVariant(sim: SimResult): SimResult {
  const failure: Failure = {
    kind: "infeasible",
    conflicts: ["energy:EX1"],
    hint: "EX1 would need at least 5,100 Wh, 40 Wh of it travel, but has 3,551 Wh.",
    details: [],
  };
  const budget = [{ kind: "robot", subject: "EX1", robots: ["EX1"], budget_wh: 3551, demanded_wh: 5100, travel_wh: 40 }];
  const replans: ReplanRecord[] = sim.replans.map((r) => {
    const steps: LoopStep[] = r.after.steps.map((st) =>
      st.stage === "replan"
        ? {
            ...st,
            ok: false,
            failure,
            message:
              "infeasible (energy:EX1) -> escalated to Nemotron (no answer: offline, no recorded repair); " +
              "no verified replan (infeasible: energy:EX1); the previous plan stays in force",
            payload: { ...(st.payload ?? {}), escalated: true, after_makespan_h: null, solve_time_s: null },
          }
        : st.stage === "schedule"
          ? { ...st, ok: false, failure, message: null, payload: { budget, offset_h: r.t_h, time_base: "relative" } }
          : st,
    );
    return {
      ...r,
      escalated: true,
      after: { ...r.after, success: false, schedule: null, repairs: 2, failure, steps },
    };
  });
  return { ...sim, replans, completed: false, finished_at_h: null, schedule: null };
}

function variantOf(sim: SimResult, variant: Variant): SimResult {
  switch (variant) {
    case "escalated":
      return escalatedVariant(sim);
    case "failed":
      return failedVariant(sim);
    default:
      return sim;
  }
}

export function SimDemo() {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [options, setOptions] = useState<SimulateOptions>(DEFAULT_SIMULATE_OPTIONS);
  const [running, setRunning] = useState(false);
  const [nowH, setNowH] = useState(0);
  const [seek, setSeek] = useState<{ t_h: number } | null>(null);
  const [lastSeek, setLastSeek] = useState<number | null>(null);
  const [variant, setVariant] = useState<Variant>("recorded");
  const [holdOnEvents, setHoldOnEvents] = useState(true);
  const [sims, setSims] = useState<Record<Variant, SimResult> | null>(null);
  const runTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    Promise.all([
      fetchJson<PlanResult>("/samples/landing_pad.plan.json", ac.signal),
      fetchJson<SimResult>("/samples/landing_pad.sim.json", ac.signal),
    ])
      .then(([plan, sim]) => {
        setData({ plan, sim });
        setSims({ recorded: sim, escalated: variantOf(sim, "escalated"), failed: variantOf(sim, "failed") });
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return;
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => ac.abort();
  }, []);

  useEffect(() => {
    return () => {
      if (runTimer.current) clearTimeout(runTimer.current);
    };
  }, []);

  const fakeRun = () => {
    setRunning(true);
    runTimer.current = setTimeout(() => setRunning(false), 1500);
  };

  if (error) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10">
        <p className="rounded-lg border border-fail/50 bg-fail/10 p-4 text-sm">Could not load the samples: {error}</p>
      </main>
    );
  }
  if (!data || !sims) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10 text-sm text-muted" aria-busy>
        Loading the recorded landing-pad plan and simulation…
      </main>
    );
  }

  const { plan } = data;
  const sim = sims[variant];
  const tasks = allTasks(plan, sim);
  const requirements = plan.requirements;
  const firstEvent = sim.events[0]?.t_h ?? null;

  const onSeek = (t_h: number) => {
    setSeek({ t_h });
    setLastSeek(t_h);
    setNowH(t_h);
  };

  const chip = (selected: boolean) =>
    `rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
      selected ? "border-accent/60 bg-accent/15 text-fg" : "border-white/15 text-muted hover:bg-white/5 hover:text-fg"
    }`;

  return (
    <main className="mx-auto w-full max-w-[1400px] space-y-6 px-4 py-6 font-sans">
      <header className="space-y-1">
        <p className="text-xs font-medium uppercase tracking-wide text-muted">Dev · simulation components</p>
        <h1 className="text-2xl font-semibold tracking-tight">
          {requirements.site.name}: {requirements.structures.map((s) => s.id).join(", ")} · {requirements.robots.length} robots ·{" "}
          {sim.frames.length} frames · {sim.events.length} event{sim.events.length === 1 ? "" : "s"} · {sim.replans.length} replan
          {sim.replans.length === 1 ? "" : "s"}
        </h1>
        <p className="text-sm text-muted">
          Caption: nowH = <span className="font-mono tabular-nums">{formatHours(nowH)}</span>
          {lastSeek !== null && (
            <>
              {" "}
              · last seek from the event list: <span className="font-mono tabular-nums">{formatHours(lastSeek)}</span>
            </>
          )}
        </p>
      </header>

      <section className={`${cardClass} space-y-3`} aria-label="Replay scenario">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted">Replan variant</span>
          {VARIANTS.map((v) => (
            <button key={v.id} type="button" aria-pressed={variant === v.id} onClick={() => setVariant(v.id)} className={chip(variant === v.id)} title={v.blurb}>
              {v.label}
            </button>
          ))}
          <span className="mx-1 h-4 w-px bg-white/15" aria-hidden />
          <label className="inline-flex cursor-pointer items-center gap-2 text-xs">
            <input type="checkbox" className="accent-accent" checked={holdOnEvents} onChange={(e) => setHoldOnEvents(e.target.checked)} />
            hold on events ({(HOLD_MS / 1000).toFixed(1)} s)
          </label>
          <span className="mx-1 h-4 w-px bg-white/15" aria-hidden />
          <button type="button" className={chip(false)} onClick={() => onSeek(Math.max(0, (firstEvent ?? START_AT_H) - PULSE_H - 0.5))}>
            Seek just before the banner
          </button>
          <button type="button" className={chip(false)} onClick={() => firstEvent !== null && onSeek(firstEvent)}>
            Seek to the event ({firstEvent !== null ? formatHours(firstEvent) : "—"})
          </button>
          <button type="button" className={chip(false)} onClick={() => firstEvent !== null && onSeek(firstEvent + 0.75)}>
            Mid-banner
          </button>
        </div>
        <p className="text-xs text-muted">
          {VARIANTS.find((v) => v.id === variant)?.blurb}. Autoplay starts at {formatHours(START_AT_H)}; the banner shows for ±{PULSE_H} h
          of simulated time around each event. Space continues a hold (or plays / pauses when the map has focus); ← → step 1 h, shift for 6 h.
        </p>
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,24rem)]">
        <section className={`${cardClass} min-w-0`} aria-label="2D replay">
          <SimReplay
            key={variant}
            sim={sim}
            plan={plan}
            tasks={tasks}
            autoPlay
            startAtH={START_AT_H}
            holdOnEvents={holdOnEvents}
            seekRequest={seek}
            onTimeChange={setNowH}
          />
        </section>
        <div className="space-y-6">
          <section className={cardClass} aria-label="Events">
            <h2 className="mb-2 text-sm font-semibold">Events and replans</h2>
            <EventList events={sim.events} replans={sim.replans} nowH={nowH} onSeek={onSeek} />
          </section>
          <section className={cardClass} aria-label="Simulation controls">
            <h2 className="mb-3 text-sm font-semibold">Run a simulation</h2>
            <SimControls
              requirements={requirements}
              horizonH={requirements.horizon_h}
              makespanH={plan.schedule?.makespan_h}
              value={options}
              onChange={setOptions}
              onRun={fakeRun}
              running={running}
            />
            <p className="mt-3 break-all text-xs text-muted">
              Dev page: Run only fakes a request. Options: {JSON.stringify(options)}
            </p>
          </section>
        </div>
      </div>

      <div className="space-y-6">
        {sim.replans.map((record, i) => (
          <BeforeAfter key={`${variant}-${i}`} record={record} requirements={requirements} tasks={tasks} index={i + 1} />
        ))}
        {sim.replans.length === 0 && (
          <p className="text-sm text-muted">No replans in this sample.</p>
        )}
      </div>
    </main>
  );
}
