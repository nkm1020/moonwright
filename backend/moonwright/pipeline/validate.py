"""F3: deterministic validator for a TaskGraph against Requirements.

Contract (see conflict id formats in schemas.py):

    validate_graph(req, graph, previous=None, *, coverage=False, volume=None, supply=None,
                   robot_positions=None) -> Failure | None

Checks, in this order, collecting *all* conflicts of the kinds below before returning:
  duplicate ids (tasks, robots, zones) .................... duplicate:<id>
  `after` references to unknown tasks ..................... reference:<task>.after=<id>
  self/cyclic precedence (report the cycle path) .......... cycle:<a>-><b>->...-><a>
  robot_type with no robot in requirements ................ capability:<task_id>
  physical task without a priced quantity (see below) ..... quantity:<task_id>
  structure coverage (coverage=True only, see below) ...... scope:<structure_id>
  planned volume short of the dimensions, or a double
    count (whole plans only, see below) ................... volume:<structure_id>
  a haul that waits for no dig, or for too little while
    another dig could supply it (whole plans only, below) . supply:<task_id>
  task energy + transit > max battery of its type ......... energy_task:<task_id>
  critical path (longest duration chain) > horizon_h ...... horizon_path:<a>-><b>->...
  per robot type: ceil(total duration / #robots) > horizon  horizon   (hint names the type)
  per robot type: energy + transit > sum of batteries ..... energy:<type>  (hint explains)
Return None when everything passes. `hint` is one human sentence per conflict group, joined.

Energy with transit (``energy_task:<task_id>``, ``energy:<type>``)
------------------------------------------------------------------
CP-SAT charges every task a transit allowance on top of its energy (``moonwright.pipeline
.schedule``, "Travel": for task *i* on robot *r*, the longest leg *r* could have to drive to
the task's zone, from where it starts or from any zone a task of its type uses, at
:data:`moonwright.geometry.TRAVEL_WH_PER_CELL`), so a plan the validator passed at 3,999 of
4,000 Wh failed in the solver at 4,037 Wh and wasted a repair round. Both energy checks now
charge the same allowance (:func:`transit_allowances`): per task the least allowance any
robot of its type would be charged, which is the solver's own figure for the pool
(``budget_rows`` "type" rows) and never more than it demands of any assignment, so a graph
the validator rejects on energy is one CP-SAT would reject too, and the figures match to the
milli-Wh on a fresh plan. ``robot_positions`` (robot id -> grid cell, the
``SolveOptions.robot_positions`` a replan passes the solver) places the robots; without it
everyone starts at the depot, as the solver assumes without it. The detail and the hint name
the travel share ("printer tasks need 4,037 Wh, about 38 Wh of it travel; its 1 robot carries
4,000 Wh") and print every figure through ``moonwright.text`` (whole Wh with thousands
separators, decimals only where two figures would otherwise print equal). One verdict per
cause: a type's overrun that its ``energy_task`` tasks alone explain (the type would fit if
each of them were cut to the largest battery) is reported as those tasks only, and their hint
says that the type's batteries cannot carry them either, so splitting alone cannot fit them
and the full work stays unless the brief allows less (the repair prompt's rule for this case);
an overrun with a cause of its own (the rest of the type's work exceeds the pool too) keeps
``energy:<type>`` beside them.

Volume (``volume:<structure_id>``, whole plans only)
---------------------------------------------------
Seen live (phase 3e): the models satisfy the solver by shrinking the work (a 20 m pad dug as
a 12 m one, a 360 m3 borrow strip planned as 600 m2 of stripping), and an advisory the repair
prompt never saw fixed none of it. Every structure whose dimensions imply a volume
(:func:`moonwright.pipeline.rates.dims_volumes`: a dug bowl, box or key trench, a placed berm
prism) must be covered by its tasks to at least :data:`~moonwright.pipeline.rates.VOLUME_LOW`
of the smallest volume the brief allows (:func:`~moonwright.pipeline.rates.volume_coverages`:
the m3 of its excavator digs on a dug shape or its hauls on a placed one, plus the stripping
credit of excavator m2 tasks on a dug shape, area x min(depth, 0.2 m) capped at the
footprint of the smallest size; ``min_<dimension>_m`` extras set the floor for a brief that
lets the work shrink, sloped walls the brief states are read at the rim, and a key trench is
judged only with its stated width). Below the stripped layer only digs count, an m2 pass
after the dig earns no credit, spoil bladed or backfilled is no dig, and a placed body whose
hauls wait for another structure's excavation is judged against the smaller of its own
volume and that spoil. The fill of a stockpile-like structure (type or label matching
stockpile | spoil | pile | depot | dump | heap, a berm excepted) is never short: a pile is
whatever gets hauled. The detail quotes the arithmetic (``S2 (borrow_strip): dimensions 100 x
6 x 0.6 m = 360 m3 of excavation; excavator tasks cover 120 m3 (33 %): T3 600 m2 counts 120 m3
at 0.2 m``, the share rounded down) and the hint says what to add (``plan the remaining 240
m3 of S2 as excavator tasks in m3 (split above 60 m3) instead of shrinking the work; keep the
m2 stripping only for the top 0.2 m``; for a berm, where its fill comes from). Over-scope
above :data:`~moonwright.pipeline.rates.VOLUME_HIGH` (150 % of the largest volume the brief
allows, the digs or hauls alone) stays an advisory of the task-graph stage, up to a *double
count*: dig parts or haul parts that sum to :data:`~moonwright.pipeline.rates.VOLUME_DOUBLE`
(175 %) of it plus 1 m3 or more count the body twice
(:func:`~moonwright.pipeline.rates.volume_double_counts`). Seen live (phase 3i): the
landing-pad demo's first graph hauled 2 x 120 m3 into the 120 m3 berm in 3 of 3 tries, the
berm's 40 x 1.5 x 4 m box rather than its triangular section; the advisory did not stop it,
and 2 of the 3 repairs answered the notes with a 114 m3 borrow pit the brief never mentions
(240 m3 less the 125.7 m3 of pad spoil). The verified plans of runs 2 and 3 that hauled a berm
at 200 % sized it as the box too, 12 of 14 ("560 m3 / 2 = 280 m3"). The largest volume the
brief allows is the dimensions' largest, or a larger volume the brief states on the
structure's line (isru_stockpile's "about 200 cubic metres" over the one 70 m3 strip Lightning
recorded); nothing is judged where the brief bounds no largest volume: a placed body whose
base width it does not state (its section is a guess), walls sloped by an unstated ratio, a
stockpile-like structure (dug or filled; a dug one keeps the advisory). A haul that drops its
material in a pile (a zone no structure claims, a stockpile) for a later haul of the body to
bring on is staging, never part of the body's double count. It is the
same ``volume:<structure>`` (a structure's shortfall and double count share one id and one
detail line), with the arithmetic in the detail (``S2 (blast_berm): dimensions 40 m long, 1.5
m high, 4 m wide at the base, triangular section = 120 m3 of fill; hauler tasks move 240 m3
(200 %), a double count: T8 120 m3, T9 120 m3 (240 m3 is the 40 x 1.5 x 4 m box, twice the
triangular section)``; parts that each repeat the body close ``(parts split a body's m3, they
do not each repeat it)``) and the cut in the hint (``its fill is length x height x base width / 2
(40 x 1.5 x 4 / 2 = 120 m3), not the box; cut the S2 hauls to 120 m3 in total (for example 2 x
60 m3), each part still waiting for the dig part that feeds it; never add a dig the brief does
not ask for``); a body whose hauls are a double count gets no supply balance note (the note's
"more than the digs before them remove" is what the invented pit answered). Like coverage, the
check judges a *whole* plan: the remaining graph of a replan has its finished work gone, so
``volume=None`` (the default) runs it exactly when ``coverage`` is on, ``volume=False``
switches it off even then (the advisory alone) and ``volume=True`` forces it without
coverage. Graphs without quantities (the hand-made fixtures) and structures without a
positive volume are untouched.

Supply order (``supply:<task_id>``, whole plans only)
----------------------------------------------------
CP-SAT schedules only the edges it is given, so a haul that waits for no excavation can start
before its material exists. Seen live (run 2, phase 3e): a horizon core made the repair drop
the berm hauls' edges to the pit and borrow digs, and the verified schedule hauled 280 m3 of
berm fill from hour 7 while the first dig ended at hour 13. A *haul* is a hauler task in m3
(:func:`moonwright.pipeline.rates.is_haul`); it places its material in the zone it names (the
coverage contract), so its *target* is the structure of that zone (the one its tag names when
several share it), or a *pile* when the target is stockpile-like or no structure claims the
zone. Its *sources* are the tasks that make material: every *dig* (an excavator task in m3
that is no rehandle, :func:`~moonwright.pipeline.rates.is_dig`: spoil bladed, pushed,
windrowed or backfilled was dug by another task), and an m2 *strip* or grade
(:func:`~moonwright.pipeline.rates.is_strip`) where its spoil can be what the haul moves: for a
haul into the strip's own structure (a haul lane's grading moved along the lane), into a pile
(road grading spoil to a stockpile, strip spoil to a dump zone), or in a graph that digs
nothing in m3 (a berm "built from the hardstand's strip spoil"); a berm built from pit spoil is
not supplied by the haul lane's grading (seen live: propellant_pit_and_berm's berm hauls waited
for the lane alone and ran before the pit was dug). A source makes available its m3, a strip
its area x 0.2 m (:func:`~moonwright.pipeline.rates.supply_m3`). A graph without any source
hauls material from outside the plan and is not judged. Otherwise a haul is ``supply:<haul>``
when

* it waits (through its ``after`` edges, transitively, through any task) for no source: ``T7
  hauls 280 m3 for S2 (plume berm) but waits for no excavation, so it could start before any
  material is dug``, the hint naming every source that does not wait for it, grouped by
  structure (``make T7 and T8 wait for the digs that supply them (S1 foundation pit: T3, T4;
  S3 borrow strip: T5, T6)``); or
* it is no pile's haul and moves, with the hauls into its target before it, more than
  :data:`SUPPLY_SLACK` (1.25: dug regolith swells by about a quarter) of what its sources make
  available plus :data:`SUPPLY_SLACK_M3`, while a dig in m3 that neither precedes nor waits
  for it could supply the rest (the review's key-trench loophole: a 300 m3 berm haul that
  waits for its own 15 m3 key-trench cut alone while the pad is dug beside it): ``T7 hauls
  300 m3 for S2 (blast berm) but the digs before it remove only 15 m3 (S2 blast berm: T6), so
  it could start before all of its material is dug``, the hint ``make T7 wait for more of the
  digs that supply it (S1 landing pad: T2, T3)``. A second surface strip is never asked for.

Sources are grouped by structure, at most four structures and four ids each: for a haul into a
placed body (a berm, a mound) the other structures first and its own (a key trench) last,
otherwise its own first, then requirements order. The hint closes with how to keep a
deadline: ``a haul part may wait for only the dig parts that remove what it moves, so split
hauls to keep both chains short.`` Ids follow the hauls' order in the graph. Like volume it
judges a whole plan (a replan's remaining graph has its digs finished): ``supply=None`` (the
default) runs it exactly when the volume check runs, so the planner's ``volume=whole``
switches both, ``False`` / ``True`` override it. It runs after volume and before energy. When
every source a body's hauls can wait for already comes before them and they still move more
than those make available (landing_pad's 300 m3 berm prism "built from pad spoil" after
125.7 m3 of bowl: the brief's own arithmetic), the plan stands and
:func:`supply_advisories` notes it per structure (``supply:S2: its hauls (T5 150 m3, T6 150
m3) move 300 m3, more than the 125.7 m3 the digs before them remove (S1 landing pad: T3,
T4)``), summed per target so chained and parallel parts read the same; never for a pile, for
a structure one of whose hauls is in conflict, nor for a body whose hauls are a double count
(the ``volume:<structure>`` verdict says why they move too much).

Quantities (``quantity:<task_id>``)
-----------------------------------
Once a graph states quantities (any task of it, or of the graph the repair answered, carries
a quantity with a unit), the rate model owns every duration and energy, so every *physical*
task of a rated robot type (:func:`needs_quantity`: excavator, hauler, compactor or printer
work that excavates, grades, hauls, compacts or sinters by :func:`task_phases`; surveyors and
pure inspection passes are exempt) must carry a positive quantity in a unit the rate table
prices for its type. A task without one, with a quantity of 0, or counted in a unit the table
has no row for would otherwise keep the model's own hours and Wh next to grounded tasks,
which is the hand-sizing the rate model exists to prevent. A graph with no quantity at all
(the hand-made fixtures, a solver-only replan) is judged on its own numbers and the task-graph
stage marks it ungrounded in its advisories instead.

Structure coverage (``scope:<structure_id>``, ``coverage=True``)
----------------------------------------------------------------
Opt-in because it judges a whole plan: a fresh graph and its repairs are checked with
``coverage=True``; the remaining graph of a replan (finished phases gone) with
``coverage=False``, where :func:`check_scope` guards the outstanding work instead.
Every structure in ``req.structures`` must be served by the graph. A task serves a structure
when its ``structure`` tag names it (:func:`tag_names`: the structure's id in any case, its
type, or a label that contains the id as a word, such as "Landing pad S1"), or when it sits
in the structure's zone while tagged with nothing that names any structure (no tag, or a tag
that resolves to none); both live in ``moonwright.pipeline.tags`` so the volume advisories
resolve tags the same way. A hauler task placed in the structure's zone counts as its haul
whatever its tag (:func:`moonwright.pipeline.tags.hauls_into`: material dropped in the
berm's zone builds the berm, even when the model tagged "haul pad spoil to berm line" with
the pad). What a structure needs depends on its ``type`` (matched by keyword after
lower-casing: "berm", "road", "foundation", "pad"; anything else is "other"):

  pad (type contains "pad") ........... survey, excavate or grade, compact, sinter
  berm (contains "berm") .............. excavate or haul, and compact
  road (contains "road") .............. grade and compact
  foundation (contains "foundation") .. excavate and compact
  other ............................... at least one task

A pad is dug or stripped (an instrument pad "stripped of fines and levelled" is graded in
m2, a landing pad's bowl is excavated in m3); when the brief says which, its listed
operation requires that one.

A phase is only required while the fleet has a robot of its type (surveyor, excavator,
hauler, compactor, printer): "sinter when a printer exists" from the brief, applied to every
phase so a fleet that cannot staff a phase is never asked for it.
A task's phases are the union of three signals, so the check only fires when work is really
absent, not when it is named unusually: the robot type (surveyor -> survey, hauler -> haul,
compactor -> compact, printer -> sinter; an excavator counts as excavate with unit m3 or m and
as grade with unit m2), the unit, and keyword stems in the task name (survey/stake/inspect/
scan/map; excavat/dig/trench/cut/bowl/pit/borrow; grad/strip/level/smooth/blade;
haul/transport/carr/mov/deliver/stag/dump; compact/roll/tamp/consolidat; sinter/print/melt/
pav/fus/bak). An excavator task with no unit and no recognised keyword counts as both
excavate and grade. With ``previous`` (the graph a repair answered), a structure that had
tasks before and has none now is reported with the tasks it lost, so the repair prompt can
ask for them back by id.
On top of its kind, every operation a structure lists (``Structure.operations``: the phases
the brief explicitly asks for on it, "sintered crest" -> sinter, "built from pad spoil" ->
haul, "key trench" -> excavate, "inspected" -> survey) is required as a group of its own
(:func:`required_phases`), under the same fleet rule: an operation no robot of the fleet can
perform is a site warning (``moonwright.pipeline.structure.site_warnings``), never a conflict.
A listed operation is met only by a task of its own robot type doing that phase
(:func:`performs`: a listed ``haul`` by a hauler task tagged with the receiving structure or
placed in its zone, a listed ``sinter`` by a printer task), not by the lenient name-keyword
union that serves the kind heuristic, so a hauler named "Haul bowl spoil" does not stand in
for the key trench the brief asked for. The hint names the operation and quotes the brief
where :func:`brief_evidence` finds the ask ("S2 (blast_berm) lists sinter (the brief asks for
"with a sintered crest to resist plume erosion") but no task sinters it; add a printer task
tagged S2"); the evidence is the phase's own verbs (excavat/dig/trench/cut, never a noun such
as "borrow" that names the structure the material comes from), so a berm "built from a
borrow strip" is not quoted as an ask to excavate the berm. One over-declaration a small
structuring model makes is caught here rather than planned: when a structure of a known kind
lists both alternatives of one of its kind groups ("excavate or haul" of a berm, "excavate or
grade" of a pad) and the brief supports only one of them, the other is *unsupported*
(:func:`unsupported_operations`): not required, reported on the requirements card by the
structure stage, and never a reason to invent work (seen live: ``excavate`` listed on a berm
the brief says is "built from a borrow strip ... placed and compacted", and the repair added
a 35 m3 "foundation" dig to satisfy it). Requirements that list no operation anywhere are
judged exactly as before.

    check_scope(graph, required, baseline=None) -> Failure | None

Scope guard of a replan (or any run that must keep given tasks): ``missing:<task_id>`` for a
required id the graph dropped and, with ``baseline`` (the remaining graph the model was given),
``changed:<task_id>`` for a required task whose definition no longer matches it.

Implementation notes
--------------------
* The result is deterministic: conflict groups appear in the order of the checks above and
  conflict ids are sorted alphabetically within a group, except ``supply:<task_id>``, which
  follows the hauls' order in the graph (T9 before T10, as the hint names them). ``Failure.details[i]`` is a short
  factual line about ``Failure.conflicts[i]`` (same length, same order).
* Precedence checks run on the id-level graph: ``p in t.after`` is the edge ``p -> t``. A
  duplicated task id merges the ``after`` lists of its copies and keeps the longest duration;
  ``after`` entries naming unknown tasks are dropped from the graph because they are already
  reported as ``reference`` conflicts.
* Cycles are the ones closed by a DFS back edge (each reported once, rotated to start at the
  smallest id). The critical path is only computed when no cycle was found.
* Capacity checks (``energy_task``, ``horizon``, ``energy``) skip robot types without any
  robot; those tasks are already reported as ``capability`` conflicts.
* Energy is compared in integer milli-Wh (:func:`moonwright.schemas.to_mwh`), the unit the
  CP-SAT model and ``check_schedule`` use too, so the validator never passes a budget the
  solver rejects (or the reverse) over a rounding difference, and float noise from summing
  Wh values cannot produce a conflict.
* Zones a task names but ``req.site.zones`` does not declare are allowed (the simulator lays
  them out), so they are never flagged.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from moonwright.geometry import distance, start_positions, travel_wh, zone_positions
from moonwright.pipeline.rates import (
    DOUBLE_MARK,
    RATED_TYPES,
    SPLIT_ABOVE_M3,
    STRIP_DEPTH_M,
    VolumeCoverage,
    ancestors,
    dims_volume,
    is_dig,
    is_haul,
    is_strip,
    rate_for,
    rated_units,
    scoped,
    stockpile_like,
    supply_m3,
    volume_conflicts,
    volume_double_counts,
)
from moonwright.pipeline.tags import (
    brief_lines,
    haul_target,
    hauls_into,
    name_score,
    structure_tasks,
    tag_names,
)
from moonwright.schemas import (
    MWH_PER_WH,
    Failure,
    Requirements,
    Robot,
    Structure,
    Task,
    TaskGraph,
    fmt_wh,
    to_mwh,
)
from moonwright.text import join_and as _join_and
from moonwright.text import pct, qty
from moonwright.text import plural as _plural
from moonwright.text import wh as _wh
from moonwright.text import whole_wh as _whole_wh

__all__ = [   # tag_names / structure_tasks live in ``tags`` and stay importable from here
    "SUPPLY_SLACK", "SUPPLY_SLACK_M3", "Supply", "brief_evidence", "check_scope",
    "covered_phases", "hauls_into", "missing_operations", "missing_phases", "needs_quantity",
    "performs", "required_phases", "structure_kind", "structure_tasks", "supply_advisories",
    "supply_report", "tag_names", "task_changes", "task_phases", "transit_allowances",
    "unstaffed_operations", "unsupported_operations", "validate_graph",
]

Positions = Mapping[str, tuple[float, float]]


def validate_graph(req: Requirements, graph: TaskGraph, previous: TaskGraph | None = None,
                   *, coverage: bool = False, volume: bool | None = None,
                   supply: bool | None = None,
                   robot_positions: Positions | None = None) -> Failure | None:
    """Run every check and return a ``validation`` Failure, or None when the graph is clean.

    ``coverage=True`` adds the structure coverage check (``scope:<structure_id>``). It is an
    opt-in because it judges a *whole* plan: the remaining graph of a replan legitimately
    lacks the phases that are already built, so a replan must be checked with
    ``coverage=False`` (its work is guarded by :func:`check_scope` instead) and a fresh plan
    and its repairs with ``coverage=True``. ``volume`` is the volume check
    (``volume:<structure_id>``), which judges a whole plan the same way: ``None`` (the
    default) runs it exactly when ``coverage`` is on, so a replan checked with
    ``coverage=False`` never sees it, ``False`` switches it off even on a whole plan (the
    task-graph stage's advisory alone) and ``True`` forces it without coverage. ``supply`` is
    the supply-order check (``supply:<task_id>``, a haul that waits for no source, or for too
    little while another dig could supply it), a whole-plan judgment too: ``None`` (the default) follows the volume check's switch as resolved above,
    ``False`` / ``True`` override it. ``robot_positions`` places the robots for the transit the energy checks charge (robot id
    -> grid cell, what a replan passes the solver as ``SolveOptions.robot_positions``); the
    depot for everyone without it. ``previous`` is the graph a repair answered (if any): a
    structure it served that ``graph`` no longer serves is reported with the tasks that went
    missing.
    """
    prec = _Precedence.from_graph(graph)
    cycles = _check_cycles(prec)
    transit = transit_allowances(req, graph, robot_positions)
    pools = _energy_pools(req, graph, transit)
    volume_on = coverage if volume is None else volume
    supply_on = volume_on if supply is None else supply
    groups = [
        _check_duplicates(req, graph),
        _check_references(graph),
        cycles,
        _check_capability(req, graph),
        _check_quantity(graph, previous),
        _check_coverage(req, graph, previous) if coverage else None,
        _check_volume(req, graph) if volume_on else None,
        _check_supply(req, graph) if supply_on else None,
        _check_task_energy(graph, transit, pools),
        None if cycles else _check_critical_path(prec, req.horizon_h),
        _check_type_horizon(req, graph),
        _check_type_energy(pools),
    ]
    found = [g for g in groups if g is not None]
    if not found:
        return None
    return Failure(
        kind="validation",
        conflicts=[cid for g in found for cid in g.ids],
        hint=" ".join(g.hint for g in found),
        details=[line for g in found for line in g.details],
    )


# ----------------------------------------------------------------------------- scope guard
def task_changes(was: Task, now: Task, ignore_after: Collection[str] = ()) -> list[str]:
    """How ``now`` departs from the outstanding work ``was`` describes (empty when it does not).

    A replanned task must keep its robot type, its zone and every predecessor edge (except
    edges to the ``ignore_after`` tasks, e.g. ones already reported missing), and it may not
    shrink: ``duration_h`` and ``energy_wh`` are what is physically still left, so a shorter
    or cheaper copy would schedule work that never gets done. Growing, adding edges and adding
    new tasks are fine (the solver judges them).
    """
    why: list[str] = []
    if now.robot_type != was.robot_type:
        why.append(f"robot_type {now.robot_type} (was {was.robot_type})")
    if now.zone != was.zone:
        why.append(f"zone {now.zone} (was {was.zone})")
    dropped = sorted(set(was.after) - set(now.after) - set(ignore_after))
    if dropped:
        why.append(f"dropped after {', '.join(dropped)}")
    if now.duration_h < was.duration_h:
        why.append(f"duration_h {now.duration_h} (needs {was.duration_h})")
    if to_mwh(now.energy_wh) < to_mwh(was.energy_wh):
        why.append(f"energy_wh {fmt_wh(to_mwh(now.energy_wh))} "
                   f"(needs {fmt_wh(to_mwh(was.energy_wh))})")
    return why


def check_scope(graph: TaskGraph, required: Collection[str],
                baseline: TaskGraph | None = None) -> Failure | None:
    """``missing:<task>`` for every required task id the graph no longer contains and, with a
    ``baseline``, ``changed:<task>`` for every required task it redefined (:func:`task_changes`).
    ``conflicts`` and ``details`` stay aligned; None when the scope is intact."""
    present = {t.id: t for t in graph.tasks}
    wanted = list(dict.fromkeys(required))
    missing = [t for t in wanted if t not in present]
    changed: list[tuple[str, str]] = []
    if baseline is not None:
        originals = {t.id: t for t in baseline.tasks}
        for tid in wanted:
            if tid in present and tid in originals:
                why = task_changes(originals[tid], present[tid], ignore_after=missing)
                if why:
                    changed.append((tid, "; ".join(why)))
    if not missing and not changed:
        return None
    hints = []
    if missing:
        them = "it" if len(missing) == 1 else "them"
        hints.append(f"The graph dropped {', '.join(missing)}; every remaining task must stay "
                     f"in the plan with its id, zone and remaining duration, so add {them} back.")
    if changed:
        ids = [tid for tid, _ in changed]
        hints.append(f"The graph redefined {', '.join(ids)}; every remaining task must keep its "
                     "robot_type, zone and after edges and at least its remaining duration_h "
                     "and energy_wh (the work still outstanding), so restore "
                     f"{'it' if len(ids) == 1 else 'them'} from the remaining task graph.")
    return Failure(
        kind="validation",
        conflicts=[f"missing:{t}" for t in missing] + [f"changed:{t}" for t, _ in changed],
        hint=" ".join(hints),
        details=[f"{t} is required but missing from the graph" for t in missing]
        + [f"{t} changed: {why}" for t, why in changed],
    )


# ----------------------------------------------------------------------------- data
@dataclass(frozen=True)
class _Group:
    """One conflict kind: sorted conflict ids, one detail line per id, one hint sentence."""

    ids: list[str]
    details: list[str]
    hint: str


@dataclass(frozen=True)
class _Precedence:
    """Id-level precedence graph with the edge ``p -> t`` for every ``p`` in ``t.after``."""

    order: list[str]  # unique task ids in first-seen order
    duration: dict[str, int]  # id -> duration_h (longest copy when the id is duplicated)
    preds: dict[str, list[str]]  # id -> known predecessors, deduplicated, in `after` order
    succ: dict[str, list[str]]  # id -> successors, deduplicated, in task order

    @classmethod
    def from_graph(cls, graph: TaskGraph) -> _Precedence:
        order = list(dict.fromkeys(graph.ids()))
        duration: dict[str, int] = {}
        preds: dict[str, list[str]] = {i: [] for i in order}
        succ: dict[str, list[str]] = {i: [] for i in order}
        for t in graph.tasks:
            duration[t.id] = max(duration.get(t.id, 0), t.duration_h)
            for p in t.after:
                if p in preds and p not in preds[t.id]:
                    preds[t.id].append(p)
                    succ[p].append(t.id)
        return cls(order, duration, preds, succ)

    def cycles(self) -> list[list[str]]:
        """Cycles closed by DFS back edges, each once, rotated to start at their smallest id.

        Iterative DFS: ``path`` is the grey stack, ``frames`` the successor iterator of each
        node on it. Meeting a grey node closes the cycle ``path[index(node):]``.
        """
        white, grey, black = 0, 1, 2
        colour = dict.fromkeys(self.order, white)
        found: list[list[str]] = []
        for root in self.order:
            if colour[root] != white:
                continue
            colour[root] = grey
            path = [root]
            frames = [iter(self.succ[root])]
            while frames:
                for nxt in frames[-1]:
                    if colour[nxt] == grey:
                        cycle = path[path.index(nxt):]
                        k = cycle.index(min(cycle))
                        found.append(cycle[k:] + cycle[:k])
                    elif colour[nxt] == white:
                        colour[nxt] = grey
                        path.append(nxt)
                        frames.append(iter(self.succ[nxt]))
                        break
                else:  # every successor handled: retreat
                    colour[path.pop()] = black
                    frames.pop()
        return found

    def critical_path(self) -> tuple[int, list[str]]:
        """Longest duration-weighted chain of an acyclic graph: ``(hours, [ids...])``.

        Dynamic programme over a Kahn topological order; ties keep the first candidate in
        ``after`` / task order, so the answer only depends on the input.
        """
        indegree = {i: len(self.preds[i]) for i in self.order}
        ready = deque(i for i in self.order if indegree[i] == 0)
        best: dict[str, int] = {}  # insertion order == topological order
        via: dict[str, str | None] = {}
        while ready:
            node = ready.popleft()
            prev = max(self.preds[node], key=best.__getitem__, default=None)
            via[node] = prev
            best[node] = self.duration[node] + (best[prev] if prev is not None else 0)
            for nxt in self.succ[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
        end = max(best, key=best.__getitem__)
        path: list[str] = []
        node: str | None = end
        while node is not None:
            path.append(node)
            node = via[node]
        return best[end], path[::-1]


# ----------------------------------------------------------------------------- checks
def _check_duplicates(req: Requirements, graph: TaskGraph) -> _Group | None:
    namespaces = (
        ("task", graph.ids()),
        ("robot", [r.id for r in req.robots]),
        ("zone", [z.id for z in req.site.zones]),
    )
    repeats: dict[str, list[str]] = {}
    for label, ids in namespaces:
        for id_, n in Counter(ids).items():
            if n > 1:
                repeats.setdefault(id_, []).append(f"{label} id '{id_}' appears {n} times")
    if not repeats:
        return None
    ids = sorted(repeats)
    return _Group(
        ids=[f"duplicate:{i}" for i in ids],
        details=["; ".join(repeats[i]) for i in ids],
        hint=f"Ids must be unique, but {_join_and([p for i in ids for p in repeats[i]])}.",
    )


def _check_references(graph: TaskGraph) -> _Group | None:
    known = set(graph.ids())
    items = {
        f"reference:{t.id}.after={p}": f"{t.id}.after names unknown task {p}"
        for t in graph.tasks
        for p in t.after
        if p not in known
    }
    if not items:
        return None
    ids = sorted(items)
    refs = [i.removeprefix("reference:") for i in ids]
    return _Group(
        ids=ids,
        details=[items[i] for i in ids],
        hint=f"Some 'after' entries name tasks that do not exist ({_join_and(refs)}); "
        "only ids present in the graph may be used.",
    )


def _check_cycles(prec: _Precedence) -> _Group | None:
    cycles = prec.cycles()
    if not cycles:
        return None
    paths = sorted("->".join([*c, c[0]]) for c in cycles)
    noun = "cycles" if len(paths) > 1 else "cycle"
    return _Group(
        ids=[f"cycle:{p}" for p in paths],
        details=[f"precedence cycle {p}" for p in paths],
        hint=f"The precedence {noun} {_join_and(paths)} can never start because every task in "
        "a cycle waits for another task of the same cycle; remove at least one 'after' edge "
        "per cycle.",
    )


def _check_capability(req: Requirements, graph: TaskGraph) -> _Group | None:
    unserved = [t for t in graph.tasks if not req.robots_of_type(t.robot_type)]
    if not unserved:
        return None
    items = {
        f"capability:{t.id}": f"{t.id} needs robot type '{t.robot_type}' but no robot has it"
        for t in unserved
    }
    by_type: dict[str, set[str]] = {}
    for t in unserved:
        by_type.setdefault(t.robot_type, set()).add(t.id)
    needs = [
        f"{_join_and(sorted(tasks))} need{'s' if len(tasks) == 1 else ''} robot type '{typ}'"
        for typ, tasks in sorted(by_type.items())
    ]
    available = sorted({r.type for r in req.robots})
    offer = (
        f"robot type in the requirements is {available[0]}"
        if len(available) == 1
        else f"robot types in the requirements are {_join_and(available)}"
    )
    ids = sorted(items)
    return _Group(
        ids=ids,
        details=[items[i] for i in ids],
        hint=f"No capable robot exists: {_join_and(needs)}, but the only {offer}; reassign "
        "these tasks to an available type.",
    )


def transit_allowances(req: Requirements, graph: TaskGraph,
                       robot_positions: Positions | None = None) -> list[int]:
    """Per task of ``graph`` (in order), the transit allowance in milli-Wh the energy checks
    charge: the least any robot of the task's type would be charged by CP-SAT, which
    reserves for task *i* on robot *r* the longest leg *r* could have to drive to the task's
    zone, from its start position (``robot_positions``, else the depot) or from the zone of
    any task of its type, at :data:`moonwright.geometry.TRAVEL_WH_PER_CELL` per cell
    (``moonwright.pipeline.schedule``, "Travel"). Zone centres come from
    :func:`moonwright.geometry.zone_positions` over every zone the graph names, exactly as
    the solver lays them out. 0 for a task of a type the fleet lacks (a ``capability``
    conflict)."""
    layout = zone_positions(req.site, (t.zone for t in graph.tasks))
    starts = start_positions(req, robot_positions)
    reach: dict[str, set[tuple[int, int]]] = {}   # robot type -> zone cells of its tasks
    for t in graph.tasks:
        reach.setdefault(t.robot_type, set()).add(layout[t.zone])
    out: list[int] = []
    for t in graph.tasks:
        robots = req.robots_of_type(t.robot_type)
        if not robots:
            out.append(0)
            continue
        here = layout[t.zone]
        farthest = max(distance(p, here) for p in reach[t.robot_type])
        cells = min(max(distance(starts[r.id], here), farthest) for r in robots)
        out.append(to_mwh(travel_wh(cells)))
    return out


def _decimals_apart(need: int, budget: int) -> int:
    """The fewest decimals (0 to 3) at which ``need`` (milli-Wh, above ``budget``) still
    prints above ``budget``: 0 for every whole-Wh case, more only for the fractional figures
    a replan hands over (4,200.251 Wh against 4,200.25 Wh), so no sentence ever reads "needs
    4,200 Wh but has 4,200 Wh". The twin of ``moonwright.pipeline.schedule._decimals_apart``;
    :func:`moonwright.text.wh` rounds the same way (half up on the integer milli-Wh)."""
    for decimals in range(3):
        unit = MWH_PER_WH // 10 ** decimals
        if (need + unit // 2) // unit > (budget + unit // 2) // unit:
            return decimals
    return 3


def _travel_clause(mwh: int) -> str:
    """', about 38 Wh of it travel' when the transit share is worth a Wh, else ''."""
    return f", about {_wh(mwh)} of it travel" if _whole_wh(mwh) >= 1 else ""


@dataclass(frozen=True)
class _Pool:
    """The energy of one robot type the graph uses and the fleet has: its tasks (positions in
    ``graph.tasks``), their work and transit (milli-Wh), its batteries, and the tasks larger
    than its largest battery with by how much (``over``: position -> milli-Wh)."""

    typ: str
    positions: list[int]
    robots: list[Robot]
    work: int
    travel: int
    cap: int
    over: dict[int, int]

    @property
    def need(self) -> int:
        return self.work + self.travel

    @property
    def have(self) -> int:
        return sum(to_mwh(r.battery_wh) for r in self.robots)

    @property
    def short(self) -> bool:
        """Whether the type's work plus transit exceeds its batteries combined."""
        return self.need > self.have

    @property
    def explained(self) -> bool:
        """Whether the overrun is its oversized tasks alone: cut each of them to the largest
        battery and the type would fit (module docstring, "Energy with transit")."""
        return self.short and bool(self.over) and self.need - sum(self.over.values()) <= self.have

    @property
    def fleet(self) -> str:
        """The subject and verb of a sentence about the pool: '1 printer carries', '2 printers
        carry'."""
        verb = "carries" if len(self.robots) == 1 else "carry"
        return f"{_plural(len(self.robots), self.typ)} {verb}"


def _energy_pools(req: Requirements, graph: TaskGraph, transit: list[int]) -> list[_Pool]:
    """One :class:`_Pool` per robot type of the graph with robots, sorted by type."""
    pools: list[_Pool] = []
    for typ, positions, robots in _served_types(req, graph):
        cap = max(to_mwh(r.battery_wh) for r in robots)
        over = {i: to_mwh(graph.tasks[i].energy_wh) + transit[i] - cap for i in positions
                if to_mwh(graph.tasks[i].energy_wh) + transit[i] > cap}
        pools.append(_Pool(typ=typ, positions=positions, robots=robots,
                           work=sum(to_mwh(graph.tasks[i].energy_wh) for i in positions),
                           travel=sum(transit[i] for i in positions), cap=cap, over=over))
    return pools


def _check_task_energy(graph: TaskGraph, transit: list[int], pools: list[_Pool]) -> _Group | None:
    items: dict[str, str] = {}
    phrases: dict[str, str] = {}
    for pool in pools:
        for i in pool.over:
            t, cap = graph.tasks[i], pool.cap
            work = to_mwh(t.energy_wh)
            need = work + transit[i]
            cid = f"energy_task:{t.id}"
            decimals = _decimals_apart(need, cap)
            travel = _travel_clause(transit[i])
            items[cid] = (
                f"{t.id} needs {_wh(need, decimals)}{travel}{',' if travel else ''} but the "
                f"largest {t.robot_type} battery is {_wh(cap, decimals)}"
            )
            against = f"against the largest {t.robot_type} battery of {_wh(cap, decimals)}"
            phrases[cid] = (
                f"{t.id} ({_wh(work)} of work plus about {_wh(transit[i])} of travel, "
                f"{_wh(need, decimals)} {against})" if travel
                else f"{t.id} ({_wh(need, decimals)} {against})"
            )
    if not items:
        return None
    ids = sorted(items)
    # a type whose overrun these tasks alone explain gets no verdict of its own (one per
    # cause), so the hint says what that verdict would have: splitting cannot fit them
    pooled = [
        f"The fleet's {pool.fleet} {_wh(pool.have, _decimals_apart(pool.need, pool.have))} in "
        f"all, so splitting {_join_and(sorted(graph.tasks[i].id for i in pool.over))} alone "
        f"cannot fit {'it' if len(pool.over) == 1 else 'them'}: keep the full work unless the "
        "brief allows less."
        for pool in pools if pool.explained
    ]
    head = (f"No single robot can power {_join_and([phrases[i] for i in ids])}; split such "
            "tasks into smaller quantities or move the work to another robot type.")
    return _Group(ids=ids, details=[items[i] for i in ids], hint=" ".join([head, *pooled]))


def _check_critical_path(prec: _Precedence, horizon_h: int) -> _Group | None:
    hours, path = prec.critical_path()
    if hours <= horizon_h:
        return None
    chain = "->".join(path)
    return _Group(
        ids=[f"horizon_path:{chain}"],
        details=[f"{chain} takes {hours} h in sequence; horizon is {horizon_h} h"],
        hint=f"The critical path {chain} alone takes {hours} h, longer than the {horizon_h} h "
        "horizon; shorten or split tasks on this chain, or drop unnecessary 'after' edges so "
        "more work runs in parallel (never a dig-to-haul edge: split the hauls per dig part "
        "instead).",
    )


def _check_type_horizon(req: Requirements, graph: TaskGraph) -> _Group | None:
    phrases: list[str] = []
    details: list[str] = []
    for typ, positions, robots in _served_types(req, graph):
        total = sum(graph.tasks[i].duration_h for i in positions)
        bound = -(-total // len(robots))  # ceil(total / robots) in integer arithmetic
        if bound > req.horizon_h:
            phrases.append(
                f"{typ} work ({total} h over {_plural(len(robots), 'robot')} needs at least "
                f"{bound} h)"
            )
            details.append(
                f"{typ}: {total} h over {_plural(len(robots), 'robot')} needs >= {bound} h; "
                f"horizon is {req.horizon_h} h"
            )
    if not phrases:
        return None
    return _Group(
        ids=["horizon"],
        details=["; ".join(details)],
        hint=f"Even spread evenly over the available robots, {_join_and(phrases)} cannot finish "
        f"within the {req.horizon_h} h horizon; shorten those tasks or move work to other "
        "robot types.",
    )


def _check_type_energy(pools: list[_Pool]) -> _Group | None:
    ids: list[str] = []
    details: list[str] = []
    phrases: list[str] = []
    for pool in pools:                       # sorted by type == sorted ids
        if not pool.short or pool.explained:  # explained: its energy_task verdicts say it
            continue
        need, have = pool.need, pool.have
        decimals = _decimals_apart(need, have)
        ids.append(f"energy:{pool.typ}")
        fleet = _plural(len(pool.robots), "robot")
        carry = "carries" if len(pool.robots) == 1 else "carry"
        details.append(
            f"{pool.typ} tasks need {_wh(need, decimals)}{_travel_clause(pool.travel)}; its "
            f"{fleet} {carry} {_wh(have, decimals)}"
        )
        split = (f", {_wh(pool.work)} of work and about {_wh(pool.travel)} of travel,"
                 if _whole_wh(pool.travel) >= 1 else "")
        phrases.append(
            f"{pool.typ} tasks need {_wh(need, decimals)}{split} but its {fleet} {carry} only "
            f"{_wh(have, decimals)} combined"
        )
    if not ids:
        return None
    return _Group(
        ids=ids,
        details=details,
        hint=f"Battery budgets are exceeded per robot type: {_join_and(phrases)}; reduce the "
        "quantities if the brief allows it or move work to other robot types.",
    )


# ----------------------------------------------------------------------------- volume
def _volume_detail(c: VolumeCoverage) -> str:
    """'dimensions 100 x 6 x 0.6 m = 360 m3 of excavation; excavator tasks cover 120 m3
    (33 %): T3 600 m2 counts 120 m3 at 0.2 m'; a key trench opens 'its key trench is 60 x
    1.2 x 0.8 m = 57.6 m3 of excavation', a placed body says what it is made of ('= 300 m3
    of fill, more than the 125.7 m3 of spoil S1 yields; hauler tasks cover 50 m3 (39 % of
    that spoil)'), and a dig whose stripping credit stands in for the layer below closes
    with that layer ('; below the top 0.2 m only digs in m3 count, 26 of 62.8 m3 (41 %)').
    The share is rounded down: a verdict on "under 70 %" never prints 70 %."""
    source, of = "", c.of_floor
    if c.sourced:
        verb = "yields" if len(c.sources) == 1 else "yield"
        source = (f", more than the {qty(c.floor_m3)} m3 of spoil "
                  f"{_join_and(list(c.sources))} {verb}")
        of = " of that spoil"
    return (f"{c.shape.head} = {c.shape.m3_text} m3 of {c.shape.material}{source}; {c.robot} "
            f"tasks cover {qty(c.counted_m3)} m3 ({pct(c.share, floor=True)}{of}): "
            f"{c.listing}{c.deep_note()}")


def _volume_sentence(c: VolumeCoverage) -> str:
    """'S2 (borrow_strip) plans 120 of 360 m3: plan the remaining 240 m3 of S2 as excavator
    tasks in m3 (split above 60 m3) instead of shrinking the work; keep the m2 stripping only
    for the top 0.2 m.' A placed body names where its fill comes from: the spoil its hauls
    wait for ('125.7 m3, all the spoil of S1 its hauls wait for'), else the dig that has to
    yield it ('each after the excavation its fill comes from, a borrow dig in m3 when no
    structure's spoil supplies it')."""
    floor = f"{qty(c.floor_m3)} m3"
    if c.sourced:
        floor += f", all the spoil of {_join_and(list(c.sources))} its hauls wait for"
    elif c.shape.ranged:
        floor += f", the smallest size the brief allows ({c.shape.floor_how})"
    where = f"the {c.structure.id} key trench" if c.shape.kind == "trench" else c.structure.id
    if c.shape.phase == "excavation":
        tail = " instead of shrinking the work"
        if c.area_m2 > 0:
            tail += f"; keep the m2 stripping only for the top {qty(STRIP_DEPTH_M)} m"
        passes = c.ids("after")
        if passes:
            tail += (f"; an m2 pass after the dig ({_join_and(passes)}) removes no surface "
                     "layer")
        moved = c.ids("rehandle")
        if moved:
            tail += f"; moving spoil already dug ({_join_and(moved)}) digs nothing"
    elif c.sourced:
        tail = " instead of shrinking the work"
    else:
        tail = (", each after the excavation its fill comes from (a borrow dig in m3 when no "
                "structure's spoil supplies it), instead of shrinking the work")
    return (f"{c.label} plans {qty(c.counted_m3)} of {floor}: plan the remaining "
            f"{qty(c.remaining_m3)} m3 of {where} as {c.robot} tasks in m3 "
            f"(split above {qty(SPLIT_ABOVE_M3)} m3){tail}.")


def _boxed(c: VolumeCoverage) -> bool:
    """Whether a double count's hauls move the box of a triangular prism (its length x height
    x base width, twice the section the check measures), within 2 %: how the live plans sized
    their berms (module docstring, "Volume")."""
    return bool(c.shape.box) and math.isclose(c.excess_m3, 2 * c.shape.m3, rel_tol=0.02)


def _double_detail(c: VolumeCoverage) -> str:
    """The arithmetic of a double count, as :func:`_volume_detail` gives a shortfall's:
    'dimensions 40 m long, 1.5 m high, 4 m wide at the base, triangular section = 120 m3 of
    fill; hauler tasks move 240 m3 (200 %), a double count: T8 120 m3, T9 120 m3 (240 m3 is
    the 40 x 1.5 x 4 m box, twice the triangular section)' when the hauls move that box (the
    live landing-pad graphs), else '... (parts split a body's m3, they do not each repeat
    it)' ('a body's m3 is dug once' for one task; a key trench opens 'its key trench is 60 x
    1.2 x 0.8 m = 57.6 m3 of excavation'). The share is of the largest volume the brief allows
    (:attr:`~moonwright.pipeline.rates.VolumeCoverage.of_largest`)."""
    shape = c.shape
    verb = "move" if shape.phase == "haul" else "remove"
    if _boxed(c):
        why = f"{qty(c.excess_m3)} m3 is the {shape.box} m box, twice the triangular section"
    elif c.parts > 1:
        why = "parts split a body's m3, they do not each repeat it"
    else:
        why = f"a body's m3 is {'hauled' if shape.phase == 'haul' else 'dug'} once"
    return (f"{shape.head} = {shape.m3_text} m3 of {shape.material}; {c.robot} tasks {verb} "
            f"{qty(c.excess_m3)} m3 ({pct(c.share_largest)}{c.of_largest}){DOUBLE_MARK}: "
            f"{c.listing_m3} ({why})")


def _double_sentence(c: VolumeCoverage) -> str:
    """'S2 (blast_berm) plans 240 m3 of hauls for its 120 m3 blast berm: its fill is length x
    height x base width / 2 (40 x 1.5 x 4 / 2 = 120 m3), not the box; cut the S2 hauls to 120
    m3 in total (for example 2 x 60 m3), each part still waiting for the dig part that feeds
    it; never add a dig the brief does not ask for.' (the box clause only when the hauls move
    that box; the live repairs answered the double haul with an invented 114 m3 borrow pit).
    A dug shape keeps the hauls of its spoil after the dig parts that remove it; one task
    alone gets no example split."""
    shape = c.shape
    noun = "haul" if shape.phase == "haul" else "dig"
    body = "key trench" if shape.kind == "trench" else _type_words(c.structure)
    largest = c.largest_m3 or shape.m3
    target = f"{qty(largest)} m3"
    whose = f"{c.structure.id} key trench" if shape.kind == "trench" else c.structure.id
    why = (f" its fill is length x height x base width / 2 ({shape.box} / 2 = "
           f"{qty(shape.m3)} m3), not the box;" if _boxed(c) else "")
    split = (f" (for example {c.parts} x {qty(largest / c.parts)} m3)"
             if c.parts > 1 else "")
    tail = ("each part still waiting for the dig part that feeds it; never add a dig the brief "
            "does not ask for" if noun == "haul"
            else "the hauls of its spoil still after the dig parts that remove it")
    return (f"{c.label} plans {qty(c.excess_m3)} m3 of {noun}s for its {target} {body}:{why} "
            f"cut the {whose} {noun}s to {target} in total{split}, {tail}.")


def _check_volume(req: Requirements, graph: TaskGraph) -> _Group | None:
    by_structure: dict[str, list[VolumeCoverage]] = {}
    for c in volume_conflicts(req, graph):        # shortfalls and double counts, shape order
        by_structure.setdefault(c.structure.id, []).append(c)
    if not by_structure:
        return None
    ids = sorted(by_structure)
    details = [f"{by_structure[sid][0].label}: "
               + " and ".join(_double_detail(c) if c.doubled else _volume_detail(c)
                              for c in by_structure[sid]) for sid in ids]
    sentences = [_double_sentence(c) if c.doubled else _volume_sentence(c)
                 for sid in ids for c in by_structure[sid]]
    return _Group(ids=[f"volume:{sid}" for sid in ids], details=details,
                  hint=" ".join(sentences))


# ----------------------------------------------------------------------------- supply
SUPPLY_SLACK = 1.25      # a haul may move this multiple of the m3 its sources make available
SUPPLY_SLACK_M3 = 1.0    # (dug regolith swells by about a quarter when loosened), plus this
_SUPPLY_GROUPS = 4       # structures a supply sentence names ...
_SUPPLY_IDS = 4          # ... and task ids per structure (or per list of hauls)
_SUPPLY_TAIL = ("; a haul part may wait for only the dig parts that remove what it moves, so "
                "split hauls to keep both chains short.")

DigGroups = tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class Supply:
    """What one haul of a graph waits for (:func:`supply_report`; module docstring, "Supply
    order").

    ``haul`` is the hauler task. ``target`` names where it places its material
    (:func:`~moonwright.pipeline.tags.haul_target`: 'S2 (plume berm)', 'zone Z3' for a zone no
    structure claims) and ``key`` is that structure's id or that zone, the group of the
    balance advisory; ``pile`` is True for a stockpile-like structure or an unclaimed zone,
    whose fill is never judged for balance (as the volume check never judges it, nor counts a
    haul placed there in another body's excess). ``digs`` are the sources among its
    transitive predecessors (graph order): every dig in m3, and an m2 strip when it can feed
    this haul (:func:`_feeds`); ``hauls`` the hauls into the same target among them.
    ``dug_by`` groups ``digs`` by structure and ``feeders`` the sources that neither come
    before it nor wait for it (they could still supply it), grouped the same way: for a haul
    into a placed body the other structures first, its own (a key trench) last, otherwise its
    own first, then requirements order, then sources no structure claims under their tag (or
    'untagged'). ``more`` are the digs in m3 among the feeders: the bulk sources a haul that
    moves more than its sources make available should wait for too (a second surface strip
    is never asked for: a berm "built from the hardstand's strip spoil" that holds more than
    the strip yields is the brief's own arithmetic).
    """

    haul: Task
    target: str
    key: str
    pile: bool
    digs: tuple[Task, ...]
    hauls: tuple[Task, ...]
    dug_by: DigGroups
    feeders: DigGroups
    more: DigGroups = ()

    @property
    def waits_for_no_dig(self) -> bool:
        """The first ``supply:<haul>`` case: no source among its predecessors."""
        return not self.digs

    @property
    def hauled_m3(self) -> float:
        """The m3 this haul and the hauls into its target before it move."""
        return (self.haul.quantity or 0.0) + sum(h.quantity or 0.0 for h in self.hauls)

    @property
    def dug_m3(self) -> float:
        """The m3 its sources make available (:func:`~moonwright.pipeline.rates.supply_m3`:
        a dig's m3, a strip's area x 0.2 m)."""
        return sum(supply_m3(d) for d in self.digs)

    @property
    def over(self) -> bool:
        """Whether it and the hauls into its target before it move more than
        :data:`SUPPLY_SLACK` (plus :data:`SUPPLY_SLACK_M3`) of what its sources make
        available; never for a pile, nor for a haul that waits for no source."""
        return (bool(self.digs) and not self.pile
                and self.hauled_m3 > self.dug_m3 * SUPPLY_SLACK + SUPPLY_SLACK_M3)

    @property
    def short(self) -> bool:
        """The second ``supply:<haul>`` case: :attr:`over` while a dig in m3 that neither
        comes before it nor waits for it could supply the rest (:attr:`more`: a 300 m3 berm
        haul that waits for its own 15 m3 key trench alone while the pad is dug beside it)."""
        return self.over and bool(self.more)

    @property
    def unsupplied(self) -> bool:
        """Whether the haul is a ``supply:<haul>`` conflict."""
        return self.waits_for_no_dig or self.short


def _owner(req: Requirements, task: Task) -> Structure | None:
    """The structure a source serves for a supply sentence: the first its tag names
    (:func:`tag_names`), else the first whose zone it is in."""
    if task.structure:
        for s in req.structures:
            if tag_names(task.structure, s):
                return s
    return next((s for s in req.structures if s.zone is not None and s.zone == task.zone), None)


def _type_words(structure: Structure) -> str:
    """'foundation_pit' -> 'foundation pit'."""
    return " ".join(structure.type.replace("_", " ").split()).lower()


def _placed_body(structure: Structure | None) -> bool:
    """Whether a haul's target is a placed body (a berm, a mound): its own excavation is a
    key trench, never the source of its fill."""
    if structure is None:
        return False
    shape = dims_volume(structure)
    return structure_kind(structure.type) == "berm" or (shape is not None and shape.phase == "haul")


def _dig_groups(req: Requirements, digs: Sequence[Task], owners: Mapping[str, Structure | None],
                own: Structure | None, placed: bool) -> DigGroups:
    """``digs`` grouped by the structure each serves ('S1 foundation pit'): ``own`` (the
    haul's target) first, or last for a placed body (its own dig is a key trench), the other
    structures in requirements order, then unclaimed sources under their tag (or 'untagged')
    in graph order; ids in graph order within a group, without repeats."""
    index = {s.id: i for i, s in enumerate(req.structures)}
    ranked: dict[str, tuple[tuple[int, int], list[str]]] = {}
    for n, d in enumerate(digs):
        s = owners.get(d.id)
        if s is None:
            label, rank = (d.structure or "untagged"), (3, n)
        else:
            label = f"{s.id} {_type_words(s)}"
            mine = own is not None and s.id == own.id
            rank = ((2 if placed else 0), 0) if mine else (1, index[s.id])
        entry = ranked.setdefault(label, (rank, []))
        if d.id not in entry[1]:
            entry[1].append(d.id)
    ordered = sorted(ranked.items(), key=lambda kv: kv[1][0])
    return tuple((label, tuple(ids)) for label, (_, ids) in ordered)


def _ids_text(ids: Sequence[str]) -> str:
    """'T3, T4', 'T3, T4, T5, T6 and 2 more' (at most :data:`_SUPPLY_IDS` named)."""
    shown = list(ids[:_SUPPLY_IDS])
    more = len(ids) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more else "")


def _groups_text(groups: DigGroups) -> str:
    """'S1 foundation pit: T3, T4; S3 borrow strip: T5, T6' (at most :data:`_SUPPLY_GROUPS`
    structures named, then 'digs of 2 more structures')."""
    parts = [f"{label}: {_ids_text(ids)}" for label, ids in groups[:_SUPPLY_GROUPS]]
    rest = len(groups) - _SUPPLY_GROUPS
    if rest > 0:
        parts.append(f"digs of {_plural(rest, 'more structure')}")
    return "; ".join(parts)


def _sources_text(groups: DigGroups, digs: Sequence[Task]) -> str:
    """:func:`_groups_text`, with '; an m2 strip counts 0.2 m' when a strip is among them."""
    strips = any(is_strip(d) for d in digs)
    return _groups_text(groups) + (f"; an m2 strip counts {qty(STRIP_DEPTH_M)} m" if strips else "")


def _feeds(source: Task, target: Structure | None, pile: bool, no_m3: bool,
           owners: Mapping[str, Structure | None]) -> bool:
    """Whether ``source`` can supply a haul into ``target``: a dig in m3 always; an m2 strip
    when it serves the target itself (a haul lane's grading spoil moved along the lane), when
    the haul fills a pile (road grading spoil to a stockpile, strip spoil to a dump zone) or
    when the graph digs nothing in m3 (a berm "built from the hardstand's strip spoil"). A
    berm built from pit spoil is not supplied by the haul lane's grading."""
    if is_dig(source):
        return True
    if not is_strip(source):
        return False
    if no_m3 or pile:
        return True
    owner = owners.get(source.id)
    return target is not None and owner is not None and owner.id == target.id


def supply_report(req: Requirements, graph: TaskGraph) -> list[Supply]:
    """One :class:`Supply` per haul of ``graph`` (:func:`~moonwright.pipeline.rates.is_haul`,
    graph order), or [] when the graph has no source at all (no
    :func:`~moonwright.pipeline.rates.is_dig` nor :func:`~moonwright.pipeline.rates.is_strip`
    task): its hauls carry material from outside the plan, which is not judged. Predecessors
    are transitive ``after`` edges through any task
    (:func:`~moonwright.pipeline.rates.ancestors`; an unknown id is skipped, a cycle
    tolerated: both are reported by their own checks)."""
    pool = list({t.id: t for t in graph.tasks if is_dig(t) or is_strip(t)}.values())
    if not pool:
        return []
    no_m3 = not any(is_dig(t) for t in pool)
    before = ancestors(graph)
    owners = {t.id: _owner(req, t) for t in pool}
    hauls = list({t.id: t for t in graph.tasks if is_haul(t)}.values())
    targets = {h.id: haul_target(h, req.structures) for h in hauls}
    keys = {h.id: (targets[h.id].id if targets[h.id] is not None else h.zone) for h in hauls}
    out: list[Supply] = []
    for h in hauls:
        target = targets[h.id]
        pile = target is None or stockpile_like(target)
        placed = _placed_body(target)
        mine = before.get(h.id, set()) - {h.id}
        sources = [t for t in pool if t.id != h.id and _feeds(t, target, pile, no_m3, owners)]
        digs = tuple(t for t in sources if t.id in mine)
        feeders = [t for t in sources if t.id not in mine and h.id not in before.get(t.id, set())]
        out.append(Supply(
            haul=h,
            target=(f"{target.id} ({_type_words(target)})" if target is not None
                    else f"zone {h.zone}"),
            key=keys[h.id],
            pile=pile,
            digs=digs,
            hauls=tuple(t for t in hauls if t.id in mine and keys[t.id] == keys[h.id]),
            dug_by=_dig_groups(req, digs, owners, target, placed),
            feeders=_dig_groups(req, feeders, owners, target, placed),
            more=_dig_groups(req, [t for t in feeders if is_dig(t)], owners, target, placed),
        ))
    return out


def supply_advisories(req: Requirements, graph: TaskGraph) -> list[str]:
    """``supply:<structure>: ...`` for every structure whose hauls together move more than the
    sources before them make available (beyond :data:`SUPPLY_SLACK` plus
    :data:`SUPPLY_SLACK_M3`), summed per target rather than along precedence chains, so the
    same parts read the same chained or in parallel: ``supply:S2: its hauls (T5 150 m3, T6 150
    m3) move 300 m3, more than the 125.7 m3 the digs before them remove (S1 landing pad: T3,
    T4)``. Often the brief's own arithmetic (a berm "built from pad spoil" whose prism holds
    more than the pad yields: landing_pad's 300 m3 after 125.7 m3 of bowl), so a note for the
    trace and the repair prompt, whose header says to keep the body's m3 and only wait for
    more of the digs that feed it; never a conflict. Not for a pile (a stockpile-like
    structure or an unclaimed zone: whatever is hauled there), for a structure one of
    whose hauls is a ``supply:<haul>`` conflict (the verdict says it), nor for a body whose
    hauls are a double count (``volume:<structure>``, phase 3i: they move more than the
    digs remove because they count the body twice, live the berm's box, and 2 of 3 repairs
    answered this note by inventing a 114 m3 borrow pit instead of halving the hauls). Meant
    for a whole plan, like the conflict: the task-graph stage does not call it on a replan's
    remaining graph."""
    report = supply_report(req, graph)
    if not report:
        return []
    groups: dict[str, list[Supply]] = {}
    for s in report:
        groups.setdefault(s.key, []).append(s)
    doubled = {c.structure.id for c in volume_double_counts(req, graph) if c.shape.phase == "haul"}
    order = {t.id: i for i, t in reversed(list(enumerate(graph.tasks)))}
    out: list[str] = []
    for key, group in groups.items():
        if group[0].pile or any(s.unsupplied for s in group) or key in doubled:
            continue
        digs = sorted({d.id: d for s in group for d in s.digs}.values(), key=lambda d: order[d.id])
        owners = {d.id: _owner(req, d) for d in digs}
        hauled = sum(s.haul.quantity or 0.0 for s in group)
        dug = sum(supply_m3(d) for d in digs)
        if hauled <= dug * SUPPLY_SLACK + SUPPLY_SLACK_M3:
            continue
        target = haul_target(group[0].haul, req.structures)
        listing = _ids_text([f"{s.haul.id} {qty(s.haul.quantity or 0.0)} m3" for s in group])
        sources = _sources_text(_dig_groups(req, digs, owners, target, _placed_body(target)), digs)
        out.append(f"supply:{key}: its hauls ({listing}) move {qty(hauled)} m3, more than the "
                   f"{qty(dug)} m3 the digs before them remove ({sources})")
    return out


def _check_supply(req: Requirements, graph: TaskGraph) -> _Group | None:
    gaps = [s for s in supply_report(req, graph) if s.unsupplied]   # graph order
    if not gaps:
        return None
    details = []
    shared: dict[tuple[bool, DigGroups], list[str]] = {}   # hauls the same digs could feed
    for s in gaps:
        h = s.haul
        where = f" to {s.target}" if s.target.startswith("zone ") else f" for {s.target}"
        if s.waits_for_no_dig:
            details.append(f"{h.id} hauls {qty(h.quantity or 0.0)} m3{where} but waits for no "
                           "excavation, so it could start before any material is dug")
        else:
            chain = (f", {qty(s.hauled_m3)} m3 with the hauls before it "
                     f"({_ids_text([t.id for t in s.hauls])})," if s.hauls else "")
            details.append(f"{h.id} hauls {qty(h.quantity or 0.0)} m3{where}{chain} but the digs "
                           f"before it remove only {qty(s.dug_m3)} m3 "
                           f"({_sources_text(s.dug_by, s.digs)}), so it could start before all "
                           "of its material is dug")
        shared.setdefault((s.waits_for_no_dig, s.feeders if s.waits_for_no_dig else s.more),
                          []).append(h.id)
    clauses = []
    for (none, feeders), hauls in shared.items():
        it = "it" if len(hauls) == 1 else "them"
        if none:
            where = (f" ({_groups_text(feeders)})" if feeders
                     else f" (every dig waits for {it} now: put the digs first)")
            clauses.append(f"make {_join_and(hauls)} wait for the digs that supply {it}{where}")
        else:
            clauses.append(f"make {_join_and(hauls)} wait for more of the digs that supply {it} "
                           f"({_groups_text(feeders)})")
    return _Group(
        ids=[f"supply:{s.haul.id}" for s in gaps],
        details=details,
        hint="A haul cannot start before its material is dug: " + "; ".join(clauses) + _SUPPLY_TAIL,
    )


# ----------------------------------------------------------------------------- quantities
_PHYSICAL_PHASES = frozenset({"excavate", "grade", "haul", "compact", "sinter"})


def needs_quantity(task: Task) -> bool:
    """Whether the rate model must price ``task``: a rated robot type other than the surveyor
    doing physical work by :func:`task_phases` (an inspection pass by an excavator is not)."""
    return (task.robot_type in RATED_TYPES and task.robot_type != "surveyor"
            and bool(task_phases(task) & _PHYSICAL_PHASES))


def _check_quantity(graph: TaskGraph, previous: TaskGraph | None) -> _Group | None:
    if not any(scoped(t) for t in graph.tasks) and not (
            previous is not None and any(scoped(t) for t in previous.tasks)):
        return None   # a graph the rate model never had standing over
    items: dict[str, str] = {}
    for t in graph.tasks:
        if not needs_quantity(t):
            continue
        if t.quantity is None:
            why = "has no quantity"
        elif t.unit is None:
            why = "has a quantity but no unit"
        elif t.quantity <= 0:
            why = f"has a quantity of 0 {t.unit}"
        elif rate_for(t.robot_type, t.unit) is None:
            why = f"is counted in {t.unit}, which the rate table does not price for a {t.robot_type}"
        else:
            continue
        items[f"quantity:{t.id}"] = f"{t.id} ({t.robot_type}: {t.name}) {why}"
    if not items:
        return None
    ids = sorted(items)
    types = sorted({graph.task(i.removeprefix("quantity:")).robot_type for i in ids})
    priced = _join_and([f"{typ} in {' or '.join(rated_units(typ))}" for typ in types])
    return _Group(
        ids=ids,
        details=[items[i] for i in ids],
        hint=f"Physical work is sized by the rate table, so {_join_and(ids)} cannot keep "
        f"hand-written hours and Wh: give each such task a positive quantity in a unit the "
        f"table prices ({priced}), with its basis; only an inspection pass or a wait may be a "
        "pure time task without one.",
    )


# ----------------------------------------------------------------------------- coverage
_PHASE_OF_TYPE = {"surveyor": "survey", "hauler": "haul", "compactor": "compact",
                  "printer": "sinter"}
_PHASE_OF_EXCAVATOR_UNIT = {"m3": "excavate", "m": "excavate", "m2": "grade"}
_PHASE_STEMS: dict[str, tuple[str, ...]] = {
    "survey": ("survey", "stak", "inspect", "scan", "map"),
    "excavate": ("excavat", "dig", "trench", "cut", "bowl", "pit", "borrow"),
    "grade": ("grad", "strip", "level", "smooth", "blade"),
    "haul": ("haul", "transport", "carr", "mov", "deliver", "stag", "dump"),
    "compact": ("compact", "roll", "tamp", "consolidat"),
    "sinter": ("sinter", "print", "melt", "pav", "fus", "bak"),
}
# structure kind -> required phases, each entry a tuple of alternatives (any one satisfies);
# a phase is only required while the fleet has a robot of its canonical type (the brief's
# "sinter when a printer exists" rule, applied to every phase so a fleet without, say, a
# surveyor is never asked for a task it cannot staff)
_REQUIRED_PHASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "landing_pad": (("survey",), ("excavate", "grade"), ("compact",), ("sinter",)),
    "berm": (("excavate", "haul"), ("compact",)),
    "road": (("grade",), ("compact",)),
    "foundation": (("excavate",), ("compact",)),
}
_CANONICAL_TYPE = {"survey": "surveyor", "excavate": "excavator", "grade": "excavator",
                   "haul": "hauler", "compact": "compactor", "sinter": "printer"}
# the word stems :func:`brief_evidence` accepts as the brief asking for a phase: the phase's
# verbs plus the nouns a brief uses instead of them ("built from pad spoil" is a haul, "signed
# off" an inspection). Never a noun that names a structure (bowl, pit, borrow): "built from a
# borrow strip" is where the fill comes from, not an ask to excavate the berm.
_EVIDENCE_STEMS: dict[str, tuple[str, ...]] = {
    "survey": ("survey", "stak", "inspect", "scan", "map", "sign", "certif", "check"),
    "excavate": ("excavat", "dig", "trench", "cut"),
    "grade": ("grad", "strip", "level", "smooth", "blade"),
    "haul": ("haul", "transport", "carr", "mov", "deliver", "stag", "dump", "spoil", "fill",
             "plac", "windrow"),
    "compact": ("compact", "roll", "tamp", "consolidat"),
    "sinter": ("sinter", "print", "melt", "pav", "fus", "bak"),
}
# the robots themselves are not asks: "no second excavator is available" in a line about the
# berm is not the brief asking to excavate the berm
_ROBOT_WORDS = frozenset(w for typ in ("excavator", "hauler", "compactor", "printer", "surveyor")
                         for w in (typ, typ + "s"))
_PHASE_VERB = {"survey": "surveys or inspects", "excavate": "excavates", "grade": "grades",
               "haul": "hauls to", "compact": "compacts", "sinter": "sinters"}
_PHASE_RULES_TEXT = (
    "landing pad: survey, excavate or grade, compact and sinter; berm: excavate or haul, then "
    "compact; road: grade and compact; foundation: excavate and compact; anything else at "
    "least one task; a phase is only required while the fleet has its robot type"
)
_OPERATIONS_RULE_TEXT = "every operation a structure lists needs a task tagged with it"
_QUOTE_MAX = 80                                       # longest brief quote in a hint
_CLAUSE_SPLIT = re.compile(r"\.(?=\s|$)|[;:,\n()]")  # clause boundaries; "0.5 m" stays whole
_WORDS = re.compile(r"[a-z0-9]+")


def structure_kind(structure_type: str) -> str | None:
    """'landing_pad' / 'berm' / 'road' / 'foundation' by keyword, None for any other type."""
    typ = structure_type.lower()
    for word, kind in (("berm", "berm"), ("road", "road"), ("foundation", "foundation"),
                       ("pad", "landing_pad")):
        if word in typ:
            return kind
    return None


def task_phases(task: Task) -> set[str]:
    """Phases a task evidently performs: robot type, unit and name keywords combined."""
    phases: set[str] = set()
    if task.robot_type in _PHASE_OF_TYPE:
        phases.add(_PHASE_OF_TYPE[task.robot_type])
    if task.robot_type == "excavator" and task.unit in _PHASE_OF_EXCAVATOR_UNIT:
        phases.add(_PHASE_OF_EXCAVATOR_UNIT[task.unit])
    words = re.findall(r"[a-z]+", task.name.lower())
    for phase, stems in _PHASE_STEMS.items():
        if any(w.startswith(stems) for w in words):
            phases.add(phase)
    if task.robot_type == "excavator" and not phases:
        phases.update(("excavate", "grade"))   # earthmoving of an unnamed kind
    return phases


def covered_phases(tasks: list[Task]) -> set[str]:
    """Union of :func:`task_phases` over ``tasks``."""
    covered: set[str] = set()
    for t in tasks:
        covered |= task_phases(t)
    return covered


def performs(task: Task, operation: str) -> bool:
    """Whether ``task`` performs ``operation`` in the strict sense a listed operation needs: a
    robot of the operation's canonical type doing that phase by :func:`task_phases` (a hauler
    hauls, a printer sinters, an excavator excavates in m3 or m and grades in m2). The kind
    heuristic stays lenient (name keywords count whatever the robot type), but the brief's own
    ask is not met by a hauler whose name mentions the bowl it empties."""
    return task.robot_type == _CANONICAL_TYPE[operation] and operation in task_phases(task)


def _required(req: Requirements, structure: Structure) -> tuple[list[tuple[str, ...]], list[str]]:
    """``(kind groups, listed operations)`` the fleet can staff: the groups of the structure's
    kind (:data:`_REQUIRED_PHASES`) minus any that a listed operation already implies
    (``excavate or haul`` when the brief asks for ``haul``), and the operations it lists, in
    order, without repeats and without the ones the brief does not support
    (:func:`unsupported_operations`). A group is staffed while the fleet has a robot of one
    of its phases' canonical types."""
    fleet = {r.type for r in req.robots}

    def staffed(group: tuple[str, ...]) -> bool:
        return any(_CANONICAL_TYPE[phase] in fleet for phase in group)

    kind = structure_kind(structure.type)
    soft = {op for op, _ in unsupported_operations(req, structure)}
    listed = list(dict.fromkeys(op for op in structure.operations
                                if staffed((op,)) and op not in soft))
    groups = [g for g in (_REQUIRED_PHASES[kind] if kind else ())
              if staffed(g) and not (len(g) > 1 and set(g) & set(listed))]
    return groups, listed


def unsupported_operations(req: Requirements, structure: Structure) -> list[tuple[str, str]]:
    """``(operation, the alternative the brief supports)`` for every operation ``structure``
    lists that the coverage check will not require: the brief is known
    (``req.source_brief``), the operation is one alternative of a multi-phase group of the
    structure's kind ("excavate or haul" of a berm, "excavate or grade" of a pad), the brief
    gives no evidence for it on this structure (:func:`brief_evidence`) and the other
    alternative is listed *with* evidence. A berm the brief says is "built from a borrow
    strip ... placed and compacted" lists ``haul`` for a reason and ``excavate`` for none;
    requiring the latter made a live repair invent a 35 m3 "foundation" dig. Nothing is
    softened without a brief to read, on a structure of unknown kind (its listed operations
    are the only contract it has), or for an operation the brief does ask for ("on a key
    trench cut into firm material" keeps a berm's ``excavate``)."""
    brief = req.source_brief
    kind = structure_kind(structure.type)
    if not brief or kind is None:
        return []
    listed = list(dict.fromkeys(structure.operations))
    out: list[tuple[str, str]] = []
    for group in _REQUIRED_PHASES[kind]:
        members = [op for op in listed if op in group]
        if len(members) < 2:
            continue
        supported = [op for op in members
                     if brief_evidence(brief, op, structure, req.structures) is not None]
        if supported:
            out += [(op, supported[0]) for op in members if op not in supported]
    return out


def required_phases(req: Requirements, structure: Structure) -> list[tuple[str, ...]]:
    """Phase groups ``structure`` must be served with, each a tuple of alternatives (any one
    satisfies): its kind's groups first, then one group per listed operation that is not
    already a kind group of its own (:func:`_required`)."""
    groups, listed = _required(req, structure)
    out = list(groups)
    for op in listed:
        if (op,) not in out:
            out.append((op,))
    return out


def missing_phases(req: Requirements, structure: Structure, tasks: list[Task],
                   hauls: Sequence[Task] = ()) -> list[str]:
    """Required phases ``tasks`` do not cover: the kind's groups as 'excavate or haul' style
    strings, judged by the lenient union of :func:`task_phases`, then every listed operation
    no task :func:`performs` (once each); ``['any work']`` for a structure of unknown kind
    that lists no staffable operation and has no task at all. ``hauls`` are the hauler tasks
    placed in the structure's zone whatever their tag (:func:`moonwright.pipeline.tags.hauls_into`):
    they count for its haul, and for nothing else."""
    groups, listed = _required(req, structure)
    if not groups and not listed and structure_kind(structure.type) is None:
        return [] if tasks else ["any work"]
    covered = covered_phases(tasks)
    if any(performs(t, "haul") for t in hauls):
        covered.add("haul")
    missing = [" or ".join(group) for group in groups if not covered & set(group)]
    for op in listed:
        doers = [*tasks, *hauls] if op == "haul" else tasks
        if op not in missing and not any(performs(t, op) for t in doers):
            missing.append(op)
    return missing


def missing_operations(req: Requirements, structure: Structure, tasks: list[Task],
                       hauls: Sequence[Task] = ()) -> list[str]:
    """Operations ``structure`` lists (the brief's own asks) that no task :func:`performs`,
    in the order listed; an operation the fleet cannot staff is skipped here and reported by
    :func:`unstaffed_operations` instead, one the brief does not support by
    :func:`unsupported_operations`. ``hauls`` as in :func:`missing_phases`."""
    out = []
    for op in _required(req, structure)[1]:
        doers = [*tasks, *hauls] if op == "haul" else tasks
        if not any(performs(t, op) for t in doers):
            out.append(op)
    return out


def unstaffed_operations(req: Requirements, structure: Structure) -> list[tuple[str, str]]:
    """``(operation, robot type)`` for every operation ``structure`` lists that no robot of
    the fleet can perform: the coverage check never asks for it (a site warning does)."""
    fleet = {r.type for r in req.robots}
    return [(op, _CANONICAL_TYPE[op]) for op in structure.operations
            if _CANONICAL_TYPE[op] not in fleet]


def _names(low: str, words: Collection[str], structure: Structure) -> tuple[int, int] | None:
    """How well a brief line names ``structure`` (:func:`moonwright.pipeline.tags.name_score`,
    shared with the volume check's reading of a structure's own entry)."""
    return name_score(low, words, structure)


def brief_evidence(brief: str | None, phase: str, structure: Structure,
                   others: Sequence[Structure] = ()) -> str | None:
    """A clause of ``brief`` that asks for ``phase`` on ``structure``, for a hint to quote.

    Among the lines holding a word with an evidence stem of the phase
    (:data:`_EVIDENCE_STEMS`: its verbs and the nouns briefs use for the ask, never a noun
    that names a structure, never a robot), the line that names the structure best wins
    (:func:`_names`: each word of its type once, the head word twice, a tie to the line that
    names it earliest, so the structure's own entry, "2. Blast berm: ...", beats a site
    paragraph that mentions it in passing); a line without any word of the type is never
    used, and with ``others`` (the brief's other structures) neither is a line that names one
    of them by a strictly higher score ("Borrow strip: 100 m by 6 m excavation ... that
    supplies the berm fill" is the strip's entry, not an ask to excavate the berm; "the
    surveyor inspects the road before the foundation is signed off" names both and counts
    for both). The quote is that line's clause
    around the word, at most :data:`_QUOTE_MAX` characters. None when the brief is unknown or
    no line qualifies: the hint then says generically that the brief asks for the operation.
    """
    if not brief:
        return None
    stems = _EVIDENCE_STEMS[phase]

    def asks(words: Collection[str]) -> bool:
        return any(w.startswith(stems) and w not in _ROBOT_WORDS for w in words)

    rivals = [s for s in others if s.id != structure.id]
    best: tuple[tuple[int, int], str] | None = None
    for line in brief_lines(brief):   # a hard-wrapped entry reads whole
        low = line.lower()
        words = set(_WORDS.findall(low))
        key = _names(low, words, structure)
        if key is None or not asks(words):
            continue
        if any((rival := _names(low, words, s)) is not None and rival[0] > key[0] for s in rivals):
            continue   # another structure's entry: its verbs are not this structure's ask
        if best is None or key > best[0]:
            best = (key, line)
    if best is None:
        return None
    for clause in _CLAUSE_SPLIT.split(best[1]):
        if asks(_WORDS.findall(clause.lower())):
            quote = re.sub(r"^[\s\-*#>]+|[\s*]+$", "", " ".join(clause.split()))
            if len(quote) > _QUOTE_MAX:
                quote = quote[:_QUOTE_MAX - 3].rstrip() + "..."
            return quote
    return None


def _coverage_phrase(label: str, structure: Structure, missing: list[str], asked: list[str],
                     brief: str | None, others: Sequence[Structure] = ()) -> str:
    """'S2 (blast_berm) has no compact task and lists sinter (the brief asks for "with a
    sintered crest to resist plume erosion") but no task sinters it; add a printer task tagged
    S2': the kind's missing phases first, then every listed operation that went unserved,
    quoting the brief where :func:`brief_evidence` finds the ask (``others`` are the brief's
    other structures, whose own entries are never quoted for this one)."""
    clauses: list[str] = []
    rest = [m for m in missing if m not in asked]
    if rest:
        clauses.append(f"has no {_join_and(rest)} task")
    if asked:
        wants: list[str] = []
        for op in asked:
            quote = brief_evidence(brief, op, structure, others)
            why = f'the brief asks for "{quote}"' if quote else "an operation the brief asks for"
            wants.append(f"{op} ({why}) but no task {_PHASE_VERB[op]} it")
        types = list(dict.fromkeys(_CANONICAL_TYPE[op] for op in asked))
        if len(types) == 1:
            fix = f"{'an' if types[0][0] in 'aeiou' else 'a'} {types[0]} task"
        else:
            fix = f"{_join_and(types)} tasks"
        clauses.append(f"lists {_join_and(wants)}; add {fix} tagged {structure.id}")
    return f"{label} " + " and ".join(clauses)


def _check_coverage(req: Requirements, graph: TaskGraph,
                    previous: TaskGraph | None) -> _Group | None:
    items: dict[str, str] = {}
    phrases: dict[str, str] = {}
    listed = False
    for s in req.structures:
        tasks = structure_tasks(s, graph, req.structures)
        served = {t.id for t in tasks}
        hauls = [t for t in hauls_into(s, graph) if t.id not in served]
        missing = missing_phases(req, s, tasks, hauls)
        if not missing:
            continue
        label = f"{s.id} ({s.type})"
        before = ([t.id for t in structure_tasks(s, previous, req.structures)]
                  if previous is not None else [])
        if not tasks and before:
            detail = (f"{label} had {_join_and(before)} before the repair and has no task now")
            phrase = f"{label} lost every task it had ({', '.join(before)})"
        elif missing == ["any work"]:
            detail = phrase = f"{label} has no task"
        else:
            noun = "task" if len(missing) == 1 else "tasks"
            detail = f"{label} lacks {_join_and(missing)} {noun}"
            asked = missing_operations(req, s, tasks, hauls)
            if asked:
                listed = True
                detail += f" (its operations list {_join_and(asked)})"
            phrase = _coverage_phrase(label, s, missing, asked, req.source_brief, req.structures)
        items[f"scope:{s.id}"] = detail
        phrases[f"scope:{s.id}"] = phrase
    if not items:
        return None
    ids = sorted(items)
    rules = f"every structure keeps its phases ({_PHASE_RULES_TEXT})"
    if listed:
        rules += f" and {_OPERATIONS_RULE_TEXT}"
    return _Group(
        ids=ids,
        details=[items[i] for i in ids],
        hint=f"Structure coverage is incomplete: {_join_and([phrases[i] for i in ids])}; "
        f"{rules}, so add the missing work back as tasks tagged with the structure id, with "
        "quantity and unit.",
    )


# ----------------------------------------------------------------------------- helpers
def _served_types(
    req: Requirements, graph: TaskGraph
) -> list[tuple[str, list[int], list[Robot]]]:
    """``(type, task positions, robots)`` for every robot type used by the graph that has
    robots; the positions index ``graph.tasks`` and :func:`transit_allowances` alike."""
    out: list[tuple[str, list[int], list[Robot]]] = []
    for typ in sorted({t.robot_type for t in graph.tasks}):
        robots = req.robots_of_type(typ)
        if robots:
            out.append((typ, [i for i, t in enumerate(graph.tasks) if t.robot_type == typ],
                        robots))
    return out

