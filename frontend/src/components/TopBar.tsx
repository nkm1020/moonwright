"use client";
import { useState } from "react";
import type { Health } from "@/lib/types";
import { RECORDED_ON, SAMPLES, type SampleFile, type SampleId, type SampleSpec } from "@/lib/api";
import { joinAnd, plural } from "@/lib/format";
import { Button, Dot, IconCheck, Spinner, cx } from "./Section";

export type FlowStage = "brief" | "requirements" | "plan" | "simulate" | "robustness";

export const FLOW_STAGES: ReadonlyArray<{ id: FlowStage; label: string }> = [
  { id: "brief", label: "Brief" },
  { id: "requirements", label: "Requirements" },
  { id: "plan", label: "Plan" },
  { id: "simulate", label: "Simulate" },
  { id: "robustness", label: "Robustness" },
];

export type HealthStatus = "checking" | "ok" | "unreachable";

const FILE_NAMES: Record<SampleFile, string> = {
  sim: "simulation",
  mc: "Monte Carlo",
  whatif: "spare what-if",
};

/** Tooltip of a sample button: the run's outcome as its PlanResult tells it (format.ts
 *  fmtRunOutcome, absent until the file is read) and what loading it shows: the plan is the
 *  live run itself, its companion files were computed from it offline by the CLI. */
function sampleTooltip(spec: SampleSpec, outcome: string | undefined): string {
  const derived = spec.files.map((f) => FILE_NAMES[f]);
  const computed = derived.length > 0 ? `, with its ${joinAnd(derived)} computed offline from it` : "";
  return `${spec.title}${outcome ? `: ${outcome}` : ""}. Loads the recorded live plan${computed}; no backend needed.`;
}

export interface TopBarProps {
  health: Health | null;
  healthStatus: HealthStatus;
  apiBase: string;
  /** Stage the app is at; earlier stages render as done, later ones as upcoming. */
  stage: FlowStage;
  /** Something is running for the current stage (spinner on the stepper). */
  busy: boolean;
  /** Loads a recorded sample; the sample buttons are hidden when omitted. */
  onLoadSample?: (id: SampleId) => void;
  sampleLoading: boolean;
  onRetryHealth: () => void;
  /** Additive: each sample's outcome in one line (format.ts fmtRunOutcome), for its tooltip. */
  sampleOutcomes?: Partial<Record<SampleId, string>>;
  /** Additive: the static demo build, which has no backend: the badge says the page replays
   *  recorded live runs instead of asking for the backend's health. */
  staticDemo?: boolean;
}

export function TopBar({
  health,
  healthStatus,
  apiBase,
  stage,
  busy,
  onLoadSample,
  sampleLoading,
  onRetryHealth,
  sampleOutcomes = {},
  staticDemo = false,
}: TopBarProps) {
  const activeIdx = FLOW_STAGES.findIndex((s) => s.id === stage);
  // which sample button shows the spinner (the parent only reports "loading"); a stale value
  // is harmless since the spinner also needs sampleLoading
  const [pending, setPending] = useState<SampleId | null>(null);

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-surface">
      {/* one row from xl (brand [+ tagline] | stepper | demo + health); below xl the stepper
          takes its own centred row. The tagline shows only where the row has room for all of
          it, measured: from lg to xl (the stepper on its own row leaves the brand ~460 px) and
          from 1400 px, where the row reaches its 1,400 px cap (1440 and the 1536 recording
          width leave the brand 340-365 px whatever the backend badge says, for a 238 px
          tagline). Between 1280 and 1399 px the stepper, the sample buttons and the badge
          fill the row, and an ellipsis is worse than no tagline (the two ranges never
          overlap: Tailwind emits min-[1400px] before the named breakpoints, so an xl:hidden
          would win over it). From xl the demo group keeps its full width (it shares the row
          with the brand); below xl it wraps rather than overflow, and below sm it dissolves
          into the row (display: contents): the badge stays beside the brand and the sample
          buttons take a row of their own, so a phone never scrolls sideways */}
      <div className="mx-auto flex w-full max-w-[1400px] flex-wrap items-center gap-x-2.5 gap-y-2 px-4 py-2 md:px-6 lg:px-8">
        <div className="flex min-w-0 flex-1 basis-0 items-baseline gap-2.5">
          <h1
            className="shrink-0 text-[15px] font-semibold tracking-tight text-fg"
            title="Verified multi-robot planning for lunar construction"
          >
            <a href="#top">Moonwright</a>
          </h1>
          <span
            className="hidden min-w-0 truncate text-[12.5px] text-muted lg:max-xl:inline min-[1400px]:inline"
            title="Verified multi-robot planning for lunar construction"
          >
            verified multi-robot planning for the Moon
          </span>
        </div>

        <nav
          aria-label="Workflow stages"
          className="order-last w-full shrink-0 xl:order-none xl:w-auto"
        >
          <ol className="flex items-center justify-center gap-0.5">
            {FLOW_STAGES.map((s, i) => {
              const state = i < activeIdx ? "done" : i === activeIdx ? "current" : "upcoming";
              return (
                <li key={s.id} className="flex items-center">
                  {i > 0 && (
                    <span
                      aria-hidden="true"
                      className={cx("mx-px h-px w-2", i <= activeIdx ? "bg-accent/50" : "bg-white/10")}
                    />
                  )}
                  <a
                    href={`#${s.id}`}
                    aria-current={state === "current" ? "step" : undefined}
                    className={cx(
                      "flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-xs transition-colors",
                      state === "current" && "border-accent/50 bg-accent/10 text-accent",
                      state === "done" && "border-white/10 bg-white/5 text-muted hover:text-fg",
                      state === "upcoming" && "border-transparent text-dim",
                    )}
                  >
                    <span className="flex size-3.5 items-center justify-center font-mono text-[11px] tabular-nums">
                      {state === "done" ? <IconCheck className="size-2.5" /> : i + 1}
                    </span>
                    <span className="hidden sm:inline">{s.label}</span>
                    {state === "current" && busy && <Spinner className="size-3" />}
                  </a>
                </li>
              );
            })}
          </ol>
        </nav>

        <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-1.5 max-sm:contents xl:min-w-max xl:flex-1 xl:basis-0 xl:flex-nowrap">
          {onLoadSample && (
            <div
              className="flex flex-wrap items-center gap-0.5 max-sm:order-1 max-sm:w-full"
              role="group"
              aria-label="Recorded demo samples"
            >
              {SAMPLES.map((spec) => (
                <Button
                  key={spec.id}
                  variant="ghost"
                  size="sm"
                  className="px-2 text-fg"
                  title={sampleTooltip(spec, sampleOutcomes[spec.id])}
                  aria-label={`${spec.title}: load the recorded run`}
                  loading={sampleLoading && pending === spec.id}
                  disabled={sampleLoading}
                  onClick={() => {
                    setPending(spec.id);
                    onLoadSample(spec.id);
                  }}
                >
                  {!(sampleLoading && pending === spec.id) && <IconPlay className="size-2.5 text-muted" />}
                  {spec.title}
                </Button>
              ))}
            </div>
          )}
          {staticDemo ? (
            <RecordedBadge />
          ) : (
            <BackendBadge health={health} status={healthStatus} apiBase={apiBase} onRetry={onRetryHealth} />
          )}
        </div>
      </div>
    </header>
  );
}

/** A small play triangle: the demo buttons load a recorded run end to end. */
function IconPlay({ className }: { className?: string }) {
  return (
    <svg className={className ?? "size-3"} viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">
      <path d="M3 1.8a.8.8 0 0 1 1.2-.7l6 4.2a.8.8 0 0 1 0 1.4l-6 4.2A.8.8 0 0 1 3 10.2z" />
    </svg>
  );
}

const badgeClass =
  "inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs";

/** The model routing of a live backend (backend llm/tokenfactory.py), for the badge tooltip. */
const LIVE_ROUTING =
  "Nemotron 3.5 Lightning structures the brief, Nemotron 3 Ultra proposes the task graph and repairs it, Nemotron 3 Super writes the explanation";

/** The backend's address shows only in a development build: the product copy names no host,
 *  port or path. */
const SHOW_ADDRESS = process.env.NODE_ENV !== "production";

/** The static demo's badge: what is on the page are live runs, recorded, not the offline
 *  fixtures of a backend in recorded-answers mode. */
function RecordedBadge() {
  return (
    <span
      className={cx(badgeClass, "border-info/30 bg-info/10 text-info")}
      title={`Static demo: live plans recorded on Nebius Token Factory on ${RECORDED_ON} (${LIVE_ROUTING}), with their CP-SAT schedules; the simulation and Monte Carlo were computed from those plans offline, by the CLI. Nothing is called now.`}
    >
      <Dot color="var(--info)" />
      recorded live runs
    </span>
  );
}

function BackendBadge({
  health,
  status,
  apiBase,
  onRetry,
}: {
  health: Health | null;
  status: HealthStatus;
  apiBase: string;
  onRetry: () => void;
}) {
  const at = SHOW_ADDRESS ? ` at ${apiBase}` : "";
  if (status === "checking") {
    return (
      <span className={cx(badgeClass, "border-white/10 text-muted")} title={`Asking the planning backend${at} whether it is up`}>
        <Spinner className="size-3" />
        checking backend
      </span>
    );
  }
  if (status === "unreachable" || !health) {
    return (
      <button
        type="button"
        onClick={onRetry}
        title={`No planning backend answered${at}. The recorded demos work without one; structuring and planning need it. Click to retry.`}
        className={cx(badgeClass, "border-white/15 bg-white/5 text-muted hover:bg-white/10 hover:text-fg")}
      >
        <Dot color="var(--fail)" />
        backend unreachable
      </button>
    );
  }
  const live = health.llm !== "fixture";
  const examples = plural(health.examples, "example brief");
  if (!live) {
    return (
      <span
        className={cx(badgeClass, "border-white/15 bg-white/5 text-muted")}
        title={`Moonwright ${health.version}${at} replays recorded Nemotron answers; CP-SAT, the simulator and Monte Carlo run live · ${examples}`}
      >
        <Dot color="var(--ok)" />
        recorded answers
      </span>
    );
  }
  return (
    <span
      className={cx(badgeClass, "border-ok/30 bg-ok/10 text-ok")}
      title={`Moonwright ${health.version}${at} · Nemotron on Nebius Token Factory: ${LIVE_ROUTING} · ${examples}`}
    >
      <Dot color="var(--ok)" />
      Nemotron live
    </span>
  );
}
