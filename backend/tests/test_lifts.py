"""Tests for the reliable in-scope metric of phase 3g (``moonwright.pipeline.rates``): the lift
count is the one a structure's own entry in the brief states (:func:`stated_lifts`, read
through :func:`moonwright.pipeline.tags.entry_line`, the entry whole when it is hard-wrapped),
the structure stage writes that count into the requirements and says so in an amber warning,
and a placed body is judged for compaction on the base width the brief states, else on the
least base it can have (as wide as it is high). The structures below are the ones Nemotron 3.5
Lightning recorded in live run 2 (types and extras as saved), read against the eval briefs
themselves."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from moonwright.llm.base import Usage
from moonwright.pipeline.rates import (
    area_coverages,
    area_shares,
    base_width_stated,
    lift_note,
    lifts,
    recorded_lifts,
    scope_gaps,
    stated_lifts,
    with_judged_lifts,
)
from moonwright.pipeline.structure import correct_lifts, lift_warnings, structure_brief
from moonwright.pipeline.tags import brief_lines, entry_line
from moonwright.schemas import Requirements, Structure, Task, TaskGraph

EVAL = Path(__file__).resolve().parents[1] / "examples" / "eval"


def brief(name: str) -> str:
    return (EVAL / f"{name}.md").read_text()


def wrapped(name: str, width: int = 90) -> str:
    """The brief hard-wrapped at ``width`` columns, continuation lines indented (as an editor
    writes markdown): an entry's tail lands on lines of its own."""
    return "\n".join("\n".join(textwrap.wrap(line, width, subsequent_indent="   "))
                     if len(line) > width else line for line in brief(name).splitlines())


def reqs(*structures: dict, source: str | None = None) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (8 * i, 10)} for i in range(1, 5)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures],
        robots=[{"id": "EX1", "type": "excavator", "battery_wh": 5000},
                {"id": "HL1", "type": "hauler", "battery_wh": 3000},
                {"id": "CP1", "type": "compactor", "battery_wh": 3500}],
        horizon_h=336, source_brief=source)


def compact(id_: str, m2: float, structure: str = "S1", zone: str = "Z1") -> Task:
    return Task(id=id_, name=f"Compact {structure} {id_}", robot_type="compactor", duration_h=4,
                energy_wh=100.0, zone=zone, structure=structure, quantity=m2, unit="m2")


# ----------------------------------------------------------------------------- the patterns
@pytest.mark.parametrize(("line", "count"), [
    ("placed in three compacted lifts from a borrow pit", 3),
    ("in three lifts", 3),
    ("three compacted lifts", 3),
    ("two compaction lifts", 2),
    ("compacted in one lift and inspected", 1),
    ("compact the whole 540 square metre surface in two full lifts", 2),
    ("placed and compacted in two lifts and inspected", 2),
    ("compacted in 2 layers", 2),
    ("placed in two layers and compacted", 2),
    ("in 4 lifts", 4),
    ("in two 15 cm lifts", 2),
    ("in three 30-cm lifts", 3),
    ("in three 0.3 m lifts", 3),
    ("a two-lift berm", 2),
    ("in a single lift", 1),
    ("Ten Lifts", 10),
    ("lifts: 3", 3),
    # an article is one lift, read only when no clause states a number
    ("graded and compacted regolith with a 20 cm compacted lift", 1),
    ("with a compacted 10 cm bedding layer in each segment", 1),
    ("a 0.2 m lift", 1),
    ("an even compacted layer", 1),
    ("on a compacted base layer, then placed in three lifts", 3),
    # a range states its smaller count, the smallest size the brief allows
    ("in 3 to 4 lifts", 3),
    ("compacted in two to three lifts", 2),
    ("compacted in 2-3 lifts", 2),
])
def test_a_stated_count_of_lifts_is_read(line: str, count: int) -> None:
    assert stated_lifts(line) == count


@pytest.mark.parametrize("line", [
    "in four 10 m segments from the top down",                          # segments, not lifts
    "a 25 cm layer of fines", "in 12 lifts", "in 0 lifts", "none", "",
    "a 20 cm compacted lift, then 12 lifts",              # a number out of range wins
    "Each lift is compacted before the next lift is placed",
    "checks the level before the second compaction lift",
    # layers that nothing compacts, or that a printer makes, are no lifts
    "2 layers", "covered with one layer of geotextile", "sintered in two layers",
    "printed in 3 layers of 10 cm, compacted beneath",
    # a hyphenated layer compound names a kind of thing, not a count
    "a two-layer regolith shield, compacted", "with a 2-layer sandbag face, compacted",
])
def test_a_thickness_segments_or_no_count_state_no_lifts(line: str) -> None:
    assert stated_lifts(line) is None
    assert stated_lifts(None) is None


# ----------------------------------------------------------------------------- the eval briefs
LIVE = [   # (brief, the structure Lightning recorded, expected lifts)
    ("crater_ramp", {"id": "S1", "type": "access_ramp", "zone": "Z1", "length_m": 40,
                     "width_m": 5, "depth_m": 1.2, "lifts": 4}, 1),
    ("access_road", {"id": "S1", "type": "access_road", "zone": "Z1", "length_m": 120,
                     "width_m": 4, "lifts": 0.2}, 1),
    ("shielding_mound", {"id": "S1", "type": "shielding_mound", "zone": "Z1", "diameter_m": 4,
                         "height_m": 2, "thickness_m": 1.5, "lifts": 3}, 3),
    ("solar_array_pad", {"id": "S1", "type": "solar_array_pad", "zone": "Z1", "length_m": 30,
                         "width_m": 18, "depth_m": 0.3, "lifts": 2}, 2),
    ("habitat_berm_tight_horizon", {"id": "S2", "type": "berm", "zone": "Z2", "length_m": 70,
                                    "height_m": 2, "width_m": 4}, 1),
    ("small_fleet_long_berm", {"id": "BERM", "type": "spool_berm", "zone": "Z1",
                               "length_m": 100, "height_m": 2, "width_m": 3.5}, 2),
    ("depleted_excavators", {"id": "STR2", "type": "spoil_berm", "zone": "Z2", "length_m": 60,
                             "height_m": 1.2, "width_m": 3}, 1),
    ("cable_trench", {"id": "S1", "type": "cable_trench", "zone": "Z1", "length_m": 150,
                      "width_m": 0.4, "depth_m": 0.6, "lifts": 3}, 1),
]


@pytest.mark.parametrize(("name", "structure", "expected"), LIVE, ids=[n for n, _, _ in LIVE])
def test_the_eval_briefs_state_their_lifts_in_the_structures_own_entry(
        name: str, structure: dict, expected: int) -> None:
    r = reqs(structure, source=brief(name))
    s = r.structures[0]
    line = entry_line(r.source_brief, s)
    assert line is not None and line.lstrip()[:1].isdigit()     # its numbered entry
    assert lifts(s, r) == expected
    # hard-wrapped at 60 columns the entry still reads whole, and the count is the same
    folded = reqs(structure, source=wrapped(name, 60))
    assert entry_line(folded.source_brief, folded.structures[0]) == " ".join(line.split())
    assert lifts(folded.structures[0], folded) == expected


def test_without_the_brief_or_an_entry_the_recorded_count_stands() -> None:
    for extras, expected in (({"lifts": 4}, 4), ({"lifts": 0.2}, 1), ({"lift_count": 2}, 2),
                             ({"compaction_lifts": 3.0}, 3), ({"lifts": 11}, 1), ({}, 1),
                             ({"lifts": True}, 1), ({"lifts": [{"thickness_m": 0.1}]}, 1)):
        s = Structure(id="S1", type="pad", **extras)
        assert lifts(s) == expected, extras
        assert lifts(s, reqs({"id": "S1", "type": "pad", **extras})) == expected, extras
    # a type no line of the brief names: nothing to read, the record stands, no correction
    odd = reqs({"id": "S9", "type": "plinth", "lifts": 3}, source=brief("crater_ramp"))
    assert entry_line(odd.source_brief, odd.structures[0]) is None
    assert lifts(odd.structures[0], odd) == 3 and lift_warnings(odd) == []


def test_an_entry_that_names_lifts_without_a_count_keeps_a_recorded_one() -> None:
    """"placed in 50 cm lifts, three in all": the entry names lifts but states no count the
    pattern reads, so a recorded count of 1 to 10 stands (a recorded 0.2 never does)."""
    text = "1. Shield berm: 30 m long, 2 m high, placed in 50 cm lifts, three in all."
    berm = {"id": "S1", "type": "shield_berm", "zone": "Z1", "length_m": 30, "height_m": 2}
    kept = reqs(berm | {"lifts": 3}, source=text)
    assert lifts(kept.structures[0], kept) == 3 and lift_warnings(kept) == []
    thin = reqs(berm | {"lifts": 0.2}, source=text)
    assert lifts(thin.structures[0], thin) == 1
    assert lift_warnings(thin) == [
        "structure S1: lifts 0.2 -> 1: the brief's entry for S1 states no lift count we can read"]


def test_the_entry_line_skips_headings_and_prefers_an_entry_on_a_tie() -> None:
    ramp = Structure(id="S1", type="access_ramp")
    assert entry_line(brief("crater_ramp"), ramp).startswith("1. Access ramp: 40 m long")
    spool = Structure(id="BERM", type="spool_berm")            # one word of the type matches
    assert entry_line(brief("small_fleet_long_berm"), spool).startswith("1. Protective berm:")
    road = Structure(id="S1", type="road")
    assert entry_line(brief("road_and_pad"), road).startswith("1. Service road:")
    assert entry_line("# Notes\nnothing here", Structure(id="S1", type="pad")) is None


def test_brief_lines_join_a_hard_wrapped_entry_and_keep_blocks_apart() -> None:
    text = ("# Berm\n\n**Structures.**\n\n1. Protective berm: 100 m long, 2 m high, placed and\n"
            "   compacted in two\nlifts and inspected.\n2. Borrow strip: 100 m by 6 m.\n"
            "**Fleet.** EX1\n| a | b |\n| 1 | 2 |\n- one\n  - two")
    assert brief_lines(text) == [
        "# Berm", "**Structures.**",
        "1. Protective berm: 100 m long, 2 m high, placed and compacted in two lifts and inspected.",
        "2. Borrow strip: 100 m by 6 m.", "**Fleet.** EX1", "| a | b |", "| 1 | 2 |", "- one",
        "  - two"]
    # a brief with one line per entry reads exactly as its lines
    for path in sorted(EVAL.glob("*.md")):
        assert brief_lines(path.read_text()) == [ln for ln in path.read_text().splitlines()
                                                 if ln.strip()], path.stem


# ----------------------------------------------------------------------------- the correction
def test_the_structure_stage_corrects_a_lift_count_to_the_brief() -> None:
    notes = {name: lift_note(r.structures[0], r) for name, r in (
        (n, reqs(s, source=brief(n))) for n, s, _ in LIVE)}
    assert notes == {
        "crater_ramp": "lifts 4 -> 1: the brief's entry for S1 states no lift count we can read",
        "access_road": "lifts 0.2 -> 1: the brief's entry for S1 states 1 compaction lift",
        "shielding_mound": None, "solar_array_pad": None, "habitat_berm_tight_horizon": None,
        "small_fleet_long_berm": "lifts none -> 2: the brief's entry for BERM states 2 compaction lifts",
        "depleted_excavators": None,
        "cable_trench": "lifts 3 -> 1: the brief's entry for S1 states 1 compaction lift",
    }
    wrong = reqs({"id": "S1", "type": "solar_array_pad", "length_m": 30, "width_m": 18,
                  "lift_count": 3}, source=brief("solar_array_pad"))
    assert recorded_lifts(wrong.structures[0]) == ("lift_count", 3.0)
    assert correct_lifts(wrong) == [
        "structure S1: lift_count 3 -> 2: the brief's entry for S1 states 2 compaction lifts"]
    assert wrong.structures[0].dims() == {"length_m": 30.0, "width_m": 18.0, "lifts": 2.0}
    assert correct_lifts(wrong) == []                              # the record stands now
    # no brief, no verdict on the recorded count
    assert lift_warnings(reqs({"id": "S1", "type": "pad", "lifts": 4})) == []


def test_the_correction_writes_the_count_the_planner_will_plan() -> None:
    """The task-graph model never sees the brief: crater_ramp's ramp loses the 4 segments it
    read as lifts (one lift, the default, so no key), small_fleet's berm gains the 2 lifts
    the brief states, and an entry that says "in one lift" over a recorded 4 keeps an explicit
    1."""
    ramp = reqs(LIVE[0][1], source=brief("crater_ramp"))
    assert correct_lifts(ramp) and "lifts" not in (ramp.structures[0].model_extra or {})
    assert ramp.structures[0].dims() == {"length_m": 40.0, "width_m": 5.0, "depth_m": 1.2}
    berm = reqs(LIVE[5][1], source=brief("small_fleet_long_berm"))
    correct_lifts(berm)
    assert berm.structures[0].model_extra["lifts"] == 2 and berm.structures[0].type == "spool_berm"
    habitat = reqs(LIVE[4][1] | {"lifts": 4}, source=brief("habitat_berm_tight_horizon"))
    assert lift_warnings(habitat) == [
        "structure S2: lifts 4 -> 1: the brief's entry for S2 states 1 compaction lift"]
    assert with_judged_lifts(habitat.structures[0], habitat).model_extra["lifts"] == 1
    # a hard-wrapped brief never erases a count it states (phase 3g read the first line only)
    folded = reqs(LIVE[5][1] | {"lifts": 2}, source=wrapped("small_fleet_long_berm"))
    assert lift_warnings(folded) == [] and lifts(folded.structures[0], folded) == 2


class RampClient:
    """Answers the structure request with crater_ramp's ramp as Lightning recorded it."""

    name = "ramp"

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        answer = reqs(LIVE[0][1], {"id": "S2", "type": "rim_turnaround", "zone": "Z2"})
        return schema.model_validate(answer.model_dump()), Usage(model="ramp")


def test_the_lift_correction_rides_the_structure_step() -> None:
    req, step = structure_brief(RampClient(), brief("crater_ramp"), key="k")
    assert req is not None and step.ok
    assert "lifts" not in (req.structures[0].model_extra or {})       # corrected in place
    warnings = step.payload["warnings"]
    lift = "structure S1: lifts 4 -> 1: the brief's entry for S1 states no lift count we can read"
    assert lift in warnings
    # before the dimension warnings, which stay last (S2 has no dimension)
    assert warnings.index(lift) < len(warnings) - 1
    assert warnings[-1].startswith("structure S2 (rim_turnaround) has no dimensions")


# ----------------------------------------------------------------------------- the area judged
def test_a_ramp_compacted_once_per_segment_is_in_scope() -> None:
    """crater_ramp: four 10 m segments of a 40 x 5 m ramp, each compacted once. With the
    recorded "lifts: 4" the plan read 25 % of its compaction; the brief states no lift count."""
    ramp = LIVE[0][1]
    graph = TaskGraph(tasks=[compact(f"C{i}", 50) for i in range(1, 5)])
    live = reqs(ramp, source=brief("crater_ramp"))
    assert area_shares(live, graph) == {"S1": 1.0}
    assert not any(g.startswith("area:") for g in scope_gaps(live, graph))
    old = reqs(ramp)                                              # the recorded count alone
    assert area_shares(old, graph) == {"S1": 0.25}
    assert any(g.startswith("area:S1: its compactor tasks treat 200 of 800 m2 (25 %; 200 m2 x 4 "
                            "lifts)") for g in scope_gaps(old, graph))
    # compacting it four times over is no gap either way: an excess is not under-scope
    four = TaskGraph(tasks=[compact(f"C{i}", 200) for i in range(1, 5)])
    assert area_shares(live, four) == {"S1": 4.0}


def test_two_lifts_the_brief_states_are_judged_even_when_unrecorded() -> None:
    berm = LIVE[5][1]                                             # spool_berm, no lifts key
    live = reqs(berm, source=brief("small_fleet_long_berm"))
    once = TaskGraph(tasks=[compact("C1", 350, "BERM")])
    assert area_shares(live, once) == {"BERM": 0.5}
    [cov] = area_coverages(live, once)
    assert cov.how == "350 m2 x 2 lifts" and cov.need_m2 == 700


@pytest.mark.parametrize(("name", "structure", "stated", "need"), [
    # "20 m long, 1.5 m high": no base width, the 1:1 section is a guess
    ("access_road", {"id": "S3", "type": "protective_berm", "zone": "Z3", "length_m": 20,
                     "height_m": 1.5}, False, 30),
    # "a 30 m arc ... 2.5 m high": the 4 m Lightning recorded is the prompt example's
    ("propellant_pit_and_berm", {"id": "S2", "type": "protective_berm", "zone": "Z2",
                                 "length_m": 30, "height_m": 2.5, "width_m": 4}, False, 75),
    ("landing_pad", {"id": "S2", "type": "spoil_berm", "zone": "Z2", "length_m": 60,
                     "height_m": 2.5, "width_m": 4}, True, 240),          # "4 m wide at the base"
    ("tight_battery", {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60,
                       "height_m": 2.5, "width_m": 4, "trench_width_m": 0.5,
                       "trench_depth_m": 0.5}, True, 240),                # "4 m at the base"
    ("small_fleet_long_berm", LIVE[5][1], True, 700),                     # "3.5 m wide", 2 lifts
    # the key trench's 1.2 m is no base width (nor is the 1.2 m height)
    ("depleted_excavators", {"id": "S2", "type": "dust_berm", "zone": "Z2", "length_m": 60,
                             "height_m": 1.2, "width_m": 1.2}, False, 72),
    ("depleted_excavators", LIVE[6][1], True, 180),                       # "3 m wide at the base"
    # Lightning typed the dust berm "borrow_strip", whose words pick the borrow pit's entry:
    # the width is read on the line that states the berm's 60 m and 1.2 m
    ("depleted_excavators", {"id": "S2", "type": "borrow_strip", "zone": "Z2", "length_m": 60,
                             "height_m": 1.2, "width_m": 3}, True, 180),
])
def test_a_placed_body_is_judged_on_its_stated_base_or_the_least_one(
        name: str, structure: dict, stated: bool, need: float) -> None:
    live = reqs(structure, source=brief(name))
    s = live.structures[0]
    assert base_width_stated(s, live) is stated
    graph = TaskGraph(tasks=[compact("C1", 10, s.id, s.zone or "Z1")])
    [cov] = area_coverages(live, graph)
    assert cov.need_m2 == pytest.approx(need)
    assert ("at a base as wide as it is high" in cov.how) is not stated
    # without the brief a recorded width is taken as stated
    plain = reqs(structure)
    assert base_width_stated(plain.structures[0]) is ("width_m" in structure)


def test_a_placed_body_without_a_stated_base_is_judged_on_the_least_one() -> None:
    """access_road's berm "20 m long, 1.5 m high" read 33-50 % of a 60 m2 base the brief never
    gave (the 1:1 section); the least base it can have, as wide as it is high, is 30 m2: a
    plan that compacts 30 m2 is in scope, a 20 m2 pass is not, and the volume (the 1:1
    section, the least it holds) is judged as before."""
    berm = {"id": "S3", "type": "protective_berm", "zone": "Z3", "length_m": 20, "height_m": 1.5}
    live = reqs(berm, source=brief("access_road"))
    hauled = Task(id="H1", name="Haul foundation spoil to berm", robot_type="hauler",
                  duration_h=4, energy_wh=100.0, zone="Z3", structure="S3", quantity=45.0,
                  unit="m3")
    full = TaskGraph(tasks=[hauled, compact("C1", 30, "S3", "Z3")])
    assert area_shares(live, full) == {"S3": 1.0} and scope_gaps(live, full) == []
    thin = TaskGraph(tasks=[hauled, compact("C1", 20, "S3", "Z3")])
    assert scope_gaps(live, thin) == [
        ("area:S3: its compactor tasks treat 20 of 30 m2 (66 %; 30 m2 at a base as wide as it is "
         "high), under the 95 % a plan in scope covers")]
    short = TaskGraph(tasks=[hauled.model_copy(update={"quantity": 20.0}), compact("C1", 30, "S3", "Z3")])
    assert [g.split(":")[0] for g in scope_gaps(live, short)] == ["volume"]
