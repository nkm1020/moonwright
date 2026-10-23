"use client";
/**
 * Solver-proven advice ("What would make it feasible"): the resource changes the backend
 * re-solved with CP-SAT when no repair could make the last task graph feasible (types.ts
 * Suggestion, backend moonwright/pipeline/relax.py). One row per suggestion: its kind ("later
 * deadline", "one more robot", "bigger battery", "smaller scope"), the detail sentence (its
 * words verbatim, its numbers in the console's format) and a chip that says whether a CP-SAT
 * solve on the modified requirements proved it ("CP-SAT verified", green) or not ("not
 * solver-verified", muted: the lone reduce_scope).
 *
 * Variants: `trace` is the compact block under the core block of the final failed step (no
 * footer); `outcome` is the card of a failed plan's verdict column: the heading row, the best
 * proven change as the one big figure ("86 h · later deadline · 26 h past the 60 h deadline"),
 * the rows and the footer, on the raised card surface so the way out outranks the red verdict
 * next to it; `replan` is compact with the time-base note of a failed replan (its hours are
 * absolute simulator time against the brief's deadline). An empty list renders nothing. The
 * block takes the verified green only when at least one change was proved.
 */
import { useId } from "react";
import type { Suggestion, SuggestionKind } from "@/lib/types";
import { SUGGESTION_COPY } from "@/lib/types";
import { fmtHours, fmtSuggestion, fmtWh, plural, suggestionKindLabel, suggestionProofLabel } from "@/lib/format";
import { Badge, IconCheck, Stat, cx } from "./Section";

export type SuggestionsVariant = "trace" | "outcome" | "replan";
export type SuggestionsTimeBase = "absolute" | "relative";

export interface SuggestionsProps {
  suggestions: Suggestion[];
  /** trace: compact, no footer (default); outcome: heading row, the big figure of the best
   *  proven change, and the footer; replan: compact with the time-base note. */
  variant?: SuggestionsVariant;
  /** Time base of the hours in the sentences: "absolute" (simulator hours against the brief's
   *  deadline: every list the backend emits, main plan or replan) or "relative" (planner hours
   *  of a replan recorded without the flag; the note then names the offset). */
  timeBase?: SuggestionsTimeBase;
  /** Planner time 0 of a replan in simulator hours (its offset_h), for the relative note. */
  offsetH?: number;
  /** The deadline the failed plan had (requirements.horizon_h): the outcome card's figure then
   *  says how far past it a later deadline goes. */
  horizonH?: number | null;
  className?: string;
}

/** "extend_horizon" -> "later deadline" (format.ts suggestionKindLabel). */
export function kindLabel(kind: SuggestionKind | string): string {
  return suggestionKindLabel(kind);
}

const VERIFIED_TITLE = "A CP-SAT solve of the last task graph with this one change found a schedule";
const UNVERIFIED_TITLE =
  "Not re-solved: no single resource change verified, so this names the work a human could cut or defer";

export interface SuggestionStat {
  /** the kind, e.g. "later deadline" */
  label: string;
  /** the figure: "86 h", "5,950 Wh", "+1 excavator" */
  value: string;
  /** the rest of the numbers: "26 h past the 60 h deadline", "on EX1 · feasible at 62 h" */
  sub: string;
}

/** The one figure of a proven change, for the outcome card's stat: null for reduce_scope, an
 *  unverified change or a suggestion without its numbers. */
export function suggestionStat(s: Suggestion, horizonH?: number | null): SuggestionStat | null {
  if (!s.verified) return null;
  const at = s.makespan_h !== null && Number.isFinite(s.makespan_h) ? `feasible at ${fmtHours(s.makespan_h, 0)}` : null;
  switch (s.kind) {
    case "extend_horizon": {
      const h = s.horizon_h ?? s.makespan_h;
      if (h === null || !Number.isFinite(h)) return null;
      const known = horizonH !== null && horizonH !== undefined && Number.isFinite(horizonH);
      const sub = known && h > horizonH ? `${fmtHours(h - horizonH, 0)} past the ${fmtHours(horizonH, 0)} deadline` : "the deadline that works";
      return { label: kindLabel(s.kind), value: fmtHours(h, 0), sub };
    }
    case "raise_battery": {
      if (s.battery_wh === null) return null;
      const sub = [s.robot_id ? `on ${s.robot_id}` : null, at].filter((x): x is string => x !== null).join(" · ");
      return { label: kindLabel(s.kind), value: fmtWh(s.battery_wh), sub };
    }
    case "add_robot": {
      if (s.robot_type === null) return null;
      const who = s.robot_id ? `${s.robot_id}${s.battery_wh !== null ? `, ${fmtWh(s.battery_wh)}` : ""}` : null;
      const sub = [who, at].filter((x): x is string => x !== null).join(" · ");
      return { label: kindLabel(s.kind), value: `+1 ${s.robot_type}`, sub };
    }
    default:
      return null;
  }
}

/** The line about the hours in the sentences, when one is due. */
function timeBaseNote(variant: SuggestionsVariant, timeBase: SuggestionsTimeBase, offsetH: number | undefined): string | null {
  if (timeBase === "relative") {
    return offsetH !== undefined && Number.isFinite(offsetH)
      ? `Hours count from the replan at ${fmtHours(offsetH, 0)}: add ${fmtHours(offsetH, 0)} to read them on the mission clock`
      : "Hours count from the replan, not from the start of the mission";
  }
  return variant === "replan" ? SUGGESTION_COPY.replanNote : null;
}

export function Suggestions({
  suggestions,
  variant = "trace",
  timeBase = "absolute",
  offsetH,
  horizonH = null,
  className,
}: SuggestionsProps) {
  const headingId = useId();
  if (suggestions.length === 0) return null;
  const proven = suggestions.filter((s) => s.verified).length;
  const note = timeBaseNote(variant, timeBase, offsetH);
  const ranked = suggestions.length > 1;

  if (variant === "outcome") {
    // the list is best first: the first proven change is the figure of the card
    const best = suggestions.find((s) => s.verified) ?? null;
    const stat = best ? suggestionStat(best, horizonH) : null;
    return (
      <section
        aria-labelledby={headingId}
        className={cx("rounded-lg border bg-raised", proven > 0 ? "border-ok/30" : "border-line", className)}
      >
        <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-line px-4 py-2.5">
          <h4 id={headingId} className="inline-flex items-center gap-2 text-[14px] font-semibold text-fg">
            {proven === suggestions.length && (
              <span aria-hidden="true" className="flex size-4 items-center justify-center rounded-full bg-ok/15 text-ok">
                <IconCheck className="size-2.5" />
              </span>
            )}
            {SUGGESTION_COPY.heading}
          </h4>
          {ranked && (
            <span className="text-[11px] text-dim">
              {plural(suggestions.length, "option")} · best first
            </span>
          )}
        </header>
        {stat && (
          <div className="border-b border-line px-4 py-3">
            <Stat label={stat.label} value={stat.value} sub={`${stat.sub} · ${SUGGESTION_COPY.verified}`} tone="ok" />
          </div>
        )}
        <ol className="divide-y divide-white/5 px-4">
          {suggestions.map((s, i) => (
            <Row key={`${s.kind}-${i}`} suggestion={s} rank={ranked ? i + 1 : null} className="py-2" />
          ))}
        </ol>
        <footer className="border-t border-line px-4 py-2 text-[11px] leading-4 text-dim">
          {SUGGESTION_COPY.footer}
          {note ? ` · ${note}` : ""}
        </footer>
      </section>
    );
  }

  return (
    <div
      role="group"
      aria-labelledby={headingId}
      className={cx(
        "rounded-r-md border-l-[3px] py-2 pl-3.5 pr-3",
        proven > 0 ? "border-ok bg-ok/[0.06]" : "border-white/25 bg-white/[0.03]",
        className,
      )}
    >
      <div
        id={headingId}
        className={cx("mb-1.5 text-[11px] font-medium uppercase tracking-wider", proven > 0 ? "text-ok" : "text-muted")}
      >
        {SUGGESTION_COPY.heading}
        {ranked && (
          <span className="ml-1.5 normal-case tracking-normal text-dim">
            · {plural(suggestions.length, "option")}, best first
          </span>
        )}
      </div>
      <ol className="space-y-1.5">
        {suggestions.map((s, i) => (
          <Row key={`${s.kind}-${i}`} suggestion={s} rank={ranked ? i + 1 : null} />
        ))}
      </ol>
      {note && <p className="mt-1.5 text-[11px] leading-4 text-dim">{note}</p>}
    </div>
  );
}

/** Kind (icon and label), the sentence, the proof chip. The label column is fixed so the
 *  sentences line up; in a narrow column the sentence wraps under the label. */
function Row({ suggestion: s, rank, className }: { suggestion: Suggestion; rank: number | null; className?: string }) {
  return (
    <li className={cx("flex flex-wrap items-start gap-x-2.5 gap-y-0.5 text-[13px] leading-5", className)}>
      {rank !== null && (
        <span aria-hidden="true" className="w-3 shrink-0 font-mono text-[11px] leading-5 tabular-nums text-dim">
          {rank}
        </span>
      )}
      <span className="inline-flex w-[7.25rem] shrink-0 items-center gap-1.5 font-medium text-fg">
        <KindIcon kind={s.kind} className="size-3.5 text-dim" />
        {kindLabel(s.kind)}
      </span>
      <span className="min-w-[14rem] flex-1 text-fg/90">{fmtSuggestion(s)}</span>
      <Badge tone={s.verified ? "ok" : "neutral"} title={s.verified ? VERIFIED_TITLE : UNVERIFIED_TITLE} className="shrink-0">
        {suggestionProofLabel(s)}
      </Badge>
    </li>
  );
}

const STROKE = { stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round", strokeLinejoin: "round" } as const;

/** One glyph per kind: a clock (later deadline), a plus (one more robot), a battery with a
 *  plus (bigger battery), a minus (smaller scope). */
function KindIcon({ kind, className }: { kind: SuggestionKind | string; className?: string }) {
  const cls = cx("shrink-0", className ?? "size-3.5");
  switch (kind) {
    case "extend_horizon":
      return (
        <svg className={cls} viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="8" cy="8" r="6" {...STROKE} />
          <path d="M8 4.75V8l2.4 1.6" {...STROKE} />
        </svg>
      );
    case "add_robot":
      return (
        <svg className={cls} viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="8" cy="8" r="6" {...STROKE} />
          <path d="M8 5.25v5.5M5.25 8h5.5" {...STROKE} />
        </svg>
      );
    case "raise_battery":
      return (
        <svg className={cls} viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <rect x="1.75" y="4.75" width="10.5" height="6.5" rx="1.5" {...STROKE} />
          <path d="M14.25 7v2M7 6.25v3.5M5.25 8h3.5" {...STROKE} />
        </svg>
      );
    case "reduce_scope":
      return (
        <svg className={cls} viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="8" cy="8" r="6" {...STROKE} />
          <path d="M5.25 8h5.5" {...STROKE} />
        </svg>
      );
    default:
      return (
        <svg className={cls} viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="8" cy="8" r="6" {...STROKE} />
        </svg>
      );
  }
}
