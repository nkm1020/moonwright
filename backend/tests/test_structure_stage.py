"""The structure stage's defences against a small model's malformed answers (phase 3c):
scrambled ``operations`` keys are salvaged and reported, a structure without dimensions is
flagged, and the recorded fixtures are untouched by both. The scrambled shapes below are the
ones Nemotron 3.5 Lightning produced live under strict json_schema decoding on Oct 22 2026."""

from __future__ import annotations

from pathlib import Path

from moonwright.llm.base import Usage
from moonwright.pipeline.structure import (
    dimension_warnings,
    recover_operations,
    structure_brief,
)
from moonwright.schemas import Requirements, Structure

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BRIEFS = ("landing_pad", "access_road", "tight_battery")


def base() -> Requirements:
    return Requirements.model_validate_json((FIXTURES / "landing_pad" / "structure.json").read_text())


def with_structures(*structures: dict) -> Requirements:
    return base().model_copy(update={"structures": [Requirements.model_validate(
        {**base().model_dump(), "structures": [s]}).structures[0] for s in structures]})


LANDING_PAD_S1 = {"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20,
                  "operations': [": "excavate", "grade": None, "compact": True, "sinter": True}
TIGHT_S1 = {"id": "S1", "type": "landing pad", "zone": "Z1", "diameter_m": 20,
            "operations excavate grade compact": [],
            "operations excavate": ["excavate", "grade", "compact"],
            "operations grade": [], "operations compact": []}
TIGHT_S2 = {"id": "S2", "type": "blast berm", "zone": "Z2", "operations excavate": ["excavate"],
            "operations haul": ["haul"], "operations compact": ["compact"]}
TIGHT_S3 = {"id": "S3", "type": "approach apron", "zone": "Z3", "length_m": 30, "width_m": 8,
            "operations grade": ["grade"]}


def test_scrambled_operations_keys_are_read_into_the_list_and_dropped() -> None:
    r = with_structures(LANDING_PAD_S1, TIGHT_S1, TIGHT_S2, TIGHT_S3)
    assert [s.operations for s in r.structures] == [[], [], [], []]   # the contract was disarmed
    notes = recover_operations(r)
    assert [s.operations for s in r.structures] == [
        ["excavate", "compact", "sinter"], ["excavate", "grade", "compact"],
        ["excavate", "haul", "compact"], ["grade"]]
    assert [s.dims() for s in r.structures] == [{"diameter_m": 20.0}, {"diameter_m": 20.0}, {},
                                                {"length_m": 30.0, "width_m": 8.0}]
    assert [s.model_extra for s in r.structures] == [{"diameter_m": 20}, {"diameter_m": 20}, {},
                                                     {"length_m": 30, "width_m": 8}]
    assert notes[0] == ("structure S1: operations [excavate, compact, sinter] recovered from "
                        "malformed keys (\"operations': [\", 'grade', 'compact', 'sinter'); "
                        "check them against the brief")
    assert len(notes) == 4 and all(n.startswith("structure S") for n in notes)
    # the salvage survives serialisation: the planner and the UI see a clean structure
    again = Requirements.model_validate_json(r.model_dump_json())
    assert again.structures[0].operations == ["excavate", "compact", "sinter"]
    assert "operations': [" not in again.model_dump_json()


def test_declared_operations_keep_their_place_and_other_keys_survive() -> None:
    r = with_structures({"id": "S2", "type": "blast_berm", "zone": "Z2", "operations": ["haul"],
                         "operations sinter": [], "length_m": 60, "material": "regolith",
                         "compact": 2, "operational_notes": "two lifts"})
    assert recover_operations(r) == [("structure S2: operations [haul, sinter] recovered from "
                                      "malformed keys ('operations sinter'); check them "
                                      "against the brief")]
    s = r.structures[0]
    assert s.operations == ["haul", "sinter"]
    # a numeric "compact" is left alone (it is not a flag), prose keys are not operations keys
    assert s.model_extra == {"length_m": 60, "material": "regolith", "compact": 2,
                             "operational_notes": "two lifts"}


def test_a_false_or_null_flag_is_dropped_without_listing_the_operation() -> None:
    r = with_structures({"id": "S1", "type": "landing_pad", "zone": "Z1", "diameter_m": 20,
                         "sinter": False, "survey": None, "compact": "yes"})
    recover_operations(r)
    assert r.structures[0].operations == ["compact"]
    assert r.structures[0].model_extra == {"diameter_m": 20}


def test_clean_fixtures_are_untouched_and_fully_dimensioned() -> None:
    for brief in BRIEFS:
        r = Requirements.model_validate_json((FIXTURES / brief / "structure.json").read_text())
        before = r.model_dump()
        assert recover_operations(r) == [] and dimension_warnings(r) == []
        assert r.model_dump() == before


def test_a_structure_without_dimensions_is_flagged() -> None:
    r = with_structures({"id": "S2", "type": "blast berm", "zone": "Z2", "operations": ["haul"]},
                        {"id": "S3", "type": "stockpile", "zone": "Z3", "length_m": 10})
    assert dimension_warnings(r) == [
        ("structure S2 (blast berm) has no dimensions: the planner sizes its tasks from numeric "
         "fields such as diameter_m, length_m, width_m, depth_m or height_m and never sees the "
         "brief, so add the brief's numbers before planning")]


class ScrambledClient:
    """Answers the structure request with a scrambled live-shaped structure."""

    name = "scrambled"

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        answer = {**base().model_dump(), "structures": [LANDING_PAD_S1, TIGHT_S2]}
        return schema.model_validate(answer), Usage(model="scrambled")


def test_structure_step_reports_the_salvage_and_the_missing_dimensions() -> None:
    req, step = structure_brief(ScrambledClient(), "Build a pad and a berm.", key="k")
    assert step.ok and req is not None
    assert [s.operations for s in req.structures] == [["excavate", "compact", "sinter"],
                                                      ["excavate", "haul", "compact"]]
    warnings = step.payload["warnings"]
    assert warnings[0].startswith("structure S1: operations [excavate, compact, sinter] recovered")
    assert warnings[1].startswith("structure S2: operations [excavate, haul, compact] recovered")
    assert warnings[-1].startswith("structure S2 (blast berm) has no dimensions")
    assert "sinter" not in " ".join(w for w in warnings if "fleet has no" in w)  # PR1 exists


def test_a_boolean_extra_is_a_flag_not_a_dimension() -> None:
    """``"inspected": true`` (a scrambled-answer shape) is an int to Python, never 1 m: it
    neither counts as a dimension nor silences the no-dimensions warning."""
    assert Structure(id="S2", type="blast_berm", inspected=True, length_m=20).dims() == {"length_m": 20.0}
    assert Structure(id="S1", type="pad", depth_m=True, diameter_m=20).dims() == {"diameter_m": 20.0}
    flagged = with_structures({"id": "S2", "type": "blast_berm", "zone": "Z2", "inspected": True})
    assert flagged.structures[0].dims() == {}
    assert recover_operations(flagged) == []
    assert [w.split(":")[0] for w in dimension_warnings(flagged)] == [
        "structure S2 (blast_berm) has no dimensions"]
