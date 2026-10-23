"""Tests for moonwright.pipeline.relax: solver-grounded advice when the fleet cannot finish.

Every suggestion is checked two ways: the figures the module reports, and the proof applied
(the modified requirements re-solved by CP-SAT under the original horizon, which must succeed
exactly as the suggestion claims). The hand-made sites put every zone on the depot cell
(``FLAT``) so energies are exact; one test moves a zone away to show that the battery figure
counts the legs the planning model charges.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from moonwright.pipeline import relax as relax_module
from moonwright.pipeline.relax import (
    KINDS,
    MAX_SUGGESTIONS,
    SOLVE_LIMIT_S,
    format_suggestions,
    new_robot_id,
    rebase_suggestion,
    suggest_relaxations,
)
from moonwright.pipeline.schedule import SolveOptions, solve_schedule
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import (
    MAX_ENERGY_WH,
    Failure,
    PlanResult,
    Requirements,
    Robot,
    Schedule,
    ScheduleEntry,
    Site,
    Suggestion,
    Task,
    TaskGraph,
    Zone,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FLAT = Site(depot=(20, 20), zones=[Zone(id=f"Z{i}", center=(20, 20)) for i in range(1, 6)])


# ----------------------------------------------------------------------------- helpers
def robot(robot_id: str, robot_type: str, battery_wh: float) -> Robot:
    return Robot(id=robot_id, type=robot_type, battery_wh=battery_wh)


def task(task_id: str, robot_type: str, duration_h: int, energy_wh: float, zone: str,
         after: tuple[str, ...] = (), **extra) -> Task:
    return Task(id=task_id, name=task_id, robot_type=robot_type, duration_h=duration_h,
                energy_wh=energy_wh, zone=zone, after=list(after), **extra)


def reqs(robots: list[Robot], horizon_h: int, site: Site = FLAT) -> Requirements:
    return Requirements(site=site, robots=robots, horizon_h=horizon_h)


def verdict(req: Requirements, graph: TaskGraph) -> Failure:
    """The failure the planner would report: the validator's, else the solver's."""
    failure = validate_graph(req, graph)
    if failure is None:
        out = solve_schedule(req, graph, SolveOptions())
        assert isinstance(out, Failure), f"expected an infeasible graph, got {out!r}"
        failure = out
    return failure


def applied(req: Requirements, s: Suggestion) -> Requirements:
    """``req`` with the suggestion carried out, so the claim can be re-solved."""
    if s.kind == "extend_horizon":
        assert s.horizon_h is not None
        return req.model_copy(update={"horizon_h": s.horizon_h})
    if s.kind == "raise_battery":
        robots = [r.model_copy(update={"battery_wh": s.battery_wh}) if r.id == s.robot_id else r
                  for r in req.robots]
        return req.model_copy(update={"robots": robots})
    if s.kind == "add_robot":
        assert s.robot_type and s.robot_id and s.battery_wh
        spare = Robot(id=s.robot_id, type=s.robot_type, battery_wh=s.battery_wh)
        return req.model_copy(update={"robots": [*req.robots, spare]})
    raise AssertionError(f"{s.kind} cannot be applied")


def assert_proven(req: Requirements, graph: TaskGraph, s: Suggestion) -> Schedule:
    """Re-solve the modified requirements: the suggestion must hold exactly as stated."""
    assert s.verified and s.makespan_h is not None
    out = solve_schedule(applied(req, s), graph, SolveOptions())
    assert isinstance(out, Schedule), f"{s.kind} did not re-solve: {out!r}"
    assert out.makespan_h == s.makespan_h
    if s.kind != "extend_horizon":
        assert out.makespan_h <= req.horizon_h
    return out


# ----------------------------------------------------------------------------- horizon
def horizon_case() -> tuple[Requirements, TaskGraph]:
    """Chain T1(4h) -> T2(3h) plus T3(5h) on two excavators: 7 h optimum, 6 h deadline."""
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h=6)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 3, 100, "Z2", after=("T1",)),
                             task("T3", "excavator", 5, 100, "Z3")])
    return req, graph


def test_horizon_only_brief_names_the_exact_makespan():
    req, graph = horizon_case()
    validator = validate_graph(req, graph)
    assert validator is not None and validator.conflicts == ["horizon_path:T1->T2"]
    solver = solve_schedule(req, graph, SolveOptions())
    assert isinstance(solver, Failure) and "horizon" in solver.conflicts

    for failure in (validator, solver):   # the validator's chain verdict and the solver's core
        found = suggest_relaxations(req, graph, failure)
        assert [s.kind for s in found] == ["extend_horizon"]   # a spare cannot shorten a chain
        (s,) = found
        assert s.horizon_h == s.makespan_h == 7 and s.verified
        assert s.detail == "finishing needs 7 h, 1 h past the 6 h deadline (CP-SAT optimal)"
        assert (s.robot_id, s.robot_type, s.battery_wh) == (None, None, None)
        assert_proven(req, graph, s)
    # one hour less is still infeasible: the figure is exact
    assert isinstance(solve_schedule(req.model_copy(update={"horizon_h": 6}), graph,
                                     SolveOptions()), Failure)


# ----------------------------------------------------------------------------- battery
def battery_case() -> tuple[Requirements, TaskGraph]:
    """One excavator (5000 Wh) asked for 3000 + 3200 Wh of work; no travel on FLAT."""
    req = reqs([robot("EX1", "excavator", 5000)], horizon_h=100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 3000, "Z1"),
                             task("T2", "excavator", 3, 3200, "Z2")])
    return req, graph


def test_battery_only_infeasibility_is_priced_at_the_solvers_own_use():
    req, graph = battery_case()
    validator = verdict(req, graph)
    assert validator.conflicts == ["energy:excavator"]   # the validator names the type ...
    solver = solve_schedule(req, graph, SolveOptions())
    assert isinstance(solver, Failure) and solver.conflicts == ["energy:EX1"]   # ... CP-SAT the robot
    found = suggest_relaxations(req, graph, validator)
    assert suggest_relaxations(req, graph, solver) == found   # same advice either way

    assert [(s.kind, s.robot_id) for s in found] == [("raise_battery", "EX1"),
                                                     ("add_robot", "EX2")]
    raise_, add = found
    assert raise_.battery_wh == 6200.0   # exactly the 6200 Wh the schedule charges EX1
    assert raise_.robot_type == "excavator" and raise_.makespan_h == 7
    assert raise_.detail == "EX1 needs 6,200 Wh (has 5,000 Wh): feasible at 7 h"
    assert add.robot_type == "excavator" and add.battery_wh == 5000.0 and add.makespan_h == 4
    assert add.detail == "one more excavator (EX2, 5,000 Wh like EX1): feasible at 4 h"
    for s in found:
        assert_proven(req, graph, s)
    # 50 Wh less than the figure is still infeasible: the amount is the solver's own use
    shy = req.model_copy(update={"robots": [robot("EX1", "excavator", 6150)]})
    assert isinstance(solve_schedule(shy, graph, SolveOptions()), Failure)


def test_raise_battery_counts_the_legs_the_model_charges():
    """3000 + 3195 Wh of work in a zone 10 cells from the depot: EX1 drives there once (5 Wh)
    and works both tasks in place, so the planning model, the checker and the simulator all
    charge 6,200 Wh, which is the figure, rounded up to 50 Wh. (The transit allowance charged
    the 5 Wh before each task, 6,205 Wh, and the advice said 6,250 Wh.)"""
    site = Site(depot=(20, 8), zones=[Zone(id="Z1", center=(20, 18))])
    req = reqs([robot("EX1", "excavator", 5000)], horizon_h=100, site=site)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 3000, "Z1"),
                             task("T2", "excavator", 3, 3195, "Z1")])
    failure = solve_schedule(req, graph, SolveOptions())
    assert isinstance(failure, Failure) and failure.conflicts == ["energy:EX1"]
    assert failure.hint == "EX1 would need at least 6,200 Wh, 5 Wh of it travel, but has 5,000 Wh."
    found = suggest_relaxations(req, graph, failure)
    raise_ = next(s for s in found if s.kind == "raise_battery")
    assert raise_.battery_wh == 6200.0
    assert_proven(req, graph, raise_)
    short = req.model_copy(update={"robots": [robot("EX1", "excavator", 6199.999)]})
    assert isinstance(solve_schedule(short, graph, SolveOptions()), Failure)


def test_locked_tasks_stay_on_their_robot_while_advice_is_verified():
    """A replan: T1 is underway on EX1 and alone exceeds its remaining battery. A spare
    cannot take it over (the lock holds), so only the battery figure is offered."""
    req = reqs([robot("EX1", "excavator", 5000)], horizon_h=100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 6, 6000, "Z1")])
    locked = [ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=6)]
    failure = solve_schedule(req, graph, SolveOptions(), locked=locked)
    assert isinstance(failure, Failure) and failure.conflicts == ["energy:EX1"]
    found = suggest_relaxations(req, graph, failure, locked=locked)
    assert [(s.kind, s.robot_id, s.battery_wh) for s in found] == [("raise_battery", "EX1", 6000.0)]
    assert found[0].makespan_h == 6


# ----------------------------------------------------------------------------- capability
def test_capability_missing_adds_a_robot_of_the_new_type_sized_to_its_work():
    req = reqs([robot("EX1", "excavator", 5000)], horizon_h=100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 1000, "Z1"),
                             task("T2", "printer", 10, 2700, "Z1", after=("T1",))])
    failure = verdict(req, graph)
    assert failure.conflicts == ["capability:T2"]
    found = suggest_relaxations(req, graph, failure)
    assert [(s.kind, s.robot_type, s.robot_id) for s in found] == [("add_robot", "printer", "PR1")]
    (s,) = found
    assert s.battery_wh == 2700.0 and s.makespan_h == 14 and s.verified
    assert s.detail == "a printer (PR1, 2,700 Wh; the fleet has none): feasible at 14 h"
    assert_proven(req, graph, s)


# ----------------------------------------------------------------------------- no advice
def test_graphs_broken_beyond_resources_get_no_advice():
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h=50)
    cycle = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1", after=("T2",)),
                             task("T2", "excavator", 3, 100, "Z2", after=("T1",))])
    reference = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1", after=("T9",))])
    duplicate = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                                 task("T1", "excavator", 3, 100, "Z2")])
    for graph, kind in ((cycle, "cycle:"), (reference, "reference:"), (duplicate, "duplicate:")):
        failure = validate_graph(req, graph)
        assert failure is not None and failure.conflicts[0].startswith(kind)
        assert suggest_relaxations(req, graph, failure) == []

    fine = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")])
    for failure in (
        Failure(kind="validation", conflicts=["scope:S1"]),
        Failure(kind="validation", conflicts=["quantity:T1"]),
        Failure(kind="validation", conflicts=["missing:T2", "changed:T1"]),
        Failure(kind="validation", conflicts=["locked:T1"]),
        Failure(kind="validation", conflicts=["energy:excavator", "scope:S1"]),  # mixed
        Failure(kind="schema", conflicts=["schema:tasks"]),
        Failure(kind="llm_error"),
        Failure(kind="solver_timeout"),
    ):
        assert suggest_relaxations(req, fine, failure) == [], failure


def test_every_candidate_failing_yields_one_reduce_scope_naming_the_work():
    """Two robot types both over budget: no single change fixes it, so the advice is to cut
    scope, and it says what was tried and where the work is."""
    req = reqs([robot("EX1", "excavator", 1000), robot("HL1", "hauler", 1000)], horizon_h=100)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 18, 2016, "Z1", quantity=126, unit="m3", structure="S1"),
        task("T2", "hauler", 16, 1316, "Z2", after=("T1",), quantity=188, unit="m3",
             structure="S2"),
        task("T3", "excavator", 2, 300, "Z1", structure="S1", quantity=10, unit="m2"),
    ])
    failure = verdict(req, graph)
    # the hauler pool is short only because of T2 (one cause, one verdict); the excavator
    # pool is short beyond T1 (T3 on top), so it keeps its own
    assert failure.conflicts == ["energy_task:T1", "energy_task:T2", "energy:excavator"]
    found = suggest_relaxations(req, graph, failure)
    assert [s.kind for s in found] == ["reduce_scope"]
    (s,) = found
    assert not s.verified and s.makespan_h is None
    assert s.detail.startswith("None of the changes tried (")
    assert "a bigger battery on EX1" in s.detail and "a bigger battery on HL1" in s.detail
    # a cloned hauler cannot carry a task larger than every hauler battery: not worth a solve
    assert "one more excavator" in s.detail and "one more hauler" not in s.detail
    assert "T1 for S1 (126 m3, 18 h, 2,016 Wh)" in s.detail
    assert "T2 for S2 (188 m3, 16 h, 1,316 Wh)" in s.detail
    assert s.detail.endswith("so cut or defer scope there.")


# ----------------------------------------------------------------------------- ranking
def test_suggestions_are_ranked_smaller_change_first_and_capped(monkeypatch):
    """Four 3000 Wh tasks for excavators of 5000 / 4000 / 3000 Wh: each robot can carry one.
    Raising EX1 to 6000 Wh is a 20 % change, a fourth excavator a 33 % fleet change, EX2 to
    6000 Wh 50 %, EX3 to 6000 Wh 100 %."""
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 4000),
                robot("EX3", "excavator", 3000)], horizon_h=100)
    graph = TaskGraph(tasks=[task(f"T{i}", "excavator", 2, 3000, f"Z{i}") for i in range(1, 5)])
    failure = Failure(kind="infeasible", conflicts=["energy:EX1", "energy:EX2", "energy:EX3"])
    found = suggest_relaxations(req, graph, failure)
    assert [(s.kind, s.robot_id, s.battery_wh, s.makespan_h) for s in found] == [
        ("raise_battery", "EX1", 6000.0, 4),
        ("add_robot", "EX4", 5000.0, 2),       # a clone of the largest battery
        ("raise_battery", "EX2", 6000.0, 4),
        ("raise_battery", "EX3", 6000.0, 4),
    ]
    assert len(found) == MAX_SUGGESTIONS == 4
    for s in found:
        assert_proven(req, graph, s)
    # the same call again gives the same answer (one solver worker, no ties left to chance)
    assert suggest_relaxations(req, graph, failure) == found

    monkeypatch.setattr(relax_module, "MAX_SUGGESTIONS", 2)
    assert suggest_relaxations(req, graph, failure) == found[:2]


def test_changes_that_keep_the_deadline_come_before_extending_it():
    """A deadline verdict whose busiest type gains from a spare: the spare (original horizon)
    is listed before the later deadline, and both are proven."""
    req = reqs([robot("EX1", "excavator", 5000)], horizon_h=6)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 5, 100, "Z2")])
    failure = verdict(req, graph)
    found = suggest_relaxations(req, graph, failure)
    assert [(s.kind, s.makespan_h) for s in found] == [("add_robot", 5), ("extend_horizon", 9)]
    assert found[1].horizon_h == 9 and found[1].detail.startswith("finishing needs 9 h, 3 h past")
    for s in found:
        assert_proven(req, graph, s)


# ----------------------------------------------------------------------------- budget
class SlowSolver:
    """Stands in for solve_schedule: sleeps, records the options, answers infeasible."""

    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self.opts: list[SolveOptions] = []

    def __call__(self, req, graph, opts=None, locked=None):
        self.opts.append(opts)
        time.sleep(self.delay_s)
        return Failure(kind="infeasible", conflicts=["energy:EX1"])


def test_budget_is_respected_and_untried_candidates_are_not_blamed(monkeypatch):
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h=10)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 3000, "Z1"),
                             task("T2", "excavator", 3, 3200, "Z2")])
    failure = Failure(kind="infeasible", conflicts=["energy:EX1", "energy:EX2", "horizon"])
    slow = SlowSolver(delay_s=0.3)
    monkeypatch.setattr(relax_module, "solve_schedule", slow)

    t0 = time.perf_counter()
    found = suggest_relaxations(req, graph, failure, SolveOptions(time_limit_s=10.0), budget_s=0.5)
    elapsed = time.perf_counter() - t0
    # two solves fit (0.3 s each against 0.5 s), the rest of the five candidates are skipped
    assert len(slow.opts) == 2 and elapsed < 1.5
    # the budget ran out before every candidate was tried, so nothing is claimed either way
    assert found == []
    first, second = slow.opts
    # every worker, interleaved in a fixed order: the same answer on every call
    assert first.time_limit_s == pytest.approx(0.5, abs=0.01) and first.deterministic
    assert first.explain_infeasibility is False
    assert 0 < second.time_limit_s <= 0.2 + 0.05   # what was left of the budget

    # with time to spare every solve is capped at SOLVE_LIMIT_S and keeps the planner's
    # offset / positions; once everything was tried and failed, reduce_scope is the answer
    slow = SlowSolver(delay_s=0.0)
    monkeypatch.setattr(relax_module, "solve_schedule", slow)
    opts = SolveOptions(time_limit_s=30.0, start_offset_h=3,
                        robot_positions={"EX1": (1.0, 2.0)})
    found = suggest_relaxations(req, graph, failure, opts, budget_s=10.0)
    assert len(slow.opts) == 4   # a later deadline, EX1, EX2, one more excavator (deduplicated)
    assert all(o.time_limit_s == SOLVE_LIMIT_S and o.start_offset_h == 3
               and o.robot_positions == {"EX1": (1.0, 2.0)} for o in slow.opts)
    assert [s.kind for s in found] == ["reduce_scope"]


# ----------------------------------------------------------------------------- kinds
def test_kinds_restrict_what_is_tried_as_a_replan_needs():
    req, graph = battery_case()
    failure = verdict(req, graph)
    replan = suggest_relaxations(req, graph, failure, kinds=("extend_horizon", "add_robot"))
    assert [(s.kind, s.robot_id) for s in replan] == [("add_robot", "EX2")]
    # nothing applicable and reduce_scope not allowed: an empty answer, not a scope verdict
    assert suggest_relaxations(req, graph, failure, kinds=("extend_horizon",)) == []
    with pytest.raises(ValueError, match="unknown suggestion kind"):
        suggest_relaxations(req, graph, failure, kinds=("bigger_rocket",))
    assert set(KINDS) == {"extend_horizon", "add_robot", "raise_battery", "reduce_scope"}


def test_inputs_are_never_mutated():
    req, graph = battery_case()
    before = (req.model_dump(), graph.model_dump())
    failure = verdict(req, graph)
    assert len(suggest_relaxations(req, graph, failure)) == 2
    assert (req.model_dump(), graph.model_dump()) == before


# ----------------------------------------------------------------------------- fixture graph
def test_tight_battery_first_graph_gets_three_proven_changes():
    """The shipped infeasible graph (energy core on EX1 and EX2): either battery raised to
    5,950 Wh, or a third excavator, each proven feasible at 62 h of the 168 h window."""
    req = Requirements.model_validate_json((FIXTURES / "tight_battery" / "structure.json").read_text())
    graph = TaskGraph.model_validate_json((FIXTURES / "tight_battery" / "taskgraph_1.json").read_text())
    failure = solve_schedule(req, graph, SolveOptions())
    assert isinstance(failure, Failure) and set(failure.conflicts) == {"energy:EX1", "energy:EX2"}
    found = suggest_relaxations(req, graph, failure, SolveOptions())
    assert [(s.kind, s.robot_id, s.battery_wh, s.makespan_h) for s in found] == [
        ("raise_battery", "EX1", 5950.0, 62),
        ("raise_battery", "EX2", 5950.0, 62),
        ("add_robot", "EX3", 5000.0, 62),
    ]
    assert found[0].detail == "EX1 needs 5,950 Wh (has 5,000 Wh): feasible at 62 h"
    assert found[2].detail == "one more excavator (EX3, 5,000 Wh like EX1): feasible at 62 h"
    for s in found:
        assert_proven(req, graph, s)
    assert suggest_relaxations(req, graph, failure, SolveOptions()) == found   # deterministic


# ----------------------------------------------------------------------------- ids
def test_new_robot_ids_follow_the_fleets_naming():
    fleet = reqs([robot("EX1", "excavator", 1), robot("EX3", "excavator", 1),
                  robot("HL1", "hauler", 1)], horizon_h=1)
    assert new_robot_id(fleet, "excavator") == "EX4"      # after the highest number in use
    assert new_robot_id(fleet, "hauler") == "HL2"
    assert new_robot_id(fleet, "printer") == "PR1"         # a type the fleet lacks
    assert new_robot_id(fleet, "rover") == "RO1"           # an unknown type: its first letters
    clash = reqs([robot("EX1", "excavator", 1), robot("EX2", "hauler", 1)], horizon_h=1)
    assert new_robot_id(clash, "excavator") == "EX3"       # never an id another robot has
    named = reqs([robot("digger-a", "excavator", 1)], horizon_h=1)
    assert new_robot_id(named, "excavator") == "EX1"       # peers without numbers
    assert new_robot_id(fleet, "excavator", reserved=["EX4"]) == "EX5"   # a robot a replan lost

    req, graph = battery_case()   # the same through suggest_relaxations
    found = suggest_relaxations(req, graph, verdict(req, graph), reserved_ids=["EX2", "EX3"])
    assert [(s.kind, s.robot_id) for s in found] == [("raise_battery", "EX1"), ("add_robot", "EX4")]


# ----------------------------------------------------------------------------- formatting
def test_format_suggestions_lists_one_bullet_per_change():
    req, _ = battery_case()
    suggestions = [
        Suggestion(kind="raise_battery", detail="EX1 needs 6,200 Wh (has 5,000 Wh): feasible at 7 h",
                   robot_id="EX1", robot_type="excavator", battery_wh=6200, makespan_h=7,
                   verified=True),
        Suggestion(kind="reduce_scope", detail="Nothing works; cut T1 (4 h, 3,000 Wh)."),
    ]
    failed = PlanResult(requirements=req, success=False,
                        failure=Failure(kind="infeasible", conflicts=["energy:EX1"]),
                        suggestions=suggestions)
    assert format_suggestions(failed) == [
        "what would make it feasible:",
        "  - raise battery: EX1 needs 6,200 Wh (has 5,000 Wh): feasible at 7 h [CP-SAT verified]",
        "  - reduce scope: Nothing works; cut T1 (4 h, 3,000 Wh). [not solver-verified]",
    ]
    assert format_suggestions(PlanResult(requirements=req, success=False)) == []
    assert format_suggestions(PlanResult(requirements=req, success=True,
                                         suggestions=suggestions)) == []
    # the JSON shape the UI reads
    data = json.loads(failed.model_dump_json())
    assert data["suggestions"][0] == {
        "kind": "raise_battery", "detail": "EX1 needs 6,200 Wh (has 5,000 Wh): feasible at 7 h",
        "makespan_h": 7, "robot_type": "excavator", "robot_id": "EX1", "battery_wh": 6200.0,
        "horizon_h": None, "verified": True,
    }


# ----------------------------------------------------------------------------- review fixes
def test_a_timed_out_verification_solve_is_neither_tried_nor_blamed(monkeypatch):
    """CP-SAT answering UNKNOWN (``solver_timeout``) proves nothing: such a candidate is not
    one of "the changes tried", and reduce_scope is never emitted on a call that had one."""
    req = Requirements.model_validate_json((FIXTURES / "tight_battery" / "structure.json").read_text())
    graph = TaskGraph.model_validate_json((FIXTURES / "tight_battery" / "taskgraph_1.json").read_text())
    failure = Failure(kind="infeasible", conflicts=["energy:EX1", "energy:EX2"])
    # the review's reproduction: the real solver starved of time used to turn the three
    # verified suggestions into "none of the changes tried ... makes this graph feasible"
    monkeypatch.setattr(relax_module, "MIN_SOLVE_S", 0.0)
    monkeypatch.setattr(relax_module, "SOLVE_LIMIT_S", 1e-4)
    assert all(s.kind != "reduce_scope" for s in suggest_relaxations(req, graph, failure))
    monkeypatch.undo()

    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h=10)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 3000, "Z1"),
                             task("T2", "excavator", 3, 3200, "Z2")])
    timeout = Failure(kind="solver_timeout", hint="no answer in time")
    monkeypatch.setattr(relax_module, "solve_schedule", lambda *a, **k: timeout)
    assert suggest_relaxations(req, graph, failure, SolveOptions(), budget_s=5.0) == []
    # refuted, unknown, refuted: still no "none of the changes tried" claim
    answers = iter([Failure(kind="infeasible", conflicts=["energy:EX1"]), timeout,
                    Failure(kind="infeasible", conflicts=["energy:EX1"])])
    monkeypatch.setattr(relax_module, "solve_schedule", lambda *a, **k: next(answers))
    assert suggest_relaxations(req, graph, failure, SolveOptions(), budget_s=5.0) == []
    # every candidate refuted for real: the reduce_scope answer as before
    monkeypatch.setattr(relax_module, "solve_schedule",
                        lambda *a, **k: Failure(kind="infeasible", conflicts=["energy:EX1"]))
    found = suggest_relaxations(req, graph, failure, SolveOptions(), budget_s=5.0)
    assert [s.kind for s in found] == ["reduce_scope"]
    assert "a bigger battery on EX1, a bigger battery on EX2 and one more excavator" in found[0].detail


def test_raise_battery_reports_the_smallest_battery_that_works():
    """Three 2600 Wh digs in a chain on two 5000 Wh excavators: the makespan-optimal lifted
    schedule happily puts all three on the lifted robot (7800 Wh), but 5200 Wh is enough for
    either robot and 5150 Wh is not; the sentence says what is needed, not what one schedule
    happened to charge."""
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], horizon_h=100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 2, 2600, "Z1"),
                             task("T2", "excavator", 2, 2600, "Z1", after=("T1",)),
                             task("T3", "excavator", 2, 2600, "Z1", after=("T2",))])
    failure = solve_schedule(req, graph, SolveOptions())
    assert isinstance(failure, Failure) and set(failure.conflicts) == {"energy:EX1", "energy:EX2"}
    found = suggest_relaxations(req, graph, failure)
    assert [(s.kind, s.robot_id, s.battery_wh, s.makespan_h) for s in found] == [
        ("raise_battery", "EX1", 5200.0, 6), ("raise_battery", "EX2", 5200.0, 6),
        ("add_robot", "EX3", 5000.0, 6)]
    for s in found[:2]:
        assert s.detail == f"{s.robot_id} needs 5,200 Wh (has 5,000 Wh): feasible at 6 h"
        assert_proven(req, graph, s)
        shy = applied(req, s.model_copy(update={"battery_wh": 5150.0}))
        assert isinstance(solve_schedule(shy, graph, SolveOptions()), Failure)


def test_a_search_cut_short_reports_enough_never_a_need(monkeypatch):
    """Probes below the proven figure that time out end the search: the figure stands as
    sufficient and the sentence says so instead of claiming a need."""
    req, graph = battery_case()
    real = relax_module.solve_schedule

    def probes_time_out(req2, graph2, opts=None, locked=None):
        if max(r.battery_wh for r in req2.robots) < MAX_ENERGY_WH:
            return Failure(kind="solver_timeout")       # every probe, and the spare's solve
        return real(req2, graph2, opts, locked=locked)  # the lifted solve

    monkeypatch.setattr(relax_module, "solve_schedule", probes_time_out)
    found = suggest_relaxations(req, graph, verdict(req, graph))
    assert [(s.kind, s.battery_wh, s.makespan_h, s.verified) for s in found] == [
        ("raise_battery", 6200.0, 7, True)]
    assert found[0].detail == ("a battery of 6,200 Wh on EX1 is enough (has 5,000 Wh; a smaller one "
                               "may be, the search ran out of time): feasible at 7 h")


def test_rebase_suggestion_moves_a_replans_advice_to_absolute_hours():
    add = Suggestion(kind="add_robot", makespan_h=75, robot_type="excavator", robot_id="EX3",
                     battery_wh=4995.0, verified=True,
                     detail="one more excavator (EX3, 4995 Wh like EX1): feasible at 75 h")
    moved = rebase_suggestion(add, shift_h=10, deadline_h=336, spare_batteries={"excavator": 5000.0})
    assert (moved.makespan_h, moved.battery_wh, moved.horizon_h) == (85, 5000.0, None)
    assert moved.detail == "one more excavator (EX3, 5,000 Wh, a full battery): feasible at 85 h"
    later = Suggestion(kind="extend_horizon", makespan_h=64, horizon_h=64, verified=True,
                       detail="finishing needs 64 h, 2 h past the 62 h deadline (CP-SAT optimal)")
    moved = rebase_suggestion(later, shift_h=22, deadline_h=84)
    assert (moved.makespan_h, moved.horizon_h) == (86, 86)
    assert moved.detail == "finishing needs 86 h, 2 h past the 84 h deadline (CP-SAT optimal)"
    sized = Suggestion(kind="add_robot", makespan_h=14, robot_type="printer", robot_id="PR2",
                       battery_wh=2700.0, verified=True,
                       detail="a printer (PR2, 2700 Wh; the fleet has none): feasible at 14 h")
    moved = rebase_suggestion(sized, shift_h=3, deadline_h=100, spare_batteries={"printer": 4000.0})
    assert moved.battery_wh == 4000.0
    assert moved.detail == ("a printer (PR2, 4,000 Wh, a full battery; the fleet has none): "
                            "feasible at 17 h")
    # a "full" battery smaller than the proven one never lowers a figure; nothing to move stays
    assert rebase_suggestion(add, shift_h=0, deadline_h=336, spare_batteries={"excavator": 4000.0}) == add
    cut = Suggestion(kind="reduce_scope", detail="None of the changes tried works; cut T1.")
    assert rebase_suggestion(cut, shift_h=10, deadline_h=336) is cut
