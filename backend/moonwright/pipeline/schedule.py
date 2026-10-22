"""F4: CP-SAT scheduling with explainable infeasibility.

Contract:

    solve_schedule(req, graph, opts=None, locked=None) -> Schedule | Failure
    check_schedule(req, graph, schedule, opts=None) -> list[str]   # independent re-verification
    sequence_travel(req, graph, schedule, opts=None) -> {robot id: RobotTravel}   # the legs driven
    budget_rows(req, graph, failure, opts=None, locked=None) -> [{kind, subject, robots,
        budget_wh, demanded_wh, travel_wh}]   # the energy figures behind a verdict, as numbers

Model (PRD section "스케줄링 모델"):
  x[i,r]  BoolVar for every task i and every robot r with r.type == task.robot_type
  s[i]    IntVar start, [start_offset_h, horizon_h]; end = s + duration
  exactly one robot per task                       hard
  x[i,r] => s_i >= start_offset_h + first_leg(i,r) hard   (the robot must be able to get there)
  precedence s_j >= s_i + d_i for each edge        soft group  precedence:<i>-><j>
  per-robot no-overlap (optional intervals on x,
    each [s_i - T[i,r], s_i + d_i])                soft group  robot:<r>
  per-zone no-overlap (optional intervals)         soft group  zone:<z>
  sum_i (e_i + travel_wh(T_cells[i,r])) x[i,r]
    <= battery_wh[r]                               soft group  energy:<r>
  C_max >= end_i, C_max <= horizon_h               soft group  horizon
  objective: minimise C_max (work end: the makespan is when the last task finishes)
"Soft group" = every constraint in the group is only_enforce_if(g) on one group literal, and the
literals go to model.add_assumptions(...). Interval presence literals for the no-overlap
groups must be p = x AND g (new BoolVar + implications) because add_no_overlap itself takes no
enforcement literal.

Travel
------
travel is a per-task transit allowance (an upper bound over the site), not terrain; the
simulator pays the actual distance. For every task i and capable robot r,
``T_cells[i,r] = max(dist(p, zone_i))`` over p in {r's start position} ∪ {centres of the zones
of every task r may run} and ``T[i,r] = ceil(T_cells[i,r] / speed_r)`` whole hours
(:func:`moonwright.geometry.travel_hours`). Whatever r did before task i, it comes from its
start position or from one of those zones, so the leg it actually drives is never longer than
the allowance: the bound is sound and independent of the sequence the solver picks. On r's
timeline the task occupies ``[s_i - T[i,r], s_i + d_i]`` (an optional interval with an affine
start), so consecutive tasks are at least a full allowance apart; the first task cannot start
before the first leg ``ceil(dist(start_r, zone_i) / speed_r)`` (a hard constraint, skipped for
a locked task, whose robot is by definition already on it); and the per-robot energy sum
budgets ``T_cells[i,r] * TRAVEL_WH_PER_CELL`` (0.5 Wh per cell, the simulator's constant) for
every task the robot takes. Group names are unchanged, so cores, hints and repair prompts read
as before. Zone no-overlap is unchanged (travel happens outside zones), and the makespan stays
the end of the last task. Zone centres come from :func:`moonwright.geometry.zone_positions`
(declared zones plus the deterministic layout of the ones only tasks use), exactly as the
simulator lays them out. ``SolveOptions.robot_positions`` says where each robot is at
``start_offset_h`` (a replan passes the simulator frame); everyone else starts at the depot.
Since the simulator leaves for the next zone as soon as a robot is free and starts work at
max(scheduled start, arrival), a disturbance-free replay of a solver schedule starts every
task on time; a ``task_overrun`` there is a genuine anomaly, not travel drift.

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
Return Failure(kind="infeasible", conflicts=[group names], hint=<one sentence per group, the
pool-form sentences of one robot type merged into one that lists the robots>).

Implementation notes
--------------------
* One builder (:func:`_build`) produces every model. ``soft=False, minimize=True`` is the
  production model: every constraint hard, makespan minimised. ``soft=True`` is the assumption
  model: no objective, one group literal per group. ``enabled`` restricts which groups are
  built at all, which is how the deletion filter "removes" a group; its trials use
  ``soft=False`` (hard constraints, no objective) because CP-SAT's presolve can use fixed
  constraints but not assumption literals, and that is the difference between proving a tight
  instance infeasible in milliseconds and timing out on it with one worker.
* The start domain is ``[start_offset_h, base + sum(durations + allowances)]`` rather than
  ``[.., horizon_h]`` so the horizon is enforced *only* through the ``horizon`` group; otherwise
  a long precedence chain would look infeasible through the variable bounds and the core would
  blame the precedence edges instead of the horizon. In the hard model ``C_max <= horizon_h``
  tightens the bounds at the root, so nothing is lost. The domain leaves room for a fully
  serial schedule with every allowance, so the hard first-leg constraints alone can never make
  the model infeasible and every core is a set of soft groups.
* Groups that can never be violated are not created (a zone or robot with fewer than two
  candidate tasks, a robot whose candidate tasks fit its battery even all together, travel
  allowances included). They would never appear in a core anyway and leaving them out keeps the
  model small. The allowance adds no variable or constraint: it only widens the intervals the
  robot no-overlap already has and adds a constant per task to the energy sum.
* Energies are integer milli-Wh (:func:`moonwright.schemas.to_mwh`, i.e. ``round(x * 1000)``)
  for task energies, travel allowances and batteries alike: the one fixed-point unit the
  validator, this model and :func:`check_schedule` share, so the three agree to 1e-3 Wh and a
  fractional budget (replans hand over 3-dp task energies and 2-dp remaining batteries) is
  neither rejected by rounding up nor accepted by rounding down. Hints quote the figures the
  model used as whole Wh with thousands separators ("9,930 Wh"), the travel share called out
  when it is worth a Wh; a fractional budget gets just the decimals that keep the two figures
  apart (see the "hints" section). :func:`budget_rows` returns the same figures as numbers.
* ``locked``: an entry pins ``x[task, robot] = 1`` and ``start == entry.start_h`` (absolute hours,
  the entry's ``end_h`` is ignored; the graph's duration counts). A locked robot that cannot do
  the task by type is treated as capable for that pair. Locks naming a task that is not in the
  graph or a robot that is not in the requirements are ignored.
* :func:`check_schedule` reports the shared conflict ids plus four of its own for defects a
  solver output cannot have but a hand-written schedule can: ``missing:<task>`` (not scheduled),
  ``unknown:<task>`` (entry for a task that is not in the graph), ``duration:<task>``
  (``end_h - start_h != duration_h``) and ``makespan`` (``makespan_h`` is not the latest
  ``end_h`` of the entries). It checks the *actual* legs of each robot's sequence
  (:func:`sequence_travel`, which the verification receipt prints too): consecutive entries
  must be a whole ``travel_hours`` apart (``robot:<r>`` otherwise) and the energy per robot
  includes the travel of that sequence (``energy:<r>``). Every actual leg is at most the
  allowance the solver reserved, so a solver schedule always passes. With ``opts`` given, the
  first leg of every robot (from ``opts.robot_positions`` or the depot, starting at
  ``opts.start_offset_h``) is timed and charged as well; without it the robots' start
  positions are unknown and only the legs between tasks are checked.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

from moonwright.geometry import (
    distance,
    start_positions,
    travel_hours,
    travel_wh,
    zone_positions,
)
from moonwright.schemas import (
    MWH_PER_WH,
    Failure,
    Requirements,
    Schedule,
    ScheduleEntry,
    Task,
    TaskGraph,
    to_mwh,
)
from moonwright.text import an as _an
from moonwright.text import h as _h
from moonwright.text import join_and as _join_and
from moonwright.text import plural as _plural
from moonwright.text import wh as _wh
from moonwright.text import whole_wh as _whole_wh

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
    # Where each robot is at ``start_offset_h``, in grid cells (floats allowed): a replan passes
    # the positions of the simulator frame it replans from. Robots missing from the dict, and
    # every robot when None, start at the depot. The first task of a robot cannot start before
    # it can drive there from this position, and its transit allowance covers that leg too.
    robot_positions: dict[str, tuple[float, float]] | None = None


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


def check_schedule(
    req: Requirements, graph: TaskGraph, schedule: Schedule, opts: SolveOptions | None = None
) -> list[str]:
    """Re-verify ``schedule`` against ``req``/``graph`` without CP-SAT.

    Returns sorted, de-duplicated conflict ids (``[]`` when the schedule is valid): the shared
    formats ``precedence:<i>-><j>``, ``robot:<r>`` (overlapping entries, or consecutive entries
    closer than the whole hours of travel between their zones), ``zone:<z>``, ``energy:<r>``
    (work plus the travel of the robot's actual sequence), ``horizon``, ``capability:<task>``
    (robot unknown or of the wrong type), ``duplicate:<task>`` (scheduled more than once) plus
    ``missing:<task>``, ``unknown:<task>``, ``duration:<task>`` and ``makespan`` (``makespan_h``
    differs from the latest ``end_h``, so a UI would print a figure the entries contradict).
    Energy is compared in milli-Wh, exactly as the solver modelled it.

    ``opts`` (the :class:`SolveOptions` the solver was given) supplies where and when each
    robot starts (``robot_positions`` or the depot, at ``start_offset_h``), so the first leg of
    every robot is timed and charged as well. Without it the start positions are unknown and
    only the legs between consecutive tasks are checked.
    """
    conflicts: set[str] = set()
    robots = {r.id: r for r in req.robots}
    tasks = {t.id: t for t in graph.tasks}
    travel = sequence_travel(req, graph, schedule, opts)
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
        if robot is None:
            continue
        # the robot's actual sequence: every leg must fit between the entries, and is paid for
        legs = travel[robot_id]
        if legs.late_legs:
            conflicts.add(f"robot:{robot_id}")
        used = sum(to_mwh(tasks[e.task].energy_wh) for e in entries) + legs.mwh
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


@dataclass(frozen=True)
class RobotTravel:
    """The legs one robot drives along its scheduled sequence: their energy in milli-Wh (each
    leg at :data:`~moonwright.geometry.TRAVEL_WH_PER_CELL`) and how many of them do not fit
    between their entries in whole hours (``late_legs``: the ``robot:<r>`` defect)."""

    mwh: int = 0
    late_legs: int = 0


def sequence_travel(
    req: Requirements, graph: TaskGraph, schedule: Schedule, opts: SolveOptions | None = None
) -> dict[str, RobotTravel]:
    """Per scheduled robot of ``req``, the travel of its actual sequence of entries (sorted by
    start): with ``opts`` the first leg from where the robot starts (``opts.robot_positions``
    or the depot, free from ``opts.start_offset_h``), then zone to zone; without ``opts`` the
    start position is unknown and only the legs between entries count. Entries naming unknown
    tasks or robots are skipped (``check_schedule`` reports them)."""
    robots = {r.id: r for r in req.robots}
    tasks = {t.id: t for t in graph.tasks}
    layout = zone_positions(req.site, (t.zone for t in graph.tasks))
    starts = None if opts is None else start_positions(req, opts.robot_positions)
    origin = 0 if opts is None else max(0, opts.start_offset_h)
    by_robot: dict[str, list[ScheduleEntry]] = defaultdict(list)
    for entry in schedule.entries:
        if entry.task in tasks and entry.robot in robots:
            by_robot[entry.robot].append(entry)
    out: dict[str, RobotTravel] = {}
    for robot_id, entries in by_robot.items():
        robot = robots[robot_id]
        here = None if starts is None else starts[robot_id]
        free_h, mwh, late = origin, 0, 0
        for entry in sorted(entries, key=lambda e: (e.start_h, e.end_h)):
            there = layout[tasks[entry.task].zone]
            if here is not None:
                cells = distance(here, there)
                mwh += to_mwh(travel_wh(cells))
                if entry.start_h < free_h + travel_hours(cells, robot.speed_cells_per_h):
                    late += 1
            here, free_h = there, max(free_h, entry.end_h)
        out[robot_id] = RobotTravel(mwh=mwh, late_legs=late)
    return out


def budget_rows(
    req: Requirements, graph: TaskGraph, failure: Failure,
    opts: SolveOptions | None = None, locked: list[ScheduleEntry] | None = None,
) -> list[dict[str, Any]]:
    """The numbers behind the ``energy:<robot>`` conflicts of ``failure`` (a verdict of
    :func:`solve_schedule` on ``req``/``graph`` under the same ``opts`` and ``locked``), as
    JSON-ready rows for a UI's budget table, in the order the conflicts first name them:

    * ``{"kind": "type", "subject": <robot type>, "robots": [every robot of the type],
      "budget_wh": <their batteries combined>, "demanded_wh": <the type's work plus the least
      transit allowance of each task>, "travel_wh": <that allowance>}``: one row per type whose
      work fits the pool but no split of it fits the single batteries, however many of its
      robots the core names;
    * ``{"kind": "robot", "subject": <robot id>, "robots": [<robot id>], "budget_wh": <its
      battery>, "demanded_wh": <the tasks only it can run, transit included>, "travel_wh":
      <that transit>}``: one row per robot whose own forced tasks exceed its battery.

    Wh to the milli-Wh: exactly the figures the hint sentences quote (rounded there). ``[]``
    when the failure has no energy conflict; conflicts naming a robot ``req`` does not have
    are skipped.
    """
    core = [c for c in failure.conflicts if c.startswith("energy:")]
    if not core:
        return []
    inst = _instance(req, graph, opts or SolveOptions(), locked or [])
    return [row.as_dict() for row in _energy_budgets(inst, core)]


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
class _Transit:
    """Travel budget of one (task, robot) pair. The allowance is the longest leg the robot
    could have to drive to the task's zone (from its start position or from any zone it can be
    sent to); the first leg is the one from its start position (0 for a locked task)."""

    allowance_h: int      # hours reserved before the task on the robot's timeline
    allowance_mwh: int    # energy reserved for that leg, milli-Wh
    first_leg_h: int      # hours before the robot can be at the zone at all


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
    transit: dict[tuple[int, str], _Transit]  # (task position, robot id) -> travel budget
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

    transit = _transits(req, tasks, capable, locks, opts.robot_positions)
    start_lb = max(0, opts.start_offset_h)
    base = max([start_lb, req.horizon_h, *(e.start_h for e in locks.values())])
    room = sum(t.duration_h + max((transit[i, r].allowance_h for r in capable[i]), default=0)
               for i, t in enumerate(tasks))
    return _Instance(
        req=req, opts=opts, tasks=tasks, index=index, capable=capable, locked=locks, edges=edges,
        edge_groups=edge_groups,
        energy=[to_mwh(t.energy_wh) for t in tasks],
        battery={r.id: to_mwh(r.battery_wh) for r in req.robots},
        transit=transit,
        start_lb=start_lb, start_ub=base + room,
    )


def _transits(
    req: Requirements, tasks: list[Task], capable: list[list[str]],
    locks: dict[int, ScheduleEntry], positions: dict[str, tuple[float, float]] | None,
) -> dict[tuple[int, str], _Transit]:
    """The transit allowance of every (task, capable robot) pair (module docstring, "Travel")."""
    layout = zone_positions(req.site, (t.zone for t in tasks))
    starts = start_positions(req, positions)
    speed = {r.id: r.speed_cells_per_h for r in req.robots}
    reach: dict[str, set[tuple[int, int]]] = defaultdict(set)  # zones a robot may be sent to
    for i, task in enumerate(tasks):
        for robot_id in capable[i]:
            reach[robot_id].add(layout[task.zone])
    transit: dict[tuple[int, str], _Transit] = {}
    for i, task in enumerate(tasks):
        here = layout[task.zone]
        for robot_id in capable[i]:
            first = distance(starts[robot_id], here)
            cells = max(first, *(distance(p, here) for p in reach[robot_id]))
            transit[i, robot_id] = _Transit(
                allowance_h=travel_hours(cells, speed[robot_id]),
                allowance_mwh=to_mwh(travel_wh(cells)),
                first_leg_h=0 if i in locks else travel_hours(first, speed[robot_id]),
            )
    return transit


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

    # per-robot energy + no-overlap (each task's interval is preceded by its transit allowance)
    candidates: dict[str, list[int]] = defaultdict(list)
    for i, robot_id in assign:
        candidates[robot_id].append(i)
    for robot in inst.req.robots:
        idxs = candidates.get(robot.id, [])
        need = {i: inst.energy[i] + inst.transit[i, robot.id].allowance_mwh for i in idxs}
        if sum(need.values()) > inst.battery[robot.id]:
            add(sum(need[i] * assign[i, robot.id] for i in idxs) <= inst.battery[robot.id],
                switch(f"energy:{robot.id}"))
        for i in idxs:  # first leg: the robot cannot be at the zone before it can drive there
            lead = inst.transit[i, robot.id].first_leg_h
            if lead:
                model.add(start[i] >= inst.start_lb + lead).only_enforce_if(assign[i, robot.id])
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
            allowance = inst.transit[i, robot.id].allowance_h
            begin = start[i] - allowance if allowance else start[i]
            intervals.append(model.new_optional_interval_var(
                begin, d + allowance, start[i] + d, present, f"iv[{tasks[i].id},{robot.id}]"))
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
# Copy rules (phase 3d; a mission planner reads the hint on the trace, the repair prompt reads
# it verbatim): whole-number figures with thousands separators and a unit ("9,930 Wh", "62 h"),
# real plurals, no nested parentheses, and one sentence per conflict, except that the robots of
# one type whose conflict has the same pool-form explanation share one sentence that lists them.
# Every figure a repair needs (budget, demand, travel share, pool) stays in the sentence;
# :func:`budget_rows` hands the energy figures over as numbers, from the very rows the
# sentences are rendered from, so prose and table can never disagree.
def _hints(inst: _Instance, core: list[str]) -> str:
    """The human hint for ``core``: one sentence per conflict in core order, pool-form energy
    and robot sentences of one type merged into one (emitted where the group's first conflict
    stands)."""
    if not core:
        return "The plan is infeasible even with every soft constraint relaxed."
    merged: dict[str, tuple[int, str]] = {}   # conflict id -> (group number, its sentence)
    groups = 0
    for budget in _energy_budgets(inst, core):
        groups += 1
        for robot_id in budget.named:
            merged[f"energy:{robot_id}"] = (groups, _energy_sentence(inst, budget))
    for load in _robot_loads(inst, core):
        groups += 1
        for robot_id in load.named:
            merged[f"robot:{robot_id}"] = (groups, _robot_sentence(load))
    sentences: list[str] = []
    said: set[int] = set()
    for conflict in core:
        if conflict in merged:
            group, sentence = merged[conflict]
            if group not in said:
                said.add(group)
                sentences.append(sentence)
            continue
        kind, _, arg = conflict.partition(":")
        hint = _HINTS.get(kind)
        sentences.append(hint(inst, arg) if hint else _unknown_hint(inst, conflict))
    return " ".join(sentences)


# -- figures: whole Wh and h with thousands separators and real plurals (moonwright.text)
def _wh_value(mwh: int) -> float:
    """Milli-Wh as the Wh number a table shows: exact to the milli-Wh, never float noise."""
    return round(mwh / MWH_PER_WH, 3)


def _decimals_apart(need: int, budget: int) -> int:
    """The fewest decimals (0 to 3) at which ``need`` (milli-Wh, above ``budget``) still prints
    above ``budget``: 0 for every whole-Wh case, more only for the fractional figures a replan
    hands over (1,784.381 Wh against 1,784.38 Wh), so no sentence ever reads "would need
    1,784 Wh but has 1,784 Wh". :func:`moonwright.text.wh` rounds the same way (half up on the
    integer milli-Wh), so the decimals chosen here are the decimals printed."""
    for decimals in range(3):
        unit = MWH_PER_WH // 10 ** decimals
        if (need + unit // 2) // unit > (budget + unit // 2) // unit:
            return decimals
    return 3


def _travel_clause(mwh: int) -> str:
    """', about 30 Wh of it travel' when the transit share is worth a Wh, else ''. The share
    is the allowance the model reserved (an upper bound), hence "about"."""
    return f", about {_wh(mwh)} of it travel" if _whole_wh(mwh) >= 1 else ""


def _transit_note(hours: int) -> str:
    return f" plus up to {_h(hours)} of transit" if hours else ""


def _forced_tasks(inst: _Instance, robot_id: str) -> list[int]:
    """Tasks that must run on ``robot_id``: locked to it, or it is the only capable robot."""
    return [i for i in range(len(inst.tasks))
            if inst.capable[i] == [robot_id]
            or (i in inst.locked and inst.locked[i].robot == robot_id)]


def _least_allowance(inst: _Instance, i: int) -> _Transit:
    """The transit budget of task ``i`` on whichever capable robot needs the least: a lower
    bound whatever the solver assigns."""
    return min((inst.transit[i, r] for r in inst.capable[i]),
               key=lambda t: (t.allowance_mwh, t.allowance_h))


# -- energy: the budget rows behind energy:<robot> conflicts, and their sentences
@dataclass(eq=False)
class _Budget:
    """The figures behind one or more ``energy:<robot>`` conflicts, in milli-Wh. ``kind``
    "robot": the tasks only ``subject`` can run (locked to it, or it is the only robot of its
    type) already exceed its battery. ``kind`` "type": the ``subject`` type's work fits the pool
    of its robots' batteries but no split of it fits the single batteries. ``named`` are the
    robots of the core the row explains; ``robots`` those whose batteries make up
    ``budget_mwh`` (the robot itself, or every robot of the type)."""

    kind: str
    subject: str
    named: list[str]
    robots: list[str]
    budget_mwh: int
    demanded_mwh: int
    travel_mwh: int

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "robots": list(self.robots),
                "budget_wh": _wh_value(self.budget_mwh),
                "demanded_wh": _wh_value(self.demanded_mwh),
                "travel_wh": _wh_value(self.travel_mwh)}


def _energy_budgets(inst: _Instance, core: list[str]) -> list[_Budget]:
    """One :class:`_Budget` per robot-form ``energy:<robot>`` conflict of ``core`` and one per
    robot *type* of the pool-form ones, in order of first appearance; conflicts naming a robot
    the instance does not have are skipped. The figures are the model's own (milli-Wh, travel
    allowances included), so a hint matches the verdict exactly."""
    rows: list[_Budget] = []
    pools: dict[str, _Budget] = {}
    for conflict in core:
        kind, _, robot_id = conflict.partition(":")
        if kind != "energy" or robot_id not in inst.battery:
            continue
        robot = inst.req.robot(robot_id)
        budget = inst.battery[robot_id]
        forced = _forced_tasks(inst, robot_id)
        travel = sum(inst.transit[i, robot_id].allowance_mwh for i in forced)
        need = sum(inst.energy[i] for i in forced) + travel
        if need > budget:
            rows.append(_Budget("robot", robot_id, [robot_id], [robot_id], budget, need, travel))
            continue
        pool = pools.get(robot.type)
        if pool is None:
            peers = inst.req.robots_of_type(robot.type)
            typed = [i for i, t in enumerate(inst.tasks) if t.robot_type == robot.type]
            travel = sum(_least_allowance(inst, i).allowance_mwh for i in typed)
            pool = _Budget("type", robot.type, [], [r.id for r in peers],
                           sum(inst.battery[r.id] for r in peers),
                           sum(inst.energy[i] for i in typed) + travel, travel)
            pools[robot.type] = pool
            rows.append(pool)
        pool.named.append(robot_id)
    return rows


def _energy_sentence(inst: _Instance, row: _Budget) -> str:
    """Robot form: "PR1 would need 4,023 Wh, about 33 Wh of it travel, but has 4,000 Wh."
    Type form: "EX1 and EX2 each have 5,000 Wh; the excavator tasks need 9,930 Wh in total,
    about 30 Wh of it travel, and cannot be split across 2 excavators (10,000 Wh combined)
    without exceeding one budget." """
    travel = _travel_clause(row.travel_mwh)
    comma = "," if travel else ""
    if row.kind == "robot":
        decimals = _decimals_apart(row.demanded_mwh, row.budget_mwh)
        return (f"{row.subject} would need {_wh(row.demanded_mwh, decimals)}{travel}{comma} "
                f"but has {_wh(row.budget_mwh, decimals)}.")
    budgets = [inst.battery[r] for r in row.named]
    if len(row.named) > 1 and len(set(budgets)) == 1:
        who = f"{_join_and(row.named)} each have {_wh(budgets[0])}"
    else:
        who = _join_and([f"{r} has {_wh(b)}" for r, b in zip(row.named, budgets, strict=True)])
    fleet = _plural(len(row.robots), row.subject)
    need = f"the {row.subject} tasks need {_wh(row.demanded_mwh)} in total{travel}"
    if row.demanded_mwh > row.budget_mwh:
        return (f"{who}; {need}, more than the {_wh(row.budget_mwh)} the {fleet} have "
                "combined.")
    return (f"{who}; {need}{comma} and cannot be split across {fleet} "
            f"({_wh(row.budget_mwh)} combined) without exceeding one budget.")


# -- robot time: the hours behind robot:<robot> conflicts, and their sentences
@dataclass(eq=False)
class _Load:
    """The hours behind ``robot:<robot>`` conflicts: ``kind`` "robot" when the ``tasks`` only
    ``subject`` can run already take ``hours`` back to back, "type" when the ``subject`` type's
    ``tasks`` take ``hours`` across its ``count`` robots; ``transit_h`` is the transit on top."""

    kind: str
    subject: str
    named: list[str]
    count: int
    tasks: int
    hours: int
    transit_h: int


def _robot_loads(inst: _Instance, core: list[str]) -> list[_Load]:
    """Like :func:`_energy_budgets`, for the ``robot:<robot>`` conflicts of ``core``."""
    rows: list[_Load] = []
    pools: dict[str, _Load] = {}
    for conflict in core:
        kind, _, robot_id = conflict.partition(":")
        if kind != "robot" or robot_id not in inst.battery:
            continue
        robot = inst.req.robot(robot_id)
        forced = _forced_tasks(inst, robot_id)
        if forced:
            rows.append(_Load("robot", robot_id, [robot_id], 1, len(forced),
                              sum(inst.tasks[i].duration_h for i in forced),
                              sum(inst.transit[i, robot_id].allowance_h for i in forced)))
            continue
        pool = pools.get(robot.type)
        if pool is None:
            typed = [i for i, t in enumerate(inst.tasks) if t.robot_type == robot.type]
            pool = _Load("type", robot.type, [], len(inst.req.robots_of_type(robot.type)),
                         len(typed), sum(inst.tasks[i].duration_h for i in typed),
                         sum(_least_allowance(inst, i).allowance_h for i in typed))
            pools[robot.type] = pool
            rows.append(pool)
        pool.named.append(robot_id)
    return rows


def _robot_sentence(row: _Load) -> str:
    if row.kind == "robot":
        if row.tasks == 1:
            return (f"{row.subject} runs one task at a time, and the task only it can do "
                    f"already takes {_h(row.hours)}{_transit_note(row.transit_h)}.")
        return (f"{row.subject} runs one task at a time, and the {_plural(row.tasks, 'task')} "
                f"only it can do already take {_h(row.hours)} back to back"
                f"{_transit_note(row.transit_h)}.")
    verb = "runs" if len(row.named) == 1 else "each run"
    return (f"{_join_and(row.named)} {verb} one task at a time; the {row.subject} tasks total "
            f"{_h(row.hours)} across {_plural(row.count, row.subject)}"
            f"{_transit_note(row.transit_h)}.")


# -- the other groups
def _zone_hint(inst: _Instance, zone_id: str) -> str:
    in_zone = [t for t in inst.tasks if t.zone == zone_id]
    hours = sum(t.duration_h for t in in_zone)
    return (f"Zone {zone_id} admits one robot at a time, so its {_plural(len(in_zone), 'task')} "
            f"({_h(hours)} in total) cannot overlap.")


def _precedence_hint(inst: _Instance, edge: str) -> str:
    i, j = inst.edge_groups[f"precedence:{edge}"]  # looked up, never re-parsed from the id
    pred_id, succ_id = inst.tasks[i].id, inst.tasks[j].id
    chains = _chains(inst)
    if chains is None:
        return (f"{succ_id} cannot start until {pred_id} finishes, but the precedence edges "
                "form a cycle, so no order exists.")
    into, out_of = chains
    return (f"{succ_id} cannot start until {pred_id} finishes ({pred_id} takes "
            f"{_h(inst.tasks[i].duration_h)}); the precedence chain through this edge is "
            f"{_h(into[i] + out_of[j])} long.")


def _horizon_hint(inst: _Instance, _arg: str) -> str:
    """"The plan cannot finish within the 60 h horizon: the longest precedence chain alone
    needs 86 h, 26 h past the 60 h deadline; the busiest robot type (excavator) needs at least
    40 h." Every figure that overshoots says by how much."""
    horizon = inst.req.horizon_h

    def past(end_h: int) -> str:
        """', 26 h past the 60 h deadline' when an absolute end overshoots, else ''."""
        return (f", {_h(end_h - horizon)} past the {_h(horizon)} deadline"
                if end_h > horizon else "")

    parts: list[str] = []
    for i, lock in sorted(inst.locked.items()):
        end = lock.start_h + inst.tasks[i].duration_h
        if end > horizon:
            parts.append(f"the locked task {inst.tasks[i].id} cannot end before {_h(end)}"
                         f"{past(end)}")
    chains = _chains(inst, with_locks=True, with_travel=True)
    if chains is not None:
        longest = max(chains[0])
        what = ("the longest chain of work, locked starts included," if inst.locked
                else "the longest precedence chain alone")
        without_travel = _chains(inst, with_locks=True)
        note = ("" if without_travel is None or max(without_travel[0]) == longest
                else " including the transit to its first zone")
        parts.append(f"{what} needs {_h(longest)}{note}{past(inst.start_lb + longest)}")
    by_type: dict[str, int] = defaultdict(int)
    for task in inst.tasks:
        by_type[task.robot_type] += task.duration_h
    loads = {rtype: math.ceil(hours / len(inst.req.robots_of_type(rtype)))
             for rtype, hours in by_type.items() if inst.req.robots_of_type(rtype)}
    if loads:
        busiest = max(loads, key=lambda rtype: (loads[rtype], rtype))
        parts.append(f"the busiest robot type ({busiest}) needs at least {_h(loads[busiest])}"
                     f"{past(inst.start_lb + loads[busiest])}")
    elapsed = f" after the {_h(inst.start_lb)} already elapsed" if inst.start_lb else ""
    return (f"The plan cannot finish within the {_h(horizon)} horizon{elapsed}: "
            f"{'; '.join(parts)}.")


def _capability_hint(inst: _Instance, task_id: str) -> str:
    task = inst.tasks[inst.index[task_id]]
    return f"Task {task_id} needs {_an(task.robot_type)} but no robot of that type exists."


def _reference_hint(_inst: _Instance, arg: str) -> str:
    task_id, _, pred_id = arg.partition(".after=")
    return f"Task {task_id} waits for unknown task {pred_id}."


def _unknown_hint(_inst: _Instance, conflict: str) -> str:
    """A conflict id the solver never produces (a caller's own), named rather than crashed on."""
    return f"The constraint {conflict} cannot be satisfied."


_HINTS = {
    "zone": _zone_hint,
    "precedence": _precedence_hint,
    "horizon": _horizon_hint,
    "capability": _capability_hint,
    "reference": _reference_hint,
}


def _chains(
    inst: _Instance, *, with_locks: bool = False, with_travel: bool = False
) -> tuple[list[int], list[int]] | None:
    """Longest duration chains ending at / starting from each task (inclusive), or None when
    the precedence graph has a cycle. With ``with_locks`` a locked task's chain cannot end
    before its pinned start plus its duration (relative to ``start_lb``), so locked ends are
    folded into the "into" figures; with ``with_travel`` neither can any task end before the
    shortest first leg a capable robot needs to reach it, so that is folded in as well."""
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
    if with_travel:
        for i in range(n):
            lead = min((inst.transit[i, r].first_leg_h for r in inst.capable[i]), default=0)
            into[i] += lead
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
