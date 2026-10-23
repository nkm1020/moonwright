"use client";
import type { LoopStep, PlanResult } from "@/lib/types";
import { taskGraphPayload } from "@/lib/types";
import {
  fmtCompact,
  fmtHours,
  fmtSeconds,
  isFixtureModel,
  modelDisplayName,
  modelTitle,
  plural,
  tidyProse,
  totalUsage,
} from "@/lib/format";
import { Badge, Card } from "./Section";

export interface ExplanationCardProps {
  plan: PlanResult;
}

function findLast<T>(items: T[], pred: (item: T) => boolean): T | null {
  for (let i = items.length - 1; i >= 0; i -= 1) if (pred(items[i])) return items[i];
  return null;
}

/** The explain step's model, or the first model of the trace for an older PlanResult. */
function writerModel(steps: LoopStep[]): string | null {
  const explain = findLast(steps, (s) => s.stage === "explain" && s.ok && Boolean(s.usage?.model));
  return explain?.usage?.model ?? null;
}

export function ExplanationCard({ plan }: ExplanationCardProps) {
  // the model's words verbatim, its figures in the console's format ("4000 Wh" -> "4,000 Wh",
  // "10 m3" -> "10 m³")
  const paragraphs = (plan.explanation ?? "")
    .split(/\n{2,}/)
    .map((p) => tidyProse(p.trim()))
    .filter(Boolean);
  const usage = totalUsage(plan.steps);
  const schedule = plan.schedule;
  const taskCount = plan.task_graph?.tasks.length ?? 0;
  const robotsUsed = new Set(schedule?.entries.map((e) => e.robot) ?? []).size;

  const writer = writerModel(plan.steps);
  const grounded = findLast(plan.steps, (s) => taskGraphPayload(s) !== null);
  const priced = grounded ? (taskGraphPayload(grounded)?.grounded ?? 0) > 0 : false;
  const source = schedule ? "from the verified schedule" : "from the last task graph (not verified)";
  const pricing = priced
    ? "the rate model priced the quantities"
    : "the task hours and Wh are the model's own estimates";
  const caption =
    paragraphs.length === 0
      ? null
      : writer && !isFixtureModel(writer)
        ? `Written by ${modelDisplayName(writer)} ${source}; ${pricing}, CP-SAT fixed the timing.`
        : `Recorded Nemotron answer, written ${source}; ${pricing}, CP-SAT fixed the timing.`;

  const fallback = schedule
    ? `No explanation was generated for this plan. The schedule is solver-verified all the same: ${plural(taskCount, "task")} on ${plural(robotsUsed, "robot")} finish in ${fmtHours(schedule.makespan_h, 0)} of the ${fmtHours(plan.requirements.horizon_h, 0)} horizon, with precedence, zone safety, robot no-overlap and battery budgets checked independently of the solver.`
    : "The plan was not verified, so there is nothing to explain yet. See the trace for the conflicts that remained after the repair rounds.";

  const asideModel = writer ?? usage.model;

  return (
    <Card
      title="Explanation"
      aside={
        asideModel ? (
          <span title={modelTitle(asideModel)}>{modelDisplayName(asideModel)}</span>
        ) : (
          <span>{usage.calls > 0 ? "recorded answer" : "no model call"}</span>
        )
      }
      className="h-full"
    >
      {/* full-width card: prose on the left (a 75ch measure), the facts as a two-column grid
          on the right from lg, so neither side leaves a dead strip */}
      <div className="lg:grid lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:gap-10">
        <div className="min-w-0 max-w-[76ch]">
          {caption && <p className="mb-3 border-l-2 border-white/15 pl-3 text-xs leading-relaxed text-muted">{caption}</p>}

          {paragraphs.length > 0 ? (
            paragraphs.map((p, i) => (
              <p key={i} className="text-[14px] leading-relaxed text-fg/90 [&+p]:mt-3">
                {p}
              </p>
            ))
          ) : (
            <p className="text-[13.5px] leading-relaxed text-muted">{fallback}</p>
          )}

          {plan.task_graph?.notes && (
            <p className="mt-4 border-l-2 border-white/15 pl-3 text-xs leading-relaxed text-muted">
              <span className="font-medium text-fg/80">Planner notes. </span>
              {tidyProse(plan.task_graph.notes)}
            </p>
          )}
        </div>

        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 border-t border-line pt-4 text-xs sm:grid-cols-3 lg:mt-0 lg:grid-cols-2 lg:content-start lg:border-t-0 lg:border-l lg:pl-8 lg:pt-0">
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
            value={
              usage.calls > 0 ? (
                <span title={modelTitle(usage.model)}>
                  {usage.calls} · {fmtSeconds(usage.latency)}
                </span>
              ) : (
                "none"
              )
            }
          />
          <Fact
            label="Tokens"
            value={usage.input + usage.output > 0 ? `${fmtCompact(usage.input)} in · ${fmtCompact(usage.output)} out` : "—"}
          />
        </dl>
      </div>
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium uppercase tracking-wider text-dim">{label}</dt>
      <dd className="mt-1 font-mono text-[14px] tabular-nums text-fg">{value}</dd>
    </div>
  );
}
