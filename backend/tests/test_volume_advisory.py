"""``volume:<structure>`` advisories (phase 3c): the m3 a structure's tasks plan against the
volume its dimensions imply, a warning the trace shows and never a conflict. The three shapes
below are live findings: a borrow strip planned in m2, a berm hauled twice over, a pad dug
smaller than the requirements say."""

from __future__ import annotations

from pathlib import Path

import pytest

from moonwright.cli import eval_row, plan_summary
from moonwright.llm.base import FixtureClient, Usage
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.rates import dims_volume, dims_volumes, volume_advisories, volume_shares
from moonwright.pipeline.taskgraph import generate_task_graph
from moonwright.schemas import (
    Failure,
    LoopStep,
    PlanResult,
    Requirements,
    Schedule,
    ScheduleEntry,
    Task,
    TaskGraph,
)

ROOT = Path(__file__).resolve().parents[1]


def req_with(*structures: dict) -> Requirements:
    base = Requirements.model_validate_json((ROOT / "fixtures/landing_pad/structure.json").read_text())
    data = {**base.model_dump(), "structures": list(structures)}
    return Requirements.model_validate(data)


def task(id: str, robot_type: str, quantity: float | None, unit: str | None,
         structure: str = "S1") -> Task:
    return Task(id=id, name=f"{robot_type} {id}", robot_type=robot_type, duration_h=1,
                energy_wh=1, zone="Z1", structure=structure, quantity=quantity, unit=unit)


def test_dims_volume_knows_bowls_boxes_and_berms() -> None:
    bowl = req_with({"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.5}).structures[0]
    box = req_with({"id": "S1", "type": "borrow_strip", "length_m": 100, "width_m": 6, "depth_m": 0.6}).structures[0]
    berm = req_with({"id": "S1", "type": "blast_berm", "length_m": 60, "height_m": 2.5, "width_m": 4}).structures[0]
    steep = req_with({"id": "S1", "type": "berm", "length_m": 20, "height_m": 1.5}).structures[0]
    road = req_with({"id": "S1", "type": "access_road", "length_m": 120, "width_m": 4}).structures[0]
    assert dims_volume(bowl) == (157.07963267948966, "bowl 20 m across, 0.5 m deep", "excavation")
    assert dims_volume(box) == (360.0, "100 x 6 x 0.6 m", "excavation")
    assert dims_volume(berm) == (300.0, ("60 m long, 2.5 m high, 4 m wide at the base, triangular "
                                        "section"), "haul")
    assert dims_volume(steep) == (45.0, "20 m long, 1.5 m high, 1:1 slopes", "haul")
    assert dims_volume(road) is None


def test_a_pad_dug_smaller_than_its_dimensions_is_reported() -> None:
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.5})
    g = TaskGraph(tasks=[task("T1", "surveyor", 314, "m2"), task("T2", "excavator", 56.55, "m3"),
                         task("T3", "compactor", 113, "m2")])
    assert volume_advisories(r, g) == [
        ("volume:S1: its dimensions give 157.1 m3 (bowl 20 m across, 0.5 m deep) but its "
        "excavation tasks (T2 56.5 m3) total only 56.5 m3 (36%)")]


def test_a_dig_planned_in_square_metres_counts_as_nothing_dug() -> None:
    r = req_with({"id": "STR2", "type": "borrow strip", "zone": "Z2", "length_m": 100,
                  "width_m": 6, "depth_m": 0.6})
    g = TaskGraph(tasks=[task("T3", "excavator", 600, "m2", structure="STR2")])
    assert volume_advisories(r, g) == [
        ("volume:STR2: its dimensions give 360 m3 (100 x 6 x 0.6 m) but its excavation tasks "
        "(T3 600 m2) total only 0 m3 (0%)")]


def test_a_berm_hauled_twice_over_is_reported() -> None:
    r = req_with({"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                  "width_m": 4})
    g = TaskGraph(tasks=[task("T7", "hauler", 300, "m3", "S2"), task("T8", "hauler", 300, "m3", "S2"),
                         task("T9", "compactor", 240, "m2", "S2")])
    assert volume_advisories(r, g) == [
        ("volume:S2: its dimensions give 300 m3 (60 m long, 2.5 m high, 4 m wide at the base, "
        "triangular section) but its haul tasks (T7 300 m3, T8 300 m3) total 600 m3 (200%)")]


def test_in_range_dimensionless_and_untasked_structures_say_nothing() -> None:
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4},
                 {"id": "S2", "type": "blast_berm", "zone": "Z2"},
                 {"id": "S3", "type": "borrow_pit", "zone": "Z3", "length_m": 8, "width_m": 8,
                  "depth_m": 0.9})
    g = TaskGraph(tasks=[task("T1", "excavator", 63, "m3"), task("T2", "excavator", 63, "m3"),
                         task("T3", "hauler", 20, "m3", "S2"), task("T4", "surveyor", 64, "m2", "S3")])
    assert volume_advisories(r, g) == []   # 126 of 125.7 m3; no volume; no excavator task on S3


def test_the_recorded_fixtures_carry_no_volume_advisory() -> None:
    for brief in ("landing_pad", "access_road", "tight_battery"):
        text = (ROOT / "examples/briefs" / f"{brief}.md").read_text()
        result = Planner(FixtureClient(), explain=False).plan(text, brief_id=brief)
        assert result.success
        advisories = [a for s in result.steps for a in ((s.payload or {}).get("advisories") or [])]
        assert not [a for a in advisories if a.startswith("volume:")]


# ----------------------------------------------------------------------------- review fixes
def test_a_zero_or_negative_dimension_is_no_volume_and_never_divides_by_zero() -> None:
    """``depth_m: 0`` (an at-grade hardstand, or a dimension cleared on the requirements
    card) crashed the task-graph stage of every quantified graph with a ZeroDivisionError;
    such a shape is simply not a volume now, and a negative figure is not one either."""
    flat = req_with({"id": "S1", "type": "equipment_hardstand", "zone": "Z1", "diameter_m": 20,
                     "depth_m": 0})
    negative = req_with({"id": "S1", "type": "borrow_strip", "zone": "Z1", "length_m": 100,
                         "width_m": 6, "depth_m": -0.6})
    g = TaskGraph(tasks=[task("T1", "excavator", 314, "m2"), task("T2", "excavator", 10, "m3")])
    for r in (flat, negative):
        assert dims_volume(r.structures[0]) is None and dims_volumes(r.structures[0]) == []
        assert volume_advisories(r, g) == [] and volume_shares(r, g) == {}

    class Echo:
        name = "echo"

        def complete_json(self, **kwargs):
            return g, Usage(model="echo")

    graph, step = generate_task_graph(Echo(), flat, key="k")   # the path that crashed
    assert step.ok and graph is not None
    assert not [a for a in step.payload["advisories"] if a.startswith("volume:")]


def test_tags_resolve_exactly_as_the_coverage_check_resolves_them() -> None:
    """A graph the validator accepts with the tag ``landing_pad``, ``Landing pad S1``, ``s1``
    or no tag at all used to get no advisory while the same graph tagged ``S1`` did."""
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.5},
                 {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5})
    expected = [("volume:S1: its dimensions give 157.1 m3 (bowl 20 m across, 0.5 m deep) but its "
                 "excavation tasks (T2 56.5 m3) total only 56.5 m3 (36%)")]
    for tag in ("S1", "s1", "landing_pad", "Landing pad S1", None):
        g = TaskGraph(tasks=[task("T2", "excavator", 56.55, "m3", structure=tag)])
        assert volume_advisories(r, g) == expected, tag
        assert volume_shares(r, g) == {"S1": pytest.approx(0.36, abs=0.001)}
    # tagged with the berm, in the berm's zone: nothing counts for the pad
    away = task("T2", "excavator", 56.55, "m3", "S2").model_copy(update={"zone": "Z2"})
    assert volume_advisories(r, TaskGraph(tasks=[away])) == []


def test_a_haul_placed_in_the_berm_zone_builds_the_berm_whatever_its_tag() -> None:
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4},
                 {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                  "width_m": 4})
    haul = task("T3", "hauler", 300, "m3", "S1").model_copy(
        update={"zone": "Z2", "name": "haul pad spoil to berm line"})
    assert volume_advisories(r, TaskGraph(tasks=[haul])) == []        # 300 of 300 m3
    twice = TaskGraph(tasks=[haul, haul.model_copy(update={"id": "T4"})])
    assert volume_advisories(r, twice) == [
        ("volume:S2: its dimensions give 300 m3 (60 m long, 2.5 m high, 4 m wide at the base, "
         "triangular section) but its haul tasks (T3 300 m3, T4 300 m3) total 600 m3 (200%)")]


def test_a_key_trench_is_a_second_dug_shape_under_a_placed_berm() -> None:
    """The video-B mechanism: the structure prompt now has keys for "on a key trench 1.2 m
    wide and 0.8 m deep", and a berm "repaired" with two 5 m3 token digs is flagged against
    the 57.6 m3 those keys imply instead of passing as covered."""
    berm = req_with({"id": "S2", "type": "dust_berm", "zone": "Z2", "length_m": 60, "height_m": 1.2,
                     "width_m": 3, "trench_width_m": 1.2, "trench_depth_m": 0.8})
    shapes = dims_volumes(berm.structures[0])
    assert [(round(v, 3), how, phase) for v, how, phase in shapes] == [
        (108.0, "60 m long, 1.2 m high, 3 m wide at the base, triangular section", "haul"),
        (57.6, "key trench 60 x 1.2 x 0.8 m", "excavation")]
    assert dims_volume(berm.structures[0]) == shapes[0]
    tokens = TaskGraph(tasks=[
        task("T13", "excavator", 5, "m3", "S2").model_copy(update={"zone": "Z2"}),
        task("T14", "excavator", 5, "m3", "S2").model_copy(update={"zone": "Z2"}),
        task("T7", "hauler", 108, "m3", "S2").model_copy(update={"zone": "Z2"})])
    assert volume_advisories(berm, tokens) == [
        ("volume:S2: its dimensions give 57.6 m3 (key trench 60 x 1.2 x 0.8 m) but its "
         "excavation tasks (T13 5 m3, T14 5 m3) total only 10 m3 (17%)")]
    assert volume_shares(berm, tokens) == {"S2": pytest.approx(10 / 57.6)}
    # a trench given by depth alone takes the base width; a dug body has no trench of its own
    by_depth = req_with({"id": "S2", "type": "berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                         "width_m": 4, "trench_depth_m": 0.5}).structures[0]
    assert dims_volumes(by_depth)[1][0] == pytest.approx(120.0)
    pit = req_with({"id": "S3", "type": "borrow_pit", "zone": "Z3", "length_m": 8, "width_m": 8,
                    "depth_m": 0.9, "trench_depth_m": 0.5}).structures[0]
    assert len(dims_volumes(pit)) == 1


def test_volume_advisories_are_judged_on_whole_plans_not_on_a_replans_remaining_graph() -> None:
    """A replan plans the work LEFT: the second half of a bowl must not read as "only 50 %"
    on the escalated repair step (seen in review on the live landing_pad_v2 plan)."""
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4})
    remaining = TaskGraph(tasks=[task("T6", "excavator", 63, "m3")])

    class Echo:
        name = "echo"

        def complete_json(self, **kwargs):
            return remaining, Usage(model="echo")

    verdict = Failure(kind="infeasible", conflicts=["energy:EX1"])
    replan_calls = ({"prior": remaining, "variant": "replan"},
                    {"attempt": 2, "prior": remaining, "feedback": verdict, "baseline": remaining,
                     "variant": "replan"})
    for kwargs in replan_calls:
        _, step = generate_task_graph(Echo(), r, key="k", **kwargs)
        assert step.ok and not [a for a in step.payload["advisories"] if a.startswith("volume:")]
    # a fresh plan and its repairs are whole plans: the same graph is judged there
    for kwargs in ({}, {"attempt": 2, "prior": remaining, "feedback": verdict}):
        _, step = generate_task_graph(Echo(), r, key="k", **kwargs)
        volume = [a for a in step.payload["advisories"] if a.startswith("volume:")]
        assert len(volume) == 1 and "total only 63 m3 (50%)" in volume[0]


def test_the_plan_summary_and_the_eval_row_say_when_scope_differs_from_dimensions() -> None:
    """small_fleet_long_berm live: a 360 m3 borrow strip "excavated" as 600 m2 verified at
    90 h as a plain success. The CLI's closing line and the eval's ``in_scope`` column say
    so now."""
    r = req_with({"id": "STR2", "type": "borrow strip", "zone": "Z1", "length_m": 100,
                  "width_m": 6, "depth_m": 0.6})
    graph = TaskGraph(tasks=[task("T3", "excavator", 600, "m2", "STR2").model_copy(
        update={"name": "excavate STR2 borrow strip", "duration_h": 24, "energy_wh": 2880})])
    schedule = Schedule(entries=[ScheduleEntry(task="T3", robot="EX1", start_h=0, end_h=24)],
                        makespan_h=24, status="optimal")
    result = PlanResult(requirements=r, task_graph=graph, schedule=schedule, success=True,
                        steps=[LoopStep(stage="structure", ok=True)])
    lines = plan_summary(result, Path("out/x.plan.json")).splitlines()
    assert lines[0] == (f"verified plan: makespan 24h of {r.horizon_h}h horizon, 0 repair(s) -> "
                        "out/x.plan.json")
    assert lines[1] == "verified against the planning model; scope differs from the dimensions:"
    assert lines[2] == ("  volume:STR2: its dimensions give 360 m3 (100 x 6 x 0.6 m) but its "
                        "excavation tasks (T3 600 m2) total only 0 m3 (0%)")
    row = eval_row("small_fleet_long_berm", "loop_on", 1, result, 1.0, kind="D")
    assert (row.success, row.in_scope, row.volume_advisories, row.min_volume_share) == (
        True, False, 1, 0.0)
    assert row.kind == "D" and len(row.requirements_sha) == 10
    # dug as its dimensions say: in scope, and the summary is the one line it always was
    dug = graph.model_copy(update={"tasks": [graph.tasks[0].model_copy(
        update={"quantity": 360.0, "unit": "m3"})]})
    fine = result.model_copy(update={"task_graph": dug})
    assert "\n" not in plan_summary(fine, Path("out/x.plan.json"))
    row = eval_row("small_fleet_long_berm", "loop_on", 1, fine, 1.0)
    assert (row.in_scope, row.volume_advisories, row.min_volume_share, row.kind) == (
        True, 0, 1.0, "")
    # a run without a graph (the structure stage failed) has nothing to judge
    row = eval_row("x", "loop_on", 1, PlanResult(requirements=r, success=False), 1.0)
    assert (row.in_scope, row.volume_advisories, row.min_volume_share, row.requirements_sha) == (
        False, 0, None, "")
