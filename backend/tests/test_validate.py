"""Tests for the deterministic task-graph validator (F3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from moonwright.llm.base import FIXTURES_DIR
from moonwright.pipeline.validate import check_scope, task_changes, validate_graph
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
)

CHECK_ORDER = [
    "duplicate", "reference", "cycle", "capability", "energy_task", "horizon_path",
    "horizon", "energy",
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
) -> Task:
    return Task(id=id_, name=id_, robot_type=type_, duration_h=dur, energy_wh=energy,
                zone=zone, after=list(after))


def req(*robots: Robot, horizon: int = 336, zones: tuple[str, ...] = ("Z1", "Z2")) -> Requirements:
    return Requirements(
        site=Site(zones=[Zone(id=z, center=(10 * i, 10)) for i, z in enumerate(zones)]),
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


def must_fail(req_: Requirements, graph_: TaskGraph) -> Failure:
    failure = validate_graph(req_, graph_)
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
    r = req(robot("EX1", battery=3000.6))
    g = graph(*(task(f"T{i}", energy=1000.2) for i in range(3)))
    assert sum(t.energy_wh for t in g.tasks) > 3000.6
    assert validate_graph(r, g) is None


# ----------------------------------------------------------------------------- one kind each
def test_duplicate_ids_in_every_namespace() -> None:
    r = req(robot("EX1"), robot("EX1"), zones=("Z1", "Z1", "Z2"))
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
    assert "5500 Wh" in failure.hint and "5000 Wh" in failure.hint


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
    assert "excavator" in failure.hint and "11000 Wh" in failure.hint and "10000 Wh" in failure.hint


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
    # every kind except horizon_path (critical path is skipped while the graph has a cycle)
    assert kinds(failure) == [k for k in CHECK_ORDER if k != "horizon_path"]
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
    r = req(robot("EX1", battery=4200.25))
    assert validate_graph(r, graph(task("T1", energy=2100.125), task("T2", energy=2100.125))) is None
    # ... and one milli-Wh over is a conflict, quoted with the figures that were compared
    failure = must_fail(r, graph(task("T1", energy=2100.126), task("T2", energy=2100.125)))
    assert failure.conflicts == ["energy:excavator"]
    assert "4200.251 Wh" in failure.hint and "4200.25 Wh" in failure.hint
    failure = must_fail(r, graph(task("T1", energy=4200.251)))
    assert failure.conflicts == ["energy_task:T1", "energy:excavator"]
    assert failure.details[0] == (
        "T1 needs 4200.251 Wh but the largest excavator battery is 4200.25 Wh")


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
