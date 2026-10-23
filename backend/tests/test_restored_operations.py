"""Tests for the operations a structure's own entry states that the structuring omitted (phase
3l): the brief reader (``moonwright.pipeline.tags.entry_operations``), the restoration at the
structure stage (``moonwright.pipeline.structure.restore_operations``,
``moonwright.pipeline.validate.restored_operations``) and the judgment of a saved plan
against it (``moonwright.pipeline.validate.restored_gaps`` / ``in_scope_gaps``). Seen live in
run 3: Nemotron 3.5 Lightning typed front_loaded_excavation's pad S1 ``excavator`` with
operations [excavate, survey] (``pad`` with the same two in the brief's other two
structurings), dropping the compaction its own entry states ("1. Heavy-lander pad: ...,
subgrade compacted and inspected. Sintering is deferred to a later sortie; no printer is on
site."). The 28 h of pad compaction were never required, the one-robot-per-zone deadline no
longer bit, and Super #1 "verified" the designed-impossible brief (kind D) at 60 h, counted in
scope (``tests/data/front_loaded_super_live.json``). The structurings below are the 198 saved
ones of runs 2, 3 and 4 with the fleets they recorded (``tests/data/saved_structurings.json``)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from moonwright.cli import eval_row, plan_summary, scope_shortfall
from moonwright.llm.base import Usage
from moonwright.pipeline.rates import scope_gaps
from moonwright.pipeline.structure import (
    correct_operations,
    restore_operations,
    restore_warnings,
    site_warnings,
    structure_brief,
)
from moonwright.pipeline.tags import entry_operations
from moonwright.pipeline.validate import (
    in_scope_gaps,
    required_phases,
    restored_gaps,
    restored_operations,
    unsupported_operations,
    validate_graph,
    with_restored_operations,
)
from moonwright.schemas import (
    LoopStep,
    PlanResult,
    Requirements,
    Schedule,
    Structure,
    TaskGraph,
)

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "examples" / "eval"
DATA = Path(__file__).resolve().parent / "data"
SAVED = json.loads((DATA / "saved_structurings.json").read_text())
FLEET = ("surveyor", "excavator", "hauler", "compactor", "printer")
PAD_NOTE = ('structure S1: operations compact restored: its entry states "subgrade compacted '
            'and inspected"')
PAD_GAP = ('scope:S1: its entry states compact ("subgrade compacted and inspected"), which the '
           "structuring omitted, and no task compacts it")


def brief(name: str) -> str:
    return (EVAL / f"{name}.md").read_text()


def reqs(*structures: dict, fleet: tuple[str, ...] | list[str] = FLEET,
         source: str | None = None) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (7 * i, 10)} for i in range(1, 6)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures],
        robots=[{"id": f"R{i}", "type": t, "battery_wh": 5000} for i, t in enumerate(fleet, 1)],
        horizon_h=336, source_brief=source)


def saved(run: str, key: str) -> Requirements:
    """One saved structuring of run 2, 3 or 4 as Lightning recorded it, with its fleet and its
    brief (``key`` is ``<brief>__<repeat>``)."""
    data = SAVED[run][key]
    return reqs(*data["structures"], fleet=data["fleet"], source=brief(key.rsplit("__", 1)[0]))


def read(source: str, sid: str, *structures: dict) -> list[str]:
    """The operations ``sid``'s own entry in ``source`` states (no quotes)."""
    r = reqs(*structures, source=source)
    s = next(x for x in r.structures if x.id == sid)
    return [op for op, _ in entry_operations(source, s, r.structures)]


PAD = {"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.5}
PIT = {"id": "S1", "type": "storage_pit", "zone": "Z1", "diameter_m": 8, "depth_m": 2}
BERM = {"id": "S2", "type": "spoil_berm", "zone": "Z2", "length_m": 70, "height_m": 2}
ROAD = {"id": "S3", "type": "access_road", "zone": "Z3", "length_m": 100, "width_m": 5}


# ----------------------------------------------------------------------------- live: run 3
@pytest.mark.parametrize("n", [1, 2, 3])
def test_the_run_3_front_loaded_structurings_get_compact_on_the_pad(n: int) -> None:
    r = saved("run3", f"front_loaded_excavation__{n}")
    pad = r.structures[0]
    assert (pad.id, pad.operations) == ("S1", ["excavate", "survey"])
    assert restore_operations(r) == [PAD_NOTE]
    assert r.structures[0].operations == ["excavate", "survey", "compact"]
    # restoring is not retyping: #1's pad stays the "excavator" Lightning typed it
    assert r.structures[0].type == ("excavator" if n == 1 else "pad")
    assert restore_warnings(r) == []   # nothing left to restore


def test_the_pads_entry_states_no_sinter_even_with_a_printer() -> None:
    """"Sintering is deferred to a later sortie; no printer is on site": with a printer in the
    fleet and nothing listed, the entry still states excavate, compact and survey only."""
    r = saved("run3", "front_loaded_excavation__2")
    s = r.structures[0].model_copy(update={"operations": []})
    assert entry_operations(r.source_brief, s, [s, *r.structures[1:]]) == [
        ("excavate", "excavated 0.4 m to firm regolith across the full footprint"),
        ("compact", "subgrade compacted and inspected"),
        ("survey", "subgrade compacted and inspected")]
    with_printer = r.model_copy(update={"structures": [s, *r.structures[1:]], "robots": [
        *r.robots, *reqs(fleet=("printer",)).robots]})
    assert [op for op, _ in restored_operations(with_printer, s)] == [
        "excavate", "compact", "survey"]


def test_no_operation_the_fleet_cannot_staff_is_restored() -> None:
    r = saved("run3", "front_loaded_excavation__1")
    without = r.model_copy(update={"robots": [x for x in r.robots if x.type != "compactor"]})
    assert restore_warnings(without) == []
    assert with_restored_operations(without) is without


def saved_plan() -> PlanResult:
    live = json.loads((DATA / "front_loaded_super_live.json").read_text())
    req = Requirements.model_validate({**live["requirements"],
                                       "source_brief": brief("front_loaded_excavation")})
    return PlanResult(requirements=req, task_graph=TaskGraph.model_validate(live["task_graph"]),
                      schedule=Schedule(entries=[], makespan_h=live["makespan_h"],
                                        status="optimal"),
                      success=live["success"], steps=[LoopStep(stage="structure", ok=True)])


def test_super_1_is_out_of_scope_against_the_restored_operations() -> None:
    """The plan as saved: every judged volume and area is in scope and the validator passes it
    against the requirements it was planned from; read with the compaction the pad's entry
    states, no task compacts S1, the eval counts it out of scope and the validator, checking it
    against the restored requirements, answers ``scope:S1``."""
    result = saved_plan()
    r, g = result.requirements, result.task_graph
    assert g is not None and result.schedule is not None and result.schedule.makespan_h == 60
    assert not any(t.robot_type == "compactor" and t.structure == "S1" for t in g.tasks)
    assert scope_gaps(r, g) == []
    assert validate_graph(r, g, coverage=True) is None
    assert restored_gaps(r, g) == in_scope_gaps(r, g) == [PAD_GAP]
    failure = validate_graph(with_restored_operations(r), g, coverage=True)
    assert failure is not None and failure.conflicts == ["scope:S1"]
    assert failure.details == ["S1 (excavator) lacks compact task (its operations list compact)"]
    row = eval_row("front_loaded_excavation", "super/loop_on", 1, result, 1.0)
    assert (row.success, row.in_scope) == (True, False)
    assert scope_shortfall(row) == "scope differs from the brief"   # every share is in scope
    # the gap is the brief's, not the dimensions' (those are all in scope)
    assert plan_summary(result, Path("out/x.plan.json")).splitlines()[1:] == [
        "verified against the planning model; scope differs from the brief:",
        f"  {PAD_GAP}"]


def test_a_plan_that_does_the_restored_work_stays_in_scope() -> None:
    result = saved_plan()
    r, g = result.requirements, result.task_graph
    assert g is not None
    compact = g.tasks[-1].model_copy(update={
        "id": "C9", "name": "compact S1 subgrade", "robot_type": "compactor", "structure": "S1",
        "zone": "Z1", "after": ["E3"], "quantity": 706.858, "unit": "m2"})
    done = g.model_copy(update={"tasks": [*g.tasks, compact]})
    assert restored_gaps(r, done) == [] and in_scope_gaps(r, done) == []


@pytest.mark.parametrize("n", [1, 2])
def test_run_3_small_fleet_long_berm_gets_its_haul(n: int) -> None:
    """Lightning listed [grade, compact, survey] on the berm "built from a borrow strip along
    its north side": the haul that builds it was never required."""
    r = saved("run3", f"small_fleet_long_berm__{n}")
    assert restore_warnings(r) == [('structure BERM: operations haul restored: its entry states '
                                    '"built from a borrow strip along its north side"')]


# ----------------------------------------------------------------------------- the reading
@pytest.mark.parametrize(("entry", "ops"), [
    ("excavated 0.5 m, compacted (not sintered).", ["excavate", "compact"]),
    ("excavated 0.5 m, subgrade compacted. No sintering this sortie.", ["excavate", "compact"]),
    ("excavated 0.5 m, subgrade compacted. Sintering is a later job.", ["excavate", "compact"]),
    ("excavated 0.5 m. Sintering is deferred to a later sortie; no printer is on site.",
     ["excavate"]),
    ("levelled and graded rather than excavated, never sintered.", []),
    ("compacted unless the regolith is already firm, sintered instead of paved.", []),
    ("nothing is hauled and nothing is sintered this campaign.", []),
    ("a 15 percent grade, levelled, graded and smoothed.", []),           # grade: never
    ("a compact surface of fines, kept level.", []),                    # an adjective
    ("Excavate a bowl 0.4 m deep, compact the subgrade, then sinter a 30 cm surface.",
     ["excavate", "compact", "sinter"]),
    ("staked, dug 0.5 m, inspected after compaction.", ["survey", "excavate", "compact"]),
    ("lightly compacted so it does not blow away.", ["compact"]),
    ("built from two 60 m segments, compaction optional.", []),          # parts, not material
    ("excavated by the excavator, ready for inspection.", ["excavate", "survey"]),
    # a negation heads the rest of its list, up to the "or"
    ("0.5 m deep. No sintering, compaction or inspection this sortie.", []),
    ("0.5 m deep, not to be excavated, compacted or sintered this sortie.", []),
    ("no loose rock, compacted to 95 percent or better.", ["compact"]),   # it heads no operation
    # and an item coordinated with it by "and"; one that names something else reaches no further
    ("0.5 m deep, must not be excavated and compacted before the lander survey.", []),
    ("the existing apron, already graded and compacted.", []),
    ("excavated 0.5 m without blasting and compacted.", ["compact"]),
    ("excavated 0.5 m and compacted, not sintered.", ["excavate", "compact"]),
    # a deferral that trails a list of operations named as nouns covers them all
    ("0.5 m deep. Sintering and compaction are deferred to a later sortie.", []),
    ("0.5 m deep. Sintering, compaction and inspection are deferred to a later sortie.", []),
    # cannot, already, postponed, excluded, out of scope, the next sortie
    ("0.5 m deep; the bowl cannot be excavated in parallel sectors.", []),
    ("0.5 m deep; hauling cannot start before the lander survey.", []),
    ("0.5 m deep. Compaction is out of scope for this sortie.", []),
    ("0.5 m deep. Compaction follows in the next sortie.", []),
    ("0.5 m deep, compaction excluded.", []),
    ("0.5 m deep. Sintering postponed to phase 2.", []),
])
def test_what_an_entry_states(entry: str, ops: list[str]) -> None:
    assert read(f"1. Landing pad: 20 m diameter, {entry}\n", "S1", PAD) == ops


def test_another_structures_words_restore_nothing() -> None:
    """A haul is the entry's own ("built from the pit spoil" is the berm's haul, never an ask to
    excavate it); any other operation word on a structure named next to it is that one's."""
    source = ("1. Storage pit: 8 m diameter, 2 m deep.\n"
              "2. Spoil berm: 70 m long, 2 m high, built from the pit spoil, compacted.\n"
              "3. Access road: 100 m long, 5 m wide, graded up to where the pit is excavated.\n")
    assert read(source, "S2", PIT, BERM, ROAD) == ["haul", "compact"]
    assert read(source, "S1", PIT, BERM, ROAD) == []
    assert read(source, "S3", PIT, BERM, ROAD) == []
    dug = "2. Spoil berm: 70 m long, 2 m high, built from spoil excavated from the pit.\n"
    assert read(dug, "S2", PIT, BERM) == ["haul"]
    road = "3. Access road: 100 m long, 5 m wide, graded and compacted from the depot to the pit.\n"
    assert read(road, "S3", PIT, ROAD) == ["compact"]                    # a span, not the pit
    # the pit's entry about the berm: the berm's compaction is not the pit's, its dig is
    told = ("1. Storage pit: 8 m diameter, 2 m deep; the berm is compacted after the pit is "
            "excavated.\n2. Spoil berm: 70 m long, 2 m high.\n")
    assert read(told, "S1", PIT, BERM) == ["excavate"] and read(told, "S2", PIT, BERM) == []


@pytest.mark.parametrize(("entries", "structures", "ops"), [
    # another structure named before the word in its part, outside a "from ..." phrase
    (("1. Landing pad: 20 m diameter.\n2. Spoil berm: 70 m long, 2 m high; placing starts once "
      "the pad subgrade is compacted.\n"), (PAD, BERM), []),
    (("1. Storage pit: 8 m diameter, 2 m deep.\n2. Spoil berm: 70 m long, 2 m high, built from "
      "the pit spoil and compacted.\n"), (PIT, BERM), ["haul", "compact"]),
    # or in the part an "and ..." item is coordinated with
    (("1. Landing pad: 20 m diameter.\n2. Spoil berm: 70 m long, 2 m high. The access road "
      "beside it is graded and compacted.\n3. Access road: 100 m long, 5 m wide.\n"),
     (PAD, BERM, ROAD), []),
    (("1. Landing pad: 20 m diameter.\n2. Spoil berm: 70 m long, 2 m high; it waits until the "
      "pad is inspected and compacted.\n"), (PAD, BERM), []),
    # the noun a relative clause follows
    (("1. Landing pad: 20 m diameter.\n2. Spoil berm: 70 m long, 2 m high, from the depot to "
      "the pad, which is compacted first.\n"), (PAD, BERM), []),
    # a dig word after material: dug elsewhere, whether or not the source is a structure
    ("2. Spoil berm: 70 m long, 2 m high, built from spoil excavated nearby.\n", (BERM,),
     ["haul"]),
    (("2. Spoil berm: 70 m long, 2 m high, built from regolith dug from a borrow strip along its "
      "north side.\n"), (BERM,), ["haul"]),
    # a structure word none of the berm's names, in a clause of time
    (("1. Landing pad: 20 m diameter.\n2. Spoil berm: 70 m long, 2 m high, hauled only after "
      "the bowl is excavated and compacted.\n"), (PAD, BERM), ["haul"]),
    # a constraint whose head is a sentence is no entry: the berm's line below is the berm's
    (("1. Landing pad: 20 m diameter.\n2. Spoil berm: 70 m long, 2 m high.\n- The berm: it "
      "waits until the pad is inspected and compacted.\n"), (PAD, BERM), []),
])
def test_what_is_said_of_another_structure_restores_nothing(
        entries: str, structures: tuple[dict, ...], ops: list[str]) -> None:
    assert read(entries, "S2", *structures) == ops


def test_a_constraints_head_names_no_structure() -> None:
    """"- The pit is one work zone: ..." is a constraint, not the pit's entry: its words ("one",
    "work", "zone") are no name of the pit, so the berm's "compacted in one lift" is the
    berm's."""
    pit = {"id": "S1", "type": "foundation_pit", "zone": "Z1", "length_m": 12, "width_m": 12,
           "depth_m": 1}
    berm = {"id": "S2", "type": "plume_berm", "zone": "Z2", "length_m": 70, "height_m": 2}
    source = ("1. Foundation pit: 12 m by 12 m, 1 m deep.\n"
              "2. Plume berm: 70 m long, 2 m high, compacted in one lift and inspected.\n"
              "- The pit is one work zone: only one robot may work in it at a time.\n")
    assert read(source, "S2", pit, berm) == ["compact", "survey"]
    assert read(source, "S1", pit, berm) == []
    # the live brief's constraint "- The pad is one work zone: ..., so the bowl cannot be
    # excavated as parallel sectors" is no entry of the pad typed "pad": without line 1's own
    # dig, nothing states excavate
    r = saved("run3", "front_loaded_excavation__2")
    live = r.source_brief or ""
    assert "- The pad is one work zone:" in live
    levelled = live.replace("excavated 0.4 m to firm regolith", "levelled to firm regolith")
    assert [op for op, _ in entry_operations(levelled, r.structures[0], r.structures)] == [
        "compact", "survey"]
    # nor is its body the pad's entry ("... so the bowl is excavated as one sector")
    pad = ("1. Landing pad: 20 m diameter, compacted.\n- The pad is one work zone: plume and dust "
           "safety allows one robot in it at a time, so the bowl is excavated as one sector.\n")
    assert read(pad, "S1", PAD) == ["compact"]


def test_the_live_briefs_tricky_entries() -> None:
    # the trench Lightning typed berm: dug, backfilled with compacted spoil, its bed inspected
    r = saved("run3", "habitat_foundation__2")
    trench = r.structures[1]
    assert entry_operations(r.source_brief, trench, r.structures) == [
        ("excavate", "dug after the pit walls are stable and backfilled with compacted spoil"),
        ("compact", "backfilled with compacted spoil once the cable bed is inspected"),
        ("survey", "the cable bed is inspected")]
    # "the excavated material placed on the plant's stockpile pad": dug elsewhere
    r = saved("run3", "isru_stockpile__1")
    assert [op for op, _ in entry_operations(r.source_brief, r.structures[1], r.structures)] == [
        "compact"]
    # a shared entry ("1. Cargo pad A, 2. cargo pad B, 3. cargo pad C: each ...") is no one's
    r = saved("run3", "three_pads_in_order__1")
    assert all(entry_operations(r.source_brief, s, r.structures) == [] for s in r.structures)


def test_a_hard_wrapped_entry_reads_whole() -> None:
    source = ("1. Protective berm: 100 m long, 2 m high, built from a borrow strip along its\n"
              "   north side, placed and compacted in two lifts and\n   inspected.\n")
    berm = {"id": "B", "type": "protective_berm", "zone": "Z1", "length_m": 100, "height_m": 2}
    assert read(source, "B", berm) == ["haul", "compact", "survey"]


# ----------------------------------------------------------------------------- 3j's softening
def test_a_restored_dig_is_not_softened_for_the_grade_listed_beside_it() -> None:
    """A pad that lists [grade, compact] and whose entry says "dug 0.3 m to firm regolith,
    levelled and compacted": the dig is restored, and 3j's softening rule, which would call it
    an alternative the brief does not ask for (no evidence stem prefixes "dug"), keeps it
    required; no note contradicts the restore note."""
    r = reqs({**PAD, "operations": ["grade", "compact"]},
             source="1. Landing pad: 20 m diameter, dug 0.3 m to firm regolith, levelled and "
                    "compacted.\n")
    assert restore_operations(r) == [
        ('structure S1: operations excavate restored: its entry states "dug 0.3 m to firm '
         'regolith"')]
    assert correct_operations(r) == []
    s = r.structures[0]
    assert s.operations == ["grade", "compact", "excavate"]
    assert unsupported_operations(r, s) == [] and site_warnings(r) == []
    assert ("excavate",) in required_phases(r, s) and ("grade",) in required_phases(r, s)


def test_a_restored_haul_is_not_softened_for_the_key_trench_dig() -> None:
    """A berm that lists [excavate, compact] on "a key trench cut 0.5 m into firm ground, built
    from a borrow strip along its north side": the haul is restored and both stay required."""
    berm = {"id": "B1", "type": "protective_berm", "zone": "Z1", "length_m": 100,
            "height_m": 2, "operations": ["excavate", "compact"]}
    strip = {"id": "B2", "type": "borrow_strip", "zone": "Z2", "length_m": 100, "width_m": 6,
             "depth_m": 0.6, "operations": ["excavate"]}
    r = reqs(berm, strip, fleet=("excavator", "hauler", "compactor", "surveyor"), source=(
        "1. Protective berm: 100 m long, 2 m high, on a key trench cut 0.5 m into firm ground, "
        "built from a borrow strip along its north side.\n"
        "2. Borrow strip: 100 m by 6 m, 0.6 m deep.\n"))
    assert restore_operations(r) == [
        ('structure B1: operations haul restored: its entry states "built from a borrow strip '
         'along its north side"')]
    correct_operations(r)
    s = r.structures[0]
    assert s.operations == ["excavate", "compact", "haul"]
    assert unsupported_operations(r, s) == [] and site_warnings(r) == []
    assert {("excavate",), ("haul",)} <= set(required_phases(r, s))


# ----------------------------------------------------------------------------- every structuring
RESTORED = [
    ("run2", "large_fleet_pad_berm_road__2", "S3", "compact"),   # the road, typed stockpile
    ("run2", "solar_array_pad__3", "S2", "compact"),             # the anchor trench
    ("run3", "front_loaded_excavation__1", "S1", "compact"),
    ("run3", "front_loaded_excavation__2", "S1", "compact"),
    ("run3", "front_loaded_excavation__3", "S1", "compact"),
    ("run3", "habitat_berm_tight_horizon__2", "S2", "survey"),
    ("run3", "habitat_foundation__2", "F2", "excavate"),         # the trench, typed berm
    ("run3", "small_fleet_long_berm__1", "BERM", "haul"),
    ("run3", "small_fleet_long_berm__2", "BERM", "haul"),
    ("run4", "cable_trench__2", "S1", "compact"),                # the trench, typed excavate
    ("run4", "front_loaded_excavation__1", "S1", "compact"),
    ("run4", "front_loaded_excavation__2", "S1", "compact"),
    ("run4", "front_loaded_excavation__3", "S1", "compact"),
    ("run4", "habitat_berm_tight_horizon__1", "S2", "survey"),
    ("run4", "habitat_berm_tight_horizon__3", "S2", "survey"),
    ("run4", "small_fleet_long_berm__3", "B1", "haul"),          # listed as [grade] alone
    ("run4", "small_fleet_long_berm__3", "B1", "compact"),
    ("run4", "small_fleet_long_berm__3", "B1", "survey"),
]


def test_every_saved_structuring_restores_exactly_these() -> None:
    """Every saved structuring of runs 2, 3 and 4, read against its brief and its own fleet, in
    the stage's order (restore, then the phase 3j drop): exactly the eighteen restorations
    above, never a grade, the drop never takes a restored operation back, and 3j's softening
    never makes one optional again."""
    found: list[tuple[str, str, str, str]] = []
    for run in ("run2", "run3", "run4"):
        for key in SAVED[run]:
            r = saved(run, key)
            before = {s.id: list(s.operations) for s in r.structures}
            notes = restore_operations(r)
            correct_operations(r)
            added = {s.id: [op for op in s.operations if op not in before[s.id]]
                     for s in r.structures}
            found += [(run, key, sid, op) for sid, ops in added.items() for op in ops]
            assert len(notes) == sum(bool(ops) for ops in added.values()), (run, key)
            assert not any(op in added[s.id] for s in r.structures
                           for op, _ in unsupported_operations(r, s)), (run, key)
    assert found == RESTORED
    assert len([k for run in ("run2", "run3", "run4") for k in SAVED[run]]) == 198


def test_run_4s_new_shapes() -> None:
    """Run 4 restores two shapes runs 2 and 3 never showed: a trench Lightning typed with an
    operation's name, and a berm listed as [grade] alone that gets its haul, its compaction and
    its inspection."""
    r = saved("run4", "cable_trench__2")
    assert (r.structures[0].type, r.structures[0].operations) == ("excavate",
                                                                  ["survey", "excavate"])
    assert restore_warnings(r) == [
        ('structure S1: operations compact restored: its entry states "with a compacted 10 cm '
         'bedding layer in each segment before the cable goes in"')]
    r = saved("run4", "small_fleet_long_berm__3")
    assert r.structures[0].operations == ["grade"]
    assert restore_warnings(r) == [
        ('structure B1: operations haul, compact and survey restored: its entry states "built '
         'from a borrow strip along its north side" and "placed and compacted in two lifts and '
         'inspected"')]


# ----------------------------------------------------------------------------- the stage
class FrontLoadedClient:
    """Answers the structure request with run 3 #1's requirements as Lightning recorded them."""

    name = "front-loaded"

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        r = saved("run3", "front_loaded_excavation__1")
        return schema.model_validate(r.model_dump(exclude={"source_brief"})), Usage(model="fl")


def test_the_structure_step_restores_the_compaction_and_says_so() -> None:
    req, step = structure_brief(FrontLoadedClient(), brief("front_loaded_excavation"), key="k")
    assert req is not None and step.ok and step.payload is not None
    assert step.payload["warnings"][0] == PAD_NOTE
    assert req.structures[0].operations == ["excavate", "survey", "compact"]
    assert req.structures[0].type == "excavator"
    # the planner sees the operation: the requirements it is handed list it
    assert '"operations":["excavate","survey","compact"]' in req.model_dump_json(
        exclude={"source_brief"})


@pytest.mark.parametrize("name", ["landing_pad", "landing_pad_v1", "access_road", "tight_battery"])
def test_the_fixture_structurings_restore_nothing(name: str) -> None:
    """The live landing_pad and the hand-made fixtures list every operation their entries state;
    the live front_loaded fixture is run 3's slip (``tests/test_pipeline_e2e.py``)."""
    raw = json.loads((ROOT / "fixtures" / name / "structure.json").read_text())
    path = ROOT / "examples" / "briefs" / f"{name}.md"
    source = (path if path.exists() else DATA / f"{name}.md").read_text()
    r = Requirements.model_validate({**raw, "source_brief": source})
    assert restore_warnings(r) == []


def test_the_registry_structurings_restore_nothing() -> None:
    """``scripts/brief_budget.py``'s registry lists, by hand, the operations each of the 22
    eval briefs asks for of each structure: the reader states none it does not list."""
    spec = importlib.util.spec_from_file_location("brief_budget", ROOT / "scripts" /
                                                  "brief_budget.py")
    assert spec is not None and spec.loader is not None
    bb = importlib.util.module_from_spec(spec)
    sys.modules["brief_budget"] = bb   # dataclasses look the module up by name
    spec.loader.exec_module(bb)
    paths = sorted(EVAL.glob("*.md"))
    assert len(paths) == 22
    for path in paths:
        data = bb.spec_from_brief(path)
        structures = [{k: v for k, v in s.items() if k != "fill_from"}
                      for s in data["structures"]]
        r = reqs(*structures, fleet=[x["type"] for x in data["robots"]], source=path.read_text())
        assert restore_warnings(r) == [], path.stem


def test_the_front_loaded_fixture_is_run_3s_slip() -> None:
    raw = json.loads((ROOT / "fixtures" / "front_loaded" / "structure.json").read_text())
    r = Requirements.model_validate({**raw, "source_brief": (
        ROOT / "examples" / "briefs" / "front_loaded.md").read_text()})
    assert restore_warnings(r) == [PAD_NOTE]
