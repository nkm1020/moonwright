"""Monte Carlo robustness (F9) tests.

The plan under test is the offline landing_pad example (``Planner(FixtureClient())`` over
``examples/briefs/landing_pad.md`` with the canned answers in ``fixtures/landing_pad/``), the
same PlanResult the frontend ships as ``public/samples/landing_pad.plan.json``: 7 robots, 14
tasks, 77 h makespan, 336 h horizon. Its plain replay finishes at 79.25 h (travel is not in the
solver's model) without any event. CP1, PR1 and SV1 are the only robot of their type, so
their failure leaves work nobody can do; the two excavators share a tight energy budget.
"""

from __future__ import annotations

import random
import time
from itertools import pairwise
from pathlib import Path

import pytest

from moonwright.llm.base import FixtureClient, FixtureMissing, LLMError
from moonwright.pipeline.loop import Planner
from moonwright.schemas import PlanResult, SimResult, TaskGraph
from moonwright.sim.montecarlo import (
    HISTOGRAM_BINS,
    MONTE_CARLO_KEY,
    FailureSpec,
    HistogramBin,
    MonteCarloPlanner,
    MonteCarloResult,
    MonteCarloSample,
    MonteCarloStats,
    SolverOnlyClient,
    draw_failures,
    finish_stats,
    format_monte_carlo,
    histogram,
    jitter_graph,
    percentile,
    run_monte_carlo,
    simulate_sample,
    solver_only_planner,
)
from moonwright.sim.replan import run_with_replanning
from moonwright.sim.simulator import FailureInjection, Simulator

BRIEF = Path(__file__).resolve().parents[1] / "examples" / "briefs" / "landing_pad.md"
HORIZON_H = 336
RATE = 0.002


@pytest.fixture(scope="module")
def plan() -> PlanResult:
    result = Planner(FixtureClient(), explain=False).plan(BRIEF.read_text(), brief_id="landing_pad")
    assert result.success and result.schedule is not None and result.task_graph is not None
    assert result.requirements.horizon_h == HORIZON_H
    return result


@pytest.fixture(scope="module")
def plain(plan: PlanResult) -> SimResult:
    """The deterministic replay: no failure, nominal durations, no event."""
    sim = Simulator(plan.requirements, plan.task_graph, plan.schedule)
    result = sim.run(stop_on_event=False)
    assert result.completed and result.finished_at_h is not None and result.events == []
    return result


class CountingPlanner(MonteCarloPlanner):
    def __init__(self):
        super().__init__(SolverOnlyClient(), max_repairs=0, explain=False)
        self.calls = 0

    def plan_from_requirements(self, req, **kw):
        self.calls += 1
        return super().plan_from_requirements(req, **kw)


# --------------------------------------------------------------------------- contract
def test_models_mirror_the_typescript_contract():
    assert list(MonteCarloResult.model_fields) == [
        "runs", "completed", "completion_rate", "within_horizon_rate", "horizon_h", "finish_h",
        "replans_mean", "failures_mean", "histogram", "samples", "elapsed_s",
    ]
    assert list(MonteCarloSample.model_fields) == [
        "seed", "completed", "within_horizon", "finished_at_h", "events", "replans", "failures",
    ]
    assert list(MonteCarloStats.model_fields) == ["mean", "p50", "p90", "min", "max"]
    assert list(HistogramBin.model_fields) == ["start_h", "end_h", "count"]
    assert list(FailureSpec.model_fields) == ["robot", "t_h"]
    assert MonteCarloStats() == MonteCarloStats(mean=None, p50=None, p90=None, min=None, max=None)


def test_solver_only_client_never_answers():
    client = SolverOnlyClient()
    assert client.name
    for role in ("structure", "taskgraph", "repair", "explain"):
        with pytest.raises(FixtureMissing):
            client.complete_json(role=role, system="s", user="u", schema=TaskGraph,
                                 key=MONTE_CARLO_KEY, attempt=2, variant="replan")
    assert issubclass(FixtureMissing, LLMError)
    planner = solver_only_planner()
    assert isinstance(planner, MonteCarloPlanner)
    assert isinstance(planner.client, SolverOnlyClient)
    assert planner.max_repairs == 0 and planner.explain is False


def test_solver_only_planner_replans_by_rescheduling_the_remaining_graph(plan):
    res = run_with_replanning(solver_only_planner(), plan, [FailureInjection("HL2", 20.0)],
                              key=MONTE_CARLO_KEY, max_replans=2)
    assert res.completed and len(res.replans) == 1
    record = res.replans[0]
    after = record.after
    assert after.success and after.schedule is not None and after.task_graph is not None
    assert [s.stage for s in after.steps] == ["taskgraph", "validate", "schedule"]
    assert after.steps[0].message is not None
    assert after.steps[0].message.startswith("offline: reused prior graph")
    assert after.task_graph.ids() == record.remaining_tasks
    assert all(s.usage is None for s in after.steps)  # no model was called
    assert "HL2" not in {e.robot for e in after.schedule.entries}


# --------------------------------------------------------------------------- the model
def test_zero_rate_and_zero_jitter_reproduce_the_plain_replay(plan, plain):
    res = run_monte_carlo(plan, runs=3, failure_rate_per_robot_h=0.0, duration_jitter=0.0, seed=5)

    assert [s.seed for s in res.samples] == [5, 6, 7]
    for s in res.samples:
        assert s.completed and s.within_horizon
        assert s.finished_at_h == plain.finished_at_h
        assert s.events == len(plain.events) and s.replans == 0 and s.failures == []
    assert res.runs == 3 and res.completed == 3
    assert res.completion_rate == 1.0 and res.within_horizon_rate == 1.0
    assert res.horizon_h == HORIZON_H
    f = plain.finished_at_h
    assert res.finish_h == MonteCarloStats(mean=f, p50=f, p90=f, min=f, max=f)
    assert res.replans_mean == 0.0 and res.failures_mean == 0.0
    assert sum(b.count for b in res.histogram) == 3
    assert res.elapsed_s >= 0

    # the same as a plain run_with_replanning of the plan
    direct = run_with_replanning(solver_only_planner(), plan, [], key=MONTE_CARLO_KEY)
    assert (direct.completed, direct.finished_at_h) == (True, plain.finished_at_h)


def test_same_seed_gives_an_identical_result(plan):
    a = run_monte_carlo(plan, runs=12, seed=42)
    b = run_monte_carlo(plan, runs=12, seed=42)
    assert a.model_dump(exclude={"elapsed_s"}) == b.model_dump(exclude={"elapsed_s"})
    # the scenario exercised failures and replans, not just the plain replay
    assert any(s.failures for s in a.samples) and any(s.replans for s in a.samples)
    assert any(not s.completed for s in a.samples) and any(s.completed for s in a.samples)

    # sample i is reproducible on its own from its seed ...
    k = next(i for i, s in enumerate(a.samples) if s.failures)
    assert simulate_sample(plan, a.samples[k].seed)[0] == a.samples[k]
    # ... and a run with base seed + 1 is the same experiment shifted by one sample
    c = run_monte_carlo(plan, runs=12, seed=43)
    assert c.samples[:11] == a.samples[1:]
    assert c.samples[11] != a.samples[11]


def test_seed_none_picks_a_base_seed_and_reports_it(plan):
    res = run_monte_carlo(plan, runs=2, seed=None)
    base = res.samples[0].seed
    assert isinstance(base, int) and res.samples[1].seed == base + 1
    again = run_monte_carlo(plan, runs=2, seed=base)
    assert again.samples == res.samples


def test_high_failure_rate_lowers_the_within_horizon_rate(plan):
    calm = run_monte_carlo(plan, runs=20, failure_rate_per_robot_h=0.0, duration_jitter=0.0,
                           seed=1)
    harsh = run_monte_carlo(plan, runs=20, failure_rate_per_robot_h=0.05, duration_jitter=0.0,
                            seed=1)
    assert calm.within_horizon_rate == 1.0
    assert harsh.within_horizon_rate < calm.within_horizon_rate
    assert harsh.completion_rate >= harsh.within_horizon_rate
    assert harsh.failures_mean > 0 and harsh.replans_mean > 0
    assert harsh.completed == sum(b.count for b in harsh.histogram)
    for s in harsh.samples:
        assert len(s.failures) <= len(plan.requirements.robots)
        assert len({f.robot for f in s.failures}) == len(s.failures)  # a robot fails once
        assert s.replans <= 2
        assert s.events >= len(s.failures)  # every injected failure is a robot_failure event


def test_sample_lists_only_the_failures_that_fired_before_the_run_ended(plan):
    res = run_monte_carlo(plan, runs=30, seed=7)
    dropped = 0
    for s in res.samples:
        drawn = draw_failures(plan, random.Random(s.seed), RATE, HORIZON_H)
        drawn_specs = [FailureSpec(robot=i.robot, t_h=i.t_h) for i in drawn]
        assert all(f in drawn_specs for f in s.failures)
        assert all(0 <= f.t_h < HORIZON_H for f in s.failures)
        if s.completed:
            assert s.finished_at_h is not None
            assert all(f.t_h <= s.finished_at_h for f in s.failures)
        dropped += len(drawn) - len(s.failures)
    assert dropped > 0  # draws after the mission ended (but before the deadline) are not listed
    assert res.failures_mean == pytest.approx(sum(len(s.failures) for s in res.samples) / 30,
                                              abs=1e-3)
    assert res.replans_mean == pytest.approx(sum(s.replans for s in res.samples) / 30, abs=1e-3)


def test_draw_failures_one_exponential_draw_per_robot_inside_the_window(plan):
    robots = [r.id for r in plan.requirements.robots]
    everyone = draw_failures(plan, random.Random(0), 5.0, 10.0)  # 5/h: all fail within hours
    assert [i.robot for i in everyone] == robots
    assert all(0 <= i.t_h < 10 and i.t_h == round(i.t_h, 2) for i in everyone)
    assert draw_failures(plan, random.Random(0), 5.0, 10.0) == everyone
    assert draw_failures(plan, random.Random(0), 0.0, 10.0) == []
    assert draw_failures(plan, random.Random(0), 5.0, 0.0) == []
    rare = draw_failures(plan, random.Random(0), 1e-6, HORIZON_H)
    assert len(rare) < len(robots)


def test_jitter_scales_durations_and_energies_and_keeps_the_schedule(plan):
    graph = plan.task_graph
    assert jitter_graph(graph, random.Random(0), 0.0) is graph

    jittered = jitter_graph(graph, random.Random(0), 0.5)
    assert jittered is not graph and jittered.ids() == graph.ids()
    changed = 0
    for before, after in zip(graph.tasks, jittered.tasks, strict=True):
        assert (after.after, after.zone, after.robot_type) == (before.after, before.zone,
                                                              before.robot_type)
        assert isinstance(after.duration_h, int) and after.duration_h >= 1
        assert 0.5 * before.duration_h - 1 <= after.duration_h <= 1.5 * before.duration_h + 1
        assert after.energy_wh == pytest.approx(
            before.energy_wh * after.duration_h / before.duration_h, abs=1e-3)
        changed += after.duration_h != before.duration_h
    assert changed > 0
    assert graph.tasks[1].duration_h == 12  # the plan's own graph is untouched

    # symmetric and unbiased: the realised factor averages to 1 (stochastic rounding)
    t = graph.task("T3")  # 18 h
    factors = [jitter_graph(graph, random.Random(k), 0.3).task("T3").duration_h / t.duration_h
               for k in range(400)]
    assert min(factors) < 0.85 and max(factors) > 1.15
    assert sum(factors) / len(factors) == pytest.approx(1.0, abs=0.03)

    # a jittered world starts on the planned schedule: the sim drifts instead of being
    # re-planned up front. Longer tasks also drain more energy, so a battery can run dry,
    # which the simulator reports as a robot_failure that triggers a replan like an injected
    # one (such failures are events, not injections, so `failures` stays empty).
    sample, sim = simulate_sample(plan, 11, failure_rate_per_robot_h=0.0, duration_jitter=0.5)
    assert sample.failures == []
    first = sim.replans[0].before if sim.replans else sim.schedule
    assert first == plan.schedule
    assert sim.events and all(e.kind in {"task_overrun", "battery_low", "robot_failure"}
                              for e in sim.events)
    assert sample.events == len(sim.events) and sample.replans == len(sim.replans)
    assert sample.completed == sim.completed
    assert sample.finished_at_h == (sim.finished_at_h if sim.completed else None)


def test_within_horizon_separates_late_completions(plan, plain):
    deadline = int(plain.finished_at_h) - 1  # the plain replay misses this deadline
    tight = plan.model_copy(update={
        "requirements": plan.requirements.model_copy(update={"horizon_h": deadline})})
    res = run_monte_carlo(tight, runs=2, failure_rate_per_robot_h=0.0, duration_jitter=0.0,
                          seed=0)
    assert res.horizon_h == deadline
    assert res.completion_rate == 1.0 and res.within_horizon_rate == 0.0
    assert all(s.completed and not s.within_horizon for s in res.samples)
    assert res.finish_h.max == plain.finished_at_h
    late = [b for b in res.histogram if b.start_h >= deadline]
    assert len(res.histogram) == HISTOGRAM_BINS + 1 and len(late) == 1
    assert late[0].count == 2 and sum(b.count for b in res.histogram) == 2


def test_custom_planner_is_used_for_every_replan(plan):
    spy = CountingPlanner()
    res = run_monte_carlo(plan, runs=6, failure_rate_per_robot_h=0.05, duration_jitter=0.0,
                          seed=2, planner=spy)
    assert spy.calls == sum(s.replans for s in res.samples) > 0


# --------------------------------------------------------------------------- statistics
def test_percentile_interpolates_linearly_like_numpy():
    xs = list(range(1, 11))
    assert percentile(xs, 0.5) == 5.5
    assert percentile(xs, 0.9) == pytest.approx(9.1)
    assert percentile(xs, 0.0) == 1 and percentile(xs, 1.0) == 10
    assert percentile([4.0], 0.9) == 4.0
    assert percentile([3, 1, 2], 0.5) == 2  # unsorted input is fine
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile(xs, 1.5)


def test_finish_stats_over_a_known_list_and_without_data():
    stats = finish_stats([80.0, 77.0, 90.0, 83.0])
    assert stats.mean == 82.5 and stats.p50 == 81.5 and stats.p90 == pytest.approx(87.9)
    assert stats.min == 77.0 and stats.max == 90.0
    assert finish_stats([]) == MonteCarloStats()
    assert finish_stats([79.25]) == MonteCarloStats(mean=79.25, p50=79.25, p90=79.25, min=79.25,
                                                    max=79.25)


def test_histogram_bins_tile_the_horizon_and_sum_to_the_completed_runs():
    bins = histogram([10.0, 20.0, 27.9, 28.0, 336.0, 350.0], 336)
    assert len(bins) == HISTOGRAM_BINS + 1
    assert bins[0].start_h == 0.0 and bins[HISTOGRAM_BINS - 1].end_h == 336.0
    assert bins[-1].end_h == 364.0
    assert all(a.end_h == b.start_h for a, b in pairwise(bins))
    assert sum(b.count for b in bins) == 6
    assert bins[0].count == 4          # (0, 28] -- an edge value goes to the lower bin
    assert bins[1].count == 0
    assert bins[HISTOGRAM_BINS - 1].count == 1  # a finish on the deadline is within the horizon
    assert bins[HISTOGRAM_BINS].count == 1      # 350 h is late

    empty = histogram([], 65)
    assert len(empty) == HISTOGRAM_BINS and all(b.count == 0 for b in empty)
    assert empty[0].start_h == 0.0 and empty[-1].end_h == 65.0
    assert empty[3] == HistogramBin(start_h=16.25, end_h=21.667, count=0)

    early = histogram([1.0, 2.0], 336)
    assert len(early) == HISTOGRAM_BINS and early[0].count == 2


# --------------------------------------------------------------------------- performance
def test_fifty_runs_of_the_landing_pad_plan_finish_quickly(plan):
    t0 = time.perf_counter()
    res = run_monte_carlo(plan, runs=50, seed=0)
    wall = time.perf_counter() - t0
    assert wall < 30, f"50 runs took {wall:.1f}s"
    assert res.elapsed_s <= wall + 0.01

    assert res.runs == 50 and len(res.samples) == 50
    assert res.completed == sum(s.completed for s in res.samples)
    assert res.completed == sum(b.count for b in res.histogram)
    assert res.completion_rate == res.completed / 50
    assert res.within_horizon_rate == sum(s.within_horizon for s in res.samples) / 50
    assert 0 < res.within_horizon_rate <= res.completion_rate < 1
    assert res.finish_h.min <= res.finish_h.p50 <= res.finish_h.p90 <= res.finish_h.max
    assert res.finish_h.min <= res.finish_h.mean <= res.finish_h.max
    assert MonteCarloResult.model_validate_json(res.model_dump_json()) == res


# --------------------------------------------------------------------------- CLI table
def test_format_monte_carlo_is_a_compact_table(plan):
    res = run_monte_carlo(plan, runs=8, seed=3)
    text = format_monte_carlo(res)
    lines = text.splitlines()
    assert lines[0].startswith(f"Monte Carlo robustness: 8 runs, horizon {HORIZON_H} h")
    assert f"({res.completion_rate:.1%})" in lines[1]
    assert f"({res.within_horizon_rate:.1%})" in lines[2]
    assert f"p50 {res.finish_h.p50:g} h" in lines[3] and f"p90 {res.finish_h.p90:g} h" in lines[3]
    assert f"replans {res.replans_mean:.2f}" in lines[4]
    assert len(lines) == 6 + len(res.histogram)
    assert any("#" in line for line in lines[6:])
    assert all(len(line) <= 100 for line in lines)
    assert not any(line.endswith("late") for line in lines[6:])  # nothing finished late

    hopeless = run_monte_carlo(plan, runs=2, failure_rate_per_robot_h=5.0, duration_jitter=0.0,
                               seed=1)
    text = format_monte_carlo(hopeless)
    assert "no run completed" in text and "#" not in text
    assert "(0.0%)" in text


# --------------------------------------------------------------------------- inputs
def test_rejects_unverified_plans_and_nonsense_parameters(plan):
    unverified = plan.model_copy(update={"success": False, "schedule": None})
    with pytest.raises(ValueError):
        run_monte_carlo(unverified, runs=1, seed=0)
    with pytest.raises(ValueError):
        simulate_sample(unverified, 0)
    for bad in ({"runs": 0}, {"failure_rate_per_robot_h": -1.0}, {"duration_jitter": 1.5},
                {"duration_jitter": -0.1}, {"max_replans": -1}, {"dt_h": 0.0}):
        with pytest.raises(ValueError):
            run_monte_carlo(plan, seed=0, **bad)
