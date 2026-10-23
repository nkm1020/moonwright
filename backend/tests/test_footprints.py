"""Tests for the footprint correction of phase 3i (``moonwright.pipeline.rates``, "Footprints"):
a square or rectangular footprint is never a circle. Nemotron 3.5 Lightning recorded the "12 m
by 12 m" pit of habitat_foundation and habitat_berm_tight_horizon, instrument_pad's "10 m by
10 m" pad and four_mast_pads' "each 8 m by 8 m" pads as ``diameter_m``, so a pit read as a
113.1 m3 bowl instead of a 144 m3 box (78.5 %, above the 70 % conflict): the planner under-dug
and the eval judged the plan in scope against the wrong floor. The reader
(:func:`stated_footprint`) takes the rectangle of the structure's own entry, read whole and
never from an entry that calls it round, the structure stage writes it into the requirements
with an amber note, and every volume and area judgment applies it whenever the brief is known.
The structures below are the ones Lightning recorded in live runs 2 and 3 (types and extras as
saved; ``tests/data/recorded_structures.json`` holds every one), read against the eval briefs
themselves; so are the review's cases that fooled the first reader (a bowl sharing its figure
with the apron around it, counts and trench sections read as footprints, a neighbour's
rectangle borrowed, a stated smaller rectangle lost)."""

from __future__ import annotations

import json
import math
import textwrap
from pathlib import Path

import pytest

from moonwright.cli import eval_row
from moonwright.llm.base import Usage
from moonwright.pipeline.rates import (
    area_shares,
    dims_volume,
    footprint,
    footprint_note,
    scope_gaps,
    stated_footprint,
    stated_footprints,
    volume_shares,
    with_judged_footprint,
)
from moonwright.pipeline.structure import correct_footprints, footprint_warnings, structure_brief
from moonwright.pipeline.tags import entry_line
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import (
    LoopStep,
    PlanResult,
    Requirements,
    Schedule,
    Structure,
    Task,
    TaskGraph,
)

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "examples" / "eval"
RECORDED = Path(__file__).resolve().parent / "data" / "recorded_structures.json"
# the briefs whose structures' own entries state a rectangle Lightning recorded as a diameter
RECTANGLES = ("habitat_foundation", "habitat_berm_tight_horizon", "instrument_pad",
              "four_mast_pads")


def brief(name: str) -> str:
    return (EVAL / f"{name}.md").read_text()


def reqs(*structures: dict, source: str | None = None) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (8 * i, 10)} for i in range(1, 5)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures],
        robots=[{"id": "EX1", "type": "excavator", "battery_wh": 5000},
                {"id": "EX2", "type": "excavator", "battery_wh": 5000},
                {"id": "CP1", "type": "compactor", "battery_wh": 3500},
                {"id": "SV1", "type": "surveyor", "battery_wh": 1500}],
        horizon_h=288, source_brief=source)


def task(id_: str, robot_type: str, quantity: float, unit: str, structure: str = "F1",
         zone: str = "Z1", after: tuple[str, ...] = ()) -> Task:
    return Task(id=id_, name=f"{robot_type} {structure} {id_}", robot_type=robot_type,
                duration_h=4, energy_wh=100.0, zone=zone, structure=structure,
                quantity=quantity, unit=unit, after=list(after))


# ----------------------------------------------------------------------------- the reader
@pytest.mark.parametrize(("line", "found"), [
    ("1. Foundation pit: 12 m by 12 m, 1.0 m deep (about 144 cubic metres)", (12, 12)),
    ("1. Instrument pad: 10 m by 10 m, stripped of fines", (10, 10)),
    ("a 10 x 10 m apron.", (10, 10)),
    ("3. Borrow strip: 40 m by 6 m, 0.5 m deep", (40, 6)),
    ("a level rectangle 30 m x 18 m. Strip the loose regolith", (30, 18)),
    ("3. Rover charging apron: 10 m by 20 m, stripped", (10, 20)),
    ("a 10 m × 20 m apron", (10, 20)),
    ("a sump 120 cm by 80 cm", (1.2, 0.8)),
    ("a 1.5 m by 50 cm slot", (1.5, 0.5)),
    ("10x10m pad", (10, 10)),
    ("12 M BY 12 M", (12, 12)),
    # length x width x depth: the first two are the footprint
    ("a 12 x 12 x 1 m pit", (12, 12)),
    ("12 m x 12 m x 1 m deep", (12, 12)),
    # a pair without a length unit is passed over, not the line
    ("dug in 2 x 6 h shifts, 12 m by 12 m", (12, 12)),
    # the first pair of the entry: four_mast_pads' anchor hole comes after its footprint
    (("1. Mast pad NE, 2. mast pad NW, 3. mast pad SE, 4. mast pad SW: each 8 m by 8 m, "
      "stripped of fines, levelled, compacted, with a 1 m deep central anchor hole"), (8, 8)),
    # "bearing" and "around" are no round words
    (("1. Foundation pit: 12 m by 12 m, excavated 1.0 m deep with vertical sides, then a "
      "sintered 20 cm bearing slab printed over the base."), (12, 12)),
])
def test_a_stated_footprint_is_read(line: str, found: tuple[float, float]) -> None:
    assert stated_footprint(line) == pytest.approx(found)


@pytest.mark.parametrize("line", [
    # the entry calls its structure round, whatever pair it also holds
    "1. Landing pad: circular, 20 m diameter. Excavate a bowl 0.4 m deep",
    "1. Beacon pad: a circular bowl 12 m in diameter excavated 0.5 m to firm regolith",
    "1. Pad: 15 m diameter, on a 30 m by 30 m level patch",
    "a round pad, 12 m by 12 m",
    "a ring of 6 m by 6 m slabs", "a 10 m by 10 m pad of 5 m radius",
    # a size and a depth, a count times a volume, no pair at all
    "a bowl 20 m by 0.4 m deep", "2 x 60 m3", "2 x 150 m3 of fill",
    "2. Perimeter drainage and cable trench: 56 m around the pit, 0.4 m wide and 0.6 m deep",
    "between 12 m and 20 m", "a 0 m by 3 m", "", "4 m in diameter by 2 m",
    # seen in review, each read as a footprint: a bowl's size and depth, a count times a length,
    # the parts of the work, a section, a wall's height and width
    "a bowl 20 m by 0.4 m, to firm regolith", "a pit 20 m by 0.4 m, deep",
    "two parallel trenches, 2 x 60 m long",
    "Cable trench: 150 m long, 0.5 m wide and 0.6 m deep, laid as 3 x 50 m segments",
    "Cable trench: 3 x 50 m segments, 0.6 m deep",
    "Drain trench: 60 m long, a 1.2 m by 0.8 m trench section",
    "Key trench: 1.2 m x 0.8 m section under the berm", "sintered in 2 m x 2 m tiles",
    "Shield wall: 30 m long, 2 m high and 4 m wide",
    "1. Landing pad: a 20 m bowl excavated 0.4 m deep, centred in a 20 m by 30 m cleared apron",
])
def test_a_round_entry_a_depth_or_a_count_states_no_footprint(line: str) -> None:
    assert stated_footprint(line) is None and stated_footprints(line) == []
    assert stated_footprint(None) is None


def test_every_stated_rectangle_is_read_in_order() -> None:
    """The footprint is the first; a smaller one later in the entry is the size the brief lets
    the work shrink to (``test_a_stated_minimum_rectangle_is_written_in``)."""
    assert stated_footprints("from 10 m by 10 m up to 12 m by 12 m") == [(10, 10), (12, 12)]
    assert stated_footprints("12 x 12 m, laid as 3 x 4 m tiles, then 6 m by 6 m") == [
        (12, 12), (6, 6)]
    assert stated_footprints("a 12 m by 12 m pit, dug in four sections") == [(12, 12)]


# ----------------------------------------------------------------------------- the live briefs
LIVE = [   # (brief, the structure Lightning recorded, the note, the dims after the correction)
    ("habitat_foundation",     # run 2, 3 of 3; run 3 #1
     {"id": "F1", "type": "foundation_pit", "zone": "Z1", "diameter_m": 12, "length_m": 12,
      "depth_m": 1.0, "thickness_m": 0.2},
     "diameter_m 12 -> 12 m x 12 m: the brief's entry for F1 states a 12 m by 12 m footprint",
     {"length_m": 12, "depth_m": 1.0, "thickness_m": 0.2, "width_m": 12}),
    ("habitat_foundation",     # run 3 #3: a type no line names; the line that states its size
     {"id": "F1", "type": "excavation", "zone": "Z1", "diameter_m": 12, "depth_m": 1.0,
      "thickness_m": 0.2},
     "diameter_m 12 -> 12 m x 12 m: the brief's entry for F1 states a 12 m by 12 m footprint",
     {"depth_m": 1.0, "thickness_m": 0.2, "length_m": 12, "width_m": 12}),
    ("habitat_berm_tight_horizon",     # run 2, 3 of 3
     {"id": "S1", "type": "foundation_pit", "zone": "Z1", "diameter_m": 12, "length_m": 12,
      "depth_m": 1.0},
     "diameter_m 12 -> 12 m x 12 m: the brief's entry for S1 states a 12 m by 12 m footprint",
     {"length_m": 12, "depth_m": 1.0, "width_m": 12}),
    ("habitat_berm_tight_horizon",     # run 3 #1: an invented minimum goes with the diameter
     {"id": "S1", "type": "excavate", "zone": "Z1", "diameter_m": 12, "depth_m": 1.0,
      "min_diameter_m": 12, "min_depth_m": 1.0},
     ("diameter_m 12, min_diameter_m 12 -> 12 m x 12 m: the brief's entry for S1 states a 12 m "
      "by 12 m footprint"),
     {"depth_m": 1.0, "min_depth_m": 1.0, "length_m": 12, "width_m": 12}),
    ("habitat_berm_tight_horizon",     # run 3 #2: a recorded minimum length stays
     {"id": "S1", "type": "excavate", "zone": "Z1", "diameter_m": 12, "length_m": 12,
      "depth_m": 1.0, "min_length_m": 12, "min_depth_m": 1.0},
     "diameter_m 12 -> 12 m x 12 m: the brief's entry for S1 states a 12 m by 12 m footprint",
     {"length_m": 12, "depth_m": 1.0, "min_length_m": 12, "min_depth_m": 1.0, "width_m": 12}),
    ("habitat_berm_tight_horizon",     # run 3 #3: its words name a constraint, not the entry
     {"id": "S1", "type": "excavation_pit", "zone": "Z1", "diameter_m": 12, "length_m": 12,
      "depth_m": 1.0, "lifts": 1},
     "diameter_m 12 -> 12 m x 12 m: the brief's entry for S1 states a 12 m by 12 m footprint",
     {"length_m": 12, "depth_m": 1.0, "lifts": 1, "width_m": 12}),
    ("instrument_pad",     # run 2 #1: a length and a width that agree stay
     {"id": "INST1", "type": "instrument_pad", "zone": "Z1", "diameter_m": 10, "length_m": 10,
      "width_m": 10, "thickness_m": 0.15},
     ("diameter_m 10 -> 10 m x 10 m: the brief's entry for INST1 states a 10 m by 10 m "
      "footprint"),
     {"length_m": 10, "width_m": 10, "thickness_m": 0.15}),
    ("four_mast_pads",     # run 2 #1 (and every structuring of runs 2 and 3)
     {"id": "PAD1", "type": "mast_pad", "zone": "Z1", "diameter_m": 8, "length_m": 8,
      "height_m": 0.2, "depth_m": 1},
     "diameter_m 8 -> 8 m x 8 m: the brief's entry for PAD1 states an 8 m by 8 m footprint",
     {"length_m": 8, "height_m": 0.2, "depth_m": 1, "width_m": 8}),
]


@pytest.mark.parametrize(("name", "structure", "note", "dims"), LIVE,
                         ids=[f"{n}-{s['type']}" for n, s, _, _ in LIVE])
def test_the_live_briefs_recorded_footprints_are_corrected(
        name: str, structure: dict, note: str, dims: dict) -> None:
    r = reqs(structure, source=brief(name))
    s = r.structures[0]
    assert footprint_note(s, r) == note
    fixed = with_judged_footprint(s, r)
    assert fixed.dims() == pytest.approx(dims) and list(fixed.dims()) == list(dims)
    assert (fixed.id, fixed.type, fixed.zone) == (s.id, s.type, s.zone)
    assert "diameter_m" not in fixed.dims() and "min_diameter_m" not in fixed.dims()
    # a minimum only where one was recorded: none is made up for the length or the width
    assert not {"min_length_m", "min_width_m"} - set(s.dims()) & set(fixed.dims())
    assert footprint_note(fixed, r) is None and with_judged_footprint(fixed, r) is fixed
    # hard-wrapped at 60 columns the entry still reads whole, and the footprint is the same
    folded = reqs(structure, source="\n".join(
        "\n".join(textwrap.wrap(line, 60, subsequent_indent="   ")) if len(line) > 60 else line
        for line in brief(name).splitlines()))
    assert footprint(folded.structures[0], folded) == footprint(s, r)


def test_a_pit_read_as_a_box_holds_the_briefs_144_m3() -> None:
    """The arithmetic of the evidence: the 12 m pit recorded as a diameter is a 113.1 m3 bowl,
    pi/4 (78.5 %) of the 144 m3 box the brief states."""
    pit = LIVE[0][1]
    r = reqs(pit, source=brief("habitat_foundation"))
    assert dims_volume(r.structures[0]).m3 == pytest.approx(math.pi * 36)       # as recorded
    shape = dims_volume(r.structures[0], r)                                     # as the brief says
    assert (shape.m3, shape.kind, shape.how) == (pytest.approx(144), "box", "12 x 12 x 1 m")
    assert shape.floor_area_m2 == pytest.approx(144)


# ----------------------------------------------------------------------------- never invented
def test_no_footprint_is_invented() -> None:
    """No rectangle stated, nothing changes: a circular pad (its entry says so, whatever other
    pair the brief holds), a figure pair about something else, a pad typed "stockpile" that no
    line names, a structure without a recorded diameter, a requirements without its brief."""
    round_pads = [
        ("landing_pad", {"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.4}),
        ("front_loaded_excavation", {"id": "S1", "type": "heavy_lander_pad", "diameter_m": 30,
                                     "depth_m": 0.4}),
        ("depleted_excavators", {"id": "S1", "type": "beacon_pad", "diameter_m": 12,
                                 "depth_m": 0.5}),
        # the site's "30 m by 30 m level patch" is not the pad's
        ("road_and_pad", {"id": "S2", "type": "pad", "diameter_m": 15, "depth_m": 0.3}),
        # recorded as stockpiles: no line names them, and the line that states their size is round
        ("three_pads_in_order", {"id": "S1", "type": "stockpile", "diameter_m": 14,
                                 "depth_m": 0.4}),
        ("two_pads_one_printer", {"id": "PAD_A", "type": "stockpile", "diameter_m": 15,
                                  "depth_m": 0.4, "thickness_m": 0.25}),
    ]
    for name, pad in round_pads:
        r = reqs(pad, source=brief(name))
        assert footprint(r.structures[0], r) is None, name
        assert with_judged_footprint(r.structures[0], r) is r.structures[0], name
    assert footprint_warnings(reqs(*(p for _, p in round_pads[:1]), source=brief("landing_pad"))) == []
    # a 20 m pad beside a pair about something else: the recorded diameter is in no pair (and
    # a bowl is a round word: that entry states no rectangle at all)
    beside = reqs({"id": "S1", "type": "pad", "diameter_m": 20, "depth_m": 0.4},
                  source="1. Pad: a 20 m pad 0.4 m deep inside a 30 m by 30 m cleared square.")
    assert stated_footprint(entry_line(beside.source_brief, beside.structures[0])) == (30, 30)
    assert footprint(beside.structures[0], beside) is None
    assert stated_footprint("1. Pad: a 20 m bowl 0.4 m deep inside a 30 m by 30 m square.") is None
    # no recorded diameter, no brief, a type no line names whose size no line states
    pit = LIVE[0][1]
    flat = {k: v for k, v in pit.items() if k != "diameter_m"}
    assert footprint(reqs(flat, source=brief("habitat_foundation")).structures[0],
                     reqs(flat, source=brief("habitat_foundation"))) is None
    plain = reqs(pit)
    assert footprint(plain.structures[0], plain) is None and footprint(plain.structures[0]) is None
    odd = reqs({"id": "S9", "type": "plinth", "diameter_m": 5, "depth_m": 2},
               source=brief("habitat_foundation"))
    assert footprint(odd.structures[0], odd) is None
    # a round line that states the recorded diameter keeps it, even for a type no line names
    both = reqs({"id": "S1", "type": "excavate", "diameter_m": 12, "depth_m": 1},
                source=("1. Beacon pad: a circular bowl 12 m in diameter, 1 m deep.\n"
                        "2. Store: 12 m by 12 m, 1 m deep."))
    assert footprint(both.structures[0], both) is None
    # an entry that states the structure's own size without a pair never borrows another's,
    # nor does the same structure under a type no line names (seen in review: the store's
    # square read as the pad's): a line that states its size states no rectangle for it
    across = reqs({"id": "S1", "type": "landing_pad", "diameter_m": 12, "depth_m": 1},
                  source="1. Landing pad: 12 m across, 1 m deep.\n2. Store: 12 m by 12 m, 1 m deep.")
    assert footprint(across.structures[0], across) is None
    named = across.model_copy(update={"structures": [
        across.structures[0].model_copy(update={"type": "excavate"})]})
    assert footprint(named.structures[0], named) is None
    # nor a cable trench's segments: "3 x 50 m" sizes the parts of the trench (seen in review:
    # a 50 m pad typed "excavate" read as a 3 x 50 m box, 981.7 m3 down to 75)
    pad = reqs({"id": "S1", "type": "excavate", "diameter_m": 50, "depth_m": 0.5},
               source=("1. Landing pad: 50 m across, 0.5 m deep.\n2. Cable trench: 150 m long, "
                       "0.5 m deep, laid as 3 x 50 m segments."))
    assert footprint(pad.structures[0], pad) is None


def test_a_pair_that_shares_the_diameter_with_a_round_size_is_no_footprint() -> None:
    """Seen in review: a 20 m bowl centred in a 20 m by 30 m apron was corrected to a 20 x 30 m
    box (240 m3 for the 125.7 m3 bowl), and the plan that dug the bowl was rejected at 52 %.
    A bowl is a round word, and an entry that states the recorded diameter apart from the pair
    ("a 20 m pad") keeps it too; a pair that is the structure's own still corrects it."""
    apron = "centred in a 20 m by 30 m cleared apron so the hauler can turn."
    bowl = {"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4}
    for entry in (f"1. Landing pad: a 20 m bowl excavated 0.4 m deep, {apron}",
                  f"1. Landing pad: a 20 m pad excavated 0.4 m deep, {apron}"):
        r = reqs(bowl, source=entry)
        assert footprint(r.structures[0], r) is None, entry
        assert footprint_note(r.structures[0], r) is None, entry
        dug = TaskGraph(tasks=[task("T1", "excavator", 62.83, "m3", "S1"),
                               task("T2", "excavator", 62.83, "m3", "S1", after=("T1",))])
        assert validate_graph(r, dug, volume=True) is None, entry
    # the pit's own pair, its depth and volume beside it: corrected
    pit = reqs({**bowl, "diameter_m": 12, "depth_m": 1},
               source="1. Landing pad: 12 m by 12 m, 1 m deep (about 144 cubic metres).")
    assert footprint(pit.structures[0], pit) == (12, 12)


def test_a_stated_minimum_rectangle_is_written_in() -> None:
    """Seen in review: "20 m by 20 m ...; it may shrink to 12 m by 12 m" recorded as a 20 m
    diameter with a 12 m minimum lost the minimum, and a 12 x 12 m plan the brief allows read
    36 % of 200 m3. A recorded ``min_diameter_m`` that a smaller pair of the entry holds is
    written as ``min_length_m`` x ``min_width_m``; an invented one (run 3: equal to the
    diameter, no second pair) is dropped (``LIVE``)."""
    entry = ("1. Apron: 20 m by 20 m, excavated 0.5 m; it may shrink to 12 m by 12 m if the "
             "batteries run short.")
    r = reqs({"id": "S1", "type": "apron", "zone": "Z1", "diameter_m": 20, "min_diameter_m": 12,
              "depth_m": 0.5}, source=entry)
    s = r.structures[0]
    assert footprint_note(s, r) == (
        "diameter_m 20, min_diameter_m 12 -> 20 m x 20 m, at least 12 m x 12 m: the brief's "
        "entry for S1 states a 20 m by 20 m footprint, at least 12 m by 12 m")
    fixed = with_judged_footprint(s, r)
    assert fixed.dims() == {"depth_m": 0.5, "length_m": 20, "width_m": 20, "min_length_m": 12,
                            "min_width_m": 12}
    shape = dims_volume(s, r)
    assert (shape.m3, shape.floor_m3, shape.how) == (200, 72, "12-20 x 12-20 x 0.5 m")
    smaller = TaskGraph(tasks=[task("T1", "excavator", 36, "m3", "S1"),
                               task("T2", "excavator", 36, "m3", "S1", after=("T1",))])
    assert validate_graph(r, smaller, volume=True) is None
    assert volume_shares(r, smaller) == {"S1": pytest.approx(1.0)}
    # a recorded minimum the entry states no smaller pair for is no minimum
    plain = reqs({"id": "S1", "type": "apron", "zone": "Z1", "diameter_m": 20,
                  "min_diameter_m": 12, "depth_m": 0.5},
                 source="1. Apron: 20 m by 20 m, excavated 0.5 m.")
    assert with_judged_footprint(plain.structures[0], plain).dims() == {
        "depth_m": 0.5, "length_m": 20, "width_m": 20}


def test_every_other_eval_briefs_recorded_structures_are_unchanged() -> None:
    """Every structure Lightning recorded in runs 2 and 3, read against its brief: only a
    recorded diameter whose brief entry states a rectangle changes (habitat_foundation,
    habitat_berm_tight_horizon, instrument_pad, four_mast_pads), every one of those does, and
    every structure of the other 18 briefs, circular pads, berms, stockpiles and pads typed
    "stockpile" included, comes back as it was."""
    recorded = json.loads(RECORDED.read_text())
    assert len([b for b in recorded if not b.startswith("_")]) == 22
    changed: dict[str, int] = {}
    for name, structures in recorded.items():
        if name.startswith("_"):
            continue
        for data in structures:
            r = reqs(data, source=brief(name))
            s = r.structures[0]
            fixed = with_judged_footprint(s, r)
            diameter = bool({"diameter_m", "min_diameter_m"} & set(s.dims()))
            if name in RECTANGLES and diameter:
                assert footprint_note(s, r) is not None, (name, data)
                assert fixed.dims()["length_m"] == fixed.dims()["width_m"], (name, data)
                changed[name] = changed.get(name, 0) + 1
            else:
                assert footprint_note(s, r) is None, (name, data)
                assert fixed is s, (name, data)
    assert sorted(changed) == sorted(RECTANGLES)


# ----------------------------------------------------------------------------- the correction
class PitClient:
    """Answers the structure request with habitat_foundation's pit as Lightning recorded it
    and the drainage trench beside it."""

    name = "pit"

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        answer = reqs(LIVE[0][1], {"id": "F2", "type": "perimeter_trench", "zone": "Z2",
                                   "length_m": 56, "width_m": 0.4, "depth_m": 0.6})
        return schema.model_validate(answer.model_dump()), Usage(model="pit")


def test_the_correction_is_written_into_the_requirements_and_rides_the_structure_step() -> None:
    req, step = structure_brief(PitClient(), brief("habitat_foundation"), key="k")
    assert req is not None and step.ok
    pit, trench = req.structures
    assert pit.dims() == {"length_m": 12, "depth_m": 1.0, "thickness_m": 0.2, "width_m": 12}
    assert trench.dims() == {"length_m": 56, "width_m": 0.4, "depth_m": 0.6}   # untouched
    note = ("structure F1: diameter_m 12 -> 12 m x 12 m: the brief's entry for F1 states a 12 m "
            "by 12 m footprint")
    assert note in step.payload["warnings"]
    # the planner sees the brief's square: the requirements it is handed carry no diameter
    assert '"diameter_m"' not in req.model_dump_json(exclude={"source_brief"})
    # applied once: the corrected requirements need no second correction
    assert correct_footprints(req) == [] and footprint_warnings(req) == []
    r = reqs(LIVE[2][1], {"id": "S2", "type": "berm", "zone": "Z2", "length_m": 70,
                          "height_m": 2, "width_m": 4}, source=brief("habitat_berm_tight_horizon"))
    assert correct_footprints(r) == [
        ("structure S1: diameter_m 12 -> 12 m x 12 m: the brief's entry for S1 states a 12 m by "
         "12 m footprint")]
    assert r.structures[0].dims() == {"length_m": 12, "depth_m": 1.0, "width_m": 12}
    assert r.structures[1].dims() == {"length_m": 70, "height_m": 2, "width_m": 4}


# ----------------------------------------------------------------------------- judge time
def saved(graph: TaskGraph) -> PlanResult:
    """A saved, verified PlanResult whose requirements still record the pit as a diameter
    (what runs 2 and 3 saved) and carry the brief they were structured from."""
    r = reqs(LIVE[0][1], source=brief("habitat_foundation"))
    schedule = Schedule(entries=[], makespan_h=1, status="optimal")
    return PlanResult(requirements=r, task_graph=graph, schedule=schedule, success=True,
                      steps=[LoopStep(stage="structure", ok=True)])


def test_a_saved_plan_is_judged_against_the_briefs_square() -> None:
    """Run 2's habitat_foundation plans dug the recorded 12 m bowl (2 x 56.55 m3) and compacted
    its 113.1 m2: in scope against the bowl, 78 % of the brief's 144 m3 box. Every volume and
    area judgment reads the square from the brief whatever the requirements recorded; the
    recorded footprint stays available for comparison (``brief_footprint=False``)."""
    bowl = TaskGraph(tasks=[task("T1", "excavator", 56.55, "m3"),
                            task("T2", "excavator", 56.55, "m3", after=("T1",)),
                            task("T3", "compactor", 113.1, "m2", after=("T2",))])
    result = saved(bowl)
    r = result.requirements
    assert volume_shares(r, bowl) == {"F1": pytest.approx(0.785, abs=0.001)}
    assert area_shares(r, bowl) == {"F1": pytest.approx(0.785, abs=0.001)}
    assert volume_shares(r, bowl, brief_footprint=False) == {"F1": pytest.approx(1.0, abs=0.001)}
    assert area_shares(r, bowl, brief_footprint=False) == {"F1": pytest.approx(1.0, abs=0.001)}
    assert scope_gaps(r, bowl) == [
        ("volume:F1: its excavation tasks cover 113.1 of 144 m3 (78 %), under the 95 % a plan "
         "in scope covers"),
        ("area:F1: its compactor tasks treat 113.1 of 144 m2 (78 %; 144 m2), under the 95 % a "
         "plan in scope covers")]
    row = eval_row("habitat_foundation", "loop_on", 1, result, 1.0)
    assert (row.success, row.in_scope, row.min_volume_share) == (True, False, 0.785)
    # dug and compacted as the brief's square: in scope
    box = TaskGraph(tasks=[task("T1", "excavator", 72, "m3"),
                           task("T2", "excavator", 72, "m3", after=("T1",)),
                           task("T3", "compactor", 144, "m2", after=("T2",))])
    assert scope_gaps(r, box) == [] and eval_row("x", "loop_on", 1, saved(box), 1.0).in_scope
    # the validator's volume conflict reads the same square: 90 m3 is 80 % of the bowl but
    # 62 % of the box
    short = TaskGraph(tasks=[task("T1", "excavator", 90, "m3")])
    failure = validate_graph(r, short, volume=True)
    assert failure is not None and failure.conflicts == ["volume:F1"]
    assert failure.details == [
        ("F1 (foundation_pit): dimensions 12 x 12 x 1 m = 144 m3 of excavation; excavator "
         "tasks cover 90 m3 (62 %): T1 90 m3")]
    assert validate_graph(r.model_copy(update={"source_brief": None}), short, volume=True) is None
