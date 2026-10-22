"""F3: deterministic validator for a TaskGraph against Requirements.

Contract (see conflict id formats in schemas.py):

    validate_graph(req, graph, previous=None, *, coverage=False) -> Failure | None

Checks, in this order, collecting *all* conflicts of the kinds below before returning:
  duplicate ids (tasks, robots, zones) .................... duplicate:<id>
  `after` references to unknown tasks ..................... reference:<task>.after=<id>
  self/cyclic precedence (report the cycle path) .......... cycle:<a>-><b>->...-><a>
  robot_type with no robot in requirements ................ capability:<task_id>
  physical task without a priced quantity (see below) ..... quantity:<task_id>
  structure coverage (coverage=True only, see below) ...... scope:<structure_id>
  task energy > max battery of any robot of that type ..... energy_task:<task_id>
  critical path (longest duration chain) > horizon_h ...... horizon_path:<a>-><b>->...
  per robot type: ceil(total duration / #robots) > horizon  horizon   (hint names the type)
  per robot type: total energy > sum of batteries ......... energy:<type>  (hint explains)
Return None when everything passes. `hint` is one human sentence per conflict group, joined.

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
that resolves to none). What a structure needs depends on its ``type`` (matched by keyword
after lower-casing: "berm", "road", "foundation", "pad"; anything else is "other"):

  landing pad (type contains "pad") ... survey, excavate, compact, sinter
  berm (contains "berm") .............. excavate or haul, and compact
  road (contains "road") .............. grade and compact
  foundation (contains "foundation") .. excavate and compact
  other ............................... at least one task

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

    check_scope(graph, required, baseline=None) -> Failure | None

Scope guard of a replan (or any run that must keep given tasks): ``missing:<task_id>`` for a
required id the graph dropped and, with ``baseline`` (the remaining graph the model was given),
``changed:<task_id>`` for a required task whose definition no longer matches it.

Implementation notes
--------------------
* The result is deterministic: conflict groups appear in the order of the checks above and
  conflict ids are sorted alphabetically within a group. ``Failure.details[i]`` is a short
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

import re
from collections import Counter, deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from moonwright.pipeline.rates import RATED_TYPES, rate_for, rated_units, scoped
from moonwright.schemas import (
    Failure,
    Requirements,
    Robot,
    Structure,
    Task,
    TaskGraph,
    fmt_wh,
    to_mwh,
)


def validate_graph(req: Requirements, graph: TaskGraph, previous: TaskGraph | None = None,
                   *, coverage: bool = False) -> Failure | None:
    """Run every check and return a ``validation`` Failure, or None when the graph is clean.

    ``coverage=True`` adds the structure coverage check (``scope:<structure_id>``). It is an
    opt-in because it judges a *whole* plan: the remaining graph of a replan legitimately
    lacks the phases that are already built, so a replan must be checked with
    ``coverage=False`` (its work is guarded by :func:`check_scope` instead) and a fresh plan
    and its repairs with ``coverage=True``. ``previous`` is the graph a repair answered (if
    any): a structure it served that ``graph`` no longer serves is reported with the tasks
    that went missing.
    """
    prec = _Precedence.from_graph(graph)
    cycles = _check_cycles(prec)
    groups = [
        _check_duplicates(req, graph),
        _check_references(graph),
        cycles,
        _check_capability(req, graph),
        _check_quantity(graph, previous),
        _check_coverage(req, graph, previous) if coverage else None,
        _check_task_energy(req, graph),
        None if cycles else _check_critical_path(prec, req.horizon_h),
        _check_type_horizon(req, graph),
        _check_type_energy(req, graph),
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


def _check_task_energy(req: Requirements, graph: TaskGraph) -> _Group | None:
    items: dict[str, str] = {}
    phrases: dict[str, str] = {}
    for t in graph.tasks:
        robots = req.robots_of_type(t.robot_type)
        if not robots:
            continue
        cap = max(to_mwh(r.battery_wh) for r in robots)
        need = to_mwh(t.energy_wh)
        if need > cap:
            cid = f"energy_task:{t.id}"
            items[cid] = (
                f"{t.id} needs {fmt_wh(need)} Wh but the largest {t.robot_type} "
                f"battery is {fmt_wh(cap)} Wh"
            )
            phrases[cid] = (
                f"{t.id} ({fmt_wh(need)} Wh, largest {t.robot_type} battery "
                f"{fmt_wh(cap)} Wh)"
            )
    if not items:
        return None
    ids = sorted(items)
    return _Group(
        ids=ids,
        details=[items[i] for i in ids],
        hint=f"No single robot can power {_join_and([phrases[i] for i in ids])}; split such "
        "tasks into smaller quantities or move the work to another robot type.",
    )


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
        "more work runs in parallel.",
    )


def _check_type_horizon(req: Requirements, graph: TaskGraph) -> _Group | None:
    phrases: list[str] = []
    details: list[str] = []
    for typ, tasks, robots in _served_types(req, graph):
        total = sum(t.duration_h for t in tasks)
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


def _check_type_energy(req: Requirements, graph: TaskGraph) -> _Group | None:
    ids: list[str] = []
    details: list[str] = []
    phrases: list[str] = []
    for typ, tasks, robots in _served_types(req, graph):  # sorted by type == sorted ids
        need = sum(to_mwh(t.energy_wh) for t in tasks)
        have = sum(to_mwh(r.battery_wh) for r in robots)
        if need > have:
            fleet = _plural(len(robots), "robot")
            carry = "carries" if len(robots) == 1 else "carry"
            ids.append(f"energy:{typ}")
            details.append(
                f"{typ} tasks need {fmt_wh(need)} Wh; its {fleet} {carry} {fmt_wh(have)} Wh"
            )
            phrases.append(
                f"{typ} tasks need {fmt_wh(need)} Wh but its {fleet} {carry} only "
                f"{fmt_wh(have)} Wh combined"
            )
    if not ids:
        return None
    return _Group(
        ids=ids,
        details=details,
        hint=f"Battery budgets are exceeded per robot type: {_join_and(phrases)}; reduce the "
        "quantities if the brief allows it or move work to other robot types.",
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
    "landing_pad": (("survey",), ("excavate",), ("compact",), ("sinter",)),
    "berm": (("excavate", "haul"), ("compact",)),
    "road": (("grade",), ("compact",)),
    "foundation": (("excavate",), ("compact",)),
}
_CANONICAL_TYPE = {"survey": "surveyor", "excavate": "excavator", "grade": "excavator",
                   "haul": "hauler", "compact": "compactor", "sinter": "printer"}
_PHASE_RULES_TEXT = (
    "landing pad: survey, excavate, compact and sinter; berm: excavate or haul, then compact; "
    "road: grade and compact; foundation: excavate and compact; anything else at least one "
    "task; a phase is only required while the fleet has its robot type"
)


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


def tag_names(tag: str, structure: Structure) -> bool:
    """Whether a task's ``structure`` tag names ``structure``: its id (any case), its type, or
    a label that contains the id as a word ("Landing pad S1")."""
    low = tag.strip().lower()
    if low == structure.id.lower() or low == structure.type.strip().lower():
        return True
    return structure.id.lower() in re.findall(r"[a-z0-9_.-]+", low)


def structure_tasks(structure: Structure, graph: TaskGraph,
                    structures: Sequence[Structure] | None = None) -> list[Task]:
    """Tasks that serve ``structure``: tagged with something that names it
    (:func:`tag_names`), or in its zone while tagged with nothing that names any structure of
    ``structures`` (default: ``structure`` alone), the tag being free text the model may fill
    with a type, a label or a different case."""
    known = list(structures) if structures is not None else [structure]

    def serves(t: Task) -> bool:
        if t.structure is not None and tag_names(t.structure, structure):
            return True
        if structure.zone is None or t.zone != structure.zone:
            return False
        return t.structure is None or not any(tag_names(t.structure, s) for s in known)

    return [t for t in graph.tasks if serves(t)]


def missing_phases(req: Requirements, structure: Structure, tasks: list[Task]) -> list[str]:
    """Required phases ``tasks`` do not cover, as 'excavate or haul' style strings; for a
    structure of unknown kind, ``['any work']`` when it has no task at all."""
    kind = structure_kind(structure.type)
    if kind is None:
        return [] if tasks else ["any work"]
    fleet = {r.type for r in req.robots}
    required = [group for group in _REQUIRED_PHASES[kind]
                if any(_CANONICAL_TYPE[phase] in fleet for phase in group)]
    covered: set[str] = set()
    for t in tasks:
        covered |= task_phases(t)
    return [" or ".join(group) for group in required if not covered & set(group)]


def _check_coverage(req: Requirements, graph: TaskGraph,
                    previous: TaskGraph | None) -> _Group | None:
    items: dict[str, str] = {}
    phrases: dict[str, str] = {}
    for s in req.structures:
        tasks = structure_tasks(s, graph, req.structures)
        missing = missing_phases(req, s, tasks)
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
            phrase = f"{label} has no {_join_and(missing)} task"
        items[f"scope:{s.id}"] = detail
        phrases[f"scope:{s.id}"] = phrase
    if not items:
        return None
    ids = sorted(items)
    return _Group(
        ids=ids,
        details=[items[i] for i in ids],
        hint=f"Structure coverage is incomplete: {_join_and([phrases[i] for i in ids])}; every "
        f"structure keeps its phases ({_PHASE_RULES_TEXT}), so add the missing work back as "
        "tasks tagged with the structure id, with quantity and unit.",
    )


# ----------------------------------------------------------------------------- helpers
def _served_types(
    req: Requirements, graph: TaskGraph
) -> list[tuple[str, list[Task], list[Robot]]]:
    """``(type, tasks, robots)`` for every robot type used by the graph that has robots."""
    out: list[tuple[str, list[Task], list[Robot]]] = []
    for typ in sorted({t.robot_type for t in graph.tasks}):
        robots = req.robots_of_type(typ)
        if robots:
            out.append((typ, [t for t in graph.tasks if t.robot_type == typ], robots))
    return out


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _join_and(parts: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]
