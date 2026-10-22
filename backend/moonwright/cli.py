"""Command-line entry point: ``moonwright plan | simulate | montecarlo | eval | serve``.

Progress lines go to stderr; the report (trace, Gantt, explanation, tables) goes to stdout;
every run also writes its result model as JSON so the UI and notebooks can pick it up.
Exit codes: 0 success, 1 pipeline finished without a feasible plan / incomplete simulation,
2 configuration or input error.
"""

from __future__ import annotations

import argparse
import csv
import inspect
import math
import os
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self

from pydantic import BaseModel, Field

from moonwright.api import ExampleBrief, clean_brief_id, list_briefs, thin_frames
from moonwright.llm.base import LLMClient, LLMError
from moonwright.llm.factory import KINDS, make_client
from moonwright.llm.tokenfactory import THINKING_ENV_VAR
from moonwright.pipeline.loop import Planner, brief_key
from moonwright.pipeline.rates import apply_rates
from moonwright.pipeline.schedule import check_schedule
from moonwright.schemas import (
    Event,
    LoopStep,
    PlanResult,
    ReplanRecord,
    Requirements,
    Role,
    Schedule,
    TaskGraph,
    Usage,
)
from moonwright.sim.montecarlo import (
    MonteCarloPlanner,
    format_monte_carlo,
    run_monte_carlo,
    solver_only_planner,
)
from moonwright.sim.replan import run_with_replanning
from moonwright.sim.simulator import FailureInjection

EXIT_OK: Final = 0
EXIT_NO_PLAN: Final = 1
EXIT_ERROR: Final = 2
OUT_DIR: Final = Path("out")
FRAME_STRIDE: Final = 4
DEFAULT_DT_H: Final = 0.25
CONDITIONS: Final[dict[str, tuple[str, ...]]] = {
    "off": ("loop_off",),
    "on": ("loop_on",),
    "both": ("loop_off", "loop_on"),
}
FEEDBACK_MODES: Final = ("core", "verdict")   # what a repair round is told (eval ablation arm)
DEFAULT_FEEDBACK: Final = "core"
MAX_REPAIRS_LIMIT: Final = 10                 # same bound as the API request models
MAX_ANSWER_RETRIES_LIMIT: Final = 5
WILSON_Z: Final = 1.959964                    # two-sided 95%


def say(message: str) -> None:
    """Progress line on stderr (stdout is reserved for the report)."""
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- shared helpers
class NarratedClient:
    """LLMClient wrapper that announces each model call, so a 60 s live call is visibly alive."""

    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.name = inner.name

    def complete_json(
        self,
        *,
        role: Role,
        system: str,
        user: str,
        schema: type,
        key: str | None = None,
        attempt: int = 1,
        variant: str | None = None,
        max_attempts: int = 2,
    ):
        tag = f" ({variant})" if variant else ""
        say(f"  -> asking {role}{tag}, attempt {attempt}, via {self.name} ...")
        return self.inner.complete_json(
            role=role, system=system, user=user, schema=schema,
            key=key, attempt=attempt, variant=variant, max_attempts=max_attempts,
        )


def read_brief(source: str) -> tuple[str, str | None]:
    """Return ``(brief text, default brief id)``; ``-`` reads stdin and has no default id."""
    if source == "-":
        return sys.stdin.read(), None
    path = Path(source)
    return path.read_text(encoding="utf-8"), path.stem


def plan_stem(path: Path) -> str:
    """``out/landing_pad.plan.json`` -> ``landing_pad``."""
    name = path.name
    for suffix in (".plan.json", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def write_json(path: Path, model: BaseModel, *, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=indent) + "\n", encoding="utf-8")


def format_step(step: LoopStep) -> str:
    """One trace line: stage, attempt, ok, duration, then message or failure conflicts."""
    status = "ok  " if step.ok else "FAIL"
    head = f"{step.stage:<9} #{step.attempt} {status} {step.duration_s:6.2f}s"
    if step.ok:
        parts = [step.message] if step.message else []
        if step.usage and step.usage.model:
            tokens = ""
            if step.usage.input_tokens or step.usage.output_tokens:
                tokens = f" {step.usage.input_tokens}->{step.usage.output_tokens} tok"
            parts.append(f"[{step.usage.model}{tokens}]")
        return "  ".join([head, *parts])
    if step.failure is None:
        return f"{head}  failed"
    conflicts = ", ".join(step.failure.conflicts) or "-"
    line = f"{head}  {step.failure.kind}: {conflicts}"
    if step.failure.hint:
        line += f"\n{'':>24}hint: {step.failure.hint}"
    return line


def format_trace(steps: Sequence[LoopStep]) -> str:
    return "\n".join(format_step(s) for s in steps)


def format_gantt(schedule: Schedule, req: Requirements) -> str:
    """Text Gantt, one robot per line: ``EX1: [T1 0-6][T3 6-18]``; idle robots are listed too."""
    by_robot = schedule.by_robot()
    robot_ids = [r.id for r in req.robots]
    robot_ids += [rid for rid in by_robot if rid not in robot_ids]
    width = max(len(rid) for rid in robot_ids)
    lines = []
    for rid in robot_ids:
        bars = "".join(f"[{e.task} {e.start_h}-{e.end_h}]" for e in by_robot.get(rid, []))
        lines.append(f"{rid:<{width}}: {bars or '(idle)'}")
    lines.append(
        f"makespan {schedule.makespan_h}h ({schedule.status}, solve {schedule.solve_time_s:.2f}s)"
    )
    return "\n".join(lines)


def plan_summary(result: PlanResult, out_path: Path) -> str:
    if result.success and result.schedule is not None:
        return (
            f"verified plan: makespan {result.schedule.makespan_h}h of "
            f"{result.requirements.horizon_h}h horizon, {result.repairs} repair(s) -> {out_path}"
        )
    failure = result.failure
    why = "unknown failure"
    if failure is not None:
        why = f"{failure.kind}: {', '.join(failure.conflicts) or failure.hint or '-'}"
    return f"no verified plan after {result.repairs} repair(s) ({why}) -> {out_path}"


# --------------------------------------------------------------------------- plan
def cmd_plan(args: argparse.Namespace) -> int:
    brief, default_id = read_brief(args.brief)
    if not brief.strip():
        raise ValueError("the brief is empty")
    brief_id = clean_brief_id(args.brief_id) or default_id
    key = brief_key(brief, brief_id)
    client = make_client(args.llm)
    planner = Planner(
        client if args.quiet else NarratedClient(client),
        max_repairs=args.max_repairs,
        solver_time_limit_s=args.time_limit,
        explain=not args.no_explain,
    )
    say(
        f"planning {key}: llm={client.name} max_repairs={args.max_repairs} "
        f"time_limit={args.time_limit:g}s"
    )
    result = planner.plan(brief, brief_id=brief_id)
    out_path = args.out or OUT_DIR / f"{key}.plan.json"
    write_json(out_path, result)

    if not args.quiet:
        print(format_trace(result.steps))
        if result.schedule is not None:
            print("\n" + format_gantt(result.schedule, result.requirements))
        if result.explanation:
            print("\n" + result.explanation)
        print()
    print(plan_summary(result, out_path))
    return EXIT_OK if result.success else EXIT_NO_PLAN


# --------------------------------------------------------------------------- simulate
def positive_float(text: str) -> float:
    """argparse type for ``--time-limit`` / ``--dt``: a finite number > 0, else a usage error."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive finite number, got {text!r}")
    return value


def unit_float(text: str) -> float:
    """argparse type for ``--failure-rate`` / ``--jitter``: a finite number in [0, 1]."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"expected a number between 0 and 1, got {text!r}")
    return value


def int_in_range(lo: int, hi: int | None = None):
    """argparse type factory: a whole number >= ``lo`` (and <= ``hi`` when given)."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
        if value < lo or (hi is not None and value > hi):
            bounds = f">= {lo}" if hi is None else f"between {lo} and {hi}"
            raise argparse.ArgumentTypeError(f"expected a whole number {bounds}, got {text!r}")
        return value

    parse.__name__ = "int"
    return parse


def load_verified_plan(path: Path) -> PlanResult:
    """A plan file with a schedule that still passes :func:`check_schedule` (the file may have
    been edited by hand since ``moonwright plan`` wrote it), else a ``ValueError``."""
    plan = PlanResult.model_validate_json(path.read_text(encoding="utf-8"))
    if not plan.success or plan.schedule is None or plan.task_graph is None:
        raise ValueError(f"{path} holds no verified schedule; run `moonwright plan` first")
    conflicts = check_schedule(plan.requirements, plan.task_graph, plan.schedule)
    if conflicts:
        raise ValueError(f"{path} fails re-verification: {', '.join(conflicts)}")
    return plan


def parse_failure(spec: str) -> FailureInjection:
    """``EX2@30`` -> FailureInjection(robot='EX2', t_h=30.0)."""
    robot, sep, raw_t = spec.partition("@")
    if not sep or not robot:
        raise argparse.ArgumentTypeError(f"expected ROBOT@HOURS (e.g. EX2@30), got {spec!r}")
    try:
        t_h = float(raw_t)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"failure time must be a number of hours: {spec!r}"
        ) from None
    if not math.isfinite(t_h):
        raise argparse.ArgumentTypeError(f"failure time must be a finite number of hours: {spec!r}")
    if t_h < 0:
        raise argparse.ArgumentTypeError(f"failure time must be >= 0: {spec!r}")
    return FailureInjection(robot=robot, t_h=t_h)


def format_event(event: Event) -> str:
    who = " ".join(x for x in (event.robot, f"task {event.task}" if event.task else "") if x)
    line = f"t={event.t_h:7.2f}h  {event.kind:<14} {who}".rstrip()
    return f"{line}  {event.detail}" if event.detail else line


def format_replan(index: int, record: ReplanRecord) -> str:
    """One report line per replan: the event, the makespan before and after, and how the
    remaining work was rescheduled (solver-first, or escalated to Nemotron) as the closing
    ``replan`` step of the trace tells it."""
    after = record.after
    if after.success and after.schedule is not None:
        outcome = (
            f"makespan {record.before.makespan_h}h -> {after.schedule.makespan_h}h "
            f"({after.schedule.status}, {after.repairs} repair(s))"
        )
    elif after.failure is not None:
        outcome = f"no feasible replan ({after.failure.kind}: {', '.join(after.failure.conflicts) or '-'})"
    else:
        outcome = "no feasible replan"
    closing = after.steps[-1] if after.steps and after.steps[-1].stage == "replan" else None
    if closing is not None and closing.message:
        how = closing.message.split("; ", 1)[0]   # "CP-SAT rescheduled ..." / "infeasible (...) -> escalated ..."
    else:
        how = "escalated to Nemotron" if record.escalated else "solver-only, Nemotron not asked"
    return (
        f"#{index} at t={record.t_h:.2f}h after {record.event.kind} {record.event.robot or ''}: "
        f"{len(record.remaining_tasks)} tasks remaining; {outcome}; {how}"
    )


def cmd_simulate(args: argparse.Namespace) -> int:
    plan = load_verified_plan(args.plan)
    assert plan.task_graph is not None  # load_verified_plan checked it
    key = brief_key(plan.requirements.source_brief or "",
                    clean_brief_id(args.brief_id) or plan_stem(args.plan))
    client = make_client(args.llm)
    planner = Planner(
        NarratedClient(client),
        max_repairs=args.max_repairs,
        solver_time_limit_s=args.time_limit,
        explain=False,
    )
    injections: list[FailureInjection] = list(args.fail)
    say(
        f"simulating {key}: {len(plan.task_graph.tasks)} tasks, "
        f"failures={[f'{i.robot}@{i.t_h:g}' for i in injections] or 'none'}, "
        f"max_replans={args.max_replans} dt={args.dt:g}h"
    )
    result = run_with_replanning(
        planner, plan, injections, key=key, max_replans=args.max_replans, dt_h=args.dt
    )

    print(f"events ({len(result.events)}):")
    for event in result.events:
        print("  " + format_event(event))
    print(f"replans ({len(result.replans)}):")
    for i, record in enumerate(result.replans, 1):
        print("  " + format_replan(i, record))
    last_t = result.frames[-1].t_h if result.frames else 0.0
    if result.completed:
        finished = result.finished_at_h if result.finished_at_h is not None else last_t
        print(f"completed at t={finished:.2f}h")
    else:
        print(f"NOT completed (simulation stopped at t={last_t:.2f}h)")

    out_path = args.out or OUT_DIR / f"{key}.sim.json"
    saved = result if args.all_frames else thin_frames(result, FRAME_STRIDE)
    write_json(out_path, saved, indent=None)
    kept = f"all {len(saved.frames)}" if args.all_frames else f"{len(saved.frames)} of {len(result.frames)}"
    print(f"-> {out_path} ({kept} frames)")
    return EXIT_OK if result.completed else EXIT_NO_PLAN


# --------------------------------------------------------------------------- montecarlo
def cmd_montecarlo(args: argparse.Namespace) -> int:
    plan = load_verified_plan(args.plan)
    stem = plan_stem(args.plan)
    if args.llm is None:
        planner = solver_only_planner(solver_time_limit_s=args.time_limit)
    else:
        planner = MonteCarloPlanner(make_client(args.llm), max_repairs=args.max_repairs,
                                    solver_time_limit_s=args.time_limit, explain=False)
    say(
        f"monte carlo {stem}: {args.runs} runs, failure rate {args.failure_rate:g}/robot/h, "
        f"jitter {args.jitter:g}, max_replans={args.max_replans}, "
        f"seed={'random' if args.seed is None else args.seed}, replans via {planner.client.name}"
    )
    result = run_monte_carlo(
        plan, runs=args.runs, failure_rate_per_robot_h=args.failure_rate,
        duration_jitter=args.jitter, seed=args.seed, max_replans=args.max_replans,
        planner=planner, dt_h=args.dt,
    )
    print(format_monte_carlo(result))
    out_path = args.out or OUT_DIR / f"{stem}.mc.json"
    write_json(out_path, result)
    print(f"-> {out_path}")
    return EXIT_OK


# --------------------------------------------------------------------------- eval
class EvalRow(BaseModel):
    """One planning run of the eval (docs/plan-phase3.md item 3).

    ``models`` are the ``Usage.model`` ids of the task-graph / repair steps (unique, in order);
    ``answer_valid`` says whether the *first* task-graph answer passed the schema;
    ``first_try_feasible`` whether the plan was verified without a repair round (a schema
    re-ask is not a repair); ``first_*`` total the first valid graph and ``final_*`` the final
    one (scheduled, or the last one tried), so ``scope_drift_pct`` = 100 x (final - first) /
    first energy says how much work the repairs re-scoped (negative = cut); ``thinking`` is the
    ``MOONWRIGHT_THINKING`` setting the run used.
    """

    brief: str
    condition: str
    repeat: int
    success: bool
    repairs: int
    makespan_h: int | None = None
    latency_s: float
    llm_latency_s: float
    input_tokens: int
    output_tokens: int
    failure_kind: str | None = None
    conflicts: list[str] = Field(default_factory=list)
    models: str = ""
    answer_valid: bool = False
    first_try_feasible: bool = False
    first_duration_h: int | None = None
    first_energy_wh: float | None = None
    final_duration_h: int | None = None
    final_energy_wh: float | None = None
    scope_drift_pct: float | None = None
    thinking: str = "auto"


class ConditionSummary(BaseModel):
    condition: str
    runs: int
    feasible: int
    feasible_rate: float
    mean_repairs: float
    mean_makespan_h: float | None = None
    mean_latency_s: float
    mean_input_tokens: float
    mean_output_tokens: float
    feasible_ci_low: float = 0.0     # Wilson 95% interval of feasible_rate
    feasible_ci_high: float = 1.0
    first_try_feasible: int = 0
    first_try_rate: float = 0.0
    answer_valid_rate: float = 0.0
    mean_scope_drift_pct: float | None = None   # over runs with a first and a final graph


class EvalReport(BaseModel):
    generated_at: str
    llm: str
    briefs_dir: str
    conditions: list[str]
    repeat: int
    summary: list[ConditionSummary]
    rows: list[EvalRow]
    max_repairs: int = 3
    feedback: str = DEFAULT_FEEDBACK
    max_answer_retries: int | None = None
    workers: int = 1
    thinking: str = "auto"


class GraphCapture:
    """LLMClient wrapper that keeps every task graph a run's model calls returned (validated
    answers only, in order), so the eval can compare the first valid graph with the final one.
    Each graph is grounded the way the planner grounds a fresh plan's answer
    (:func:`~moonwright.pipeline.rates.apply_rates`, no remaining graph), so ``first_*`` and
    ``final_*`` are both post-rate-model figures and ``scope_drift_pct`` measures what the
    repair rounds re-scoped, not what the rate table recomputed. One per run: the wrapped
    client is shared between the eval's worker threads."""

    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.name = inner.name
        self.graphs: list[TaskGraph] = []

    def complete_json(
        self,
        *,
        role: Role,
        system: str,
        user: str,
        schema: type,
        key: str | None = None,
        attempt: int = 1,
        variant: str | None = None,
        max_attempts: int = 2,
    ):
        obj, usage = self.inner.complete_json(
            role=role, system=system, user=user, schema=schema,
            key=key, attempt=attempt, variant=variant, max_attempts=max_attempts,
        )
        if isinstance(obj, TaskGraph):
            self.graphs.append(apply_rates(obj)[0])
        return obj, usage


def graph_totals(graph: TaskGraph | None) -> tuple[int | None, float | None]:
    """``(sum of duration_h, sum of energy_wh)`` of a graph, ``(None, None)`` without one."""
    if graph is None:
        return None, None
    return (sum(t.duration_h for t in graph.tasks),
            round(sum(t.energy_wh for t in graph.tasks), 3))


def scope_drift(first_energy_wh: float | None, final_energy_wh: float | None) -> float | None:
    """Signed percentage change of total energy from the first valid graph to the final one
    (2 dp); None without both graphs or when the first graph carries no energy."""
    if first_energy_wh is None or final_energy_wh is None or first_energy_wh <= 0:
        return None
    return round(100.0 * (final_energy_wh - first_energy_wh) / first_energy_wh, 2)


def thinking_setting() -> str:
    """``MOONWRIGHT_THINKING`` as the eval records it: on | off | auto (unset or blank = auto)."""
    return os.environ.get(THINKING_ENV_VAR, "").strip().lower() or "auto"


def eval_row(
    brief_id: str,
    condition: str,
    repeat: int,
    result: PlanResult,
    wall_s: float,
    *,
    first_graph: TaskGraph | None = None,
    thinking: str = "auto",
) -> EvalRow:
    usage: Usage = result.total_usage()
    graph_steps = [s for s in result.steps if s.stage in ("taskgraph", "repair")]
    models = dict.fromkeys(s.usage.model for s in graph_steps if s.usage and s.usage.model)
    first_duration, first_energy = graph_totals(first_graph)
    final_duration, final_energy = graph_totals(result.task_graph)
    return EvalRow(
        brief=brief_id,
        condition=condition,
        repeat=repeat,
        success=result.success,
        repairs=result.repairs,
        makespan_h=result.schedule.makespan_h if result.schedule else None,
        latency_s=wall_s,
        llm_latency_s=usage.latency_s,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        failure_kind=result.failure.kind if result.failure else None,
        conflicts=list(result.failure.conflicts) if result.failure else [],
        models=",".join(models),
        answer_valid=bool(graph_steps) and graph_steps[0].ok,
        first_try_feasible=result.success and not any(s.stage == "repair" for s in result.steps),
        first_duration_h=first_duration,
        first_energy_wh=first_energy,
        final_duration_h=final_duration,
        final_energy_wh=final_energy,
        scope_drift_pct=scope_drift(first_energy, final_energy),
        thinking=thinking,
    )


def wilson_interval(successes: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for a proportion (95% with the default ``z``), clipped to [0, 1];
    ``(0, 1)`` when there is no data. Unlike the normal approximation it never collapses to a
    point at 0/n or n/n, which is what a 3-repeat eval table needs."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def summarise(rows: Sequence[EvalRow]) -> list[ConditionSummary]:
    """Per-condition aggregates in first-seen order; makespan is averaged over feasible runs,
    scope drift over runs that have one, the CI is :func:`wilson_interval` of the feasible rate."""
    out: list[ConditionSummary] = []
    for condition in dict.fromkeys(r.condition for r in rows):
        group = [r for r in rows if r.condition == condition]
        n = len(group)
        makespans = [r.makespan_h for r in group if r.success and r.makespan_h is not None]
        drifts = [r.scope_drift_pct for r in group if r.scope_drift_pct is not None]
        feasible = sum(r.success for r in group)
        first_try = sum(r.first_try_feasible for r in group)
        low, high = wilson_interval(feasible, n)
        out.append(
            ConditionSummary(
                condition=condition,
                runs=n,
                feasible=feasible,
                feasible_rate=feasible / n,
                mean_repairs=statistics.fmean(r.repairs for r in group),
                mean_makespan_h=statistics.fmean(makespans) if makespans else None,
                mean_latency_s=statistics.fmean(r.latency_s for r in group),
                mean_input_tokens=statistics.fmean(r.input_tokens for r in group),
                mean_output_tokens=statistics.fmean(r.output_tokens for r in group),
                feasible_ci_low=round(low, 4),
                feasible_ci_high=round(high, 4),
                first_try_feasible=first_try,
                first_try_rate=first_try / n,
                answer_valid_rate=sum(r.answer_valid for r in group) / n,
                mean_scope_drift_pct=round(statistics.fmean(drifts), 2) if drifts else None,
            )
        )
    return out


def format_summary(summary: Sequence[ConditionSummary]) -> str:
    header = (
        f"{'condition':<10}{'runs':>6}{'feasible':>10}{'95% CI':>12}{'1st-try':>9}"
        f"{'repairs':>9}{'makespan':>10}{'drift':>8}{'latency':>9}{'tok in/out':>14}"
    )
    lines = [header]
    for s in summary:
        makespan = f"{s.mean_makespan_h:.1f}h" if s.mean_makespan_h is not None else "-"
        ci = f"{s.feasible_ci_low:.0%}-{s.feasible_ci_high:.0%}"
        drift = f"{s.mean_scope_drift_pct:+.0f}%" if s.mean_scope_drift_pct is not None else "-"
        tokens = f"{s.mean_input_tokens:.0f}/{s.mean_output_tokens:.0f}"
        lines.append(
            f"{s.condition:<10}{s.runs:>6}{s.feasible_rate:>10.0%}{ci:>12}{s.first_try_rate:>9.0%}"
            f"{s.mean_repairs:>9.2f}{makespan:>10}{drift:>8}{s.mean_latency_s:>8.2f}s{tokens:>14}"
        )
    return "\n".join(lines)


CSV_FIELDS: Final = tuple(EvalRow.model_fields)


def csv_record(row: EvalRow) -> dict[str, Any]:
    record = row.model_dump()
    record["conflicts"] = ";".join(row.conflicts)
    return record


class CsvSink:
    """``eval.csv`` written one row at a time: the header on open, every row appended and
    flushed the moment it arrives, so a run killed half-way keeps the rows it finished."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None
        self._writer: csv.DictWriter | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=list(CSV_FIELDS))
        self._writer.writeheader()
        self._fh.flush()
        return self

    def add(self, row: EvalRow) -> None:
        assert self._writer is not None and self._fh is not None
        self._writer.writerow(csv_record(row))
        self._fh.flush()

    def __exit__(self, *exc: object) -> None:
        if self._fh is not None:
            self._fh.close()


def write_csv(path: Path, rows: Sequence[EvalRow]) -> None:
    with CsvSink(path) as sink:
        for row in rows:
            sink.add(row)


def planner_options(feedback: str, max_answer_retries: int | None) -> dict[str, Any]:
    """Keyword arguments for :class:`Planner` from ``--feedback`` / ``--max-answer-retries``.

    Only options this build's ``Planner`` accepts are passed (``feedback_mode`` and
    ``max_answer_retries`` land with the repair-loop work); asking for a value the planner
    cannot honour is an input error rather than a silently different experiment.
    """
    params = inspect.signature(Planner.__init__).parameters
    options: dict[str, Any] = {}
    if "feedback_mode" in params:
        options["feedback_mode"] = feedback
    elif feedback != DEFAULT_FEEDBACK:
        raise ValueError(
            f"--feedback {feedback} is not supported by this Planner (no feedback_mode option)"
        )
    if max_answer_retries is not None:
        if "max_answer_retries" not in params:
            raise ValueError("--max-answer-retries is not supported by this Planner")
        options["max_answer_retries"] = max_answer_retries
    return options


EvalJob = tuple[ExampleBrief, str, int]


def run_eval_jobs(
    jobs: Sequence[EvalJob],
    run: Callable[[ExampleBrief, str, int], EvalRow],
    *,
    workers: int,
    csv_path: Path,
    report: Callable[[EvalRow], None],
) -> list[EvalRow]:
    """Run every ``(brief, condition, repeat)`` job, ``workers`` at a time, appending each row
    to ``csv_path`` and reporting it the moment it completes (so a killed run keeps the rows
    it finished, in completion order); the rows come back in job order whatever order they
    finished in. An interrupt cancels the jobs not yet started."""
    done: dict[int, EvalRow] = {}
    with CsvSink(csv_path) as sink, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, *job): i for i, job in enumerate(jobs)}
        try:
            for future in as_completed(futures):
                row = future.result()
                done[futures[future]] = row
                sink.add(row)
                report(row)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return [done[i] for i in range(len(jobs))]


def report_row(row: EvalRow) -> None:
    verdict = f"makespan {row.makespan_h}h" if row.success else f"FAIL {row.failure_kind}"
    say(
        f"  {row.brief:<16} {row.condition:<8} #{row.repeat} {verdict}, "
        f"{row.repairs} repair(s), {row.latency_s:.1f}s"
    )


def cmd_eval(args: argparse.Namespace) -> int:
    briefs = list_briefs(args.briefs)
    if not briefs:
        raise ValueError(f"no briefs (*.md, *.txt) found in {args.briefs}")
    conditions = CONDITIONS[args.loop]
    options = planner_options(args.feedback, args.max_answer_retries)
    client = make_client(args.llm)
    thinking = thinking_setting()
    jobs: list[EvalJob] = [
        (brief, condition, repeat)
        for brief in briefs
        for condition in conditions
        for repeat in range(1, args.repeat + 1)
    ]
    say(
        f"eval: {len(briefs)} brief(s) x {len(conditions)} condition(s) x {args.repeat} repeat(s), "
        f"llm={client.name} workers={args.workers} feedback={args.feedback} thinking={thinking}"
    )

    def run(brief: ExampleBrief, condition: str, repeat: int) -> EvalRow:
        capture = GraphCapture(client)
        planner = Planner(
            capture,
            max_repairs=args.max_repairs if condition == "loop_on" else 0,
            solver_time_limit_s=args.time_limit,
            explain=args.explain,
            **options,
        )
        t0 = time.perf_counter()
        result = planner.plan(brief.text_markdown, brief_id=brief.id)
        return eval_row(
            brief.id, condition, repeat, result, time.perf_counter() - t0,
            first_graph=capture.graphs[0] if capture.graphs else None, thinking=thinking,
        )

    rows = run_eval_jobs(jobs, run, workers=args.workers, csv_path=args.csv, report=report_row)
    summary = summarise(rows)
    print(format_summary(summary))
    report = EvalReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        llm=client.name,
        briefs_dir=str(args.briefs),
        conditions=list(conditions),
        repeat=args.repeat,
        summary=summary,
        rows=rows,
        max_repairs=args.max_repairs,
        feedback=args.feedback,
        max_answer_retries=args.max_answer_retries,
        workers=args.workers,
        thinking=thinking,
    )
    write_json(args.out, report)
    print(f"-> {args.out}, {args.csv}")
    return EXIT_OK


# --------------------------------------------------------------------------- serve
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn  # server-only dependency; imported here so the other commands start fast

    uvicorn.run("moonwright.api:app", host=args.host, port=args.port, log_level="info")
    return EXIT_OK


# --------------------------------------------------------------------------- parser / main
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm", choices=KINDS, default=None,
        help="LLM backend (default: MOONWRIGHT_LLM, else nebius if NEBIUS_API_KEY is set, else fixture)",
    )
    parser.add_argument(
        "--time-limit", type=positive_float, default=10.0, metavar="S",
        help="CP-SAT time limit in seconds",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moonwright",
        description="Natural-language lunar construction brief -> solver-verified robot schedule.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="brief -> task graph -> validated CP-SAT schedule")
    p.add_argument("brief", help="path to a brief (.md/.txt) or '-' for stdin")
    p.add_argument("--brief-id", help="stable id for fixtures and output names (default: file stem)")
    _add_common(p)
    p.add_argument("--max-repairs", type=int_in_range(0, MAX_REPAIRS_LIMIT), default=3,
                   help=f"LLM repair rounds after a failure, 0..{MAX_REPAIRS_LIMIT}")
    p.add_argument("--out", type=Path, help="PlanResult JSON path (default out/<brief_id>.plan.json)")
    p.add_argument("--no-explain", action="store_true", help="skip the LLM explanation stage")
    p.add_argument("--quiet", "-q", action="store_true", help="only print the final summary line")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("simulate", help="replay a plan with robot failures and replanning")
    p.add_argument("plan", type=Path, help="PlanResult JSON written by `moonwright plan`")
    p.add_argument(
        "--fail", action="append", default=[], type=parse_failure, metavar="ROBOT@T",
        help="inject a robot failure at T hours (repeatable), e.g. --fail EX2@30",
    )
    p.add_argument("--dt", type=positive_float, default=DEFAULT_DT_H,
                   help="simulation step in hours")
    p.add_argument("--max-replans", type=int_in_range(0, 5), default=2, help="0..5")
    p.add_argument("--max-repairs", type=int_in_range(0, MAX_REPAIRS_LIMIT), default=3,
                   help=f"LLM repair rounds per replan, 0..{MAX_REPAIRS_LIMIT}")
    p.add_argument("--brief-id", help="fixture key for offline replans (default: plan file stem)")
    _add_common(p)
    p.add_argument("--out", type=Path, help="SimResult JSON path (default out/<brief_id>.sim.json)")
    p.add_argument(
        "--all-frames", action="store_true",
        help=f"keep every frame in the JSON (default: every {FRAME_STRIDE}th plus the last)",
    )
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser(
        "montecarlo",
        help="robustness: replay a plan N times with random robot failures and duration jitter",
    )
    p.add_argument("plan", type=Path, help="PlanResult JSON written by `moonwright plan`")
    p.add_argument("--runs", type=int_in_range(1), default=50, help="number of replays (default 50)")
    p.add_argument("--failure-rate", type=unit_float, default=0.002, metavar="PER_H",
                   help="per-robot failure probability per hour, 0..1 (default 0.002)")
    p.add_argument("--jitter", type=unit_float, default=0.15,
                   help="relative task-duration spread, 0..1 (default 0.15)")
    p.add_argument("--seed", type=int, help="base seed; run i uses seed+i (default: random)")
    p.add_argument("--max-replans", type=int_in_range(0, 5), default=2,
                   help="replans per run, 0..5")
    p.add_argument("--dt", type=positive_float, default=DEFAULT_DT_H,
                   help="simulation step in hours")
    p.add_argument(
        "--llm", choices=KINDS, default=None,
        help="replan through this LLM client instead of the solver-only replanner (default)",
    )
    p.add_argument("--max-repairs", type=int_in_range(0, MAX_REPAIRS_LIMIT), default=3,
                   help=f"LLM repair rounds per replan, 0..{MAX_REPAIRS_LIMIT} (only with --llm)")
    p.add_argument("--time-limit", type=positive_float, default=10.0, metavar="S",
                   help="CP-SAT time limit per replan in seconds")
    p.add_argument("--out", type=Path, help="MonteCarloResult JSON path (default out/<stem>.mc.json)")
    p.set_defaults(func=cmd_montecarlo)

    p = sub.add_parser("eval", help="plan every brief in a directory with the repair loop off/on")
    p.add_argument("--briefs", type=Path, required=True, help="directory of *.md / *.txt briefs")
    p.add_argument("--repeat", type=int_in_range(1), default=1)
    p.add_argument("--loop", choices=sorted(CONDITIONS), default="both",
                   help="repair loop off (max_repairs 0), on, or both")
    _add_common(p)
    p.add_argument("--max-repairs", type=int_in_range(0, MAX_REPAIRS_LIMIT), default=3,
                   help=f"repair rounds for the loop-on condition, 0..{MAX_REPAIRS_LIMIT}")
    p.add_argument(
        "--max-answer-retries", type=int_in_range(0, MAX_ANSWER_RETRIES_LIMIT), default=None,
        metavar="N",
        help=f"re-asks after a schema / transport failure, 0..{MAX_ANSWER_RETRIES_LIMIT}, in "
             "both conditions (default: the planner's own)",
    )
    p.add_argument(
        "--feedback", choices=FEEDBACK_MODES, default=DEFAULT_FEEDBACK,
        help="what a repair round is told: the minimal infeasibility core with its hints "
             "(core) or only the verdict kind (verdict, the ablation arm)",
    )
    p.add_argument(
        "--workers", type=int_in_range(1), default=1, metavar="N",
        help="parallel runs over brief x condition x repeat; rows reach the CSV as they finish",
    )
    p.add_argument("--explain", action="store_true", help="include the explanation stage")
    p.add_argument("--out", type=Path, default=Path("eval.json"))
    p.add_argument("--csv", type=Path, default=Path("eval.csv"))
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("serve", help="run the FastAPI app with uvicorn")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (LLMError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
