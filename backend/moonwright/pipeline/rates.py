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
from collections.abc import Collection
from dataclasses import dataclass

from moonwright.pipeline.tags import serving_tasks
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

ADVISORY_THRESHOLD = 0.5   # relative deviation of the model's estimate that earns an advisory
VOLUME_LOW = 0.7           # volume:<structure> when the planned m3 are below this share of the
VOLUME_HIGH = 1.5          # dimensions' volume, or above this multiple of it
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
    Rate("excavator", "m2", 25.0, 4.8, "strip / rough grade / level",
         "assumption: 314 m2 pad strip = 13 h / ~1500 Wh (blade pass at ~0.1 m depth, "
         "Chariot/LANCE-style blade work scaled down)"),
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
    """1300.0 -> '1300', 1316.4 -> '1316.4'."""
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


# ----------------------------------------------------------------------- volume advisories
Shape = tuple[float, str, str]   # (m3, how, phase)


def _body(d: dict[str, float]) -> Shape | None:
    """The main body a structure's dimensions describe (None when they describe none)."""
    if "diameter_m" in d and "depth_m" in d:
        radius = d["diameter_m"] / 2
        return (math.pi * radius * radius * d["depth_m"],
                f"bowl {_fmt(d['diameter_m'])} m across, {_fmt(d['depth_m'])} m deep", "excavation")
    if {"length_m", "width_m", "depth_m"} <= d.keys():
        return (d["length_m"] * d["width_m"] * d["depth_m"],
                f"{_fmt(d['length_m'])} x {_fmt(d['width_m'])} x {_fmt(d['depth_m'])} m", "excavation")
    if {"length_m", "height_m", "width_m"} <= d.keys():
        return (0.5 * d["height_m"] * d["width_m"] * d["length_m"],
                (f"{_fmt(d['length_m'])} m long, {_fmt(d['height_m'])} m high, {_fmt(d['width_m'])} m "
                "wide at the base, triangular section"), "haul")
    if {"length_m", "height_m"} <= d.keys():
        return (d["height_m"] ** 2 * d["length_m"],
                f"{_fmt(d['length_m'])} m long, {_fmt(d['height_m'])} m high, 1:1 slopes", "haul")
    return None


def _key_trench(d: dict[str, float]) -> Shape | None:
    """The key trench under a placed body: ``length_m`` x (``trench_width_m``, else the base
    ``width_m``, else twice ``height_m``) x ``trench_depth_m``."""
    if "trench_depth_m" not in d or "length_m" not in d:
        return None
    width = d.get("trench_width_m", d.get("width_m", 2 * d.get("height_m", 0.0)))
    return (d["length_m"] * width * d["trench_depth_m"],
            f"key trench {_fmt(d['length_m'])} x {_fmt(width)} x {_fmt(d['trench_depth_m'])} m",
            "excavation")


def dims_volumes(structure: Structure) -> list[Shape]:
    """Every ``(m3, how, phase)`` a structure's dimensions imply, main body first: a bowl
    (``diameter_m`` x ``depth_m``) or a box (``length_m`` x ``width_m`` x ``depth_m``) is dug,
    so its phase is ``excavation``; a prism (``length_m`` x ``height_m`` [x ``width_m`` at the
    base], a berm or a stockpile) is placed, so its phase is ``haul``; a placed body on a key
    trench (``trench_depth_m``, the structure prompt's key for "on a key trench 1.2 m wide and
    0.8 m deep") adds the trench as a second, dug shape. The same simplifications as
    ``scripts/brief_budget.py``: vertical walls, a triangular berm section (``height^2`` with
    1:1 slopes when no base width is given). A shape whose numbers give no positive volume
    (``depth_m: 0`` for an at-grade hardstand, a negative figure a user typed) is left out, so
    nothing downstream divides by it."""
    d = structure.dims()
    body = _body(d)
    shapes = [body] if body is not None else []
    if body is None or body[2] == "haul":
        trench = _key_trench(d)
        if trench is not None:
            shapes.append(trench)
    return [s for s in shapes if s[0] > 0]


def dims_volume(structure: Structure) -> Shape | None:
    """The main ``(m3, how, phase)`` of :func:`dims_volumes`, or None when the dimensions form
    no positive volume."""
    shapes = dims_volumes(structure)
    return shapes[0] if shapes else None


def volume_advisories(req: Requirements, graph: TaskGraph) -> list[str]:
    """``volume:<structure>: ...`` for every structure whose tasks plan clearly less (below
    :data:`VOLUME_LOW`) or clearly more (above :data:`VOLUME_HIGH`) than the m3 its dimensions
    imply (:func:`dims_volumes`): the excavator tasks of a dug shape, the hauler tasks of a
    placed one, summed over their ``m3`` quantities. The tasks of a structure are resolved
    exactly as the coverage check resolves them (:func:`moonwright.pipeline.tags.serving_tasks`:
    the id in any case, the type, a label containing the id, untagged tasks in its zone, and
    hauls placed in its zone), so a graph the validator accepts cannot escape this check by
    its tag spelling. A warning, never a conflict: a brief may allow a smaller scope and the
    user can correct the dimensions, but the re-scope must be visible. Seen live (phase 3c): a
    360 m3 borrow strip planned as 600 m2 of stripping to fit one battery, a 300 m3 berm hauled
    twice over, a 20 m pad dug as a 12 m one, a 57.6 m3 key trench "repaired" with two 5 m3
    tasks. A structure without a volume, or without any task of the shape's robot type
    (coverage's business), gets nothing; a graph without quantities (the hand-made fixtures)
    therefore gets nothing. Meant for a whole plan: on the remaining graph of a replan the
    finished work is gone, so the task-graph stage does not call it there."""
    out: list[str] = []
    for s in req.structures:
        shapes = dims_volumes(s)
        if not shapes:
            continue
        served = serving_tasks(s, graph, req.structures)
        for volume, how, phase in shapes:
            robot = "excavator" if phase == "excavation" else "hauler"
            typed = [t for t in served if t.robot_type == robot and t.quantity is not None]
            if not typed:
                continue
            total = sum(t.quantity for t in typed if t.unit == "m3")
            share = total / volume
            if VOLUME_LOW <= share <= VOLUME_HIGH:
                continue
            listed = ", ".join(f"{t.id} {_fmt(t.quantity)} {t.unit or '?'}" for t in typed)
            word = "only " if share < VOLUME_LOW else ""
            out.append(f"volume:{s.id}: its dimensions give {_fmt(volume)} m3 ({how}) but its "
                       f"{phase} tasks ({listed}) total {word}{_fmt(total)} m3 ({share:.0%})")
    return out


def volume_shares(req: Requirements, graph: TaskGraph) -> dict[str, float]:
    """``{structure id: planned m3 / dimension m3}`` for every structure and shape the
    advisory judges (the smallest share when a structure has several shapes), whether or not
    it is out of range: what the eval reports as ``min_volume_share``."""
    out: dict[str, float] = {}
    for s in req.structures:
        served = serving_tasks(s, graph, req.structures)
        for volume, _how, phase in dims_volumes(s):
            robot = "excavator" if phase == "excavation" else "hauler"
            typed = [t for t in served if t.robot_type == robot and t.quantity is not None]
            if not typed:
                continue
            share = sum(t.quantity for t in typed if t.unit == "m3") / volume
            out[s.id] = min(share, out.get(s.id, math.inf))
    return out
