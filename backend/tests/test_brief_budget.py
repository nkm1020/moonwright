"""Tests for ``scripts/brief_budget.py``: the geometry behind the demo structures, the verdict
thresholds, the brief registry (every brief under ``examples/eval`` parses into the spec the
script needs and lands in the kind ``docs/eval-briefs.md`` promises), the fixture inputs, and
the mechanism the two video candidates rely on, proved by the real validator and CP-SAT."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest

from moonwright.pipeline.rates import RATED_TYPES, scope_gaps, volume_advisories, volume_shares
from moonwright.pipeline.validate import supply_report, validate_graph
from moonwright.schemas import Requirements

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "scripts" / "brief_budget.py"
EVAL_DIR = BACKEND / "examples" / "eval"
DEMO_DIR = BACKEND / "examples" / "briefs"
FIXTURES = BACKEND / "fixtures"

# The kind every eval brief is designed for (docs/eval-briefs.md). A brief whose numbers drift
# out of its kind, or a new brief without a registry entry, fails here.
EXPECTED_KIND: dict[str, str] = {
    "access_road": "comfortable",
    "cable_trench": "comfortable",
    "crater_ramp": "comfortable",
    "four_mast_pads": "comfortable",
    "habitat_foundation": "comfortable",
    "instrument_pad": "comfortable",
    "isru_stockpile": "comfortable",
    "landing_pad": "comfortable",
    "one_surveyor_three_structures": "comfortable",
    "propellant_pit_and_berm": "comfortable",
    "shielding_mound": "comfortable",
    "tight_battery": "comfortable",          # B by its recorded fixture, A by the rate table
    "two_pads_one_printer": "comfortable",
    "depleted_excavators": "careful_split",   # B, video candidate
    "large_fleet_pad_berm_road": "careful_split",
    "road_and_pad": "careful_split",
    "habitat_berm_tight_horizon": "horizon_bound",
    "three_pads_in_order": "horizon_bound",   # C, video candidate
    "front_loaded_excavation": "infeasible",  # D, horizon short
    "small_fleet_long_berm": "infeasible",    # D, energy short 15 %
    "solar_array_pad": "infeasible",          # D, energy short 25 %
    "low_battery_pad": "infeasible",          # E: infeasible as written, feasible re-scoped
}


@pytest.fixture(scope="module")
def bb() -> ModuleType:
    spec = importlib.util.spec_from_file_location("brief_budget", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["brief_budget"] = module   # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


FLEET = [{"id": "EX1", "type": "excavator", "battery_wh": 5000},
         {"id": "EX2", "type": "excavator", "battery_wh": 5000},
         {"id": "HL1", "type": "hauler", "battery_wh": 3000},
         {"id": "CP1", "type": "compactor", "battery_wh": 3500},
         {"id": "PR1", "type": "printer", "battery_wh": 4000},
         {"id": "SV1", "type": "surveyor", "battery_wh": 1500}]


def phases(bb: ModuleType, structure: dict, fleet: set[str] | None = None,
           **kwargs) -> dict[str, object]:
    budget = bb.structure_budget(structure, fleet or RATED_TYPES, "Z1", [], **kwargs)
    return {p.phase: p for p in budget.phases}


# ----------------------------------------------------------------------------- geometry
def test_landing_pad_bowl_is_125_7_m3_at_0_4_m(bb: ModuleType) -> None:
    pad = {"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.4,
           "thickness_m": 0.3, "operations": ["survey", "excavate", "compact", "sinter"]}
    area, basis = bb.footprint(pad, "pad")
    assert area == pytest.approx(math.pi * 100) and basis == "pi*(10 m)^2"
    volume, vbasis = bb.cut_volume(pad, area, basis)
    assert round(volume, 1) == 125.7 and vbasis == "pi*(10 m)^2 x 0.4 m"
    p = phases(bb, pad)
    assert [k for k in p] == ["survey", "excavate", "haul", "compact", "sinter", "inspect"]
    assert (p["excavate"].quantity, p["excavate"].unit) == (125.7, "m3")
    assert (p["excavate"].duration_h, p["excavate"].energy_wh) == (18, 2010.6)   # 7 m3/h, 16 Wh/m3
    assert (p["compact"].quantity, p["compact"].duration_h, p["compact"].energy_wh) == (314.2, 13, 1256.6)
    assert (p["sinter"].quantity, p["sinter"].duration_h, p["sinter"].energy_wh) == (314.2, 23, 2701.8)
    assert p["haul"].quantity == 125.7 and p["survey"].duration_h == 3
    # the spoil is counted at the structure built from it, never twice
    assert "haul" not in phases(bb, pad, spoil_taken=True)


def test_berm_volume_from_the_triangular_section_and_the_1_to_1_default(bb: ModuleType) -> None:
    berm = {"id": "S2", "type": "blast_berm", "length_m": 60, "height_m": 2.5, "width_m": 4,
            "fill_from": "S1", "operations": ["haul", "compact", "sinter", "survey"]}
    assert bb.fill_volume(berm, "berm", 240.0, "60 m x 4 m") == (300.0, "60 m x 0.5 x 2.5 m x 4 m")
    p = phases(bb, berm)
    assert (p["haul"].quantity, p["haul"].duration_h, p["haul"].energy_wh) == (300.0, 25, 2100.0)
    assert (p["compact"].quantity, p["compact"].energy_wh) == (240.0, 960.0)      # length x base
    assert (p["sinter"].quantity, p["sinter"].basis) == (90.0, "crest 60 m x 1.5 m")
    assert "excavate" not in p and p["inspect"].quantity == 240.0
    # access_road's berm gives only a height: base = 2 x height, section = height^2
    small = {"id": "S3", "type": "protective_berm", "length_m": 20, "height_m": 1.5,
             "operations": ["haul", "compact"]}
    q = phases(bb, small)
    assert q["haul"].quantity == 45.0 and "1:1 slopes" in q["haul"].basis
    assert q["compact"].quantity == 60.0 and "base = 2 x height" in q["compact"].basis
    # a key trench is sized by its own width when given, by the base otherwise
    keyed = dict(berm, trench_depth_m=0.5, operations=["excavate", "haul", "compact"])
    assert phases(bb, keyed)["excavate"].quantity == 120.0
    assert phases(bb, dict(keyed, trench_width_m=1.2, trench_depth_m=0.8))["excavate"].quantity == 57.6


def test_apron_area_is_stripped_and_fines_by_their_depth(bb: ModuleType) -> None:
    """The rate table's excavator m2 row is a surface layer up to 0.2 m (STRIP_DEPTH_M), the
    same rule the validator's volume check credits: 15 cm of fines is stripped over the
    footprint in m2, 30 cm is dug in m3, and so is a cut whose own depth_m is that shallow."""
    apron = {"id": "S3", "type": "approach_apron", "length_m": 30, "width_m": 8,
             "operations": ["grade"]}
    p = phases(bb, apron)
    assert list(p) == ["strip"]
    assert (p["strip"].quantity, p["strip"].unit, p["strip"].duration_h, p["strip"].energy_wh) == (
        240.0, "m2", 10, 1152.0)                                                    # 25 m2/h, 4.8 Wh/m2
    fleet = {"excavator", "compactor", "surveyor"}
    pad = {"id": "S1", "type": "instrument_pad", "length_m": 10, "width_m": 10,
           "fines_depth_m": 0.15, "operations": ["survey", "grade", "compact"]}
    q = phases(bb, pad, fleet=fleet)
    assert (q["strip"].quantity, q["strip"].unit, q["strip"].duration_h, q["strip"].energy_wh) == (
        100.0, "m2", 4, 480.0)
    assert q["strip"].basis == "10 m x 10 m (fines 0.15 m deep: a surface layer)"
    assert "excavate" not in q and "haul" not in q and "sinter" not in q
    deep = phases(bb, dict(pad, fines_depth_m=0.3), fleet=fleet)
    assert deep["excavate"].quantity == 30.0 and deep["excavate"].basis == "fines 10 m x 10 m x 0.3 m"
    assert "strip" not in deep
    at_limit = phases(bb, dict(pad, fines_depth_m=0.2), fleet=fleet)
    assert at_limit["strip"].quantity == 100.0 and "excavate" not in at_limit
    shallow = {"id": "S1", "type": "borrow_pit", "length_m": 10, "width_m": 6, "depth_m": 0.15,
               "operations": ["excavate"]}
    r = phases(bb, shallow, fleet=fleet)
    assert list(r) == ["strip"] and r["strip"].quantity == 60.0


def test_phases_follow_operations_flags_and_the_fleet(bb: ModuleType) -> None:
    pit = {"id": "S1", "type": "foundation_pit", "length_m": 12, "width_m": 12, "depth_m": 1.0,
           "thickness_m": 0.2, "operations": ["excavate", "compact", "sinter", "survey"]}
    p = phases(bb, pit)
    assert list(p) == ["excavate", "compact", "sinter", "inspect"]
    assert p["excavate"].quantity == 144.0 and p["sinter"].quantity == 144.0
    assert list(phases(bb, dict(pit, survey=True, haul=True))) == [
        "survey", "excavate", "haul", "compact", "sinter", "inspect"]
    assert "sinter" not in phases(bb, pit, fleet={"excavator", "compactor", "surveyor"})
    assert bb.kind_of("borrow_pit") == "borrow" and bb.kind_of("floor_landing_pad") == "pad"
    assert bb.kind_of("shoulder_windrows") == "berm" and bb.kind_of("equipment_hardstand") == "other"
    assert bb.kind_of("haul_lane") == "road" and bb.kind_of("depression_crossing") == "fill"


# ----------------------------------------------------------------------------- packing / verdict
def test_best_packing_is_exact_for_small_instances(bb: ModuleType) -> None:
    ratio, loads, exact = bb.best_packing([904.8, 921.6, 921.6], [1750, 1750])
    assert exact and ratio == pytest.approx(1826.4 / 1750) and sorted(loads) == [921.6, 1826.4]
    ratio, _, _ = bb.best_packing([452.4, 452.4, 921.6, 921.6], [1750, 1750])
    assert ratio == pytest.approx(1374.0 / 1750)
    ratio, _, _ = bb.best_packing([3000, 3000, 3000], [5000, 5000])
    assert ratio == pytest.approx(1.2)
    assert bb.best_packing([], [5000]) == (0.0, [0.0], True)
    assert bb.best_packing([10.0], []) == (math.inf, [], True)
    ratio, loads, exact = bb.best_packing([100.0] * 30, [1000, 1000, 1000])
    assert not exact and ratio == pytest.approx(1.0) and sum(loads) == 3000


def spec(bb: ModuleType, structures: list[dict], robots: list[dict], horizon_h: int):
    return bb.compute_budget({"structures": structures, "robots": robots, "horizon_h": horizon_h})


def test_verdict_comfortable_careful_split_and_balance(bb: ModuleType) -> None:
    pad = {"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.4,
           "operations": ["survey", "excavate", "compact"]}
    easy = spec(bb, [pad], FLEET, 336)
    assert easy.verdict.kind == "comfortable" and easy.verdict.text == "comfortable"
    assert easy.types["excavator"].margin_pct == pytest.approx(397.4, abs=0.1)
    assert easy.lower_bound_h == easy.critical_path_h == 48 and easy.serial_h == 48
    assert [p.phase for p in easy.structures[0].phases] == [
        "survey", "excavate", "haul", "compact", "inspect"]      # 3 + 18 + 11 + 13 + 3 h
    # three ~920 Wh digs on two 1750 Wh excavators: every pair overflows a battery
    digs = [{"id": "S1", "type": "beacon_pad", "diameter_m": 12, "depth_m": 0.5,
             "operations": ["survey", "excavate", "compact"]},
            {"id": "S2", "type": "dust_berm", "length_m": 60, "height_m": 1.2, "width_m": 3,
             "trench_depth_m": 0.8, "trench_width_m": 1.2, "fill_from": ["S1", "S3"],
             "operations": ["excavate", "haul", "compact"]},
            {"id": "S3", "type": "borrow_pit", "length_m": 8, "width_m": 8, "depth_m": 0.9,
             "operations": ["excavate"]}]
    weak = [{"id": "EX1", "type": "excavator", "battery_wh": 1750},
            {"id": "EX2", "type": "excavator", "battery_wh": 1750},
            {"id": "HL1", "type": "hauler", "battery_wh": 2000},
            {"id": "CP1", "type": "compactor", "battery_wh": 2000},
            {"id": "SV1", "type": "surveyor", "battery_wh": 1000}]
    tight = spec(bb, digs, weak, 120)
    ex = tight.types["excavator"]
    assert (ex.need_wh, ex.fleet_wh, ex.margin_pct) == (2748.0, 3500.0, 27.4)
    assert not ex.pack_feasible and ex.pack_max_load_wh == 1826.4 and ex.pack_max_load_pct == 104.4
    assert tight.verdict.kind == "careful_split"
    assert tight.verdict.text == ("needs a careful split (excavator phases must be split (one task "
                                  "per phase does not pack into 2 x 1750 Wh))")
    assert any("energy_task / energy core" in r for r in tight.verdict.reasons)
    # a thin margin reads as the balance the split must keep
    thin = spec(bb, digs, [dict(r, battery_wh=1420) if r["type"] == "excavator" else r
                          for r in weak], 120)
    assert thin.verdict.kind == "careful_split"
    assert "excavator Wh fits only if work is balanced within 3 %" in thin.verdict.text
    assert thin.types["excavator"].balance_pct == pytest.approx(1420 / 1374 * 100 - 100, abs=0.1)


def test_verdict_horizon_bound_and_infeasible(bb: ModuleType) -> None:
    pads = [{"id": f"S{i}", "type": f"cargo_pad_{c}", "diameter_m": 14, "depth_m": 0.4,
             "haul": False, "operations": ["survey", "excavate", "compact"]}
            for i, c in enumerate("abc", start=1)]
    crew = [r for r in FLEET if r["type"] in ("excavator", "compactor", "surveyor")]
    bound = spec(bb, pads, crew, 48)
    assert bound.verdict.kind == "horizon_bound"
    assert (bound.lower_bound_h, bound.serial_h, bound.horizon_h) == (19, 57, 48)
    assert bound.verdict.text == ("horizon-bound (deadline fits only with parallel zones: serial "
                                  "57 h vs horizon 48 h, parallel phase-order bound 19 h)")
    assert spec(bb, pads, crew, 72).verdict.kind == "comfortable"       # serial 57 h fits
    late = spec(bb, pads, crew, 18)
    assert late.verdict.kind == "infeasible"
    assert late.verdict.text == ("infeasible without relaxation (horizon short by 1 h "
                                 "(phase-order bound 19 h vs 18 h))")
    borrow = [{"id": "S1", "type": "borrow_strip", "length_m": 100, "width_m": 6, "depth_m": 0.6,
               "operations": ["excavate"]}]
    short = spec(bb, borrow, [FLEET[0]], 336)
    assert short.verdict.kind == "infeasible"
    assert short.verdict.text == ("infeasible without relaxation (excavator energy short by 760 Wh "
                                  "(15 % of its 5000 Wh))")
    # a type the fleet lacks is dropped from the budget, as the validator drops the phase
    none = spec(bb, borrow, [FLEET[5]], 336)
    assert none.verdict.kind == "comfortable" and "excavate: no excavator" in none.assumptions[-1]


def test_single_robot_tightness_is_a_note_not_a_split_verdict(bb: ModuleType) -> None:
    pad = {"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.4,
           "thickness_m": 0.3, "operations": ["survey", "excavate", "compact", "sinter"]}
    b = spec(bb, [pad], [FLEET[0], FLEET[3], dict(FLEET[4], battery_wh=2800), FLEET[5]], 336)
    assert b.verdict.kind == "comfortable"
    assert b.verdict.reasons == ["printer at 96 % of its single battery (transit Wh counts too)"]


# ----------------------------------------------------------------------------- briefs
def test_every_eval_brief_parses_and_lands_in_its_kind(bb: ModuleType) -> None:
    briefs = sorted(p.stem for p in EVAL_DIR.glob("*.md"))
    assert briefs == sorted(EXPECTED_KIND) and len(briefs) <= 24
    assert set(briefs) <= set(bb.BRIEF_STRUCTURES)
    for stem in briefs:
        path = EVAL_DIR / f"{stem}.md"
        text = path.read_text(encoding="utf-8")
        assert "one robot may work inside" in text.lower(), stem
        robots, horizon_h = bb.parse_brief(text)
        assert robots and horizon_h > 0, stem
        assert {r["type"] for r in robots} <= RATED_TYPES, stem
        assert len({r["id"] for r in robots}) == len(robots), stem
        s = bb.spec_from_brief(path)
        budget = bb.compute_budget(s, str(path))
        assert budget.verdict.kind == EXPECTED_KIND[stem], (stem, budget.verdict.text)
        assert isinstance(budget.requirements, Requirements)
        assert all(st.operations for st in budget.requirements.structures), stem
        assert all(p.quantity > 0 for st in budget.structures for p in st.phases), stem
        graph = bb.build_graph(budget, "lazy")
        assert all(t.quantity and t.unit and t.basis for t in graph.tasks), stem
        assert len(set(graph.ids())) == len(graph.tasks)


def test_demo_briefs_share_the_registry_with_their_eval_twins(bb: ModuleType) -> None:
    for path in sorted(DEMO_DIR.glob("*.md")):
        assert path.read_text() == (EVAL_DIR / path.name).read_text()
        budget = bb.compute_budget(bb.spec_from_brief(path), str(path))
        assert budget.verdict.kind == "comfortable"


def test_parse_brief_formats(bb: ModuleType) -> None:
    text = ("# T\n\n**Fleet.** EX1, EX2 and EX3 (excavators, 5,000 Wh each), HL1 (hauler, "
            "3000 Wh remaining) and SV1 (surveyor, 1500 Wh).\n\n**Deadline.** 60 hours.\n")
    robots, horizon = bb.parse_brief(text)
    assert [r["id"] for r in robots] == ["EX1", "EX2", "EX3", "HL1", "SV1"]
    assert [r["battery_wh"] for r in robots] == [5000, 5000, 5000, 3000, 1500]
    assert [r["type"] for r in robots] == ["excavator"] * 3 + ["hauler", "surveyor"] and horizon == 60
    assert bb.parse_brief(text.replace("60 hours", "2.5 days"))[1] == 60
    with pytest.raises(ValueError, match="no fleet"):
        bb.parse_brief("**Deadline.** 3 days.")
    with pytest.raises(ValueError, match="no deadline"):
        bb.parse_brief("**Fleet.** EX1 (excavator, 5000 Wh).")


def test_the_kind_e_floor_reaches_the_requirements_and_the_notes(bb: ModuleType) -> None:
    """low_battery_pad allows "any diameter between 12 m and 20 m": the registry records the
    floor as the structure stage does (min_diameter_m), the requirements carry it to the
    volume check, and the budget, which prices the full size, says where the floor is."""
    budget = bb.compute_budget(bb.spec_from_brief(EVAL_DIR / "low_battery_pad.md"), "x")
    pad = budget.requirements.structures[0]
    assert pad.dims()["min_diameter_m"] == 12
    assert ("S1: the brief allows bowl 12-20 m across, 0.5 m deep: the validator's volume floor "
            "is 56.5 of 157.1 m3 (bowl 12 m across, 0.5 m deep), 70 % of which a plan must "
            "cover (95 % to count in scope); this budget prices the full size") in budget.assumptions
    assert budget.types["excavator"].need_wh == 2513.3          # the full 20 m bowl


def test_overrides_edit_structures_robots_and_the_horizon(bb: ModuleType) -> None:
    s = bb.spec_from_brief(EVAL_DIR / "low_battery_pad.md")
    bb.apply_overrides(s, ["S1.diameter_m=16", "EX1.battery_wh=2000", "horizon_h=72",
                           "S2.haul=false"])
    assert s["structures"][0]["diameter_m"] == 16 and s["structures"][1]["haul"] is False
    assert s["robots"][0]["battery_wh"] == 2000 and s["horizon_h"] == 72
    with pytest.raises(ValueError, match="no structure or robot"):
        bb.apply_overrides(s, ["S9.depth_m=1"])


# ----------------------------------------------------------------------------- inputs / CLI
@pytest.mark.parametrize("brief", ["landing_pad", "access_road", "tight_battery"])
def test_script_runs_on_every_fixture_structure_json(bb: ModuleType, brief: str, capsys) -> None:
    path = FIXTURES / brief / "structure.json"
    assert bb.main([str(path), "--no-solve"]) == 0
    out = capsys.readouterr().out
    assert "verdict: comfortable" in out and "assumptions:" in out
    assert bb.main([str(path), "--json", "--no-solve"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["horizon_h"] == Requirements.model_validate_json(path.read_text()).horizon_h
    assert {s["id"] for s in report["structures"]} == {
        s["id"] for s in json.loads(path.read_text())["structures"]}
    assert report["graphs"] == {} and report["verdict"]["kind"] == "comfortable"


def test_landing_pad_fixture_budget_matches_the_hand_arithmetic(bb: ModuleType) -> None:
    budget = bb.compute_budget(bb.spec_from_json(FIXTURES / "landing_pad" / "structure.json"))
    assert [s.zone for s in budget.structures] == ["Z1", "Z2"]     # the fixture's own zones
    assert budget.types["printer"].need_wh == 3475.8 and budget.types["hauler"].need_wh == 2100.0
    assert budget.critical_path == ["S1/survey", "S1/excavate", "S2/haul", "S2/compact",
                                    "S2/sinter", "S2/inspect"]          # 3+18+25+10+7+2 h
    assert (budget.critical_path_h, budget.serial_h, budget.zones) == (65, 104, {"Z1": 60, "Z2": 44})


def test_plan_json_and_inline_spec_inputs(bb: ModuleType, tmp_path, capsys) -> None:
    req = json.loads((FIXTURES / "access_road" / "structure.json").read_text())
    plan = tmp_path / "x.plan.json"
    plan.write_text(json.dumps({"requirements": req, "success": True}))
    assert bb.main([str(plan), "--no-solve", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["types"]["compactor"]["robots"] == 2
    inline = json.dumps({"horizon_h": 48, "robots": FLEET[:1] + FLEET[5:],
                         "structures": [{"id": "S1", "type": "borrow_strip", "length_m": 10,
                                         "width_m": 6, "depth_m": 0.5, "operations": ["excavate"]}]})
    assert bb.main(["-", "--spec", inline, "--no-solve"]) == 0
    assert "30 m3" in capsys.readouterr().out
    unknown = tmp_path / "unknown.md"
    unknown.write_text("**Fleet.** EX1 (excavator, 5000 Wh).\n\n**Deadline.** 1 day.\n")
    with pytest.raises(ValueError, match="no structures registered"):
        bb.spec_from_brief(unknown)


def test_all_mode_prints_one_row_per_brief(bb: ModuleType, capsys) -> None:
    assert bb.main(["--all", str(EVAL_DIR), "--markdown", "--no-solve"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("| brief | fleet | horizon |") and len(lines) == 2 + len(EXPECTED_KIND)
    assert bb.main(["--all", str(EVAL_DIR), "--no-solve"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == len(EXPECTED_KIND)
    assert bb.main(["--list"]) == 0
    assert set(capsys.readouterr().out.split()) >= set(EXPECTED_KIND)


# ----------------------------------------------------------------------------- mechanism (CP-SAT)
def outcome(bb: ModuleType, stem: str, variant: str, **overrides: str) -> dict:
    s = bb.spec_from_brief(EVAL_DIR / f"{stem}.md")
    bb.apply_overrides(s, [f"{k}={v}" for k, v in overrides.items()])
    budget = bb.compute_budget(s, stem)
    return bb.evaluate_graph(budget.requirements, bb.build_graph(budget, variant), 5.0)


def test_depleted_excavators_fails_with_an_energy_core_until_the_bowl_is_split(bb: ModuleType) -> None:
    lazy = outcome(bb, "depleted_excavators", "lazy")
    assert (lazy["stage"], lazy["ok"]) == ("schedule", False)      # the validator passes it
    assert lazy["conflicts"] == ["energy:EX1", "energy:EX2"]
    assert outcome(bb, "depleted_excavators", "split") == lazy     # nothing is over 60 m3
    fine = outcome(bb, "depleted_excavators", "fine")
    assert fine["ok"] and fine["makespan_h"] <= 48 and fine["tasks"] == 13


def test_three_pads_in_order_fails_only_when_the_pads_are_chained(bb: ModuleType) -> None:
    lazy = outcome(bb, "three_pads_in_order", "lazy")
    assert lazy["ok"] and lazy["makespan_h"] <= 40      # well inside the 48 h horizon
    serial = outcome(bb, "three_pads_in_order", "serial")
    assert serial["stage"] == "validate" and serial["conflicts"][0].startswith("horizon_path:")
    assert "57 h" in serial["hint"]


def test_relaxation_briefs_name_the_shortfall(bb: ModuleType) -> None:
    # the 360 m3 borrow dig is one task larger than the single excavator's battery, which is
    # also the whole pool's shortfall: one cause, one verdict (energy_task), whose hint says
    # that splitting cannot fit it
    berm = outcome(bb, "small_fleet_long_berm", "lazy")
    assert berm["stage"] == "validate" and berm["conflicts"] == ["energy_task:T4"]
    assert "5,760 Wh" in berm["hint"] and "5,000 Wh" in berm["hint"]
    assert "The fleet's 1 excavator carries 5,000 Wh in all" in berm["hint"]
    pad = outcome(bb, "front_loaded_excavation", "split")
    assert pad["stage"] == "schedule" and set(pad["conflicts"]) == {"horizon", "zone:Z1"}
    assert "60 h horizon" in pad["hint"] and "Zone Z1 admits one robot at a time" in pad["hint"]
    # E: infeasible as written, feasible at the diameter the brief allows
    full = outcome(bb, "low_battery_pad", "lazy")
    assert full["stage"] == "validate" and full["conflicts"] == ["energy_task:T2"]
    small = outcome(bb, "low_battery_pad", "lazy", **{"S1.diameter_m": "16"})
    assert small["ok"] and small["makespan_h"] <= 168


# ----------------------------------------------------------------------------- review fixes
def test_kinds_json_names_every_brief_with_the_letter_of_its_designed_kind() -> None:
    """``examples/eval/kinds.json`` is what ``moonwright eval`` joins into its per-kind table;
    it must agree with EXPECTED_KIND and docs/eval-briefs.md."""
    kinds = json.loads((EVAL_DIR / "kinds.json").read_text())
    assert sorted(kinds) == sorted(EXPECTED_KIND)
    letter = {"comfortable": "A", "careful_split": "B", "horizon_bound": "C", "infeasible": "D"}
    for stem, kind in EXPECTED_KIND.items():
        expected = "E" if stem == "low_battery_pad" else letter[kind]
        assert kinds[stem] == expected, stem
    assert set(kinds.values()) == set("ABCDE")


def test_instrument_pad_is_stripped_in_square_metres_and_passes_the_volume_check(
        bb: ModuleType) -> None:
    """The brief says "stripped of fines, levelled ... and compacted" over 15 cm of fines: the
    registry lists grade and the fines are a surface layer stripped in m2 (the prompt's own
    example). The live false positive of phase 3e: with the depth the structure stage records
    (depth_m 0.15), the same strip covers 15 of 15 m3 and passes the volume check; the old
    rule read it as 0 %."""
    (s,) = bb.BRIEF_STRUCTURES["instrument_pad"]
    assert s["operations"] == ["survey", "grade", "compact"]
    fleet = {"excavator", "compactor", "surveyor"}
    assert bb.wanted_phases(s, "pad", fleet) == ["survey", "strip", "compact", "inspect"]
    p = phases(bb, s, fleet=fleet)
    assert (p["strip"].quantity, p["strip"].unit) == (100.0, "m2") and "excavate" not in p
    budget = bb.compute_budget(bb.spec_from_brief(EVAL_DIR / "instrument_pad.md"), "x")
    g = bb.build_graph(budget, "lazy")
    assert [t.name[:5] for t in g.tasks] == ["Surve", "Strip", "Compa", "Inspe"]
    assert bb.evaluate_graph(budget.requirements, g, 5.0)["ok"]
    live = budget.requirements.model_copy(update={"structures": [
        st.model_copy(update={"depth_m": 0.15}) for st in budget.requirements.structures]})
    assert validate_graph(live, g, coverage=True) is None
    assert volume_shares(live, g) == {"S1": pytest.approx(1.0)}


def test_canonical_graphs_cover_the_volumes_the_validator_measures(bb: ModuleType) -> None:
    """Every brief's lazy and split graphs plan the volumes ``rates.dims_volumes`` measures (no
    ``volume:`` conflict) and are in scope for the eval (no ``scope_gaps``: every judged
    volume and compaction / sintering area at 95 % or more, with the brief's own text as a
    live run has it, so a sloped pit wall is read too), while the ``shrunk`` graph, the live
    re-scope that plans a dig as stripping in m2, is a volume conflict wherever a dig is
    deeper than the 0.2 m layer it strips: the kind-D borrow strip is rejected before any
    energy verdict."""
    for path in sorted(EVAL_DIR.glob("*.md")):
        budget = bb.compute_budget(bb.spec_from_brief(path), path.stem)
        live = budget.requirements.model_copy(update={"source_brief": path.read_text()})
        for variant in ("lazy", "split"):
            graph = bb.build_graph(budget, variant)
            failure = validate_graph(budget.requirements, graph, coverage=True)
            volume = [c for c in (failure.conflicts if failure else []) if c.startswith("volume:")]
            assert volume == [], (path.stem, variant, volume)
            assert scope_gaps(live, graph) == [], (path.stem, variant)
            assert volume_advisories(live, graph) == [], (path.stem, variant)
    berm = outcome(bb, "small_fleet_long_berm", "shrunk")
    assert berm["stage"] == "validate" and berm["conflicts"] == ["volume:S2"]
    assert "plan the remaining 240 m3 of S2 as excavator tasks in m3" in berm["hint"]
    # the E brief's 12 m floor is no longer covered by stripping the whole 20 m pad in m2: the
    # credit stops at the floor's own footprint and the 0.3 m below it needs a dig
    pad = outcome(bb, "low_battery_pad", "shrunk")
    assert pad["conflicts"] == ["scope:S1", "volume:S1"]


def test_reference_graphs_haul_only_after_the_dig_their_fill_comes_from(bb: ModuleType) -> None:
    """Phase 3g's supply check (``supply:<haul>``: a haul that waits for no source, or for too
    little while another dig could supply it) on every brief's canonical graphs, the
    ``shrunk`` re-scope included: each haul waits for the excavation of the structures its
    fill comes from wherever the registry lists them (the mound of ``shielding_mound``, the
    berm of ``small_fleet_long_berm`` and the berms of ``habitat_berm_tight_horizon`` and
    ``depleted_excavators`` come before a source of theirs), a source stripped in m2 included
    (``one_surveyor_three_structures``' berm is built from the hardstand's strip spoil), the
    serial graph chains the sources first, and the bounds read them in that order."""
    for path in sorted(EVAL_DIR.glob("*.md")):
        budget = bb.compute_budget(bb.spec_from_brief(path), path.stem)
        live = budget.requirements.model_copy(update={"source_brief": path.read_text()})
        for variant in ("lazy", "split", "fine", "serial", "shrunk"):
            graph = bb.build_graph(budget, variant)
            failure = validate_graph(live, graph, supply=True)
            supply = [c for c in (failure.conflicts if failure else []) if c.startswith("supply:")]
            assert supply == [], (path.stem, variant, supply)
            assert all(s.digs for s in supply_report(live, graph)), (path.stem, variant)
    surveyor = bb.compute_budget(
        bb.spec_from_brief(EVAL_DIR / "one_surveyor_three_structures.md"), "x")
    haul = bb.build_graph(surveyor, "lazy").task("T6")
    assert haul.name == "Haul fill to S2 containment berm" and haul.after == ["T5", "T2"]
    budget = bb.compute_budget(bb.spec_from_brief(EVAL_DIR / "small_fleet_long_berm.md"), "x")
    assert [s.id for s in bb.fill_order(budget.structures)] == ["S2", "S1"]
    lazy = bb.build_graph(budget, "lazy")
    assert lazy.task("T1").name == "Haul fill to S1 protective berm"
    assert lazy.task("T1").after == ["T4"] and lazy.task("T4").name.startswith("Excavate S2")
    serial = bb.build_graph(budget, "serial")
    assert serial.task("T1").after == ["T4"] and serial.task("T4").after == []
    # the critical path waits for the borrow dig too: 52 h of digging before the haul
    assert budget.critical_path[:2] == ["S2/excavate", "S1/haul"] and budget.lower_bound_h == 112
    # road_and_pad's windrows are grading spoil pushed by the grader, not a haul of the road's
    # m2 grading (which digs nothing): the pad keeps its own spoil haul after its dig
    road = bb.compute_budget(bb.spec_from_brief(EVAL_DIR / "road_and_pad.md"), "x")
    assert [p.phase for p in road.structures[2].phases] == ["strip", "compact"]
    assert [p.phase for p in road.structures[1].phases] == [
        "survey", "excavate", "haul", "compact", "sinter", "inspect"]
