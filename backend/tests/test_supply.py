"""Tests for the supply-order check (phase 3g, ``moonwright.pipeline.validate``, "Supply order"):
a haul of a whole plan must wait (``after``, transitively) for the sources of its material, so
CP-SAT cannot schedule it before that material exists, and the balance of what a body's
hauls move against what the digs before them remove is an advisory that reaches the repair
prompt.

The live cases are trimmed copies of run-2 PlanResults (``tests/data/*_live_plan.json``):
habitat (a repair dropped the berm hauls' edges to the pit and borrow digs, and the verified
schedule hauled 280 m3 of berm fill from hour 7 while the first dig ended at hour 13),
propellant (a brief-compliant lane hauled after its own m2 grading, which an m3-only rule
rejected), one_surveyor (a berm "built from the hardstand's strip spoil" hauled before the
strip, which an m3-only rule never judged) and tight_battery (a berm haul that waits for its
own 15 m3 key-trench cut alone satisfied a rule that asked for any dig)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moonwright.llm.base import Usage
from moonwright.llm.prompts import ADVISORIES_HEADER, repair_messages
from moonwright.pipeline.loop import Planner, _repeats_verdict
from moonwright.pipeline.rates import (
    STRIP_DEPTH_M,
    ancestors,
    apply_rates,
    is_dig,
    is_haul,
    is_strip,
    supply_m3,
)
from moonwright.pipeline.taskgraph import generate_task_graph
from moonwright.pipeline.validate import (
    SUPPLY_SLACK,
    SUPPLY_SLACK_M3,
    supply_advisories,
    supply_report,
    validate_graph,
)
from moonwright.schemas import Failure, Requirements, Structure, Task, TaskGraph

DATA = Path(__file__).resolve().parent / "data"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HINT_TAIL = ("; a haul part may wait for only the dig parts that remove what it moves, so split "
             "hauls to keep both chains short.")


def live(name: str = "habitat_berm") -> tuple[Requirements, TaskGraph]:
    data = json.loads((DATA / f"{name}_live_plan.json").read_text())
    return (Requirements.model_validate(data["requirements"]),
            TaskGraph.model_validate(data["task_graph"]))


def entries(name: str) -> dict[str, dict]:
    data = json.loads((DATA / f"{name}_live_plan.json").read_text())
    return {e["task"]: e for e in data["schedule"]["entries"]}


def edges(graph: TaskGraph, **after: list[str]) -> TaskGraph:
    """The graph with the ``after`` lists of the named tasks replaced."""
    return TaskGraph(tasks=[t.model_copy(update={"after": after[t.id]}) if t.id in after else t
                            for t in graph.tasks])


def task(id_: str, robot_type: str, quantity: float | None, unit: str | None,
         structure: str | None = "S1", zone: str = "Z1", name: str | None = None,
         after: tuple[str, ...] = ()) -> Task:
    return Task(id=id_, name=name or f"{robot_type} {id_}", robot_type=robot_type, duration_h=4,
                energy_wh=100.0, zone=zone, structure=structure, quantity=quantity, unit=unit,
                after=list(after))


def reqs(*structures: dict) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (7 * i, 10)} for i in range(1, 6)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures],
        robots=[{"id": "EX1", "type": "excavator", "battery_wh": 5000},
                {"id": "EX2", "type": "excavator", "battery_wh": 5000},
                {"id": "HL1", "type": "hauler", "battery_wh": 3000},
                {"id": "HL2", "type": "hauler", "battery_wh": 3000},
                {"id": "CP1", "type": "compactor", "battery_wh": 3500}],
        horizon_h=336)


PAD = {"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20, "depth_m": 0.4}
BERM = {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 60, "height_m": 2.5,
        "width_m": 4}
TRENCH = {"id": "S3", "type": "anchor_trench", "zone": "Z3", "length_m": 30, "width_m": 0.5,
          "depth_m": 0.5}
LANE = {"id": "S4", "type": "haul_lane", "zone": "Z4", "length_m": 40, "width_m": 5}
PILE = {"id": "S5", "type": "spoil_stockpile", "zone": "Z5"}


# ----------------------------------------------------------------------------- the live cases
def test_the_live_habitat_plan_hauls_berm_fill_before_any_dig() -> None:
    req, graph = live()
    when = entries("habitat_berm")
    digs = [t.id for t in graph.tasks if is_dig(t)]
    assert digs == ["T3", "T4", "T5", "T6"]
    assert min(when[i]["end_h"] for i in digs) == 13 and when["T7"]["start_h"] == 7
    failure = validate_graph(req, graph, coverage=True)
    assert failure is not None and failure.conflicts == ["supply:T7", "supply:T8"]
    assert failure.details == [
        ("T7 hauls 280 m3 for S2 (berm) but waits for no excavation, so it could start before "
         "any material is dug"),
        ("T8 hauls 280 m3 for S2 (berm) but waits for no excavation, so it could start before "
         "any material is dug")]
    assert failure.hint == (
        "A haul cannot start before its material is dug: make T7 and T8 wait for the digs that "
        "supply them (S1 foundation pit: T3, T4; S3 borrow strip: T5, T6)" + HINT_TAIL)
    # nothing else is wrong with the plan: CP-SAT verified it as it is
    assert validate_graph(req, graph, coverage=True, supply=False) is None


def test_the_repair_the_hint_asks_for_passes_with_each_haul_part_on_its_dig_parts() -> None:
    """T7 waits for the first part of each dig, T8 for the second: both chains stay short
    (the deadline was why the edges went), every haul has its material, and hauling the
    233 m3 the digs remove satisfies the berm's volume check, which judges a berm "built from
    the pit spoil plus the borrow strip" against that spoil."""
    req, graph = live()
    parts = {"T7": ["T2", "T3", "T5"], "T8": ["T7", "T4", "T6"]}
    fixed = TaskGraph(tasks=[
        t.model_copy(update={"after": parts[t.id], "quantity": 116.5}) if t.id in parts else t
        for t in graph.tasks])
    assert validate_graph(req, fixed, coverage=True) is None
    assert supply_advisories(req, fixed) == []
    [t8] = [s for s in supply_report(req, fixed) if s.haul.id == "T8"]
    assert [h.id for h in t8.hauls] == ["T7"] and [d.id for d in t8.digs] == ["T3", "T4", "T5", "T6"]
    assert (t8.hauled_m3, t8.dug_m3) == (233.0, 233.0)


def test_the_live_propellant_lane_plan_hauls_after_its_own_grading() -> None:
    """propellant_pit_and_berm super #2, verified in run 2 and brief-compliant: the lane is
    graded, hauled and compacted before the pit is dug ("The haul lane is finished before pit
    excavation starts") and the berm hauls follow the pit digs. The lane hauls carry the
    lane's own grading spoil (an m2 strip of the haul's own structure is a source), so the plan
    passes; every way an m3-only rule pushed it (wait for the pit digs, drop the lane hauls)
    broke the brief or coverage."""
    req, graph = live("propellant_lane")
    assert req.structures[2].operations == ["grade", "haul", "compact"]
    assert validate_graph(req, graph, coverage=True) is None
    assert supply_advisories(req, graph) == []
    report = {s.haul.id: s for s in supply_report(req, graph)}
    assert [d.id for d in report["T2"].digs] == ["T1"] and is_strip(graph.task("T1"))
    assert report["T3"].hauled_m3 == 40.0 and report["T3"].dug_m3 == pytest.approx(40.0)
    assert [d.id for d in report["T10"].digs] == ["T5", "T6"]     # the berm: the pit's spoil
    # the lane's grading does not supply the berm, which is built from pit spoil: berm hauls
    # that wait for the lane alone are the conflict (the defect of ultra #2 and #3; the berm's
    # volume is then judged against its own prism, no longer against the pit's spoil)
    early = edges(graph, T10=["T4"])
    failure = validate_graph(req, early, coverage=True)
    assert failure is not None
    assert failure.conflicts == ["volume:S2", "supply:T10", "supply:T11"]
    assert ("make T10 and T11 wait for the digs that supply them (S1 storage pit: T5, T6)"
            in failure.hint)


def test_the_live_one_surveyor_plan_hauls_before_the_strip_it_is_built_from() -> None:
    """one_surveyor_three_structures ultra #1: the brief builds the berm from the hardstand's
    strip spoil; the graph digs nothing in m3, every strip is a source, and the berm haul that
    waits for a survey only is the conflict. CP-SAT hauled from hour 9 while the strip ran
    from hour 13. Waiting for the strip passes: the berm holds more than the strip yields
    (the brief's own arithmetic), a note, and no second strip is asked for."""
    req, graph = live("one_surveyor")
    when = entries("one_surveyor")
    assert when["T6"]["start_h"] == 9 and when["T2"]["start_h"] == 13
    assert not any(is_dig(t) for t in graph.tasks)
    failure = validate_graph(req, graph, coverage=True)
    assert failure is not None and failure.conflicts == ["supply:T6"]
    assert failure.details == [
        ("T6 hauls 90 m3 for B1 (spoil berm) but waits for no excavation, so it could start "
         "before any material is dug")]
    assert failure.hint == ("A haul cannot start before its material is dug: make T6 wait for "
                            "the digs that supply it (H1 hardstand: T2; A1 apron: T10)" + HINT_TAIL)
    fixed = edges(graph, T6=["T5", "T2"])
    assert validate_graph(req, fixed, coverage=True) is None
    assert supply_advisories(req, fixed) == [
        ("supply:B1: its hauls (T6 90 m3) move 90 m3, more than the 28.8 m3 the digs before them "
         "remove (H1 hardstand: T2; an m2 strip counts 0.2 m)")]


def test_a_berm_haul_that_waits_for_its_own_key_trench_alone_is_short() -> None:
    """tight_battery ultra #3 (the demo twin's brief): the 300 m3 berm haul T7 waits for the
    berm's 15 m3 key-trench cut and both bowl halves as saved. Waiting for the trench alone
    satisfied a rule that asked for any dig, and CP-SAT hauled the berm from hour 5 while the
    bowl was dug from hour 8 to 39; it is the conflict now, and the hint names the bowl, never
    the berm's own trench first."""
    req, graph = live("tight_battery")
    assert validate_graph(req, graph, coverage=True) is None          # as verified
    trench_only = edges(graph, T7=["T6"])
    failure = validate_graph(req, trench_only, coverage=True)
    assert failure is not None and failure.conflicts == ["supply:T7"]
    assert failure.details == [
        ("T7 hauls 300 m3 for S2 (blast berm) but the digs before it remove only 15 m3 (S2 blast "
         "berm: T6), so it could start before all of its material is dug")]
    assert failure.hint == ("A haul cannot start before its material is dug: make T7 wait for "
                            "more of the digs that supply it (S1 landing pad: T2, T3)" + HINT_TAIL)
    # waiting for nothing at all: the bowl first, the berm's own trench last
    loose = validate_graph(req, edges(graph, T7=[]), coverage=True)
    assert loose is not None and loose.conflicts == ["supply:T7"]
    assert "(S1 landing pad: T2, T3; S2 blast berm: T6)" in loose.hint
    # one bowl half and the trench: still short while the other half is dug beside it
    half = validate_graph(req, edges(graph, T7=["T6", "T2"]), coverage=True)
    assert half is not None and "make T7 wait for more of the digs that supply it (S1 landing "\
        "pad: T3)" in half.hint


# ----------------------------------------------------------------------------- switches
def test_supply_judges_whole_plans_only_and_follows_the_volume_switch() -> None:
    req, graph = live()
    assert validate_graph(req, graph) is None                       # a replan's remaining graph
    assert validate_graph(req, graph, coverage=True, volume=False) is None  # the planner's replan
    forced = validate_graph(req, graph, supply=True)
    assert forced is not None and forced.conflicts == ["supply:T7", "supply:T8"]
    assert validate_graph(req, graph, volume=True) == forced        # volume=True brings it along
    assert validate_graph(req, graph, coverage=True, volume=True, supply=False) is None


def test_supply_runs_after_volume_and_before_energy() -> None:
    r = reqs(PAD, BERM)
    g = TaskGraph(tasks=[
        task("T1", "excavator", 30, "m3", name="Excavate pad bowl"),           # 24 % of S1
        task("T2", "hauler", 30, "m3", "S2", "Z2", "Haul fill to berm"),        # no dig before
        task("T3", "excavator", 400, "m3", name="Excavate pad bowl", after=("T1",)),
    ]).model_copy(deep=True)
    g.tasks[2] = g.tasks[2].model_copy(update={"energy_wh": 6400.0})            # one battery over
    failure = validate_graph(r, g, volume=True)
    assert failure is not None
    assert [c.split(":")[0] for c in failure.conflicts] == ["volume", "supply", "energy_task"]
    assert failure.conflicts[1] == "supply:T2"


def test_conflict_ids_follow_the_hauls_order_in_the_graph() -> None:
    """Run 2's small_fleet super #1 read ['supply:T12', 'supply:T3', 'supply:T6', 'supply:T9']
    and its hint 'make T12, T3, T6 and T9 wait': ids now follow the graph."""
    r = reqs(PAD, BERM)
    hauls = [task(f"T{i}", "hauler", 20, "m3", "S2", "Z2") for i in (3, 6, 9, 12)]
    g = TaskGraph(tasks=[task("T1", "excavator", 126, "m3", name="Excavate bowl"), *hauls])
    failure = validate_graph(r, g, supply=True)
    assert failure is not None
    assert failure.conflicts == ["supply:T3", "supply:T6", "supply:T9", "supply:T12"]
    assert "make T3, T6, T9 and T12 wait" in failure.hint


# ----------------------------------------------------------------------------- what is a source
def test_a_graph_without_any_source_hauls_material_from_outside_the_plan() -> None:
    r = reqs(PAD, BERM)
    compact = task("T1", "compactor", 314, "m2", name="Compact pad")
    hauls_only = TaskGraph(tasks=[compact, task("T2", "hauler", 90, "m3", "S2", "Z2")])
    assert supply_report(r, hauls_only) == [] and supply_advisories(r, hauls_only) == []
    assert validate_graph(r, hauls_only, supply=True) is None
    # spoil moved by an excavator is no source either: bladed, pushed, backfilled
    blade = task("T3", "excavator", 126, "m3", name="Blade pad spoil into the north windrow")
    assert not is_dig(blade) and not is_strip(blade)
    assert supply_report(r, TaskGraph(tasks=[blade, *hauls_only.tasks])) == []


def test_a_strip_feeds_its_own_structure_a_pile_or_a_graph_without_digs() -> None:
    """An m2 strip or grade yields a surface layer, at most 0.2 m: a source for a haul into
    the strip's own structure (a lane's grading moved along it), into a pile (a stockpile-like
    structure, or a zone no structure claims: "Haul strip spoil to stockpile") and for any
    haul of a graph that digs nothing in m3; never for a designed body another structure's
    spoil builds while digs in m3 exist."""
    r = reqs(PAD, BERM, LANE, PILE)
    strip = task("T1", "excavator", 314, "m2", name="Strip pad fines")
    grade = task("T2", "excavator", 200, "m2", "S4", "Z4", "Grade haul lane")
    bowl = task("T3", "excavator", 126, "m3", name="Excavate pad bowl", after=("T1",))
    assert is_strip(strip) and supply_m3(strip) == pytest.approx(314 * STRIP_DEPTH_M)
    assert is_dig(bowl) and supply_m3(bowl) == 126 and not is_strip(bowl)
    hauls = [task("H1", "hauler", 60, "m3", "S2", "Z2", "Haul strip spoil to berm", ("T1",)),
             task("H2", "hauler", 40, "m3", "S4", "Z4", "Haul lane spoil", ("T2",)),
             task("H3", "hauler", 60, "m3", "S5", "Z5", "Haul strip spoil to pile", ("T1",)),
             task("H4", "hauler", 60, "m3", None, "Z9", "Haul strip spoil to dump", ("T1",))]
    g = TaskGraph(tasks=[strip, grade, bowl, *hauls])
    report = {s.haul.id: s for s in supply_report(r, g)}
    assert report["H1"].waits_for_no_dig and report["H1"].target == "S2 (blast berm)"
    assert [d.id for d in report["H2"].digs] == ["T2"]                 # its own structure
    assert [d.id for d in report["H3"].digs] == ["T1"] and report["H3"].pile
    assert report["H4"].target == "zone Z9" and report["H4"].pile and report["H4"].digs
    failure = validate_graph(r, g, supply=True)
    assert failure is not None and failure.conflicts == ["supply:H1"]
    assert failure.hint.startswith("A haul cannot start before its material is dug: make H1 "
                                   "wait for the digs that supply it (S1 landing pad: T3)")
    # without a dig in m3 the strip supplies the berm too (one_surveyor's hardstand)
    flat = TaskGraph(tasks=[strip, grade, *hauls])
    assert validate_graph(r, flat, supply=True) is None
    assert [d.id for d in {s.haul.id: s for s in supply_report(r, flat)}["H1"].digs] == ["T1"]


def test_a_rehandle_is_no_dig_but_a_chain_through_one_is_supplied() -> None:
    r = reqs(PAD, BERM)
    blade = task("T2", "excavator", 126, "m3", name="Blade pad spoil into the north windrow",
                 after=("T1",))
    haul = task("T3", "hauler", 126, "m3", "S2", "Z2", "Haul windrow to berm", after=("T2",))
    dig = task("T1", "excavator", 126, "m3", name="Excavate pad bowl")
    assert is_dig(dig) and not is_dig(blade) and is_haul(haul)
    # dig -> blade -> haul: the haul waits for the dig through the rehandle
    assert validate_graph(r, TaskGraph(tasks=[dig, blade, haul]), supply=True) is None
    # a haul that waits for the rehandle alone, while the dig runs in parallel, has no dig
    loose = TaskGraph(tasks=[dig, blade.model_copy(update={"after": []}), haul])
    failure = validate_graph(r, loose, supply=True)
    assert failure is not None and failure.conflicts == ["supply:T3"]
    # a graph whose only m3 excavator work is a rehandle digs nothing: not judged
    only = TaskGraph(tasks=[blade.model_copy(update={"after": []}), haul])
    assert validate_graph(r, only, supply=True) is None and supply_report(r, only) == []


def test_a_trench_backfill_fed_by_its_own_dig_is_supplied() -> None:
    """solar_array_pad's anchor trench "backfilled and compacted": the spoil of the trench's
    own dig hauled back in is supplied, balanced and named with its structure."""
    r = reqs(TRENCH)
    g = TaskGraph(tasks=[
        task("T1", "excavator", 7.5, "m3", "S3", "Z3", "Excavate anchor trench"),
        task("T2", "hauler", 7.5, "m3", "S3", "Z3", "Haul spoil back as backfill", after=("T1",)),
        task("T3", "excavator", 7.5, "m3", "S3", "Z3", "Backfill anchor trench", after=("T2",))])
    assert validate_graph(r, g, coverage=True) is None and supply_advisories(r, g) == []
    [s] = supply_report(r, g)
    assert s.target == "S3 (anchor trench)" and [d.id for d in s.digs] == ["T1"]
    assert s.dug_by == (("S3 anchor trench", ("T1",)),)


def test_the_landing_pad_fixture_hauls_strip_spoil_to_its_stockpile_zone() -> None:
    """The Oct 22 demo graph (fixtures/landing_pad_v1/taskgraph_1.json) with the quantities the
    pipeline tests give it: "Haul strip spoil to stockpile" (T6, zone Z3, which no structure
    claims) follows the strip T2, and "Stage sinter feedstock on the apron" (T8, zone Z4)
    follows T6; both are hauls to a pile, so the strip supplies them. Phase 3g first rejected
    the shape and its tests added an edge from the bowl, which yields no strip spoil."""
    req = Requirements.model_validate_json(
        (FIXTURES / "landing_pad_v1" / "structure.json").read_text())
    data = json.loads((FIXTURES / "landing_pad_v1" / "taskgraph_1.json").read_text())
    scope = {"T2": (300, "m2"), "T3": (190, "m3"), "T4": (98, "m3"), "T5": (150, "m2"),
             "T6": (120, "m3"), "T7": (188, "m3"), "T8": (70, "m3"), "T9": (314, "m2"),
             "T10": (350, "m2"), "T11": (314, "m2"), "T12": (80, "m2")}
    for t in data["tasks"]:
        if t["id"] in scope:
            t["quantity"], t["unit"] = scope[t["id"]]
    graph, _ = apply_rates(TaskGraph.model_validate(data))
    assert validate_graph(req, graph, coverage=True, volume=True) is None
    report = {s.haul.id: s for s in supply_report(req, graph)}
    assert (report["T6"].target, report["T6"].pile) == ("zone Z3", True)
    assert [d.id for d in report["T6"].digs] == ["T2"] and [d.id for d in report["T8"].digs] == ["T2"]
    assert [d.id for d in report["T7"].digs] == ["T3", "T4"]          # the berm: bowl and trench


def test_a_haul_part_may_wait_for_the_dig_part_that_feeds_it_only() -> None:
    r = reqs(PAD, BERM)
    g = TaskGraph(tasks=[
        task("T3", "excavator", 62.85, "m3", name="Excavate pad bowl north half"),
        task("T4", "excavator", 62.85, "m3", name="Excavate pad bowl south half"),
        task("T5", "hauler", 62.85, "m3", "S2", "Z2", after=("T3",)),
        task("T6", "hauler", 62.85, "m3", "S2", "Z2", after=("T4",))])
    assert validate_graph(r, g, volume=True) is None     # the berm is judged against S1's spoil
    assert supply_advisories(r, g) == []
    # chained hauls into one body count together: the second part after the first has both digs
    chained = TaskGraph(tasks=[*g.tasks[:3], g.tasks[3].model_copy(update={"after": ["T4", "T5"]})])
    assert supply_advisories(r, chained) == []
    [_, t6] = supply_report(r, chained)
    assert (t6.hauled_m3, t6.dug_m3) == (pytest.approx(125.7), pytest.approx(125.7))
    # a part that moves more than the dig part it waits for, the other part dug beside it
    heavy = TaskGraph(tasks=[*g.tasks[:2], g.tasks[2].model_copy(update={"quantity": 100.0}),
                             g.tasks[3]])
    failure = validate_graph(r, heavy, supply=True)
    assert failure is not None and failure.conflicts == ["supply:T5"]
    assert failure.details == [
        ("T5 hauls 100 m3 for S2 (blast berm) but the digs before it remove only 62.9 m3 (S1 "
         "landing pad: T3), so it could start before all of its material is dug")]
    assert "make T5 wait for more of the digs that supply it (S1 landing pad: T4)" in failure.hint


# ----------------------------------------------------------------------------- balance
def test_the_balance_note_sums_a_bodys_hauls_however_they_are_ordered() -> None:
    """landing_pad's eval brief: a berm "built from pad spoil" whose prism (300 m3) holds more
    than the 125.7 m3 the bowl yields, with every dig before its hauls: the plan stands (the
    brief's own arithmetic) and the note says so once per body, the same for chained and
    parallel parts."""
    r = reqs(PAD, BERM)
    digs = [task("T3", "excavator", 62.85, "m3", name="Excavate pad bowl north half"),
            task("T4", "excavator", 62.85, "m3", name="Excavate pad bowl south half")]
    note = ("supply:S2: its hauls (T5 150 m3, T6 150 m3) move 300 m3, more than the 125.7 m3 the "
            "digs before them remove (S1 landing pad: T3, T4)")
    chain = TaskGraph(tasks=[*digs,
                             task("T5", "hauler", 150, "m3", "S2", "Z2", after=("T3", "T4")),
                             task("T6", "hauler", 150, "m3", "S2", "Z2", after=("T5",))])
    parallel = TaskGraph(tasks=[*chain.tasks[:3],
                                chain.tasks[3].model_copy(update={"after": ["T3", "T4"]})])
    assert supply_advisories(r, chain) == supply_advisories(r, parallel) == [note]
    assert validate_graph(r, chain, volume=True) is None        # a note, never a conflict
    assert validate_graph(r, parallel, volume=True) is None
    # the threshold: more than 125 % of the m3 the sources make available, plus 1 m3
    edge = 125.7 * SUPPLY_SLACK + SUPPLY_SLACK_M3
    one = task("T5", "hauler", 0, "m3", "S2", "Z2", after=("T3", "T4"))
    at = TaskGraph(tasks=[*digs, one.model_copy(update={"quantity": round(edge - 0.01, 2)})])
    over = TaskGraph(tasks=[*digs, one.model_copy(update={"quantity": round(edge + 0.01, 2)})])
    assert supply_advisories(r, at) == [] and len(supply_advisories(r, over)) == 1


def test_the_balance_note_never_counts_a_rehandle_twice_nor_judges_a_pile() -> None:
    """dig 125.7 m3 -> haul it to a stockpile -> haul the stockpile to the berm: the same
    material, summed per target, balances; the pile's own fill is never judged (whatever is
    hauled there), and a body with a haul in conflict gets the verdict, not a note."""
    r = reqs(PAD, BERM, PILE)
    dig = task("T1", "excavator", 125.7, "m3", name="Excavate pad bowl")
    to_pile = task("T2", "hauler", 125.7, "m3", "S5", "Z5", "Haul bowl spoil to stockpile", ("T1",))
    to_berm = task("T3", "hauler", 125.7, "m3", "S2", "Z2", "Haul stockpile to berm", ("T2",))
    g = TaskGraph(tasks=[dig, to_pile, to_berm])
    assert validate_graph(r, g, supply=True) is None and supply_advisories(r, g) == []
    piled = TaskGraph(tasks=[dig, to_pile.model_copy(update={"quantity": 500.0}), to_berm])
    assert validate_graph(r, piled, supply=True) is None and supply_advisories(r, piled) == []
    loose = TaskGraph(tasks=[dig, to_pile, to_berm.model_copy(update={"after": []})])
    assert validate_graph(r, loose, supply=True).conflicts == ["supply:T3"]
    assert supply_advisories(r, loose) == []


def test_the_hint_groups_the_feeding_digs_and_caps_the_list() -> None:
    structures = [{"id": f"S{i}", "type": f"borrow_pit_{c}", "zone": "Z1"}
                  for i, c in enumerate("abcde", start=1)]
    r = reqs(*structures, BERM | {"id": "S9"}, LANE | {"id": "S8"})
    digs = [task(f"D{i}{k}", "excavator", 10, "m3", f"S{i}") for i in range(1, 6) for k in range(6)]
    late = task("L1", "excavator", 10, "m3", "S1", after=("H1",))   # waits for the haul
    haul = task("H1", "hauler", 50, "m3", "S9", "Z2", "Haul fill to berm")
    failure = validate_graph(r, TaskGraph(tasks=[*digs, late, haul]), supply=True)
    assert failure is not None and failure.conflicts == ["supply:H1"]
    assert failure.hint == (
        "A haul cannot start before its material is dug: make H1 wait for the digs that supply "
        "it (S1 borrow pit a: D10, D11, D12, D13 and 2 more; S2 borrow pit b: D20, D21, D22, D23 "
        "and 2 more; S3 borrow pit c: D30, D31, D32, D33 and 2 more; S4 borrow pit d: D40, D41, "
        "D42, D43 and 2 more; digs of 1 more structure)" + HINT_TAIL)
    assert "L1" not in failure.hint                 # a dig that waits for the haul cannot feed it
    # a placed body's own dig is its key trench: named last, never first
    own = TaskGraph(tasks=[task("D1", "excavator", 10, "m3", "S1"),
                           task("K1", "excavator", 10, "m3", "S9", "Z2", "Cut key trench"),
                           haul])
    assert "(S1 borrow pit a: D1; S9 blast berm: K1)" in validate_graph(r, own, supply=True).hint
    # any other target's own dig comes first (a lane's cut feeds its own haul)
    lane = TaskGraph(tasks=[task("D1", "excavator", 10, "m3", "S1"),
                            task("C1", "excavator", 10, "m3", "S8", "Z4", "Cut lane"),
                            task("H2", "hauler", 10, "m3", "S8", "Z4", "Haul lane spoil")])
    assert "(S8 haul lane: C1; S1 borrow pit a: D1)" in validate_graph(r, lane, supply=True).hint
    # every dig waits for the haul: say so instead of naming none
    only_late = TaskGraph(tasks=[late, haul])
    assert ("make H1 wait for the digs that supply it (every dig waits for it now: put the "
            "digs first)") in validate_graph(r, only_late, supply=True).hint


def test_a_haul_is_placed_in_its_zone_and_sources_are_named_by_tag_then_zone() -> None:
    """The coverage contract: a haul's zone is where its material is PLACED, so its target is
    the structure of that zone (the one its tag names when several share it), whatever else
    its tag says; a zone no structure claims is a pile."""
    r = reqs(PAD, BERM)
    g = TaskGraph(tasks=[task("T1", "excavator", 126, "m3", None, "Z1", "Excavate bowl"),
                         task("T2", "hauler", 90, "m3", None, "Z2"),
                         task("T3", "hauler", 30, "m3", "S2", "Z4"),
                         task("T4", "hauler", 30, "m3", "S1", "Z2", "Haul pad spoil to berm")])
    report = {s.haul.id: s for s in supply_report(r, g)}
    assert report["T2"].target == "S2 (blast berm)" and report["T4"].target == "S2 (blast berm)"
    assert (report["T3"].target, report["T3"].pile) == ("zone Z4", True)
    assert report["T2"].feeders == (("S1 landing pad", ("T1",)),)
    unclaimed = TaskGraph(tasks=[task("T1", "excavator", 126, "m3", "pit-9", "Z4"),
                                 task("T2", "hauler", 90, "m3", "S2", "Z2")])
    assert supply_report(r, unclaimed)[0].feeders == (("pit-9", ("T1",)),)
    assert ancestors(g) == {"T1": set(), "T2": set(), "T3": set(), "T4": set()}


def test_the_horizon_hint_never_asks_to_drop_a_dig_to_haul_edge() -> None:
    """The live repair that answered a horizon core deleted the berm hauls' edges to their
    digs; the horizon_path hint says which edges may go."""
    r = reqs(PAD, BERM).model_copy(update={"horizon_h": 6})
    g = TaskGraph(tasks=[task("T1", "excavator", 126, "m3", name="Excavate bowl"),
                         task("T2", "hauler", 126, "m3", "S2", "Z2", after=("T1",))])
    failure = validate_graph(r, g)
    assert failure is not None and failure.conflicts[0].startswith("horizon_path:")
    assert ("drop unnecessary 'after' edges so more work runs in parallel (never a dig-to-haul "
            "edge: split the hauls per dig part instead)") in failure.hint


# ----------------------------------------------------------------------------- repair path
class Scripted:
    """Answers the first task-graph request with ``first`` and every repair with ``repair``,
    recording the prompts."""

    name = "scripted"

    def __init__(self, first: TaskGraph, repair: TaskGraph):
        self.answers = {"taskgraph": first, "repair": repair}
        self.users: list[tuple[str, str]] = []

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        self.users.append((role, user))
        return schema.model_validate(self.answers[role].model_dump()), Usage(model="scripted")


def test_the_advisory_rides_the_step_payload_of_whole_plans_only() -> None:
    r = reqs(PAD, BERM)
    digs = [task("T3", "excavator", 62.85, "m3", name="Excavate pad bowl north half"),
            task("T4", "excavator", 62.85, "m3", name="Excavate pad bowl south half")]
    over = TaskGraph(tasks=[*digs, task("T5", "hauler", 300, "m3", "S2", "Z2", after=("T3", "T4"))])
    note = ("supply:S2: its hauls (T5 300 m3) move 300 m3, more than the 125.7 m3 the digs "
            "before them remove (S1 landing pad: T3, T4)")
    _, step = generate_task_graph(Scripted(over, over), r, key="k")
    assert note in step.payload["advisories"]
    # a replan's remaining graph (prior without feedback) is never judged: its digs may be done
    _, replan = generate_task_graph(Scripted(over, over), r, key="k", prior=over)
    assert not any(a.startswith("supply:") for a in replan.payload["advisories"])
    # nor a repair round of a replan (the remaining graph as baseline)
    failure = Failure(kind="validation", conflicts=["horizon"])
    _, round_ = generate_task_graph(Scripted(over, over), r, key="k", prior=over,
                                    feedback=failure, baseline=over, attempt=2)
    assert not any(a.startswith("supply:") for a in round_.payload["advisories"])


def test_a_balance_note_reaches_the_repair_with_how_to_read_it() -> None:
    note = ("supply:S2: its hauls (T5 150 m3, T6 150 m3) move 300 m3, more than the 125.7 m3 the "
            "digs before them remove (S1 landing pad: T3, T4)")
    assert _repeats_verdict(note, {"supply:S2", "supply:T6"}) is False
    assert _repeats_verdict("volume:S2: ... total only 50 m3 (39 %)", {"volume:S2"}) is True
    planner = Planner(Scripted(TaskGraph(tasks=[task("T1", "excavator", 1, "m3")]),
                               TaskGraph(tasks=[task("T1", "excavator", 1, "m3")])))
    verdict = Failure(kind="validation", conflicts=["supply:T6", "horizon"])
    assert planner._repair_notes([note, "estimate:T3: model said ..."], verdict) == [
        note, "estimate:T3: model said ..."]
    _, user = repair_messages(reqs(PAD, BERM), TaskGraph(tasks=[task("T1", "excavator", 1, "m3")]),
                              verdict, advisories=[note])
    assert user.endswith(ADVISORIES_HEADER + "\n- " + note)
    # the header keeps the body's m3: the note is the brief's arithmetic, never a reason to
    # cut a berm or invent a dig (COVERAGE_RULES sizes a receiving body by its own volume)
    for part in ("a supply note", "keeps the body's m3", "never invent a dig or cut the body",
                 "only wait for more of the digs that feed it"):
        assert part in ADVISORIES_HEADER, part


def test_the_planner_repairs_a_haul_that_waits_for_no_dig() -> None:
    """The live failure replayed end to end: the first graph hauls before any dig, the
    validator rejects it with supply:<haul> (CP-SAT would have verified it), the repair prompt
    carries the verdict's hint, and the answer that waits for the digs is scheduled with the
    haul after them."""
    req, graph = live()
    parts = {"T7": ["T2", "T3", "T5"], "T8": ["T7", "T4", "T6"]}
    fixed = TaskGraph(tasks=[
        t.model_copy(update={"after": parts[t.id], "quantity": 116.5}) if t.id in parts else t
        for t in graph.tasks])
    client = Scripted(graph, fixed)
    result = Planner(client, explain=False, suggest_budget_s=0).plan_from_requirements(req, key="k")
    assert result.success and result.repairs == 1
    verdict = next(s for s in result.steps if s.stage == "validate" and not s.ok)
    assert verdict.failure.conflicts == ["supply:T7", "supply:T8"]
    repair_user = next(u for role, u in client.users if role == "repair")
    assert "supply:T7" in repair_user and "S3 borrow strip: T5, T6" in repair_user
    entries_ = {e.task: e for e in result.schedule.entries}
    assert entries_["T7"].start_h >= max(entries_[d].end_h for d in ("T3", "T5"))
    assert entries_["T8"].start_h >= max(entries_[d].end_h for d in ("T4", "T6"))
