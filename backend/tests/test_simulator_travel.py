"""Planner <-> simulator agreement on travel (phase 3a, plan item 4; exact since phase 3k).

The solver times and charges the legs of each robot's actual sequence, straight lines between
zone centres in whole hours (each leg's energy rounded up to the milli-Wh), and the simulator
drives exactly those lines: a disturbance-free replay of a verified plan must therefore start
every task at its scheduled hour, spend what the plan charged and raise no event at all.
``task_overrun`` and battery depletion keep their phase-2 meaning and flag genuine anomalies
only.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from moonwright import geometry
from moonwright.geometry import (
    TRAVEL_WH_PER_CELL,
    depot_position,
    distance,
    start_positions,
    travel_hours,
    travel_wh,
)
from moonwright.llm.base import FixtureClient
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.schedule import (
    SolveOptions,
    check_schedule,
    sequence_travel,
    solve_schedule,
)
from moonwright.schemas import (
    PlanResult,
    Requirements,
    Robot,
    Schedule,
    ScheduleEntry,
    Site,
    Task,
    TaskGraph,
    Zone,
)
from moonwright.sim.montecarlo import solver_only_planner
from moonwright.sim.replan import robot_ready_h, run_with_replanning
from moonwright.sim.simulator import FailureInjection, Simulator, zone_positions
from moonwright.sim.simulator import depot_position as sim_depot

BRIEFS_DIR = Path(__file__).resolve().parents[1] / "examples" / "briefs"
BRIEF_IDS = ["landing_pad", "access_road", "tight_battery"]
FAST = SolveOptions(time_limit_s=5.0, num_workers=4, core_time_limit_s=3.0)
DT_H = 0.25


# --------------------------------------------------------------------------- helpers
def fixture_plan(brief_id: str) -> PlanResult:
    plan = Planner(FixtureClient(), explain=False).plan((BRIEFS_DIR / f"{brief_id}.md").read_text(),
                                                        brief_id=brief_id)
    assert plan.success and plan.schedule is not None and plan.task_graph is not None
    return plan


def first_work_step(frames, task_id: str) -> float | None:
    """Clock at the end of the first step the task was being worked on."""
    return next((f.t_h for f in frames if task_id in f.active_tasks), None)


def assert_on_time_replay(req: Requirements, graph: TaskGraph, schedule: Schedule,
                          dt_h: float = DT_H) -> None:
    """A nominal replay of a solver schedule: no event, every task starts at its hour and the
    run ends exactly at the makespan."""
    res = Simulator(req, graph, schedule, dt_h=dt_h).run(stop_on_event=False)
    assert res.events == [], [e.detail for e in res.events]
    assert res.completed and res.finished_at_h == pytest.approx(schedule.makespan_h)
    for entry in schedule.entries:
        # the task works during [start_h, start_h + dt): the frame at start_h + dt lists it
        assert first_work_step(res.frames, entry.task) == pytest.approx(entry.start_h + dt_h), \
            entry
    assert set(res.frames[-1].done_tasks) == set(graph.ids())
    assert all(r.battery_wh >= 0 for f in res.frames for r in f.robots)


# --------------------------------------------------------------------------- geometry
def test_geometry_helpers():
    assert distance((0, 0), (3, 4)) == 5.0 and distance((1.5, 2.0), (1.5, 2.0)) == 0.0
    assert travel_hours(0.0, 12) == 0 and travel_hours(1e-12, 12) == 0
    assert travel_hours(24, 12) == 2 and travel_hours(24.001, 12) == 3   # whole hours, rounded up
    assert travel_hours(1, 100) == 1                                     # any leg costs an hour
    assert travel_hours(0.3 * 3, 0.3) == 3                               # no float creep
    with pytest.raises(ValueError):
        travel_hours(1.0, 0)
    assert TRAVEL_WH_PER_CELL == 0.5 and travel_wh(10) == 5.0 and travel_wh(-1) == 0.0

    site = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(10, 30))])
    assert depot_position(site) == (20, 1) and depot_position(Site(depot=(3, 3))) == (3, 3)
    layout = geometry.zone_positions(site, ["Z3", "Z1", "Z2"])
    assert layout["Z1"] == (10, 30) and set(layout) == {"Z1", "Z2", "Z3"}
    assert layout["Z2"] == (4, 36) and layout["Z3"] == (12, 36)          # deterministic layout
    assert geometry.zone_positions(site) == {"Z1": (10, 30)}
    req = Requirements(site=site, robots=[Robot(id="A", type="x", battery_wh=1),
                                          Robot(id="B", type="x", battery_wh=1)], horizon_h=1)
    assert start_positions(req) == {"A": (20.0, 1.0), "B": (20.0, 1.0)}
    assert start_positions(req, {"B": (1, 2), "ghost": (9, 9)}) == {"A": (20.0, 1.0),
                                                                     "B": (1.0, 2.0)}


def test_simulator_layout_is_the_shared_geometry():
    """The simulator's zone_positions(req, graph) and depot are the geometry module's, so the
    planner and the simulator put every zone in the same cell."""
    req = Requirements(site=Site(grid=(40, 40), zones=[Zone(id="Z1", center=(10, 30)),
                                                       Zone(id="Z2", center=(30, 30))]),
                       robots=[Robot(id="EX1", type="excavator", battery_wh=5000)], horizon_h=10)
    graph = TaskGraph(tasks=[Task(id="T1", name="t", robot_type="excavator", duration_h=1,
                                  energy_wh=1, zone="Z3")])
    assert sim_depot is depot_position
    assert zone_positions(req, graph) == geometry.zone_positions(req.site, ["Z3"])
    assert zone_positions(req, graph) == {"Z1": (10, 30), "Z2": (30, 30), "Z3": (4, 36)}
    sim = Simulator(req, graph, Schedule(entries=[ScheduleEntry(task="T1", robot="EX1",
                                                                 start_h=0, end_h=1)],
                                         makespan_h=1, status="feasible"))
    assert sim.layout == zone_positions(req, graph) and sim.depot == (20, 1)


# --------------------------------------------------------------------------- fixtures replay
@pytest.mark.parametrize("brief_id", BRIEF_IDS)
def test_fixture_plan_replays_without_any_event(brief_id):
    plan = fixture_plan(brief_id)
    assert check_schedule(plan.requirements, plan.task_graph, plan.schedule, SolveOptions()) == []
    assert_on_time_replay(plan.requirements, plan.task_graph, plan.schedule)
    # a coarser step that still divides the hour arrives on time as well
    assert_on_time_replay(plan.requirements, plan.task_graph, plan.schedule, dt_h=0.5)


def test_fixture_plans_charge_what_the_simulator_spends():
    """The solver charges the legs of each robot's sequence and the replay drives them, so
    every robot ends with the battery the plan left it (each leg is charged rounded up to the
    milli-Wh, so the replay never spends more than the plan charged)."""
    for brief_id in BRIEF_IDS:
        plan = fixture_plan(brief_id)
        req, graph, schedule = plan.requirements, plan.task_graph, plan.schedule
        res = Simulator(req, graph, schedule).run(stop_on_event=False)
        layout = zone_positions(req, graph)
        for robot_id, entries in schedule.by_robot().items():
            robot = req.robot(robot_id)
            pos = start_positions(req)[robot_id]
            cells = 0.0
            for e in entries:
                cells += distance(pos, layout[graph.task(e.task).zone])
                pos = layout[graph.task(e.task).zone]
            work = sum(graph.task(e.task).energy_wh for e in entries)
            left = next(r.battery_wh for r in res.frames[-1].robots if r.id == robot_id)
            assert left == pytest.approx(robot.battery_wh - work - travel_wh(cells), abs=0.01)
            charged = sequence_travel(req, graph, schedule, SolveOptions())[robot_id].mwh
            assert 0 <= charged - travel_wh(cells) * 1000 < len(entries)   # < 1 mWh per leg
            assert left >= 0.10 * robot.battery_wh        # every demo robot keeps a tenth in hand


# --------------------------------------------------------------------------- random geometry
def random_instance(seed: int) -> tuple[Requirements, TaskGraph]:
    """3 robots of 2 types at mixed speeds, 4 zones anywhere on the grid, 8 tasks with a few
    precedence edges: a different geometry every seed."""
    rng = random.Random(seed)
    zones = [Zone(id=f"Z{k}", center=(rng.randint(0, 39), rng.randint(0, 39))) for k in range(1, 5)]
    site = Site(grid=(40, 40), zones=zones, depot=(rng.randint(0, 39), rng.randint(0, 39)))
    robots = [Robot(id="EX1", type="excavator", battery_wh=10**5,
                    speed_cells_per_h=rng.choice([6.0, 12.0, 30.0])),
              Robot(id="EX2", type="excavator", battery_wh=10**5,
                    speed_cells_per_h=rng.choice([6.0, 12.0, 30.0])),
              Robot(id="HL1", type="hauler", battery_wh=10**5,
                    speed_cells_per_h=rng.choice([6.0, 12.0, 30.0]))]
    tasks = [Task(id=f"T{i}", name=f"t{i}", robot_type="hauler" if i % 3 == 2 else "excavator",
                  duration_h=rng.randint(1, 6), energy_wh=rng.randint(50, 400),
                  zone=f"Z{rng.randint(1, 4)}",
                  after=[f"T{j}" for j in range(max(0, i - 2), i) if rng.random() < 0.4])
             for i in range(8)]
    return Requirements(site=site, robots=robots, horizon_h=10**4), TaskGraph(tasks=tasks)


@pytest.mark.parametrize("seed", range(6))
def test_solver_schedules_start_every_task_on_time_in_the_simulator(seed):
    req, graph = random_instance(seed)
    out = solve_schedule(req, graph, FAST)
    assert isinstance(out, Schedule), out
    assert check_schedule(req, graph, out, FAST) == []
    assert_on_time_replay(req, graph, out)


def test_replan_from_frame_positions_starts_on_time_as_well():
    """Replanning mid-run: the remaining work is solved from where the frame has every robot
    (``robot_positions``) and from the current hour (``start_offset_h``); applied to the same
    simulator, the new schedule starts every task on time too."""
    req, graph = random_instance(1)
    first = solve_schedule(req, graph, FAST)
    assert isinstance(first, Schedule)
    sim = Simulator(req, graph, first)
    sim.run(stop_on_event=False, until_h=3.0)
    frame = sim.state()
    positions = {r.id: r.pos for r in frame.robots}
    assert any(tuple(p) != start_positions(req)[rid] for rid, p in positions.items())
    remaining = TaskGraph(tasks=[
        t.model_copy(update={"after": [a for a in t.after if a not in frame.done_tasks]})
        for t in graph.tasks if t.id not in frame.done_tasks])
    # a fresh plan for the remaining work from where everyone is now (nothing is locked)
    o = SolveOptions(time_limit_s=5.0, num_workers=4, start_offset_h=3,
                     robot_positions=positions)
    out = solve_schedule(req, remaining, o)
    assert isinstance(out, Schedule) and check_schedule(req, remaining, out, o) == []
    assert min(e.start_h for e in out.entries) >= 3
    sim.apply_new_plan(out, remaining)
    res = sim.run(stop_on_event=False)
    assert res.events == [] and res.completed
    assert res.finished_at_h == pytest.approx(out.makespan_h)
    for entry in out.entries:
        assert first_work_step(res.frames, entry.task) == pytest.approx(entry.start_h + DT_H)


def test_a_replan_at_a_fractional_hour_times_free_robots_from_when_they_are_free():
    """3k review: planner hour 0 of a replan is floor(t_h), but the simulator resumes at t_h,
    and an exact first leg timed from hour 0 arrived up to that fraction late (the transit
    allowance's slack used to absorb it). R2 fails at 2.5 h on T1; R1, idle at the depot, must
    redo it 9 cells away (0.75 h at 12 cells/h). Free at 0.5 in planner time, it arrives at
    1.25, so T1 is planned from hour 2, 4 h in simulator time, and starts then; from hour 0 it
    was planned for 3 h and started at 3.25 h."""
    site = Site(grid=(40, 40), depot=(20, 1), zones=[Zone(id="Z1", center=(20, 10))])
    req = Requirements(site=site, horizon_h=40, robots=[
        Robot(id="R1", type="excavator", battery_wh=1000, speed_cells_per_h=12.0),
        Robot(id="R2", type="excavator", battery_wh=1000, speed_cells_per_h=12.0)])
    graph = TaskGraph(tasks=[Task(id="T1", name="dig", robot_type="excavator", duration_h=4,
                                  energy_wh=100, zone="Z1")])
    schedule = Schedule(status="optimal", makespan_h=5,
                        entries=[ScheduleEntry(task="T1", robot="R2", start_h=1, end_h=5)])
    assert check_schedule(req, graph, schedule, SolveOptions()) == []
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    res = run_with_replanning(solver_only_planner(), plan, [FailureInjection("R2", 2.5)],
                              key="k", max_replans=1)
    [record] = res.replans
    frame = next(f for f in res.frames if f.t_h == pytest.approx(2.5))
    assert robot_ready_h(frame, record.event) == {"R1": 0.5}
    redo = record.after.schedule.entry("T1")
    assert (redo.robot, redo.start_h, redo.end_h) == ("R1", 4, 8)
    redone = next(f.t_h for f in res.frames if f.t_h > 2.5 and "T1" in f.active_tasks)
    assert redone == pytest.approx(redo.start_h + DT_H)   # on time: R1 was there by 3.25 h
    assert res.completed and res.finished_at_h == pytest.approx(record.after.schedule.makespan_h)
    # a whole hour needs no set-off hour, and a robot on a locked task never gets one
    assert robot_ready_h(frame.model_copy(update={"t_h": 3.0}), record.event) == {}


# --------------------------------------------------------------------------- anomalies stay
def test_task_overrun_is_now_a_genuine_anomaly():
    """Tasks that really run longer than planned still overrun (Monte Carlo jitter), while the
    plan as planned never does."""
    req, graph = random_instance(2)
    out = solve_schedule(req, graph, FAST)
    assert isinstance(out, Schedule)
    assert_on_time_replay(req, graph, out)
    longer = TaskGraph(tasks=[t.model_copy(update={"duration_h": t.duration_h + 3})
                              for t in graph.tasks])
    res = Simulator(req, longer, out).run(stop_on_event=False)
    overruns = [e for e in res.events if e.kind == "task_overrun"]
    assert overruns and {e.task for e in overruns} == set(graph.ids())
    assert all("(3 h over)" in (e.detail or "") for e in overruns)


def test_battery_depletion_still_stops_the_robot_and_the_checker_now_says_why():
    """The phase-2 scenario: 200 Wh of work in a 200 Wh budget on a hand-written schedule that
    ignores the 77 cells of travel. The simulator still strands the robot; the checker, which
    used to accept the schedule, now reports the leg it cannot afford or make in time."""
    req = Requirements(
        site=Site(grid=(40, 40), zones=[Zone(id="Z1", center=(2, 38)),
                                        Zone(id="Z2", center=(38, 38))]),
        robots=[Robot(id="R1", type="excavator", battery_wh=200, speed_cells_per_h=12)],
        horizon_h=50,
    )
    graph = TaskGraph(tasks=[
        Task(id="T1", name="a", robot_type="excavator", duration_h=4, energy_wh=100, zone="Z1"),
        Task(id="T2", name="b", robot_type="excavator", duration_h=4, energy_wh=100, zone="Z2",
             after=["T1"]),
    ])
    schedule = Schedule(entries=[ScheduleEntry(task="T1", robot="R1", start_h=0, end_h=4),
                                 ScheduleEntry(task="T2", robot="R1", start_h=4, end_h=8)],
                        makespan_h=8, status="optimal")
    assert check_schedule(req, graph, schedule) == ["energy:R1", "robot:R1"]
    res = Simulator(req, graph, schedule).run(stop_on_event=False)
    assert not res.completed and res.finished_at_h is None
    failure = next(e for e in res.events if e.kind == "robot_failure")
    assert (failure.robot, failure.task, failure.t_h) == ("R1", "T2", 13.0)
    assert failure.detail is not None and "ran out of battery" in failure.detail
    assert res.events[-1] is failure
    last = res.frames[-1].robots[0]
    assert last.status == "failed" and last.battery_wh == pytest.approx(5.18, abs=0.05)
    # and the solver's answer to the same brief is honest: the legs do not fit the battery
    verdict = solve_schedule(req, graph, FAST)
    assert not isinstance(verdict, Schedule) and verdict.conflicts == ["energy:R1"]
    assert verdict.hint == "R1 would need at least 239 Wh, 39 Wh of it travel, but has 200 Wh."
    assert math.isclose(distance((20, 1), (2, 38)) + 36, 77.146, abs_tol=0.001)
