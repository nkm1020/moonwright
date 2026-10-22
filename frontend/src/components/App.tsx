"use client";
/**
 * The Moonwright console: a single scrolling page whose state machine runs the PRD flow
 * brief -> requirements -> plan (streamed trace) -> simulate -> robustness. All backend calls
 * happen here; the stage components are presentational.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from "react";
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
  VerificationReceipt as ReceiptData,
} from "@/lib/types";
import { lastReceipt } from "@/lib/types";
import {
  API_BASE,
  ApiError,
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
  type SampleId,
} from "@/lib/api";
import { planStream } from "@/lib/sse";
import { allTasks, tasksById } from "@/lib/tasks";
import { EVENT_COLORS, robotTypeColor } from "@/lib/palette";
import { fmtHours, fmtPercent, fmtSeconds, fmtWh, plural, tidyHint, titleFromBrief } from "@/lib/format";
import { TopBar, type FlowStage, type HealthStatus } from "./TopBar";
import { BriefForm, stripMarkdown } from "./BriefForm";
import { RequirementsCard } from "./RequirementsCard";
import { ProgressTrace } from "./ProgressTrace";
import { VerificationReceipt } from "./VerificationReceipt";
import { ExplanationCard } from "./ExplanationCard";
import { MonteCarloPanel } from "./MonteCarloPanel";
import { Badge, Card, CurrentStageContext, Dot, IconChevron, Notice, Section, Stat, cx, type Tone } from "./Section";
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
  /** The example the plan's requirements came from (sent as brief_id to the simulator and
   *  Monte Carlo so recorded replan answers are looked up under the right key). */
  briefId: string | null;
}

const idlePlan = (): PlanState => ({ ...idle<PlanResult>(), steps: [], startedAt: null, briefId: null });

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
  /** The brief (example id and text) the requirements were structured from; another brief in
   *  the textarea makes the card stale (planning waits for a re-structure). */
  requirementsBrief: { id: string | null; text: string } | null;
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
  requirementsBrief: null,
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
  /** Requirements handed in from outside the editor (the Monte Carlo what-if fleet). */
  | { type: "adoptRequirements"; value: Requirements }
  | { type: "maxRepairs"; value: number }
  | { type: "planStart"; startedAt: number; briefId: string | null }
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
  | { type: "sampleDone"; plan: PlanResult; sim: SimResult | null; mc: MonteCarloResult | null; id: SampleId }
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
          ? `${tidyHint(f.hint) || f.kind}${f.conflicts.length ? ` [${f.conflicts.join(", ")}]` : ""}`
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
        requirementsBrief: { id: state.briefId, text: state.brief },
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
    case "adoptRequirements":
      // the editor is keyed by requirementsVersion: bump it so the new fleet shows up; the
      // original stays, so the "edited by you" badge marks the spares as a change
      return { ...state, requirements: action.value, requirementsVersion: state.requirementsVersion + 1 };
    case "maxRepairs":
      return { ...state, maxRepairs: action.value };
    case "planStart":
      // the previous plan, its simulation and its Monte Carlo stay on screen (dimmed) until the
      // new result arrives; a request that fails leaves them untouched
      return {
        ...state,
        plan: { ...state.plan, status: "running", error: null, steps: [], startedAt: action.startedAt, briefId: action.briefId },
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
        sim: idle(),
        mc: idle(),
        selectedTask: null,
        sample: { ...state.sample, note: null },
      };
    case "planError":
      return { ...state, plan: { ...state.plan, status: "error", error: action.message } };
    case "selectTask":
      return { ...state, selectedTask: action.id };
    case "simOptions":
      return { ...state, simOptions: action.value };
    // a run in progress or a failed request keeps the previous result on screen; only a new
    // result replaces it
    case "simStart":
      return { ...state, sim: { status: "running", result: state.sim.result, error: null } };
    case "simDone":
      return { ...state, sim: { status: "done", result: action.result, error: null } };
    case "simError":
      return { ...state, sim: { status: "error", result: state.sim.result, error: action.message } };
    case "mcStart":
      return { ...state, mc: { status: "running", result: state.mc.result, error: null } };
    case "mcDone":
      return { ...state, mc: { status: "done", result: action.result, error: null } };
    case "mcError":
      return { ...state, mc: { status: "error", result: state.mc.result, error: action.message } };
    case "sampleStart":
      return { ...state, sample: { loading: true, note: null, error: null } };
    case "sampleDone": {
      const { plan, sim, mc, id } = action;
      const failures = (sim?.events ?? [])
        .filter((e) => e.kind === "robot_failure" && e.robot !== null)
        .map((e) => ({ robot: e.robot as string, t_h: e.t_h }));
      const parts = ["plan", sim ? "simulation" : null, mc ? "Monte Carlo" : null]
        .filter((f): f is string => f !== null)
        .join(" + ");
      // the textarea holds plain prose: the example's text when the backend sent it, else the
      // recorded brief, markdown markers stripped either way
      const example = state.examples.find((ex) => ex.id === id);
      const rawBrief = example?.text ?? plan.requirements.source_brief ?? null;
      const brief = rawBrief ? stripMarkdown(rawBrief) : state.brief;
      return {
        ...state,
        brief,
        briefId: id,
        structure: idle(),
        requirements: plan.requirements,
        requirementsOriginal: plan.requirements,
        requirementsSource: "sample",
        requirementsVersion: state.requirementsVersion + 1,
        requirementsBrief: { id, text: brief },
        plan: { status: "done", result: plan, error: null, steps: plan.steps, startedAt: null, briefId: id },
        selectedTask: null,
        simOptions: { ...DEFAULT_SIMULATE_OPTIONS, failures },
        sim: sim ? { status: "done", result: sim, error: null } : idle(),
        mc: mc ? { status: "done", result: mc, error: null } : idle(),
        sample: {
          loading: false,
          note: `Recorded demo · ${parts}`,
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
  // a plan in progress comes first: the previous plan's simulation / Monte Carlo may still be
  // on screen underneath it
  if (s.plan.status === "running") return { stage: "plan", busy: true };
  if (s.mc.status === "running") return { stage: "robustness", busy: true };
  if (s.mc.result) return { stage: "robustness", busy: false };
  if (s.sim.status === "running") return { stage: "simulate", busy: true };
  if (s.sim.result) return { stage: "simulate", busy: false };
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

/** Recorded MonteCarloResult next to a sample plan (only landing_pad ships one). */
async function loadSampleMonteCarlo(id: SampleId): Promise<MonteCarloResult> {
  const path = `/samples/${id}.mc.json`;
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, `could not load ${path} (${errorMessage(e)})`);
  }
  if (!res.ok) throw new ApiError(res.status, `could not load ${path}`);
  return (await res.json()) as MonteCarloResult;
}

/** A sample's optional companion file: null when the sample does not ship it (404), any
 *  other failure propagates. */
async function optionalSample<T>(load: Promise<T>): Promise<T | null> {
  try {
    return await load;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

// ------------------------------------------------------------------ component
export function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const planAbort = useRef<AbortController | null>(null);
  // the requirements belong to the brief they were structured from: another example in the
  // textarea, or edited text, means the card no longer matches what is on screen
  const requirementsStale =
    state.requirements !== null &&
    state.requirementsBrief !== null &&
    (state.requirementsBrief.id !== state.briefId || state.requirementsBrief.text.trim() !== state.brief.trim());

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
            // mirrors the backend's ExampleBrief: plain prose in `text`, the file as written in
            // `text_markdown`
            text: stripMarkdown(p.requirements.source_brief ?? ""),
            text_markdown: p.requirements.source_brief ?? "",
          }))
          .filter((ex) => ex.text.length > 0);
        dispatch({
          type: "examples",
          examples,
          note: "backend offline: example briefs taken from the bundled recordings",
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
    if (!requirements || requirementsStale) return;
    cancelPlan();
    const ctrl = new AbortController();
    planAbort.current = ctrl;
    // recorded answers are keyed by brief_id: send the id of the brief these requirements came
    // from, never the example that merely sits in the textarea
    const briefId = state.requirementsBrief?.id ?? null;
    dispatch({ type: "planStart", startedAt: Date.now(), briefId });
    scrollToSection("plan");
    try {
      const result = await planStream(
        {
          requirements,
          max_repairs: state.maxRepairs,
          ...(briefId ? { brief_id: briefId } : {}),
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
        toSimulateRequest(plan, state.simOptions, state.plan.briefId ? { brief_id: state.plan.briefId } : {}),
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

  /** Load a recorded run from /samples into the normal state: the plan always, the simulation
   *  and Monte Carlo result when the sample ships them (landing_pad does, tight_battery is a
   *  plan with one repair round and no simulation). */
  const loadSample = async (id: SampleId = "landing_pad") => {
    cancelPlan();
    dispatch({ type: "sampleStart" });
    try {
      const [plan, sim, mc] = await Promise.all([
        loadSamplePlan(id),
        optionalSample(loadSampleSim(id)),
        optionalSample(loadSampleMonteCarlo(id)),
      ]);
      dispatch({ type: "sampleDone", plan, sim, mc, id });
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
  // the trace that goes with planResult: the streamed steps once the run is done, else the
  // result's own steps (a previous plan kept while a new run streams or after one failed)
  const resultSteps = useMemo(
    () => (state.plan.status === "done" ? state.plan.steps : (planResult?.steps ?? [])),
    [state.plan.status, state.plan.steps, planResult],
  );
  const receipt = useMemo<ReceiptData | null>(() => lastReceipt(resultSteps)?.receipt ?? null, [resultSteps]);
  const planBusy = state.plan.status === "running";
  /** a previous plan on screen while a new run streams or after a failed request */
  const planKept = planResult !== null && state.plan.status !== "done";
  const keptClass = planBusy ? "opacity-50 transition-opacity" : "transition-opacity";
  const simBusy = state.sim.status === "running";
  const planHorizonH = state.requirements?.horizon_h ?? planResult?.requirements.horizon_h ?? null;
  const highlight = useMemo(() => (state.selectedTask ? [state.selectedTask] : []), [state.selectedTask]);
  const selectTask = (id: string | null) => dispatch({ type: "selectTask", id });
  const requirementsEdited =
    state.requirements !== null &&
    JSON.stringify(state.requirements) !== JSON.stringify(state.requirementsOriginal);

  return (
    <CurrentStageContext.Provider value={stage}>
    <div id="top" className="flex min-h-screen flex-col">
      <TopBar
        health={state.health}
        healthStatus={state.healthStatus}
        apiBase={API_BASE}
        stage={stage}
        busy={busy}
        onLoadSample={(id) => void loadSample(id)}
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
            tasks={planResult?.task_graph?.tasks ?? null}
            staleBrief={requirementsStale}
          />
        )}

        {state.plan.status !== "idle" && (
          <Section
            id="plan"
            step="03"
            title="Plan"
            subtitle="Nemotron proposes a task graph; the deterministic validator and OR-Tools CP-SAT verify precedence, one-robot-per-zone safety, per-robot no-overlap, battery budgets and the horizon. Infeasibility cores go back to the model for repair."
            actions={
              planKept ? (
                <Badge
                  tone={planBusy ? "neutral" : "warn"}
                  title={
                    planBusy
                      ? "The previous plan stays on screen until the new result arrives."
                      : "The last planning request failed; the previous plan below is unchanged."
                  }
                >
                  {planBusy ? "previous plan shown until the new one arrives" : "previous plan kept"}
                </Badge>
              ) : state.sample.note ? (
                <Badge tone="info" title="Loaded from the bundled recording of this run; Nemotron and CP-SAT were not called.">
                  {state.sample.note}
                </Badge>
              ) : undefined
            }
          >
            <div className="space-y-5">
              <div className="grid grid-cols-[minmax(0,1fr)] gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <ProgressTrace
                  steps={state.plan.steps}
                  running={planBusy}
                  startedAt={state.plan.startedAt}
                  result={state.plan.status === "done" ? planResult : null}
                  error={state.plan.error}
                  onCancel={stopPlanning}
                  horizonH={planHorizonH}
                  tasks={planTasks}
                />
                {planResult && verified && (
                  <div className={keptClass} aria-busy={planBusy || undefined}>
                    <PlanSummary plan={planResult} tasks={planTasks} receipt={receipt} />
                  </div>
                )}
              </div>

              {planResult && verified && planResult.schedule && (
                <div className={cx("space-y-5", keptClass)} aria-busy={planBusy || undefined}>
                  <VerificationReceipt plan={planResult} steps={resultSteps} horizonH={planHorizonH} />
                  <Card bodyClassName="p-4 md:p-5">
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
                  </Card>
                  <ExplanationCard plan={planResult} />
                  <TaskTableDisclosure tasks={planTasks}>
                    <TaskTable
                      tasks={planTasks}
                      schedule={planResult.schedule}
                      requirements={planResult.requirements}
                      selectedTask={state.selectedTask}
                      onSelectTask={selectTask}
                    />
                  </TaskTableDisclosure>
                </div>
              )}

              {planResult && !verified && (
                <div className={cx("space-y-5", keptClass)} aria-busy={planBusy || undefined}>
                  <VerificationReceipt plan={planResult} steps={resultSteps} horizonH={planHorizonH} />
                  {planResult.task_graph && (
                    <>
                      <ExplanationCard plan={planResult} />
                      <TaskTableDisclosure tasks={planTasks} label="Last task graph (not scheduled)" defaultOpen>
                        <TaskTable tasks={planTasks} schedule={null} requirements={planResult.requirements} />
                      </TaskTableDisclosure>
                    </>
                  )}
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
                    {simResult && <div className="mt-1 text-xs">The previous run stays below.</div>}
                  </Notice>
                )}
              </Card>

              {simResult && (
                <div className={cx("space-y-5", simBusy ? "opacity-50 transition-opacity" : "transition-opacity")} aria-busy={simBusy || undefined}>
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
                </div>
              )}
            </div>
          </Section>
        )}

        {planResult && verified && (
          <MonteCarloPanel
            plan={planResult}
            briefId={state.plan.briefId}
            result={state.mc.result}
            running={state.mc.status === "running"}
            error={state.mc.error}
            onRun={runMonteCarlo}
            onAdoptRequirements={(requirements) => {
              dispatch({ type: "adoptRequirements", value: requirements });
              scrollToSection("requirements");
            }}
          />
        )}
      </main>

      <footer className="mt-8 border-t border-line px-4 py-5 text-center text-[12px] text-muted">
        Moonwright · Nemotron on Nebius Token Factory + OR-Tools CP-SAT
        {process.env.NODE_ENV !== "production" && (
          <>
            {" · "}
            <span className="font-mono">API {API_BASE}</span>
          </>
        )}
      </footer>
    </div>
    </CurrentStageContext.Provider>
  );
}

// ------------------------------------------------------------------ task table disclosure
/** The task table folded under one row: "Show task table · 14 tasks · 123 h · 15,700 Wh". */
function TaskTableDisclosure({
  tasks,
  label = "task table",
  defaultOpen = false,
  children,
}: {
  tasks: Task[];
  label?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const hours = tasks.reduce((acc, t) => acc + t.duration_h, 0);
  const wh = tasks.reduce((acc, t) => acc + t.energy_wh, 0);
  return (
    <details className="group rounded-lg border border-line bg-raised" open={defaultOpen || undefined}>
      <summary className="flex cursor-pointer select-none list-none items-center gap-2 px-4 py-2.5 text-[13px] font-medium text-fg hover:bg-white/[0.03] [&::-webkit-details-marker]:hidden">
        <IconChevron className="size-3 text-dim transition-transform group-open:rotate-90" />
        <span className="group-open:hidden">Show {label}</span>
        <span className="hidden group-open:inline">Hide {label}</span>
        <span className="font-mono text-[11px] font-normal tabular-nums text-dim">
          {plural(tasks.length, "task")} · {fmtHours(hours, 0)} · {fmtWh(wh)}
        </span>
      </summary>
      <div className="border-t border-line p-4 md:p-5">{children}</div>
    </details>
  );
}

// ------------------------------------------------------------------ plan summary
/** Per-robot load. Battery figures come from the verification receipt when the trace carries
 *  one (work plus the transit the robot's sequence drives, the numbers the receipt prints),
 *  else from the task energies alone. */
function PlanSummary({ plan, tasks, receipt }: { plan: PlanResult; tasks: Task[]; receipt: ReceiptData | null }) {
  const schedule = plan.schedule;
  if (!schedule) return null;
  const req = plan.requirements;
  const byId = tasksById(tasks);
  const util = req.horizon_h > 0 ? schedule.makespan_h / req.horizon_h : 0;
  const rows = req.robots.map((robot) => {
    const entries = schedule.entries.filter((e) => e.robot === robot.id);
    const busy = entries.reduce((acc, e) => acc + (e.end_h - e.start_h), 0);
    const fromReceipt = receipt?.battery.find((b) => b.robot === robot.id);
    const energy = fromReceipt
      ? fromReceipt.used_wh
      : entries.reduce((acc, e) => acc + (byId[e.task]?.energy_wh ?? 0), 0);
    return { robot, count: entries.length, busy, energy, ratio: robot.battery_wh > 0 ? energy / robot.battery_wh : 0 };
  });
  const withTransit = Boolean(receipt?.travel_allowance);
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
        <div className="h-full rounded-full bg-muted" style={{ width: `${Math.min(100, util * 100)}%` }} />
      </div>
      <div className="mt-4 overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-dim">
            <th className="pb-1 font-medium">Robot</th>
            <th className="pb-1 font-medium">Tasks</th>
            <th className="pb-1 font-medium">Busy</th>
            <th className="pb-1 font-medium">{withTransit ? "Battery budget (incl. transit)" : "Battery budget"}</th>
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
                      className={ratio > 0.9 ? "h-full rounded-full bg-warn" : "h-full rounded-full bg-muted"}
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
    <SimReplay
      sim={sim}
      plan={plan}
      tasks={tasks}
      autoPlay
      seekRequest={seek}
      onTimeChange={setNowH}
      maxHeight={600}
      heading={<h3 className="text-[14px] font-medium">Replay</h3>}
      aside={
        <div>
          <h3 className="mb-2 text-[14px] font-medium">
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
      }
    />
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
      detail = `replan ${sim.replans.length} failed: ${f ? tidyHint(f.hint) || f.kind : "no verified schedule"}${f && f.conflicts.length ? ` [${f.conflicts.join(", ")}]` : ""}`;
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
