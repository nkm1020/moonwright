"use client";
/**
 * The verification receipt: what CP-SAT proved about the plan's schedule, as five rows with
 * green checks and counts, read from the last ok `schedule` step's payload.verification
 * (backend loop.py verification_receipt) plus the plan. A plan without a verified schedule
 * renders the red variant: the failure kind and the conflict core as chips, with the hint and
 * the numbers behind a disclosure (the trace's final step shows that core in full, so one
 * screen holds one full core), then the solver-proven advice ("What would make it feasible":
 * PlanResult.suggestions, each change re-solved by CP-SAT) as the panel with its footer.
 */
import type { ReactNode } from "react";
import type { LoopStep, PlanResult, Schedule, VerificationReceipt as ReceiptData } from "@/lib/types";
import { lastReceipt, taskGraphPayload } from "@/lib/types";
import { fmtHours, fmtNum, fmtPercent, fmtSeconds, isFixtureModel, modelDisplayName, planSuggestions, plural } from "@/lib/format";
import { Badge, Card, IconCheck, IconX, cx } from "./Section";
import { FailureDetails, isVerdict, roundsPhrase } from "./ProgressTrace";
import { Suggestions } from "./Suggestions";

export interface VerificationReceiptProps {
  plan: PlanResult;
  /** The trace to read the receipt from; defaults to plan.steps. */
  steps?: LoopStep[];
  /** Horizon for the red variant's budget table (defaults to the plan's requirements). */
  horizonH?: number | null;
  /** Render the solver-proven advice panel inside the red variant (default); the console
   *  passes false when it shows that panel itself, next to the apply buttons. */
  showAdvice?: boolean;
  className?: string;
}

export function VerificationReceipt({ plan, steps, horizonH = null, showAdvice = true, className }: VerificationReceiptProps) {
  const trace = steps && steps.length > 0 ? steps : plan.steps;
  if (plan.success && plan.schedule) {
    const found = lastReceipt(trace);
    return (
      <ReceiptCard
        plan={plan}
        steps={trace}
        receipt={found?.receipt ?? deriveReceipt(plan, plan.schedule)}
        derived={!found}
        className={className}
      />
    );
  }
  return (
    <FailedCard plan={plan} horizonH={horizonH ?? plan.requirements.horizon_h} showAdvice={showAdvice} className={className} />
  );
}

/** A receipt computed from the schedule itself, for a PlanResult recorded before the backend
 *  carried one (no travel share, no recheck note; the footer says so). */
function deriveReceipt(plan: PlanResult, schedule: Schedule): ReceiptData {
  const tasks = plan.task_graph?.tasks ?? [];
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const work = new Map<string, number>();
  for (const e of schedule.entries) work.set(e.robot, (work.get(e.robot) ?? 0) + (byId.get(e.task)?.energy_wh ?? 0));
  const battery = plan.requirements.robots.map((r) => {
    const used = work.get(r.id) ?? 0;
    return {
      robot: r.id,
      used_wh: used,
      work_wh: used,
      travel_wh: 0,
      budget_wh: r.battery_wh,
      pct: r.battery_wh > 0 ? Math.round((1000 * used) / r.battery_wh) / 10 : 0,
    };
  });
  let tightest: (typeof battery)[number] | null = null;
  for (const b of battery) if (!tightest || b.pct > tightest.pct) tightest = b;
  let edges = 0;
  for (const t of tasks) for (const p of t.after) if (byId.has(p)) edges += 1;
  return {
    precedence_edges: edges,
    robot_rows: plan.requirements.robots.length,
    zone_groups: new Set(tasks.map((t) => t.zone)).size,
    battery,
    tightest_robot: tightest?.robot ?? null,
    horizon_used_h: schedule.makespan_h,
    horizon_h: plan.requirements.horizon_h,
    status: schedule.status,
    objective_bound_h: schedule.objective_bound_h,
    solve_time_s: schedule.solve_time_s,
    travel_allowance: false,
    recheck: "",
  };
}

/** The last step of one of `stages` that carries a model name. */
function lastModel(steps: LoopStep[], stages: ReadonlyArray<LoopStep["stage"]>): string | null {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const s = steps[i];
    if (stages.includes(s.stage) && s.usage?.model) return s.usage.model;
  }
  return null;
}

function lastGrounded(steps: LoopStep[]): number | null {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const p = taskGraphPayload(steps[i]);
    if (p) return p.grounded;
  }
  return null;
}

interface Row {
  label: string;
  value: string;
  detail: string;
  extra?: ReactNode;
}

function ReceiptCard({
  plan,
  steps,
  receipt,
  derived,
  className,
}: {
  plan: PlanResult;
  steps: LoopStep[];
  receipt: ReceiptData;
  derived: boolean;
  className?: string;
}) {
  const tight = receipt.battery.find((b) => b.robot === receipt.tightest_robot) ?? null;
  const share = receipt.horizon_h > 0 ? receipt.horizon_used_h / receipt.horizon_h : 0;
  const rows: Row[] = [
    {
      label: "Precedence",
      value: `${plural(receipt.precedence_edges, "edge")} honoured`,
      detail: "each task starts after every task it waits for has ended",
    },
    {
      label: "One task per robot",
      value: plural(receipt.robot_rows, "robot"),
      detail: receipt.travel_allowance
        ? "no overlap on any robot row, transit between zones reserved"
        : "no overlap on any robot row",
    },
    {
      label: "One robot per zone",
      value: plural(receipt.zone_groups, "zone"),
      detail: "tasks in one zone never overlap in time",
    },
    {
      label: "Battery",
      value: plural(receipt.battery.length, "budget"),
      detail: tight
        ? `tightest ${tight.robot} ${fmtNum(tight.pct, 0)}%${receipt.travel_allowance ? " incl. transit" : ""}`
        : "no robot scheduled",
      extra: <BatteryPills receipt={receipt} />,
    },
    {
      label: "Deadline",
      value: `${fmtNum(receipt.horizon_used_h, 0)} of ${fmtNum(receipt.horizon_h, 0)} h`,
      detail: `${fmtPercent(share)} of the horizon${receipt.status === "optimal" ? ", makespan proved optimal" : ""}`,
      extra: (
        <div className="mt-1.5 h-1.5 w-full max-w-[220px] overflow-hidden rounded-full bg-white/10" aria-hidden="true">
          <div className="h-full rounded-full bg-ok" style={{ width: `${Math.min(100, share * 100)}%` }} />
        </div>
      ),
    },
  ];

  const proposer = lastModel(steps, ["taskgraph", "repair"]);
  const grounded = lastGrounded(steps);
  const bound =
    receipt.status !== "optimal" && receipt.objective_bound_h !== null
      ? `, bound ${fmtHours(receipt.objective_bound_h, 0)}`
      : "";
  const footer = [
    proposer && !isFixtureModel(proposer) ? `${modelDisplayName(proposer)} proposed` : "Nemotron proposed (recorded answer)",
    grounded === null ? null : grounded > 0 ? `rate model priced ${plural(grounded, "task")}` : "model's own estimates (no quantities stated)",
    `CP-SAT proved (${receipt.status}${bound}, ${fmtSeconds(receipt.solve_time_s)})`,
    receipt.recheck ? "re-checked independently" : null,
    derived ? "receipt derived from the schedule (trace carried none)" : null,
  ]
    .filter((s): s is string => Boolean(s))
    .join(" · ");

  return (
    <Card
      className={cx("border-ok/30", className)}
      title={
        <span className="inline-flex items-center gap-2">
          <span className="flex size-4 items-center justify-center rounded-full bg-ok/15 text-ok">
            <IconCheck className="size-2.5" />
          </span>
          Verification receipt
        </span>
      }
      aside={
        <span className="flex flex-wrap items-center gap-1.5">
          <Badge tone="ok">5 of 5 constraint families proved</Badge>
          <Badge mono>{plural(plan.repairs, "repair round")}</Badge>
        </span>
      }
      bodyClassName="p-4 md:px-5"
    >
      <ol className="grid grid-cols-1 gap-x-5 gap-y-3 sm:grid-cols-2 lg:grid-cols-5">
        {rows.map((r) => (
          <li key={r.label} className="flex min-w-0 items-start gap-2.5">
            <span
              aria-label="proved"
              className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-ok/15 text-ok"
            >
              <IconCheck className="size-3" />
            </span>
            <div className="min-w-0">
              <div className="text-[11px] font-medium uppercase tracking-wider text-dim">{r.label}</div>
              <div className="text-[15px] font-semibold tracking-tight text-fg">{r.value}</div>
              <div className="mt-0.5 text-xs text-muted">{r.detail}</div>
              {r.extra}
            </div>
          </li>
        ))}
      </ol>
      <footer className="mt-4 border-t border-line pt-2.5 text-xs text-muted">{footer}</footer>
    </Card>
  );
}

/** One pill per robot: used share of its budget (work + the transit its sequence drives). */
function BatteryPills({ receipt }: { receipt: ReceiptData }) {
  if (receipt.battery.length === 0) return null;
  return (
    <ul className="mt-1.5 flex flex-wrap gap-1">
      {receipt.battery.map((b) => (
        <li
          key={b.robot}
          title={`${b.robot}: ${fmtNum(b.used_wh, 0)} of ${fmtNum(b.budget_wh, 0)} Wh (work ${fmtNum(b.work_wh, 0)} Wh + travel ${fmtNum(b.travel_wh, 0)} Wh)`}
          className={cx(
            "rounded border px-1.5 font-mono text-[11px] leading-[18px] tabular-nums",
            b.pct > 90
              ? "border-warn/40 text-warn"
              : b.robot === receipt.tightest_robot
                ? "border-ok/40 text-fg"
                : "border-white/10 text-dim",
          )}
        >
          {b.robot} {fmtNum(b.pct, 0)}%
        </li>
      ))}
    </ul>
  );
}

function FailedCard({
  plan,
  horizonH,
  showAdvice,
  className,
}: {
  plan: PlanResult;
  horizonH: number;
  showAdvice: boolean;
  className?: string;
}) {
  const failure = plan.failure;
  const advice = showAdvice ? planSuggestions(plan) : [];
  const repairsOff = plan.repairs === 0 && isVerdict(failure);
  const why = !failure
    ? "The planner stopped without a failure record."
    : failure.kind === "infeasible"
      ? "CP-SAT proved the last task graph infeasible; the conflicts below are the minimal core it extracted (the trace's final step has the numbers)."
      : failure.kind === "validation"
        ? "The validator rejected the last task graph before the solver ran; the conflicts below name what it found."
        : "The planner stopped before a schedule could be proved; the trace's final step says why.";
  const rounds = repairsOff ? " Repairs were off, so this first verdict was final." : "";
  return (
    <Card
      className={cx("border-fail/40", className)}
      title={
        <span className="inline-flex items-center gap-2">
          <span className="flex size-4 items-center justify-center rounded-full bg-fail/15 text-fail">
            <IconX className="size-2.5" />
          </span>
          Verification receipt
        </span>
      }
      aside={<Badge tone="fail">no verified schedule</Badge>}
    >
      <div className="text-[15px] font-semibold tracking-tight text-fail">Not proved {roundsPhrase(plan)}</div>
      <p className="mt-0.5 text-xs text-muted">
        {why}
        {rounds}
      </p>
      {failure && <FailureDetails failure={failure} horizonH={horizonH} className="mt-3" collapsed />}
      {advice.length > 0 && <Suggestions variant="outcome" suggestions={advice} className="mt-3" />}
    </Card>
  );
}
