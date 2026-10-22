"""F4: CP-SAT scheduling with explainable infeasibility.

Contract:

    solve_schedule(req, graph, opts=None, locked=None) -> Schedule | Failure
    check_schedule(req, graph, schedule) -> list[str]        # independent re-verification

Model (PRD section "스케줄링 모델"):
  x[i,r]  BoolVar for every task i and every robot r with r.type == task.robot_type
  s[i]    IntVar start, [start_offset_h, horizon_h]; end = s + duration
  exactly one robot per task                       hard
  precedence s_j >= s_i + d_i for each edge        soft group  precedence:<i>-><j>
  per-robot no-overlap (optional intervals on x)   soft group  robot:<r>
  per-zone no-overlap (optional intervals)         soft group  zone:<z>
  sum_i e_i x[i,r] <= battery_wh[r]                soft group  energy:<r>
  C_max >= end_i, C_max <= horizon_h               soft group  horizon
  objective: minimise C_max
"Soft group" = every constraint in the group is only_enforce_if(g) on one group literal, and the
literals go to model.add_assumptions(...). Interval presence literals for the no-overlap
groups must be p = x AND g (new BoolVar + implications) because add_no_overlap itself takes no
enforcement literal.

Solve strategy:
  1. normal solve (opts.num_workers, time limit). OPTIMAL/FEASIBLE -> Schedule.
  2. INFEASIBLE -> re-solve with assumptions, num_workers=1 (cores are only produced there);
     read solver.sufficient_assumptions_for_infeasibility() -> group names.
     If opts.minimize_core: shrink by deletion (drop a group, re-solve; keep it out while still
     infeasible) so the core is minimal and small enough to be a useful repair hint.
     When the single-worker assumption solve times out, start from every group instead and let
     the deletion filter isolate the core; every re-solve of the filter is a hard feasibility
     trial on all workers (see below). After the first core its groups are disabled and up to
     ``MAX_CORES - 1`` further, disjoint cores are looked for the same way, so one repair round
     sees every independent conflict; ``Failure.conflicts`` is their union. The whole
     explanation shares one wall-clock budget (opts.explain_time_limit_s).
  3. UNKNOWN (timeout) -> Failure(kind="solver_timeout").
`locked` entries pin task->robot and start (replanning of tasks already underway).
Return Failure(kind="infeasible", conflicts=[group names], hint=<one sentence per group>).

Implementation notes
--------------------
* One builder (:func:`_build`) produces every model. ``soft=False, minimize=True`` is the
  production model: every constraint hard, makespan minimised. ``soft=True`` is the assumption
  model: no objective, one group literal per group. ``enabled`` restricts which groups are
  built at all, which is how the deletion filter "removes" a group; its trials use
  ``soft=False`` (hard constraints, no objective) because CP-SAT's presolve can use fixed
  constraints but not assumption literals, and that is the difference between proving a tight
  instance infeasible in milliseconds and timing out on it with one worker.
* The start domain is ``[start_offset_h, base + sum(durations)]`` rather than ``[.., horizon_h]``
  so the horizon is enforced *only* through the ``horizon`` group; otherwise a long precedence
  chain would look infeasible through the variable bounds and the core would blame the
  precedence edges instead of the horizon. In the hard model ``C_max <= horizon_h`` tightens the
  bounds at the root, so nothing is lost.
* Groups that can never be violated are not created (a zone or robot with fewer than two
  candidate tasks, a robot whose candidate tasks fit its battery even all together). They would
  never appear in a core anyway and leaving them out keeps the model small.
* Energies are integer milli-Wh (:func:`moonwright.schemas.to_mwh`, i.e. ``round(x * 1000)``)
  for task energies and batteries alike: the one fixed-point unit the validator, this model
  and :func:`check_schedule` share, so the three agree to 1e-3 Wh and a fractional budget
  (replans hand over 3-dp task energies and 2-dp remaining batteries) is neither rejected by
  rounding up nor accepted by rounding down. Hints quote the figures the model used.
* ``locked``: an entry pins ``x[task, robot] = 1`` and ``start == entry.start_h`` (absolute hours,
  the entry's ``end_h`` is ignored; the graph's duration counts). A locked robot that cannot do
  the task by type is treated as capable for that pair. Locks naming a task that is not in the
  graph or a robot that is not in the requirements are ignored.
* :func:`check_schedule` reports the shared conflict ids plus four of its own for defects a
  solver output cannot have but a hand-written schedule can: ``missing:<task>`` (not scheduled),
  ``unknown:<task>`` (entry for a task that is not in the graph), ``duration:<task>``
  (``end_h - start_h != duration_h``) and ``makespan`` (``makespan_h`` is not the latest
  ``end_h`` of the entries).
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from ortools.sat.python import cp_model

from moonwright.schemas import (
    Failure,
    Requirements,
    Schedule,
    ScheduleEntry,
    Task,
    TaskGraph,
    fmt_wh,
    to_mwh,
)

MAX_CORES = 3  # disjoint conflict cores reported per infeasible solve


@dataclass
class SolveOptions:
    time_limit_s: float = 10.0
    num_workers: int = 8
    start_offset_h: int = 0
    explain_infeasibility: bool = True
    minimize_core: bool = True
    core_time_limit_s: float = 5.0      # per solve while explaining (assumption solve, each trial)
    explain_time_limit_s: float = 15.0  # wall-clock budget for the whole explanation


# ----------------------------------------------------------------------------- public API
def solve_schedule(
    req: Requirements,
    graph: TaskGraph,
    opts: SolveOptions | None = None,
    locked: list[ScheduleEntry] | None = None,
) -> Schedule | Failure:
    """Schedule ``graph`` on ``req.robots`` (see the module docstring for the model).

    Returns a :class:`Schedule` (absolute hours, ``status`` "optimal" or "feasible") or a
    :class:`Failure` of kind ``infeasible`` (with the conflict core and a human hint) or
    ``solver_timeout`` (no answer within ``opts.time_limit_s``).
    """
    opts = opts or SolveOptions()
    inst = _instance(req, graph, opts, locked or [])
    failure = _precheck(inst)
    if failure is not None:
        return failure

    built = _build(inst, soft=False, minimize=True)
    solver, status = _run(built, workers=opts.num_workers, time_limit_s=opts.time_limit_s)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _extract(inst, built, solver, status)
    if status == cp_model.INFEASIBLE:
        if not opts.explain_infeasibility:
            return Failure(kind="infeasible", hint="The solver proved the plan infeasible "
                           "(infeasibility explanation disabled).")
        return _explain(inst)
    if status == cp_model.MODEL_INVALID:
        raise RuntimeError(f"CP-SAT rejected the schedule model: {built.model.validate()}")
    return Failure(
        kind="solver_timeout",
        hint=f"CP-SAT found no schedule within the {opts.time_limit_s:g} s budget; the plan may "
             "still be feasible. Simplify the graph (fewer tasks, fewer precedence edges, more "
             "zones) or raise the solver time limit.",
    )


def check_schedule(req: Requirements, graph: TaskGraph, schedule: Schedule) -> list[str]:
    """Re-verify ``schedule`` against ``req``/``graph`` without CP-SAT.

    Returns sorted, de-duplicated conflict ids (``[]`` when the schedule is valid): the shared
    formats ``precedence:<i>-><j>``, ``robot:<r>``, ``zone:<z>``, ``energy:<r>``, ``horizon``,
    ``capability:<task>`` (robot unknown or of the wrong type), ``duplicate:<task>`` (scheduled
    more than once) plus ``missing:<task>``, ``unknown:<task>``, ``duration:<task>`` and
    ``makespan`` (``makespan_h`` differs from the latest ``end_h``, so a UI would print a
    figure the entries contradict). Energy is compared in milli-Wh, exactly as the solver
    modelled it.
    """
    conflicts: set[str] = set()
    robots = {r.id: r for r in req.robots}
    tasks = {t.id: t for t in graph.tasks}
    by_task: dict[str, list[ScheduleEntry]] = defaultdict(list)
    for entry in schedule.entries:
        by_task[entry.task].append(entry)

    for task_id in tasks:
        count = len(by_task.get(task_id, []))
        if count == 0:
            conflicts.add(f"missing:{task_id}")
        elif count > 1:
            conflicts.add(f"duplicate:{task_id}")

    by_robot: dict[str, list[ScheduleEntry]] = defaultdict(list)
    by_zone: dict[str, list[ScheduleEntry]] = defaultdict(list)
    for entry in schedule.entries:
        task = tasks.get(entry.task)
        if task is None:
            conflicts.add(f"unknown:{entry.task}")
            continue
        if entry.end_h - entry.start_h != task.duration_h:
            conflicts.add(f"duration:{entry.task}")
        robot = robots.get(entry.robot)
        if robot is None or robot.type != task.robot_type:
            conflicts.add(f"capability:{entry.task}")
        by_robot[entry.robot].append(entry)
        by_zone[task.zone].append(entry)

    for task in graph.tasks:
        for pred_id in task.after:
            for pred in by_task.get(pred_id, []):
                for succ in by_task.get(task.id, []):
                    if succ.start_h < pred.end_h:
                        conflicts.add(f"precedence:{pred_id}->{task.id}")

    for robot_id, entries in by_robot.items():
        if _overlapping(entries):
            conflicts.add(f"robot:{robot_id}")
        robot = robots.get(robot_id)
        if robot is not None:
            used = sum(to_mwh(tasks[e.task].energy_wh) for e in entries)
            if used > to_mwh(robot.battery_wh):
                conflicts.add(f"energy:{robot_id}")
    for zone_id, entries in by_zone.items():
        if _overlapping(entries):
            conflicts.add(f"zone:{zone_id}")

    if any(entry.end_h > req.horizon_h for entry in schedule.entries):
        conflicts.add("horizon")
    if schedule.makespan_h != max((entry.end_h for entry in schedule.entries), default=0):
        conflicts.add("makespan")
    return sorted(conflicts)


def _overlapping(entries: Iterable[ScheduleEntry]) -> bool:
    """True when two entries share a moment (touching end == start is fine)."""
    latest_end = -1
    for entry in sorted(entries, key=lambda e: (e.start_h, e.end_h)):
        if entry.start_h < latest_end:
            return True
        latest_end = max(latest_end, entry.end_h)
    return False


# ----------------------------------------------------------------------------- instance
@dataclass(frozen=True)
class _Instance:
    """The problem pre-digested into integer data, capability sets and the lock table."""

    req: Requirements
    opts: SolveOptions
    tasks: list[Task]
    index: dict[str, int]               # task id -> position in `tasks` (first occurrence)
    capable: list[list[str]]            # per task: robot ids it may run on (type match or lock)
    locked: dict[int, ScheduleEntry]    # task position -> pinned entry
    edges: list[tuple[int, int]]        # (i, j): task i must finish before task j starts
    edge_groups: dict[str, tuple[int, int]]  # precedence group name -> its edge (i, j)
    energy: list[int]                   # milli-Wh per task
    battery: dict[str, int]             # milli-Wh per robot id
    start_lb: int                       # earliest start (start_offset_h)
    start_ub: int                       # latest start that still leaves room for every task


def _edge_name(tasks: list[Task], i: int, j: int) -> str:
    return f"precedence:{tasks[i].id}->{tasks[j].id}"


def _instance(
    req: Requirements, graph: TaskGraph, opts: SolveOptions, locked: list[ScheduleEntry]
) -> _Instance:
    tasks = list(graph.tasks)
    index: dict[str, int] = {}
    for i, task in enumerate(tasks):
        index.setdefault(task.id, i)
    robot_ids = {r.id for r in req.robots}
    locks = {index[e.task]: e for e in locked if e.task in index and e.robot in robot_ids}

    capable: list[list[str]] = []
    for i, task in enumerate(tasks):
        ids = [r.id for r in req.robots if r.type == task.robot_type]
        lock = locks.get(i)
        if lock is not None and lock.robot not in ids:
            ids.append(lock.robot)
        capable.append(ids)

    edges: list[tuple[int, int]] = []
    for j, task in enumerate(tasks):
        for pred_id in task.after:
            i = index.get(pred_id)
            if i is not None and (i, j) not in edges:
                edges.append((i, j))
    edge_groups: dict[str, tuple[int, int]] = {}
    for i, j in edges:
        edge_groups.setdefault(_edge_name(tasks, i, j), (i, j))

    start_lb = max(0, opts.start_offset_h)
    base = max([start_lb, req.horizon_h, *(e.start_h for e in locks.values())])
    return _Instance(
        req=req, opts=opts, tasks=tasks, index=index, capable=capable, locked=locks, edges=edges,
        edge_groups=edge_groups,
        energy=[to_mwh(t.energy_wh) for t in tasks],
        battery={r.id: to_mwh(r.battery_wh) for r in req.robots},
        start_lb=start_lb, start_ub=base + sum(t.duration_h for t in tasks),
    )


def _precheck(inst: _Instance) -> Failure | None:
    """Structural problems the validator normally catches first; reported instead of crashing."""
    conflicts: list[str] = []
    for i, task in enumerate(inst.tasks):
        if not inst.capable[i]:
            conflicts.append(f"capability:{task.id}")
        for pred_id in task.after:
            if pred_id not in inst.index:
                conflicts.append(f"reference:{task.id}.after={pred_id}")
    if not conflicts:
        return None
    core = sorted(set(conflicts))
    return Failure(kind="infeasible", conflicts=core, hint=_hints(inst, core))


# ----------------------------------------------------------------------------- model
@dataclass
class _Model:
    model: cp_model.CpModel
    assign: dict[tuple[int, str], cp_model.IntVar]   # (task position, robot id) -> x[i, r]
    start: list[cp_model.IntVar]                     # s[i]
    groups: dict[str, cp_model.IntVar]               # group name -> literal (soft model only)


def _build(
    inst: _Instance, *, soft: bool, enabled: Collection[str] | None = None, minimize: bool = False
) -> _Model:
    """Build the CP-SAT model.

    ``soft=False``: every built constraint is hard; ``minimize`` adds the makespan objective.
    ``soft=True``: each constraint group hangs off a Boolean group literal and there is no
    objective. With ``enabled`` given, groups outside it are not built at all (either mode).
    """
    model = cp_model.CpModel()
    groups: dict[str, cp_model.IntVar] = {}
    tasks = inst.tasks

    def switch(name: str) -> cp_model.IntVar | bool:
        """Group switch: False when the group is disabled by ``enabled``, True (always on) in
        the hard model, the group literal in the soft model."""
        if enabled is not None and name not in enabled:
            return False
        if not soft:
            return True
        if name not in groups:
            groups[name] = model.new_bool_var(name)
        return groups[name]

    def add(expr: cp_model.BoundedLinearExpression, g: cp_model.IntVar | bool) -> None:
        """Add a linear constraint under group switch ``g``."""
        if g is False:
            return
        ct = model.add(expr)
        if g is not True:
            ct.only_enforce_if(g)

    # variables + hard assignment constraints
    assign: dict[tuple[int, str], cp_model.IntVar] = {}
    start: list[cp_model.IntVar] = []
    for i, task in enumerate(tasks):
        lock = inst.locked.get(i)
        lb, ub = (lock.start_h, lock.start_h) if lock else (inst.start_lb, inst.start_ub)
        start.append(model.new_int_var(lb, ub, f"s[{task.id}]"))
        for robot_id in inst.capable[i]:
            assign[i, robot_id] = model.new_bool_var(f"x[{task.id},{robot_id}]")
        model.add_exactly_one([assign[i, r] for r in inst.capable[i]])
        if lock is not None:
            model.add(assign[i, lock.robot] == 1)

    # precedence (one group per edge, named as in inst.edge_groups)
    for i, j in inst.edges:
        add(start[j] >= start[i] + tasks[i].duration_h, switch(_edge_name(tasks, i, j)))

    # per-robot energy + no-overlap
    candidates: dict[str, list[int]] = defaultdict(list)
    for i, robot_id in assign:
        candidates[robot_id].append(i)
    for robot in inst.req.robots:
        idxs = candidates.get(robot.id, [])
        if sum(inst.energy[i] for i in idxs) > inst.battery[robot.id]:
            add(sum(inst.energy[i] * assign[i, robot.id] for i in idxs) <= inst.battery[robot.id],
                switch(f"energy:{robot.id}"))
        if len(idxs) < 2:
            continue
        g = switch(f"robot:{robot.id}")
        if g is False:
            continue
        intervals = []
        for i in idxs:
            x = assign[i, robot.id]
            if g is True:
                present = x
            else:  # present <=> x AND g
                present = model.new_bool_var(f"p[{tasks[i].id},{robot.id}]")
                model.add_bool_and([x, g]).only_enforce_if(present)
                model.add_bool_or([x.negated(), g.negated(), present])
            d = tasks[i].duration_h
            intervals.append(model.new_optional_interval_var(
                start[i], d, start[i] + d, present, f"iv[{tasks[i].id},{robot.id}]"))
        model.add_no_overlap(intervals)

    # per-zone no-overlap (a task occupies its zone whichever robot runs it)
    zones: dict[str, list[int]] = defaultdict(list)
    for i, task in enumerate(tasks):
        zones[task.zone].append(i)
    for zone_id, idxs in zones.items():
        if len(idxs) < 2:
            continue
        g = switch(f"zone:{zone_id}")
        if g is False:
            continue
        intervals = []
        for i in idxs:
            d = tasks[i].duration_h
            name = f"iv[{tasks[i].id},{zone_id}]"
            if g is True:
                intervals.append(model.new_interval_var(start[i], d, start[i] + d, name))
            else:
                intervals.append(
                    model.new_optional_interval_var(start[i], d, start[i] + d, g, name))
        model.add_no_overlap(intervals)

    # makespan + horizon
    max_d = max(t.duration_h for t in tasks)
    makespan = model.new_int_var(0, inst.start_ub + max_d, "makespan")
    model.add_max_equality(makespan, [start[i] + tasks[i].duration_h for i in range(len(tasks))])
    add(makespan <= inst.req.horizon_h, switch("horizon"))
    if minimize:
        model.minimize(makespan)
    return _Model(model=model, assign=assign, start=start, groups=groups)


def _run(
    built: _Model, *, workers: int, time_limit_s: float
) -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus]:
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = workers
    solver.parameters.max_time_in_seconds = time_limit_s
    status = solver.solve(built.model)
    return solver, status


def _extract(
    inst: _Instance, built: _Model, solver: cp_model.CpSolver, status: cp_model.CpSolverStatus
) -> Schedule:
    entries = []
    for i, task in enumerate(inst.tasks):
        robot = next(r for r in inst.capable[i] if solver.boolean_value(built.assign[i, r]))
        s = solver.value(built.start[i])
        entries.append(
            ScheduleEntry(task=task.id, robot=robot, start_h=s, end_h=s + task.duration_h))
    return Schedule(
        entries=entries,
        makespan_h=max(e.end_h for e in entries),
        status="optimal" if status == cp_model.OPTIMAL else "feasible",
        objective_bound_h=int(solver.best_objective_bound),
        solve_time_s=solver.wall_time,
    )


# ----------------------------------------------------------------------------- infeasibility
def _time_slice(inst: _Instance, deadline: float) -> float:
    """Seconds the next explanation solve may use: the per-solve limit, capped by ``deadline``
    (a ``time.monotonic`` instant)."""
    return min(inst.opts.core_time_limit_s, max(0.0, deadline - time.monotonic()))


def _core_solve(
    inst: _Instance, enabled: Collection[str] | None, *, time_limit_s: float | None = None
) -> tuple[cp_model.CpSolverStatus, list[str], list[str]]:
    """Solve the soft model with every built group literal assumed true, on a single worker
    (the only mode in which CP-SAT fills ``sufficient_assumptions_for_infeasibility``).

    Returns ``(status, core, assumed)``: ``core`` is the sorted set of group names CP-SAT
    reports as sufficient for infeasibility (empty unless INFEASIBLE), ``assumed`` every group
    that was assumed. A non-positive ``time_limit_s`` skips the solve (status UNKNOWN).
    """
    limit = inst.opts.core_time_limit_s if time_limit_s is None else time_limit_s
    built = _build(inst, soft=True, enabled=enabled)
    assumed = sorted(built.groups)
    if limit <= 0:
        return cp_model.UNKNOWN, [], assumed
    built.model.add_assumptions(list(built.groups.values()))
    solver, status = _run(built, workers=1, time_limit_s=limit)
    if status != cp_model.INFEASIBLE:
        return status, [], assumed
    by_index = {lit.index: name for name, lit in built.groups.items()}
    reported = solver.sufficient_assumptions_for_infeasibility()
    return status, sorted({by_index[i] for i in reported if i in by_index}), assumed


def _trial(
    inst: _Instance, enabled: Collection[str], *, time_limit_s: float
) -> cp_model.CpSolverStatus:
    """Feasibility of the hard model restricted to the ``enabled`` groups, on every worker.

    The groups are plain constraints here, so presolve can use them; that is what proves the
    tight instances the single-worker assumption solve cannot finish. No sub-core is reported,
    the caller isolates. A non-positive ``time_limit_s`` skips the solve (status UNKNOWN).
    """
    if time_limit_s <= 0:
        return cp_model.UNKNOWN
    built = _build(inst, soft=False, enabled=enabled)
    _, status = _run(built, workers=inst.opts.num_workers, time_limit_s=time_limit_s)
    return status


def _minimize_core(inst: _Instance, core: list[str], *, deadline: float | None = None) -> list[str]:
    """Deletion filter: drop each group in turn and keep it out while the rest stay infeasible.

    Every re-solve is a :func:`_trial`. A trial that times out, or that no longer fits before
    ``deadline`` (default: ``explain_time_limit_s`` from now), counts as "needed", so the
    result is always a sound core (a superset of a minimal one), minimal when time allowed.
    """
    if deadline is None:
        deadline = time.monotonic() + inst.opts.explain_time_limit_s
    current = list(core)
    for name in core:
        if name not in current:
            continue
        trial = [n for n in current if n != name]
        if _trial(inst, trial, time_limit_s=_time_slice(inst, deadline)) == cp_model.INFEASIBLE:
            current = trial
    return sorted(set(current))


def _find_core(
    inst: _Instance, enabled: Collection[str] | None, deadline: float
) -> tuple[list[str], list[str]] | None:
    """One conflict core among the ``enabled`` groups (every group when None), as
    ``(core, assumed)``; None when the model restricted to those groups is feasible or the
    budget ran out before infeasibility could be confirmed.

    Fast path: one assumption solve, which usually names a small sufficient subset in
    milliseconds. When it times out (one worker cannot always prove what the multi-worker
    production solve refuted), the fallback confirms infeasibility with one hard trial and lets
    the deletion filter isolate the core from there; the fallback always minimises because
    the full set is not an explanation.
    """
    status, core, assumed = _core_solve(inst, enabled, time_limit_s=_time_slice(inst, deadline))
    if status == cp_model.INFEASIBLE:
        core = core or assumed  # no core reported: start from every group and let deletion shrink
        if inst.opts.minimize_core:
            core = _minimize_core(inst, core, deadline=deadline)
        return core, assumed
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None  # these groups are satisfiable together
    if _trial(inst, assumed, time_limit_s=_time_slice(inst, deadline)) != cp_model.INFEASIBLE:
        return None
    return _minimize_core(inst, assumed, deadline=deadline), assumed


def _explain(inst: _Instance) -> Failure:
    """Turn a proven-infeasible hard model into named conflict cores plus human hints.

    The first core is found among every group; its groups are then disabled and further cores
    (up to :data:`MAX_CORES` in all) are looked for among the rest, so independent conflicts
    (say an overloaded battery *and* a chain longer than the horizon) are reported together.
    The search stops as soon as the remaining groups are satisfiable together, every solve
    shares the ``explain_time_limit_s`` deadline, and the result is deterministic for a given
    instance. ``conflicts`` is the sorted union of the cores.
    """
    deadline = time.monotonic() + inst.opts.explain_time_limit_s
    cores: list[list[str]] = []
    all_groups: list[str] | None = None  # every buildable group, known after the first solve
    disabled: set[str] = set()
    for _ in range(MAX_CORES):
        enabled = None if all_groups is None else [g for g in all_groups if g not in disabled]
        if enabled is not None and not enabled:
            break
        found = _find_core(inst, enabled, deadline)
        if found is None:
            break
        core, assumed = found
        if all_groups is None:
            all_groups = assumed
        cores.append(core)
        disabled.update(core)
        if not core:
            break  # infeasible without any soft group: nothing left to disable
    if not cores:
        return Failure(kind="infeasible", hint="The solver proved the plan infeasible but "
                       "could not isolate the conflicting constraints within the "
                       f"{inst.opts.explain_time_limit_s:g} s explanation budget.")
    union = sorted(set().union(*cores))
    return Failure(kind="infeasible", conflicts=union, hint=_hints(inst, union))


# ----------------------------------------------------------------------------- hints
def _hints(inst: _Instance, core: list[str]) -> str:
    """One human sentence per conflict id, joined with spaces."""
    if not core:
        return "The plan is infeasible even with every soft constraint relaxed."
    return " ".join(_hint(inst, conflict) for conflict in core)


def _hint(inst: _Instance, conflict: str) -> str:
    kind, _, arg = conflict.partition(":")
    return _HINTS[kind](inst, arg)


def _forced_tasks(inst: _Instance, robot_id: str) -> list[int]:
    """Tasks that must run on ``robot_id``: locked to it, or it is the only capable robot."""
    return [i for i in range(len(inst.tasks))
            if inst.capable[i] == [robot_id]
            or (i in inst.locked and inst.locked[i].robot == robot_id)]


def _join_and(parts: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _energy_hint(inst: _Instance, robot_id: str) -> str:
    """Quotes the milli-Wh figures the model used, so the hint matches the verdict exactly."""
    robot = inst.req.robot(robot_id)
    budget = inst.battery[robot_id]
    forced = sum(inst.energy[i] for i in _forced_tasks(inst, robot_id))
    if forced > budget:
        return f"{robot_id} would need {fmt_wh(forced)} Wh but has {fmt_wh(budget)} Wh."
    peers = inst.req.robots_of_type(robot.type)
    total = sum(inst.energy[i] for i, t in enumerate(inst.tasks) if t.robot_type == robot.type)
    pool = sum(inst.battery[r.id] for r in peers)
    return (f"{robot_id} has {fmt_wh(budget)} Wh, and the {robot.type} tasks "
            f"({fmt_wh(total)} Wh in total) cannot be split across the {len(peers)} {robot.type} "
            f"robot(s) ({fmt_wh(pool)} Wh combined) without exceeding its budget.")


def _robot_hint(inst: _Instance, robot_id: str) -> str:
    robot = inst.req.robot(robot_id)
    forced = _forced_tasks(inst, robot_id)
    if forced:
        hours = sum(inst.tasks[i].duration_h for i in forced)
        return (f"{robot_id} runs one task at a time, and the {len(forced)} task(s) only it can "
                f"do already take {hours} h back to back.")
    peers = inst.req.robots_of_type(robot.type)
    hours = sum(t.duration_h for t in inst.tasks if t.robot_type == robot.type)
    return (f"{robot_id} runs one task at a time; the {robot.type} tasks total {hours} h across "
            f"{len(peers)} {robot.type} robot(s).")


def _zone_hint(inst: _Instance, zone_id: str) -> str:
    in_zone = [t for t in inst.tasks if t.zone == zone_id]
    hours = sum(t.duration_h for t in in_zone)
    return (f"Zone {zone_id} admits one robot at a time, so its {len(in_zone)} tasks "
            f"({hours} h in total) cannot overlap.")


def _precedence_hint(inst: _Instance, edge: str) -> str:
    i, j = inst.edge_groups[f"precedence:{edge}"]  # looked up, never re-parsed from the id
    pred_id, succ_id = inst.tasks[i].id, inst.tasks[j].id
    chains = _chains(inst)
    if chains is None:
        return (f"{succ_id} cannot start until {pred_id} finishes, but the precedence edges "
                "form a cycle, so no order exists.")
    into, out_of = chains
    return (f"{succ_id} cannot start until {pred_id} finishes ({pred_id} takes "
            f"{inst.tasks[i].duration_h} h); the precedence chain through this edge is "
            f"{into[i] + out_of[j]} h long.")


def _horizon_hint(inst: _Instance, _arg: str) -> str:
    horizon = inst.req.horizon_h
    parts = []
    overdue = [(inst.tasks[i].id, lock.start_h + inst.tasks[i].duration_h)
               for i, lock in sorted(inst.locked.items())
               if lock.start_h + inst.tasks[i].duration_h > horizon]
    if overdue:
        parts.append(_join_and([f"the locked task {task_id} cannot end before {end} h"
                                for task_id, end in overdue]))
    chains = _chains(inst, with_locks=True)
    if chains is not None:
        into, _ = chains
        what = ("the longest chain of work, locked starts included," if inst.locked
                else "the longest precedence chain alone")
        parts.append(f"{what} takes {max(into)} h")
    by_type: dict[str, int] = defaultdict(int)
    for task in inst.tasks:
        by_type[task.robot_type] += task.duration_h
    loads = {rtype: math.ceil(hours / len(inst.req.robots_of_type(rtype)))
             for rtype, hours in by_type.items() if inst.req.robots_of_type(rtype)}
    if loads:
        busiest = max(loads, key=lambda rtype: (loads[rtype], rtype))
        parts.append(f"the busiest robot type ({busiest}) needs at least {loads[busiest]} h")
    elapsed = f" after the {inst.start_lb} h already elapsed" if inst.start_lb else ""
    return f"The plan cannot finish within the {horizon} h horizon{elapsed}: {_join_and(parts)}."


def _capability_hint(inst: _Instance, task_id: str) -> str:
    task = inst.tasks[inst.index[task_id]]
    return f"Task {task_id} needs a {task.robot_type} but no robot of that type exists."


def _reference_hint(_inst: _Instance, arg: str) -> str:
    task_id, _, pred_id = arg.partition(".after=")
    return f"Task {task_id} waits for unknown task {pred_id}."


_HINTS = {
    "energy": _energy_hint,
    "robot": _robot_hint,
    "zone": _zone_hint,
    "precedence": _precedence_hint,
    "horizon": _horizon_hint,
    "capability": _capability_hint,
    "reference": _reference_hint,
}


def _chains(inst: _Instance, *, with_locks: bool = False) -> tuple[list[int], list[int]] | None:
    """Longest duration chains ending at / starting from each task (inclusive), or None when
    the precedence graph has a cycle. With ``with_locks`` a locked task's chain cannot end
    before its pinned start plus its duration (relative to ``start_lb``), so locked ends are
    folded into the "into" figures."""
    n = len(inst.tasks)
    succs: list[list[int]] = [[] for _ in range(n)]
    indegree = [0] * n
    for i, j in inst.edges:
        succs[i].append(j)
        indegree[j] += 1
    order = [i for i in range(n) if indegree[i] == 0]
    for i in order:  # Kahn's algorithm; `order` grows while we iterate
        for j in succs[i]:
            indegree[j] -= 1
            if indegree[j] == 0:
                order.append(j)
    if len(order) < n:
        return None
    durations = [t.duration_h for t in inst.tasks]
    into = list(durations)
    if with_locks:
        for i, lock in inst.locked.items():
            into[i] = max(into[i], lock.start_h - inst.start_lb + durations[i])
    for i in order:
        for j in succs[i]:
            into[j] = max(into[j], into[i] + durations[j])
    out_of = list(durations)
    for i in reversed(order):
        for j in succs[i]:
            out_of[i] = max(out_of[i], durations[i] + out_of[j])
    return into, out_of
