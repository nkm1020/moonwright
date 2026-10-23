"""Brief-declared operations (phase 3c).

``Structure.operations`` is the set of phases the BRIEF explicitly asks for on a structure
(closed vocabulary: survey, excavate, grade, haul, compact, sinter). The validator requires a
task of the right robot type for each one the fleet can staff, on top of the structure-type
heuristic; the ``scope:<structure>`` hint names the operation, quotes the brief where it can
and says which robot task to add; an operation no robot of the fleet can perform is never a
conflict (a site warning until phase 3j, since then dropped from the requirements by the
structure stage with a note); the prompts state the rules the validator checks; and the three
hand-made fixtures declare operations their briefs ask for and their recorded graphs serve.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from moonwright.llm.base import FIXTURES_DIR
from moonwright.llm.prompts import (
    COVERAGE_RULES,
    REPAIR_SYSTEM,
    STRUCTURE_SYSTEM,
    TASKGRAPH_SYSTEM,
    repair_messages,
    structure_messages,
    taskgraph_messages,
)
from moonwright.pipeline.structure import operation_warnings, site_warnings
from moonwright.pipeline.validate import (
    brief_evidence,
    missing_operations,
    missing_phases,
    performs,
    required_phases,
    structure_tasks,
    task_phases,
    unstaffed_operations,
    unsupported_operations,
    validate_graph,
)
from moonwright.schemas import Failure, Requirements, Robot, Site, Structure, Task, TaskGraph, Zone

BRIEFS_DIR = Path(__file__).resolve().parents[1] / "examples" / "briefs"
V1_BRIEF = Path(__file__).resolve().parent / "data" / "landing_pad_v1.md"
OPERATIONS = ["survey", "excavate", "grade", "haul", "compact", "sinter"]
# what the three hand-made fixtures' briefs ask for per structure (and their graphs serve)
FIXTURE_OPERATIONS = {
    "landing_pad_v1": {"S1": ["survey", "excavate", "compact", "sinter"],
                       "S2": ["haul", "compact", "sinter", "survey"]},
    "access_road": {"S1": ["grade", "compact", "survey"],
                    "S2": ["excavate", "compact", "survey"],
                    "S3": ["haul"]},
    "tight_battery": {"S1": ["excavate", "compact"],
                      "S2": ["excavate", "haul", "compact"],
                      "S3": ["grade"]},
}
RULES_TAIL = (", so add the missing work back as tasks tagged with the structure id, with "
              "quantity and unit.")


# ----------------------------------------------------------------------------- builders
def robot(id_: str, type_: str = "excavator", battery: float = 5000.0) -> Robot:
    return Robot(id=id_, type=type_, battery_wh=battery)


def task(id_: str, *, type_: str = "excavator", name: str | None = None, zone: str = "Z1",
         structure: str | None = None, unit: str | None = None, quantity: float | None = None,
         after: tuple[str, ...] = ()) -> Task:
    return Task(id=id_, name=name or id_, robot_type=type_, duration_h=4, energy_wh=100.0,
                zone=zone, after=list(after), structure=structure, unit=unit,
                quantity=quantity)


def req(*robots: Robot, structures: tuple[Structure, ...] = (),
        brief: str | None = None) -> Requirements:
    """Zones Z1..Z5 with a depot (so the layout itself warns about nothing), the structures
    in the order given, the fleet, and ``brief`` as the source brief."""
    return Requirements(
        site=Site(zones=[Zone(id=f"Z{i}", center=(6 * i, 10)) for i in range(1, 6)],
                  depot=(20, 0)),
        structures=list(structures), robots=list(robots), horizon_h=336, source_brief=brief,
    )


def graph(*tasks: Task) -> TaskGraph:
    """The tasks with their ``after`` edges to tasks not in the graph dropped: the berm's haul
    waits for the pad's dig (T2) when the pad is planned too, as the supply check wants."""
    present = {t.id for t in tasks}
    return TaskGraph(tasks=[t.model_copy(update={"after": [a for a in t.after if a in present]})
                            for t in tasks])


def must_fail(r: Requirements, g: TaskGraph, **kwargs: object) -> Failure:
    failure = validate_graph(r, g, coverage=True, **kwargs)  # type: ignore[arg-type]
    assert failure is not None, "expected a validation failure"
    assert failure.kind == "validation"
    assert len(failure.details) == len(failure.conflicts)
    return failure


FLEET = (robot("EX1"), robot("HL1", "hauler", 3000), robot("CP1", "compactor", 3500),
         robot("PR1", "printer", 4000), robot("SV1", "surveyor", 1500))
NO_PRINTER = FLEET[:3] + FLEET[4:]


def pad(*operations: str) -> Structure:
    return Structure(id="S1", type="landing_pad", zone="Z1", operations=list(operations))


def berm(*operations: str) -> Structure:
    return Structure(id="S2", type="blast_berm", zone="Z2", operations=list(operations))


def pad_tasks(*skip: str) -> list[Task]:
    """The four phases of the pad S1 (zone Z1) minus the phases in ``skip``."""
    all_ = {
        "survey": task("T1", type_="surveyor", name="Survey and stake pad", structure="S1"),
        "excavate": task("T2", name="Excavate pad bowl", unit="m3", quantity=126,
                         structure="S1"),
        "compact": task("T3", type_="compactor", name="Compact pad subgrade", unit="m2",
                        quantity=314, structure="S1"),
        "sinter": task("T4", type_="printer", name="Sinter pad surface", unit="m2",
                       quantity=314, structure="S1"),
    }
    return [t for phase, t in all_.items() if phase not in skip]


def berm_tasks(*skip: str) -> list[Task]:
    """Haul, compact, sinter and inspect the berm S2 (zone Z2) minus the phases in ``skip``."""
    all_ = {
        "haul": task("B1", type_="hauler", name="Haul pad spoil to berm line", unit="m3",
                     quantity=90, zone="Z2", structure="S2", after=("T2",)),
        "compact": task("B2", type_="compactor", name="Shape and compact berm", unit="m2",
                        quantity=240, zone="Z2", structure="S2"),
        "sinter": task("B3", type_="printer", name="Sinter berm crest", unit="m2",
                       quantity=80, zone="Z2", structure="S2"),
        "survey": task("B4", type_="surveyor", name="Inspect berm profile", zone="Z2",
                       structure="S2"),
    }
    return [t for phase, t in all_.items() if phase not in skip]


def drop(g: TaskGraph, *ids: str) -> TaskGraph:
    kept = [t for t in g.tasks if t.id not in ids]
    return graph(*(t.model_copy(update={"after": [a for a in t.after if a not in ids]})
                   for t in kept))


def fixture(brief_id: str, filename: str = "taskgraph_1.json") -> tuple[Requirements, TaskGraph]:
    r = Requirements.model_validate_json((FIXTURES_DIR / brief_id / "structure.json").read_text())
    brief = V1_BRIEF if brief_id == "landing_pad_v1" else BRIEFS_DIR / f"{brief_id}.md"
    r.source_brief = brief.read_text()
    return r, TaskGraph.model_validate_json((FIXTURES_DIR / brief_id / filename).read_text())


# ----------------------------------------------------------------------------- contract
def test_operations_are_a_closed_vocabulary_deduplicated_in_order() -> None:
    props = Requirements.model_json_schema()["$defs"]["Structure"]["properties"]["operations"]
    assert props["type"] == "array" and props["items"] == {"enum": OPERATIONS, "type": "string"}
    for text in ("closed vocabulary", "sintered crest", "built from pad spoil", "key trench",
                 "inspected", "Never add operations the brief does not ask for"):
        assert text in props["description"]
    assert "operations" not in Requirements.model_json_schema()["$defs"]["Structure"]["required"]
    # the structure prompt carries the enum, so the model sees the vocabulary and its rule
    system, _ = structure_messages("Build a pad.")
    assert '"operations"' in system and '"enum"' in system
    assert all(f'"{op}"' in system for op in OPERATIONS)

    assert Structure(id="S1", type="x").operations == []
    s = Structure(id="S1", type="x", operations=["sinter", "haul", "sinter", "compact", "haul"])
    assert s.operations == ["sinter", "haul", "compact"]
    with pytest.raises(ValidationError):
        Structure(id="S1", type="x", operations=["inspect"])
    with pytest.raises(ValidationError):
        Structure(id="S1", type="x", operations="haul")
    # dimensions stay the numeric extras; operations are not one of them
    assert Structure(id="S1", type="x", operations=["haul"], length_m=20).dims() == {"length_m": 20.0}
    parsed = Requirements.model_validate_json(
        '{"robots": [{"id": "EX1", "type": "excavator", "battery_wh": 5000}], "horizon_h": 10,'
        ' "structures": [{"id": "S1", "type": "landing_pad", "diameter_m": 20,'
        ' "operations": ["excavate", "excavate", "compact"]}]}')
    assert parsed.structures[0].operations == ["excavate", "compact"]
    assert parsed.structures[0].dims() == {"diameter_m": 20.0}


# ----------------------------------------------------------------------------- validator
def test_declared_operation_without_a_task_is_a_scope_conflict_quoting_the_brief() -> None:
    brief = (BRIEFS_DIR / "landing_pad.md").read_text()
    r = req(*FLEET, structures=(pad(), berm("haul", "compact", "sinter")), brief=brief)
    assert validate_graph(r, graph(*pad_tasks(), *berm_tasks()), coverage=True) is None
    failure = must_fail(r, graph(*pad_tasks(), *berm_tasks("sinter", "survey")))
    assert failure.conflicts == ["scope:S2"]
    assert failure.details == ["S2 (blast_berm) lacks sinter task (its operations list sinter)"]
    assert ('S2 (blast_berm) lists sinter (the brief asks for "with a sintered crest to resist '
            'plume erosion") but no task sinters it; add a printer task tagged S2') in failure.hint
    assert failure.hint.endswith(" and every operation a structure lists needs a task tagged "
                                 "with it" + RULES_TAIL)
    # a printer task tagged with another structure does not sinter the berm
    stray = task("B3", type_="printer", name="Sinter berm crest", unit="m2", quantity=80,
                 zone="Z2", structure="S1")
    assert must_fail(r, graph(*pad_tasks(), *berm_tasks("sinter", "survey"), stray)) \
        .conflicts == ["scope:S2"]
    # without the brief the hint still names the rule, the operation and the robot to add
    r.source_brief = None
    failure = must_fail(r, graph(*pad_tasks(), *berm_tasks("sinter", "survey")))
    assert ("S2 (blast_berm) lists sinter (an operation the brief asks for) but no task "
            "sinters it; add a printer task tagged S2") in failure.hint
    # several listed operations at once: each quoted where the brief has words for it
    r = req(*FLEET, structures=(pad(), berm("haul", "compact", "sinter", "survey")), brief=brief)
    failure = must_fail(r, graph(*pad_tasks(), *berm_tasks("haul", "sinter", "survey")))
    assert failure.details == [("S2 (blast_berm) lacks haul, sinter and survey tasks (its "
                                "operations list haul, sinter and survey)")]
    assert ('S2 (blast_berm) lists haul (the brief asks for "built from pad spoil") but no task '
            'hauls to it, sinter (the brief asks for "with a sintered crest to resist plume '
            'erosion") but no task sinters it and survey (an operation the brief asks for) but '
            "no task surveys or inspects it; add hauler, printer and surveyor tasks tagged S2"
            ) in failure.hint


def test_declared_operation_the_fleet_cannot_staff_is_never_a_conflict() -> None:
    """Never required, and since phase 3j dropped from the requirements by the structure stage
    with a note (``tests/test_unstaffed_operations.py``); a list edited by hand that names it
    again is still judged without it."""
    r = req(*NO_PRINTER, structures=(pad(), berm("haul", "compact", "sinter")))
    g = graph(*pad_tasks("sinter"), *berm_tasks("sinter", "survey"))
    assert validate_graph(r, g, coverage=True) is None
    assert required_phases(r, r.structures[1]) == [("compact",), ("haul",)]
    assert missing_phases(r, r.structures[1], berm_tasks("sinter", "survey")) == []
    assert missing_operations(r, r.structures[1], []) == ["haul", "compact"]  # sinter skipped
    assert unstaffed_operations(r, r.structures[1]) == [("sinter", "printer")]
    assert site_warnings(r) == []
    assert operation_warnings(r) == [
        ("structure S2: operations sinter dropped: the fleet has no printer, so no sinter task "
         "will be required of it (add a printer and the operation again to plan it)"),
    ]
    # one note per structure, structures in order; nothing with the printer
    two = req(*NO_PRINTER, structures=(pad("sinter"), berm("sinter", "survey")))
    assert [w.split(":")[0] for w in operation_warnings(two)] == ["structure S1", "structure S2"]
    assert operation_warnings(req(*FLEET, structures=(pad("sinter"), berm("sinter")))) == []
    # the moment a printer joins the fleet the same berm tasks are short of the sinter one
    with_printer = req(*FLEET, structures=(pad(), berm("haul", "compact", "sinter")))
    assert must_fail(with_printer, graph(*pad_tasks(), *berm_tasks("sinter", "survey"))) \
        .conflicts == ["scope:S2"]


def test_declared_haul_needs_a_hauler_task_tagged_with_the_receiving_structure() -> None:
    stockpile = Structure(id="S2", type="spoil_stockpile", zone="Z2", operations=["haul"])
    r = req(*FLEET, structures=(pad(), stockpile))
    to_pile = task("H1", type_="hauler", name="Haul pad spoil to the stockpile", unit="m3",
                   quantity=120, zone="Z2", structure="S2", after=("T2",))
    assert validate_graph(r, graph(*pad_tasks(), to_pile), coverage=True) is None
    # hauled "to depot" and tagged with the source structure: the stockpile gets nothing
    to_depot = task("H1", type_="hauler", name="Haul pad spoil to depot", unit="m3",
                    quantity=120, zone="Z1", structure="S1", after=("T2",))
    failure = must_fail(r, graph(*pad_tasks(), to_depot))
    assert failure.conflicts == ["scope:S2"]
    assert failure.details == ["S2 (spoil_stockpile) lacks haul task (its operations list haul)"]
    assert ("S2 (spoil_stockpile) lists haul (an operation the brief asks for) but no task "
            "hauls to it; add a hauler task tagged S2") in failure.hint
    # a listed operation is strict about the robot type: an excavator "hauling" is not a haul,
    # and a hauler whose name mentions the bowl it empties is not the excavation of a trench
    by_excavator = task("H1", name="Haul spoil to stockpile", unit="m3", quantity=120,
                        zone="Z2", structure="S2")
    assert must_fail(r, graph(*pad_tasks(), by_excavator)).conflicts == ["scope:S2"]
    assert performs(to_pile, "haul") and not performs(by_excavator, "haul")
    bowl_haul = task("H2", type_="hauler", name="Haul bowl spoil to berm line", unit="m3",
                     quantity=90, zone="Z2", structure="S2")
    assert "excavate" in task_phases(bowl_haul) and not performs(bowl_haul, "excavate")
    assert performs(task("X", name="Cut berm key trench", unit="m3", quantity=30), "excavate")
    assert performs(task("X", name="Strip apron", unit="m2", quantity=240), "grade")
    assert not performs(task("X", name="Strip apron", unit="m2", quantity=240), "excavate")
    # an unknown kind without operations keeps the old "at least one task" rule; with
    # operations the missing ones are named instead
    bare = req(*FLEET, structures=(pad(), Structure(id="S2", type="spoil_stockpile", zone="Z2")))
    assert must_fail(bare, graph(*pad_tasks())).details == ["S2 (spoil_stockpile) has no task"]
    asked = req(*FLEET, structures=(pad(), Structure(id="S2", type="spoil_stockpile", zone="Z2",
                                                     operations=["haul", "compact"])))
    failure = must_fail(asked, graph(*pad_tasks()))
    assert failure.details == [("S2 (spoil_stockpile) lacks haul and compact tasks (its "
                                "operations list haul and compact)")]
    assert ("S2 (spoil_stockpile) lists haul (an operation the brief asks for) but no task "
            "hauls to it and compact (an operation the brief asks for) but no task compacts "
            "it; add hauler and compactor tasks tagged S2") in failure.hint


def test_kind_heuristic_and_listed_operations_combine_without_repeats() -> None:
    # every pad phase listed as well: the same four requirements, each reported once
    r = req(*FLEET, structures=(pad("survey", "excavate", "compact", "sinter"),))
    assert validate_graph(r, graph(*pad_tasks()), coverage=True) is None
    # the listed excavate subsumes the pad's "excavate or grade" and is required on its own
    assert required_phases(r, r.structures[0]) == [("survey",), ("compact",), ("sinter",),
                                                   ("excavate",)]
    failure = must_fail(r, graph(*pad_tasks("sinter")))
    assert failure.conflicts == ["scope:S1"]
    assert failure.details == ["S1 (landing_pad) lacks sinter task (its operations list sinter)"]
    assert must_fail(r, graph(*pad_tasks("compact", "sinter"))).details == [
        "S1 (landing_pad) lacks compact and sinter tasks (its operations list compact and sinter)"]
    # a listed haul subsumes the berm's "excavate or haul": a trench alone no longer satisfies it
    r = req(*FLEET, structures=(berm("haul"),))
    assert required_phases(r, r.structures[0]) == [("compact",), ("haul",)]
    trench = task("B0", name="Cut berm key trench", unit="m3", quantity=30, zone="Z2",
                  structure="S2")
    assert validate_graph(req(*FLEET, structures=(berm(),)), graph(trench, *berm_tasks("haul")),
                          coverage=True) is None
    failure = must_fail(r, graph(trench, *berm_tasks("haul")))
    assert failure.details == ["S2 (blast_berm) lacks haul task (its operations list haul)"]
    # a kind phase and a listed operation missing together: both named, kind first
    r = req(*FLEET, structures=(berm("sinter"),))
    failure = must_fail(r, graph(*berm_tasks("compact", "sinter", "survey")))
    assert failure.details == [("S2 (blast_berm) lacks compact and sinter tasks (its operations "
                                "list sinter)")]
    assert ("S2 (blast_berm) has no compact task and lists sinter (an operation the brief asks "
            "for) but no task sinters it; add a printer task tagged S2") in failure.hint
    # the kind heuristic stays lenient (an excavator named for compaction counts) while the
    # listed operation wants the compactor itself
    blade = task("B2", name="Compact berm with blade", unit="m2", quantity=240, zone="Z2",
                 structure="S2")
    lenient = graph(*berm_tasks("compact", "sinter", "survey"), blade)
    assert validate_graph(req(*FLEET, structures=(berm(),)), lenient, coverage=True) is None
    failure = must_fail(req(*FLEET, structures=(berm("compact"),)), lenient)
    assert failure.details == ["S2 (blast_berm) lacks compact task (its operations list compact)"]
    # the hint's phrasing picks the article for the robot type
    assert "add an excavator task tagged S2" in must_fail(
        req(*FLEET, structures=(berm("excavate"),)), graph(*berm_tasks("sinter", "survey"))).hint


def test_requirements_without_operations_are_judged_exactly_as_before() -> None:
    r = req(*FLEET, structures=(pad(), berm()))
    failure = must_fail(r, graph(*pad_tasks("sinter"), *berm_tasks("compact")))
    assert failure.conflicts == ["scope:S1", "scope:S2"]
    assert failure.details == ["S1 (landing_pad) lacks sinter task",
                               "S2 (blast_berm) lacks compact task"]
    assert failure.hint.startswith("Structure coverage is incomplete: S1 (landing_pad) has no "
                                   "sinter task and S2 (blast_berm) has no compact task; every "
                                   "structure keeps its phases (")
    assert failure.hint.endswith(")" + RULES_TAIL)
    assert "operation" not in failure.hint and " lists " not in failure.hint
    assert site_warnings(r) == []


# ----------------------------------------------------------------------------- brief quotes
TWO_STRUCTURES = """# Two structures

**Structures.**

1. Landing pad: circular, 20 m diameter. Excavate a bowl 0.4 m deep, compact the subgrade, then sinter a 30 cm regolith surface.
2. Blast berm: a 60 m arc, 2.5 m high, built from pad spoil, compacted, with a sintered crest to resist plume erosion.

- The surveyor must inspect both structures before the pad is signed off.
"""


def test_brief_evidence_quotes_the_structures_own_clause_or_nothing() -> None:
    assert brief_evidence(TWO_STRUCTURES, "sinter", berm()) == \
        "with a sintered crest to resist plume erosion"
    assert brief_evidence(TWO_STRUCTURES, "sinter", pad()) == "then sinter a 30 cm regolith surface"
    assert brief_evidence(TWO_STRUCTURES, "haul", berm()) == "built from pad spoil"
    assert brief_evidence(TWO_STRUCTURES, "compact", berm()) == "compacted"
    assert brief_evidence(TWO_STRUCTURES, "excavate", pad()) == "Excavate a bowl 0.4 m deep"
    assert brief_evidence(TWO_STRUCTURES, "survey", pad()) == \
        "The surveyor must inspect both structures before the pad is signed off"
    # the inspection line names the pad only: no quote for the berm rather than a wrong one
    assert brief_evidence(TWO_STRUCTURES, "survey", berm()) is None
    assert brief_evidence(TWO_STRUCTURES, "haul", Structure(id="S3", type="stockpile")) is None
    assert brief_evidence(None, "haul", berm()) is None and brief_evidence("", "haul", berm()) is None
    # the structure's own entry beats a site paragraph that mentions it in passing
    passing = ("**Site.** Same footprint as the landing-pad job; the berm needs a key trench "
               "cut into firm material.\n1. Landing pad: 20 m diameter bowl excavated 0.6 m to "
               "firm regolith across the full footprint, with a compacted subgrade.\n")
    assert brief_evidence(passing, "excavate", pad()) == \
        "20 m diameter bowl excavated 0.6 m to firm regolith across the full footprint"
    long = "1. Blast berm: " + "a very long clause that keeps going " * 4 + "with a sintered crest.\n"
    quote = brief_evidence(long, "sinter", berm())
    assert quote is not None and len(quote) == 80 and quote.endswith("...")


# ----------------------------------------------------------------------------- fixtures
@pytest.mark.parametrize("brief_id", sorted(FIXTURE_OPERATIONS))
def test_fixtures_declare_what_their_briefs_ask_and_their_graphs_serve(brief_id: str) -> None:
    r, _ = fixture(brief_id)
    assert {s.id: s.operations for s in r.structures} == FIXTURE_OPERATIONS[brief_id]
    assert site_warnings(r) == []
    for path in sorted((FIXTURES_DIR / brief_id).glob("taskgraph_*.json")):
        g = TaskGraph.model_validate_json(path.read_text())
        assert validate_graph(r, g, coverage=True) is None, path
        for s in r.structures:
            assert missing_operations(r, s, structure_tasks(s, g, r.structures)) == [], (path, s.id)


@pytest.mark.parametrize(("brief_id", "structure_id", "phase", "quote"), [
    ("landing_pad", "BERM1", "sinter", "with a sintered crest to resist plume erosion"),
    ("landing_pad", "BERM1", "haul", "built from pad spoil"),
    ("landing_pad", "PAD1", "sinter", "then sinter a 30 cm regolith surface"),
    ("landing_pad", "PAD1", "excavate", "Excavate a bowl 0.4 m deep to firm regolith"),
    ("landing_pad", "BERM1", "survey", None),
    ("tight_battery", "S2", "excavate", "on a 0.5 m deep key trench"),
    ("tight_battery", "S1", "excavate",
     "20 m diameter bowl excavated 0.6 m to firm regolith across the full footprint"),
    ("tight_battery", "S3", "grade",
     "strip a 30 m by 8 m lane on the south side so the hauler can reach the pad"),
    ("access_road", "S3", "haul", "built from foundation spoil"),
    ("access_road", "S2", "survey",
     "The surveyor inspects the road before the foundation is signed off"),
])
def test_brief_evidence_on_the_demo_briefs(brief_id: str, structure_id: str, phase: str,
                                           quote: str | None) -> None:
    r, _ = fixture(brief_id)
    structure = next(s for s in r.structures if s.id == structure_id)
    assert brief_evidence(r.source_brief, phase, structure) == quote


def test_dropping_a_brief_asked_task_from_a_demo_graph_is_caught_and_explained() -> None:
    """The live omissions of phase 3a, replayed on the fixtures: no berm-crest sintering, no
    key trench, no inspection. Each is a scope conflict whose hint quotes the brief."""
    r, g = fixture("landing_pad_v1")
    failure = must_fail(r, drop(g, "T12"))
    assert failure.conflicts == ["scope:S2"]
    assert failure.details == ["S2 (blast_berm) lacks sinter task (its operations list sinter)"]
    assert ('S2 (blast_berm) lists sinter (the brief asks for "with a sintered crest to resist '
            'plume erosion") but no task sinters it; add a printer task tagged S2') in failure.hint

    r, g = fixture("tight_battery", "taskgraph_2.json")
    failure = must_fail(r, drop(g, "T4"))   # the key trench; the hauler's "bowl spoil" is no dig
    assert failure.conflicts == ["scope:S2"]
    assert ('S2 (blast_berm) lists excavate (the brief asks for "on a 0.5 m deep key trench") '
            "but no task excavates it; add an excavator task tagged S2") in failure.hint

    r, g = fixture("access_road")
    failure = must_fail(r, drop(g, "T6"))   # the berm haul; compaction alone is not the berm
    assert failure.conflicts == ["scope:S3"]
    assert ('S3 (protective_berm) lists haul (the brief asks for "built from foundation spoil") '
            "but no task hauls to it; add a hauler task tagged S3") in failure.hint
    failure = must_fail(r, drop(g, "T11", "T12"))   # the inspections: the staking survey still
    assert failure.conflicts == ["scope:S2"]        # serves the road, the foundation has none
    assert ('S2 (habitat_foundation) lists survey (the brief asks for "The surveyor inspects the '
            'road before the foundation is signed off") but no task surveys or inspects it; add '
            "a surveyor task tagged S2") in failure.hint


# ----------------------------------------------------------------------------- prompts
def test_prompts_state_the_operations_and_coverage_rules() -> None:
    structure = " ".join(STRUCTURE_SYSTEM.split())
    for text in ("operations", "closed", "sintered crest", "built from pad spoil", "key trench",
                 "signed off", "Never add an operation the brief does not ask for",
                 "never invented", "copy every number the brief",
                 "trench_width_m and trench_depth_m", '"trench_width_m": 0.7',
                 "a stockpile described only by its position has none",
                 'a "Spoil berm" is spoil_berm (never stockpile)'):
        assert text in structure, text
    assert '"stockpile", not "spool"' not in structure
    assert COVERAGE_RULES in TASKGRAPH_SYSTEM and COVERAGE_RULES in REPAIR_SYSTEM
    flat = " ".join(COVERAGE_RULES.split())
    for text in (("every operation listed in a structure's `operations` needs a task tagged "
                  "with that structure"), "scope:<structure>", "zone where the material is PLACED",
                 "its structure is the receiving one", "built from structure A's spoil",
                 "quantity follows from B's volume, not from A's", "inspection or sign-off",
                 "one surveyor inspection task per structure as the last task of that structure",
                 "landing pad = survey, excavate or grade",
                 "over at least as many tasks as there are robots of that type",
                 "more than 60 m3", "more than 300 m2",
                 # the split rule says the parts sum to the total (live: a berm hauled twice)
                 "The parts SUM to the structure's total, never repeat it",
                 '2 x 150 m3, each part\'s basis stating its share ("300 m3 / 2 = 150 m3")'):
        assert text in flat, text
    assert "equal shares" not in flat
    assert "a listed operation or a whole structure went missing" in REPAIR_SYSTEM
    assert "the shares summing to the original quantity" in " ".join(REPAIR_SYSTEM.split())
    # every conflict id the validator can emit is named to the repair model
    for cid in ("energy:<type>", "energy_task:<task>", "horizon_path:<chain>", "quantity:<task>",
                "scope:<structure>", "locked:<task>"):
        assert cid in REPAIR_SYSTEM, cid
    # the unit rule prices what the rate table prices: no trench in m, no marker in ea for a
    # physical robot type
    tg = " ".join(TASKGRAPH_SYSTEM.split())
    assert "a trench is length x width x depth in m3, never m" in tg
    assert "m and ea (a cable run, a marker) only for surveyor tasks" in tg
    assert "m for linear work" not in tg and "ea for discrete items" not in tg
    assert "6 to 24 tasks (more only when the split rule below needs them)" in tg
    for system in (STRUCTURE_SYSTEM, TASKGRAPH_SYSTEM, REPAIR_SYSTEM):
        assert len(system) < 5500   # Ultra reads these with thinking on
    # the requirements the model plans from carry the listed operations
    r = req(*FLEET, structures=(berm("haul", "sinter"),))
    _, user = taskgraph_messages(r)
    assert '"operations": [\n    "haul",\n    "sinter"\n   ]' in user
    _, user = repair_messages(r, graph(*berm_tasks()),
                              Failure(kind="validation", conflicts=["scope:S2"]))
    assert '"operations": [\n    "haul",\n    "sinter"\n   ]' in user


# ----------------------------------------------------------------------------- review fixes
SMALL_FLEET = (BRIEFS_DIR.parent / "eval" / "small_fleet_long_berm.md").read_text()
SMALL_FLEET_BERM = Structure(id="STR1", type="protective berm", zone="Z1",
                             operations=["excavate", "haul", "compact", "survey"])
SMALL_FLEET_STRIP = Structure(id="STR2", type="borrow strip", zone="Z2", operations=["excavate"])


def small_fleet_req(brief: str | None = SMALL_FLEET) -> Requirements:
    return req(robot("EX1"), robot("HL1", "hauler", 3000), robot("CP1", "compactor", 3500),
               robot("SV1", "surveyor", 1500), structures=(SMALL_FLEET_BERM, SMALL_FLEET_STRIP),
               brief=brief)


def test_brief_evidence_takes_verbs_never_a_structures_noun_a_robot_or_another_entry() -> None:
    """Live: the hint for an over-declared ``excavate`` on the berm quoted "built from a borrow
    strip along its north side" as the ask, and the repair invented a 35 m3 "foundation" dig
    to satisfy it. 'borrow' names the strip, 'excavator' names a robot and the strip's own
    entry ("... excavation 0.6 m deep ... that supplies the berm fill") is the strip's: none
    of them is the brief asking to excavate the berm."""
    others = (SMALL_FLEET_BERM, SMALL_FLEET_STRIP)
    assert brief_evidence(SMALL_FLEET, "excavate", SMALL_FLEET_BERM, others) is None
    assert brief_evidence(SMALL_FLEET, "haul", SMALL_FLEET_BERM, others) == \
        "about 350 cubic metres of fill"
    assert brief_evidence(SMALL_FLEET, "compact", SMALL_FLEET_BERM, others) == \
        "placed and compacted in two lifts and inspected"
    assert brief_evidence(SMALL_FLEET, "survey", SMALL_FLEET_BERM, others) == \
        "placed and compacted in two lifts and inspected"
    assert brief_evidence(SMALL_FLEET, "excavate", SMALL_FLEET_STRIP, others) == \
        "100 m by 6 m excavation 0.6 m deep"
    # the strip's entry mentions the berm ("supplies the berm fill"): only the other
    # structures tell brief_evidence that the line is the strip's, not the berm's
    assert brief_evidence(SMALL_FLEET, "excavate", SMALL_FLEET_BERM) == \
        "100 m by 6 m excavation 0.6 m deep"


def test_an_alternative_the_brief_does_not_support_is_soft_and_warned_about() -> None:
    r = small_fleet_req()
    assert unsupported_operations(r, r.structures[0]) == [("excavate", "haul")]
    assert unsupported_operations(r, r.structures[1]) == []
    assert required_phases(r, r.structures[0]) == [("compact",), ("haul",), ("survey",)]
    assert site_warnings(r) == [
        ("structure STR1 lists excavate but the brief line about it asks for haul, not excavate: "
         "no excavate task will be required of it (drop the operation on the requirements card "
         "if the brief does not want it)")]
    # the physically right graph: dig the strip, haul to the berm, compact and inspect it
    g = graph(task("T1", name="Excavate STR2 borrow strip", unit="m3", quantity=360, zone="Z2",
                   structure="STR2"),
              task("T2", type_="hauler", name="Haul fill to STR1 berm", unit="m3", quantity=350,
                   zone="Z1", structure="STR1", after=("T1",)),
              task("T3", type_="compactor", name="Compact STR1 berm lifts", unit="m2", quantity=700,
                   zone="Z1", structure="STR1", after=("T2",)),
              task("T4", type_="surveyor", name="Inspect STR1 berm", zone="Z1", structure="STR1",
                   after=("T3",)))
    assert validate_graph(r, g, coverage=True) is None
    # without the brief nothing is softened: the listed excavate is required as before
    blind = small_fleet_req(brief=None)
    assert unsupported_operations(blind, blind.structures[0]) == [] and site_warnings(blind) == []
    failure = must_fail(blind, g)
    assert failure.details == [
        "STR1 (protective berm) lacks excavate task (its operations list excavate)"]
    assert ("STR1 (protective berm) lists excavate (an operation the brief asks for) but no task "
            "excavates it; add an excavator task tagged STR1") in failure.hint
    # an alternative the brief supports stays strict: the demo berm's key trench
    r2, g2 = fixture("tight_battery", "taskgraph_2.json")
    assert unsupported_operations(r2, r2.structures[1]) == []
    assert must_fail(r2, drop(g2, "T4")).conflicts == ["scope:S2"]
    # an unknown kind keeps every listed operation: its list is the only contract it has
    pile = Structure(id="S3", type="spoil_stockpile", zone="Z3", operations=["haul", "compact"])
    assert unsupported_operations(req(*FLEET, structures=(pile,), brief=SMALL_FLEET), pile) == []


def test_a_haul_placed_in_the_receiving_zone_serves_it_whatever_its_tag() -> None:
    """The review's complete graph: the pad spoil hauled to the berm line but tagged with the
    pad it came from. It builds the berm, so the listed haul is met and the hint no longer
    asks to ADD a second haul (the berm hauled twice)."""
    brief = (BRIEFS_DIR / "landing_pad.md").read_text()
    r = req(*FLEET, structures=(pad(), berm("haul", "compact", "sinter")), brief=brief)
    source_tagged = task("B1", type_="hauler", name="Haul pad spoil to berm line", unit="m3",
                         quantity=90, zone="Z2", structure="S1", after=("T2",))
    rest = berm_tasks("haul", "survey")
    assert validate_graph(r, graph(*pad_tasks(), source_tagged, *rest), coverage=True) is None
    assert missing_operations(r, r.structures[1], rest, hauls=[source_tagged]) == []
    assert missing_phases(r, r.structures[1], rest, hauls=[source_tagged]) == []
    assert missing_operations(r, r.structures[1], rest) == ["haul"]
    # in the pad's zone it is the pad's spoil leaving, not the berm being built
    stays = source_tagged.model_copy(update={"zone": "Z1"})
    failure = must_fail(r, graph(*pad_tasks(), stays, *rest))
    assert failure.conflicts == ["scope:S2"]
    assert "add a hauler task tagged S2" in failure.hint
