"use client";
/**
 * Dev harness for the Gantt and TaskTable components: loads the recorded landing-pad plan and
 * its simulation from public/samples and renders the full chart, the compact side-by-side form
 * (the plan beside the schedule after the recorded replan), the "now + marker" form used during
 * replay, and a set of edge cases (missing task, empty schedule, 12 robots, deadline in / off
 * plot). The hours in the captions are read from the recording.
 */
import { useEffect, useState } from "react";
import { Gantt } from "@/components/Gantt";
import { TaskTable } from "@/components/TaskTable";
import { sampleUrl } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { EVENT_COLORS } from "@/lib/palette";
import { allTasks, zoneOrder } from "@/lib/tasks";
import type { PlanResult, Requirements, Schedule, SimResult } from "@/lib/types";

interface Loaded {
  landing_pad: PlanResult;
  sim: SimResult | null;
}

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

const EMPTY_SCHEDULE: Schedule = {
  entries: [],
  makespan_h: 0,
  status: "feasible",
  objective_bound_h: null,
  solve_time_s: 0,
};

/** 12 robots with the landing-pad entries dealt round-robin: exercises the scrolling body. */
function manyRobots(plan: PlanResult): { requirements: Requirements; schedule: Schedule } {
  const base = plan.requirements.robots;
  const robots = Array.from({ length: 12 }, (_, i) => ({
    ...base[i % base.length],
    id: `R${String(i + 1).padStart(2, "0")}`,
  }));
  const entries = (plan.schedule?.entries ?? []).map((e, i) => ({ ...e, robot: robots[i % 12].id }));
  return {
    requirements: { ...plan.requirements, robots },
    schedule: { ...(plan.schedule ?? EMPTY_SCHEDULE), entries },
  };
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-white/10 bg-white/[0.02] p-4">
      <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
      {hint ? <p className="mb-3 mt-0.5 text-[11px] text-slate-400">{hint}</p> : <div className="mb-3" />}
      {children}
    </section>
  );
}

export function GanttDemo() {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selA, setSelA] = useState<string | null>(null);
  const [selB, setSelB] = useState<string | null>(null);
  const [selEdge, setSelEdge] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetchJson<PlanResult>(sampleUrl("landing_pad", "plan")),
      fetchJson<SimResult>(sampleUrl("landing_pad", "sim")).catch(() => null),
    ])
      .then(([landing_pad, sim]) => {
        if (!cancelled) setData({ landing_pad, sim });
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="min-h-screen bg-[#0b0f14] px-4 py-6 text-[#e5e7eb] sm:px-8">
      <div className="mx-auto flex max-w-6xl flex-col gap-6">
        <header>
          <p className="text-[11px] uppercase tracking-widest text-slate-500">Moonwright · dev</p>
          <h1 className="text-xl font-semibold">Gantt and task table</h1>
          <p className="mt-1 text-xs text-slate-400">
            Rendered from the bundled recorded plans. Hover or focus a bar for details, click to select a task and
            see its precedence arrows, Escape to clear. The table and the charts of a section share one selection.
          </p>
        </header>

        {error ? (
          <div className="rounded-md border border-red-400/40 bg-red-400/10 p-3 text-sm text-red-200">
            Could not load the recorded plans: {error}
          </div>
        ) : null}
        {!data && !error ? <div className="text-sm text-slate-400">Loading sample plans…</div> : null}

        {data && data.landing_pad.schedule ? (
          <LoadedSections
            data={data}
            selA={selA}
            setSelA={setSelA}
            selB={selB}
            setSelB={setSelB}
            selEdge={selEdge}
            setSelEdge={setSelEdge}
          />
        ) : null}
      </div>
    </div>
  );
}

function LoadedSections({
  data,
  selA,
  setSelA,
  selB,
  setSelB,
  selEdge,
  setSelEdge,
}: {
  data: Loaded;
  selA: string | null;
  setSelA: (id: string | null) => void;
  selB: string | null;
  setSelB: (id: string | null) => void;
  selEdge: string | null;
  setSelEdge: (id: string | null) => void;
}) {
  const lp = data.landing_pad;
  // the original graph for the plan views; the replan views merge the re-scoped tasks of the sim
  const lpTasks = allTasks(lp);
  const lpSimTasks = allTasks(lp, data.sim);
  const lpZones = zoneOrder(lp, lpSimTasks);
  const lpSchedule = lp.schedule as Schedule;
  const replan = data.sim?.replans[0] ?? null;
  // the schedule the recorded run finished on: the replan's, else the plan's own
  const finalSchedule = data.sim?.schedule ?? lpSchedule;
  // the recorded failure marks the replay views (else a failure 40 % into the plan)
  const failure = data.sim?.events.find((e) => e.kind === "robot_failure") ?? null;
  const failAt = failure?.t_h ?? Math.round(lpSchedule.makespan_h * 0.4);
  const failLabel = `${failure?.robot ?? "EX2"} failed`;
  const horizonRatio = lpSchedule.makespan_h > 0 ? lp.requirements.horizon_h / lpSchedule.makespan_h : 0;
  // a deadline 1.2x the makespan: inside the plot
  const nearHorizon = Math.round(lpSchedule.makespan_h * 1.2);
  const many = manyRobots(lp);
  const withMissing: Schedule = {
    ...lpSchedule,
    entries: [
      ...lpSchedule.entries,
      { task: "T99", robot: "HL1", start_h: 10, end_h: 22 },
      { task: "T7", robot: "HL9", start_h: 60, end_h: 70 },
    ],
  };

  return (
    <>
      <Section
        title="Landing pad - verified plan"
        hint={`Full Gantt + table, shared selection. The ${fmtNum(lp.requirements.horizon_h, 0)} h horizon is ${fmtNum(horizonRatio, 1)}x the makespan, so the deadline is a badge, not a line.`}
      >
        <Gantt
          schedule={lpSchedule}
          tasks={lpTasks}
          requirements={lp.requirements}
          title={`${lp.requirements.site.name} · landing pad`}
          selectedTask={selA}
          onSelectTask={setSelA}
          zoneIds={lpZones}
        />
        <div className="mt-4">
          <TaskTable
            tasks={lpTasks}
            schedule={lpSchedule}
            requirements={lp.requirements}
            selectedTask={selA}
            onSelectTask={setSelA}
            zoneIds={lpZones}
          />
        </div>
      </Section>

      <Section
        title={`Replay view: now = ${fmtNum(failAt, 1)} h, marker at ${fmtNum(failAt, 1)} h`}
        hint="The white now line and a robot failure marker share the same hour; labels are nudged apart."
      >
        <Gantt
          schedule={lpSchedule}
          tasks={lpTasks}
          requirements={lp.requirements}
          title={`Landing pad · simulated to ${fmtNum(failAt, 1)} h`}
          nowH={failAt}
          markers={[{ t_h: failAt, label: failLabel, color: EVENT_COLORS.robot_failure }]}
          selectedTask={selA}
          onSelectTask={setSelA}
          zoneIds={lpZones}
        />
      </Section>

      <Section
        title="Compact, side by side"
        hint="compact=true: shorter rows, no in-bar type labels, tooltips on one line. Left: the verified plan, right: the schedule the recorded simulation finished on."
      >
        <div className="grid gap-4 md:grid-cols-2">
          <div className="min-w-0 rounded-md border border-white/10 p-2">
            <Gantt
              schedule={lpSchedule}
              tasks={lpTasks}
              requirements={lp.requirements}
              title="Landing pad"
              compact
              highlightTasks={selA ? [selA] : []}
              selectedTask={selA}
              onSelectTask={setSelA}
              zoneIds={lpZones}
            />
          </div>
          <div className="min-w-0 rounded-md border border-white/10 p-2">
            <Gantt
              schedule={finalSchedule}
              tasks={lpSimTasks}
              requirements={lp.requirements}
              title="After the replan"
              compact
              selectedTask={selB}
              onSelectTask={setSelB}
              zoneIds={lpZones}
            />
          </div>
        </div>
      </Section>

      {replan && replan.after.schedule ? (
        <Section
          title="Replan #1 before / after (the recorded landing-pad simulation)"
          hint={`Both charts share the scale through minSpanH; the remaining tasks are highlighted; the failed robot ${replan.event.robot ?? ""} keeps an empty row on the right because it is gone from the replanned requirements.`}
        >
          <div className="grid gap-4 md:grid-cols-2">
            <div className="min-w-0 rounded-md border border-white/10 p-2">
              <Gantt
                schedule={replan.before}
                tasks={lpSimTasks}
                requirements={lp.requirements}
                title={`Before · makespan ${replan.before.makespan_h} h`}
                compact
                nowH={replan.t_h}
                markers={[{ t_h: replan.event.t_h, label: `${replan.event.robot} failed`, color: EVENT_COLORS.robot_failure }]}
                highlightTasks={replan.remaining_tasks}
                selectedTask={selA}
                onSelectTask={setSelA}
                zoneIds={lpZones}
                minSpanH={Math.max(replan.before.makespan_h, replan.after.schedule.makespan_h)}
                showLegend={false}
              />
            </div>
            <div className="min-w-0 rounded-md border border-white/10 p-2">
              <Gantt
                schedule={replan.after.schedule}
                tasks={lpSimTasks}
                requirements={lp.requirements}
                title={`After · makespan ${replan.after.schedule.makespan_h} h`}
                compact
                nowH={replan.t_h}
                highlightTasks={replan.remaining_tasks}
                selectedTask={selA}
                onSelectTask={setSelA}
                zoneIds={lpZones}
                minSpanH={Math.max(replan.before.makespan_h, replan.after.schedule.makespan_h)}
              />
            </div>
          </div>
        </Section>
      ) : null}

      <Section
        title="After the replan - the schedule the simulation finished on"
        hint={`Horizon pinned to ${fmtNum(nearHorizon, 0)} h (1.2x the plan's makespan), so the dashed deadline line is in the plot.`}
      >
        <Gantt
          schedule={finalSchedule}
          tasks={lpSimTasks}
          requirements={lp.requirements}
          title={`${lp.requirements.site.name} · after the replan`}
          horizonH={nearHorizon}
          selectedTask={selB}
          onSelectTask={setSelB}
          zoneIds={lpZones}
        />
        <div className="mt-4">
          <TaskTable
            tasks={lpSimTasks}
            schedule={finalSchedule}
            requirements={lp.requirements}
            selectedTask={selB}
            onSelectTask={setSelB}
            zoneIds={lpZones}
          />
        </div>
      </Section>

      <Section title="Edge cases">
        <div className="flex flex-col gap-6">
          <Gantt
            schedule={withMissing}
            tasks={lpTasks}
            requirements={lp.requirements}
            title="Entry T99 missing from the task graph (grey) + robot HL9 missing from requirements (extra row)"
            horizonH={nearHorizon}
            selectedTask={selEdge}
            onSelectTask={setSelEdge}
            zoneIds={lpZones}
          />
          <Gantt
            schedule={EMPTY_SCHEDULE}
            tasks={[]}
            requirements={lp.requirements}
            title="Makespan 0, no entries, no tasks"
            selectedTask={selEdge}
            onSelectTask={setSelEdge}
          />
          <Gantt
            schedule={many.schedule}
            tasks={lpTasks}
            requirements={many.requirements}
            title="12 robots: body scrolls after 10 rows, axis stays"
            selectedTask={selEdge}
            onSelectTask={setSelEdge}
            zoneIds={lpZones}
          />
          <Gantt
            schedule={lpSchedule}
            tasks={lpTasks}
            requirements={lp.requirements}
            title={`Deadline inside the plot (horizonH = ${fmtNum(nearHorizon, 0)} h, 1.2x makespan)`}
            horizonH={nearHorizon}
            selectedTask={selEdge}
            onSelectTask={setSelEdge}
            zoneIds={lpZones}
          />
          <div className="max-w-sm">
            <Gantt
              schedule={lpSchedule}
              tasks={lpTasks}
              requirements={lp.requirements}
              title="Narrow container (24 rem)"
              compact
              nowH={failAt}
              markers={[{ t_h: failAt, label: failLabel, color: EVENT_COLORS.robot_failure }]}
              selectedTask={selEdge}
              onSelectTask={setSelEdge}
              zoneIds={lpZones}
            />
          </div>
        </div>
      </Section>
    </>
  );
}
