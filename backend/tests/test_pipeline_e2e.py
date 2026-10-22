"""End-to-end test of the Moonwright planner in offline fixture mode (no API key needed).

``Planner(FixtureClient())`` is run over the three example briefs in ``examples/briefs`` with the
canned LLM answers in ``fixtures/<brief_id>/`` (``brief_key("...", "landing_pad") == "landing_pad"``):

* ``landing_pad``    feasible at the first attempt (repairs == 0)
* ``access_road``    feasible at the first attempt (repairs == 0)
* ``tight_battery``  ``taskgraph_1.json`` passes the validator but the CP-SAT energy constraints
                     reject it; the ``taskgraph_2.json`` repair is feasible (repairs == 1)

Hand-verified feasibility arithmetic for the fixtures
-----------------------------------------------------
Per robot type: summed task duration / robot count (must leave slack against the horizon), summed
energy against the summed batteries (the validator's aggregate check), and the largest load any
one robot carries in a budget-respecting split (the bin packing CP-SAT has to find).

landing_pad   horizon 336 h; EX1 EX2 5000, HL1 HL2 3000, CP1 3500, PR1 4000, SV1 1500 Wh
  excavator  T2 12h/1500 + T3 18h/2000 + T4 14h/1600 + T5 6h/600  = 50 h -> 25 h per robot
             energy 5700 / 10000 Wh; no 3-task subset containing T2, T3 and T4 fits
             (1500+2000+1600 = 5100 > 5000), so one excavator carries at most 2000+1600+600 = 4200
  hauler     T6 10h/800 + T7 16h/1300 + T8 6h/400                 = 32 h -> 16 h per robot
             energy 2500 / 6000 Wh; one hauler carrying all three still fits (2500 <= 3000)
  compactor  T9 12h/1300 + T10 14h/1500                           = 26 h; 2800 / 3500 Wh
  printer    T11 22h/2700 + T12 6h/700                            = 28 h; 3400 / 4000 Wh
  surveyor   T1 4h/300 + T13 3h/200 + T14 4h/300                  = 11 h;  800 / 1500 Wh
  critical path T1>T2>T3>T7>T10>T12>T13 = 4+12+18+16+14+6+3 = 73 h. PR1 must finish the pad
  surface (46-68) before the berm crest (68-74), so the optimum makespan is 77 h (T13 at 74-77).
  same-zone serial load: Z1 72 h, Z2 53 h, Z3 10 h, Z4 12 h; every zone chain is already
  serial through precedence, so one-robot-per-zone costs nothing extra.

access_road   horizon 240 h; EX1 5000, HL1 3000, CP1 CP2 3500, SV1 1500 Wh
  excavator  T2 10h/1200 + T3 10h/1200 + T4 12h/1500              = 32 h; 3900 / 5000 Wh
  hauler     T5 8h/700 + T6 10h/900                               = 18 h; 1600 / 3000 Wh
  compactor  T7 12h/1400 + T8 12h/1400 + T9 8h/800 + T10 8h/800   = 40 h -> 20 h per robot
             energy 4400 / 7000 Wh; {1400,1400,800} = 3600 does not fit, so one compactor
             carries at most 1400+800+800 = 3000 Wh
  surveyor   T1 5h/350 + T11 4h/300 + T12 4h/300                  = 13 h;  950 / 1500 Wh
  critical path T1>T2>T3>T4>T6>T10>T12 = 5+10+10+12+10+8+4 = 59 h = optimum makespan.
  T7 (23-35) and T8 (25-37) overlap, which is what the second compactor is for.
  same-zone serial load: Z1 39 h, Z2 22 h, Z3 24 h, Z4 18 h.

tight_battery horizon 168 h; EX1 EX2 5000, HL1 3000, CP1 3500, SV1 1500 Wh
  taskgraph_1 (must be rejected by CP-SAT, not by the validator)
  excavator  T2 24h/4000 + T3 24h/4000 + T4 12h/1900              = 60 h -> 30 h per robot
             energy 9900 <= 10000 Wh and every task <= 5000 Wh, so the validator's aggregate
             and per-task energy checks pass. T2 and T3 must sit on different robots
             (8000 > 5000) and T4 lifts either one to 5900 > 5000: no assignment exists, so the
             CP-SAT core is {energy:EX1, energy:EX2}. Everything else is slack (critical path
             T1>T3>T5>T8>T9 = 61 h, zone loads Z1 46 h, Z2 57 h), so energy is the only reason.
  hauler     T5 16h/1300 + T6 8h/700 = 24 h; 2000 / 3000 Wh
  compactor  T7 14h/1500 + T8 14h/1500 = 28 h; 3000 / 3500 Wh
  surveyor   T1 4h/300 + T9 3h/200 + T10 4h/300 = 11 h; 800 / 1500 Wh
  taskgraph_2 (the repair)
  excavator  T2 14h/2100 + T3 14h/2100 + T4 14h/2100 + T5 12h/1900 = 54 h -> 27 h per robot
             energy 8200 / 10000 Wh; every 3-task subset is >= 6100 > 5000, so the only split
             is {2100,2100} = 4200 and {2100,1900} = 4000 Wh, both inside 5000 (84 % / 80 %)
  hauler, compactor, surveyor unchanged (2000/3000, 3000/3500, 800/1500 Wh)
  critical path T1>T2>T3>T6>T9>T10 = 4+14+14+16+14+3 = 65 h = optimum makespan.
  same-zone serial load: Z1 50 h, Z2 47 h, Z3 8 h, Z4 12 h.
  No replan fixture is shipped: an offline replan keeps the exact remaining graph that
  build_replan_inputs derives from the live simulator state (a canned graph cannot know which
  tasks are done when the failure happens).

Wherever the solver has a choice, no feasible split loads a robot above 90 % of its battery, so
the simulator's 10 % reserve does not fire spurious battery_low events during the demo.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from moonwright.llm.base import FIXTURES_DIR, ExplainText, FixtureClient
from moonwright.pipeline.loop import Planner, brief_key
from moonwright.pipeline.schedule import SolveOptions, check_schedule, solve_schedule
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import Failure, PlanResult, Requirements, TaskGraph

BRIEFS_DIR = Path(__file__).resolve().parents[1] / "examples" / "briefs"
BRIEF_IDS = ["landing_pad", "access_road", "tight_battery"]
HORIZONS_H = {"landing_pad": 14 * 24, "access_road": 10 * 24, "tight_battery": 7 * 24}
# (brief id, task-graph fixture, whether a budget-respecting robot assignment exists)
GRAPH_FIXTURES = [
    ("landing_pad", "taskgraph_1.json", True),
    ("access_road", "taskgraph_1.json", True),
    ("tight_battery", "taskgraph_1.json", False),
    ("tight_battery", "taskgraph_2.json", True),
]


# --------------------------------------------------------------------------- helpers
def _brief(brief_id: str) -> str:
    return (BRIEFS_DIR / f"{brief_id}.md").read_text()


def _requirements(brief_id: str) -> Requirements:
    path = FIXTURES_DIR / brief_id / "structure.json"
    return Requirements.model_validate_json(path.read_text())


def _graph(brief_id: str, filename: str) -> TaskGraph:
    return TaskGraph.model_validate_json((FIXTURES_DIR / brief_id / filename).read_text())


def _explanation(brief_id: str) -> str:
    path = FIXTURES_DIR / brief_id / "explain.json"
    return ExplainText.model_validate_json(path.read_text()).text


def _plan(brief_id: str) -> PlanResult:
    return Planner(FixtureClient()).plan(_brief(brief_id), brief_id=brief_id)


def _word_count(text: str) -> int:
    return len(text.split())


def _critical_path_h(graph: TaskGraph) -> int:
    memo: dict[str, int] = {}

    def longest(task_id: str) -> int:
        if task_id not in memo:
            task = graph.task(task_id)
            memo[task_id] = task.duration_h + max((longest(p) for p in task.after), default=0)
        return memo[task_id]

    return max(longest(t.id) for t in graph.tasks)


def _energy_packable(req: Requirements, graph: TaskGraph) -> bool:
    """Brute-force the per-robot bin packing CP-SAT must solve, one robot type at a time."""
    for robot_type in {t.robot_type for t in graph.tasks}:
        tasks = [t for t in graph.tasks if t.robot_type == robot_type]
        robots = req.robots_of_type(robot_type)
        for assignment in itertools.product(robots, repeat=len(tasks)):
            loads = {r.id: 0.0 for r in robots}
            for task, robot in zip(tasks, assignment):
                loads[robot.id] += task.energy_wh
            if all(loads[r.id] <= r.battery_wh for r in robots):
                break
        else:
            return False
    return True


def _assert_verified_plan(plan: PlanResult) -> None:
    assert plan.success and plan.failure is None
    assert plan.task_graph is not None and plan.schedule is not None
    assert plan.schedule.makespan_h <= plan.requirements.horizon_h
    assert sorted(e.task for e in plan.schedule.entries) == sorted(plan.task_graph.ids())
    assert check_schedule(plan.requirements, plan.task_graph, plan.schedule) == []
    assert f"{plan.schedule.makespan_h} hours" in (plan.explanation or "")


# --------------------------------------------------------------------------- fixture content
@pytest.mark.parametrize("brief_id", BRIEF_IDS)
def test_brief_key_resolves_to_fixture_directory(brief_id: str) -> None:
    assert brief_key(_brief(brief_id), brief_id) == brief_id
    for name in ("structure.json", "taskgraph_1.json", "explain.json"):
        assert (FIXTURES_DIR / brief_id / name).is_file()


@pytest.mark.parametrize("brief_id", BRIEF_IDS)
def test_brief_and_structure_fixture_agree(brief_id: str) -> None:
    brief = _brief(brief_id)
    assert 120 <= _word_count(brief) <= 250
    req = _requirements(brief_id)
    assert req.horizon_h == HORIZONS_H[brief_id]
    for robot in req.robots:
        assert robot.id in brief and f"{robot.battery_wh:.0f} Wh" in brief
    zone_ids = [z.id for z in req.site.zones]
    assert len(set(zone_ids)) == len(zone_ids) and set(zone_ids) <= {"Z1", "Z2", "Z3", "Z4"}
    assert len(req.structures) >= 2
    assert all(s.dims() and s.zone in zone_ids for s in req.structures)
    assert 1 <= len(req.constraints) <= 3


@pytest.mark.parametrize("brief_id", BRIEF_IDS)
def test_explain_fixture_is_a_plain_prose_summary(brief_id: str) -> None:
    text = _explanation(brief_id)
    assert 120 <= _word_count(text) <= 200
    assert "\n" not in text and not text.startswith(("-", "*", "#"))


@pytest.mark.parametrize(("brief_id", "filename", "packable"), GRAPH_FIXTURES)
def test_graph_fixture_hand_arithmetic(brief_id: str, filename: str, packable: bool) -> None:
    req = _requirements(brief_id)
    graph = _graph(brief_id, filename)
    zone_ids = {z.id for z in req.site.zones}
    structure_ids = {s.id for s in req.structures}
    robot_types = {r.type for r in req.robots}

    assert 8 <= len(graph.tasks) <= 14
    assert len(set(graph.ids())) == len(graph.tasks)
    for task in graph.tasks:
        assert 2 <= task.duration_h <= 24
        assert task.robot_type in robot_types and task.zone in zone_ids
        assert task.structure is None or task.structure in structure_ids
        assert set(task.after) <= set(graph.ids()) and task.id not in task.after
        assert task.energy_wh <= max(r.battery_wh for r in req.robots_of_type(task.robot_type))
        assert 60 <= task.energy_wh / task.duration_h <= 170  # Wh per working hour

    for robot_type in robot_types:
        tasks = [t for t in graph.tasks if t.robot_type == robot_type]
        robots = req.robots_of_type(robot_type)
        assert sum(t.duration_h for t in tasks) / len(robots) <= req.horizon_h / 4
        assert sum(t.energy_wh for t in tasks) <= sum(r.battery_wh for r in robots)
    assert _critical_path_h(graph) <= req.horizon_h / 2
    for zone in zone_ids:
        assert sum(t.duration_h for t in graph.tasks if t.zone == zone) <= req.horizon_h / 2
    assert _energy_packable(req, graph) is packable


# --------------------------------------------------------------------------- end to end
@pytest.mark.parametrize("brief_id", ["landing_pad", "access_road"])
def test_feasible_brief_plans_without_repair(brief_id: str) -> None:
    plan = _plan(brief_id)

    stages = [s.stage for s in plan.steps]
    assert stages == ["structure", "taskgraph", "validate", "schedule", "explain"]
    assert all(s.ok for s in plan.steps)
    assert plan.repairs == 0
    assert plan.requirements.source_brief == _brief(brief_id)
    assert plan.steps[1].usage is not None
    assert plan.steps[1].usage.model == "fixture:taskgraph_1.json"
    assert plan.task_graph == _graph(brief_id, "taskgraph_1.json")
    assert plan.steps[-1].usage is not None
    assert plan.steps[-1].usage.model == "fixture:explain.json"
    assert plan.explanation == _explanation(brief_id)
    _assert_verified_plan(plan)


def test_tight_battery_first_graph_is_rejected_only_by_the_solver() -> None:
    req = _requirements("tight_battery")
    graph = _graph("tight_battery", "taskgraph_1.json")

    assert validate_graph(req, graph) is None
    out = solve_schedule(req, graph, SolveOptions(time_limit_s=10.0))
    assert isinstance(out, Failure) and out.kind == "infeasible"
    energy_conflicts = {c for c in out.conflicts if c.startswith("energy:")}
    assert energy_conflicts and energy_conflicts <= {"energy:EX1", "energy:EX2"}


def test_tight_battery_demonstrates_the_repair_loop() -> None:
    plan = _plan("tight_battery")

    stages = [s.stage for s in plan.steps]
    assert stages == ["structure", "taskgraph", "validate", "schedule",
                      "repair", "validate", "schedule", "explain"]
    assert plan.repairs == 1
    first_validate, rejected, repair, second_validate, accepted = plan.steps[2:7]
    assert first_validate.ok, "taskgraph_1 must be rejected by CP-SAT, not by the validator"
    assert not rejected.ok and rejected.failure is not None
    assert rejected.failure.kind == "infeasible"
    energy_conflicts = {c for c in rejected.failure.conflicts if c.startswith("energy:")}
    assert energy_conflicts and energy_conflicts <= {"energy:EX1", "energy:EX2"}
    assert repair.ok and repair.attempt == 2 and repair.usage is not None
    assert repair.usage.model == "fixture:taskgraph_2.json"
    assert second_validate.ok and accepted.ok
    assert plan.task_graph == _graph("tight_battery", "taskgraph_2.json")
    assert plan.explanation == _explanation("tight_battery")
    _assert_verified_plan(plan)

    # The repaired split is forced: one excavator carries 4200 Wh, the other 4000 Wh.
    loads = {robot: sum(plan.task_graph.task(e.task).energy_wh for e in entries)
             for robot, entries in plan.schedule.by_robot().items()}
    assert sorted(loads.get(r, 0.0) for r in ("EX1", "EX2")) == [4000.0, 4200.0]
