"use client";
/**
 * The Moonwright console: a single scrolling page whose state machine runs the PRD flow
 * brief -> requirements -> plan (streamed trace) -> simulate -> robustness. All backend calls
 * happen here; the stage components are presentational.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type {
  ExampleBrief,
  Health,
  LoopStep,
  MonteCarloRequest,
  MonteCarloResult,
  PlanResult,
  Requirements,
  SimEvent,
  SimResult,
  StructureResponse,
  Task,
} from "@/lib/types";
import {
  API_BASE,
  SAMPLE_IDS,
  errorMessage,
  getExamples,
  getHealth,
  isAbortError,
  loadSamplePlan,
  loadSampleSim,
  postMonteCarlo,
  postSimulate,
  postStructure,
} from "@/lib/api";
import { planStream } from "@/lib/sse";
import { allTasks, tasksById } from "@/lib/tasks";
import { EVENT_COLORS, robotTypeColor } from "@/lib/palette";
import { fmtHours, fmtPercent, fmtSeconds, fmtWh, plural, titleFromBrief } from "@/lib/format";
import { TopBar, type FlowStage, type HealthStatus } from "./TopBar";
import { BriefForm } from "./BriefForm";
import { RequirementsCard } from "./RequirementsCard";
import { ProgressTrace } from "./ProgressTrace";
import { ExplanationCard } from "./ExplanationCard";
import { MonteCarloPanel } from "./MonteCarloPanel";
import { Badge, Card, Dot, Notice, Section, Stat, type Tone } from "./Section";
import { Gantt, type GanttMarker } from "./Gantt";
import { TaskTable } from "./TaskTable";
import { DEFAULT_SIMULATE_OPTIONS, SimControls, toSimulateRequest, type SimulateOptions } from "./SimControls";
import { SimReplay } from "./SimReplay";
import { EventList, eventKindLabel, formatHours } from "./EventList";
import { BeforeAfter } from "./BeforeAfter";

// ------------------------------------------------------------------ state
type Status = "idle" | "running" | "done" | "error";

interface Async<T> {
  status: Status;
  result: T | null;
  error: string | null;
}

function idle<T>(): Async<T> {
  return { status: "idle", result: null, error: null };
}

interface PlanState extends Async<PlanResult> {
  steps: LoopStep[];
  startedAt: number | null;
}

const idlePlan = (): PlanState => ({ ...idle<PlanResult>(), steps: [], startedAt: null });

interface State {
  healthStatus: HealthStatus;
  health: Health | null;
  examples: ExampleBrief[];
  examplesNote: string | null;
  examplesLoading: boolean;
  brief: string;
  briefId: string | null;
  structure: Async<StructureResponse>;
  requirements: Requirements | null;
  requirementsOriginal: Requirements | null;
  requirementsSource: "model" | "sample" | null;
  /** Bumped whenever requirements arrive from outside (remounts the editor). */
  requirementsVersion: number;
  maxRepairs: number;
  plan: PlanState;
  selectedTask: string | null;
  simOptions: SimulateOptions;
  sim: Async<SimResult>;
  mc: Async<MonteCarloResult>;
  sample: { loading: boolean; note: string | null; error: string | null };
}

const initialState: State = {
  healthStatus: "checking",
  health: null,
  examples: [],
  examplesNote: null,
  examplesLoading: true,
  brief: "",
  briefId: null,
  structure: idle(),
  requirements: null,
  requirementsOriginal: null,
  requirementsSource: null,
  requirementsVersion: 0,
  maxRepairs: 3,
  plan: idlePlan(),
  selectedTask: null,
  simOptions: DEFAULT_SIMULATE_OPTIONS,
  sim: idle(),
  mc: idle(),
  sample: { loading: false, note: null, error: null },
};

type Action =
  | { type: "health"; status: HealthStatus; health: Health | null }
  | { type: "examples"; examples: ExampleBrief[]; note: string | null }
  | { type: "brief"; text: string }
  | { type: "useExample"; example: ExampleBrief }
  | { type: "structureStart" }
  | { type: "structureDone"; response: StructureResponse }
  | { type: "structureError"; message: string }
  | { type: "requirements"; value: Requirements }
  | { type: "maxRepairs"; value: number }
  | { type: "planStart"; startedAt: number }
  | { type: "planStep"; step: LoopStep }
  | { type: "planDone"; result: PlanResult }
  | { type: "planError"; message: string }
  | { type: "selectTask"; id: string | null }
  | { type: "simOptions"; value: SimulateOptions }
  | { type: "simStart" }
  | { type: "simDone"; result: SimResult }
  | { type: "simError"; message: string }
  | { type: "mcStart" }
  | { type: "mcDone"; result: MonteCarloResult }
  | { type: "mcError"; message: string }
  | { type: "sampleStart" }
  | { type: "sampleDone"; plan: PlanResult; sim: SimResult; id: string }
  | { type: "sampleError"; message: string };

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "health":
      return { ...state, healthStatus: action.status, health: action.health };
    case "examples":
      return { ...state, examples: action.examples, examplesNote: action.note, examplesLoading: false };
    case "brief":
      return { ...state, brief: action.text, briefId: action.text.trim() ? state.briefId : null };
    case "useExample":
      return {
        ...state,
        brief: action.example.text,
        briefId: action.example.id,
        structure: state.structure.status === "error" ? idle() : state.structure,
      };
    case "structureStart":
      return { ...state, structure: { status: "running", result: null, error: null } };
    case "structureDone": {
      const { response } = action;
      if (!response.requirements) {
        const f = response.step.failure;
        const why = f
          ? `${f.hint || f.kind}${f.conflicts.length ? ` [${f.conflicts.join(", ")}]` : ""}`
          : response.step.message || "the model returned no requirements";
        return { ...state, structure: { status: "error", result: response, error: why } };
      }
      return {
        ...state,
        structure: { status: "done", result: response, error: null },
        requirements: response.requirements,
        requirementsOriginal: response.requirements,
        requirementsSource: "model",
        requirementsVersion: state.requirementsVersion + 1,
        plan: idlePlan(),
        sim: idle(),
        mc: idle(),
        selectedTask: null,
        simOptions: DEFAULT_SIMULATE_OPTIONS,
        sample: { ...state.sample, note: null },
      };
    }
    case "structureError":
      return { ...state, structure: { status: "error", result: null, error: action.message } };
    case "requirements":
      return { ...state, requirements: action.value };
    case "maxRepairs":
      return { ...state, maxRepairs: action.value };
    case "planStart":
      return {
        ...state,
        plan: { status: "running", result: null, error: null, steps: [], startedAt: action.startedAt },
        sim: idle(),
        mc: idle(),
        selectedTask: null,
        sample: { ...state.sample, note: null },
      };
    case "planStep":
      if (state.plan.status !== "running") return state;
      return { ...state, plan: { ...state.plan, steps: [...state.plan.steps, action.step] } };
    case "planDone":
      return {
        ...state,
        plan: {
          ...state.plan,
          status: "done",
          result: action.result,
          error: null,
          steps: action.result.steps.length ? action.result.steps : state.plan.steps,
        },
      };
    case "planError":
      return { ...state, plan: { ...state.plan, status: "error", error: action.message } };
    case "selectTask":
      return { ...state, selectedTask: action.id };
    case "simOptions":
      return { ...state, simOptions: action.value };
    case "simStart":
      return { ...state, sim: { status: "running", result: null, error: null } };
    case "simDone":
      return { ...state, sim: { status: "done", result: action.result, error: null } };
    case "simError":
      return { ...state, sim: { status: "error", result: null, error: action.message } };
    case "mcStart":
      return { ...state, mc: { status: "running", result: null, error: null } };
    case "mcDone":
      return { ...state, mc: { status: "done", result: action.result, error: null } };
    case "mcError":
      return { ...state, mc: { status: "error", result: null, error: action.message } };
    case "sampleStart":
      return { ...state, sample: { loading: true, note: null, error: null } };
    case "sampleDone": {
      const failures = action.sim.events
        .filter((e) => e.kind === "robot_failure" && e.robot !== null)
        .map((e) => ({ robot: e.robot as string, t_h: e.t_h }));
      return {
        ...state,
        brief: action.plan.requirements.source_brief ?? state.brief,
        briefId: action.id,
        structure: idle(),
        requirements: action.plan.requirements,
        requirementsOriginal: action.plan.requirements,
        requirementsSource: "sample",
        requirementsVersion: state.requirementsVersion + 1,
        plan: { status: "done", result: action.plan, error: null, steps: action.plan.steps, startedAt: null },
        selectedTask: null,
        simOptions: { ...DEFAULT_SIMULATE_OPTIONS, failures },
        sim: { status: "done", result: action.sim, error: null },
        mc: idle(),
        sample: {
          loading: false,
          note: `Sample result loaded from /samples/${action.id}.plan.json and .sim.json; no backend call was made.`,
          error: null,
        },
      };
    }
    case "sampleError":
      return { ...state, sample: { loading: false, note: null, error: action.message } };
    default:
      return state;
  }
}

function deriveStage(s: State): { stage: FlowStage; busy: boolean } {
  if (s.mc.status === "running") return { stage: "robustness", busy: true };
  if (s.mc.result) return { stage: "robustness", busy: false };
  if (s.sim.status === "running") return { stage: "simulate", busy: true };
  if (s.sim.result) return { stage: "simulate", busy: false };
  if (s.plan.status === "running") return { stage: "plan", busy: true };
  if (s.plan.result || s.plan.status === "error") return { stage: "plan", busy: false };
  if (s.structure.status === "running") return { stage: "requirements", busy: true };
  if (s.requirements) return { stage: "requirements", busy: false };
  return { stage: "brief", busy: s.sample.loading };
}

function scrollToSection(id: string) {
  window.setTimeout(() => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 80);
}

// ------------------------------------------------------------------ component
export function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const planAbort = useRef<AbortController | null>(null);

  const refreshHealth = useCallback(async (signal?: AbortSignal) => {
    try {
      const health = await getHealth(signal);
      dispatch({ type: "health", status: "ok", health });
    } catch (e) {
      if (isAbortError(e)) return;
      dispatch({ type: "health", status: "unreachable", health: null });
    }
  }, []);

  const loadExamples = useCallback(async (signal?: AbortSignal) => {
    try {
      const examples = await getExamples(signal);
      dispatch({
        type: "examples",
        examples,
        note: examples.length ? null : "the backend has no example briefs",
      });
    } catch (e) {
      if (isAbortError(e)) return;
      try {
        const plans = await Promise.all(SAMPLE_IDS.map((id) => loadSamplePlan(id)));
        const examples = plans
          .map((p, i) => ({
            id: SAMPLE_IDS[i],
            title: titleFromBrief(p.requirements.source_brief ?? "", SAMPLE_IDS[i]),
            text: p.requirements.source_brief ?? "",
          }))
          .filter((ex) => ex.text.length > 0);
        dispatch({
          type: "examples",
          examples,
          note: "backend unreachable: example texts taken from the bundled sample plans",
        });
      } catch {
        dispatch({ type: "examples", examples: [], note: `could not load examples (${errorMessage(e)})` });
      }
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void refreshHealth(ctrl.signal);
    void loadExamples(ctrl.signal);
    return () => ctrl.abort();
  }, [refreshHealth, loadExamples]);

  const retryHealth = () => {
    dispatch({ type: "health", status: "checking", health: null });
    void refreshHealth();
  };

  const cancelPlan = () => {
    planAbort.current?.abort();
    planAbort.current = null;
  };

  const structureBrief = async () => {
    cancelPlan();
    dispatch({ type: "structureStart" });
    try {
      const response = await postStructure({
        brief: state.brief,
        ...(state.briefId ? { brief_id: state.briefId } : {}),
      });
      dispatch({ type: "structureDone", response });
      if (response.requirements) scrollToSection("requirements");
    } catch (e) {
      dispatch({ type: "structureError", message: errorMessage(e) });
    }
  };

  const planWithRequirements = async () => {
    const requirements = state.requirements;
    if (!requirements) return;
    cancelPlan();
    const ctrl = new AbortController();
    planAbort.current = ctrl;
    dispatch({ type: "planStart", startedAt: Date.now() });
    scrollToSection("plan");
    try {
      const result = await planStream(
        {
          requirements,
          max_repairs: state.maxRepairs,
          ...(state.briefId ? { brief_id: state.briefId } : {}),
        },
        (step) => {
          if (!ctrl.signal.aborted) dispatch({ type: "planStep", step });
        },
        ctrl.signal,
      );
      if (ctrl.signal.aborted) return;
      dispatch({ type: "planDone", result });
    } catch (e) {
      if (ctrl.signal.aborted) return;
      dispatch({ type: "planError", message: errorMessage(e) });
    } finally {
      if (planAbort.current === ctrl) planAbort.current = null;
    }
  };

  const stopPlanning = () => {
    cancelPlan();
    dispatch({ type: "planError", message: "Planning cancelled." });
  };

  const runSimulation = async () => {
    const plan = state.plan.result;
    if (!plan?.schedule) return;
    dispatch({ type: "simStart" });
    try {
      // frame_stride follows the step so that about one frame per hour is kept whatever
      // the slider says (see toSimulateRequest)
      const result = await postSimulate(
        toSimulateRequest(plan, state.simOptions, state.briefId ? { brief_id: state.briefId } : {}),
      );
      dispatch({ type: "simDone", result });
      scrollToSection("simulate");
    } catch (e) {
      dispatch({ type: "simError", message: errorMessage(e) });
    }
  };

  const runMonteCarlo = async (req: MonteCarloRequest) => {
    dispatch({ type: "mcStart" });
    try {
      const result = await postMonteCarlo(req);
      dispatch({ type: "mcDone", result });
    } catch (e) {
      dispatch({ type: "mcError", message: errorMessage(e) });
    }
  };

  const loadSample = async () => {
    cancelPlan();
    dispatch({ type: "sampleStart" });
    try {
      const [plan, sim] = await Promise.all([loadSamplePlan("landing_pad"), loadSampleSim("landing_pad")]);
      dispatch({ type: "sampleDone", plan, sim, id: "landing_pad" });
      scrollToSection("plan");
    } catch (e) {
      dispatch({ type: "sampleError", message: errorMessage(e) });
    }
  };

  // ---------------------------------------------------------------- derived
  const planResult = state.plan.result;
  const simResult = state.sim.result;
  const planTasks = useMemo(() => planResult?.task_graph?.tasks ?? [], [planResult]);
  const everyTask = useMemo(() => (planResult ? allTasks(planResult, simResult) : []), [planResult, simResult]);
  const simMarkers = useMemo<GanttMarker[]>(
    () =>
      (simResult?.events ?? []).map((e) => ({
        t_h: e.t_h,
        label: `${eventKindLabel(e.kind)}${e.robot ? ` ${e.robot}` : ""}`,
        color: EVENT_COLORS[e.kind],
        group: eventKindLabel(e.kind),
      })),
    [simResult],
  );
  const { stage, busy } = deriveStage(state);
  const verified = Boolean(planResult?.success && planResult.schedule && planResult.task_graph);
  const highlight = useMemo(() => (state.selectedTask ? [state.selectedTask] : []), [state.selectedTask]);
  const selectTask = (id: string | null) => dispatch({ type: "selectTask", id });
  const requirementsEdited =
    state.requirements !== null &&
    JSON.stringify(state.requirements) !== JSON.stringify(state.requirementsOriginal);

  return (
    <div id="top" className="flex min-h-screen flex-col">
      <TopBar
        health={state.health}
        healthStatus={state.healthStatus}
        apiBase={API_BASE}
        stage={stage}
        busy={busy}
        onLoadSample={loadSample}
        sampleLoading={state.sample.loading}
        onRetryHealth={retryHealth}
      />

      <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 md:px-6 lg:px-8">
        {state.sample.error && (
          <Notice tone="fail" title="Could not load the sample" className="mt-4">
            {state.sample.error}
          </Notice>
        )}

        <BriefForm
          brief={state.brief}
          briefId={state.briefId}
          onBriefChange={(text) => dispatch({ type: "brief", text })}
          examples={state.examples}
          examplesLoading={state.examplesLoading}
          examplesNote={state.examplesNote}
          onUseExample={(example) => dispatch({ type: "useExample", example })}
          onStructure={structureBrief}
          structuring={state.structure.status === "running"}
          error={state.structure.error}
          disabled={state.plan.status === "running"}
        />

        {state.structure.status === "running" && !state.requirements && (
          <Section id="requirements" step="02" title="Requirements">
            <Card>
              <div className="flex items-center gap-3 text-[13px] text-muted">
                <Dot color="var(--accent)" pulse className="size-2.5" />
                Nemotron is structuring the brief: site, structures, fleet, horizon, constraints…
              </div>
            </Card>
          </Section>
        )}

        {state.requirements && (
          <RequirementsCard
            key={state.requirementsVersion}
            value={state.requirements}
            onChange={(value) => dispatch({ type: "requirements", value })}
            structureStep={state.structure.result?.step ?? null}
            source={state.requirementsSource}
            edited={requirementsEdited}
            maxRepairs={state.maxRepairs}
            onMaxRepairsChange={(value) => dispatch({ type: "maxRepairs", value })}
            onPlan={planWithRequirements}
            planning={state.plan.status === "running"}
            onRestructure={structureBrief}
            restructuring={state.structure.status === "running"}
            canRestructure={state.brief.trim().length > 0}
          />
        )}

        {state.plan.status !== "idle" && (
          <Section
            id="plan"
            step="03"
            title="Plan"
            subtitle="Nemotron proposes a task graph; the deterministic validator and OR-Tools CP-SAT verify precedence, one-robot-per-zone safety, per-robot no-overlap, battery budgets and the horizon. Infeasibility cores go back to the model for repair."
            actions={state.sample.note ? <Badge tone="info">{state.sample.note}</Badge> : undefined}
          >
            <div className="space-y-5">
              <div className="grid grid-cols-[minmax(0,1fr)] gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <ProgressTrace
                  steps={state.plan.steps}
                  running={state.plan.status === "running"}
                  startedAt={state.plan.startedAt}
                  result={planResult}
                  error={state.plan.error}
                  onCancel={stopPlanning}
                />
                {planResult && verified && <PlanSummary plan={planResult} tasks={planTasks} />}
              </div>

              {planResult && verified && planResult.schedule && (
                <>
                  <Gantt
                    schedule={planResult.schedule}
                    tasks={planTasks}
                    requirements={planResult.requirements}
                    title={simMarkers.length > 0 ? "Verified schedule · simulation events marked" : "Verified schedule"}
                    highlightTasks={highlight}
                    markers={simMarkers}
                    selectedTask={state.selectedTask}
                    onSelectTask={selectTask}
                  />
                  <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
                    <div>
                      <h3 className="mb-2 text-[13px] font-medium">Tasks</h3>
                      <TaskTable
                        tasks={planTasks}
                        schedule={planResult.schedule}
                        requirements={planResult.requirements}
                        selectedTask={state.selectedTask}
                        onSelectTask={selectTask}
                      />
                    </div>
                    <ExplanationCard plan={planResult} />
                  </div>
                </>
              )}

              {planResult && !verified && planResult.task_graph && (
                <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
                  <div>
                    <h3 className="mb-2 text-[13px] font-medium">Last task graph (not scheduled)</h3>
                    <TaskTable tasks={planTasks} schedule={null} requirements={planResult.requirements} />
                  </div>
                  <ExplanationCard plan={planResult} />
                </div>
              )}
            </div>
          </Section>
        )}

        {planResult && verified && planResult.schedule && (
          <Section
            id="simulate"
            step="04"
            title="Simulate"
            subtitle="Replay the verified schedule in the 2D simulator with robot positions and batteries. Inject robot failures; on each event the agent replans the remaining work from the task-graph stage and the solver re-verifies it."
          >
            <div className="space-y-5">
              <Card title="Scenario" bodyClassName="p-4">
                <SimControls
                  requirements={planResult.requirements}
                  horizonH={planResult.requirements.horizon_h}
                  makespanH={planResult.schedule.makespan_h}
                  value={state.simOptions}
                  onChange={(value) => dispatch({ type: "simOptions", value })}
                  onRun={runSimulation}
                  running={state.sim.status === "running"}
                />
                {state.sim.error && (
                  <Notice tone="fail" title="Simulation failed" className="mt-3">
                    {state.sim.error}
                  </Notice>
                )}
              </Card>

              {simResult && (
                <>
                  <CompletionLine sim={simResult} horizonH={planResult.requirements.horizon_h} />
                  <ReplayPane sim={simResult} plan={planResult} tasks={everyTask} />
                  {simResult.replans.map((record, i) => (
                    <BeforeAfter
                      key={i}
                      record={record}
                      requirements={planResult.requirements}
                      tasks={everyTask}
                      index={i + 1}
                    />
                  ))}
                </>
              )}
            </div>
          </Section>
        )}

        {planResult && verified && (
          <MonteCarloPanel
            plan={planResult}
            briefId={state.briefId}
            result={state.mc.result}
            running={state.mc.status === "running"}
            error={state.mc.error}
            onRun={runMonteCarlo}
          />
        )}
      </main>

      <footer className="mt-8 border-t border-line py-5 text-center text-[11px] text-dim">
        Moonwright · Nemotron on Nebius Token Factory + OR-Tools CP-SAT ·{" "}
        <span className="font-mono">API {API_BASE}</span>
      </footer>
    </div>
  );
}

// ------------------------------------------------------------------ plan summary
function PlanSummary({ plan, tasks }: { plan: PlanResult; tasks: Task[] }) {
  const schedule = plan.schedule;
  if (!schedule) return null;
  const req = plan.requirements;
  const byId = tasksById(tasks);
  const util = req.horizon_h > 0 ? schedule.makespan_h / req.horizon_h : 0;
  const rows = req.robots.map((robot) => {
    const entries = schedule.entries.filter((e) => e.robot === robot.id);
    const busy = entries.reduce((acc, e) => acc + (e.end_h - e.start_h), 0);
    const energy = entries.reduce((acc, e) => acc + (byId[e.task]?.energy_wh ?? 0), 0);
    return { robot, count: entries.length, busy, energy, ratio: robot.battery_wh > 0 ? energy / robot.battery_wh : 0 };
  });
  return (
    <Card
      title="Plan summary"
      aside={<Badge tone={schedule.status === "optimal" ? "ok" : "info"}>{schedule.status}</Badge>}
    >
      <div className="grid grid-cols-3 gap-3">
        <Stat label="Makespan" value={fmtHours(schedule.makespan_h, 0)} sub={`${fmtPercent(util)} of ${fmtHours(req.horizon_h, 0)} horizon`} />
        <Stat label="Tasks" value={tasks.length} sub={plural(req.structures.length, "structure")} />
        <Stat label="Repairs" value={plan.repairs} sub={`solver ${fmtSeconds(schedule.solve_time_s)}`} />
      </div>
      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-white/10" aria-hidden="true">
        <div className="h-full rounded-full bg-accent" style={{ width: `${Math.min(100, util * 100)}%` }} />
      </div>
      <div className="mt-4 overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-dim">
            <th className="pb-1 font-medium">Robot</th>
            <th className="pb-1 font-medium">Tasks</th>
            <th className="pb-1 font-medium">Busy</th>
            <th className="pb-1 font-medium">Battery budget</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ robot, count, busy, energy, ratio }) => (
            <tr key={robot.id} className="border-t border-white/5">
              <td className="py-1.5 pr-2">
                <span className="inline-flex items-center gap-1.5 font-mono">
                  <Dot color={robotTypeColor(robot.type)} />
                  {robot.id}
                  <span className="text-dim">{robot.type}</span>
                </span>
              </td>
              <td className="py-1.5 pr-2 font-mono tabular-nums">{count}</td>
              <td className="py-1.5 pr-2 font-mono tabular-nums">{fmtHours(busy, 0)}</td>
              <td className="py-1.5">
                <div className="flex items-center gap-2">
                  <div className="h-1.5 w-24 overflow-hidden rounded-full bg-white/10" aria-hidden="true">
                    <div
                      className={ratio > 0.9 ? "h-full rounded-full bg-warn" : "h-full rounded-full bg-accent"}
                      style={{ width: `${Math.min(100, ratio * 100)}%` }}
                    />
                  </div>
                  <span className="font-mono tabular-nums text-muted">
                    {fmtWh(energy)} / {fmtWh(robot.battery_wh)}
                  </span>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ replay + events
/** The 2D replay next to the event list. Holds the playhead time locally so the ~12 Hz time
 *  updates from the canvas re-render only this pane: the event list highlights the rows at
 *  or before the playhead, and clicking a row seeks the replay to that event. */
function ReplayPane({ sim, plan, tasks }: { sim: SimResult; plan: PlanResult; tasks: Task[] }) {
  const [nowH, setNowH] = useState<number | null>(null);
  const [seek, setSeek] = useState<{ t_h: number } | null>(null);
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div>
        <h3 className="mb-2 text-[13px] font-medium">Replay</h3>
        <SimReplay sim={sim} plan={plan} tasks={tasks} autoPlay seekRequest={seek} onTimeChange={setNowH} />
      </div>
      <div>
        <h3 className="mb-2 text-[13px] font-medium">
          Events <span className="font-normal text-dim">{plural(sim.events.length, "event")}</span>
          {nowH !== null && <span className="ml-2 font-mono text-[11px] font-normal text-dim">t = {formatHours(nowH)}</span>}
        </h3>
        <EventList
          events={sim.events}
          replans={sim.replans}
          nowH={nowH ?? undefined}
          onSeek={(t_h) => setSeek({ t_h })}
        />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ simulation summary
/** "1 robot failure · 2 task overruns" rather than "3 events": the kinds are what matter. */
function eventSummary(events: SimEvent[]): string {
  if (events.length === 0) return "no events";
  const counts = new Map<string, number>();
  for (const e of events) counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1);
  return [...counts].map(([kind, n]) => plural(n, eventKindLabel(kind))).join(" · ");
}

function CompletionLine({ sim, horizonH }: { sim: SimResult; horizonH: number }) {
  let tone: Tone;
  let title: string;
  let detail: string;
  if (sim.completed && sim.finished_at_h !== null) {
    const late = sim.finished_at_h > horizonH;
    tone = late ? "warn" : "ok";
    title = `${late ? "Completed late" : "Completed"} at ${formatHours(sim.finished_at_h)} of the ${fmtHours(horizonH, 0)} horizon`;
    detail = `${eventSummary(sim.events)} · ${plural(sim.replans.length, "replan")}${sim.schedule ? ` · final schedule makespan ${fmtHours(sim.schedule.makespan_h, 0)}` : ""}`;
  } else {
    tone = "fail";
    title = "Not completed";
    const lastReplan = sim.replans[sim.replans.length - 1];
    if (lastReplan && !lastReplan.after.success) {
      const f = lastReplan.after.failure;
      detail = `replan ${sim.replans.length} failed: ${f ? f.hint || f.kind : "no verified schedule"}${f && f.conflicts.length ? ` [${f.conflicts.join(", ")}]` : ""}`;
    } else if (sim.frames.length > 0) {
      detail = `the simulation stopped at ${formatHours(sim.frames[sim.frames.length - 1].t_h)} with work remaining · ${eventSummary(sim.events)} · ${plural(sim.replans.length, "replan")}`;
    } else {
      detail = "the simulator produced no frames";
    }
  }
  return (
    <Notice tone={tone} title={title}>
      {detail}
    </Notice>
  );
}
