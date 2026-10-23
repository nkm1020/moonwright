"""Brief budget: the arithmetic behind a Moonwright brief, from the planner's own rate table.

A brief-design and documentation tool (docs/eval-briefs.md). Given the structures of a brief
with their dimensions and the fleet with its batteries, it computes what the rate table in
``moonwright.pipeline.rates`` implies before any model is asked:

* per structure, the quantities its dimensions imply (bowl ``pi r^2 d``, berm length x
  cross-section, road / apron / strip area, pit volume, surface area for compaction and
  sintering) and the hours and Wh of every phase (survey, strip, excavate, haul, compact,
  sinter, inspect), one canonical task per phase;
* per robot type, the Wh needed against the fleet's Wh (margin %), the hours needed per robot
  against the horizon (a lower bound), and whether the canonical one-task-per-phase graph can
  be packed into the robots' batteries at all (exact bin packing);
* per zone, the serial hours (one robot per zone), and the critical path of the canonical
  phase order with the fill dependency (a berm's haul waits for its source's whole
  excavation, a strip in m2 included); the largest of the three is the *phase-order bound*
  (``LB``): a lower bound for a plan that keeps each phase whole, as the canonical graphs do,
  but not for one that pipelines dig and haul parts (a haul part after the dig part that
  feeds it, the supply verdict's advice), which can finish below it;
* a verdict: "comfortable" / "needs a careful split (per-type Wh fits only if work is balanced
  within X %)" / "horizon-bound (deadline fits only with parallel zones)" / "infeasible without
  relaxation (energy short by Y Wh / horizon short by Z h)";
* optionally (``--graphs``), the canonical graphs are built as real ``TaskGraph`` objects and
  run through the real validator and CP-SAT, so the "expected first-try outcome" column of
  the docs is the solver's verdict on a stated graph, not a guess: ``lazy`` (one task per
  phase per structure), ``split`` (the prompt's rule: physical work over 60 m3 / 300 m2 split
  into as many equal tasks as the type has robots), ``fine`` (every physical phase shared
  that way whatever its size: the split a battery core asks for), ``serial`` (lazy plus
  every structure after the previous one, the serial planner's habit) and ``shrunk`` (lazy
  with every dig in m3 planned as stripping its footprint in m2: the re-scope the live models
  made to fit a battery, which the validator's ``volume:<structure>`` check now rejects).

Inputs
------
``SOURCE`` is one of: a brief ``.md`` (the fleet and the deadline are parsed from its text;
the structures come from :data:`BRIEF_STRUCTURES`, keyed by the file stem, because the prose
dimensions are the model's job to read), a Requirements JSON (``fixtures/<brief>/
structure.json``) or a plan JSON (its ``requirements``), or ``-`` with ``--spec``. ``--spec``
takes the same shape as Requirements (``structures`` with dims and optional ``operations``,
``robots``, ``horizon_h``) inline or as ``@path``; ``--set S1.depth_m=0.5`` edits a value
before the budget is computed (``horizon_h=72``, ``EX1.battery_wh=1800`` work too), which is
how a brief's numbers are tuned.

Assumptions (every one printed with the budget, every number an assumption of the rate table)
--------------------------------------------------------------------------------------------
* Durations and energies are the rate table's: ``ceil(quantity / rate)`` whole hours and
  ``quantity x Wh/unit``; the CP-SAT model adds the whole hours and the 0.5 Wh per grid cell
  of every leg each robot drives, so a solver makespan can sit a few hours above the bounds
  here.
* Phases per structure follow the kind of its type (pad, berm, road, pit, trench, apron,
  ramp, stockpile, borrow, fill, other) and the ``operations`` the structure lists; the
  defaults are documented at :data:`KIND_DEFAULTS`. Flags on a structure override them
  (``"haul": false``, ``"inspect": true``, ``"sinter": true``, ``"lifts": 2`` ...).
* A berm's cross-section is triangular (``0.5 x height x base width``) when both are given
  and ``height^2`` (1:1 slopes, base = 2 x height) when only the height is; its compaction
  area is ``length x base width x lifts``; a sintered crest is ``length x crest_width_m``
  (default 1.5 m). A pad or pit bowl is ``area x depth`` (vertical sides; a sloped wall is
  not modelled here). These are the volumes ``moonwright.pipeline.rates.dims_volumes`` gives
  the validator's ``volume:<structure>`` check, so a canonical graph covers what it measures:
  where the check reads more than the script (a sloped wall the brief states, judged between
  its rim and floor readings; a berm judged against the spoil its hauls wait for) the range
  contains these figures, and a key trench without ``trench_width_m``, which the script prices
  at the berm's base width, is not judged at all.
* A layer no deeper than :data:`~moonwright.pipeline.rates.STRIP_DEPTH_M` (0.2 m: a pad's
  ``fines_depth_m``, or a cut's ``depth_m``) is stripped over its footprint in m2, the rate
  table's excavator m2 row ("a surface layer up to 0.2 m"), exactly as the volume check
  credits an m2 task; deeper fines are excavated in m3 (``area x fines_depth_m``).
* ``min_<dimension>_m`` keys (the smallest size a kind-E brief allows, as the structure stage
  records them) are passed to the requirements, so the volume check's floor is the brief's;
  the budget itself prices the full size and notes the floor (``--set`` prices a smaller one).
* A haul belongs to the receiving structure and its zone (the coverage contract); its volume
  is the fill the structure needs, and it waits for the excavation of ``fill_from`` (default:
  the first other structure that excavates). A phase whose robot type is absent from the
  fleet is dropped, as the validator drops it.
* The bounds ignore transit and resource contention beyond one robot per zone; the
  ``--graphs`` solve is the number to trust.

Usage (from backend/)::

    uv run python scripts/brief_budget.py fixtures/landing_pad/structure.json
    uv run python scripts/brief_budget.py examples/eval/tight_battery.md --graphs lazy,split
    uv run python scripts/brief_budget.py examples/eval/low_battery_pad.md --set S1.diameter_m=12
    uv run python scripts/brief_budget.py --all examples/eval --markdown     # the docs table
    uv run python scripts/brief_budget.py - --spec '{"horizon_h": 72, "robots": [...], ...}' --json
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from moonwright.geometry import place_zones
from moonwright.pipeline.rates import STRIP_DEPTH_M, apply_rates, dims_volume, rate_for
from moonwright.pipeline.schedule import SolveOptions, solve_schedule
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import (
    Failure,
    Requirements,
    Robot,
    Schedule,
    Site,
    Structure,
    Task,
    TaskGraph,
    Zone,
)
from moonwright.text import qty

# ----------------------------------------------------------------------------- constants
PHASES: tuple[str, ...] = ("survey", "strip", "excavate", "haul", "compact", "sinter", "inspect")
PHASE_TYPE: dict[str, str] = {
    "survey": "surveyor", "strip": "excavator", "excavate": "excavator", "haul": "hauler",
    "compact": "compactor", "sinter": "printer", "inspect": "surveyor",
}
PHASE_UNIT: dict[str, str] = {
    "survey": "m2", "strip": "m2", "excavate": "m3", "haul": "m3", "compact": "m2",
    "sinter": "m2", "inspect": "m2",
}
PHYSICAL: frozenset[str] = frozenset({"strip", "excavate", "haul", "compact", "sinter"})
GRAPH_VARIANTS: tuple[str, ...] = ("lazy", "split", "fine", "serial", "shrunk")
# the task-graph prompt's split rule: physical work above this is split into as many equal
# tasks as the type has robots
SPLIT_THRESHOLD: dict[str, float] = {"m3": 60.0, "m2": 300.0}
# ``Structure.operations`` vocabulary -> phases of this script
OPERATION_PHASES: dict[str, tuple[str, ...]] = {
    "survey": ("inspect",), "grade": ("strip",), "excavate": ("excavate",), "haul": ("haul",),
    "compact": ("compact",), "sinter": ("sinter",),
}
FLAGS: tuple[str, ...] = ("survey", "strip", "excavate", "haul", "compact", "sinter", "inspect")
CREST_WIDTH_M = 1.5
ZONE_RADIUS = 4

# verdict thresholds
ENERGY_COMFORT = 0.25     # every multi-robot type keeps at least this margin -> comfortable
HORIZON_COMFORT = 0.25    # horizon / phase-order bound - 1 at least this -> comfortable
TIGHT_SINGLE = 0.10       # a single-robot type above 90 % of its battery is flagged, not judged
MAX_EXACT_PACK_ITEMS = 18

# default phases per kind: (phase, condition) where condition is a key the structure must
# have ("depth_m"), "haulers" / "compactors" / "printer" for a fleet type, or "" for always
KIND_DEFAULTS: dict[str, tuple[tuple[str, str], ...]] = {
    "pad": (("survey", ""), ("strip", "fines_depth_m"), ("excavate", "depth_m"),
            ("haul", "haulers"), ("compact", ""), ("sinter", "thickness_m"), ("inspect", "")),
    "berm": (("excavate", "trench_depth_m"), ("haul", "haulers"), ("compact", "")),
    "road": (("strip", ""), ("compact", "")),
    "apron": (("strip", ""),),
    "ramp": (("excavate", "depth_m"), ("haul", "haulers"), ("compact", "")),
    "pit": (("excavate", ""), ("compact", ""), ("sinter", "thickness_m")),
    "trench": (("excavate", ""), ("compact", "")),
    "stockpile": (("haul", "haulers"), ("compact", "")),
    "fill": (("haul", "haulers"), ("compact", "")),
    "borrow": (("excavate", ""),),
    "other": (("excavate", "volume_m3"), ("strip", "area_m2")),
}
_KIND_WORDS: tuple[tuple[str, str], ...] = (
    ("trench", "trench"), ("stockpile", "stockpile"), ("borrow", "borrow"), ("windrow", "berm"),
    ("berm", "berm"), ("mound", "berm"), ("crossing", "fill"), ("bridge", "fill"),
    ("foundation", "pit"), ("pit", "pit"), ("ramp", "ramp"), ("road", "road"), ("lane", "road"),
    ("apron", "apron"), ("turnaround", "apron"), ("strip", "apron"), ("pad", "pad"),
)
_DIM_KEYS = ("diameter_m", "length_m", "width_m", "height_m", "depth_m", "thickness_m",
             "trench_depth_m", "trench_width_m", "fines_depth_m", "crest_width_m", "area_m2",
             "volume_m3", "fill_m3", "lifts", "min_diameter_m", "min_length_m", "min_width_m",
             "min_height_m", "min_depth_m")


# ----------------------------------------------------------------------------- brief registry
# Structures of the briefs under examples/eval and examples/briefs, keyed by file stem: the
# dimensions the brief states (the model's job to read), the operations it asks for in the
# validator's vocabulary, ``fill_from`` where the brief names the spoil source, and flags
# where the kind default would not match the brief. The fleet and the deadline are parsed
# from the brief text itself (:func:`parse_brief`), so a battery or deadline edit in the
# .md changes the budget without touching this table.
BRIEF_STRUCTURES: dict[str, list[dict[str, Any]]] = {
    "landing_pad": [
        {"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.4, "thickness_m": 0.3,
         "operations": ["survey", "excavate", "compact", "sinter"]},
        {"id": "S2", "type": "blast_berm", "length_m": 60, "height_m": 2.5, "width_m": 4,
         "fill_from": "S1", "operations": ["haul", "compact", "sinter", "survey"]},
    ],
    "access_road": [
        {"id": "S1", "type": "access_road", "length_m": 120, "width_m": 4, "thickness_m": 0.2,
         "operations": ["grade", "compact", "survey"]},
        {"id": "S2", "type": "habitat_foundation", "length_m": 8, "width_m": 8, "depth_m": 0.5,
         "operations": ["excavate", "compact", "survey"]},
        {"id": "S3", "type": "protective_berm", "length_m": 20, "height_m": 1.5,
         "fill_from": "S2", "operations": ["haul", "compact"]},
    ],
    "tight_battery": [
        {"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.6,
         "operations": ["excavate", "compact"]},
        {"id": "S2", "type": "blast_berm", "length_m": 60, "height_m": 2.5, "width_m": 4,
         "trench_depth_m": 0.5, "fill_from": "S1", "operations": ["excavate", "haul", "compact"]},
        {"id": "S3", "type": "approach_apron", "length_m": 30, "width_m": 8,
         "operations": ["grade"]},
    ],
    "cable_trench": [
        {"id": "S1", "type": "cable_trench", "length_m": 150, "width_m": 0.4, "depth_m": 0.6,
         "operations": ["excavate", "compact", "survey"]},
        {"id": "S2", "type": "depression_crossing", "length_m": 10, "width_m": 4,
         "thickness_m": 0.5, "fill_from": "S1", "operations": ["haul", "compact"]},
    ],
    "crater_ramp": [
        {"id": "S1", "type": "access_ramp", "length_m": 40, "width_m": 5, "depth_m": 1.2,
         "operations": ["excavate", "haul", "compact", "survey"]},
        {"id": "S2", "type": "rim_turnaround", "length_m": 10, "width_m": 10, "compact": True,
         "operations": ["grade", "compact"]},
        {"id": "S3", "type": "floor_landing", "length_m": 8, "width_m": 8, "compact": True,
         "operations": ["grade", "compact"]},
    ],
    "four_mast_pads": [
        {"id": f"S{i}", "type": f"mast_pad_{c}", "length_m": 8, "width_m": 8,
         "fines_depth_m": 0.2, "volume_m3": 0.8, "haul": False,
         "operations": ["survey", "grade", "excavate", "compact"]}
        for i, c in enumerate(("ne", "nw", "se", "sw"), start=1)
    ],
    "front_loaded_excavation": [
        {"id": "S1", "type": "heavy_lander_pad", "diameter_m": 30, "depth_m": 0.4,
         "operations": ["survey", "excavate", "compact"]},
        {"id": "S2", "type": "spoil_berm", "length_m": 70, "height_m": 2, "width_m": 4,
         "fill_from": "S1", "operations": ["haul", "compact"]},
    ],
    "habitat_berm_tight_horizon": [
        {"id": "S1", "type": "foundation_pit", "length_m": 12, "width_m": 12, "depth_m": 1.0,
         "survey": True, "operations": ["survey", "excavate", "compact"]},
        {"id": "S2", "type": "plume_berm", "length_m": 70, "height_m": 2, "width_m": 4,
         "survey": True, "fill_from": ["S1", "S3"], "operations": ["haul", "compact", "survey"]},
        {"id": "S3", "type": "borrow_strip", "length_m": 40, "width_m": 6, "depth_m": 0.5,
         "operations": ["excavate"]},
    ],
    "habitat_foundation": [
        {"id": "S1", "type": "foundation_pit", "length_m": 12, "width_m": 12, "depth_m": 1.0,
         "thickness_m": 0.2, "haul": True,
         "operations": ["excavate", "compact", "sinter", "survey"]},
        {"id": "S2", "type": "perimeter_trench", "length_m": 56, "width_m": 0.4, "depth_m": 0.6,
         "operations": ["excavate", "compact", "survey"]},
    ],
    "instrument_pad": [
        # "stripped of fines, levelled ... and compacted": the brief asks for grading, not a
        # dig, and 15 cm of fines is a surface layer the excavator strips in m2 (the rate
        # table's m2 row covers up to 0.2 m), which the validator's pad rule accepts
        {"id": "S1", "type": "instrument_pad", "length_m": 10, "width_m": 10,
         "fines_depth_m": 0.15, "operations": ["survey", "grade", "compact"]},
    ],
    "isru_stockpile": [
        {"id": "S1", "type": "borrow_excavation", "length_m": 60, "width_m": 10, "depth_m": 0.35,
         "survey": True, "operations": ["excavate", "survey"]},
        {"id": "S2", "type": "feedstock_stockpile", "length_m": 25, "width_m": 5,
         "fill_m3": 210, "fill_from": "S1", "operations": ["haul", "compact"]},
    ],
    "large_fleet_pad_berm_road": [
        {"id": "S1", "type": "landing_pad", "diameter_m": 30, "depth_m": 0.4, "thickness_m": 0.3,
         "operations": ["survey", "excavate", "compact", "sinter"]},
        {"id": "S2", "type": "blast_berm", "length_m": 90, "height_m": 2.5, "width_m": 5,
         "crest_width_m": 1.0, "fill_from": "S1",
         "operations": ["haul", "compact", "sinter", "survey"]},
        {"id": "S3", "type": "access_road", "length_m": 100, "width_m": 5,
         "operations": ["grade", "compact", "survey"]},
    ],
    "low_battery_pad": [
        # "any diameter between 12 m and 20 m that fits is acceptable": the floor the
        # structure stage records as min_diameter_m
        {"id": "S1", "type": "landing_pad", "diameter_m": 20, "depth_m": 0.5,
         "min_diameter_m": 12, "operations": ["survey", "excavate", "compact"]},
        {"id": "S2", "type": "spoil_stockpile", "length_m": 12, "width_m": 8,
         "fill_from": "S1", "operations": ["haul"]},
    ],
    "one_surveyor_three_structures": [
        {"id": "S1", "type": "equipment_hardstand", "length_m": 12, "width_m": 12,
         "fines_depth_m": 0.25, "survey": True, "operations": ["survey", "grade", "compact"]},
        {"id": "S2", "type": "containment_berm", "length_m": 40, "height_m": 1.5,
         "survey": True, "fill_from": "S1", "operations": ["survey", "haul", "compact"]},
        {"id": "S3", "type": "charging_apron", "length_m": 10, "width_m": 20,
         "survey": True, "compact": True, "sinter": True,
         "operations": ["survey", "grade", "compact", "sinter"]},
    ],
    "propellant_pit_and_berm": [
        {"id": "S1", "type": "storage_pit", "diameter_m": 8, "depth_m": 2, "thickness_m": 0.2,
         "operations": ["excavate", "compact", "sinter", "survey"]},
        {"id": "S2", "type": "protective_berm", "length_m": 30, "height_m": 2.5,
         "fill_from": "S1", "operations": ["haul", "compact", "survey"]},
        {"id": "S3", "type": "haul_lane", "length_m": 40, "width_m": 5,
         "operations": ["grade", "compact"]},
    ],
    "road_and_pad": [
        {"id": "S1", "type": "service_road", "length_m": 200, "width_m": 5,
         "operations": ["grade", "compact"]},
        {"id": "S2", "type": "maintenance_pad", "diameter_m": 15, "depth_m": 0.3,
         "thickness_m": 0.2, "operations": ["survey", "excavate", "compact", "sinter"]},
        # "grading spoil pushed into continuous windrows on both shoulders": the grader
        # pushes it (an excavator m2 pass, Lightning's own reading: grade, compact), no
        # hauler carries it; the supply check would rightly reject a haul of the road's m2
        # grading, which digs nothing
        {"id": "S3", "type": "shoulder_windrows", "length_m": 200, "width_m": 1, "haul": False,
         "operations": ["grade", "compact"]},
    ],
    "shielding_mound": [
        {"id": "S1", "type": "shielding_mound", "length_m": 14, "width_m": 8, "lifts": 3,
         "fill_m3": 120, "fill_from": "S2", "operations": ["haul", "compact", "survey"]},
        {"id": "S2", "type": "borrow_pit", "length_m": 15, "width_m": 10, "depth_m": 1.0,
         "strip": True, "operations": ["excavate", "grade"]},
    ],
    "small_fleet_long_berm": [
        {"id": "S1", "type": "protective_berm", "length_m": 100, "height_m": 2, "width_m": 3.5,
         "lifts": 2, "fill_from": "S2", "operations": ["haul", "compact", "survey"]},
        {"id": "S2", "type": "borrow_strip", "length_m": 100, "width_m": 6, "depth_m": 0.6,
         "operations": ["excavate"]},
    ],
    "solar_array_pad": [
        {"id": "S1", "type": "solar_array_pad", "length_m": 30, "width_m": 18,
         "fines_depth_m": 0.3, "lifts": 2, "operations": ["survey", "excavate", "compact"]},
        {"id": "S2", "type": "anchor_trench", "length_m": 30, "width_m": 0.5, "depth_m": 0.5,
         "operations": ["excavate", "compact", "survey"]},
    ],
    "two_pads_one_printer": [
        {"id": "S1", "type": "cargo_pad_a", "diameter_m": 15, "depth_m": 0.4, "thickness_m": 0.25,
         "operations": ["survey", "excavate", "compact", "sinter"]},
        {"id": "S2", "type": "cargo_pad_b", "diameter_m": 15, "depth_m": 0.4, "thickness_m": 0.25,
         "operations": ["survey", "excavate", "compact", "sinter"]},
        {"id": "S3", "type": "shared_blast_berm", "length_m": 40, "height_m": 2,
         "fill_from": ["S1", "S2"], "operations": ["haul", "compact", "sinter"]},
    ],
    "depleted_excavators": [
        {"id": "S1", "type": "beacon_pad", "diameter_m": 12, "depth_m": 0.5,
         "operations": ["survey", "excavate", "compact"]},
        {"id": "S2", "type": "dust_berm", "length_m": 60, "height_m": 1.2, "width_m": 3,
         "trench_depth_m": 0.8, "trench_width_m": 1.2, "survey": True,
         "fill_from": ["S1", "S3"], "operations": ["survey", "excavate", "haul", "compact"]},
        {"id": "S3", "type": "borrow_pit", "length_m": 8, "width_m": 8, "depth_m": 0.9,
         "operations": ["excavate"]},
    ],
    "three_pads_in_order": [
        {"id": f"S{i}", "type": f"cargo_pad_{c}", "diameter_m": 14, "depth_m": 0.4, "haul": False,
         "operations": ["survey", "excavate", "compact"]}
        for i, c in enumerate("abc", start=1)
    ],
}


# ----------------------------------------------------------------------------- data
@dataclass
class Phase:
    phase: str
    robot_type: str
    unit: str
    quantity: float
    basis: str
    duration_h: int
    energy_wh: float


@dataclass
class StructureBudget:
    id: str
    type: str
    kind: str
    zone: str
    phases: list[Phase]
    fill_from: list[str]
    notes: list[str] = field(default_factory=list)
    area_m2: float | None = None      # the footprint (what a ``shrunk`` graph strips)

    @property
    def hours(self) -> int:
        return sum(p.duration_h for p in self.phases)

    @property
    def energy_wh(self) -> float:
        return round(sum(p.energy_wh for p in self.phases), 1)


@dataclass
class TypeBudget:
    robot_type: str
    robots: int
    fleet_wh: float
    min_battery_wh: float
    need_wh: float
    hours: int
    tasks: int
    margin_pct: float | None          # fleet / need - 1 (None when nothing is needed)
    balance_pct: float | None         # min battery / even share - 1 (multi-robot types)
    lower_bound_h: int                # ceil(hours / robots)
    pack_feasible: bool               # canonical one-task-per-phase graph fits the batteries
    pack_max_load_wh: float           # heaviest robot in the best packing of that graph
    pack_max_load_pct: float          # that load as % of its robot's battery
    pack_exact: bool                  # exact search (True) or first-fit upper bound (False)


@dataclass
class Verdict:
    kind: str                          # comfortable | careful_split | horizon_bound | infeasible
    text: str
    reasons: list[str]


# ----------------------------------------------------------------------------- geometry
def kind_of(structure_type: str) -> str:
    low = structure_type.lower()
    for word, kind in _KIND_WORDS:
        if word in low:
            return kind
    return "other"


def _num(s: dict[str, Any], key: str) -> float | None:
    v = s.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def fmt(x: float) -> str:
    """1300.0 -> '1300', 125.66 -> '125.7', 0.4 -> '0.4'."""
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.1f}" if abs(x) >= 10 else f"{x:g}"


def footprint(s: dict[str, Any], kind: str) -> tuple[float | None, str]:
    """Plan area in m2 with its basis: ``area_m2``, a circle from ``diameter_m``, ``length_m x
    width_m``, or for a berm without a base width ``length_m x 2 height_m`` (1:1 slopes)."""
    area = _num(s, "area_m2")
    if area is not None:
        return area, f"{fmt(area)} m2 given"
    d = _num(s, "diameter_m")
    if d is not None:
        return math.pi * (d / 2) ** 2, f"pi*({fmt(d / 2)} m)^2"
    length, width, height = _num(s, "length_m"), _num(s, "width_m"), _num(s, "height_m")
    if length is not None and width is not None:
        return length * width, f"{fmt(length)} m x {fmt(width)} m"
    if length is not None and height is not None and kind == "berm":
        return length * 2 * height, f"{fmt(length)} m x {fmt(2 * height)} m (base = 2 x height)"
    return None, ""


def berm_section(s: dict[str, Any]) -> tuple[float | None, str]:
    """Cross-section in m2: triangular ``0.5 x height x base`` or ``height^2`` (1:1 slopes)."""
    height, width = _num(s, "height_m"), _num(s, "width_m")
    if height is None:
        return None, ""
    if width is not None:
        return 0.5 * height * width, f"0.5 x {fmt(height)} m x {fmt(width)} m"
    return height * height, f"({fmt(height)} m)^2 (1:1 slopes, base 2 x height)"


def fill_volume(s: dict[str, Any], kind: str, area: float | None, area_basis: str) -> tuple[float | None, str]:
    """Volume a berm / mound / stockpile / fill needs: ``fill_m3`` or ``volume_m3`` given,
    length x cross-section for a berm, area x thickness for a fill."""
    for key in ("fill_m3", "volume_m3"):
        v = _num(s, key)
        if v is not None:
            return v, f"{fmt(v)} m3 given"
    length = _num(s, "length_m")
    if kind == "berm" and length is not None:
        section, basis = berm_section(s)
        if section is not None:
            return length * section, f"{fmt(length)} m x {basis}"
    thickness = _num(s, "thickness_m")
    if area is not None and thickness is not None:
        return area * thickness, f"{area_basis} x {fmt(thickness)} m"
    return None, ""


def cut_volume(s: dict[str, Any], area: float | None, area_basis: str) -> tuple[float | None, str]:
    """Excavated volume of a bowl / pit / trench / borrow / ramp: ``volume_m3`` or area x depth."""
    v = _num(s, "volume_m3")
    if v is not None:
        return v, f"{fmt(v)} m3 given"
    depth = _num(s, "depth_m")
    if area is not None and depth is not None:
        return area * depth, f"{area_basis} x {fmt(depth)} m"
    return None, ""


# ----------------------------------------------------------------------------- phases
def wanted_phases(s: dict[str, Any], kind: str, fleet: set[str]) -> list[str]:
    """Phases a structure gets, in canonical order: the kind's defaults whose condition holds,
    plus the phases of every listed operation, minus / plus explicit boolean flags."""
    want: set[str] = set()
    for phase, condition in KIND_DEFAULTS[kind]:
        ok = (condition == "" or condition in s
              or (condition == "haulers" and "hauler" in fleet))
        if phase == "excavate" and kind == "pad" and condition == "depth_m":
            ok = "depth_m" in s or "volume_m3" in s
        if ok:
            want.add(phase)
    for op in s.get("operations") or ():
        want.update(OPERATION_PHASES.get(op, ()))
    if "survey" in (s.get("operations") or ()) and kind == "pad":
        want.add("survey")
    if kind == "pad" and "strip" in want and _deep_fines(s):
        want.add("excavate")   # fines deeper than the m2 row's surface layer are dug in m3
    for flag in FLAGS:
        if isinstance(s.get(flag), bool):
            (want.add if s[flag] else want.discard)(flag)
    return [p for p in PHASES if p in want]


RECEIVING_KINDS: frozenset[str] = frozenset({"berm", "stockpile", "fill"})


def _deep_fines(s: dict[str, Any]) -> bool:
    """Whether the structure's fines are deeper than the surface layer an excavator m2 task
    strips (:data:`~moonwright.pipeline.rates.STRIP_DEPTH_M`), so they are dug in m3."""
    fines = _num(s, "fines_depth_m")
    return fines is not None and fines > STRIP_DEPTH_M


def fill_sources(s: dict[str, Any]) -> list[str]:
    raw = s.get("fill_from")
    return [raw] if isinstance(raw, str) else list(raw or [])


def structure_budget(s: dict[str, Any], fleet: set[str], zone: str,
                     default_fill_from: list[str], spoil_taken: bool = False) -> StructureBudget:
    """The phases of one structure spec. ``default_fill_from`` is the id of the first
    structure that excavates (the fill source assumed for a receiving kind without
    ``fill_from``); ``spoil_taken`` says another structure is built from this one's spoil, so
    the haul is counted there and not here (unless ``"haul": true`` insists)."""
    kind = kind_of(str(s["type"]))
    notes: list[str] = []
    area, area_basis = footprint(s, kind)
    lifts = int(_num(s, "lifts") or 1)
    fill_from = fill_sources(s)
    phases: list[Phase] = []

    def add(phase: str, quantity: float | None, basis: str) -> None:
        if quantity is None or quantity <= 0:
            notes.append(f"{phase}: no dimensions to size it (skipped)")
            return
        robot_type, unit = PHASE_TYPE[phase], PHASE_UNIT[phase]
        if robot_type not in fleet:
            notes.append(f"{phase}: no {robot_type} in the fleet (dropped, as the validator does)")
            return
        rate = rate_for(robot_type, unit)
        assert rate is not None
        phases.append(Phase(phase, robot_type, unit, round(quantity, 1), basis,
                            rate.duration_h(quantity), rate.energy_wh(quantity)))

    excavated = 0.0
    wanted = wanted_phases(s, kind, fleet)
    if (spoil_taken and kind not in RECEIVING_KINDS and s.get("haul") is not True
            and "haul" in wanted):
        wanted.remove("haul")
        notes.append("haul: its spoil is hauled by the structure built from it (counted there)")
    for phase in wanted:
        if phase in ("survey", "inspect"):
            add(phase, area, area_basis)
        elif phase == "strip":
            fines = _num(s, "fines_depth_m")
            if kind == "pad" and fines is not None and area is not None:
                if _deep_fines(s):
                    continue   # deeper than a surface layer: dug in m3 below
                add("strip", area, f"{area_basis} (fines {fmt(fines)} m deep: a surface layer)")
                excavated += area * fines
                continue
            add("strip", area, area_basis)
        elif phase == "excavate":
            fines = _num(s, "fines_depth_m")
            if fines is not None and area is not None and _deep_fines(s):
                add("excavate", area * fines, f"fines {area_basis} x {fmt(fines)} m")
                excavated += area * fines
            if kind == "berm":
                trench = _num(s, "trench_depth_m")
                length, width = _num(s, "length_m"), _num(s, "width_m")
                if trench is not None and length is not None:
                    base = width if width is not None else 2 * (_num(s, "height_m") or 0)
                    twidth = _num(s, "trench_width_m") or base
                    vol = length * twidth * trench
                    add("excavate", vol, f"key trench {fmt(length)} m x {fmt(twidth)} m x "
                                         f"{fmt(trench)} m")
                    excavated += vol
                else:
                    notes.append("excavate: a berm needs trench_depth_m to size a key trench")
            else:
                vol, basis = cut_volume(s, area, area_basis)
                depth = _num(s, "depth_m")
                shallow = (vol is not None and area is not None and depth is not None
                           and _num(s, "volume_m3") is None and depth <= STRIP_DEPTH_M)
                if vol is None and fines is None:
                    notes.append("excavate: no depth_m / volume_m3 to size the cut (skipped)")
                elif shallow and any(p.phase == "strip" for p in phases):
                    notes.append(f"excavate: {fmt(depth)} m is a surface layer, stripped above")
                elif shallow:
                    # no deeper than the m2 row's surface layer: stripped over the footprint
                    add("strip", area, f"{area_basis} ({fmt(depth)} m deep: a surface layer)")
                    excavated += vol
                elif vol is not None:
                    add("excavate", vol, basis)
                    excavated += vol
        elif phase == "haul":
            if kind in RECEIVING_KINDS:
                vol, basis = fill_volume(s, kind, area, area_basis)
                if vol is None and (fill_from or default_fill_from) and "hauler" in fleet:
                    notes.append("haul: fill volume taken from the source's excavation")
                    phases.append(Phase("haul", "hauler", "m3", 0.0, "source excavation", 0, 0.0))
                else:
                    add("haul", vol, f"fill {basis}")
            else:
                vol = excavated or _num(s, "fill_m3") or 0.0
                add("haul", vol, f"spoil of {fmt(vol)} m3 excavated here")
        elif phase == "compact":
            if kind == "berm" and area is None:
                notes.append("compact: a berm needs length_m and width_m or height_m")
            qty = (area or 0) * lifts
            add("compact", qty, area_basis + (f" x {lifts} lifts" if lifts > 1 else ""))
        elif phase == "sinter":
            if kind == "berm":
                crest = _num(s, "crest_width_m") or CREST_WIDTH_M
                length = _num(s, "length_m") or 0
                add("sinter", length * crest, f"crest {fmt(length)} m x {fmt(crest)} m")
            else:
                add("sinter", area, area_basis)
    if any(p.phase == "haul" for p in phases) and not fill_from and kind in RECEIVING_KINDS:
        fill_from = [sid for sid in default_fill_from if sid != s["id"]][:1]
        if fill_from:
            notes.append(f"haul waits for the excavation of {fill_from[0]} (first structure "
                         "that excavates; set fill_from to change)")
    return StructureBudget(str(s["id"]), str(s["type"]), kind, zone, phases, fill_from, notes,
                           area_m2=area)


# ----------------------------------------------------------------------------- budget
@dataclass
class Budget:
    source: str
    horizon_h: int
    robots: list[Robot]
    structures: list[StructureBudget]
    types: dict[str, TypeBudget]
    zones: dict[str, int]
    critical_path_h: int
    critical_path: list[str]
    serial_h: int
    lower_bound_h: int
    horizon_slack_pct: float
    verdict: Verdict
    assumptions: list[str]
    requirements: Requirements
    graphs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "horizon_h": self.horizon_h,
            "robots": [r.model_dump() for r in self.robots],
            "structures": [
                {"id": s.id, "type": s.type, "kind": s.kind, "zone": s.zone,
                 "fill_from": s.fill_from, "hours": s.hours, "energy_wh": s.energy_wh,
                 "phases": [asdict(p) for p in s.phases], "notes": s.notes}
                for s in self.structures
            ],
            "types": {k: asdict(v) for k, v in self.types.items()},
            "zones": self.zones,
            "critical_path_h": self.critical_path_h,
            "critical_path": self.critical_path,
            "serial_h": self.serial_h,
            "lower_bound_h": self.lower_bound_h,
            "horizon_slack_pct": self.horizon_slack_pct,
            "verdict": asdict(self.verdict),
            "graphs": self.graphs,
            "assumptions": self.assumptions,
        }


def best_packing(items: list[float], caps: list[float]) -> tuple[float, list[float], bool]:
    """``(ratio, loads, exact)``: the smallest achievable ``max(load / cap)`` over assignments
    of ``items`` to bins of ``caps`` (exact branch and bound up to
    :data:`MAX_EXACT_PACK_ITEMS` items, first-fit decreasing beyond that)."""
    if not caps:
        return math.inf, [], True
    order = sorted(items, reverse=True)
    n = len(caps)
    if len(order) > MAX_EXACT_PACK_ITEMS:
        loads = [0.0] * n
        for x in order:
            b = min(range(n), key=lambda i: (loads[i] + x) / caps[i])
            loads[b] += x
        return max(loads[i] / caps[i] for i in range(n)), loads, False
    loads = [0.0] * n
    best: list[Any] = [math.inf, loads.copy()]

    def rec(k: int) -> None:
        if k == len(order):
            ratio = max(loads[i] / caps[i] for i in range(n))
            if ratio < best[0]:
                best[0], best[1] = ratio, loads.copy()
            return
        x = order[k]
        seen: set[tuple[float, float]] = set()
        for b in range(n):
            key = (caps[b], loads[b])
            if key in seen:
                continue
            seen.add(key)
            if (loads[b] + x) / caps[b] >= best[0]:
                continue
            loads[b] += x
            rec(k + 1)
            loads[b] -= x

    rec(0)
    return best[0], best[1], True


def _type_budgets(robots: list[Robot], structures: list[StructureBudget]) -> dict[str, TypeBudget]:
    out: dict[str, TypeBudget] = {}
    for typ in sorted({r.type for r in robots} | {p.robot_type for s in structures for p in s.phases}):
        fleet = [r for r in robots if r.type == typ]
        tasks = [p for s in structures for p in s.phases if p.robot_type == typ]
        need = round(sum(p.energy_wh for p in tasks), 1)
        hours = sum(p.duration_h for p in tasks)
        caps = [r.battery_wh for r in fleet]
        fleet_wh = sum(caps)
        ratio, loads, exact = best_packing([p.energy_wh for p in tasks], caps)
        heaviest = max(range(len(caps)), key=lambda i: loads[i] / caps[i]) if caps else None
        even = need / len(fleet) if fleet and need else None
        out[typ] = TypeBudget(
            robot_type=typ, robots=len(fleet), fleet_wh=fleet_wh,
            min_battery_wh=min(caps) if caps else 0.0, need_wh=need, hours=hours,
            tasks=len(tasks),
            margin_pct=round((fleet_wh / need - 1) * 100, 1) if need else None,
            balance_pct=(round((min(caps) / even - 1) * 100, 1)
                         if even and len(fleet) > 1 else None),
            lower_bound_h=math.ceil(hours / len(fleet)) if fleet else hours,
            pack_feasible=bool(fleet) and ratio <= 1.0 + 1e-9,
            pack_max_load_wh=round(loads[heaviest], 1) if heaviest is not None else need,
            pack_max_load_pct=round(ratio * 100, 1) if fleet else math.inf,
            pack_exact=exact,
        )
    return out


def _bounds(structures: list[StructureBudget]) -> tuple[int, list[str], int, dict[str, int]]:
    """Critical path of the canonical phase order with the fill dependency (structures read
    sources first, :func:`fill_order`, so a berm the registry lists before its borrow pit
    still waits for it), the fully serial total, and the per-zone serial hours."""
    finish: dict[str, int] = {}          # "S1/excavate" -> earliest finish (longest path)
    via: dict[str, str | None] = {}
    excavation_end: dict[str, tuple[int, str]] = {}   # structure -> (finish, key) of its last cut
    for s in fill_order(structures):
        prev: str | None = None
        for i, p in enumerate(s.phases):
            key = f"{s.id}/{p.phase}" + (f"#{i}" if sum(q.phase == p.phase for q in s.phases) > 1 else "")
            start, pred = (finish[prev], prev) if prev is not None else (0, None)
            if p.phase == "haul":
                for src in s.fill_from:
                    if src in excavation_end and excavation_end[src][0] > start:
                        start, pred = excavation_end[src]
            finish[key] = start + p.duration_h
            via[key] = pred
            if p.phase in ("strip", "excavate"):   # a hardstand stripped in m2 feeds a berm too
                excavation_end[s.id] = max(excavation_end.get(s.id, (0, key)), (finish[key], key))
            prev = key
    if not finish:
        return 0, [], 0, {}
    end = max(finish, key=finish.__getitem__)
    path: list[str] = []
    node: str | None = end
    while node is not None:
        path.append(node)
        node = via[node]
    serial = sum(p.duration_h for s in structures for p in s.phases)
    zones: dict[str, int] = {}
    for s in structures:
        zones[s.zone] = zones.get(s.zone, 0) + s.hours
    return finish[end], path[::-1], serial, zones


def _verdict(horizon_h: int, types: dict[str, TypeBudget], lower_bound_h: int,
             serial_h: int, slack: float) -> Verdict:
    short: list[str] = []
    for t in types.values():
        if t.need_wh and not t.robots:
            short.append(f"no {t.robot_type} in the fleet for {fmt(t.need_wh)} Wh of work")
        elif t.need_wh > t.fleet_wh:
            gap = t.need_wh - t.fleet_wh
            short.append(f"{t.robot_type} energy short by {fmt(gap)} Wh "
                         f"({gap / t.fleet_wh * 100:.0f} % of its {fmt(t.fleet_wh)} Wh)")
    if lower_bound_h > horizon_h:
        short.append(f"horizon short by {lower_bound_h - horizon_h} h (phase-order bound "
                     f"{lower_bound_h} h vs {horizon_h} h)")
    if short:
        return Verdict("infeasible", f"infeasible without relaxation ({'; '.join(short)})", short)
    reasons: list[str] = []
    kinds: list[str] = []
    split_texts: list[str] = []
    for t in types.values():
        if t.robots > 1 and t.need_wh:
            thin = t.margin_pct is not None and t.margin_pct < ENERGY_COMFORT * 100
            if thin:
                split_texts.append(f"{t.robot_type} Wh fits only if work is balanced within "
                                   f"{t.balance_pct:.0f} %")
                reasons.append(f"{t.robot_type}: {fmt(t.need_wh)} of {fmt(t.fleet_wh)} Wh, so "
                               f"the heaviest robot may carry at most {t.balance_pct:.0f} % more "
                               f"than an even share")
            if not t.pack_feasible:
                if not thin:
                    split_texts.append(f"{t.robot_type} phases must be split (one task per "
                                       f"phase does not pack into {t.robots} x "
                                       f"{fmt(t.min_battery_wh)} Wh)")
                reasons.append(
                    f"{t.robot_type}: one task per phase cannot be packed into "
                    f"{t.robots} x {fmt(t.min_battery_wh)} Wh (heaviest robot would carry "
                    f"{fmt(t.pack_max_load_wh)} Wh = {t.pack_max_load_pct:.0f} %): expect an "
                    f"energy_task / energy core unless the work is split")
        elif t.robots == 1 and t.margin_pct is not None and t.margin_pct < TIGHT_SINGLE * 100:
            reasons.append(f"{t.robot_type} at {t.need_wh / t.fleet_wh * 100:.0f} % of its "
                           f"single battery (transit Wh counts too)")
    if split_texts:
        kinds.append("careful_split")
    if serial_h > horizon_h or slack < HORIZON_COMFORT:
        kinds.append("horizon_bound")
        reasons.append(f"deadline fits only with parallel zones: serial plan {serial_h} h vs "
                       f"{horizon_h} h horizon; parallel phase-order bound {lower_bound_h} h "
                       f"(slack {slack * 100:.0f} %)")
    if not kinds:
        return Verdict("comfortable", "comfortable", reasons)
    texts = []
    if split_texts:
        texts.append("needs a careful split (" + "; ".join(split_texts) + ")")
    if "horizon_bound" in kinds:
        texts.append(f"horizon-bound (deadline fits only with parallel zones: serial {serial_h} h "
                     f"vs horizon {horizon_h} h, parallel phase-order bound {lower_bound_h} h)")
    return Verdict("+".join(kinds), "; ".join(texts), reasons)


def compute_budget(spec: dict[str, Any], source: str = "spec") -> Budget:
    """The budget of a Requirements-shaped ``spec`` (``structures``, ``robots``,
    ``horizon_h``; optional ``site``)."""
    robots = [Robot.model_validate(r) for r in spec["robots"]]
    horizon_h = int(spec["horizon_h"])
    fleet = {r.type for r in robots}
    specs = [dict(s) for s in spec["structures"]]
    zones = _zones_for(spec, specs)
    excavators_first = [s["id"] for s in specs
                        if "excavate" in wanted_phases(s, kind_of(str(s["type"])), fleet)]
    receivers = {s["id"] for s in specs if kind_of(str(s["type"])) in RECEIVING_KINDS
                 and s.get("haul") is not False}   # pushed windrows haul nothing
    taken = {src for s in specs for src in fill_sources(s)}
    for s in specs:   # a receiving kind without fill_from takes the first excavation's spoil
        if s["id"] in receivers and not fill_sources(s):
            taken.update(sid for sid in excavators_first if sid != s["id"])
            break
    structures = [structure_budget(s, fleet, zones[s["id"]], excavators_first,
                                   spoil_taken=s["id"] in taken) for s in specs]
    _resolve_source_fill(structures)
    types = _type_budgets(robots, structures)
    cp_h, cp_path, serial_h, zone_hours = _bounds(structures)
    lower = max([cp_h, *(t.lower_bound_h for t in types.values()), *zone_hours.values()])
    slack = horizon_h / lower - 1 if lower else math.inf
    verdict = _verdict(horizon_h, types, lower, serial_h, slack)
    req = _requirements(spec, specs, robots, horizon_h, zones)
    for structure, budget in zip(req.structures, structures, strict=True):
        shape = dims_volume(structure)
        if shape is not None and shape.ranged:
            budget.notes.append(
                f"the brief allows {shape.how}: the validator's volume floor is "
                f"{qty(shape.floor_m3)} of {qty(shape.m3)} m3 ({shape.floor_how}), 70 % of "
                "which a plan must cover (95 % to count in scope); this budget prices the full "
                "size")
    assumptions = [
        ("rate table: duration_h = ceil(quantity / rate), energy_wh = quantity x Wh/unit "
         "(moonwright.pipeline.rates; illustrative lunar-rated figures, not measurements)"),
        ("bounds ignore transit (CP-SAT adds the whole hours and 0.5 Wh per cell of every leg "
         "a robot drives) and resource contention beyond one robot per zone"),
        ("one canonical task per phase per structure; a haul belongs to the receiving structure "
         "(its volume is the fill that structure needs) and waits for its source's excavation; "
         "spoil that another structure receives is not hauled twice"),
    ] + [f"{s.id}: {n}" for s in structures for n in s.notes]
    return Budget(source, horizon_h, robots, structures, types, zone_hours, cp_h, cp_path,
                  serial_h, lower, round(slack * 100, 1) if math.isfinite(slack) else math.inf,
                  verdict, assumptions, req)


def _resolve_source_fill(structures: list[StructureBudget]) -> None:
    """A haul whose volume was left to 'source excavation' takes the excavated volume of its
    ``fill_from`` structures."""
    by_id = {s.id: s for s in structures}
    for s in structures:
        for i, p in enumerate(s.phases):
            if p.phase == "haul" and p.basis == "source excavation":
                vol = sum(q.quantity for src in s.fill_from if src in by_id
                          for q in by_id[src].phases if q.phase in ("excavate", "strip")
                          and q.unit == "m3")
                rate = rate_for("hauler", "m3")
                assert rate is not None
                s.phases[i] = Phase("haul", "hauler", "m3", round(vol, 1),
                                    f"spoil of {', '.join(s.fill_from)}", rate.duration_h(vol),
                                    rate.energy_wh(vol))


def _zones_for(spec: dict[str, Any], specs: list[dict[str, Any]]) -> dict[str, str]:
    """Structure id -> zone id: the structure's own ``zone`` when given, else Z<n> in order."""
    zones: dict[str, str] = {}
    used = 0
    for s in specs:
        if isinstance(s.get("zone"), str) and s["zone"]:
            zones[s["id"]] = s["zone"]
        else:
            used += 1
            while f"Z{used}" in {z for z in zones.values()}:
                used += 1
            zones[s["id"]] = f"Z{used}"
            s["zone"] = zones[s["id"]]
    return zones


def _requirements(spec: dict[str, Any], specs: list[dict[str, Any]], robots: list[Robot],
                  horizon_h: int, zones: dict[str, str]) -> Requirements:
    """Requirements the validator and CP-SAT can run on: the spec's site when it has one
    (a fixture structure.json), else a default 40 x 40 site with the zones laid out by
    :func:`moonwright.geometry.place_zones` (deterministic, as the simulator would)."""
    if spec.get("site"):
        site = Site.model_validate(spec["site"])
        declared = {z.id for z in site.zones}
        missing = sorted({z for z in zones.values() if z not in declared})
        if missing:
            centres = place_zones(site, missing, {tuple(z.center) for z in site.zones})
            site = site.model_copy(update={"zones": site.zones + [
                Zone(id=z, center=centres[z], radius=min(ZONE_RADIUS, Site.max_zone_radius(site.grid)))
                for z in missing]})
    else:
        base = Site()
        ids = list(dict.fromkeys(zones.values()))
        centres = place_zones(base, ids, set())
        site = Site(zones=[Zone(id=z, center=centres[z], radius=ZONE_RADIUS) for z in ids])
    structures = []
    for s in specs:
        dims = {k: v for k, v in s.items() if k in _DIM_KEYS and isinstance(v, (int, float))
                and not isinstance(v, bool)}
        structures.append(Structure(id=s["id"], type=s["type"], zone=zones[s["id"]],
                                    operations=list(s.get("operations") or []), **dims))
    return Requirements(site=site, structures=structures, robots=robots, horizon_h=horizon_h,
                        constraints=list(spec.get("constraints") or []),
                        source_brief=spec.get("source_brief"))


# ----------------------------------------------------------------------------- graphs
_VERB = {"survey": "Survey and stake", "strip": "Strip and grade", "excavate": "Excavate",
         "haul": "Haul fill to", "compact": "Compact", "sinter": "Sinter", "inspect": "Inspect"}


def fill_order(structures: list[StructureBudget]) -> list[StructureBudget]:
    """The structures with every fill source before the structure built from it, registry
    order otherwise (a cycle of sources keeps registry order): the order the ``serial`` graph
    chains them in and :func:`_bounds` reads them in. The registry lists some receivers
    first (``shielding_mound``'s mound, ``small_fleet_long_berm``'s berm, the borrow pits of
    ``habitat_berm_tight_horizon`` and ``depleted_excavators``), and a haul must still wait
    for its source's dig."""
    ids = {s.id for s in structures}
    done: set[str] = set()
    pending = list(structures)
    out: list[StructureBudget] = []
    while pending:
        ready = next((s for s in pending if all(src in done or src not in ids or src == s.id
                                                for src in s.fill_from)), pending[0])
        pending.remove(ready)
        out.append(ready)
        done.add(ready.id)
    return out


def build_graph(budget: Budget, variant: str = "lazy") -> TaskGraph:
    """The canonical graph of a budget: ``lazy`` (one task per phase), ``split`` (physical
    phases above the prompt's threshold split into as many equal parts as the type has
    robots), ``fine`` (every physical phase split that way, whatever its size: the repair a
    battery core asks for), ``serial`` (lazy, every structure after the previous one, fill
    sources first: :func:`fill_order`), ``shrunk`` (lazy, every dig in m3 but a key trench
    planned as stripping the structure's footprint in m2: the live models' re-scope, a
    ``volume:<structure>`` conflict now). Ids follow the registry order; every haul waits for
    the excavation tasks of each structure its fill comes from, wherever the registry lists
    it, so every variant passes the validator's ``supply:<haul>`` check."""
    robots_of = {t: b.robots for t, b in budget.types.items()}
    specs: list[dict[str, Any]] = []              # the tasks' fields, in registry order
    first_of: dict[str, list[str]] = {}           # structure -> ids of its first phase
    last_of: dict[str, list[str]] = {}            # structure -> ids of its last phase
    excav_of: dict[str, list[str]] = {}           # structure -> ids of its excavation tasks
    hauls_of: dict[str, list[str]] = {}           # structure -> ids of its haul tasks
    n = 0
    for s in budget.structures:
        prev_ids: list[str] = []
        label = f"{s.id} {s.type.replace('_', ' ')}"
        for p in s.phases:
            n += 1
            if (variant == "shrunk" and p.phase == "excavate" and p.unit == "m3"
                    and s.area_m2 and not p.basis.startswith("key trench")):
                p = Phase("strip", p.robot_type, "m2", round(s.area_m2, 1),
                          f"footprint {fmt(s.area_m2)} m2 in place of the {fmt(p.quantity)} m3 dig",
                          1, 0.0)
            parts = 1
            threshold = 0.0 if variant == "fine" else SPLIT_THRESHOLD.get(p.unit, math.inf)
            if variant in ("split", "fine") and p.phase in PHYSICAL and p.quantity > threshold:
                parts = max(1, robots_of.get(p.robot_type, 1))
            after = list(prev_ids)
            ids: list[str] = []
            for k in range(parts):
                tid = f"T{n}" if parts == 1 else f"T{n}.{k + 1}"
                share = p.quantity / parts
                verb = _VERB[p.phase]
                if p.phase == "excavate" and p.basis.startswith("key trench"):
                    verb = "Excavate key trench of"
                elif p.phase == "excavate" and p.basis.startswith("fines"):
                    verb = "Strip fines of"            # grade by name, excavate by unit
                name = f"{verb} {label}" + (f" (part {k + 1} of {parts})" if parts > 1 else "")
                specs.append({
                    "id": tid, "name": name, "robot_type": p.robot_type, "duration_h": 1,
                    "energy_wh": 0.0, "zone": s.zone, "after": list(after), "structure": s.id,
                    "quantity": round(share, 2), "unit": p.unit,
                    "basis": p.basis + (f" / {parts}" if parts > 1 else "")})
                ids.append(tid)
            if p.phase in ("excavate", "strip"):   # a strip in m2 is a source too
                excav_of.setdefault(s.id, []).extend(ids)
            if p.phase == "haul":
                hauls_of.setdefault(s.id, []).extend(ids)
            first_of.setdefault(s.id, ids)
            prev_ids = ids
        last_of[s.id] = prev_ids
    # cross-structure edges, wherever the registry lists the structures: a haul waits for
    # the excavation of every structure its fill comes from, and the serial graph chains the
    # structures sources first
    extra: dict[str, list[str]] = {}
    for s in budget.structures:
        feed = [i for src in s.fill_from for i in excav_of.get(src, [])]
        for tid in hauls_of.get(s.id, []):
            extra.setdefault(tid, []).extend(feed)
    if variant == "serial":
        chain = [s for s in fill_order(budget.structures) if s.id in first_of]
        for a, b in itertools.pairwise(chain):
            for tid in first_of[b.id]:
                extra.setdefault(tid, []).extend(last_of[a.id])
    tasks = [Task(**{**spec, "after": list(dict.fromkeys([*spec["after"],
                                                           *extra.get(spec["id"], [])]))})
             for spec in specs]
    graph, _ = apply_rates(TaskGraph(tasks=tasks, notes=f"canonical {variant} graph"))
    return graph


def evaluate_graph(req: Requirements, graph: TaskGraph, time_limit_s: float = 10.0) -> dict[str, Any]:
    """Validator verdict then CP-SAT verdict on ``graph``: the outcome the pipeline would
    reach with this graph as the model's answer."""
    out: dict[str, Any] = {"tasks": len(graph.tasks),
                           "hours": sum(t.duration_h for t in graph.tasks),
                           "energy_wh": round(sum(t.energy_wh for t in graph.tasks), 1)}
    failure = validate_graph(req, graph, coverage=True)
    if failure is not None:
        out.update(stage="validate", ok=False, conflicts=failure.conflicts, hint=failure.hint)
        return out
    result = solve_schedule(req, graph, SolveOptions(time_limit_s=time_limit_s))
    if isinstance(result, Failure):
        out.update(stage="schedule", ok=False, kind=result.kind, conflicts=result.conflicts,
                   hint=result.hint)
    else:
        assert isinstance(result, Schedule)
        out.update(stage="schedule", ok=True, makespan_h=result.makespan_h,
                   status=result.status, solve_time_s=round(result.solve_time_s, 2))
    return out


# ----------------------------------------------------------------------------- inputs
_FLEET_RE = re.compile(
    r"((?:[A-Z]{2,3}\d+)(?:(?:,\s*|,?\s+and\s+)[A-Z]{2,3}\d+)*)\s*\(([a-z]+?)s?,\s*([\d,]+)\s*Wh")
_ID_RE = re.compile(r"[A-Z]{2,3}\d+")
_DEADLINE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|hours?|h)\b")


def parse_brief(text: str) -> tuple[list[dict[str, Any]], int]:
    """Fleet (``[{id, type, battery_wh}]``) and horizon hours from a brief's **Fleet.** and
    **Deadline.** paragraphs. Raises ``ValueError`` when either is missing."""
    robots: list[dict[str, Any]] = []
    for m in _FLEET_RE.finditer(_paragraph(text, "Fleet") or text):
        wh = float(m.group(3).replace(",", ""))
        for rid in _ID_RE.findall(m.group(1)):
            robots.append({"id": rid, "type": m.group(2), "battery_wh": wh})
    if not robots:
        raise ValueError("no fleet found: expected 'EX1 (excavator, 5000 Wh)' style entries")
    deadline = _paragraph(text, "Deadline")
    m = _DEADLINE_RE.search(deadline or "")
    if m is None:
        raise ValueError("no deadline found: expected '**Deadline.** N days' or 'N hours'")
    amount = float(m.group(1))
    horizon_h = round(amount * 24) if m.group(2).startswith("d") else round(amount)
    return robots, horizon_h


def _paragraph(text: str, heading: str) -> str | None:
    """The paragraph that starts with ``**<heading>.**`` (or ``<heading>.``)."""
    pattern = re.compile(rf"^\W*{heading}\.\**\s*(.*?)(?:\n\s*\n|\Z)", re.DOTALL | re.MULTILINE | re.IGNORECASE)
    m = pattern.search(text)
    return m.group(1) if m else None


def spec_from_brief(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    robots, horizon_h = parse_brief(text)
    if path.stem not in BRIEF_STRUCTURES:
        raise ValueError(f"{path.name}: no structures registered under BRIEF_STRUCTURES[{path.stem!r}]")
    return {"structures": [dict(s) for s in BRIEF_STRUCTURES[path.stem]], "robots": robots,
            "horizon_h": horizon_h, "source_brief": text}


def spec_from_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "requirements" in data and "robots" not in data:
        data = data["requirements"]                        # a plan.json
    req = Requirements.model_validate(data)                # a fixture structure.json
    spec = req.model_dump(exclude_none=True)
    spec["structures"] = [dict(s.model_dump(exclude_none=True)) for s in req.structures]
    return spec


def load_spec(source: str | None, inline: str | None) -> tuple[dict[str, Any], str]:
    if inline:
        raw = Path(inline[1:]).read_text(encoding="utf-8") if inline.startswith("@") else inline
        return json.loads(raw), inline[:40] if not inline.startswith("@") else inline[1:]
    if not source or source == "-":
        raise ValueError("give a SOURCE (brief .md, Requirements / plan .json) or --spec")
    path = Path(source)
    if path.suffix == ".json":
        return spec_from_json(path), str(path)
    return spec_from_brief(path), str(path)


def apply_overrides(spec: dict[str, Any], overrides: Iterable[str]) -> None:
    """``S1.depth_m=0.5``, ``EX1.battery_wh=1800``, ``horizon_h=72`` applied in place."""
    for item in overrides:
        key, _, raw = item.partition("=")
        value = _literal(raw)
        if "." not in key:
            spec[key] = value
            continue
        owner, _, attr = key.partition(".")
        for group in ("structures", "robots"):
            for entry in spec.get(group, []):
                if entry.get("id") == owner:
                    entry[attr] = value
                    break
            else:
                continue
            break
        else:
            raise ValueError(f"--set {item}: no structure or robot with id {owner!r}")


def _literal(raw: str) -> Any:
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw) if re.fullmatch(r"-?\d+", raw.strip()) else float(raw)
    except ValueError:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw


# ----------------------------------------------------------------------------- reporting
def summary_line(b: Budget) -> str:
    name = Path(b.source).stem if b.source != "spec" else "spec"
    parts = [f"{t.robot_type[:2].upper()} {fmt(t.need_wh)}/{fmt(t.fleet_wh)}"
             + (f" ({t.margin_pct:+.0f}%)" if t.margin_pct is not None else "")
             for t in b.types.values() if t.need_wh]
    graphs = ", ".join(f"{k}: {_graph_outcome(v)}" for k, v in b.graphs.items())
    return (f"{name:30} H {b.horizon_h:>4} h  LB {b.lower_bound_h:>3} h  serial {b.serial_h:>3} h  "
            f"| {'  '.join(parts)} | {b.verdict.text}" + (f" | {graphs}" if graphs else ""))


def _graph_outcome(g: dict[str, Any]) -> str:
    if g.get("ok"):
        return f"feasible {g['makespan_h']} h"
    conflicts = g.get("conflicts", [])
    return f"{g.get('stage')} {', '.join(conflicts[:4])}{'...' if len(conflicts) > 4 else ''}"


def format_budget(b: Budget) -> str:
    lines = [f"brief: {b.source}   horizon {b.horizon_h} h   fleet: "
             + ", ".join(f"{r.id} {r.type} {fmt(r.battery_wh)} Wh" for r in b.robots), ""]
    lines.append(f"{'structure':<34}{'phase':<10}{'robot':<11}{'quantity':>12}  {'h':>4}  {'Wh':>8}  basis")
    for s in b.structures:
        head = f"{s.id} {s.type} [{s.kind}] zone {s.zone}"
        if s.fill_from:
            head += f" fill from {', '.join(s.fill_from)}"
        lines.append(head)
        for p in s.phases:
            lines.append(f"{'':<34}{p.phase:<10}{p.robot_type:<11}{fmt(p.quantity):>9} {p.unit:<2}  "
                         f"{p.duration_h:>4}  {fmt(p.energy_wh):>8}  {p.basis}")
        lines.append(f"{'':<34}{'total':<10}{'':<11}{'':>12}  {s.hours:>4}  {fmt(s.energy_wh):>8}")
        for note in s.notes:
            lines.append(f"{'':<34}note: {note}")
    lines.append("")
    lines.append(f"{'robot type':<11}{'robots':>6}  {'Wh need / fleet':>18}  {'margin':>7}  "
                 f"{'balance':>7}  {'hours':>5}  {'LB h':>4}  one-task-per-phase packing")
    for t in b.types.values():
        if not t.need_wh and not t.robots:
            continue
        margin = f"{t.margin_pct:+.0f} %" if t.margin_pct is not None else "-"
        balance = f"{t.balance_pct:.0f} %" if t.balance_pct is not None else "-"
        if not t.robots:
            pack = "no robot of this type"
        elif t.pack_feasible:
            pack = f"fits (heaviest {fmt(t.pack_max_load_wh)} Wh = {t.pack_max_load_pct:.0f} %)"
        else:
            pack = f"FAILS (heaviest {fmt(t.pack_max_load_wh)} Wh = {t.pack_max_load_pct:.0f} %)"
        if not t.pack_exact:
            pack += " [first-fit bound]"
        lines.append(f"{t.robot_type:<11}{t.robots:>6}  {fmt(t.need_wh):>8} / {fmt(t.fleet_wh):<7}  "
                     f"{margin:>7}  {balance:>7}  {t.hours:>5}  {t.lower_bound_h:>4}  {pack}")
    lines.append("")
    zones = ", ".join(f"{z} {h} h" for z, h in b.zones.items())
    lines.append(f"zones (serial hours, one robot per zone): {zones}")
    lines.append(f"critical path {b.critical_path_h} h: {' -> '.join(b.critical_path)}")
    slack = f"{b.horizon_slack_pct:+.0f} %" if math.isfinite(b.horizon_slack_pct) else "n/a"
    lines.append(f"phase-order bound {b.lower_bound_h} h (max of critical path, per-type hours / "
                 f"robots, zone serial)   serial plan {b.serial_h} h   horizon {b.horizon_h} h "
                 f"(slack vs that bound {slack})")
    lines.append("")
    lines.append(f"verdict: {b.verdict.text}")
    for r in b.verdict.reasons:
        lines.append(f"  - {r}")
    if b.graphs:
        lines.append("")
        lines.append("canonical graphs through the real validator and CP-SAT:")
        for name, g in b.graphs.items():
            lines.append(f"  {name:<7} {g['tasks']:>2} tasks, {g['hours']} h, {fmt(g['energy_wh'])} Wh "
                         f"-> {_graph_outcome(g)}")
            if not g.get("ok") and g.get("hint"):
                lines.append(f"          {g['hint'][:240]}")
    lines.append("")
    lines.append("assumptions:")
    for a in b.assumptions:
        lines.append(f"  - {a}")
    return "\n".join(lines)


def markdown_row(b: Budget) -> str:
    name = Path(b.source).stem
    fleet = ", ".join(f"{t.robots} {t.robot_type}" for t in b.types.values() if t.robots)
    margins = "; ".join(
        f"{t.robot_type} {fmt(t.need_wh)}/{fmt(t.fleet_wh)} Wh ({t.margin_pct:+.0f} %)"
        for t in b.types.values() if t.need_wh and t.robot_type != "surveyor")
    graphs = "; ".join(f"{k} {_graph_outcome(v)}" for k, v in b.graphs.items())
    return (f"| `{name}` | {fleet} | {b.horizon_h} h | LB {b.lower_bound_h} h, serial {b.serial_h} h | "
            f"{margins} | {b.verdict.text} | {graphs} |")


# ----------------------------------------------------------------------------- main
def run(spec: dict[str, Any], source: str, graphs: list[str], time_limit_s: float) -> Budget:
    budget = compute_budget(spec, source)
    for variant in graphs:
        graph = build_graph(budget, variant)
        budget.graphs[variant] = evaluate_graph(budget.requirements, graph, time_limit_s)
    return budget


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="brief .md, Requirements / plan .json, or '-'")
    ap.add_argument("--spec", help="inline Requirements-shaped JSON, or @path")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override before computing: S1.depth_m=0.5, EX1.battery_wh=1800, horizon_h=72")
    ap.add_argument("--graphs", default="lazy,split",
                    help="canonical graphs to run through validator + CP-SAT: lazy, split, fine, "
                         "serial (comma-separated; '' for none)")
    ap.add_argument("--no-solve", action="store_true", help="skip the graphs")
    ap.add_argument("--time-limit", type=float, default=10.0, help="CP-SAT seconds per graph")
    ap.add_argument("--all", metavar="DIR", help="budget every *.md brief in DIR, one line each")
    ap.add_argument("--markdown", action="store_true", help="with --all: rows for docs/eval-briefs.md")
    ap.add_argument("--json", action="store_true", help="print the budget as JSON")
    ap.add_argument("--list", action="store_true", help="list the registered brief ids")
    args = ap.parse_args(argv)
    graphs = [] if args.no_solve else [g for g in args.graphs.split(",") if g]
    for g in graphs:
        if g not in GRAPH_VARIANTS:
            ap.error(f"unknown graph variant {g!r}")
    if args.list:
        print("\n".join(sorted(BRIEF_STRUCTURES)))
        return 0
    if args.all:
        paths = sorted(p for p in Path(args.all).iterdir() if p.suffix == ".md")
        budgets = []
        for path in paths:
            spec = spec_from_brief(path)
            apply_overrides(spec, args.set)
            budgets.append(run(spec, str(path), graphs, args.time_limit))
            if not args.json and not args.markdown:
                print(summary_line(budgets[-1]), flush=True)
        if args.json:
            print(json.dumps([b.to_dict() for b in budgets], indent=1))
        elif args.markdown:
            print("| brief | fleet | horizon | bounds | per-type Wh (margin) | verdict | canonical graphs |")
            print("| --- | --- | --- | --- | --- | --- | --- |")
            for b in budgets:
                print(markdown_row(b))
        return 0
    spec, source = load_spec(args.source, args.spec)
    apply_overrides(spec, args.set)
    budget = run(spec, source, graphs, args.time_limit)
    print(json.dumps(budget.to_dict(), indent=1) if args.json else format_budget(budget))
    return 0


if __name__ == "__main__":
    sys.exit(main())
