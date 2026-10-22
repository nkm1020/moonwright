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
/** Unit of Task.quantity: m3 excavated or hauled, m2 stripped / graded / compacted / sintered /
 *  surveyed, m of linear work, ea for discrete items. */
export type Unit = "m3" | "m2" | "m" | "ea";

/** Display labels of the quantity units (chips, task table, diff chips). */
export const UNIT_LABELS: Record<Unit, string> = {
  m3: "m³",
  m2: "m²",
  m: "m",
  ea: "ea",
};

/** "m3" -> "m³"; null / unknown -> "" so a quantity without a unit prints bare. */
export function unitLabel(unit: Unit | string | null | undefined): string {
  if (!unit) return "";
  return UNIT_LABELS[unit as Unit] ?? unit;
}

export interface Task {
  id: string;
  name: string;
  robot_type: string;
  duration_h: number;
  energy_wh: number;
  zone: string;
  after: string[];
  structure: string | null;
  /** Scope statement (phase 3a): how much work, in `unit`, derived as `basis` from the
   *  structure's dimensions (e.g. "pi*(10 m)^2*0.4 m = 126 m3"); null for a pure time task or
   *  a graph recorded before the rate model. For a task with a quantity, duration_h and
   *  energy_wh are computed by the rate model (backend/moonwright/pipeline/rates.py) and the
   *  model's own figures are kept in estimated_duration_h / estimated_energy_wh. */
  quantity: number | null;
  unit: Unit | null;
  basis: string | null;
  estimated_duration_h: number | null;
  estimated_energy_wh: number | null;
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
  scope: "structure lacks a required phase or lost every task",
  locked: "task underway at the event redefined by the repair",
  quantity: "physical task without a priced quantity",
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
  quantity: 1e5,
} as const;

// ------------------------------------------------------------------ F7 -> F8 events
/** robot_failure: an injected failure or a battery that ran dry (the robot stops for good);
 *  battery_low: the simulator's forecast that a robot's battery no longer covers what the plan
 *  in force still asks of it (rest of its task, queued tasks, travel), fired once per plan in
 *  force and never on a disturbance-free replay of a verified plan; task_overrun: a task took
 *  more than 2 h longer than its entry. */
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

/** `structure` step payload (ok): zone geometry summary with soft warnings. */
export interface StructurePayload {
  zones: number;
  grid: [number, number];
  depot: [number, number] | null;
  /** overlapping zones, a structure in an undeclared zone, no depot, ... (never a failure) */
  warnings: string[];
}

/** `taskgraph` / `repair` step payload (ok): what the rate model did to the answer. */
export interface TaskGraphPayload {
  /** "estimate:<task>: model said 10 h / 1000 Wh, rate model gives 27 h / 3000 Wh (+170%)",
   *  or "ungrounded: no task states a quantity, ..." when the rate table could not be applied
   *  at all (the hand-made fixtures); warnings, never conflicts */
  advisories: string[];
  rate_model: boolean;
  /** tasks whose duration / energy came from the rate table (they carried a quantity) */
  grounded: number;
}

export type DiffField = "duration_h" | "energy_wh" | "quantity" | "robot_type" | "zone" | "after";

/** Short words for the diff chips ("T4 energy 4,000 → 2,100 Wh"). */
export const DIFF_FIELD_LABELS: Record<DiffField, string> = {
  duration_h: "duration",
  energy_wh: "energy",
  quantity: "quantity",
  robot_type: "robot type",
  zone: "zone",
  after: "after",
};

/** Added to a `repair` step payload (`diff`) when the answer validated: what the model
 *  changed relative to the graph the repair answered. */
export interface RepairDiff {
  added: string[];
  removed: string[];
  changed: { task: string; field: DiffField; before: unknown; after: unknown }[];
  per_type_energy_before: Record<string, number>;
  per_type_energy_after: Record<string, number>;
  /** signed % change of total energy_wh (negative = the repair re-scoped work away) */
  scope_drift_pct: number;
  total_energy_wh_before: number;
  total_energy_wh_after: number;
  total_duration_h_before: number;
  total_duration_h_after: number;
}

export type RepairPayload = TaskGraphPayload & { diff: RepairDiff };

/** `validate` step payload: the producing step's advisories, or null when it had none. */
export type ValidatePayload = { advisories: string[] } | null;

/** The verification receipt carried by an ok `schedule` step. */
export interface VerificationReceipt {
  precedence_edges: number;
  /** = number of robots (one no-overlap row each) */
  robot_rows: number;
  zone_groups: number;
  /** one row per robot in requirements order: work_wh is the energy of its entries, travel_wh
   *  the legs its sequence drives (depot to first zone, then zone to zone, 0.5 Wh per cell: what
   *  a replay spends, at most what CP-SAT reserved), used_wh = work_wh + travel_wh, pct of budget */
  battery: {
    robot: string;
    used_wh: number;
    work_wh: number;
    travel_wh: number;
    budget_wh: number;
    pct: number;
  }[];
  tightest_robot: string | null;
  horizon_used_h: number;
  horizon_h: number;
  status: "optimal" | "feasible";
  objective_bound_h: number | null;
  solve_time_s: number;
  /** true when CP-SAT budgeted transit between zones (time and 0.5 Wh per cell) */
  travel_allowance: boolean;
  /** "independent check_schedule passed" */
  recheck: string;
}

export interface SchedulePayload {
  makespan_h: number;
  status: "optimal" | "feasible";
  verified: true;
  verification: VerificationReceipt;
}

/** The closing `replan` step of every ReplanRecord.after trace (ok = a verified replan). */
export interface ReplanOutcomePayload {
  /** a repair prompt went to Nemotron (CP-SAT alone could not finish the remaining work) */
  escalated: boolean;
  offset_h: number;
  time_base: "absolute";
  remaining_tasks: number;
  before_makespan_h: number;
  after_makespan_h: number | null;
  solve_time_s: number | null;
}

export interface LoopStep {
  stage: Stage;
  attempt: number;
  ok: boolean;
  failure: Failure | null;
  duration_s: number;
  usage: Usage | null;
  message: string | null;
  /**
   * Small stage-specific extras: StructurePayload, TaskGraphPayload (a `repair` adds
   * `diff: RepairDiff`), ValidatePayload, SchedulePayload (the verification receipt) and
   * ReplanOutcomePayload. Inside ReplanRecord.after.steps every payload also carries
   * `offset_h` (planner time 0 in simulator hours) and `time_base: "relative" | "absolute"`:
   * an ok schedule step is absolute (makespan_h and the receipt's horizon figures shifted by
   * offset_h), validate / failed-schedule / repair steps quote planner hours (relative).
   */
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

// ------------------------------------------------------------------ payload accessors
/** Typed views of LoopStep.payload. Each returns null (or []) for a step of another stage,
 *  a failed step, or a payload recorded before the field existed, so the trace renders older
 *  PlanResults without special cases. */
function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** The receipt carried by an ok `schedule` step. */
export function scheduleReceipt(step: LoopStep): VerificationReceipt | null {
  if (step.stage !== "schedule" || !step.ok || !step.payload) return null;
  const v = step.payload.verification;
  return isRecord(v) && Array.isArray(v.battery) ? (v as unknown as VerificationReceipt) : null;
}

/** The last ok schedule step of a trace and its receipt: the schedule the plan carries. */
export function lastReceipt(steps: LoopStep[]): { step: LoopStep; receipt: VerificationReceipt } | null {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const receipt = scheduleReceipt(steps[i]);
    if (receipt) return { step: steps[i], receipt };
  }
  return null;
}

/** The diff of a validated repair answer. */
export function repairDiff(step: LoopStep): RepairDiff | null {
  if (step.stage !== "repair" || !step.ok || !step.payload) return null;
  const d = step.payload.diff;
  return isRecord(d) && Array.isArray(d.changed) ? (d as unknown as RepairDiff) : null;
}

/** Advisories of a taskgraph / repair / validate step (warnings, never conflicts). */
export function stepAdvisories(step: LoopStep): string[] {
  const a = step.payload?.advisories;
  return Array.isArray(a) ? a.filter((x): x is string => typeof x === "string") : [];
}

/** Rate-model summary of an ok taskgraph / repair step. */
export function taskGraphPayload(step: LoopStep): TaskGraphPayload | null {
  if ((step.stage !== "taskgraph" && step.stage !== "repair") || !step.ok || !step.payload) return null;
  const p = step.payload;
  if (typeof p.rate_model !== "boolean" || typeof p.grounded !== "number") return null;
  return { advisories: stepAdvisories(step), rate_model: p.rate_model, grounded: p.grounded };
}

/** Zone geometry summary of an ok structure step. */
export function structurePayload(step: LoopStep): StructurePayload | null {
  if (step.stage !== "structure" || !step.ok || !step.payload) return null;
  const p = step.payload;
  if (typeof p.zones !== "number" || !Array.isArray(p.grid)) return null;
  return {
    zones: p.zones,
    grid: p.grid as [number, number],
    depot: Array.isArray(p.depot) ? (p.depot as [number, number]) : null,
    warnings: Array.isArray(p.warnings) ? p.warnings.filter((w): w is string => typeof w === "string") : [],
  };
}

/** The closing step of a replan trace. */
export function replanOutcome(step: LoopStep): ReplanOutcomePayload | null {
  if (step.stage !== "replan" || !step.payload) return null;
  const p = step.payload;
  if (typeof p.escalated !== "boolean" || typeof p.remaining_tasks !== "number") return null;
  return p as unknown as ReplanOutcomePayload;
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
  /** The replanned PlanResult; its schedule is in absolute simulator hours, while
   *  after.requirements is what the planner was given: the surviving robots with their
   *  remaining batteries, every zone the simulator laid out declared, and horizon_h = the
   *  hours left at the event (relative); the ok schedule step's receipt quotes the brief's
   *  deadline. */
  after: PlanResult;
  context: string;
  /** True when CP-SAT alone could not finish the remaining work and a repair prompt went to
   *  Nemotron (solver-first replanning escalated); also true when an offline client had no
   *  recorded answer (the closing replan step's message says so). */
  escalated: boolean;
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
  /** plain prose (markdown markers stripped) */
  text: string;
  /** the brief file as written, what `moonwright plan <file>` reads */
  text_markdown: string;
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
  /** LLM repair rounds per replan, 0..10 (only used with `llm`) */
  max_repairs?: number;
  /** replan through this client instead of the solver-only replanner (default) */
  llm?: LLMKind;
  brief_id?: string;
}

export interface MonteCarloSample {
  seed: number;
  completed: boolean;
  within_horizon: boolean;
  /** "completed" | "late" | "lost_only:<type>[,<type>]" | "replan_infeasible:<kind>[,<kind>]"
   *  | "stuck" */
  outcome: string;
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
  /** Wilson 95% interval of within_horizon_rate, [low, high] */
  within_horizon_ci: [number, number];
  horizon_h: number;
  /** over completed runs */
  finish_h: MonteCarloStats;
  replans_mean: number;
  failures_mean: number;
  /** MonteCarloSample.outcome -> number of runs, most frequent first */
  outcomes: Record<string, number>;
  /** robot types with exactly one robot (sorted): losing it strands that type's work */
  single_points_of_failure: string[];
  histogram: HistogramBin[];
  samples: MonteCarloSample[];
  elapsed_s: number;
}
