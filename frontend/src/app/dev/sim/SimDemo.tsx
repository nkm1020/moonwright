"use client";
/**
 * Dev harness for the simulation components, fed by the recorded landing_pad sample payloads.
 * SimControls keeps local state only (no backend call); the Run button fakes a 1.5 s run.
 */
import { useEffect, useRef, useState } from "react";
import type { PlanResult, SimResult } from "@/lib/types";
import { allTasks } from "@/lib/tasks";
import { BeforeAfter } from "@/components/BeforeAfter";
import { EventList, formatHours } from "@/components/EventList";
import { SimControls, DEFAULT_SIMULATE_OPTIONS, type SimulateOptions } from "@/components/SimControls";
import { SimReplay } from "@/components/SimReplay";

interface Loaded {
  plan: PlanResult;
  sim: SimResult;
}

const cardClass = "rounded-xl border border-line bg-raised p-4";

async function fetchJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

export function SimDemo() {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [options, setOptions] = useState<SimulateOptions>(DEFAULT_SIMULATE_OPTIONS);
  const [running, setRunning] = useState(false);
  const [nowH, setNowH] = useState(0);
  const [seek, setSeek] = useState<{ t_h: number } | null>(null);
  const [lastSeek, setLastSeek] = useState<number | null>(null);
  const runTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    Promise.all([
      fetchJson<PlanResult>("/samples/landing_pad.plan.json", ac.signal),
      fetchJson<SimResult>("/samples/landing_pad.sim.json", ac.signal),
    ])
      .then(([plan, sim]) => setData({ plan, sim }))
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
  if (!data) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10 text-sm text-muted" aria-busy>
        Loading /samples/landing_pad.plan.json and landing_pad.sim.json…
      </main>
    );
  }

  const { plan, sim } = data;
  const tasks = allTasks(plan, sim);
  const requirements = plan.requirements;

  const onSeek = (t_h: number) => {
    setSeek({ t_h });
    setLastSeek(t_h);
    setNowH(t_h);
  };

  return (
    <main className="mx-auto w-full max-w-[1400px] space-y-6 px-4 py-6 font-sans">
      <header className="space-y-1">
        <p className="text-xs font-medium uppercase tracking-wide text-muted">Dev · simulation components</p>
        <h1 className="text-2xl font-semibold tracking-tight">
          {requirements.site.name}: {requirements.structures.map((s) => s.id).join(", ")} · {requirements.robots.length} robots ·{" "}
          {sim.frames.length} frames · {sim.events.length} events · {sim.replans.length} replan{sim.replans.length === 1 ? "" : "s"}
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

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,24rem)]">
        <section className={`${cardClass} min-w-0`} aria-label="2D replay">
          <SimReplay sim={sim} plan={plan} tasks={tasks} autoPlay seekRequest={seek} onTimeChange={setNowH} />
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
          <BeforeAfter key={i} record={record} requirements={requirements} tasks={tasks} index={i + 1} />
        ))}
        {sim.replans.length === 0 && (
          <p className="text-sm text-muted">No replans in this sample.</p>
        )}
      </div>
    </main>
  );
}
