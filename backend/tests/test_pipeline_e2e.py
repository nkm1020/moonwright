"""End-to-end test of the Moonwright planner in offline fixture mode (no API key needed).

``Planner(FixtureClient())`` replays the canned LLM answers in ``fixtures/<brief_id>/``
(``brief_key("...", "landing_pad") == "landing_pad"``). The hand-made fixtures of Oct 22 carry no
quantities and are checked against the arithmetic below:

* ``landing_pad_v1`` feasible at the first attempt (repairs == 0); its brief is the demo brief as
                     it was before the berm was cut to 120 m3 (``tests/data/landing_pad_v1.md``)
* ``access_road``    feasible at the first attempt (repairs == 0)
* ``tight_battery``  ``taskgraph_1.json`` passes the validator but the CP-SAT energy constraints
                     reject it; the ``taskgraph_2.json`` repair is feasible (repairs == 1)

The fixtures of the demo briefs ``landing_pad``, ``front_loaded`` and ``habitat_berm`` are live
runs recorded on Nebius Token Factory on Oct 24 on the final pipeline; each replay is pinned to
its run's story (three tests near the end), and the UI's samples (``frontend/public/samples``)
are checked to be those live runs themselves, which the fixtures replay step for step (the last
test).

Hand-verified feasibility arithmetic for the fixtures
-----------------------------------------------------
Per robot type: summed task duration / robot count (must leave slack against the horizon), summed
energy against the summed batteries (the validator's aggregate check), and the largest load any
one robot carries in a budget-respecting split (the bin packing CP-SAT has to find).

landing_pad_v1 horizon 336 h; EX1 EX2 5000, HL1 HL2 3000, CP1 3500, PR1 4000, SV1 1500 Wh
  excavator  T2 12h/1500 + T3 18h/2000 + T4 14h/1600 + T5 6h/600  = 50 h -> 25 h per robot
             energy 5700 / 10000 Wh; no 3-task subset containing T2, T3 and T4 fits
             (1500+2000+1600 = 5100 > 5000), so one excavator carries at most 2000+1600+600 = 4200
  hauler     T6 10h/800 + T7 16h/1300 + T8 6h/400                 = 32 h -> 16 h per robot
             energy 2500 / 6000 Wh; one hauler carrying all three still fits (2500 <= 3000)
  compactor  T9 12h/1300 + T10 14h/1500                           = 26 h; 2800 / 3500 Wh
  printer    T11 22h/2700 + T12 6h/700                            = 28 h; 3400 / 4000 Wh
  surveyor   T1 4h/300 + T13 3h/200 + T14 4h/300                  = 11 h;  800 / 1500 Wh
  critical path T1>T2>T3>T7>T10>T12>T13 = 4+12+18+16+14+6+3 = 73 h. PR1 must finish the pad
  surface before the berm crest, so the work-only optimum is 77 h (T13 at 74-77); with travel
  (whole hours per leg, 1-2 h on this site: SV1's 1 h from the depot to Z1 and PR1's 2 h from
  the pad surface in Z1 to the crest in Z2) CP-SAT proves 80 h (T13 at 77-80), as it did with
  the phase-3 transit allowance.
  same-zone serial load: Z1 72 h, Z2 53 h, Z3 10 h, Z4 12 h; every zone chain is already
  serial through precedence, so one-robot-per-zone costs nothing extra.

access_road   horizon 240 h; EX1 5000, HL1 3000, CP1 CP2 3500, SV1 1500 Wh
  excavator  T2 10h/1200 + T3 10h/1200 + T4 12h/1500              = 32 h; 3900 / 5000 Wh
  hauler     T5 8h/700 + T6 10h/900                               = 18 h; 1600 / 3000 Wh
  compactor  T7 12h/1400 + T8 12h/1400 + T9 8h/800 + T10 8h/800   = 40 h -> 20 h per robot
             energy 4400 / 7000 Wh; {1400,1400,800} = 3600 does not fit, so one compactor
             carries at most 1400+800+800 = 3000 Wh
  surveyor   T1 5h/350 + T11 4h/300 + T12 4h/300                  = 13 h;  950 / 1500 Wh
  critical path T1>T2>T3>T4>T6>T10>T12 = 5+10+10+12+10+8+4 = 59 h = work-only optimum; travel
  makes it 63 h: SV1's 2 h from the depot to Z1 and EX1's two 1 h moves (Z1 -> Z2 -> Z3). The
  phase-3 transit allowance, the longest leg into each zone before every task, made it 64 h.
  T7 (25-37) and T8 (28-40) overlap, which is what the second compactor is for.
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
  critical path T1>T2>T3>T6>T9>T10 = 4+14+14+16+14+3 = 65 h = work-only optimum; travel makes
  it 66 h, SV1's 1 h from the depot to Z1 (one excavator, EX2 in the settled schedule, digs
  both halves of the bowl in Z1 back to back, with no leg between them; the phase-3 transit
  allowance charged one and made it 68 h).
  same-zone serial load: Z1 50 h, Z2 47 h, Z3 8 h, Z4 12 h.
  No replan fixture is shipped: an offline replan keeps the exact remaining graph that
  build_replan_inputs derives from the live simulator state (a canned graph cannot know which
  tasks are done when the failure happens).

Wherever the solver has a choice, no feasible split loads a robot above 90 % of its battery, so
every demo robot ends its replay with a tenth of its battery in hand (battery_low itself is a
forecast that a robot will run dry under the plan in force, which a verified plan never trips).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from moonwright.llm.base import FIXTURES_DIR, ExplainText, FixtureClient, Usage, validate_into
from moonwright.pipeline.loop import Planner, brief_key
from moonwright.pipeline.rates import UNGROUNDED_ADVISORY, ancestors, apply_rates, scope_gaps
from moonwright.pipeline.schedule import SolveOptions, check_schedule, solve_schedule
from moonwright.pipeline.taskgraph import generate_task_graph
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import Failure, PlanResult, Requirements, TaskGraph

BRIEFS_DIR = Path(__file__).resolve().parents[1] / "examples" / "briefs"
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "frontend" / "public" / "samples"
MODEL_STAGES = ("structure", "taskgraph", "repair", "explain")
V1_BRIEF = Path(__file__).resolve().parent / "data" / "landing_pad_v1.md"
BRIEF_IDS = ["landing_pad_v1", "access_road", "tight_battery"]   # the hand-made fixtures
HORIZONS_H = {"landing_pad_v1": 14 * 24, "access_road": 10 * 24, "tight_battery": 7 * 24}
MAKESPANS_H = {"landing_pad_v1": 77, "access_road": 59, "tight_battery": 65}  # work, no transit
TRAVEL_MAKESPANS_H = {"landing_pad_v1": 80, "access_road": 63, "tight_battery": 66}  # phase 3k
# (brief id, task-graph fixture, whether a budget-respecting robot assignment exists)
GRAPH_FIXTURES = [
    ("landing_pad_v1", "taskgraph_1.json", True),
    ("access_road", "taskgraph_1.json", True),
    ("tight_battery", "taskgraph_1.json", False),
    ("tight_battery", "taskgraph_2.json", True),
]


# --------------------------------------------------------------------------- helpers
def _brief(brief_id: str) -> str:
    return (V1_BRIEF if brief_id == "landing_pad_v1" else BRIEFS_DIR / f"{brief_id}.md").read_text()


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


def _assert_verified_plan(plan: PlanResult, explained: bool = True) -> None:
    assert plan.success and plan.failure is None
    assert plan.task_graph is not None and plan.schedule is not None
    assert plan.schedule.makespan_h <= plan.requirements.horizon_h
    assert sorted(e.task for e in plan.schedule.entries) == sorted(plan.task_graph.ids())
    assert check_schedule(plan.requirements, plan.task_graph, plan.schedule) == []
    assert plan.explanation
    if explained:
        assert f"{plan.schedule.makespan_h} hours" in plan.explanation


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
@pytest.mark.parametrize("brief_id", ["landing_pad_v1", "access_road"])
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


# --------------------------------------------------------------------------- rate model (phase 3a)
@pytest.mark.parametrize("brief_id", BRIEF_IDS)
def test_fixture_plans_keep_their_makespans_under_the_rate_model(brief_id: str) -> None:
    """The fixture graphs carry no quantity, so the rate model leaves them untouched (and the
    task-graph step says so with the ungrounded advisory): the planned graph is the recorded
    one, field for field, and the makespan is exactly what CP-SAT gives the raw fixture graph
    (77 / 59 / 65 h with the phase-2 scheduler, 80 / 64 / 68 h with the transit allowance of
    phase 3, 80 / 63 / 66 h with the exact travel of phase 3k: travel shifts the figures, not
    this equality)."""
    plan = _plan(brief_id)
    assert plan.success and plan.schedule is not None and plan.task_graph is not None
    final = "taskgraph_2.json" if brief_id == "tight_battery" else "taskgraph_1.json"
    assert plan.task_graph == _graph(brief_id, final)
    raw = solve_schedule(_requirements(brief_id), _graph(brief_id, final),
                         SolveOptions(time_limit_s=10.0))
    assert not isinstance(raw, Failure)
    assert plan.schedule.makespan_h == raw.makespan_h == TRAVEL_MAKESPANS_H[brief_id]
    assert plan.schedule.makespan_h >= MAKESPANS_H[brief_id]  # phase-2 optimum, no travel
    assert all(t.quantity is None and t.estimated_duration_h is None
               and t.estimated_energy_wh is None for t in plan.task_graph.tasks)
    for step in plan.steps:
        if step.stage in ("taskgraph", "repair"):
            payload = step.payload or {}   # the loop may add its own keys (e.g. a repair diff)
            assert (payload["advisories"], payload["rate_model"], payload["grounded"]) == \
                ([UNGROUNDED_ADVISORY], True, 0)
            assert step.message is not None and step.message.endswith(" tasks")


@pytest.mark.parametrize(("brief_id", "filename", "_packable"), GRAPH_FIXTURES)
def test_fixture_graphs_pass_validation_with_coverage(brief_id: str, filename: str,
                                                      _packable: bool) -> None:
    req = _requirements(brief_id)
    graph = _graph(brief_id, filename)
    assert validate_graph(req, graph, coverage=True) is None
    grounded, advisories = apply_rates(graph)
    assert grounded is graph and advisories == []


class _Answer:
    """LLM stub that answers every task-graph request with one canned graph."""

    name = "stub"

    def __init__(self, payload: dict):
        self.payload = payload
        self.prompts: list[str] = []

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        self.prompts.append(user)
        return validate_into(schema, self.payload), Usage(model="stub")


# quantity + unit per physical task of the landing_pad_v1 fixture graph, sized so that the rate
# table lands near the hand-made figures (T3, the bowl, is deliberately under-estimated below)
LANDING_PAD_SCOPE = {
    "T2": (300, "m2"), "T3": (190, "m3"), "T4": (98, "m3"), "T5": (150, "m2"), "T6": (120, "m3"),
    "T7": (188, "m3"), "T8": (70, "m3"), "T9": (314, "m2"), "T10": (350, "m2"),
    "T11": (314, "m2"), "T12": (80, "m2"),
}


def test_quantities_ground_the_graph_before_it_is_scheduled() -> None:
    """A live-style answer that under-sizes the pad bowl (10 h / 1000 Wh for 190 m3, what the
    live model does) is re-sized by the rate table on its way out of the task-graph stage,
    the advisory names the gap, and CP-SAT schedules the computed hours. An answer that states
    quantities for some physical tasks but not for others is rejected by the validator."""
    req = _requirements("landing_pad_v1")
    data = json.loads((FIXTURES_DIR / "landing_pad_v1" / "taskgraph_1.json").read_text())
    for t in data["tasks"]:
        if t["id"] == "T3":   # excavate pad bowl: the model's guess is far too small
            t.update(basis="pi*(10 m)^2*0.6 m = 188 m3 (+1%)", duration_h=10, energy_wh=1000)
        if t["id"] in LANDING_PAD_SCOPE:
            t["quantity"], t["unit"] = LANDING_PAD_SCOPE[t["id"]]
    # the bowl and the sinter sized, everything else hand-written: not accepted any more
    partial = TaskGraph.model_validate({"tasks": [
        t if t["id"] in ("T3", "T11") else dict(t, quantity=None, unit=None)
        for t in data["tasks"]]})
    rejected = validate_graph(req, apply_rates(partial)[0], coverage=True)
    assert rejected is not None
    assert rejected.conflicts == sorted(f"quantity:{i}" for i in LANDING_PAD_SCOPE
                                        if i not in ("T3", "T11"))
    assert all(d.endswith("has no quantity") for d in rejected.details)
    client = _Answer(data)

    graph, step = generate_task_graph(client, req, key="k")
    assert graph is not None and step.ok and step.stage == "taskgraph"
    bowl, sinter = graph.task("T3"), graph.task("T11")
    assert (bowl.duration_h, bowl.energy_wh) == (28, 3040.0)
    assert (bowl.estimated_duration_h, bowl.estimated_energy_wh) == (10.0, 1000.0)
    assert (sinter.duration_h, sinter.energy_wh) == (23, 2700.4)
    assert step.payload == {
        "advisories": [("estimate:T3: model said 10 h / 1000 Wh, rate model gives 28 h / "
                        "3040 Wh (+204%)"),
                       # the bowl is sized for 0.6 m while the structure says 0.4 m deep;
                       # an excess is judged on the m3 alone, so the 300 m2 strip of T2 (its
                       # top 0.2 m) does not count twice
                       ("volume:S1: its dimensions give 125.7 m3 (bowl 20 m across, 0.4 m deep) "
                        "but its excavation tasks in m3 (T3 190 m3) total 190 m3 (151 %): dug "
                        "1.5 times over")],
        "rate_model": True, "grounded": len(LANDING_PAD_SCOPE),
    }
    # pure-time tasks of an engaged graph (the surveys) are charged at least their base power
    assert graph.task("T13").energy_wh == 225.0 and graph.task("T13").estimated_energy_wh == 200.0
    assert graph.task("T1") == TaskGraph.model_validate(data).task("T1")   # 4 h x 75 W == 300
    assert validate_graph(req, graph, coverage=True) is None

    plan = Planner(client, explain=False).plan_from_requirements(req, key="k")
    assert plan.success and plan.schedule is not None and plan.task_graph is not None
    assert plan.task_graph.task("T3").duration_h == 28
    entry = plan.schedule.entry("T3")
    assert entry.end_h - entry.start_h == 28
    raw = solve_schedule(req, _graph("landing_pad_v1", "taskgraph_1.json"),
                         SolveOptions(time_limit_s=10.0))
    assert not isinstance(raw, Failure)
    assert plan.schedule.makespan_h > raw.makespan_h  # the bowl really takes longer
    assert check_schedule(plan.requirements, plan.task_graph, plan.schedule) == []
    assert plan.steps[0].payload is not None
    assert plan.steps[0].payload["advisories"][0].startswith("estimate:T3:")


# --------------------------------------------------------------------------- live recordings
def test_landing_pad_replays_its_live_run() -> None:
    """Oct 24, on the final pipeline: Nemotron 3 Ultra's first graph needs at least 4,095 Wh of
    the printer's 4,000 (4,078 Wh of work, 18 Wh of travel). Repair 1 trims the berm crest's
    sintering from 160 to 150 m2 and is still 9 Wh over; repair 2 trims it to 145 m2 and CP-SAT
    proves 72 h: in scope, never a supply conflict, each robot's legs timed exactly."""
    plan = _plan("landing_pad")

    assert [(s.stage, s.ok) for s in plan.steps] == [
        ("structure", True), ("taskgraph", True), ("validate", False), ("repair", True),
        ("validate", False), ("repair", True), ("validate", True), ("schedule", True),
        ("explain", True)]
    assert (plan.steps[0].payload or {})["warnings"] == []
    assert [(s.id, s.type) for s in plan.requirements.structures] == [
        ("PAD1", "landing_pad"), ("BERM1", "blast_berm")]
    verdicts = [(s.stage, s.attempt, s.failure.kind, s.failure.conflicts)
                for s in plan.steps if s.failure is not None]
    assert verdicts == [("validate", 1, "validation", ["energy:printer"]),
                        ("validate", 2, "validation", ["energy:printer"])]
    first, second = (s.failure for s in plan.steps if s.failure is not None)
    assert first is not None and second is not None
    assert ("printer tasks need at least 4,095 Wh, 4,078 Wh of work and 18 Wh of travel, but its "
            "1 robot carries only 4,000 Wh combined") in first.hint
    assert ("printer tasks need at least 4,009 Wh, 3,992 Wh of work and 18 Wh of travel, but its "
            "1 robot carries only 4,000 Wh combined") in second.hint

    repairs = [s for s in plan.steps if s.stage == "repair"]
    assert [s.attempt for s in repairs] == [2, 3]
    assert [s.usage.model for s in repairs if s.usage is not None] == [
        "fixture:taskgraph_2.json", "fixture:taskgraph_3.json"]
    assert [(s.payload or {})["diff"]["scope_drift_pct"] for s in repairs] == [-0.9, -0.5]
    graph = plan.task_graph
    assert graph is not None and graph.ids() == _graph("landing_pad", "taskgraph_3.json").ids()
    assert graph.task("T10").quantity == 145.0
    assert plan.repairs == 2 and plan.schedule is not None
    assert (plan.schedule.makespan_h, plan.schedule.status) == (72, "optimal")
    assert not [c for s in plan.steps if s.failure is not None for c in s.failure.conflicts
                if c.startswith("supply:")]
    assert validate_graph(plan.requirements, graph, coverage=True, supply=True) is None
    assert scope_gaps(plan.requirements, graph) == []
    _assert_verified_plan(plan)


def test_front_loaded_replays_its_live_run() -> None:
    """Designed impossible: a 30 m pad dug and compacted in one zone, which admits one robot at
    a time, and a 60 h window. Lightning typed the pad ``excavator`` and listed only [excavate,
    survey] on it, as in all three run-3 structurings of the eval brief; the structure stage
    restores the compaction its entry states (phase 3l). Ultra's first graph validates and
    CP-SAT proves ``horizon, zone:Z1``; repair 1 is refused on an 82 h chain; repair 2 is
    proved infeasible on the same core; repair 3 is refused on the chain again. No repair
    shrinks the work (scope drift 0 % each time): no plan after three repairs, and the advice
    is CP-SAT-verified."""
    plan = _plan("front_loaded")
    assert plan.steps[0].payload is not None
    assert plan.steps[0].payload["warnings"] == [
        ('structure S1: operations compact restored: its entry states "subgrade compacted and '
         'inspected"')]
    assert [(s.id, s.type, s.operations) for s in plan.requirements.structures] == [
        ("S1", "excavator", ["excavate", "survey", "compact"]),
        ("S2", "spoil_berm", ["haul", "compact"])]

    assert [s.stage for s in plan.steps] == [
        "structure", "taskgraph", "validate", "schedule", "repair", "validate", "repair",
        "validate", "schedule", "repair", "validate"]
    verdicts = [(s.stage, s.attempt, s.failure.kind, s.failure.conflicts)
                for s in plan.steps if s.failure is not None]
    assert verdicts == [
        ("schedule", 1, "infeasible", ["horizon", "zone:Z1"]),
        ("validate", 2, "validation", ["horizon_path:T1->T2->T3->T6->T7->T8"]),
        ("schedule", 3, "infeasible", ["horizon", "zone:Z1"]),
        ("validate", 4, "validation", ["horizon_path:T1->T2->T3->T6->T7->T8"])]
    core = plan.steps[3].failure
    assert core is not None
    assert ("Zone Z1 admits one robot at a time, so its 5 tasks (75 h in total) cannot "
            "overlap.") in core.hint
    assert [(s.payload or {})["diff"]["scope_drift_pct"] for s in plan.steps
            if s.stage == "repair"] == [0.0, 0.0, 0.0]

    assert not plan.success and plan.repairs == 3 and plan.schedule is None
    assert plan.failure is not None and plan.failure == plan.steps[-1].failure
    assert plan.failure.details == ["T1->T2->T3->T6->T7->T8 takes 82 h in sequence; horizon is 60 h"]
    assert plan.explanation is None
    [advice] = plan.suggestions
    assert (advice.kind, advice.horizon_h, advice.makespan_h, advice.verified) == (
        "extend_horizon", 85, 85, True)
    assert advice.detail == "finishing needs 85 h, 25 h past the 60 h deadline (CP-SAT optimal)"
    assert (plan.steps[-1].payload or {})["suggestions"] == [advice.model_dump()]
    assert plan.task_graph is not None
    assert [t.robot_type for t in plan.task_graph.tasks].count("compactor") == 3


def test_habitat_berm_replays_its_live_run() -> None:
    """Designed horizon-bound (kind C): a pit and a borrow strip feed a 280 m3 berm in 72 h,
    which holds only if both are dug at once. The structure stage drops the berm's sintering
    (no printer) and corrects the pit's diameter to its 12 m by 12 m footprint. Ultra's first
    graph validates and already lets both digs run at once, but its hauls move 400 m3 into the
    berm against 264 m3 dug (an advisory), so zone Z2 needs 49 h one robot at a time and
    CP-SAT proves the deadline cannot hold: a 13-conflict core of the horizon, 10 precedence
    edges, SV1 and Z2. One repair drops the double haul (280 m3, Z2 39 h, energy -8.4 %) and
    splits the digs so each haul follows its part (11 tasks added, 8 removed, 6 renumbered);
    both excavators start at 8 h and CP-SAT proves 52 h, in scope. The brief's own digs give
    264 of the berm's 280 m3: the final graph's notes say so and haul the last 16 m3 as
    sourced externally, which the supply check accepts within SUPPLY_SLACK."""
    plan = _plan("habitat_berm")
    assert (plan.steps[0].payload or {})["warnings"] == [
        ("structure S2: operations sinter dropped: the fleet has no printer, so no sinter task "
         "will be required of it (add a printer and the operation again to plan it)"),
        ("structure S1: diameter_m 12 -> 12 m x 12 m: the brief's entry for S1 states a 12 m by "
         "12 m footprint")]
    assert [(s.id, s.type, s.operations) for s in plan.requirements.structures] == [
        ("S1", "excavation_pit", ["survey", "excavate", "compact"]),
        ("S2", "berm", ["haul", "compact", "survey"]),
        ("S3", "borrow_strip", ["excavate", "haul", "compact"])]
    assert (plan.requirements.structures[0].length_m, plan.requirements.structures[0].width_m) == (12, 12)

    assert [(s.stage, s.ok) for s in plan.steps] == [
        ("structure", True), ("taskgraph", True), ("validate", True), ("schedule", False),
        ("repair", True), ("validate", True), ("schedule", True), ("explain", True)]
    assert (plan.steps[1].payload or {})["advisories"] == [
        ("supply:S2: its hauls (T7 60 m3, T8 60 m3, T9 140 m3, T10 140 m3) move 400 m3, more "
         "than the 264 m3 the digs before them remove (S1 excavation pit: T3, T4; S3 borrow "
         "strip: T5, T6)")]
    edges = ["T1->T5", "T10->T13", "T13->T14", "T2->T5", "T3->T4", "T4->T9", "T5->T6", "T6->T7",
             "T7->T8", "T9->T10"]
    assert _verdicts(plan) == [("schedule", 1, "infeasible", [
        "horizon", *(f"precedence:{e}" for e in edges), "robot:SV1", "zone:Z2"])]
    core = plan.steps[3].failure
    assert core is not None
    assert core.hint.startswith(
        "The plan cannot finish within the 72 h horizon: the longest precedence chain alone needs "
        "64 h including the transit to its first zone; the busiest robot type (excavator) needs "
        "at least 20 h.")
    assert core.hint.endswith(
        "Zone Z2 admits one robot at a time, so its 7 tasks (49 h in total) cannot overlap.")

    [repair] = [s for s in plan.steps if s.stage == "repair"]
    assert repair.usage is not None and repair.usage.model == "fixture:taskgraph_2.json"
    diff = (repair.payload or {})["diff"]
    assert (len(diff["added"]), len(diff["removed"]), len(diff["renumbered"]),
            diff["scope_drift_pct"]) == (11, 8, 6, -8.4)
    graph = plan.task_graph
    assert graph is not None and graph.ids() == _graph("habitat_berm", "taskgraph_2.json").ids()
    assert len(graph.tasks) == 17
    assert plan.repairs == 1 and plan.schedule is not None
    assert (plan.schedule.makespan_h, plan.schedule.status) == (52, "optimal")
    # what the repair changed: the hauls into the berm (400 -> 280 m3) and with them zone Z2's
    # one-robot-at-a-time work (49 -> 39 h); the first graph already let both digs run at once
    first = _graph("habitat_berm", "taskgraph_1.json")
    assert [sum(t.duration_h for t in g.tasks if t.zone == "Z2") for g in (first, graph)] == [49, 39]
    before = ancestors(first)
    assert all(pit not in before[strip] and strip not in before[pit]
               for pit in ("T3", "T4") for strip in ("T5", "T6"))
    # the pit (EX2) and the borrow strip (EX1) are dug at the same time
    digs = {e.task: e for e in plan.schedule.entries if e.task.startswith("EX_")}
    pit = [digs[t] for t in ("EX_S1_1", "EX_S1_2", "EX_S1_3")]
    strip = [digs[t] for t in ("EX_S3_1", "EX_S3_2")]
    assert {e.robot for e in pit} == {"EX2"} and {e.robot for e in strip} == {"EX1"}
    assert min(e.start_h for e in pit) == min(e.start_h for e in strip) == 8
    assert validate_graph(plan.requirements, graph, coverage=True, supply=True) is None
    assert scope_gaps(plan.requirements, graph) == []
    assert graph.notes is not None
    assert "264 m3, a 16 m3 deficit" in graph.notes and "sourced externally" in graph.notes
    _assert_verified_plan(plan)


# --------------------------------------------------------------------------- the UI's samples
def _verdicts(plan: PlanResult) -> list[tuple[str, int, str, list[str]]]:
    return [(s.stage, s.attempt, s.failure.kind, s.failure.conflicts)
            for s in plan.steps if s.failure is not None]


# a sample whose take predates a verdict change would be listed here (strict xfail) until it is
# re-recorded; the three takes were recorded on the final code (Oct 24)
_STALE_SAMPLES: set[str] = set()


@pytest.mark.parametrize("path", [
    pytest.param(p, marks=pytest.mark.xfail(
        strict=True, reason="take predates phase 3i's double-count conflict; re-record"))
    if p.name in _STALE_SAMPLES else p
    for p in sorted(SAMPLES_DIR.glob("*.plan.json"))], ids=lambda p: p.name)
def test_ui_samples_are_the_live_runs_their_fixtures_replay(path: Path) -> None:
    """A sample is a take's own PlanResult, never a replay of it: every model step names a
    Token Factory model (a replay's steps say ``fixture:<file>``, and the page would lose its
    "recorded live run" labels). The take's fixtures replay it step for step: the same stages,
    verdicts, repairs, task ids, advice and schedule (the solve breaks ties between optima the
    same way every time), so the CLI's offline numbers are the demo's."""
    sample = PlanResult.model_validate_json(path.read_text())
    brief_id = path.name.removesuffix(".plan.json")
    model_steps = [s for s in sample.steps if s.stage in MODEL_STAGES]
    assert model_steps and all(s.usage is not None for s in model_steps)
    assert not [s.usage.model for s in model_steps
                if s.usage is not None and s.usage.model.startswith("fixture:")]

    replay = _plan(brief_id)
    assert [(s.stage, s.ok) for s in replay.steps] == [(s.stage, s.ok) for s in sample.steps]
    assert _verdicts(replay) == _verdicts(sample)
    assert (replay.success, replay.repairs) == (sample.success, sample.repairs)
    assert replay.task_graph is not None and sample.task_graph is not None
    assert replay.task_graph.ids() == sample.task_graph.ids()
    assert replay.suggestions == sample.suggestions
    if sample.schedule is None:
        assert replay.schedule is None
    else:
        assert replay.schedule is not None
        assert (replay.schedule.makespan_h, replay.schedule.status) == (
            sample.schedule.makespan_h, sample.schedule.status)
        assert replay.schedule.entries == sample.schedule.entries
