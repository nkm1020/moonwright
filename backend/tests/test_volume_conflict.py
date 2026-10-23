"""Phase 3e: under-scope is a repairable conflict (``volume:<structure>``).

The first live evaluation verified 85-91 % of the briefs but only 38-45 % "in scope": the
models satisfied the solver by shrinking the work (a 20 m pad dug as a 12 m one, a 360 m3
borrow strip planned as 600 m2 of stripping, a 300 m3 berm hauled twice over), the volume
advisory never reached the repair prompt and no repair ever fixed one. The validator now
rejects a whole plan whose tasks cover less than 70 % of the smallest volume a structure's
dimensions allow, with a detail that shows the arithmetic and a hint that says what to add;
over-scope stays an advisory, the fill of a stockpile is never short, a replan (the remaining
graph after an event) is never judged, and the hand-made fixtures (no quantities) are untouched.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from moonwright.llm.base import Usage
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.rates import (
    SPLIT_ABOVE_M3,
    STRIP_DEPTH_M,
    Slope,
    dims_volumes,
    volume_advisories,
    volume_coverages,
    volume_shortfalls,
    wall_slope,
)
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import Failure, Requirements, Robot, Site, Structure, Task, TaskGraph, Zone

ZONES = [Zone(id="Z1", center=(10, 20)), Zone(id="Z2", center=(30, 20)), Zone(id="Z3", center=(20, 30))]
FLEET = [Robot(id="EX1", type="excavator", battery_wh=5000),
         Robot(id="HL1", type="hauler", battery_wh=3000)]


def reqs(*structures: dict, robots: list[Robot] = FLEET) -> Requirements:
    """A site with a depot and three zones, a fleet of one excavator and one hauler (so the
    coverage check asks a pad for its dig only and a berm for its haul only), and the given
    structures (dicts with id, type, zone and dimensions)."""
    return Requirements(site=Site(depot=(20, 2), zones=ZONES), robots=robots, horizon_h=336,
                        structures=[Structure.model_validate(s) for s in structures])


def task(id: str, robot_type: str, quantity: float, unit: str, structure: str = "S1",
         zone: str = "Z1", name: str | None = None) -> Task:
    return Task(id=id, name=name or f"{robot_type} {structure} {id}", robot_type=robot_type,
                duration_h=1, energy_wh=1, zone=zone, structure=structure, quantity=quantity,
                unit=unit)


def must_fail(req: Requirements, graph: TaskGraph, **kwargs: object) -> Failure:
    failure = validate_graph(req, graph, coverage=True, **kwargs)  # type: ignore[arg-type]
    assert failure is not None and failure.kind == "validation"
    assert len(failure.details) == len(failure.conflicts)
    return failure


PAD = {"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4}
STRIP = {"id": "S2", "type": "borrow_strip", "zone": "Z2", "length_m": 100, "width_m": 6, "depth_m": 0.6}
SHALLOW = {"id": "S1", "type": "instrument_pad", "zone": "Z1", "length_m": 10, "width_m": 10,
           "depth_m": 0.15}


# ----------------------------------------------------------------------------- worked examples
def test_a_shallow_pad_stripped_and_graded_over_its_footprint_passes() -> None:
    """10 x 10 x 0.15 m pad planned as strip 100 m2 + grade 100 m2: the areas are summed,
    capped at the 100 m2 footprint and credited at min(0.15, 0.2) m, 15 of 15 m3."""
    g = TaskGraph(tasks=[task("T1", "excavator", 100, "m2", name="Strip pad fines"),
                         task("T2", "excavator", 100, "m2", name="Grade pad")])
    assert validate_graph(reqs(SHALLOW), g, coverage=True) is None
    assert volume_advisories(reqs(SHALLOW), g) == []


def test_a_borrow_strip_planned_in_square_metres_is_a_conflict_with_the_arithmetic() -> None:
    """100 x 6 x 0.6 m strip planned as 600 m2: the stripping credits the top 0.2 m, 120 of
    360 m3 (33 %); the detail shows the arithmetic and the hint says what to add."""
    g = TaskGraph(tasks=[task("T3", "excavator", 600, "m2", "S2", "Z2", "Strip borrow strip")])
    failure = must_fail(reqs(STRIP), g)
    assert failure.conflicts == ["volume:S2"]
    assert failure.details == [
        ("S2 (borrow_strip): dimensions 100 x 6 x 0.6 m = 360 m3 of excavation; excavator tasks "
         "cover 120 m3 (33 %): T3 600 m2 counts 120 m3 at 0.2 m")]
    assert failure.hint == (
        "S2 (borrow_strip) plans 120 of 360 m3: plan the remaining 240 m3 of S2 as excavator "
        "tasks in m3 (split above 60 m3) instead of shrinking the work; keep the m2 stripping "
        "only for the top 0.2 m.")
    assert f"split above {SPLIT_ABOVE_M3:g} m3" in failure.hint
    assert f"top {STRIP_DEPTH_M} m" in failure.hint
    # the same strip planned in m3 is covered; so is strip + the rest in m3
    assert validate_graph(reqs(STRIP), TaskGraph(tasks=[
        task("T3", "excavator", 360, "m3", "S2", "Z2")]), coverage=True) is None
    assert validate_graph(reqs(STRIP), TaskGraph(tasks=[
        task("T3", "excavator", 600, "m2", "S2", "Z2"),
        task("T4", "excavator", 240, "m3", "S2", "Z2")]), coverage=True) is None


def test_a_bowl_stripped_in_square_metres_is_half_dug() -> None:
    """20 m x 0.4 m bowl planned as 314 m2: 62.8 of 125.7 m3 (49.97 %, rounded down: a
    shortfall's share never reads as more than it is)."""
    g = TaskGraph(tasks=[task("T2", "excavator", 314, "m2", name="Strip pad")])
    failure = must_fail(reqs(PAD), g)
    assert failure.conflicts == ["volume:S1"]
    assert failure.details == [
        ("S1 (landing_pad): dimensions bowl 20 m across, 0.4 m deep = 125.7 m3 of excavation; "
         "excavator tasks cover 62.8 m3 (49 %): T2 314 m2 counts 62.8 m3 at 0.2 m")]
    assert failure.hint == (
        "S1 (landing_pad) plans 62.8 of 125.7 m3: plan the remaining 62.9 m3 of S1 as excavator "
        "tasks in m3 (split above 60 m3) instead of shrinking the work; keep the m2 stripping "
        "only for the top 0.2 m.")
    # dug in m3 but too small (the live 20 m pad dug as a 12 m one): no stripping clause
    small = TaskGraph(tasks=[task("T2", "excavator", 45.2, "m3", name="Excavate pad bowl")])
    failure = must_fail(reqs(PAD), small)
    assert failure.details == [
        ("S1 (landing_pad): dimensions bowl 20 m across, 0.4 m deep = 125.7 m3 of excavation; "
         "excavator tasks cover 45.2 m3 (35 %): T2 45.2 m3")]
    assert failure.hint == (
        "S1 (landing_pad) plans 45.2 of 125.7 m3: plan the remaining 80.5 m3 of S1 as excavator "
        "tasks in m3 (split above 60 m3) instead of shrinking the work.")


# ----------------------------------------------------------------------------- floors (kind E)
def test_the_smallest_size_the_brief_allows_is_the_floor() -> None:
    """low_battery_pad: "any diameter between 12 m and 20 m that fits is acceptable". With
    min_diameter_m 12 on the structure a 12 m dig passes, an 8 m dig fails, and the verdict
    quotes both sizes so the allowed re-scope stays plannable."""
    pad = {**PAD, "depth_m": 0.5, "min_diameter_m": 12}
    twelve = TaskGraph(tasks=[task("T2", "excavator", round(math.pi * 36 * 0.5, 1), "m3")])
    assert validate_graph(reqs(pad), twelve, coverage=True) is None
    eight = TaskGraph(tasks=[task("T2", "excavator", round(math.pi * 16 * 0.5, 1), "m3")])
    failure = must_fail(reqs(pad), eight)
    assert failure.conflicts == ["volume:S1"]
    assert failure.details == [
        ("S1 (landing_pad): dimensions bowl 12-20 m across, 0.5 m deep = 56.5-157.1 m3 of "
         "excavation; excavator tasks cover 25.1 m3 (44 % of the smallest size the brief allows): "
         "T2 25.1 m3")]
    assert failure.hint == (
        "S1 (landing_pad) plans 25.1 of 56.5 m3, the smallest size the brief allows (bowl 12 m "
        "across, 0.5 m deep): plan the remaining 31.4 m3 of S1 as excavator tasks in m3 (split "
        "above 60 m3) instead of shrinking the work.")
    # without the floor the same 12 m dig is a conflict (36 %)
    assert must_fail(reqs({**PAD, "depth_m": 0.5}), twelve).conflicts == ["volume:S1"]


# ----------------------------------------------------------------------------- what is judged
def test_a_key_trench_and_a_berm_are_judged_separately_under_one_id() -> None:
    berm = {"id": "S2", "type": "dust_berm", "zone": "Z2", "length_m": 60, "height_m": 1.2,
            "width_m": 3, "trench_width_m": 1.2, "trench_depth_m": 0.8}
    g = TaskGraph(tasks=[task("T13", "excavator", 5, "m3", "S2", "Z2", "Cut key trench"),
                         task("T14", "excavator", 5, "m3", "S2", "Z2", "Cut key trench"),
                         task("T7", "hauler", 50, "m3", "S2", "Z2", "Haul fill to berm")])
    failure = must_fail(reqs(berm), g)
    assert failure.conflicts == ["volume:S2"]
    assert failure.details == [
        ("S2 (dust_berm): dimensions 60 m long, 1.2 m high, 3 m wide at the base, triangular "
         "section = 108 m3 of fill; hauler tasks cover 50 m3 (46 %): T7 50 m3 and its key "
         "trench is 60 x 1.2 x 0.8 m = 57.6 m3 of excavation; excavator tasks cover 10 m3 "
         "(17 %): T13 5 m3, T14 5 m3")]
    assert failure.hint == (
        "S2 (dust_berm) plans 50 of 108 m3: plan the remaining 58 m3 of S2 as hauler tasks in m3 "
        "(split above 60 m3), each after the excavation its fill comes from (a borrow dig in m3 "
        "when no structure's spoil supplies it), instead of shrinking the work. S2 (dust_berm) "
        "plans 10 of 57.6 m3: plan the remaining 47.6 m3 of the S2 key trench as excavator "
        "tasks in m3 (split above 60 m3) instead of shrinking the work.")


def test_over_scope_stays_an_advisory_and_a_stockpiles_fill_is_never_short() -> None:
    berm = {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5, "width_m": 4}
    twice = TaskGraph(tasks=[task("T7", "hauler", 300, "m3", "S2", "Z2"),
                             task("T8", "hauler", 300, "m3", "S2", "Z2")])
    assert validate_graph(reqs(berm), twice, coverage=True) is None
    assert volume_advisories(reqs(berm), twice)[0].endswith("(200 %): hauled twice over")
    # a stockpile is whatever gets hauled: 10 of 48 m3 is no shortfall ...
    pile = {"id": "S2", "type": "spoil_stockpile", "zone": "Z2", "length_m": 12, "height_m": 2}
    g = TaskGraph(tasks=[task("T3", "hauler", 10, "m3", "S2", "Z2", "Haul spoil to stockpile")])
    assert validate_graph(reqs(pile), g, coverage=True) is None
    assert volume_advisories(reqs(pile), g) == []
    labelled = {**pile, "type": "fill_area", "label": "Spoil heap"}
    assert validate_graph(reqs(labelled), g, coverage=True) is None
    # ... but a spoil berm is a berm
    failure = must_fail(reqs({**pile, "type": "spoil_berm"}), g)
    assert failure.conflicts == ["volume:S2"]
    assert failure.details == [("S2 (spoil_berm): dimensions 12 m long, 2 m high, 1:1 slopes = "
                                "48 m3 of fill; hauler tasks cover 10 m3 (20 %): T3 10 m3")]
    assert failure.hint == ("S2 (spoil_berm) plans 10 of 48 m3: plan the remaining 38 m3 of S2 as "
                            "hauler tasks in m3 (split above 60 m3), each after the excavation "
                            "its fill comes from (a borrow dig in m3 when no structure's spoil "
                            "supplies it), instead of shrinking the work.")


def test_graphs_without_quantities_and_structures_without_a_volume_are_untouched() -> None:
    r = reqs(PAD, {"id": "S2", "type": "stockpile", "zone": "Z2"},
             {"id": "S3", "type": "hardstand", "zone": "Z3", "diameter_m": 20, "depth_m": 0})
    plain = TaskGraph(tasks=[
        Task(id="T1", name="Excavate pad bowl", robot_type="excavator", duration_h=10,
             energy_wh=1000, zone="Z1", structure="S1"),
        Task(id="T2", name="Haul spoil", robot_type="hauler", duration_h=4, energy_wh=400,
             zone="Z2", structure="S2"),
        Task(id="T3", name="Grade hardstand", robot_type="excavator", duration_h=4, energy_wh=400,
             zone="Z3", structure="S3")])
    assert validate_graph(r, plain, coverage=True) is None
    quantified = TaskGraph(tasks=[
        task("T1", "excavator", 126, "m3"), task("T2", "hauler", 5, "m3", "S2", "Z2"),
        task("T3", "excavator", 10, "m2", "S3", "Z3")])
    assert validate_graph(r, quantified, coverage=True) is None


def test_volume_sits_between_scope_and_energy_task_in_check_order() -> None:
    r = reqs(PAD, {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5},
             {"id": "S3", "type": "borrow_pit", "zone": "Z3", "length_m": 20, "width_m": 20, "depth_m": 1})
    g = TaskGraph(tasks=[task("T1", "excavator", 30, "m3"),                          # 24 % of S1
                         task("T2", "excavator", 400, "m3", "S3", "Z3").model_copy(
                             update={"energy_wh": 6400.0})])                        # 400 m3 x 16 Wh
    failure = must_fail(r, g)
    assert failure.conflicts == ["scope:S2", "volume:S1", "energy_task:T2", "energy:excavator"]
    assert failure.details[1].startswith("S1 (landing_pad): dimensions bowl 20 m across")
    assert failure.hint.index("S2 (blast_berm)") < failure.hint.index("S1 (landing_pad) plans") \
        < failure.hint.index("No single robot can power T2")


# ----------------------------------------------------------------------------- whole plans only
class Echo:
    """A client that answers every call with one graph."""

    name = "echo"

    def __init__(self, graph: TaskGraph):
        self.graph = graph

    def complete_json(self, **kwargs):
        return self.graph, Usage(model="echo")


def test_a_replan_is_never_judged_but_a_fresh_plan_and_its_repairs_are() -> None:
    """The remaining graph after an event has its finished work gone: half a bowl left is
    not an under-scoped pad. ``volume`` follows ``coverage`` by default, can be switched off
    on a whole plan, and can be forced on without coverage."""
    remaining = TaskGraph(tasks=[task("T6", "excavator", 63, "m3", name="Excavate pad bowl, south half")])
    r = reqs(PAD)
    assert validate_graph(r, remaining) is None                                   # a replan
    assert validate_graph(r, remaining, coverage=True, volume=False) is None      # advisory only
    assert must_fail(r, remaining).conflicts == ["volume:S1"]                    # a whole plan
    forced = validate_graph(r, remaining, volume=True)
    assert forced is not None and forced.conflicts == ["volume:S1"]
    # through the planner: the replan path verifies the remaining graph and schedules it ...
    planner = Planner(Echo(remaining), max_repairs=0, explain=False, suggest_budget_s=0)
    replan = planner.plan_from_requirements(r, key="k", prior_graph=remaining, context="HL1 failed",
                                            variant="replan", required_tasks=["T6"])
    assert replan.success and [s.stage for s in replan.steps] == ["taskgraph", "validate", "schedule"]
    assert not [c for s in replan.steps if s.failure for c in s.failure.conflicts]
    # ... while the same graph as a fresh plan is rejected at the validate step
    fresh = planner.plan_from_requirements(r, key="k")
    assert not fresh.success and fresh.failure is not None
    assert fresh.failure.conflicts == ["volume:S1"]
    assert [s.stage for s in fresh.steps] == ["taskgraph", "validate"]
    assert fresh.steps[1].failure == fresh.failure
    assert "plan the remaining 62.7 m3 of S1 as excavator tasks in m3" in fresh.failure.hint
    # the advisory of the task-graph step says the same, in its own words
    advisories = fresh.steps[0].payload["advisories"]
    assert [a for a in advisories if a.startswith("volume:")] == [
        ("volume:S1: its dimensions give 125.7 m3 (bowl 20 m across, 0.4 m deep) but its "
         "excavation tasks (T6 63 m3) total only 63 m3 (50 %)")]


@pytest.mark.parametrize("share", [0.69, 0.7, 0.71])
def test_the_threshold_is_seventy_percent_of_the_floor(share: float) -> None:
    volume = math.pi * 100 * 0.4
    g = TaskGraph(tasks=[task("T1", "excavator", volume * share, "m3")])
    failure = validate_graph(reqs(PAD), g, coverage=True)
    assert (failure is None) == (share >= 0.7), share


def test_a_shortfall_share_is_rounded_down_so_it_never_reads_as_the_threshold() -> None:
    volume = math.pi * 100 * 0.4
    failure = must_fail(reqs(PAD), TaskGraph(tasks=[task("T1", "excavator", volume * 0.6996, "m3")]))
    assert "(69 %)" in failure.details[0] and "70 %" not in failure.details[0]


# ----------------------------------------------------------------------------- review of 3e
ROOT = Path(__file__).resolve().parents[1]


def after(t: Task, *ids: str) -> Task:
    return t.model_copy(update={"after": list(ids)})


def test_a_key_trench_without_a_stated_width_is_not_judged() -> None:
    """tight_battery, the demo twin, states "on a 0.5 m deep key trench" and no width. The
    berm's 4 m base used to stand in for it, so a correct 60 x 1.2 x 0.5 m trench (36 m3) was
    rejected as 30 % of an invented 120 m3 and the hint asked for 84 m3 more. Only the
    numbers the brief states are judged now; with the width stated the trench is judged."""
    req = Requirements.model_validate_json((ROOT / "fixtures/tight_battery/structure.json").read_text())
    berm = next(s for s in req.structures if s.id == "S2")
    assert "trench_width_m" not in berm.dims() and berm.dims()["trench_depth_m"] == 0.5
    assert [s.kind for s in dims_volumes(berm)] == ["prism"]
    g = TaskGraph(tasks=[
        task("T1", "surveyor", 314, "m2", "S1", "Z1", "Survey and stake pad"),
        task("T2", "excavator", 94.25, "m3", "S1", "Z1", "Excavate pad bowl north half"),
        task("T3", "excavator", 94.25, "m3", "S1", "Z1", "Excavate pad bowl south half"),
        task("T4", "excavator", 36, "m3", "S2", "Z2", "Cut berm key trench 60 x 1.2 x 0.5 m"),
        after(task("T5", "hauler", 150, "m3", "S2", "Z2", "Haul bowl spoil to berm"), "T2", "T4"),
        after(task("T6", "hauler", 150, "m3", "S2", "Z2", "Haul bowl spoil to berm"), "T3", "T5"),
        task("T7", "excavator", 240, "m2", "S3", "Z4", "Strip approach apron"),
        after(task("T8", "compactor", 314, "m2", "S1", "Z1", "Compact pad subgrade"), "T2", "T3"),
        after(task("T9", "compactor", 240, "m2", "S2", "Z2", "Compact berm"), "T6")])
    assert validate_graph(req, g, coverage=True) is None
    assert volume_advisories(req, g) == []
    stated = req.model_copy(update={"structures": [
        s.model_copy(update={"trench_width_m": 1.2}) if s.id == "S2" else s for s in req.structures]})
    assert validate_graph(stated, g, coverage=True) is None          # 36 of 36 m3
    token = g.model_copy(update={"tasks": [
        t.model_copy(update={"quantity": 10.0}) if t.id == "T4" else t for t in g.tasks]})
    failure = must_fail(stated, token)
    assert failure.conflicts == ["volume:S2"]
    assert failure.details == [
        ("S2 (blast_berm): its key trench is 60 x 1.2 x 0.5 m = 36 m3 of excavation; excavator "
         "tasks cover 10 m3 (27 %): T4 10 m3")]


def test_sloped_walls_the_brief_states_are_judged_between_the_rim_and_the_floor() -> None:
    """propellant_pit_and_berm: "Storage pit: 8 m diameter, 2 m deep, with a 1:1 sloped wall".
    Priced with vertical walls (100.5 m3), the pit dug to its real volume (8 m across at the
    rim, 4 m at the floor: 58.6 m3) was a 58 % shortfall, and reading 8 m at the floor (159.2
    m3) was "dug 1.6 times over". The brief's own entry (``source_brief``, which a live run
    carries) or a ``wall_slope`` extra now gives the range, and either reading passes."""
    brief = (ROOT / "examples/eval/propellant_pit_and_berm.md").read_text()
    pit = {"id": "S1", "type": "storage_pit", "zone": "Z1", "diameter_m": 8, "depth_m": 2}
    live = reqs(pit).model_copy(update={"source_brief": brief})
    assert wall_slope(live.structures[0], live) == Slope(1.0, "1:1 sloped walls")
    shape = dims_volumes(live.structures[0], live)[0]
    assert (round(shape.floor_m3, 1), round(shape.m3, 1)) == (58.6, 159.2)
    assert shape.how == "bowl 8 m across, 2 m deep, 1:1 sloped walls"
    assert shape.floor_area_m2 == pytest.approx(math.pi * 4)     # the 4 m floor, for the slab
    for dug in (58.6, 100.5, 159.2):
        g = TaskGraph(tasks=[task("T1", "excavator", dug, "m3", name="Excavate storage pit")])
        assert validate_graph(live, g, coverage=True) is None, dug
        assert volume_advisories(live, g) == [], dug
    failure = must_fail(live, TaskGraph(tasks=[task("T1", "excavator", 30, "m3")]))
    assert failure.details == [
        ("S1 (storage_pit): dimensions bowl 8 m across, 2 m deep, 1:1 sloped walls = "
         "58.6-159.2 m3 of excavation; excavator tasks cover 30 m3 (51 % of the smallest size "
         "the brief allows): T1 30 m3")]
    assert failure.hint.startswith(
        "S1 (storage_pit) plans 30 of 58.6 m3, the smallest size the brief allows (bowl 8 m "
        "across at the rim, 2 m deep, 1:1 sloped walls): plan the remaining 28.6 m3 of S1")
    # without the brief (a hand-made fixture) the walls are vertical, as before
    frustum = TaskGraph(tasks=[task("T1", "excavator", 58.6, "m3")])
    assert must_fail(reqs(pit), frustum).conflicts == ["volume:S1"]
    # the structure stage's key reads the same; a slope of no stated ratio only bounds the
    # floor (a cone: the shallowest walls that still reach 2 m) and judges no excess
    keyed = reqs({**pit, "wall_slope": "1:1"})
    assert validate_graph(keyed, frustum, coverage=True) is None
    unknown = reqs({**pit, "wall_slope": True})
    cone = dims_volumes(unknown.structures[0], unknown)[0]
    assert cone.floor_m3 == pytest.approx(math.pi * 16 * 2 / 3) and not cone.excess_judged
    big = TaskGraph(tasks=[task("T1", "excavator", 300, "m3")])
    assert volume_advisories(unknown, big) == []
    # a slope of the ground, or a drainage gradient, is no wall slope
    for line in ("1. Storage pit: 8 m diameter, 2 m deep, on a gentle 2 degree slope.",
                 "1. Storage pit: 8 m diameter, 2 m deep, graded at 1:50 to drain.",
                 "**Site.** The plateau slopes north.\n1. Storage pit: 8 m diameter, 2 m deep."):
        r = reqs(pit).model_copy(update={"source_brief": line})
        assert wall_slope(r.structures[0], r) is None, line
    sides = reqs(pit).model_copy(update={"source_brief": "1. Storage pit: 8 m, with 2:1 side slopes"})
    assert wall_slope(sides.structures[0], sides) == Slope(2.0, "2:1 sloped walls")


def test_a_levelling_pass_after_the_dig_earns_no_stripping_credit() -> None:
    """Review: "a 20 m pad dug as a 12 m one" passed when the plan also levelled the pad
    (landing_pad asks for +/-5 cm): the 314 m2 pass after the dig was credited with the top
    0.2 m of the whole pad, 86 % in all. A pass after a dig removes no surface layer unless
    it is named a strip; a strip before the dig is the normal sequence and still counts."""
    dig = task("T2", "excavator", 45.2, "m3", name="Excavate pad bowl")
    level = after(task("T3", "excavator", 314, "m2", name="Level pad base to +/-5 cm"), "T2")
    failure = must_fail(reqs(PAD), TaskGraph(tasks=[dig, level]))
    assert failure.details == [
        ("S1 (landing_pad): dimensions bowl 20 m across, 0.4 m deep = 125.7 m3 of excavation; "
         "excavator tasks cover 45.2 m3 (35 %): T2 45.2 m3, T3 314 m2 counts nothing (a pass "
         "after the dig)")]
    assert failure.hint.endswith(
        "instead of shrinking the work; an m2 pass after the dig (T3) removes no surface layer.")
    deeper = TaskGraph(tasks=[dig.model_copy(update={"quantity": 26.0}), level])
    assert must_fail(reqs(PAD), deeper).conflicts == ["volume:S1"]
    # strip first, then the bowl below it: the whole pad
    strip = task("T1", "excavator", 314, "m2", name="Strip pad")
    assert validate_graph(reqs(PAD), TaskGraph(tasks=[
        strip, after(task("T2", "excavator", 62.9, "m3", name="Excavate pad bowl"), "T1")]),
        coverage=True) is None
    # a pass after the dig that the plan names a strip keeps its credit
    named = level.model_copy(update={"name": "Strip pad margin"})
    assert validate_graph(reqs(PAD), TaskGraph(tasks=[dig, named]), coverage=True) is None


def test_below_the_stripped_layer_only_digs_count() -> None:
    """Review: a strip over the whole footprint lifted a token dig over 70 %. 26 m3 under a
    314 m2 strip of a 0.4 m pad is 70.7 % of the bowl but 41 % of the layer below the top
    0.2 m, which only a dig in m3 removes: a conflict, with the layer in the detail."""
    g = TaskGraph(tasks=[task("T1", "excavator", 314, "m2", name="Strip pad"),
                         after(task("T2", "excavator", 26, "m3", name="Excavate pad"), "T1")])
    failure = must_fail(reqs(PAD), g)
    assert failure.details == [
        ("S1 (landing_pad): dimensions bowl 20 m across, 0.4 m deep = 125.7 m3 of excavation; "
         "excavator tasks cover 88.8 m3 (70 %): T1 314 m2 counts 62.8 m3 at 0.2 m, T2 26 m3; "
         "below the top 0.2 m only digs in m3 count, 26 of 62.8 m3 (41 %)")]
    assert "plan the remaining 36.9 m3 of S1 as excavator tasks in m3" in failure.hint
    # a 0.3 m layer of fines: the strip covers 0.2 m, the dig must cover most of the rest
    fines = {"id": "S1", "type": "solar_array_pad", "zone": "Z1", "length_m": 30,
             "width_m": 18, "depth_m": 0.3}
    token = TaskGraph(tasks=[task("T1", "excavator", 540, "m2", name="Strip pad"),
                             after(task("T2", "excavator", 5, "m3", name="Excavate fines"), "T1")])
    assert must_fail(reqs(fines), token).conflicts == ["volume:S1"]
    full = token.model_copy(update={"tasks": [
        t.model_copy(update={"quantity": 54.0}) if t.id == "T2" else t for t in token.tasks]})
    assert validate_graph(reqs(fines), full, coverage=True) is None


def test_a_kind_e_pad_stripped_at_full_size_earns_only_its_floors_top_layer() -> None:
    """Review: low_battery_pad (12-20 m, 0.5 m deep) "planned" as a strip of the whole 20 m
    pad plus a 1 m3 test pit read 113 % of its 12 m floor and verified at 37 h. The credit
    is capped at the floor's footprint (113.1 m2: 22.6 m3) and the 33.9 m3 below it needs
    digs, so the plan is a conflict; the 12 m floor dug in full still passes."""
    pad = {**PAD, "depth_m": 0.5, "min_diameter_m": 12}
    g = TaskGraph(tasks=[task("T2", "excavator", 314, "m2", name="Strip pad footprint"),
                         after(task("T3", "excavator", 1, "m3", name="Excavate test pit"), "T2")])
    failure = must_fail(reqs(pad), g)
    assert failure.conflicts == ["volume:S1"]
    assert failure.details == [
        ("S1 (landing_pad): dimensions bowl 12-20 m across, 0.5 m deep = 56.5-157.1 m3 of "
         "excavation; excavator tasks cover 23.6 m3 (41 % of the smallest size the brief "
         "allows): T2 314 m2 counts 22.6 m3 at 0.2 m over the 113.1 m2 footprint of the "
         "smallest size, T3 1 m3")]
    floor = TaskGraph(tasks=[task("T2", "excavator", 113.1, "m2", name="Strip pad"),
                             after(task("T3", "excavator", 34, "m3", name="Excavate bowl"), "T2")])
    assert validate_graph(reqs(pad), floor, coverage=True) is None


def test_spoil_moved_is_no_dig() -> None:
    """three_pads_in_order: "the spoil of each pad is bladed into a low windrow ... by the
    excavator". Done as its own m3 task it read "dug twice over"; a backfill of a trench
    likewise. Moving spoil counts neither for a dug shape nor against it."""
    pad = {"id": "S1", "type": "cargo_pad_a", "zone": "Z1", "diameter_m": 14, "depth_m": 0.4}
    bowl = round(math.pi * 49 * 0.4, 1)
    g = TaskGraph(tasks=[
        task("T2", "excavator", bowl, "m3", name="Excavate S1 bowl"),
        after(task("T3", "excavator", bowl, "m3", name="Blade S1 spoil into the north windrow"), "T2")])
    assert validate_graph(reqs(pad), g, coverage=True) is None
    assert volume_advisories(reqs(pad), g) == []
    trench = {"id": "S2", "type": "anchor_trench", "zone": "Z2", "length_m": 30, "width_m": 0.5,
              "depth_m": 0.5}
    backfill = task("T6", "excavator", 7.5, "m3", "S2", "Z2", "Backfill anchor trench")
    dug = TaskGraph(tasks=[task("T5", "excavator", 7.5, "m3", "S2", "Z2", "Excavate anchor trench"),
                           after(backfill, "T5")])
    assert volume_advisories(reqs(trench), dug) == []
    failure = must_fail(reqs(trench), TaskGraph(tasks=[backfill]))
    assert failure.details[0].endswith("T6 7.5 m3 counts nothing (moves spoil already dug)")
    assert failure.hint.endswith("moving spoil already dug (T6) digs nothing.")
    # a name that says it digs is a dig, whatever else it does
    both = task("T5", "excavator", bowl, "m3", name="Excavate and blade the S1 bowl")
    assert volume_advisories(reqs(pad), TaskGraph(tasks=[both, g.tasks[0]]))[0].endswith(
        "dug twice over")


def test_a_berm_built_from_another_structures_spoil_is_judged_against_that_spoil() -> None:
    """landing_pad: "Blast berm ... built from pad spoil". The 300 m3 prism holds more than
    the 125.7 m3 the pad yields, so hauling ALL the spoil was a 42 % shortfall and only a plan
    hauling more "pad spoil" than the pad produces passed. A placed body whose hauls wait for
    another structure's dig is judged against the smaller of its own volume and that dig's;
    without such a wait the hint says where the fill has to come from."""
    req = Requirements.model_validate_json((ROOT / "fixtures/landing_pad/structure.json").read_text())
    digs = [task("T2", "excavator", 62.83, "m3", "S1", "Z1", "Excavate pad bowl north half"),
            task("T3", "excavator", 62.83, "m3", "S1", "Z1", "Excavate pad bowl south half")]
    hauls = [after(task("T4", "hauler", 62.83, "m3", "S2", "Z2", "Haul bowl spoil to berm"), "T2"),
             after(task("T5", "hauler", 62.83, "m3", "S2", "Z2", "Haul bowl spoil to berm"), "T3")]
    g = TaskGraph(tasks=[*digs, *hauls])
    assert volume_shortfalls(req, g) == [] and volume_advisories(req, g) == []
    [berm] = [c for c in volume_coverages(req, g) if c.structure.id == "S2"]
    assert berm.sources == ("S1",) and berm.floor_m3 == pytest.approx(math.pi * 40)
    # the prism's own volume still says what a full haul is: 300 m3 is no excess
    full = TaskGraph(tasks=[*digs, hauls[0].model_copy(update={"quantity": 150.0}),
                            hauls[1].model_copy(update={"quantity": 150.0})])
    assert volume_advisories(req, full) == []
    short = TaskGraph(tasks=[*digs, hauls[0].model_copy(update={"quantity": 50.0})])
    failure = validate_graph(req, short, volume=True)
    assert failure is not None and failure.conflicts == ["volume:S2"]
    assert failure.details == [
        ("S2 (blast_berm): dimensions 60 m long, 2.5 m high, 4 m wide at the base, triangular "
         "section = 300 m3 of fill, more than the 125.7 m3 of spoil S1 yields; hauler tasks "
         "cover 50 m3 (39 % of that spoil): T4 50 m3")]
    assert failure.hint == (
        "S2 (blast_berm) plans 50 of 125.7 m3, all the spoil of S1 its hauls wait for: plan the "
        "remaining 75.7 m3 of S2 as hauler tasks in m3 (split above 60 m3) instead of "
        "shrinking the work.")
    assert volume_advisories(req, short) == [
        ("volume:S2: its dimensions give 300 m3 (60 m long, 2.5 m high, 4 m wide at the base, "
         "triangular section) but its haul tasks (T4 50 m3) total only 50 m3 (39 % of the "
         "125.7 m3 of spoil S1 yields)")]
    # hauls that wait for no dig are judged against the prism, and told where fill comes from
    loose = TaskGraph(tasks=[*digs, *(h.model_copy(update={"after": []}) for h in hauls)])
    failure = validate_graph(req, loose, volume=True)
    assert failure is not None and failure.conflicts == ["volume:S2"]
    assert "plans 125.7 of 300 m3" in failure.hint
    assert "each after the excavation its fill comes from" in failure.hint
