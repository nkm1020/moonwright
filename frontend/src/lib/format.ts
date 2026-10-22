/**
 * Formatting helpers shared by the shell components. Hours are the planner's unit (integer in
 * schedules, float in the simulator); energy is Wh; token counts come from LoopStep.usage.
 */
import { CONFLICT_PREFIXES, conflictPrefix, type LoopStep, type Structure } from "./types";

const NON_DIMENSION_KEYS = new Set(["id", "type", "zone"]);

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
    if (typeof v === "number") out.push([k, v]);
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

/** "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B" -> "Nemotron-3-Super-120B-A12B"; the offline
 *  client reports "fixture:<file>" as its model, which reads as "offline fixture". */
export function shortModel(model: string): string {
  if (/^fixture:/i.test(model)) return "offline fixture";
  const tail = model.split("/").pop() ?? model;
  return tail.replace(/^NVIDIA-/i, "");
}

export interface UsageTotal {
  calls: number;
  input: number;
  output: number;
  latency: number;
  model: string | null;
}

/** Sum of the LLM usage over a trace (steps without usage are solver/validator stages). */
export function totalUsage(steps: LoopStep[]): UsageTotal {
  const t: UsageTotal = { calls: 0, input: 0, output: 0, latency: 0, model: null };
  for (const s of steps) {
    if (!s.usage) continue;
    t.calls += 1;
    t.input += s.usage.input_tokens;
    t.output += s.usage.output_tokens;
    t.latency += s.usage.latency_s;
    if (!t.model && s.usage.model) t.model = s.usage.model;
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
