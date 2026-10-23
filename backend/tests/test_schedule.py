"""Tests for moonwright.pipeline.schedule (CP-SAT scheduling + infeasibility cores)."""

from __future__ import annotations

import itertools
import json
import math
import random
import time
from dataclasses import replace
from pathlib import Path

import pytest
from ortools.sat.python import cp_model

import moonwright.pipeline.schedule as schedule_module
from moonwright.geometry import (
    arrival_hour,
    cheapest_legs,
    distance,
    leg_mwh,
    route_cost,
    spanning_cost,
    start_positions,
    travel_hours,
    travel_wh,
    zone_positions,
)
from moonwright.pipeline.schedule import (
    SolveOptions,
    _build,
    _instance,
    _likely_cores,
    _minimize_core,
    _trial,
    check_schedule,
    sequence_travel,
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
    The production solve settles the optimum it proves (module docstring, "Solve strategy"),
    so every call returns the same schedule (what lets an offline replay reproduce a recorded
    run's schedule, and its simulation)."""
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h=40)
    graph = TaskGraph(tasks=[task(f"T{i}", "excavator", 4, 500, f"Z{i}") for i in range(1, 5)])
    first = assert_schedule(solve_schedule(req, graph, FAST))
    for _ in range(4):
        assert assert_schedule(solve_schedule(req, graph, FAST)).entries == first.entries


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
# and Z3 auto-placed at (4, 36). At 60 cells/h every leg is under an hour, so each takes 1 h.
# Site B puts two zones 36 cells apart in the far corners, 41 cells from the depot: at
# 12 cells/h the first leg takes 4 h (20.574 Wh, rounded up to the milli-Wh), the leg between
# them 3 h (18 Wh). Phase 3k made travel exact: each robot pays the legs of the order CP-SAT
# picks for it, where it used to pay, before every task, the longest leg it could ever drive
# to that zone (the "transit allowance").
SITE_A = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(10, 30)),
                                    Zone(id="Z2", center=(30, 30))])
DEPOT_A = (20, 1)
Z3_A = (4, 36)
SITE_B = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(2, 38)),
                                    Zone(id="Z2", center=(38, 38))])
DEPOT_B = (20, 1)
DEPOT_TO_Z1_B = math.hypot(2 - 20, 38 - 1)   # 41.15 cells
Z1_TO_Z2_B = 36.0
FIRST_LEG_B = leg_mwh(DEPOT_B, (2, 38))      # 20,574 mWh
DATA = Path(__file__).resolve().parent / "data"


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


def assert_no_drift(req: Requirements, graph: TaskGraph, out: Schedule) -> None:
    """A disturbance-free replay: no event, every task works from its scheduled hour (the
    frame at ``start_h + dt`` lists it) and the run ends exactly at the makespan."""
    res = Simulator(req, graph, out).run(stop_on_event=False)
    assert res.completed and res.events == [], [e.detail for e in res.events]
    assert res.finished_at_h == pytest.approx(out.makespan_h)
    for entry in out.entries:
        first = next(f.t_h for f in res.frames if entry.task in f.active_tasks)
        assert first == pytest.approx(entry.start_h + 0.25), entry


def test_first_leg_delays_the_start_and_the_simulator_arrives_on_time():
    req, graph = site_a_req(), three_task_graph()
    out = assert_schedule(solve_schedule(req, graph, FAST))
    # 7 h of work plus the hour the first legs take (31 and 38 cells at 60 cells/h round up)
    assert out.makespan_h == 8 and out.status == "optimal"
    assert {e.task: e.start_h for e in out.entries} == {"T1": 1, "T2": 1, "T3": 5}
    assert check_schedule(req, graph, out) == [] and check_schedule(req, graph, out, FAST) == []
    inst = _instance(req, graph, FAST, [])
    assert {(i, r): inst.first_leg_h(i, r) for i in range(3) for r in inst.capable[i]} == {
        (0, "EX1"): 1, (0, "EX2"): 1, (1, "EX1"): 1, (1, "EX2"): 1, (2, "HL1"): 1}
    # the first leg is priced from where the robot starts, at 0.5 Wh per cell
    hauler = inst.routes["HL1"]
    assert hauler.origin == DEPOT_A and hauler.mwh[0][hauler.nodes[2]] == leg_mwh(DEPOT_A, Z3_A)
    # the simulator drives the real legs and starts every task at its scheduled hour
    assert_no_drift(req, graph, out)


def test_consecutive_tasks_on_one_robot_are_their_exact_leg_apart():
    req, graph = far_zones_req(300), two_far_tasks()
    out = assert_schedule(solve_schedule(req, graph, FAST))
    # 41 cells from the depot at 12 cells/h: T1 cannot start before 4 h. Z1 -> Z2 is 36 cells,
    # 3 h, so T2 starts 3 h after T1 ends: 15 h. The allowance before T2 used to be the
    # longest leg R1 could ever drive to Z2 (the 41 cells from the depot, 4 h): 16 h
    entries = sorted(out.entries, key=lambda e: e.start_h)
    assert [(e.task, e.start_h, e.end_h) for e in entries] == [("T1", 4, 8), ("T2", 11, 15)]
    assert out.makespan_h == 15 and out.status == "optimal"
    assert check_schedule(req, graph, out, FAST) == []
    assert sequence_travel(req, graph, out, FAST)["R1"].mwh == FIRST_LEG_B + 18000
    assert_no_drift(req, graph, out)
    # the simulator spends the real legs (41 + 36 cells, 38.573 Wh), which is what was charged
    res = Simulator(req, graph, out).run(stop_on_event=False)
    left = res.frames[-1].robots[0].battery_wh
    assert left == pytest.approx(300 - 200 - travel_wh(DEPOT_TO_Z1_B + Z1_TO_Z2_B), abs=0.01)


def test_consecutive_tasks_in_one_zone_drive_nothing():
    # the printer works twice in Z1, 4 cells from the depot: one 1 h leg, then T2 right after
    # T1 (the allowance made it wait an hour in its own zone); the excavator's far zone does
    # not enter the printer's legs at all
    site = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(20, 5)),
                                      Zone(id="Z2", center=(20, 38))])
    req = reqs([robot("PR1", "printer", 4000, 12.0), robot("EX1", "excavator", 5000, 12.0)],
               100, site=site)
    graph = TaskGraph(tasks=[task("T1", "printer", 4, 100, "Z1"),
                             task("T2", "printer", 4, 100, "Z1", after=("T1",)),
                             task("T3", "excavator", 4, 100, "Z2")])
    out = assert_schedule(solve_schedule(req, graph, FAST))
    assert [(e.start_h, e.end_h) for e in (out.entry("T1"), out.entry("T2"))] == [(1, 5), (5, 9)]
    assert out.entry("T3").start_h == 4 and out.makespan_h == 9    # 10 h with the allowance
    assert check_schedule(req, graph, out, FAST) == []
    assert sequence_travel(req, graph, out, FAST)["PR1"].mwh == leg_mwh((20, 1), (20, 5)) == 2000
    # every task the printer may run is in one zone: no order to pick, so no circuit at all
    built = _build(_instance(req, graph, FAST, []), soft=False, minimize=True)
    assert not any(c.has_circuit() for c in built.model.proto.constraints)
    assert_no_drift(req, graph, out)


def test_the_solver_picks_the_order_that_drives_least():
    """Three tasks in three zones on a line, 10 cells apart, the depot at its end: one hour per
    leg at 10 cells/h when R1 drives them in line (6 h, 15 Wh of travel), more in any other
    order. The allowance charged each task the longest leg into its zone (2, 2 and 3 h): 9 h."""
    site = Site(grid=(40, 40), depot=(0, 0),
                zones=[Zone(id=f"Z{k}", center=(0, 10 * k)) for k in (1, 2, 3)])
    graph = TaskGraph(tasks=[task("T1", "excavator", 1, 100, "Z3"),
                             task("T2", "excavator", 1, 100, "Z1"),
                             task("T3", "excavator", 1, 100, "Z2")])

    def line(battery_wh: float) -> Requirements:
        return reqs([robot("R1", "excavator", battery_wh, 10.0)], 50, site=site)

    out = assert_schedule(solve_schedule(line(10**4), graph, FAST))
    order = sorted(out.entries, key=lambda e: e.start_h)
    assert [(e.task, e.start_h) for e in order] == [("T2", 1), ("T3", 3), ("T1", 5)]
    assert out.makespan_h == 6 and out.status == "optimal"
    # the arcs carry the energy: 300 Wh of work and 30 cells is exactly enough, the 20 Wh of
    # the next best order (10 + 20 + 10 cells) is not, and a milli-Wh less is infeasible
    out = assert_schedule(solve_schedule(line(315), graph, FAST))
    assert check_schedule(line(315), graph, out, FAST) == []
    assert sequence_travel(line(315), graph, out, FAST)["R1"].mwh == 15000
    failure = assert_infeasible(solve_schedule(line(314.999), graph, FAST))
    assert failure.conflicts == ["energy:R1"]
    assert failure.hint == "R1 would need at least 315 Wh, 15 Wh of it travel, but has 314.999 Wh."


def test_travel_energy_is_charged_on_the_arcs_and_the_hint_names_its_share():
    req, graph = far_zones_req(200), two_far_tasks()
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["energy:R1"]
    # the share is the least travel any order needs (depot -> Z1 -> Z2): "at least"
    assert failure.hint == "R1 would need at least 239 Wh, 39 Wh of it travel, but has 200 Wh."
    # exact to the milli-Wh: 200 Wh of work, 20.574 Wh to Z1 and 18 Wh on to Z2 (41.146 Wh
    # with the allowance)
    assert assert_schedule(solve_schedule(far_zones_req(238.574), graph, FAST)).makespan_h == 15
    assert assert_infeasible(solve_schedule(far_zones_req(238.573), graph, FAST)).conflicts == \
        ["energy:R1"]

    # the shared-pool hint carries the share as well: three 100 Wh tasks on two 125 Wh robots,
    # so no robot can take two of them; reaching both zones costs the pool at least 38.574 Wh
    pool = reqs([robot("R1", "excavator", 125, 12.0), robot("R2", "excavator", 125, 12.0)], 50,
                site=SITE_B)
    three = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 4, 100, "Z2"),
                             task("T3", "excavator", 4, 100, "Z1")])
    failure = assert_infeasible(solve_schedule(pool, three, FAST))
    assert set(failure.conflicts) == {"energy:R1", "energy:R2"}
    assert failure.hint == ("R1 and R2 each have 125 Wh; the excavator tasks need at least "
                            "339 Wh in total, 39 Wh of it travel, more than the 250 Wh the "
                            "2 excavators have combined.")


def test_the_way_back_is_free():
    # two tasks in Z1, 41 cells out, then one in Z2: the work and the legs out (Z1 to Z1 drives
    # nothing) are exactly the battery. The robot never drives the 41 cells home, so the arc
    # that closes its circuit is not charged
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 4, 100, "Z1", after=("T1",)),
                             task("T3", "excavator", 4, 100, "Z2", after=("T2",))])
    exact = 300 + (FIRST_LEG_B + 18000) / 1000
    out = assert_schedule(solve_schedule(far_zones_req(exact), graph, FAST))
    assert check_schedule(far_zones_req(exact), graph, out, FAST) == []
    assert isinstance(solve_schedule(far_zones_req(exact - 0.001), graph, FAST), Failure)


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
    # a replan passes the frame positions: the first leg is timed and priced from there
    far = opts(robot_positions={"EX1": (39.0, 0.0)})            # 41.7 cells from Z1: 4 h
    inst = _instance(req, graph, far, [])
    route = inst.routes["EX1"]
    assert inst.first_leg_h(0, "EX1") == 4 and route.origin == (39.0, 0.0)
    assert route.mwh[0][route.nodes[0]] == leg_mwh((39, 0), (10, 30))


def test_a_locked_task_is_where_its_robot_starts():
    # a replan pins the task underway to planner time 0: its robot is on it, so its circuit
    # starts there, it drives no first leg, and the next leg is the one out of its zone
    req, graph = far_zones_req(300), two_far_tasks()
    lock = ScheduleEntry(task="T1", robot="R1", start_h=0, end_h=4)
    out = assert_schedule(solve_schedule(req, graph, FAST, locked=[lock]))
    assert (out.entry("T1").start_h, out.entry("T2").start_h) == (0, 7)   # 4 h + the 3 h leg
    assert check_schedule(req, graph, out) == []
    at_zone = SolveOptions(robot_positions={"R1": (2.0, 38.0)})  # where the frame has it
    assert check_schedule(req, graph, out, at_zone) == []
    route = _instance(req, graph, FAST, [lock]).routes["R1"]
    assert route.origin == (2, 38) and route.on == route.nodes[0]
    # the work and the one leg out of Z1, to the milli-Wh: nothing for the way into T1
    assert assert_schedule(solve_schedule(far_zones_req(218), graph, FAST, locked=[lock]))
    failure = assert_infeasible(
        solve_schedule(far_zones_req(217.999), graph, FAST, locked=[lock]))
    assert failure.conflicts == ["energy:R1"]
    assert failure.hint == "R1 would need at least 218 Wh, 18 Wh of it travel, but has 217.999 Wh."
    # replanned mid-run: from the frame positions, hours already elapsed, the lock first
    late = SolveOptions(time_limit_s=5.0, num_workers=4, start_offset_h=6,
                        robot_positions={"R1": (2.0, 38.0)})
    lock = ScheduleEntry(task="T1", robot="R1", start_h=6, end_h=10)
    out = assert_schedule(solve_schedule(req, graph, late, locked=[lock]))
    assert (out.entry("T1").start_h, out.entry("T2").start_h) == (6, 13)
    assert check_schedule(req, graph, out, late) == []


def test_horizon_and_robot_hints_count_the_transit():
    req = far_zones_req(300, horizon_h=7)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["horizon"]
    assert "7 h horizon" in failure.hint
    assert ("needs 8 h including the transit to its first zone, 1 h past the 7 h deadline"
            in failure.hint)

    # 8 h of work on one robot, 4 h to the first zone and 3 h to the second: 15 h, which the
    # allowance (4 h before each) made 16 h; a 14 h horizon is one hour short
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 4, 100, "Z2")])
    assert assert_schedule(solve_schedule(far_zones_req(300, horizon_h=15), graph, FAST)) \
        .makespan_h == 15
    failure = assert_infeasible(solve_schedule(far_zones_req(300, horizon_h=14), graph, FAST))
    assert set(failure.conflicts) == {"horizon", "robot:R1"}
    assert ("R1 runs one task at a time, and the 2 tasks only it can do already take 8 h "
            "back to back plus at least 7 h of transit.") in failure.hint


def test_a_circuit_only_where_there_is_an_order_to_pick():
    graph = three_task_graph()
    built = _build(_instance(site_a_req(), graph, FAST, []), soft=False, minimize=True)
    circuits = [c.circuit for c in built.model.proto.constraints if c.has_circuit()]
    # EX1 and EX2 may each run T1 (Z1) and T2 (Z2): 3 self-loops (idle on the start), 2 arcs
    # into each task and 2 back to the start; HL1 has one task and no group to read an order
    assert [len(c.literals) for c in circuits] == [9, 9]
    # every zone on one cell: no order to pick and no circuit, the model of a travel-free site
    flat = reqs([robot("EX1", "excavator", 5000, 60.0), robot("EX2", "excavator", 5000, 60.0),
                 robot("HL1", "hauler", 3000, 60.0)], 100)
    flat_model = _build(_instance(flat, graph, FAST, []), soft=False, minimize=True)
    assert not any(c.has_circuit() for c in flat_model.model.proto.constraints)
    # the explanation model hangs the timed arcs on robot:<r> together with the arc literal
    soft = _build(_instance(site_a_req(), graph, FAST, []), soft=True)
    timed = [c for c in soft.model.proto.constraints
             if c.has_linear() and len(c.enforcement_literal) == 2]
    assert len(timed) == 4 and {"robot:EX1", "robot:EX2"} <= set(soft.groups)


def test_twins_start_in_graph_order_and_cost_nothing():
    """Four equal halves of two digs (as the task-graph prompt splits work) are interchangeable:
    the production model starts them in graph order, and the optimum is the one the model
    without that ordering finds."""
    req = reqs([robot("EX1", "excavator", 5000, 12.0), robot("EX2", "excavator", 5000, 12.0)],
               100, site=SITE_A)
    graph = TaskGraph(tasks=[task("T1.1", "excavator", 3, 400, "Z1"),
                             task("T1.2", "excavator", 3, 400, "Z1"),
                             task("T2.1", "excavator", 2, 300, "Z2"),
                             task("T2.2", "excavator", 2, 300, "Z2"),
                             task("T3", "excavator", 1, 100, "Z1", after=("T1.1", "T1.2"))])
    inst = _instance(req, graph, FAST, [])
    assert schedule_module._twins(inst) == [[0, 1], [2, 3]]
    out = assert_schedule(solve_schedule(req, graph, FAST))
    assert out.entry("T1.1").start_h <= out.entry("T1.2").start_h
    assert out.entry("T2.1").start_h <= out.entry("T2.2").start_h
    every_group = sorted(_build(inst, soft=True).groups)
    plain = _build(inst, soft=False, enabled=every_group, minimize=True)   # no twin ordering
    solver = cp_model.CpSolver()
    assert solver.solve(plain.model) == cp_model.OPTIMAL
    assert solver.objective_value == out.makespan_h
    assert check_schedule(req, graph, out, FAST) == []


def test_cores_name_the_groups_they_named_before():
    """Travel moved into the circuit, the group names stayed: the recorded tight-battery graph
    is refused on the same two batteries, the long chain on the horizon and its edges."""
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "tight_battery"
    req = Requirements.model_validate_json((fixtures / "structure.json").read_text())
    graph = TaskGraph.model_validate_json((fixtures / "taskgraph_1.json").read_text())
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["energy:EX1", "energy:EX2"]
    req, graph = chain_of_three()
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["horizon", "precedence:T1->T2", "precedence:T2->T3"]


@pytest.mark.parametrize("name", ["four_mast_pads", "shielding_mound"])
def test_the_solver_never_loses_to_the_model_it_checks(name):
    """scripts/llm_schedule_check.py: on these run-3 graphs Nemotron 3 Ultra wrote schedules
    check_schedule accepted at 43 h and 36 h, against CP-SAT's 'optimal' 60 h and 45 h under
    the allowance. With exact travel the solver is at or below the model's makespan."""
    data = json.loads((DATA / f"{name}_llm_schedule_plan.json").read_text())
    req = Requirements.model_validate(data["requirements"])
    graph = TaskGraph.model_validate(data["task_graph"])
    out = assert_schedule(solve_schedule(req, graph, opts(num_workers=8)))
    assert out.status == "optimal"
    assert out.makespan_h <= data["llm_makespan_h"] < data["schedule"]["makespan_h"]
    assert check_schedule(req, graph, out, SolveOptions()) == []
    assert_no_drift(req, graph, out)


def brute_force_makespan(req: Requirements, graph: TaskGraph, o: SolveOptions,
                         locked: list[ScheduleEntry]) -> int | None:
    """The optimum under check_schedule's rules, by enumeration: every assignment, every order
    per robot and per zone, each timed by the longest path through precedence, the legs of the
    robot orders (the first from the start) and the zone orders; the batteries charge the work
    and the legs of the robot orders. A locked task runs first on its robot, from its pinned
    hour. None when nothing fits."""
    tasks = graph.tasks
    layout = zone_positions(req.site, (t.zone for t in tasks))
    starts = start_positions(req, o.robot_positions)
    robots = {r.id: r for r in req.robots}
    index = {t.id: i for i, t in enumerate(tasks)}
    pins = {index[e.task]: e for e in locked}
    capable = [[pins[i].robot] if i in pins else [r.id for r in req.robots
                                                  if r.type == t.robot_type]
               for i, t in enumerate(tasks)]
    edges = [(index[p], j, tasks[index[p]].duration_h) for j, t in enumerate(tasks)
             for p in t.after]
    zones: dict[str, list[int]] = {}
    for i, t in enumerate(tasks):
        zones.setdefault(t.zone, []).append(i)
    best = None
    for assign in itertools.product(*capable):
        mine: dict[str, list[int]] = {}
        for i, r in enumerate(assign):
            mine.setdefault(r, []).append(i)
        for orders in itertools.product(*(itertools.permutations(v) for v in mine.values())):
            arcs, floor, fits = list(edges), [o.start_offset_h] * len(tasks), True
            for i, e in pins.items():
                floor[i] = e.start_h
            for robot_id, order in zip(mine, orders, strict=True):
                if any(i in pins for i in order[1:]):
                    fits = False
                here, used = starts[robot_id], 0
                for k, i in enumerate(order):
                    there = layout[tasks[i].zone]
                    used += to_mwh(tasks[i].energy_wh) + leg_mwh(here, there)
                    hours = travel_hours(distance(here, there), robots[robot_id].speed_cells_per_h)
                    if k == 0:
                        floor[i] = max(floor[i], o.start_offset_h + hours)
                    else:
                        arcs.append((order[k - 1], i, tasks[order[k - 1]].duration_h + hours))
                    here = there
                fits = fits and used <= to_mwh(robots[robot_id].battery_wh)
            if not fits:
                continue
            for zone_orders in itertools.product(*(itertools.permutations(z)
                                                   for z in zones.values())):
                timed = arcs + [(z[k], z[k + 1], tasks[z[k]].duration_h)
                                for z in zone_orders for k in range(len(z) - 1)]
                start = list(floor)
                for _ in range(len(tasks) + 1):   # longest paths; still moving: a cycle
                    moved = False
                    for i, j, w in timed:
                        if start[i] + w > start[j]:
                            start[j], moved = start[i] + w, True
                    if not moved:
                        break
                if moved or any(start[i] != e.start_h for i, e in pins.items()):
                    continue
                makespan = max(s + t.duration_h for s, t in zip(start, tasks, strict=True))
                if makespan <= req.horizon_h and (best is None or makespan < best):
                    best = makespan
    return best


def tiny_instance(seed: int) -> tuple[Requirements, TaskGraph, SolveOptions, list[ScheduleEntry]]:
    """Five tasks on three robots of two types at mixed speeds and tight or loose batteries,
    three zones and a depot anywhere, a few edges; sometimes hours already elapsed, robots
    elsewhere and a task underway (its robot standing in its zone), as a replan has them."""
    rng = random.Random(seed)
    site = Site(grid=(40, 40), depot=(rng.randint(0, 39), rng.randint(0, 39)),
                zones=[Zone(id=f"Z{k}", center=(rng.randint(0, 39), rng.randint(0, 39)))
                       for k in (1, 2, 3)])
    robots = [robot("EX1", "excavator", rng.choice([400, 700, 10**5]), rng.choice([6, 12, 30])),
              robot("EX2", "excavator", rng.choice([400, 700, 10**5]), rng.choice([6, 12, 30])),
              robot("HL1", "hauler", rng.choice([400, 10**5]), rng.choice([6, 12]))]
    tasks = [task(f"T{i}", "hauler" if i % 3 == 2 else "excavator", rng.randint(1, 5),
                  rng.randint(50, 250), f"Z{rng.randint(1, 3)}",
                  tuple(f"T{j}" for j in range(i) if rng.random() < 0.25)) for i in range(5)]
    req = reqs(robots, rng.choice([10**4, 14, 20]), site=site)
    graph = TaskGraph(tasks=tasks)
    offset = rng.choice([0, 3])
    positions = {r.id: (rng.uniform(0, 39), rng.uniform(0, 39)) for r in robots
                 if rng.random() < 0.5}
    locked: list[ScheduleEntry] = []
    free = [t for t in tasks if not t.after]
    if seed % 2 and free:
        t = rng.choice(free)
        r = rng.choice([r for r in robots if r.type == t.robot_type])
        locked = [ScheduleEntry(task=t.id, robot=r.id, start_h=offset,
                                end_h=offset + t.duration_h)]
        cell = zone_positions(site, [t.zone])[t.zone]
        positions[r.id] = (float(cell[0]), float(cell[1]))
    o = SolveOptions(time_limit_s=5.0, num_workers=4, start_offset_h=offset,
                     robot_positions=positions or None)
    return req, graph, o, locked


@pytest.mark.parametrize("seed", range(24))
def test_optimal_is_the_optimum_check_schedule_admits(seed):
    """The solver's makespan is the best any schedule check_schedule accepts can reach, found
    by enumeration (2,000 seeds of each kind agreed when phase 3k landed; the allowance missed
    the optimum on 88 of the first 200)."""
    req, graph, o, locked = tiny_instance(seed)
    out = solve_schedule(req, graph, o, locked=locked)
    best = brute_force_makespan(req, graph, o, locked)
    if best is None:
        assert isinstance(out, Failure) and out.kind == "infeasible"
        return
    out = assert_schedule(out)
    assert out.makespan_h == best and out.status == "optimal"
    assert check_schedule(req, graph, out, o) == []


# ----------------------------------------------------------------------------- 3k review
def test_route_cost_is_the_cheapest_route_and_never_above_one():
    """One robot cannot branch: its least travel through a set of zones is the cheapest route
    from its start (Held-Karp), on the cheapest chains of legs, so never above a route it can
    drive and never below the tree the pool bound uses."""
    rng = random.Random(7)
    for _ in range(40):
        start = (rng.uniform(0, 39), rng.uniform(0, 39))
        zones = [(rng.randint(0, 39), rng.randint(0, 39)) for _ in range(rng.randint(1, 6))]
        cost = cheapest_legs([start, *zones], leg_mwh)
        best = min(sum(leg_mwh(p, q) for p, q in itertools.pairwise([start, *order]))
                   for order in itertools.permutations(dict.fromkeys(zones)))
        assert spanning_cost([start], zones, cost) <= route_cost(start, zones, cost) <= best
    assert route_cost((0, 0), [], leg_mwh) == 0 and route_cost((3, 4), [(3, 4)], leg_mwh) == 0


def test_arrival_hour_rounds_the_whole_trip_from_a_fractional_hour():
    """A replan at t = 30.5 h hands the planner hour 0 = 30 h and robots that are free at 0.5:
    a 0.75 h leg arrives at 1.25, by hour 2, not by 0 + 1; standing in its zone the robot still
    cannot start before hour 1. From a whole hour it is the hour plus travel_hours."""
    assert arrival_hour(0.5, 9.0, 12.0) == 2 and travel_hours(9.0, 12.0) == 1
    assert arrival_hour(0.5, 0.0, 12.0) == 1 and arrival_hour(0.5, 4.8, 12.0) == 1
    assert arrival_hour(3, 41.146, 12.0) == 3 + travel_hours(41.146, 12.0) == 7
    assert arrival_hour(3.0, 0.0, 12.0) == 3


def doubled(req: Requirements, graph: TaskGraph) -> tuple[Requirements, TaskGraph]:
    """``graph`` twice over, the copy in zones of its own 7 cells right and 11 up (wrapped),
    against twice the horizon: the 3k review's 48-task explanation case."""
    zones = list(req.site.zones)
    for z in req.site.zones:
        zones.append(Zone(id=f"{z.id}c", center=((z.center[0] + 7) % 40, (z.center[1] + 11) % 40)))
    tasks = [*graph.tasks, *(t.model_copy(update={
        "id": f"{t.id}c", "zone": f"{t.zone}c", "after": [f"{a}c" for a in t.after]})
        for t in graph.tasks)]
    return (req.model_copy(update={"site": req.site.model_copy(update={"zones": zones}),
                                   "horizon_h": 2 * req.horizon_h}), TaskGraph(tasks=tasks))


def test_a_pool_shortfall_is_isolated_by_the_batteries_alone():
    """3k review: the run-3 four_mast_pads graph twice over puts 10,649.6 Wh of excavation on
    two 5,000 Wh excavators. The single-worker assumption solve times out on the circuits, and
    the deletion filter then spent the 15 s explanation budget and left a 32-group core,
    precedence edges and all. A hard trial on the two batteries alone (a likely core) proves
    it in milliseconds."""
    data = json.loads((DATA / "four_mast_pads_llm_schedule_plan.json").read_text())
    req, graph = doubled(Requirements.model_validate(data["requirements"]),
                         TaskGraph.model_validate(data["task_graph"]))
    inst = _instance(req, graph, SolveOptions(), [])
    every = sorted(_build(inst, soft=True).groups)
    assert ["energy:EX1", "energy:EX2"] in _likely_cores(inst, every)
    t0 = time.perf_counter()
    failure = assert_infeasible(solve_schedule(req, graph, SolveOptions()))
    assert time.perf_counter() - t0 < 6.0
    assert failure.conflicts == ["energy:EX1", "energy:EX2"]
    assert failure.hint.startswith("EX1 and EX2 each have 5,000 Wh; the excavator tasks need "
                                   "at least 10,")
    assert "more than the 10,000 Wh the 2 excavators have combined" in failure.hint


def test_a_later_lock_is_a_task_with_a_pinned_start():
    """3k review: a robot's earliest lock was taken to be the task it is on whatever its start,
    so a lock dated later forbade the robot any work before it (11 h where check_schedule's
    optimum was 10 h) and put it in the lock's zone without a leg, which check_schedule then
    flagged. Only a lock that starts at or before start_offset_h is the task its robot is on;
    a later one is a stop of its circuit like any other, its leg timed and charged."""
    req = far_zones_req(300, horizon_h=50)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 4, 100, "Z2")])
    lock = ScheduleEntry(task="T2", robot="R1", start_h=11, end_h=15)
    out = assert_schedule(solve_schedule(req, graph, FAST, locked=[lock]))
    # 4 h to Z1, T1, 3 h on to Z2: T2 at its pinned 11 h. Taken as "on" T2, R1 used to wait
    # in Z2 and dig T1 after it, from 18 h
    assert (out.entry("T1").start_h, out.entry("T2").start_h, out.makespan_h) == (4, 11, 15)
    assert check_schedule(req, graph, out, SolveOptions()) == []
    assert sequence_travel(req, graph, out, SolveOptions())["R1"].mwh == FIRST_LEG_B + 18000
    # the leg into the lock is charged like any other: the work and both legs, to the milli-Wh
    exact = 200 + (FIRST_LEG_B + 18000) / 1000
    assert isinstance(solve_schedule(far_zones_req(exact), graph, FAST, locked=[lock]), Schedule)
    failure = assert_infeasible(
        solve_schedule(far_zones_req(exact - 0.001), graph, FAST, locked=[lock]))
    assert failure.conflicts == ["energy:R1"]
    # a lock at the offset is still the task its robot is on: no leg into it
    now = ScheduleEntry(task="T2", robot="R1", start_h=0, end_h=4)
    out = assert_schedule(solve_schedule(req, graph, FAST, locked=[now]))
    assert (out.entry("T2").start_h, out.entry("T1").start_h) == (0, 7)


def test_a_lock_its_robot_cannot_reach_is_reported_before_solving():
    """A later lock the robot cannot reach by its pinned start (the first leg alone is 4 h)
    has no schedule; the model would refuse it on a hard constraint no core can name, so the
    precheck reports it, as robot:<r>, which is what check_schedule calls such a start."""
    req = far_zones_req(300)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")])
    lock = ScheduleEntry(task="T1", robot="R1", start_h=2, end_h=6)
    failure = assert_infeasible(
        solve_schedule(req, graph, opts(start_offset_h=1), locked=[lock]))
    assert failure.conflicts == ["robot:R1"]
    assert failure.hint == "T1 is locked to R1 from 2 h, but R1 cannot be in its zone before 5 h."
    late = Schedule(status="feasible", makespan_h=6,
                    entries=[ScheduleEntry(task="T1", robot="R1", start_h=2, end_h=6)])
    assert check_schedule(req, graph, late, SolveOptions(start_offset_h=1)) == ["robot:R1"]
    # the lock a robot is on cannot start before the hour the options let it set off either
    on = ScheduleEntry(task="T1", robot="R1", start_h=0, end_h=4)
    failure = assert_infeasible(solve_schedule(
        req, graph, opts(robot_positions={"R1": (2.0, 38.0)}, robot_ready_h={"R1": 0.5}),
        locked=[on]))
    assert failure.hint == "T1 is locked to R1 from 0 h, but R1 cannot be in its zone before 1 h."


def test_a_fractional_set_off_hour_times_the_first_leg_whole():
    """A replan at a fractional hour lets the robots that are not on a locked task set off at
    the fraction (``robot_ready_h``): R1 stands 9 cells from Z1 (0.75 h at 12 cells/h) and is
    free at 0.5, so T1 starts at hour 2 (1.25 rounded up), where hour 0 + travel_hours made it
    1 and the robot arrived a quarter of an hour late. check_schedule times it the same way."""
    req = far_zones_req(300)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")])
    here = {"R1": (2.0, 29.0)}
    assert assert_schedule(solve_schedule(req, graph, opts(robot_positions=here))) \
        .entry("T1").start_h == 1
    ready = opts(robot_positions=here, robot_ready_h={"R1": 0.5})
    out = assert_schedule(solve_schedule(req, graph, ready))
    assert out.entry("T1").start_h == 2 and check_schedule(req, graph, out, ready) == []
    early = out.model_copy(update={"entries": [ScheduleEntry(task="T1", robot="R1", start_h=1,
                                                             end_h=5)], "makespan_h": 5})
    assert check_schedule(req, graph, early, ready) == ["robot:R1"]
    assert check_schedule(req, graph, early, opts(robot_positions=here)) == []


def test_deterministic_and_settled_schedules_repeat():
    """The production solve races its workers and then settles (``_settle``): the settling
    solve interleaves the portfolio in a fixed order, so the run-3 four_mast_pads graph, which
    a single worker could not settle within the budget (7 different 41 h schedules in 12
    solves in the 3k review), gets one schedule whatever the worker count. Monte Carlo replans
    and the relaxation advice solve deterministically from the start: the hand-made landing-pad
    fixture (``landing_pad_v1``), whose racing workers find 3 different 80 h schedules in 30
    solves, gets one."""
    data = json.loads((DATA / "four_mast_pads_llm_schedule_plan.json").read_text())

    def entries(req: Requirements, graph: TaskGraph, o: SolveOptions) -> list[tuple]:
        out = assert_schedule(solve_schedule(req, graph, o))
        assert out.status == "optimal" and check_schedule(req, graph, out, o) == []
        return sorted((e.task, e.robot, e.start_h, e.end_h) for e in out.entries)

    four_mast = (Requirements.model_validate(data["requirements"]),
                 TaskGraph.model_validate(data["task_graph"]))
    assert entries(*four_mast, opts(num_workers=8)) == entries(*four_mast, opts(num_workers=4))
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "landing_pad_v1"
    landing = (Requirements.model_validate_json((fixtures / "structure.json").read_text()),
               TaskGraph.model_validate_json((fixtures / "taskgraph_1.json").read_text()))
    o = opts(num_workers=8, deterministic=True)
    first = entries(*landing, o)
    assert all(entries(*landing, replace(o, num_workers=w)) == first for w in (8, 4, 2))


def test_single_worker_and_deterministic_solves_are_not_settled(monkeypatch):
    """One worker already breaks ties the same way on every call, and so does the
    interleaved portfolio: settling them again would only cost its budget (Monte Carlo
    replans and relaxation solves used to pay it every time)."""
    def refuse(*args, **kwargs):
        raise AssertionError("settled")

    monkeypatch.setattr(schedule_module, "_settle", refuse)
    req, graph = site_a_req(), three_task_graph()
    for o in (opts(num_workers=1), opts(num_workers=8, deterministic=True)):
        assert assert_schedule(solve_schedule(req, graph, o)).makespan_h == 8


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
    # the circuits prove 46 h in about 0.2 s, and settling may add its 0.2 deterministic
    # seconds (SETTLE_DETERMINISTIC_S, about 0.3 s of wall time here)
    assert time.perf_counter() - t0 < 2.0
    assert out.status == "optimal" and check_schedule(req, graph, out, FAST) == []
    res = Simulator(req, graph, out).run(stop_on_event=False)
    assert res.completed and res.events == [] and res.finished_at_h == pytest.approx(out.makespan_h)

    tight = req.model_copy(update={"horizon_h": out.makespan_h - 1})
    t0 = time.perf_counter()
    failure = assert_infeasible(solve_schedule(tight, graph, opts(core_time_limit_s=1.0)))
    assert time.perf_counter() - t0 < 8.0
    assert "horizon" in failure.conflicts and "could not isolate" not in failure.hint
    assert f"{out.makespan_h - 1} h horizon" in failure.hint
