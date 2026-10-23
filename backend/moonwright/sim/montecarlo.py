"""F9: Monte Carlo robustness of a verified plan.

Contract::

    result = run_monte_carlo(plan, runs=50, failure_rate_per_robot_h=0.002, duration_jitter=0.15,
                             seed=None, max_replans=2, planner=None, dt_h=0.25)
    print(format_monte_carlo(result))                    # compact text table for the CLI
    sample, sim = simulate_sample(plan, seed, ...)       # one run, e.g. to replay a sample

``MonteCarloResult`` / ``MonteCarloSample`` / ``MonteCarloStats`` / ``HistogramBin`` mirror the
TypeScript types in ``frontend/src/lib/types.ts`` field for field; ``FailureSpec`` is the
``{robot, t_h}`` pair the ``/simulate`` endpoint accepts, so a sample's failures can be replayed.

What one run does (run ``i`` draws everything from ``random.Random(seed + i)``)
--------------------------------------------------------------------------------
1. Failures: every robot gets an exponential failure time with the given per-hour rate (a
   constant hazard; a robot fails at most once). Draws inside the mission window
   ``[0, horizon_h)`` become :class:`FailureInjection`; a failure after the deadline is outside
   the mission by definition and is dropped. The simulator stops when the work is done (or
   stuck) and skips an injection for a robot that has already run dry, so only the injections
   that actually fired a ``robot_failure`` event are the ones the sample lists under
   ``failures`` (and the ones ``failures_mean`` averages), which keeps "failures per run" an
   honest count of failures that hit the crew and makes a sample's list replayable via
   ``/simulate`` as-is.
2. Duration jitter: every task's duration is multiplied by a factor drawn uniformly from
   ``[1 - j, 1 + j]`` (symmetric, relative spread ``j``) and rounded stochastically to whole
   hours (up with probability equal to the fractional part, floor 1 h), so the expected
   duration stays the nominal one even for short tasks that plain rounding would never move.
   Energy scales with the realised duration. The *schedule* is left as planned: longer tasks
   drift later and ``task_overrun`` events fire naturally, as they would on site. A battery
   that runs dry under the extra load is a ``robot_failure`` event to the simulator and
   triggers a replan exactly like an injected failure (it counts in ``events``, not in the
   sample's ``failures``, which lists injections only).
3. ``run_with_replanning`` replays the jittered plan with the injections. Every replan is
   solver-first (``start_with_prior``: CP-SAT re-schedules the remaining work with the failed
   robot removed, no model call), and the default planner wraps :class:`SolverOnlyClient` with
   a repair budget of zero, so an infeasible remaining graph ends with the solver's verdict:
   nothing is read from ``fixtures/`` or the network and a run costs milliseconds.
4. ``within_horizon`` is ``completed and finished_at_h <= horizon_h``; ``outcome`` says what
   happened (below).

Outcomes (``MonteCarloSample.outcome``, counted in ``MonteCarloResult.outcomes``)
  ``completed``                      finished within the horizon
  ``late``                           finished, but after the horizon
  ``lost_only:<type>[,<type>]``      work remained for a robot type whose every robot had
                                     failed: nobody could do it (the failed robot was the last
                                     of its type), whatever else went wrong
  ``replan_infeasible:<kind>[,..]``  the last replan ended with a verdict and the previous plan
                                     could not finish the work; the kinds are the conflict ids'
                                     prefixes (``energy``, ``horizon``, ...), or the failure's
                                     kind when it named no conflict
  ``stuck``                          not finished for another reason (the replan budget was
                                     spent on earlier failures, a battery ran dry with nothing
                                     left to replan with, ...)
``single_points_of_failure`` lists the robot types the plan has exactly one robot of: the
types whose single failure makes a ``lost_only`` outcome possible.

Aggregates: ``finish_h`` statistics (mean, p50, p90, min, max; linear-interpolation percentiles
as numpy's default) over completed runs (``None`` when none completed), ``replans_mean`` and
``failures_mean`` over all runs, ``within_horizon_ci`` (the Wilson 95 % interval of
``within_horizon_rate``, :func:`wilson_interval`), and a histogram of ``finished_at_h`` over
completed runs whose bins are ``horizon_h / 12`` wide: the horizon is always a bin edge, bins
past it hold late finishes, so a chart can colour "late" bins differently. A bin is
``(start_h, end_h]`` (the first one includes 0), which keeps a finish exactly at the deadline on
the within-horizon side.

Determinism: the same ``seed`` gives an identical result (``elapsed_s`` aside); ``seed=None``
picks a base seed and reports it as ``samples[0].seed`` (sample ``i`` has seed ``base + i``).
The remaining-work models often have several optimal schedules, and with the 8-worker portfolio
racing, whichever worker reports first wins, so two identical replans could pick different
(equally good) assignments and the simulated finish would differ by a few steps. The default
planner (:class:`MonteCarloPlanner`) therefore solves deterministically: CP-SAT's portfolio
interleaved in a fixed order on every worker (``SolveOptions.deterministic``), which breaks
ties the same way every time, whatever the worker count. It used to run one worker, which
breaks ties as reliably but proves the travel-heavy remaining-work models (exact,
sequence-dependent travel since phase 3k) far slower: 200 runs of the canonical "fine"
four_mast_pads plan took 40 to 49 s on one worker, one replan cut at its 10 s limit, against
26 s interleaved, none above 2 s (2 s at base, under the transit allowance); the landing-pad
fixture's 200 runs take about 2 s either way. A solve that hits its time limit is the
exception: what it returns depends on the clock.
"""

from __future__ import annotations

import math
import random
import time
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from moonwright.llm.base import FixtureMissing
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.schedule import SolveOptions
from moonwright.schemas import (
    Failure,
    LoopStep,
    PlanResult,
    Requirements,
    Role,
    Schedule,
    ScheduleEntry,
    SimResult,
    TaskGraph,
    Usage,
)
from moonwright.sim.replan import run_with_replanning
from moonwright.sim.simulator import FailureInjection

T = TypeVar("T", bound=BaseModel)

MONTE_CARLO_KEY = "montecarlo"   # fixture key of the replans: no fixtures exist, by design
HISTOGRAM_BINS = 12              # bins up to the horizon; more are added for late finishes
WILSON_Z = 1.959964              # two-sided 95 %
_EPS = 1e-9


# --------------------------------------------------------------------------- models
class FailureSpec(BaseModel):
    """``{robot, t_h}``: the shape ``POST /simulate`` takes, so samples can be replayed."""

    robot: str
    t_h: float = Field(ge=0)


class MonteCarloStats(BaseModel):
    mean: float | None = None
    p50: float | None = None
    p90: float | None = None
    min: float | None = None
    max: float | None = None


class HistogramBin(BaseModel):
    start_h: float
    end_h: float
    count: int = Field(ge=0)


class MonteCarloSample(BaseModel):
    seed: int
    completed: bool
    within_horizon: bool
    outcome: str = Field(description="completed | late | lost_only:<types> | "
                                     "replan_infeasible:<kinds> | stuck (module docstring)")
    finished_at_h: float | None = None
    events: int = Field(ge=0)
    replans: int = Field(ge=0)
    failures: list[FailureSpec] = Field(default_factory=list)


class MonteCarloResult(BaseModel):
    runs: int = Field(ge=1)
    completed: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    within_horizon_rate: float = Field(ge=0, le=1)
    within_horizon_ci: tuple[float, float] = Field(
        description="Wilson 95 % interval [lo, hi] of within_horizon_rate")
    horizon_h: int
    finish_h: MonteCarloStats
    replans_mean: float = Field(ge=0)
    failures_mean: float = Field(ge=0)
    outcomes: dict[str, int] = Field(default_factory=dict, description="outcome -> runs")
    single_points_of_failure: list[str] = Field(
        default_factory=list, description="robot types the plan has exactly one robot of")
    histogram: list[HistogramBin]
    samples: list[MonteCarloSample]
    elapsed_s: float = Field(ge=0)


# --------------------------------------------------------------------------- planner
class SolverOnlyClient:
    """``LLMClient`` that never answers: every call raises :class:`FixtureMissing`.

    Under this client a replan keeps the remaining task graph as it is and CP-SAT re-schedules
    it (the solver-first path never asks it at all), a repair round ends with the solver's
    verdict, and nothing is read from ``fixtures/`` or the network.
    """

    name = "solver-only"

    def complete_json(
        self,
        *,
        role: Role,
        system: str,
        user: str,
        schema: type[T],
        key: str | None = None,
        attempt: int = 1,
        variant: str | None = None,
        max_attempts: int = 2,
    ) -> tuple[T, Usage]:
        raise FixtureMissing(
            f"solver-only client answers no {role} request "
            f"(key={key!r}, attempt={attempt}, variant={variant!r})"
        )


class MonteCarloPlanner(Planner):
    """A :class:`Planner` whose CP-SAT solves are deterministic (``SolveOptions
    .deterministic``: the portfolio interleaved in a fixed order on every worker), so that ties
    between equally good schedules break the same way on every call (module docstring,
    Determinism)."""

    # Overrides the Planner's verification hook only to make its solves deterministic (further
    # keyword arguments pass through untouched); a constructor option on Planner (SolveOptions)
    # would make this subclass unnecessary.
    def _check(
        self,
        req: Requirements,
        graph: TaskGraph,
        opts: SolveOptions,
        locked: list[ScheduleEntry] | None,
        required: list[str],
        steps: list[LoopStep],
        **kwargs: Any,
    ) -> tuple[Failure | None, Schedule | None]:
        return super()._check(req, graph, replace(opts, deterministic=True), locked, required,
                              steps, **kwargs)


def solver_only_planner(*, solver_time_limit_s: float = 10.0) -> Planner:
    """The default Monte Carlo planner: CP-SAT only (deterministic), no repairs, no explanation
    and no relaxation advice on an infeasible replan (nothing displays it in a Monte Carlo
    sample, so ``suggest_budget_s=0`` saves the verification solves)."""
    return MonteCarloPlanner(SolverOnlyClient(), max_repairs=0,
                             solver_time_limit_s=solver_time_limit_s, explain=False,
                             suggest_budget_s=0.0)


# --------------------------------------------------------------------------- one run
def draw_failures(plan: PlanResult, rng: random.Random, rate_per_h: float,
                  window_h: float) -> list[FailureInjection]:
    """One exponential failure time per robot (in ``requirements.robots`` order); the draws
    that fall inside ``[0, window_h)`` become injections, rounded to 0.01 h."""
    if rate_per_h <= 0:
        return []
    injections = []
    for robot in plan.requirements.robots:
        t_h = rng.expovariate(rate_per_h)
        if t_h < window_h:
            injections.append(FailureInjection(robot=robot.id, t_h=round(t_h, 2)))
    return injections


def jitter_graph(graph: TaskGraph, rng: random.Random, jitter: float) -> TaskGraph:
    """A copy of ``graph`` whose durations are scaled by ``uniform(1 - jitter, 1 + jitter)``
    and rounded stochastically to whole hours (floor 1); energies scale with the realised
    duration. ``jitter <= 0`` returns ``graph`` itself."""
    if jitter <= 0:
        return graph
    tasks = []
    for task in graph.tasks:
        exact = task.duration_h * rng.uniform(1.0 - jitter, 1.0 + jitter)
        whole = math.floor(exact)
        if rng.random() < exact - whole:
            whole += 1
        duration = max(1, whole)
        energy = round(task.energy_wh * duration / task.duration_h, 3)
        tasks.append(task.model_copy(update={"duration_h": duration, "energy_wh": energy}))
    return graph.model_copy(update={"tasks": tasks})


def classify_outcome(result: SimResult, req: Requirements, horizon_h: int) -> str:
    """The sample's ``outcome`` for a finished :class:`SimResult` (module docstring)."""
    if result.completed:
        finished = result.finished_at_h if result.finished_at_h is not None else 0.0
        return "completed" if finished <= horizon_h + _EPS else "late"
    last = result.frames[-1] if result.frames else None
    failed = {r.id for r in last.robots if r.status == "failed"} if last else set()
    done = set(last.done_tasks) if last else set()
    graph = result.task_graph
    remaining = [t for t in graph.tasks if t.id not in done] if graph else []
    healthy_types = {r.type for r in req.robots if r.id not in failed}
    lost = sorted({t.robot_type for t in remaining if t.robot_type not in healthy_types})
    if lost:
        return "lost_only:" + ",".join(lost)
    if result.replans and not result.replans[-1].after.success:
        failure = result.replans[-1].after.failure
        kinds = (sorted({c.split(":", 1)[0] for c in failure.conflicts})
                 if failure and failure.conflicts else [failure.kind] if failure else [])
        return "replan_infeasible:" + ",".join(kinds or ["unknown"])
    return "stuck"


def single_points_of_failure(req: Requirements) -> list[str]:
    """Robot types with exactly one robot, sorted: a single failure leaves nobody for them."""
    counts: dict[str, int] = {}
    for robot in req.robots:
        counts[robot.type] = counts.get(robot.type, 0) + 1
    return sorted(typ for typ, n in counts.items() if n == 1)


def simulate_sample(
    plan: PlanResult,
    seed: int,
    *,
    failure_rate_per_robot_h: float = 0.002,
    duration_jitter: float = 0.15,
    max_replans: int = 2,
    planner: Planner | None = None,
    dt_h: float = 0.25,
) -> tuple[MonteCarloSample, SimResult]:
    """One Monte Carlo run drawn from ``random.Random(seed)`` (module docstring: steps 1-4).

    Returns the sample and the full :class:`SimResult` behind it, so a UI can replay one run.
    """
    _check_plan(plan)
    rng = random.Random(seed)
    horizon = plan.requirements.horizon_h
    injections = draw_failures(plan, rng, failure_rate_per_robot_h, horizon)
    graph = jitter_graph(plan.task_graph, rng, duration_jitter)  # type: ignore[arg-type]
    world = plan.model_copy(update={"task_graph": graph})
    result = run_with_replanning(planner or solver_only_planner(), world, injections,
                                 key=MONTE_CARLO_KEY, max_replans=max_replans, dt_h=dt_h)
    # an injected failure announces itself as "<robot> failed at"; a battery that ran dry
    # before the injection time, or work that finished first, leaves the injection unfired
    struck = {e.robot for e in result.events if e.kind == "robot_failure" and e.robot
              and (e.detail or "").startswith(f"{e.robot} failed at")}
    fired = [FailureSpec(robot=i.robot, t_h=i.t_h) for i in injections if i.robot in struck]
    finished = result.finished_at_h if result.completed else None
    sample = MonteCarloSample(
        seed=seed, completed=result.completed,
        within_horizon=finished is not None and finished <= horizon + _EPS,
        outcome=classify_outcome(result, plan.requirements, horizon),
        finished_at_h=finished, events=len(result.events), replans=len(result.replans),
        failures=fired,
    )
    return sample, result


# --------------------------------------------------------------------------- statistics
def percentile(values: Sequence[float], p: float) -> float:
    """``p`` in [0, 1] percentile with linear interpolation between order statistics (numpy's
    default): ``percentile([1..10], 0.5) == 5.5`` and ``percentile([1..10], 0.9) == 9.1``."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be within [0, 1]")
    xs = sorted(values)
    rank = p * (len(xs) - 1)
    lo = math.floor(rank)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


def wilson_interval(successes: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Wilson score interval of a proportion (``[lo, hi]``, 4 decimals): sound for small ``n``
    and for rates at 0 or 1, unlike the normal approximation."""
    if n <= 0:
        raise ValueError("n must be >= 1")
    if not 0 <= successes <= n:
        raise ValueError("successes must be within [0, n]")
    p = successes / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4 * n * n)) / denominator
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def finish_stats(finishes: Sequence[float]) -> MonteCarloStats:
    """Mean / p50 / p90 / min / max of the finish times (all ``None`` without data)."""
    if not finishes:
        return MonteCarloStats()
    return MonteCarloStats(
        mean=round(sum(finishes) / len(finishes), 3),
        p50=round(percentile(finishes, 0.5), 3),
        p90=round(percentile(finishes, 0.9), 3),
        min=min(finishes),
        max=max(finishes),
    )


def histogram(finishes: Sequence[float], horizon_h: int,
              bins: int = HISTOGRAM_BINS) -> list[HistogramBin]:
    """Counts of ``finishes`` in ``bins`` equal bins up to ``horizon_h`` plus as many more of
    the same width as the latest finish needs. Bins are ``(start_h, end_h]`` (the first
    includes 0), so a finish exactly on the deadline counts as within the horizon."""
    width = horizon_h / bins
    top = max([float(horizon_h), *finishes])
    n = max(bins, math.ceil(top / width - _EPS))
    edges = [round(k * width, 3) for k in range(n + 1)]
    counts = [0] * n
    for finish in finishes:
        k = min(n - 1, max(0, bisect_left(edges, finish) - 1))
        counts[k] += 1
    return [HistogramBin(start_h=edges[k], end_h=edges[k + 1], count=counts[k])
            for k in range(n)]


def count_outcomes(samples: Sequence[MonteCarloSample]) -> dict[str, int]:
    """Runs per outcome, most frequent first (ties by name)."""
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.outcome] = counts.get(sample.outcome, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# --------------------------------------------------------------------------- the experiment
def run_monte_carlo(
    plan: PlanResult,
    *,
    runs: int = 50,
    failure_rate_per_robot_h: float = 0.002,
    duration_jitter: float = 0.15,
    seed: int | None = None,
    max_replans: int = 2,
    planner: Planner | None = None,
    dt_h: float = 0.25,
) -> MonteCarloResult:
    """Replay ``plan`` ``runs`` times with random robot failures and duration jitter and report
    how often it finishes, and finishes within the horizon (module docstring has the model).

    ``seed=None`` picks a base seed; run ``i`` always uses ``base + i`` and reports it, so any
    sample can be reproduced with :func:`simulate_sample`. ``planner`` defaults to
    :func:`solver_only_planner`; one with an LLM client and a repair budget works too, but
    every replan whose remaining work is infeasible then costs a model call.
    """
    _check_plan(plan)
    if runs < 1:
        raise ValueError("runs must be >= 1")
    if failure_rate_per_robot_h < 0:
        raise ValueError("failure_rate_per_robot_h must be >= 0")
    if not 0.0 <= duration_jitter <= 1.0:
        raise ValueError("duration_jitter must be within [0, 1]")
    if max_replans < 0:
        raise ValueError("max_replans must be >= 0")
    if dt_h <= 0:
        raise ValueError("dt_h must be positive")

    t0 = time.perf_counter()
    base = random.SystemRandom().randrange(1 << 31) if seed is None else seed
    planner = planner or solver_only_planner()
    samples = [
        simulate_sample(plan, base + i, failure_rate_per_robot_h=failure_rate_per_robot_h,
                        duration_jitter=duration_jitter, max_replans=max_replans,
                        planner=planner, dt_h=dt_h)[0]
        for i in range(runs)
    ]
    finishes = [s.finished_at_h for s in samples if s.finished_at_h is not None]
    horizon = plan.requirements.horizon_h
    within = sum(s.within_horizon for s in samples)
    return MonteCarloResult(
        runs=runs,
        completed=len(finishes),
        completion_rate=len(finishes) / runs,
        within_horizon_rate=within / runs,
        within_horizon_ci=wilson_interval(within, runs),
        horizon_h=horizon,
        finish_h=finish_stats(finishes),
        replans_mean=round(sum(s.replans for s in samples) / runs, 3),
        failures_mean=round(sum(len(s.failures) for s in samples) / runs, 3),
        outcomes=count_outcomes(samples),
        single_points_of_failure=single_points_of_failure(plan.requirements),
        histogram=histogram(finishes, horizon),
        samples=samples,
        elapsed_s=round(time.perf_counter() - t0, 3),
    )


def format_monte_carlo(result: MonteCarloResult, *, bar_width: int = 40) -> str:
    """Compact text table for the CLI: rates as percentages (with the 95 % interval of the
    within-horizon rate), finish statistics, the outcome table, the single points of failure
    and an ASCII histogram."""
    within = sum(s.within_horizon for s in result.samples)
    lo, hi = result.within_horizon_ci
    stats = result.finish_h
    lines = [
        (f"Monte Carlo robustness: {result.runs} runs, horizon {result.horizon_h} h, "
         f"{result.elapsed_s:.1f} s"),
        (f"  completed          {result.completed:>4} / {result.runs}"
         f"  ({result.completion_rate:.1%})"),
        (f"  within horizon     {within:>4} / {result.runs}  ({result.within_horizon_rate:.1%})"
         f"  95% CI {lo:.1%}-{hi:.1%}"),
    ]
    if stats.mean is None:
        lines.append("  finish (completed) no run completed")
    else:
        lines.append(
            f"  finish (completed) mean {stats.mean:g} h  p50 {stats.p50:g} h  "
            f"p90 {stats.p90:g} h  min {stats.min:g} h  max {stats.max:g} h"
        )
    lines.append(f"  per run            replans {result.replans_mean:.2f}  "
                 f"failures {result.failures_mean:.2f}")
    lines.append("  outcomes:")
    for outcome, count in result.outcomes.items():
        lines.append(f"    {outcome:<36} {count:>4}  ({count / result.runs:.1%})")
    spof = result.single_points_of_failure
    lines.append("  single points of failure: "
                 + (", ".join(spof) + " (one robot each)" if spof
                    else "none (every robot type has a spare)"))
    lines.append("  finished_at_h of completed runs (bins past the horizon are marked late):")
    peak = max((b.count for b in result.histogram), default=0)
    for b in result.histogram:
        bar = "#" * (round(bar_width * b.count / peak) if peak else 0)
        late = "  late" if b.start_h >= result.horizon_h - _EPS else ""
        lines.append(f"  {b.start_h:>7.1f} - {b.end_h:>7.1f} h  {bar:<{bar_width}} {b.count:>4}"
                     f"{late}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- helpers
def _check_plan(plan: PlanResult) -> None:
    if not plan.success or plan.schedule is None or plan.task_graph is None:
        raise ValueError("Monte Carlo needs a successful PlanResult with a verified schedule")
