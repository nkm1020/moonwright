"""F3: deterministic validator for a TaskGraph against Requirements.

Contract (see conflict id formats in schemas.py):

    validate_graph(req, graph) -> Failure | None

Checks, in this order, collecting *all* conflicts of the kinds below before returning:
  duplicate ids (tasks, robots, zones) .................... duplicate:<id>
  `after` references to unknown tasks ..................... reference:<task>.after=<id>
  self/cyclic precedence (report the cycle path) .......... cycle:<a>-><b>->...-><a>
  robot_type with no robot in requirements ................ capability:<task_id>
  task energy > max battery of any robot of that type ..... energy_task:<task_id>
  critical path (longest duration chain) > horizon_h ...... horizon_path:<a>-><b>->...
  per robot type: ceil(total duration / #robots) > horizon  horizon   (hint names the type)
  per robot type: total energy > sum of batteries ......... energy:<type>  (hint explains)
Return None when everything passes. `hint` is one human sentence per conflict group, joined.

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

from collections import Counter, deque
from collections.abc import Collection
from dataclasses import dataclass

from moonwright.schemas import Failure, Requirements, Robot, Task, TaskGraph, fmt_wh, to_mwh


def validate_graph(req: Requirements, graph: TaskGraph) -> Failure | None:
    """Run every check and return a ``validation`` Failure, or None when the graph is clean."""
    prec = _Precedence.from_graph(graph)
    cycles = _check_cycles(prec)
    groups = [
        _check_duplicates(req, graph),
        _check_references(graph),
        cycles,
        _check_capability(req, graph),
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
        "tasks or lower their energy_wh.",
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
        hint=f"Battery budgets are exceeded per robot type: {_join_and(phrases)}; lower "
        "energy_wh or move work to other robot types.",
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
