/**
 * Moonwright data contract (TypeScript mirror of backend/moonwright/schemas.py).
 *
 * Field names and nullability follow the pydantic models exactly: the backend serialises
 * optional fields as `null`, never omits them. Time is integer hours in planner output
 * (Schedule) and float hours in simulator output (SimFrame/Event). Energy is Wh.
 *
 * Conflict ids (Failure.conflicts) are shared with the backend; see CONFLICT_PREFIXES.
 */

// ------------------------------------------------------------------ F1 requirements
export type Stage =
  | "structure"
  | "taskgraph"
  | "validate"
  | "schedule"
  | "repair"
  | "explain"
  | "simulate"
  | "replan";

export interface Zone {
  id: string;
  center: [number, number];
  radius: number;
}

export interface Site {
  name: string;
  grid: [number, number];
  zones: Zone[];
  depot: [number, number] | null;
}

/** Extra numeric keys (diameter_m, length_m, width_m, depth_m, height_m, thickness_m, ...) are
 *  the structure's dimensions. */
export interface Structure {
  id: string;
  type: string;
  zone: string | null;
  [dimension: string]: unknown;
}

export interface Robot {
  id: string;
  type: string;
  battery_wh: number;
  speed_cells_per_h: number;
}

export interface Requirements {
  site: Site;
  structures: Structure[];
  robots: Robot[];
  horizon_h: number;
  constraints: string[];
  source_brief: string | null;
}

// ------------------------------------------------------------------ F2 task graph
export interface Task {
  id: string;
  name: string;
  robot_type: string;
  duration_h: number;
  energy_wh: number;
  zone: string;
  after: string[];
  structure: string | null;
}

export interface TaskGraph {
  tasks: Task[];
  notes: string | null;
}

// ------------------------------------------------------------------ F4 schedule
export interface ScheduleEntry {
  task: string;
  robot: string;
  start_h: number;
  end_h: number;
}

export interface Schedule {
  entries: ScheduleEntry[];
  makespan_h: number;
  status: "optimal" | "feasible";
  objective_bound_h: number | null;
  solve_time_s: number;
}

// ------------------------------------------------------------------ F3/F4 -> F5 failure
export type FailureKind = "validation" | "infeasible" | "solver_timeout" | "schema" | "llm_error";

export interface Failure {
  kind: FailureKind;
  /** Machine-readable ids, e.g. "energy:EX1", "precedence:T4->T2", "horizon". */
  conflicts: string[];
  hint: string;
  details: string[];
}

/** Conflict id prefixes and what they mean (for chips / tooltips). */
export const CONFLICT_PREFIXES: Record<string, string> = {
  precedence: "precedence edge cannot be honoured",
  energy: "battery budget exceeded",
  zone: "one robot per zone infeasible",
  robot: "robot double-booked or overloaded",
  horizon: "cannot finish within the horizon",
  capability: "no robot of that type",
  reference: "`after` points to an unknown task",
  duplicate: "duplicated id",
  cycle: "precedence cycle",
  energy_task: "single task exceeds every battery",
  horizon_path: "critical path exceeds the horizon",
  schema: "LLM output failed schema validation",
  missing: "required task missing from the graph",
  unknown: "scheduled task not in the graph",
  duration: "entry length differs from task duration",
  makespan: "makespan differs from the latest entry end",
  changed: "remaining task redefined by the replan",
};

export function conflictPrefix(conflict: string): string {
  const i = conflict.indexOf(":");
  return i < 0 ? conflict : conflict.slice(0, i);
}

/** Id pattern and magnitude limits of schemas.py (ID_PATTERN, MAX_*), so an editor can flag
 *  what the backend would reject with a 422 before a request is made. */
export const ID_PATTERN = /^[A-Za-z0-9_.-]+$/;
export const LIMITS = {
  energyWh: 1e7,
  durationH: 10_000,
  horizonH: 100_000,
  speedCellsPerH: 1e4,
} as const;

// ------------------------------------------------------------------ F7 -> F8 events
export type EventKind = "robot_failure" | "battery_low" | "task_overrun";

export interface SimEvent {
  t_h: number;
  kind: EventKind;
  robot: string | null;
  task: string | null;
  detail: string | null;
}

// ------------------------------------------------------------------ traces
export interface Usage {
  model: string;
  input_tokens: number;
  output_tokens: number;
  latency_s: number;
}

export interface LoopStep {
  stage: Stage;
  attempt: number;
  ok: boolean;
  failure: Failure | null;
  duration_s: number;
  usage: Usage | null;
  message: string | null;
  /** Small stage-specific extras, e.g. schedule: {makespan_h, status, verified}. */
  payload: Record<string, unknown> | null;
}

export interface PlanResult {
  requirements: Requirements;
  task_graph: TaskGraph | null;
  schedule: Schedule | null;
  explanation: string | null;
  steps: LoopStep[];
  repairs: number;
  success: boolean;
  failure: Failure | null;
}

// ------------------------------------------------------------------ simulation
export type RobotStatus = "idle" | "moving" | "working" | "failed" | "done";

export interface RobotState {
  id: string;
  pos: [number, number];
  battery_wh: number;
  status: RobotStatus;
  current_task: string | null;
}

export interface SimFrame {
  t_h: number;
  robots: RobotState[];
  done_tasks: string[];
  active_tasks: string[];
  /** task id -> 0..1 */
  progress: Record<string, number>;
}

export interface ReplanRecord {
  event: SimEvent;
  t_h: number;
  remaining_tasks: string[];
  /** Schedule in force before the event (absolute simulator hours). */
  before: Schedule;
  /** The replanned PlanResult; its schedule is in absolute simulator hours. */
  after: PlanResult;
  context: string;
}

export interface SimResult {
  /** Thinned frames (the API keeps every n-th frame plus the last). */
  frames: SimFrame[];
  events: SimEvent[];
  replans: ReplanRecord[];
  completed: boolean;
  finished_at_h: number | null;
  /** Schedule in force at the end (absolute hours). */
  schedule: Schedule | null;
  /** Task graph in force at the end: only the tasks that were still planned; take the full
   *  task list from PlanResult.task_graph plus replans[i].after.task_graph. */
  task_graph: TaskGraph | null;
}

// ------------------------------------------------------------------ HTTP API (backend/moonwright/api.py)
export type LLMKind = "fixture" | "nebius" | "record";

export interface Health {
  status: string;
  version: string;
  /** Client kind the server would use by default: "fixture" | "nebius" | "record". */
  llm: string;
  examples: number;
}

export interface ExampleBrief {
  id: string;
  title: string;
  text: string;
}

export interface StructureRequest {
  brief: string;
  brief_id?: string;
  llm?: LLMKind;
}

export interface StructureResponse {
  /** null when the model's answer could not be structured (see step.failure). */
  requirements: Requirements | null;
  step: LoopStep;
  /** Fixture/recording key derived from brief_id or the brief hash. */
  key: string;
}

/** Exactly one of `brief` / `requirements` must be given. With `requirements` (edited by the
 *  user) the structure stage is skipped. */
export interface PlanRequest {
  brief?: string;
  requirements?: Requirements;
  brief_id?: string;
  max_repairs?: number;
  llm?: LLMKind;
  solver_time_limit_s?: number;
  explain?: boolean;
}

/** Server-sent events of POST /plan/stream (same body as PlanRequest). */
export type PlanStreamEvent =
  | { event: "step"; data: LoopStep }
  | { event: "result"; data: PlanResult }
  | { event: "error"; data: { detail: string } };

export interface FailureSpec {
  robot: string;
  t_h: number;
}

export interface SimulateRequest {
  plan: PlanResult;
  failures: FailureSpec[];
  max_replans?: number;
  max_repairs?: number;
  brief_id?: string;
  llm?: LLMKind;
  /** keep every n-th frame (default 4) */
  frame_stride?: number;
  /** simulation step in hours (default 0.25, min 0.05) */
  dt_h?: number;
}

export interface MonteCarloRequest {
  plan: PlanResult;
  /** 1..200, default 50 */
  runs?: number;
  /** per-robot failure probability per hour, e.g. 0.002 (default) */
  failure_rate_per_robot_h?: number;
  /** relative task-duration spread, 0..1, e.g. 0.15 (default) */
  duration_jitter?: number;
  seed?: number;
  max_replans?: number;
  brief_id?: string;
}

export interface MonteCarloSample {
  seed: number;
  completed: boolean;
  within_horizon: boolean;
  finished_at_h: number | null;
  events: number;
  replans: number;
  failures: FailureSpec[];
}

export interface MonteCarloStats {
  mean: number | null;
  p50: number | null;
  p90: number | null;
  min: number | null;
  max: number | null;
}

export interface HistogramBin {
  start_h: number;
  end_h: number;
  count: number;
}

export interface MonteCarloResult {
  runs: number;
  completed: number;
  /** completed / runs */
  completion_rate: number;
  /** completed within horizon_h / runs: the "기한 내 완공 확률" of the PRD */
  within_horizon_rate: number;
  horizon_h: number;
  /** over completed runs */
  finish_h: MonteCarloStats;
  replans_mean: number;
  failures_mean: number;
  histogram: HistogramBin[];
  samples: MonteCarloSample[];
  elapsed_s: number;
}
