"""Command-line entry point: ``moonwright plan | simulate | montecarlo | eval | serve``.

Progress lines go to stderr; the report (trace, Gantt, explanation, tables) goes to stdout;
every run also writes its result model as JSON so the UI and notebooks can pick it up.
Exit codes: 0 success, 1 pipeline finished without a feasible plan / incomplete simulation,
2 configuration or input error.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import io
import json
import math
import os
import re
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self

from pydantic import BaseModel, Field

from moonwright.api import ExampleBrief, clean_brief_id, list_briefs, thin_frames
from moonwright.llm.base import FixtureClient, LLMClient, LLMError
from moonwright.llm.factory import KINDS, load_env, make_client, resolve_kind
from moonwright.llm.prices import cost_usd, format_usd
from moonwright.llm.tokenfactory import (
    THINKING_ENV_VAR,
    RecordingClient,
    TokenFactoryClient,
    models_from_env,
)
from moonwright.pipeline.loop import Planner, brief_key, unstructured_result
from moonwright.pipeline.rates import (
    IN_SCOPE_SHARE,
    apply_rates,
    area_shares,
    volume_advisories,
    volume_shares,
)
from moonwright.pipeline.relax import format_suggestions
from moonwright.pipeline.schedule import check_schedule
from moonwright.pipeline.validate import in_scope_gaps
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
    percentile,
    run_monte_carlo,
    solver_only_planner,
)
from moonwright.sim.replan import run_with_replanning
from moonwright.sim.simulator import FailureInjection
from moonwright.text import pct, plural

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
ARM_MODELS: Final[dict[str, str]] = {         # eval `--models` arm -> task-graph + repair model
    "ultra": "nvidia/Nemotron-3-Ultra-550b-a55b",
    "super": "nvidia/nemotron-3-super-120b-a12b",
    "nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "lightning": "nvidia/Nemotron-3_5-Lightning",
}
ARM_ROLES: Final = ("taskgraph", "repair")    # the roles an arm (and --thinking) re-routes
THINKING_CHOICES: Final = ("on", "off", "auto")
ARM_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+$")   # no "/" or ",": it joins the condition name
REJECTED_KINDS: Final = frozenset({"schema", "llm_error"})   # an answer rejected before any verdict
FILE_NAME_RE: Final = re.compile(r"[^A-Za-z0-9_.-]+")        # --save-plans file names


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
    """The closing line(s): a verified plan names its makespan, and when the final graph is
    not in scope (:func:`~moonwright.pipeline.validate.in_scope_gaps`: a structure's volume or
    area of work under 95 % of what its dimensions give, or an operation its entry in the
    brief states that the structuring omitted and no task performs) or plans clearly more m3
    than they imply (the ``volume:<structure>`` excess advisories), those lines follow it, so
    "verified" is never read as "built to the brief's size". The header names the dimensions
    for a volume or an area, the brief when a structure's entry states an operation no task
    performs ('scope:<structure>')."""
    if result.success and result.schedule is not None:
        line = (
            f"verified plan: makespan {result.schedule.makespan_h}h of "
            f"{result.requirements.horizon_h}h horizon, {result.repairs} repair(s) -> {out_path}"
        )
        scope: list[str] = []
        if result.task_graph is not None:
            scope = in_scope_gaps(result.requirements, result.task_graph)
            scope += [a for a in volume_advisories(result.requirements, result.task_graph)
                      if a not in scope]
        if scope:
            # a restored operation left undone (a 'scope:' line, phase 3l) is the brief's, not
            # the dimensions'; a volume or area shortfall or an excess keeps the old header
            what = ("brief" if any(a.startswith("scope:") for a in scope) else "dimensions")
            line += (f"\nverified against the planning model; scope differs from the {what}:\n"
                     + "\n".join(f"  {a}" for a in scope))
        return line
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
        for line in format_suggestions(result):   # a failed run: what would make it feasible
            print(line)
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
        for line in format_suggestions(record.after):   # a failed replan: the solver's advice
            print("    " + line)
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
                                    solver_time_limit_s=args.time_limit, explain=False,
                                    suggest_budget_s=0.0)   # advice is never shown here
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
    thinking setting the run used (``--thinking``, else ``MOONWRIGHT_THINKING``); ``cost_usd``
    is what the run's model calls cost at the Token Factory list prices
    (:func:`run_cost_usd`: 0 for an offline run, None when a model is not priced).
    ``volume_advisories`` counts the ``volume:<structure>`` advisories on the final graph
    (:func:`~moonwright.pipeline.rates.volume_advisories`: a structure whose planned m3 are
    below 70 % or above 150 % of what its dimensions imply), ``min_volume_share`` is the
    smallest judged volume share over the structures the check could judge and
    ``min_area_share`` the smallest compaction / sintering area share
    (:func:`~moonwright.pipeline.rates.area_shares`: footprint x lifts), None when nothing
    was judged (no dimensions, no quantities); ``in_scope`` is ``success`` with every judged
    volume and area at 95 % or more of the smallest size the brief allows
    (:func:`~moonwright.pipeline.rates.scope_gaps`) and every operation its structures' own
    entries in the brief state done (:func:`~moonwright.pipeline.validate.restored_gaps`,
    phase 3l: the ones the structuring omitted; both in
    :func:`~moonwright.pipeline.validate.in_scope_gaps`), the rate the eval reports as
    "feasible in scope": an impossible brief "verified" by digging a third of the pad, by
    trimming the dig or the compaction lifts 15-25 %, or by never compacting a pad its entry
    says is compacted, is never a success in scope, while an excess
    (hauled twice over) is counted in ``volume_advisories`` but is no gap. ``requirements_sha``
    identifies the structured requirements the run planned from (the first 10 hex digits of
    the SHA-1 of their JSON): every condition of a brief and repeat shares one, which is
    what makes the conditions a paired comparison. ``kind`` is the brief's designed kind
    (``--kinds``, else ``kinds.json`` next to the briefs; empty when unknown).
    ``rejected_answers`` counts the task-graph / repair answers the run could not use: steps
    of those stages with ``ok`` False and a ``schema`` or ``llm_error`` failure (an answer
    cut off at max_tokens and re-asked counts once per re-ask; a verdict is never one).
    ``plan_path`` is the run's PlanResult file (``--save-plans DIR``,
    :func:`plan_file_name`), relative to the CSV's directory (:func:`csv_relative`: rows a
    ``--resume`` kept from a run that saved its plans elsewhere still name their own files;
    empty when the run kept no plan). New columns go at the end so the CSV header keeps its
    prefix; ``plan_path`` stays last.
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
    cost_usd: float | None = None
    volume_advisories: int = 0
    min_volume_share: float | None = None
    in_scope: bool | None = None     # None: a row recorded before the column existed
    requirements_sha: str = ""
    kind: str = ""
    rejected_answers: int = 0
    min_area_share: float | None = None
    plan_path: str = ""           # --save-plans: the run's PlanResult, relative to the CSV


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
    latency_p50_s: float = 0.0                  # wall time per run (percentile, linear interp.)
    latency_p95_s: float = 0.0
    mean_llm_latency_s: float = 0.0             # the model calls' share of the wall time
    mean_cost_usd: float | None = None          # over the priced runs; None when none is priced
    total_cost_usd: float | None = None
    priced_runs: int = 0
    in_scope: int = 0                           # verified with no volume advisory
    in_scope_rate: float = 0.0
    in_scope_ci_low: float = 0.0
    in_scope_ci_high: float = 1.0
    rejected_runs: int = 0                      # runs with a schema / llm_error answer rejected
    rejected_answers: int = 0                   # those rejections, summed over the runs
    cost_p50_usd: float | None = None           # median cost of a priced run


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
    arms: dict[str, str] = Field(default_factory=dict)         # --models arm -> model id
    role_models: dict[str, str] = Field(default_factory=dict)  # structure / explain (live runs)
    resumed_rows: int = 0            # --resume: rows kept from the CSV of an earlier run
    plans_dir: str | None = None     # --save-plans DIR (``plan_path`` in the rows)


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


def thinking_overrides(setting: str | None) -> dict[str, bool]:
    """The client's per-role thinking overrides for ``--thinking``: ``on`` / ``off`` switch the
    task-graph and repair roles (structure and explain keep their own setting); ``auto`` or
    None leaves every role to ``MOONWRIGHT_THINKING`` and the client defaults."""
    if setting is None or setting == "auto":
        return {}
    if setting not in THINKING_CHOICES:
        raise ValueError(
            f"--thinking must be one of {', '.join(THINKING_CHOICES)}, got {setting!r}"
        )
    return dict.fromkeys(ARM_ROLES, setting == "on")


def parse_arms(spec: str | None) -> dict[str, str] | None:
    """``--models`` -> ordered ``{arm: model id}``. An arm is one of :data:`ARM_MODELS`
    (``ultra``, ``super``, ``nano``, ``lightning``) or ``name=model-id`` for any other
    catalogue model; None when nothing was asked for (the conditions keep their plain
    ``loop_*`` names). Unknown arms, bad names and duplicates are a ``ValueError``."""
    if spec is None or not spec.strip():
        return None
    arms: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, model = part.partition("=")
        name, model = name.strip().lower(), model.strip()
        if sep:
            if not name or not model:
                raise ValueError(f"--models: expected name=model-id, got {part!r}")
        elif name in ARM_MODELS:
            model = ARM_MODELS[name]
        else:
            raise ValueError(f"--models: unknown arm {part!r}; expected one of "
                             f"{', '.join(ARM_MODELS)} or name=model-id")
        if not ARM_NAME_RE.match(name):
            raise ValueError(f"--models: arm name {name!r} may only use letters, digits, _ . -")
        if name in arms:
            raise ValueError(f"--models: arm {name!r} is listed twice")
        arms[name] = model
    return arms or None


def condition_name(arm: str | None, loop: str) -> str:
    """``ultra/loop_on`` for an arm, the bare ``loop_on`` without one."""
    return f"{arm}/{loop}" if arm else loop


def make_arm_client(kind: str | None, model: str | None = None,
                    thinking: Mapping[str, bool] | None = None) -> LLMClient:
    """The client of one eval arm. ``model`` routes the task-graph and repair roles (None
    keeps ``MOONWRIGHT_MODEL_*`` / the defaults; structure and explain are never touched) and
    ``thinking`` overrides the per-role switch (:func:`thinking_overrides`). The fixture client
    ignores both (the arm name is kept on the condition); ``record`` wraps the live client.
    One live client serves every worker thread: it only keeps per-model request modes and a
    locked call log, so concurrent planners are safe on it."""
    load_env()
    resolved = resolve_kind(kind)
    if resolved == "fixture":
        return FixtureClient()
    if resolved not in KINDS:
        raise ValueError(
            f"unknown LLM client kind {resolved!r}; expected one of {', '.join(KINDS)}"
        )
    models = dict.fromkeys(ARM_ROLES, model) if model else None
    live = TokenFactoryClient(models=models, thinking=dict(thinking) if thinking else None)
    return RecordingClient(live) if resolved == "record" else live


def run_cost_usd(result: PlanResult) -> float | None:
    """What the run's model calls cost at the Token Factory list prices
    (``moonwright.llm.prices``), summed over the steps that carry token usage. 0 for a run
    without tokens (offline fixtures); None when a step with tokens names a model the price
    table does not know, so an unpriced model never shows up as free."""
    total = 0.0
    for step in result.steps:
        usage = step.usage
        if usage is None or not (usage.input_tokens or usage.output_tokens):
            continue
        cost = cost_usd(usage.model, usage.input_tokens, usage.output_tokens, decimals=None)
        if cost is None:
            return None
        total += cost
    return round(total, 6)


def requirements_sha(req: Requirements | None) -> str:
    """The first 10 hex digits of the SHA-1 of the requirements' JSON (without the brief
    text): the same for every condition that planned from one structuring; "" without one."""
    if req is None:
        return ""
    return hashlib.sha1(req.model_dump_json(exclude={"source_brief"}).encode()).hexdigest()[:10]


def load_kinds(path: Path | None, briefs_dir: Path) -> dict[str, str]:
    """``{brief id: kind}`` from ``--kinds PATH`` (a JSON object), else from ``kinds.json`` in
    the briefs directory when it exists, else empty. A malformed file is an input error."""
    source = path if path is not None else briefs_dir / "kinds.json"
    if path is None and not source.is_file():
        return {}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"--kinds {source}: {e}") from None
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"--kinds {source}: expected a JSON object of brief id -> kind")
    return data


def eval_row(
    brief_id: str,
    condition: str,
    repeat: int,
    result: PlanResult,
    wall_s: float,
    *,
    first_graph: TaskGraph | None = None,
    thinking: str = "auto",
    kind: str = "",
    sha: str | None = None,
    plan_path: str = "",
) -> EvalRow:
    """The row of one run. ``sha`` is the :func:`requirements_sha` of the requirements the
    run planned from; when None it is derived from the result (empty when the run never got
    past structuring). ``plan_path`` is where ``--save-plans`` wrote the run's PlanResult,
    relative to the CSV's directory."""
    if sha is None:
        structured = any(s.stage == "structure" and s.ok for s in result.steps)
        sha = requirements_sha(result.requirements) if structured else ""
    usage: Usage = result.total_usage()
    graph_steps = [s for s in result.steps if s.stage in ("taskgraph", "repair")]
    models = dict.fromkeys(s.usage.model for s in graph_steps if s.usage and s.usage.model)
    first_duration, first_energy = graph_totals(first_graph)
    final_duration, final_energy = graph_totals(result.task_graph)
    volume = (volume_advisories(result.requirements, result.task_graph)
              if result.task_graph is not None else [])
    shares = (volume_shares(result.requirements, result.task_graph)
              if result.task_graph is not None else {})
    areas = (area_shares(result.requirements, result.task_graph)
             if result.task_graph is not None else {})
    gaps = (in_scope_gaps(result.requirements, result.task_graph)
            if result.task_graph is not None else [])
    rejected = sum(1 for s in graph_steps
                   if not s.ok and s.failure is not None and s.failure.kind in REJECTED_KINDS)
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
        cost_usd=run_cost_usd(result),
        volume_advisories=len(volume),
        min_volume_share=round(min(shares.values()), 3) if shares else None,
        in_scope=result.success and not gaps,
        requirements_sha=sha,
        kind=kind,
        rejected_answers=rejected,
        min_area_share=round(min(areas.values()), 3) if areas else None,
        plan_path=plan_path,
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
    scope drift over runs that have one, the CI is :func:`wilson_interval` of the feasible rate,
    ``rejected_runs`` counts the runs with a rejected task-graph / repair answer
    (``rejected_answers`` those answers) and ``cost_p50_usd`` is the median cost of a priced
    run next to the mean."""
    out: list[ConditionSummary] = []
    for condition in dict.fromkeys(r.condition for r in rows):
        group = [r for r in rows if r.condition == condition]
        n = len(group)
        makespans = [r.makespan_h for r in group if r.success and r.makespan_h is not None]
        drifts = [r.scope_drift_pct for r in group if r.scope_drift_pct is not None]
        feasible = sum(r.success for r in group)
        first_try = sum(r.first_try_feasible for r in group)
        in_scope = sum(r.in_scope is True for r in group)
        low, high = wilson_interval(feasible, n)
        scope_low, scope_high = wilson_interval(in_scope, n)
        latencies = [r.latency_s for r in group]
        costs = [r.cost_usd for r in group if r.cost_usd is not None]
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
                latency_p50_s=round(percentile(latencies, 0.5), 3),
                latency_p95_s=round(percentile(latencies, 0.95), 3),
                mean_llm_latency_s=statistics.fmean(r.llm_latency_s for r in group),
                mean_cost_usd=round(statistics.fmean(costs), 6) if costs else None,
                total_cost_usd=round(sum(costs), 6) if costs else None,
                priced_runs=len(costs),
                in_scope=in_scope,
                in_scope_rate=in_scope / n,
                in_scope_ci_low=round(scope_low, 4),
                in_scope_ci_high=round(scope_high, 4),
                rejected_runs=sum(r.rejected_answers > 0 for r in group),
                rejected_answers=sum(r.rejected_answers for r in group),
                cost_p50_usd=round(percentile(costs, 0.5), 6) if costs else None,
            )
        )
    return out


def format_summary(summary: Sequence[ConditionSummary]) -> str:
    """The per-condition text table (stdout, and stderr at the end of a run): feasible rate
    with its Wilson interval, the in-scope rate (verified with every judged volume and area
    at 95 % or more of the brief's), first-try
    rate, repairs, makespan, scope drift, mean and p95 wall latency, tokens and the mean cost
    of a priced run (``-`` when no run is priced)."""
    width = max([10, *(len(s.condition) + 1 for s in summary)])
    header = (
        f"{'condition':<{width}}{'runs':>6}{'feasible':>10}{'95% CI':>12}{'in-scope':>10}"
        f"{'1st-try':>9}{'repairs':>9}{'makespan':>10}{'drift':>8}{'latency':>9}{'p95':>8}"
        f"{'tok in/out':>14}{'cost/run':>10}"
    )
    lines = [header]
    for s in summary:
        makespan = f"{s.mean_makespan_h:.1f}h" if s.mean_makespan_h is not None else "-"
        ci = f"{s.feasible_ci_low:.0%}-{s.feasible_ci_high:.0%}"
        drift = f"{s.mean_scope_drift_pct:+.0f}%" if s.mean_scope_drift_pct is not None else "-"
        tokens = f"{s.mean_input_tokens:.0f}/{s.mean_output_tokens:.0f}"
        cost = format_usd(s.mean_cost_usd)
        lines.append(
            f"{s.condition:<{width}}{s.runs:>6}{s.feasible_rate:>10.0%}{ci:>12}"
            f"{s.in_scope_rate:>10.0%}{s.first_try_rate:>9.0%}{s.mean_repairs:>9.2f}"
            f"{makespan:>10}{drift:>8}{s.mean_latency_s:>8.2f}s{s.latency_p95_s:>7.1f}s"
            f"{tokens:>14}{cost:>10}"
        )
    return "\n".join(lines)


CSV_FIELDS: Final = tuple(EvalRow.model_fields)


def csv_record(row: EvalRow) -> dict[str, Any]:
    record = row.model_dump()
    record["conflicts"] = ";".join(row.conflicts)
    return record


def csv_row(record: Mapping[str, str | None]) -> EvalRow:
    """:func:`csv_record` inverted: the row of one CSV record. An empty cell is the field's
    default (None for an optional column, so a row written before a column existed still
    parses), ``conflicts`` splits on ``;`` and columns this build does not know are ignored.
    A value that does not fit its column is a ``ValueError``."""
    data: dict[str, Any] = {k: v for k, v in record.items()
                            if k in EvalRow.model_fields and v not in ("", None)}
    data["conflicts"] = [c for c in (record.get("conflicts") or "").split(";") if c]
    return EvalRow.model_validate(data)


def _read_csv(path: Path) -> tuple[list[str], list[EvalRow], bool]:
    """:func:`read_csv` plus whether a cut-off last record was left out."""
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    records: list[tuple[int, dict[str, str | None]]] = []
    for record in reader:
        records.append((reader.line_num, record))
    header = list(reader.fieldnames or [])
    cut = bool(records) and (not text.endswith(("\n", "\r"))
                             or any(v is None for v in records[-1][1].values())
                             or len(records[-1][1]) < len(header))
    if cut:
        records.pop()
    rows: list[EvalRow] = []
    for line, record in records:
        try:
            rows.append(csv_row(record))
        except ValueError as e:   # pydantic's ValidationError is one
            raise ValueError(f"{path} line {line}: {e}") from None
    return header, rows, cut


def read_csv(path: Path) -> tuple[list[str], list[EvalRow]]:
    """``(header, rows)`` of an eval CSV; a row that does not parse is an input error naming
    the file and the line. A last record that is cut off (no newline after it, or fewer
    fields than the header: a run killed while appending it) is left out: it is no finished
    run, and its job runs again on ``--resume``."""
    header, rows, _cut = _read_csv(path)
    return header, rows


def resume_csv(path: Path) -> list[EvalRow]:
    """The rows an earlier run left in ``path`` (``--resume``), one per (brief, condition,
    repeat) with the last occurrence winning. A CSV whose header is not this build's
    :data:`CSV_FIELDS` (written by an older build), or whose last record was cut off, is
    rewritten with the current header and its rows re-serialised, so the rows appended next
    line up with it and the cut record is gone for good."""
    header, rows, cut = _read_csv(path)
    unique = {(r.brief, r.condition, r.repeat): r for r in rows}
    rows = list(unique.values())
    if cut or tuple(header) != CSV_FIELDS:
        write_csv(path, rows)
    return rows


class CsvSink:
    """``eval.csv`` written one row at a time: the header on open, every row appended and
    flushed the moment it arrives, so a run killed half-way keeps the rows it finished.
    ``append`` (``--resume``) keeps the rows already in the file and writes the header only
    when the file is new or empty, so the file never carries two headers."""

    def __init__(self, path: Path, *, append: bool = False):
        self.path = path
        self.append = append
        self._fh = None
        self._writer: csv.DictWriter | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        resume = self.append and self.path.is_file() and self.path.stat().st_size > 0
        missing_newline = False
        if resume:   # a file whose last line has no newline would swallow the first new row
            with self.path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                missing_newline = fh.read(1) not in (b"\n", b"\r")
        self._fh = self.path.open("a" if resume else "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=list(CSV_FIELDS))
        if not resume:
            self._writer.writeheader()
        elif missing_newline:
            self._fh.write("\r\n")
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


EvalJob = tuple[ExampleBrief, int, tuple[str, ...]]   # brief, repeat, the conditions to run


def run_eval_jobs(
    jobs: Sequence[EvalJob],
    run: Callable[[ExampleBrief, int, tuple[str, ...]], Sequence[EvalRow]],
    *,
    workers: int,
    csv_path: Path,
    report: Callable[[EvalRow], None],
    append: bool = False,
) -> list[EvalRow]:
    """Run every ``(brief, repeat, conditions)`` job, ``workers`` at a time; a job structures
    its brief once and returns one row per condition, each appended to ``csv_path`` and
    reported the moment the job completes (so a killed run keeps the rows it finished, in
    completion order); the rows come back in job order whatever order the jobs finished in.
    ``append`` keeps the rows already in ``csv_path`` (``--resume``). An interrupt cancels
    the jobs not yet started."""
    done: dict[int, Sequence[EvalRow]] = {}
    with CsvSink(csv_path, append=append) as sink, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, *job): i for i, job in enumerate(jobs)}
        try:
            for future in as_completed(futures):
                rows = future.result()
                done[futures[future]] = rows
                for row in rows:
                    sink.add(row)
                    report(row)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return [row for i in range(len(jobs)) for row in done[i]]


def scope_shortfall(row: EvalRow) -> str:
    """Why a verified row is not in scope, from its smallest shares: 'volume 85 %, area 78 %'
    (each only when under the in-scope share); 'scope differs from the brief' when neither is
    (since phase 3l, an operation a structure's entry states that no task performs:
    :func:`~moonwright.pipeline.validate.restored_gaps`)."""
    parts = [f"{name} {pct(share, floor=True)}"
             for name, share in (("volume", row.min_volume_share), ("area", row.min_area_share))
             if share is not None and share < IN_SCOPE_SHARE]
    return ", ".join(parts) or "scope differs from the brief"


def report_row(row: EvalRow) -> None:
    verdict = f"makespan {row.makespan_h}h" if row.success else f"FAIL {row.failure_kind}"
    if row.success and row.in_scope is False:
        verdict += f" (not in scope: {scope_shortfall(row)})"
    cost = f", {format_usd(row.cost_usd)}" if row.cost_usd else ""
    rejected = (f", {plural(row.rejected_answers, 'rejected answer')}"
                if row.rejected_answers else "")
    say(
        f"  {row.brief:<16} {row.condition:<8} #{row.repeat} {verdict}, "
        f"{row.repairs} repair(s), {row.latency_s:.1f}s{cost}{rejected}"
    )


# --------------------------------------------------------------------------- eval plan files
def file_safe(text: str) -> str:
    """``text`` as a file-name part: a condition's ``/`` becomes ``__`` (``ultra/loop_on`` ->
    ``ultra__loop_on``) and every other character outside letters, digits, ``_ . -`` an ``_``."""
    return FILE_NAME_RE.sub("_", text.replace("/", "__"))


def csv_relative(path: Path, csv_path: Path) -> str:
    """``path`` as the CSV's rows name it: relative to the CSV's directory with ``/``
    separators (``plans/landing_pad__loop_on__1.plan.json``), absolute when no relative path
    exists (another drive), so a row resumed from an earlier run whose ``--save-plans`` went
    elsewhere still names its own file."""
    try:
        return Path(os.path.relpath(path.resolve(), csv_path.resolve().parent)).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def free_name(directory: Path, name: str) -> str:
    """``name``, or ``<stem>.2.structure.json`` (``.3``, ...; the last two suffixes kept)
    when a file of that name exists: a resumed partial job's structuring never overwrites
    the record of the structuring the earlier conditions of its job planned from."""
    if not (directory / name).exists():
        return name
    suffix = "".join(Path(name).suffixes[-2:])
    stem = name.removesuffix(suffix)
    n = 2
    while (directory / f"{stem}.{n}{suffix}").exists():
        n += 1
    return f"{stem}.{n}{suffix}"


def plan_file_name(brief_id: str, condition: str, repeat: int) -> str:
    """``landing_pad__ultra__loop_on__2.plan.json`` (``landing_pad__loop_on__2.plan.json``
    without an arm): the ``--save-plans`` file of one run, relative to the directory."""
    return f"{file_safe(brief_id)}__{file_safe(condition)}__{repeat}.plan.json"


def structure_file_name(brief_id: str, repeat: int) -> str:
    """``landing_pad__2.structure.json``: the structuring every condition of a (brief, repeat)
    job planned from."""
    return f"{file_safe(brief_id)}__{repeat}.structure.json"


def structure_record(brief_id: str, repeat: int, req: Requirements | None, step: LoopStep,
                     latency_s: float) -> dict[str, Any]:
    """What ``--save-plans`` keeps of a job's structuring: the requirements its conditions
    planned from (None when the model failed, with the step's failure), the ``structure``
    step's payload (its ``warnings``), its usage and the wall time of the call, so a volume
    flag or a leaked dimension can be traced to the structuring after the run."""
    return {
        "brief": brief_id,
        "repeat": repeat,
        "requirements_sha": requirements_sha(req),
        "requirements": None if req is None else req.model_dump(mode="json"),
        "ok": step.ok,
        "message": step.message,
        "payload": step.payload,
        "failure": None if step.failure is None else step.failure.model_dump(mode="json"),
        "usage": None if step.usage is None else step.usage.model_dump(mode="json"),
        "latency_s": round(latency_s, 3),
    }


def sort_rows(rows: Sequence[EvalRow], briefs: Sequence[ExampleBrief],
              conditions: Sequence[str]) -> list[EvalRow]:
    """Report order: brief x condition x repeat in this run's order; rows resumed from a CSV
    whose brief or condition this run does not have (a brief since removed, an arm since
    dropped) follow, by name."""
    brief_rank = {b.id: i for i, b in enumerate(briefs)}
    condition_rank = {c: i for i, c in enumerate(conditions)}
    return sorted(rows, key=lambda r: (
        brief_rank.get(r.brief, len(brief_rank)), r.brief,
        condition_rank.get(r.condition, len(condition_rank)), r.condition, r.repeat))


# --------------------------------------------------------------------------- eval markdown
def _md_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _num(value: float) -> str:
    """``0.3`` / ``1`` / ``12``: one decimal at most, no trailing zero."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def failure_kinds(rows: Sequence[EvalRow]) -> str:
    """The conflict families (``energy``, ``scope``, ...) of failed runs, else their failure
    kinds, comma-separated and sorted: what a matrix cell says after ``fail``."""
    kinds = sorted({c.split(":", 1)[0] for r in rows for c in r.conflicts})
    if not kinds:
        kinds = sorted({r.failure_kind for r in rows if r.failure_kind})
    return ",".join(kinds) or "unknown"


def matrix_cell(rows: Sequence[EvalRow]) -> str:
    """One cell of the per-brief matrix: ``ok 76 h / 0 rep`` for a verified run (``ok 90 h /
    1 rep scope!`` when it is verified but not in scope),
    ``fail energy`` for one that ended without a plan (the conflict families, else the
    failure kind); with repeats the share ``k/n`` follows the word and makespan / repairs are
    means over the verified runs, e.g. ``ok 2/3 76 h / 0.5 rep; fail 1/3 scope``."""
    if not rows:
        return "-"
    n = len(rows)
    ok = [r for r in rows if r.success]
    failed = [r for r in rows if not r.success]
    parts = []
    if ok:
        makespans = [r.makespan_h for r in ok if r.makespan_h is not None]
        makespan = f"{statistics.fmean(makespans):.0f} h" if makespans else "- h"
        head = "ok" if n == 1 else f"ok {len(ok)}/{n}"
        cell = f"{head} {makespan} / {_num(statistics.fmean(r.repairs for r in ok))} rep"
        off = [r for r in ok if r.in_scope is False]
        if off:   # verified, but a structure's planned work is short of its dimensions
            cell += " scope!" if len(ok) == 1 else f" scope! {len(off)}/{len(ok)}"
        parts.append(cell)
    if failed:
        head = "fail" if n == 1 else f"fail {len(failed)}/{n}"
        parts.append(f"{head} {failure_kinds(failed)}")
    return "; ".join(parts)


def rejected_cell(s: ConditionSummary) -> str:
    """``1/3``: the runs with a rejected task-graph / repair answer; ``1/3 (4 answers)`` when
    the re-asks add up to more rejected answers than runs."""
    cell = f"{s.rejected_runs}/{s.runs}"
    if s.rejected_answers > s.rejected_runs:
        cell += f" ({s.rejected_answers} answers)"
    return cell


def condition_table(summary: Sequence[ConditionSummary]) -> str:
    """The per-condition markdown table of :func:`eval_markdown`; new columns go last."""
    header = ["condition", "n", "feasible (95% CI)", "feasible in scope (95% CI)", "first try",
              "repairs", "makespan", "latency p50 / p95", "tokens in / out", "cost / run",
              "cost total", "rejected answers", "p50 cost"]
    rows = []
    for s in summary:
        rows.append([
            f"`{s.condition}`", str(s.runs),
            f"{s.feasible_rate:.0%} ({s.feasible_ci_low:.0%}-{s.feasible_ci_high:.0%})",
            f"{s.in_scope_rate:.0%} ({s.in_scope_ci_low:.0%}-{s.in_scope_ci_high:.0%})",
            f"{s.first_try_rate:.0%}", f"{s.mean_repairs:.2f}",
            f"{s.mean_makespan_h:.0f} h" if s.mean_makespan_h is not None else "-",
            f"{s.latency_p50_s:.1f} s / {s.latency_p95_s:.1f} s",
            f"{s.mean_input_tokens:.0f} / {s.mean_output_tokens:.0f}",
            format_usd(s.mean_cost_usd), format_usd(s.total_cost_usd, decimals=2),
            rejected_cell(s), format_usd(s.cost_p50_usd),
        ])
    return _md_table(header, rows)


def kind_table(report: EvalReport) -> str | None:
    """The per-kind markdown table of :func:`eval_markdown` (kinds down, one row per kind and
    condition), or None when no row carries a kind."""
    kinds = list(dict.fromkeys(r.kind for r in report.rows if r.kind))
    if not kinds:
        return None
    conditions = list(dict.fromkeys([*report.conditions, *(r.condition for r in report.rows)]))
    header = ["kind", "condition", "n", "feasible", "feasible in scope", "first try", "repairs",
              "briefs"]
    rows = []
    for kind in kinds:
        for condition in conditions:
            group = [r for r in report.rows if r.kind == kind and r.condition == condition]
            if not group:
                continue
            n = len(group)
            rows.append([
                f"`{kind}`", f"`{condition}`", str(n),
                f"{sum(r.success for r in group)}/{n}",
                f"{sum(r.in_scope is True for r in group)}/{n}",
                f"{sum(r.first_try_feasible for r in group)}/{n}",
                f"{statistics.fmean(r.repairs for r in group):.2f}",
                ", ".join(f"`{b}`" for b in dict.fromkeys(r.brief for r in group)),
            ])
    return _md_table(header, rows)


def brief_matrix(report: EvalReport) -> str:
    """The per-brief markdown matrix of :func:`eval_markdown`: briefs down, conditions across."""
    conditions = list(dict.fromkeys([*report.conditions, *(r.condition for r in report.rows)]))
    briefs = list(dict.fromkeys(r.brief for r in report.rows))
    rows = []
    for brief in briefs:
        cells = [matrix_cell([r for r in report.rows if r.brief == brief and r.condition == c])
                 for c in conditions]
        rows.append([f"`{brief}`", *cells])
    return _md_table(["brief", *conditions], rows)


def how_to_read(report: EvalReport) -> str:
    """The paragraph that names the models, the thinking setting and what each column means."""
    roles = report.role_models
    parts: list[str] = []
    if report.arms:
        arms = ", ".join(f"`{arm}` = `{model}`" for arm, model in report.arms.items())
        parts.append("A condition is `<arm>/<loop>`: the arm names the model that proposes the "
                     f"task graph and answers the repair prompts ({arms}) and the loop is the "
                     "repair-loop setting.")
    else:
        model = f"`{roles['taskgraph']}`" if "taskgraph" in roles else "the configured model"
        parts.append("A condition is the repair-loop setting; the task graph and the repairs come "
                     f"from {model}.")
    told = ("the solver's minimal infeasibility core with its numeric hints"
            if report.feedback == "core" else "only the verdict kind (the ablation arm)")
    parts.append(f"`loop_on` allows up to {report.max_repairs} repair round(s), each fed {told}; "
                 "`loop_off` takes the first valid graph's verdict as final.")
    if report.max_answer_retries is not None:
        parts.append(f"Schema / transport re-asks are capped at {report.max_answer_retries} in "
                     "both conditions.")
    if roles:
        line = f"Briefs are structured by `{roles.get('structure', '?')}`"
        if "explain" in roles:
            line += f"; explanations (only with `--explain`) are written by `{roles['explain']}`"
        parts.append(line + ".")
    thinking = {
        "auto": "the task-graph and repair calls reason, structure and explain answer directly",
        "on": "the task-graph and repair calls reason (reasoning and answer share max_tokens)",
        "off": "no call reasons: every model answers directly",
    }.get(report.thinking, report.thinking)
    parts.append(f"Thinking `{report.thinking}`: {thinking}.")
    parts.append("Each brief is structured once per repeat and every condition plans from that "
                 "same requirements object (`requirements_sha` in the rows), so arms and loop "
                 "settings are a paired comparison on the same first-graph verdict rather than "
                 "each sampling the structuring model anew; the shared structure step and its "
                 "tokens appear in every row of the pair.")
    parts.append("`feasible` is the share of runs that ended with a CP-SAT-verified schedule, "
                 "with a Wilson 95 % interval; `feasible in scope` the share that is feasible "
                 "AND whose final graph plans every structure's volume (dug m3, hauled fill) "
                 "and its compaction and sintering area (footprint x lifts) at 95 % or more of "
                 "the smallest size the brief allows, and every operation a structure's own "
                 "entry in the brief states (phase 3l: one the structuring omitted is still "
                 "asked; a plan that never compacts a pad its entry says is compacted is "
                 "feasible but not in scope) (the validator already rejects a volume "
                 "under 70 %, so a plan that verified after trimming the work 15-25 % is "
                 "feasible but not in scope; `min_volume_share` and `min_area_share` in the rows "
                 "say how far off; an excess such as a berm hauled twice is counted in "
                 "`volume_advisories`, not against the scope); `first try` the share "
                 "verified without a repair round; `repairs` the mean repair rounds per run; "
                 "`makespan` the mean over feasible runs; latency is wall time per run (p50 / "
                 "p95, model calls and solver included); tokens are per run; cost is per run at "
                 "the Token Factory list prices (`-` when the model is not priced).")
    parts.append("In the per-brief matrix a cell reads `ok <makespan> h / <repairs> rep` for a "
                 "verified run (with `k/n` when repeated), `ok ... scope!` when it is verified "
                 "but not in scope, and `fail <conflict families>` for a run that ended without "
                 "a verified plan.")
    parts.append("`rejected answers` is the share of runs in which at least one task-graph or "
                 "repair answer was rejected before any verdict (it failed the schema or the "
                 "call failed: `schema` / `llm_error`, an answer cut off at max_tokens and "
                 "re-asked among them; when the re-asks add up to more such answers than runs "
                 "their number follows in parentheses) and `p50 cost` the median cost of a "
                 "priced run, next to the mean in `cost / run`.")
    if report.plans_dir:
        parts.append(f"Every run's PlanResult is kept under `{report.plans_dir}` as "
                     "`<brief>__<arm>__<loop>__<repeat>.plan.json` (`plan_path` in the rows, "
                     "relative to the CSV's directory, so a row resumed from a run that kept "
                     "its plans elsewhere names its own file: the JSON `POST /plan` returns, so "
                     "`moonwright simulate` and `scripts/replan_eval.py` read it) and each "
                     "(brief, repeat) job's structuring as `<brief>__<repeat>.structure.json` "
                     "(`<brief>__<repeat>.2.structure.json` for the second structuring of a "
                     "job `--resume` completed; its `requirements_sha` pairs it with the "
                     "rows).")
    if report.resumed_rows:
        parts.append(f"{plural(report.resumed_rows, 'row')} come from an earlier run's CSV "
                     "(`--resume`): the jobs they complete were not run again, a job with some "
                     "conditions missing ran only those (structured anew, so its "
                     "`requirements_sha` may differ), and every table counts old and new rows "
                     "alike.")
    if any(r.kind for r in report.rows):
        parts.append("The per-kind table groups briefs by their designed kind (`--kinds`, or "
                     "`kinds.json` next to the briefs; `docs/eval-briefs.md`): A comfortable, "
                     "B battery-split, C horizon-bound, D impossible, E scope reduction "
                     "allowed.")
    if report.llm == "fixture":
        parts.append("This run used `--llm fixture`: every arm replays the same recorded "
                     "Nemotron answers, so arms differ in name only and tokens and cost are 0.")
    return " ".join(parts)


def eval_markdown(report: EvalReport) -> str:
    """The results document of ``moonwright eval --markdown``: header (with the resumed-rows
    note of a ``--resume`` run), how to read, the per-condition table and the per-brief matrix."""
    briefs = len({r.brief for r in report.rows})
    retries = ("the planner's own" if report.max_answer_retries is None
               else str(report.max_answer_retries))
    title = (f"# Moonwright eval: {briefs} brief(s) x {len(report.conditions)} condition(s) x "
             f"{report.repeat} repeat(s)")
    setup = (f"Generated {report.generated_at}; llm `{report.llm}`; briefs from "
             f"`{report.briefs_dir}`; {len(report.rows)} run(s) on {report.workers} worker(s); "
             f"max_repairs {report.max_repairs}; feedback `{report.feedback}`; "
             f"max_answer_retries {retries}; thinking `{report.thinking}`.")
    lines = [title, "", setup, ""]
    if report.resumed_rows:
        lines += [f"resumed: {plural(report.resumed_rows, 'row')} from a previous run", ""]
    lines += [
        "## How to read",
        "",
        how_to_read(report),
        "",
        "## Per condition",
        "",
        condition_table(report.summary),
        "",
    ]
    kinds = kind_table(report)
    if kinds is not None:
        lines += ["## Per kind", "", kinds, ""]
    lines += ["## Per brief", "", brief_matrix(report), ""]
    return "\n".join(lines)


def write_markdown(path: Path, report: EvalReport) -> str:
    text = eval_markdown(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def token_budgets(client: object) -> str | None:
    """The token budgets of the live client inside ``client`` (through its wrappers'
    ``inner``), e.g. 'reasoning 32,768 tokens (x2 when cut, up to 65,536), direct 4,096';
    None for an offline client."""
    for _ in range(4):
        budgets = getattr(client, "budgets", None)
        if isinstance(budgets, str):
            return budgets
        client = getattr(client, "inner", None)
        if client is None:
            return None
    return None


def cmd_eval(args: argparse.Namespace) -> int:
    briefs = list_briefs(args.briefs)
    if not briefs:
        raise ValueError(f"no briefs (*.md, *.txt) found in {args.briefs}")
    loops = CONDITIONS[args.loop]
    options = planner_options(args.feedback, args.max_answer_retries)
    arms = parse_arms(getattr(args, "models", None))
    thinking_arg = getattr(args, "thinking", None)
    thinking = thinking_arg or thinking_setting()
    overrides = thinking_overrides(thinking_arg)
    # a condition is "<arm>/<loop>" (or the bare loop without arms); one client per arm,
    # shared by the workers (see make_arm_client)
    clients: dict[str, LLMClient] = {}
    loop_of: dict[str, str] = {}
    for arm, model in (arms or {None: None}).items():
        client = make_arm_client(args.llm, model, overrides)
        for loop in loops:
            name = condition_name(arm, loop)
            clients[name] = client
            loop_of[name] = loop
    conditions = list(loop_of)
    llm_name = next(iter(clients.values())).name
    kinds = load_kinds(getattr(args, "kinds", None), args.briefs)
    plans_dir: Path | None = getattr(args, "save_plans", None)
    if plans_dir is not None:
        plans_dir.mkdir(parents=True, exist_ok=True)
    # --resume: the rows an earlier run left in the CSV stay; a (brief, repeat) job whose every
    # condition is among them is skipped, one with some conditions missing runs only those
    # (structured anew), and the summaries cover old and new rows alike
    resume = bool(getattr(args, "resume", False)) and args.csv.is_file()
    old_rows = resume_csv(args.csv) if resume else []
    have = {(r.brief, r.condition, r.repeat) for r in old_rows}
    # one job per (brief, repeat): the brief is structured ONCE and every condition plans from
    # the same requirements, so the conditions are paired (same first graph's verdict for
    # loop off / on, same dimensions and operations for every model arm)
    jobs: list[EvalJob] = []
    skipped = 0
    for brief in briefs:
        for repeat in range(1, args.repeat + 1):
            missing = tuple(c for c in conditions if (brief.id, c, repeat) not in have)
            if missing:
                jobs.append((brief, repeat, missing))
            else:
                skipped += 1
    say(
        f"eval: {len(briefs)} brief(s) x {len(conditions)} condition(s) x {args.repeat} repeat(s), "
        f"llm={llm_name} workers={args.workers} feedback={args.feedback} thinking={thinking}"
        + (" arms=" + ",".join(f"{a}={m}" for a, m in arms.items()) if arms else "")
        + (f" kinds={len(kinds)}" if kinds else "")
        + (f" save_plans={plans_dir}" if plans_dir is not None else "")
    )
    budgets = dict.fromkeys(b for c in clients.values() if (b := token_budgets(c)))
    for line in budgets:   # the live client's effective budgets (an old .env can pin them)
        say(f"tokens: {line}")
    if resume:
        partial = sum(len(wanted) < len(conditions) for _brief, _repeat, wanted in jobs)
        say(
            f"resume: {plural(len(old_rows), 'row')} from a previous run in {args.csv}; "
            f"{skipped} of {skipped + len(jobs)} job(s) already complete, skipped"
            + (f"; {plural(partial, 'partial job')}: only the missing conditions run"
               if partial else "")
        )
    elif getattr(args, "resume", False):
        say(f"resume: no earlier rows at {args.csv}, starting afresh")

    def run(brief: ExampleBrief, repeat: int, wanted: tuple[str, ...]) -> list[EvalRow]:
        planners: dict[str, tuple[GraphCapture, Planner]] = {}
        for condition in wanted:
            capture = GraphCapture(clients[condition])
            planners[condition] = (capture, Planner(
                capture,
                max_repairs=args.max_repairs if loop_of[condition] == "loop_on" else 0,
                solver_time_limit_s=args.time_limit,
                explain=args.explain,
                **options,
            ))
        t0 = time.perf_counter()
        # the structure role is the same model in every arm: ask it once, through the first
        # condition's planner (so a stubbed Planner is honoured)
        req, step = planners[wanted[0]][1].structure(brief.text_markdown, brief_id=brief.id)
        structure_s = time.perf_counter() - t0
        key = brief_key(brief.text_markdown, brief.id)
        sha = requirements_sha(req)
        if plans_dir is not None:   # written now: a killed run keeps it
            record = structure_record(brief.id, repeat, req, step, structure_s)
            name = structure_file_name(brief.id, repeat)
            if resume and len(wanted) < len(conditions):   # the earlier conditions' record stays
                name = free_name(plans_dir, name)
            (plans_dir / name).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        rows: list[EvalRow] = []
        for condition in wanted:
            capture, planner = planners[condition]
            t1 = time.perf_counter()
            if req is None:
                result = unstructured_result(step, brief.text_markdown)
            else:
                result = planner.plan_from_requirements(req, key=key)
                result.steps.insert(0, step)
            plan_path = ""
            if plans_dir is not None:   # the PlanResult as POST /plan returns it
                plan_file = plans_dir / plan_file_name(brief.id, condition, repeat)
                write_json(plan_file, result)
                plan_path = csv_relative(plan_file, args.csv)
            rows.append(eval_row(
                brief.id, condition, repeat, result, structure_s + time.perf_counter() - t1,
                first_graph=capture.graphs[0] if capture.graphs else None, thinking=thinking,
                kind=kinds.get(brief.id, ""), sha=sha, plan_path=plan_path,
            ))
        return rows

    new_rows = run_eval_jobs(jobs, run, workers=args.workers, csv_path=args.csv,
                             report=report_row, append=resume)
    rows = sort_rows([*old_rows, *new_rows], briefs, conditions)   # brief x cond x rep
    summary = summarise(rows)
    print(format_summary(summary))
    report = EvalReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        llm=llm_name,
        briefs_dir=str(args.briefs),
        conditions=conditions,
        repeat=args.repeat,
        summary=summary,
        rows=rows,
        max_repairs=args.max_repairs,
        feedback=args.feedback,
        max_answer_retries=args.max_answer_retries,
        workers=args.workers,
        thinking=thinking,
        arms=arms or {},
        role_models={} if llm_name == "fixture" else dict(models_from_env()),
        resumed_rows=len(old_rows),
        plans_dir=None if plans_dir is None else str(plans_dir),
    )
    write_json(args.out, report)
    outputs = [str(args.out), str(args.csv)]
    markdown = getattr(args, "markdown", None)
    if markdown is not None:
        write_markdown(markdown, report)
        outputs.append(str(markdown))
    say(format_summary(summary))   # the table again where the progress lines went
    print("-> " + ", ".join(outputs))
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
    p.add_argument(
        "--models", default=None, metavar="ARM[,ARM...]",
        help="model arms for the task-graph and repair roles, each its own set of conditions "
             f"named <arm>/<loop>: {', '.join(ARM_MODELS)}, or name=model-id (default: the "
             "configured models; ignored by --llm fixture, the names are kept)",
    )
    p.add_argument(
        "--thinking", choices=THINKING_CHOICES, default=None,
        help="reasoning for the task-graph and repair calls (default: MOONWRIGHT_THINKING)",
    )
    p.add_argument("--out", type=Path, default=Path("eval.json"))
    p.add_argument("--csv", type=Path, default=Path("eval.csv"))
    p.add_argument(
        "--save-plans", type=Path, default=None, metavar="DIR",
        help="keep every run's PlanResult as DIR/<brief>__<arm>__<loop>__<repeat>.plan.json "
             "(plan_path in the rows) and each brief x repeat job's structuring as "
             "DIR/<brief>__<repeat>.structure.json",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="when --csv exists, keep its rows: skip the brief x repeat jobs it already holds "
             "every condition of, append the new rows and summarise old and new together "
             "(default: overwrite the CSV)",
    )
    p.add_argument(
        "--markdown", type=Path, default=None, metavar="PATH",
        help="also write the results document: per-condition table, per-kind table, per-brief "
             "matrix, how to read",
    )
    p.add_argument(
        "--kinds", type=Path, default=None, metavar="PATH",
        help="JSON object brief id -> designed kind (A..E) for the per-kind table (default: "
             "kinds.json next to the briefs when it exists)",
    )
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
