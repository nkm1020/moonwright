"""``volume:<structure>`` advisories (phase 3c, stripping semantics and floors in phase 3e):
the m3 a structure's tasks plan against the volume its dimensions imply. A shortfall is also
the validator's conflict on a whole plan (``tests/test_volume_conflict.py``); an excess stays
a warning the trace shows up to the double count (175 %, phase 3i, a conflict too:
``tests/test_double_count.py``), whose advisory says so. The shapes below are live findings: a
borrow strip planned in m2, a berm hauled twice over, a pad dug smaller than the requirements
say, a shallow instrument pad "stripped" over its whole footprint (a false positive until the
m2 credit)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from moonwright.cli import eval_row, plan_summary
from moonwright.llm.base import FixtureClient, Usage
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.rates import (
    STRIP_DEPTH_M,
    VOLUME_DOUBLE,
    VOLUME_HIGH,
    VOLUME_LOW,
    dims_volume,
    dims_volumes,
    stockpile_like,
    volume_advisories,
    volume_coverages,
    volume_shares,
    volume_shortfalls,
)
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
    base = Requirements.model_validate_json(
        (ROOT / "fixtures/landing_pad_v1/structure.json").read_text())
    data = {**base.model_dump(), "structures": list(structures)}
    return Requirements.model_validate(data)


def task(id: str, robot_type: str, quantity: float | None, unit: str | None,
         structure: str = "S1") -> Task:
    return Task(id=id, name=f"{robot_type} {id}", robot_type=robot_type, duration_h=1,
                energy_wh=1, zone="Z1", structure=structure, quantity=quantity, unit=unit)


def summary(shape) -> tuple[float, str, str]:
    return (shape.m3, shape.how, shape.phase)


def test_dims_volume_knows_bowls_boxes_and_berms() -> None:
    bowl = req_with({"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.5}).structures[0]
    box = req_with({"id": "S1", "type": "borrow_strip", "length_m": 100, "width_m": 6, "depth_m": 0.6}).structures[0]
    berm = req_with({"id": "S1", "type": "blast_berm", "length_m": 60, "height_m": 2.5, "width_m": 4}).structures[0]
    steep = req_with({"id": "S1", "type": "berm", "length_m": 20, "height_m": 1.5}).structures[0]
    road = req_with({"id": "S1", "type": "access_road", "length_m": 120, "width_m": 4}).structures[0]
    assert summary(dims_volume(bowl)) == (157.07963267948966, "bowl 20 m across, 0.5 m deep", "excavation")
    assert summary(dims_volume(box)) == (360.0, "100 x 6 x 0.6 m", "excavation")
    assert summary(dims_volume(berm)) == (300.0, ("60 m long, 2.5 m high, 4 m wide at the base, "
                                                  "triangular section"), "haul")
    assert summary(dims_volume(steep)) == (45.0, "20 m long, 1.5 m high, 1:1 slopes", "haul")
    assert dims_volume(road) is None
    # a dug shape knows the footprint an m2 task can strip and its depth; a placed body has neither
    assert (dims_volume(bowl).footprint_m2, dims_volume(bowl).depth_m, dims_volume(bowl).kind) == (
        pytest.approx(math.pi * 100), 0.5, "bowl")
    assert (dims_volume(box).footprint_m2, dims_volume(box).depth_m, dims_volume(box).kind) == (
        600.0, 0.6, "box")
    assert (dims_volume(berm).footprint_m2, dims_volume(berm).depth_m, dims_volume(berm).kind) == (
        0.0, 0.0, "prism")
    # without min dims the floor is the volume itself and nothing reads as a range
    for s, text in zip((bowl, box, berm, steep), ("157.1", "360", "300", "45"), strict=True):
        shape = dims_volume(s)
        assert shape.floor_m3 == shape.m3 and shape.floor_how == shape.how and not shape.ranged
        assert shape.m3_text == text


def test_a_pad_dug_smaller_than_its_dimensions_is_reported() -> None:
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.5})
    g = TaskGraph(tasks=[task("T1", "surveyor", 314, "m2"), task("T2", "excavator", 56.55, "m3"),
                         task("T3", "compactor", 113, "m2")])
    assert volume_advisories(r, g) == [
        ("volume:S1: its dimensions give 157.1 m3 (bowl 20 m across, 0.5 m deep) but its "
        "excavation tasks (T2 56.5 m3) total only 56.5 m3 (36 %)")]


# ----------------------------------------------------------------------------- stripping
def test_a_dig_planned_in_square_metres_counts_the_top_layer_only() -> None:
    """Live: a 360 m3 borrow strip planned as 600 m2 of stripping. The rate table's m2 row
    means a surface layer up to 0.2 m, so the strip counts 120 m3 (33 %), not 0 and not
    all of it."""
    assert STRIP_DEPTH_M == 0.2
    r = req_with({"id": "STR2", "type": "borrow strip", "zone": "Z2", "length_m": 100,
                  "width_m": 6, "depth_m": 0.6})
    g = TaskGraph(tasks=[task("T3", "excavator", 600, "m2", structure="STR2")])
    assert volume_advisories(r, g) == [
        ("volume:STR2: its dimensions give 360 m3 (100 x 6 x 0.6 m) but its excavation tasks "
        "(T3 600 m2 counts 120 m3 at 0.2 m) total only 120 m3 (33 %)")]
    [c] = volume_coverages(r, g)
    assert (c.counted_m3, c.stripped_m3, c.area_m2, c.robot) == (120.0, 120.0, 600.0, "excavator")
    assert c.short and not c.over and not c.excused and volume_shortfalls(r, g) == [c]
    # the 240 m3 below the stripped 0.2 m has no dig at all: the judged share is that layer's
    assert (c.share, c.deep_share) == (pytest.approx(1 / 3), 0.0)
    assert volume_shares(r, g) == {"STR2": 0.0}


def test_a_shallow_pad_stripped_over_its_footprint_is_fully_covered() -> None:
    """Live false positive: a 10 x 10 m instrument pad "stripped of 15 cm of fines" is a
    15 m3 box, the model plans strip 100 m2 and grade 100 m2 (the table's own m2 row) and
    the old rule said 0 %. The areas are summed, capped at the footprint (strip + grade over
    the same pad do not double count) and credited at min(depth, 0.2 m): 15 of 15 m3."""
    r = req_with({"id": "S1", "type": "instrument_pad", "zone": "Z1", "length_m": 10,
                  "width_m": 10, "depth_m": 0.15})
    g = TaskGraph(tasks=[task("T1", "excavator", 100, "m2"), task("T2", "excavator", 100, "m2"),
                         task("T3", "compactor", 100, "m2")])
    assert volume_advisories(r, g) == [] and volume_shortfalls(r, g) == []
    assert volume_shares(r, g) == {"S1": pytest.approx(1.0)}
    [c] = volume_coverages(r, g)
    assert (c.counted_m3, c.stripped_m3, c.area_m2) == (pytest.approx(15.0), pytest.approx(15.0), 200.0)
    assert c.shape.strip_depth_m == 0.15
    assert c.listing == "T1 100 m2 + T2 100 m2 count 15 m3 at 0.15 m over the 100 m2 footprint"
    assert c.deep_share is None   # 0.15 m: nothing lies below the stripped layer
    # a 20 m x 0.4 m bowl "stripped" as 314 m2 is half dug: 62.8 of 125.7 m3
    bowl = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4})
    g = TaskGraph(tasks=[task("T2", "excavator", 314, "m2")])
    assert volume_advisories(bowl, g) == [
        ("volume:S1: its dimensions give 125.7 m3 (bowl 20 m across, 0.4 m deep) but its "
         "excavation tasks (T2 314 m2 counts 62.8 m3 at 0.2 m) total only 62.8 m3 (49 %)")]
    assert volume_shares(bowl, g) == {"S1": 0.0}   # the layer below 0.2 m is not dug
    # strip the top 0.2 m and dig the rest in m3: the two together are the whole bowl
    both = TaskGraph(tasks=[task("T2", "excavator", 314, "m2"), task("T3", "excavator", 63, "m3")])
    assert volume_advisories(bowl, both) == []
    assert volume_shares(bowl, both) == {"S1": pytest.approx(1.0, abs=0.002)}


def test_a_hauler_in_square_metres_and_odd_units_count_nothing() -> None:
    r = req_with({"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                  "width_m": 4})
    g = TaskGraph(tasks=[task("T7", "hauler", 240, "m2", "S2"), task("T8", "hauler", 100, "m3", "S2"),
                         task("T9", "hauler", 3, "ea", "S2")])
    assert volume_advisories(r, g) == [
        ("volume:S2: its dimensions give 300 m3 (60 m long, 2.5 m high, 4 m wide at the base, "
         "triangular section) but its haul tasks (T7 240 m2 counts nothing, T8 100 m3, T9 3 ea "
         "counts nothing) total only 100 m3 (33 %)")]


# ----------------------------------------------------------------------------- over-scope
def test_a_berm_hauled_twice_over_is_reported_as_such() -> None:
    r = req_with({"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                  "width_m": 4})
    g = TaskGraph(tasks=[task("T7", "hauler", 300, "m3", "S2"), task("T8", "hauler", 300, "m3", "S2"),
                         task("T9", "compactor", 240, "m2", "S2")])
    assert volume_advisories(r, g) == [
        ("volume:S2: its dimensions give 300 m3 (60 m long, 2.5 m high, 4 m wide at the base, "
        "triangular section) but its haul tasks (T7 300 m3, T8 300 m3) total 600 m3 (200 %): "
        "hauled twice over, a double count")]
    # a shortfall it is not; from 175 % it is the validator's double count (phase 3i)
    assert volume_shortfalls(r, g) == []
    assert VOLUME_HIGH == 1.5 and VOLUME_LOW == 0.7 and VOLUME_DOUBLE == 1.75
    pad = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.5})
    g = TaskGraph(tasks=[task("T2", "excavator", 250, "m3")])
    assert volume_advisories(pad, g) == [
        ("volume:S1: its dimensions give 157.1 m3 (bowl 20 m across, 0.5 m deep) but its "
         "excavation tasks (T2 250 m3) total 250 m3 (159 %): dug 1.6 times over")]
    g = TaskGraph(tasks=[task("T2", "excavator", 471.24, "m3")])
    assert volume_advisories(pad, g)[0].endswith("(300 %): dug three times over, a double count")


def test_an_excess_counts_the_m3_alone() -> None:
    """Strip the pad, then dig its whole bowl in m3 (what the prompt asks for): the stripping
    credit would make 150.2 % of it, but the credit only fills a shortfall, so this is no
    excess. A bowl dug at 0.6 m under a 0.4 m pad is one, and it names the m3 task alone."""
    pad = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4})
    full = TaskGraph(tasks=[task("T2", "excavator", 314.16, "m2"), task("T3", "excavator", 125.7, "m3")])
    assert volume_advisories(pad, full) == []
    [c] = volume_coverages(pad, full)
    assert c.counted_m3 == pytest.approx(188.53, abs=0.01) and c.share_full == pytest.approx(1.0, abs=0.001)
    deep = TaskGraph(tasks=[task("T2", "excavator", 300, "m2"), task("T3", "excavator", 188.5, "m3")])
    assert volume_advisories(pad, deep) == [
        ("volume:S1: its dimensions give 125.7 m3 (bowl 20 m across, 0.4 m deep) but its "
         "excavation tasks in m3 (T3 188.5 m3) total 188.5 m3 (150 %): dug 1.5 times over")]


def test_in_range_dimensionless_and_untasked_structures_say_nothing() -> None:
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4},
                 {"id": "S2", "type": "blast_berm", "zone": "Z2"},
                 {"id": "S3", "type": "borrow_pit", "zone": "Z3", "length_m": 8, "width_m": 8,
                  "depth_m": 0.9})
    g = TaskGraph(tasks=[task("T1", "excavator", 63, "m3"), task("T2", "excavator", 63, "m3"),
                         task("T3", "hauler", 20, "m3", "S2"), task("T4", "surveyor", 64, "m2", "S3")])
    assert volume_advisories(r, g) == []   # 126 of 125.7 m3; no volume; no excavator task on S3


def test_the_recorded_fixtures_carry_no_volume_advisory() -> None:
    """The hand-made fixtures; the live landing_pad's double haul is pinned by its replay."""
    for brief in ("landing_pad_v1", "access_road", "tight_battery"):
        folder = "tests/data" if brief == "landing_pad_v1" else "examples/briefs"
        text = (ROOT / folder / f"{brief}.md").read_text()
        result = Planner(FixtureClient(), explain=False).plan(text, brief_id=brief)
        assert result.success
        advisories = [a for s in result.steps for a in ((s.payload or {}).get("advisories") or [])]
        assert not [a for a in advisories if a.startswith("volume:")]


# ----------------------------------------------------------------------------- floors
def test_min_dimensions_set_the_floor_the_brief_allows() -> None:
    """Kind E (low_battery_pad): "any diameter between 12 m and 20 m that fits is acceptable".
    The structure stage records min_diameter_m 12; the shape comes from the regular
    dimensions, the floor from the smallest ones, the advisory quotes both, and the share is
    judged against the floor so the allowed re-scope stays plannable."""
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20,
                  "depth_m": 0.5, "min_diameter_m": 12})
    shape = dims_volume(r.structures[0])
    assert (shape.m3, shape.floor_m3) == (pytest.approx(157.08, abs=0.01), pytest.approx(56.55, abs=0.01))
    assert shape.how == "bowl 12-20 m across, 0.5 m deep" and shape.ranged
    assert shape.floor_how == "bowl 12 m across, 0.5 m deep" and shape.m3_text == "56.5-157.1"
    assert shape.footprint_m2 == pytest.approx(math.pi * 100)   # the full pad can be stripped
    twelve = TaskGraph(tasks=[task("T2", "excavator", 56.5, "m3")])
    assert volume_advisories(r, twelve) == [] and volume_shares(r, twelve) == {"S1": pytest.approx(0.999, abs=0.001)}
    eight = TaskGraph(tasks=[task("T2", "excavator", 25.1, "m3")])
    assert volume_advisories(r, eight) == [
        ("volume:S1: its dimensions give 56.5-157.1 m3 (bowl 12-20 m across, 0.5 m deep) but its "
         "excavation tasks (T2 25.1 m3) total only 25.1 m3 (44 % of the smallest size the brief "
         "allows)")]
    # over-scope is still judged against the full dimensions
    big = TaskGraph(tasks=[task("T2", "excavator", 200, "m3")])
    assert volume_advisories(r, big) == []
    # every dimension has a floor key; a min above the dimension, or at 0, is ignored
    box = req_with({"id": "S2", "type": "borrow_strip", "zone": "Z2", "length_m": 100, "width_m": 6,
                    "depth_m": 0.6, "min_length_m": 80, "min_depth_m": 0.4}).structures[0]
    assert dims_volume(box).how == "80-100 x 6 x 0.4-0.6 m" and dims_volume(box).floor_m3 == pytest.approx(192)
    berm = req_with({"id": "S3", "type": "berm", "zone": "Z3", "length_m": 60, "height_m": 2.5,
                     "width_m": 4, "min_height_m": 2, "min_width_m": 5, "min_length_m": 0,
                     "trench_depth_m": 0.5, "trench_width_m": 1.2}).structures[0]
    body, trench = dims_volumes(berm)
    assert body.how == "60 m long, 2-2.5 m high, 4 m wide at the base, triangular section"
    assert body.floor_m3 == pytest.approx(240) and body.m3 == pytest.approx(300)
    assert trench.how == "key trench 60 x 1.2 x 0.5 m" and not trench.ranged
    steep = req_with({"id": "S3", "type": "berm", "zone": "Z3", "length_m": 20, "height_m": 1.5,
                      "min_height_m": 1, "min_length_m": 16, "trench_depth_m": 0.5,
                      "trench_width_m": 1}).structures[0]
    body, trench = dims_volumes(steep)
    assert body.how == "16-20 m long, 1-1.5 m high, 1:1 slopes" and body.floor_m3 == pytest.approx(16)
    assert trench.how == "key trench 16-20 x 1 x 0.5 m" and trench.floor_m3 == pytest.approx(8)
    assert trench.head == "its key trench is 16-20 x 1 x 0.5 m"


# ----------------------------------------------------------------------------- stockpiles
def test_the_fill_of_a_stockpile_is_never_short() -> None:
    """A stockpile is whatever gets hauled: its nominal size is no scope. A spoil berm is a
    berm (the structure prompt's own distinction), judged like any designed body."""
    pile = req_with({"id": "S2", "type": "spoil_stockpile", "zone": "Z2", "length_m": 12, "height_m": 2})
    g = TaskGraph(tasks=[task("T3", "hauler", 10, "m3", "S2")])   # 10 of 48 m3
    assert volume_advisories(pile, g) == [] and volume_shortfalls(pile, g) == []
    assert volume_shares(pile, g) == {}
    [c] = volume_coverages(pile, g)
    assert c.short and c.excused
    for typ in ("stockpile", "Spoil pile", "feedstock_heap", "dump_area", "equipment_depot"):
        assert stockpile_like(pile.structures[0].model_copy(update={"type": typ})), typ
    labelled = req_with({"id": "S2", "type": "fill_area", "zone": "Z2", "length_m": 12, "height_m": 2,
                         "label": "Spoil heap north of the pad"})
    assert stockpile_like(labelled.structures[0]) and volume_advisories(labelled, g) == []
    for typ in ("spoil_berm", "blast_berm", "containment_berm", "shielding_mound", "fill_area"):
        assert not stockpile_like(pile.structures[0].model_copy(update={"type": typ})), typ
    berm = req_with({"id": "S2", "type": "spoil_berm", "zone": "Z2", "length_m": 12, "height_m": 2})
    assert volume_advisories(berm, g) == [
        ("volume:S2: its dimensions give 48 m3 (12 m long, 2 m high, 1:1 slopes) but its haul "
         "tasks (T3 10 m3) total only 10 m3 (20 %)")]
    assert volume_shares(berm, g) == {"S2": pytest.approx(10 / 48)}
    # nor an excess: a pile sized by the brief that takes the whole pad bowl (157 of 48 m3)
    # used to read "hauled 3.3 times over", which marked a verified plan out of scope and
    # told the next repair to cut the haul (seen in review)
    sized = req_with({"id": "S2", "type": "spoil_stockpile", "zone": "Z2", "length_m": 12,
                      "width_m": 8, "height_m": 1})
    bowl = TaskGraph(tasks=[task("T2", "hauler", 157.1, "m3", "S2")])
    assert volume_advisories(sized, bowl) == [] and volume_shares(sized, bowl) == {}


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
                 "excavation tasks (T2 56.5 m3) total only 56.5 m3 (36 %)")]
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
         "triangular section) but its haul tasks (T3 300 m3, T4 300 m3) total 600 m3 (200 %): "
         "hauled twice over, a double count")]


def test_a_key_trench_is_a_second_dug_shape_under_a_placed_berm() -> None:
    """The video-B mechanism: the structure prompt now has keys for "on a key trench 1.2 m
    wide and 0.8 m deep", and a berm "repaired" with two 5 m3 token digs is flagged against
    the 57.6 m3 those keys imply instead of passing as covered."""
    berm = req_with({"id": "S2", "type": "dust_berm", "zone": "Z2", "length_m": 60, "height_m": 1.2,
                     "width_m": 3, "trench_width_m": 1.2, "trench_depth_m": 0.8})
    shapes = dims_volumes(berm.structures[0])
    assert [(round(s.m3, 3), s.how, s.phase) for s in shapes] == [
        (108.0, "60 m long, 1.2 m high, 3 m wide at the base, triangular section", "haul"),
        (57.6, "key trench 60 x 1.2 x 0.8 m", "excavation")]
    assert (shapes[1].footprint_m2, shapes[1].depth_m, shapes[1].kind) == (72.0, 0.8, "trench")
    assert dims_volume(berm.structures[0]) == shapes[0]
    tokens = TaskGraph(tasks=[
        task("T13", "excavator", 5, "m3", "S2").model_copy(update={"zone": "Z2"}),
        task("T14", "excavator", 5, "m3", "S2").model_copy(update={"zone": "Z2"}),
        task("T7", "hauler", 108, "m3", "S2").model_copy(update={"zone": "Z2"})])
    assert volume_advisories(berm, tokens) == [
        ("volume:S2: its dimensions give 57.6 m3 (key trench 60 x 1.2 x 0.8 m) but its "
         "excavation tasks (T13 5 m3, T14 5 m3) total only 10 m3 (17 %)")]
    assert volume_shares(berm, tokens) == {"S2": pytest.approx(10 / 57.6)}
    # a trench given by depth alone has no volume to judge (the base width it used to take
    # is a number the brief never states); a dug body has no trench of its own
    by_depth = req_with({"id": "S2", "type": "berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                         "width_m": 4, "trench_depth_m": 0.5}).structures[0]
    assert [s.kind for s in dims_volumes(by_depth)] == ["prism"]
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
        assert len(volume) == 1 and "total only 63 m3 (50 %)" in volume[0]


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
                        "excavation tasks (T3 600 m2 counts 120 m3 at 0.2 m) total only 120 m3 "
                        "(33 %)")
    row = eval_row("small_fleet_long_berm", "loop_on", 1, result, 1.0, kind="D")
    assert (row.success, row.in_scope, row.volume_advisories, row.min_volume_share) == (
        True, False, 1, 0.0)   # nothing below the stripped 0.2 m is dug
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


# ----------------------------------------------------------------------------- in scope (eval)
def _budget_module():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("bb_scope", ROOT / "scripts" / "brief_budget.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bb_scope"] = module
    spec.loader.exec_module(module)
    return module


def _trimmed(graph: TaskGraph, robot_type: str, factor: float) -> TaskGraph:
    from moonwright.pipeline.rates import apply_rates
    tasks = [t.model_copy(update={"quantity": round(t.quantity * factor, 1)})
             if t.robot_type == robot_type and t.quantity else t for t in graph.tasks]
    return apply_rates(TaskGraph(tasks=tasks))[0]


def _verified(req: Requirements, graph: TaskGraph) -> PlanResult:
    schedule = Schedule(entries=[], makespan_h=1, status="optimal")
    return PlanResult(requirements=req, task_graph=graph, schedule=schedule, success=True,
                      steps=[LoopStep(stage="structure", ok=True)])


def test_a_kind_d_trim_that_verifies_is_not_in_scope() -> None:
    """Review: the D briefs are short of energy by 15-25 % with the scope fixed. A repair that
    trimmed the borrow dig of small_fleet_long_berm to 85 %, or the two compaction lifts of
    solar_array_pad to 78 %, passed the validator (above its 70 %) and CP-SAT, and the eval
    counted the run "feasible in scope". In scope now means every judged volume AND every
    compaction / sintering area (footprint x lifts) at 95 % or more."""
    from moonwright.pipeline.rates import IN_SCOPE_SHARE, area_shares, scope_gaps
    bb = _budget_module()
    assert IN_SCOPE_SHARE == 0.95
    berm = bb.compute_budget(bb.spec_from_brief(ROOT / "examples/eval/small_fleet_long_berm.md"), "x")
    lazy = bb.build_graph(berm, "lazy")
    assert scope_gaps(berm.requirements, lazy) == []           # the full work is in scope
    cut = _trimmed(lazy, "excavator", 0.85)
    assert volume_shortfalls(berm.requirements, cut) == []     # no conflict: 85 % > 70 %
    gaps = scope_gaps(berm.requirements, cut)
    assert gaps == [("volume:S2: its excavation tasks cover 306 of 360 m3 (85 %), under the "
                     "95 % a plan in scope covers")]
    row = eval_row("small_fleet_long_berm", "loop_on", 1, _verified(berm.requirements, cut), 1.0,
                   first_graph=lazy, kind="D")
    assert (row.success, row.in_scope, row.min_volume_share) == (True, False, 0.85)
    solar = bb.compute_budget(bb.spec_from_brief(ROOT / "examples/eval/solar_array_pad.md"), "x")
    pad = solar.requirements.structures[0]
    assert pad.model_extra["lifts"] == 2
    lazy = bb.build_graph(solar, "lazy")
    assert scope_gaps(solar.requirements, lazy) == [] and area_shares(solar.requirements, lazy) == {
        "S1": pytest.approx(1.0), "S2": pytest.approx(1.0)}
    cut = _trimmed(lazy, "compactor", 0.78)
    assert scope_gaps(solar.requirements, cut)[0] == (
        "area:S1: its compactor tasks treat 842.4 of 1,080 m2 (78 %; 540 m2 x 2 lifts), under "
        "the 95 % a plan in scope covers")
    row = eval_row("solar_array_pad", "loop_on", 1, _verified(solar.requirements, cut), 1.0)
    assert (row.in_scope, row.min_volume_share, row.min_area_share) == (False, 1.0, 0.78)
    # an excess is no gap: a berm hauled twice is the whole berm (the advisory says the rest;
    # since phase 3i the validator rejects the double count, so no new run verifies one)
    r = req_with({"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                  "width_m": 4})
    twice = TaskGraph(tasks=[task("T7", "hauler", 300, "m3", "S2"), task("T8", "hauler", 300, "m3", "S2")])
    row = eval_row("x", "loop_on", 1, _verified(r, twice), 1.0)
    assert (row.in_scope, row.volume_advisories) == (True, 1)
    assert plan_summary(_verified(r, twice), Path("o.json")).splitlines()[2].endswith(
        "hauled twice over, a double count")


def test_area_work_is_judged_on_the_floor_of_the_smallest_size() -> None:
    from moonwright.pipeline.rates import area_coverages, lifts
    r = req_with({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20,
                  "depth_m": 0.5, "min_diameter_m": 12},
                 {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
                  "width_m": 4, "lifts": 2},
                 {"id": "S3", "type": "spoil_stockpile", "zone": "Z3", "length_m": 10, "width_m": 5})
    g = TaskGraph(tasks=[task("T1", "compactor", 113.1, "m2"), task("T2", "printer", 120, "m2"),
                         task("T3", "compactor", 480, "m2", "S2"), task("T4", "printer", 60, "m2", "S2"),
                         task("T5", "compactor", 10, "m2", "S3")])
    found = {(a.structure.id, a.robot): (round(a.area_m2, 1), round(a.need_m2, 1), a.how)
             for a in area_coverages(r, g)}
    # the kind-E pad at its 12 m floor; the berm's base x 2 lifts; a berm's sintered crest and
    # a stockpile are not judged
    assert found == {("S1", "compactor"): (113.1, 113.1, "113.1 m2"),
                     ("S1", "printer"): (120.0, 113.1, "113.1 m2"),
                     ("S2", "compactor"): (480.0, 480.0, "240 m2 x 2 lifts")}
    assert lifts(r.structures[1]) == 2 and lifts(r.structures[0]) == 1
