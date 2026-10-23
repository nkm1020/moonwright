"""Tests for the haul a dug structure lists and the brief quote of its hint (phase 3j,
``moonwright.pipeline.validate``, "Structure coverage").

The coverage contract tags a haul with the structure that RECEIVES it, so the ``haul`` a
structure dug for its material lists (a borrow strip, a pit) is carried by the hauls its digs
feed: :func:`source_hauls` counts a hauler task that waits, through any chain of ``after``
edges, for a dig serving it. Seen live in the habitat berm demo
(``tests/data/habitat_berm_demo_live.json``): in try 1 the third repair round dug the borrow
strip and hauled it to the berm, tagged with the berm as the rules say, and the validator
answered "S2 (borrow_strip) lists haul ... but no task hauls to it; add a hauler task tagged
S2"; the fourth round hauled another 120 m3 of strip fill into the berm zone tagged S2. In try 3
the fourth round re-tagged the strip's hauls to the berm and lost the strip's haul the same
way. Where such a haul still lacks, the hint says what is true and asks for the edge, not for
a haul into the strip. Only a structure dug for its material counts so: the review found the
shielding mound (``tests/data/shielding_mound_live.json``) counted as dug, its fill haul
swapped for a haul of its foundation spoil validated, and its hint asked for that edge. And
the quote: both hints quoted "about 280 cubic metres of fill", the berm's entry, for the strip,
because the strip's two type words outscored the berm's one in the body of the berm's own
line; a quote now comes from the structure's own entry or a line that names it, never from an
entry its name or its figures give another structure
(:func:`moonwright.pipeline.tags.entry_owners`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moonwright.pipeline.tags import entry_owners
from moonwright.pipeline.validate import (
    _source,
    brief_evidence,
    missing_operations,
    source_hauls,
    structure_tasks,
    validate_graph,
)
from moonwright.schemas import Requirements, Structure, Task, TaskGraph

EVAL = Path(__file__).resolve().parents[1] / "examples" / "eval"
DATA = Path(__file__).resolve().parent / "data"
RECORDED = DATA / "recorded_structures.json"
SOURCE_RULE = ("a structure dug for its material is hauled by the hauls that wait for its digs, "
               "so make the haul of that material wait for them, tagged with the structure it "
               "fills, rather than adding a haul tagged with the dug structure.")


def brief(name: str) -> str:
    return (EVAL / f"{name}.md").read_text()


def demo(n: int) -> tuple[Requirements, dict]:
    data = json.loads((DATA / "habitat_berm_demo_live.json").read_text())[f"try{n}"]
    r = Requirements.model_validate(data["requirements"])
    r.source_brief = brief("habitat_berm_tight_horizon")
    return r, data


def graph(data: dict) -> TaskGraph:
    return TaskGraph.model_validate(data)


def task(id_: str, robot_type: str, quantity: float, unit: str, structure: str, zone: str,
         after: tuple[str, ...] = (), name: str | None = None) -> Task:
    return Task(id=id_, name=name or f"{robot_type} {structure} {id_}", robot_type=robot_type,
                duration_h=4, energy_wh=100.0, zone=zone, structure=structure,
                quantity=quantity, unit=unit, after=list(after))


def reqs(*structures: dict, source: str | None = None) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (8 * i, 10)} for i in range(1, 5)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures],
        robots=[{"id": "EX1", "type": "excavator", "battery_wh": 5000},
                {"id": "HL1", "type": "hauler", "battery_wh": 3000},
                {"id": "CP1", "type": "compactor", "battery_wh": 3500},
                {"id": "SV1", "type": "surveyor", "battery_wh": 1500}],
        horizon_h=336, source_brief=source)


# ----------------------------------------------------------------------------- the live demo
def test_the_strips_haul_is_the_haul_its_dig_feeds_try1() -> None:
    """Try 1, round 3: the strip S2 is dug (EX_S2) and hauled to the berm S3 (HL_S2_to_S3,
    tagged S3 in the berm's zone, after EX_S2). The recorded verdict was scope:S2 alone; the
    graph is now clean, so the round that invented HL_S2_haul has nothing to answer."""
    r, data = demo(1)
    assert data["repair3_verdict"]["conflicts"] == ["scope:S2"]
    assert ('S2 (borrow_strip) lists haul (the brief asks for "about 280 cubic metres of fill") '
            "but no task hauls to it; add a hauler task tagged S2") in data["repair3_verdict"]["hint"]
    round3 = graph(data["repair3_graph"])
    strip = r.structures[1]
    assert [t.id for t in source_hauls(strip, round3, r.structures)] == ["HL_S2_to_S3"]
    assert missing_operations(r, strip, structure_tasks(strip, round3, r.structures),
                              source_hauls(strip, round3, r.structures)) == []
    assert validate_graph(r, round3, coverage=True) is None
    # the round that answered the old hint: a second 120 m3 of strip fill into the berm zone
    final = graph(data["task_graph"])
    invented = final.task("HL_S2_haul")
    assert (invented.structure, invented.zone, invented.quantity) == ("S2", "Z3", 120.0)
    assert {t.id for t in final.tasks} - {t.id for t in round3.tasks} == {"HL_S2_haul"}


def test_the_strips_haul_is_the_haul_its_digs_feed_try3() -> None:
    """Try 3, round 4: the strip S3 is dug in two parts (T6, T7) and each part hauled to the
    berm S2 after it (T13, T14), tagged S2. Recorded: scope:S3 and supply:T15; now the depot
    haul T15, which waits for no dig, is the only verdict, word for word as recorded."""
    r, data = demo(3)
    recorded = data["final_verdict"]
    assert recorded["conflicts"] == ["scope:S3", "supply:T15"]
    final = graph(data["task_graph"])
    assert [t.id for t in source_hauls(r.structures[2], final, r.structures)] == ["T13", "T14"]
    failure = validate_graph(r, final, coverage=True)
    assert failure is not None and failure.conflicts == ["supply:T15"]
    assert failure.details == recorded["details"][1:]
    coverage, supply = recorded["hint"].split(" A haul cannot start", 1)
    assert coverage.startswith("Structure coverage is incomplete: S3 (borrow_strip)")
    assert failure.hint == "A haul cannot start" + supply


def test_a_haul_still_missing_reads_what_is_true() -> None:
    """The case that still fails: the strip's digs are there but no haul waits for them (try
    3's graph with the edges T6 -> T13 and T7 -> T14 dropped). The detail names the digs, the
    hint asks for the edges and against a haul tagged with the strip, and the quote is no other
    structure's (the strip's own entry asks for no haul: none)."""
    r, data = demo(3)
    cut = TaskGraph(tasks=[t.model_copy(update={"after": [a for a in t.after if a not in ("T6", "T7")]})
                           if t.id in ("T13", "T14") else t
                           for t in graph(data["task_graph"]).tasks])
    failure = validate_graph(r, cut, coverage=True)
    assert failure is not None
    assert failure.conflicts == ["scope:S3", "supply:T13", "supply:T14", "supply:T15"]
    assert failure.details[0] == ("S3 (borrow_strip) lacks haul task (its operations list haul; "
                                  "no haul waits for its digs T6, T7)")
    assert failure.hint.startswith(
        "Structure coverage is incomplete: S3 (borrow_strip) lists haul (an operation the brief "
        "asks for) but no hauler task is tagged S3 or waits for its digs (T6, T7); "
        + SOURCE_RULE + " A haul cannot start before its material is dug: ")
    assert "add a hauler task tagged S3" not in failure.hint
    assert "every structure keeps its phases" not in failure.hint   # nothing else is missing
    # nothing digs the strip at all: the dig and a haul after it, never a haul into the strip
    nodig = TaskGraph(tasks=[t.model_copy(update={"after": [a for a in t.after if a not in ("T6", "T7")]})
                             for t in graph(data["task_graph"]).tasks if t.id not in ("T6", "T7")])
    failure = validate_graph(r, nodig, coverage=True)
    assert failure is not None and failure.details[0] == (
        "S3 (borrow_strip) lacks excavate and haul tasks (its operations list excavate and haul)")
    assert ("lists excavate (the brief asks for \"so both excavators may dig at once on separate "
            "zones\") but no task excavates it and haul (an operation the brief asks for) but no "
            "hauler task is tagged S3 or waits for a dig of it; add an excavator task tagged S3 "
            "and make the haul of its material wait for the dig, tagged with the structure it "
            "fills; every structure keeps its phases") in failure.hint


# ----------------------------------------------------------------------------- the rule
STRIP = {"id": "S1", "type": "borrow_strip", "zone": "Z1", "length_m": 40, "width_m": 6,
         "depth_m": 0.5, "operations": ["excavate", "haul"]}
BERM = {"id": "S2", "type": "berm", "zone": "Z2", "length_m": 70, "height_m": 2, "width_m": 4,
        "operations": ["haul", "compact"]}


def test_a_haul_after_any_chain_to_its_dig_counts_and_one_after_another_dig_does_not() -> None:
    dig = task("D1", "excavator", 120, "m3", "S1", "Z1")
    check = Task(id="C1", name="Survey strip floor", robot_type="surveyor", duration_h=1,
                 energy_wh=10.0, zone="Z1", structure="S1", after=["D1"])
    haul = task("H1", "hauler", 120, "m3", "S2", "Z2", after=("C1",))
    pack = task("P1", "compactor", 140, "m2", "S2", "Z2", after=("H1",))
    r = reqs(STRIP, BERM)
    g = TaskGraph(tasks=[dig, check, haul, pack])
    assert [t.id for t in source_hauls(r.structures[0], g, r.structures)] == ["H1"]
    assert validate_graph(r, g, coverage=True) is None
    # the same haul after another structure's dig carries nothing of the strip
    pit = {"id": "S3", "type": "excavation_pit", "zone": "Z3", "length_m": 12, "width_m": 12,
           "depth_m": 1, "operations": ["excavate"]}
    other = task("D3", "excavator", 144, "m3", "S3", "Z3")
    elsewhere = TaskGraph(tasks=[dig, other, haul.model_copy(update={"after": ["D3"]}), pack])
    r3 = reqs(STRIP, BERM, pit)
    failure = validate_graph(r3, elsewhere, coverage=True, supply=False)
    assert failure is not None and failure.conflicts == ["scope:S1"]
    # a hauler tagged with the strip still serves it, as before
    tagged = TaskGraph(tasks=[dig, task("H2", "hauler", 120, "m3", "S1", "Z1", after=("D1",))])
    assert missing_operations(r, r.structures[0], list(tagged.tasks)) == []


def test_a_placed_body_or_a_pile_needs_the_fill_brought_onto_it() -> None:
    """A berm's or a stockpile's haul is the fill brought onto it: its own key trench's spoil
    carried off to a dump, or a stockpile loaded out, does not serve it."""
    berm = {**BERM, "trench_width_m": 1.2, "trench_depth_m": 0.8,
            "operations": ["excavate", "haul", "compact"]}
    trench = task("K1", "excavator", 67.2, "m3", "S2", "Z2", name="Cut berm key trench")
    away = task("H1", "hauler", 67.2, "m3", "dump", "Z4", after=("K1",))
    r = reqs(berm)
    g = TaskGraph(tasks=[trench, away])
    assert source_hauls(r.structures[0], g, r.structures) == []
    failure = validate_graph(r, g, coverage=True, volume=False, supply=False)
    assert failure is not None and failure.conflicts == ["scope:S2"]
    assert ("haul (an operation the brief asks for) but no task hauls to it" in failure.hint
            and "add hauler and compactor tasks tagged S2" in failure.hint)   # true for a body
    pile = {"id": "S3", "type": "spoil_stockpile", "zone": "Z3", "operations": ["haul"]}
    load = task("L1", "excavator", 50, "m3", "S3", "Z3", name="Excavate stockpile for loading")
    out = task("H2", "hauler", 50, "m3", "dump", "Z4", after=("L1",))
    r = reqs(pile)
    assert source_hauls(r.structures[0], TaskGraph(tasks=[load, out]), r.structures) == []


def mound_plan() -> tuple[Requirements, TaskGraph]:
    """Run 3 shielding_mound ultra #1 as saved (``tests/data/shielding_mound_live.json``)."""
    data = json.loads((DATA / "shielding_mound_live.json").read_text())
    r = Requirements.model_validate(data["requirements"])
    r.source_brief = brief("shielding_mound")
    return r, TaskGraph.model_validate(data["task_graph"])


def test_a_mound_dug_at_its_foot_still_needs_its_fill_hauled_onto_it() -> None:
    """Seen in review: the shielding mound (``diameter_m``, ``height_m``, ``thickness_m``: no
    volume shape) counted as dug for its material, so run 3's plan with its fill haul T4
    swapped for a 2.5 m3 haul of the mound-tagged foundation dig T1 to the borrow pit
    validated clean, though the mound receives no fill at all, and the hint of a round that
    lost the fill haul asked for exactly that edge. A mound is filled: its haul is the fill
    brought onto it, and its hint is the one it had before phase 3j."""
    r, g = mound_plan()
    mound = r.structures[0]
    assert mound.operations == ["excavate", "haul", "compact", "sinter"]
    assert validate_graph(r, g, coverage=True, volume=True) is None          # as saved
    assert source_hauls(mound, g, r.structures) == []
    away = task("T4", "hauler", 2.5, "m3", "S2", "Z2", after=("T1",),
                name="haul foundation spoil to borrow pit")
    swapped = TaskGraph(tasks=[away if t.id == "T4" else t for t in g.tasks])
    failure = validate_graph(r, swapped, coverage=True, volume=True)
    assert failure is not None and failure.conflicts == ["scope:S1"]
    assert failure.details == ["S1 (shielding_mound) lacks haul task (its operations list haul)"]
    assert failure.hint.startswith(
        "Structure coverage is incomplete: S1 (shielding_mound) lists haul (the brief asks for "
        "\"about 120 cubic metres of fill\") but no task hauls to it; add a hauler task tagged "
        "S1; every structure keeps its phases")
    # nothing dug at its foot and no fill: an excavator and a hauler task tagged with it
    gone = {"T1", "T4"}
    bare = TaskGraph(tasks=[t.model_copy(update={"after": [a for a in t.after if a not in gone]})
                            for t in g.tasks if t.id not in gone])
    failure = validate_graph(r, bare, coverage=True, volume=False, supply=False)
    assert failure is not None and failure.conflicts == ["scope:S1"]
    assert "but no task hauls to it; add excavator and hauler tasks tagged S1;" in failure.hint
    assert "waits for" not in failure.hint


def test_an_earth_bridge_is_filled_from_the_trench_not_dug() -> None:
    """cable_trench's crossing, as Lightning recorded it ("a 10 m long, 4 m wide earth bridge
    of compacted fill, 0.5 m thick ..., taken from the trench spoil": ``earth_bridge`` with a
    ``thickness_m``): a seat dug for it and carried off to a dump is no fill of it, the trench
    spoil hauled onto it is."""
    recorded = json.loads(RECORDED.read_text())["cable_trench"]
    trench = next(s for s in recorded if s["type"] == "cable_trench")
    bridge = next(s for s in recorded if s["type"] == "earth_bridge")
    r = reqs(trench, bridge, source=brief("cable_trench"))
    cut = task("T1", "excavator", 36, "m3", "S1", "Z1")
    seat = task("T2", "excavator", 2, "m3", "S2", "Z2", name="Excavate bridge seat")
    pack = task("P1", "compactor", 40, "m2", "S2", "Z2", after=("H1",))
    bed = task("P2", "compactor", 60, "m2", "S1", "Z1", after=("T1",))
    dumped = TaskGraph(tasks=[cut, seat, task("H1", "hauler", 2, "m3", "dump", "Z4",
                                              after=("T2",)), pack, bed])
    assert source_hauls(r.structures[1], dumped, r.structures) == []
    failure = validate_graph(r, dumped, coverage=True, volume=False, supply=False)
    assert failure is not None and failure.conflicts == ["scope:S2"]
    filled = TaskGraph(tasks=[cut, seat, task("H1", "hauler", 20, "m3", "S2", "Z2",
                                              after=("T1",)), pack, bed])
    assert validate_graph(r, filled, coverage=True, volume=False, supply=False) is None


def test_only_a_dug_body_is_hauled_by_the_hauls_its_digs_feed() -> None:
    """Every structure Lightning recorded with a listed haul (``recorded_structures.json``):
    the ramps cut into the crater wall, the dug borrow strips and the drainage trench are dug
    for their material; the mound, the earth bridge, the solar pad's fill layer, the berms
    (depleted_excavators' dust berm typed ``borrow_strip`` among them), the lanes, the apron
    and the piles are filled. A type that names fill wins over a dug shape, a placed layer
    over a type that names nothing, and a type that names a dig counts without a shape."""
    recorded = json.loads(RECORDED.read_text())
    listed = [(name, Structure.model_validate(data)) for name, structures in recorded.items()
              if not name.startswith("_") for data in structures
              if "haul" in data.get("operations", [])]
    assert {(name, s.type) for name, s in listed if _source(s)} == {
        ("crater_ramp", "access_ramp"), ("crater_ramp", "ramp"),
        ("habitat_berm_tight_horizon", "borrow_strip"),
        ("habitat_foundation", "perimeter_drainage_trench"),
        ("small_fleet_long_berm", "borrow_strip")}
    filled = {(name, s.type) for name, s in listed if not _source(s)}
    assert {("shielding_mound", "shielding_mound"), ("cable_trench", "earth_bridge"),
            ("solar_array_pad", "solar_array_pad"), ("depleted_excavators", "borrow_strip"),
            ("tight_battery", "approach_apron"), ("propellant_pit_and_berm", "haul_lane")} <= filled
    assert not _source(Structure(id="E", type="embankment", length_m=30, width_m=6, depth_m=1))
    assert not _source(Structure(id="P", type="raised_pad", diameter_m=12, thickness_m=0.5))
    assert not _source(Structure(id="A", type="work_area", length_m=20, width_m=10))
    assert _source(Structure(id="B", type="borrow_pit"))
    assert _source(Structure(id="Q", type="quarry", length_m=20, width_m=10))
    assert _source(Structure(id="F", type="foundation_pit", length_m=12, width_m=12,
                             depth_m=1, thickness_m=0.2))     # a dug body, its slab aside


# ----------------------------------------------------------------------------- the quote
HABITAT = (
    {"id": "S1", "type": "excavation_pit", "zone": "Z1", "length_m": 12, "width_m": 12,
     "depth_m": 1.0},
    {"id": "S2", "type": "berm", "zone": "Z2", "length_m": 70, "height_m": 2, "width_m": 4},
    {"id": "S3", "type": "borrow_strip", "zone": "Z3", "length_m": 40, "width_m": 6,
     "depth_m": 0.5},
)


def test_the_strips_hint_never_quotes_the_berms_entry() -> None:
    r = reqs(*HABITAT, source=brief("habitat_berm_tight_horizon"))
    q = {(s.id, op): brief_evidence(r.source_brief, op, s, r.structures)
         for s in r.structures for op in ("haul", "compact", "survey", "grade")}
    # the berm's own entry is the berm's, whatever its body names ("plus the borrow strip")
    assert q["S2", "haul"] == "about 280 cubic metres of fill"
    assert q["S2", "compact"] == q["S2", "survey"] == "compacted in one lift and inspected"
    assert q["S2", "grade"] is None            # "plus the borrow strip" is a name, not an ask
    # and never the strip's: its own entry asks for no haul, compaction or inspection
    assert q["S3", "haul"] is q["S3", "compact"] is q["S3", "survey"] is None
    assert q["S1", "compact"] == "base levelled and compacted"
    owners = entry_owners(next(line for line in r.source_brief.splitlines()
                               if line.startswith("2. Plume berm")), r.structures)
    assert [s.id for s in owners] == ["S2"]


@pytest.mark.parametrize(("name", "structures", "which", "phase", "quote"), [
    # the pit's grade was the haul lane's entry ("a 40 m by 5 m stripped ... lane ... to the pit")
    ("propellant_pit_and_berm",
     ({"id": "PIT", "type": "pit", "zone": "Z1", "diameter_m": 8, "depth_m": 2},
      {"id": "BERM", "type": "berm", "zone": "Z2", "length_m": 30, "height_m": 2.5},
      {"id": "HAUL_LANE", "type": "lane", "zone": "Z3", "length_m": 40, "width_m": 5}),
     "PIT", "grade", None),
    ("propellant_pit_and_berm",
     ({"id": "PIT", "type": "pit", "zone": "Z1", "diameter_m": 8, "depth_m": 2},
      {"id": "BERM", "type": "berm", "zone": "Z2", "length_m": 30, "height_m": 2.5},
      {"id": "HAUL_LANE", "type": "lane", "zone": "Z3", "length_m": 40, "width_m": 5}),
     "HAUL_LANE", "grade", "a 40 m by 5 m stripped and compacted lane from the depot to the pit"),
    # the borrow pit's compaction was the mound's entry ("... compacted lifts from a borrow pit")
    ("shielding_mound",
     ({"id": "S1", "type": "shielding_mound", "zone": "Z1", "height_m": 2},
      {"id": "S2", "type": "borrow_pit", "zone": "Z2", "length_m": 15, "width_m": 10,
       "depth_m": 1}),
     "S2", "compact", None),
    # Lightning typed crater_ramp's floor landing "pad": its entry, "3. Floor landing: a
    # compacted 8 m by 8 m pad at the foot of the ramp", is the pad's by its figures, never the
    # ramp's compaction (nor is the turnaround's, "compacted so haulers can turn")
    ("crater_ramp",
     ({"id": "S1", "type": "ramp", "zone": "Z1", "length_m": 40, "width_m": 5, "depth_m": 1.2},
      {"id": "S2", "type": "turnaround", "zone": "Z2", "length_m": 10, "width_m": 10},
      {"id": "S3", "type": "pad", "zone": "Z3", "length_m": 8, "width_m": 8}),
     "S1", "compact", None),
    ("crater_ramp",
     ({"id": "S1", "type": "ramp", "zone": "Z1", "length_m": 40, "width_m": 5, "depth_m": 1.2},
      {"id": "S2", "type": "turnaround", "zone": "Z2", "length_m": 10, "width_m": 10},
      {"id": "S3", "type": "pad", "zone": "Z3", "length_m": 8, "width_m": 8}),
     "S3", "compact", "a compacted 8 m by 8 m pad at the foot of the ramp"),
    # run 3 #3 typed propellant_pit_and_berm's berm "borrow_strip" with a 4 m width the brief
    # never states: its entry is the berm's by its length and height, never the pit's haul
    # (seen in review: "built from pit spoil and compacted" quoted for PIT)
    ("propellant_pit_and_berm",
     ({"id": "PIT", "type": "pit", "zone": "Z1", "diameter_m": 8, "depth_m": 2,
       "thickness_m": 0.2},
      {"id": "BERM", "type": "borrow_strip", "zone": "Z2", "length_m": 30, "height_m": 2.5,
       "width_m": 4},
      {"id": "HAUL_LANE", "type": "haul_lane", "zone": "Z3", "length_m": 40, "width_m": 5}),
     "PIT", "haul", None),
    # Lightning typed depleted_excavators' dust berm "borrow_strip": its own entry by its figures
    ("depleted_excavators",
     ({"id": "S1", "type": "stockpile", "zone": "Z1", "diameter_m": 12, "depth_m": 0.5},
      {"id": "S2", "type": "borrow_strip", "zone": "Z2", "length_m": 60, "height_m": 1.2,
       "width_m": 3, "trench_width_m": 1.2, "trench_depth_m": 0.8},
      {"id": "S3", "type": "borrow_pit", "zone": "Z3", "length_m": 8, "width_m": 8,
       "depth_m": 0.9}),
     "S2", "compact", "compacted in one lift"),
])
def test_a_quote_comes_from_the_structures_own_entry_or_none(
        name: str, structures: tuple[dict, ...], which: str, phase: str, quote: str | None) -> None:
    r = reqs(*structures, source=brief(name))
    s = next(x for x in r.structures if x.id == which)
    assert brief_evidence(r.source_brief, phase, s, r.structures) == quote


def test_entry_owners() -> None:
    pad = Structure(id="S1", type="landing_pad")
    berm = Structure(id="S2", type="blast_berm")
    strip = Structure(id="S3", type="borrow_strip", length_m=40, width_m=6)
    both = [pad, berm, strip]
    assert entry_owners("2. Blast berm: built from pad spoil and the borrow strip.", both) == [berm]
    assert entry_owners("- Borrow strip: 40 m by 6 m.", both) == [strip]
    assert entry_owners("Pad and berm: compacted together.", both) == [pad, berm]
    # a name that is no type's: the structure whose recorded size the line states, else the
    # one whose recorded length and height it states (beside a width it does not)
    assert entry_owners("3. Spare cut: 40 m by 6 m, 0.5 m deep.", both) == [strip]
    mound = Structure(id="S4", type="spoil_heap", length_m=30, height_m=2.5, width_m=4)
    assert entry_owners("2. Protective berm: a 30 m arc, 2.5 m high.", [pad, mound]) == [mound]
    assert entry_owners("2. Protective berm: a 30 m arc, 3 m high.", [pad, mound]) == []
    # a sentence, a bold label, a heading, a name that is nobody's: no entry
    for line in ("- The berm is built from the borrow strip.", "**Site.** Next to the pad.",
                 "# Landing pad: notes", "Note: the pad comes first."):
        assert entry_owners(line, both) == [], line
