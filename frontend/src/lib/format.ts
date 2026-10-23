/**
 * Formatting helpers shared by the shell components. Hours are the planner's unit (integer in
 * schedules, float in the simulator); energy is Wh; token counts come from LoopStep.usage.
 */
import {
  CONFLICT_PREFIXES,
  OPERATIONS,
  OPERATION_LABELS,
  SUGGESTION_COPY,
  SUGGESTION_KIND_LABELS,
  conflictPrefix,
  isSuggestion,
  unitLabel,
  type DiffField,
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

export function fmtNum(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (Number.isInteger(n)) return n.toLocaleString("en-US");
  return n.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

/** "77 h", "90.3 h"; integers print without decimals, null prints an em dash. */
export function fmtHours(h: number | null | undefined, digits = 1): string {
  if (h === null || h === undefined || !Number.isFinite(h)) return "—";
  return `${fmtNum(h, digits)} h`;
}

/** "336 h (14 d)" for horizons; anything under two days stays in hours only. */
export function fmtHorizon(h: number): string {
  if (!Number.isFinite(h)) return "—";
  if (h < 48) return fmtHours(h, 0);
  return `${fmtHours(h, 0)} (${fmtNum(h / 24, 1)} d)`;
}

export function fmtSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  if (s < 0.005) return "<0.01 s";
  if (s < 10) return `${s.toFixed(2)} s`;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return `${m} m ${String(rest).padStart(2, "0")} s`;
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
  return `${fmtNum(wh, 0)} Wh`;
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

/** Meaning of a conflict id's prefix, e.g. "energy:EX1" -> "battery budget exceeded". */
export function conflictMeaning(conflict: string): string | null {
  return CONFLICT_PREFIXES[conflictPrefix(conflict)] ?? null;
}

/** "energy:EX1" -> "battery budget exceeded (EX1)"; unknown prefixes return the raw id. */
export function conflictLabel(conflict: string): string {
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
 *  answer carries no tokens and reads "recorded answer · <0.01 s". */
export function fmtUsage(usage: Usage): string {
  const parts = [modelDisplayName(usage.model) || "model"];
  if (usage.input_tokens + usage.output_tokens > 0) {
    parts.push(`${fmtCompact(usage.input_tokens)} in`, `${fmtCompact(usage.output_tokens)} out`);
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
 *  other: any sentence the console does not know. */
export type AdvisoryKind = "estimate" | "ungrounded" | "volume" | "other";

/** Chip label per advisory kind, in sentence case (notes, not error codes). */
export const ADVISORY_KIND_LABELS: Record<AdvisoryKind, string> = {
  estimate: "estimate",
  ungrounded: "no quantities",
  volume: "scope differs from dimensions",
  other: "advisory",
};

export interface Advisory {
  /** `category` without "volume" (a volume line reads "other" here): kept so a label table
   *  typed Record<Advisory["kind"], string> keeps compiling; new code indexes by `category`. */
  kind: "estimate" | "ungrounded" | "other";
  /** the full classification: "volume" tells a scope-vs-dimensions warning apart */
  category: AdvisoryKind;
  /** the task an estimate advisory is about, the structure a volume advisory is about */
  task: string | null;
  text: string;
  raw: string;
}

/** "estimate:T4: model said 10 h / 1000 Wh, rate model gives 27 h / 3000 Wh (+170%)" ->
 *  {category: "estimate", task: "T4", text: "model said ..."}; "ungrounded: no task states a
 *  quantity, ..." -> {category: "ungrounded", task: null, text: "no task ..."}; "volume:S1: its
 *  dimensions give 360 m3 (100 x 6 x 0.6 m) but its excavation tasks (T3 600 m2) total only
 *  0 m3 (0%)" -> {category: "volume", kind: "other", task: "S1", text: "its dimensions give
 *  360 m³ (100 × 6 × 0.6 m) but ..."}. */
export function parseAdvisory(raw: string): Advisory {
  const est = /^estimate:([^:\s]+):\s*([\s\S]*)$/.exec(raw);
  if (est) return { kind: "estimate", category: "estimate", task: est[1], text: humanizeFields(est[2].trim()), raw };
  const ung = /^ungrounded:\s*([\s\S]*)$/.exec(raw);
  if (ung) return { kind: "ungrounded", category: "ungrounded", task: null, text: humanizeFields(ung[1].trim()), raw };
  const vol = /^volume:([^:\s]+):\s*([\s\S]*)$/.exec(raw);
  if (vol) return { kind: "other", category: "volume", task: vol[1], text: humanizeVolume(vol[2].trim()), raw };
  return { kind: "other", category: "other", task: null, text: humanizeFields(raw), raw };
}

/** The volume sentence with the console's units: "360 m3 (100 x 6 x 0.6 m)" ->
 *  "360 m³ (100 × 6 × 0.6 m)"; task ids such as T3 are left alone. */
export function humanizeVolume(text: string): string {
  return humanizeFields(text)
    .replace(/\bm3\b/g, "m³")
    .replace(/\bm2\b/g, "m²")
    .replace(/(\d)\s+x\s+(?=\d)/g, "$1 × ");
}

/** Backend sentences name schema fields ("every duration_h and energy_wh"): read them as words. */
export function humanizeFields(text: string): string {
  return text
    .replace(/\bduration_h\b/g, "duration")
    .replace(/\benergy_wh\b/g, "energy")
    .replace(/\bestimated_duration_h\b/g, "estimated duration")
    .replace(/\bestimated_energy_wh\b/g, "estimated energy")
    .replace(/\brobot_type\b/g, "robot type")
    .replace(/\bhorizon_h\b/g, "horizon")
    .replace(/\bbattery_wh\b/g, "battery");
}

/** Backend step messages print "horizon 336h" / "makespan 80h": space the unit like the rest
 *  of the console ("336 h"). */
export function tidyUnits(text: string): string {
  return text.replace(/(\d)(Wh|h)\b/g, "$1 $2");
}

/** Numbers that carry a unit get the console's formatting: "9929.649 Wh" -> "9,929.6 Wh",
 *  "5000 Wh" -> "5,000 Wh", "336h" -> "336 h"; bare counts and ids are left alone. */
export function tidyNumbers(text: string): string {
  return text.replace(/(\d+(?:\.\d+)?)\s?(kWh|Wh|h)\b/g, (_, n: string, unit: string) => `${fmtNum(Number(n), 1)} ${unit}`);
}

/** A failure detail line as the console prints it: the backend's file-system clauses dropped
 *  ("(looked in /…/x)"), absolute paths removed and its recording vocabulary read as words
 *  ("no fixture directory for brief 'x'" -> "no recording for brief 'x'"), units spaced. */
export function tidyDetail(text: string): string {
  return tidyUnits(
    text
      .replace(/\s*\(looked in [^)]*\)/g, "")
      .replace(/(^|[\s(])\/(?:[^\s/)]+\/)+[^\s)]*/g, "$1…")
      .replace(/\bfixture directory\b/gi, "recording")
      .replace(/\bfixtures?\b/gi, "recording"),
  ).trim();
}

/** "a", "a and b", "a, b and c". */
function joinAnd(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** Solver sentence for a robot type whose work fits the pool but no single battery
 *  (backend schedule.py _energy_hint), one copy per robot of the type. */
const POOL_SENTENCE =
  /^(\S+) has (\d+(?:\.\d+)?) Wh, and the (\S+) tasks \((\d+(?:\.\d+)?) Wh in total(?: \(incl\. ~(\d+(?:\.\d+)?) Wh of travel\))?\) cannot be split across the (\d+) \S+ robot\(s\) \((\d+(?:\.\d+)?) Wh combined\) without exceeding its budget\.?$/;

interface PoolHint {
  robots: { id: string; budget: number }[];
  total: number;
  travel: number | null;
  count: number;
  pool: number;
}

function renderPool(type: string, p: PoolHint): string {
  const ids = p.robots.map((r) => r.id);
  const sameBudget = p.robots.every((r) => r.budget === p.robots[0].budget);
  const who =
    p.robots.length === 1
      ? `${ids[0]} has ${fmtWh(p.robots[0].budget)}`
      : sameBudget
        ? `${joinAnd(ids)} each have ${fmtWh(p.robots[0].budget)}`
        : p.robots.map((r) => `${r.id} has ${fmtWh(r.budget)}`).join(", ");
  const whose = p.robots.length === 1 ? "its budget" : p.robots.length === 2 ? "either budget" : "one of their budgets";
  const travel = p.travel !== null ? ` (incl. ~${fmtNum(p.travel, 0)} Wh of travel)` : "";
  return `${who}; the ${type} tasks need ${fmtNum(p.total, 1)} Wh in total${travel} and cannot be split across ${plural(p.count, type)} (${fmtWh(p.pool)} combined) without exceeding ${whose}.`;
}

/** One sentence of a hint, tidied: formatted numbers, "robot(s)" resolved by its count, the
 *  nested "(… (incl. …))" flattened. */
function tidySentence(s: string): string {
  return tidyNumbers(
    s
      .replace(/\(([^()]*) \(incl\. ([^()]*)\)\)/g, "($1, incl. $2)")
      .replace(/\b(\d+)((?:\s+[a-z][a-z-]*){0,2})\s+(robot|task|zone|structure|repair|conflict)\(s\)/g, (_, n: string, mid: string, w: string) => `${n}${mid} ${w}${n === "1" ? "" : "s"}`),
  );
}

/**
 * A backend hint as the console prints it: numbers formatted like every other figure on the
 * page, "robot(s)" resolved, duplicate sentences dropped, and the solver's per-robot copies of
 * the energy-pool sentence merged into one ("EX1 and EX2 each have 5,000 Wh; the excavator
 * tasks need 9,929.6 Wh in total (incl. ~30 Wh of travel) and cannot be split across
 * 2 excavators (10,000 Wh combined) without exceeding either budget."). Unknown text passes
 * through with its numbers tidied, never reworded.
 */
export function tidyHint(text: string | null | undefined): string {
  if (!text) return "";
  const sentences = text.trim().split(/(?<=[.!?])\s+(?=[A-Z("])/);
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

/** "extend_horizon" -> "later deadline"; an unknown kind prints as words. */
export function suggestionKindLabel(kind: SuggestionKind | string): string {
  return SUGGESTION_KIND_LABELS[kind as SuggestionKind] ?? String(kind).replace(/_/g, " ");
}

/** The detail sentence as the console prints it: the words verbatim, every number that carries
 *  a unit in the console's format ("5950 Wh" -> "5,950 Wh", "86h" -> "86 h") so the row reads
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
export interface BudgetRow {
  /** robot id, robot type, task id or a precedence chain */
  subject: string;
  kind: "robot" | "type" | "task" | "chain" | "zone";
  budget: number;
  demanded: number;
  unit: "Wh" | "h";
  note: string | null;
}

const NUM = "(\\d+(?:\\.\\d+)?)";

/**
 * The numbers behind a Failure as rows of a robot / budget / demanded table, read from
 * `details` first (the validator's one line per conflict) and then from the solver's hint
 * sentences (backend/moonwright/pipeline/schedule.py `_energy_hint`, `_horizon_hint`,
 * `_robot_hint`, `_zone_hint`). Rows the sentences only imply against the deadline need
 * `horizonH`. Unparseable text yields no row, never a wrong one.
 */
export function budgetRows(failure: Failure, horizonH?: number | null): BudgetRow[] {
  const rows: BudgetRow[] = [];
  const seen = new Set<string>();
  const add = (row: BudgetRow) => {
    const key = `${row.kind}|${row.subject}|${row.unit}`;
    if (seen.has(key) || !Number.isFinite(row.budget) || !Number.isFinite(row.demanded)) return;
    seen.add(key);
    rows.push(row);
  };
  const each = (text: string, re: RegExp, fn: (m: RegExpExecArray) => void) => {
    const g = new RegExp(re.source, re.flags.includes("g") ? re.flags : `${re.flags}g`);
    for (let m = g.exec(text); m; m = g.exec(text)) fn(m);
  };
  const robots = (n: string) => `${n} ${n === "1" ? "robot" : "robots"}`;
  // the solver's energy-pool sentence comes once per robot of the type; the numbers it carries
  // are the type's (total work against the combined budget), so they make one row per type
  const pools = new Map<string, { robots: { id: string; budget: number }[]; demanded: number; travel: string | null; pool: number }>();

  const texts = [...failure.details, failure.hint].filter(Boolean);
  for (const text of texts) {
    // validator: "excavator tasks need 9900 Wh; its 2 robots carry 10000 Wh"
    each(text, new RegExp(`(\\S+) tasks need ${NUM} Wh; its (\\d+) robots? carry ${NUM} Wh`), (m) =>
      add({ subject: m[1], kind: "type", demanded: +m[2], budget: +m[4], unit: "Wh", note: `${robots(m[3])} combined` }),
    );
    // validator hint: "excavator tasks need 9900 Wh but its 2 robots carry only 10000 Wh combined"
    each(text, new RegExp(`(\\S+) tasks need ${NUM} Wh but its (\\d+) robots? carry only ${NUM} Wh combined`), (m) =>
      add({ subject: m[1], kind: "type", demanded: +m[2], budget: +m[4], unit: "Wh", note: `${robots(m[3])} combined` }),
    );
    // validator: "T4 needs 5200 Wh but the largest excavator battery is 5000 Wh"
    each(text, new RegExp(`(\\S+) needs ${NUM} Wh but the largest (\\S+) battery is ${NUM} Wh`), (m) =>
      add({ subject: m[1], kind: "task", demanded: +m[2], budget: +m[4], unit: "Wh", note: `largest ${m[3]} battery` }),
    );
    // validator hint: "T4 (5200 Wh, largest excavator battery 5000 Wh)"
    each(text, new RegExp(`(\\S+) \\(${NUM} Wh, largest (\\S+) battery ${NUM} Wh\\)`), (m) =>
      add({ subject: m[1], kind: "task", demanded: +m[2], budget: +m[4], unit: "Wh", note: `largest ${m[3]} battery` }),
    );
    // solver: "EX1 would need 5200 Wh (incl. ~30 Wh of travel) but has 5000 Wh."
    each(text, new RegExp(`(\\S+) would need ${NUM} Wh(?: \\(incl\\. ~${NUM} Wh of travel\\))? but has ${NUM} Wh`), (m) =>
      add({
        subject: m[1],
        kind: "robot",
        demanded: +m[2],
        budget: +m[4],
        unit: "Wh",
        note: m[3] ? `incl. ~${m[3]} Wh of travel` : null,
      }),
    );
    // solver: "EX1 has 5000 Wh, and the excavator tasks (9929.649 Wh in total (incl. ~30 Wh of
    // travel)) cannot be split across the 2 excavator robot(s) (10000 Wh combined) ..."
    each(
      text,
      new RegExp(
        `(\\S+) has ${NUM} Wh, and the (\\S+) tasks \\(${NUM} Wh in total(?: \\(incl\\. ~${NUM} Wh of travel\\))?\\) cannot be split across the (\\d+) \\S+ robot\\(s\\) \\(${NUM} Wh combined\\)`,
      ),
      (m) => {
        const p = pools.get(m[3]) ?? { robots: [], demanded: +m[4], travel: m[5] ?? null, pool: +m[7] };
        if (!p.robots.some((r) => r.id === m[1])) p.robots.push({ id: m[1], budget: +m[2] });
        pools.set(m[3], p);
      },
    );
    // validator: "excavator: 120 h over 2 robots needs >= 60 h; horizon is 48 h"
    each(text, new RegExp(`(\\S+): ${NUM} h over (\\d+) robots? needs >= ${NUM} h; horizon is ${NUM} h`), (m) =>
      add({ subject: m[1], kind: "type", demanded: +m[4], budget: +m[5], unit: "h", note: `${m[2]} h over ${robots(m[3])}` }),
    );
    // validator: "T1->T2->T5 takes 200 h in sequence; horizon is 168 h"
    each(text, new RegExp(`(\\S+) takes ${NUM} h in sequence; horizon is ${NUM} h`), (m) =>
      add({ subject: m[1], kind: "chain", demanded: +m[2], budget: +m[3], unit: "h", note: "critical path" }),
    );
    // validator hint: "The critical path T1->T2 alone takes 200 h, longer than the 168 h horizon"
    each(text, new RegExp(`The critical path (\\S+) alone takes ${NUM} h, longer than the ${NUM} h horizon`), (m) =>
      add({ subject: m[1], kind: "chain", demanded: +m[2], budget: +m[3], unit: "h", note: "critical path" }),
    );
    // solver horizon hint: "... within the 168 h horizon: the longest precedence chain alone
    // takes 200 h, the busiest robot type (excavator) needs at least 180 h"
    const horizon = /within the (\d+(?:\.\d+)?) h horizon/.exec(text);
    const deadline = horizon ? +horizon[1] : horizonH ?? null;
    if (deadline !== null) {
      each(text, new RegExp(`the longest (?:precedence chain alone|chain of work, locked starts included,) takes ${NUM} h`), (m) =>
        add({ subject: "longest chain", kind: "chain", demanded: +m[1], budget: deadline, unit: "h", note: "precedence" }),
      );
      each(text, new RegExp(`the busiest robot type \\((\\S+)\\) needs at least ${NUM} h`), (m) =>
        add({ subject: m[1], kind: "type", demanded: +m[2], budget: deadline, unit: "h", note: "busiest type" }),
      );
      each(text, new RegExp(`the locked task (\\S+) cannot end before ${NUM} h`), (m) =>
        add({ subject: m[1], kind: "task", demanded: +m[2], budget: deadline, unit: "h", note: "locked start" }),
      );
      // validator hint: "excavator work (120 h over 2 robots needs at least 60 h)"
      each(text, new RegExp(`(\\S+) work \\(${NUM} h over (\\d+) robots? needs at least ${NUM} h\\)`), (m) =>
        add({ subject: m[1], kind: "type", demanded: +m[4], budget: deadline, unit: "h", note: `${m[2]} h over ${robots(m[3])}` }),
      );
      // solver: "EX1 runs one task at a time, and the 3 task(s) only it can do already take
      // 120 h back to back plus up to 4 h of transit."
      each(text, new RegExp(`(\\S+) runs one task at a time, and the (\\d+) task\\(s\\) only it can do already take ${NUM} h back to back(?: plus up to ${NUM} h of transit)?`), (m) =>
        add({
          subject: m[1],
          kind: "robot",
          demanded: +m[3] + (m[4] ? +m[4] : 0),
          budget: deadline,
          unit: "h",
          note: `${m[2]} ${m[2] === "1" ? "task" : "tasks"} only it can do${m[4] ? `, incl. ${m[4]} h of transit` : ""}`,
        }),
      );
      // solver: "EX1 runs one task at a time; the excavator tasks total 120 h across 2 excavator robot(s) ..."
      each(text, new RegExp(`(\\S+) runs one task at a time; the (\\S+) tasks total ${NUM} h across (\\d+) \\S+ robot\\(s\\)(?: plus up to ${NUM} h of transit)?`), (m) =>
        add({
          subject: m[1],
          kind: "robot",
          demanded: +m[3] + (m[5] ? +m[5] : 0),
          budget: deadline,
          unit: "h",
          note: `${m[2]} total across ${robots(m[4])}${m[5] ? `, incl. ${m[5]} h of transit` : ""}`,
        }),
      );
      // solver: "Zone Z1 admits one robot at a time, so its 5 tasks (60 h in total) cannot overlap."
      each(text, new RegExp(`Zone (\\S+) admits one robot at a time, so its (\\d+) tasks \\(${NUM} h in total\\)`), (m) =>
        add({ subject: m[1], kind: "zone", demanded: +m[3], budget: deadline, unit: "h", note: `${m[2]} tasks, one robot at a time` }),
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
    add({
      subject: type,
      kind: "type",
      demanded: p.demanded,
      budget: p.pool,
      unit: "Wh",
      note: `cannot be split across ${across}${p.travel ? ` · incl. ~${p.travel} Wh travel` : ""}`,
    });
  }
  return rows;
}

// ------------------------------------------------------------------ repair diff
export interface DiffChip {
  task: string;
  kind: "added" | "removed" | "split" | "changed";
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
  /** "15,700 → 14,000 Wh (−10.8%) · 123 → 117 h" */
  totals: string;
}

function fmtDiffValue(field: DiffField, v: unknown, unit: string): string {
  if (v === null || v === undefined) return "none";
  if (field === "after") return Array.isArray(v) && v.length ? v.map(String).join(" + ") : "none";
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

/** A task's state on one side of the diff, for matching work across ids. */
interface TaskState {
  robot_type: string;
  zone: string;
  duration_h: number;
  energy_wh: number;
  quantity: number | null;
}

type FieldChanges = Map<DiffField, { before: unknown; after: unknown }>;

function stateSig(s: TaskState): string {
  return `${s.robot_type}|${s.zone}|${s.duration_h}|${s.energy_wh}|${s.quantity ?? ""}`;
}

/** The task as it was before the repair: its final state with the diff's `before` values put back. */
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
 * Old id -> new id for tasks the repair renumbered. The backend diff is per id; when the model
 * inserts a task and shifts every later id by one, that diff reads as a cascade of re-scopes
 * ("T9 moved to compactor, 3 -> 14 h") although the work did not change. A changed id whose
 * pre-repair state equals the final state of another added or changed id is such a shift.
 */
function detectRenumbering(changes: Map<string, FieldChanges>, diff: RepairDiff, tasks: Task[], byId: Map<string, Task>): Map<string, string> {
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

/** The per-field clauses of one changed id: "moved to excavator", "zone Z2 → Z4", ... */
function fieldParts(task: string, fields: FieldChanges, unit: string, splitChildren?: string[]): { parts: string[]; quantityDropped: boolean } {
  const parts: string[] = [];
  if (splitChildren) parts.push(`split into ${task} + ${splitChildren.join(" + ")}`);
  const rt = fields.get("robot_type");
  if (rt) parts.push(`moved to ${String(rt.after)}`);
  const zone = fields.get("zone");
  if (zone) parts.push(`zone ${String(zone.before)} → ${String(zone.after)}`);
  let quantityDropped = false;
  const q = fields.get("quantity");
  if (q) {
    parts.push(`quantity ${range("quantity", q.before, q.after, unit)}`);
    quantityDropped =
      (typeof q.before === "number" && typeof q.after === "number" && q.after < q.before) ||
      (typeof q.before === "number" && (q.after === null || q.after === undefined));
  }
  const d = fields.get("duration_h");
  if (d) parts.push(`duration ${range("duration_h", d.before, d.after, unit)}`);
  const e = fields.get("energy_wh");
  if (e) parts.push(`energy ${range("energy_wh", e.before, e.after, unit)}`);
  const after = fields.get("after");
  if (after) parts.push(`after ${range("after", after.before, after.after, unit)}`);
  return { parts, quantityDropped };
}

/** "duration 24 → 14 h · energy 4,000 → 2,100 Wh" between an old state and a final task. */
function stateDelta(old: TaskState, now: Task): { parts: string[]; quantityDropped: boolean } {
  const parts: string[] = [];
  const unit = unitLabel(now.unit);
  if (old.robot_type !== now.robot_type) parts.push(`moved to ${now.robot_type}`);
  if (old.zone !== now.zone) parts.push(`zone ${old.zone} → ${now.zone}`);
  let quantityDropped = false;
  if (old.quantity !== now.quantity) {
    parts.push(`quantity ${range("quantity", old.quantity, now.quantity, unit)}`);
    quantityDropped = typeof old.quantity === "number" && (now.quantity === null || now.quantity < old.quantity);
  }
  if (old.duration_h !== now.duration_h) parts.push(`duration ${range("duration_h", old.duration_h, now.duration_h, unit)}`);
  if (old.energy_wh !== now.energy_wh) parts.push(`energy ${range("energy_wh", old.energy_wh, now.energy_wh, unit)}`);
  return { parts, quantityDropped };
}

const workOf = (t: Pick<TaskState, "duration_h" | "energy_wh">) => `${fmtNum(t.duration_h, 1)} h · ${fmtWh(t.energy_wh)}`;

/**
 * The repair diff as chips, one per task: "T2 split into T2 + T11 · energy 4,000 → 2,100 Wh",
 * "T5 moved to excavator · zone Z2 → Z4", "T11 added", "T9 removed". A split is detected
 * when an added task's energy plus the shrunken task's new energy adds back to the old energy
 * (within 10 %) on the same robot type; `tasks` supplies names, units and robot types.
 *
 * `tasks` must be the graph THIS repair produced: the plan's final graph for the step
 * lastGraphStep names, and [] for every earlier round (and while the trace still streams), so
 * those chips come from the diff's fields verbatim. Matching a round's diff against a graph
 * that later rounds rewrote describes changes that never happened ("renumbered, work
 * unchanged" for tasks that were re-scoped) and drops the real ones.
 *
 * When the repair renumbered the graph (see detectRenumbering) the shifted ids collapse into
 * one "renumbered, work unchanged" chip and the ids whose old work is gone are matched to the
 * new work by robot type and zone: "T2 + T3 split from T2 (24 h · 4,000 Wh) into 14 h · 2,100 Wh
 * + 14 h · 2,100 Wh", "T4 re-scoped (was T3) · duration 24 → 14 h · energy 4,000 → 2,100 Wh".
 */
export function describeRepairDiff(diff: RepairDiff, tasks: Task[] = []): RepairSummary {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const changes = new Map<string, FieldChanges>();
  for (const c of diff.changed) {
    let fields = changes.get(c.task);
    if (!fields) {
      fields = new Map();
      changes.set(c.task, fields);
    }
    fields.set(c.field, { before: c.before, after: c.after });
  }

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
    const contiguous =
      pairs.length > 1 &&
      pairs.every(([o, n], i) => {
        const a = idNumber(o);
        const b = idNumber(n);
        const prev = i === 0 ? null : idNumber(pairs[i - 1][0]);
        return a !== null && b === a + 1 && (prev === null || a === prev + 1);
      });
    const label = contiguous
      ? `${pairs[0][0]}–${pairs[pairs.length - 1][0]} → ${pairs[0][1]}–${pairs[pairs.length - 1][1]}`
      : pairs.map(([o, n]) => `${o}→${n}`).join(" ");
    // listed after the substantive changes: it is the least interesting chip
    const renumberedChip: DiffChip = {
      task: label,
      kind: "changed",
      text: `${plural(pairs.length, "task")} renumbered, work unchanged`,
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
      const group = old ? tasks.filter((t) => free.has(t.id) && t.robot_type === old.robot_type && t.zone === old.zone) : [];
      if (!old || group.length === 0) {
        const { parts, quantityDropped: dropped } = fieldParts(id, fields, unitLabel(task?.unit));
        noteDrop(dropped, id);
        chips.push({ task: id, kind: "changed", text: parts.join(" · "), title: `${id}${task ? ` ${task.name}` : ""}: ${parts.join("; ")}` });
        handled.add(id);
        continue;
      }
      const sum = group.reduce((acc, t) => acc + t.energy_wh, 0);
      if (group.length > 1 && Math.abs(sum - old.energy_wh) <= Math.max(1, 0.1 * old.energy_wh)) {
        const ids = group.map((t) => t.id);
        chips.push({
          task: ids.join(" + "),
          kind: "split",
          text: `split from ${id} (${workOf(old)}) into ${group.map(workOf).join(" + ")}`,
          title: group.map((t) => `${t.id} ${t.name} (${workOf(t)})`).join("; "),
        });
        for (const t of group) {
          free.delete(t.id);
          handled.add(t.id);
        }
        continue;
      }
      const now = group.find((t) => t.id === id) ?? group[0];
      let words: string[];
      if (now.id === id) {
        // the id kept its work: the diff's own fields, with the task's unit
        const { parts, quantityDropped: dropped } = fieldParts(id, fields, unitLabel(now.unit));
        noteDrop(dropped, id);
        words = parts;
      } else {
        // the old work went on under another id: what changed on the way, the edges included
        // when the diff recorded the old ones
        const { parts, quantityDropped: dropped } = stateDelta(old, now);
        const edges = fields.get("after");
        if (edges && Array.isArray(edges.before)) {
          const was = edges.before.map(String).sort();
          const is = [...now.after].sort();
          if (was.length !== is.length || was.some((x, k) => x !== is[k])) {
            parts.push(`after ${range("after", edges.before, now.after, "")}`);
          }
        }
        noteDrop(dropped, now.id);
        const scoped = parts.some((x) => /^(duration|energy|quantity)/.test(x));
        words = [`${scoped ? "re-scoped" : "re-issued"} (was ${id})`, ...parts];
      }
      chips.push({
        task: now.id,
        kind: "changed",
        text: words.join(" · "),
        title: `${now.id} ${now.name}: ${words.join("; ")}`,
      });
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
        if (parent && parent.robot_type !== child.robot_type) continue;
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
      const { parts, quantityDropped: dropped } = fieldParts(task, fields, unitLabel(byId.get(task)?.unit), children);
      noteDrop(dropped, task);
      const name = byId.get(task)?.name;
      chips.push({
        task,
        kind: children ? "split" : "changed",
        text: parts.join(" · "),
        title: `${task}${name ? ` ${name}` : ""}: ${parts.join("; ")}`,
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
      title: t ? `${id} ${t.name} (${t.robot_type}, ${fmtNum(t.duration_h, 1)} h, ${fmtNum(t.energy_wh, 0)} Wh)` : `${id} added by the repair`,
    });
  }
  for (const id of diff.removed) {
    chips.push({ task: id, kind: "removed", text: "removed", title: `${id} removed by the repair` });
  }

  const drift = Number.isFinite(diff.scope_drift_pct) ? diff.scope_drift_pct : 0;
  let scopeReason: string | null = null;
  if (drift < -25) scopeReason = `total energy ${fmtSignedPct(drift)}: work was re-scoped away`;
  else if (quantityDropped) scopeReason = `quantity reduced on ${quantityDropped}`;
  const totals = `${fmtNum(diff.total_energy_wh_before, 0)} → ${fmtNum(diff.total_energy_wh_after, 0)} Wh (${fmtSignedPct(drift)}) · ${fmtNum(diff.total_duration_h_before, 0)} → ${fmtNum(diff.total_duration_h_after, 0)} h`;
  return { chips, scopeReview: scopeReason !== null, scopeReason, drift, totals };
}
