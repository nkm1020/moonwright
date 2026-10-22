"use client";
import type { PlanResult } from "@/lib/types";
import { fmtCompact, fmtHours, fmtSeconds, plural, shortModel, totalUsage } from "@/lib/format";
import { Badge, Card } from "./Section";

export interface ExplanationCardProps {
  plan: PlanResult;
}

export function ExplanationCard({ plan }: ExplanationCardProps) {
  const paragraphs = (plan.explanation ?? "")
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);
  const usage = totalUsage(plan.steps);
  const schedule = plan.schedule;
  const taskCount = plan.task_graph?.tasks.length ?? 0;
  const robotsUsed = new Set(schedule?.entries.map((e) => e.robot) ?? []).size;

  const fallback = schedule
    ? `No explanation was generated for this plan. The schedule is solver-verified all the same: ${plural(taskCount, "task")} on ${plural(robotsUsed, "robot")} finish in ${fmtHours(schedule.makespan_h, 0)} of the ${fmtHours(plan.requirements.horizon_h, 0)} horizon, with precedence, zone safety, robot no-overlap and battery budgets checked independently of the solver.`
    : "The plan was not verified, so there is nothing to explain yet. See the trace for the conflicts that remained after the repair rounds.";

  return (
    <Card
      title="Explanation"
      aside={
        usage.model ? (
          <span title={usage.model}>{shortModel(usage.model)}</span>
        ) : (
          <span>{usage.calls > 0 ? "offline fixture" : "no model call"}</span>
        )
      }
      className="h-full"
    >
      {paragraphs.length > 0 ? (
        paragraphs.map((p, i) => (
          <p key={i} className="text-[13.5px] leading-relaxed text-fg/90 [&+p]:mt-3">
            {p}
          </p>
        ))
      ) : (
        <p className="text-[13px] leading-relaxed text-muted">{fallback}</p>
      )}

      {plan.task_graph?.notes && (
        <p className="mt-4 border-l-2 border-accent/40 pl-3 text-xs leading-relaxed text-muted">
          <span className="font-medium text-fg/80">Planner notes. </span>
          {plan.task_graph.notes}
        </p>
      )}

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 border-t border-line pt-3 text-xs sm:grid-cols-3">
        <Fact label="Tasks" value={String(taskCount)} />
        <Fact label="Robots used" value={`${robotsUsed} of ${plan.requirements.robots.length}`} />
        <Fact label="Repair rounds" value={String(plan.repairs)} />
        <Fact
          label="Solver"
          value={
            schedule ? (
              <>
                <Badge tone={schedule.status === "optimal" ? "ok" : "info"}>{schedule.status}</Badge>{" "}
                {fmtSeconds(schedule.solve_time_s)}
              </>
            ) : (
              "—"
            )
          }
        />
        <Fact
          label="Model calls"
          value={usage.calls > 0 ? `${usage.calls} · ${fmtSeconds(usage.latency)}` : "none"}
        />
        <Fact
          label="Tokens"
          value={usage.input + usage.output > 0 ? `${fmtCompact(usage.input)} in · ${fmtCompact(usage.output)} out` : "—"}
        />
      </dl>
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] uppercase tracking-wider text-dim">{label}</dt>
      <dd className="mt-0.5 font-mono tabular-nums text-fg">{value}</dd>
    </div>
  );
}
