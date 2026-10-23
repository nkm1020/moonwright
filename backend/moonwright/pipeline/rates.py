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
:data:`VOLUME_HIGH` of the largest ("dug 1.6 times over") stays an advisory, and from
:data:`VOLUME_DOUBLE` of the largest volume the brief allows (plus :data:`VOLUME_DOUBLE_M3`)
it is a double count, the validator's conflict again (:func:`volume_double_counts`, phase
3i). Live, Nemotron 3 Ultra's first graph of the landing-pad demo hauled 2 x 120 m3 into the
120 m3 berm in 3 of 3 tries: 240 m3 is the berm's 40 x 1.5 x 4 m box, twice the triangular
section the check measures. The advisory did not stop it, and 2 of the 3 repairs answered the
notes with a 114.34 m3 borrow pit the brief never mentions (240 - 125.66 m3 of pad spoil: the
box again). The basis fields of the verified plans of runs 2 and 3 that hauled a berm at
200 % say the same: 12 of 14 sized the berm as its length x height x base width ("560 m3 / 2 =
280 m3" for habitat_berm_tight_horizon's 70 x 2 x 4 m berm, which its brief puts at about 280
cubic metres) and split that box evenly; the other 2 are ambiguous. The task-graph prompts now
give a berm's fill (length x height x base width / 2) and the verdict quotes the section. The
largest volume a double count is judged against is the dimensions', or a larger one the brief
states on the structure's line (:func:`_largest_m3`); none where the brief bounds none (a berm
whose base width it does not state, walls sloped by an unstated ratio), and a haul staged in a
pile for a later haul of the body counts once (:class:`VolumeCoverage`, ``staged``).
What the review of phase 3e made exact:

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
the work 15-25 % is a re-scope, not a success in scope; an excess is no gap. Phase 3g made the
area judge only what the brief states, as the volume does: the lift count is the one the
structure's own entry in the brief states (:func:`lifts`, :func:`stated_lifts`: "in three
lifts", "two compaction lifts", "compacted in 2 layers", the smaller of a range; the entry
read whole when hard-wrapped; a recorded count kept when the entry names lifts without a
count we can read; "a 20 cm compacted lift" is one), never "four 10 m segments" (Lightning
recorded crater_ramp's segments as 4 lifts and access_road's lift as 0.2, and a correct crater
ramp read 25 %), and the structure
stage writes that count into the requirements (:func:`with_judged_lifts`), so the planner
compacts what the eval judges. A placed body is judged on the base width the brief states
(:func:`base_width_stated`: its entry, or the line that states its length and height), never
on the 1:1 section the volume check assumes nor on a width the structure stage copied from
elsewhere; without one, on the least base it can have, as wide as it is high (length x
height), so a sliver of compaction on a width-less berm is still a gap.

Footprints (phase 3i)
---------------------
Nemotron 3.5 Lightning records a square footprint as a diameter: in the saved structurings of
live runs 2 and 3, the "12 m by 12 m" pit of habitat_foundation and habitat_berm_tight_horizon
(3 of 3 in run 3), the "10 m by 10 m" instrument_pad and the "each 8 m by 8 m" mast pads of
four_mast_pads came back as ``diameter_m`` (some beside a ``length_m``, one with an invented
``min_diameter_m``). A 12 m pit read as a bowl holds 113.1 m3, not 144 (pi/4, 78.5 %), above
the 70 % conflict, so the planner under-dug and the eval judged the plan in scope against the
wrong floor. :func:`footprint` reads the rectangle the structure's own entry states
(:func:`stated_footprint`: two length figures joined by "by", "x" or "×", the entry read
whole; never a size and a depth, a count times a length, nor the size of the parts of the work
or of a section: "3 x 50 m segments", "a 1.2 m by 0.8 m trench section"), never in an entry
that calls the structure round (diameter, circular, round, radius, ring, bowl), and only when
the recorded diameter is one of its figures and stands nowhere else in the entry, so a figure
pair about something else ("a 20 m pad ... centred in a 20 m by 30 m cleared apron") is no
footprint; Lightning's verb-like types ("excavate", "excavation_pit") that name no entry are
matched by the lines that state the structure's recorded size, when every one of them states
the same rectangle. :func:`with_judged_footprint` writes it in (``diameter_m`` and
``min_diameter_m`` dropped, ``length_m`` x ``width_m`` the brief's figures; ``min_length_m`` x
``min_width_m`` only where a recorded ``min_diameter_m`` and the entry state a smaller
rectangle, "may shrink to 12 m by 12 m"), the structure stage does so with an amber note
(``moonwright.pipeline.structure.correct_footprints``: "structure S1: diameter_m 12 -> 12 m x
12 m: the brief's entry for S1 states a 12 m by 12 m footprint"), and every volume and area
judgment applies it whenever the brief is known (:func:`dims_volumes`, :func:`_work_area`), so
a saved plan is judged against the brief's square whatever its requirements recorded.

Sizes (phase 3j)
----------------
Two more of Lightning's slips decide what a plan is judged against. low_battery_pad (kind E)
allows "any diameter between 12 m and 20 m that fits" in a constraint, and Lightning recorded
no ``min_diameter_m`` in 6 of 6 structurings of runs 2 and 3, so the plans that dug a 16.8 m
bowl, inside the allowed range, were judged against 20 m (0 of 6 in scope in run 3; the
structure prompt asks for the key, the brief_budget registry records it). And run 3 recorded
its stockpile "30 m north of the pad" as a 30 m diameter (typed ``spoil_berm``, so its
compaction was judged over 707 m2), run 2 as a 30 m length. :func:`with_brief_sizes` writes the
sizes the brief states in: a smaller size it allows for a dimension the structure records
(:func:`stated_minimums`: a range, "between 12 m and 20 m", "12 to 20 m", "12-20 m", "from" one
only after a verb that shrinks ("extend the road from 40 m to 60 m" gives the size it grows
from), or a smallest size, "may be reduced to 12 m", "down to 12 m", "at least 12 m", "no
smaller than 12 m", each bound to the nearest dimension word: diameter, length, width, depth,
deep, long, wide; "keeps its full 20 m diameter but its depth may be reduced to 0.3 m" bounds
the depth), as ``min_<dimension>_m``, only below the recorded size and only where none is
recorded; and every length the structure's own entry gives only as a distance dropped
(:func:`distance_figures`: "30 m north of the pad", "15 m from the pit", "80 m away", "5 m
beyond the rim"; never a span or an extent, "120 m from the depot to the pad", "runs 60 m from
the array"). A statement is tied to a structure (:func:`_takers`) by the subject its own
segment (cut at commas, brackets and conjunctions) gives it, when it gives one: the structure
that word names by its type or by the name its entry gives it ("the bowl" is the pad Lightning
typed ``landing_pad`` under "Landing pad bowl"), none when it names none ("the apron is at
least 5 m wide" sizes no pad); else by the nearest name in its sentence, its own segment's,
else an earlier segment's, the entry's own name when the search reaches it; two structures
named by different words leave it to none ("the stockpile next to the pad may shrink to 10 m").
With no name in its sentence the entry it stands in takes it, else a range goes by its top (the
one structure recorded at 20 m: in low_battery_pad's constraint nothing before "or any diameter
between 12 m and 20 m that fits" in its sentence names a structure, the bowl and the stockpile
come after it) and a smallest size, on a line that names no structure at all, to the one
structure that records that dimension. A statement about something else sizes nothing: the
Site, Fleet and Deadline paragraphs are not read, a quantity of something is no size ("at least
0.5 m of compacted fill"), and a clause about a part of a structure, the ground, a treatment or
the way between structures ("with a crest at least 2 m wide", "loose regolith at least 40 cm
deep", "compacted to a depth of at least 0.3 m", "the haul length to the berm is at least 40
m") sizes only a structure whose type has that word. Each of these was read as a minimum the
brief never allows before the review's fixes (``tests/test_sizes.py``). The structure stage
writes the sizes with an amber note (``moonwright.pipeline.structure.correct_sizes``:
"structure S1: min_diameter_m none -> 12: the brief allows "any diameter between 12 m and 20 m
that fits"", "structure S2: diameter_m 30 dropped: the brief's entry for S2 states 30 m only as
a distance ("30 m north of the pad")"), and every volume and area judgment reads them from the
brief the same way (``brief_sizes=False`` judges the sizes as recorded;
``scripts/rescore_eval.py`` compares the two). Of the 22 eval briefs only low_battery_pad
states either.

Supply (phase 3g)
-----------------
:func:`is_dig` (an excavator task in m3 that is no :func:`rehandles`), :func:`is_strip` (one in
m2: a surface layer, a source for the hauls it can feed), :func:`supply_m3` (what a source
makes available: a dig's m3, a strip's area x :data:`STRIP_DEPTH_M`) and :func:`is_haul` (a
hauler task in m3), with :func:`ancestors` (every task's transitive ``after``), are what the
validator's ``supply:<haul>`` check and its balance advisory read
(``moonwright.pipeline.validate``, "Supply order"): a haul waits for the digs that produce
its material.

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

import functools
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from moonwright.pipeline.tags import (
    FIGURE,
    WORDS,
    brief_lines,
    entry_head,
    entry_line,
    entry_owners,
    figures_m,
    haul_target,
    name_score,
    serving_tasks,
    states_figures,
)
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
from moonwright.text import an, join_and, pct, plural, qty

ADVISORY_THRESHOLD = 0.5   # relative deviation of the model's estimate that earns an advisory
VOLUME_LOW = 0.7           # volume:<structure> when the planned m3 are below this share of the
VOLUME_HIGH = 1.5          # smallest volume the brief allows (a conflict), above this multiple
VOLUME_DOUBLE = 1.75       # of the largest one (an advisory), or at this multiple of it plus
VOLUME_DOUBLE_M3 = 1.0     # this many m3 or more (a double count: a conflict again)
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
# a rectangular footprint: two length figures joined by "by", "x" or "×", a unit on one of them
# at least ("12 m by 12 m", "10 x 10 m", "40 m by 6 m", "30 m x 18 m", "120 cm by 80 cm"), or
# the first two of three ("12 x 12 x 1 m": length, width, depth); a pair whose second figure
# is a depth, a height or a length alone ("a bowl 20 m by 0.4 m deep", "20 m by 0.4 m, deep",
# "2 x 60 m long") is none, nor is a count times a volume ("2 x 60 m3": no length unit), nor a
# pair that sizes the parts of the work or a section ("laid as 3 x 50 m segments", "a 1.2 m by
# 0.8 m trench section", "sintered in 2 m x 2 m tiles"; seen in review, each read as a
# footprint)
_BY = r"\s*(?:\bby\b|x|×)\s*"
_FOOTPRINT_RE = re.compile(
    rf"(?<![\w.])(?P<a>\d+(?:\.\d+)?)\s*(?P<ua>cm|m)?{_BY}(?P<b>\d+(?:\.\d+)?)\s*(?P<ub>cm|m)?"
    rf"(?:{_BY}(?P<c>\d+(?:\.\d+)?)\s*(?P<uc>cm|m)?)?(?!\w|\.\d)",
    re.IGNORECASE)
_DEPTH_WORD_RE = re.compile(r"\s*,?\s*(?:deep|high|tall|thick|long)\b", re.IGNORECASE)
_PART_WORD_RE = re.compile(   # up to two words, then a noun for parts of the work or a section
    r"(?:\s+[a-z]+){0,2}?\s+(?:segments?|sections?|cross-sections?|strips|lengths|bays|tiles"
    r"|panels|blocks|cells|rows|runs|passes|lifts|layers)\b", re.IGNORECASE)
# an entry that calls its structure round states no rectangle, whatever pair it holds
# ("circular, 20 m diameter", "a circular bowl 12 m in diameter", "a 20 m bowl ... centred in a
# 20 m by 30 m apron"; "around the pit" and "bearing slab" are no such words)
_ROUND_RE = re.compile(r"\b(?:diameters?|circular|circles?|round|radius|radii|rings?|bowls?)\b",
                       re.IGNORECASE)
_DIAMETER_KEYS = ("diameter_m", "min_diameter_m")
_LIFT_KEYS = ("lifts", "lift_count", "compaction_lifts")
_MAX_LIFTS = 10
_COUNT_WORDS = {"one": 1, "single": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_COUNT = r"(?:\d+|" + "|".join(_COUNT_WORDS) + r")"
# a count of compaction layers the brief states: "in three lifts", "three compacted lifts",
# "two compaction lifts", "in one lift", "two 15 cm lifts", "in three 30-cm lifts", "a two-lift
# berm", "compacted in 2 layers". The count stands right before the noun (thicknesses and a
# few adjectives may sit between, in any order), so "in four 10 m segments" (no lift) states
# none; an article is one lift ("a 20 cm compacted lift", "a compacted 10 cm bedding layer":
# seen live, Lightning recorded cable_trench's "three 50 m segments" as 3 lifts beside that
# bedding layer), read only when no clause of the line states a number; a decimal ("0.2") is
# never a count, and a range ("2-3 lifts", "3 to 4 lifts") states its smaller count, the
# smallest size the brief allows.
# "layer(s)" is a count of lifts only in a clause that compacts and never in one that sinters
# or prints ("sintered in two layers" is the printer's work), nor as a hyphenated compound
# ("a two-layer regolith shield")
_LIFT_COUNT_RE = re.compile(
    rf"(?<![\w.])(?:(?P<n>{_COUNT})(?!\.\d)"
    rf"(?:\s*(?:-|\u2013|to|or)\s*(?P<m>{_COUNT})(?!\.\d))?|(?P<a>an?))"
    r"(?:[\s-]+(?:\d+(?:\.\d+)?[\s-]*(?:cm|mm|m)\b|compacted|compaction|full|separate"
    r"|successive|equal|even|horizontal|thin|uniform|individual|distinct|placed|fill|regolith"
    r"|loose|bedding|base))*"
    r"(?P<sep>[\s-]+)(?P<noun>lifts?|layers?)\b",
    re.IGNORECASE)
_LIFTS_KEY_RE = re.compile(rf"\blifts?\s*[:=]\s*(?P<n>{_COUNT})(?![\w.])", re.IGNORECASE)  # "lifts: 3"
_LIFT_MENTION_RE = re.compile(r"\b(?:lifts?|layers?)\b", re.IGNORECASE)
_COMPACTS_RE = re.compile(r"\b(?:compact|tamp|roll|consolidat)", re.IGNORECASE)
_SURFACES_RE = re.compile(r"\b(?:sinter|print|melt|pav|fus)", re.IGNORECASE)
_LIFT_CLAUSE_SPLIT = re.compile(r"\.(?=\s|$)|[;,()]")   # keeps "0.5 m" whole
# a placed body's base width as its entry states it: a figure in a clause that says width or
# base ("4 m wide at the base", "4 m at the base", "a base 3.5 m wide"), never the clause of a
# key trench or a crest ("on a key trench 1.2 m wide", "the crest sintered 1 m wide")
_WIDTH_CLAUSE_SPLIT = re.compile(r"\.(?=\s|$)|[,;()]|\band\b")
_WIDTH_WORD_RE = re.compile(r"\b(?:wide|width|base|broad|across)\b", re.IGNORECASE)
_NOT_BASE_RE = re.compile(r"\b(?:trench|crest|key)\b", re.IGNORECASE)
# a volume a brief line states: "about 144 cubic metres", "(about 280 cubic metres of fill)",
# "60 m3", "1,125 m³"
_VOLUME_FIGURE_RE = re.compile(
    r"(?<![\w.,])(\d[\d,]*(?:\.\d+)?)\s*(?:cubic\s+met(?:re|er)s?|m3|m³)(?!\w)", re.IGNORECASE)
# sizes the brief states (phase 3j): a smaller size it allows for a dimension, a range ("any
# diameter between 12 m and 20 m", "12 to 20 m wide", "a 12-20 m diameter", a unit on the
# second figure at least) or a smallest size ("may be reduced to 12 m", "down to 12 m", "at
# least 12 m", "no smaller than 12 m"), and a figure it gives only as a distance
_SIZE_RANGE_RE = re.compile(
    r"(?<![\w.])(?:between\s+(?P<a>\d+(?:\.\d+)?)\s*(?P<ua>cm|m)?\s+and\s+"
    r"|(?:(?P<from_>from)\s+)?(?P<a2>\d+(?:\.\d+)?)\s*(?P<ua2>cm|m)?(?:\s+to\s+|\s*[-–—]\s*))"
    r"(?P<b>\d+(?:\.\d+)?)\s*(?P<ub>cm|m)\b", re.IGNORECASE)
# "from 20 m to 12 m" allows the smaller size only after a verb that shrinks or a word that
# offers the choice ("may shrink from 20 m to 12 m", "anywhere from 12 m to 20 m"): "extend the
# existing road from 40 m to 60 m long" and "widen the lane from 3 m to 5 m" give the size it
# grows from (seen in review, read as a 40 m and a 3 m minimum)
_SHRINK_RE = re.compile(r"\b(?:reduc\w*|shrink\w*|shrunk|smaller|any|anywhere)\b", re.IGNORECASE)
_SIZE_FLOOR_RE = re.compile(
    r"\b(?:(?:reduced|reduce|shrunk|shrink|shrinks)\s+(?:down\s+)?to|(?P<down>down\s+to)"
    r"|at\s+least|no\s+(?:smaller|less|narrower|shorter|shallower)\s+than)\s+"
    r"(?P<a>\d+(?:\.\d+)?)\s*(?P<ua>cm|m)\b", re.IGNORECASE)
# "excavated down to 0.5 m", "dig the pad down to 0.4 m": a depth the dig reaches, not a size
# the brief lets it shrink to ("the trench may go down to 0.4 m" is one)
_DUG_DOWN_RE = re.compile(r"\b(?:(?:excavat\w*|dug|dig\w*)\b(?:\W+[^\W\d]+){0,3}|(?:cut\w*|"
                          r"trench\w*|bored|drilled|sunk|sink\w*)\b)\W*$", re.IGNORECASE)
# the dimension a size statement bounds: a word right after its figure ("20 m deep", "12 m in
# diameter", "12-20 m diameter"; one the reader does not take, "2 m high", "0.3 m thick", "12 m
# across", bounds nothing it reads), else the nearest dimension noun shortly before it, with no
# figure, no other dimension word and no conjunction between ("any diameter between", "the
# depth may be reduced to"; seen in review, "keeps its full 20 m diameter but its depth may be
# reduced to 0.3 m" was read as a 0.3 m diameter, and the bowl's floor fell to nothing)
_SIZE_KEYS = {"diameter": "diameter_m", "length": "length_m", "long": "length_m",
              "width": "width_m", "wide": "width_m", "depth": "depth_m", "deep": "depth_m"}
_DIM_WORDS = (r"diameter|length|width|depth|deep|long|wide|height|high|tall|thickness|thick|"
              r"across|radius")
_DIM_AFTER_RE = re.compile(rf"\s*(?:in\s+)?(?P<dim>{_DIM_WORDS})s?\b", re.IGNORECASE)
_OF_RE = re.compile(r"\s+of\b", re.IGNORECASE)   # "at least 0.5 m of fill": a quantity of it
_DIM_BEFORE_RE = re.compile(
    r"\b(?P<dim>diameter|length|width|depth|height|thickness|radius)s?\b"
    rf"(?:\W+(?!(?:{_DIM_WORDS}|and|but|or|while|whereas|though|although|yet)s?\b)"
    r"[^\W\d]+){0,6}\W*$", re.IGNORECASE)
_SIZE_CLAUSE_SPLIT = re.compile(r"\.(?=\s|$)|[;,()]")   # keeps "0.5 m" whole
# where the subject of a size statement is looked for (:func:`_segments`): its sentence, cut
# at a full stop or a semicolon, and in it the segments between commas, brackets and
# conjunctions
_SENTENCE_END = re.compile(r"\.(?=\s|$)|;")
_SEGMENT_CUT = re.compile(r"[,()]|\b(?:and|but|or|while|whereas|though|although|yet|if|unless|"
                          r"when|because|since)\b", re.IGNORECASE)
# the subject a statement's own segment gives it (:func:`_subject`): the last word before it
# that is no linking word, figure or unit ("the apron is at least 5 m wide": apron; "the
# surveyor's stakes are at least 0.2 m deep": stakes; "the pad may drop down to": pad); one
# that names no other thing is none ("its depth may be reduced to", "any diameter between",
# "a smaller one", a statement that opens its segment)
_LINKING = frozenset({
    "is", "are", "was", "were", "be", "been", "being", "may", "might", "can", "could", "must",
    "should", "shall", "will", "would", "also", "still", "only", "then", "now", "even", "just",
    "not", "to", "of", "at", "s", "anywhere", "somewhere", "safely", "ideally", "preferably",
    "nominally", "roughly", "about", "around", "approximately", "go", "goes", "get", "gets",
    "come", "comes", "drop", "drops", "fall", "falls", "shrink", "shrinks", "narrow", "narrows",
    "shorten", "shortens", "scaled", "taken", "brought", "back", "allowed", "permitted", "kept",
    "keep", "keeps", "stay", "stays", "remain", "remains", "measure", "measures", "span",
    "spans", "vary", "varies", "need", "needs", "has", "have", "dug", "excavated", "cut",
    "built", "made", "sized", "set", "m", "cm"})
_SIZE_SUBJECTS = frozenset({"diameter", "length", "width", "depth", "height", "thickness",
                            "radius", "size", "footprint", "dimension", "dimensions", "one",
                            "it", "its", "them", "all", "any", "anything", "the", "a", "an"})
# a statement whose segment is about something other than a structure's body sizes only a
# structure whose type has the word: a part of it ("with a crest at least 2 m wide", "on a key
# trench at least 1 m wide", "a cable bed at least 0.3 m deep"), the ground ("loose regolith
# at least 40 cm deep"), a treatment ("subgrade compacted to a depth of at least 0.3 m") or the
# way between structures ("the haul length to the berm is at least 40 m"), each seen in
# review read as a smaller size of the structure; "compacted to" stands as "compaction"
_PART_RE = re.compile(
    r"\b(?P<part>crest|key|trench|toe|bed|bedding|slab|layer|lift|subgrade|regolith|fines|soil|"
    r"bedrock|haul|route|travel|path|corridor|gap|clearance|spacing|buffer|setback|margin|"
    r"cable)s?\b|\bcompact\w*\s+to\b|\bcompaction\b", re.IGNORECASE)
# the brief's own paragraphs about the ground, the robots and the time size no structure:
# "**Site.** ... loose regolith at least 40 cm deep" is not the pad's smallest depth
_NOT_SIZED_RE = re.compile(r"^\W*(?:site|fleet|deadline)\b", re.IGNORECASE)
# a figure that places something rather than sizing it: "30 m north of the pad", "15 m from the
# pit", "80 m away", "5 m beyond the rim"; never a span, a "to" later in its clause ("120 m
# from the depot to the pad", a road's length), or an extent ("runs 60 m from the array", "a
# ring extending 5 m beyond the pad edge"), each seen in review dropped as a distance
_DISTANCE_RE = re.compile(
    r"\s+(?:(?:north|south|east|west)(?:-?(?:east|west))?\s+of|(?P<from_>from)|away|beyond)\b",
    re.IGNORECASE)
_EXTENT_RE = re.compile(r"\b(?:runs?|running|ran|extend\w*|stretch\w*|reach\w*|span\w*|leads?|"
                        r"leading|goes|going|continu\w*)\W+(?:(?:for|some|about)\s+)?$",
                        re.IGNORECASE)
_TO_RE = re.compile(r"\bto\b", re.IGNORECASE)


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
    unstated ratio). ``kind`` is bowl / box / trench / prism. ``box`` is a triangular prism's
    length x height x base width ("40 x 1.5 x 4"), the box its section is half of (phase 3i:
    live plans sized berms as that box and hauled twice the prism); '' for every other shape.
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
    box: str = ""

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


def stated_footprints(line: str | None) -> list[tuple[float, float]]:
    """Every rectangle a brief line states, ``(first, second)`` in metres, in order: each pair
    of length figures joined by "by", "x" or "×" (:data:`_FOOTPRINT_RE`: "12 m by 12 m", "10 x
    10 m", "40 m by 6 m", "30 m x 18 m", the first two of "12 x 12 x 1 m", cm converted), never
    a figure and a depth ("a bowl 20 m by 0.4 m deep"), a count times a length or a volume ("2
    x 60 m long", "2 x 60 m3") nor the size of the parts of the work or of a section
    (:data:`_PART_WORD_RE`: "3 x 50 m segments", "a 1.2 m by 0.8 m trench section"); [] when
    the line states none, or calls its structure round (:data:`_ROUND_RE`: "circular, 20 m
    diameter", "a circular bowl 12 m in diameter", "a 20 m bowl"), whatever pair it also
    holds."""
    if not line or _ROUND_RE.search(line):
        return []
    out: list[tuple[float, float]] = []
    for m in _FOOTPRINT_RE.finditer(line):
        second_unit = m["ub"] or m["uc"]
        first_unit = m["ua"] or second_unit
        if first_unit is None:
            continue                       # no length unit: a count, a duration
        if m["c"] is None and _DEPTH_WORD_RE.match(line, m.end()):
            continue                       # "20 m by 0.4 m deep": a size and a depth
        if _PART_WORD_RE.match(line, m.end()):
            continue                       # "3 x 50 m segments": the parts of the work
        first = float(m["a"]) / (100 if first_unit.lower() == "cm" else 1)
        second = float(m["b"]) / (100 if (second_unit or first_unit).lower() == "cm" else 1)
        if first > 0 and second > 0:
            out.append((first, second))
    return out


def stated_footprint(line: str | None) -> tuple[float, float] | None:
    """The rectangular footprint a brief line states: the first of :func:`stated_footprints`;
    None when it states none or calls its structure round."""
    found = stated_footprints(line)
    return found[0] if found else None


def _recorded_diameter(structure: Structure) -> tuple[list[str], float] | None:
    """``(keys, value)``: the diameter keys a structure records (``diameter_m``,
    ``min_diameter_m``) and the figure a footprint must contain, the plain diameter when it
    is there; None without either."""
    d = structure.dims()
    keys = [k for k in _DIAMETER_KEYS if k in d]
    return (keys, d[keys[0]]) if keys else None


def _rectangle(line: str, diameter: float) -> tuple[float, float] | None:
    """The footprint ``line`` states for a structure recorded with ``diameter``: its first
    rectangle (:func:`stated_footprint`) when the diameter is one of the two figures and the
    line states that figure nowhere outside its figure pairs; None otherwise. Seen in review:
    in "a 20 m bowl excavated 0.4 m deep, centred in a 20 m by 30 m cleared apron" the pair
    sizes the apron, and read as the pad's footprint it asked for a 240 m3 box instead of the
    125.7 m3 bowl (a bowl is a round word too, :data:`_ROUND_RE`; "a 20 m pad" in its place
    is caught here)."""
    found = stated_footprint(line)
    if found is None or not any(math.isclose(diameter, f, abs_tol=1e-6) for f in found):
        return None
    alone = _FOOTPRINT_RE.sub(" ", line)
    if any(math.isclose(diameter, f, abs_tol=1e-6) for f in figures_m(alone)):
        return None
    return found


def _footprint_line(structure: Structure, req: Requirements | None
                    ) -> tuple[str, tuple[float, float]] | None:
    """``(line, (length, width))``: the brief line that states the rectangle of a structure
    recorded with a diameter, and that rectangle (:func:`footprint`); None when there is
    none."""
    recorded = _recorded_diameter(structure)
    if recorded is None or req is None or not req.source_brief:
        return None
    diameter = recorded[1]
    entry = entry_line(req.source_brief, structure)
    if entry is not None and _ROUND_RE.search(entry):
        return None
    if entry is not None and (stated_footprint(entry) is not None
                              or states_figures(entry, structure)):
        found = _rectangle(entry, diameter)    # its own entry decides
        return None if found is None else (entry, found)
    lines = [line for line in brief_lines(req.source_brief)
             if line != entry and not line.lstrip().startswith("#")]
    if any(_ROUND_RE.search(line) and any(math.isclose(f, diameter, abs_tol=1e-6)
                                           for f in figures_m(line)) for line in lines):
        return None
    # every line that states its size must state the same rectangle: one without a rectangle
    # for it is its own entry under a type no line names ("Landing pad: 12 m across" beside
    # "Store: 12 m by 12 m", seen in review), and the store's square is not the pad's
    sized = [line for line in lines if states_figures(line, structure)]
    rectangles = {_rectangle(line, diameter) for line in sized}
    if len(rectangles) != 1:
        return None
    found = rectangles.pop()
    return None if found is None else (sized[0], found)


def footprint(structure: Structure, req: Requirements | None = None) -> tuple[float, float] | None:
    """The rectangle the brief states for a structure that records a diameter (module
    docstring, "Footprints"): ``(length, width)`` in metres from the structure's own entry
    (:func:`~moonwright.pipeline.tags.entry_line`, read whole; :func:`stated_footprint`), only
    when the recorded diameter is one of its two figures and the entry states that figure
    nowhere else (:func:`_rectangle`: "a 20 m pad ... centred in a 20 m by 30 m apron" is no
    20 x 30 m box). An entry that calls the structure round keeps the diameter, and so does an
    entry that states the structure's recorded size without a pair. When the type's words name
    no line, or a line that is not its entry (Lightning typed habitat_berm_tight_horizon's pit
    "excavate", which no line names, and "excavation_pit", whose words pick a constraint),
    the lines that state every length figure the structure records stand in for it
    (:func:`~moonwright.pipeline.tags.states_figures`) when each states the same rectangle
    and no round line of the brief states the recorded diameter (that circle may be the
    structure's own): one of them without a rectangle ("Landing pad: 12 m across" beside
    "Store: 12 m by 12 m") keeps the diameter. None without the brief (``req.source_brief``),
    a recorded diameter or a stated rectangle: no footprint is ever invented."""
    found = _footprint_line(structure, req)
    return None if found is None else found[1]


def _stated_minimum(structure: Structure, line: str,
                    found: tuple[float, float]) -> tuple[float, float] | None:
    """The smaller rectangle ``line`` states after the footprint ``found`` for a structure that
    records ``min_diameter_m``: the first later pair that holds the recorded minimum and
    covers less ("20 m by 20 m ...; it may shrink to 12 m by 12 m"); None without a recorded
    minimum or such a pair (an invented ``min_diameter_m`` equal to the diameter, run 3: no
    minimum)."""
    least = structure.dims().get("min_diameter_m")
    if least is None:
        return None
    for pair in stated_footprints(line)[1:]:
        if (any(math.isclose(least, f, abs_tol=1e-6) for f in pair)
                and pair[0] * pair[1] < found[0] * found[1]):
            return pair
    return None


def _judged_footprint(structure: Structure, req: Requirements | None
                      ) -> tuple[tuple[float, float], tuple[float, float] | None] | None:
    """``(footprint, minimum)`` of :func:`footprint` and :func:`_stated_minimum`; None when the
    brief states no rectangle for the structure."""
    found = _footprint_line(structure, req)
    if found is None:
        return None
    line, pair = found
    return pair, _stated_minimum(structure, line, pair)


def footprint_note(structure: Structure, req: Requirements) -> str | None:
    """The correction of a structure's recorded diameter to the rectangle the brief states
    (:func:`footprint`), as the structure stage's warning reads it: "diameter_m 12 -> 12 m x
    12 m: the brief's entry for S1 states a 12 m by 12 m footprint" ("diameter_m 12,
    min_diameter_m 12 -> ..." when both were recorded; "diameter_m 20, min_diameter_m 12 -> 20
    m x 20 m, at least 12 m x 12 m: ... states a 20 m by 20 m footprint, at least 12 m by 12 m"
    when the entry states the smaller rectangle too); None when the brief states no rectangle
    for it."""
    judged = _judged_footprint(structure, req)
    recorded = _recorded_diameter(structure)
    if judged is None or recorded is None:
        return None
    found, least = judged
    d = structure.dims()
    keys = ", ".join(f"{k} {qty(d[k])}" for k in recorded[0])
    a, b = (qty(x) for x in found)
    floor_x = floor_by = ""
    if least is not None:
        lo_a, lo_b = (qty(x) for x in least)
        floor_x, floor_by = f", at least {lo_a} m x {lo_b} m", f", at least {lo_a} m by {lo_b} m"
    return (f"{keys} -> {a} m x {b} m{floor_x}: the brief's entry for {structure.id} states "
            f"{an(f'{a} m by {b} m footprint')}{floor_by}")


def _write_pair(data: dict[str, object], recorded: dict[str, float], keys: tuple[str, str],
                pair: tuple[float, float]) -> None:
    """Write ``pair`` under ``keys`` into ``data`` (a structure's dump), leaving a recorded pair
    that agrees as it is, in either order; a whole figure is written as an integer."""

    def same(x: float | None, y: float) -> bool:
        return x is not None and math.isclose(x, y, abs_tol=1e-6)

    was = (recorded.get(keys[0]), recorded.get(keys[1]))
    if same(was[0], pair[1]) and same(was[1], pair[0]):
        return
    for key, value, old in zip(keys, pair, was, strict=True):
        if not same(old, value):
            data[key] = int(value) if float(value).is_integer() else value


def with_judged_footprint(structure: Structure, req: Requirements | None) -> Structure:
    """The structure with the footprint of :func:`footprint` written in: ``diameter_m`` and
    ``min_diameter_m`` dropped, ``length_m`` and ``width_m`` the brief's first and second
    figure, and ``min_length_m`` x ``min_width_m`` only where a recorded ``min_diameter_m``
    and the entry state a smaller rectangle (:func:`_stated_minimum`; a recorded pair that
    agrees stays, in either order); the structure itself when the brief states no rectangle
    for it."""
    judged = _judged_footprint(structure, req)
    if judged is None:
        return structure
    found, least = judged
    data = {k: v for k, v in structure.model_dump().items() if k not in _DIAMETER_KEYS}
    d = structure.dims()
    _write_pair(data, d, ("length_m", "width_m"), found)
    if least is not None:
        _write_pair(data, d, ("min_length_m", "min_width_m"), least)
    return Structure.model_validate(data)


# ----------------------------------------------------------------------- sizes (phase 3j)
@dataclass(frozen=True)
class SizeStatement:
    """A smaller size a brief line allows for one dimension (:func:`_size_statements`):
    ``key`` the recorded dimension it bounds ('diameter_m'), ``least`` the smallest value and,
    for a range, ``full`` its top (metres); ``line`` is the brief line it stands on, ``quote``
    its clause ('any diameter between 12 m and 20 m that fits'), ``at`` where the statement
    starts in the line, ``spans`` the spans of the line its subject is looked for in, nearest
    first (:func:`_segments`), and ``parts`` the words of its own segment that name something
    other than a structure's body ('crest', 'regolith', 'compaction'; :data:`_PART_RE`)."""

    key: str
    least: float
    full: float | None
    line: str
    quote: str
    at: int = 0
    spans: tuple[tuple[int, int], ...] = ()
    parts: frozenset[str] = frozenset()


def _metres(value: str, unit: str | None) -> float:
    return float(value) / (100 if (unit or "m").lower() == "cm" else 1)


def _size_key(clause: str, start: int, end: int) -> str | None:
    """The recorded dimension a size statement at ``clause[start:end]`` bounds: the dimension
    word right after it (:data:`_DIM_AFTER_RE`: "20 m deep", "12 m in diameter"; "2 m high"
    bounds none the reader takes), else, unless a quantity of something follows ("at least
    0.5 m of compacted fill"), the nearest dimension noun shortly before it, only words
    between (:data:`_DIM_BEFORE_RE`: "any diameter between", "the depth may be reduced to");
    None without one."""
    m = _DIM_AFTER_RE.match(clause, end)
    if m is None and _OF_RE.match(clause, end) is None:
        m = _DIM_BEFORE_RE.search(clause, 0, start)
    return None if m is None else _SIZE_KEYS.get(m["dim"].lower())


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _subject(text: str) -> str | None:
    """The word the text of a statement's own segment before it gives the statement as its
    subject, singular: the last that is no linking word, figure or unit (:data:`_LINKING`:
    "the apron is at least 5 m wide" -> 'apron', "with a crest at least 2 m wide" -> 'crest',
    "the pad may drop down to" -> 'pad'); None when there is none, or it names the dimension
    itself or stands for the structure (:data:`_SIZE_SUBJECTS`: "its depth may be reduced
    to", "any diameter between"). A verb the list lacks stands as a subject no structure has:
    the statement then sizes nothing."""
    words = [w for w in WORDS.findall(text.lower()) if not w.isdigit()]
    while words and words[-1] in _LINKING:
        words.pop()
    if not words or words[-1] in _SIZE_SUBJECTS:
        return None
    return _singular(words[-1])


def _aliases(brief: str, structures: Sequence[Structure]) -> list[frozenset[str]]:
    """Per structure, the words of the name its own entry gives it (the name before the colon
    of each line :func:`~moonwright.pipeline.tags.entry_owners` gives it, singular): "1.
    Landing pad bowl: ..." names a structure Lightning typed ``landing_pad`` "bowl" too."""
    entries = [(end, entry_owners(line, structures), line) for line in brief_lines(brief)
               if (end := entry_head(line)) is not None]
    return [frozenset(_singular(w) for end, owners, line in entries if any(s is o for o in owners)
                      for w in WORDS.findall(line[:end].lower()) if not w.isdigit())
            for s in structures]


def _segments(line: str, start: int, end: int) -> tuple[tuple[int, int], ...]:
    """The spans of ``line`` the size statement at ``line[start:end]`` may be about, nearest
    first: its own segment, cut at the commas, brackets and conjunctions around it (never
    inside it: "between 12 m and 20 m"), then each earlier segment of its sentence, back to
    the sentence's start (a full stop or a semicolon)."""
    s0 = max((m.end() for m in _SENTENCE_END.finditer(line, 0, start)), default=0)
    s1 = next((m.start() for m in _SENTENCE_END.finditer(line, end)), len(line))
    bounds = [s0]
    for m in _SEGMENT_CUT.finditer(line, s0, s1):
        if m.end() <= start or m.start() >= end:
            bounds += [m.start(), m.end()]
    bounds.append(s1)
    spans = list(zip(bounds[::2], bounds[1::2], strict=True))
    own = next(i for i, (a, b) in enumerate(spans) if a <= start and end <= b)
    return tuple(reversed(spans[:own + 1]))


@functools.lru_cache(maxsize=64)
def _size_statements(brief: str) -> tuple[SizeStatement, ...]:
    """Every smaller size the brief allows, in order: a range ("any diameter between 12 m and
    20 m", "12 to 20 m wide", "a 12-20 m diameter"; "from 20 m to 12 m" only after a verb that
    shrinks or a word that offers the choice, :data:`_SHRINK_RE`) or a smallest size ("the
    diameter may be reduced to 12 m", "down to 12 m in diameter", "at least 12 m long", "no
    smaller than 12 m wide"), each bound to a dimension by a word beside it
    (:func:`_size_key`; none, none read: "keep at least 30 m from the pad"), with the spans
    its subject is looked for in (:func:`_segments`) and the part words of its own segment
    (:data:`_PART_RE`). "Down to" after a dig is a depth reached, not a size allowed
    ("excavated down to 0.5 m deep"), and a range is read only when its two figures differ.
    Headings and the Site, Fleet and Deadline paragraphs are skipped (:data:`_NOT_SIZED_RE`);
    a hard-wrapped line reads whole."""
    out: list[SizeStatement] = []
    for line in brief_lines(brief):
        if line.lstrip().startswith("#") or _NOT_SIZED_RE.match(line):
            continue
        cuts = [0, *(i for m in _SIZE_CLAUSE_SPLIT.finditer(line) for i in (m.start(), m.end())),
                len(line)]
        for c0, c1 in zip(cuts[::2], cuts[1::2], strict=True):
            clause = line[c0:c1]
            found: list[tuple[int, int, float, float | None]] = []
            for m in _SIZE_RANGE_RE.finditer(clause):
                if m["from_"] and not _SHRINK_RE.search(clause, 0, m.start()):
                    continue
                a, b = m["a"] or m["a2"], m["b"]
                unit = m["ua"] or m["ua2"] or m["ub"]
                lo, hi = sorted((_metres(a, unit), _metres(b, m["ub"])))
                if 0 < lo < hi:
                    found.append((m.start(), m.end(), lo, hi))
            for m in _SIZE_FLOOR_RE.finditer(clause):
                if m["down"] and _DUG_DOWN_RE.search(clause, 0, m.start()):
                    continue
                least = _metres(m["a"], m["ua"])
                if least > 0:
                    found.append((m.start(), m.end(), least, None))
            for start, end, least, full in sorted(found, key=lambda f: f[0]):
                key = _size_key(clause, start, end)
                if key is None:
                    continue
                spans = _segments(line, c0 + start, c0 + end)
                own = line[spans[0][0]:spans[0][1]]
                parts = frozenset((m["part"] or "compaction").lower()
                                  for m in _PART_RE.finditer(own))
                quote = re.sub(r"^[\s\-*#>]+|[\s*]+$", "", " ".join(clause.split()))
                quote = re.sub(r"^(?:or|and|but|so|then)\s+", "", quote, flags=re.IGNORECASE)
                if len(quote) > 80:
                    quote = quote[:77].rstrip() + "..."
                out.append(SizeStatement(key=key, least=least, full=full, line=line,
                                         quote=quote, at=c0 + start, spans=spans, parts=parts))
    return tuple(out)


def _named(text: str, structures: Sequence[Structure]) -> list[Structure] | None:
    """The structure ``text`` names, as a one-item list: of those whose type words it holds
    (:func:`~moonwright.pipeline.tags.name_score`), the one that scores best while every
    other is named only by words it holds too ("the landing pad" beside a ``mast_pad``); []
    when it names two by different words ("the stockpile next to the pad", seen in review
    handing the stockpile's 10 m to the pad) or alike (two ``mast_pad``); None when it names
    none."""
    low = text.lower()
    words = set(WORDS.findall(low))
    named = [(s, key[0], {w for w in WORDS.findall(s.type.lower()) if w in words})
             for s in structures if (key := name_score(low, words, s)) is not None]
    if not named:
        return None
    best = max(named, key=lambda n: n[1])
    if all(n is best or (n[1] < best[1] and n[2] <= best[2]) for n in named):
        return [best[0]]
    return []


def _takers(st: SizeStatement, structures: Sequence[Structure],
            aliases: Sequence[frozenset[str]] = ()) -> list[Structure]:
    """The structure a size statement is about, as a one-item list ([] when it is about none
    or the brief does not say which), and only if it records the statement's dimension above
    its smallest value (for a range, at its top: the full size it runs down from) and its type
    has every part word of the statement (:attr:`SizeStatement.parts`: "with a crest at least
    2 m wide" sizes no berm). A subject its own segment gives it decides (:func:`_subject`):
    the one structure that word names, by a word of its type or of the name its entry gives
    it (``aliases``, :func:`_aliases`), none when it names none ("the apron is at least 5 m
    wide" sizes no pad) or the segment names another. Else the subject is looked for nearest
    first (:attr:`SizeStatement.spans`): the first segment of the statement's sentence that
    names a structure decides it (:func:`_named`: two by different words, none), and the
    entry the line is (:func:`~moonwright.pipeline.tags.entry_owners`) decides it when the
    search reaches the entry's name, unless the rest of that segment names another structure
    ("1. Landing pad: 20 m by 20 m with a spoil berm at least 2 m wide" sizes no pad). With no
    name in its sentence: the entry the line is, else for a range the only structure recorded
    at its top ("or any diameter between 12 m and 20 m that fits" in low_battery_pad's
    constraint, the 20 m bowl), else for a smallest size, on a line that names no structure at
    all, the only structure that records that dimension."""
    def fits(s: Structure) -> bool:
        v = s.dims().get(st.key, 0.0)
        if v <= 0 or st.least >= v - 1e-6 or not st.parts <= set(WORDS.findall(s.type.lower())):
            return False
        return st.full is None or math.isclose(v, st.full, abs_tol=1e-6)

    def only(found: Sequence[Structure]) -> list[Structure]:
        return [found[0]] if len(found) == 1 and fits(found[0]) else []

    owners = entry_owners(st.line, structures)
    a, b = st.spans[0]
    subject = _subject(st.line[a:st.at])
    if subject is not None:
        padded = [*aliases, *[frozenset[str]()] * (len(structures) - len(aliases))]
        who = [s for s, alias in zip(structures, padded, strict=True)
               if subject in alias or subject in map(_singular, WORDS.findall(s.type.lower()))]
        named = _named(st.line[a:b], structures)
        if len(who) > 1:      # a word two names hold: the one the segment or the entry names
            who = [s for s in who if (named and s is named[0]) or any(s is o for o in owners)]
        if len(who) != 1 or (named is not None and not (named and named[0] is who[0])):
            return []
        return only(who)
    head = entry_head(st.line) if owners else None
    for a, b in st.spans:
        if head is not None and a < head <= b:        # the segment that holds the entry's name
            named = _named(st.line[head:b], structures)
            if named is not None and not (len(named) == 1 and any(named[0] is o for o in owners)):
                return []
            return only(owners)
        named = _named(st.line[a:b], structures)
        if named is not None:
            return only(named)
    if owners:
        return only(owners)
    if st.full is not None:
        return only([s for s in structures if fits(s)])
    low = st.line.lower()
    words = set(WORDS.findall(low))
    if any(name_score(low, words, s) is not None for s in structures):
        return []
    return only([s for s in structures if s.dims().get(st.key, 0.0) > 0])


def _distance_at(entry: str, figure: re.Match[str]) -> bool:
    """Whether a figure of an entry places something rather than sizing it
    (:data:`_DISTANCE_RE`: "30 m north of the pad", "15 m from the pit", "80 m away", "5 m
    beyond the rim"): never a span, a "to" later in its clause ("120 m from the depot to the
    pad"), nor an extent (:data:`_EXTENT_RE`: "runs 60 m from the array", "extending 5 m
    beyond the pad edge")."""
    d = _DISTANCE_RE.match(entry, figure.end())
    if d is None or _EXTENT_RE.search(entry, 0, figure.start()):
        return False
    if d["from_"]:
        rest = _SIZE_CLAUSE_SPLIT.split(entry[d.end():], maxsplit=1)[0]
        return _TO_RE.search(rest) is None
    return True


def distance_figures(structure: Structure, req: Requirements | None) -> dict[str, str]:
    """``{key: the phrase}`` for every length a structure records that its own entry in the
    brief (the line :func:`~moonwright.pipeline.tags.entry_owners` gives it, read whole)
    states only as a distance (:func:`_distance_at`: "30 m north of the pad", "15 m from the
    pit", "80 m away", "5 m beyond the rim"): a position, never a size. Seen live (run 3):
    Lightning recorded low_battery_pad's "Spoil stockpile: pad spoil placed in a compact pile
    30 m north of the pad" as a 30 m diameter (typed ``spoil_berm``, so its compaction was
    judged over 707 m2), and run 2 as a 30 m length. A figure the entry also states as a size,
    or does not state at all, stays, and so does every figure of a structure no entry is
    given to (a constraint, "Keep the blast berm 30 m from the landing pad edge", is no
    entry); {} without the brief."""
    if req is None or not req.source_brief:
        return {}
    known = [structure, *(s for s in req.structures if s.id != structure.id)]
    entry = next((line for line in brief_lines(req.source_brief)
                  if any(s is structure for s in entry_owners(line, known))), None)
    if entry is None:
        return {}
    out: dict[str, str] = {}
    for key, value in structure.dims().items():
        if not key.endswith("_m") or value <= 0:
            continue
        found = [m for m in FIGURE.finditer(entry)
                 if math.isclose(_metres(m[1], m[2]), value, abs_tol=1e-6)]
        if found and all(_distance_at(entry, m) for m in found):
            phrase = _SIZE_CLAUSE_SPLIT.split(entry[found[0].start():])[0]
            out[key] = " ".join(phrase.split())
    return out


def _as_read(structure: Structure, req: Requirements | None) -> Structure:
    """The structure with the footprint the brief states (:func:`with_judged_footprint`) and
    without the figures its entry gives only as distances (:func:`distance_figures`): what the
    minimums are read against, for it and for the structures beside it."""
    structure = with_judged_footprint(structure, req)
    far = distance_figures(structure, req)
    if not far:
        return structure
    return Structure.model_validate({k: v for k, v in structure.model_dump().items()
                                     if k not in far})


def stated_minimums(structure: Structure, req: Requirements | None
                    ) -> dict[str, SizeStatement]:
    """``{min_<dimension>_m: statement}``: the smallest size the brief allows for a dimension
    the structure records and has no minimum for (module docstring, "Sizes"), read anywhere in
    the brief (:func:`_size_statements`) and tied to the structure (:func:`_takers`), the
    smallest when several lines bound one dimension; {} without the brief. Never invented: a
    statement no dimension word binds, one about another structure or about none the brief
    names alone, one about a part, the ground, a treatment or the way between structures
    (:data:`_PART_RE`), one whose range top is no recorded size, or one no smaller than the
    recorded size gives nothing."""
    if req is None or not req.source_brief:
        return {}
    statements = _size_statements(req.source_brief)
    if not statements:
        return {}
    me = _as_read(structure, req)
    context = [me, *(_as_read(s, req) for s in req.structures if s.id != structure.id)]
    aliases = _aliases(req.source_brief, context)
    recorded = me.dims()
    out: dict[str, SizeStatement] = {}
    for st in statements:
        key = f"min_{st.key}"
        if key in recorded or not any(t is me for t in _takers(st, context, aliases)):
            continue
        if key not in out or st.least < out[key].least:
            out[key] = st
    return out


def size_notes(structure: Structure, req: Requirements) -> list[str]:
    """The corrections :func:`with_brief_sizes` makes, as the structure stage's warnings read
    them: "diameter_m 30 dropped: the brief's entry for S2 states 30 m only as a distance
    ("30 m north of the pad")", "min_diameter_m none -> 12: the brief allows "any diameter
    between 12 m and 20 m that fits""; [] when the record stands."""
    d = structure.dims()
    notes = [f"{key} {qty(d[key])} dropped: the brief's entry for {structure.id} states "
             f"{qty(d[key])} m only as a distance (\"{phrase}\")"
             for key, phrase in distance_figures(structure, req).items()]
    notes += [f"{key} none -> {qty(st.least)}: the brief allows \"{st.quote}\""
              for key, st in stated_minimums(structure, req).items()]
    return notes


def with_brief_sizes(structure: Structure, req: Requirements | None) -> Structure:
    """The structure with the sizes the brief states written in (module docstring, "Sizes"):
    every length its entry gives only as a distance dropped (:func:`distance_figures`) and
    every smaller size the brief allows for a recorded dimension written as
    ``min_<dimension>_m`` (:func:`stated_minimums`); the structure itself when the record
    stands."""
    far = distance_figures(structure, req)
    least = stated_minimums(structure, req)
    if not far and not least:
        return structure
    data = {k: v for k, v in structure.model_dump().items() if k not in far}
    for key, st in least.items():
        data[key] = int(st.least) if float(st.least).is_integer() else st.least
    return Structure.model_validate(data)


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
            kind="prism", floor_area_m2=lo["length_m"] * lo["width_m"], head=f"dimensions {how}",
            box=f"{qty(d['length_m'])} x {qty(d['height_m'])} x {qty(d['width_m'])}")
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


def dims_volumes(structure: Structure, req: Requirements | None = None, *,
                 brief_footprint: bool = True, brief_sizes: bool = True) -> list[Shape]:
    """Every :class:`Shape` a structure's dimensions imply, main body first: a bowl
    (``diameter_m`` x ``depth_m``) or a box (``length_m`` x ``width_m`` x ``depth_m``) is dug,
    so its phase is ``excavation``; a prism (``length_m`` x ``height_m`` [x ``width_m`` at the
    base], a berm or a stockpile) is placed, so its phase is ``haul``; a placed body on a key
    trench (``trench_width_m`` and ``trench_depth_m``, the structure prompt's keys for "on a
    key trench 0.7 m wide and 0.45 m deep") adds the trench as a second, dug shape. Walls are
    vertical unless :func:`wall_slope` finds them sloped (an extra, or with ``req`` the
    structure's own entry in the brief or a constraint); a berm's section is triangular
    (``height^2`` with 1:1 slopes when no base width is given: the steepest a dumped berm
    stands, so the least it holds). With ``req`` a recorded diameter whose entry states a
    rectangle is read as that rectangle (:func:`with_judged_footprint`, phase 3i: a "12 m by
    12 m" pit recorded as a 12 m bowl is a 12 x 12 m box); ``brief_footprint=False`` keeps the
    footprint as recorded (``scripts/rescore_eval.py`` compares the two).
    ``min_<dimension>_m`` extras give each shape its floor (the smallest volume the brief
    allows; :class:`Shape`), and with ``req`` the sizes the brief states are read too
    (:func:`with_brief_sizes`, phase 3j: a smaller size it allows anywhere, written as that
    extra, a figure its entry gives only as a distance dropped); ``brief_sizes=False`` keeps
    the sizes as recorded. A shape whose numbers give no positive volume (``depth_m: 0`` for
    an at-grade hardstand, a negative figure a user typed) is left out, so nothing downstream
    divides by it; so is a key trench without a stated width."""
    if brief_footprint:
        structure = with_judged_footprint(structure, req)
    if brief_sizes:
        structure = with_brief_sizes(structure, req)
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
    :data:`VOLUME_LOW`: ``short``). ``share_full`` (``excess_m3``: the digs, or the hauls bar
    those staged in a pile, over the largest volume the dimensions allow, without the credit:
    stripping a pad and then digging its whole bowl is the normal sequence) is the over-scope
    test: above :data:`VOLUME_HIGH` ``over`` (an advisory). ``doubled`` (a conflict, phase
    3i) is ``excess_m3`` at :data:`VOLUME_DOUBLE` of ``largest_m3`` plus
    :data:`VOLUME_DOUBLE_M3` or more: ``largest_m3`` is the largest volume the brief allows
    for the shape (:func:`_largest_m3`), None where it bounds none. ``staged`` are the hauls
    of a placed body that drop their material in a pile
    (:func:`~moonwright.pipeline.tags.haul_target`: a zone no structure claims, or a
    stockpile-like structure's, as the supply check reads them) for a later haul of the body
    to bring on: the shortfall counts them as the coverage check does, the over-scope tests do
    not, since the leg that brings the pile on to the body is the one that fills it (seen in
    review: pad spoil hauled to a stockpile and on to the berm, both legs tagged with the
    berm, read as a double count). A haul into a pile that no other haul of the body waits for
    is the body's haul (its tag says so; a haul lane's zone, say). ``excused``: the fill of a
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
    staged: tuple[str, ...] = ()
    largest_m3: float | None = None

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
    def excess_m3(self) -> float:
        """The m3 the over-scope tests count: :attr:`cubic_m3` without the hauls staged in a
        pile."""
        staged = sum(t.quantity or 0.0 for t, r in zip(self.tasks, self.roles, strict=True)
                     if r == "dig" and t.id in self.staged)
        return self.cubic_m3 - staged

    @property
    def share_full(self) -> float:
        return self.excess_m3 / self.shape.m3

    @property
    def share_largest(self) -> float:
        """``excess_m3`` over the largest volume the brief allows (``largest_m3``; the
        dimensions' largest where the brief bounds none): the double count's share."""
        return self.excess_m3 / (self.largest_m3 or self.shape.m3)

    @property
    def short(self) -> bool:
        return self.judged_share < VOLUME_LOW

    @property
    def over(self) -> bool:
        return self.shape.excess_judged and self.share_full > VOLUME_HIGH

    @property
    def doubled(self) -> bool:
        """Whether the digs (or the hauls, bar those staged in a pile) count the body twice: at
        least :data:`VOLUME_DOUBLE` of the largest volume the brief allows (``largest_m3``)
        plus :data:`VOLUME_DOUBLE_M3` (a 1 m3 anchor hole dug as two 1 m3 parts is rounding,
        not a second hole), where the brief bounds one; never for a stockpile-like structure,
        dug or filled: a pile is whatever gets hauled. That is wider than the shortfall's
        exemption (``excused``, the fill alone), on purpose: a dug stockpile-like shape is a
        pile whose height Lightning recorded as a depth, loaded out by "excavate stockpile"
        tasks that dig nothing new, or a pad Lightning typed "stockpile" (run 2), and the
        typing alone is too weak to reject a plan on; its excess stays the advisory."""
        return (self.largest_m3 is not None and not stockpile_like(self.structure)
                and self.excess_m3 >= VOLUME_DOUBLE * self.largest_m3 + VOLUME_DOUBLE_M3)

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
        """'T7 300 m3, T8 300 m3': the tasks the over-scope tests count (no haul staged in a
        pile)."""
        return ", ".join(f"{t.id} {qty(t.quantity or 0.0)} m3"
                         for t, r in zip(self.tasks, self.roles, strict=True)
                         if r == "dig" and t.id not in self.staged)

    @property
    def parts(self) -> int:
        """How many tasks :attr:`listing_m3` names: the parts a double count's cut splits
        over."""
        return sum(r == "dig" and t.id not in self.staged
                   for t, r in zip(self.tasks, self.roles, strict=True))

    @property
    def of_largest(self) -> str:
        """What a double count's share is of, after its percentage: '' for the dimensions as
        given, ' of the largest size the brief allows', ' of the 200 m3 the brief's entry
        states' (:func:`_largest_m3`)."""
        if self.largest_m3 is not None and self.largest_m3 > self.shape.m3:
            return f" of the {qty(self.largest_m3)} m3 the brief's entry states"
        return " of the largest size the brief allows" if self.shape.ranged else ""

    def _strip_entry(self, strips: list[Task]) -> str:
        named = " + ".join(f"{t.id} {qty(t.quantity or 0.0)} m2" for t in strips)
        verb = "counts" if len(strips) == 1 else "count"
        text = f"{named} {verb} {qty(self.stripped_m3)} m3 at {qty(self.shape.strip_depth_m)} m"
        if self.area_m2 > self.shape.floor_footprint_m2:
            text += f" over the {qty(self.shape.floor_footprint_m2)} m2 footprint"
            if self.shape.floor_footprint_m2 < self.shape.footprint_m2:
                text += " of the smallest size"
        return text


def ancestors(graph: TaskGraph) -> dict[str, set[str]]:
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


def is_dig(task: Task) -> bool:
    """Whether a task digs out material a haul can carry: an excavator task with a positive
    quantity in m3 that is no :func:`rehandles` (spoil bladed, pushed or backfilled was dug
    by another task). The supply check's dig, a source for every haul."""
    return task.unit == "m3" and (task.quantity or 0.0) > 0 and _digs(task)


def is_strip(task: Task) -> bool:
    """Whether a task strips or grades a surface layer off: an excavator task with a positive
    quantity in m2 that is no :func:`rehandles` (the rate table's "strip or grade a surface
    layer up to 0.2 m"). The supply check counts its spoil only for the hauls it can feed:
    those into its own structure or into a pile, or any haul of a graph that digs nothing
    in m3 (``moonwright.pipeline.validate``, "Supply order")."""
    return task.unit == "m2" and (task.quantity or 0.0) > 0 and _digs(task)


def supply_m3(task: Task) -> float:
    """The m3 a source makes available to the hauls after it: a dig's quantity, a strip's
    area x :data:`STRIP_DEPTH_M` (the most the rate table's m2 row removes, so a 144 m2 strip
    yields at most 28.8 m3); 0 for any other task."""
    if is_dig(task):
        return task.quantity or 0.0
    if is_strip(task):
        return (task.quantity or 0.0) * STRIP_DEPTH_M
    return 0.0


def is_haul(task: Task) -> bool:
    """Whether a task hauls material: a hauler task with a positive quantity in m3."""
    return task.robot_type == "hauler" and task.unit == "m3" and (task.quantity or 0.0) > 0


def volume_coverages(req: Requirements, graph: TaskGraph, *, brief_footprint: bool = True,
                     brief_sizes: bool = True) -> list[VolumeCoverage]:
    """One :class:`VolumeCoverage` per structure and shape (:func:`dims_volumes`, read with
    the brief and the constraints of ``req``: a recorded diameter whose entry states a
    rectangle is that rectangle unless ``brief_footprint`` is False, and the sizes the brief
    states are read unless ``brief_sizes`` is False) that has at least one
    task of the shape's robot type with a quantity, in requirements order. The tasks of a
    structure are resolved exactly as the coverage check resolves them
    (:func:`moonwright.pipeline.tags.serving_tasks`: the id in any case, the type, a label
    containing the id, untagged tasks in its zone, and hauls placed in its zone), so a graph
    the validator accepts cannot escape the volume check by its tag spelling; a dig a haul
    waits for (its ``after`` edges, transitively) names the haul's sources the same way. A
    structure without a volume, or without any task of the shape's type (coverage's
    business), gets nothing; a graph without quantities (the hand-made fixtures) therefore
    gets nothing."""
    shapes = {s.id: dims_volumes(s, req, brief_footprint=brief_footprint,
                                 brief_sizes=brief_sizes) for s in req.structures}
    if not any(shapes.values()):
        return []
    served = {s.id: serving_tasks(s, graph, req.structures) for s in req.structures}
    serves: dict[str, set[str]] = {}
    for sid, tasks in served.items():
        for t in tasks:
            serves.setdefault(t.id, set()).add(sid)
    before = ancestors(graph)
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
            staged: tuple[str, ...] = ()
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
                piled = {t.id for t in hauls if _in_pile(t, req.structures)}
                staged = tuple(t.id for t in hauls if t.id in piled and any(
                    t.id in before.get(h.id, set()) for h in hauls if h.id not in piled))
            cubic = sum(t.quantity or 0.0 for t, r in zip(typed, roles, strict=True) if r == "dig")
            out.append(VolumeCoverage(
                structure=s, shape=shape, robot=robot, tasks=typed, roles=roles,
                cubic_m3=cubic, stripped_m3=stripped, area_m2=area, sources=sources,
                source_m3=source_m3, staged=staged,
                largest_m3=_largest_m3(s, shape, req, body=shape is shapes[s.id][0])))
    return out


def _in_pile(haul: Task, structures: Sequence[Structure]) -> bool:
    """Whether a haul drops its material in a pile, read zone first as the supply check reads
    its target (:func:`~moonwright.pipeline.tags.haul_target`): a zone no structure claims, or
    a stockpile-like structure's."""
    target = haul_target(haul, structures)
    return target is None or stockpile_like(target)


def _stated_m3(structure: Structure, req: Requirements) -> float | None:
    """The largest volume the brief states on the lines that state every length figure the
    structure records (:func:`~moonwright.pipeline.tags.states_figures`: its entry, whatever
    its type; "about 280 cubic metres of fill", "60 m3"); None when they state none or no
    brief is known."""
    lines = [line for line in brief_lines(req.source_brief)
             if states_figures(line, structure)] if req.source_brief else []
    figures = [float(m[1].replace(",", "")) for line in lines
               for m in _VOLUME_FIGURE_RE.finditer(line)]
    return max(figures) if figures else None


def _largest_m3(structure: Structure, shape: Shape, req: Requirements, *,
                body: bool) -> float | None:
    """The largest volume the brief allows for one shape of a structure, a double count's
    measure (phase 3i): the dimensions' largest (``shape.m3``), or for the main body a larger
    volume the brief states on the line that states the structure's size (:func:`_stated_m3`:
    isru_stockpile's "about 200 cubic metres of loose regolith taken from three 20 m by 10 m
    strips", which Lightning recorded as one 70 m3 "borrow_strip" in runs 2 and 3: a plan that
    digs all three is no double count). None where the brief bounds none: walls sloped by an
    unstated ratio (``excess_judged`` False) and a placed body whose base width the brief does
    not state (:func:`base_width_stated`: its section is a guess, the least it holds, never
    the largest; propellant_pit_and_berm's berm "30 m arc ... 2.5 m high", recorded 4 m wide
    from the structure prompt's example)."""
    if not shape.excess_judged:
        return None
    if shape.phase == "haul" and not base_width_stated(structure, req):
        return None
    stated = _stated_m3(structure, req) if body and shape.kind != "trench" else None
    return max(shape.m3, stated or 0.0)


def volume_shortfalls(req: Requirements, graph: TaskGraph) -> list[VolumeCoverage]:
    """The coverages below :data:`VOLUME_LOW` of their floor, the fill of stockpile-like
    structures excused: what ``validate_graph`` reports as ``volume:<structure>``, with the
    double counts (:func:`volume_double_counts`)."""
    return [c for c in volume_coverages(req, graph) if c.short and not c.excused]


def volume_double_counts(req: Requirements, graph: TaskGraph) -> list[VolumeCoverage]:
    """The coverages whose digs or hauls count the body twice (:attr:`VolumeCoverage.doubled`:
    :data:`VOLUME_DOUBLE` of the largest volume the brief allows plus :data:`VOLUME_DOUBLE_M3`
    or more, never a stockpile's): what ``validate_graph`` reports as ``volume:<structure>``
    besides the shortfalls. Seen live (phase 3i): the landing-pad demo's first graph hauled
    240 m3, the 40 x 1.5 x 4 m box, into the 120 m3 berm in 3 of 3 tries, and run 2's
    habitat_berm_tight_horizon ultra #1 verified with 2 x 280 m3 for its 280 m3 berm ("560 m3 /
    2 = 280 m3": the box of the 70 x 2 x 4 m berm, split in two)."""
    return [c for c in volume_coverages(req, graph) if c.doubled]


def volume_conflicts(req: Requirements, graph: TaskGraph) -> list[VolumeCoverage]:
    """Every coverage the validator reports as ``volume:<structure>``, in requirements and
    shape order: a shortfall (:func:`volume_shortfalls`) or a double count
    (:func:`volume_double_counts`); one coverage is never both."""
    return [c for c in volume_coverages(req, graph) if (c.short and not c.excused) or c.doubled]


def _times(share: float) -> str:
    """'twice', 'three times', '1.6 times'."""
    rounded = round(share, 1)
    if rounded == 2.0:
        return "twice"
    if rounded == 3.0:
        return "three times"
    return f"{rounded:.1f} times"


SHORTFALL_MARK = " total only "   # in every shortfall advisory, never in an excess one
DOUBLE_MARK = ", a double count"  # closes an excess the validator rejects (VOLUME_DOUBLE),
#                                   and marks the verdict's detail of one


def is_shortfall(advisory: str) -> bool:
    """Whether a ``volume:<structure>`` advisory is a shortfall (what the validator's verdict
    also carries) rather than an excess (which no verdict carries below
    :data:`VOLUME_DOUBLE`: a berm hauled 1.6 times over while its key trench is short shares
    the id ``volume:S2`` with the trench's conflict)."""
    return advisory.startswith("volume:") and SHORTFALL_MARK in advisory


def is_double_count(advisory: str) -> bool:
    """Whether a ``volume:<structure>`` advisory is an excess the validator's verdict also
    carries (:attr:`VolumeCoverage.doubled`, its text closing with :data:`DOUBLE_MARK`)
    rather than one between :data:`VOLUME_HIGH` and :data:`VOLUME_DOUBLE`, a warning alone."""
    return advisory.startswith("volume:") and advisory.endswith(DOUBLE_MARK)


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
    windrow is no second dig), reads ``total 250 m3 (159 %): dug 1.6 times over`` and stays
    a warning for the trace and the repair prompt (the parts of a split must sum to the
    total, never repeat it), up to :data:`VOLUME_DOUBLE` of the largest volume the brief
    allows: from there it is a double count and the validator's conflict too
    (:attr:`VolumeCoverage.doubled`), and its text closes with :data:`DOUBLE_MARK` (``total
    600 m3 (200 %): hauled twice over, a double count``) so a repair is not told it twice
    (:func:`is_double_count`). Hauls staged in a pile count for neither (the leg that brings
    the pile on to the body is the one that fills it). The fill of a stockpile-like
    structure (:func:`stockpile_like`) is judged neither way: a pile is whatever gets
    hauled. Seen live (phase 3c): a 360 m3 borrow strip planned as 600 m2 of stripping to fit
    one battery, a 300 m3 berm hauled twice over, a 20 m pad dug as a 12 m one, a 57.6 m3 key
    trench "repaired" with two 5 m3 tasks. Meant for a whole plan: on the remaining graph of
    a replan the finished work is gone, so the task-graph stage does not call it there."""
    out: list[str] = []
    for c in volume_coverages(req, graph):
        if c.excused:
            continue
        if c.short:
            out.append(_shortfall_text(c))
        elif c.over:
            shape = c.shape
            verb = "dug" if shape.phase == "excavation" else "hauled"
            which = (" placed in it" if c.staged
                     else "" if all(r == "dig" for r in c.roles)
                     else " that dig" if "rehandle" in c.roles else " in m3")
            out.append(f"volume:{c.structure.id}: its dimensions give {shape.m3_text} m3 "
                       f"({shape.how}) but its {shape.phase} tasks{which} ({c.listing_m3}) "
                       f"total {qty(c.excess_m3)} m3 ({pct(c.share_full)}): {verb} "
                       f"{_times(c.share_full)} over" + (DOUBLE_MARK if c.doubled else ""))
    return out


def volume_shares(req: Requirements, graph: TaskGraph, *, brief_footprint: bool = True,
                  brief_sizes: bool = True) -> dict[str, float]:
    """``{structure id: judged share}`` for every structure and shape the advisory judges
    (:attr:`VolumeCoverage.judged_share`, the smallest when a structure has several shapes;
    the fill of a stockpile-like structure is not judged), whether or not it is out of range:
    what the eval reports as ``min_volume_share`` (``brief_footprint`` and ``brief_sizes`` as
    in :func:`volume_coverages`)."""
    out: dict[str, float] = {}
    for c in volume_coverages(req, graph, brief_footprint=brief_footprint,
                              brief_sizes=brief_sizes):
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


def _count_of(word: str) -> int:
    low = word.lower()
    return _COUNT_WORDS[low] if low in _COUNT_WORDS else int(low)


def _clause_lifts(clause: str) -> tuple[int | None, bool]:
    """``(count, stated as a number)`` for one clause (:data:`_LIFT_COUNT_RE`, then "lifts:
    3"): the first number before a lift noun, else 1 for an article ("a 20 cm compacted
    lift"), else ``(None, False)``."""
    article = False
    for m in _LIFT_COUNT_RE.finditer(clause):
        if m["noun"].lower().startswith("layer") and (
                "-" in m["sep"] or not _COMPACTS_RE.search(clause)
                or _SURFACES_RE.search(clause)):
            continue   # a two-layer compound, or layers nothing compacts (a sintered surface)
        if m["a"]:
            article = True
            continue
        n = _count_of(m["n"])
        return (min(n, _count_of(m["m"])) if m["m"] else n), True
    key = _LIFTS_KEY_RE.search(clause)
    if key:
        return _count_of(key["n"]), True
    return (1, False) if article else (None, False)


def stated_lifts(line: str | None) -> int | None:
    """The count of compaction lifts a brief line states (:data:`_LIFT_COUNT_RE`: digits or
    one to ten before "lift(s)", or before "layer(s)" in a clause that compacts; the smaller
    of a range), the first one when it states several; 1 when it names only "a" lift or
    layer; None when it states none or a count outside 1 to 10."""
    article = False
    for clause in _LIFT_CLAUSE_SPLIT.split(line or ""):
        n, number = _clause_lifts(clause)
        if number:
            return n if n is not None and 1 <= n <= _MAX_LIFTS else None
        article = article or n is not None
    return 1 if article else None


def recorded_lifts(structure: Structure) -> tuple[str, float] | None:
    """``(key, value)`` of the first numeric ``lifts`` / ``lift_count`` / ``compaction_lifts``
    extra the structure stage recorded, whatever its value (``("lifts", 0.2)`` for "a 20 cm
    compacted lift"); None when none is numeric."""
    extras = structure.model_extra or {}
    for key in _LIFT_KEYS:
        v = extras.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return key, float(v)
    return None


def _recorded_count(structure: Structure) -> int | None:
    """The first ``lifts`` / ``lift_count`` / ``compaction_lifts`` extra of 1 to 10, rounded;
    None without one."""
    extras = structure.model_extra or {}
    for key in _LIFT_KEYS:
        v = extras.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= _MAX_LIFTS:
            return round(v)
    return None


def lifts(structure: Structure, req: Requirements | None = None) -> int:
    """How many lifts the brief compacts the structure in. With the brief known
    (``req.source_brief``) and the structure's own entry found in it
    (:func:`~moonwright.pipeline.tags.entry_line`, read whole even when hard-wrapped), the
    count that entry states (:func:`stated_lifts`); when it names lifts or layers without a
    count we can read ("placed in 50 cm lifts, three in all"), a recorded count of 1 to 10
    stands; otherwise 1: a recorded ``lifts`` the entry gives no ground for is not accepted
    (seen live: crater_ramp's "in four 10 m segments" recorded as 4 lifts read a correct plan
    as 25 % of its compaction, and access_road's "a 20 cm compacted lift" as 0.2). Without the
    brief or an entry (older requirements, hand-made ones, a type no line of the brief names)
    a ``lifts`` (``lift_count``, ``compaction_lifts``) extra of 1 to 10, else 1. The structure
    stage writes this count into the requirements (:func:`with_judged_lifts`), so the planner
    plans the count the eval judges."""
    recorded = _recorded_count(structure)
    line = entry_line(req.source_brief, structure) if req is not None and req.source_brief else None
    if line is None:
        return recorded or 1
    stated = stated_lifts(line)
    if stated is not None:
        return stated
    if recorded is not None and _LIFT_MENTION_RE.search(line):
        return recorded
    return 1


def lift_note(structure: Structure, req: Requirements) -> str | None:
    """The correction of a structure's recorded lift count to the one compaction is planned
    and judged with (:func:`lifts`), as the structure stage's warning reads it: "lifts 4 -> 1:
    the brief's entry for S1 states no lift count we can read", "lifts 3 -> 2: the brief's
    entry for S1 states 2 compaction lifts", "lifts none -> 2: the brief's entry for BERM
    states 2 compaction lifts"; None when the record stands (it agrees, or nothing is recorded
    and the brief states no count above one) or the brief or the structure's entry in it is
    unknown."""
    line = entry_line(req.source_brief, structure) if req.source_brief else None
    if line is None:
        return None
    n = lifts(structure, req)
    stated = stated_lifts(line)
    recorded = recorded_lifts(structure)
    entry = f"the brief's entry for {structure.id}"
    why = (f"{entry} states {plural(stated, 'compaction lift')}" if stated is not None
           else f"{entry} states no lift count we can read")
    if recorded is None:
        return f"lifts none -> {n}: {why}" if n > 1 else None
    key, value = recorded
    return None if value == n else f"{key} {qty(value)} -> {n}: {why}"


def with_judged_lifts(structure: Structure, req: Requirements) -> Structure:
    """The structure with the lift count of :func:`lift_note` written in: ``lifts`` set to the
    count the brief's entry states (an explicit 1 too, "in one lift"), or every recorded lift
    key dropped when the entry states none (one lift, the default); the structure itself when
    the record stands."""
    if lift_note(structure, req) is None:
        return structure
    data = {k: v for k, v in structure.model_dump().items() if k not in _LIFT_KEYS}
    n = lifts(structure, req)
    if n > 1 or stated_lifts(entry_line(req.source_brief, structure)) is not None:
        data["lifts"] = n
    return Structure.model_validate(data)


def _states_size(line: str, structure: Structure) -> bool:
    """Whether a brief line states every length and height figure the structure records: its
    entry under another name (Lightning typed depleted_excavators' dust berm "borrow_strip",
    whose words pick the borrow pit's entry)."""
    return states_figures(line, structure, ("length_m", "height_m"))


def _width_in(line: str, width: float) -> bool:
    """Whether ``line`` states ``width`` as a width or a base, outside a key trench's or a
    crest's clause."""
    for clause in _WIDTH_CLAUSE_SPLIT.split(line):
        if not _WIDTH_WORD_RE.search(clause) or _NOT_BASE_RE.search(clause):
            continue
        if any(math.isclose(f, width, abs_tol=1e-6) for f in figures_m(clause)):
            return True
    return False


def base_width_stated(structure: Structure, req: Requirements | None = None) -> bool:
    """Whether a placed body's base width is a number the brief gives: a positive
    ``width_m`` is recorded and, with the brief known, the structure's own entry states that
    figure as a width or a base (:data:`_WIDTH_WORD_RE`; "2.5 m high and 4 m wide at the
    base", "4 m at the base"), outside a key trench's or a crest's clause; when the entry
    does not, a line that states the structure's recorded length and height does (its entry
    under a type whose words pick another line). A berm without one holds the 1:1 section the
    volume check assumes, a guess, and a width the structure stage copied from elsewhere
    (propellant_pit_and_berm's berm "30 m arc ... 2.5 m high" recorded 4 m wide, the
    structure prompt's example) is one too: compaction is then judged on the least base the
    body can have (:func:`_work_area`). Without the brief a recorded ``width_m`` counts as
    stated."""
    width = structure.dims().get("width_m", 0.0)
    if width <= 0:
        return False
    if req is None or not req.source_brief:
        return True
    entry = entry_line(req.source_brief, structure)
    if entry is not None and _width_in(entry, width):
        return True
    return any(_width_in(line, width) for line in brief_lines(req.source_brief)
               if line != entry and _states_size(line, structure))


def _work_area(structure: Structure, req: Requirements | None, *,
               brief_footprint: bool = True,
               brief_sizes: bool = True) -> tuple[float, bool, str] | None:
    """``(m2, placed, basis)``: the area compaction and sintering treat on the smallest size
    the brief allows (an ``area_m2`` extra; the floor of a dug bowl or box, the bottom when
    its walls slope; a berm's base when the brief states its width, :func:`base_width_stated`,
    else the least base it can have, as wide as it is high (side slopes of 63 degrees or
    less): length x height, ``basis`` saying so; a flat ``length_m`` x ``width_m`` or
    ``diameter_m`` surface), whether it is a placed body, and how the area was read ('' for
    a stated size); None when the dimensions give none. A recorded diameter is read as the
    rectangle the brief states and the sizes the brief states are read, as
    :func:`dims_volumes` reads them (``brief_footprint``, ``brief_sizes``)."""
    if brief_footprint:
        structure = with_judged_footprint(structure, req)
    if brief_sizes:
        structure = with_brief_sizes(structure, req)
    d = structure.dims()
    if d.get("area_m2", 0.0) > 0:
        return d["area_m2"], False, ""
    shapes = dims_volumes(structure, req, brief_footprint=brief_footprint,
                          brief_sizes=brief_sizes)
    body = shapes[0] if shapes and shapes[0].kind != "trench" else None
    if body is not None:
        if body.phase == "haul" and not base_width_stated(structure, req):
            # no base the brief states (the 1:1 section is a guess, a copied width too): the
            # least base the body can have is as wide as it is high (access_road's berm "20 m
            # long, 1.5 m high": 30 m2; a 10 m2 pass over a 40 m x 2 m berm is no compaction)
            area = _least(d, "length_m") * _least(d, "height_m")
            return (area, True, "a base as wide as it is high") if area > 0 else None
        return ((body.floor_area_m2, body.phase == "haul", "") if body.floor_area_m2 > 0
                else None)
    if "depth_m" in d or "height_m" in d:
        return None   # a body whose numbers form no positive volume
    lo = {k: _least(d, k) for k in _MIN_KEYS if k in d}
    if {"length_m", "width_m"} <= lo.keys() and lo["length_m"] > 0 and lo["width_m"] > 0:
        return lo["length_m"] * lo["width_m"], False, ""
    if lo.get("diameter_m", 0.0) > 0:
        return math.pi * (lo["diameter_m"] / 2) ** 2, False, ""
    return None


def area_coverages(req: Requirements, graph: TaskGraph, *, brief_footprint: bool = True,
                   brief_sizes: bool = True) -> list[AreaCoverage]:
    """One :class:`AreaCoverage` per structure and area-work robot type (compactor, printer)
    with at least one task of that type with a quantity: the m2 its tasks plan against the
    structure's work area (:func:`_work_area`, a recorded diameter read as the rectangle the
    brief states unless ``brief_footprint`` is False, the sizes the brief states read unless
    ``brief_sizes`` is False), times its :func:`lifts` for compaction
    (the count the brief states, read with ``req``). A placed body whose base width the brief
    does not state is judged on the least base it can have, as wide as it is high
    (:func:`_work_area`, :func:`base_width_stated`: access_road's berm "20 m long, 1.5 m high"
    read 33-50 % of a 1:1 base the brief never gave, and is 30 m2 now; a 10 m2 pass over a
    40 m x 2 m berm is still a gap). A placed body's sintering (a berm's crest, whose width no
    dimension gives) and the fill of a stockpile-like structure are not judged. The eval's scope test only (:func:`scope_gaps`), never a validation conflict:
    seen in review, a kind-D brief whose two compaction lifts the repair trimmed by 22 %
    verified "in scope"."""
    out: list[AreaCoverage] = []
    for s in req.structures:
        if stockpile_like(s):
            continue
        work = _work_area(s, req, brief_footprint=brief_footprint, brief_sizes=brief_sizes)
        if work is None:
            continue
        area, placed, basis = work
        served = serving_tasks(s, graph, req.structures)
        for robot in ("compactor", "printer"):
            if robot == "printer" and placed:
                continue
            typed = tuple(t for t in served if t.robot_type == robot and t.quantity is not None)
            if not typed:
                continue
            n = lifts(s, req) if robot == "compactor" else 1
            how = (f"{qty(area)} m2" + (f" at {basis}" if basis else "")
                   + (f" x {n} lifts" if n > 1 else ""))
            planned = sum(t.quantity or 0.0 for t in typed if t.unit == "m2")
            out.append(AreaCoverage(structure=s, robot=robot, tasks=typed, area_m2=planned,
                                    need_m2=area * n, how=how))
    return out


def area_shares(req: Requirements, graph: TaskGraph, *, brief_footprint: bool = True,
                brief_sizes: bool = True) -> dict[str, float]:
    """``{structure id: planned m2 / work area}``, the smallest over its compaction and
    sintering: what the eval reports as ``min_area_share`` (``brief_footprint`` and
    ``brief_sizes`` as in :func:`area_coverages`)."""
    out: dict[str, float] = {}
    for a in area_coverages(req, graph, brief_footprint=brief_footprint,
                            brief_sizes=brief_sizes):
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
