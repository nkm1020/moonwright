"""Tests for moonwright.pipeline.schedule (CP-SAT scheduling + infeasibility cores)."""

from __future__ import annotations

import random
import time

import pytest
from ortools.sat.python import cp_model

import moonwright.pipeline.schedule as schedule_module
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
    Task,
    TaskGraph,
)

FAST = SolveOptions(time_limit_s=5.0, num_workers=4, core_time_limit_s=3.0)


# ----------------------------------------------------------------------------- helpers
def robot(robot_id: str, robot_type: str, battery_wh: float) -> Robot:
    return Robot(id=robot_id, type=robot_type, battery_wh=battery_wh)


def task(task_id: str, robot_type: str, duration_h: int, energy_wh: float, zone: str,
         after: tuple[str, ...] = ()) -> Task:
    return Task(id=task_id, name=task_id, robot_type=robot_type, duration_h=duration_h,
                energy_wh=energy_wh, zone=zone, after=list(after))


def opts(**overrides) -> SolveOptions:
    values = {"time_limit_s": 5.0, "num_workers": 4, "core_time_limit_s": 3.0}
    values.update(overrides)
    return SolveOptions(**values)


def two_excavators(horizon_h: int = 40) -> Requirements:
    return Requirements(robots=[robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)],
                        horizon_h=horizon_h)


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
    req = Requirements(robots=[robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000),
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
    req = Requirements(
        robots=[robot("SV1", "surveyor", 1500), robot("EX1", "excavator", 5000),
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
    req = Requirements(robots=[robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000),
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
    req = Requirements(robots=[robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000)],
                       horizon_h=100)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 4, 3000, "Z1"),
        task("T2", "excavator", 3, 3200, "Z2"),
        task("T3", "hauler", 2, 100, "Z2"),
    ])
    failure = assert_infeasible(solve_schedule(req, graph, FAST))
    assert failure.conflicts == ["energy:EX1"]
    assert "EX1 would need 6200 Wh but has 5000 Wh" in failure.hint


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
    assert "9900 Wh" in failure.hint and "10000 Wh" in failure.hint


def test_robot_no_overlap_core_with_horizon():
    req = Requirements(robots=[robot("EX1", "excavator", 5000)], horizon_h=15)
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
    req = Requirements(robots=[robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000)],
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
    req = Requirements(robots=[robot("EX1", "excavator", 5000)], horizon_h=100)
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


# ----------------------------------------------------------------------------- check_schedule
def test_check_schedule_reports_every_kind_of_violation():
    req = Requirements(robots=[robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000),
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
    return Requirements(robots=robots, horizon_h=50), TaskGraph(tasks=tasks)


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
    req = Requirements(robots=[robot("EX1", "excavator", 1784.38)], horizon_h=100)
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
    assert "EX1 would need 1784.381 Wh but has 1784.38 Wh" in failure.hint
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
    assert "4001 Wh in total" in failure.hint and "4001 Wh combined" in failure.hint


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
    assert "the locked task T3 cannot end before 13 h" in failure.hint
    assert "locked starts included, takes 13 h" in failure.hint  # folded into the chain figure

    # the same lock inside the horizon: no complaint, the plan simply waits for it
    out = assert_schedule(solve_schedule(two_excavators(horizon_h=13), graph, FAST, locked=[lock]))
    assert out.makespan_h == 13 and out.entry("T3").start_h == 8


# ----------------------------------------------------------------------------- disjoint cores
def test_independent_conflicts_are_reported_together_as_disjoint_cores():
    req = Requirements(robots=[robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000),
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
    assert "EX1 would need 6200 Wh but has 5000 Wh" in failure.hint
    assert "SV1 would need 2000 Wh but has 1500 Wh" in failure.hint
    assert "15 h horizon" in failure.hint and "HL1 runs one task at a time" in failure.hint

    # deterministic, and the union really explains everything: without it the rest is feasible
    assert assert_infeasible(solve_schedule(req, graph, FAST)).conflicts == failure.conflicts
    inst = _instance(req, graph, FAST, [])
    rest = [g for g in sorted(_build(inst, soft=True).groups) if g not in failure.conflicts]
    assert _trial(inst, rest, time_limit_s=5.0) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    for conflict in failure.conflicts:  # and no reported group is idle
        assert conflict in _build(inst, soft=True).groups
