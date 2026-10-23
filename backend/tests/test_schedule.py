"""Tests for moonwright.pipeline.schedule (CP-SAT scheduling + infeasibility cores)."""

from __future__ import annotations

import math
import random
import time

import pytest
from ortools.sat.python import cp_model

import moonwright.pipeline.schedule as schedule_module
from moonwright.geometry import distance, travel_hours, travel_wh
from moonwright.pipeline.schedule import (
    SolveOptions,
    _build,
    _instance,
    _minimize_core,
    _trial,
    check_schedule,
    solve_schedule,
)
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import (
    Failure,
    Requirements,
    Robot,
    Schedule,
    ScheduleEntry,
    Site,
    Task,
    TaskGraph,
    Zone,
    to_mwh,
)
from moonwright.sim.simulator import Simulator

FAST = SolveOptions(time_limit_s=5.0, num_workers=4, core_time_limit_s=3.0)

# Every zone on the depot cell: no leg to drive, so the scheduling logic is tested on its own
# (travel has its own section at the end of this module).
FLAT = Site(depot=(20, 20), zones=[Zone(id=f"Z{i}", center=(20, 20)) for i in range(1, 6)])


# ----------------------------------------------------------------------------- helpers
def robot(robot_id: str, robot_type: str, battery_wh: float,
          speed_cells_per_h: float = 12.0) -> Robot:
    return Robot(id=robot_id, type=robot_type, battery_wh=battery_wh,
                 speed_cells_per_h=speed_cells_per_h)


def reqs(robots: list[Robot], horizon_h: int, site: Site = FLAT) -> Requirements:
    return Requirements(site=site, robots=robots, horizon_h=horizon_h)


def task(task_id: str, robot_type: str, duration_h: int, energy_wh: float, zone: str,
         after: tuple[str, ...] = ()) -> Task:
    return Task(id=task_id, name=task_id, robot_type=robot_type, duration_h=duration_h,
                energy_wh=energy_wh, zone=zone, after=list(after))


def opts(**overrides) -> SolveOptions:
    values = {"time_limit_s": 5.0, "num_workers": 4, "core_time_limit_s": 3.0}
    values.update(overrides)
    return SolveOptions(**values)


def two_excavators(horizon_h: int = 40) -> Requirements:
    return reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h)


def hand_solvable_graph() -> TaskGraph:
    """Chain T1(4h) -> T2(3h) plus independent T3(5h): optimum 7 h on two robots."""
    return TaskGraph(tasks=[
        task("T1", "excavator", 4, 100, "Z1"),
        task("T2", "excavator", 3, 100, "Z2", after=("T1",)),
        task("T3", "excavator", 5, 100, "Z3"),
    ])


def assert_schedule(out: Schedule | Failure) -> Schedule:
    assert isinstance(out, Schedule), f"expected a Schedule, got {out!r}"
    return out


def assert_infeasible(out: Schedule | Failure) -> Failure:
    assert isinstance(out, Failure), f"expected a Failure, got {out!r}"
    assert out.kind == "infeasible"
    assert out.conflicts == sorted(set(out.conflicts)), "core must be sorted and de-duplicated"
    return out


# ----------------------------------------------------------------------------- feasible solves
def test_feasible_small_instance_passes_check_schedule():
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000),
                robot("HL1", "hauler", 3000)], horizon_h=40)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 4, 1200, "Z1"),
        task("T2", "excavator", 3, 900, "Z2", after=("T1",)),
        task("T3", "hauler", 5, 800, "Z1", after=("T1",)),
        task("T4", "excavator", 6, 2000, "Z3"),
        task("T5", "hauler", 2, 300, "Z2", after=("T2", "T3")),
    ])
    out = assert_schedule(solve_schedule(req, graph, FAST))

    assert check_schedule(req, graph, out) == []
    assert out.status in ("optimal", "feasible")
    assert {e.task for e in out.entries} == set(graph.ids())
    assert out.makespan_h == max(e.end_h for e in out.entries)
    assert out.objective_bound_h is not None and out.objective_bound_h <= out.makespan_h
    assert out.solve_time_s >= 0.0
    assert out.makespan_h <= req.horizon_h


def test_optimal_makespan_on_hand_solvable_instance():
    out = assert_schedule(solve_schedule(two_excavators(), hand_solvable_graph(), FAST))
    assert out.status == "optimal"
    assert out.makespan_h == 7
    assert out.objective_bound_h == 7
    assert out.entry("T2").start_h >= out.entry("T1").end_h


def test_zone_no_overlap_serialises_two_tasks_in_one_zone():
    req = two_excavators()
    same_zone = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                                 task("T2", "excavator", 3, 100, "Z1")])
    out = assert_schedule(solve_schedule(req, same_zone, FAST))
    assert out.makespan_h == 7  # 4 + 3: the zone forces them back to back
    first, second = sorted(out.entries, key=lambda e: e.start_h)
    assert second.start_h >= first.end_h
    assert check_schedule(req, same_zone, out) == []

    split_zones = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                                   task("T2", "excavator", 3, 100, "Z2")])
    assert assert_schedule(solve_schedule(req, split_zones, FAST)).makespan_h == 4


def test_realistic_landing_pad_instance_is_optimal_at_critical_path():
    req = reqs(
        [robot("SV1", "surveyor", 1500), robot("EX1", "excavator", 5000),
         robot("EX2", "excavator", 5000), robot("HL1", "hauler", 3000),
         robot("HL2", "hauler", 3000), robot("CP1", "compactor", 3500),
         robot("PR1", "printer", 4000)],
        horizon_h=120,
    )
    graph = TaskGraph(tasks=[
        task("T1", "surveyor", 2, 150, "Z1"),
        task("T2", "excavator", 8, 2200, "Z1", after=("T1",)),
        task("T3", "excavator", 6, 1800, "Z2", after=("T1",)),
        task("T4", "hauler", 6, 1500, "Z1", after=("T2",)),
        task("T5", "hauler", 5, 1200, "Z2", after=("T3",)),
        task("T6", "excavator", 4, 1100, "Z1", after=("T4",)),
        task("T7", "compactor", 6, 1600, "Z1", after=("T6",)),
        task("T8", "excavator", 5, 1400, "Z2", after=("T5",)),
        task("T9", "compactor", 4, 1200, "Z2", after=("T8",)),
        task("T10", "printer", 10, 3000, "Z1", after=("T7",)),
        task("T11", "surveyor", 2, 150, "Z1", after=("T10",)),
        task("T12", "surveyor", 1, 100, "Z2", after=("T9",)),
    ])
    out = assert_schedule(solve_schedule(req, graph, FAST))
    assert check_schedule(req, graph, out) == []
    assert out.status == "optimal"
    assert out.makespan_h == 38  # T1->T2->T4->T6->T7->T10->T11 = 2+8+6+4+6+10+2


# ----------------------------------------------------------------------------- locked / offset
def test_locked_task_is_pinned_to_robot_and_start():
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000),
                robot("HL1", "hauler", 3000)], horizon_h=60)
    graph = hand_solvable_graph()
    lock = ScheduleEntry(task="T3", robot="EX2", start_h=3, end_h=8)
    out = assert_schedule(solve_schedule(req, graph, FAST, locked=[lock]))
    pinned = out.entry("T3")
    assert (pinned.robot, pinned.start_h, pinned.end_h) == ("EX2", 3, 8)
    assert check_schedule(req, graph, out) == []

    # a lock onto a robot of the wrong type is honoured (treated as capable for that pair)
    odd = ScheduleEntry(task="T3", robot="HL1", start_h=0, end_h=5)
    out = assert_schedule(solve_schedule(req, graph, FAST, locked=[odd]))
    assert out.entry("T3").robot == "HL1"
    assert check_schedule(req, graph, out) == ["capability:T3"]  # the checker stays strict

    # locks naming an unknown task or robot are ignored instead of crashing
    stale = [ScheduleEntry(task="T99", robot="EX1", start_h=0, end_h=1),
             ScheduleEntry(task="T1", robot="GHOST", start_h=0, end_h=4)]
    out = assert_schedule(solve_schedule(req, graph, FAST, locked=stale))
    assert check_schedule(req, graph, out) == []


def test_start_offset_shifts_absolute_times():
    req = two_excavators(horizon_h=40)
    graph = hand_solvable_graph()
    out = assert_schedule(solve_schedule(req, graph, opts(start_offset_h=10)))
    assert min(e.start_h for e in out.entries) == 10
    assert out.makespan_h == 17  # 7 h of work after 10 h already elapsed
    assert out.status == "optimal"
    assert check_schedule(req, graph, out) == []

    # the offset eats into the horizon: 10 + 7 > 16
    out = solve_schedule(two_excavators(horizon_h=16), graph, opts(start_offset_h=10))
    assert "horizon" in assert_infeasible(out).conflicts


# ----------------------------------------------------------------------------- infeasibility cores
def test_energy_core_for_single_capable_robot():
    req = reqs([robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000)],
                horizon_h=100)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 4, 3000, "Z1"),
        task("T2", "excavator", 3, 3200, "Z2"),
        task("T3", "hauler", 2, 100, "Z2"),
    ])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["energy:EX1"]
    assert failure.hint == "EX1 would need 6,200 Wh but has 5,000 Wh."


def test_precedence_plus_horizon_core():
    req = two_excavators(horizon_h=20)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 10, 100, "Z1"),
        task("T2", "excavator", 10, 100, "Z2", after=("T1",)),
        task("T3", "excavator", 10, 100, "Z3", after=("T2",)),
    ])
    expected = {"horizon", "precedence:T1->T2", "precedence:T2->T3"}
    minimized = assert_infeasible(solve_schedule(req, graph, FAST))
    assert set(minimized.conflicts) == expected
    assert "20 h horizon" in minimized.hint and "30 h" in minimized.hint

    raw = assert_infeasible(solve_schedule(req, graph, opts(minimize_core=False)))
    assert expected <= set(raw.conflicts)


def test_bin_packing_core_names_both_robots_but_not_horizon():
    req = two_excavators(horizon_h=100)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 1, 4000, "Z1"),
        task("T2", "excavator", 1, 4000, "Z2"),
        task("T3", "excavator", 1, 1900, "Z3"),
    ])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert set(failure.conflicts) == {"energy:EX1", "energy:EX2"}
    assert "horizon" not in failure.conflicts
    assert "9,900 Wh in total" in failure.hint and "10,000 Wh combined" in failure.hint


def test_robot_no_overlap_core_with_horizon():
    req = reqs([robot("EX1", "excavator", 5000)], horizon_h=15)
    graph = TaskGraph(tasks=[task("T1", "excavator", 10, 100, "Z1"),
                             task("T2", "excavator", 10, 100, "Z2")])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert set(failure.conflicts) == {"horizon", "robot:EX1"}
    assert "EX1 runs one task at a time" in failure.hint and "20 h" in failure.hint


def test_zone_core_with_horizon():
    req = two_excavators(horizon_h=15)
    graph = TaskGraph(tasks=[task("T1", "excavator", 10, 100, "Z1"),
                             task("T2", "excavator", 10, 100, "Z1")])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert set(failure.conflicts) == {"horizon", "zone:Z1"}
    assert "Zone Z1 admits one robot at a time" in failure.hint


def test_precedence_cycle_core():
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1", after=("T2",)),
                             task("T2", "excavator", 3, 100, "Z2", after=("T1",))])
    failure = assert_infeasible(solve_schedule(two_excavators(), graph, FAST))
    assert failure.conflicts == ["precedence:T1->T2", "precedence:T2->T1"]
    assert "cycle" in failure.hint


def test_minimize_core_shrinks_a_redundant_core():
    req = reqs([robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000)],
                horizon_h=100)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 4, 3000, "Z1"),
        task("T2", "excavator", 3, 3200, "Z2"),
        task("T3", "hauler", 2, 100, "Z2"),
    ])
    inst = _instance(req, graph, FAST, [])
    redundant = ["zone:Z2", "energy:EX1", "horizon", "robot:EX1"]
    assert _minimize_core(inst, redundant) == ["energy:EX1"]

    # a core that is already minimal survives untouched (sorted)
    assert _minimize_core(inst, ["energy:EX1"]) == ["energy:EX1"]


def test_explain_infeasibility_can_be_disabled():
    req = reqs([robot("EX1", "excavator", 5000)], horizon_h=100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 3000, "Z1"),
                             task("T2", "excavator", 3, 3200, "Z2")])
    failure = assert_infeasible(solve_schedule(req, graph, opts(explain_infeasibility=False)))
    assert failure.conflicts == []
    assert failure.hint


# ----------------------------------------------------------------------------- defensive paths
def test_capability_and_reference_problems_are_reported_not_raised():
    graph = TaskGraph(tasks=[task("T1", "crane", 4, 100, "Z1"),
                             task("T2", "excavator", 3, 100, "Z2", after=("T9",))])
    failure = assert_infeasible(solve_schedule(two_excavators(), graph, FAST))
    assert failure.conflicts == ["capability:T1", "reference:T2.after=T9"]
    assert "crane" in failure.hint and "T9" in failure.hint


def test_solver_timeout_maps_unknown_status(monkeypatch):
    monkeypatch.setattr(cp_model.CpSolver, "solve",
                        lambda self, model, solution_callback=None: cp_model.UNKNOWN)
    out = solve_schedule(two_excavators(), hand_solvable_graph(), FAST)
    assert isinstance(out, Failure)
    assert out.kind == "solver_timeout"
    assert out.conflicts == []
    assert out.hint


def test_model_invalid_status_raises(monkeypatch):
    monkeypatch.setattr(cp_model.CpSolver, "solve",
                        lambda self, model, solution_callback=None: cp_model.MODEL_INVALID)
    with pytest.raises(RuntimeError):
        solve_schedule(two_excavators(), hand_solvable_graph(), FAST)


def test_core_extraction_timeout_yields_empty_core(monkeypatch):
    statuses = iter([cp_model.INFEASIBLE])  # hard solve: infeasible; core solve: times out

    def fake_solve(self, model, solution_callback=None):
        return next(statuses, cp_model.UNKNOWN)

    monkeypatch.setattr(cp_model.CpSolver, "solve", fake_solve)
    failure = assert_infeasible(solve_schedule(two_excavators(), hand_solvable_graph(), FAST))
    assert failure.conflicts == []
    assert "could not isolate" in failure.hint


def test_equally_good_schedules_break_the_same_way_on_every_call():
    """Two identical excavators and four equal, independent digs: every pairing is optimal.
    The production solve starts on one worker, so every call returns the same schedule (what
    lets an offline replay reproduce a recorded run's schedule, and its simulation)."""
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h=40)
    graph = TaskGraph(tasks=[task(f"T{i}", "excavator", 4, 500, f"Z{i}") for i in range(1, 5)])
    first = assert_schedule(solve_schedule(req, graph, FAST))
    for _ in range(4):
        assert assert_schedule(solve_schedule(req, graph, FAST)).entries == first.entries


def test_the_portfolio_takes_over_when_one_worker_does_not_settle_it(monkeypatch):
    """A one-worker solve that proves nothing in its slice (SINGLE_WORKER_S set to 0) hands the
    rest of the time limit to the portfolio, which still proves the optimum."""
    calls: list[int] = []
    run = schedule_module._run

    def recording(built, *, workers, time_limit_s):
        calls.append(workers)
        return run(built, workers=workers, time_limit_s=time_limit_s)

    monkeypatch.setattr(schedule_module, "_run", recording)
    monkeypatch.setattr(schedule_module, "SINGLE_WORKER_S", 0.0)
    out = assert_schedule(solve_schedule(two_excavators(), hand_solvable_graph(), FAST))
    assert calls == [1, FAST.num_workers]
    assert out.status == "optimal"


# ----------------------------------------------------------------------------- check_schedule
def test_check_schedule_reports_every_kind_of_violation():
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000),
                robot("HL1", "hauler", 3000)], horizon_h=10)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 4, 3000, "Z1"),
        task("T2", "excavator", 3, 2500, "Z1", after=("T1",)),
        task("T3", "hauler", 2, 100, "Z2"),
        task("T4", "hauler", 1, 50, "Z3"),
    ])
    bad = Schedule(status="feasible", makespan_h=11, entries=[
        ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=4),
        ScheduleEntry(task="T2", robot="EX1", start_h=2, end_h=5),  # precedence/robot/zone/energy
        ScheduleEntry(task="T3", robot="HL1", start_h=9, end_h=11),  # horizon
        ScheduleEntry(task="T3", robot="XX", start_h=0, end_h=3),  # duplicate/capability/duration
        ScheduleEntry(task="T9", robot="EX1", start_h=7, end_h=8),   # unknown
    ])
    assert check_schedule(req, graph, bad) == sorted([
        "precedence:T1->T2", "robot:EX1", "zone:Z1", "energy:EX1", "horizon",
        "duplicate:T3", "capability:T3", "duration:T3", "unknown:T9", "missing:T4",
    ])

    # makespan_h must be the latest end_h: a client could send any figure with valid entries
    wrong_makespan = Schedule(status="optimal", makespan_h=0, entries=[
        ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=4),
        ScheduleEntry(task="T2", robot="EX2", start_h=4, end_h=7),
        ScheduleEntry(task="T3", robot="HL1", start_h=0, end_h=2),
        ScheduleEntry(task="T4", robot="HL1", start_h=2, end_h=3),
    ])
    assert check_schedule(req, graph, wrong_makespan) == ["makespan"]
    assert check_schedule(req, graph, wrong_makespan.model_copy(update={"makespan_h": 9})) == \
        ["makespan"]

    good = Schedule(status="optimal", makespan_h=7, entries=[
        ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=4),
        ScheduleEntry(task="T2", robot="EX2", start_h=4, end_h=7),
        ScheduleEntry(task="T3", robot="HL1", start_h=0, end_h=2),
        ScheduleEntry(task="T4", robot="HL1", start_h=2, end_h=3),
    ])
    assert check_schedule(req, graph, good) == []


# ----------------------------------------------------------------------------- core fallback
def chain_of_three() -> tuple[Requirements, TaskGraph]:
    """Three 10 h tasks in a chain against a 20 h horizon: core {horizon, both edges}."""
    return two_excavators(horizon_h=20), TaskGraph(tasks=[
        task("T1", "excavator", 10, 100, "Z1"),
        task("T2", "excavator", 10, 100, "Z2", after=("T1",)),
        task("T3", "excavator", 10, 100, "Z3", after=("T2",)),
    ])


def tight_two_robot_instance() -> tuple[Requirements, TaskGraph]:
    """20 excavator tasks on 2 robots / 4 zones with dense precedence at horizon = optimum - 1
    (optimum 51). The multi-worker production solve refutes it in milliseconds; the
    single-worker assumption solve cannot within its budget (CP-SAT 9.15)."""
    rng = random.Random(1)
    robots = [robot(f"EX{k}", "excavator", 10**6) for k in range(2)]
    tasks = [task(f"T{i}", "excavator", rng.randint(2, 8), 10, f"Z{rng.randint(1, 4)}",
                  tuple(f"T{j}" for j in range(max(0, i - 3), i) if rng.random() < 0.3))
             for i in range(20)]
    return reqs(robots, 50), TaskGraph(tasks=tasks)


def test_core_isolation_falls_back_to_hard_trials_when_the_assumption_solve_times_out():
    req, graph = tight_two_robot_instance()
    loose = assert_schedule(solve_schedule(req.model_copy(update={"horizon_h": 10**4}), graph,
                                           opts(num_workers=8)))
    assert loose.makespan_h == 51

    o = opts(core_time_limit_s=1.0)  # the assumption solve gives up after 1 s; trials take ms
    t0 = time.perf_counter()
    failure = assert_infeasible(solve_schedule(req, graph, o))
    elapsed = time.perf_counter() - t0
    assert failure.conflicts == ["horizon", "robot:EX0", "robot:EX1"]
    assert "could not isolate" not in failure.hint and "50 h horizon" in failure.hint
    assert elapsed < 4.0

    # sound (infeasible on its own) and minimal (every group is needed)
    inst = _instance(req, graph, o, [])
    assert _trial(inst, failure.conflicts, time_limit_s=5.0) == cp_model.INFEASIBLE
    for name in failure.conflicts:
        rest = [n for n in failure.conflicts if n != name]
        assert _trial(inst, rest, time_limit_s=5.0) in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def test_core_isolation_fallback_on_an_inconclusive_assumption_solve(monkeypatch):
    def inconclusive(inst, enabled, **kwargs):
        groups = sorted(_build(inst, soft=True, enabled=enabled).groups)
        return cp_model.UNKNOWN, [], groups

    monkeypatch.setattr(schedule_module, "_core_solve", inconclusive)
    req, graph = chain_of_three()
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert set(failure.conflicts) == {"horizon", "precedence:T1->T2", "precedence:T2->T3"}
    assert "20 h horizon" in failure.hint

    # the fallback always isolates, even with core minimisation switched off
    failure = assert_infeasible(solve_schedule(req, graph, opts(minimize_core=False)))
    assert set(failure.conflicts) == {"horizon", "precedence:T1->T2", "precedence:T2->T3"}


def test_explanation_deadline_bounds_every_solve():
    req, graph = chain_of_three()
    failure = assert_infeasible(solve_schedule(req, graph, opts(explain_time_limit_s=0.0)))
    assert failure.conflicts == [] and "could not isolate" in failure.hint

    # a deletion filter that runs out of time keeps the remaining groups (sound, not minimal)
    inst = _instance(req, graph, FAST, [])
    redundant = ["zone:Z2", "horizon", "precedence:T1->T2", "precedence:T2->T3"]
    assert _minimize_core(inst, redundant, deadline=time.monotonic()) == sorted(redundant)
    assert _minimize_core(inst, redundant) == ["horizon", "precedence:T1->T2", "precedence:T2->T3"]


def test_trial_skips_the_solve_without_time():
    inst = _instance(*chain_of_three(), FAST, [])
    assert _trial(inst, ["horizon"], time_limit_s=0.0) == cp_model.UNKNOWN
    assert _trial(inst, ["horizon"], time_limit_s=1.0) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    assert _trial(inst, ["horizon", "precedence:T1->T2", "precedence:T2->T3"],
                  time_limit_s=1.0) == cp_model.INFEASIBLE


# ----------------------------------------------------------------------------- milli-Wh unit
def test_fractional_energies_and_batteries_are_exact_to_a_milli_wh():
    """Replans hand over 3-dp task energies and 2-dp remaining batteries. Rounding tasks up and
    batteries down to whole Wh used to reject budgets the validator and the checker accept."""
    req = reqs([robot("EX1", "excavator", 1784.38)], horizon_h=100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 892.19, "Z1"),
                             task("T2", "excavator", 3, 892.19, "Z2")])
    assert validate_graph(req, graph) is None
    out = assert_schedule(solve_schedule(req, graph, FAST))
    assert check_schedule(req, graph, out) == []

    # one milli-Wh over the budget: the solver, the checker and the hint all agree, exactly
    over = TaskGraph(tasks=[task("T1", "excavator", 4, 892.191, "Z1"),
                            task("T2", "excavator", 3, 892.19, "Z2")])
    failure = assert_infeasible(solve_schedule(req, over, FAST))
    assert failure.conflicts == ["energy:EX1"]
    assert failure.hint == "EX1 would need 1,784.381 Wh but has 1,784.38 Wh."
    hand = Schedule(status="feasible", makespan_h=7, entries=[
        ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=4),
        ScheduleEntry(task="T2", robot="EX1", start_h=4, end_h=7),
    ])
    assert check_schedule(req, over, hand) == ["energy:EX1"]
    assert check_schedule(req, graph, hand) == []

    # the shared-pool hint quotes the model's figures as well
    req2 = two_excavators()
    req2 = req2.model_copy(update={"robots": [robot("EX1", "excavator", 2000.5),
                                              robot("EX2", "excavator", 2000.5)]})
    split = TaskGraph(tasks=[task("T1", "excavator", 1, 1500.25, "Z1"),
                             task("T2", "excavator", 1, 1500.25, "Z2"),
                             task("T3", "excavator", 1, 1000.5, "Z3")])
    failure = assert_infeasible(solve_schedule(req2, split, FAST))
    assert set(failure.conflicts) == {"energy:EX1", "energy:EX2"}
    assert "4,001 Wh in total" in failure.hint and "4,001 Wh combined" in failure.hint


# ----------------------------------------------------------------------------- hint robustness
def test_precedence_hint_looks_the_edge_up_instead_of_parsing_the_id():
    """Ids with '->' cannot pass the schema any more, but a hand-built graph can still carry
    them; the hint must name the right tasks instead of raising KeyError."""
    def raw(task_id: str, after: tuple[str, ...]) -> Task:
        return Task.model_construct(id=task_id, name=task_id, robot_type="excavator",
                                    duration_h=4, energy_wh=100.0, zone=f"Z-{task_id}",
                                    after=list(after), structure=None)

    graph = TaskGraph.model_construct(tasks=[raw("A->B", ("C",)), raw("C", ("A->B",))],
                                      notes=None)
    failure = assert_infeasible(solve_schedule(two_excavators(), graph, FAST))
    assert failure.conflicts == ["precedence:A->B->C", "precedence:C->A->B"]
    assert "C cannot start until A->B finishes" in failure.hint
    assert "A->B cannot start until C finishes" in failure.hint and "cycle" in failure.hint


def test_horizon_hint_names_a_locked_task_that_ends_past_the_horizon():
    req = two_excavators(horizon_h=10)
    graph = hand_solvable_graph()  # T1(4) -> T2(3), T3(5)
    lock = ScheduleEntry(task="T3", robot="EX2", start_h=8, end_h=13)
    failure = assert_infeasible(solve_schedule(req, graph, FAST, locked=[lock]))
    assert failure.conflicts == ["horizon"]
    assert "10 h horizon" in failure.hint
    assert "the locked task T3 cannot end before 13 h, 3 h past the 10 h deadline" in failure.hint
    # folded into the chain figure
    assert "locked starts included, needs 13 h, 3 h past the 10 h deadline" in failure.hint

    # the same lock inside the horizon: no complaint, the plan simply waits for it
    out = assert_schedule(solve_schedule(two_excavators(horizon_h=13), graph, FAST, locked=[lock]))
    assert out.makespan_h == 13 and out.entry("T3").start_h == 8


# ----------------------------------------------------------------------------- disjoint cores
def test_independent_conflicts_are_reported_together_as_disjoint_cores():
    req = reqs([robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000),
                robot("SV1", "surveyor", 1500)], horizon_h=15)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 4, 3000, "Z1"),   # EX1 cannot power T1 + T2 ........ energy:EX1
        task("T2", "excavator", 3, 3200, "Z2"),
        task("T3", "hauler", 10, 100, "Z3"),      # HL1 cannot do both in 15 h .. horizon+robot:HL1
        task("T4", "hauler", 10, 100, "Z4"),
        task("T5", "surveyor", 2, 2000, "Z5"),    # SV1 cannot power T5 ............. energy:SV1
    ])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["energy:EX1", "energy:SV1", "horizon", "robot:HL1"]
    sentences = failure.hint.split(". ")
    assert len(sentences) == 4 and failure.hint.endswith(".")  # one sentence per conflict
    assert "EX1 would need 6,200 Wh but has 5,000 Wh." in failure.hint
    assert "SV1 would need 2,000 Wh but has 1,500 Wh." in failure.hint
    assert "15 h horizon" in failure.hint and "HL1 runs one task at a time" in failure.hint

    # deterministic, and the union really explains everything: without it the rest is feasible
    assert assert_infeasible(solve_schedule(req, graph, FAST)).conflicts == failure.conflicts
    inst = _instance(req, graph, FAST, [])
    rest = [g for g in sorted(_build(inst, soft=True).groups) if g not in failure.conflicts]
    assert _trial(inst, rest, time_limit_s=5.0) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    for conflict in failure.conflicts:  # and no reported group is idle
        assert conflict in _build(inst, soft=True).groups


# ----------------------------------------------------------------------------- travel
# Site A is the simulator tests' geometry: default depot (20, 1), Z1 = (10, 30), Z2 = (30, 30)
# and Z3 auto-placed at (4, 36). At 60 cells/h every leg is under an hour, so each task gets a
# 1 h allowance. Site B puts two zones 36 cells apart in the far corners, 41 cells from the
# depot: at 12 cells/h the first leg takes 4 h (20.573 Wh), the leg between them 3 h (18 Wh).
SITE_A = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(10, 30)),
                                    Zone(id="Z2", center=(30, 30))])
DEPOT_A = (20, 1)
Z3_A = (4, 36)
SITE_B = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(2, 38)),
                                    Zone(id="Z2", center=(38, 38))])
DEPOT_TO_Z1_B = math.hypot(2 - 20, 38 - 1)   # 41.15 cells
Z1_TO_Z2_B = 36.0


def three_task_graph() -> TaskGraph:
    """T1 (Z1) and T2 (Z2) for the excavators, T3 (Z3) for the hauler after both: 7 h."""
    return TaskGraph(tasks=[
        task("T1", "excavator", 4, 800, "Z1"),
        task("T2", "excavator", 4, 800, "Z2"),
        task("T3", "hauler", 3, 600, "Z3", after=("T1", "T2")),
    ])


def site_a_req(speed: float = 60.0) -> Requirements:
    return reqs([robot("EX1", "excavator", 5000, speed), robot("EX2", "excavator", 5000, speed),
                 robot("HL1", "hauler", 3000, speed)], 100, site=SITE_A)


def far_zones_req(battery_wh: float, horizon_h: int = 50) -> Requirements:
    return reqs([robot("R1", "excavator", battery_wh, 12.0)], horizon_h, site=SITE_B)


def two_far_tasks() -> TaskGraph:
    return TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                            task("T2", "excavator", 4, 100, "Z2", after=("T1",))])


def test_first_leg_delays_the_start_and_the_simulator_arrives_on_time():
    req, graph = site_a_req(), three_task_graph()
    out = assert_schedule(solve_schedule(req, graph, FAST))
    # 7 h of work plus the hour the first legs take (31 and 38 cells at 60 cells/h round up)
    assert out.makespan_h == 8 and out.status == "optimal"
    assert {e.task: e.start_h for e in out.entries} == {"T1": 1, "T2": 1, "T3": 5}
    assert check_schedule(req, graph, out) == [] and check_schedule(req, graph, out, FAST) == []
    inst = _instance(req, graph, FAST, [])
    assert {k: (t.allowance_h, t.first_leg_h) for k, t in inst.transit.items()} == {
        (0, "EX1"): (1, 1), (0, "EX2"): (1, 1), (1, "EX1"): (1, 1), (1, "EX2"): (1, 1),
        (2, "HL1"): (1, 1)}
    # the energy allowance is the longest leg to the zone at 0.5 Wh per cell
    assert inst.transit[0, "EX1"].allowance_mwh == to_mwh(travel_wh(distance(DEPOT_A, (10, 30))))
    assert inst.transit[2, "HL1"].allowance_mwh == to_mwh(travel_wh(distance(DEPOT_A, Z3_A)))
    # the simulator drives the real legs and starts every task at its scheduled hour
    res = Simulator(req, graph, out).run(stop_on_event=False)
    assert res.completed and res.finished_at_h == pytest.approx(8.0) and res.events == []


def test_consecutive_tasks_on_one_robot_are_a_whole_allowance_apart():
    req, graph = far_zones_req(300), two_far_tasks()
    out = assert_schedule(solve_schedule(req, graph, FAST))
    # 41 cells from the depot at 12 cells/h: T1 cannot start before 4 h. The allowance before
    # T2 is the longest leg R1 could drive to Z2 (41 cells from the depot, not the 36 from Z1):
    # the bound is independent of the sequence, so T2 waits 4 h after T1 rather than 3.
    entries = sorted(out.entries, key=lambda e: e.start_h)
    assert [(e.task, e.start_h, e.end_h) for e in entries] == [("T1", 4, 8), ("T2", 12, 16)]
    assert out.makespan_h == 16 and out.status == "optimal"
    assert check_schedule(req, graph, out, FAST) == []
    inst = _instance(req, graph, FAST, [])
    assert inst.transit[1, "R1"] == inst.transit[0, "R1"]
    assert inst.transit[0, "R1"].allowance_h == travel_hours(DEPOT_TO_Z1_B, 12) == 4
    res = Simulator(req, graph, out).run(stop_on_event=False)
    assert res.completed and res.finished_at_h == pytest.approx(16.0) and res.events == []
    # the real legs (41 + 36 cells, 38.6 Wh) stay within the 41.1 Wh the solver reserved
    left = res.frames[-1].robots[0].battery_wh
    assert left == pytest.approx(300 - 200 - travel_wh(DEPOT_TO_Z1_B + Z1_TO_Z2_B), abs=0.05)


def test_travel_energy_is_budgeted_and_the_hint_names_its_share():
    req, graph = far_zones_req(200), two_far_tasks()
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["energy:R1"]
    assert failure.hint == "R1 would need 241 Wh, about 41 Wh of it travel, but has 200 Wh."
    # the allowance is a bound: 238.6 Wh would really be spent, 241.146 Wh is what must fit
    assert assert_schedule(solve_schedule(far_zones_req(241.15), graph, FAST)).makespan_h == 16
    assert assert_infeasible(solve_schedule(far_zones_req(241.14), graph, FAST)).conflicts == \
        ["energy:R1"]

    # the shared-pool hint carries the share as well: three 100 Wh tasks on two 125 Wh robots,
    # each task worth 120.573 Wh with its leg, so no robot can take two of them
    pool = reqs([robot("R1", "excavator", 125, 12.0), robot("R2", "excavator", 125, 12.0)], 50,
                site=SITE_B)
    three = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 4, 100, "Z2"),
                             task("T3", "excavator", 4, 100, "Z1")])
    failure = assert_infeasible(solve_schedule(pool, three, FAST))
    assert set(failure.conflicts) == {"energy:R1", "energy:R2"}
    assert failure.hint == ("R1 and R2 each have 125 Wh; the excavator tasks need 362 Wh in "
                            "total, about 62 Wh of it travel, more than the 250 Wh the "
                            "2 excavators have combined.")


def test_check_schedule_times_and_charges_the_actual_legs():
    req, graph = far_zones_req(200), two_far_tasks()
    back_to_back = Schedule(status="optimal", makespan_h=8, entries=[
        ScheduleEntry(task="T1", robot="R1", start_h=0, end_h=4),
        ScheduleEntry(task="T2", robot="R1", start_h=4, end_h=8),
    ])
    # the 36-cell leg between the zones needs 3 h and 18 Wh the schedule does not have
    assert check_schedule(req, graph, back_to_back) == ["energy:R1", "robot:R1"]
    # with the solver's options the first leg (41 cells from the depot) is timed and charged too
    assert check_schedule(req, graph, back_to_back, SolveOptions()) == ["energy:R1", "robot:R1"]

    spaced = Schedule(status="feasible", makespan_h=14, entries=[
        ScheduleEntry(task="T1", robot="R1", start_h=3, end_h=7),
        ScheduleEntry(task="T2", robot="R1", start_h=10, end_h=14),
    ])
    roomy = far_zones_req(300)
    assert check_schedule(roomy, graph, spaced) == []             # start position unknown
    assert check_schedule(roomy, graph, spaced, SolveOptions()) == ["robot:R1"]  # 3 h < 4 h
    assert check_schedule(req, graph, spaced) == ["energy:R1"]    # 200 Wh of work + the 18 Wh leg
    nearby = SolveOptions(robot_positions={"R1": (2.0, 34.0)})   # 4 cells from Z1: 1 h
    assert check_schedule(roomy, graph, spaced, nearby) == []
    later = SolveOptions(start_offset_h=3, robot_positions={"R1": (2.0, 34.0)})
    assert check_schedule(roomy, graph, spaced, later) == ["robot:R1"]  # 3 + 1 > 3


def test_robot_positions_move_the_first_leg():
    # one task in Z1 from three starting points: the depot (31 cells away, 3 h at 12 cells/h),
    # a cell 5 cells away (1 h) and the zone itself (no leg at all)
    req = reqs([robot("EX1", "excavator", 5000, 12.0)], 50, site=SITE_A)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")])
    for positions, start in ((None, 3), ({"EX1": (10.0, 25.0)}, 1), ({"EX1": (10.0, 30.0)}, 0)):
        o = opts(robot_positions=positions)
        out = assert_schedule(solve_schedule(req, graph, o))
        assert (out.entry("T1").start_h, out.makespan_h) == (start, start + 4)
        assert check_schedule(req, graph, out, o) == []
    # verified against the wrong starting point, the schedule from the zone fails its first leg
    assert check_schedule(req, graph, out, SolveOptions()) == ["robot:EX1"]
    # the leg counts from the moment the plan starts
    shifted = opts(start_offset_h=10)
    out = assert_schedule(solve_schedule(req, graph, shifted))
    assert out.entry("T1").start_h == 13 and check_schedule(req, graph, out, shifted) == []
    # a replan passes the frame positions: the allowance covers the leg from there as well
    far = opts(robot_positions={"EX1": (39.0, 0.0)})            # 41.7 cells from Z1: 4 h
    transit = _instance(req, graph, far, []).transit[0, "EX1"]
    assert transit.first_leg_h == transit.allowance_h == 4
    assert transit.allowance_mwh == to_mwh(travel_wh(distance((39, 0), (10, 30))))


def test_allowance_covers_only_the_zones_a_robot_can_be_sent_to():
    # the printer only ever works in Z1, 4 cells from the depot; the excavators' far zone must
    # not inflate its allowance (the bound is over the zones the robot can be sent to)
    site = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(20, 5)),
                                      Zone(id="Z2", center=(20, 38))])
    req = reqs([robot("PR1", "printer", 4000, 12.0), robot("EX1", "excavator", 5000, 12.0)],
               100, site=site)
    graph = TaskGraph(tasks=[task("T1", "printer", 4, 100, "Z1"),
                             task("T2", "printer", 4, 100, "Z1", after=("T1",)),
                             task("T3", "excavator", 4, 100, "Z2")])
    inst = _instance(req, graph, FAST, [])
    assert inst.transit[0, "PR1"].allowance_h == inst.transit[1, "PR1"].allowance_h == 1
    assert inst.transit[2, "EX1"].allowance_h == 4            # 37 cells
    out = assert_schedule(solve_schedule(req, graph, FAST))
    assert [(e.start_h, e.end_h) for e in (out.entry("T1"), out.entry("T2"))] == [(1, 5), (6, 10)]
    assert out.entry("T3").start_h == 4 and out.makespan_h == 10
    assert check_schedule(req, graph, out, FAST) == []


def test_locked_task_is_not_held_up_by_its_first_leg():
    # a replan pins the task underway to planner time 0: its robot is on it already, wherever
    # the options say the robots start
    req, graph = far_zones_req(300), two_far_tasks()
    lock = ScheduleEntry(task="T1", robot="R1", start_h=0, end_h=4)
    out = assert_schedule(solve_schedule(req, graph, FAST, locked=[lock]))
    assert (out.entry("T1").start_h, out.entry("T2").start_h) == (0, 8)
    assert check_schedule(req, graph, out) == []
    at_zone = SolveOptions(robot_positions={"R1": (2.0, 38.0)})  # where the frame has it
    assert check_schedule(req, graph, out, at_zone) == []


def test_horizon_and_robot_hints_count_the_transit():
    req = far_zones_req(300, horizon_h=7)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["horizon"]
    assert "7 h horizon" in failure.hint
    assert ("needs 8 h including the transit to its first zone, 1 h past the 7 h deadline"
            in failure.hint)

    # 8 h of work on one robot, but 4 h to reach each zone: 16 h against a 15 h horizon
    req = far_zones_req(300, horizon_h=15)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 4, 100, "Z2")])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert set(failure.conflicts) == {"horizon", "robot:R1"}
    assert ("R1 runs one task at a time, and the 2 tasks only it can do already take 8 h "
            "back to back plus up to 8 h of transit.") in failure.hint


def test_allowance_adds_no_variables_to_the_model():
    graph = three_task_graph()
    with_travel = _build(_instance(site_a_req(), graph, FAST, []), soft=False, minimize=True)
    flat = reqs([robot("EX1", "excavator", 5000, 60.0), robot("EX2", "excavator", 5000, 60.0),
                 robot("HL1", "hauler", 3000, 60.0)], 100)
    without = _build(_instance(flat, graph, FAST, []), soft=False, minimize=True)
    assert len(with_travel.model.proto.variables) == len(without.model.proto.variables)
    # the only additions are the five first-leg bounds, one per (task, capable robot) pair
    assert len(with_travel.model.proto.constraints) == len(without.model.proto.constraints) + 5


def spread_instance(horizon_h: int) -> tuple[Requirements, TaskGraph]:
    """20 tasks on 3 robots of 2 types over four corner zones at 12 cells/h (3-5 h legs)."""
    rng = random.Random(3)
    site = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(4, 4)), Zone(id="Z2", center=(35, 4)),
                                      Zone(id="Z3", center=(4, 35)), Zone(id="Z4", center=(35, 35))])
    robots = [robot("EX1", "excavator", 10**6, 12.0), robot("EX2", "excavator", 10**6, 12.0),
              robot("HL1", "hauler", 10**6, 12.0)]
    tasks = [task(f"T{i}", "excavator" if i % 3 else "hauler", rng.randint(2, 8), 10,
                  f"Z{rng.randint(1, 4)}",
                  tuple(f"T{j}" for j in range(max(0, i - 3), i) if rng.random() < 0.3))
             for i in range(20)]
    return reqs(robots, horizon_h, site=site), TaskGraph(tasks=tasks)


def test_twenty_task_instance_with_travel_solves_fast_and_still_explains():
    req, graph = spread_instance(10**4)
    t0 = time.perf_counter()
    out = assert_schedule(solve_schedule(req, graph, opts(num_workers=8)))
    assert time.perf_counter() - t0 < 1.0
    assert out.status == "optimal" and check_schedule(req, graph, out, FAST) == []
    res = Simulator(req, graph, out).run(stop_on_event=False)
    assert res.completed and res.events == [] and res.finished_at_h == pytest.approx(out.makespan_h)

    tight = req.model_copy(update={"horizon_h": out.makespan_h - 1})
    t0 = time.perf_counter()
    failure = assert_infeasible(solve_schedule(tight, graph, opts(core_time_limit_s=1.0)))
    assert time.perf_counter() - t0 < 8.0
    assert "horizon" in failure.conflicts and "could not isolate" not in failure.hint
    assert f"{out.makespan_h - 1} h horizon" in failure.hint
