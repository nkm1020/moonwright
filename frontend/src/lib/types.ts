/**
 * Moonwright data contract (TypeScript mirror of backend/moonwright/schemas.py).
 *
 * Field names and nullability follow the pydantic models exactly: the backend serialises
 * optional fields as `null`, never omits them. Time is integer hours in planner output
 * (Schedule) and float hours in simulator output (SimFrame/Event). Energy is Wh.
 *
 * Conflict ids (Failure.conflicts) are shared with the backend; see CONFLICT_PREFIXES.
 *
 * Phase 3c additions: Structure.operations (the phases the brief asks for), Suggestion /
 * PlanResult.suggestions (solver-proven advice when no repair can work; the final failed step
 * carries the same list in its payload) and the `volume:<structure>` advisory.
 *
 * Phase 3d: RepairDiff.renumbered and changed[].from (the backend pairs tasks across a
 * renumbering), `name` / `structure` / `unit` as diffed fields, BudgetRow on a failed schedule
 * step's payload (the numbers behind its energy core). Phase 3e: the `volume:<structure>`
 * validation conflict, `schema:json` / `schema:shape` for rejected model answers, and the
 * `min_<dimension>_m` structure extras (the smallest size a brief allows). Phase 3g: the
 * `supply:<task>` validation conflict (a haul that waits for no dig, or for too little while
 * another dig could supply it), the `supply:<structure>` balance advisory, and the structure
 * step's lift-count correction. Phase 3i: `volume:<structure>` also for a double count (dig or
 * haul parts summing to 175 % or more of the largest volume the brief allows; live, a berm
 * hauled as its length x height x base width box, twice its triangular section), and the
 * structure step's footprint correction (a "12 m by 12 m" pit recorded as a 12 m diameter).
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

/** The phases a brief can ask for on a structure: the validator's closed vocabulary, in work
 *  order (inspection and staking are "survey", stripping and levelling are "grade"). */
export const OPERATIONS = ["survey", "excavate", "grade", "haul", "compact", "sinter"] as const;
export type Operation = (typeof OPERATIONS)[number];

/** Display words of the operations (the verbs stay lower case: "sinter", "excavate"). */
export const OPERATION_LABELS: Record<Operation, string> = {
  survey: "survey",
  excavate: "excavate",
  grade: "grade",
  haul: "haul",
  compact: "compact",
  sinter: "sinter",
};

/** Extra numeric keys (diameter_m, length_m, width_m, depth_m, height_m, thickness_m, and
 *  trench_width_m / trench_depth_m for the key trench under a berm) are the structure's
 *  dimensions; `operations` is not one (format.ts NON_DIMENSION_KEYS skips it). A brief that
 *  lets the work shrink ("any diameter between 12 m and 20 m") adds `min_<dimension>_m`
 *  beside the regular key (min_diameter_m, min_depth_m, min_length_m, min_width_m,
 *  min_height_m: the smallest size the brief allows, phase 3e); the validator's volume check
 *  takes its floor from them. They are dimensions like the others (editable, round-tripped to
 *  POST /plan); format.ts minDimension / dimPhrase read them ("min diameter 12 m"). */
export interface Structure {
  id: string;
  type: string;
  zone: string | null;
  /** The phases the brief explicitly asks for on this structure (closed vocabulary,
   *  deduplicated, order kept; [] when it names none): always present in backend JSON, absent
   *  in recordings older than phase 3c. The validator requires a task of the right robot type
   *  tagged with the structure for each one the fleet can staff, else the validate step fails
   *  with `scope:<structure>` ("S2 (dust berm) lacks excavate task (its operations list
   *  excavate)" and a hint that quotes the brief). The editable requirements card round-trips
   *  the key to POST /plan {requirements}. */
  operations?: Operation[];
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

/** Conflict id prefixes and what they mean (for chips / tooltips). A few ids are complete
 *  words rather than prefixes (`schema:json`, `schema:shape`): look a conflict up through
 *  conflictMeaning (format.ts), which tries the whole id before its prefix. */
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
  schema: "model answer failed schema validation",
  /** phase 3e: a rejected model answer, by what went wrong on its asks */
  "schema:json": "answer cut off or not valid JSON",
  "schema:shape": "answer did not match the schema",
  missing: "required task missing from the graph",
  unknown: "scheduled task not in the graph",
  duration: "entry length differs from task duration",
  makespan: "makespan differs from the latest entry end",
  changed: "remaining task redefined by the replan",
  scope: "structure lacks a required phase or lost every task",
  locked: "task underway at the event redefined by the repair",
  quantity: "physical task without a priced quantity",
  /** phase 3e: the plan's m³ fall below 70 % of the smallest volume the dimensions allow;
   *  phase 3i: or its dig / haul parts sum to 175 % of the largest, counting the body twice
   *  (the detail then carries ", a double count": format.ts isDoubleCountFailure) */
  volume: "planned volume off the structure's dimensions (short, or counted twice)",
  /** phase 3g: a haul waits (transitively) for no dig, or for less than it moves while another
   *  dig could supply it, so it could start before its material exists */
  supply: "haul planned before its material is dug",
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
  /**
   * Never a failure. In this order: recovered notes, restored operations, dropped operations,
   * site warnings, lift corrections, footprint corrections, size corrections, dimension
   * warnings.
   * (a) "structure S1: operations [excavate, compact, sinter] recovered from malformed keys
   *     ("operations': [", 'grade', 'compact', 'sinter'); check them against the brief": the
   *     structuring model's scrambled answer was salvaged (amber: ask the user to check);
   * (a2) phase 3l: "structure S1: operations compact restored: its entry states "subgrade
   *     compacted and inspected"" (or "operations haul and survey restored: its entry states
   *     "..." and "...""): an operation the structure's own entry in the brief states that the
   *     structuring model omitted (never grade, never a negated or deferred one, never one
   *     said of another structure, never one the fleet cannot staff), already appended to the
   *     structure's operations so the planner plans it and the coverage check requires it
   *     (amber: check it against the brief; filed under its structure);
   * (b) phase 3j: "structure S2: operations sinter dropped: the fleet has no printer, so no
   *     sinter task will be required of it (add a printer and the operation again to plan
   *     it)": an operation no robot type of the fleet can perform, already dropped from the
   *     requirements so the planner never plans it (muted; the card shows the operation as a
   *     dashed "dropped" chip). Recordings older than phase 3j carry instead "structure S2
   *     lists sinter but the fleet has no printer: no sinter task will be required of it (add
   *     a printer or drop the operation)" (muted);
   * (b2) "structure STR1 lists excavate but the brief line about it asks for haul, not
   *     excavate: no excavate task will be required of it (...)" (muted);
   * (c) "structure S2 (blast berm) has no dimensions: the planner sizes its tasks from numeric
   *     fields such as diameter_m, length_m, width_m, depth_m or height_m and never sees the
   *     brief, so add the brief's numbers before planning" (amber: fill them before planning);
   * (d) phase 3g, between the site and the dimension warnings: "structure S1: lifts 4 -> 1:
   *     the brief's entry for S1 states no lift count we can read" (or "lifts 3 -> 2: the
   *     brief's entry for S1 states 2 compaction lifts", "lifts none -> 2: ..."): the
   *     structuring stage already wrote the corrected count into the requirements, so the
   *     planner compacts what the eval judges (amber: check it against the brief; filed under
   *     its structure);
   * (e) phase 3i, after the lift corrections: "structure S1: diameter_m 12 -> 12 m x 12 m: the
   *     brief's entry for S1 states a 12 m by 12 m footprint" (or "diameter_m 12,
   *     min_diameter_m 12 -> ...", or "diameter_m 20, min_diameter_m 12 -> 20 m x 20 m, at
   *     least 12 m x 12 m: ... footprint, at least 12 m by 12 m" when the entry states a smaller
   *     size too): a square or rectangle the structuring model recorded as a diameter, already
   *     written into the requirements as length_m x width_m (and min_length_m x min_width_m)
   *     (amber: check it against the brief; filed under its structure);
   * (f) phase 3j, after the footprint corrections: "structure S1: min_diameter_m none -> 12:
   *     the brief allows "any diameter between 12 m and 20 m that fits"": a smaller size the
   *     brief allows, read anywhere in it and tied to its structure, already written into the
   *     requirements, and "structure S2: diameter_m 30 dropped: the brief's entry for S2
   *     states 30 m only as a distance ("30 m north of the pad")": a position recorded as a
   *     size, already dropped (amber: check it against the brief; filed under its structure;
   *     no (c) note follows for a structure left without dimensions this way, since the
   *     brief gives it no size to add);
   * plus the older zone-overlap / undeclared-zone / no-zone / no-depot warnings (muted).
   */
  warnings: string[];
}

/** `taskgraph` / `repair` step payload (ok): what the rate model did to the answer. */
export interface TaskGraphPayload {
  /** "estimate:<task>: model said 10 h / 1000 Wh, rate model gives 27 h / 3000 Wh (+170%)",
   *  "ungrounded: no task states a quantity, ..." when the rate table could not be applied at
   *  all (the hand-made fixtures), or "volume:<structure>: its dimensions give 360 m3
   *  (100 x 6 x 0.6 m) but its excavation tasks (T3 600 m2) total only 0 m3 (0%)" when the
   *  planned m3 clearly differ from what the dimensions imply (two lines may exist for one
   *  structure: a berm body and its key trench; never on a replan's steps; since phase 3i an
   *  excess of 175 % or more of the largest volume the brief allows ends ", a double count":
   *  the validator rejects it as volume:<structure> too), or (phase 3g)
   *  "supply:<structure>: its hauls (T5 150 m3, T6 150 m3) move 300 m3, more than the 125.7 m3
   *  the digs before them remove (S1 landing pad: T3, T4)" when a body's hauls together move
   *  more than the digs before them remove (often the brief's own arithmetic; never on a
   *  replan's steps, nor for a body whose hauls are a double count); warnings, never
   *  conflicts. format.ts parseAdvisory reads them. */
  advisories: string[];
  rate_model: boolean;
  /** tasks whose duration / energy came from the rate table (they carried a quantity) */
  grounded: number;
}

/** The task fields a repair diff compares (backend loop.py DIFF_FIELDS); `name`, `structure`
 *  and `unit` arrived in phase 3d (a recording made before carries only the other six). */
export type DiffField =
  | "name"
  | "duration_h"
  | "energy_wh"
  | "quantity"
  | "robot_type"
  | "zone"
  | "structure"
  | "unit"
  | "after";

/** Short words for the diff chips ("T4 energy 4,000 → 2,100 Wh"). */
export const DIFF_FIELD_LABELS: Record<DiffField, string> = {
  name: "name",
  duration_h: "duration",
  energy_wh: "energy",
  quantity: "quantity",
  robot_type: "robot type",
  zone: "zone",
  structure: "structure",
  unit: "unit",
  after: "after",
};

/** One changed field of a task the backend paired across the repair. `task` is the id in the
 *  answer (the after graph); `from` the id the task had in the graph the repair answered,
 *  present only when the task was renumbered. An `after` row's `before` list is written in the
 *  answer's ids (renumbering applied) and a predecessor that no longer exists reads
 *  "T2 (removed)"; its `after` list is the answer's own. */
export interface DiffChange {
  task: string;
  from?: string;
  field: DiffField;
  before: unknown;
  after: unknown;
}

/** One id the repair renumbered: the task kept its work (name and slot) under a new id. */
export interface DiffRenumbering {
  from: string;
  to: string;
}

/** Added to a `repair` step payload (`diff`) when the answer validated: what the model
 *  changed relative to the graph the repair answered.
 *
 *  Phase 3d recordings pair the two graphs before diffing them (same name and slot, a unique
 *  name, a unique robot type / zone / structure / unit slot, a kept id): `added` / `removed`
 *  are the truly unmatched ids, `renumbered` lists every paired task whose id changed (always
 *  present, [] when none) and `changed` rows carry `from` for those. A recording made before
 *  (no `renumbered` key) diffed per id, so a renumbering there reads as a cascade of field
 *  changes and is only ever inferred (format.ts describeRepairDiff). */
export interface RepairDiff {
  added: string[];
  removed: string[];
  /** absent in recordings older than phase 3d */
  renumbered?: DiffRenumbering[];
  changed: DiffChange[];
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

/** `validate` step payload: the producing step's advisories, or null when it had none; the
 *  final failed validate step of a failed run adds the advice (SuggestionsPayload). */
export type ValidatePayload = ({ advisories?: string[] } & Partial<SuggestionsPayload>) | null;

/** The verification receipt carried by an ok `schedule` step. */
export interface VerificationReceipt {
  precedence_edges: number;
  /** = number of robots (one no-overlap row each) */
  robot_rows: number;
  zone_groups: number;
  /** one row per robot in requirements order: work_wh is the energy of its entries, travel_wh
   *  the legs its sequence drives (depot to first zone, then zone to zone, 0.5 Wh per cell: what
   *  a replay spends; since phase 3k exactly what CP-SAT charged, before it at most what its
   *  per-task transit allowance reserved), used_wh = work_wh + travel_wh, pct of budget */
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
  /** true when CP-SAT timed and charged travel between zones (whole hours and 0.5 Wh per cell
   *  per leg); the name predates phase 3k, which made the legs exact instead of an allowance */
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

/**
 * The numbers behind the `energy:<robot>` conflicts of a failed `schedule` step (backend
 * schedule.py budget_rows), exact Wh floats (the hint quotes them rounded: "9,930 Wh").
 * kind "type": the type's work fits the pool of its robots' batteries but no split of it fits
 * the single batteries (or, when demanded_wh > budget_wh, it exceeds even the pool): `robots`
 * is every robot of the type, `budget_wh` their batteries combined. kind "robot": the tasks
 * only that robot can run (locked to it, or it is the only robot of its type) exceed its
 * battery. kind "order" (phase 3k): the only robot of its type, whose tasks fit its battery in
 * their cheapest order (`demanded_wh`, within `budget_wh`) but not in the order the conflict's
 * precedence edges, deadline or zones hold it to. `travel_wh` is the travel inside
 * `demanded_wh`: since phase 3k the least any order and split of the tasks needs, so
 * `demanded_wh` is a lower bound (a per-task transit allowance before it). Rows come in the
 * order the conflicts first name them.
 */
export interface BudgetRow {
  kind: "type" | "robot" | "order";
  subject: string;
  robots: string[];
  budget_wh: number;
  demanded_wh: number;
  travel_wh: number;
}

/** Payload of a `schedule` step with ok=false (phase 3d): `budget` is present only when the
 *  verdict names energy:<robot> conflicts; the final failed step of a failed run adds the
 *  advice (SuggestionsPayload). Inside ReplanRecord.after.steps it also carries `offset_h` and
 *  `time_base: "relative"` (the rows themselves have no time base). Read it through
 *  stepBudget. */
export type BudgetPayload = ({ budget?: BudgetRow[] } & Partial<SuggestionsPayload>) | null;

// ------------------------------------------------------------------ F5 -> advice
/** extend_horizon: a later deadline; add_robot: one more robot of a type; raise_battery: one
 *  robot's battery raised; reduce_scope: no single change verifies, cut or defer work. */
export type SuggestionKind = "extend_horizon" | "add_robot" | "raise_battery" | "reduce_scope";

/** Row labels of the advice ("later deadline · finishing needs 85 h, ..."). */
export const SUGGESTION_KIND_LABELS: Record<SuggestionKind, string> = {
  extend_horizon: "later deadline",
  add_robot: "one more robot",
  raise_battery: "bigger battery",
  reduce_scope: "smaller scope",
};

/** Copy of the advice block, shared by Suggestions.tsx and every place that repeats it (the
 *  trace under the final failed step, the outcome area of a failed plan, the failed-replan
 *  row). The kind label and the chip go on each row; the detail sentence is printed with its
 *  words verbatim (format.ts fmtSuggestion spaces units and formats the numbers that carry
 *  one, "5950 Wh" -> "5,950 Wh", like every other figure on the page). */
export const SUGGESTION_COPY = {
  heading: "What would make it feasible",
  /** chip of a suggestion with verified: true */
  verified: "CP-SAT verified",
  /** chip of reduce_scope (never verified) */
  unverified: "not solver-verified",
  footer:
    "Each change was re-solved by CP-SAT on the last task graph; figures are in the planning model's units (work + the legs each robot drives)",
  /** note of the failed-replan variant: its hours need no offset */
  replanNote: "Hours are absolute simulator time against the brief's deadline",
} as const;

/**
 * A resource change that would make the last task graph feasible, found by the backend
 * (moonwright.pipeline.relax) when a plan ends without a verified schedule. `detail` is one
 * sentence with units on every number: "EX1 needs 5950 Wh (has 5000 Wh): feasible at 62 h",
 * "one more excavator (EX3, 5000 Wh like EX1): feasible at 62 h", "a battery of 5200 Wh on
 * EX2 is enough (has 5000 Wh; a smaller one may be, the search ran out of time): feasible at
 * 62 h", "finishing needs 85 h, 25 h past the 60 h deadline (CP-SAT optimal)"; a replan's
 * add_robot reads "(EX3, 5000 Wh, a full battery)". Lists are best first, at most 4: changes
 * that keep the deadline (raise_battery, add_robot) before extend_horizon, smaller relative
 * change first; a lone reduce_scope when no single change verifies.
 */
export interface Suggestion {
  kind: SuggestionKind;
  detail: string;
  /** makespan of the CP-SAT solve that proved the change; null for reduce_scope */
  makespan_h: number | null;
  /** add_robot: the type added; raise_battery: the robot's type */
  robot_type: string | null;
  /** add_robot: the new robot's id (EX3); raise_battery: the robot whose battery is raised */
  robot_id: string | null;
  /** add_robot: the new robot's battery; raise_battery: the battery that is enough */
  battery_wh: number | null;
  /** extend_horizon: the deadline that works (= makespan_h) */
  horizon_h: number | null;
  /** true when a CP-SAT solve on the modified requirements proved it; reduce_scope never is */
  verified: boolean;
}

/** Shape test for one suggestion read from an untyped payload or an older recording. */
export function isSuggestion(v: unknown): v is Suggestion {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return false;
  const s = v as Record<string, unknown>;
  return typeof s.kind === "string" && s.kind in SUGGESTION_KIND_LABELS && typeof s.detail === "string";
}

/**
 * Carried by the FINAL failed step of a failed run (the validate or schedule step with
 * ok=false that no repair could answer, or the rejected repair step that ended the last round)
 * in the SSE "step" event itself, so the trace can render the advice before the "result"
 * event; JSON-identical to PlanResult.suggestions; absent when there is nothing to say.
 * Inside ReplanRecord.after.steps the same payload also carries
 * `suggestions_time_base: "absolute"` next to its `time_base: "relative"`: replan advice is in
 * absolute simulator hours against the brief's deadline (no offset to add); only
 * extend_horizon / add_robot occur in replans. format.ts stepSuggestions reads it.
 */
export interface SuggestionsPayload {
  suggestions: Suggestion[];
  suggestions_time_base?: "absolute";
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
   * `diff: RepairDiff`), ValidatePayload, SchedulePayload (the verification receipt),
   * ReplanOutcomePayload, and SuggestionsPayload on the final failed step of a failed run
   * (a failed validate step may carry advisories next to it). Inside ReplanRecord.after.steps
   * every payload also carries `offset_h` (planner time 0 in simulator hours) and
   * `time_base: "relative" | "absolute"`: an ok schedule step is absolute (makespan_h and the
   * receipt's horizon figures shifted by offset_h), validate / failed-schedule / repair steps
   * quote planner hours (relative), while their advice is absolute (suggestions_time_base).
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
  /** Solver-proven advice of a failed run (see Suggestion); [] on success; absent in
   *  recordings older than phase 3c. Read it through format.ts planSuggestions, which falls
   *  back to the final failed step's payload while a run is still streaming. */
  suggestions?: Suggestion[];
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

/** The diff of a validated repair answer. A malformed `renumbered` list is dropped (the diff
 *  then reads like a recording older than phase 3d). */
export function repairDiff(step: LoopStep): RepairDiff | null {
  if (step.stage !== "repair" || !step.ok || !step.payload) return null;
  const d = step.payload.diff;
  if (!isRecord(d) || !Array.isArray(d.changed) || !Array.isArray(d.added) || !Array.isArray(d.removed)) return null;
  const diff = d as unknown as RepairDiff;
  if (d.renumbered === undefined) return diff;
  const ok =
    Array.isArray(d.renumbered) &&
    d.renumbered.every((r) => isRecord(r) && typeof r.from === "string" && typeof r.to === "string");
  if (ok) return diff;
  const copy: RepairDiff = { ...diff };
  delete copy.renumbered;
  return copy;
}

/** True for a diff the backend computed by pairing tasks (phase 3d): `renumbered` is present,
 *  so added / removed / renumbered / changed can be read verbatim. */
export function isMatchedDiff(diff: RepairDiff): diff is RepairDiff & { renumbered: DiffRenumbering[] } {
  return Array.isArray(diff.renumbered);
}

function isBudgetRow(v: unknown): v is BudgetRow {
  if (!isRecord(v)) return false;
  return (
    (v.kind === "type" || v.kind === "robot" || v.kind === "order") &&
    typeof v.subject === "string" &&
    Array.isArray(v.robots) &&
    v.robots.every((r) => typeof r === "string") &&
    typeof v.budget_wh === "number" &&
    typeof v.demanded_wh === "number" &&
    typeof v.travel_wh === "number"
  );
}

/** The budget rows of a failed `schedule` step (the numbers behind its energy core); [] for
 *  any other step and for a recording made before the backend attached them. */
export function stepBudget(step: LoopStep): BudgetRow[] {
  if (step.stage !== "schedule" || step.ok || !step.payload) return [];
  const b = step.payload.budget;
  return Array.isArray(b) ? b.filter(isBudgetRow) : [];
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
   *  deadline. The advice of a failed replan (after.suggestions and the final failed step's
   *  SuggestionsPayload) is rebased the same way: absolute simulator hours against the
   *  brief's deadline, and a spare robot is a full battery. */
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
  /** completed within horizon_h / runs: the PRD's "probability of finishing by the deadline" */
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
