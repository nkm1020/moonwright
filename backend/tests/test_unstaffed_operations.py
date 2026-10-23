"""Tests for the operations the fleet cannot perform (phase 3j, ``moonwright.pipeline.structure``:
:func:`correct_operations`): an operation a structure lists whose robot type the fleet lacks is
dropped from the requirements at the structure stage, with a note that keeps what the old
warning said. Seen live in the habitat berm demo (``tests/data/habitat_berm_demo_live.json``,
tries 2 and 3): Nemotron 3.5 Lightning listed ``sinter`` on the berm, which the brief never asks
for and no robot of the printer-less fleet can do; the structure step warned that "no sinter
task will be required of it" but the requirements kept the operation, the task-graph model
(which never sees the brief) planned a printer task for it, and the validator answered
``capability:T14`` twice. The structures below are the ones Lightning recorded, read against
the eval briefs and their own fleets."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from moonwright.llm.base import Usage
from moonwright.llm.prompts import taskgraph_messages
from moonwright.pipeline.structure import (
    correct_operations,
    operation_warnings,
    site_warnings,
    structure_brief,
)
from moonwright.pipeline.validate import (
    missing_operations,
    required_phases,
    unstaffed_operations,
    validate_graph,
)
from moonwright.schemas import Requirements, Structure, Task, TaskGraph

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "examples" / "eval"
DATA = Path(__file__).resolve().parent / "data"
RECORDED = DATA / "recorded_structures.json"
SCRIPT = ROOT / "scripts" / "brief_budget.py"
NOTE = ("operations sinter dropped: the fleet has no printer, so no sinter task will be required "
        "of it (add a printer and the operation again to plan it)")


def demo(n: int) -> tuple[Requirements, dict]:
    """Try ``n``'s requirements as Lightning structured them (its brief attached) and the rest
    of its record."""
    data = json.loads((DATA / "habitat_berm_demo_live.json").read_text())[f"try{n}"]
    r = Requirements.model_validate(data["requirements"])
    r.source_brief = (EVAL / "habitat_berm_tight_horizon.md").read_text()
    return r, data


def fleet(*types: str) -> list[dict]:
    return [{"id": f"R{i}", "type": t, "battery_wh": 5000} for i, t in enumerate(types, 1)]


def reqs(*structures: dict, robots: list[dict], source: str | None = None) -> Requirements:
    return Requirements(
        site={"zones": [{"id": f"Z{i}", "center": (8 * i, 10)} for i in range(1, 5)],
              "depot": (20, 0)},
        structures=[Structure.model_validate(s) for s in structures], robots=robots,
        horizon_h=336, source_brief=source)


@pytest.fixture(scope="module")
def bb() -> ModuleType:
    """``scripts/brief_budget.py``, whose ``parse_brief`` reads a brief's fleet."""
    spec = importlib.util.spec_from_file_location("brief_budget", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["brief_budget"] = module   # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------- the live demo
@pytest.mark.parametrize("n", [1, 2, 3])
def test_the_live_berms_sinter_is_dropped_and_the_planner_never_sees_it(n: int) -> None:
    r, data = demo(n)
    berm = next(s for s in r.structures if s.type == "berm")
    assert "sinter" in berm.operations                       # as Lightning recorded it
    assert data["structure_warnings"] == [                   # what the structure step said
        (f"structure {berm.id} lists sinter but the fleet has no printer: no sinter task will "
         "be required of it (add a printer or drop the operation)")]
    assert correct_operations(r) == [f"structure {berm.id}: {NOTE}"]
    fixed = next(s for s in r.structures if s.id == berm.id)
    assert fixed.operations == [op for op in berm.operations if op != "sinter"]
    assert (fixed.type, fixed.zone, fixed.dims()) == (berm.type, berm.zone, berm.dims())
    # the note keeps what the warning said, and site_warnings no longer repeats it
    assert site_warnings(r) == [] and operation_warnings(r) == []
    # the task-graph model plans from the requirements alone: no sinter left to plan
    _, user = taskgraph_messages(r)
    assert "sinter" not in user
    # applied once
    assert correct_operations(r) == []


@pytest.mark.parametrize("n", [2, 3])
def test_the_live_first_graphs_planned_a_printer_for_it(n: int) -> None:
    """The evidence: the first verdict of tries 2 and 3 was the printer task Ultra planned for
    the listed sinter."""
    _, data = demo(n)
    verdict = data["first_verdict"]
    assert "capability:T14" in verdict["conflicts"]
    assert "T14 needs robot type 'printer' but no robot has it" in verdict["details"]


class DemoClient:
    """Answers the structure request with try 2's requirements as Lightning recorded them."""

    name = "demo"

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        r, _ = demo(2)
        return schema.model_validate(r.model_dump(exclude={"source_brief"})), Usage(model="demo")


def test_the_structure_step_writes_the_drop_and_says_so() -> None:
    req, step = structure_brief(DemoClient(), (EVAL / "habitat_berm_tight_horizon.md").read_text(),
                                key="k")
    assert req is not None and step.ok
    assert [s.operations for s in req.structures] == [
        ["survey", "excavate", "compact"], ["haul", "compact", "survey"], ["excavate", "haul"]]
    warnings = step.payload["warnings"]
    assert f"structure S2: {NOTE}" in warnings
    assert not any("lists sinter but the fleet has no" in w for w in warnings)
    assert correct_operations(req) == []


# ----------------------------------------------------------------------------- the rule
def test_every_operation_without_a_robot_type_is_dropped_by_its_type() -> None:
    """The fleet rule of the coverage check, per operation: sinter needs a printer, compact a
    compactor, excavate and grade an excavator, haul a hauler, survey a surveyor. One note per
    structure, the operations and robot types in the order listed."""
    every = ["survey", "excavate", "grade", "haul", "compact", "sinter"]
    pad = {"id": "S1", "type": "landing_pad", "zone": "Z1", "operations": every, "diameter_m": 20}
    cases = [
        (("excavator", "hauler", "compactor", "surveyor"), ["sinter"],
         ("operations sinter dropped: the fleet has no printer, so no sinter task will be "
          "required of it (add a printer and the operation again to plan it)")),
        (("excavator", "hauler", "printer", "surveyor"), ["compact"],
         ("operations compact dropped: the fleet has no compactor, so no compact task will be "
          "required of it (add a compactor and the operation again to plan it)")),
        (("hauler", "compactor", "printer", "surveyor"), ["excavate", "grade"],
         ("operations excavate and grade dropped: the fleet has no excavator, so no excavate "
          "or grade task will be required of it (add an excavator and the operations again to "
          "plan them)")),
        (("excavator", "compactor", "printer", "surveyor"), ["haul"],
         ("operations haul dropped: the fleet has no hauler, so no haul task will be required "
          "of it (add a hauler and the operation again to plan it)")),
        (("excavator", "hauler", "compactor"), ["survey", "sinter"],
         ("operations survey and sinter dropped: the fleet has no surveyor and no printer, so "
          "no survey or sinter task will be required of it (add a surveyor and a printer and "
          "the operations again to plan them)")),
    ]
    for types, dropped, note in cases:
        r = reqs(pad, robots=fleet(*types))
        assert [op for op, _ in unstaffed_operations(r, r.structures[0])] == dropped
        assert operation_warnings(r) == [f"structure S1: {note}"]
        assert correct_operations(r) == [f"structure S1: {note}"]
        assert r.structures[0].operations == [op for op in every if op not in dropped]
        assert r.structures[0].dims() == {"diameter_m": 20}
    # a fleet that staffs every operation changes nothing, nor does a structure listing none
    full = reqs(pad, {"id": "S2", "type": "stockpile", "zone": "Z2"},
                robots=fleet("excavator", "hauler", "compactor", "printer", "surveyor"))
    before = full.model_dump()
    assert correct_operations(full) == [] and full.model_dump() == before


def test_the_coverage_check_reads_the_corrected_list_and_still_skips_a_hand_listed_one() -> None:
    """After the drop the coverage check and the requirements card read the list the planner
    plans from. Requirements edited by hand may list the operation again without a printer:
    the coverage check still never asks for it (the card shows its chip "not required")."""
    berm = {"id": "S2", "type": "blast_berm", "zone": "Z2", "length_m": 40, "height_m": 1.5,
            "width_m": 4, "operations": ["haul", "compact", "sinter"]}
    r = reqs(berm, robots=fleet("excavator", "hauler", "compactor"))
    graph = TaskGraph(tasks=[
        Task(id="T1", name="Haul fill to berm", robot_type="hauler", duration_h=4,
             energy_wh=100.0, zone="Z2", structure="S2"),
        Task(id="T2", name="Compact berm", robot_type="compactor", duration_h=4,
             energy_wh=100.0, zone="Z2", structure="S2", after=["T1"])])
    assert validate_graph(r, graph, coverage=True) is None                  # listed, skipped
    assert required_phases(r, r.structures[0]) == [("compact",), ("haul",)]
    correct_operations(r)
    assert r.structures[0].operations == ["haul", "compact"]
    assert validate_graph(r, graph, coverage=True) is None
    assert missing_operations(r, r.structures[0], list(graph.tasks)) == []


# ----------------------------------------------------------------------------- the eval briefs
# every (brief, operation) Lightning listed in runs 2 and 3 that the brief's own fleet cannot
# perform: the sintered surfaces of four printer-less briefs and of the habitat berm, and a
# spoil berm's haul on instrument_pad, whose fleet has no hauler
DROPPED = {("four_mast_pads", "sinter"), ("habitat_berm_tight_horizon", "sinter"),
           ("instrument_pad", "haul"), ("shielding_mound", "sinter"),
           ("solar_array_pad", "sinter"), ("tight_battery", "sinter")}


def test_only_the_operations_a_briefs_fleet_cannot_perform_are_dropped(bb: ModuleType) -> None:
    """Every structure Lightning recorded for the 22 eval briefs in runs 2 and 3, with its
    brief's fleet (``brief_budget.parse_brief``): an operation is dropped exactly when that
    fleet has no robot of its type, every other operation keeps its place, and nothing else of
    the structure changes."""
    recorded = json.loads(RECORDED.read_text())
    seen: set[tuple[str, str]] = set()
    for name, structures in recorded.items():
        if name.startswith("_"):
            continue
        brief = (EVAL / f"{name}.md").read_text()
        robots, _ = bb.parse_brief(brief)
        types = {r["type"] for r in robots}
        for data in structures:
            r = reqs(data, robots=robots, source=brief)
            s = r.structures[0]
            lacking = [op for op in s.operations
                       if {"survey": "surveyor", "excavate": "excavator", "grade": "excavator",
                           "haul": "hauler", "compact": "compactor",
                           "sinter": "printer"}[op] not in types]
            notes = correct_operations(r)
            assert bool(notes) == bool(lacking), (name, data)
            fixed = r.structures[0]
            assert fixed.operations == [op for op in s.operations if op not in lacking]
            assert fixed.model_dump(exclude={"operations"}) == s.model_dump(exclude={"operations"})
            seen |= {(name, op) for op in lacking}
    assert seen == DROPPED
