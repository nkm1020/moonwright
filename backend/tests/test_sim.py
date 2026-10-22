"""Simulator (F7) and replanning (F8) tests with hand-written schedules and a fake planner.

Geometry used throughout: 40x40 grid, depot (20, 1), Z1 = (10, 30), Z2 = (30, 30) and Z3
auto-placed at (4, 36). Robots drive 60 cells/h, so every first leg takes three 0.25 h steps and
work starts at t = 0.75 h; T1/T2 (4 h) finish at 4.75 h and T3 (3 h, after both) at 7.75 h.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from moonwright.llm.base import FixtureClient
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.schedule import check_schedule
from moonwright.schemas import (
    Event,
    Failure,
    PlanResult,
    Requirements,
    Robot,
    RobotState,
    Schedule,
    ScheduleEntry,
    SimFrame,
    Site,
    Task,
    TaskGraph,
    Zone,
)
from moonwright.sim.replan import (
    build_replan_inputs,
    locked_entries,
    remaining_horizon_h,
    replan_after_event,
    replan_offset_h,
    run_with_replanning,
    shift_schedule,
)
from moonwright.sim.simulator import (
    FailureInjection,
    Simulator,
    depot_position,
    zone_positions,
)

SPEED = 60.0
DIST_DEPOT_Z1 = math.hypot(10 - 20, 30 - 1)   # 30.68 cells -> 15.34 Wh of travel
DIST_DEPOT_Z3 = math.hypot(4 - 20, 36 - 1)    # 38.48 cells


def make_req(*, speed: float = SPEED, ex1_battery: float = 5000.0,
             horizon: int = 100, extra_hauler: bool = False) -> Requirements:
    robots = [
        Robot(id="EX1", type="excavator", battery_wh=ex1_battery, speed_cells_per_h=speed),
        Robot(id="EX2", type="excavator", battery_wh=5000, speed_cells_per_h=speed),
        Robot(id="HL1", type="hauler", battery_wh=3000, speed_cells_per_h=speed),
    ]
    if extra_hauler:
        robots.append(Robot(id="HL2", type="hauler", battery_wh=3000, speed_cells_per_h=speed))
    return Requirements(
        site=Site(grid=(40, 40), zones=[Zone(id="Z1", center=(10, 30)),
                                        Zone(id="Z2", center=(30, 30))]),
        robots=robots,
        horizon_h=horizon,
    )


def make_graph(*, t2_zone: str = "Z2") -> TaskGraph:
    return TaskGraph(tasks=[
        Task(id="T1", name="excavate pad", robot_type="excavator", duration_h=4, energy_wh=800,
             zone="Z1"),
        Task(id="T2", name="excavate berm", robot_type="excavator", duration_h=4, energy_wh=800,
             zone=t2_zone),
        Task(id="T3", name="haul regolith", robot_type="hauler", duration_h=3, energy_wh=600,
             zone="Z3", after=["T1", "T2"]),
    ])


def stretched_graph(extra_h: int = 3) -> TaskGraph:
    """The graph with every task ``extra_h`` longer than its schedule entry allots: what Monte
    Carlo jitter hands the simulator, and the one thing that makes a task overrun."""
    return TaskGraph(tasks=[t.model_copy(update={"duration_h": t.duration_h + extra_h})
                            for t in make_graph().tasks])


def make_schedule() -> Schedule:
    return Schedule(entries=[
        ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=4),
        ScheduleEntry(task="T2", robot="EX2", start_h=0, end_h=4),
        ScheduleEntry(task="T3", robot="HL1", start_h=4, end_h=7),
    ], makespan_h=7, status="optimal")


def robot(frame, robot_id):
    return next(r for r in frame.robots if r.id == robot_id)


def assert_strictly_increasing(times):
    assert all(b > a for a, b in pairwise(times)), times


def greedy_schedule(req: Requirements, graph: TaskGraph, locked: list[ScheduleEntry],
                    offset: int) -> Schedule:
    """List scheduler for the fake planner: tasks in graph order, earliest free capable robot."""
    free = {r.id: offset for r in req.robots}
    end: dict[str, int] = {}
    pinned = {e.task: e for e in locked}
    entries = []
    for task in graph.tasks:
        preds_end = max((end[a] for a in task.after if a in end), default=offset)
        if task.id in pinned:
            rid, start = pinned[task.id].robot, pinned[task.id].start_h
        else:
            rid = min((r.id for r in req.robots if r.type == task.robot_type),
                      key=lambda r: free[r])
            start = max(free[rid], preds_end)
        stop = start + task.duration_h
        entries.append(ScheduleEntry(task=task.id, robot=rid, start_h=start, end_h=stop))
        free[rid] = stop
        end[task.id] = stop
    return Schedule(entries=entries, makespan_h=max(e.end_h for e in entries), status="feasible")


class FakePlanner:
    """Stands in for moonwright.pipeline.loop.Planner; records every call it receives."""

    def __init__(self, *, succeed: bool = True):
        self.succeed = succeed
        self.calls: list[dict] = []

    def plan_from_requirements(self, req, *, key, prior_graph=None, context=None, variant=None,
                               start_offset_h=0, locked=None, max_repairs=None,
                               required_tasks=None):
        locked = list(locked or [])
        self.calls.append({"req": req, "key": key, "prior_graph": prior_graph,
                           "context": context, "variant": variant,
                           "start_offset_h": start_offset_h, "locked": locked,
                           "required_tasks": list(required_tasks or [])})
        if not self.succeed:
            return PlanResult(requirements=req, task_graph=prior_graph, success=False,
                              failure=Failure(kind="infeasible", conflicts=["horizon"]))
        types = {r.type for r in req.robots}
        unserved = [t.id for t in prior_graph.tasks if t.robot_type not in types]
        if unserved:  # what the validator would say: no robot of that type is left
            return PlanResult(requirements=req, task_graph=prior_graph, success=False,
                              failure=Failure(kind="validation",
                                              conflicts=[f"capability:{t}" for t in unserved]))
        schedule = greedy_schedule(req, prior_graph, locked, start_offset_h)
        return PlanResult(requirements=req, task_graph=prior_graph, schedule=schedule,
                          success=True)


class DroppingPlanner(FakePlanner):
    """A planner whose "LLM" loses one remaining task (and the edges into it)."""

    def __init__(self, drop: str):
        super().__init__()
        self.drop = drop

    def plan_from_requirements(self, req, *, prior_graph=None, locked=None, **kw):
        kept = [t.model_copy(update={"after": [a for a in t.after if a != self.drop]})
                for t in prior_graph.tasks if t.id != self.drop]
        return super().plan_from_requirements(
            req, prior_graph=TaskGraph(tasks=kept),
            locked=[e for e in (locked or []) if e.task != self.drop], **kw)


class MovingPlanner(FakePlanner):
    """A planner that keeps every task but restarts the running one an hour later."""

    def plan_from_requirements(self, req, *, locked=None, **kw):
        delayed = [e.model_copy(update={"start_h": e.start_h + 1}) for e in (locked or [])]
        return super().plan_from_requirements(req, locked=delayed, **kw)


class ShrinkingPlanner(FakePlanner):
    """A planner whose "LLM" keeps every id but makes the lost task a 1 h / 1 Wh stub."""

    def plan_from_requirements(self, req, *, prior_graph=None, **kw):
        tasks = [t.model_copy(update={"duration_h": 1, "energy_wh": 1}) if t.id == "T2" else t
                 for t in prior_graph.tasks]
        return super().plan_from_requirements(req, prior_graph=TaskGraph(tasks=tasks), **kw)


# --------------------------------------------------------------------------- layout
def test_zone_positions_keeps_site_zones_and_places_missing_ones_deterministically():
    req, graph = make_req(), make_graph()
    layout = zone_positions(req, graph)
    assert layout["Z1"] == (10, 30) and layout["Z2"] == (30, 30)
    assert layout["Z3"] == (4, 36)
    assert zone_positions(req, graph) == layout
    assert depot_position(req.site) == (20, 1)
    assert depot_position(Site(depot=(3, 3))) == (3, 3)

    many = TaskGraph(tasks=[Task(id=f"T{i}", name="t", robot_type="excavator", duration_h=1,
                                 energy_wh=1, zone=f"Z{i:02d}") for i in range(1, 21)])
    big = zone_positions(req, many)
    cells = [big[f"Z{i:02d}"] for i in range(1, 21)]
    assert len(set(cells)) == 20
    assert all(0 <= x < 40 and 0 <= y < 40 for x, y in cells)
    assert all(math.dist(c, (20, 1)) >= 4 for c in cells)
    assert not {(10, 30), (30, 30)} & set(cells)
    assert big["Z1"] == (10, 30)


# --------------------------------------------------------------------------- plain run
def test_completes_three_task_plan():
    sim = Simulator(make_req(), make_graph(), make_schedule())
    res = sim.run(stop_on_event=False)

    assert res.completed and res.finished_at_h == pytest.approx(7.75)
    assert res.events == []
    times = [f.t_h for f in res.frames]
    assert times[0] == 0.0 and times[-1] == pytest.approx(7.75)
    assert all(b - a == pytest.approx(0.25) for a, b in pairwise(times))
    last = res.frames[-1]
    assert last.done_tasks == ["T1", "T2", "T3"]
    assert last.active_tasks == []
    assert all(r.status == "done" for r in last.robots)
    assert last.progress == {"T1": 1.0, "T2": 1.0, "T3": 1.0}
    assert robot(last, "EX1").battery_wh == pytest.approx(5000 - 800 - 0.5 * DIST_DEPOT_Z1,
                                                          abs=0.05)
    assert robot(last, "HL1").battery_wh == pytest.approx(3000 - 600 - 0.5 * DIST_DEPOT_Z3,
                                                          abs=0.05)
    assert tuple(robot(last, "HL1").pos) == (4.0, 36.0)
    assert res.schedule is sim.schedule and res.task_graph is sim.task_graph


def test_precedence_and_scheduled_start_are_respected():
    sim = Simulator(make_req(), make_graph(), make_schedule())
    res = sim.run(stop_on_event=False)
    for frame in res.frames:
        if "T3" in frame.active_tasks:
            assert {"T1", "T2"} <= set(frame.done_tasks)
            assert frame.t_h >= 4.0
    # T3 starts the step after both predecessors finished (4.75 h): worked from 4.75 to 7.75.
    first_active = next(f.t_h for f in res.frames if "T3" in f.active_tasks)
    assert first_active == pytest.approx(5.0)
    # HL1 pre-positions at Z3 and waits there idle until its predecessors are done.
    waiting = [f for f in res.frames if 1.0 <= f.t_h <= 4.5]
    assert all(robot(f, "HL1").status == "idle" and tuple(robot(f, "HL1").pos) == (4.0, 36.0)
               for f in waiting)


def test_two_robots_never_work_in_the_same_zone_at_once():
    # T1 and T2 are both put in Z1 for 0-4 h: EX2 must wait until EX1 is done.
    sim = Simulator(make_req(), make_graph(t2_zone="Z1"), make_schedule())
    res = sim.run(stop_on_event=False)
    graph = make_graph(t2_zone="Z1")
    for frame in res.frames:
        zones = [graph.task(r.current_task).zone for r in frame.robots if r.status == "working"]
        assert len(zones) == len(set(zones)), f"zone shared at t={frame.t_h}"
    waited = [f for f in res.frames if robot(f, "EX2").status == "idle"
              and robot(f, "EX1").status == "working"]
    assert waited and all(tuple(robot(f, "EX2").pos) == (10.0, 30.0) for f in waited)
    assert res.completed
    # T2 ran 4.75-8.75 h against a 4 h scheduled end and pushed T3 to 8.75-11.75 h, but each
    # took exactly the hours its entry allots: waiting for the zone is drift, not an overrun
    assert res.events == [] and res.finished_at_h == pytest.approx(11.75)


def test_until_h_caps_the_clock_and_run_resumes_without_duplicate_frames():
    sim = Simulator(make_req(), make_graph(), make_schedule())
    first = sim.run(stop_on_event=False, until_h=2.0)
    assert not first.completed and first.finished_at_h is None
    assert first.frames[-1].t_h == pytest.approx(2.0)
    second = sim.run(stop_on_event=False)
    assert second.completed
    assert_strictly_increasing([f.t_h for f in first.frames + second.frames])
    assert second.frames[0].t_h == pytest.approx(2.25)


def test_simulator_rejects_inconsistent_inputs():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    with pytest.raises(ValueError):
        Simulator(req, graph, schedule, injections=[FailureInjection("NOPE", 1.0)])
    for bad_time in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite"):
            Simulator(req, graph, schedule, injections=[FailureInjection("EX2", bad_time)])
    with pytest.raises(ValueError):
        Simulator(req, graph, schedule, dt_h=0)
    bad_robot = Schedule(entries=[ScheduleEntry(task="T1", robot="ZZ9", start_h=0, end_h=4)],
                         makespan_h=4, status="feasible")
    with pytest.raises(ValueError):
        Simulator(req, graph, bad_robot)
    twice = Schedule(entries=[ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=4),
                              ScheduleEntry(task="T1", robot="EX2", start_h=0, end_h=4)],
                     makespan_h=4, status="feasible")
    with pytest.raises(ValueError):
        Simulator(req, graph, twice)


# --------------------------------------------------------------------------- events
def test_robot_failure_injection_stops_the_robot_and_fires_once():
    sim = Simulator(make_req(), make_graph(), make_schedule(),
                    injections=[FailureInjection("EX2", 2.0)])
    res = sim.run(stop_on_event=True)

    assert [(e.kind, e.robot, e.task, e.t_h) for e in res.events] == \
        [("robot_failure", "EX2", "T2", 2.0)]
    assert not res.completed
    frame = sim.state()
    assert frame.t_h == pytest.approx(2.0)
    ex2 = robot(frame, "EX2")
    assert ex2.status == "failed" and ex2.current_task == "T2"
    assert frame.progress["T2"] == pytest.approx(0.3125)   # worked 0.75 -> 2.0 h of 4 h
    assert frame.active_tasks == ["T1"]

    rest = sim.run(stop_on_event=False)
    assert rest.events == []                                 # the injection fires only once
    assert not rest.completed
    last = rest.frames[-1]
    assert last.done_tasks == ["T1"]
    assert last.progress["T2"] == pytest.approx(0.3125)      # progress kept, robot stays failed
    assert all(robot(f, "EX2").status == "failed" for f in rest.frames)
    # T3 can never start, so the run stops as soon as nothing can make progress.
    assert last.t_h == pytest.approx(5.0)
    assert sim.events == res.events


def test_injection_at_time_zero_fires_before_the_first_frame():
    sim = Simulator(make_req(), make_graph(), make_schedule(),
                    injections=[FailureInjection("EX2", 0.0)])
    res = sim.run(stop_on_event=True)
    assert [(e.kind, e.robot, e.task, e.t_h) for e in res.events] == \
        [("robot_failure", "EX2", None, 0.0)]
    assert [f.t_h for f in res.frames] == [0.0]                # nothing happened yet
    assert robot(res.frames[0], "EX2").status == "failed"
    assert not res.completed and sim.t_h == 0.0

    rest = sim.run(stop_on_event=False)
    assert rest.events == [] and rest.frames[0].t_h == pytest.approx(0.25)
    assert all(robot(f, "EX2").status == "failed" for f in rest.frames)
    assert not rest.completed and rest.frames[-1].done_tasks == ["T1"]   # T3 waits on T2 forever


def test_run_with_replanning_replans_a_failure_at_time_zero():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("EX2", 0.0)], key="k")

    assert [(e.kind, e.robot, e.t_h) for e in res.events] == [("robot_failure", "EX2", 0.0)]
    assert len(res.replans) == 1 and res.replans[0].t_h == 0.0 and res.replans[0].after.success
    call = planner.calls[0]
    assert [r.id for r in call["req"].robots] == ["EX1", "HL1"]
    assert call["req"].horizon_h == 100 and call["locked"] == []  # nothing had started
    assert call["prior_graph"].task("T2").duration_h == 4         # T2 re-added in full
    assert res.completed and res.schedule.entry("T2").robot == "EX1"
    times = [f.t_h for f in res.frames]
    assert times[0] == 0.0 and robot(res.frames[0], "EX2").status == "failed"
    assert_strictly_increasing(times)
    assert res.frames[-1].done_tasks == ["T1", "T2", "T3"]


def test_battery_low_fires_once_while_work_remains():
    # 830 Wh budget, 10 % reserve = 83 Wh: EX1 drops below it on its 15th work step (t = 4.5 h).
    sim = Simulator(make_req(ex1_battery=830), make_graph(), make_schedule())
    res = sim.run(stop_on_event=True)
    assert [(e.kind, e.robot, e.task, e.t_h) for e in res.events] == \
        [("battery_low", "EX1", "T1", 4.5)]
    assert "T1" in sim.state().active_tasks
    rest = sim.run(stop_on_event=False)
    assert rest.completed and rest.events == []
    ex1 = robot(rest.frames[-1], "EX1")
    assert ex1.battery_wh == pytest.approx(830 - 800 - 0.5 * DIST_DEPOT_Z1, abs=0.05)


def test_task_overrun_fires_once_per_late_task():
    # every task takes 3 h longer than its entry allots (the Monte Carlo jitter case): T1 and
    # T2 work 0.75-7.75 h against 4 h scheduled, T3 (waiting at Z3) then 7.75-13.75 h
    # against 3 h
    sim = Simulator(make_req(), stretched_graph(3), make_schedule())
    res = sim.run(stop_on_event=True)
    assert {(e.kind, e.task, e.t_h) for e in res.events} == \
        {("task_overrun", "T1", 7.75), ("task_overrun", "T2", 7.75)}
    assert res.events[0].detail is not None
    assert "took 7 h against the 4 h scheduled" in res.events[0].detail
    assert "(3 h over)" in res.events[0].detail
    rest = sim.run(stop_on_event=False)
    assert rest.completed
    assert [(e.kind, e.task, e.t_h) for e in rest.events] == [("task_overrun", "T3", 13.75)]
    assert len({e.task for e in sim.events}) == len(sim.events) == 3


def test_upstream_drift_is_not_an_overrun():
    # 6 cells/h: 5.25 h of travel, so T1 and T2 end at 9.25 h against a 4 h scheduled end and
    # T3 at 12.25 h against 7 h; every task still took exactly its scheduled hours, so a
    # verified plan replayed as planned raises nothing (travel is not in the solver's model)
    res = Simulator(make_req(speed=6.0), make_graph(), make_schedule()).run(stop_on_event=False)
    assert res.completed and res.finished_at_h == pytest.approx(12.25)
    assert res.events == []


# --------------------------------------------------------------------------- replan inputs
def failed_mid_run():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    sim = Simulator(req, graph, schedule, injections=[FailureInjection("EX2", 2.0)])
    res = sim.run(stop_on_event=True)
    return req, graph, schedule, sim, sim.state(), res.events[0]


def test_build_replan_inputs_drops_failed_robot_and_shrinks_the_work():
    req, graph, schedule, _, frame, event = failed_mid_run()
    req2, graph2, context = build_replan_inputs(req, graph, schedule, frame, event)

    assert [r.id for r in req2.robots] == ["EX1", "HL1"]
    assert req2.robot("EX1").battery_wh == pytest.approx(5000 - 0.5 * DIST_DEPOT_Z1 - 250, abs=0.05)
    assert req2.robot("HL1").battery_wh == pytest.approx(3000 - 0.5 * DIST_DEPOT_Z3, abs=0.05)
    assert req2.horizon_h == 98 and req.horizon_h == 100       # original untouched
    assert graph2.ids() == ["T1", "T2", "T3"]
    t1 = graph2.task("T1")                                      # underway on EX1: 2.75 h -> 3 h
    assert t1.duration_h == 3 and t1.energy_wh == pytest.approx(550)
    t2 = graph2.task("T2")                                      # was on EX2: re-added in full
    assert t2.duration_h == 4 and t2.energy_wh == 800
    assert graph2.task("T3").after == ["T1", "T2"]
    assert locked_entries(graph2, frame) == \
        [ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=3)]
    assert "EX2" in context and "T2" in context and "98 h remain" in context
    assert "T1 (underway on EX1, 3 h left" in context


def test_build_replan_inputs_removes_done_tasks_and_their_edges():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    sim = Simulator(req, graph, schedule, injections=[FailureInjection("HL1", 6.0)])
    res = sim.run(stop_on_event=True)
    frame, event = sim.state(), res.events[0]
    assert frame.done_tasks == ["T1", "T2"] and event.task == "T3"

    req2, graph2, context = build_replan_inputs(req, graph, schedule, frame, event)
    assert [r.id for r in req2.robots] == ["EX1", "EX2"]
    assert req2.horizon_h == 94
    assert graph2.ids() == ["T3"]
    assert graph2.task("T3").after == []
    assert graph2.task("T3").duration_h == 3 and graph2.task("T3").energy_wh == 600
    assert locked_entries(graph2, frame) == []
    assert "T1, T2" in context and "HL1" in context


def test_failed_robot_is_never_locked_even_when_the_frame_lags_the_event():
    """A frame that still shows the failing robot as working (the event is what says it
    failed): build_replan_inputs and locked_entries must agree, so T2 is re-added in full and
    only EX1's task is pinned."""
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    frame = SimFrame(
        t_h=2.0,
        robots=[RobotState(id="EX1", pos=(10, 30), battery_wh=4700, status="working",
                           current_task="T1"),
                RobotState(id="EX2", pos=(30, 30), battery_wh=4700, status="working",
                           current_task="T2"),
                RobotState(id="HL1", pos=(4, 36), battery_wh=2980, status="idle",
                           current_task="T3")],
        active_tasks=["T1", "T2"], progress={"T1": 0.3125, "T2": 0.3125},
    )
    event = Event(t_h=2.0, kind="robot_failure", robot="EX2", task="T2")

    req2, graph2, context = build_replan_inputs(req, graph, schedule, frame, event)
    assert [r.id for r in req2.robots] == ["EX1", "HL1"]
    assert (graph2.task("T2").duration_h, graph2.task("T2").energy_wh) == (4, 800)
    assert graph2.task("T1").duration_h == 3
    assert locked_entries(graph2, frame, event) == \
        [ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=3)]
    assert "EX2" in context and "must not be used again" in context

    record = replan_after_event(FakePlanner(), req, graph, schedule, frame, event, key="k")
    assert record.after.success and record.after.schedule is not None
    assert all(e.robot != "EX2" for e in record.after.schedule.entries)
    assert record.after.schedule.entry("T1").robot == "EX1"


def test_replan_after_event_shifts_the_schedule_to_absolute_time():
    req, graph, schedule, _, frame, event = failed_mid_run()
    planner = FakePlanner()
    record = replan_after_event(planner, req, graph, schedule, frame, event, key="brief-1")

    call = planner.calls[0]
    assert call["key"] == "brief-1" and call["variant"] == "replan"
    assert call["start_offset_h"] == 0 and call["required_tasks"] == ["T1", "T2", "T3"]
    assert call["locked"] == [ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=3)]
    assert call["prior_graph"].ids() == ["T1", "T2", "T3"]
    assert record.context == call["context"]
    assert record.before is schedule and record.remaining_tasks == ["T1", "T2", "T3"]
    assert record.t_h == 2.0 and record.event is event
    after = record.after.schedule
    assert replan_offset_h(frame.t_h) == 2
    assert (after.entry("T1").start_h, after.entry("T1").end_h) == (2, 5)
    t2 = after.entry("T2")
    assert (t2.robot, t2.start_h, t2.end_h) == ("EX1", 5, 9)
    assert (after.entry("T3").start_h, after.entry("T3").end_h) == (9, 12)
    assert after.makespan_h == 12


def test_shift_schedule_and_offset_helpers():
    schedule = make_schedule()
    assert shift_schedule(schedule, 0) is schedule
    assert replan_offset_h(2.75) == 2 and replan_offset_h(0.0) == 0
    bounded = schedule.model_copy(update={"objective_bound_h": 5})
    moved = shift_schedule(bounded, 3)
    assert [(e.start_h, e.end_h) for e in moved.entries] == [(3, 7), (3, 7), (7, 10)]
    assert moved.makespan_h == 10 and moved.objective_bound_h == 8
    assert shift_schedule(schedule, 3).objective_bound_h is None
    assert schedule.makespan_h == 7                             # input not mutated


# --------------------------------------------------------------------------- replanning loop
def test_apply_new_plan_continues_the_running_task_and_restarts_the_lost_one():
    req, graph, schedule, sim, frame, event = failed_mid_run()
    _, graph2, _ = build_replan_inputs(req, graph, schedule, frame, event)
    new_schedule = Schedule(entries=[
        ScheduleEntry(task="T1", robot="EX1", start_h=2, end_h=5),
        ScheduleEntry(task="T2", robot="EX1", start_h=5, end_h=9),
        ScheduleEntry(task="T3", robot="HL1", start_h=9, end_h=12),
    ], makespan_h=12, status="feasible")
    sim.apply_new_plan(new_schedule, graph2)

    now = sim.state()
    assert now.progress["T1"] == pytest.approx(0.3125) and "T2" not in now.progress
    assert robot(now, "EX1").status == "working" and robot(now, "EX1").current_task == "T1"
    assert robot(now, "EX2").status == "failed" and robot(now, "EX2").current_task is None
    res = sim.run(stop_on_event=False)
    assert res.completed and res.finished_at_h == pytest.approx(12.5)
    assert res.events == []
    t1_progress = [f.progress.get("T1", 0.0) for f in res.frames]
    assert all(b >= a for a, b in pairwise(t1_progress))
    assert res.frames[-1].done_tasks == ["T1", "T2", "T3"]
    assert all(robot(f, "EX2").status == "failed" for f in res.frames)


def test_run_with_replanning_recovers_from_a_robot_failure():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("EX2", 2.0)], key="k",
                              max_replans=2)

    assert res.completed and res.finished_at_h == pytest.approx(12.5)
    assert [e.kind for e in res.events] == ["robot_failure"]
    assert len(res.replans) == 1 and len(planner.calls) == 1
    record = res.replans[0]
    assert record.event.robot == "EX2" and record.t_h == 2.0 and record.after.success
    assert res.schedule is record.after.schedule
    assert res.schedule.entry("T2").robot == "EX1"
    assert res.task_graph is record.after.task_graph
    times = [f.t_h for f in res.frames]
    assert times[0] == 0.0 and times[-1] == pytest.approx(12.5)
    assert_strictly_increasing(times)
    after_replan = next(f for f in res.frames if f.t_h == pytest.approx(2.25))
    assert after_replan.progress["T1"] > 0.3125 and "T2" not in after_replan.progress
    assert robot(after_replan, "EX2").status == "failed"
    assert all(robot(f, "EX2").status == "failed" for f in res.frames if f.t_h >= 2.0)
    assert res.frames[-1].done_tasks == ["T1", "T2", "T3"]


def test_run_with_replanning_keeps_the_old_plan_when_the_replan_fails():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    res = run_with_replanning(FakePlanner(succeed=False), plan, [FailureInjection("EX2", 2.0)],
                              key="k")
    assert not res.completed and res.finished_at_h is None
    assert len(res.replans) == 1 and not res.replans[0].after.success
    assert res.schedule is schedule and res.task_graph is graph
    assert [e.kind for e in res.events] == ["robot_failure"]
    assert res.frames[-1].done_tasks == ["T1"]
    assert res.frames[-1].t_h == pytest.approx(5.0)            # stuck on T3 -> stops early


def test_run_with_replanning_without_budget_runs_on_to_the_end():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("HL1", 6.0)], key="k",
                              max_replans=0)
    assert planner.calls == [] and res.replans == []
    assert [e.kind for e in res.events] == ["robot_failure"]
    assert not res.completed and res.frames[-1].done_tasks == ["T1", "T2"]
    assert_strictly_increasing([f.t_h for f in res.frames])


def test_run_with_replanning_honours_dt_h():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    res = run_with_replanning(FakePlanner(), plan, [], key="k", dt_h=0.5)
    assert res.completed and res.events == [] and res.replans == []
    times = [f.t_h for f in res.frames]
    assert times[0] == 0.0 and all(b - a == pytest.approx(0.5) for a, b in pairwise(times))
    # the 0.51 h first leg now costs two 0.5 h steps: work starts at 1.0 h instead of 0.75 h,
    # T1/T2 end at 5.0 h and T3 runs 5.0-8.0 h
    assert res.finished_at_h == pytest.approx(8.0)


def test_run_with_replanning_rejects_an_unverified_plan():
    plan = PlanResult(requirements=make_req(), task_graph=make_graph(), success=False)
    with pytest.raises(ValueError):
        run_with_replanning(FakePlanner(), plan, [], key="k")


# --------------------------------------------------------------------------- replan semantics
def test_remaining_horizon_rounds_the_elapsed_time_up():
    assert remaining_horizon_h(100, 2.0) == 98
    assert remaining_horizon_h(100, 2.25) == 97
    assert remaining_horizon_h(8, 2.5) == 5
    assert remaining_horizon_h(3, 2.5) == 1                     # floor 1
    assert remaining_horizon_h(100, 0.0) == 100


def test_build_replan_inputs_at_a_fractional_time_shrinks_the_horizon_by_the_ceiling():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    sim = Simulator(req, graph, schedule, injections=[FailureInjection("EX2", 2.25)])
    res = sim.run(stop_on_event=True)
    req2, _, context = build_replan_inputs(req, graph, schedule, sim.state(), res.events[0])
    assert req2.horizon_h == 97 and "97 h remain" in context


def _deadline_scenario(horizon: int):
    """R1 works T1 (4 h) then T2 (4 h) in a zone at the depot, so the baseline ends exactly at
    the horizon; R2, which still has T4 queued, fails at 2.5 h. Planned from floor(2.5) = 2 but
    resumed at 2.5, so the replan may only use horizon - ceil(2.5) hours."""
    req = Requirements(
        site=Site(grid=(40, 40), depot=(5, 5), zones=[Zone(id="Z1", center=(5, 5)),
                                                      Zone(id="Z2", center=(6, 5))]),
        robots=[Robot(id="R1", type="x", battery_wh=1000, speed_cells_per_h=100),
                Robot(id="R2", type="y", battery_wh=1000, speed_cells_per_h=100),
                Robot(id="R3", type="y", battery_wh=1000, speed_cells_per_h=100)],
        horizon_h=horizon,
    )
    graph = TaskGraph(tasks=[
        Task(id="T1", name="a", robot_type="x", duration_h=4, energy_wh=10, zone="Z1"),
        Task(id="T2", name="b", robot_type="x", duration_h=4, energy_wh=10, zone="Z1",
             after=["T1"]),
        Task(id="T3", name="c", robot_type="y", duration_h=1, energy_wh=10, zone="Z2"),
        Task(id="T4", name="d", robot_type="y", duration_h=1, energy_wh=10, zone="Z2",
             after=["T3"]),
    ])
    schedule = Schedule(entries=[
        ScheduleEntry(task="T1", robot="R1", start_h=0, end_h=4),
        ScheduleEntry(task="T2", robot="R1", start_h=4, end_h=8),
        ScheduleEntry(task="T3", robot="R2", start_h=0, end_h=1),
        ScheduleEntry(task="T4", robot="R2", start_h=6, end_h=7),
    ], makespan_h=8, status="optimal")
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = Planner(FixtureClient(), explain=False)  # offline: adapts the remaining graph as-is
    return req, run_with_replanning(planner, plan, [FailureInjection("R2", 2.5)], key="nokey")


def test_verified_replan_never_overshoots_the_deadline_in_simulator_time():
    # 8 h horizon: 2 + 4 h of remaining work do not fit the 5 h left, so the replan is refused
    # by the validator and the old plan, which still ends on time, stays in force.
    req, res = _deadline_scenario(horizon=8)
    record = res.replans[0]
    assert not record.after.success and record.after.requirements.horizon_h == 5
    assert record.after.failure is not None
    assert record.after.failure.conflicts == ["horizon_path:T1->T2", "horizon"]
    # the old plan stays in force: R1's chain still ends on the deadline, R2's T4 is lost
    assert not res.completed and "T4" not in res.frames[-1].done_tasks
    t2_done = next(f.t_h for f in res.frames if "T2" in f.done_tasks)
    assert t2_done == pytest.approx(8.0) and t2_done <= req.horizon_h

    # 9 h horizon: the replan fits the 6 h left, and ends at 8.5 h in simulator time.
    req, res = _deadline_scenario(horizon=9)
    record = res.replans[0]
    after = record.after
    assert after.success and after.requirements.horizon_h == 6 and after.schedule is not None
    relative = shift_schedule(after.schedule, -replan_offset_h(record.t_h))
    assert check_schedule(after.requirements, after.task_graph, relative) == []
    assert relative.makespan_h == 6
    assert after.task_graph.task("T1").duration_h == 2                 # 1.5 h truly left
    assert res.completed and res.finished_at_h == pytest.approx(8.5)
    assert res.finished_at_h <= req.horizon_h


def test_remaining_work_is_derived_from_the_original_task_across_replans():
    """T1 (4 h, 800 Wh) keeps running on EX1 through two replans (EX2 fails at 2 h, then HL1,
    which holds T3, at 2.5 h). The second replan must size the remainder from the original
    definition with the overall progress, not shrink the already-reduced 3 h copy again."""
    req, graph, schedule = make_req(extra_hauler=True), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("EX2", 2.0),
                                              FailureInjection("HL1", 2.5)], key="k")

    assert [(r.t_h, r.event.robot) for r in res.replans] == [(2.0, "EX2"), (2.5, "HL1")]
    first, second = (c["prior_graph"].task("T1") for c in planner.calls)
    assert (first.duration_h, first.energy_wh) == (3, pytest.approx(550))   # 2.75 h, 550 Wh left
    progress = 0.3125 + 0.6875 * 0.5 / 3                     # 0.5 h worked on the 3 h copy
    assert second.duration_h == 3                            # 2.29 h left, not ceil(3 * 0.57) = 2
    # frame.progress is rounded to 4 decimals, hence the 0.04 Wh tolerance; the buggy value
    # would have been 550 * (1 - progress) = 315 Wh
    assert second.energy_wh == pytest.approx(800 * (1 - progress), abs=0.05)
    assert planner.calls[1]["locked"] == [ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=3)]
    assert res.completed
    t1_done = next(f.t_h for f in res.frames if "T1" in f.done_tasks)
    assert t1_done == pytest.approx(5.5)                     # worked 1.25 + 0.5 + 3 h >= 4 h
    # EX1 drew exactly T1's 800 Wh plus T2's 800 Wh plus its travel
    ex1 = robot(res.frames[-1], "EX1")
    assert ex1.battery_wh == pytest.approx(5000 - 1600 - 0.5 * (DIST_DEPOT_Z1 + 20), abs=0.05)


def test_lost_task_is_re_added_with_its_original_definition_after_an_earlier_replan():
    """EX2 fails at 2 h (T2 lost), then EX1 fails at 2.5 h while continuing the shortened T1:
    T1 must come back in full (4 h, 800 Wh), not as the 3 h copy of the first replan."""
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("EX2", 2.0),
                                              FailureInjection("EX1", 2.5)], key="k")
    assert [r.event.robot for r in res.replans] == ["EX2", "EX1"]
    second = planner.calls[1]
    t1 = second["prior_graph"].task("T1")
    assert (t1.duration_h, t1.energy_wh) == (4, 800) and second["locked"] == []
    assert [r.id for r in second["req"].robots] == ["HL1"]  # no excavator left: honest failure
    assert not res.replans[1].after.success and not res.completed


def test_replan_that_drops_a_remaining_task_is_refused_and_the_old_plan_stays():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = DroppingPlanner("T2")                 # EX2's lost task vanishes from the answer
    res = run_with_replanning(planner, plan, [FailureInjection("EX2", 2.0)], key="k")

    record = res.replans[0]
    assert planner.calls[0]["required_tasks"] == ["T1", "T2", "T3"]
    assert not record.after.success and record.after.schedule is None
    failure = record.after.failure
    assert failure is not None and failure.kind == "validation"
    assert failure.conflicts == ["missing:T2"] and "dropped T2" in failure.hint
    assert record.after.steps[-1].stage == "replan" and not record.after.steps[-1].ok
    assert record.remaining_tasks == ["T1", "T2", "T3"]
    assert res.schedule is schedule and res.task_graph is graph
    assert not res.completed and "T2" not in res.frames[-1].done_tasks


def test_replan_that_redefines_a_remaining_task_is_refused():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    res = run_with_replanning(ShrinkingPlanner(), plan, [FailureInjection("EX2", 2.0)], key="k")
    after = res.replans[0].after
    assert not after.success and after.schedule is None and after.failure is not None
    assert after.failure.kind == "validation" and after.failure.conflicts == ["changed:T2"]
    assert "redefined the remaining task(s) T2" in after.failure.hint
    assert after.failure.details == ["T2 changed: duration_h 1 (needs 4); energy_wh 1 (needs 800)"]
    assert res.schedule is schedule and not res.completed


def test_failure_of_a_robot_without_work_does_not_replan():
    """EX2 finishes T2 at 4.75 h; its failure at 6 h takes no work away, so the event is
    recorded and the run continues on the plan in force instead of spending a replan."""
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("EX2", 6.0)], key="k")
    assert planner.calls == [] and res.replans == []
    assert [(e.kind, e.robot, e.task) for e in res.events] == [("robot_failure", "EX2", None)]
    assert res.completed and res.finished_at_h == pytest.approx(7.75)
    assert all(robot(f, "EX2").status == "failed" for f in res.frames if f.t_h >= 6.0)

    # a robot that is waiting at its zone for a task it still owns does trigger one
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("HL1", 3.0)], key="k")
    assert len(planner.calls) == len(res.replans) == 1
    assert (res.replans[0].event.robot, res.replans[0].event.task) == ("HL1", "T3")


def test_replan_that_moves_a_running_task_is_refused():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    res = run_with_replanning(MovingPlanner(), plan, [FailureInjection("EX2", 2.0)], key="k")
    after = res.replans[0].after
    assert not after.success and after.failure is not None
    assert after.failure.conflicts == [] and "moved the running task(s) T1" in after.failure.hint
    assert res.schedule is schedule


def test_simulator_completion_covers_every_task_ever_planned():
    req, graph, schedule = make_req(), make_graph(), make_schedule()
    sim = Simulator(req, graph, schedule)
    sim.run(stop_on_event=False, until_h=1.0)
    without_t2 = TaskGraph(tasks=[
        t.model_copy(update={"after": [a for a in t.after if a != "T2"]})
        for t in graph.tasks if t.id != "T2"
    ])
    sim.apply_new_plan(Schedule(entries=[
        ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=4),
        ScheduleEntry(task="T3", robot="HL1", start_h=4, end_h=7),
    ], makespan_h=7, status="feasible"), without_t2)
    res = sim.run(stop_on_event=False)
    assert res.frames[-1].done_tasks == ["T1", "T3"]
    assert not res.completed and res.finished_at_h is None   # T2 was planned once: still owed


def test_task_overrun_does_not_spend_the_replan_budget():
    # every task runs 3 h over its entry (see test_task_overrun_fires_once_per_late_task)
    req, graph, schedule = make_req(), stretched_graph(3), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [], key="k", max_replans=2)
    assert res.completed and planner.calls == [] and res.replans == []
    assert [e.kind for e in res.events] == ["task_overrun"] * 3

    # the trigger set is a parameter: opting in makes the first overrun replan
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [], key="k", max_replans=2,
                              replan_on=("robot_failure", "task_overrun"))
    assert res.completed and len(planner.calls) == len(res.replans) == 1
    assert res.replans[0].event.kind == "task_overrun"


def test_each_replan_of_a_run_gets_its_own_fixture_variant():
    req, graph, schedule = make_req(extra_hauler=True), make_graph(), make_schedule()
    plan = PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True)
    planner = FakePlanner()
    res = run_with_replanning(planner, plan, [FailureInjection("EX2", 2.0),
                                              FailureInjection("HL1", 6.0)], key="k")
    assert [c["variant"] for c in planner.calls] == ["replan", "replan2"]
    assert [r.event.robot for r in res.replans] == ["EX2", "HL1"]
    assert res.completed and res.schedule.entry("T3").robot == "HL2"


def test_robot_that_runs_out_of_battery_fails_instead_of_working_on_at_zero():
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
    # 200 Wh of work in a 200 Wh budget passes verification; the 77 cells of travel do not fit.
    assert check_schedule(req, graph, schedule) == []
    res = Simulator(req, graph, schedule).run(stop_on_event=False)

    assert not res.completed and res.finished_at_h is None
    failure = next(e for e in res.events if e.kind == "robot_failure")
    assert (failure.robot, failure.task, failure.t_h) == ("R1", "T2", 13.0)
    assert failure.detail is not None and "ran out of battery" in failure.detail
    assert res.events[-1] is failure                         # nothing happens after it
    last = robot(res.frames[-1], "R1")
    assert last.status == "failed" and last.battery_wh == pytest.approx(5.18, abs=0.05)
    assert all(robot(f, "R1").battery_wh >= 0 for f in res.frames)
    assert res.frames[-1].progress["T2"] == pytest.approx(0.5625)  # 9 of 16 work steps done
