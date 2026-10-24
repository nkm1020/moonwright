"use client";
/**
 * Outcome attribution of a Monte Carlo result: MonteCarloResult.outcomes folded into five
 * families (completed / late / lost every robot of a type / replan proved infeasible / stuck)
 * as a stacked bar with a legend, plus the sentence that says where the misses came from
 * ("71 of 82 misses came from a robot with no backup: SV1, CP1, PR1"), generated from the
 * data. Colours are the semantic tokens of globals.css (ok / warn / fail), never robot colours:
 * the bar answers "did it finish, and if not why", not "which robot".
 */
import { useState } from "react";
import type { MonteCarloResult, Robot } from "@/lib/types";
import { CONFLICT_PREFIXES } from "@/lib/types";
import { plural, roundShares } from "@/lib/format";

export type OutcomeFamily = "completed" | "late" | "lost_only" | "replan_infeasible" | "stuck";

/** Display order of the segments and the legend: good to bad. */
export const FAMILY_ORDER: readonly OutcomeFamily[] = [
  "completed",
  "late",
  "lost_only",
  "replan_infeasible",
  "stuck",
];

interface FamilyStyle {
  label: string;
  background: string;
  /** colour of a label drawn inside the fill, picked by the fill's luminance */
  ink: string;
}

/** Replan-infeasible shares the failure hue with "lost every robot of a type" (both are misses
 *  of the deadline) and is told apart by a 45° hatch, so the bar keeps a single red. */
const HATCH =
  "repeating-linear-gradient(45deg, var(--fail) 0 3px, color-mix(in oklab, var(--fail) 30%, var(--raised)) 3px 6px)";

export const FAMILY_STYLE: Record<OutcomeFamily, FamilyStyle> = {
  completed: { label: "Completed within the horizon", background: "var(--ok)", ink: "#0b0f14" },
  late: { label: "Finished late", background: "var(--warn)", ink: "#0b0f14" },
  lost_only: { label: "Lost every robot of a type", background: "var(--fail)", ink: "#ffffff" },
  replan_infeasible: { label: "Replan proved infeasible", background: HATCH, ink: "#ffffff" },
  stuck: { label: "Stuck", background: "#64748b", ink: "#ffffff" },
};

/** "lost_only:printer,surveyor" -> ["lost_only", ["printer", "surveyor"]]. */
function splitKey(key: string): [string, string[]] {
  const i = key.indexOf(":");
  if (i < 0) return [key, []];
  return [
    key.slice(0, i),
    key
      .slice(i + 1)
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  ];
}

export function outcomeFamily(key: string): OutcomeFamily {
  const [prefix] = splitKey(key);
  return (FAMILY_ORDER as readonly string[]).includes(prefix) ? (prefix as OutcomeFamily) : "stuck";
}

/** "a" / "a and b" / "a, b and c". */
function joinWords(words: string[], conjunction: "and" | "or"): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} ${conjunction} ${words[words.length - 1]}`;
}

/** One line for a MonteCarloSample.outcome, e.g. "no printer or surveyor left for the
 *  remaining work" or "replan proved infeasible: battery budget exceeded". */
export function describeOutcome(key: string): string {
  const [prefix, rest] = splitKey(key);
  switch (prefix) {
    case "completed":
      return "completed within the horizon";
    case "late":
      return "finished after the horizon";
    case "lost_only":
      return rest.length
        ? `no ${joinWords(rest, "or")} left for the remaining work`
        : "a robot type died out with work remaining";
    case "replan_infeasible":
      return `replan proved infeasible: ${rest.map((k) => CONFLICT_PREFIXES[k] ?? k).join("; ") || "no conflict named"}`;
    case "stuck":
      return "stuck: replan budget spent or a battery ran dry";
    default:
      return key;
  }
}

/** result.outcomes, or the same table rebuilt from the samples for a result recorded before
 *  the backend counted outcomes. */
export function outcomeCounts(result: MonteCarloResult): Record<string, number> {
  if (result.outcomes && Object.keys(result.outcomes).length > 0) return result.outcomes;
  const counts: Record<string, number> = {};
  for (const s of result.samples) {
    const key = s.outcome || (s.completed ? (s.within_horizon ? "completed" : "late") : "stuck");
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

export interface OutcomePart {
  key: string;
  count: number;
  detail: string;
}

export interface OutcomeSlice {
  family: OutcomeFamily;
  count: number;
  /** count / runs */
  share: number;
  /** the raw outcome keys folded into this family, most frequent first */
  parts: OutcomePart[];
}

/** The families in display order; empty ones are dropped except "completed", which is the
 *  headline and stays visible at zero. */
export function outcomeSlices(outcomes: Record<string, number>, runs: number): OutcomeSlice[] {
  const slices: OutcomeSlice[] = FAMILY_ORDER.map((family) => ({ family, count: 0, share: 0, parts: [] }));
  for (const [key, count] of Object.entries(outcomes)) {
    if (!(count > 0)) continue;
    const slice = slices[FAMILY_ORDER.indexOf(outcomeFamily(key))];
    slice.count += count;
    slice.parts.push({ key, count, detail: describeOutcome(key) });
  }
  for (const s of slices) {
    s.parts.sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
    s.share = runs > 0 ? s.count / runs : 0;
  }
  return slices.filter((s) => s.count > 0 || s.family === "completed");
}

/** Runs that finished within the horizon (the samples when they are all there). */
export function withinHorizonCount(result: MonteCarloResult): number {
  if (result.samples.length === result.runs) return result.samples.filter((s) => s.within_horizon).length;
  return Math.round(result.within_horizon_rate * result.runs);
}

export interface SpofRow {
  /** a robot type the fleet has exactly one robot of */
  type: string;
  /** its robot id(s) in the fleet (one, by definition) */
  robots: string[];
  /** misses in which that type died out with work left (a run can count for two types) */
  misses: number;
}

export interface Attribution {
  runs: number;
  within: number;
  misses: number;
  /** misses in which a single-point type died out: the misses a spare would address */
  noBackup: number;
  /** every single point of failure, most misses first */
  spof: SpofRow[];
  /** "71 of 82 misses came from a robot with no backup: SV1, CP1, PR1." */
  lead: string;
  /** "The other 11: 9 where CP-SAT proved the remaining work infeasible (energy), 2 lost
   *  every hauler." or null when nothing is left to explain */
  rest: string | null;
}

export function attributeOutcomes(result: MonteCarloResult, robots: Robot[]): Attribution {
  const outcomes = outcomeCounts(result);
  const spofTypes = result.single_points_of_failure ?? [];
  const spof = new Set(spofTypes);
  const within = withinHorizonCount(result);
  const misses = result.runs - within;
  const perType = new Map<string, number>(spofTypes.map((t) => [t, 0]));
  const otherLost: { types: string[]; count: number }[] = [];
  const kinds = new Set<string>();
  let noBackup = 0;
  let infeasible = 0;
  let late = 0;
  let stuck = 0;
  for (const [key, count] of Object.entries(outcomes)) {
    if (!(count > 0)) continue;
    const [prefix, rest] = splitKey(key);
    if (prefix === "lost_only") {
      const hit = rest.filter((t) => spof.has(t));
      if (hit.length > 0) {
        noBackup += count;
        for (const t of hit) perType.set(t, (perType.get(t) ?? 0) + count);
      } else {
        otherLost.push({ types: rest, count });
      }
    } else if (prefix === "replan_infeasible") {
      infeasible += count;
      for (const k of rest) kinds.add(k);
    } else if (prefix === "late") {
      late += count;
    } else if (prefix !== "completed") {
      stuck += count;
    }
  }
  const rows: SpofRow[] = spofTypes
    .map((type) => ({
      type,
      robots: robots.filter((r) => r.type === type).map((r) => r.id),
      misses: perType.get(type) ?? 0,
    }))
    .sort((a, b) => b.misses - a.misses || a.type.localeCompare(b.type));
  const culprits = rows.filter((r) => r.misses > 0).flatMap((r) => (r.robots.length ? r.robots : [r.type]));

  let lead: string;
  if (misses === 0) {
    lead = `Every one of the ${result.runs} runs finished within the horizon.`;
  } else if (noBackup > 0) {
    const share =
      misses === 1 ? "The only miss" : noBackup === misses ? `All ${misses} misses` : `${noBackup} of ${misses} misses`;
    // the chips count a run once per single-point type it lost; say so when the sums differ
    const overlap = rows.reduce((acc, r) => acc + r.misses, 0) - noBackup;
    lead = `${share} came from a robot with no backup: ${culprits.join(", ")}${
      overlap > 0 ? `; ${overlap} of those runs lost two of them` : ""
    }.`;
  } else {
    lead = `None of the ${plural(misses, "miss", "misses")} came from a robot with no backup.`;
  }

  const clauses: string[] = [];
  if (otherLost.length > 2) {
    // many type combinations: one clause with the per-type tally instead of a list
    const total = otherLost.reduce((acc, c) => acc + c.count, 0);
    const tally = new Map<string, number>();
    for (const c of otherLost) for (const t of c.types) tally.set(t, (tally.get(t) ?? 0) + c.count);
    const detail = [...tally]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([t, n]) => `${t} ${n}`)
      .join(", ");
    // the tally counts a run once per type it lost; say so when it adds up to more than total
    const multi = otherLost.filter((c) => c.types.length > 1);
    const multiRuns = multi.reduce((acc, c) => acc + c.count, 0);
    const overlap =
      multiRuns > 0
        ? `; ${plural(multiRuns, "run")} lost ${multi.every((c) => c.types.length === 2) ? "two types" : "more than one type"}`
        : "";
    clauses.push(`${total} lost every robot of a type (types involved: ${detail}${overlap})`);
  } else {
    for (const c of otherLost) clauses.push(`${c.count} lost every ${joinWords(c.types, "and")}`);
  }
  if (infeasible > 0) {
    clauses.push(
      `${infeasible} where CP-SAT proved the remaining work infeasible (${[...kinds].sort().map((k) => CONFLICT_PREFIXES[k] ?? k).join("; ") || "no conflict named"})`,
    );
  }
  if (late > 0) clauses.push(`${late} finished late`);
  if (stuck > 0) clauses.push(`${stuck} stuck`);
  const others = misses - noBackup;
  const rest =
    others > 0 && clauses.length > 0
      ? `${noBackup > 0 ? `The other ${others}` : "Instead"}: ${clauses.join(", ")}.`
      : null;
  return { runs: result.runs, within, misses, noBackup, spof: rows, lead, rest };
}

// ------------------------------------------------------------------ component
export interface OutcomeBarProps {
  result: MonteCarloResult;
  title?: string;
  className?: string;
}

/** Stacked part-to-whole bar of the outcome families with a 2 px surface gap between
 *  segments, a share label inside every segment wide enough to hold it, a legend with counts
 *  (the table twin of the bar) and a hover / focus readout of the raw outcomes. */
export function OutcomeBar({ result, title = "Outcomes", className }: OutcomeBarProps) {
  const [hover, setHover] = useState<OutcomeFamily | null>(null);
  const slices = outcomeSlices(outcomeCounts(result), result.runs);
  // integer shares that add up to 100 (independent rounding prints 59 + 37 + 5 = 101)
  const pct = roundShares(slices.map((s) => s.count), result.runs);
  // a family with runs never reads 0% (1 of 200 runs reads "<1%")
  const share = (i: number) => (slices[i].count > 0 && pct[i] === 0 ? "<1%" : `${pct[i]}%`);
  const hovered = hover === null ? null : (slices.find((s) => s.family === hover) ?? null);
  return (
    <div className={className}>
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] font-medium">{title}</h3>
        <span className="text-[11px] text-dim">{plural(result.runs, "run")}</span>
      </div>
      <div className="flex h-5 w-full gap-0.5 overflow-hidden rounded" onMouseLeave={() => setHover(null)}>
        {slices.map((s, i) => {
            if (s.count === 0) return null;
            const style = FAMILY_STYLE[s.family];
            const dimmed = hover !== null && hover !== s.family;
            const text = `${style.label}: ${plural(s.count, "run")} (${share(i)})`;
            return (
              <div
                key={s.family}
                role="img"
                aria-label={text}
                title={text}
                tabIndex={0}
                style={{
                  flexGrow: s.count,
                  flexBasis: 0,
                  background: style.background,
                  color: style.ink,
                  opacity: dimmed ? 0.45 : 1,
                }}
                className="flex min-w-[3px] items-center justify-center text-[11px] font-semibold leading-none transition-opacity focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
                onMouseEnter={() => setHover(s.family)}
                onFocus={() => setHover(s.family)}
                onBlur={() => setHover(null)}
              >
                {s.share >= 0.1 && <span className="px-1">{pct[i]}%</span>}
              </div>
            );
          })}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
        {slices.map((s, i) => (
          <li key={s.family} className="inline-flex items-center gap-1.5 text-muted">
            <span
              aria-hidden="true"
              className="inline-block size-2.5 shrink-0 rounded-sm"
              style={{ background: FAMILY_STYLE[s.family].background }}
            />
            <span>{FAMILY_STYLE[s.family].label}</span>
            <span className="font-mono tabular-nums text-fg">{s.count}</span>
            <span className="font-mono tabular-nums text-dim">{share(i)}</span>
          </li>
        ))}
      </ul>
      <p className="mt-1 min-h-4 text-[11px] text-dim">
        {hovered
          ? hovered.parts.map((p) => `${p.count} ${p.detail}`).join(" · ")
          : "hover a segment for the outcomes behind it"}
      </p>
    </div>
  );
}
