/**
 * Formatting helpers shared by the shell components. Hours are the planner's unit (integer in
 * schedules, float in the simulator); energy is Wh; token counts come from LoopStep.usage.
 */
import {
  CONFLICT_PREFIXES,
  DIFF_FIELD_LABELS,
  OPERATIONS,
  OPERATION_LABELS,
  SUGGESTION_COPY,
  SUGGESTION_KIND_LABELS,
  conflictPrefix,
  isMatchedDiff,
  isSuggestion,
  repairDiff,
  unitLabel,
  type BudgetRow,
  type DiffField,
  type DiffRenumbering,
  type Failure,
  type LoopStep,
  type Operation,
  type PlanResult,
  type RepairDiff,
  type Structure,
  type Suggestion,
  type SuggestionKind,
  type Task,
  type Usage,
} from "./types";

export { OPERATIONS } from "./types";

/** Structure keys that are never dimensions: the identity fields and the operations list
 *  (a non-numeric value is skipped anyway; a boolean flag is never 1 m). */
export const NON_DIMENSION_KEYS: ReadonlySet<string> = new Set(["id", "type", "zone", "operations"]);

/** The space inside a figure ("52 h", "1 m 16 s", "4,009 Wh"): no-break, so a figure never
 *  wraps between its number and its unit. */
const NBSP = "\u00a0";

export function fmtNum(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (Number.isInteger(n)) return n.toLocaleString("en-US");
  return n.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

/** "77 h", "90.3 h"; integers print without decimals, null prints an em dash. */
export function fmtHours(h: number | null | undefined, digits = 1): string {
  if (h === null || h === undefined || !Number.isFinite(h)) return "—";
  return `${fmtNum(h, digits)}${NBSP}h`;
}

/** "336 h (14 d)" for horizons; anything under two days stays in hours only. */
export function fmtHorizon(h: number): string {
  if (!Number.isFinite(h)) return "—";
  if (h < 48) return fmtHours(h, 0);
  return `${fmtHours(h, 0)} (${fmtNum(h / 24, 1)}${NBSP}d)`;
}

/** "4.57 s", "12.3 s", "1 m 16 s", one unbreakable figure. */
export function fmtSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  if (s < 0.005) return `<0.01${NBSP}s`;
  if (s < 10) return `${s.toFixed(2)}${NBSP}s`;
  if (s < 60) return `${s.toFixed(1)}${NBSP}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return `${m}${NBSP}m${NBSP}${String(rest).padStart(2, "0")}${NBSP}s`;
}

export function fmtPercent(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

/** 1234 -> "1.2k", 12 -> "12", 2_500_000 -> "2.5M". */
export function fmtCompact(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
}

export function fmtWh(wh: number): string {
  return `${fmtNum(wh, 0)}${NBSP}Wh`;
}

export function plural(n: number, singular: string, pluralForm = `${singular}s`): string {
  return `${fmtNum(n, 0)} ${n === 1 ? singular : pluralForm}`;
}

/** "diameter_m" -> "diameter (m)", "trench_depth_m" -> "trench depth (m)", "slots" -> "slots". */
export function dimLabel(key: string): string {
  const m = /^(.*)_(m|m2|m3|cm|mm|kg|t|wh|kwh|h|deg)$/i.exec(key);
  const base = (m ? m[1] : key).replace(/_/g, " ");
  return m ? `${base} (${m[2]})` : base;
}

/** The dimension a phase-3e floor key bounds: "min_diameter_m" -> "diameter_m" (the smallest
 *  size the brief allows); null for any other key. */
export function minDimension(key: string): string | null {
  const m = /^min_([a-z][a-z_]*_m)$/.exec(key);
  return m ? m[1] : null;
}

/** A floor key as prose: ("min_diameter_m", 12) -> "min diameter 12 m"; null for any other
 *  key (the card's own phrases cover the regular dimensions). */
export function dimPhrase(key: string, value: number): string | null {
  const base = minDimension(key);
  if (!base) return null;
  return `min ${base.replace(/_m$/, "").replace(/_/g, " ")} ${fmtNum(value, 2)} m`;
}

/** Numeric extras of a structure (its dimensions) in declaration order. */
export function structureDims(s: Structure): [string, number][] {
  const out: [string, number][] = [];
  for (const [k, v] of Object.entries(s)) {
    if (NON_DIMENSION_KEYS.has(k)) continue;
    if (typeof v === "number" && Number.isFinite(v)) out.push([k, v]);
  }
  return out;
}

// ------------------------------------------------------------------ operations
/** "sinter" -> "sinter" (the verbs stay lower case); an unknown word prints as it came, with
 *  underscores read as spaces. */
export function operationLabel(op: Operation | string): string {
  return OPERATION_LABELS[op as Operation] ?? String(op).replace(/_/g, " ");
}

export function isOperation(v: unknown): v is Operation {
  return typeof v === "string" && (OPERATIONS as readonly string[]).includes(v);
}

/** The operations a structure lists, defensively: unknown strings dropped, repeats dropped
 *  (first mention's place kept), [] when the key is missing (a recording older than phase 3c)
 *  or malformed. */
export function structureOperations(s: Structure): Operation[] {
  const raw = s.operations;
  if (!Array.isArray(raw)) return [];
  const out: Operation[] = [];
  for (const v of raw) {
    const op = typeof v === "string" ? v.trim().toLowerCase() : v;
    if (isOperation(op) && !out.includes(op)) out.push(op);
  }
  return out;
}

/** Meaning of a conflict id, e.g. "energy:EX1" -> "battery budget exceeded"; an id that is a
 *  complete word of its own ("schema:json" -> "answer cut off or not valid JSON") is looked up
 *  before its prefix. */
export function conflictMeaning(conflict: string): string | null {
  return CONFLICT_PREFIXES[conflict] ?? CONFLICT_PREFIXES[conflictPrefix(conflict)] ?? null;
}

/** "energy:EX1" -> "battery budget exceeded (EX1)", "schema:json" -> "answer cut off or not
 *  valid JSON"; unknown prefixes return the raw id. */
export function conflictLabel(conflict: string): string {
  const whole = CONFLICT_PREFIXES[conflict];
  if (whole && conflict.includes(":")) return whole;
  const prefix = conflictPrefix(conflict);
  const meaning = CONFLICT_PREFIXES[prefix];
  if (!meaning) return conflict;
  const rest = conflict.slice(prefix.length + 1);
  return rest ? `${meaning} (${rest})` : meaning;
}

/** Conflict prefixes a resource change (bigger battery, one more robot, later deadline) can
 *  answer, as the backend's advice stage has them (moonwright.pipeline.relax
 *  RESOURCE_CONFLICTS); a verdict with any other prefix (scope, missing, reference, cycle,
 *  quantity, ...) means the task graph itself is wrong, so no battery or spare fixes it. */
export const RESOURCE_CONFLICT_PREFIXES: ReadonlySet<string> = new Set([
  "energy",
  "energy_task",
  "horizon",
  "horizon_path",
  "robot",
  "zone",
  "precedence",
  "capability",
]);

/** True when a verdict (validator or CP-SAT) names only conflicts a resource change can
 *  answer; false for a coverage verdict and for a broken or missing model answer. */
export function isResourceFailure(failure: Failure | null | undefined): boolean {
  if (!failure || (failure.kind !== "infeasible" && failure.kind !== "validation")) return false;
  return failure.conflicts.length > 0 && failure.conflicts.every((c) => RESOURCE_CONFLICT_PREFIXES.has(conflictPrefix(c)));
}

/** The words that mark a double count (backend rates.DOUBLE_MARK, phase 3i): in the verdict's
 *  volume detail ("... hauler tasks move 240 m3 (200 %), a double count: T8 120 m3, T9 120 m3
 *  (240 m3 is the 40 x 1.5 x 4 m box, twice the triangular section)") and at the end of the
 *  task-graph step's excess advisory ("... hauled twice over, a double count"). */
export const DOUBLE_MARK = ", a double count";

/** True when a validation verdict's volume conflicts are all double counts (digs or hauls that
 *  count a body twice; live, a berm hauled as its box) and its other conflicts, if any, are the
 *  supply conflicts of those hauls: the graph moves too much, so no work is missing. A volume
 *  conflict's detail (failure.details, aligned with failure.conflicts) says which it is. */
export function isDoubleCountFailure(failure: Failure | null | undefined): boolean {
  if (!failure || failure.kind !== "validation") return false;
  const kinds = failure.conflicts.map((c, i) =>
    conflictPrefix(c) === "volume"
      ? (failure.details[i] ?? "").includes(DOUBLE_MARK)
        ? "double"
        : "short"
      : conflictPrefix(c),
  );
  return kinds.includes("double") && kinds.every((k) => k === "double" || k === "supply");
}

/** "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B" -> "Nemotron-3-Super-120B-A12B"; the offline
 *  client reports "fixture:<file>" as its model, which reads as "recorded answer". */
export function shortModel(model: string): string {
  if (/^fixture:/i.test(model)) return "recorded answer";
  const tail = model.split("/").pop() ?? model;
  return tail.replace(/^NVIDIA-/i, "");
}

export interface UsageTotal {
  calls: number;
  input: number;
  output: number;
  latency: number;
  /** The first model seen (the structuring model of a full run). */
  model: string | null;
  /** Every distinct model, in order of first appearance: a live run uses several. */
  models: string[];
}

/** True when steps[i] is a repair step that opens a verdict round: it answers a failed
 *  validate / schedule step. A repair step that follows a rejected repair answer is the same
 *  round asked again (LoopStep.attempt counts every answer; PlanResult.repairs only rounds). */
export function opensRepairRound(steps: LoopStep[], i: number): boolean {
  const s = steps[i];
  if (!s || s.stage !== "repair") return false;
  const prev = i > 0 ? steps[i - 1] : null;
  return prev === null || ((prev.stage === "validate" || prev.stage === "schedule") && !prev.ok);
}

/** Repair rounds in a trace, counted like the backend's PlanResult.repairs: re-asks within a
 *  round do not add one. Pass a prefix of the trace for the round a given step belongs to. */
export function repairRounds(steps: LoopStep[]): number {
  let n = 0;
  for (let i = 0; i < steps.length; i += 1) if (opensRepairRound(steps, i)) n += 1;
  return n;
}

/** Sum of the LLM usage over a trace (steps without usage are solver/validator stages). */
export function totalUsage(steps: LoopStep[]): UsageTotal {
  const t: UsageTotal = { calls: 0, input: 0, output: 0, latency: 0, model: null, models: [] };
  for (const s of steps) {
    if (!s.usage) continue;
    t.calls += 1;
    t.input += s.usage.input_tokens;
    t.output += s.usage.output_tokens;
    t.latency += s.usage.latency_s;
    if (!t.model && s.usage.model) t.model = s.usage.model;
    if (s.usage.model && !t.models.includes(s.usage.model)) t.models.push(s.usage.model);
  }
  return t;
}

/** First non-empty line of a brief without markdown heading markers, capped at 80 chars. */
export function titleFromBrief(text: string, fallback: string): string {
  for (const line of text.split("\n")) {
    const stripped = line.trim().replace(/^#+/, "").trim();
    if (stripped) return stripped.slice(0, 80);
  }
  return fallback;
}

export function truncate(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, Math.max(0, max - 1))}…`;
}

export function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

// ------------------------------------------------------------------ model names and usage
/** Human name of a Token Factory model id: "nvidia/Nemotron-3-Ultra-550b-a55b" ->
 *  "Nemotron 3 Ultra", "nvidia/Nemotron-3_5-Lightning" -> "Nemotron 3.5 Lightning",
 *  "nvidia/nemotron-3-super-120b-a12b" -> "Nemotron 3 Super"; the offline client's
 *  "fixture:<file>" reads "recorded answer"; anything else falls back to shortModel. */
export function modelDisplayName(model: string): string {
  if (!model) return "";
  if (/^fixture:/i.test(model)) return "recorded answer";
  const m = /nemotron[-_ ]?(\d+(?:[._]\d+)?)[-_ ]?(lightning|super|ultra|nano)/i.exec(model);
  if (!m) return shortModel(model);
  const tier = m[2].charAt(0).toUpperCase() + m[2].slice(1).toLowerCase();
  return `Nemotron ${m[1].replace("_", ".")} ${tier}`;
}

export function isFixtureModel(model: string | null | undefined): boolean {
  return !model || /^fixture:/i.test(model);
}

/** Tooltip for a model badge: the full Token Factory id for a live call, a plain sentence for
 *  a recorded answer (the recording's file name never reaches the UI). */
export function modelTitle(model: string | null | undefined): string {
  if (!model) return "no model name recorded";
  if (isFixtureModel(model)) return "Recorded Nemotron answer replayed by the offline demo";
  return model;
}

/** Badge text of one model call: "Nemotron 3 Ultra · 1.2k in · 0.8k out · 12.3 s"; a recorded
 *  answer carries no tokens and reads "recorded answer · <0.01 s". A narrow badge wraps only
 *  between the parts, never inside a figure. */
export function fmtUsage(usage: Usage): string {
  const parts = [modelDisplayName(usage.model) || "model"];
  if (usage.input_tokens + usage.output_tokens > 0) {
    parts.push(`${fmtCompact(usage.input_tokens)}${NBSP}in`, `${fmtCompact(usage.output_tokens)}${NBSP}out`);
  }
  parts.push(fmtSeconds(usage.latency_s));
  return parts.join(" · ");
}

/** "−10.8%", "+5.0%", "0%" (a true minus sign, as BeforeAfter prints deltas). */
export function fmtSignedPct(pct: number | null | undefined, digits = 1): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return "—";
  const r = Number(pct.toFixed(digits));
  if (r === 0) return "0%";
  return `${r > 0 ? "+" : "−"}${Math.abs(r).toFixed(digits)}%`;
}

// ------------------------------------------------------------------ advisories
/** estimate: the rate model overrode the model's hours / Wh of a task; ungrounded: no task
 *  states a quantity; volume (phase 3c): a structure's planned m3 clearly differ from what its
 *  dimensions imply (a re-scope to make visible, amber, label "scope differs from dimensions");
 *  supply (phase 3g): a body's hauls move more m3 than the digs before them remove (the
 *  `supply:<structure>` balance note, often the brief's own arithmetic; the conflict of the
 *  same prefix, `supply:<haul>`, is a haul that waits for no dig or for too little); other:
 *  any sentence the console does not know. */
export type AdvisoryKind = "estimate" | "ungrounded" | "volume" | "supply" | "other";

/** Chip label per advisory kind, in sentence case (notes, not error codes). */
export const ADVISORY_KIND_LABELS: Record<AdvisoryKind, string> = {
  estimate: "estimate",
  ungrounded: "no quantities",
  volume: "scope differs from dimensions",
  supply: "hauled more than dug",
  other: "advisory",
};

export interface Advisory {
  /** `category` without "volume" (a volume line reads "other" here): kept so a label table
   *  typed Record<Advisory["kind"], string> keeps compiling; new code indexes by `category`. */
  kind: "estimate" | "ungrounded" | "other";
  /** the full classification: "volume" tells a scope-vs-dimensions warning apart */
  category: AdvisoryKind;
  /** the task an estimate advisory is about, the structure a volume or supply advisory is
   *  about */
  task: string | null;
  text: string;
  raw: string;
}

/** "estimate:T4: model said 10 h / 1000 Wh, rate model gives 27 h / 3000 Wh (+170%)" ->
 *  {category: "estimate", task: "T4", text: "model said 10 h / 1,000 Wh, ..."}; "ungrounded:
 *  no task states a quantity, ..." -> {category: "ungrounded", task: null, text: "no task
 *  ..."}; a volume line (phase 3e: a shortfall "volume:S2: its dimensions give 360 m3 (100 x 6
 *  x 0.6 m) but its excavation tasks (T3 600 m2 counts 120 m3 at 0.2 m) total only 120 m3
 *  (33 %)", or an excess "... total 600 m3 (200 %): hauled twice over") -> {category: "volume",
 *  kind: "other", task: "S2", text: "its dimensions give 360 m³ (100 × 6 × 0.6 m) but its
 *  excavation tasks (T3 600 m² counts 120 m³ at 0.2 m) total only 120 m³ (33%)"}; a supply
 *  line (phase 3g: "supply:S2: its hauls (T5 150 m3, T6 150 m3) move 300 m3, more than the
 *  125.7 m3 the digs before them remove (S1 landing pad: T3, T4)") -> {category: "supply",
 *  kind: "other", task: "S2", text: "its hauls (T5 150 m³, T6 150 m³) move 300 m³, more than
 *  the 125.7 m³ the digs before them remove (S1 landing pad: T3, T4)"}. */
export function parseAdvisory(raw: string): Advisory {
  const est = /^estimate:([^:\s]+):\s*([\s\S]*)$/.exec(raw);
  if (est) return { kind: "estimate", category: "estimate", task: est[1], text: tidyNumbers(humanizeFields(est[2].trim())), raw };
  const ung = /^ungrounded:\s*([\s\S]*)$/.exec(raw);
  if (ung) return { kind: "ungrounded", category: "ungrounded", task: null, text: humanizeFields(ung[1].trim()), raw };
  const vol = /^volume:([^:\s]+):\s*([\s\S]*)$/.exec(raw);
  if (vol) return { kind: "other", category: "volume", task: vol[1], text: humanizeVolume(vol[2].trim()), raw };
  const sup = /^supply:([^:\s]+):\s*([\s\S]*)$/.exec(raw);
  if (sup) return { kind: "other", category: "supply", task: sup[1], text: humanizeVolume(sup[2].trim()), raw };
  return { kind: "other", category: "other", task: null, text: tidyNumbers(humanizeFields(raw)), raw };
}

/** The volume sentence with the console's units and numbers: "360 m3 (100 x 6 x 0.6 m)" ->
 *  "360 m³ (100 × 6 × 0.6 m)", "bowl 12-20 m across" -> "bowl 12–20 m across", "(33 %)" ->
 *  "(33%)", "T3 600 m2 counts 120 m3 at 0.2 m" -> "T3 600 m² counts 120 m³ at 0.2 m"; task ids
 *  such as T3 and the words ("hauled twice over") are left alone. */
export function humanizeVolume(text: string): string {
  return tidyNumbers(tidyQuantities(humanizeFields(text)));
}

/** Quantities as the console writes them: "360 m3" -> "360 m³", "600 m2" -> "600 m²" (also
 *  "10m3" and "10 m^3", as model prose writes them; the figure never wraps from its unit),
 *  "100 x 6 x 0.6 m" -> "100 × 6 × 0.6 m", "12-20 m" / "68-189 m3" -> "12–20 m" / "68–189 m³",
 *  "(33 %)" -> "(33%)". Ids and words are left alone. */
export function tidyQuantities(text: string): string {
  return text
    .replace(/(\d)\s?m\^?3\b/g, `$1${NBSP}m³`)
    .replace(/(\d)\s?m\^?2\b/g, `$1${NBSP}m²`)
    .replace(/\bm\^?3\b/g, "m³")
    .replace(/\bm\^?2\b/g, "m²")
    .replace(/(\d)\s+x\s+(?=\d)/g, "$1 × ")
    .replace(/(\d)-(?=\d[\d.,]*\s?(?:m³|m²|m\b))/g, "$1–")
    .replace(/(\d) %/g, "$1%");
}

/** A structure's dimension fields as the backend names them in a note ("diameter_m",
 *  "trench_depth_m", "min_width_m"); the first group is the dimension, the second its value. */
const DIMENSION_FIELD =
  /\b((?:min_)?(?:trench_)?(?:diameter|length|width|depth|height|thickness))_m\b(?: (\d+(?:\.\d+)?)(?!\d|\.\d| ?m\b))?/g;

/** Backend sentences name schema fields ("every duration_h and energy_wh", "diameter_m 12 -> 12
 *  m x 12 m"): read them as words ("diameter 12 m -> 12 m x 12 m"). */
export function humanizeFields(text: string): string {
  return text
    .replace(/\bduration_h\b/g, "duration")
    .replace(/\benergy_wh\b/g, "energy")
    .replace(/\bestimated_duration_h\b/g, "estimated duration")
    .replace(/\bestimated_energy_wh\b/g, "estimated energy")
    .replace(/\brobot_type\b/g, "robot type")
    .replace(/\bhorizon_h\b/g, "horizon")
    .replace(/\bbattery_wh\b/g, "battery")
    .replace(DIMENSION_FIELD, (_, field: string, value?: string) =>
      `${field.replace(/_/g, " ")}${value !== undefined ? ` ${value}${NBSP}m` : ""}`,
    );
}

/** Backend step messages print "horizon 336h" / "makespan 72h": space the unit like the rest
 *  of the console ("336 h"). */
export function tidyUnits(text: string): string {
  return text.replace(/(\d)(Wh|h)\b/g, "$1 $2");
}

/** A number as backend text writes it: "9930", "9929.649", and since phase 3d "9,930" or
 *  "1,784.39" (thousands separators). parseNum reads either. */
const NUM = "(\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?|\\d+(?:\\.\\d+)?)";

function parseNum(s: string): number {
  return Number(s.replace(/,/g, ""));
}

/** "5950" -> "5,950", "9929.649" -> "9,929.649", "10,000" -> "10,000": thousands separators
 *  on the whole part, the decimals kept as written (the backend chooses them so that two
 *  figures that differ never print equal: "1,784.39 Wh but has 1,784.38 Wh"). */
export function groupDigits(n: string): string {
  const [whole, frac] = n.replace(/,/g, "").split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return frac !== undefined ? `${grouped}.${frac}` : grouped;
}

const UNIT_NUMBER = new RegExp(`\\b${NUM}\\s?(kWh|Wh|h)\\b`, "g");

/** Numbers that carry a unit get the console's formatting: "5000 Wh" -> "5,000 Wh", "336h" ->
 *  "336 h", while text the backend already formatted stays as it is ("10,000 Wh",
 *  "9,930 Wh", "1,784.39 Wh"); bare counts, token counts and ids are left alone. The space is
 *  the figures' no-break one (fmtHours), so a hint never wraps "64 / h" on a phone. */
export function tidyNumbers(text: string): string {
  return text.replace(UNIT_NUMBER, (_, n: string, unit: string) => `${groupDigits(n)}${NBSP}${unit}`);
}

/** The model's own prose (the explanation, the planner's notes) with the console's figures:
 *  quantities with their symbols ("10 m3" -> "10 m³", "300 m2" -> "300 m²", "100 x 6 m" ->
 *  "100 × 6 m") and every number that carries a unit grouped ("4000 Wh" -> "4,000 Wh"); the
 *  words are the model's. */
export function tidyProse(text: string): string {
  return tidyNumbers(tidyQuantities(text));
}

/** Raw Token Factory model ids inside backend text ("nvidia/Nemotron-3-Ultra-550b-a55b") read
 *  as the model's name ("Nemotron 3 Ultra"), like every model badge on the page. */
export function tidyModels(text: string): string {
  return text.replace(/\b[\w.-]+\/[\w.-]*nemotron[\w.-]*/gi, (id) => modelDisplayName(id));
}

/** The planner roles as the trace names them ("taskgraph (Nemotron 3 Ultra): ..." -> "Task
 *  graph (Nemotron 3 Ultra): ..."). */
const ROLE_WORDS: Record<string, string> = {
  structure: "Structure",
  taskgraph: "Task graph",
  repair: "Repair",
  explain: "Explain",
};

/** "3 robot(s)" -> "3 robots", "1 attempt(s)" -> "1 attempt": the backend's older plurals. */
function resolvePlurals(s: string): string {
  return s.replace(
    /\b(\d+)((?:\s+[a-z][a-z-]*){0,2})\s+(robot|task|zone|structure|repair|conflict|attempt|ask|answer|token)\(s\)/g,
    (_, n: string, mid: string, w: string) => `${n}${mid} ${w}${n === "1" ? "" : "s"}`,
  );
}

/** Machine words inside backend prose read as words: a structure's type id in its label ("S2
 *  (blast_berm)" -> "S2 (blast berm)"), a chain of ids drawn with the console's arrow
 *  ("T1->T2->T7" -> "T1→T2→T7", "72 h -> 79 h" -> "72 h → 79 h"), the Token Factory's request
 *  formats ("json_schema, json_object and plain text were all refused" -> "every request format
 *  (JSON schema, JSON object, plain text) was refused") and the API's finish reasons ("empty
 *  completion (finish_reason=stop)" -> "empty answer", "(finish_reason=length)" -> "(cut off at
 *  the token limit)"). */
function tidyWords(s: string): string {
  return s
    .replace(
      /\b([A-Z][A-Za-z]*\d+[a-z]?) \(([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\)/g,
      (_, id: string, type: string) => `${id} (${type.replace(/_/g, " ")})`,
    )
    .replace(/(\w)->(?=\w)/g, "$1→")
    .replace(/ -> /g, " → ")
    .replace(
      /\bjson_schema, json_object and plain text were all refused\b/g,
      "every request format (JSON schema, JSON object, plain text) was refused",
    )
    .replace(/\s*\(finish_reason=stop\)/g, "")
    .replace(/\(finish_reason=length\)/g, "(cut off at the token limit)")
    .replace(/\(finish_reason=([\w-]+)\)/g, (_, r: string) => `(stopped: ${r.replace(/_/g, " ")})`)
    .replace(/\bempty completion\b/g, "empty answer");
}

/** Backend prose as the console prints it (hints, details, step messages): schema field
 *  names read as words, model ids as model names, a leading role word as its stage label,
 *  "(s)" plurals resolved, machine words read as words (tidyWords), quantities with their
 *  symbols (m³, m², ×) and every number that carries a unit grouped ("5000 Wh" -> "5,000 Wh"),
 *  the words otherwise verbatim. */
export function tidyText(text: string): string {
  const s = tidyWords(resolvePlurals(tidyModels(humanizeFields(text)))).replace(
    /^(structure|taskgraph|repair|explain) \(/,
    (_, role: string) => `${ROLE_WORDS[role] ?? role} (`,
  );
  return tidyNumbers(tidyQuantities(tidyUnits(s)));
}

/** True for the hint of an offline backend that has no recorded answer to give (backend
 *  llm/base.py FixtureClient: "This backend is running offline on recorded Nemotron answers and
 *  has no recording for brief 'x'. ...", "missing fixture explain.json for brief 'x'"). */
export function isOfflineHint(hint: string | null | undefined): boolean {
  return /running offline on recorded|no recording for brief|missing fixture|fixture lookup/i.test(hint ?? "");
}

/** How a model stage ended without a usable answer, one reading for the trace, the receipt and
 *  the console's guidance: "offline" (an offline backend has no recorded answer for the brief,
 *  so nothing was asked), "empty" (the model answered with nothing on every ask: "Nemotron 3
 *  Ultra returned an empty completion on each of 2 asks", or before phase 3e "taskgraph (...):
 *  empty completion (finish_reason=stop) on every one of 2 attempt(s)"), "request" (the Token
 *  Factory request itself failed, "The Token Factory request to Nemotron 3 Ultra failed:
 *  APITimeoutError (...)", or any other failed call), "rejected" (an answer came back but was
 *  cut off, not JSON or off the schema: kind schema). Null for a verdict, a solver time-out or
 *  no failure. */
export type AnswerFailure = "offline" | "empty" | "request" | "rejected";

export function answerFailure(failure: Failure | null | undefined): AnswerFailure | null {
  if (!failure) return null;
  if (failure.kind === "schema") return "rejected";
  if (failure.kind !== "llm_error") return null;
  if (isOfflineHint(failure.hint)) return "offline";
  if (/\bempty (?:completion|answer)\b/i.test(failure.hint)) return "empty";
  return "request";
}

/** An offline backend's miss in the console's words: its sentences name the lookup key and a
 *  server setting ("... has no recording for brief 'front_loaded'. Pick one of the example
 *  briefs, or start the backend with NEBIUS_API_KEY set to plan new briefs live." -> "... has
 *  no recording for this brief. Pick one of the example briefs, or connect a live backend to
 *  plan new briefs.") and a recording's file name ("missing fixture explain.json for brief
 *  'x'" -> "no recorded answer for this step of the brief"). Other text passes through. */
export function tidyOffline(text: string): string {
  return text
    .replace(/\bThis backend is running offline on recorded Nemotron answers\b/g, "This backend replays recorded Nemotron answers")
    .replace(/\bstart the backend with [A-Z][A-Z0-9_]* set to plan new briefs live\b/g, "connect a live backend to plan new briefs")
    .replace(/\bmissing fixture [\w.-]+ for brief '[^']*'/g, "no recorded answer for this step of the brief")
    .replace(/\bfixture lookup for role=\w+ needs a brief key\b/g, "recorded answers are looked up by brief, and this request named none")
    .replace(/\bfor brief '[^']*'/g, "for this brief");
}

/** A failure detail line as the console prints it: the backend's file-system clauses dropped
 *  ("(looked in /…/x)"), endpoint URLs, absolute paths and recording file names removed, its
 *  recording vocabulary read as words ("no fixture directory for brief 'x'" -> "no recording
 *  for brief 'x'"), an older rejected answer's machine wording read as words ("json: the
 *  answer was cut off at max_tokens=16384 (...); answer with compact JSON only, no reasoning"
 *  -> "the answer was cut off at 16,384 tokens (...)", ": Input should be ..." -> "Input should
 *  be ..."), then tidyText (model ids as names, units and numbers formatted). */
export function tidyDetail(text: string): string {
  const s = tidyOffline(text)
    .replace(/\s*\(looked in [^)]*\)/g, "")
    .replace(/\s+on https?:\/\/\S+?(?=[,;]|:(?:\s|$)|\s|$)/g, "")
    .replace(/(^|[\s(])\/(?:[^\s/)]+\/)+[^\s)]*/g, "$1…")
    .replace(/\bfixture directory\b/gi, "recording")
    .replace(/\bfixture file\b/gi, "recording")
    .replace(/\bfixtures?\b/gi, "recording")
    .replace(/\s*'?\b[\w.-]+\.json\b'?/g, "")
    .replace(/^json:\s*/i, "")
    .replace(/^:\s*/, "")
    .replace(/\bmax_tokens=(\d+)/g, (_, n: string) => `${groupDigits(n)} tokens`)
    .replace(/;\s*answer with compact JSON only, no reasoning\.?$/i, "");
  return tidyText(s).replace(/\s{2,}/g, " ").trim();
}

/** A step message as the trace prints it: "makespan 68h (optimal)" -> "makespan 68 h
 *  (optimal)", a replan's "72 h -> 79 h" -> "72 h → 79 h"; an offline backend's miss
 *  ("offline: missing fixture taskgraph_3.json for brief 'x'", "offline: This backend is
 *  running offline ...") reads as one short clause, since the step's failure carries the rest
 *  and recording file names never reach the page. */
export function tidyMessage(message: string | null | undefined): string {
  if (!message) return "";
  const m = message.trim();
  if (/^offline:/i.test(m)) {
    return /reused prior graph/i.test(m)
      ? "offline: kept the remaining graph, no recorded answer"
      : "offline: no recorded answer";
  }
  return tidyText(m).replace(/ -> /g, " → ");
}

/** A rejected answer's per-ask detail line (phase 3e, backend tokenfactory AskOutcome.line):
 *  "ask 1: cut off at 32,768 tokens" -> {ask: "ask 1", text: "cut off at 32,768 tokens"},
 *  "ask 3 (reasoning off): task T3: 'after' must be a list" -> {ask: "ask 3 (reasoning off)",
 *  text: "task T3: 'after' must be a list"}; any other line -> {ask: null, text: tidied}. */
export function parseAskLine(line: string): { ask: string | null; text: string } {
  const m = /^(ask \d+(?: \(reasoning off\))?):\s*([\s\S]*)$/.exec(line.trim());
  return m ? { ask: m[1], text: tidyDetail(m[2]) } : { ask: null, text: tidyDetail(line) };
}

/** "a", "a and b", "a, b and c". */
export function joinAnd(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** Solver sentence for a robot type whose work fits the pool but no single battery, as the
 *  backend wrote it before phase 3d (one copy per robot of the type); kept so recordings older
 *  than phase 3d still merge into one sentence. */
const POOL_SENTENCE =
  /^(\S+) has (\d+(?:\.\d+)?) Wh, and the (\S+) tasks \((\d+(?:\.\d+)?) Wh in total(?: \(incl\. ~(\d+(?:\.\d+)?) Wh of travel\))?\) cannot be split across the (\d+) \S+ robot\(s\) \((\d+(?:\.\d+)?) Wh combined\) without exceeding its budget\.?$/;

interface PoolHint {
  robots: { id: string; budget: number }[];
  total: number;
  travel: number | null;
  count: number;
  pool: number;
}

/** The merged pool sentence, worded like the backend's own of phases 3d to 3i ("EX1 and EX2
 *  each have 5,000 Wh; the excavator tasks need 9,930 Wh in total, about 30 Wh of it travel,
 *  and cannot be split across 2 excavators (10,000 Wh combined) without exceeding one
 *  budget."), so an older recording reads like a newer one. Its travel share is the per-task
 *  transit allowance those recordings were planned with, hence "about"; since phase 3k the
 *  backend quotes the least travel any order needs and says "at least". */
function renderPool(type: string, p: PoolHint): string {
  const ids = p.robots.map((r) => r.id);
  const sameBudget = p.robots.every((r) => r.budget === p.robots[0].budget);
  const who =
    p.robots.length === 1
      ? `${ids[0]} has ${fmtWh(p.robots[0].budget)}`
      : sameBudget
        ? `${joinAnd(ids)} each have ${fmtWh(p.robots[0].budget)}`
        : joinAnd(p.robots.map((r) => `${r.id} has ${fmtWh(r.budget)}`));
  const travel = p.travel !== null && Math.round(p.travel) >= 1 ? `, about ${fmtWh(p.travel)} of it travel,` : "";
  return `${who}; the ${type} tasks need ${fmtWh(p.total)} in total${travel} and cannot be split across ${plural(p.count, type)} (${fmtWh(p.pool)} combined) without exceeding one budget.`;
}

/** One sentence of a hint, tidied (tidyText), with an older recording's nested
 *  "(… (incl. …))" flattened. */
function tidySentence(s: string): string {
  return tidyText(s.replace(/\(([^()]*) \(incl\. ([^()]*)\)\)/g, "($1, incl. $2)"));
}

/**
 * A backend hint as the console prints it: numbers formatted like every other figure on the
 * page (text the backend already formatted, "EX1 and EX2 each have 5,000 Wh; the excavator
 * tasks need at least 9,915 Wh in total, 15 Wh of it travel, ...", is kept as written), model
 * ids as names, m³ / m² / ×, duplicate sentences dropped, an offline backend's miss without
 * its lookup key or server setting (tidyOffline). Recordings made before phase 3d
 * also get "robot(s)" resolved and the solver's per-robot copies of the energy-pool sentence
 * merged into one. Unknown text passes through tidied, never reworded.
 */
export function tidyHint(text: string | null | undefined): string {
  if (!text) return "";
  const sentences = tidyOffline(text.trim()).split(/(?<=[.!?])\s+(?=[A-Z("])/);
  const pools = new Map<string, PoolHint>();
  const out: string[] = [];
  const seen = new Set<string>();
  const push = (s: string) => {
    if (!s || seen.has(s)) return;
    seen.add(s);
    out.push(s);
  };
  for (const raw of sentences) {
    const s = raw.trim();
    const m = POOL_SENTENCE.exec(s);
    if (m) {
      const type = m[3];
      let p = pools.get(type);
      if (!p) {
        p = { robots: [], total: +m[4], travel: m[5] ? +m[5] : null, count: +m[6], pool: +m[7] };
        pools.set(type, p);
        push(`\u0000${type}`);
      }
      if (!p.robots.some((r) => r.id === m[1])) p.robots.push({ id: m[1], budget: +m[2] });
      continue;
    }
    push(tidySentence(s));
  }
  return out
    .map((s) => (s.startsWith("\u0000") ? renderPool(s.slice(1), pools.get(s.slice(1)) as PoolHint) : s))
    .join(" ");
}

/** A hint longer than this many characters opens folded (hintLead). */
const HINT_FOLD_CHARS = 480;
/** The folded hint's opening: whole sentences up to about this many characters, at least one. */
const HINT_LEAD_CHARS = 260;

/** A tidied hint as the console first shows it. Most hints are a sentence or three and show
 *  whole (`rest` is ""); a long one, such as a large core's one sentence per precedence edge,
 *  opens on its first sentences (the verdict and its bounds) with the rest folded behind a
 *  toggle, every sentence still there. `folded` counts the sentences in `rest`. */
export function hintLead(hint: string): { lead: string; rest: string; folded: number } {
  const text = hint.trim();
  if (text.length <= HINT_FOLD_CHARS) return { lead: text, rest: "", folded: 0 };
  const sentences = text.split(/(?<=[.!?])\s+(?=[A-Z("])/);
  let n = 1;
  while (n < sentences.length && sentences.slice(0, n + 1).join(" ").length <= HINT_LEAD_CHARS) n += 1;
  if (n >= sentences.length) return { lead: text, rest: "", folded: 0 };
  return { lead: sentences.slice(0, n).join(" "), rest: sentences.slice(n).join(" "), folded: sentences.length - n };
}

// ------------------------------------------------------------------ conflict groups
/** One entry of a verdict's conflict list as the console shows it: a conflict id, or the
 *  precedence edges of a core joined into the chain they form ("precedence:T3->T4" and
 *  "precedence:T4->T9" read "precedence:T3->T4->T9"). `ids` keeps every conflict the entry
 *  stands for, in the verdict's order. */
export interface ConflictGroup {
  id: string;
  ids: string[];
}

/** "precedence:T3->T4" -> ["T3", "T4"]; null for any other id. */
function precedenceEdge(conflict: string): [string, string] | null {
  if (conflictPrefix(conflict) !== "precedence") return null;
  const ends = conflict.slice("precedence:".length).split("->");
  return ends.length === 2 && ends[0] && ends[1] ? [ends[0], ends[1]] : null;
}

/** A verdict's conflicts with its precedence edges joined into chains, so a CP-SAT core that
 *  names ten edges reads as the three chains they form (T1->T5->T6->T7->T8, T2->T5,
 *  T3->T4->T9->T10->T13->T14). Each chain starts at an edge no unused edge leads into and
 *  follows the verdict's order where it forks; the chains stand where the first edge stood,
 *  every other conflict where it was. A verdict with one edge or none comes back one id per
 *  entry. */
export function conflictGroups(conflicts: string[]): ConflictGroup[] {
  const edges = conflicts.flatMap((c) => {
    const e = precedenceEdge(c);
    return e ? [{ id: c, from: e[0], to: e[1] }] : [];
  });
  if (edges.length < 2) return conflicts.map((c) => ({ id: c, ids: [c] }));
  const used = new Set<number>();
  const chains: ConflictGroup[] = [];
  while (used.size < edges.length) {
    const open = (j: number) => !used.has(j);
    let i = edges.findIndex((e, j) => open(j) && !edges.some((f, k) => open(k) && k !== j && f.to === e.from));
    if (i < 0) i = edges.findIndex((_, j) => open(j)); // a cycle: start anywhere
    const chain = [i];
    used.add(i);
    for (;;) {
      const last = edges[chain[chain.length - 1]];
      const next = edges.findIndex((e, j) => open(j) && e.from === last.to);
      if (next < 0) break;
      chain.push(next);
      used.add(next);
    }
    const nodes = [edges[chain[0]].from, ...chain.map((j) => edges[j].to)];
    const ids = chain.map((j) => edges[j].id);
    chains.push({ id: ids.length === 1 ? ids[0] : `precedence:${nodes.join("->")}`, ids });
  }
  const out: ConflictGroup[] = [];
  for (const c of conflicts) {
    if (!precedenceEdge(c)) out.push({ id: c, ids: [c] });
    else if (c === edges[0].id) out.push(...chains);
  }
  return out;
}

/** A verdict's conflicts for a one-line outcome ("rejected horizon, 10 precedence edges,
 *  robot:SV1, zone:Z2"): ids with the console's arrow, the precedence edges counted in one
 *  phrase where there are several. */
export function conflictPhrases(conflicts: string[]): string[] {
  const edges = conflicts.filter((c) => precedenceEdge(c) !== null);
  if (edges.length < 2) return conflicts.map(fmtConflictId);
  return conflicts.flatMap((c) =>
    precedenceEdge(c) === null ? [fmtConflictId(c)] : c === edges[0] ? [plural(edges.length, "precedence edge")] : [],
  );
}

// ------------------------------------------------------------------ structure warnings
export interface StructureWarning {
  /** the structure the warning names ("structure S2 ..."), null for a site-wide one */
  structure: string | null;
  /** "warn" (amber): the user should act before planning (operations recovered from malformed
   *  keys: check them against the brief; a structure without dimensions: add the brief's
   *  numbers); "muted": an operation the fleet cannot staff or the brief does not ask for,
   *  overlapping zones, an undeclared or missing zone, no depot */
  tone: "warn" | "muted";
  /** the sentence with field names read as words and units spaced */
  text: string;
  raw: string;
}

/** One StructurePayload.warnings line as the console shows it. */
export function parseStructureWarning(raw: string): StructureWarning {
  const m = /^structure (\S+?):?(?:\s|$)/.exec(raw);
  const tone = /recovered from malformed keys|has no dimensions/.test(raw) ? "warn" : "muted";
  return { structure: m ? m[1] : null, tone, text: tidyUnits(humanizeFields(raw.trim())), raw };
}

// ------------------------------------------------------------------ advice (suggestions)
/** Well-formed suggestions of an untyped list (anything else dropped, order kept). */
function suggestionList(v: unknown): Suggestion[] {
  return Array.isArray(v) ? v.filter(isSuggestion) : [];
}

/** The advice a step carries (SuggestionsPayload): non-empty only on the final failed step of
 *  a failed run or replan, [] for every other step and for a payload recorded before the
 *  field existed. */
export function stepSuggestions(step: LoopStep): Suggestion[] {
  return suggestionList(step.payload?.suggestions);
}

/** Time base of a step's advice: "absolute" for a replan step that says so
 *  (suggestions_time_base) and for a main-plan step (planner hours start at simulator 0, so
 *  there is no offset to add); "relative" only for a replan step whose advice was recorded
 *  without the flag. */
export function suggestionsTimeBase(step: LoopStep): "absolute" | "relative" {
  const p = step.payload;
  if (!p) return "absolute";
  if (p.suggestions_time_base === "absolute") return "absolute";
  return p.time_base === "relative" || p.time_base === "absolute" ? "relative" : "absolute";
}

/** The last step with ok=false: on a failed run the verdict no repair could answer (its
 *  payload carries the advice); null when every step passed. */
export function lastFailedStep(steps: LoopStep[]): LoopStep | null {
  for (let i = steps.length - 1; i >= 0; i -= 1) if (!steps[i].ok) return steps[i];
  return null;
}

/** The same verdict (kind, conflicts, hint): a step's failure and the PlanResult's. */
export function sameFailure(a: Failure, b: Failure): boolean {
  return (
    a.kind === b.kind &&
    a.hint === b.hint &&
    a.conflicts.length === b.conflicts.length &&
    a.conflicts.every((c, i) => c === b.conflicts[i])
  );
}

/** The last step whose failure is `failure` (the step that produced a PlanResult's verdict:
 *  its payload carries the budget rows and the advice), or null. A run that ends in a
 *  rejected repair answer reports the verdict that round answered, which is an earlier step
 *  than lastFailedStep. */
export function failureStep(steps: readonly LoopStep[], failure: Failure | null | undefined): LoopStep | null {
  if (!failure) return null;
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const f = steps[i].failure;
    if (f && sameFailure(f, failure)) return steps[i];
  }
  return null;
}

/** Index of the step whose answer is the plan's task graph (PlanResult.task_graph): the last
 *  ok taskgraph / repair step, since a rejected repair answer produces no graph; -1 when there
 *  is none. Only that step's repair diff may be read against the final graph. */
export function lastGraphStep(steps: LoopStep[]): number {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const s = steps[i];
    if ((s.stage === "taskgraph" || s.stage === "repair") && s.ok) return i;
  }
  return -1;
}

/** The advice of a plan: PlanResult.suggestions, else (a recording without the field, or a
 *  run still streaming) the final failed step's payload, else []; always [] for a verified
 *  plan. The same call works on ReplanRecord.after. */
export function planSuggestions(plan: Pick<PlanResult, "success" | "steps"> & { suggestions?: unknown }): Suggestion[] {
  if (plan.success) return [];
  const own = suggestionList(plan.suggestions);
  if (own.length > 0) return own;
  const failed = lastFailedStep(plan.steps);
  return failed ? stepSuggestions(failed) : [];
}

/** What a recorded run shows, read from its PlanResult so a card or a tooltip can never
 *  disagree with the file. */
export interface RunOutcome {
  /** conflict ids of the first verdict (validate or schedule) that rejected a graph; [] when
   *  none did */
  rejected: string[];
  /** repair rounds, as PlanResult.repairs counts them */
  repairs: number;
  /** "72 h of 336 h" for a verified plan, else null */
  verified: string | null;
  /** the top solver-proven suggestion of a plan that was not verified, as the console prints
   *  it ("finishing needs 85 h, 25 h past the 60 h deadline (CP-SAT optimal)"), else null */
  advice: string | null;
}

export function runOutcome(plan: PlanResult): RunOutcome {
  const first = plan.steps.find((s) => !s.ok && (s.stage === "validate" || s.stage === "schedule"));
  const verified =
    plan.success && plan.schedule
      ? `${fmtHours(plan.schedule.makespan_h, 0)} of ${fmtHours(plan.requirements.horizon_h, 0)}`
      : null;
  const top = verified === null ? planSuggestions(plan)[0] : undefined;
  return {
    rejected: first?.failure?.conflicts ?? [],
    repairs: plan.repairs,
    verified,
    advice: top ? fmtSuggestion(top) : null,
  };
}

/** A conflict id with the console's arrow: "horizon_path:T1->T2" -> "horizon_path:T1→T2". */
export function fmtConflictId(conflict: string): string {
  return conflict.replace(/->/g, "→");
}

/** The outcome in one line, for a tooltip: "rejected energy:printer → 2 repair rounds →
 *  verified, 72 h of 336 h" (several precedence edges counted: conflictPhrases). */
export function fmtRunOutcome(o: RunOutcome): string {
  return [
    o.rejected.length > 0 ? `rejected ${conflictPhrases(o.rejected).join(", ")}` : "first graph accepted",
    plural(o.repairs, "repair round"),
    o.verified ? `verified, ${o.verified}` : `not verified${o.advice ? `: ${o.advice}` : ""}`,
  ].join(" → ");
}

/** "extend_horizon" -> "later deadline"; an unknown kind prints as words. */
export function suggestionKindLabel(kind: SuggestionKind | string): string {
  return SUGGESTION_KIND_LABELS[kind as SuggestionKind] ?? String(kind).replace(/_/g, " ");
}

/** The detail sentence as the console prints it: the words verbatim, every number that carries
 *  a unit in the console's format ("5950 Wh" -> "5,950 Wh", "85h" -> "85 h") so the row reads
 *  like the apply button and the hint next to it; ids and bare counts are left alone. */
export function fmtSuggestion(s: Suggestion): string {
  return tidyNumbers(tidyUnits(s.detail.trim()));
}

/** Chip text of a suggestion: "CP-SAT verified" or "not solver-verified" (reduce_scope). */
export function suggestionProofLabel(s: Suggestion): string {
  return s.verified ? SUGGESTION_COPY.verified : SUGGESTION_COPY.unverified;
}

/** Integer percentages that add up to 100 (largest remainder), for shares shown side by side
 *  where independent rounding would print 59 + 37 + 5 = 101. Zero counts stay 0. */
export function roundShares(counts: number[], total: number): number[] {
  if (!(total > 0)) return counts.map(() => 0);
  const raw = counts.map((c) => (100 * c) / total);
  const out = raw.map(Math.floor);
  let rest = 100 - out.reduce((a, b) => a + b, 0);
  const order = raw
    .map((r, i) => [r - out[i], i] as const)
    .sort((a, b) => b[0] - a[0] || a[1] - b[1]);
  for (const [, i] of order) {
    if (rest <= 0) break;
    if (counts[i] > 0) {
      out[i] += 1;
      rest -= 1;
    }
  }
  return out;
}

// ------------------------------------------------------------------ failure numbers
/** One row of the subject / budget / demanded table behind a failure, as the console shows
 *  it. (The backend's own rows are types.ts BudgetRow, read from a failed schedule step's
 *  payload; budgetPayloadRows turns them into these.) */
export interface BudgetTableRow {
  /** robot id, robot type, task id, zone or a precedence chain */
  subject: string;
  kind: "robot" | "type" | "task" | "chain" | "zone";
  budget: number;
  demanded: number;
  unit: "Wh" | "h";
  note: string | null;
  /** the row is the conflict itself (a failed schedule step's own energy rows, which bind even
   *  at 99 %: no split fits); absent: binding when demanded exceeds budget */
  binding?: boolean;
  /** why a row binds although its figure is within the budget ("no split fits one battery"),
   *  printed next to that figure */
  why?: string | null;
}

/** "incl. 30 Wh travel" for a travel share worth a Wh, else null. */
function travelNote(wh: number | null | undefined): string | null {
  return typeof wh === "number" && Number.isFinite(wh) && wh >= 0.5 ? `incl. ${fmtNum(wh, 0)} Wh travel` : null;
}

function withNote(note: string, extra: string | null): string {
  return extra ? `${note} · ${extra}` : note;
}

/** The table rows of a failed schedule step's own budget rows (payload.budget, phase 3d):
 *  the exact figures the hint quotes rounded, the pool or the robot in the note with the
 *  travel share ("EX1 and EX2 combined, no split fits one battery · incl. 30 Wh travel",
 *  "tasks only it can run · incl. 41 Wh travel"; a row does not say which phase wrote it, and
 *  its travel was an allowance before phase 3k, the least any order needs since). A robot
 *  alone in its type has no split to blame: within its battery its row binds because of the
 *  order it is held to (kind "order", phase 3k), never "no split fits one battery". */
export function budgetPayloadRows(rows: readonly BudgetRow[]): BudgetTableRow[] {
  return rows.map((r) => {
    let note: string;
    let why: string | null = null;
    if (r.kind === "type" && r.robots.length !== 1) {
      note = r.robots.length > 1 ? `${joinAnd(r.robots)} combined` : "the type's robots";
      // within the pool, the row binds because no split of the work fits one battery each;
      // above it, the work exceeds even every battery of the type together
      if (r.demanded_wh <= r.budget_wh) why = "no split fits one battery";
    } else if (r.kind === "type" || r.kind === "order") {
      // one robot: its tasks in their cheapest order, which fits; the order it must keep does not
      note = r.kind === "type" ? r.robots[0] : "its tasks in the cheapest order";
      if (r.demanded_wh <= r.budget_wh) why = "the order it must keep costs more";
    } else {
      note = "tasks only it can run";
    }
    return {
      subject: r.subject,
      kind: r.kind === "order" ? "robot" : r.kind,
      budget: r.budget_wh,
      demanded: r.demanded_wh,
      unit: "Wh" as const,
      note: withNote(note, travelNote(r.travel_wh)),
      binding: true,
      why,
    };
  });
}

/**
 * The numbers behind a Failure as rows of a subject / budget / demanded table.
 *
 * A failed schedule step's own rows (`budget`: its payload.budget, see types.ts stepBudget)
 * come first and are exact; then the validator's detail lines (one per conflict) and the
 * hint sentences, for what the payload does not carry: the deadline, robot time, zones, the
 * validator's energy checks, and the solver's energy sentences of a recording made before the
 * payload had rows. Rows the sentences only imply against the deadline need `horizonH`. Every
 * pattern reads numbers with or without thousands separators ("9,930" and "9929.649") and the
 * sentence shapes of every backend phase (backend schedule.py `_energy_sentence`,
 * `_horizon_hint`, `_robot_sentence`, `_zone_hint`; validate.py `_check_*`). Unparseable text
 * yields no row, never a wrong one.
 */
export function budgetRows(
  failure: Failure,
  horizonH?: number | null,
  budget?: readonly BudgetRow[] | null,
): BudgetTableRow[] {
  // rows in reading order: the payload's first, then by where their sentence stands
  const rows: { row: BudgetTableRow; at: number }[] = [];
  const seen = new Set<string>();
  let at = -1;
  let base = 0;
  const add = (row: BudgetTableRow) => {
    const key = `${row.kind}|${row.subject}|${row.unit}`;
    if (seen.has(key) || !Number.isFinite(row.budget) || !Number.isFinite(row.demanded)) return;
    seen.add(key);
    rows.push({ row, at });
  };
  const each = (text: string, re: RegExp, fn: (m: RegExpExecArray) => void) => {
    const g = new RegExp(re.source, re.flags.includes("g") ? re.flags : `${re.flags}g`);
    for (let m = g.exec(text); m; m = g.exec(text)) {
      at = base + m.index;
      fn(m);
    }
  };
  const robots = (n: string) => `${n} ${n === "1" ? "robot" : "robots"}`;
  const fleet = (n: string) => (n === "1" ? "1 robot" : `${n} robots combined`);
  const arrows = (chain: string) => chain.replace(/->/g, "→");
  const travel = (s: string | undefined) => (s === undefined ? null : travelNote(parseNum(s)));

  const own = budget ?? [];
  for (const row of budgetPayloadRows(own)) add(row);
  // the payload is the energy core's own account: the solver's energy sentences would only
  // repeat it rounded
  const solverEnergy = own.length === 0;
  // a recording older than phase 3d writes the solver's energy-pool sentence once per robot of
  // the type; the numbers it carries are the type's, so they make one row per type
  const pools = new Map<string, { robots: { id: string; budget: number }[]; demanded: number; travel: string | null; pool: number; at: number }>();

  const texts = [...failure.details, failure.hint].filter(Boolean);
  for (const [ti, text] of texts.entries()) {
    base = (ti + 1) * 1e6;
    // validator, per robot type: "excavator tasks need 9900 Wh; its 2 robots carry 10000 Wh";
    // phase 3e: "printer tasks need 4,037 Wh, about 38 Wh of it travel; its 1 robot carries
    // 4,000 Wh"; phase 3k (the least travel any order needs): "printer tasks need at least
    // 4,001 Wh, 11 Wh of it travel; its 1 robot carries 4,000 Wh"
    each(text, new RegExp(`(\\S+) tasks need (?:at least )?${NUM} Wh(?:, (?:about )?${NUM} Wh of it travel)?; its (\\d+) robots? (?:carry|carries) ${NUM} Wh`), (m) =>
      add({ subject: m[1], kind: "type", demanded: parseNum(m[2]), budget: parseNum(m[5]), unit: "Wh", note: withNote(fleet(m[4]), travel(m[3])) }),
    );
    // validator hint: "excavator tasks need 9900 Wh but its 2 robots carry only 10000 Wh
    // combined"; phase 3e: "printer tasks need 4,037 Wh, 3,999 Wh of work and about 38 Wh of
    // travel, but its 1 robot carries only 4,000 Wh combined"; phase 3k: "printer tasks need
    // at least 4,001 Wh, 3,990 Wh of work and 11 Wh of travel, but ..."
    each(text, new RegExp(`(\\S+) tasks need (?:at least )?${NUM} Wh(?:, ${NUM} Wh of work and (?:about )?${NUM} Wh of travel,)? but its (\\d+) robots? (?:carry|carries) only ${NUM} Wh combined`), (m) =>
      add({ subject: m[1], kind: "type", demanded: parseNum(m[2]), budget: parseNum(m[6]), unit: "Wh", note: withNote(fleet(m[5]), travel(m[4])) }),
    );
    // validator: "T4 needs 5200 Wh but the largest excavator battery is 5000 Wh"; phase 3e:
    // "T1 needs 4,200 Wh, about 38 Wh of it travel, but the largest excavator battery is 4,000 Wh";
    // phase 3k: "T1 needs at least 4,006 Wh, 11 Wh of it travel, but the largest ..."
    each(text, new RegExp(`(\\S+) needs (?:at least )?${NUM} Wh(?:, (?:about )?${NUM} Wh of it travel,)? but the largest (\\S+) battery is ${NUM} Wh`), (m) =>
      add({ subject: m[1], kind: "task", demanded: parseNum(m[2]), budget: parseNum(m[5]), unit: "Wh", note: withNote(`largest ${m[4]} battery`, travel(m[3])) }),
    );
    // validator hint: "T4 (5200 Wh, largest excavator battery 5000 Wh)"
    each(text, new RegExp(`(\\S+) \\(${NUM} Wh, largest (\\S+) battery ${NUM} Wh\\)`), (m) =>
      add({ subject: m[1], kind: "task", demanded: parseNum(m[2]), budget: parseNum(m[4]), unit: "Wh", note: `largest ${m[3]} battery` }),
    );
    // validator hint, phase 3e: "T1 (4,162 Wh of work plus about 38 Wh of travel, 4,200 Wh
    // against the largest excavator battery of 4,000 Wh)"; phase 3k: "... plus at least 11 Wh
    // of travel, ..."
    each(text, new RegExp(`(\\S+) \\((?:${NUM} Wh of work plus (?:about|at least) ${NUM} Wh of travel, )?${NUM} Wh against the largest (\\S+) battery of ${NUM} Wh\\)`), (m) =>
      add({ subject: m[1], kind: "task", demanded: parseNum(m[4]), budget: parseNum(m[6]), unit: "Wh", note: withNote(`largest ${m[5]} battery`, travel(m[3])) }),
    );
    if (solverEnergy) {
      // solver, the tasks only one robot can run: "EX1 would need 5200 Wh (incl. ~30 Wh of
      // travel) but has 5000 Wh." (3c); "PR1 would need 4,031 Wh, about 41 Wh of it travel,
      // but has 4,000 Wh." (3d); "PR1 would need at least 4,029 Wh, 39 Wh of it travel, but
      // has 4,000 Wh." (3k: the least travel any order needs)
      each(text, new RegExp(`(\\S+) would need (?:at least )?${NUM} Wh(?: \\(incl\\. ~${NUM} Wh of travel\\)|, (?:about )?${NUM} Wh of it travel,)? but has ${NUM} Wh`), (m) =>
        add({ subject: m[1], kind: "robot", demanded: parseNum(m[2]), budget: parseNum(m[5]), unit: "Wh", note: withNote("tasks only it can run", travel(m[3] ?? m[4])) }),
      );
      // solver, a type's pool (3d): "EX1 and EX2 each have 5,000 Wh; the excavator tasks need
      // 9,930 Wh in total, about 30 Wh of it travel, and cannot be split across 2 excavators
      // (10,000 Wh combined) without exceeding one budget." / "... need 362 Wh in total, about
      // 62 Wh of it travel, more than the 250 Wh the 2 excavators have combined."; 3k: "...
      // need at least 9,915 Wh in total, 15 Wh of it travel, and cannot be split ..."
      each(text, new RegExp(`the (\\S+) tasks need (?:at least )?${NUM} Wh in total(?:, (?:about )?${NUM} Wh of it travel)?(?:,? and cannot be split across (\\d+) \\S+ \\(${NUM} Wh combined\\)|, more than the ${NUM} Wh the (\\d+) \\S+ have combined)`), (m) => {
        const split = m[4] !== undefined;
        const count = split ? m[4] : m[7];
        add({
          subject: m[1],
          kind: "type",
          demanded: parseNum(m[2]),
          budget: parseNum(split ? m[5] : m[6]),
          unit: "Wh",
          note: withNote(fleet(count), travel(m[3])),
          // "cannot be split ... without exceeding one budget": the row is the conflict itself
          // (one robot has no split to blame: the order it must keep costs more, phase 3k)
          ...(split ? { binding: true, why: count === "1" ? "the order it must keep costs more" : "no split fits one battery" } : {}),
        });
      });
      // solver, pre-3d pool sentence: "EX1 has 5000 Wh, and the excavator tasks (9929.649 Wh in
      // total (incl. ~30 Wh of travel)) cannot be split across the 2 excavator robot(s)
      // (10000 Wh combined) ..."
      each(
        text,
        new RegExp(
          `(\\S+) has ${NUM} Wh, and the (\\S+) tasks \\(${NUM} Wh in total(?: \\(incl\\. ~${NUM} Wh of travel\\))?\\) cannot be split across the (\\d+) \\S+ robot\\(s\\) \\(${NUM} Wh combined\\)`,
        ),
        (m) => {
          const p = pools.get(m[3]) ?? { robots: [], demanded: parseNum(m[4]), travel: m[5] ?? null, pool: parseNum(m[7]), at };
          if (!p.robots.some((r) => r.id === m[1])) p.robots.push({ id: m[1], budget: parseNum(m[2]) });
          pools.set(m[3], p);
        },
      );
    }
    // validator: "excavator: 120 h over 2 robots needs >= 60 h; horizon is 48 h"
    each(text, new RegExp(`(\\S+): ${NUM} h over (\\d+) robots? needs >= ${NUM} h; horizon is ${NUM} h`), (m) =>
      add({ subject: m[1], kind: "type", demanded: parseNum(m[4]), budget: parseNum(m[5]), unit: "h", note: `${groupDigits(m[2])} h over ${robots(m[3])}` }),
    );
    // validator: "T1->T2->T5 takes 200 h in sequence; horizon is 168 h" (subject "T1→T2→T5",
    // drawn with the arrow the hint above it prints)
    each(text, new RegExp(`(\\S+) takes ${NUM} h in sequence; horizon is ${NUM} h`), (m) =>
      add({ subject: arrows(m[1]), kind: "chain", demanded: parseNum(m[2]), budget: parseNum(m[3]), unit: "h", note: "critical path" }),
    );
    // validator hint: "The critical path T1->T2 alone takes 200 h, longer than the 168 h horizon"
    each(text, new RegExp(`The critical path (\\S+) alone takes ${NUM} h, longer than the ${NUM} h horizon`), (m) =>
      add({ subject: arrows(m[1]), kind: "chain", demanded: parseNum(m[2]), budget: parseNum(m[3]), unit: "h", note: "critical path" }),
    );
    // solver horizon hint: "The plan cannot finish within the 60 h horizon: the longest
    // precedence chain alone needs 85 h, 25 h past the 60 h deadline; the busiest robot type
    // (hauler) needs at least 24 h." (3d; "takes 61 h (transit to its first zone included) and"
    // before)
    const horizon = new RegExp(`within the ${NUM} h horizon`).exec(text);
    const deadline = horizon ? parseNum(horizon[1]) : (horizonH ?? null);
    if (deadline !== null) {
      each(text, new RegExp(`the longest (?:precedence chain alone|chain of work, locked starts included,) (?:takes|needs) ${NUM} h( \\(transit to its first zone included\\)| including the transit to its first zone)?`), (m) =>
        add({
          subject: "longest chain",
          kind: "chain",
          demanded: parseNum(m[1]),
          budget: deadline,
          unit: "h",
          note: m[2] ? "precedence, incl. transit to its first zone" : "precedence",
        }),
      );
      each(text, new RegExp(`the busiest robot type \\((\\S+)\\) needs at least ${NUM} h`), (m) =>
        add({ subject: m[1], kind: "type", demanded: parseNum(m[2]), budget: deadline, unit: "h", note: "busiest type, work spread over its robots" }),
      );
      each(text, new RegExp(`the locked task (\\S+) cannot end before ${NUM} h`), (m) =>
        add({ subject: m[1], kind: "task", demanded: parseNum(m[2]), budget: deadline, unit: "h", note: "locked start" }),
      );
      // validator hint: "excavator work (120 h over 2 robots needs at least 60 h)"
      each(text, new RegExp(`(\\S+) work \\(${NUM} h over (\\d+) robots? needs at least ${NUM} h\\)`), (m) =>
        add({ subject: m[1], kind: "type", demanded: parseNum(m[4]), budget: deadline, unit: "h", note: `${groupDigits(m[2])} h over ${robots(m[3])}` }),
      );
      // solver, robot time: "EX1 runs one task at a time, and the 2 tasks only it can do
      // already take 20 h back to back plus up to 8 h of transit." / "..., and the task only it
      // can do already takes 8 h plus up to 2 h of transit." (3d; "plus at least 7 h of
      // transit" since 3k, the least driving any order needs); "the 3 task(s) only it can do
      // already take 120 h back to back" (before)
      each(text, new RegExp(`(\\S+) runs one task at a time, and the (?:(\\d+) tasks?(?:\\(s\\))?|task) only it can do already takes? ${NUM} h(?: back to back)?(?: plus (up to|at least) ${NUM} h of transit)?`), (m) => {
        const n = m[2] ?? "1";
        add({
          subject: m[1],
          kind: "robot",
          demanded: parseNum(m[3]) + (m[5] ? parseNum(m[5]) : 0),
          budget: deadline,
          unit: "h",
          note: `${n} ${n === "1" ? "task" : "tasks"} only it can do${m[5] ? `, incl. ${m[4]} ${groupDigits(m[5])} h of transit` : ""}`,
        });
      });
      // solver, a type's robot time: "EX1 and EX2 each run one task at a time; the excavator
      // tasks total 40 h across 2 excavators plus up to 3 h of transit." (3d; "plus at least
      // 3 h" since 3k); "EX1 runs one task at a time; the excavator tasks total 120 h across 2
      // excavator robot(s) ..." (before). The type's robots together have the deadline once each.
      each(text, new RegExp(`(?:runs|each run) one task at a time; the (\\S+) tasks total ${NUM} h across (\\d+) \\S+(?: robot\\(s\\))?(?: plus (up to|at least) ${NUM} h of transit)?`), (m) =>
        add({
          subject: m[1],
          kind: "type",
          demanded: parseNum(m[2]) + (m[5] ? parseNum(m[5]) : 0),
          budget: deadline * Number(m[3]),
          unit: "h",
          note: `${robots(m[3])} × ${fmtNum(deadline, 0)} h${m[5] ? `, incl. ${m[4]} ${groupDigits(m[5])} h of transit` : ""}`,
        }),
      );
      // solver: "Zone Z1 admits one robot at a time, so its 5 tasks (60 h in total) cannot
      // overlap." ("its 1 task (8 h in total)")
      each(text, new RegExp(`Zone (\\S+) admits one robot at a time, so its (\\d+) tasks? \\(${NUM} h in total\\)`), (m) =>
        add({ subject: m[1], kind: "zone", demanded: parseNum(m[3]), budget: deadline, unit: "h", note: `${m[2]} ${m[2] === "1" ? "task" : "tasks"}, one robot at a time` }),
      );
    }
  }
  for (const [type, p] of pools) {
    const sameBudget = p.robots.every((r) => r.budget === p.robots[0].budget);
    const across =
      p.robots.length === 1
        ? `${p.robots[0].id} (${fmtNum(p.robots[0].budget, 0)} Wh)`
        : sameBudget
          ? `${p.robots.map((r) => r.id).join(", ")} (${fmtNum(p.robots[0].budget, 0)} Wh each)`
          : p.robots.map((r) => `${r.id} ${fmtNum(r.budget, 0)} Wh`).join(", ");
    at = p.at;
    add({
      subject: type,
      kind: "type",
      demanded: p.demanded,
      budget: p.pool,
      unit: "Wh",
      note: withNote(`no split fits ${across}`, p.travel !== null ? travelNote(parseNum(p.travel)) : null),
    });
  }
  return rows.sort((a, b) => a.at - b.at).map((r) => r.row);
}

// ------------------------------------------------------------------ repair diff
export interface DiffChip {
  /** the task id (the answer's id; a removed task's old id), or the id range of a
   *  renumbering ("T4–T10 → T5–T11") */
  task: string;
  /** the id a renumbered task had in the graph the repair answered: "T4 (was T3)" */
  was?: string;
  /** "replaced": the repair removed a task and gave its id to a new one (the id is both removed
   *  and added) and the diff does not prove a split; one neutral chip, never "T2 added" next to
   *  "T2 removed" */
  kind: "added" | "removed" | "replaced" | "split" | "changed" | "renumbered";
  /** chip text after the task id, e.g. "energy 4,000 → 2,100 Wh · zone Z2 → Z1" */
  text: string;
  /** tooltip: the task name when known plus the full change list */
  title: string;
}

export interface RepairSummary {
  chips: DiffChip[];
  /** energy dropped more than 25 % or a quantity was reduced: the repair re-scoped work */
  scopeReview: boolean;
  scopeReason: string | null;
  /** signed % change of total energy (RepairDiff.scope_drift_pct) */
  drift: number;
  /** "15,700 → 14,000 Wh (−10.8%) · 123 → 117 robot-hours" (the work summed over tasks) */
  totals: string;
}

type FieldChanges = Map<DiffField, { before: unknown; after: unknown }>;

/** The diff's changed rows grouped by the task they belong to (the answer's id), in the
 *  diff's order, with the id the task had before when it was renumbered. */
function groupChanges(diff: RepairDiff): Map<string, { from: string | null; fields: FieldChanges }> {
  const out = new Map<string, { from: string | null; fields: FieldChanges }>();
  for (const c of diff.changed) {
    let entry = out.get(c.task);
    if (!entry) {
      entry = { from: null, fields: new Map() };
      out.set(c.task, entry);
    }
    if (typeof c.from === "string" && c.from !== c.task) entry.from = c.from;
    entry.fields.set(c.field, { before: c.before, after: c.after });
  }
  return out;
}

/** A quantity change under this share of the larger figure, or one the chips would print as the
 *  same number, is not shown: a repair that writes 706.86 m² as 706.858347 m² re-derived the
 *  same work. Recordings made before the backend compared quantities to two decimals (loop.py
 *  _diff_value) carry such rows. */
const QUANTITY_NOISE = 0.005;
/** A quantity that fell by more than this share was cut: the "scope changed" flag. */
const QUANTITY_CUT = 0.01;

function quantityNoise(before: unknown, after: unknown): boolean {
  if (typeof before !== "number" || typeof after !== "number") return false;
  return (
    fmtNum(before, 1) === fmtNum(after, 1) ||
    Math.abs(after - before) < QUANTITY_NOISE * Math.max(Math.abs(before), Math.abs(after))
  );
}

/** The changed rows the chips show: groupChanges without quantity noise (quantityNoise), a row
 *  left with no change dropped, so a task whose only change was noise reads as unchanged (or
 *  as renumbered, work unchanged). graphBefore keeps every row, so a rebuilt graph still fits
 *  the earlier diffs exactly. */
function shownChanges(diff: RepairDiff): Map<string, { from: string | null; fields: FieldChanges }> {
  const rows = groupChanges(diff);
  for (const [id, row] of rows) {
    const q = row.fields.get("quantity");
    if (q && quantityNoise(q.before, q.after)) row.fields.delete("quantity");
    if (row.fields.size === 0) rows.delete(id);
  }
  return rows;
}

function fmtDiffValue(field: DiffField, v: unknown, unit: string): string {
  if (v === null || v === undefined) return "none";
  if (field === "after") return Array.isArray(v) && v.length ? v.map(String).join(" + ") : "none";
  if (field === "unit" && typeof v === "string") return unitLabel(v) || v;
  if (typeof v === "number") {
    if (field === "duration_h") return `${fmtNum(v, 1)} h`;
    if (field === "energy_wh") return `${fmtNum(v, 0)} Wh`;
    if (field === "quantity") return `${fmtNum(v, 1)}${unit ? ` ${unit}` : ""}`;
    return fmtNum(v, 1);
  }
  return String(v);
}

/** One number range without repeating the unit: "4,000 → 2,100 Wh". */
function range(field: DiffField, before: unknown, after: unknown, unit: string): string {
  const a = fmtDiffValue(field, before, unit);
  const b = fmtDiffValue(field, after, unit);
  const m = /^(.*\S)( (?:h|Wh|m³|m²|m|ea))$/.exec(a);
  return m && b.endsWith(m[2]) ? `${m[1]} → ${b}` : `${a} → ${b}`;
}

/** A quantity range with each side's own unit when the unit changed ("353.4 m² → 141.4 m³"),
 *  one unit when it did not ("141.4 → 280 m³"), none when neither is known ("100 → 353.4");
 *  when only the new unit is known (a re-typed task of an older recording, whose diff has no
 *  unit field) the old figure is quoted bare after the new one ("353.4 m² (was 100)"), so no
 *  unit is claimed for it. */
function quantityRange(before: unknown, after: unknown, unitBefore: string, unitAfter: string): string {
  if (unitBefore === unitAfter) return range("quantity", before, after, unitAfter);
  if (!unitBefore && typeof before === "number") return `${fmtDiffValue("quantity", after, unitAfter)} (was ${fmtNum(before, 1)})`;
  return `${fmtDiffValue("quantity", before, unitBefore)} → ${fmtDiffValue("quantity", after, unitAfter)}`;
}

const workOf = (t: { duration_h: number; energy_wh: number }) => `${fmtNum(t.duration_h, 1)} h · ${fmtWh(t.energy_wh)}`;

const SAME = 1e-6;

function sameNumber(a: number, b: number, tolerance = SAME): boolean {
  return Math.abs(a - b) <= tolerance * Math.max(1, Math.abs(a), Math.abs(b));
}

/** A diff reports a quantity to two decimals (backend loop.py _diff_value): a task's own figure
 *  fits it within half a hundredth. */
function sameQuantity(a: number, b: number): boolean {
  return Math.abs(a - b) <= 0.005 + SAME * Math.max(1, Math.abs(a), Math.abs(b));
}

/** Whether `tasks` is the graph this repair produced, as far as the diff can tell: every added
 *  id is in it (and, in a per-id diff, no removed one), every changed row's `after` value is
 *  the task's own, and its energy per robot type, total energy and total hours are the
 *  diff's after-figures. A graph that fails is never used to name, unit or match the chips:
 *  a round's chips must not be read against a graph later rounds rewrote. */
export function graphFitsDiff(diff: RepairDiff, tasks: readonly Task[]): boolean {
  if (tasks.length === 0) return false;
  const byId = new Map(tasks.map((t) => [t.id, t]));
  if (byId.size !== tasks.length) return false;
  if (diff.added.some((id) => !byId.has(id))) return false;
  if (!isMatchedDiff(diff) && diff.removed.some((id) => byId.has(id))) return false;
  for (const c of diff.changed) {
    const t = byId.get(c.task);
    if (!t) return false;
    if (c.field === "after") continue; // edge lists are compared through the renumbering
    const now = (t as unknown as Record<string, unknown>)[c.field];
    if (typeof c.after === "number" && typeof now === "number") {
      if (!(c.field === "quantity" ? sameQuantity(c.after, now) : sameNumber(c.after, now))) return false;
    } else if (c.field === "name") {
      if (typeof c.after !== "string" || typeof now !== "string" || nameKey(c.after) !== nameKey(now)) return false;
    } else if ((c.after ?? null) !== (now ?? null)) {
      return false;
    }
  }
  const perType = new Map<string, number>();
  let energy = 0;
  let hours = 0;
  for (const t of tasks) {
    perType.set(t.robot_type, (perType.get(t.robot_type) ?? 0) + t.energy_wh);
    energy += t.energy_wh;
    hours += t.duration_h;
  }
  const after = diff.per_type_energy_after ?? {};
  for (const type of new Set([...perType.keys(), ...Object.keys(after)])) {
    if (Math.abs((perType.get(type) ?? 0) - (after[type] ?? 0)) > 0.5) return false;
  }
  if (Number.isFinite(diff.total_energy_wh_after) && Math.abs(energy - diff.total_energy_wh_after) > 0.5) return false;
  if (Number.isFinite(diff.total_duration_h_after) && Math.abs(hours - diff.total_duration_h_after) > 0.5) return false;
  return true;
}

/** Names compared the way the backend pairs them: case, whitespace and punctuation ignored. */
function nameKey(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/** The scalar fields a repair can change on a task, in the order the chips list them. */
const SCALAR_FIELDS: DiffField[] = ["name", "robot_type", "zone", "structure", "unit", "quantity", "duration_h", "energy_wh"];

/**
 * The graph a repair answered, rebuilt from the graph it produced and its diff (phase 3d
 * diffs only): every paired task with its `before` values and its old id, the added tasks
 * left out. Null when that graph cannot be known: the diff removed tasks (their work is not
 * in the diff) or is a per-id diff of an older recording. Predecessor lists are translated
 * back through the renumbering ("T2 (removed)" read as the old T2); edges the backend forgave
 * as a re-point stay as the answer wrote them, so they are approximate, and nothing here reads
 * them.
 */
export function graphBefore(diff: RepairDiff, after: readonly Task[]): Task[] | null {
  if (!isMatchedDiff(diff) || diff.removed.length > 0 || after.length === 0) return null;
  const added = new Set(diff.added);
  const rows = groupChanges(diff);
  const back = new Map(diff.renumbered.map((r) => [r.to, r.from]));
  const oldId = (id: string) => back.get(id) ?? id;
  const out: Task[] = [];
  for (const t of after) {
    if (added.has(t.id)) continue;
    const row = rows.get(t.id);
    const was: Task = { ...t, id: row?.from ?? oldId(t.id), after: t.after.filter((p) => !added.has(p)).map(oldId) };
    const record = was as unknown as Record<string, unknown>;
    for (const field of SCALAR_FIELDS) {
      const change = row?.fields.get(field);
      if (change) record[field] = change.before;
    }
    const edges = row?.fields.get("after");
    if (edges && Array.isArray(edges.before)) {
      was.after = edges.before.map((p) => String(p).replace(/ \(removed\)$/, "")).map(oldId);
    }
    out.push(was);
  }
  return out;
}

/** Index of the ok taskgraph / repair step before step `i` (the answer the repair at `i`
 *  repaired), or -1. */
function previousGraphStep(steps: readonly LoopStep[], i: number): number {
  for (let j = i - 1; j >= 0; j -= 1) {
    const s = steps[j];
    if ((s.stage === "taskgraph" || s.stage === "repair") && s.ok) return j;
  }
  return -1;
}

/**
 * The graph each validated repair produced, for its diff chips: step index -> tasks. The
 * repair that produced the plan's final graph (lastGraphStep) gets `finalTasks`; walking back,
 * each earlier repair gets the graph the next one answered, rebuilt from that one's diff
 * (graphBefore), as long as it can be rebuilt exactly and fits the diff it is read with
 * (graphFitsDiff). A recording made before phase 3d (a per-id diff) stops the walk: there
 * only the producing repair gets a graph, never an earlier round.
 */
export function repairGraphs(steps: readonly LoopStep[], finalTasks: readonly Task[]): Map<number, Task[]> {
  const out = new Map<number, Task[]>();
  let i = lastGraphStep([...steps]);
  let graph: Task[] | null = finalTasks.length > 0 ? [...finalTasks] : null;
  while (i >= 0 && graph) {
    const step = steps[i];
    if (step.stage !== "repair") break;
    const diff = repairDiff(step);
    if (!diff || !graphFitsDiff(diff, graph)) break;
    out.set(i, graph);
    graph = graphBefore(diff, graph);
    i = previousGraphStep(steps, i);
  }
  return out;
}

/** "T4–T10 → T5–T11" for a run of ids shifted together, runs joined by ", ". */
function renumberLabel(pairs: readonly DiffRenumbering[]): string {
  const split = (id: string) => {
    const m = /^(.*?)(\d+)$/.exec(id);
    return m ? { prefix: m[1], n: Number(m[2]) } : null;
  };
  const runs: DiffRenumbering[][] = [];
  for (const p of pairs) {
    const run = runs[runs.length - 1];
    const last = run?.[run.length - 1];
    const a = last ? split(last.from) : null;
    const b = last ? split(last.to) : null;
    const c = split(p.from);
    const d = split(p.to);
    const continues =
      a !== null && b !== null && c !== null && d !== null &&
      c.prefix === a.prefix && d.prefix === b.prefix && c.n === a.n + 1 && d.n === b.n + 1;
    if (continues) run.push(p);
    else runs.push([p]);
  }
  return runs
    .map((run) =>
      run.length === 1
        ? `${run[0].from} → ${run[0].to}`
        : `${run[0].from}–${run[run.length - 1].from} → ${run[0].to}–${run[run.length - 1].to}`,
    )
    .join(", ");
}

/** The clauses of one changed task, in reading order: "renamed", "moved to excavator", "zone
 *  Z2 → Z4", "quantity 141.4 → 280 m³", "duration 24 → 14 h", "energy 4,000 → 2,100 Wh",
 *  "after T2 (removed) + T4 → T4". `unitBefore` / `unitAfter` are the quantity's units when
 *  known ("" when not). `quantityDropped` says the same work shrank: a quantity more than 1 %
 *  smaller (QUANTITY_CUT), or none, in the same unit, robot type, zone and structure. */
function changeParts(
  fields: FieldChanges,
  unitBefore: string,
  unitAfter: string,
  splitChildren?: string[],
  self?: string,
): { parts: string[]; detail: string[]; quantityDropped: boolean } {
  const parts: string[] = [];
  const detail: string[] = [];
  if (splitChildren && self) {
    parts.push(`split into ${self} + ${splitChildren.join(" + ")}`);
    detail.push(parts[0]);
  }
  const name = fields.get("name");
  if (name) {
    parts.push("renamed");
    detail.push(`renamed “${String(name.before)}” → “${String(name.after)}”`);
  }
  const rt = fields.get("robot_type");
  if (rt) {
    parts.push(`moved to ${String(rt.after)}`);
    detail.push(`robot type ${String(rt.before)} → ${String(rt.after)}`);
  }
  for (const field of ["zone", "structure"] as const) {
    const c = fields.get(field);
    if (!c) continue;
    const text = `${field} ${fmtDiffValue(field, c.before, "")} → ${fmtDiffValue(field, c.after, "")}`;
    parts.push(text);
    detail.push(text);
  }
  const q = fields.get("quantity");
  const unit = fields.get("unit");
  if (q) {
    const text = `quantity ${quantityRange(q.before, q.after, unitBefore, unitAfter)}`;
    parts.push(text);
    detail.push(text);
  } else if (unit) {
    const text = `unit ${range("unit", unit.before, unit.after, "")}`;
    parts.push(text);
    detail.push(text);
  }
  for (const field of ["duration_h", "energy_wh", "after"] as const) {
    const c = fields.get(field);
    if (!c) continue;
    const text = `${DIFF_FIELD_LABELS[field]} ${range(field, c.before, c.after, "")}`;
    parts.push(text);
    detail.push(text);
  }
  const quantityDropped =
    q !== undefined &&
    !unit &&
    !rt &&
    !fields.has("zone") &&
    !fields.has("structure") &&
    typeof q.before === "number" &&
    (q.after === null ||
      q.after === undefined ||
      (typeof q.after === "number" && q.after < q.before * (1 - QUANTITY_CUT)));
  return { parts, detail, quantityDropped };
}

/** A removed task the answer split into several added ones, proven from the diff: exactly one
 *  task was removed, its robot type and energy follow from the diff's per-type energy before
 *  the repair minus what the paired tasks had then, and exactly one set of at least two added
 *  tasks of that type adds back to that energy within 10 %. Null when the diff does not prove
 *  it (or `tasks`, the graph the repair produced, is not known). */
function provenSplit(
  diff: RepairDiff,
  tasks: readonly Task[],
  rows: Map<string, { from: string | null; fields: FieldChanges }>,
): { removed: string; type: string; energy: number; duration: number | null; halves: Task[] } | null {
  if (tasks.length === 0 || diff.removed.length !== 1) return null;
  const added = new Set(diff.added);
  const paired = new Map<string, number>();
  let pairedHours = 0;
  for (const t of tasks) {
    if (added.has(t.id)) continue;
    const fields = rows.get(t.id)?.fields;
    const rt = fields?.get("robot_type")?.before;
    const e = fields?.get("energy_wh")?.before;
    const d = fields?.get("duration_h")?.before;
    const type = typeof rt === "string" ? rt : t.robot_type;
    paired.set(type, (paired.get(type) ?? 0) + (typeof e === "number" ? e : t.energy_wh));
    pairedHours += typeof d === "number" ? d : t.duration_h;
  }
  const before = diff.per_type_energy_before ?? {};
  const left: [string, number][] = [];
  for (const type of new Set([...Object.keys(before), ...paired.keys()])) {
    const rest = (before[type] ?? 0) - (paired.get(type) ?? 0);
    if (Math.abs(rest) > 1) left.push([type, rest]);
  }
  if (left.length !== 1 || left[0][1] <= 0) return null;
  const [type, energy] = left[0];
  const candidates = tasks.filter((t) => added.has(t.id) && t.robot_type === type);
  if (candidates.length < 2 || candidates.length > 10) return null;
  const fits: Task[][] = [];
  for (let mask = 1; mask < 1 << candidates.length; mask += 1) {
    const subset = candidates.filter((_, k) => (mask >> k) & 1);
    if (subset.length < 2) continue;
    const sum = subset.reduce((acc, t) => acc + t.energy_wh, 0);
    if (Math.abs(sum - energy) <= 0.1 * energy) fits.push(subset);
  }
  if (fits.length !== 1) return null;
  const hours = Number.isFinite(diff.total_duration_h_before) ? diff.total_duration_h_before - pairedHours : NaN;
  return { removed: diff.removed[0], type, energy, duration: hours > 0 ? hours : null, halves: fits[0] };
}

/**
 * The repair diff as chips: what THIS round changed, from its own diff.
 *
 * A phase-3d diff (the backend paired the two graphs, `renumbered` present) is rendered
 * verbatim: one chip per changed task ("T4 (was T3) renamed · duration 24 → 14 h · energy
 * 4,000 → 2,100 Wh"), "added" / "removed" for the unmatched ids, a split only when the diff
 * proves it ("T2 split into T2 + T3 · 24 h · 4,000 Wh → 14 h · 2,100 Wh each": one removed
 * task whose energy, from the per-type figures, the added tasks of its robot type give back
 * within 10 %), and one line for the renumbered ids that changed nothing ("T4–T10 → T5–T11 ·
 * 7 tasks renumbered, work unchanged"). A per-id diff of an older recording keeps the old
 * inference (a split from a shrunken task plus an added one, a renumbering from equal work
 * under shifted ids), and only with the graph that repair produced.
 *
 * `tasks` is the graph THIS repair produced (repairGraphs: the plan's final graph for the step
 * lastGraphStep names, a graph rebuilt from the later diffs for an earlier phase-3d round, []
 * otherwise); it names the tasks and gives the quantities their units. A graph that does not
 * fit the diff (graphFitsDiff) is ignored, so a round is never read against a graph later
 * rounds rewrote.
 */
export function describeRepairDiff(diff: RepairDiff, tasks: readonly Task[] = []): RepairSummary {
  const graph = graphFitsDiff(diff, tasks) ? tasks : [];
  const { chips, quantityDropped } = isMatchedDiff(diff) ? matchedChips(diff, graph) : legacyChips(diff, graph);
  const drift = Number.isFinite(diff.scope_drift_pct) ? diff.scope_drift_pct : 0;
  let scopeReason: string | null = null;
  if (drift < -25) scopeReason = `total energy ${fmtSignedPct(drift)}: work was re-scoped away`;
  else if (quantityDropped) scopeReason = `quantity reduced on ${quantityDropped}`;
  const totals = `${fmtNum(diff.total_energy_wh_before, 0)} → ${fmtNum(diff.total_energy_wh_after, 0)} Wh (${fmtSignedPct(drift)}) · ${fmtNum(diff.total_duration_h_before, 0)} → ${fmtNum(diff.total_duration_h_after, 0)} robot-hours`;
  return { chips, scopeReview: scopeReason !== null, scopeReason, drift, totals };
}

/** The chips of a phase-3d diff (see describeRepairDiff). */
function matchedChips(
  diff: RepairDiff & { renumbered: DiffRenumbering[] },
  tasks: readonly Task[],
): { chips: DiffChip[]; quantityDropped: string | null } {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const rows = shownChanges(diff);
  const chips: DiffChip[] = [];
  let quantityDropped: string | null = null;
  const doneAdded = new Set<string>();
  const doneRemoved = new Set<string>();

  const split = provenSplit(diff, tasks, rows);
  if (split) {
    const ids = split.halves.map((t) => t.id);
    const was = split.duration !== null ? workOf({ duration_h: split.duration, energy_wh: split.energy }) : fmtWh(split.energy);
    const equal = split.halves.every((t) => t.duration_h === split.halves[0].duration_h && t.energy_wh === split.halves[0].energy_wh);
    const into = equal ? `${workOf(split.halves[0])} each` : split.halves.map(workOf).join(" + ");
    const sum = split.halves.reduce((acc, t) => acc + t.energy_wh, 0);
    chips.push({
      task: split.removed,
      kind: "split",
      text: `split into ${ids.join(" + ")} · ${was} → ${into}`,
      title:
        `${split.removed} (${split.type}, ${was}) of the graph the repair answered became ` +
        `${joinAnd(split.halves.map((t) => `${t.id} ${t.name} (${workOf(t)})`))}; ` +
        `together ${fmtWh(sum)}, ${fmtSignedPct((100 * (sum - split.energy)) / split.energy)} against the task they replace`,
    });
    doneRemoved.add(split.removed);
    for (const id of ids) doneAdded.add(id);
  }

  // the changed tasks in the order of the graph they belong to, when it is known
  const order = new Map(tasks.map((t, i) => [t.id, i]));
  const changed = [...rows.entries()].sort((a, b) => (order.get(a[0]) ?? 1e9) - (order.get(b[0]) ?? 1e9));
  for (const [id, row] of changed) {
    const task = byId.get(id);
    const unitRow = row.fields.get("unit");
    const unitAfter = unitRow ? unitLabel(String(unitRow.after ?? "")) : unitLabel(task?.unit);
    const unitBefore = unitRow ? unitLabel(String(unitRow.before ?? "")) : unitAfter;
    const { parts, detail, quantityDropped: dropped } = changeParts(row.fields, unitBefore, unitAfter);
    if (dropped && !quantityDropped) quantityDropped = id;
    const head = `${id}${row.from ? ` (was ${row.from})` : ""}`;
    chips.push({
      task: id,
      ...(row.from ? { was: row.from } : {}),
      kind: "changed",
      text: parts.join(" · "),
      title: `${head}${task ? ` ${task.name}` : ""}: ${detail.join("; ")}`,
    });
  }

  const removed = new Set(diff.removed);
  for (const id of diff.added) {
    if (doneAdded.has(id)) continue;
    const t = byId.get(id);
    if (removed.has(id) && !doneRemoved.has(id)) {
      // the old task is gone and a new one took its id: one chip, whether or not the graph
      // that would prove a split is known yet (it is not while the run streams)
      doneRemoved.add(id);
      chips.push({
        task: id,
        kind: "replaced",
        text: "replaced",
        title: `${id} of the graph the repair answered is gone; the answer's ${id} is a new task${
          t ? `: ${t.name} (${t.robot_type}, ${workOf(t)})` : ""
        }`,
      });
      continue;
    }
    chips.push({
      task: id,
      kind: "added",
      text: "added",
      title: t ? `${id} ${t.name} (${t.robot_type}, ${workOf(t)})` : `${id} added by the repair`,
    });
  }
  for (const id of diff.removed) {
    if (doneRemoved.has(id)) continue;
    chips.push({
      task: id,
      kind: "removed",
      text: "removed",
      title: `${id} of the graph the repair answered is gone`,
    });
  }

  // the renumbered tasks that changed nothing: one line, after the substantive chips
  const quiet = diff.renumbered.filter((r) => !rows.has(r.to));
  if (quiet.length > 0) {
    chips.push({
      task: renumberLabel(quiet),
      kind: "renumbered",
      text: `${quiet.length === 1 ? "renumbered" : `${plural(quiet.length, "task")} renumbered`}, work unchanged`,
      title: quiet.map((r) => `${r.from} → ${r.to}${byId.get(r.to) ? ` ${byId.get(r.to)?.name}` : ""}`).join("; "),
    });
  }
  return { chips, quantityDropped };
}

// ---- recordings made before phase 3d: a per-id diff, so splits and renumberings are inferred
/** A task's state on one side of a per-id diff, for matching work across ids. */
interface TaskState {
  robot_type: string;
  zone: string;
  duration_h: number;
  energy_wh: number;
  quantity: number | null;
}

function stateSig(s: TaskState): string {
  return `${s.robot_type}|${s.zone}|${s.duration_h}|${s.energy_wh}|${s.quantity ?? ""}`;
}

/** The task as it was before the repair: its state in the graph the repair produced with the
 *  diff's `before` values put back. */
function stateBefore(task: Task, fields: FieldChanges): TaskState {
  const s: TaskState = {
    robot_type: task.robot_type,
    zone: task.zone,
    duration_h: task.duration_h,
    energy_wh: task.energy_wh,
    quantity: task.quantity,
  };
  for (const [field, v] of fields) {
    if (field === "robot_type" && typeof v.before === "string") s.robot_type = v.before;
    else if (field === "zone" && typeof v.before === "string") s.zone = v.before;
    else if (field === "duration_h" && typeof v.before === "number") s.duration_h = v.before;
    else if (field === "energy_wh" && typeof v.before === "number") s.energy_wh = v.before;
    else if (field === "quantity") s.quantity = typeof v.before === "number" ? v.before : null;
  }
  return s;
}

function idNumber(id: string): number | null {
  const m = /(\d+)$/.exec(id);
  return m ? Number(m[1]) : null;
}

/**
 * Old id -> new id for tasks a per-id diff shows renumbered: when the model inserts a task and
 * shifts every later id by one, the per-id diff reads as a cascade of re-scopes although the
 * work did not change. A changed id whose pre-repair state equals the final state of another
 * added or changed id is such a shift, kept only when the shift is a chain that ends in an
 * added id.
 */
function detectRenumbering(changes: Map<string, FieldChanges>, diff: RepairDiff, tasks: readonly Task[], byId: Map<string, Task>): Map<string, string> {
  const touched = new Set([...diff.added, ...changes.keys()]);
  const order = new Map(tasks.map((t, i) => [t.id, i]));
  const out = new Map<string, string>();
  const taken = new Set<string>();
  for (const [id, fields] of changes) {
    const task = byId.get(id);
    if (!task) continue;
    const want = stateSig(stateBefore(task, fields));
    const own = order.get(id) ?? 0;
    const candidates = tasks.filter((c) => c.id !== id && touched.has(c.id) && !taken.has(c.id) && stateSig(c) === want);
    if (candidates.length === 0) continue;
    // the shift pattern first: the nearest later id
    candidates.sort((a, b) => {
      const da = (order.get(a.id) ?? 0) - own;
      const db = (order.get(b.id) ?? 0) - own;
      return (da > 0 ? 0 : 1) - (db > 0 ? 0 : 1) || Math.abs(da) - Math.abs(db);
    });
    out.set(id, candidates[0].id);
    taken.add(candidates[0].id);
  }
  // a shift is a chain that ends in a new id: every target is itself a source (its own old
  // work moved on) or an added id. A lone match against an id that kept its own diff entry is
  // a coincidence of equal figures (a task re-scoped to the work another one gave up), not a
  // renumbering, and would print "renumbered, work unchanged" over a real change.
  const added = new Set(diff.added);
  for (let pruned = true; pruned; ) {
    pruned = false;
    for (const [o, n] of out) {
      if (!added.has(n) && !out.has(n)) {
        out.delete(o);
        pruned = true;
      }
    }
  }
  return out;
}

/** "duration 24 → 14 h · energy 4,000 → 2,100 Wh" between an old state and a final task. */
function stateDelta(old: TaskState, now: Task): { parts: string[]; quantityDropped: boolean } {
  const parts: string[] = [];
  const unit = unitLabel(now.unit);
  if (old.robot_type !== now.robot_type) parts.push(`moved to ${now.robot_type}`);
  if (old.zone !== now.zone) parts.push(`zone ${old.zone} → ${now.zone}`);
  let quantityDropped = false;
  if (old.quantity !== now.quantity && !quantityNoise(old.quantity, now.quantity)) {
    // the unit of a re-typed task's old quantity is not in a per-id diff
    const u = old.robot_type === now.robot_type ? unit : "";
    parts.push(`quantity ${quantityRange(old.quantity, now.quantity, u, unit)}`);
    quantityDropped =
      old.robot_type === now.robot_type &&
      old.zone === now.zone &&
      typeof old.quantity === "number" &&
      (now.quantity === null || now.quantity < old.quantity * (1 - QUANTITY_CUT));
  }
  if (old.duration_h !== now.duration_h) parts.push(`duration ${range("duration_h", old.duration_h, now.duration_h, unit)}`);
  if (old.energy_wh !== now.energy_wh) parts.push(`energy ${range("energy_wh", old.energy_wh, now.energy_wh, unit)}`);
  return { parts, quantityDropped };
}

/** The quantity units of a per-id changed row: the task's unit on both sides when its robot
 *  type stayed (an older diff has no unit field), only on the new side when it was re-typed. */
function legacyUnits(task: Task | undefined, fields: FieldChanges): [string, string] {
  const unit = unitLabel(task?.unit);
  return [fields.has("robot_type") ? "" : unit, unit];
}

/** The chips of a per-id diff (a recording made before phase 3d; see describeRepairDiff). */
function legacyChips(diff: RepairDiff, tasks: readonly Task[]): { chips: DiffChip[]; quantityDropped: string | null } {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const changes = new Map<string, FieldChanges>();
  for (const [id, row] of shownChanges(diff)) changes.set(id, row.fields);

  const chips: DiffChip[] = [];
  let quantityDropped: string | null = null;
  const noteDrop = (dropped: boolean, id: string) => {
    if (dropped && !quantityDropped) quantityDropped = id;
  };
  /** final ids already described by a chip (the "added" loop skips them) */
  const handled = new Set<string>();

  const renumbered = detectRenumbering(changes, diff, tasks, byId);
  if (renumbered.size > 0) {
    const pairs = [...renumbered].sort((a, b) => (idNumber(a[0]) ?? 0) - (idNumber(b[0]) ?? 0));
    // listed after the substantive changes: it is the least interesting chip
    const renumberedChip: DiffChip = {
      task: renumberLabel(pairs.map(([from, to]) => ({ from, to }))),
      kind: "renumbered",
      text: `${pairs.length === 1 ? "renumbered" : `${plural(pairs.length, "task")} renumbered`}, work unchanged`,
      title: pairs.map(([o, n]) => `${o} → ${n}${byId.get(n) ? ` ${byId.get(n)?.name}` : ""}`).join("; "),
    };
    for (const [, n] of pairs) handled.add(n);

    // ids whose old work is gone: match them to the new work (added or changed ids nothing
    // moved into) by robot type and zone, as a split or a re-scope
    const movedInto = new Set(renumbered.values());
    const free = new Set(tasks.filter((t) => (diff.added.includes(t.id) || changes.has(t.id)) && !movedInto.has(t.id)).map((t) => t.id));
    for (const [id, fields] of changes) {
      if (renumbered.has(id)) continue;
      const task = byId.get(id);
      const old = task ? stateBefore(task, fields) : null;
      // the id itself first: an id that still holds work of its own type and zone kept it
      const group = old ? tasks.filter((t) => free.has(t.id) && t.robot_type === old.robot_type && t.zone === old.zone) : [];
      if (!old || group.length === 0) {
        const [ub, ua] = legacyUnits(task, fields);
        const { parts, detail, quantityDropped: dropped } = changeParts(fields, ub, ua);
        noteDrop(dropped, id);
        chips.push({ task: id, kind: "changed", text: parts.join(" · "), title: `${id}${task ? ` ${task.name}` : ""}: ${detail.join("; ")}` });
        handled.add(id);
        continue;
      }
      const sum = group.reduce((acc, t) => acc + t.energy_wh, 0);
      if (group.length > 1 && Math.abs(sum - old.energy_wh) <= Math.max(1, 0.1 * old.energy_wh)) {
        const ids = group.map((t) => t.id);
        chips.push({
          task: id,
          kind: "split",
          text: `split into ${ids.join(" + ")} · ${workOf(old)} → ${group.map(workOf).join(" + ")}`,
          title: group.map((t) => `${t.id} ${t.name} (${workOf(t)})`).join("; "),
        });
        for (const t of group) {
          free.delete(t.id);
          handled.add(t.id);
        }
        continue;
      }
      const now = group.find((t) => t.id === id) ?? group[0];
      if (now.id === id) {
        // the id kept its work: the diff's own fields, with the task's unit
        const [ub, ua] = legacyUnits(task, fields);
        const { parts, detail, quantityDropped: dropped } = changeParts(fields, ub, ua);
        noteDrop(dropped, id);
        chips.push({ task: id, kind: "changed", text: parts.join(" · "), title: `${id}${task ? ` ${task.name}` : ""}: ${detail.join("; ")}` });
        free.delete(id);
        handled.add(id);
        continue;
      }
      // the old work went on under another id: what changed on the way, the edges included
      // when the diff recorded the old ones
      const { parts, quantityDropped: dropped } = stateDelta(old, now);
      const edges = fields.get("after");
      if (edges && Array.isArray(edges.before)) {
        const was = edges.before.map(String).sort();
        const is = [...now.after].sort();
        if (was.length !== is.length || was.some((x, k) => x !== is[k])) parts.push(`after ${range("after", edges.before, now.after, "")}`);
      }
      noteDrop(dropped, now.id);
      const scoped = parts.some((x) => /^(duration|energy|quantity)/.test(x));
      const words = [scoped ? "re-scoped" : "renumbered", ...parts];
      chips.push({ task: now.id, was: id, kind: "changed", text: words.join(" · "), title: `${now.id} (was ${id}) ${now.name}: ${words.join("; ")}` });
      free.delete(now.id);
      handled.add(now.id);
    }
    chips.push(renumberedChip);
  } else {
    const splitOf = new Map<string, string[]>();
    for (const added of diff.added) {
      const child = byId.get(added);
      if (!child) continue;
      let best: { parent: string; err: number } | null = null;
      for (const [task, fields] of changes) {
        const e = fields.get("energy_wh");
        if (!e || typeof e.before !== "number" || typeof e.after !== "number" || e.after >= e.before) continue;
        const parent = byId.get(task);
        if (!parent || parent.robot_type !== child.robot_type || fields.has("robot_type")) continue;
        const err = Math.abs(e.before - (e.after + child.energy_wh));
        if (err <= Math.max(1, 0.1 * e.before) && (!best || err < best.err)) best = { parent: task, err };
      }
      if (best) {
        splitOf.set(best.parent, [...(splitOf.get(best.parent) ?? []), added]);
        handled.add(added);
      }
    }
    for (const [task, fields] of changes) {
      const children = splitOf.get(task);
      const [ub, ua] = legacyUnits(byId.get(task), fields);
      const { parts, detail, quantityDropped: dropped } = changeParts(fields, ub, ua, children, task);
      noteDrop(dropped, task);
      const name = byId.get(task)?.name;
      chips.push({
        task,
        kind: children ? "split" : "changed",
        text: parts.join(" · "),
        title: `${task}${name ? ` ${name}` : ""}: ${detail.join("; ")}`,
      });
    }
  }

  for (const id of diff.added) {
    if (handled.has(id)) continue;
    const t = byId.get(id);
    chips.push({
      task: id,
      kind: "added",
      text: "added",
      title: t ? `${id} ${t.name} (${t.robot_type}, ${workOf(t)})` : `${id} added by the repair`,
    });
  }
  for (const id of diff.removed) {
    chips.push({ task: id, kind: "removed", text: "removed", title: `${id} of the graph the repair answered is gone` });
  }
  return { chips, quantityDropped };
}
