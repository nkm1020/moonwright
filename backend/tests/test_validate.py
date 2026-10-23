"""Tests for the deterministic task-graph validator (F3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from moonwright.geometry import leg_mwh
from moonwright.llm.base import FIXTURES_DIR, SchemaMismatch, validate_into
from moonwright.pipeline.schedule import (
    SolveOptions,
    _instance,
    _travel_floor,
    budget_rows,
    solve_schedule,
)
from moonwright.pipeline.validate import (
    check_scope,
    missing_phases,
    needs_quantity,
    structure_kind,
    structure_tasks,
    tag_names,
    task_changes,
    task_phases,
    travel_floor,
    validate_graph,
)
from moonwright.schemas import (
    Event,
    Failure,
    Requirements,
    Robot,
    Site,
    Structure,
    Task,
    TaskGraph,
    Zone,
    to_mwh,
)

CHECK_ORDER = [
    "duplicate", "reference", "cycle", "capability", "quantity", "scope", "volume",
    "energy_task", "horizon_path", "horizon", "energy",
]


# ----------------------------------------------------------------------------- builders
def robot(id_: str, type_: str = "excavator", battery: float = 5000.0) -> Robot:
    return Robot(id=id_, type=type_, battery_wh=battery)


def task(
    id_: str,
    *,
    type_: str = "excavator",
    dur: int = 4,
    energy: float = 100.0,
    after: tuple[str, ...] = (),
    zone: str = "Z1",
    name: str | None = None,
    structure: str | None = None,
    unit: str | None = None,
    quantity: float | None = None,
) -> Task:
    return Task(id=id_, name=name or id_, robot_type=type_, duration_h=dur, energy_wh=energy,
                zone=zone, after=list(after), structure=structure, unit=unit,
                quantity=quantity)


def req(*robots: Robot, horizon: int = 336, zones: tuple[str, ...] = ("Z1", "Z2"),
        depot: tuple[int, int] | None = None) -> Requirements:
    """Zones Z1 (0, 10), Z2 (10, 10), ... on the default grid; the depot at the bottom centre
    (20, 1) unless given, so every robot has a leg to drive (``depot=(0, 10)`` puts the robots
    on Z1 and makes a single-zone graph travel-free)."""
    return Requirements(
        site=Site(zones=[Zone(id=z, center=(10 * i, 10)) for i, z in enumerate(zones)],
                  depot=depot),
        robots=list(robots) or [robot("EX1")],
        horizon_h=horizon,
    )


def graph(*tasks: Task) -> TaskGraph:
    return TaskGraph(tasks=list(tasks))


def kinds(failure: Failure) -> list[str]:
    """Conflict kinds in order of appearance, consecutive repeats collapsed."""
    out: list[str] = []
    for c in failure.conflicts:
        kind = c.split(":", 1)[0]
        if not out or out[-1] != kind:
            out.append(kind)
    return out


def must_fail(req_: Requirements, graph_: TaskGraph, **kwargs: object) -> Failure:
    failure = validate_graph(req_, graph_, **kwargs)  # type: ignore[arg-type]
    assert failure is not None, "expected a validation failure"
    assert failure.kind == "validation"
    assert len(failure.details) == len(failure.conflicts)
    assert len(set(failure.conflicts)) == len(failure.conflicts), "conflict ids must be unique"
    return failure


# ----------------------------------------------------------------------------- clean paths
# ----------------------------------------------------------------------------- scope guard
def test_check_scope_reports_dropped_and_redefined_tasks() -> None:
    base = graph(task("T1", dur=4, energy=800.0),
                 task("T2", type_="hauler", dur=3, energy=600.0, zone="Z2", after=("T1",)))
    assert check_scope(base, ["T1", "T2"], base) is None
    assert check_scope(base, [], base) is None

    # growing a task, adding an edge and adding a task are not scope changes
    grown = graph(task("T1", dur=5, energy=900.0),
                  task("T2", type_="hauler", dur=3, energy=600.0, zone="Z2", after=("T1", "T3")),
                  task("T3", type_="surveyor", dur=1, energy=10.0))
    assert check_scope(grown, ["T1", "T2"], base) is None
    assert task_changes(base.task("T1"), grown.task("T1")) == []

    changed = graph(task("T1", type_="hauler", dur=1, energy=1.0, zone="Z3"),
                    task("T2", type_="hauler", dur=3, energy=599.9995, zone="Z2"))
    failure = check_scope(changed, ["T1", "T2"], base)
    assert failure is not None and failure.kind == "validation"
    assert failure.conflicts == ["changed:T1", "changed:T2"]
    assert failure.details == [
        ("T1 changed: robot_type hauler (was excavator); zone Z3 (was Z1); duration_h 1 "
         "(needs 4); energy_wh 1 (needs 800)"),
        "T2 changed: dropped after T1",
    ]
    assert "redefined T1, T2" in failure.hint and "restore them" in failure.hint
    # a milli-Wh short counts, float noise above it does not
    assert task_changes(base.task("T2"), task("T2", type_="hauler", dur=3, energy=599.999,
                                              zone="Z2", after=("T1",))) == \
        ["energy_wh 599.999 (needs 600)"]
    assert task_changes(base.task("T2"), task("T2", type_="hauler", dur=3, energy=600.0 - 1e-9,
                                              zone="Z2", after=("T1",))) == []

    # a dropped task is reported once: the edge into it is not a change of its successor
    dropped = graph(task("T2", type_="hauler", dur=3, energy=600.0, zone="Z2"))
    failure = check_scope(dropped, ["T1", "T2"], base)
    assert failure is not None and failure.conflicts == ["missing:T1"]
    assert failure.details == ["T1 is required but missing from the graph"]
    assert failure.hint.startswith("The graph dropped T1")

    # without a baseline only the ids are checked (the planner's required_tasks contract)
    assert check_scope(changed, ["T1", "T2"]) is None
    failure = check_scope(dropped, ["T1", "T2", "T1"])
    assert failure is not None and failure.conflicts == ["missing:T1"]

    # both kinds together, conflicts and details aligned
    failure = check_scope(graph(task("T2", type_="hauler", dur=1, energy=600.0, zone="Z2")),
                          ["T1", "T2"], base)
    assert failure is not None
    assert failure.conflicts == ["missing:T1", "changed:T2"]
    assert failure.details == ["T1 is required but missing from the graph",
                               "T2 changed: duration_h 1 (needs 3)"]  # aligned with conflicts
    assert "dropped T1" in failure.hint and "redefined T2" in failure.hint


def test_clean_graph_returns_none() -> None:
    r = req(robot("EX1"), robot("EX2"), robot("HL1", "hauler", 3000), robot("CP1", "compactor", 3500))
    g = graph(
        task("T1", dur=6, energy=900),
        task("T2", dur=8, energy=1200, after=("T1",), zone="Z2"),
        task("T3", type_="hauler", dur=10, energy=1500, after=("T1",)),
        task("T4", type_="hauler", dur=10, energy=1400, after=("T2",), zone="Z2"),
        task("T5", type_="compactor", dur=12, energy=2000, after=("T3", "T4")),
        task("T6", dur=4, energy=500, after=("T5",)),
    )
    assert validate_graph(r, g) is None


def test_zone_missing_from_site_is_allowed() -> None:
    g = graph(task("T1", zone="Z9"), task("T2", zone="Z42", after=("T1",)))
    assert validate_graph(req(zones=("Z1",)), g) is None


def test_floating_point_noise_in_energy_sums_is_not_a_conflict() -> None:
    # 3 x 1000.2 Wh is exactly the 3000.6 Wh budget in decimal, but the float sum overshoots
    # by one ulp (even with Python's compensated sum()); that must not count as a conflict.
    r = req(robot("EX1", battery=3000.6), depot=(0, 10))   # on Z1: no transit to charge
    g = graph(*(task(f"T{i}", energy=1000.2) for i in range(3)))
    assert sum(t.energy_wh for t in g.tasks) > 3000.6
    assert validate_graph(r, g) is None


# ----------------------------------------------------------------------------- one kind each
def test_duplicate_ids_in_every_namespace() -> None:
    # Site refuses a duplicated zone id at the schema boundary (test_site_geometry_rules), so
    # the validator's zone namespace is reached through an unvalidated Site
    r = Requirements(robots=[robot("EX1"), robot("EX1")], horizon_h=336)
    r.site = Site.model_construct(name="LS-1", grid=(40, 40), depot=None, zones=[
        Zone(id="Z1", center=(0, 10)), Zone(id="Z1", center=(10, 10)), Zone(id="Z2", center=(20, 10)),
    ])
    g = graph(task("T1"), task("T2", after=("T1",)), task("T2", after=("T1",)))
    failure = must_fail(r, g)
    assert failure.conflicts == ["duplicate:EX1", "duplicate:T2", "duplicate:Z1"]
    assert failure.details == [
        "robot id 'EX1' appears 2 times",
        "task id 'T2' appears 2 times",
        "zone id 'Z1' appears 2 times",
    ]
    assert "task id 'T2' appears 2 times" in failure.hint


def test_reference_to_unknown_task() -> None:
    g = graph(task("T1"), task("T2", after=("T1", "T9")), task("T3", after=("TX", "T9")))
    failure = must_fail(req(), g)
    assert failure.conflicts == [
        "reference:T2.after=T9", "reference:T3.after=T9", "reference:T3.after=TX",
    ]
    assert "T3.after=TX" in failure.hint


def test_self_loop_is_a_cycle_not_a_reference() -> None:
    failure = must_fail(req(), graph(task("T1", after=("T1",)), task("T2", after=("T1",))))
    assert failure.conflicts == ["cycle:T1->T1"]


def test_cycle_path_is_reported_once_in_precedence_order() -> None:
    # T0 -> T1 -> T2 -> T3 -> T1 : the cycle is reachable from T0 and closed by one back edge.
    g = graph(
        task("T0"),
        task("T1", after=("T0", "T3")),
        task("T2", after=("T1",)),
        task("T3", after=("T2",)),
        task("T4", after=("T3",)),
    )
    failure = must_fail(req(), g)
    assert failure.conflicts == ["cycle:T1->T2->T3->T1"]
    assert "T1->T2->T3->T1" in failure.hint


def test_two_disjoint_cycles_both_reported() -> None:
    g = graph(
        task("T5", after=("T6",)), task("T6", after=("T5",)),
        task("T1", after=("T2",)), task("T2", after=("T1",)),
    )
    failure = must_fail(req(), g)
    assert failure.conflicts == ["cycle:T1->T2->T1", "cycle:T5->T6->T5"]
    assert "cycles" in failure.hint


def test_capability_missing_type_is_the_only_conflict() -> None:
    # The crane task has absurd energy, but energy_task / horizon / energy must skip a type
    # with no robots (and must not divide by zero); only `capability` is reported.
    r = req(robot("EX1"), robot("HL1", "hauler", 3000))
    g = graph(task("T1"), task("T2", type_="crane", energy=1e7, after=("T1",)),
              task("T3", type_="crane", energy=1e7))
    failure = must_fail(r, g)
    assert failure.conflicts == ["capability:T2", "capability:T3"]
    assert "crane" in failure.hint and "excavator and hauler" in failure.hint


def test_energy_task_exceeds_largest_battery_of_its_type() -> None:
    r = req(robot("EX1", battery=5000), robot("EX2", battery=3000))
    g = graph(task("T1", energy=5500), task("T2", energy=4000), task("T3", energy=100))
    failure = must_fail(r, g)
    assert "energy_task:T1" in failure.conflicts
    assert "energy_task:T2" not in failure.conflicts  # 4000 Wh fits EX1's 5000 Wh
    assert "energy:excavator" in failure.conflicts  # 9600 Wh total > 8000 Wh fleet, as well
    assert "5,500 Wh" in failure.hint and "5,000 Wh" in failure.hint


def test_energy_task_alone_when_fleet_total_fits() -> None:
    r = req(robot("EX1", battery=5000), robot("EX2", battery=3000))
    g = graph(task("T1", energy=5500), task("T2", energy=1000))
    failure = must_fail(r, g)
    assert failure.conflicts == ["energy_task:T1"]


def test_critical_path_is_the_longest_chain_by_duration() -> None:
    r2 = req(robot("EX1"), robot("EX2"), horizon=30)
    g = graph(
        task("T1", dur=10),
        task("T2", dur=5, after=("T1",)),
        task("T3", dur=20, after=("T1",)),
        task("T4", dur=1, after=("T2", "T3")),
        # longer by task count (4 tasks) but only 4 h: must not be chosen
        task("T5", dur=1), task("T6", dur=1, after=("T5",)),
        task("T7", dur=1, after=("T6",)), task("T8", dur=1, after=("T7",)),
    )
    failure = must_fail(r2, g)
    assert failure.conflicts == ["horizon_path:T1->T3->T4"]
    assert "31 h" in failure.hint and "30 h" in failure.hint
    # a chain equal to the horizon is fine
    assert validate_graph(req(robot("EX1"), robot("EX2"), horizon=31), g) is None


def test_type_horizon_lower_bound_uses_ceil_and_names_the_type() -> None:
    two = (robot("EX1"), robot("EX2"))
    fits = graph(task("T1", dur=200), task("T2", dur=200), task("T3", dur=272))  # 672 / 2 = 336
    assert validate_graph(req(*two), fits) is None
    over = graph(task("T1", dur=200), task("T2", dur=200), task("T3", dur=273))  # ceil(336.5)
    failure = must_fail(req(*two), over)
    assert failure.conflicts == ["horizon"]  # no chain exceeds the horizon on its own
    assert "excavator" in failure.hint and "337 h" in failure.hint and "336 h" in failure.hint


def test_type_energy_sum_exceeds_fleet_batteries() -> None:
    r = req(robot("EX1"), robot("EX2"), robot("HL1", "hauler", 3000))
    g = graph(task("T1", energy=4000), task("T2", energy=4000), task("T3", energy=3000),
              task("T4", type_="hauler", energy=2500))
    failure = must_fail(r, g)
    assert failure.conflicts == ["energy:excavator"]
    assert "excavator" in failure.hint and "11,000 Wh" in failure.hint and "10,000 Wh" in failure.hint


# ----------------------------------------------------------------------------- combinations
def _messy() -> tuple[Requirements, TaskGraph]:
    r = req(robot("EX1", battery=5000), robot("HL1", "hauler", 3000), horizon=100)
    g = graph(
        task("T1", dur=60, energy=6000, after=("T2",)),  # energy_task + cycle with T2
        task("T2", dur=60, energy=100, after=("T1", "T9")),  # reference T9, cycle
        task("T2", dur=10, energy=100),  # duplicate id
        task("T3", type_="crane", dur=5, energy=50),  # capability
        task("T4", type_="hauler", dur=90, energy=1000, zone="Z2"),
        task("T5", type_="hauler", dur=90, energy=2500, zone="Z2"),  # hauler: 180 h, 3500 Wh
    )
    return r, g


def test_multiple_conflicts_are_all_collected_in_check_order() -> None:
    failure = must_fail(*_messy())
    # every kind except horizon_path (critical path is skipped while the graph has a cycle),
    # quantity (no task states one), scope and volume (whole-plan checks, no structures here)
    assert kinds(failure) == [k for k in CHECK_ORDER
                              if k not in ("horizon_path", "quantity", "scope", "volume")]
    assert failure.conflicts == [
        "duplicate:T2",
        "reference:T2.after=T9",
        "cycle:T1->T2->T1",
        "capability:T3",
        "energy_task:T1",
        "horizon",
        "energy:excavator",
        "energy:hauler",
    ]
    # one sentence per conflict group, joined with spaces
    sentences = failure.hint.split(". ")
    assert len(sentences) == 7 and failure.hint.endswith(".")
    assert "excavator work (130 h over 1 robot" in failure.hint
    assert "hauler work (180 h over 1 robot" in failure.hint


def test_cycle_suppresses_critical_path_but_not_other_checks() -> None:
    r = req(robot("EX1"), robot("EX2"), horizon=50)
    g = graph(
        task("T1", dur=40), task("T2", dur=40, after=("T1",)),  # chain 80 h > horizon
        task("T3", after=("T4",)), task("T4", after=("T3",)),
    )
    failure = must_fail(r, g)
    assert failure.conflicts == ["cycle:T3->T4->T3"]
    assert validate_graph(r, graph(task("T1", dur=40), task("T2", dur=40, after=("T1",)))) \
        .conflicts == ["horizon_path:T1->T2"]


def test_deterministic_order_and_sorted_within_group() -> None:
    r, g = _messy()
    first = must_fail(r, g)
    second = must_fail(r, g)
    assert first == second
    for kind in CHECK_ORDER:
        ids = [c for c in first.conflicts if c.split(":", 1)[0] == kind]
        assert ids == sorted(ids), kind
    # declaration order does not change the verdict: cycles are rotated to a canonical
    # start and ids are sorted inside every group
    reversed_graph = TaskGraph(tasks=list(reversed(g.tasks)))
    assert must_fail(r, reversed_graph).conflicts == first.conflicts


@pytest.mark.parametrize("horizon, expected", [(130, None), (129, ["horizon"])])
def test_type_horizon_boundary_single_robot(horizon: int, expected: list[str] | None) -> None:
    g = graph(task("T1", dur=65), task("T2", dur=65))
    failure = validate_graph(req(robot("EX1"), horizon=horizon), g)
    assert (None if failure is None else failure.conflicts) == expected


# ----------------------------------------------------------------------------- milli-Wh unit
def test_energy_checks_are_exact_to_a_milli_wh() -> None:
    # replans hand over 3-dp task energies and 2-dp batteries: a budget met exactly passes ...
    r = req(robot("EX1", battery=4200.25), depot=(0, 10))   # on Z1: no transit to charge
    assert validate_graph(r, graph(task("T1", energy=2100.125), task("T2", energy=2100.125))) is None
    # ... and one milli-Wh over is a conflict, quoted with the figures that were compared
    failure = must_fail(r, graph(task("T1", energy=2100.126), task("T2", energy=2100.125)))
    assert failure.conflicts == ["energy:excavator"]
    assert "4,200.251 Wh" in failure.hint and "4,200.25 Wh" in failure.hint
    assert failure.details == [
        "excavator tasks need 4,200.251 Wh; its 1 robot carries 4,200.25 Wh"]
    failure = must_fail(r, graph(task("T1", energy=4200.251)))
    assert failure.conflicts == ["energy_task:T1"]   # one cause, one verdict (see below)
    assert failure.details[0] == (
        "T1 needs 4,200.251 Wh but the largest excavator battery is 4,200.25 Wh")
    assert "T1 (4,200.251 Wh against the largest excavator battery of 4,200.25 Wh)" in failure.hint
    assert failure.hint.endswith(
        "The fleet's 1 excavator carries 4,200.25 Wh in all, so splitting T1 alone cannot fit "
        "it: keep the full work unless the brief allows less.")
    # the decimals are the fewest that keep the two figures apart, never float noise
    failure = must_fail(r, graph(task("T1", energy=4200.3)))
    assert failure.details[0] == (
        "T1 needs 4,200.3 Wh but the largest excavator battery is 4,200.25 Wh")
    failure = must_fail(r, graph(task("T1", energy=4201)))
    assert failure.details[0] == "T1 needs 4,201 Wh but the largest excavator battery is 4,200 Wh"


# ----------------------------------------------------------------------------- travel
def test_energy_checks_charge_the_travel_no_schedule_avoids() -> None:
    """Live (phase 3e): a repair brought the printer to 3,999 of 4,000 Wh, the validator
    passed it and CP-SAT failed it at 4,037 Wh with travel, one repair round wasted, so both
    energy checks charge travel. Since CP-SAT charges the legs of the order it picks (phase
    3k), they charge only what no order avoids (``travel_floor``): per task the least leg into
    its zone, per type the cheapest tree from the robots' starts through its zones; named,
    and quoted through ``moonwright.text`` with "at least"."""
    r = req(robot("PR1", "printer", 4000), robot("EX1"))
    # Z1 is 21.93 cells from the depot (20, 1): 10.966 Wh, the second task there drives nothing
    leg = leg_mwh((20, 1), (0, 10))
    assert leg == 10966
    assert travel_floor(r, graph(task("T1", type_="printer"))) == ([leg], {"printer": leg})
    g = graph(task("T1", type_="printer", energy=2000), task("T2", type_="printer", energy=1975))
    assert validate_graph(r, g) is None                        # 3,975 + 11 Wh fits 4,000 Wh
    g = graph(task("T1", type_="printer", energy=2000), task("T2", type_="printer", energy=1990))
    failure = must_fail(r, g)                                  # 3,990 Wh of work passed before 3e
    assert failure.conflicts == ["energy:printer"]
    assert failure.details == [
        "printer tasks need at least 4,001 Wh, 11 Wh of it travel; its 1 robot carries 4,000 Wh"]
    assert failure.hint == (
        "Battery budgets are exceeded per robot type: printer tasks need at least 4,001 Wh, "
        "3,990 Wh of work and 11 Wh of travel, but its 1 robot carries only 4,000 Wh combined; "
        "reduce the quantities if the brief allows it or move work to other robot types.")
    # a single task is charged the leg to its zone, against the largest battery of its type.
    # The printer pool is short as well, but only because of T1: one cause, one verdict, and
    # the hint says what the pool verdict would have said (splitting cannot fit it)
    failure = must_fail(r, graph(task("T1", type_="printer", energy=3995)))
    assert failure.conflicts == ["energy_task:T1"]
    assert failure.details == [
        ("T1 needs at least 4,006 Wh, 11 Wh of it travel, but the largest printer battery is "
         "4,000 Wh")]
    assert failure.hint == (
        "No single robot can power T1 (3,995 Wh of work plus at least 11 Wh of travel, "
        "4,006 Wh against the largest printer battery of 4,000 Wh); split such tasks into "
        "smaller quantities or move the work to another robot type. The fleet's 1 printer "
        "carries 4,000 Wh in all, so splitting T1 alone cannot fit it: keep the full work "
        "unless the brief allows less.")
    # a pool overrun with a cause of its own (T2's 500 Wh on top) keeps its verdict, and the
    # task's hint no longer speaks for the pool
    failure = must_fail(r, graph(task("T1", type_="printer", energy=3995),
                                 task("T2", type_="printer", energy=500)))
    assert failure.conflicts == ["energy_task:T1", "energy:printer"]
    assert "in all" not in failure.hint
    assert failure.details[1] == (
        "printer tasks need at least 4,506 Wh, 11 Wh of it travel; its 1 robot carries 4,000 Wh")
    # with a second printer the pool fits and only the oversized task is a verdict
    two_printers = req(robot("PR1", "printer", 4000), robot("PR2", "printer", 4000), robot("EX1"))
    failure = must_fail(two_printers, graph(task("T1", type_="printer", energy=3995),
                                            task("T2", type_="printer", energy=500)))
    assert failure.conflicts == ["energy_task:T1"] and "in all" not in failure.hint
    # two zones: each task's least leg is the one from the depot, and the type's floor is the
    # cheapest way to reach both (depot -> Z2 -> Z1, 6.727 + 5 Wh), not the sum of the legs
    far = graph(task("T1", type_="printer"), task("T2", type_="printer", zone="Z2"))
    to_z2 = leg_mwh((20, 1), (10, 10))
    assert travel_floor(r, far) == ([leg, to_z2], {"printer": to_z2 + leg_mwh((10, 10), (0, 10))})
    # robots placed elsewhere (a replan) change the first leg; a type without robots has none
    at_z1 = {"PR1": (0.0, 10.0)}
    assert travel_floor(r, graph(task("T1", type_="printer")), at_z1) == ([0], {"printer": 0})
    assert travel_floor(r, graph(task("T1", type_="crane"))) == ([0], {})
    # with two robots of a type the nearer one counts: never more than CP-SAT would demand
    two = req(robot("PR1", "printer", 4000), robot("PR2", "printer", 4000))
    assert travel_floor(two, graph(task("T1", type_="printer")), {"PR2": (0.0, 10.0)}) == \
        ([0], {"printer": 0})


def test_validator_travel_matches_the_solver_on_the_tight_battery_fixture() -> None:
    """The validator's pool figure is the solver's: the least travel the type's robots need to
    reach its zones (``schedule._travel_floor``), so the pool it checks is the ``budget_rows``
    "type" row to the milli-Wh, and a pool one milli-Wh short is flagged with the figures the
    solver would quote."""
    r = Requirements.model_validate_json((FIXTURES_DIR / "tight_battery/structure.json").read_text())
    g = TaskGraph.model_validate_json((FIXTURES_DIR / "tight_battery/taskgraph_1.json").read_text())
    excavator = [i for i, t in enumerate(g.tasks) if t.robot_type == "excavator"]
    inst = _instance(r, g, SolveOptions(), [])
    legs, floors = travel_floor(r, g)
    assert floors["excavator"] == _travel_floor(inst, ["EX1", "EX2"], excavator, hours=False)
    assert validate_graph(r, g, coverage=True) is None   # 9,915 Wh fit the 10,000 Wh pool ...
    verdict = solve_schedule(r, g, SolveOptions(time_limit_s=5.0, num_workers=4, core_time_limit_s=3.0))
    assert isinstance(verdict, Failure) and verdict.conflicts == ["energy:EX1", "energy:EX2"]
    [row] = budget_rows(r, g, verdict)                   # ... but no split fits one battery
    demanded = to_mwh(row["demanded_wh"])
    # the depot (20, 8) to Z4 (8.2 cells) and to Z1 (10 cells), then Z1 to Z2 (12.6 cells)
    assert demanded == 9915449 and to_mwh(row["travel_wh"]) == floors["excavator"] == 15449
    assert demanded == sum(to_mwh(g.tasks[i].energy_wh) for i in excavator) + floors["excavator"]
    # the per-task legs are each the leg from the depot: no order reaches a zone for less
    assert [legs[i] for i in excavator] == [leg_mwh((20, 8), inst.cells[i]) for i in excavator]

    def with_pool(mwh_each: int) -> Requirements:
        return r.model_copy(update={"robots": [
            rb.model_copy(update={"battery_wh": mwh_each / 1000}) if rb.type == "excavator" else rb
            for rb in r.robots]})

    assert validate_graph(with_pool(4957725), g) is None          # pool 9,915.450 >= 9,915.449
    failure = must_fail(with_pool(4957724), g)                    # pool 9,915.448: one short
    assert failure.conflicts == ["energy:excavator"]
    assert failure.details == [
        ("excavator tasks need at least 9,915.449 Wh, 15 Wh of it travel; its 2 robots carry "
         "9,915.448 Wh")]
    assert "excavator tasks need at least 9,915.449 Wh, 9,900 Wh of work and 15 Wh of travel, " \
           "but its 2 robots carry only 9,915.448 Wh combined" in failure.hint
    # a replan passes the robots' positions to both: the figures still agree
    positions = {"EX1": (20.0, 18.0), "EX2": (8.0, 22.0), "HL1": (32.0, 22.0)}
    inst = _instance(r, g, SolveOptions(robot_positions=positions), [])
    assert travel_floor(r, g, positions)[1]["excavator"] == \
        _travel_floor(inst, ["EX1", "EX2"], excavator, hours=False)


# ----------------------------------------------------------------------------- schema hardening
def test_schema_rejects_non_finite_and_out_of_range_numbers() -> None:
    for battery in (float("inf"), float("nan"), 1e7 + 1):
        with pytest.raises(ValidationError):
            Robot(id="EX1", type="excavator", battery_wh=battery)
    with pytest.raises(ValidationError):
        Robot(id="EX1", type="excavator", battery_wh=100, speed_cells_per_h=1e4 + 1)
    for energy in (float("nan"), float("-inf"), 1e7 + 1):
        with pytest.raises(ValidationError):
            task("T1", energy=energy)
    with pytest.raises(ValidationError):
        task("T1", dur=10_001)
    with pytest.raises(ValidationError):
        Requirements(robots=[robot("EX1")], horizon_h=100_001)
    with pytest.raises(ValidationError):
        Event(t_h=float("inf"), kind="robot_failure")
    with pytest.raises(ValidationError):
        Structure(id="S1", type="landing_pad", diameter_m=float("inf"))
    with pytest.raises(ValidationError):  # NaN / Infinity tokens in JSON are a schema error too
        Requirements.model_validate_json(
            '{"robots": [{"id": "EX1", "type": "x", "battery_wh": Infinity}], "horizon_h": 10}')
    # the bounds themselves are allowed
    assert Robot(id="EX1", type="excavator", battery_wh=1e7, speed_cells_per_h=1e4).battery_wh == 1e7
    assert task("T1", dur=10_000, energy=1e7).duration_h == 10_000
    assert Requirements(robots=[robot("EX1")], horizon_h=100_000).horizon_h == 100_000
    assert Structure(id="S1", type="landing_pad", diameter_m=20).dims() == {"diameter_m": 20.0}


@pytest.mark.parametrize("bad", ["T 1", "A->B", "a:b", "", "T1\n", "zone/1", "T1 "])
def test_schema_rejects_ids_the_conflict_grammar_cannot_carry(bad: str) -> None:
    with pytest.raises(ValidationError):
        Task(id=bad, name="n", robot_type="excavator", duration_h=1, energy_wh=1, zone="Z1")
    with pytest.raises(ValidationError):
        task("T1", zone=bad)
    with pytest.raises(ValidationError):
        task("T1", after=(bad,))
    with pytest.raises(ValidationError):
        Robot(id=bad, type="excavator", battery_wh=1)
    with pytest.raises(ValidationError):
        Zone(id=bad, center=(0, 0))
    with pytest.raises(ValidationError):
        Structure(id=bad, type="landing_pad")
    with pytest.raises(ValidationError):
        Structure(id="S1", type="landing_pad", zone=bad)


@pytest.mark.parametrize("good", ["T1", "pad-01", "z.north", "EX_2", "a", "berm.key-trench_2"])
def test_schema_accepts_plain_ids(good: str) -> None:
    assert task(good, zone=good, after=(good,)).id == good
    assert Robot(id=good, type="excavator", battery_wh=1).id == good
    assert Zone(id=good, center=(0, 0)).id == good
    assert Structure(id=good, type="landing_pad", zone=good).zone == good


def test_fixtures_still_validate_under_the_hardened_schema() -> None:
    dirs = sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())
    assert dirs, "no fixtures found"
    for d in dirs:
        Requirements.model_validate_json((d / "structure.json").read_text())
        graphs = sorted(d.glob("taskgraph_*.json"))
        assert graphs, f"no task graph fixtures in {d}"
        for g in graphs:
            TaskGraph.model_validate_json(g.read_text())


# ----------------------------------------------------------------------------- site geometry
def test_site_geometry_accepts_the_bounds() -> None:
    site = Site(grid=(40, 40), depot=(20, 0), zones=[
        Zone(id="Z1", center=(0, 0), radius=1), Zone(id="Z2", center=(39, 39), radius=10),
        Zone(id="Z3", center=(1, 1), radius=5),   # overlaps Z1: allowed, the prompt discourages it
    ])
    assert [z.id for z in site.zones] == ["Z1", "Z2", "Z3"]
    assert Site.max_zone_radius((40, 40)) == 10
    assert Site.max_zone_radius((8, 20)) == 2 and Site.max_zone_radius((3, 3)) == 1
    assert Site(grid=(8, 20), zones=[Zone(id="Z1", center=(7, 19), radius=2)]).grid == (8, 20)
    assert Site(grid=(3, 3), zones=[Zone(id="Z1", center=(2, 2), radius=1)]).zones[0].radius == 1
    assert Site().depot is None and Site(depot=(0, 39)).depot == (0, 39)
    for d in sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir()):
        Requirements.model_validate_json((d / "structure.json").read_text())  # all inside


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"zones": [Zone(id="Z1", center=(40, 40), radius=5)]},
     "zone Z1: centre (40, 40) is outside the 40x40 grid (0 <= x < 40, 0 <= y < 40)"),
    ({"zones": [Zone(id="Z1", center=(-1, 5))]}, "zone Z1: centre (-1, 5) is outside"),
    ({"zones": [Zone(id="Z1", center=(5, 5), radius=11)]},
     "zone Z1: radius 11 is outside 1..10 (at most a quarter of the grid's shorter side)"),
    ({"grid": (8, 20), "zones": [Zone(id="Z1", center=(4, 4), radius=3)]},
     "zone Z1: radius 3 is outside 1..2"),
    ({"zones": [Zone(id="Z1", center=(5, 5)), Zone(id="Z1", center=(20, 20))]},
     "zone Z1: id is used by more than one zone"),
    ({"depot": (40, 0)}, "depot (40, 0) is outside the 40x40 grid"),
    ({"depot": (0, -1)}, "depot (0, -1) is outside the 40x40 grid"),
    ({"grid": (0, 40)}, "grid 0x40 must be at least 1x1"),
])
def test_site_geometry_rules_name_the_offender(kwargs: dict, message: str) -> None:
    with pytest.raises(ValidationError) as info:
        Site(**kwargs)
    assert message in str(info.value)


def test_site_geometry_reports_every_violation_at_once() -> None:
    # the live structure stage shows zone (40, 40) with radius 40 and a depot off-grid: the
    # re-ask must carry all of it, naming each zone
    with pytest.raises(ValidationError) as info:
        Site(zones=[Zone(id="Z1", center=(40, 40), radius=40), Zone(id="Z2", center=(10, 50))],
             depot=(99, 99))
    text = str(info.value)
    for part in ("zone Z1: centre (40, 40)", "zone Z1: radius 40", "zone Z2: centre (10, 50)",
                 "depot (99, 99)"):
        assert part in text
    with pytest.raises(ValidationError):
        Zone(id="Z1", center=(5, 5), radius=0)  # Zone's own bound


def test_structure_stage_reports_soft_geometry_warnings() -> None:
    """What the schema allows but the prompt discourages is reported on the structure step's
    payload for the UI, never as a failure (the simulator copes with all of it)."""
    from moonwright.pipeline.structure import site_warnings

    for d in sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir()):
        assert site_warnings(Requirements.model_validate_json(
            (d / "structure.json").read_text())) == []
    messy = Requirements(
        site=Site(zones=[Zone(id="Z1", center=(10, 10), radius=5),
                         Zone(id="Z2", center=(16, 10), radius=4),      # 6 < 5 + 4: overlap
                         Zone(id="Z3", center=(30, 30), radius=3)]),
        structures=[Structure(id="S1", type="landing_pad", zone="Z1"),
                    Structure(id="S2", type="blast_berm", zone="Z9"),
                    Structure(id="S3", type="stockpile")],
        robots=[robot("EX1")], horizon_h=100,
    )
    assert site_warnings(messy) == [
        "zones Z1 and Z2 overlap (centres 6.0 cells apart, radii 5 + 4)",
        "structure S2 names zone Z9, which the site does not declare",
        "structure S3 has no zone",
        "no depot given; robots start at the bottom centre of the grid",
    ]


def test_site_geometry_error_reaches_the_structure_re_ask() -> None:
    """``validate_into`` is what every client re-asks with: the error line names the zone."""
    data = {"site": {"zones": [{"id": "Z1", "center": [40, 40], "radius": 40}], "depot": None},
            "robots": [{"id": "EX1", "type": "excavator", "battery_wh": 5000}], "horizon_h": 100}
    with pytest.raises(SchemaMismatch) as info:
        validate_into(Requirements, data)
    assert len(info.value.errors) == 1
    assert info.value.errors[0].startswith("site: Value error, zone Z1: centre (40, 40)")


# ----------------------------------------------------------------------------- coverage
def site_req(*robots: Robot, structures: tuple[tuple[str, str], ...] = (), horizon: int = 336,
             ) -> Requirements:
    """Requirements with zones Z1..Z5 and ``structures`` = ((id, type), ...) in zones Z1, Z2, ..."""
    zones = [Zone(id=f"Z{i}", center=(6 * i, 10)) for i in range(1, 6)]
    return Requirements(
        site=Site(zones=zones),
        structures=[Structure(id=sid, type=typ, zone=f"Z{i + 1}")
                    for i, (sid, typ) in enumerate(structures)],
        robots=list(robots), horizon_h=horizon,
    )


FLEET = (robot("EX1"), robot("HL1", "hauler", 3000), robot("CP1", "compactor", 3500),
         robot("PR1", "printer", 4000), robot("SV1", "surveyor", 1500))


def pad_tasks(*skip: str) -> list[Task]:
    """The four phases of a landing pad S1, minus the phases in ``skip``."""
    all_ = {
        "survey": task("T1", type_="surveyor", name="Survey and stake pad", structure="S1"),
        "excavate": task("T2", name="Excavate pad bowl", unit="m3", quantity=126,
                         structure="S1", after=("T1",)),
        "compact": task("T3", type_="compactor", name="Compact pad subgrade", unit="m2",
                        quantity=314, structure="S1", after=("T2",)),
        "sinter": task("T4", type_="printer", name="Sinter pad surface", unit="m2",
                       quantity=314, structure="S1", after=("T3",)),
    }
    kept = [t for phase, t in all_.items() if phase not in skip]
    present = {t.id for t in kept}
    return [t.model_copy(update={"after": [a for a in t.after if a in present]}) for t in kept]


def test_coverage_is_opt_in_and_landing_pad_needs_all_four_phases() -> None:
    r = site_req(*FLEET, structures=(("S1", "landing_pad"),))
    assert validate_graph(r, graph(*pad_tasks()), coverage=True) is None
    for phase, wording in (("survey", "survey"), ("excavate", "excavate or grade"),
                           ("compact", "compact"), ("sinter", "sinter")):
        g = graph(*pad_tasks(phase))
        assert validate_graph(r, g) is None, "coverage is off by default (replans)"
        failure = must_fail(r, g, coverage=True)
        assert failure.conflicts == ["scope:S1"]
        assert failure.details == [f"S1 (landing_pad) lacks {wording} task"]
        assert f"S1 (landing_pad) has no {wording} task" in failure.hint
        assert "tagged with the structure id" in failure.hint
    failure = must_fail(r, graph(*pad_tasks("compact", "sinter")), coverage=True)
    assert failure.details == ["S1 (landing_pad) lacks compact and sinter tasks"]


def test_a_pad_stripped_in_square_metres_is_dug_enough_for_the_kind_rule() -> None:
    """An instrument pad "stripped of fines, levelled and compacted" is graded in m2 (the
    prompt's unit for stripping): the pad kind accepts excavate OR grade, so a stripped-only
    pad is not a false scope conflict; a brief that lists excavate still requires the dig."""
    r = site_req(*FLEET, structures=(("S1", "instrument_pad"),))
    strip = task("T2", name="Strip pad fines", unit="m2", quantity=100, structure="S1",
                 after=("T1",))
    stripped = graph(*[t if t.id != "T2" else strip for t in pad_tasks()])
    assert validate_graph(r, stripped, coverage=True) is None
    asks_dig = r.model_copy(update={"structures": [
        r.structures[0].model_copy(update={"operations": ["excavate"]})]})
    failure = must_fail(asks_dig, stripped, coverage=True)
    assert failure.details == ["S1 (instrument_pad) lacks excavate task (its operations list excavate)"]


def test_phases_are_only_required_while_the_fleet_can_staff_them() -> None:
    no_printer = site_req(*FLEET[:3], FLEET[4], structures=(("S1", "landing_pad"),))
    assert validate_graph(no_printer, graph(*pad_tasks("sinter")), coverage=True) is None
    no_surveyor = site_req(*FLEET[:4], structures=(("S1", "landing_pad"),))
    assert validate_graph(no_surveyor, graph(*pad_tasks("survey")), coverage=True) is None
    # ... but a phase the fleet can staff is still required
    failure = must_fail(no_printer, graph(*pad_tasks("sinter", "compact")), coverage=True)
    assert failure.details == ["S1 (landing_pad) lacks compact task"]


def test_berm_road_and_foundation_phases() -> None:
    r = site_req(*FLEET, structures=(("S1", "blast_berm"), ("S2", "access_road"),
                                     ("S3", "habitat_foundation")))
    berm_haul = task("B1", type_="hauler", name="Haul spoil to berm line", unit="m3",
                     quantity=32, zone="Z1", structure="S1", after=("F1",))   # the pit's spoil
    berm_dig = task("B2", name="Cut berm key trench", unit="m3", quantity=30, zone="Z1",
                    structure="S1")
    berm_compact = task("B3", type_="compactor", name="Shape and compact berm", unit="m2",
                        quantity=240, zone="Z1", structure="S1")
    road_grade = task("R1", name="Grade road bed", unit="m2", quantity=480, zone="Z2",
                      structure="S2")
    road_compact = task("R2", type_="compactor", name="Compact road base", unit="m2",
                        quantity=480, zone="Z2", structure="S2")
    pit = task("F1", name="Excavate foundation pit", unit="m3", quantity=32, zone="Z3",
               structure="S3")
    pit_compact = task("F2", type_="compactor", name="Compact foundation base", unit="m2",
                       quantity=64, zone="Z3", structure="S3")
    full = graph(berm_haul, berm_dig, berm_compact, road_grade, road_compact, pit, pit_compact)
    assert validate_graph(r, full, coverage=True) is None
    # berm: excavate OR haul satisfies the first phase
    assert validate_graph(r, graph(berm_haul, berm_compact, road_grade, road_compact, pit,
                                   pit_compact), coverage=True) is None
    assert validate_graph(r, graph(berm_dig, berm_compact, road_grade, road_compact, pit,
                                   pit_compact), coverage=True) is None
    failure = must_fail(r, graph(berm_compact, road_grade, road_compact, pit, pit_compact),
                        coverage=True)
    assert failure.details == ["S1 (blast_berm) lacks excavate or haul task"]
    # road: an excavator counted in m3 with no grading verb is excavation, not grading
    cut = task("R1", name="Cut road bed", unit="m3", quantity=96, zone="Z2", structure="S2")
    failure = must_fail(r, graph(berm_haul, berm_compact, cut, road_compact, pit, pit_compact),
                        coverage=True)
    assert failure.conflicts == ["scope:S2"]
    assert failure.details == ["S2 (access_road) lacks grade task"]
    # foundation: excavate and compact
    failure = must_fail(r, graph(berm_haul, berm_compact, road_grade, road_compact, pit),
                        coverage=True)
    assert failure.details == ["S3 (habitat_foundation) lacks compact task"]
    # several structures at once: sorted by structure id, details aligned
    failure = must_fail(r, graph(berm_compact, road_compact, pit), coverage=True)
    assert failure.conflicts == ["scope:S1", "scope:S2", "scope:S3"]
    assert failure.details == ["S1 (blast_berm) lacks excavate or haul task",
                               "S2 (access_road) lacks grade task",
                               "S3 (habitat_foundation) lacks compact task"]


def test_unknown_structure_types_need_at_least_one_task() -> None:
    r = site_req(*FLEET, structures=(("S1", "landing_pad"), ("S2", "approach_apron")))
    pad = pad_tasks()
    failure = must_fail(r, graph(*pad), coverage=True)
    assert failure.conflicts == ["scope:S2"]
    assert failure.details == ["S2 (approach_apron) has no task"]
    strip = task("A1", name="Strip approach apron", unit="m2", quantity=240, zone="Z2",
                 structure="S2")
    assert validate_graph(r, graph(*pad, strip), coverage=True) is None
    # the type is matched by keyword, whatever the spelling around it
    assert structure_kind("Landing-Pad") == "landing_pad"
    assert structure_kind("protective_berm") == structure_kind("BLAST BERM") == "berm"
    assert structure_kind("haul road") == "road"
    assert structure_kind("habitat_foundation") == "foundation"
    assert structure_kind("approach_apron") is None and structure_kind("stockpile") is None


def test_untagged_tasks_in_the_structures_zone_count_for_it() -> None:
    r = site_req(*FLEET, structures=(("S1", "landing_pad"),))
    untagged = [t.model_copy(update={"structure": None}) for t in pad_tasks()]  # all in Z1
    assert validate_graph(r, graph(*untagged), coverage=True) is None
    assert [t.id for t in structure_tasks(r.structures[0], graph(*untagged))] == \
        ["T1", "T2", "T3", "T4"]
    elsewhere = [t.model_copy(update={"zone": "Z2"}) for t in untagged]
    failure = must_fail(r, graph(*elsewhere), coverage=True)
    assert failure.details == [
        "S1 (landing_pad) lacks survey, excavate or grade, compact and sinter tasks"]


def test_task_phases_combine_robot_type_unit_and_name() -> None:
    assert task_phases(task("T", type_="surveyor", name="As-built survey")) == {"survey"}
    assert task_phases(task("T", type_="hauler", name="Stage feedstock")) == {"haul"}
    assert task_phases(task("T", type_="compactor", name="Roll berm lift")) == {"compact"}
    assert task_phases(task("T", type_="printer", name="Pave the apron")) == {"sinter"}
    assert task_phases(task("T", name="Excavate bowl", unit="m3")) == {"excavate"}
    assert task_phases(task("T", name="Strip footprint", unit="m2")) == {"grade"}
    assert task_phases(task("T", name="Dig cable trench", unit="m")) == {"excavate"}
    # an excavator with nothing to go on is earthmoving of some kind
    assert task_phases(task("T", name="Prepare pad")) == {"excavate", "grade"}
    # name keywords add phases whatever the robot type (lenient union: a dropped phase is
    # only reported when nothing in the graph suggests it)
    assert task_phases(task("T", name="Compact berm lift")) == {"compact"}
    assert task_phases(task("T", name="Compact berm with blade")) == {"compact", "grade"}
    assert task_phases(task("T", type_="hauler", name="Haul and level fill")) == {"haul", "grade"}
    # word-prefix matching, not substring: "execute" is not a cut
    assert task_phases(task("T", type_="hauler", name="Execute delivery run")) == {"haul"}
    assert task_phases(task("T", type_="surveyor", name="Execute scan")) == {"survey"}


def test_repair_that_drops_a_structure_names_the_lost_tasks() -> None:
    r = site_req(*FLEET, structures=(("S1", "landing_pad"), ("S2", "blast_berm")))
    berm = [task("B1", type_="hauler", name="Haul spoil to berm", unit="m3", quantity=90,
                 zone="Z2", structure="S2", after=("T2",)),
            task("B2", type_="compactor", name="Compact berm", unit="m2", quantity=240,
                 zone="Z2", structure="S2")]
    before = graph(*pad_tasks(), *berm)
    assert validate_graph(r, before, coverage=True) is None
    after = graph(*pad_tasks())
    failure = must_fail(r, after, previous=before, coverage=True)
    assert failure.conflicts == ["scope:S2"]
    assert failure.details == ["S2 (blast_berm) had B1 and B2 before the repair and has no task now"]
    assert "S2 (blast_berm) lost every task it had (B1, B2)" in failure.hint
    # without the previous graph the same hole is reported by its missing phases
    assert must_fail(r, after, coverage=True).details == \
        ["S2 (blast_berm) lacks excavate or haul and compact tasks"]
    # a repair that keeps one berm task but drops the other is a phase hole, not a lost structure
    failure = must_fail(r, graph(*pad_tasks(), berm[0]), previous=before, coverage=True)
    assert failure.details == ["S2 (blast_berm) lacks compact task"]
    assert missing_phases(r, r.structures[1], [berm[0]]) == ["compact"]


def test_scope_sits_between_capability_and_energy_task_in_check_order() -> None:
    r = site_req(robot("EX1", battery=5000), structures=(("S1", "landing_pad"), ("S2", "berm")))
    g = graph(
        task("T1", type_="crane", name="Lift beacon", zone="Z1", structure="S1"),  # capability
        task("T2", energy=6000, name="Inspect pad surface", zone="Z1",
             structure="S1"),                        # energy_task; an inspection, no earthwork
        task("T3", name="Cut berm key trench", unit="m3", quantity=30, zone="Z2",
             structure="S2"),
    )
    failure = must_fail(r, g, coverage=True)
    assert kinds(failure) == ["capability", "scope", "energy_task", "energy"]
    # only the dig (excavate or grade) is required of the pad with this one-excavator fleet,
    # and T2 is an inspection pass by its name, neither excavation nor grading
    assert failure.conflicts == ["capability:T1", "scope:S1", "energy_task:T2",
                                 "energy:excavator"]
    assert failure.details[1] == "S1 (landing_pad) lacks excavate or grade task"
    assert must_fail(r, g, coverage=True) == failure   # deterministic


def test_fixture_graphs_pass_the_coverage_check() -> None:
    for d in sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir()):
        r = Requirements.model_validate_json((d / "structure.json").read_text())
        for path in sorted(d.glob("taskgraph_*.json")):
            g = TaskGraph.model_validate_json(path.read_text())
            assert validate_graph(r, g, coverage=True) is None, path
            for s in r.structures:
                assert structure_tasks(s, g), f"{path}: {s.id} has no task"


# ----------------------------------------------------------------------------- quantities
def test_physical_tasks_need_a_priced_quantity_once_the_graph_states_any() -> None:
    r = req(robot("EX1"), robot("HL1", "hauler", 3000), robot("SV1", "surveyor", 1500))
    bowl = task("T1", name="Excavate full pad bowl (188 m3)", dur=6, energy=500)
    survey = task("T2", type_="surveyor", name="Survey pad", unit="m2", quantity=314)
    # no quantity anywhere: a pre-rate-model graph, judged on its own numbers
    assert validate_graph(r, graph(bowl, task("T3", type_="surveyor", name="Survey pad"))) is None
    # one task states a quantity: the bowl must too
    failure = must_fail(r, graph(bowl, survey))
    assert failure.conflicts == ["quantity:T1"]
    assert failure.details == ["T1 (excavator: Excavate full pad bowl (188 m3)) has no quantity"]
    assert "excavator in m3 or m2" in failure.hint and "rate table" in failure.hint
    assert "inspection pass or a wait" in failure.hint
    cases = {
        "has a quantity of 0 m3": task("T1", name="Excavate bowl", unit="m3", quantity=0),
        "has a quantity but no unit": task("T1", name="Excavate bowl", quantity=120),
        "is counted in m2, which the rate table does not price for a hauler":
            task("T1", type_="hauler", name="Haul spoil", unit="m2", quantity=500),
    }
    for why, bad in cases.items():
        failure = must_fail(r, graph(bad, survey))
        assert failure.conflicts == ["quantity:T1"] and failure.details[0].endswith(why), why
    # exempt: surveyors, inspection passes and robot types the table does not know
    fleet = req(robot("EX1"), robot("SV1", "surveyor", 1500), robot("CR1", "crane", 2000))
    fine = graph(task("T1", name="Excavate bowl", unit="m3", quantity=126),
                 task("T2", type_="surveyor", name="Level check of the pad"),
                 task("T3", name="Inspect subgrade surface"),             # excavator, no digging
                 task("T4", type_="crane", name="Lift beacon", quantity=1, unit="ea"))
    assert validate_graph(fleet, fine, coverage=False) is None
    assert needs_quantity(bowl) and needs_quantity(cases["has a quantity of 0 m3"])
    assert not needs_quantity(fine.tasks[1]) and not needs_quantity(fine.tasks[2])
    assert not needs_quantity(fine.tasks[3])
    # the graph a repair answered keeps the rate model engaged even when the answer drops
    # every quantity (the rate model gives them back; the validator sees the result)
    quantified = graph(bowl.model_copy(update={"quantity": 188, "unit": "m3"}))
    assert validate_graph(r, graph(bowl)) is None
    assert must_fail(r, graph(bowl), previous=quantified).conflicts == ["quantity:T1"]
    # several tasks: sorted ids, details aligned, between capability and scope in the order
    messy = graph(task("T5", type_="crane", name="Lift beacon"),
                  task("T6", name="Excavate bowl"),
                  task("T7", type_="hauler", name="Haul spoil", unit="m2", quantity=10),
                  survey)
    failure = must_fail(r, messy)
    assert kinds(failure) == ["capability", "quantity"]
    assert failure.conflicts == ["capability:T5", "quantity:T6", "quantity:T7"]
    assert [c.split(":")[0] for c in failure.conflicts] == \
        sorted([c.split(":")[0] for c in failure.conflicts], key=CHECK_ORDER.index)


def test_structure_tags_resolve_leniently() -> None:
    r = site_req(*FLEET, structures=(("S1", "landing_pad"), ("S2", "blast_berm")))
    berm = [task("B1", type_="hauler", name="Haul spoil to berm", unit="m3", quantity=90,
                 zone="Z2", structure="S2", after=("T2",)),
            task("B2", type_="compactor", name="Compact berm", unit="m2", quantity=240,
                 zone="Z2", structure="S2")]
    for pad_tag, berm_tag in (("s1", "s2"), ("landing_pad", "blast_berm"),
                              ("Landing pad S1", "Blast berm S2")):
        tagged = ([t.model_copy(update={"structure": pad_tag}) for t in pad_tasks()]
                  + [t.model_copy(update={"structure": berm_tag}) for t in berm])
        assert validate_graph(r, graph(*tagged), coverage=True) is None, (pad_tag, berm_tag)
        served = structure_tasks(r.structures[0], graph(*tagged), r.structures)
        assert [t.id for t in served] == ["T1", "T2", "T3", "T4"]
    # a tag that names no structure at all falls back to the zone, like no tag
    stray = [t.model_copy(update={"structure": "pad-area"}) for t in pad_tasks()] + berm
    assert validate_graph(r, graph(*stray), coverage=True) is None
    # a tag that names the other structure does not count for this one's zone
    swapped = [t.model_copy(update={"structure": "S2"}) for t in pad_tasks()] + berm
    failure = must_fail(r, graph(*swapped), coverage=True)
    assert failure.conflicts == ["scope:S1"]
    assert failure.details == [
        "S1 (landing_pad) lacks survey, excavate or grade, compact and sinter tasks"]
    pad = r.structures[0]
    assert tag_names("S1", pad) and tag_names("s1 ", pad) and tag_names("Landing_Pad", pad)
    assert tag_names("Landing pad S1", pad) and not tag_names("S10", pad)
    assert not tag_names("pad", pad) and not tag_names("S2", pad)
