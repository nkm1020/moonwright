"""Tests for the sizes the brief states (phase 3j, ``moonwright.pipeline.rates``, "Sizes"): a
smaller size the brief allows anywhere is read, tied to its structure and written as
``min_<dimension>_m``, and a length a structure's entry gives only as a distance is dropped,
at the structure stage and at judge time alike.

The evidence is low_battery_pad (kind E, ``tests/data/low_battery_pad_live.json``): its
constraint allows "any diameter between 12 m and 20 m that fits", Nemotron 3.5 Lightning recorded
no ``min_diameter_m`` in 6 of 6 structurings of runs 2 and 3, and the eval judged the five
verified plans, each a bowl of about 16.8 m (111 m3) inside the allowed range, against 20 m
(70 %): 0 of 6 in scope in run 3. Run 3 #1 also recorded the stockpile "30 m north of the pad" as
a 30 m diameter typed ``spoil_berm`` (its compaction judged over 707 m2), runs 2 #2 and #3 as a
30 m length. Every other eval brief's recorded structures come back as they were, read alone
(``tests/data/recorded_structures.json``) and beside the structures they were recorded with
(``tests/data/recorded_structurings.json``), and the reader never invents a minimum: the review
of the first cut wrote one from a depth allowance read as a diameter, from the ground, a
compaction depth, a crest, a key trench, a road's growth, a haul length, a statement about the
stockpile and a berm in the pad's own entry, and dropped a road's "120 m from the depot to the
pad" as a distance; each is a test here."""

from __future__ import annotations

import json
import math
import textwrap
from pathlib import Path

import pytest

from moonwright.cli import eval_row
from moonwright.llm.base import Usage
from moonwright.pipeline.rates import (
    _size_statements,
    area_shares,
    dims_volume,
    distance_figures,
    scope_gaps,
    size_notes,
    stated_minimums,
    volume_shares,
    with_brief_sizes,
)
from moonwright.pipeline.structure import correct_sizes, size_warnings, structure_brief
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

EVAL = Path(__file__).resolve().parents[1] / "examples" / "eval"
DATA = Path(__file__).resolve().parent / "data"
RECORDED = DATA / "recorded_structures.json"
MIN_NOTE = ("structure S1: min_diameter_m none -> 12: the brief allows \"any diameter between 12 m "
            "and 20 m that fits\"")


def brief(name: str) -> str:
    return (EVAL / f"{name}.md").read_text()


def live() -> dict:
    return json.loads((DATA / "low_battery_pad_live.json").read_text())


def job(key: str) -> Requirements:
    """One low_battery_pad job's requirements as Lightning structured them, its brief attached."""
    r = Requirements.model_validate(live()["requirements"][key])
    r.source_brief = brief("low_battery_pad")
    return r


def reqs(*structures: dict, source: str | None = None) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (8 * i, 10)} for i in range(1, 5)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures],
        robots=[{"id": "EX1", "type": "excavator", "battery_wh": 1800},
                {"id": "HL1", "type": "hauler", "battery_wh": 2000},
                {"id": "CP1", "type": "compactor", "battery_wh": 2000}],
        horizon_h=168, source_brief=source)


def task(id_: str, robot_type: str, quantity: float, unit: str, structure: str = "S1",
         zone: str = "Z1", after: tuple[str, ...] = ()) -> Task:
    return Task(id=id_, name=f"{robot_type} {structure} {id_}", robot_type=robot_type,
                duration_h=4, energy_wh=100.0, zone=zone, structure=structure,
                quantity=quantity, unit=unit, after=list(after))


# ----------------------------------------------------------------------------- the reader
@pytest.mark.parametrize(("line", "found"), [
    ("or any diameter between 12 m and 20 m that fits", ("diameter_m", 12, 20)),
    ("a diameter between 12 and 20 m", ("diameter_m", 12, 20)),
    ("between 30 cm and 50 cm deep", ("depth_m", 0.3, 0.5)),
    ("a 12-20 m diameter bowl", ("diameter_m", 12, 20)),
    ("a 12 – 20 m diameter bowl", ("diameter_m", 12, 20)),
    ("12 to 20 m wide", ("width_m", 12, 20)),
    ("the pad may shrink from 20 m to 12 m in diameter", ("diameter_m", 12, 20)),
    ("anywhere from 12 m to 20 m in diameter", ("diameter_m", 12, 20)),
    ("the diameter may be reduced to 12 m", ("diameter_m", 12, None)),
    ("the depth of the bowl may be reduced to 0.3 m", ("depth_m", 0.3, None)),
    ("the strip may shrink down to 0.3 m deep", ("depth_m", 0.3, None)),
    ("any length down to 60 m", ("length_m", 60, None)),
    ("at least 12 m long", ("length_m", 12, None)),
    ("a diameter of at least 12 m", ("diameter_m", 12, None)),
    ("no smaller than 12 m in diameter", ("diameter_m", 12, None)),
    ("no narrower than 3 m wide", ("width_m", 3, None)),
    # the nearest dimension word binds, never one across a conjunction or another dimension
    ("keeps its full 20 m diameter but its depth may be reduced to 0.3 m", ("depth_m", 0.3, None)),
    ("keep the full diameter but the depth may be reduced to 0.3 m", ("depth_m", 0.3, None)),
    ("the diameter stays and the depth can go down to 0.3 m", ("depth_m", 0.3, None)),
])
def test_a_smaller_size_the_brief_allows_is_read(line: str, found: tuple) -> None:
    [st] = _size_statements(line)
    assert (st.key, st.least, st.full) == pytest.approx(found)


@pytest.mark.parametrize("line", [
    "keep at least 30 m from the pad",               # a distance, no dimension word
    "excavated down to 0.5 m deep",                  # a depth the dig reaches
    "dug down to 1.2 m depth",
    "0.5 m to firm regolith",                        # a depth and a material
    "placed in 2-3 lifts", "2 to 3 hours per segment", "2 x 60 m3",
    "between 20 m and 40 m from the pit",            # a range of distances
    "at least 1.5 m high",                           # height is no word the reader binds
    "up to 3 m deep",                                # a largest size, not a smallest
    "between 12 m and 12 m in diameter",             # no range
    "the pad is 20 m in diameter", "a central 12 m",
    "Plan only what the remaining batteries can actually finish",
    # the size it grows from, and "from ... to" without a word that shrinks (seen in review)
    "extend the existing road from 40 m to 60 m long", "widen the lane from 3 m to 5 m wide",
    "from 20 m to 12 m in diameter",
    # a dimension the reader does not take, nearest to the figure
    "the berm length and height may be reduced to 1.5 m", "at least 12 m across",
    "keeps its 60 m length but may be no less than 1.5 m high",
    # the brief's own paragraphs on the ground, the robots and the time
    "**Site.** The LS-1 landing pad footprint, loose regolith at least 40 cm deep",
    "Site: loose regolith at least 40 cm deep", "**Fleet.** Haulers need lanes at least 4 m wide",
])
def test_no_smaller_size_is_read_where_none_is_stated(line: str) -> None:
    assert _size_statements(line) == ()


# ----------------------------------------------------------------------------- live: stage
@pytest.mark.parametrize("key", ["run2 #1", "run2 #2", "run2 #3", "run3 #1", "run3 #2", "run3 #3"])
def test_the_live_pads_minimum_is_written_in_every_structuring(key: str) -> None:
    r = job(key)
    pad = r.structures[0]
    assert "min_diameter_m" not in pad.dims() and pad.dims()["diameter_m"] == 20
    notes = correct_sizes(r)
    assert notes[0] == MIN_NOTE
    assert r.structures[0].dims() == {**pad.dims(), "min_diameter_m": 12}
    assert dims_volume(r.structures[0]).floor_m3 == pytest.approx(math.pi * 36 * 0.5)  # 56.5 m3
    # applied once, and the judge reads the same from the corrected requirements
    assert correct_sizes(r) == [] and size_warnings(r) == []
    assert with_brief_sizes(r.structures[0], r) is r.structures[0]


@pytest.mark.parametrize(("key", "dropped"), [
    ("run2 #2", ("length_m", "30")), ("run2 #3", ("length_m", "30")),
    ("run3 #1", ("diameter_m", "30")),
])
def test_the_live_stockpiles_distance_is_dropped(key: str, dropped: tuple[str, str]) -> None:
    r = job(key)
    pile = r.structures[1]
    assert distance_figures(pile, r) == {dropped[0]: "30 m north of the pad"}
    assert correct_sizes(r) == [
        MIN_NOTE,
        (f"structure S2: {dropped[0]} {dropped[1]} dropped: the brief's entry for S2 states 30 m "
         "only as a distance (\"30 m north of the pad\")")]
    assert dropped[0] not in r.structures[1].dims()
    assert (r.structures[1].type, r.structures[1].operations) == (pile.type, pile.operations)


def test_a_stockpile_size_the_brief_never_states_is_left_alone() -> None:
    """Run 2 #1 recorded the stockpile at 10 m by 3 m and run 3 #3 at 5 m by 3 m: figures the
    entry does not state at all (an invention, but no distance, and a stockpile is not judged);
    run 3 #2 recorded none."""
    for key in ("run2 #1", "run3 #2", "run3 #3"):
        r = job(key)
        assert distance_figures(r.structures[1], r) == {}
        assert size_notes(r.structures[1], r) == []


class LowBatteryClient:
    """Answers the structure request with run 3 #1's requirements as Lightning recorded them."""

    name = "low-battery"

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        r = job("run3 #1")
        return schema.model_validate(r.model_dump(exclude={"source_brief"})), Usage(model="lb")


def test_the_structure_step_writes_the_sizes_and_says_so() -> None:
    req, step = structure_brief(LowBatteryClient(), brief("low_battery_pad"), key="k")
    assert req is not None and step.ok
    assert req.structures[0].dims() == {"diameter_m": 20, "depth_m": 0.5, "min_diameter_m": 12}
    assert req.structures[1].dims() == {}
    warnings = step.payload["warnings"]
    assert MIN_NOTE in warnings
    assert ("structure S2: diameter_m 30 dropped: the brief's entry for S2 states 30 m only as a "
            "distance (\"30 m north of the pad\")") in warnings
    # the brief gives the stockpile no size to add: the drop's note stands for the dimension
    # one (seen in review under the amber "Add the brief's dimensions before planning")
    assert not any("has no dimensions" in w for w in warnings)
    # the planner sees the smaller size it may plan: the requirements it is handed carry it
    assert '"min_diameter_m":12' in req.model_dump_json(exclude={"source_brief"})


def test_a_hard_wrapped_brief_reads_the_same() -> None:
    folded = "\n".join(
        "\n".join(textwrap.wrap(line, 60, subsequent_indent="  ")) if len(line) > 60 else line
        for line in brief("low_battery_pad").splitlines())
    r = job("run3 #1").model_copy(update={"source_brief": folded})
    assert [n.split(":")[1] for n in size_warnings(r)] == [" min_diameter_m none -> 12",
                                                           " diameter_m 30 dropped"]


# ----------------------------------------------------------------------------- live: judge
def saved(key: str) -> PlanResult:
    plan = live()["plans"][key]
    schedule = Schedule(entries=[], makespan_h=1, status="optimal")
    return PlanResult(requirements=job(plan["job"]),
                      task_graph=TaskGraph.model_validate(plan["task_graph"]),
                      schedule=schedule, success=True, steps=[LoopStep(stage="structure", ok=True)])


@pytest.mark.parametrize("key", ["run2 ultra #1", "run3 ultra #1", "run3 super #1",
                                 "run3 ultra #3", "run3 super #3"])
def test_a_saved_plan_inside_the_allowed_range_is_in_scope(key: str) -> None:
    """The verified live plans, judged as saved: as recorded the bowl of about 16.8 m reads 70 %
    of the 20 m one (the eval wrote ``min_volume_share`` 0.705 / 0.707 and not in scope); read
    with the brief's 12 m it is nearly twice the smallest size allowed, and in scope. The
    stockpile Lightning sized from its distance is no longer judged."""
    plan = live()["plans"][key]
    result = saved(key)
    r, g = result.requirements, result.task_graph
    assert plan["in_scope"] is False
    assert volume_shares(r, g, brief_sizes=False)["S1"] == pytest.approx(plan["min_volume_share"],
                                                                         abs=0.001)
    assert volume_shares(r, g)["S1"] == pytest.approx(111 / (math.pi * 36 * 0.5), rel=0.01)
    assert scope_gaps(r, g) == [] and "S2" not in area_shares(r, g)
    row = eval_row("low_battery_pad", "ultra/loop_on", 1, result, 1.0)
    assert (row.success, row.in_scope) == (True, True)


def test_run_3_1s_stockpile_was_judged_over_its_distance() -> None:
    r = saved("run3 ultra #1").requirements
    g = saved("run3 ultra #1").task_graph
    assert area_shares(r, g, brief_sizes=False)["S2"] == pytest.approx(176.84 / (math.pi * 225),
                                                                        rel=0.001)


def test_the_volume_conflict_reads_the_brief_floor() -> None:
    """At validation time: a 12 m bowl (56.5 m3) is what the brief allows at least; a 10 m one
    (39.3 m3, 69 % of it) is short."""
    r = job("run3 #3")
    twelve = TaskGraph(tasks=[task("T1", "excavator", 56.55, "m3")])
    ten = TaskGraph(tasks=[task("T1", "excavator", 39.27, "m3")])
    assert validate_graph(r, twelve, volume=True) is None
    failure = validate_graph(r, ten, volume=True)
    assert failure is not None and failure.conflicts == ["volume:S1"]
    assert failure.details == [
        ("S1 (landing_pad_bowl): dimensions bowl 12-20 m across, 0.5 m deep = 56.5-157.1 m3 of "
         "excavation; excavator tasks cover 39.3 m3 (69 % of the smallest size the brief "
         "allows): T1 39.3 m3")]
    assert validate_graph(r.model_copy(update={"source_brief": None}), twelve, volume=True) \
        is not None


# ----------------------------------------------------------------------------- never invented
PAD = {"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.5}
PILE = {"id": "S2", "type": "stockpile", "zone": "Z2", "length_m": 15, "width_m": 12}


def minimum(source: str, *structures: dict) -> dict[str, dict[str, float]]:
    r = reqs(*structures, source=source)
    return {s.id: {k: st.least for k, st in stated_minimums(s, r).items()}
            for s in r.structures}


def test_a_minimum_is_tied_to_its_structure_or_to_none() -> None:
    entry = "1. Landing pad: 20 m diameter, 0.5 m deep.\n2. Stockpile: 15 m by 12 m.\n"
    # its own entry, or a line holding its type words
    assert minimum(entry + "- The pad may be reduced to 12 m in diameter.", PAD, PILE) == {
        "S1": {"min_diameter_m": 12}, "S2": {}}
    assert minimum("1. Landing pad: 20 m diameter, at least 14 m in diameter, 0.5 m deep.\n",
                   PAD) == {"S1": {"min_diameter_m": 14}}
    # a line that names another structure says nothing of this one
    assert minimum(entry + "- The stockpile may be reduced to 10 m wide.", PAD, PILE) == {
        "S1": {}, "S2": {"min_width_m": 10}}
    assert minimum(entry + "- The stockpile may shrink to 10 m in diameter.", PAD, PILE) == {
        "S1": {}, "S2": {}}
    # a line that names none: a range by its top, a smallest size when one structure records it
    assert minimum(entry + "- Any diameter between 12 m and 20 m is fine.", PAD, PILE) == {
        "S1": {"min_diameter_m": 12}, "S2": {}}
    assert minimum(entry + "- Any diameter down to 12 m is fine.", PAD, PILE) == {
        "S1": {"min_diameter_m": 12}, "S2": {}}
    other = {**PILE, "type": "turnaround", "diameter_m": 18}
    assert minimum(entry + "- Any diameter down to 12 m is fine.", PAD, other) == {
        "S1": {}, "S2": {}}                                   # two record a diameter: neither
    assert minimum(entry + "- Any diameter between 12 m and 20 m is fine.", PAD, other) == {
        "S1": {"min_diameter_m": 12}, "S2": {}}               # only the pad is 20 m
    twin = {**PAD, "id": "S3"}
    assert minimum("- Any diameter between 12 m and 20 m is fine.", PAD, twin) == {
        "S1": {}, "S3": {}}                                   # two pads at 20 m: neither


def test_no_minimum_where_the_record_already_has_one_or_the_brief_allows_no_less() -> None:
    line = "\n- Any diameter between 12 m and 20 m is fine."
    assert minimum(line, {**PAD, "min_diameter_m": 16}) == {"S1": {}}   # recorded: stands
    assert minimum("- Any diameter between 20 m and 24 m.", PAD) == {"S1": {}}  # top not 20
    assert minimum("- The pad is at least 20 m in diameter.", PAD) == {"S1": {}}
    assert minimum("- The pad is at least 24 m in diameter.", PAD) == {"S1": {}}
    assert minimum("- The pad depth may be reduced to 0.3 m.", PAD) == {"S1": {"min_depth_m": 0.3}}
    assert minimum("- The pad may be reduced to 12 m in diameter.", {**PAD, "diameter_m": None}) \
        == {"S1": {}}
    r = reqs(PAD, source=line)
    assert stated_minimums(r.structures[0], r.model_copy(update={"source_brief": None})) == {}


def test_a_figure_is_dropped_only_when_the_entry_gives_it_as_a_distance_alone() -> None:
    def far(entry: str, **dims: float) -> dict[str, str]:
        r = reqs({"id": "S2", "type": "stockpile", "zone": "Z2", **dims}, source=entry)
        return distance_figures(r.structures[0], r)

    assert far("2. Stockpile: a pile 30 m north of the pad.", length_m=30) == {
        "length_m": "30 m north of the pad"}
    for phrase in ("15 m from the pit", "80 m away", "5 m beyond the rim",
                   "30 m north-east of the pad", "3000 cm south of the pad"):
        assert far(f"2. Stockpile: a pile {phrase}.", length_m=float(phrase.split()[0]) /
                   (100 if " cm " in f" {phrase} " else 1)), phrase
    # a figure the entry also states as a size, or does not state, stays
    assert far("2. Stockpile: a 30 m pile 30 m north of the pad.", length_m=30) == {}
    assert far("2. Stockpile: a pile 30 m north of the pad.", length_m=10) == {}
    # "runs 120 m west from", "80 m to the west": a length, a direction; no entry, nothing read
    assert far("2. Stockpile: the route runs 30 m west from the pad.", length_m=30) == {}
    assert far("2. Stockpile: 30 m to the west.", length_m=30) == {}
    assert far("- The surveyor works 30 m north of the pad.", length_m=30) == {}


# ----------------------------------------------------------------------------- seen in review
DEPTH_ALLOWANCES = [
    ("1. Landing pad: 20 m diameter, 0.5 m deep, compacted.\n\n- The pad keeps its full 20 m "
     "diameter but its depth may be reduced to 0.3 m if the batteries run short.\n"),
    ("1. Landing pad: 20 m diameter, 0.5 m deep; keep the full diameter but the depth may be "
     "reduced to 0.3 m.\n"),
    ("1. Landing pad: 20 m diameter, 0.5 m deep.\n\n- The pad keeps its diameter and the depth "
     "can go down to 0.3 m if the batteries run short.\n"),
]


@pytest.mark.parametrize("source", DEPTH_ALLOWANCES)
def test_a_smaller_depth_is_never_read_as_a_diameter(source: str) -> None:
    """Seen in review: the dimension noun bound was the leftmost within reach, so "keeps its
    full 20 m diameter but its depth may be reduced to 0.3 m" wrote ``min_diameter_m: 0.3``,
    the bowl's floor fell from 157.1 m3 to nothing, and a 5 m3 dig with 20 m2 of compaction
    validated clean. The nearest dimension word binds: the depth may be 0.3 m, the floor is
    94.2 m3, and the token dig is short of it."""
    r = reqs({**PAD, "operations": ["excavate", "compact"]}, source=source)
    assert [n.split(":")[1] for n in correct_sizes(r)] == [" min_depth_m none -> 0.3"]
    assert r.structures[0].dims() == {"diameter_m": 20, "depth_m": 0.5, "min_depth_m": 0.3}
    assert dims_volume(r.structures[0]).floor_m3 == pytest.approx(math.pi * 100 * 0.3)
    token = TaskGraph(tasks=[task("T1", "excavator", 5, "m3"),
                             task("T2", "compactor", 20, "m2", after=("T1",))])
    failure = validate_graph(r, token, coverage=True, volume=True)
    assert failure is not None and failure.conflicts == ["volume:S1"]


BERM = {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2,
        "width_m": 4}
SQUARE = {"id": "S1", "type": "landing_pad", "zone": "Z1", "length_m": 20, "width_m": 20,
          "depth_m": 0.5}


@pytest.mark.parametrize(("source", "structures"), [
    # the ground (the Site paragraph is skipped whole; a constraint about it sizes nothing)
    (("1. Landing pad: 20 m diameter, 0.5 m deep.\n\n- Expect loose regolith at least 40 cm "
      "deep across the pad.\n"), (PAD,)),
    # a treatment
    (("1. Landing pad: 20 m diameter, excavated 0.5 m, subgrade compacted to a depth of at "
      "least 0.3 m.\n"), (PAD,)),
    # a part of the structure
    (("1. Landing pad: 20 m diameter, 0.5 m deep.\n2. Blast berm: 60 m long, 2 m high, 4 m "
      "wide at the base, with a crest at least 2 m wide.\n"), (PAD, BERM)),
    (("1. Landing pad: 20 m diameter, 0.5 m deep.\n2. Blast berm: 60 m long, 2 m high, 4 m "
      "wide at the base, on a key trench at least 1 m wide.\n"), (PAD, BERM)),
    # the size it grows from
    (("1. Access road: extend the existing road from 40 m to 60 m long, 5 m wide, graded and "
      "compacted.\n"),
     ({"id": "S1", "type": "access_road", "zone": "Z1", "length_m": 60, "width_m": 5},)),
    ("1. Haul lane: 80 m long, widen the lane from 3 m to 5 m wide, graded.\n",
     ({"id": "S1", "type": "haul_lane", "zone": "Z1", "length_m": 80, "width_m": 5},)),
    # the way between two structures
    (("1. Pit: 12 m by 12 m, 1 m deep.\n2. Berm: 60 m long, 2 m high, built from the pit "
      "spoil.\n\n- The haul length to the berm is at least 40 m.\n"),
     ({"id": "S1", "type": "pit", "zone": "Z1", "length_m": 12, "width_m": 12, "depth_m": 1},
      {"id": "S2", "type": "berm", "zone": "Z2", "length_m": 60, "height_m": 2})),
    # a subject of its own that no structure is, beside a structure that records the dimension
    (("1. Landing pad: 20 m by 20 m, 0.5 m deep.\n\n- The pad is square and the apron is at "
      "least 5 m wide.\n"), (SQUARE,)),
    ("1. Landing pad: 20 m by 20 m, 0.5 m deep, with an apron at least 5 m wide.\n", (SQUARE,)),
    (("1. Landing pad: 20 m by 20 m, 0.5 m deep.\n\n- The apron can be anywhere between 4 m "
      "and 20 m wide.\n"), (SQUARE,)),
    (("1. Landing pad: 20 m diameter, 0.5 m deep.\n\n- The surveyor's stakes are at least "
      "0.2 m deep.\n"), (PAD,)),
    ("1. Landing pad: 20 m diameter, 0.5 m deep.\n\n- Each lift is at least 0.2 m deep.\n",
     (PAD,)),
    # a quantity of something, and a depth the dig reaches
    (("1. Landing pad: 20 m diameter, 0.5 m deep.\n\n- The pad is 20 m in diameter with at "
      "least 0.5 m of compacted fill.\n"), (PAD,)),
    ("1. Landing pad: 20 m diameter, 0.5 m deep.\n\n- Dig the pad down to 0.4 m deep.\n",
     (PAD,)),
])
def test_no_minimum_from_a_sentence_that_allows_no_smaller_structure(
        source: str, structures: tuple[dict, ...]) -> None:
    """Seen in review, each written as a minimum and applied at judge time (a berm's floor
    from 240 to 60 m3 by its key trench's width): a clause about the ground, a treatment, a
    part of the structure, the size it grows from or the way between two structures allows
    no smaller structure."""
    assert all(found == {} for found in minimum(source, *structures).values())


def test_the_site_paragraph_sizes_no_structure() -> None:
    """low_battery_pad with its Site paragraph's "loose regolith 40 cm deep" turned into "at
    least 40 cm deep": seen in review, the pad also got ``min_depth_m: 0.4`` (its floor 56.5
    -> 45.2 m3). Only the constraint's 12 m is read."""
    r = job("run3 #1")
    r.source_brief = brief("low_battery_pad").replace("loose regolith 40 cm deep",
                                                      "loose regolith at least 40 cm deep")
    assert "at least 40 cm" in r.source_brief
    assert correct_sizes(r)[0] == MIN_NOTE
    assert r.structures[0].dims() == {"diameter_m": 20, "depth_m": 0.5, "min_diameter_m": 12}


def test_a_part_word_sizes_a_structure_of_that_type() -> None:
    trench = {"id": "S1", "type": "cable_trench", "zone": "Z1", "length_m": 150,
              "width_m": 0.4, "depth_m": 0.6}
    source = ("1. Cable trench: 150 m long, 0.4 m wide, 0.6 m deep.\n\n- The trench may be "
              "reduced to 100 m long if the cable arrives short.\n")
    assert minimum(source, trench) == {"S1": {"min_length_m": 100}}
    source = ("1. Cable trench: 150 m long, 0.4 m wide, 0.6 m deep.\n\n- The trench may go down "
              "to 0.4 m deep where the cable is armoured.\n")
    assert minimum(source, trench) == {"S1": {"min_depth_m": 0.4}}


STOCKPILE = {"id": "S2", "type": "stockpile", "zone": "Z2"}
PILE_ENTRIES = ("1. Landing pad: 20 m diameter, 0.5 m deep.\n2. Spoil stockpile: pad spoil in a "
                "compact pile north of the pad.\n\n")


@pytest.mark.parametrize("line", [
    "- The stockpile next to the pad may shrink to 10 m in diameter.",
    "- The stockpile beside the pad may be reduced to 8 m in diameter if space is short.",
    "- The stockpile next to the landing pad may shrink to 10 m in diameter.",
    "- Keep the pad clear; the stockpile can be anywhere between 6 m and 20 m in diameter.",
    "- The stockpile may be anywhere between 12 m and 20 m in diameter.",
])
def test_a_size_stated_of_another_structure_is_never_the_pads(line: str) -> None:
    """Seen in review: naming was ranked only among the structures that record the dimension,
    so a size stated of the stockpile Lightning recorded without one went to the pad (and a
    range naming only the stockpile went to the pad by its 20 m top). The subject is looked
    for in the statement's own segment first, over every structure, and two named by
    different words leave it to none."""
    assert minimum(PILE_ENTRIES + line + "\n", PAD, STOCKPILE) == {"S1": {}, "S2": {}}


def test_an_entry_that_sizes_another_structure_gives_its_owner_nothing() -> None:
    """Seen in review: the pad's own entry gave it ``min_width_m: 2`` from the berm along its
    edge (its floor 200 -> 20 m3)."""
    pad = {"id": "S1", "type": "landing_pad", "zone": "Z1", "length_m": 20, "width_m": 20,
           "depth_m": 0.5}
    berm = {"id": "S2", "type": "spoil_berm", "zone": "Z2", "length_m": 20, "height_m": 1}
    source = ("1. Landing pad: 20 m by 20 m, 0.5 m deep, with a spoil berm along its north edge "
              "at least 2 m wide.\n2. Spoil berm: 20 m long, 1 m high.\n")
    assert minimum(source, pad, berm) == {"S1": {}, "S2": {}}
    assert minimum(source.replace("edge at", "edge, at"), pad, berm) == {"S1": {}, "S2": {}}


def test_the_subject_is_the_nearest_structure_its_sentence_names() -> None:
    entries = "1. Landing pad: 20 m diameter, 0.5 m deep.\n2. Blast berm: 60 m long, 2 m high.\n\n"
    berm = {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2}
    assert minimum(entries + "- If the batteries run short, the pad may be reduced to 12 m in "
                   "diameter and the berm shortened to match.\n", PAD, berm) == {
        "S1": {"min_diameter_m": 12}, "S2": {}}
    assert minimum(entries + "- The pad keeps its diameter and the depth can go down to 0.3 m.\n",
                   PAD, berm) == {"S1": {"min_depth_m": 0.3}, "S2": {}}
    # a type that holds the other's words names it better: the landing pad, not the mast pad
    mast = {"id": "S3", "type": "mast_pad", "zone": "Z3", "diameter_m": 16, "depth_m": 0.3}
    assert minimum("- The landing pad may be reduced to 12 m in diameter.\n", PAD, mast) == {
        "S1": {"min_diameter_m": 12}, "S3": {}}


def test_a_subject_gives_the_size_to_the_structure_it_names() -> None:
    """The subject of a statement's own segment names its structure by a word of its type or
    of the name its entry gives it: run 2 typed low_battery_pad's "Landing pad bowl"
    ``landing_pad``, and "the bowl" is still the pad; a plural names it too; four mast pads
    alike are none of them; a condition after "if" is no part of the statement ("if the
    regolith is firm" is not the ground's size)."""
    pile = {"id": "S2", "type": "stockpile", "zone": "Z2", "diameter_m": 10, "height_m": 3}
    entries = ("1. Landing pad bowl: 20 m diameter, 0.5 m deep.\n2. Spoil stockpile: pad spoil "
               "in a compact pile.\n\n")
    assert minimum(entries + "- The bowl may be reduced to 12 m in diameter.\n", PAD, pile) == {
        "S1": {"min_diameter_m": 12}, "S2": {}}
    assert minimum("- The pads may be reduced to 12 m in diameter.\n", PAD) == {
        "S1": {"min_diameter_m": 12}}
    assert minimum("1. Landing pad: 20 m diameter, 0.5 m deep.\n\n- The pad may be dug at "
                   "least 0.3 m deep if the regolith is firm.\n", PAD) == {
        "S1": {"min_depth_m": 0.3}}
    masts = [{"id": f"M{i}", "type": "mast_pad", "zone": "Z1", "length_m": 4, "width_m": 4,
              "depth_m": 0.3} for i in range(1, 5)]
    assert minimum("1. Mast pads: four, 4 m by 4 m, 0.3 m deep.\n\n- Each mast pad may be "
                   "reduced to 3 m wide.\n", *masts) == {m["id"]: {} for m in masts}


def test_a_size_note_quotes_the_clause_without_its_list_marker() -> None:
    r = reqs(PAD, STOCKPILE, source=PILE_ENTRIES + "- The pad may be reduced to 12 m in diameter.\n")
    assert size_notes(r.structures[0], r) == [
        "min_diameter_m none -> 12: the brief allows \"The pad may be reduced to 12 m in diameter\""]


def test_a_span_or_an_extent_is_no_distance() -> None:
    """Seen in review, each dropped as a distance: a road "120 m from the depot to the pad"
    (no longer judged by its 600 m2), a trench that "runs 60 m from the array" (its 24 m3 no
    longer judged), a ring "extending 5 m beyond the pad edge", and a berm whose constraint
    ("Keep the blast berm 30 m from the landing pad edge") outnamed its own entry. Only the
    entry the structure owns is read, and a span or an extent is a size."""
    def far(source: str, *structures: dict) -> dict[str, dict[str, str]]:
        r = reqs(*structures, source=source)
        return {s.id: distance_figures(s, r) for s in r.structures}

    road = {"id": "S2", "type": "access_road", "zone": "Z2", "length_m": 120, "width_m": 5}
    source = ("1. Landing pad: 20 m diameter, 0.5 m deep.\n2. Access road: 120 m from the depot "
              "to the pad, 5 m wide, graded and compacted.\n")
    assert far(source, PAD, road) == {"S1": {}, "S2": {}}
    r = reqs(road, source=source)
    g = TaskGraph(tasks=[task("T1", "excavator", 20, "m2", "S2", "Z2"),
                         task("T2", "compactor", 20, "m2", "S2", "Z2", after=("T1",))])
    assert area_shares(r, g) == area_shares(r, g, brief_sizes=False) == {
        "S2": pytest.approx(20 / 600)}
    trench = {"id": "S1", "type": "cable_trench", "zone": "Z1", "length_m": 60, "width_m": 0.5,
              "depth_m": 0.8}
    for entry in (("1. Cable trench: runs 60 m from the array to the habitat, 0.5 m wide and "
                   "0.8 m deep, backfilled.\n"),
                  "1. Cable trench: runs 60 m from the array, 0.5 m wide and 0.8 m deep.\n"):
        assert far(entry, trench) == {"S1": {}}, entry
    apron = {"id": "S2", "type": "apron", "zone": "Z2", "width_m": 5, "diameter_m": 30}
    assert far("2. Apron: a compacted ring extending 5 m beyond the pad edge.\n", apron) == {
        "S2": {}}
    berm = {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 30, "height_m": 2}
    assert far("1. Landing pad: 20 m diameter, 0.5 m deep.\n2. Berm: 30 m long, 2 m high, built "
               "from the pad spoil.\n\n- Keep the blast berm 30 m from the landing pad edge.\n",
               PAD, berm) == {"S1": {}, "S2": {}}


# ----------------------------------------------------------------------------- the other briefs
def test_every_other_eval_briefs_recorded_structures_are_unchanged() -> None:
    """Every structure Lightning recorded in runs 2 and 3, read against its brief: only
    low_battery_pad's change (its 20 m pad gets the 12 m minimum, its stockpile loses the 30 m
    it was given from "30 m north of the pad"), and every structure of the other 21 briefs
    comes back as it was."""
    recorded = json.loads(RECORDED.read_text())
    changed: list[tuple[str, str]] = []
    for name, structures in recorded.items():
        if name.startswith("_"):
            continue
        for data in structures:
            r = reqs(data, source=brief(name))
            s = r.structures[0]
            notes = size_notes(s, r)
            if name != "low_battery_pad":
                assert notes == [] and with_brief_sizes(s, r) is s, (name, data)
                continue
            changed += [(s.type, n.split(":")[0]) for n in notes]
    assert sorted(changed) == [("landing_pad", "min_diameter_m none -> 12"),
                               ("landing_pad_bowl", "min_diameter_m none -> 12"),
                               ("spoil_berm", "diameter_m 30 dropped"),
                               ("stockpile", "length_m 30 dropped")]


def test_every_recorded_structuring_reads_as_before_but_low_battery_pads() -> None:
    """The same, each structure read beside the structures Lightning recorded it with
    (``tests/data/recorded_structurings.json``, every distinct structuring of runs 2 and 3), as
    the structure stage reads it: the review's misreadings all hand one structure's statement
    to another, which a structure read alone cannot show."""
    recorded = json.loads((DATA / "recorded_structurings.json").read_text())
    changed: list[tuple[str, str]] = []
    for name, structurings in recorded.items():
        if name.startswith("_"):
            continue
        for structures in structurings:
            r = reqs(*structures, source=brief(name))
            for s in r.structures:
                notes = size_notes(s, r)
                if name != "low_battery_pad":
                    assert notes == [] and with_brief_sizes(s, r) is s, (name, s.id)
                changed += [(s.type, n.split(":")[0]) for n in notes]
    assert sorted(changed) == [*[("landing_pad", "min_diameter_m none -> 12")] * 2,
                               *[("landing_pad_bowl", "min_diameter_m none -> 12")] * 3,
                               ("spoil_berm", "diameter_m 30 dropped"),
                               ("stockpile", "length_m 30 dropped")]
