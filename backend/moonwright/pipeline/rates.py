"""Deterministic rate model: a task's quantity decides its duration and energy (phase 3a).

Why
---
Asked for durations and energies directly, the planning model sizes them to the batteries it
is told about ("excavate the full pad bowl: 10 h, 1000 Wh" for 190 m3), so nothing is ever
infeasible and nothing can be checked. Under this module the model proposes WHAT and HOW MUCH
(``Task.quantity`` + ``Task.unit`` + ``Task.basis``) and the rate table decides HOW LONG and
HOW MUCH ENERGY::

    duration_h = max(1, ceil(quantity / rate))       whole hours, the planner's unit
    energy_wh  = round(quantity * wh_per_unit, 1)

so an under-scoped plan becomes a real validator / CP-SAT conflict, and a repair has to split
or re-scope work instead of editing numbers.

Rules of :func:`apply_rates`
----------------------------
* A task with ``quantity`` and ``unit`` and a known ``(robot_type, unit)`` rate gets the
  computed numbers; the model's own figures move to ``estimated_duration_h`` /
  ``estimated_energy_wh`` (kept for the UI).
* A task without a quantity keeps the model's ``duration_h``; its ``energy_wh`` becomes at
  least ``duration_h * BASE_POWER_W[robot_type]`` (a robot draws its base power while on
  task), the model's figure moving to ``estimated_energy_wh`` when it was raised.
* An unknown ``(robot_type, unit)`` pair (a crane, a hauler counted in m2) keeps the model's
  numbers untouched.
* A graph with no quantity at all (the hand-made fixtures, a solver-only replan, any graph
  produced before the rate model) is returned unchanged: the rate model has no standing over
  numbers it did not derive, so the base-power floor only applies once the model has engaged
  with quantities (:func:`is_engaged`). The graph the model was handed (``keep``) counts: an
  answer that drops every quantity of a quantified graph does not leave the rate model behind.
* ``keep`` is the graph the model was handed: the remaining graph of a replan, or the graph a
  repair answered. A task of ``keep`` that the model returned with the same robot_type,
  quantity, unit, duration_h and energy_wh is left as it is (it *is* the outstanding work;
  there is nothing to re-derive, and no base-power floor is applied to it). A task of ``keep``
  returned without a quantity inherits the quantity and unit it had: the model dropped the
  scope statement, not the work. Every other task is grounded afresh from its quantity, which
  is exactly right for work that has not started: editing duration_h or energy_wh while
  keeping the quantity changes nothing (the advisory names the attempt).
* ``locked`` are the ids of the tasks underway on a robot (a replan passes its locked
  entries). A locked task of ``keep`` that kept its robot_type, quantity and unit is left
  exactly as the model returned it whatever its numbers: its remaining hours and Wh are owned
  by the simulator and the scope guard (``locked:<task>`` refuses a cut), and recomputing them
  from the full quantity would schedule the finished part again.
* Pure: no I/O, no randomness; applying it twice gives the same graph.

Advisories
----------
``apply_rates`` also returns one string per computed task whose model estimate was off by more
than :data:`ADVISORY_THRESHOLD` (50 %) in duration or energy, quoting the larger deviation::

    estimate:T3: model said 10 h / 1000 Wh, rate model gives 27 h / 3000 Wh (+200%)

They are warnings for the UI (``LoopStep.payload["advisories"]`` of the taskgraph / repair
step), never conflicts: the computed numbers are what gets scheduled either way. The task-graph
stage adds :data:`UNGROUNDED_ADVISORY` when an answer carries no quantity at all, so a plan the
rate model never touched says so in its trace.

Volumes (phase 3e)
------------------
A structure's dimensions imply a volume (:func:`dims_volumes`: a bowl or a box is dug, a berm
prism is placed, a key trench under it is dug) and the plan covers some of it
(:func:`volume_coverages`): the ``m3`` of the structure's excavator digs on a dug shape, or of
its hauls on a placed one, plus the *stripping credit* of excavator ``m2`` tasks on a dug
shape, area x min(depth, :data:`STRIP_DEPTH_M`), the areas summed and capped at the
footprint (the rate table's m2 row is a surface layer up to 0.2 m, so a 10 x 10 x 0.15 m pad
"stripped" as 100 m2 is fully covered, and a 100 x 6 x 0.6 m borrow strip planned as 600 m2
is covered 120 of 360 m3). A plan below :data:`VOLUME_LOW` of the smallest volume the brief
allows is a shortfall: an advisory here (:func:`volume_advisories`) and, on a whole plan,
the validator's ``volume:<structure>`` conflict (:func:`volume_shortfalls`); a plan above
:data:`VOLUME_HIGH` of the largest ("hauled twice over") stays an advisory. What the review
of phase 3e made exact:

* Only what the brief states is judged. The floor comes from ``min_<dimension>_m`` extras
  (a brief that lets the work shrink); a bowl or box whose walls slope (:func:`wall_slope`:
  an extra, the structure's own entry in the brief, a constraint) is judged between its
  size read at the rim and at the floor; a key trench needs its ``trench_width_m`` (no
  base-width guess) and a berm without a base width holds at least its 1:1 section.
* The credit cannot stand in for the dig: it is capped at the footprint of the smallest
  size the brief allows, an m2 pass after a dig of the shape (a levelling pass) earns none
  unless it is named a strip, and below the stripped layer only digs in m3 count, to
  :data:`VOLUME_LOW` of that layer (a 20 m pad as a 12 m dig plus a full-footprint levelling
  pass, or kind E's bowl as a 1 m3 test pit under a strip, are shortfalls).
* Spoil moved is no dig (:func:`rehandles`: bladed into a windrow, backfilled): it counts
  neither way, and the fill of a stockpile-like structure (:func:`stockpile_like`) is judged
  neither way.
* A placed body whose hauls wait for the excavation of other structures is judged against
  the smaller of its own floor and the floor dug there: a berm "built from pad spoil" is
  covered by all of that spoil even when its prism would hold more.

The eval's "in scope" is stricter than the conflict: every judged volume and every area of
compaction and sintering (:func:`area_coverages`: footprint x :func:`lifts`) at
:data:`IN_SCOPE_SHARE` or more (:func:`scope_gaps`), so a plan that verified after trimming
the work 15-25 % is a re-scope, not a success in scope; an excess is no gap.

Calibration
-----------
Illustrative "lunar-rated low-power robot" units, chosen so the three example briefs come out
at the hand-made fixture figures (126 m3 pad bowl -> 18 h / ~2000 Wh; 314 m2 strip -> 13 h /
~1500 Wh; 188 m3 spoil haul -> 16 h / ~1300 Wh; 314 m2 compaction -> 13 h / ~1300 Wh; 314 m2
sinter -> 23 h / ~2700 Wh). The regolith-excavation literature (NASA's RASSOR bucket-drum
excavator and Chariot/LANCE blade tests, ISRU excavation surveys) is the inspiration for the
ORDER of magnitude (tens of kilograms per scoop, kilowatt-hour-class budgets per shift), but
every number here is an assumption, not a measurement, and the table says so row by row.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from moonwright.pipeline.tags import WORDS, entry_line, name_score, serving_tasks
from moonwright.schemas import (
    MAX_DURATION_H,
    MAX_ENERGY_WH,
    Requirements,
    Structure,
    Task,
    TaskGraph,
    Unit,
    to_mwh,
)
from moonwright.text import join_and, pct, qty

ADVISORY_THRESHOLD = 0.5   # relative deviation of the model's estimate that earns an advisory
VOLUME_LOW = 0.7           # volume:<structure> when the planned m3 are below this share of the
VOLUME_HIGH = 1.5          # smallest volume the brief allows (a conflict), or above this
#                            multiple of the dimensions' volume (an advisory)
_CEIL_EPS = 1e-9           # 2.1 / 0.7 is 3.0000000000000004 in floats: do not ceil that to 4
UNGROUNDED_ADVISORY = (
    "ungrounded: no task states a quantity, so every duration_h and energy_wh is the model's "
    "own estimate and the rate table was not applied"
)


@dataclass(frozen=True)
class Rate:
    """One row of the rate table: ``per_h`` units of work per hour at ``wh_per_unit`` Wh."""

    robot_type: str
    unit: Unit
    per_h: float
    wh_per_unit: float
    work: str   # what the row covers
    note: str   # source / assumption behind the numbers

    def duration_h(self, quantity: float) -> int:
        """``max(1, ceil(quantity / per_h))`` in whole hours, capped at ``MAX_DURATION_H``."""
        hours = math.ceil(quantity / self.per_h - _CEIL_EPS)
        return max(1, min(MAX_DURATION_H, hours))

    def energy_wh(self, quantity: float) -> float:
        """``quantity * wh_per_unit`` to 0.1 Wh, capped at ``MAX_ENERGY_WH``."""
        return min(MAX_ENERGY_WH, round(quantity * self.wh_per_unit, 1))


# Illustrative lunar-rated low-power robot units, calibrated to the example briefs (module
# docstring). Every row is an assumption; the literature named is inspiration for the order of
# magnitude only.
RATES: tuple[Rate, ...] = (
    Rate("excavator", "m3", 7.0, 16.0, "bulk excavation (bowls, pits, trenches)",
         "assumption calibrated to the landing_pad fixture: 126 m3 bowl = 18 h / ~2000 Wh; a "
         "RASSOR-class bucket drum moves ~0.1 m3 per pass, so 7 m3/h is a lunar-rated "
         "low-power figure, not a terrestrial one"),
    Rate("excavator", "m2", 25.0, 4.8,
         "strip or grade a surface layer up to 0.2 m; deeper cuts are excavation in m3",
         "assumption: 314 m2 pad strip = 13 h / ~1500 Wh (blade pass at ~0.1 m depth, "
         "Chariot/LANCE-style blade work scaled down); the volume check credits an m2 task "
         "on a dug shape with its area x min(depth, STRIP_DEPTH_M)"),
    Rate("hauler", "m3", 12.0, 7.0, "haul spoil or fill between zones",
         "assumption: 188 m3 of bowl spoil to the berm line = 16 h / ~1300 Wh; short in-site "
         "hauls, ~0.5 m3 bins"),
    Rate("compactor", "m2", 26.0, 4.0, "compact subgrade, berm lifts, road base",
         "assumption: 314 m2 pad subgrade = 13 h / ~1300 Wh (several vibratory passes)"),
    Rate("printer", "m2", 14.0, 8.6, "sinter / print a regolith surface",
         "assumption: 314 m2 pad surface = 23 h / ~2700 Wh; sintering is the slow, "
         "energy-dense step of every regolith-construction study"),
    Rate("surveyor", "m2", 150.0, 0.5, "stake out, scan, inspect an area",
         "assumption: 314 m2 pad = 3 h / ~160 Wh; a pure-time survey (no quantity) keeps the "
         "model's hours"),
)

# Power a robot draws while on task whatever it does (W): the floor for tasks without a
# quantity. Assumptions consistent with the Wh budgets of the briefs.
BASE_POWER_W: dict[str, float] = {
    "excavator": 110.0,
    "hauler": 80.0,
    "compactor": 105.0,
    "printer": 120.0,
    "surveyor": 75.0,
}

_RATE_INDEX: dict[tuple[str, str], Rate] = {(r.robot_type, r.unit): r for r in RATES}
RATED_TYPES: frozenset[str] = frozenset(r.robot_type for r in RATES)


def rate_for(robot_type: str, unit: str | None) -> Rate | None:
    """The rate row for ``(robot_type, unit)``, or None when the table has no such row."""
    if unit is None:
        return None
    return _RATE_INDEX.get((robot_type, unit))


def rated_units(robot_type: str) -> list[str]:
    """Units the table prices for ``robot_type`` (table order), empty for an unrated type."""
    return [r.unit for r in RATES if r.robot_type == robot_type]


def scoped(task: Task) -> bool:
    """Whether a task states its scope (a quantity with a unit)."""
    return task.quantity is not None and task.unit is not None


def is_engaged(graph: TaskGraph, keep: TaskGraph | None = None) -> bool:
    """Whether the rate model has standing over ``graph``: some task of it, or of the graph
    the model was handed (``keep``), states a quantity and unit."""
    return any(scoped(t) for t in graph.tasks) or (
        keep is not None and any(scoped(t) for t in keep.tasks))


def _fmt(x: float) -> str:
    """1300.0 -> '1300', 1316.4 -> '1316.4' (the estimate advisories' figures)."""
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _rate_table_text() -> str:
    rows = [("robot type", "unit", "rate", "energy", "covers")]
    rows += [(r.robot_type, r.unit, f"{_fmt(r.per_h)} {r.unit}/h",
              f"{_fmt(r.wh_per_unit)} Wh/{r.unit}", r.work) for r in RATES]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = ["  ".join(cell.ljust(widths[i]) if i < 4 else cell for i, cell in enumerate(row))
             for row in rows]
    base = ", ".join(f"{typ} {_fmt(w)} W" for typ, w in BASE_POWER_W.items())
    return "\n".join([
        *lines,
        "duration_h = ceil(quantity / rate), at least 1 h; energy_wh = quantity x Wh/unit.",
        f"Base power while on task, charged to tasks without a quantity: {base}.",
        ("Illustrative lunar-rated low-power robot units calibrated to the example briefs; "
         "assumptions, not measurements."),
    ])


RATE_TABLE_TEXT = _rate_table_text()


# ----------------------------------------------------------------------------- apply
def apply_rates(graph: TaskGraph, keep: TaskGraph | None = None,
                locked: Collection[str] = ()) -> tuple[TaskGraph, list[str]]:
    """The graph with durations and energies decided by the rate table, plus advisories.

    See the module docstring for the rules. ``keep`` is the graph the model was handed (the
    remaining graph of a replan, the previous graph of a repair) and ``locked`` the ids of the
    tasks underway on a robot: a task returned exactly as handed over is left alone, a locked
    task is left alone whatever its numbers, anything else is grounded from its quantity.
    """
    if not is_engaged(graph, keep):
        return graph, []
    kept = {t.id: t for t in keep.tasks} if keep is not None else {}
    pinned = set(locked)
    tasks: list[Task] = []
    advisories: list[str] = []
    for t in graph.tasks:
        k = kept.get(t.id)
        if k is not None and k.robot_type == t.robot_type:
            if t.quantity is None and k.quantity is not None:
                # the scope statement was dropped, not the work: it keeps the scope it had
                t = t.model_copy(update={"quantity": k.quantity, "unit": k.unit,
                                         "basis": k.basis if t.basis is None else t.basis})
            same_scope = (k.quantity, k.unit) == (t.quantity, t.unit)
            as_handed = (same_scope and k.duration_h == t.duration_h
                         and to_mwh(k.energy_wh) == to_mwh(t.energy_wh))
            if same_scope and (as_handed or t.id in pinned):
                tasks.append(t)   # outstanding work: the simulator / scope guard own the numbers
                continue
        rate = rate_for(t.robot_type, t.unit)
        if t.quantity is not None and rate is not None:
            grounded = t.model_copy(update={
                "duration_h": rate.duration_h(t.quantity),
                "energy_wh": rate.energy_wh(t.quantity),
                "estimated_duration_h": float(t.duration_h),
                "estimated_energy_wh": float(t.energy_wh),
            })
            tasks.append(grounded)
            advisory = _advisory(t, grounded)
            if advisory:
                advisories.append(advisory)
            continue
        if t.quantity is None:
            floor = min(MAX_ENERGY_WH, t.duration_h * BASE_POWER_W.get(t.robot_type, 0.0))
            if floor > t.energy_wh:
                tasks.append(t.model_copy(update={"energy_wh": float(floor),
                                                  "estimated_energy_wh": float(t.energy_wh)}))
                continue
        tasks.append(t)   # unknown (robot_type, unit) pair, or nothing to raise
    return graph.model_copy(update={"tasks": tasks}), advisories


def _deviation(model: float, computed: float) -> float:
    """Relative deviation of the computed figure from the model's (inf when the model said 0
    and the rate model does not)."""
    if model > 0:
        return (computed - model) / model
    return math.inf if computed > 0 else 0.0


def _advisory(model: Task, grounded: Task) -> str | None:
    d_dur = _deviation(model.duration_h, grounded.duration_h)
    d_en = _deviation(model.energy_wh, grounded.energy_wh)
    worst = max(d_dur, d_en, key=abs)
    if abs(worst) <= ADVISORY_THRESHOLD:
        return None
    pct = "model said 0" if math.isinf(worst) else f"{worst:+.0%}"
    return (f"estimate:{model.id}: model said {model.duration_h} h / {_fmt(model.energy_wh)} Wh, "
            f"rate model gives {grounded.duration_h} h / {_fmt(grounded.energy_wh)} Wh ({pct})")


# ----------------------------------------------------------------------- volumes
STRIP_DEPTH_M = 0.2        # an excavator m2 task on a dug shape strips at most this depth
SPLIT_ABOVE_M3 = 60.0      # the task-graph prompt's split rule: m3 work above this is split
IN_SCOPE_SHARE = 0.95      # the eval's "in scope": every judged volume and area at least this
#                            share of the smallest size the brief allows (module docstring)
_MIN_KEYS = ("diameter_m", "depth_m", "length_m", "width_m", "height_m")
# a stockpile is whatever gets hauled: its planned fill is never a shortfall nor an excess. A
# berm made of spoil ("spoil_berm", the structure prompt's own example) is a designed body.
_STOCKPILE_RE = re.compile(r"stockpile|spoil|pile|depot|dump|heap", re.IGNORECASE)
# an excavator task that moves spoil already dug (blade it into a windrow, push it, backfill a
# trench) digs nothing: it counts neither for nor against a dug shape. A name that also says
# it digs ("excavate and blade the spoil") is a dig.
_REHANDLE_RE = re.compile(r"blad|push|windrow|stockpil|backfill|spread|rehandl", re.IGNORECASE)
_DIG_RE = re.compile(r"excavat|\bdig", re.IGNORECASE)
_STRIP_RE = re.compile(r"strip", re.IGNORECASE)
# sloped walls of a dug bowl or box: "with a 1:1 sloped wall", "2:1 side slopes", "walls
# battered at 45 degrees"
_SLOPE_WORD_RE = re.compile(r"slop|batter", re.IGNORECASE)
_WALL_WORD_RE = re.compile(r"\b(?:wall|side|bank|edge)", re.IGNORECASE)
_RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")
_DEGREES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°|deg)", re.IGNORECASE)
_SLOPE_CLAUSE_SPLIT = re.compile(r"\.(?=\s|$)|[;,\n()]")   # keeps "1:1" and "0.5 m" whole
_WALL_RATIO_MAX = 4.0      # a ratio up to 4:1 reads as a wall slope without the word "wall";
#                            a flatter one ("graded at 1:50") is a surface gradient
_LIFT_KEYS = ("lifts", "lift_count", "compaction_lifts")
_MAX_LIFTS = 10


@dataclass(frozen=True)
class Slope:
    """Sloped walls of a dug bowl or box (:func:`wall_slope`): ``ratio`` horizontal per
    vertical (1.0 for "1:1"), None when the brief says the walls slope but not how much, and
    ``text`` as the shapes quote it ("1:1 sloped walls")."""

    ratio: float | None
    text: str


@dataclass(frozen=True)
class Shape:
    """One body a structure's dimensions describe (:func:`dims_volumes`).

    ``floor_m3`` is the smallest volume the brief's numbers allow and ``m3`` the largest: a
    structure carrying ``min_<dimension>_m`` extras (``min_diameter_m``, ``min_depth_m``,
    ``min_length_m``, ``min_width_m``, ``min_height_m``, recorded by the structure stage for a
    brief that lets the work shrink, "any diameter between 12 m and 20 m") has its floor from
    the smallest dimensions; a bowl or box with sloped walls (:class:`Slope`) has its floor
    from the stated size read at the rim (walls narrowing down) and ``m3`` from the same size
    read at the floor (walls widening up), so either reading of the brief lies in the range;
    otherwise the two are equal. ``how`` quotes the dimensions ("bowl 12-20 m across, 0.5 m
    deep"), ``floor_how`` the floor alone and ``head`` opens a verdict's detail ("dimensions
    bowl ...", "its key trench is 60 x 1.2 x 0.8 m"). A dug shape (``phase`` "excavation")
    has its full ``footprint_m2`` and ``depth_m``, and the ``floor_footprint_m2`` and
    ``floor_depth_m`` of its floor (the stripping credit's cap and depth); a placed body
    (``phase`` "haul") has neither. ``floor_area_m2`` is the area compaction or sintering
    treats on the floor shape (the bottom of sloped walls, a berm's base; 0 when unknown).
    ``excess_judged`` is False when the numbers bound no largest volume (sloped walls of an
    unstated ratio). ``kind`` is bowl / box / trench / prism.
    """

    m3: float
    how: str
    phase: str
    floor_m3: float
    floor_how: str
    footprint_m2: float = 0.0
    depth_m: float = 0.0
    kind: str = "body"
    floor_footprint_m2: float = 0.0
    floor_depth_m: float = 0.0
    floor_area_m2: float = 0.0
    excess_judged: bool = True
    head: str = ""

    @property
    def ranged(self) -> bool:
        """Whether the brief's numbers allow a smaller volume than the largest one."""
        return self.floor_m3 < self.m3

    @property
    def m3_text(self) -> str:
        """'360', or '68-189' when the brief allows a smaller size."""
        return f"{qty(self.floor_m3)}-{qty(self.m3)}" if self.ranged else qty(self.m3)

    @property
    def material(self) -> str:
        """What the volume is: 'excavation' for a dug shape, 'fill' for a placed body."""
        return "excavation" if self.phase == "excavation" else "fill"

    @property
    def strip_depth_m(self) -> float:
        """The depth an excavator m2 task counts on this shape: the floor's depth up to
        :data:`STRIP_DEPTH_M` (0 for a placed body: a hauler m2 task counts nothing)."""
        return min(self.floor_depth_m, STRIP_DEPTH_M)

    @property
    def deep_floor_m3(self) -> float:
        """The floor's volume below the stripped layer: what only digs in m3 can cover (0 for
        a placed body and for a dug shape no deeper than :data:`STRIP_DEPTH_M`)."""
        if self.phase != "excavation":
            return 0.0
        return max(0.0, self.floor_m3 - self.floor_footprint_m2 * self.strip_depth_m)


def _ratio_text(ratio: float) -> str:
    """1.0 -> '1:1', 1.5 -> '1.5:1'."""
    return f"{qty(ratio)}:1"


def _slope_of_ratio(a: float, b: float, text: str) -> Slope | None:
    """'2:1' or '1:2' as horizontal per vertical: the shallower reading (the larger ratio),
    which gives the smallest volume at the rim and the largest at the floor, so either
    convention lies inside the range the check allows."""
    if a <= 0 or b <= 0:
        return None
    return Slope(max(a / b, b / a), f"{text} sloped walls")


def _slope_value(value: object) -> Slope | None:
    """A ``*slope*`` / ``*batter*`` extra the structure stage recorded: True (sloped, ratio
    unstated), a number (horizontal per vertical; 10 to 89 read as degrees from the
    horizontal) or a string ("1:1", "1.5", "yes")."""
    if isinstance(value, bool):
        return Slope(None, "sloped walls") if value else None
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v) or v <= 0:
            return None
        if 10 <= v < 90:
            return Slope(1 / math.tan(math.radians(v)), f"{qty(v)} degree sloped walls")
        ratio = max(v, 1 / v)
        return Slope(ratio, f"{_ratio_text(ratio)} sloped walls")
    if isinstance(value, str):
        m = _RATIO_RE.search(value)
        if m:
            return _slope_of_ratio(float(m[1]), float(m[2]), f"{m[1]}:{m[2]}")
        try:
            return _slope_value(float(value))
        except ValueError:
            pass
        if re.search(r"slop|batter|yes|true", value, re.IGNORECASE):
            return Slope(None, "sloped walls")
    return None


def _slope_in(line: str) -> Slope | None:
    """The wall slope a brief line states, clause by clause: a ratio in a clause that says
    slope (past 4:1 only when it also names a wall or side: "graded at 1:50" is a surface
    gradient), an angle in a clause that names a wall, or a clause that says the walls slope
    without a figure. "The plateau slopes north" names no wall and states nothing."""
    for clause in _SLOPE_CLAUSE_SPLIT.split(line):
        if not _SLOPE_WORD_RE.search(clause):
            continue
        walls = _WALL_WORD_RE.search(clause) is not None
        m = _RATIO_RE.search(clause)
        if m:
            slope = _slope_of_ratio(float(m[1]), float(m[2]), f"{m[1]}:{m[2]}")
            if slope is not None and (walls or (slope.ratio or 0.0) <= _WALL_RATIO_MAX):
                return slope
            continue
        if walls:
            d = _DEGREES_RE.search(clause)
            if d and 0 < float(d[1]) < 90:
                return Slope(1 / math.tan(math.radians(float(d[1]))),
                             f"{d[1]} degree sloped walls")
            return Slope(None, "sloped walls")
    return None


def wall_slope(structure: Structure, req: Requirements | None = None) -> Slope | None:
    """The sloped walls a structure is dug with, or None for vertical ones: a ``*slope*`` /
    ``*batter*`` extra the structure stage recorded ("wall_slope": "1:1"), else, with
    ``req``, the structure's own entry in the brief (``req.source_brief``,
    :func:`~moonwright.pipeline.tags.entry_line`) or a constraint that names it. Seen in
    review: "Storage pit: 8 m diameter, 2 m deep, with a 1:1 sloped wall" priced with
    vertical walls (100.5 m3) rejected the pit dug to its real 58.6 m3 as a 58 % shortfall."""
    for key, value in (structure.model_extra or {}).items():
        if _SLOPE_WORD_RE.search(key):
            slope = _slope_value(value)
            if slope is not None:
                return slope
    if req is None:
        return None
    lines = [entry_line(req.source_brief, structure)]
    lines += [c for c in req.constraints
              if name_score(c.lower(), set(WORDS.findall(c.lower())), structure) is not None]
    for line in lines:
        slope = _slope_in(line) if line else None
        if slope is not None:
            return slope
    return None


def _bowl_m3(diameter: float, depth: float, ratio: float | None = None, *,
             rim: bool = True) -> float:
    """A bowl ``diameter`` across and ``depth`` deep: vertical walls without ``ratio``, else
    walls sloping ``ratio`` horizontal per vertical from the stated diameter at the rim
    (narrowing down; a cone once the walls meet) or, ``rim=False``, at the floor (widening
    up)."""
    r = diameter / 2
    if not ratio:
        return math.pi * r * r * depth
    if rim:
        bottom = max(0.0, r - ratio * depth)
        return math.pi / (3 * ratio) * (r ** 3 - bottom ** 3)
    return math.pi / (3 * ratio) * ((r + ratio * depth) ** 3 - r ** 3)


def _box_m3(length: float, width: float, depth: float, ratio: float | None = None, *,
            rim: bool = True) -> float:
    """A box ``length`` x ``width`` x ``depth`` with vertical walls, or with walls sloping
    ``ratio`` from the stated size at the rim (narrowing down until the walls meet) or at
    the floor (widening up): the integral of the section over the depth."""
    if not ratio:
        return length * width * depth
    t = 2 * ratio
    if rim:
        z = min(depth, min(length, width) / t)
        return length * width * z - t * (length + width) * z * z / 2 + t * t * z ** 3 / 3
    return length * width * depth + t * (length + width) * depth ** 2 / 2 + t * t * depth ** 3 / 3


def _least(d: dict[str, float], key: str) -> float:
    """The smallest value the brief allows for ``key``: ``min_<key>`` when the structure
    carries one below the regular value, else the value itself."""
    value = d[key]
    least = d.get(f"min_{key}")
    return least if least is not None and 0 < least < value else value


def _span(d: dict[str, float], key: str) -> str:
    """'20', or '12-20' when the brief allows less."""
    lo, hi = _least(d, key), d[key]
    return f"{qty(lo)}-{qty(hi)}" if lo < hi else qty(hi)


def _dug(d: dict[str, float], lo: dict[str, float], kind: str, slope: Slope | None) -> Shape:
    """A bowl (``diameter_m`` x ``depth_m``) or a box (``length_m`` x ``width_m`` x
    ``depth_m``), vertical-walled or with ``slope``."""
    if kind == "bowl":
        size = f"bowl {_span(d, 'diameter_m')} m across, {_span(d, 'depth_m')} m deep"
        floor_size = f"bowl {qty(lo['diameter_m'])} m across, {qty(lo['depth_m'])} m deep"
        rim_size = f"bowl {qty(lo['diameter_m'])} m across at the rim, {qty(lo['depth_m'])} m deep"
        footprint = math.pi * (d["diameter_m"] / 2) ** 2
        floor_footprint = math.pi * (lo["diameter_m"] / 2) ** 2
        half_span = lo["diameter_m"] / 2

        def volume(dims: dict[str, float], ratio: float | None, rim: bool) -> float:
            return _bowl_m3(dims["diameter_m"], dims["depth_m"], ratio, rim=rim)

        def bottom(ratio: float) -> float:
            return math.pi * max(0.0, lo["diameter_m"] / 2 - ratio * lo["depth_m"]) ** 2
    else:
        size = f"{_span(d, 'length_m')} x {_span(d, 'width_m')} x {_span(d, 'depth_m')} m"
        floor_size = f"{qty(lo['length_m'])} x {qty(lo['width_m'])} x {qty(lo['depth_m'])} m"
        rim_size = f"{floor_size} at the rim"
        footprint = d["length_m"] * d["width_m"]
        floor_footprint = lo["length_m"] * lo["width_m"]
        half_span = min(lo["length_m"], lo["width_m"]) / 2

        def volume(dims: dict[str, float], ratio: float | None, rim: bool) -> float:
            return _box_m3(dims["length_m"], dims["width_m"], dims["depth_m"], ratio, rim=rim)

        def bottom(ratio: float) -> float:
            cut = 2 * ratio * lo["depth_m"]
            return max(0.0, lo["length_m"] - cut) * max(0.0, lo["width_m"] - cut)

    common = {"phase": "excavation", "footprint_m2": footprint, "depth_m": d["depth_m"],
              "kind": kind, "floor_footprint_m2": floor_footprint,
              "floor_depth_m": lo["depth_m"]}
    if slope is None:
        return Shape(m3=volume(d, None, True), how=size, floor_m3=volume(lo, None, True),
                     floor_how=floor_size, floor_area_m2=floor_footprint,
                     head=f"dimensions {size}", **common)
    how = f"{size}, {slope.text}"
    if slope.ratio is None:   # sloped by an unstated ratio: at least the walls that meet
        # at the stated depth (the shallowest walls that still reach it: a cone, a wedge)
        meet = half_span / lo["depth_m"] if lo["depth_m"] > 0 else None
        return Shape(m3=volume(d, None, True), how=how, floor_m3=volume(lo, meet, True),
                     floor_how=f"{rim_size}, walls sloping to a point", floor_area_m2=0.0,
                     excess_judged=False, head=f"dimensions {how}", **common)
    return Shape(m3=volume(d, slope.ratio, False), how=how,
                 floor_m3=volume(lo, slope.ratio, True), floor_how=f"{rim_size}, {slope.text}",
                 floor_area_m2=bottom(slope.ratio), head=f"dimensions {how}", **common)


def _body(d: dict[str, float], slope: Slope | None = None) -> Shape | None:
    """The main body a structure's dimensions describe (None when they describe none);
    ``slope`` applies to a dug bowl or box."""
    lo = {k: _least(d, k) for k in _MIN_KEYS if k in d}
    if "diameter_m" in d and "depth_m" in d:
        return _dug(d, lo, "bowl", slope)
    if {"length_m", "width_m", "depth_m"} <= d.keys():
        return _dug(d, lo, "box", slope)
    if {"length_m", "height_m", "width_m"} <= d.keys():
        how = (f"{_span(d, 'length_m')} m long, {_span(d, 'height_m')} m high, "
               f"{_span(d, 'width_m')} m wide at the base, triangular section")
        return Shape(
            m3=0.5 * d["height_m"] * d["width_m"] * d["length_m"],
            how=how,
            phase="haul",
            floor_m3=0.5 * lo["height_m"] * lo["width_m"] * lo["length_m"],
            floor_how=(f"{qty(lo['length_m'])} m long, {qty(lo['height_m'])} m high, "
                       f"{qty(lo['width_m'])} m wide at the base, triangular section"),
            kind="prism", floor_area_m2=lo["length_m"] * lo["width_m"], head=f"dimensions {how}")
    if {"length_m", "height_m"} <= d.keys():
        how = f"{_span(d, 'length_m')} m long, {_span(d, 'height_m')} m high, 1:1 slopes"
        return Shape(
            m3=d["height_m"] ** 2 * d["length_m"],
            how=how,
            phase="haul",
            floor_m3=lo["height_m"] ** 2 * lo["length_m"],
            floor_how=f"{qty(lo['length_m'])} m long, {qty(lo['height_m'])} m high, 1:1 slopes",
            kind="prism", floor_area_m2=lo["length_m"] * 2 * lo["height_m"],
            head=f"dimensions {how}")
    return None


def _key_trench(d: dict[str, float]) -> Shape | None:
    """The key trench under a placed body: ``length_m`` x ``trench_width_m`` x
    ``trench_depth_m``. A trench the brief gives no width for has no volume to judge: the
    berm's base width it used to take is a number no brief states (seen in review: a correct
    60 x 1.2 x 0.5 m trench read as 30 % of an invented 60 x 4 x 0.5 m one)."""
    if not {"trench_depth_m", "trench_width_m", "length_m"} <= d.keys():
        return None
    depth, width = d["trench_depth_m"], d["trench_width_m"]
    length, least_length = d["length_m"], _least(d, "length_m")
    size = f"{_span(d, 'length_m')} x {qty(width)} x {qty(depth)} m"
    return Shape(
        m3=length * width * depth,
        how=f"key trench {size}",
        phase="excavation",
        floor_m3=least_length * width * depth,
        floor_how=f"key trench {qty(least_length)} x {qty(width)} x {qty(depth)} m",
        footprint_m2=length * width, depth_m=depth, kind="trench",
        floor_footprint_m2=least_length * width, floor_depth_m=depth,
        floor_area_m2=least_length * width, head=f"its key trench is {size}")


def dims_volumes(structure: Structure, req: Requirements | None = None) -> list[Shape]:
    """Every :class:`Shape` a structure's dimensions imply, main body first: a bowl
    (``diameter_m`` x ``depth_m``) or a box (``length_m`` x ``width_m`` x ``depth_m``) is dug,
    so its phase is ``excavation``; a prism (``length_m`` x ``height_m`` [x ``width_m`` at the
    base], a berm or a stockpile) is placed, so its phase is ``haul``; a placed body on a key
    trench (``trench_width_m`` and ``trench_depth_m``, the structure prompt's keys for "on a
    key trench 0.7 m wide and 0.45 m deep") adds the trench as a second, dug shape. Walls are
    vertical unless :func:`wall_slope` finds them sloped (an extra, or with ``req`` the
    structure's own entry in the brief or a constraint); a berm's section is triangular
    (``height^2`` with 1:1 slopes when no base width is given: the steepest a dumped berm
    stands, so the least it holds). ``min_<dimension>_m`` extras give each shape its floor
    (the smallest volume the brief allows; :class:`Shape`). A shape whose numbers give no
    positive volume (``depth_m: 0`` for an at-grade hardstand, a negative figure a user
    typed) is left out, so nothing downstream divides by it; so is a key trench without a
    stated width."""
    d = structure.dims()
    body = _body(d)
    if body is not None and body.kind in ("bowl", "box"):
        slope = wall_slope(structure, req)
        if slope is not None:
            body = _body(d, slope)
    shapes = [body] if body is not None else []
    if body is None or body.phase == "haul":
        trench = _key_trench(d)
        if trench is not None:
            shapes.append(trench)
    return [s for s in shapes if s.m3 > 0 and s.floor_m3 > 0]


def dims_volume(structure: Structure, req: Requirements | None = None) -> Shape | None:
    """The main :class:`Shape` of :func:`dims_volumes`, or None when the dimensions form no
    positive volume."""
    shapes = dims_volumes(structure, req)
    return shapes[0] if shapes else None


def stockpile_like(structure: Structure) -> bool:
    """Whether a structure is a pile of hauled material rather than a designed body: its type,
    or a ``label`` / ``name`` extra, matches stockpile | spoil | pile | depot | dump | heap
    (any case). A berm is never one, whatever it is made of (a "spoil berm" has a designed
    length and height)."""
    extras = structure.model_extra or {}
    names = [structure.type, *(str(extras[k]) for k in ("label", "name")
                               if isinstance(extras.get(k), str))]
    if "berm" in structure.type.lower():
        return False
    return any(_STOCKPILE_RE.search(name) for name in names)


def rehandles(task: Task) -> bool:
    """Whether an excavator task moves spoil already dug instead of digging: its name says
    blade, push, windrow, stockpile, backfill, spread or rehandle and not excavate or dig
    ("Blade S1 spoil into the north windrow", "Backfill anchor trench")."""
    return bool(_REHANDLE_RE.search(task.name)) and not _DIG_RE.search(task.name)


@dataclass(frozen=True)
class VolumeCoverage:
    """The m3 a plan covers of one :class:`Shape` of a structure.

    ``tasks`` are the structure's tasks of the shape's robot type (excavator for a dug shape,
    hauler for a placed one) that state a quantity, in graph order, and ``roles`` what each
    counts as: ``dig`` (its m3 counts: an excavation, or a haul into a placed body),
    ``strip`` (an excavator m2 task that earns the stripping credit), ``after`` (an m2 pass
    that follows a dig of the shape and is not named a strip: levelling a floor already dug
    removes no surface layer), ``rehandle`` (:func:`rehandles`: spoil moved, nothing dug) or
    ``none`` (a hauler in m2, a count). ``cubic_m3`` sums the digs, ``area_m2`` the strips and
    ``stripped_m3`` is their credit: the areas capped at the footprint of the smallest size
    the brief allows (a strip and a grade over the same pad do not double count, and a kind-E
    pad stripped over its full size earns no more than its floor's top layer), times its
    depth up to :data:`STRIP_DEPTH_M`; ``counted_m3`` is the two together.

    The test is against ``floor_m3``: the shape's floor, or for a placed body whose hauls wait
    for the excavation of other structures (``sources``, in requirements order) the floor
    volume dug there (``source_m3``) when that is smaller: a berm "built from pad spoil"
    cannot be given more of it than the pad yields (seen in review: landing_pad's 300 m3
    berm prism against its 125.7 m3 bowl). ``share`` is counted over floor; on a dug shape
    deeper than the stripped layer, ``deep_share`` is the digs alone over the floor's volume
    below that layer (the stripping credit cannot stand in for the dig), and
    ``judged_share`` the smaller of the two, what every test reads (under
    :data:`VOLUME_LOW`: ``short``). ``share_full`` (the digs over the largest volume the
    dimensions allow, without the credit: stripping a pad and then digging its whole bowl
    is the normal sequence) is the over-scope test. ``excused``: the fill of a
    stockpile-like structure (:func:`stockpile_like`) has no scope to judge either way.
    """

    structure: Structure
    shape: Shape
    robot: str
    tasks: tuple[Task, ...]
    roles: tuple[str, ...]
    cubic_m3: float
    stripped_m3: float
    area_m2: float
    sources: tuple[str, ...] = ()
    source_m3: float | None = None

    @property
    def counted_m3(self) -> float:
        return self.cubic_m3 + self.stripped_m3

    @property
    def label(self) -> str:
        return f"{self.structure.id} ({self.structure.type})"

    @property
    def sourced(self) -> bool:
        """Whether the floor is the spoil of the structures the hauls wait for."""
        return self.source_m3 is not None and self.source_m3 < self.shape.floor_m3

    @property
    def floor_m3(self) -> float:
        if self.source_m3 is not None and self.source_m3 < self.shape.floor_m3:
            return self.source_m3
        return self.shape.floor_m3

    @property
    def share(self) -> float:
        return self.counted_m3 / self.floor_m3

    @property
    def deep_share(self) -> float | None:
        deep = self.shape.deep_floor_m3
        return self.cubic_m3 / deep if deep > 0 else None

    @property
    def judged_share(self) -> float:
        deep = self.deep_share
        return self.share if deep is None else min(self.share, deep)

    @property
    def remaining_m3(self) -> float:
        """The m3 of digs (or hauls) that bring the plan up to its floor, the layer below the
        stripped one included."""
        return max(0.0, self.floor_m3 - self.counted_m3,
                   self.shape.deep_floor_m3 - self.cubic_m3)

    @property
    def share_full(self) -> float:
        return self.cubic_m3 / self.shape.m3

    @property
    def short(self) -> bool:
        return self.judged_share < VOLUME_LOW

    @property
    def over(self) -> bool:
        return self.shape.excess_judged and self.share_full > VOLUME_HIGH

    @property
    def excused(self) -> bool:
        return self.shape.phase == "haul" and stockpile_like(self.structure)

    @property
    def of_floor(self) -> str:
        """What a share is of, after its percentage: '' for the dimensions as given, ' of the
        smallest size the brief allows', ' of the 125.7 m3 of spoil S1 yields'."""
        if self.sourced:
            verb = "yields" if len(self.sources) == 1 else "yield"
            return f" of the {qty(self.floor_m3)} m3 of spoil {join_and(self.sources)} {verb}"
        return " of the smallest size the brief allows" if self.shape.ranged else ""

    def deep_note(self, below: float = VOLUME_LOW) -> str:
        """'; below the top 0.2 m only digs in m3 count, 26 of 62.8 m3 (41 %)' when that layer
        is what falls under ``below``: a stripping credit lifts the total share to ``below``
        or more while the digs cover less of the layer under it (empty otherwise: the total
        share already says it)."""
        deep = self.deep_share
        if deep is None or deep >= below or self.share < below or self.stripped_m3 <= 0:
            return ""
        return (f"; below the top {qty(STRIP_DEPTH_M)} m only digs in m3 count, "
                f"{qty(self.cubic_m3)} of {qty(self.shape.deep_floor_m3)} m3 "
                f"({pct(deep, floor=True)})")

    def ids(self, role: str) -> list[str]:
        """The ids of the tasks with ``role``, in graph order."""
        return [t.id for t, r in zip(self.tasks, self.roles, strict=True) if r == role]

    @property
    def listing(self) -> str:
        """'T2 300 m2 counts 60 m3 at 0.2 m, T3 190 m3': every task with what it covers, in
        graph order; the strips of a dug shape are one entry, at the place of the first
        ('T1 100 m2 + T2 100 m2 count 15 m3 at 0.15 m over the 100 m2 footprint' when their
        areas were capped), because their credit is shared; a task that counts nothing says
        so, and why when it is a pass after the dig or spoil moved."""
        strips = [t for t, r in zip(self.tasks, self.roles, strict=True) if r == "strip"]
        parts: list[str] = []
        for t, role in zip(self.tasks, self.roles, strict=True):
            amount = f"{t.id} {qty(t.quantity or 0.0)} {t.unit or '?'}"
            if role == "dig":
                parts.append(amount)
            elif role == "strip":
                if t is strips[0]:
                    parts.append(self._strip_entry(strips))
            elif role == "after":
                parts.append(f"{amount} counts nothing (a pass after the dig)")
            elif role == "rehandle":
                parts.append(f"{amount} counts nothing (moves spoil already dug)")
            else:
                parts.append(f"{amount} counts nothing")
        return ", ".join(parts)

    @property
    def listing_m3(self) -> str:
        """'T7 300 m3, T8 300 m3': the tasks the over-scope test counts."""
        return ", ".join(f"{t.id} {qty(t.quantity or 0.0)} m3"
                         for t, r in zip(self.tasks, self.roles, strict=True) if r == "dig")

    def _strip_entry(self, strips: list[Task]) -> str:
        named = " + ".join(f"{t.id} {qty(t.quantity or 0.0)} m2" for t in strips)
        verb = "counts" if len(strips) == 1 else "count"
        text = f"{named} {verb} {qty(self.stripped_m3)} m3 at {qty(self.shape.strip_depth_m)} m"
        if self.area_m2 > self.shape.floor_footprint_m2:
            text += f" over the {qty(self.shape.floor_footprint_m2)} m2 footprint"
            if self.shape.floor_footprint_m2 < self.shape.footprint_m2:
                text += " of the smallest size"
        return text


def _ancestors(graph: TaskGraph) -> dict[str, set[str]]:
    """Every task's transitive predecessors by its ``after`` edges (an unknown id is
    skipped and a cycle tolerated: the validator reports both on its own)."""
    parents = {t.id: list(t.after) for t in graph.tasks}
    out: dict[str, set[str]] = {}
    for tid, first in parents.items():
        seen: set[str] = set()
        stack = list(first)
        while stack:
            a = stack.pop()
            if a in seen or a not in parents:
                continue
            seen.add(a)
            stack.extend(parents[a])
        out[tid] = seen
    return out


def _dug_role(task: Task, digs: set[str], before: Mapping[str, set[str]]) -> str:
    """What an excavator task with a quantity counts as on a dug shape (the roles of
    :class:`VolumeCoverage`): its m3 a ``dig`` or a ``rehandle``; its m2 a ``strip``, or a
    pass ``after`` a dig of the shape when it follows one and is not named a strip."""
    if task.unit == "m3":
        return "dig" if task.id in digs else "rehandle"
    if task.unit == "m2":
        follows = bool(before.get(task.id, set()) & digs)
        return "after" if follows and not _STRIP_RE.search(task.name) else "strip"
    return "none"


def _digs(task: Task | None) -> bool:
    """Whether a task a haul waits for digs material out (an excavator task that is no
    :func:`rehandles`), so the structure it serves is the haul's source."""
    return task is not None and task.robot_type == "excavator" and not rehandles(task)


def volume_coverages(req: Requirements, graph: TaskGraph) -> list[VolumeCoverage]:
    """One :class:`VolumeCoverage` per structure and shape (:func:`dims_volumes`, read with
    the brief and the constraints of ``req``) that has at least one task of the shape's robot
    type with a quantity, in requirements order. The tasks of a structure are resolved
    exactly as the coverage check resolves them
    (:func:`moonwright.pipeline.tags.serving_tasks`: the id in any case, the type, a label
    containing the id, untagged tasks in its zone, and hauls placed in its zone), so a graph
    the validator accepts cannot escape the volume check by its tag spelling; a dig a haul
    waits for (its ``after`` edges, transitively) names the haul's sources the same way. A
    structure without a volume, or without any task of the shape's type (coverage's
    business), gets nothing; a graph without quantities (the hand-made fixtures) therefore
    gets nothing."""
    shapes = {s.id: dims_volumes(s, req) for s in req.structures}
    if not any(shapes.values()):
        return []
    served = {s.id: serving_tasks(s, graph, req.structures) for s in req.structures}
    serves: dict[str, set[str]] = {}
    for sid, tasks in served.items():
        for t in tasks:
            serves.setdefault(t.id, set()).add(sid)
    before = _ancestors(graph)
    by_id = {t.id: t for t in graph.tasks}
    dug = {sid for sid, found in shapes.items() if any(x.phase == "excavation" for x in found)}
    out: list[VolumeCoverage] = []
    for s in req.structures:
        for shape in shapes[s.id]:
            robot = "excavator" if shape.phase == "excavation" else "hauler"
            typed = tuple(t for t in served[s.id] if t.robot_type == robot and t.quantity is not None)
            if not typed:
                continue
            area, stripped = 0.0, 0.0
            sources: tuple[str, ...] = ()
            source_m3: float | None = None
            if shape.phase == "excavation":
                digs = {t.id for t in typed if t.unit == "m3" and not rehandles(t)}
                roles = tuple(_dug_role(t, digs, before) for t in typed)
                area = sum(t.quantity or 0.0 for t, r in zip(typed, roles, strict=True) if r == "strip")
                stripped = min(area, shape.floor_footprint_m2) * shape.strip_depth_m
            else:
                roles = tuple("dig" if t.unit == "m3" else "none" for t in typed)
                hauls = [t for t, r in zip(typed, roles, strict=True) if r == "dig"]
                found = {sid for t in hauls for a in before.get(t.id, set())
                         if _digs(by_id.get(a)) for sid in serves.get(a, set())
                         if sid != s.id and sid in dug}
                sources = tuple(x.id for x in req.structures if x.id in found)
                if sources:
                    source_m3 = sum(x.floor_m3 for sid in sources for x in shapes[sid]
                                    if x.phase == "excavation")
            cubic = sum(t.quantity or 0.0 for t, r in zip(typed, roles, strict=True) if r == "dig")
            out.append(VolumeCoverage(structure=s, shape=shape, robot=robot, tasks=typed,
                                      roles=roles, cubic_m3=cubic, stripped_m3=stripped,
                                      area_m2=area, sources=sources, source_m3=source_m3))
    return out


def volume_shortfalls(req: Requirements, graph: TaskGraph) -> list[VolumeCoverage]:
    """The coverages below :data:`VOLUME_LOW` of their floor, the fill of stockpile-like
    structures excused: what ``validate_graph`` reports as ``volume:<structure>``."""
    return [c for c in volume_coverages(req, graph) if c.short and not c.excused]


def _times(share: float) -> str:
    """'twice', 'three times', '1.6 times'."""
    rounded = round(share, 1)
    if rounded == 2.0:
        return "twice"
    if rounded == 3.0:
        return "three times"
    return f"{rounded:.1f} times"


SHORTFALL_MARK = " total only "   # in every shortfall advisory, never in an excess one


def is_shortfall(advisory: str) -> bool:
    """Whether a ``volume:<structure>`` advisory is a shortfall (what the validator's verdict
    also carries) rather than an excess (which no verdict carries: a berm hauled twice over
    while its key trench is short shares the id ``volume:S2`` with the trench's conflict)."""
    return advisory.startswith("volume:") and SHORTFALL_MARK in advisory


def _shortfall_text(c: VolumeCoverage) -> str:
    shape = c.shape
    return (f"volume:{c.structure.id}: its dimensions give {shape.m3_text} m3 ({shape.how}) but "
            f"its {shape.phase} tasks ({c.listing}){SHORTFALL_MARK}{qty(c.counted_m3)} m3 "
            f"({pct(c.share, floor=True)}{c.of_floor}){c.deep_note()}")


def volume_advisories(req: Requirements, graph: TaskGraph) -> list[str]:
    """``volume:<structure>: ...`` for every structure whose tasks plan clearly less (below
    :data:`VOLUME_LOW` of the smallest volume the brief allows, :class:`VolumeCoverage`) or
    clearly more (above :data:`VOLUME_HIGH` of the largest) than its dimensions imply
    (:func:`volume_coverages`: the excavator tasks of a dug shape, m3 digs plus the stripping
    credit, the hauler tasks of a placed one). A shortfall reads ``total only 120 m3 (33 %)``
    (the share rounded down, so a 69.96 % shortfall never prints the 70 % it missed) and is
    also a validation conflict on a whole plan (``validate_graph``); an excess, judged on the
    digs alone (a pad stripped and then dug in full is no excess, and spoil bladed into a
    windrow is no second dig), reads ``total 600 m3 (200 %): hauled twice over`` / ``dug
    twice over`` and stays a warning for the trace and the repair prompt (the parts of a
    split must sum to the total, never repeat it). The fill of a stockpile-like structure
    (:func:`stockpile_like`) is judged neither way: a pile is whatever gets hauled. Seen live
    (phase 3c): a 360 m3 borrow strip planned as 600 m2 of stripping to fit one battery, a
    300 m3 berm hauled twice over, a 20 m pad dug as a 12 m one, a 57.6 m3 key trench
    "repaired" with two 5 m3 tasks. Meant for a whole plan: on the remaining graph of a
    replan the finished work is gone, so the task-graph stage does not call it there."""
    out: list[str] = []
    for c in volume_coverages(req, graph):
        if c.excused:
            continue
        if c.short:
            out.append(_shortfall_text(c))
        elif c.over:
            shape = c.shape
            verb = "dug" if shape.phase == "excavation" else "hauled"
            which = ("" if all(r == "dig" for r in c.roles)
                     else " that dig" if "rehandle" in c.roles else " in m3")
            out.append(f"volume:{c.structure.id}: its dimensions give {shape.m3_text} m3 "
                       f"({shape.how}) but its {shape.phase} tasks{which} ({c.listing_m3}) "
                       f"total {qty(c.cubic_m3)} m3 ({pct(c.share_full)}): {verb} "
                       f"{_times(c.share_full)} over")
    return out


def volume_shares(req: Requirements, graph: TaskGraph) -> dict[str, float]:
    """``{structure id: judged share}`` for every structure and shape the advisory judges
    (:attr:`VolumeCoverage.judged_share`, the smallest when a structure has several shapes;
    the fill of a stockpile-like structure is not judged), whether or not it is out of range:
    what the eval reports as ``min_volume_share``."""
    out: dict[str, float] = {}
    for c in volume_coverages(req, graph):
        if not c.excused:
            out[c.structure.id] = min(c.judged_share, out.get(c.structure.id, math.inf))
    return out


# ----------------------------------------------------------------------- areas (eval scope)
@dataclass(frozen=True)
class AreaCoverage:
    """The m2 a plan's ``robot`` (compactor or printer) tasks treat on one structure against
    ``need_m2``, the area its dimensions give (:func:`area_coverages`; ``how`` says how:
    '540 m2 x 2 lifts')."""

    structure: Structure
    robot: str
    tasks: tuple[Task, ...]
    area_m2: float
    need_m2: float
    how: str

    @property
    def share(self) -> float:
        return self.area_m2 / self.need_m2


def lifts(structure: Structure) -> int:
    """How many lifts the brief compacts the structure in: a ``lifts`` (``lift_count``,
    ``compaction_lifts``) extra of 1 to 10, else 1."""
    extras = structure.model_extra or {}
    for key in _LIFT_KEYS:
        v = extras.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= _MAX_LIFTS:
            return round(v)
    return 1


def _work_area(structure: Structure, req: Requirements | None) -> tuple[float, bool] | None:
    """``(m2, placed)``: the area compaction and sintering treat on the smallest size the
    brief allows (an ``area_m2`` extra; the floor of a dug bowl or box, the bottom when its
    walls slope; a berm's base; a flat ``length_m`` x ``width_m`` or ``diameter_m`` surface)
    and whether it is a placed body; None when the dimensions give none."""
    d = structure.dims()
    if d.get("area_m2", 0.0) > 0:
        return d["area_m2"], False
    shapes = dims_volumes(structure, req)
    body = shapes[0] if shapes and shapes[0].kind != "trench" else None
    if body is not None:
        return (body.floor_area_m2, body.phase == "haul") if body.floor_area_m2 > 0 else None
    if "depth_m" in d or "height_m" in d:
        return None   # a body whose numbers form no positive volume
    lo = {k: _least(d, k) for k in _MIN_KEYS if k in d}
    if {"length_m", "width_m"} <= lo.keys() and lo["length_m"] > 0 and lo["width_m"] > 0:
        return lo["length_m"] * lo["width_m"], False
    if lo.get("diameter_m", 0.0) > 0:
        return math.pi * (lo["diameter_m"] / 2) ** 2, False
    return None


def area_coverages(req: Requirements, graph: TaskGraph) -> list[AreaCoverage]:
    """One :class:`AreaCoverage` per structure and area-work robot type (compactor, printer)
    with at least one task of that type with a quantity: the m2 its tasks plan against the
    structure's work area (:func:`_work_area`), times its :func:`lifts` for compaction. A
    placed body's sintering (a berm's crest, whose width no dimension gives) and the fill of
    a stockpile-like structure are not judged. The eval's scope test only
    (:func:`scope_gaps`), never a validation conflict: seen in review, a kind-D brief whose
    two compaction lifts the repair trimmed by 22 % verified "in scope"."""
    out: list[AreaCoverage] = []
    for s in req.structures:
        if stockpile_like(s):
            continue
        work = _work_area(s, req)
        if work is None:
            continue
        area, placed = work
        served = serving_tasks(s, graph, req.structures)
        for robot in ("compactor", "printer"):
            if robot == "printer" and placed:
                continue
            typed = tuple(t for t in served if t.robot_type == robot and t.quantity is not None)
            if not typed:
                continue
            n = lifts(s) if robot == "compactor" else 1
            how = f"{qty(area)} m2" + (f" x {n} lifts" if n > 1 else "")
            planned = sum(t.quantity or 0.0 for t in typed if t.unit == "m2")
            out.append(AreaCoverage(structure=s, robot=robot, tasks=typed, area_m2=planned,
                                    need_m2=area * n, how=how))
    return out


def area_shares(req: Requirements, graph: TaskGraph) -> dict[str, float]:
    """``{structure id: planned m2 / work area}``, the smallest over its compaction and
    sintering: what the eval reports as ``min_area_share``."""
    out: dict[str, float] = {}
    for a in area_coverages(req, graph):
        out[a.structure.id] = min(a.share, out.get(a.structure.id, math.inf))
    return out


def scope_gaps(req: Requirements, graph: TaskGraph) -> list[str]:
    """Why a plan is not "in scope" (empty when it is): one line per judged volume below
    :data:`IN_SCOPE_SHARE` of its floor (the shortfall advisory itself below
    :data:`VOLUME_LOW`, which the validator also rejects) and per judged area below it. The
    eval's ``in_scope`` is a verified run without any: a kind-D brief "verified" by trimming
    the work 15-25 % is a re-scope, not a success in scope. An excess (hauled twice over) is
    no gap: the work the brief asks for is all there."""
    out: list[str] = []
    rule = f"under the {pct(IN_SCOPE_SHARE)} a plan in scope covers"
    for c in volume_coverages(req, graph):
        if c.excused or c.judged_share >= IN_SCOPE_SHARE:
            continue
        if c.short:
            out.append(_shortfall_text(c))
            continue
        out.append(f"volume:{c.structure.id}: its {c.shape.phase} tasks cover "
                   f"{qty(c.counted_m3)} of {qty(c.floor_m3)} m3 ({pct(c.share, floor=True)}"
                   f"{c.of_floor}){c.deep_note(IN_SCOPE_SHARE)}, {rule}")
    for a in area_coverages(req, graph):
        if a.share < IN_SCOPE_SHARE:
            out.append(f"area:{a.structure.id}: its {a.robot} tasks treat {qty(a.area_m2)} of "
                       f"{qty(a.need_m2)} m2 ({pct(a.share, floor=True)}; {a.how}), {rule}")
    return out
