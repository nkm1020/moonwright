"use client";
/**
 * The Moonwright console: a single scrolling page whose state machine runs the PRD flow
 * brief -> requirements -> plan (streamed trace) -> simulate -> robustness. All backend calls
 * happen here; the stage components are presentational.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from "react";
import type {
  ExampleBrief,
  Failure,
  Health,
  LoopStep,
  MonteCarloRequest,
  MonteCarloResult,
  PlanResult,
  Requirements,
  SimEvent,
  SimResult,
  Stage,
  StructureResponse,
  Suggestion,
  Task,
  VerificationReceipt as ReceiptData,
} from "@/lib/types";
import { conflictPrefix, lastReceipt, scheduleReceipt, taskGraphPayload } from "@/lib/types";
import {
  API_BASE,
  ApiError,
  SAMPLE_IDS,
  errorMessage,
  getExamples,
  getHealth,
  isAbortError,
  loadSampleBundle,
  loadSamplePlan,
  postMonteCarlo,
  postSimulate,
  postStructure,
  type SampleId,
} from "@/lib/api";
import { planStream } from "@/lib/sse";
import { allTasks, tasksById } from "@/lib/tasks";
import { EVENT_COLORS, robotTypeColor } from "@/lib/palette";
import {
  answerFailure,
  fmtHours,
  fmtPercent,
  fmtSeconds,
  fmtSuggestion,
  fmtWh,
  isResourceFailure,
  lastFailedStep,
  planSuggestions,
  plural,
  repairRounds,
  stepSuggestions,
  tidyHint,
  titleFromBrief,
} from "@/lib/format";
import { TopBar, type FlowStage, type HealthStatus } from "./TopBar";
import { BriefForm, stripMarkdown } from "./BriefForm";
import { RequirementsCard } from "./RequirementsCard";
import { ProgressTrace, expectedNextStage, isVerdict, roundsPhrase } from "./ProgressTrace";
import { VerificationReceipt } from "./VerificationReceipt";
import { ExplanationCard } from "./ExplanationCard";
import { MonteCarloPanel } from "./MonteCarloPanel";
import { Suggestions } from "./Suggestions";
import {
  Badge,
  Button,
  Card,
  CurrentStageContext,
  Dot,
  IconCheck,
  IconChevron,
  IconX,
  Notice,
  Section,
  Stat,
  cx,
  type Tone,
} from "./Section";
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
  /** Repair rounds this run was allowed (PlanRequest.max_repairs); null for a recording, whose
   *  trace says it by itself. */
  maxRepairs: number | null;
  /** The finished plan that was on screen when this run started. It comes back when the run
   *  planned nothing at all (an offline backend without a recording of the brief), so a
   *  recorded run is never replaced by an empty one. */
  kept: KeptPlan | null;
  /** Why the plan on screen is not the last run's (that run had no recorded answer). */
  notice: string | null;
}

type KeptPlan = Omit<PlanState, "kept" | "notice">;

const idlePlan = (): PlanState => ({
  ...idle<PlanResult>(),
  steps: [],
  startedAt: null,
  briefId: null,
  maxRepairs: null,
  kept: null,
  notice: null,
});

/** The parts of a finished plan state that put it back on screen. */
function keepPlan(p: PlanState): KeptPlan {
  return {
    status: p.status,
    result: p.result,
    error: p.error,
    steps: p.steps,
    startedAt: p.startedAt,
    briefId: p.briefId,
    maxRepairs: p.maxRepairs,
  };
}

/** A run that planned nothing because an offline backend has no recorded answer for the brief:
 *  not one task graph came back. */
function plannedNothing(result: PlanResult): boolean {
  return (
    !result.success &&
    isOfflineMiss(result.failure, result.steps) &&
    !result.steps.some((s) => (s.stage === "taskgraph" || s.stage === "repair") && s.ok)
  );
}

interface State {
  healthStatus: HealthStatus;
  health: Health | null;
  examples: ExampleBrief[];
  examplesNote: string | null;
  examplesLoading: boolean;
  brief: string;
  briefId: string | null;
  structure: Async<StructureResponse>;
  /** Date.now() when the running structure request started (the placeholder's timer). */
  structureStartedAt: number | null;
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
  structureStartedAt: null,
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
  | { type: "structureStart"; startedAt: number }
  | { type: "structureDone"; response: StructureResponse }
  | { type: "structureError"; message: string }
  | { type: "requirements"; value: Requirements }
  /** Requirements handed in from outside the editor (the Monte Carlo what-if fleet). */
  | { type: "adoptRequirements"; value: Requirements }
  | { type: "maxRepairs"; value: number }
  | { type: "planStart"; startedAt: number; briefId: string | null; maxRepairs: number }
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
      return { ...state, structure: { status: "running", result: null, error: null }, structureStartedAt: action.startedAt };
    case "structureDone": {
      const { response } = action;
      if (!response.requirements) {
        const f = response.step.failure;
        // an offline backend without a recording for this brief says so in product words (its
        // own sentence names the lookup key and a server setting)
        const why = isOfflineMiss(f, [response.step])
          ? `${OFFLINE_MISS} ${OFFLINE_HELP}`
          : f
            ? `${tidyHint(f.hint) || f.kind}${f.conflicts.length ? ` [${f.conflicts.join(", ")}]` : ""}`
            : response.step.message || "The model returned no requirements.";
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
      // new result arrives; a request that fails leaves them untouched, and a run that plans
      // nothing (no recorded answer) puts the finished plan back
      return {
        ...state,
        plan: {
          ...state.plan,
          status: "running",
          error: null,
          steps: [],
          startedAt: action.startedAt,
          briefId: action.briefId,
          maxRepairs: action.maxRepairs,
          kept: state.plan.status === "done" && state.plan.result ? keepPlan(state.plan) : state.plan.kept,
          notice: null,
        },
      };
    case "planStep":
      if (state.plan.status !== "running") return state;
      return { ...state, plan: { ...state.plan, steps: [...state.plan.steps, action.step] } };
    case "planDone":
      // an offline backend without a recording of the brief planned nothing: the plan that was
      // on screen (a recorded run, its trace and its advice) comes back with a notice, as a
      // failed request leaves it
      if (state.plan.kept && plannedNothing(action.result)) {
        return { ...state, plan: { ...state.plan.kept, kept: null, notice: OFFLINE_KEPT } };
      }
      return {
        ...state,
        plan: {
          ...state.plan,
          status: "done",
          result: action.result,
          error: null,
          steps: action.result.steps.length ? action.result.steps : state.plan.steps,
          kept: null,
          notice: null,
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
      // "plan only", "plan and simulation", "plan, simulation and Monte Carlo"
      const names = ["plan", sim ? "simulation" : null, mc ? "Monte Carlo" : null].filter((f): f is string => f !== null);
      const parts =
        names.length === 1 ? "plan only" : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
      // a verified recording is a demo of the whole flow; a failed one is replayed for its
      // trace, its cores and the solver-proven advice that ends it
      const note = plan.success
        ? `Recorded demo · ${parts}`
        : planSuggestions(plan).length > 0
          ? "Recorded live run · plan could not be verified; here is what would make it feasible"
          : "Recorded live run · plan could not be verified";
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
        plan: {
          status: "done",
          result: plan,
          error: null,
          steps: plan.steps,
          startedAt: null,
          briefId: id,
          maxRepairs: null,
          kept: null,
          notice: null,
        },
        selectedTask: null,
        simOptions: { ...DEFAULT_SIMULATE_OPTIONS, failures },
        sim: sim ? { status: "done", result: sim, error: null } : idle(),
        mc: mc ? { status: "done", result: mc, error: null } : idle(),
        sample: { loading: false, note, error: null },
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

// ------------------------------------------------------------------ copy of the failure paths
/** An offline backend (recorded Nemotron answers) asked for a brief it has no recording of. */
const OFFLINE_MISS = "This brief has no recorded answer; connect a live backend to plan it.";
const OFFLINE_HELP = "The example briefs and the recorded demos work offline.";

/** The requirements card of a brief a recorded-answers backend has no recording of: the card
 *  takes edits, planning them takes a live backend. */
const UNRECORDED_CARD =
  "This backend replays recorded Nemotron answers and has none for this brief, so it cannot plan these requirements. The example briefs plan offline; a live backend plans any brief.";
const UNRECORDED_WHY = "this backend replays recorded Nemotron answers and has none for this brief.";

/** The notice of a run that planned nothing and left the previous plan on screen. */
const OFFLINE_KEPT =
  "This backend replays recorded Nemotron answers and has none for this brief, so the requirements were not planned. The run below is the one that was on screen; a live backend plans the edited card.";

/** True when a failed stage had no answer at all because an offline backend has no recording
 *  for the brief: the step's message starts with "offline:" (task graph and repair stages),
 *  or the failure says so (the structure stage). The backend's own sentence names its lookup
 *  key and a server setting, so the console says it in its own words. */
function isOfflineMiss(failure: Failure | null | undefined, steps: LoopStep[]): boolean {
  const last = lastFailedStep(steps);
  if (last && /^offline:/i.test(last.message ?? "")) return true;
  return failure?.kind === "llm_error" && /\bno recording for brief\b/i.test(failure.hint);
}

const DEV = process.env.NODE_ENV !== "production";

/** The error a cancelled run leaves in the plan state (the trace prints it). */
const PLAN_CANCELLED = "Planning cancelled.";

/** A request error as the page prints it: an unreachable backend or a broken stream in product
 *  words (the backend's address only in a development build), anything else as the server
 *  said it. */
function requestError(e: unknown): string {
  if (e instanceof ApiError && e.status === 0) {
    if (/^cannot reach the backend\b/i.test(e.detail)) {
      return `No backend answered${DEV ? ` at ${API_BASE}` : ""}. The recorded demos work without one; structuring and planning need it.`;
    }
    if (/did not answer within/i.test(e.detail)) return "The backend did not answer in time; try again.";
    if (/plan\/stream/i.test(e.detail)) return "The planning stream broke off before the result arrived; plan again.";
  }
  return errorMessage(e);
}

/** Seconds since `since` while `active`, ticking four times a second (the first tick lands
 *  250 ms in); null when inactive. */
function useElapsed(since: number | null, active: boolean): number | null {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [active]);
  if (!active || since === null || now === null) return null;
  return Math.max(0, (now - since) / 1000);
}

/** The bundled recordings that double as example briefs when the backend is unreachable: the
 *  briefs the backend itself ships, so the strip reads the same online and offline (the
 *  impossible-deadline recording is reached from its demo button instead). */
const OFFLINE_EXAMPLE_IDS: readonly SampleId[] = SAMPLE_IDS.filter((id) => id !== "front_loaded");

// ------------------------------------------------------------------ solver-proven advice
/** The hours an extend_horizon suggestion asks for (its horizon_h, else the makespan it
 *  proved). */
function suggestedHorizon(s: Suggestion): number | null {
  const h = s.horizon_h ?? s.makespan_h;
  return h !== null && Number.isFinite(h) && h > 0 ? Math.ceil(h) : null;
}

function nextRobotId(type: string, taken: string[]): string {
  const prefix = type.slice(0, 2).toUpperCase() || "R";
  for (let n = 1; ; n += 1) {
    const id = `${prefix}${n}`;
    if (!taken.includes(id)) return id;
  }
}

/** The requirements card after one solver-proven change, or null when the change cannot be
 *  made mechanically (reduce_scope, a robot the card no longer has, a suggestion without its
 *  numbers): the user then edits the card by hand. */
function applySuggestion(req: Requirements, s: Suggestion): Requirements | null {
  switch (s.kind) {
    case "extend_horizon": {
      const h = suggestedHorizon(s);
      return h === null ? null : { ...req, horizon_h: h };
    }
    case "raise_battery": {
      const { robot_id: id, battery_wh: wh } = s;
      if (id === null || wh === null || !req.robots.some((r) => r.id === id)) return null;
      return { ...req, robots: req.robots.map((r) => (r.id === id ? { ...r, battery_wh: wh } : r)) };
    }
    case "add_robot": {
      const { robot_type: type, battery_wh: wh } = s;
      if (type === null || wh === null) return null;
      const taken = req.robots.map((r) => r.id);
      const id = s.robot_id && !taken.includes(s.robot_id) ? s.robot_id : nextRobotId(type, taken);
      // a spare moves like its peers (the first robot of that type, else any robot)
      const peer = req.robots.find((r) => r.type === type) ?? req.robots[0];
      const speed = peer?.speed_cells_per_h ?? 12;
      return { ...req, robots: [...req.robots, { id, type, battery_wh: wh, speed_cells_per_h: speed }] };
    }
    default:
      return null;
  }
}

/** True when the card already holds the change (applied, or edited by hand): the deadline is
 *  at least the suggested one, the robot's battery at least the suggested one, the spare robot
 *  present (by id, else one more robot of that type than the failed plan had). */
function suggestionSatisfied(req: Requirements, planned: Requirements, s: Suggestion): boolean {
  switch (s.kind) {
    case "extend_horizon": {
      const h = suggestedHorizon(s);
      return h !== null && req.horizon_h >= h;
    }
    case "raise_battery": {
      const robot = req.robots.find((r) => r.id === s.robot_id);
      return robot !== undefined && s.battery_wh !== null && robot.battery_wh >= s.battery_wh;
    }
    case "add_robot": {
      if (s.robot_id && req.robots.some((r) => r.id === s.robot_id)) return true;
      if (s.robot_type === null) return false;
      const count = (list: Requirements) => list.robots.filter((r) => r.type === s.robot_type).length;
      return count(req) > count(planned);
    }
    default:
      return false;
  }
}

/** "an excavator", "a hauler". */
function withArticle(word: string): string {
  return `${/^[aeiou]/i.test(word) ? "an" : "a"} ${word}`;
}

/** Button label of a change the console can make for the user, with the figure the solver
 *  proved: "Set the deadline to 86 h", "Raise EX1 to 5,950 Wh", "Add EX3 (excavator,
 *  5,000 Wh)"; null for a suggestion that needs a hand edit (reduce_scope). */
function suggestionActionLabel(s: Suggestion): string | null {
  switch (s.kind) {
    case "extend_horizon": {
      const h = suggestedHorizon(s);
      return h === null ? null : `Set the deadline to ${fmtHours(h, 0)}`;
    }
    case "raise_battery":
      return s.robot_id === null || s.battery_wh === null ? null : `Raise ${s.robot_id} to ${fmtWh(s.battery_wh)}`;
    case "add_robot":
      if (s.robot_type === null || s.battery_wh === null) return null;
      return s.robot_id
        ? `Add ${s.robot_id} (${s.robot_type}, ${fmtWh(s.battery_wh)})`
        : `Add ${withArticle(s.robot_type)} (${fmtWh(s.battery_wh)})`;
    default:
      return null;
  }
}

/** Badge label once the change is in the requirements card (applied, or made by hand), with
 *  the figure the card holds: the past tense when it is the proven one ("Deadline set to
 *  86 h", "EX1 raised to 5,950 Wh", "EX3 added (excavator, 5,000 Wh)"), both figures when the
 *  card went further by hand ("Deadline 100 h in the card · 86 h proved enough"); null when the
 *  suggestion has no figure to name. */
function suggestionAppliedLabel(s: Suggestion, card: Requirements): string | null {
  switch (s.kind) {
    case "extend_horizon": {
      const h = suggestedHorizon(s);
      if (h === null) return null;
      return card.horizon_h === h
        ? `Deadline set to ${fmtHours(h, 0)}`
        : `Deadline ${fmtHours(card.horizon_h, 0)} in the card · ${fmtHours(h, 0)} proved enough`;
    }
    case "raise_battery": {
      if (s.robot_id === null || s.battery_wh === null) return null;
      const robot = card.robots.find((r) => r.id === s.robot_id);
      return !robot || robot.battery_wh === s.battery_wh
        ? `${s.robot_id} raised to ${fmtWh(s.battery_wh)}`
        : `${s.robot_id} at ${fmtWh(robot.battery_wh)} in the card · ${fmtWh(s.battery_wh)} proved enough`;
    }
    case "add_robot": {
      if (s.robot_type === null || s.battery_wh === null) return null;
      const robot = s.robot_id ? card.robots.find((r) => r.id === s.robot_id) : undefined;
      return robot
        ? `${robot.id} added (${robot.type}, ${fmtWh(robot.battery_wh)})`
        : `One more ${s.robot_type} in the card`;
    }
    default:
      return null;
  }
}

/** One suggestion as the outcome column offers it. */
interface AdviceAction {
  suggestion: Suggestion;
  /** the button label; null when the change needs a hand edit */
  label: string | null;
  /** the requirements card already holds the change */
  satisfied: boolean;
  /** the badge once it does, with the figure the card holds (suggestionAppliedLabel) */
  applied: string | null;
  /** the console can write the change into the card */
  applicable: boolean;
}

/** What to tell the user after a failed plan: `note` for the outcome column (under the apply
 *  buttons, or in the "Plan again" box), `next` for the line where the next stages would
 *  start. Resource changes are named only when the solver-proven advice names one; a
 *  coverage, scope or volume verdict is answered in the requirements or the brief; a model
 *  stage that ended without an answer is worded as format.ts answerFailure reads it, as the
 *  trace and the receipt word it. `unrecorded`: a recorded-answers backend has no recording of
 *  this brief, so the card takes the change but planning it takes a live backend. */
function failedPlanGuidance(
  plan: PlanResult,
  steps: LoopStep[],
  suggestions: Suggestion[],
  actions: AdviceAction[],
  unrecorded: boolean,
): { note: string; next: string } {
  if (isOfflineMiss(plan.failure, steps)) {
    return { note: `${OFFLINE_MISS} ${OFFLINE_HELP}`, next: "connect a live backend to plan this brief." };
  }
  if (suggestions.length > 0) {
    // one proven change is enough: once the card holds one, planning again is the next step
    if (actions.some((a) => a.label !== null && a.satisfied)) {
      return unrecorded
        ? {
            note: `The change is in the requirements card above. Verifying it takes a live backend: ${UNRECORDED_WHY}`,
            next: "connect a live backend to verify the change in the requirements card.",
          }
        : {
            note: "The change is in the requirements card above: plan with these requirements to verify it.",
            next: "plan again with the change in the requirements card.",
          };
    }
    const pending = actions.filter((a) => a.label !== null && a.applicable).length;
    if (pending > 0) {
      const writes = pending === 1 ? "The button writes the change" : "Each button writes its change";
      return unrecorded
        ? {
            note: `${writes} into the requirements card above. Verifying it takes a live backend: ${UNRECORDED_WHY}`,
            next: "apply a proven change above; verifying it takes a live backend.",
          }
        : {
            note: `${writes} into the requirements card above; check it there, then plan again. The planner only ever sees that card.`,
            next: "apply a proven change above, then plan again.",
          };
    }
    return unrecorded
      ? {
          note: `No single resource change was proved: trim or defer work in the requirements card above (structures, dimensions, operations). Planning it takes a live backend: ${UNRECORDED_WHY}`,
          next: "trim the scope in the requirements card; planning it takes a live backend.",
        }
      : {
          note: "No single resource change was proved: trim or defer work in the requirements card above (structures, dimensions, operations), then plan again.",
          next: "trim the scope in the requirements card, then plan again.",
        };
  }
  const g = retryGuidance(plan);
  return unrecorded
    ? { note: `${g.note} Planning again takes a live backend: ${UNRECORDED_WHY}`, next: "connect a live backend to plan again." }
    : g;
}

/** The guidance of a failed plan without solver-proven advice (see failedPlanGuidance). */
function retryGuidance(plan: PlanResult): { note: string; next: string } {
  if (!isVerdict(plan.failure)) {
    if (plan.failure?.kind === "solver_timeout") {
      return {
        note: "CP-SAT ran out of time before it could prove or refute a schedule. Plan again, or trim the requirements so the search is smaller.",
        next: "plan again, or trim the requirements.",
      };
    }
    switch (answerFailure(plan.failure)) {
      case "request":
        return {
          note: "The request to Nemotron failed (the trace's last step says why). Plan again once Nebius Token Factory answers.",
          next: "plan again once the model answers.",
        };
      case "empty":
        return {
          note: "Nemotron returned an empty answer on every ask (the trace's last step says so). Plan again: a new ask can answer differently.",
          next: "plan again for a new task graph.",
        };
      default:
        return {
          note: "Nemotron did not return a valid task graph (the trace's last step says why). Plan again: a new ask can answer differently.",
          next: "plan again for a new task graph.",
        };
    }
  }
  if (isResourceFailure(plan.failure)) {
    return {
      note: "Edit the requirements or the brief, then plan again: the core above names the budget that does not fit.",
      next: "edit the requirements or the brief, then plan again.",
    };
  }
  const conflicts = plan.failure?.conflicts ?? [];
  if (conflicts.length > 0 && conflicts.every((c) => conflictPrefix(c) === "supply")) {
    // phase 3g: the order of the work, not its scope: no edit of the requirements fixes it
    return {
      note: `Plan again: the verdict above names hauls that must wait for the digs that feed them${
        plan.repairs === 0 ? "; with repair rounds on, Nemotron adds those edges itself" : ""
      }.`,
      next: "plan again for a graph that hauls after its digs.",
    };
  }
  return {
    note: `Edit the requirements or the brief, then plan again: the verdict above names the work the task graph is missing${
      plan.repairs === 0 ? "; with repair rounds on, Nemotron adds it itself" : ""
    }.`,
    next: "edit the requirements or the brief, then plan again.",
  };
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
        const plans = await Promise.all(OFFLINE_EXAMPLE_IDS.map((id) => loadSamplePlan(id)));
        const examples = plans
          .map((p, i) => ({
            id: OFFLINE_EXAMPLE_IDS[i],
            title: titleFromBrief(p.requirements.source_brief ?? "", OFFLINE_EXAMPLE_IDS[i]),
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
        dispatch({ type: "examples", examples: [], note: `could not load examples (${requestError(e)})` });
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
    dispatch({ type: "structureStart", startedAt: Date.now() });
    try {
      const response = await postStructure({
        brief: state.brief,
        ...(state.briefId ? { brief_id: state.briefId } : {}),
      });
      dispatch({ type: "structureDone", response });
      if (response.requirements) scrollToSection("requirements");
    } catch (e) {
      dispatch({ type: "structureError", message: requestError(e) });
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
    dispatch({ type: "planStart", startedAt: Date.now(), briefId, maxRepairs: state.maxRepairs });
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
      // the section was short when the run started: land on the outcome now that it is there
      scrollToSection("plan");
    } catch (e) {
      if (ctrl.signal.aborted) return;
      dispatch({ type: "planError", message: requestError(e) });
    } finally {
      if (planAbort.current === ctrl) planAbort.current = null;
    }
  };

  const stopPlanning = () => {
    cancelPlan();
    dispatch({ type: "planError", message: PLAN_CANCELLED });
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
      dispatch({ type: "simError", message: requestError(e) });
    }
  };

  const runMonteCarlo = async (req: MonteCarloRequest) => {
    dispatch({ type: "mcStart" });
    try {
      const result = await postMonteCarlo(req);
      dispatch({ type: "mcDone", result });
    } catch (e) {
      dispatch({ type: "mcError", message: requestError(e) });
    }
  };

  /** Load a recorded run from /samples into the normal state: the plan always, the simulation
   *  and Monte Carlo result when the sample ships them (landing_pad does; tight_battery is a
   *  plan with one repair round and no simulation; front_loaded is a failed live run whose
   *  trace ends with solver-proven advice, so there is nothing to simulate). */
  const loadSample = async (id: SampleId = "landing_pad") => {
    cancelPlan();
    dispatch({ type: "sampleStart" });
    try {
      const { plan, sim, mc } = await loadSampleBundle(id);
      dispatch({ type: "sampleDone", plan, sim, mc, id });
      scrollToSection("plan");
    } catch (e) {
      dispatch({ type: "sampleError", message: errorMessage(e) });
    }
  };

  /** One solver-proven change written into the requirements card (the editor remounts on the
   *  new version and marks the card as edited); the user checks it there and plans again. */
  const adoptSuggestion = (s: Suggestion) => {
    const base = state.requirements ?? state.plan.result?.requirements ?? null;
    const next = base ? applySuggestion(base, s) : null;
    if (!next) return;
    dispatch({ type: "adoptRequirements", value: next });
    scrollToSection("requirements");
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
  // the deadline the trace's budget tables quote: the plan's own once it is done, the card's
  // while a run streams (its steps belong to the request, not to the previous plan)
  const traceHorizonH =
    state.plan.status === "done"
      ? (planResult?.requirements.horizon_h ?? null)
      : (state.requirements?.horizon_h ?? planResult?.requirements.horizon_h ?? null);
  /** solver-proven advice of a failed plan ([] for a verified one) */
  const planAdvice = useMemo<Suggestion[]>(() => (planResult ? planSuggestions(planResult) : []), [planResult]);
  const planFailed = planResult !== null && !verified;
  /** the failed plan had no answer at all: an offline backend without a recording of it */
  const planOffline = planFailed && isOfflineMiss(planResult?.failure, resultSteps);
  /** Nemotron's answers are live (or recorded by this backend while it calls the model); an
   *  unknown backend counts as live, since only a fixture backend says otherwise */
  const live = state.health?.llm !== "fixture";
  // a recorded-answers backend replays only the briefs it has recordings of, which are the
  // example briefs it lists: the card of any other brief (the impossible-deadline recording, a
  // brief of its own) can be edited but not planned there. A run that came back with no
  // recorded answer at all (state.plan.notice) says the same of this backend.
  const recordedBriefs = useMemo(() => new Set(state.examples.map((ex) => ex.id)), [state.examples]);
  const cardBriefId = state.requirementsBrief?.id ?? null;
  const unrecorded =
    state.plan.notice !== null ||
    (!live && !state.examplesLoading && !(cardBriefId !== null && recordedBriefs.has(cardBriefId)));
  // the way back into the loop after a failed plan: each proven change as a button against the
  // requirements card as it stands now, and what to tell the user
  const adviceActions = useMemo<AdviceAction[]>(() => {
    if (!planResult || !planFailed) return [];
    const card = state.requirements ?? planResult.requirements;
    return planAdvice.map((s) => ({
      suggestion: s,
      label: suggestionActionLabel(s),
      satisfied: suggestionSatisfied(card, planResult.requirements, s),
      applied: suggestionAppliedLabel(s, card),
      applicable: applySuggestion(card, s) !== null,
    }));
  }, [planResult, planFailed, planAdvice, state.requirements]);
  const guidance = useMemo(
    () =>
      planResult && planFailed
        ? failedPlanGuidance(planResult, resultSteps, planAdvice, adviceActions, unrecorded)
        : null,
    [planResult, planFailed, resultSteps, planAdvice, adviceActions, unrecorded],
  );
  /** proven changes of the failed plan (the line that stands for it while a new run streams) */
  const provenAdvice = planAdvice.filter((s) => s.verified).length;
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
            <StructuringPlaceholder startedAt={state.structureStartedAt} live={live} />
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
            planBlocked={unrecorded ? UNRECORDED_CARD : null}
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
                      ? "The previous plan is kept until the new result arrives; the column beside the trace follows the run in progress."
                      : "The last planning request failed; the previous plan below is unchanged."
                  }
                >
                  {planBusy ? "previous plan kept until this run lands" : "previous plan kept"}
                </Badge>
              ) : state.sample.note ? (
                <Badge
                  tone={planFailed ? "warn" : "info"}
                  title={
                    planFailed
                      ? "Replayed from the recording of a live Nemotron run: the trace, the cores and the advice are exactly what the planner produced; nothing was called now."
                      : "Loaded from the bundled recording of this run; Nemotron and CP-SAT were not called."
                  }
                >
                  {state.sample.note}
                </Badge>
              ) : planOffline ? (
                <Badge title="This backend replays recorded Nemotron answers and has none for this brief; a live backend plans any brief.">
                  No recorded answer for this brief
                </Badge>
              ) : planFailed && planResult && !isVerdict(planResult.failure) ? (
                <Badge tone="warn" title="The run stopped before a schedule could be proved; the trace's last step says why.">
                  {planResult.failure?.kind === "solver_timeout"
                    ? "CP-SAT ran out of time"
                    : answerFailure(planResult.failure) === "request"
                      ? "The model request failed"
                      : answerFailure(planResult.failure) === "empty"
                        ? "Nemotron returned an empty answer"
                        : "No valid task graph came back"}
                </Badge>
              ) : planFailed ? (
                <Badge
                  tone="warn"
                  title={
                    planAdvice.length > 0
                      ? "The run ended without a verified schedule. The advice next to the trace was re-solved by CP-SAT on the last task graph."
                      : "The run ended without a verified schedule; the verdict next to the trace says why."
                  }
                >
                  {planAdvice.length > 0
                    ? "Plan could not be verified; here is what would make it feasible"
                    : "Plan could not be verified"}
                </Badge>
              ) : undefined
            }
          >
            <div className="space-y-5">
              {state.plan.notice && (
                <Notice tone="warn" title="Nothing was planned: no recorded answer for these requirements">
                  {state.plan.notice}
                </Notice>
              )}
              <div className="grid grid-cols-[minmax(0,1fr)] gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <ProgressTrace
                  steps={state.plan.steps}
                  running={planBusy}
                  startedAt={state.plan.startedAt}
                  result={state.plan.status === "done" ? planResult : null}
                  error={state.plan.error}
                  onCancel={stopPlanning}
                  horizonH={traceHorizonH}
                  tasks={state.plan.status === "done" ? planTasks : []}
                  maxRepairs={state.plan.maxRepairs}
                  adviceShown={state.plan.status === "done" && planFailed && !planOffline && planAdvice.length > 0}
                />
                {planBusy && (
                  // the column beside the trace always follows the run in progress; the plan it
                  // replaces is one line under it
                  <div className="space-y-3">
                    <PlanningPlaceholder
                      steps={state.plan.steps}
                      startedAt={state.plan.startedAt}
                      maxRepairs={state.plan.maxRepairs}
                      live={live}
                    />
                    {planResult && <PreviousPlanLine plan={planResult} verified={verified} proven={provenAdvice} />}
                  </div>
                )}
                {!planBusy && state.plan.status === "error" && !planResult && (
                  <PlanStopped error={state.plan.error} onEditRequirements={() => scrollToSection("requirements")} />
                )}
                {!planBusy && planResult && verified && (
                  // wrapped, so the card keeps its own height beside a long trace
                  <div>
                    <PlanSummary plan={planResult} tasks={planTasks} receipt={receipt} />
                  </div>
                )}
                {!planBusy && planResult && planFailed && (
                  // the outcome of a failed plan sits beside the trace and stays in view while
                  // the trace is read (first on narrow screens, where it would otherwise be
                  // below eleven steps of trace)
                  <div className="order-first lg:order-none lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)] lg:self-start lg:overflow-y-auto">
                    <FailedOutcome
                      plan={planResult}
                      steps={resultSteps}
                      suggestions={planAdvice}
                      actions={adviceActions}
                      guidance={guidance?.note ?? ""}
                      offline={planOffline}
                      planning={planBusy}
                      onApply={adoptSuggestion}
                      onEditRequirements={() => scrollToSection("requirements")}
                    />
                  </div>
                )}
              </div>

              {planBusy && !(planResult && verified) && (
                <div
                  role="status"
                  className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-dashed border-line px-4 py-2.5 text-[12.5px] text-muted"
                >
                  <span className="font-medium text-fg/80">Verification receipt, Gantt, explanation and task table</span>
                  <span>
                    land here once CP-SAT proves the plan: the five constraint families with their counts, the verified
                    schedule, Nemotron&apos;s explanation of it and every task with its quantity.
                  </span>
                </div>
              )}

              {planResult && verified && planResult.schedule && (
                <div className={cx("space-y-5", keptClass)} aria-busy={planBusy || undefined}>
                  <VerificationReceipt plan={planResult} steps={resultSteps} horizonH={planResult.requirements.horizon_h} />
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

              {!planBusy && planResult && planFailed && (
                <div className="space-y-5">
                  {planResult.task_graph && (
                    <TaskTableDisclosure tasks={planTasks} label="the last task graph (not scheduled)" defaultOpen>
                      <TaskTable tasks={planTasks} schedule={null} requirements={planResult.requirements} />
                    </TaskTableDisclosure>
                  )}
                  {guidance && <DownstreamNote next={guidance.next} />}
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

// ------------------------------------------------------------------ failed plan outcome
/** What a failed plan leaves the user with, beside its trace: the solver-proven advice as the
 *  card that leads the column (the best proven change as its figure, the apply buttons right
 *  under it, the best one primary: each writes its change into the requirements card above,
 *  where "Plan with these requirements" runs the planner again), then the red verification
 *  receipt with the core. Without advice the receipt leads and a "Plan again" box says what to
 *  edit; an offline backend without a recording of the brief gets one plain card instead. */
function FailedOutcome({
  plan,
  steps,
  suggestions,
  actions,
  guidance,
  offline,
  planning,
  onApply,
  onEditRequirements,
}: {
  plan: PlanResult;
  steps: LoopStep[];
  suggestions: Suggestion[];
  /** one entry per suggestion, against the requirements card as it stands now */
  actions: AdviceAction[];
  /** what to do next, one or two sentences (failedPlanGuidance) */
  guidance: string;
  /** an offline backend had no recorded answer for the brief: nothing was planned */
  offline: boolean;
  planning: boolean;
  onApply: (s: Suggestion) => void;
  onEditRequirements: () => void;
}) {
  if (offline) {
    return (
      <Card title="No recorded answer" aside={<Badge>offline backend</Badge>}>
        <p className="text-[13px] leading-relaxed text-fg/90">{OFFLINE_MISS}</p>
        <p className="mt-1 text-xs leading-relaxed text-muted">
          {OFFLINE_HELP} A live backend structures and plans any brief with Nemotron on Nebius Token Factory.
        </p>
      </Card>
    );
  }
  // the best change the console can make is the column's primary action while the card holds
  // none of them: it is the next step of the loop (the list is best first). One proven change
  // is enough, so once one is in the card the next step is planning again, up in the card.
  const applied = actions.some((a) => a.label !== null && a.satisfied);
  const primary = applied ? -1 : actions.findIndex((a) => a.label !== null && !a.satisfied && a.applicable);
  const pending = primary >= 0;
  const editButton = (
    <Button
      size="sm"
      variant={pending ? "ghost" : "secondary"}
      disabled={planning}
      onClick={onEditRequirements}
      className={pending ? "text-fg" : undefined}
    >
      {applied && !pending ? "Back to the requirements" : "Edit the requirements"}
      <span aria-hidden="true">↑</span>
    </Button>
  );
  // the advice card leads this column, so the receipt does not repeat it
  const receipt = (
    <VerificationReceipt plan={plan} steps={steps} horizonH={plan.requirements.horizon_h} showAdvice={false} />
  );
  if (suggestions.length === 0) {
    return (
      <div className="space-y-4">
        {receipt}
        <div className="rounded-lg border border-line bg-raised px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wider text-dim">Plan again</div>
          <p className="mt-1 text-[13px] leading-relaxed text-muted">{guidance}</p>
          <div className="mt-2.5 flex flex-wrap items-center gap-2">{editButton}</div>
        </div>
      </div>
    );
  }
  const buttons = (
    <>
      {actions.map(({ suggestion, label, satisfied, applied, applicable }, i) => {
        if (label === null) return null;
        if (satisfied) {
          return (
            <Badge key={i} tone="ok" title={`${fmtSuggestion(suggestion)} · already in the requirements card`}>
              <IconCheck className="size-2.5" />
              {applied ?? label}
            </Badge>
          );
        }
        if (!applicable) return null;
        return (
          <Button
            key={i}
            size="sm"
            variant={i === primary ? "primary" : "secondary"}
            disabled={planning}
            title={`${fmtSuggestion(suggestion)} · writes the change into the requirements card`}
            onClick={() => onApply(suggestion)}
          >
            {label}
          </Button>
        );
      })}
    </>
  );
  // the hand edit is the secondary way back: it sits with the note, not among the changes
  const note = (
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
      <p className="min-w-0 flex-1 basis-64">{guidance}</p>
      {editButton}
    </div>
  );
  const anyButton = actions.some((a) => a.label !== null && (a.satisfied || a.applicable));
  return (
    <div className="space-y-4">
      <Suggestions
        suggestions={suggestions}
        variant="outcome"
        horizonH={plan.requirements.horizon_h}
        actions={anyButton ? buttons : undefined}
        actionsNote={note}
      />
      {receipt}
    </div>
  );
}

/** One line where the next stages would start: why the console stops here and how to go on. */
function DownstreamNote({ next }: { next: string }) {
  return (
    <div
      role="status"
      className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-dashed border-line px-4 py-2.5 text-[12.5px] text-muted"
    >
      <span className="font-mono text-[11px] font-medium tracking-[0.16em] text-dim">04 · 05</span>
      <span className="font-medium text-fg/80">Explanation, simulation and robustness wait for a verified schedule</span>
      <span>Next: {next}</span>
    </div>
  );
}

// ------------------------------------------------------------------ loading and stopped states
/** Step 02 while the first structuring request runs: who is working, for how long, what comes. */
function StructuringPlaceholder({ startedAt, live }: { startedAt: number | null; live: boolean }) {
  const elapsed = useElapsed(startedAt, true);
  return (
    <Card>
      <div className="flex items-center gap-3 text-[13px]">
        <Dot color="var(--accent)" pulse className="size-2.5" />
        <span className="min-w-0 flex-1 text-muted">
          <span className="font-medium text-fg">{live ? "Nemotron 3.5 Lightning" : "Nemotron"}</span> is structuring
          the brief: site, structures, fleet, horizon, constraints…
        </span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-dim">{elapsed === null ? "" : fmtSeconds(elapsed)}</span>
      </div>
      <p className="mt-1.5 pl-[22px] text-xs text-dim">
        {live ? "Usually about 5 s." : "This backend replays the recorded structuring answer."} Every field is
        editable before anything is scheduled.
      </p>
    </Card>
  );
}

/** The loop's stages as the placeholder draws them: a repair rewrites the task graph, so it
 *  lights the task-graph chip ("Repair · round 2") while the verdict it answers stays red. */
type PipelineChip = "structure" | "graph" | "validate" | "schedule" | "explain";

const PIPELINE: readonly { id: PipelineChip; label: string }[] = [
  { id: "structure", label: "Structure" },
  { id: "graph", label: "Task graph" },
  { id: "validate", label: "Validate" },
  { id: "schedule", label: "Schedule" },
  { id: "explain", label: "Explain" },
];

function chipOf(stage: Stage): PipelineChip | null {
  switch (stage) {
    case "structure":
      return "structure";
    case "taskgraph":
    case "repair":
      return "graph";
    case "validate":
      return "validate";
    case "schedule":
      return "schedule";
    case "explain":
      return "explain";
    default:
      return null;
  }
}

type ChipState = "done" | "current" | "failed" | "upcoming";

/** Done stages read like the top bar's done steps (neutral with a check); amber only for the
 *  stage running now; red for the verdict a repair round answers. */
const CHIP_TONE: Record<ChipState, string> = {
  current: "border-accent/50 bg-accent/10 text-accent",
  done: "border-white/10 bg-white/5 text-muted",
  failed: "border-fail/40 bg-fail/10 text-fail",
  upcoming: "border-white/10 text-dim",
};

/** "13 tasks" in a task-graph or repair step's message -> 13. */
function taskCount(step: LoopStep): number | null {
  const m = /^(\d+) tasks?\b/.exec(step.message ?? "");
  return m ? Number(m[1]) : null;
}

/** The right column while a plan streams (the first one, or a new run while the previous plan
 *  is kept): the loop's stages with the one running now pulsing, the elapsed time, the figures
 *  as they land (task count, makespan once CP-SAT proves it, repair rounds) and what lands
 *  here at the end, so the column beside the trace always describes the run in progress, live
 *  runs of several minutes included. */
function PlanningPlaceholder({
  steps,
  startedAt,
  maxRepairs,
  live,
}: {
  steps: LoopStep[];
  startedAt: number | null;
  /** repair rounds this run may spend (null when unknown) */
  maxRepairs: number | null;
  /** the backend calls Nemotron live (else it replays recorded answers) */
  live: boolean;
}) {
  const elapsed = useElapsed(startedAt, true);
  const next = expectedNextStage(steps);
  const last = steps.length > 0 ? steps[steps.length - 1] : null;
  const rounds = repairRounds(steps);
  // the final verdict streamed in with its advice: no repair follows, the result is on its way
  const closing = last !== null && !last.ok && stepSuggestions(last).length > 0;
  // a rejected answer is asked again (a repair answer within its round, as the trace counts)
  const reasked = last !== null && !last.ok && (last.stage === "taskgraph" || last.stage === "repair");
  const round = next === "repair" ? (last?.stage === "repair" && !last.ok ? rounds : rounds + 1) : rounds;
  // the verdict a repair round answers: the latest validate / schedule step, when it failed
  let verdict: PipelineChip | null = null;
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const s = steps[i];
    if (s.stage === "validate" || s.stage === "schedule") {
      verdict = s.ok ? null : chipOf(s.stage);
      break;
    }
  }
  const order = PIPELINE.map((c) => c.id);
  const current = closing ? null : chipOf(next);
  const stopAt = closing && last ? chipOf(last.stage) : null;
  const stateOf = (chip: PipelineChip): ChipState => {
    const i = order.indexOf(chip);
    if (closing) {
      const j = stopAt ? order.indexOf(stopAt) : order.length;
      return i < j ? "done" : i === j ? "failed" : "upcoming";
    }
    const c = current ? order.indexOf(current) : 0;
    if (i < c) return "done";
    if (i === c) return "current";
    return next === "repair" && chip === verdict ? "failed" : "upcoming";
  };
  const labelOf = (chip: PipelineChip, label: string): string => {
    if (chip !== "graph" || closing) return label;
    if (next === "repair") return `Repair · round ${round}`;
    return reasked && last?.stage === "taskgraph" ? "Task graph · asked again" : label;
  };

  // the trace's live row already says what the running stage does; this line speaks only when
  // the loop turns: a rejected answer asked again, a repair round, the closing verdict
  const ultra = live ? "Nemotron 3 Ultra" : "Nemotron";
  let status: string | null = null;
  if (closing)
    status = `${maxRepairs === 0 ? "Repairs are off" : "No repair round left"}: the verdict and its solver-proven advice are on the way.`;
  else if (next === "taskgraph" && reasked) status = `The last answer could not be used; ${ultra} is asked again.`;
  else if (next === "repair")
    status =
      last?.stage === "repair"
        ? `The repair answer could not be used; ${ultra} is asked again within round ${round}.`
        : `The infeasibility core went back to ${ultra}: repair round ${round}${maxRepairs ? ` of ${maxRepairs}` : ""}.`;

  // the figures as they land: the latest graph's task count (and how many of its tasks the
  // rate model priced, as the trace's rate-model line says it), the makespan CP-SAT proved
  let tasks: number | null = null;
  let grounded: number | null = null;
  let makespan: number | null = null;
  for (const s of steps) {
    if ((s.stage === "taskgraph" || s.stage === "repair") && s.ok) {
      tasks = taskCount(s) ?? tasks;
      grounded = taskGraphPayload(s)?.grounded ?? grounded;
    }
    const proved = scheduleReceipt(s);
    if (proved) makespan = proved.horizon_used_h;
  }
  const priced =
    tasks === null || grounded === null
      ? "proposed by Nemotron"
      : grounded === 0
        ? "the model's own estimates"
        : grounded >= tasks
          ? "priced by the rate model"
          : `${grounded} priced by the rate model`;
  const none = <span className="text-dim">—</span>;

  return (
    <Card
      title="Plan summary"
      aside={
        <span className="text-xs text-muted">
          elapsed{" "}
          <span className="font-mono tabular-nums text-fg/80">{elapsed === null ? "" : fmtSeconds(elapsed)}</span>
        </span>
      }
    >
      <ol className="flex flex-wrap items-center gap-y-1.5" aria-label="Planning stages">
        {PIPELINE.map((c, i) => {
          const st = stateOf(c.id);
          return (
            <li key={c.id} className="flex items-center" aria-current={st === "current" ? "step" : undefined}>
              {i > 0 && <span aria-hidden="true" className="mx-1 h-px w-3 bg-white/15" />}
              <span
                className={cx(
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
                  CHIP_TONE[st],
                )}
              >
                {st === "current" ? (
                  <Dot color="var(--accent)" pulse />
                ) : st === "done" ? (
                  <IconCheck className="size-2.5" />
                ) : st === "failed" ? (
                  <IconX className="size-2.5" />
                ) : null}
                {labelOf(c.id, c.label)}
              </span>
            </li>
          );
        })}
      </ol>
      {status && <p className={cx("mt-2 text-xs leading-relaxed", closing ? "text-muted" : "text-fg/80")}>{status}</p>}
      <div className="mt-4 grid grid-cols-3 gap-3 border-t border-line pt-3">
        <Stat
          label="Makespan"
          value={makespan !== null ? fmtHours(makespan, 0) : none}
          sub={makespan !== null ? "proved by CP-SAT" : "CP-SAT proves it"}
          tone={makespan !== null ? "ok" : "neutral"}
        />
        <Stat label="Tasks" value={tasks ?? none} sub={priced} />
        <Stat
          label="Repair rounds"
          value={round}
          sub={maxRepairs === null ? "so far" : maxRepairs === 0 ? "repairs off" : `of ${maxRepairs} allowed`}
        />
      </div>
      <div className="mt-4 space-y-1 border-t border-line pt-3 text-[13px] leading-relaxed">
        <p className="text-fg/90">
          {live
            ? "Nemotron 3 Ultra reasons for about a minute per task graph; a repair round adds about a minute."
            : "This backend replays recorded Nemotron answers, so the plan lands in seconds; CP-SAT still solves it live."}
        </p>
        <p className="text-muted">The verified schedule, or the solver-proven way to make it feasible, lands here.</p>
      </div>
    </Card>
  );
}

/** One line under the planning placeholder while a new run streams: the plan it will replace,
 *  whose outcome gives way to the run in progress ("Previous plan · verified, 80 h of the 336 h
 *  horizon · stays below, dimmed, until this run lands"). */
function PreviousPlanLine({ plan, verified, proven }: { plan: PlanResult; verified: boolean; proven: number }) {
  const what =
    verified && plan.schedule
      ? `verified, ${fmtHours(plan.schedule.makespan_h, 0)} of the ${fmtHours(plan.requirements.horizon_h, 0)} horizon · stays below, dimmed, until this run lands`
      : `not proved ${roundsPhrase(plan)}${proven > 0 ? ` · ${plural(proven, "proven change")} to make it feasible` : ""}`;
  return (
    <div className="rounded-lg border border-line px-4 py-2.5 text-xs leading-relaxed text-muted">
      <span className="font-medium text-fg/80">Previous plan</span> · {what}
    </div>
  );
}

/** The right column after a first run ended without a result (cancelled, or the request
 *  failed): what happened and the way back, so the column is never left blank. */
function PlanStopped({ error, onEditRequirements }: { error: string | null; onEditRequirements: () => void }) {
  const cancelled = error === PLAN_CANCELLED;
  return (
    <Card title="Plan summary" aside={<Badge>{cancelled ? "cancelled" : "no result"}</Badge>}>
      <p className="text-[13px] leading-relaxed text-fg/90">
        {cancelled ? "The run was cancelled before a result arrived." : "The run stopped before a result arrived; the trace says why."}
      </p>
      <p className="mt-1 text-xs leading-relaxed text-muted">
        The requirements card above is unchanged: plan with it again when ready.
      </p>
      <div className="mt-3">
        <Button size="sm" variant="secondary" onClick={onEditRequirements}>
          Back to the requirements
          <span aria-hidden="true">↑</span>
        </Button>
      </div>
    </Card>
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
      // the verified outcome reads as one at first sight, like the receipt below it: the red
      // cores of a repaired run sit right next to it in the trace
      className="border-ok/30"
      title={
        <span className="inline-flex items-center gap-2">
          <span className="flex size-4 items-center justify-center rounded-full bg-ok/15 text-ok">
            <IconCheck className="size-2.5" />
          </span>
          Verified plan
        </span>
      }
      aside={
        <Badge tone="ok" title="The schedule CP-SAT proved; the verification receipt below lists each constraint family">
          {schedule.status === "optimal" ? "CP-SAT optimal" : `CP-SAT ${schedule.status}`}
        </Badge>
      }
    >
      <div className="grid grid-cols-3 gap-3">
        <Stat
          label="Makespan"
          value={fmtHours(schedule.makespan_h, 0)}
          sub={`${fmtPercent(util)} of ${fmtHours(req.horizon_h, 0)} horizon`}
          tone="ok"
        />
        <Stat label="Tasks" value={tasks.length} sub={plural(req.structures.length, "structure")} />
        <Stat label="Repair rounds" value={plan.repairs} sub={`solver ${fmtSeconds(schedule.solve_time_s)}`} />
      </div>
      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-white/10" aria-hidden="true">
        <div className="h-full rounded-full bg-ok" style={{ width: `${Math.min(100, util * 100)}%` }} />
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
