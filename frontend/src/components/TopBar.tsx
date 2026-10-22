"use client";
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

export interface TopBarProps {
  health: Health | null;
  healthStatus: HealthStatus;
  apiBase: string;
  /** Stage the app is at; earlier stages render as done, later ones as upcoming. */
  stage: FlowStage;
  /** Something is running for the current stage (spinner on the stepper). */
  busy: boolean;
  onLoadSample: () => void;
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
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-surface/85 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1400px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5 md:px-6 lg:px-8">
        <div className="flex min-w-0 items-baseline gap-3">
          <a href="#top" className="text-[15px] font-semibold tracking-tight text-fg">
            Moonwright
          </a>
          <span className="hidden truncate text-xs text-muted xl:inline">
            verified multi-robot construction planning for the lunar surface
          </span>
        </div>

        <nav aria-label="Workflow stages" className="order-last w-full md:order-none md:w-auto md:flex-1">
          <ol className="flex items-center gap-0.5 md:justify-center">
            {FLOW_STAGES.map((s, i) => {
              const state = i < activeIdx ? "done" : i === activeIdx ? "current" : "upcoming";
              return (
                <li key={s.id} className="flex items-center">
                  {i > 0 && (
                    <span
                      aria-hidden="true"
                      className={cx("mx-1 h-px w-2 md:w-5", i <= activeIdx ? "bg-accent/50" : "bg-white/10")}
                    />
                  )}
                  <a
                    href={`#${s.id}`}
                    aria-current={state === "current" ? "step" : undefined}
                    className={cx(
                      "flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition-colors",
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

        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onLoadSample}
            loading={sampleLoading}
            title="Load the recorded landing-pad plan and simulation from /samples (no backend needed)"
          >
            Load sample result
          </Button>
          <BackendBadge health={health} status={healthStatus} apiBase={apiBase} onRetry={onRetryHealth} />
        </div>
      </div>
    </header>
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
        title={`GET ${apiBase}/health failed. Click to retry; "Load sample result" works without a backend.`}
        className={cx(badgeClass, "border-fail/30 bg-fail/10 text-fail hover:bg-fail/15")}
      >
        <Dot color="var(--fail)" />
        backend unreachable
      </button>
    );
  }
  const live = health.llm !== "fixture";
  return (
    <span
      className={cx(
        badgeClass,
        live ? "border-ok/30 bg-ok/10 text-ok" : "border-white/15 bg-white/5 text-muted",
      )}
      title={`Moonwright ${health.version} at ${apiBase} · llm=${health.llm} · ${health.examples} example briefs`}
    >
      <Dot color={live ? "var(--ok)" : "var(--fg-muted)"} />
      {live ? `Nemotron live · ${health.llm}` : "offline fixtures"}
    </span>
  );
}
