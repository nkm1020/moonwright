"use client";
import { useState } from "react";
import type { Health } from "@/lib/types";
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

/** The two recorded entry points under public/samples (no backend needed). */
export type SampleId = "landing_pad" | "tight_battery";

export const SAMPLE_ACTIONS: ReadonlyArray<{ id: SampleId; label: string; detail: string; title: string }> = [
  {
    id: "landing_pad",
    label: "Landing pad",
    detail: "failure → replan",
    title:
      "Landing pad: failure → replan. Loads the recorded 14-task plan and its simulation: EX2 fails at 30 h and CP-SAT reschedules the remaining work. No backend needed.",
  },
  {
    id: "tight_battery",
    label: "Tight battery",
    detail: "infeasible → repair",
    title:
      "Tight battery: infeasible → repair. Loads the recorded plan whose first schedule failed on the excavator batteries; Nemotron repaired it from the infeasibility core. No backend needed.",
  },
];

export interface TopBarProps {
  health: Health | null;
  healthStatus: HealthStatus;
  apiBase: string;
  /** Stage the app is at; earlier stages render as done, later ones as upcoming. */
  stage: FlowStage;
  /** Something is running for the current stage (spinner on the stepper). */
  busy: boolean;
  /** Loads a recorded sample; the two sample actions are hidden when omitted. */
  onLoadSample?: (id: SampleId) => void;
  sampleLoading: boolean;
  onRetryHealth: () => void;
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
}: TopBarProps) {
  const activeIdx = FLOW_STAGES.findIndex((s) => s.id === stage);
  // which sample button shows the spinner (the parent only reports "loading"); a stale value
  // is harmless since the spinner also needs sampleLoading
  const [pending, setPending] = useState<SampleId | null>(null);

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-surface">
      {/* one row from xl (brand + tagline | stepper | demo + health); below xl the stepper
          takes its own centred row so the tagline never has to truncate */}
      <div className="mx-auto flex w-full max-w-[1400px] flex-wrap items-center gap-x-2.5 gap-y-2 px-4 py-2 md:px-6 lg:px-8">
        <div className="flex min-w-0 flex-1 basis-0 items-baseline gap-2.5">
          <h1 className="shrink-0 text-[15px] font-semibold tracking-tight text-fg">
            <a href="#top">Moonwright</a>
          </h1>
          <span
            className="hidden min-w-0 truncate text-[12.5px] text-muted md:inline"
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
                    <span className="flex size-3.5 items-center justify-center font-mono text-[10px] tabular-nums">
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

        <div className="ml-auto flex shrink-0 items-center gap-1.5 xl:flex-1 xl:basis-0 xl:justify-end">
          {onLoadSample && (
            <div className="flex items-center gap-0.5" role="group" aria-label="Recorded demo samples">
              {SAMPLE_ACTIONS.map((a) => (
                <Button
                  key={a.id}
                  variant="ghost"
                  size="sm"
                  className="px-2 text-fg"
                  title={a.title}
                  aria-label={`${a.label}: ${a.detail}`}
                  loading={sampleLoading && pending === a.id}
                  disabled={sampleLoading}
                  onClick={() => {
                    setPending(a.id);
                    onLoadSample(a.id);
                  }}
                >
                  {!(sampleLoading && pending === a.id) && <IconPlay className="size-2.5 text-muted" />}
                  {a.label}
                </Button>
              ))}
            </div>
          )}
          <BackendBadge health={health} status={healthStatus} apiBase={apiBase} onRetry={onRetryHealth} />
        </div>
      </div>
    </header>
  );
}

/** A small play triangle: the two demo buttons load a recorded run end to end. */
function IconPlay({ className }: { className?: string }) {
  return (
    <svg className={className ?? "size-3"} viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">
      <path d="M3 1.8a.8.8 0 0 1 1.2-.7l6 4.2a.8.8 0 0 1 0 1.4l-6 4.2A.8.8 0 0 1 3 10.2z" />
    </svg>
  );
}

const badgeClass =
  "inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs";

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
  if (status === "checking") {
    return (
      <span className={cx(badgeClass, "border-white/10 text-muted")} title={`GET ${apiBase}/health`}>
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
        title={`No backend answered at ${apiBase} (GET /health). The two recorded samples work without one; structuring and planning need it. Click to retry.`}
        className={cx(badgeClass, "border-white/15 bg-white/5 text-muted hover:bg-white/10 hover:text-fg")}
      >
        <Dot color="var(--fail)" />
        backend unreachable
      </button>
    );
  }
  const live = health.llm !== "fixture";
  if (!live) {
    return (
      <span
        className={cx(badgeClass, "border-white/15 bg-white/5 text-muted")}
        title={`Moonwright ${health.version} at ${apiBase} replays recorded Nemotron answers; CP-SAT, the simulator and Monte Carlo run live · ${health.examples} example briefs`}
      >
        <Dot color="var(--ok)" />
        recorded answers
        <span className="hidden 2xl:inline">· live solver</span>
      </span>
    );
  }
  return (
    <span
      className={cx(badgeClass, "border-ok/30 bg-ok/10 text-ok")}
      title={`Moonwright ${health.version} at ${apiBase} · Nebius Token Factory · Nemotron Lightning structures the brief, Super proposes the task graph and writes the explanation, Ultra repairs · ${health.examples} example briefs`}
    >
      <Dot color="var(--ok)" />
      Nemotron live
      <span className="hidden 2xl:inline">· 3 models</span>
    </span>
  );
}
