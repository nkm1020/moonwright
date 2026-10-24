"use client";
/**
 * Step 01: the brief. A three-tile strip says what the console does (propose, prove / repair,
 * stress), example cards load a brief, the textarea holds plain prose (markdown markers are
 * stripped) and the primary action structures it with Nemotron. The landing-pad example is
 * pre-filled once the examples arrive and the textarea is still empty. A card with a bundled
 * recording says what that run did, read from its PlanResult; in the static demo the cards are
 * the recordings, the textarea is read-only and the primary action replays the structuring.
 */
import { useEffect, useRef, type KeyboardEvent } from "react";
import type { ExampleBrief, PlanResult, Requirements } from "@/lib/types";
import {
  conflictPhrases,
  fmtHours,
  fmtNum,
  fmtSeconds,
  isFixtureModel,
  modelDisplayName,
  plural,
  runOutcome,
  type RunOutcome,
} from "@/lib/format";
import { Badge, Button, Card, Notice, Section, Spinner, cx } from "./Section";

export interface BriefFormProps {
  brief: string;
  /** Example id sent as brief_id (recorded answers are keyed by it); null for a free brief. */
  briefId: string | null;
  onBriefChange: (text: string) => void;
  examples: ExampleBrief[];
  examplesLoading: boolean;
  /** Shown next to the cards, e.g. when the examples came from the bundled samples. */
  examplesNote: string | null;
  onUseExample: (example: ExampleBrief) => void;
  onStructure: () => void;
  structuring: boolean;
  error: string | null;
  disabled?: boolean;
  /** Additive: the backend replays recorded answers keyed by brief_id (llm=fixture). Shows the
   *  "replays recorded answers for <id>" note; omitted (live mode) = hidden. */
  offline?: boolean;
  /** Additive: example pre-filled when the textarea is empty on first load. */
  defaultExampleId?: string;
  /** Additive: the bundled recordings by example id; a card with one shows that run's outcome
   *  and its fleet, structures and horizon as the recording has them. */
  recordings?: Partial<Record<string, PlanResult>>;
  /** Additive: the static demo replays recordings: the textarea is read-only, this reason
   *  takes the place of the shortcut hint, and the primary action replays the structuring of
   *  the recording on screen. Null or absent: a live console. */
  recorded?: string | null;
  /** Additive: the static demo's walk is past the brief, so the loaded card starts it again
   *  (and says so); before that the loaded card does nothing. */
  restartable?: boolean;
}

const PLACEHOLDER = `Site. LS-1 plateau near the lunar south pole, 80 m by 80 m of loose regolith; depot on the south edge; no recharging.
Structures. 1. Landing pad: circular, 20 m diameter, excavate 0.4 m, compact, sinter a 30 cm surface. 2. Blast berm: 40 m arc, 1.5 m high and 4 m wide at the base, built from pad spoil.
Fleet. EX1 and EX2 (excavators, 5000 Wh), HL1 and HL2 (haulers, 3000 Wh), CP1 (compactor, 3500 Wh), PR1 (printer, 4000 Wh), SV1 (surveyor, 1500 Wh).
Deadline. 14 days. Only one robot per zone at a time.`;

/** Mirrors the backend's strip_markdown: ATX heading markers, **bold** / __bold__, *em* /
 *  _em_ (word-bounded, so landing_pad survives) and inline `code` are removed; lists,
 *  numbering and line breaks stay. */
export function stripMarkdown(text: string): string {
  return text
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")
    .replace(/(\*\*|__)(?=\S)([\s\S]+?)(?<=\S)\1/g, "$2")
    .replace(/(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])/g, "$1")
    .replace(/(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])/g, "$1")
    .replace(/`([^`\n]+)`/g, "$1");
}

function plainExample(ex: ExampleBrief): ExampleBrief {
  const text = stripMarkdown(ex.text);
  return text === ex.text ? ex : { ...ex, text };
}

// ------------------------------------------------------------------ mechanism strip
const MECHANISM: { title: string; text: string; glyph: "propose" | "prove" | "stress" }[] = [
  {
    title: "Nemotron proposes",
    text: "Nemotron structures the brief and proposes a task graph with quantities",
    glyph: "propose",
  },
  {
    title: "CP-SAT proves, Nemotron repairs",
    text:
      "A rate model prices it; CP-SAT proves it against five constraint families or names the conflict and Nemotron repairs it",
    glyph: "prove",
  },
  {
    title: "The simulator stresses it",
    text: "The simulator injects failures, the agent replans, Monte Carlo gives P(on time)",
    glyph: "stress",
  },
];

function Glyph({ kind }: { kind: "propose" | "prove" | "stress" }) {
  const common = { className: "size-4 shrink-0 text-muted", viewBox: "0 0 16 16", fill: "none", "aria-hidden": true as const };
  if (kind === "propose") {
    // three task nodes with edges: a graph
    return (
      <svg {...common}>
        <path d="M4 4.5h5M9 4.5l-5 7M9 4.5l3 7" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        <circle cx="3.5" cy="4.5" r="2" fill="currentColor" />
        <circle cx="9.5" cy="4.5" r="2" fill="currentColor" />
        <circle cx="3.5" cy="11.5" r="2" fill="currentColor" />
        <circle cx="12.5" cy="11.5" r="2" fill="currentColor" />
      </svg>
    );
  }
  if (kind === "prove") {
    // a check inside a shield
    return (
      <svg {...common}>
        <path d="M8 1.5l5.5 2v4.2c0 3.1-2.3 5.4-5.5 6.8C4.8 13.1 2.5 10.8 2.5 7.7V3.5L8 1.5z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        <path d="M5.3 8l1.9 1.9 3.6-3.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  // histogram bars with a marker: Monte Carlo
  return (
    <svg {...common}>
      <path d="M2 13.5h12" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      <rect x="3" y="8" width="2.2" height="5" fill="currentColor" />
      <rect x="6.4" y="4" width="2.2" height="9" fill="currentColor" />
      <rect x="9.8" y="6.5" width="2.2" height="6.5" fill="currentColor" />
      <path d="M12.8 2.5v11" stroke="currentColor" strokeWidth="1.2" strokeDasharray="2 1.5" />
    </svg>
  );
}

// ------------------------------------------------------------------ example cards
interface ExampleMeta {
  fleet: string;
  structures: string;
  horizon: string | null;
}

/** "7 robots · 5 types", "landing pad + blast berm", "14 d" (a 60 h window stays "60 h"): a
 *  recording's own requirements. */
function recordedMeta(req: Requirements): ExampleMeta {
  const types = new Set(req.robots.map((r) => r.type));
  const days = req.horizon_h >= 48 && req.horizon_h % 24 === 0;
  return {
    fleet: `${plural(req.robots.length, "robot")} · ${plural(types.size, "type")}`,
    structures: req.structures.map((s) => s.type.replace(/_/g, " ")).join(" + ") || "no structures",
    horizon: days ? `${fmtNum(req.horizon_h / 24, 0)} d` : fmtHours(req.horizon_h, 0),
  };
}

/** Robot ids like EX1 / HL2, numbered structures and the deadline, read from the prose (a
 *  brief without a recording). */
function deriveMeta(text: string): ExampleMeta {
  const robots = new Set(text.match(/\b[A-Z]{2}\d\b/g) ?? []);
  const structures = (text.match(/^\s*\d+\.\s+\S/gm) ?? []).length;
  return {
    fleet: robots.size > 0 ? `${robots.size} robots` : "fleet in text",
    structures: structures > 0 ? `${structures} structure${structures === 1 ? "" : "s"}` : "structures in text",
    horizon: deriveHorizon(text),
  };
}

function deriveHorizon(text: string): string | null {
  const m = /(\d+(?:\.\d+)?)\s*(days?|d|hours?|h)\b/i.exec(text.match(/deadline[^\n]*/i)?.[0] ?? "");
  if (!m) return null;
  const n = Number(m[1]);
  return /^d/i.test(m[2]) ? `${n} d` : `${n} h`;
}

/** What a card's recorded run did, every figure read from its PlanResult: "rejected
 *  energy:printer → 2 repair rounds → verified, 72 h of 336 h"; a core's precedence edges are
 *  counted in one phrase ("10 precedence edges"), and an id never breaks inside itself. */
function OutcomeLine({ outcome }: { outcome: RunOutcome }) {
  const phrases = conflictPhrases(outcome.rejected);
  return (
    <span className="text-[11.5px] leading-snug text-muted">
      {outcome.rejected.length > 0 ? (
        <>
          rejected{" "}
          <span className="font-mono text-fail/90">
            {phrases.map((p, i) => (
              <span key={p}>
                <span className="whitespace-nowrap">{p}</span>
                {i < phrases.length - 1 ? ", " : ""}
              </span>
            ))}
          </span>
        </>
      ) : (
        "first graph accepted"
      )}
      {" → "}
      {plural(outcome.repairs, "repair round")}
      {" → "}
      {outcome.verified ? (
        <span className="text-ok">verified, {outcome.verified}</span>
      ) : (
        `not verified${outcome.advice ? `: ${outcome.advice}` : ""}`
      )}
    </span>
  );
}

/** "Nemotron 3.5 Lightning structured it live in 7.69 s": the structure step of a recording,
 *  or null for a recording of offline answers. */
function structuredBy(plan: PlanResult | undefined): string | null {
  const usage = plan?.steps.find((s) => s.stage === "structure" && s.ok)?.usage;
  if (!usage || isFixtureModel(usage.model)) return null;
  return `${modelDisplayName(usage.model)} structured it live in ${fmtSeconds(usage.latency_s)}`;
}

export function BriefForm({
  brief,
  briefId,
  onBriefChange,
  examples,
  examplesLoading,
  examplesNote,
  onUseExample,
  onStructure,
  structuring,
  error,
  disabled = false,
  offline = false,
  defaultExampleId = "landing_pad",
  recordings = {},
  recorded = null,
  restartable = false,
}: BriefFormProps) {
  // the static demo replays a recording: only a recording's brief can be "structured"
  const canStructure = brief.trim().length > 0 && !structuring && !disabled && (recorded === null || briefId !== null);
  const words = brief.trim() ? brief.trim().split(/\s+/).length : 0;
  const replayedBy = recorded !== null && briefId ? structuredBy(recordings[briefId]) : null;

  // pre-fill the default example once, only while the textarea is still untouched
  const prefilled = useRef(false);
  useEffect(() => {
    if (prefilled.current || examplesLoading || examples.length === 0) return;
    prefilled.current = true;
    if (brief.trim().length > 0 || briefId) return;
    const ex = examples.find((e) => e.id === defaultExampleId) ?? examples[0];
    onUseExample(plainExample(ex));
  }, [examples, examplesLoading, brief, briefId, defaultExampleId, onUseExample]);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && canStructure) {
      e.preventDefault();
      onStructure();
    }
  };

  return (
    <Section
      id="brief"
      step="01"
      title="Brief"
      subtitle={
        recorded !== null
          ? "A recorded run's brief in plain English: the site, the structures, the robot fleet and the deadline. Nemotron turned it into the structured requirements of step 02; a brief of your own needs the backend."
          : "Describe the site, the structures, the robot fleet and the deadline in plain English. Nemotron turns it into structured requirements you can correct before anything is scheduled."
      }
    >
      <Card>
        {/* mechanism strip */}
        <ol className="mb-4 grid gap-2 lg:grid-cols-3" aria-label="How Moonwright works">
          {MECHANISM.map((m, i) => (
            <li
              key={m.glyph}
              className="relative flex gap-2.5 rounded-md border border-white/10 bg-white/[0.02] px-3 py-2.5"
            >
              <span className="mt-0.5 flex items-center gap-1.5">
                <span className="font-mono text-[11px] tabular-nums text-dim">{i + 1}</span>
                <Glyph kind={m.glyph} />
              </span>
              <span className="min-w-0">
                <span className="block text-[13px] font-semibold text-fg">{m.title}</span>
                <span className="mt-0.5 block text-[12.5px] leading-snug text-muted">{m.text}</span>
              </span>
              {i < MECHANISM.length - 1 && (
                <span
                  aria-hidden="true"
                  className="absolute -right-[9px] top-1/2 hidden -translate-y-1/2 text-[13px] text-dim lg:inline"
                >
                  ›
                </span>
              )}
            </li>
          ))}
        </ol>

        {/* example cards */}
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="text-[12.5px] font-medium text-muted">Example briefs</span>
          {examplesLoading && <Spinner className="size-3 text-dim" />}
          {!examplesLoading && examples.length === 0 && (
            <span className="text-xs text-dim">no examples available</span>
          )}
          {examplesNote && <span className="text-[11px] text-dim">{examplesNote}</span>}
        </div>
        {examples.length > 0 && (
          <div className="mb-3 grid gap-2 lg:grid-cols-3">
            {examples.map((ex) => {
              const recording = recordings[ex.id];
              const meta = recording ? recordedMeta(recording.requirements) : deriveMeta(ex.text);
              const active = briefId === ex.id;
              // the static demo's loaded card: a restart of the walk once it has moved on,
              // nothing to do before
              const restart = recorded !== null && active && restartable;
              const inert = recorded !== null && active && !restartable;
              return (
                <button
                  key={ex.id}
                  type="button"
                  onClick={() => {
                    if (!inert) onUseExample(plainExample(ex));
                  }}
                  disabled={disabled || structuring}
                  aria-pressed={active}
                  title={
                    restart
                      ? "Start the walk again from the brief; the stages below are cleared"
                      : active
                        ? "This brief is in the textarea"
                        : recorded !== null
                          ? `Walk through the recorded run of “${ex.title}” from its brief`
                          : `Put “${ex.title}” in the textarea`
                  }
                  className={cx(
                    "flex min-w-0 flex-col items-start gap-1 rounded-md border px-3 py-2.5 text-left transition-colors",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                    active
                      ? "border-white/40 bg-white/[0.07]"
                      : "border-white/10 bg-white/[0.02] hover:border-white/25 hover:bg-white/[0.04]",
                  )}
                >
                  <span className="flex w-full items-center justify-between gap-2">
                    <span className="truncate text-[13px] font-medium text-fg">{ex.title}</span>
                    {active && <Badge tone="neutral">{restart ? "start again" : "loaded"}</Badge>}
                  </span>
                  <span className="font-mono text-[11px] text-dim">
                    {meta.fleet} · {meta.structures}
                    {meta.horizon ? ` · ${meta.horizon}` : ""}
                  </span>
                  {recording && (
                    <span className="text-[11.5px] leading-snug text-muted">
                      {/* the static demo's cards are all recordings; elsewhere say whose
                          outcome this is */}
                      {recorded === null && <span className="text-dim">Recorded run: </span>}
                      <OutcomeLine outcome={runOutcome(recording)} />
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}

        <textarea
          id="brief-text"
          aria-label="Construction brief"
          value={brief}
          onChange={(e) => onBriefChange(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          readOnly={recorded !== null}
          title={recorded ?? undefined}
          rows={9}
          spellCheck={false}
          placeholder={PLACEHOLDER}
          className="min-h-[200px] w-full resize-y rounded-md border border-white/10 bg-surface p-3 text-[14px] leading-relaxed text-fg placeholder:text-dim focus:border-accent/60 focus:outline-none focus:ring-2 focus:ring-accent/20 disabled:opacity-60"
        />

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-dim">
            <span className="font-mono tabular-nums">{words} words</span>
            {recorded !== null ? (
              <span>
                {replayedBy && <span className="text-muted">{replayedBy} · </span>}
                {recorded}
              </span>
            ) : (
              <>
                {offline && briefId && (
                  <span
                    className="text-[11px]"
                    title="Offline mode: the backend replays the recorded Nemotron answers for this example even if you edit the text; clear the textarea to detach."
                  >
                    replays this example&apos;s recorded answers
                  </span>
                )}
                <span className="hidden items-center gap-1 md:inline-flex">
                  <kbd className="mw-kbd">⌘</kbd>
                  <span>/</span>
                  <kbd className="mw-kbd">Ctrl</kbd>
                  <span>+</span>
                  <kbd className="mw-kbd">Enter</kbd>
                  <span>to structure</span>
                </span>
              </>
            )}
          </div>
          <Button
            variant="primary"
            onClick={onStructure}
            disabled={!canStructure}
            loading={structuring}
            title={recorded !== null ? "Show the requirements the recorded run structured from this brief" : undefined}
          >
            {recorded !== null ? "Replay structuring" : structuring ? "Structuring…" : "Structure brief"}
          </Button>
        </div>

        {error && (
          <Notice tone="fail" title="Structuring failed" className="mt-3">
            {error}
          </Notice>
        )}
      </Card>
    </Section>
  );
}
