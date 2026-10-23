"""Tests for the double count of phase 3i (``moonwright.pipeline.validate``, "Volume"): dig parts
or haul parts that sum to :data:`VOLUME_DOUBLE` (175 %) of the largest volume the brief allows,
plus 1 m3, count the body twice, and the validator rejects the plan as ``volume:<structure>``,
the shortfall's conflict family. Between 150 % and 175 % the excess stays the advisory it was.

The live cases are trimmed copies of today's landing-pad demo recordings
(``tests/data/landing_pad_try{1,2}_live_plan.json``): in 3 of 3 tries Nemotron 3 Ultra's first
graph hauled 2 x 120 m3 into the 120 m3 berm, which is the berm's 40 x 1.5 x 4 m box, twice the
triangular section the check measures; the excess advisory did not stop it, and in tries 1 and
2 the repair answered the excess and supply notes by inventing a 114 m3 borrow pit the brief
never mentions (240 m3 less the pad's 125.7 m3: the box again); both plans verified. The
verified double counts of runs 2 and 3 sized their berms as the box too ("560 m3 / 2 = 280 m3"),
so the verdict quotes the section and the cut (``scripts/rescore_eval.py`` lists them)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from moonwright.llm.base import FixtureClient, Usage
from moonwright.llm.prompts import COVERAGE_RULES, REPAIR_SYSTEM, STRUCTURE_SYSTEM, TASKGRAPH_SYSTEM
from moonwright.pipeline.loop import Planner, _repeats_verdict
from moonwright.pipeline.rates import (
    DOUBLE_MARK,
    VOLUME_DOUBLE,
    VOLUME_DOUBLE_M3,
    VOLUME_HIGH,
    is_double_count,
    is_shortfall,
    volume_advisories,
    volume_coverages,
    volume_double_counts,
    volume_shortfalls,
    with_judged_footprint,
)
from moonwright.pipeline.validate import supply_advisories, validate_graph
from moonwright.schemas import Requirements, Structure, Task, TaskGraph
from moonwright.text import an

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "data"


def live(n: int) -> tuple[Requirements, TaskGraph, TaskGraph]:
    """Try ``n``'s requirements, its first graph (rebuilt from the repair diff) and the graph
    its repair verified."""
    data = json.loads((DATA / f"landing_pad_try{n}_live_plan.json").read_text())
    return (Requirements.model_validate(data["requirements"]),
            TaskGraph.model_validate(data["first_graph"]),
            TaskGraph.model_validate(data["task_graph"]))


def reqs(*structures: dict) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (8 * i, 10)} for i in range(1, 5)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures],
        robots=[{"id": "EX1", "type": "excavator", "battery_wh": 5000},
                {"id": "EX2", "type": "excavator", "battery_wh": 5000},
                {"id": "HL1", "type": "hauler", "battery_wh": 3000},
                {"id": "HL2", "type": "hauler", "battery_wh": 3000}],
        horizon_h=336)


def task(id_: str, robot_type: str, quantity: float, structure: str = "S2", zone: str = "Z2",
         name: str | None = None, after: tuple[str, ...] = ()) -> Task:
    return Task(id=id_, name=name or f"{robot_type} {structure} {id_}", robot_type=robot_type,
                duration_h=4, energy_wh=100.0, zone=zone, structure=structure,
                quantity=quantity, unit="m3", after=list(after))


PAD = {"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4}
BERM = {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 40, "height_m": 1.5,
        "width_m": 4}                                               # the demo's 120 m3 berm
DIGS = [task("D1", "excavator", 62.83, "S1", "Z1", "Excavate pad bowl part 1"),
        task("D2", "excavator", 62.83, "S1", "Z1", "Excavate pad bowl part 2", ("D1",))]


def hauls(*m3: float) -> TaskGraph:
    """The pad's two bowl digs and one haul into the berm per figure, each after both digs."""
    return TaskGraph(tasks=[*DIGS, *(task(f"H{i}", "hauler", q, after=("D2",))
                                     for i, q in enumerate(m3, start=1))])


# ----------------------------------------------------------------------------- the live cases
FIRST_VERDICTS = {   # the verdict each try's first graph got live, before phase 3i
    1: ["supply:T8", "supply:T9", "energy:compactor", "energy:printer"],
    2: ["energy:printer"],
}
# the verdict leads with the arithmetic of the section and names the box the hauls move
SECTION = ("S2 (blast_berm): dimensions 40 m long, 1.5 m high, 4 m wide at the base, triangular "
           "section = 120 m3 of fill; hauler tasks move 240 m3 (200 %), a double count: ")
BOX = "(240 m3 is the 40 x 1.5 x 4 m box, twice the triangular section)"
DETAILS = {1: f"{SECTION}T8 120 m3, T9 120 m3 {BOX}", 2: f"{SECTION}T7a 120 m3, T7b 120 m3 {BOX}"}
HINT = ("S2 (blast_berm) plans 240 m3 of hauls for its 120 m3 blast berm: its fill is length x "
        "height x base width / 2 (40 x 1.5 x 4 / 2 = 120 m3), not the box; cut the S2 hauls to "
        "120 m3 in total (for example 2 x 60 m3), each part still waiting for the dig part that "
        "feeds it; never add a dig the brief does not ask for.")


@pytest.mark.parametrize("n", [1, 2])
def test_the_live_first_graphs_haul_the_berm_twice(n: int) -> None:
    req, first, _ = live(n)
    # the recorded verdict, reproduced without the double count: the data is the live graph
    before = validate_graph(req, first, coverage=True, volume=False, supply=True)
    assert before is not None and before.conflicts == FIRST_VERDICTS[n]
    failure = validate_graph(req, first, coverage=True)
    assert failure is not None and failure.conflicts == ["volume:S2", *FIRST_VERDICTS[n]]
    assert failure.details[0] == DETAILS[n]
    assert failure.hint.startswith(HINT)
    [c] = volume_double_counts(req, first)
    assert (c.structure.id, c.cubic_m3, c.shape.m3, c.share_full) == (
        "S2", 240.0, pytest.approx(120.0), pytest.approx(2.0))
    # the advisory names the verdict it shares, and the supply note it used to come with (the
    # note the live repairs answered with a borrow pit) is left to the verdict
    [advisory] = [a for a in volume_advisories(req, first) if a.startswith("volume:")]
    assert advisory.endswith("(200 %): hauled twice over" + DOUBLE_MARK)
    assert is_double_count(advisory) and not is_shortfall(advisory)
    assert supply_advisories(req, first) == []


@pytest.mark.parametrize("n", [1, 2])
def test_the_repairs_that_invented_a_borrow_pit_are_rejected_too(n: int) -> None:
    """Both verified repairs kept 240 m3 of hauls for the 120 m3 berm, the third part fed by a
    114.34 m3 borrow pit the brief never mentions: the double count again."""
    req, _, repaired = live(n)
    borrow = [t for t in repaired.tasks if t.robot_type == "excavator" and t.quantity == 114.34]
    assert len(borrow) == 1 and "borrow" in borrow[0].name
    failure = validate_graph(req, repaired, coverage=True)
    assert failure is not None and failure.conflicts == ["volume:S2"]
    parts = {1: "T8 62.8 m3, T9 62.8 m3, T16 114.3 m3", 2: "T7a 125.7 m3, T7b 114.3 m3"}[n]
    assert failure.details == [f"{SECTION}{parts} {BOX}"]
    assert "(40 x 1.5 x 4 / 2 = 120 m3), not the box" in failure.hint
    assert "never add a dig the brief does not ask for" in failure.hint


def test_halving_the_hauls_is_the_answer() -> None:
    """Try 3's repair: the two parts cut to 60 m3 each, each after the dig part that feeds it.
    On try 2's first graph that leaves only its printer verdict."""
    req, first, _ = live(2)
    cut = {"T7a": ["T2a", "T11"], "T7b": ["T7a", "T2b", "T11"]}
    halved = TaskGraph(tasks=[t.model_copy(update={"quantity": 60.0, "after": cut[t.id]})
                              if t.id in cut else t for t in first.tasks])
    failure = validate_graph(req, halved, coverage=True)
    assert failure is not None and failure.conflicts == ["energy:printer"]
    assert volume_advisories(req, halved) == [] and supply_advisories(req, halved) == []


# ----------------------------------------------------------------------------- the threshold
def test_the_threshold_is_175_percent_of_the_largest_volume_plus_1_m3() -> None:
    assert (VOLUME_HIGH, VOLUME_DOUBLE, VOLUME_DOUBLE_M3) == (1.5, 1.75, 1.0)
    r = reqs(PAD, BERM)
    edge = 120 * VOLUME_DOUBLE + VOLUME_DOUBLE_M3                   # 211 m3
    at = validate_graph(r, hauls(edge / 2, edge / 2), volume=True)
    assert at is not None and at.conflicts == ["volume:S2"]
    assert validate_graph(r, hauls(edge / 2, edge / 2 - 0.1), volume=True) is None
    # below it the excess is the advisory it was (175 % itself: 1 m3 short of the double count)
    [advisory] = volume_advisories(r, hauls(105, 105))
    assert advisory.endswith("total 210 m3 (175 %): hauled 1.8 times over")
    assert not is_double_count(advisory)
    # the 1 m3 is for small bodies: a 1 m3 anchor hole dug as two 1 m3 parts is rounding
    hole = reqs({"id": "S3", "type": "anchor_hole", "zone": "Z3", "length_m": 1, "width_m": 1,
                 "depth_m": 1})
    twice = TaskGraph(tasks=[task("A1", "excavator", 1, "S3", "Z3"),
                             task("A2", "excavator", 1, "S3", "Z3")])
    assert validate_graph(hole, twice, volume=True) is None
    assert volume_advisories(hole, twice) == [
        ("volume:S3: its dimensions give 1 m3 (1 x 1 x 1 m) but its excavation tasks (A1 1 m3, "
         "A2 1 m3) total 2 m3 (200 %): dug twice over")]


def test_the_advisory_band_stays_an_advisory() -> None:
    """160 % of the berm: an advisory on the task-graph step, no verdict; a repair is told it
    even when its structure's id is in the verdict for another shape (its key trench dug
    short), while the double count's advisory is left to the verdict that carries it."""
    r = reqs(PAD, BERM)
    assert validate_graph(r, hauls(96, 96), volume=True) is None
    [band] = volume_advisories(r, hauls(96, 96))
    assert band.endswith("total 192 m3 (160 %): hauled 1.6 times over")
    [doubled] = volume_advisories(r, hauls(120, 120))
    assert doubled.endswith("total 240 m3 (200 %): hauled twice over, a double count")
    assert _repeats_verdict(band, {"volume:S2"}) is False
    assert _repeats_verdict(doubled, {"volume:S2"}) is True
    assert _repeats_verdict(doubled, {"volume:S1"}) is False      # another structure's verdict


# ----------------------------------------------------------------------------- what is exempt
def test_stockpiles_piles_and_shapes_without_a_largest_volume_are_never_a_double_count() -> None:
    pile = {"id": "S2", "type": "spoil_stockpile", "zone": "Z2", "length_m": 12, "height_m": 2}
    g = hauls(100, 100)                                         # 200 m3 into a 48 m3 heap
    assert validate_graph(reqs(PAD, pile), g, volume=True) is None
    assert volume_double_counts(reqs(PAD, pile), g) == []
    labelled = {**pile, "type": "fill_area", "label": "Spoil heap north of the pad"}
    assert validate_graph(reqs(PAD, labelled), g, volume=True) is None
    # a pad Lightning typed "stockpile" (two_pads_one_printer, run 2) dug twice: stockpile-like,
    # never a double count (the advisory still says it). Wider than the shortfall, which still
    # judges a dug stockpile-like shape (VolumeCoverage.doubled says why)
    typed = {**PAD, "type": "stockpile"}
    twice = TaskGraph(tasks=[task("D1", "excavator", 125.7, "S1", "Z1"),
                             task("D2", "excavator", 125.7, "S1", "Z1")])
    assert validate_graph(reqs(typed), twice, volume=True) is None
    assert volume_advisories(reqs(typed), twice)[0].endswith("dug twice over")
    short = TaskGraph(tasks=[task("D1", "excavator", 20, "S1", "Z1")])
    assert validate_graph(reqs(typed), short, volume=True).conflicts == ["volume:S1"]
    # hauls into a zone no structure claims fill a pile: no structure's volume
    loose = TaskGraph(tasks=[*DIGS, task("H1", "hauler", 500, None, "Z4", after=("D2",))])
    assert [c.structure.id for c in volume_coverages(reqs(PAD, BERM), loose)] == ["S1"]
    assert validate_graph(reqs(PAD, BERM), loose, volume=True) is None
    # sloped walls of no stated ratio bound no largest volume: no excess is judged
    sloped = {**PAD, "wall_slope": True}
    assert validate_graph(reqs(sloped), twice, volume=True) is None


def with_brief(r: Requirements, text: str) -> Requirements:
    return r.model_copy(update={"source_brief": text})


def test_a_berm_whose_base_width_the_brief_does_not_state_bounds_no_double_count() -> None:
    """Seen in review: propellant_pit_and_berm's berm ("a 30 m arc ..., 2.5 m high") was
    recorded 4 m wide, the structure prompt's example, and 2 x 150 m3 read as 200 % of a 150 m3
    prism the brief never gave (on the 1:1 section a width-less berm holds at least, 187.5 m3,
    it is 160 %). Without a base width the brief states, its section is a guess: the excess
    stays the advisory, which the repair is told."""
    berm = {"id": "BERM", "type": "protective_berm", "zone": "Z2", "length_m": 30,
            "height_m": 2.5, "width_m": 4}
    pit = {"id": "PIT", "type": "storage_pit", "zone": "Z1", "length_m": 12, "width_m": 12,
           "depth_m": 2.5}
    g = TaskGraph(tasks=[task("D1", "excavator", 180, "PIT", "Z1"),
                         task("D2", "excavator", 180, "PIT", "Z1", after=("D1",)),
                         task("H1", "hauler", 150, "BERM", after=("D2",)),
                         task("H2", "hauler", 150, "BERM", after=("D2",))])
    unstated = with_brief(reqs(pit, berm), (
        "1. Storage pit: 12 m by 12 m, 2.5 m deep.\n"
        "2. Protective berm: a 30 m arc on the pad side of the pit, 2.5 m high, built from pit "
        "spoil and compacted.\n"))
    assert validate_graph(unstated, g, coverage=True) is None
    assert volume_double_counts(unstated, g) == []
    [advisory] = volume_advisories(unstated, g)
    assert advisory.endswith("(200 %): hauled twice over") and not is_double_count(advisory)
    assert _repeats_verdict(advisory, {"volume:BERM"}) is False
    # the same berm "4 m wide at the base": the box, a double count
    stated = with_brief(unstated, unstated.source_brief.replace("2.5 m high,",
                                                                "2.5 m high and 4 m wide at the base,"))
    failure = validate_graph(stated, g, coverage=True)
    assert failure is not None and failure.conflicts == ["volume:BERM"]
    assert failure.details[0].endswith(
        "a double count: H1 150 m3, H2 150 m3 (300 m3 is the 30 x 2.5 x 4 m box, twice the "
        "triangular section)")
    # a berm without any width (its 1:1 section is the least it holds) bounds none either
    bare = {k: v for k, v in berm.items() if k != "width_m"}
    assert volume_double_counts(reqs(pit, bare), hauls(300, 300)) == []


def test_a_volume_the_entry_states_is_the_largest_the_brief_allows() -> None:
    """Seen in review: a "Shield wall: 30 m long, 2 m high and 1 m wide (about 60 cubic
    metres)" is a 30 m3 triangle by its dimensions, and a plan that hauled the brief's 60 m3 was
    told to cut to 30. isru_stockpile's "about 200 cubic metres ... from three 20 m by 10 m
    strips" was recorded as one 70 m3 strip in runs 2 and 3: digging all three is no double
    count. A double count is judged against the larger of the two."""
    wall = {"id": "S2", "type": "shield_wall", "zone": "Z2", "length_m": 30, "height_m": 2,
            "width_m": 1}
    r = with_brief(reqs(PAD, wall), (
        "1. Landing pad: circular, 20 m diameter, 0.4 m deep.\n"
        "2. Shield wall: 30 m long, 2 m high and 1 m wide (about 60 cubic metres), from pad "
        "spoil.\n"))
    assert volume_double_counts(r, hauls(30, 30)) == []
    [c] = [c for c in volume_coverages(r, hauls(30, 30)) if c.structure.id == "S2"]
    assert (c.shape.m3, c.largest_m3) == (30, 60)
    failure = validate_graph(r, hauls(60, 60), volume=True, supply=False)
    assert failure is not None and failure.conflicts == ["volume:S2"]
    assert ("hauler tasks move 120 m3 (200 % of the 60 m3 the brief's entry states), a double "
            "count: H1 60 m3, H2 60 m3 (parts split a body's m3") in failure.details[0]
    assert "cut the S2 hauls to 60 m3 in total (for example 2 x 30 m3)" in failure.hint
    strip = {"id": "S1", "type": "borrow_strip", "zone": "Z1", "length_m": 20, "width_m": 10,
             "depth_m": 0.35}
    isru = with_brief(reqs(strip), (ROOT / "examples" / "eval" / "isru_stockpile.md").read_text())
    three = TaskGraph(tasks=[task(f"D{i}", "excavator", 70, "S1", "Z1") for i in (1, 2, 3)])
    assert validate_graph(isru, three, volume=True) is None
    assert validate_graph(isru.model_copy(update={"source_brief": None}), three,
                          volume=True).conflicts == ["volume:S1"]       # 300 % of one strip


def test_a_haul_staged_in_a_pile_is_no_second_haul_of_the_body() -> None:
    """Seen in review: pad spoil hauled to a stockpile zone and on to the berm, both legs tagged
    with the berm, read as 240 of 120 m3 and the hint asked to cut the hauls to 120 m3 in total,
    which would starve the berm. A haul that drops its material in a pile (a zone no structure
    claims, a stockpile) for a later haul of the body to bring on is staging: the supply check
    reads it as a pile's haul, and the double count does not count it. Hauls of the body that
    no other haul waits for still count wherever they are placed."""
    r = reqs(PAD, BERM)
    staged = TaskGraph(tasks=[*DIGS,
                              task("H1", "hauler", 120, "S2", "Z3", "Haul pad spoil to stockpile",
                                   after=("D2",)),
                              task("H2", "hauler", 120, "S2", "Z2", "Haul stockpile to berm",
                                   after=("H1",))])
    assert validate_graph(r, staged, coverage=True) is None
    [c] = [c for c in volume_coverages(r, staged) if c.structure.id == "S2"]
    assert (c.staged, c.cubic_m3, c.excess_m3) == (("H1",), 240, 120)
    assert volume_advisories(r, staged) == []
    # through a listed stockpile, the same
    pile = {"id": "S3", "type": "stockpile", "zone": "Z3"}
    assert validate_graph(reqs(PAD, BERM, pile), staged, volume=True) is None
    # both legs in a zone no structure claims (a haul lane): the body's hauls, a double count
    lane = TaskGraph(tasks=[*DIGS, task("H1", "hauler", 120, "S2", "Z3", after=("D2",)),
                            task("H2", "hauler", 120, "S2", "Z3", after=("H1",))])
    assert validate_graph(r, lane, volume=True, supply=False).conflicts == ["volume:S2"]


class Echo:
    """A client that answers every call with one graph."""

    name = "echo"

    def __init__(self, graph: TaskGraph):
        self.graph = graph

    def complete_json(self, **kwargs):
        return self.graph, Usage(model="echo")


def test_a_replan_is_never_judged_but_a_fresh_plan_is() -> None:
    """The double count follows the shortfall's switches: the remaining graph of a replan (and
    its repairs) is never judged, a whole plan is."""
    r = reqs(PAD, BERM)
    g = hauls(120, 120)
    assert validate_graph(r, g) is None                                       # a replan
    assert validate_graph(r, g, coverage=True, volume=False) is None          # advisory only
    assert validate_graph(r, g, volume=True).conflicts == ["volume:S2"]       # forced
    planner = Planner(Echo(g), max_repairs=0, explain=False, suggest_budget_s=0)
    replan = planner.plan_from_requirements(r, key="k", prior_graph=g, context="HL1 failed",
                                            variant="replan", required_tasks=g.ids())
    assert replan.success and [s.stage for s in replan.steps] == ["taskgraph", "validate",
                                                                   "schedule"]
    fresh = planner.plan_from_requirements(r, key="k")
    assert not fresh.success and fresh.failure is not None
    assert fresh.failure.conflicts == ["volume:S2"] and fresh.failure.hint == HINT


# ----------------------------------------------------------------------------- the words
def test_the_detail_and_the_hint_state_the_numbers() -> None:
    """Digs and hauls, one task or several, the box of a berm or parts that each repeat the
    body, a key trench: the detail leads with the arithmetic as a shortfall's does."""
    pad = reqs(PAD)
    dug = TaskGraph(tasks=[task("D1", "excavator", 125.66, "S1", "Z1"),
                           task("D2", "excavator", 125.66, "S1", "Z1")])
    failure = validate_graph(pad, dug, volume=True)
    assert failure is not None and failure.details == [
        ("S1 (landing_pad): dimensions bowl 20 m across, 0.4 m deep = 125.7 m3 of excavation; "
         "excavator tasks remove 251.3 m3 (200 %), a double count: D1 125.7 m3, D2 125.7 m3 "
         "(parts split a body's m3, they do not each repeat it)")]
    assert failure.hint == (
        "S1 (landing_pad) plans 251.3 m3 of digs for its 125.7 m3 landing pad: cut the S1 digs to "
        "125.7 m3 in total (for example 2 x 62.8 m3), the hauls of its spoil still after the dig "
        "parts that remove it.")
    # one haul of the box: the box's words, no example split
    one = validate_graph(reqs(PAD, BERM), hauls(240), volume=True, supply=False)
    assert one is not None and one.details == [f"{SECTION}H1 240 m3 {BOX}"]
    assert one.hint == (
        "S2 (blast_berm) plans 240 m3 of hauls for its 120 m3 blast berm: its fill is length x "
        "height x base width / 2 (40 x 1.5 x 4 / 2 = 120 m3), not the box; cut the S2 hauls to "
        "120 m3 in total, each part still waiting for the dig part that feeds it; never add a "
        "dig the brief does not ask for.")
    # three parts that each carry the berm: no box, the parts' words
    three = validate_graph(reqs(PAD, BERM), hauls(120, 120, 120), volume=True, supply=False)
    assert three is not None and three.details == [
        ("S2 (blast_berm): dimensions 40 m long, 1.5 m high, 4 m wide at the base, triangular "
         "section = 120 m3 of fill; hauler tasks move 360 m3 (300 %), a double count: H1 120 m3, "
         "H2 120 m3, H3 120 m3 (parts split a body's m3, they do not each repeat it)")]
    assert three.hint == (
        "S2 (blast_berm) plans 360 m3 of hauls for its 120 m3 blast berm: cut the S2 hauls to "
        "120 m3 in total (for example 3 x 40 m3), each part still waiting for the dig part that "
        "feeds it; never add a dig the brief does not ask for.")
    single = validate_graph(reqs(PAD, BERM), hauls(400), volume=True, supply=False)
    assert single is not None and single.details[0].endswith(
        "a double count: H1 400 m3 (a body's m3 is hauled once)")
    # a key trench dug twice over, its berm hauled in full: the trench's own words
    dust = {"id": "S2", "type": "dust_berm", "zone": "Z2", "length_m": 60, "height_m": 1.2,
            "width_m": 3, "trench_width_m": 1.2, "trench_depth_m": 0.8}
    keyed = TaskGraph(tasks=[task("K1", "excavator", 57.6, name="Cut key trench part 1"),
                             task("K2", "excavator", 57.6, name="Cut key trench part 2"),
                             task("H1", "hauler", 108, after=("K2",))])
    failure = validate_graph(reqs(dust), keyed, volume=True, supply=False)
    assert failure is not None and failure.conflicts == ["volume:S2"]
    assert failure.details == [
        ("S2 (dust_berm): its key trench is 60 x 1.2 x 0.8 m = 57.6 m3 of excavation; excavator "
         "tasks remove 115.2 m3 (200 %), a double count: K1 57.6 m3, K2 57.6 m3 (parts split a "
         "body's m3, they do not each repeat it)")]
    assert failure.hint == (
        "S2 (dust_berm) plans 115.2 m3 of digs for its 57.6 m3 key trench: cut the S2 key trench "
        "digs to 57.6 m3 in total (for example 2 x 28.8 m3), the hauls of its spoil still after "
        "the dig parts that remove it.")
    # a range: the share is of the largest size the brief allows, the cut to that size
    ranged = reqs(PAD, {**BERM, "min_length_m": 30})
    wide = validate_graph(ranged, hauls(120, 120), volume=True, supply=False)
    assert wide is not None and (
        "hauler tasks move 240 m3 (200 % of the largest size the brief allows), a double count"
        in wide.details[0]) and "cut the S2 hauls to 120 m3 in total" in wide.hint


@pytest.mark.parametrize(("phrase", "article"), [
    ("120 m3 berm", "a"), ("80 m3 berm", "an"), ("8 m by 8 m footprint", "an"),
    ("11 m3 trench", "an"), ("11.5 m3 trench", "an"), ("18,000 m3 pit", "an"),
    ("180 m3 pit", "a"), ("1,800 m3 pit", "a"), ("0.8 m3 hole", "a"), ("excavator", "an"),
    ("hauler", "a"),
])
def test_a_figure_takes_the_article_it_is_read_with(phrase: str, article: str) -> None:
    assert an(phrase) == f"{article} {phrase}"


def test_a_double_count_and_a_shortfall_share_one_volume_group() -> None:
    """A berm hauled twice over on a key trench dug short: one ``volume:S2`` id, one detail line
    joining both shapes, both sentences in the hint; the group sits where the shortfall sat,
    after coverage and before supply and energy."""
    dust = {"id": "S2", "type": "dust_berm", "zone": "Z2", "length_m": 60, "height_m": 1.2,
            "width_m": 3, "trench_width_m": 1.2, "trench_depth_m": 0.8}
    g = TaskGraph(tasks=[task("K1", "excavator", 5, name="Cut key trench"),
                         task("H1", "hauler", 108, after=("K1",)),
                         task("H2", "hauler", 108, after=("K1",))])
    failure = validate_graph(reqs(dust), g, volume=True, supply=False)
    assert failure is not None and failure.conflicts == ["volume:S2"]
    assert failure.details == [
        ("S2 (dust_berm): dimensions 60 m long, 1.2 m high, 3 m wide at the base, triangular "
         "section = 108 m3 of fill; hauler tasks move 216 m3 (200 %), a double count: H1 108 m3, "
         "H2 108 m3 (216 m3 is the 60 x 1.2 x 3 m box, twice the triangular section) and its key "
         "trench is 60 x 1.2 x 0.8 m = 57.6 m3 of excavation; excavator tasks cover 5 m3 (8 %): "
         "K1 5 m3")]
    assert failure.hint.startswith("S2 (dust_berm) plans 216 m3 of hauls for its 108 m3 dust berm")
    assert "S2 (dust_berm) plans 5 of 57.6 m3: plan the remaining 52.6 m3" in failure.hint
    assert volume_shortfalls(reqs(dust), g)[0].shape.kind == "trench"
    # ordering: the double count of S1 before the supply verdict of the haul that waits for no dig
    r = reqs(PAD, BERM)
    loose = TaskGraph(tasks=[task("D1", "excavator", 125.66, "S1", "Z1"),
                             task("D2", "excavator", 125.66, "S1", "Z1"),
                             task("H1", "hauler", 120)])
    order = validate_graph(r, loose, volume=True)
    assert order is not None and order.conflicts == ["volume:S1", "supply:H1"]


# ----------------------------------------------------------------------------- what still passes
def _budget_module():
    spec = importlib.util.spec_from_file_location("bb_double", ROOT / "scripts" / "brief_budget.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bb_double"] = module
    spec.loader.exec_module(module)
    return module


def test_every_canonical_graph_and_fixture_plan_still_validates() -> None:
    """Every eval brief's lazy, split, fine, serial and shrunk graph (scripts/brief_budget.py)
    splits its bodies instead of repeating them and gets no new volume verdict (the shrunk
    re-scope keeps its designed shortfalls only), the registry's structures need no footprint
    correction, and each recorded demo fixture's final verified graph validates as it did."""
    bb = _budget_module()
    for path in sorted((ROOT / "examples" / "eval").glob("*.md")):
        budget = bb.compute_budget(bb.spec_from_brief(path), path.stem)
        live_req = budget.requirements.model_copy(update={"source_brief": path.read_text()})
        assert all(with_judged_footprint(s, live_req) is s for s in live_req.structures), path.stem
        for variant in bb.GRAPH_VARIANTS:
            graph = bb.build_graph(budget, variant)
            assert volume_double_counts(live_req, graph) == [], (path.stem, variant)
            shortfalls = {f"volume:{c.structure.id}" for c in volume_shortfalls(live_req, graph)}
            failure = validate_graph(live_req, graph, coverage=True)
            volume = {c for c in (failure.conflicts if failure else []) if c.startswith("volume:")}
            assert volume == shortfalls, (path.stem, variant, volume)
            if variant != "shrunk":
                assert volume == set(), (path.stem, variant, volume)
    for name in ("landing_pad", "access_road", "tight_battery"):
        text = (ROOT / "examples" / "briefs" / f"{name}.md").read_text()
        result = Planner(FixtureClient(), explain=False).plan(text, brief_id=name)
        assert result.success and result.task_graph is not None, name
        assert validate_graph(result.requirements, result.task_graph, coverage=True) is None, name
        assert volume_double_counts(result.requirements, result.task_graph) == [], name


def test_the_repair_prompt_covers_both_directions_within_the_cap() -> None:
    flat = " ".join(REPAIR_SYSTEM.split())
    assert ("volume:<structure> (planned m3 off the dimensions) -> add or cut m3 as the hint "
            "says, never shrink the body.") in flat
    assert "from 175 % rejected" in " ".join(COVERAGE_RULES.split())
    for system in (STRUCTURE_SYSTEM, TASKGRAPH_SYSTEM, REPAIR_SYSTEM):
        assert len(system) < 5500
