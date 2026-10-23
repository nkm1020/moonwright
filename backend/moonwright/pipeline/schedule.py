"""F4: CP-SAT scheduling with explainable infeasibility.

Contract:

    solve_schedule(req, graph, opts=None, locked=None) -> Schedule | Failure
    check_schedule(req, graph, schedule, opts=None) -> list[str]   # independent re-verification
    sequence_travel(req, graph, schedule, opts=None) -> {robot id: RobotTravel}   # the legs driven
    budget_rows(req, graph, failure, opts=None, locked=None) -> [{kind, subject, robots,
        budget_wh, demanded_wh, travel_wh}]   # the energy figures behind a verdict, as numbers
                                              # (kind "type", "robot" or "order")

Model (PRD section "스케줄링 모델"):
  x[i,r]  BoolVar for every task i and every robot r with r.type == task.robot_type
  s[i]    IntVar start, [start_offset_h, horizon_h]; end = s + duration
  a[r,k,l] BoolVar per arc of robot r's circuit: task l is the next r runs after k ("Travel")
  exactly one robot per task                       hard
  x[i,r] => s_i >= start_offset_h + first_leg(i,r) hard   (the robot must be able to get there)
  one circuit per robot over its start and tasks   hard   (the order it runs them in)
  precedence s_j >= s_i + d_i for each edge        soft group  precedence:<i>-><j>
  a[r,k,l] => s_l >= s_k + d_k + leg_h(k,l), plus
    a redundant no-overlap and timeline row        soft group  robot:<r>
  per-zone no-overlap (optional intervals)         soft group  zone:<z>
  sum_i e_i x[i,r] + sum_(k,l) leg_wh(k,l) a[r,k,l]
    <= battery_wh[r]                               soft group  energy:<r>
  C_max >= end_i, C_max <= horizon_h               soft group  horizon
  objective: minimise C_max (work end: the makespan is when the last task finishes)
"Soft group" = every constraint in the group is only_enforce_if(g) on one group literal, and the
literals go to model.add_assumptions(...). Interval presence literals for the no-overlap
groups must be p = x AND g (new BoolVar + implications) because add_no_overlap itself takes no
enforcement literal.

Travel
------
Travel is exact and sequence-dependent (phase 3k): CP-SAT picks each robot's order of tasks
and times and prices the legs of that order, so "optimal" is optimal for the driving the
robots really do. Seen live: given the requirements and the verified task graphs of 21 run-3
plans, Nemotron 3 Ultra wrote schedules that :func:`check_schedule` accepted and that beat
CP-SAT's "optimal" makespan on 9 of them (four_mast_pads 43 h against 60 h, shielding_mound
36 h against 45 h; ``scripts/llm_schedule_check.py``), because the solver reserved before
every task the longest leg its robot could ever drive to that zone, even between two tasks
in one zone, and charged that allowance's energy for every task. On the same graphs the
solver now finishes at or below the model every time (four_mast_pads 41 h, shielding_mound
36 h). Per robot r there is one ``AddCircuit`` over node 0, where r is at ``start_offset_h``
(``SolveOptions.robot_positions`` for a replan, which passes the simulator frame, the depot
otherwise; free to set off then, or at its ``SolveOptions.robot_ready_h`` hour), and one node
per task r may run:

* a task node's self-loop is NOT x[i,r] (r does not run the task); node 0's is idle[r], true
  exactly when r runs nothing;
* an arc k -> l (literal a[r,k,l]) puts l right after k on r's timeline and
  a[r,k,l] => s_l >= s_k + d_k + leg_h(zone_k, zone_l), enforced on [a, robot group literal]
  in the soft model so that robot:<r> keeps covering ordering and travel: ``leg_h`` is
  ``ceil(dist / speed_r)`` whole hours between zone centres (:func:`moonwright.geometry
  .travel_hours`, the rule :func:`check_schedule` and the simulator use), 0 within one zone;
* an arc 0 -> l needs no constraint of its own: it implies x[l,r], and the hard first leg
  x[l,r] => s_l >= start_offset_h + leg_h(start_r, zone_l), kept for every assigned task (a
  valid bound whatever r does first), times it; from a later, fractional set-off hour (a
  replan, :func:`moonwright.geometry.arrival_hour`) the wait and the leg are rounded up
  together. The arc l -> 0 closing the circuit is free (no return leg, as before);
* energy:<r> charges every arc taken, ``leg_wh`` = dist x TRAVEL_WH_PER_CELL in whole
  milli-Wh per leg, rounded up (:func:`moonwright.geometry.leg_mwh`): exactly what
  :func:`check_schedule` charges the same sequence, and never less than the simulator drains
  driving it.

The circuit itself is unconditional (it only orders). Two redundant constraints under
robot:<r> help the solver prove a makespan: a per-robot NoOverlap over ``[s_i, s_i + d_i]``
(it propagates) and one linear row saying that the robot's work plus the leg hours of its arcs
end by the makespan (it bounds). Group names are unchanged, so cores, hints and repair prompts
read as before. A lock that starts at or before ``start_offset_h`` (a replan's running task)
is the task its robot is on: node 0 is its zone, the arc 0 -> it is forced and charges no leg.
A later lock is a stop like any other with a pinned start: the robot may work before it, and
the leg into it is timed and charged (the 3k review found the earliest lock of a robot taken
to be the task it is on whatever its start, which forbade earlier work and drove it there
for free). A robot whose tasks all lie in one zone centre gets
no circuit, since it has no order to pick and no leg between its tasks: the no-overlap times
it exactly, and its energy row pays the first leg once if it runs anything (busy[r]). Zone
no-overlap is unchanged (travel happens outside zones), and the makespan stays the end of the
last task. Zone centres come from :func:`moonwright.geometry.zone_positions` (declared zones
plus the deterministic layout of the ones only tasks use), exactly as the simulator lays them
out. Since the simulator leaves for the next zone as soon as a robot is free and starts work
at max(scheduled start, arrival), a disturbance-free replay of a solver schedule starts every
task on time; a ``task_overrun`` there is a genuine anomaly, not travel drift.

Solve strategy:
  1. normal solve (opts.num_workers, time limit). OPTIMAL/FEASIBLE -> Schedule. Exact travel
     leaves many optimal schedules and the parallel workers race to one of them (the
     landing-pad fixture got 3 different 80 h schedules in 30 solves, and its EX2-at-30 h
     replan had 9 or 11 tasks to reschedule depending on which), so an optimal makespan is
     then settled (:func:`_settle`): one more solve holds the makespan and minimises the sum of
     the start times, on CP-SAT's portfolio interleaved in a fixed order (``interleave_search``,
     the same answer whatever the number of workers) within a deterministic time budget
     (:data:`SETTLE_DETERMINISTIC_S`) and what is left of the time limit. Whenever it finds a
     schedule in that budget the same graph gets the same schedule (the fixtures and the
     run-3 four_mast_pads graph, where a single settling worker found none and 12 solves gave
     7 schedules); otherwise the first, just as optimal, is returned, and its tasks keep the
     race's starts. Status, makespan and bound stay those of the first solve. A solve on one
     worker, or with ``SolveOptions.deterministic`` (the interleaved portfolio from the start:
     Monte Carlo replans and the relaxation advice), breaks ties the same way every time
     already and is not settled.
  2. INFEASIBLE -> re-solve with assumptions, num_workers=1 (cores are only produced there);
     read solver.sufficient_assumptions_for_infeasibility() -> group names.
     If opts.minimize_core: shrink by deletion (drop a group, re-solve; keep it out while still
     infeasible) so the core is minimal and small enough to be a useful repair hint.
     Before it, the likely cores (:func:`_likely_cores`: a robot type's batteries together)
     are hard trials of their own, which settle a pool shortfall in milliseconds where the
     assumption solve can time out on the circuits. When the single-worker assumption solve
     times out, start from every group instead and let the deletion filter isolate the core;
     every re-solve of the filter is a hard feasibility trial on all workers (see below).
     After the first core its groups are disabled and up to ``MAX_CORES - 1`` further,
     disjoint cores are looked for the same way, each search opening with one hard trial on
     every group left (most often they fit together, which a trial tells far sooner than the
     assumption solve), so one repair round sees every independent conflict;
     ``Failure.conflicts`` is their union. The whole explanation shares one wall-clock budget
     (opts.explain_time_limit_s).
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
* The start domain is ``[start_offset_h, base + sum(durations + longest legs)]`` rather than
  ``[.., horizon_h]`` so the horizon is enforced *only* through the ``horizon`` group; otherwise
  a long precedence chain would look infeasible through the variable bounds and the core would
  blame the precedence edges instead of the horizon. In the hard model ``C_max <= horizon_h``
  tightens the bounds at the root, so nothing is lost. The domain leaves room for a fully
  serial schedule with the longest leg any capable robot could drive into every task, so the
  hard first-leg constraints alone can never make the model infeasible and every core is a set
  of soft groups (a pinned start a robot cannot reach is the one exception, and the precheck
  reports it before any model is built, see ``locked`` below).
* Groups that can never be violated are not created (a zone or robot with fewer than two
  candidate tasks, a robot whose candidate tasks fit its battery even all together, each with
  the longest leg it could drive to it). They would never appear in a core anyway and leaving
  them out keeps the model small; a robot with neither group gets no circuit either, since
  nothing would read its order. A robot with n candidate tasks in two zones or more adds
  n * (n + 1) arc literals, plus n * (n - 1) timed arcs and one timeline row when it has a
  robot group and one energy row when it has an energy group; arcs into a locked task the
  robot is on, and out of its start to any other task, are never created.
* Every hard model (``soft=False``) also starts twins in graph order: tasks with the same
  robot type, zone, duration, energy, predecessors and successors (:func:`_twins`; a phase
  split in equal parts, as the task-graph prompt asks for), counting only the precedence edges
  the model builds. Swapping two twins keeps every constraint, so no makespan is lost and no
  trial of the deletion filter changes its verdict, and CP-SAT stops proving the same bound
  once per permutation (the canonical "fine" graph of four_mast_pads: 7 s to 1 s to optimal;
  at a horizon one hour short, the fallback trial now proves it infeasible and a core comes
  back where none did). The assumption model goes without: a core found there must hold
  without anything it does not name.
* Energies are integer milli-Wh (:func:`moonwright.schemas.to_mwh`, i.e. ``round(x * 1000)``)
  for task energies and batteries, legs rounded up per leg (:func:`moonwright.geometry
  .leg_mwh`): the one fixed-point unit the
  validator, this model and :func:`check_schedule` share, so the three agree to 1e-3 Wh and a
  fractional budget (replans hand over 3-dp task energies and 2-dp remaining batteries) is
  neither rejected by rounding up nor accepted by rounding down. Hints quote the figures the
  model used as whole Wh with thousands separators ("9,930 Wh"), the travel share called out
  when it is worth a Wh (the least travel any order needs, one robot's cheapest route or the
  cheapest tree for several: a lower bound like every figure a hint states); a fractional
  budget gets just the decimals that keep the two figures apart (see the "hints" section).
  When a robot's tasks fit its battery in their cheapest order, the hint names the order
  the rest of the core holds it to instead (the "order" form). :func:`budget_rows` returns
  the same figures as numbers.
* ``locked``: an entry pins ``x[task, robot] = 1`` and ``start == entry.start_h`` (absolute hours,
  the entry's ``end_h`` is ignored; the graph's duration counts). A locked robot that cannot do
  the task by type is treated as capable for that pair. Locks naming a task that is not in the
  graph or a robot that is not in the requirements are ignored. A robot's earliest lock that
  starts at or before ``start_offset_h`` is the task it is on: its circuit starts there, its
  start position is that task's zone (``robot_positions``, if given for it, must say the same
  for :func:`check_schedule` to agree, as a replan's frame does) and it drives no leg into it.
  Any other lock is a stop of its robot's circuit with a pinned start: its first leg is timed
  and charged like any task's, and a lock its robot cannot reach by its pinned start (nor the
  task it is on, when ``robot_ready_h`` frees the robot only later) is reported by the
  precheck as ``robot:<r>`` (:func:`_unkept_locks`), since the model would refuse it on a
  hard constraint no core can name.
* :func:`check_schedule` reports the shared conflict ids plus four of its own for defects a
  solver output cannot have but a hand-written schedule can: ``missing:<task>`` (not scheduled),
  ``unknown:<task>`` (entry for a task that is not in the graph), ``duration:<task>``
  (``end_h - start_h != duration_h``) and ``makespan`` (``makespan_h`` is not the latest
  ``end_h`` of the entries). It checks the *actual* legs of each robot's sequence
  (:func:`sequence_travel`, which the verification receipt prints too): consecutive entries
  must be a whole ``travel_hours`` apart (``robot:<r>`` otherwise) and the energy per robot
  includes the travel of that sequence (``energy:<r>``). Those are the legs the solver's
  circuit times and charges, computed on their own here, so a solver schedule always passes.
  With ``opts`` given, the first leg of every robot (from ``opts.robot_positions`` or the
  depot, starting at ``opts.start_offset_h``) is timed and charged as well; without it the
  robots' start positions are unknown and only the legs between tasks are checked.
"""

from __future__ import annotations

import itertools
import math
import time
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

from moonwright.geometry import (
    Cell,
    Point,
    arrival_hour,
    cheapest_legs,
    distance,
    leg_mwh,
    route_cost,
    spanning_cost,
    start_positions,
    travel_hours,
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
LIKELY_CORE_S = 1.0  # time limit of each likely-core trial while explaining (_likely_cores)
SETTLE_DETERMINISTIC_S = 0.2  # deterministic-time budget of the settling solve ("Solve strategy")
SETTLE_MIN_S = 0.05           # no settling solve is started with less wall time left


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
    # it can drive there from this position, and its circuit charges that leg.
    robot_positions: dict[str, tuple[float, float]] | None = None
    # The hour (absolute, like ``start_offset_h``, fractions allowed) from which each robot
    # listed may set off from that position; the others may from ``start_offset_h``. A replan
    # at a fractional hour lists every robot that is not on a locked task at the fraction:
    # planner hour 0 is floor(t_h), but the simulator resumes at t_h (``moonwright.sim
    # .replan``), so a first leg is timed from when the robot really is free
    # (:func:`moonwright.geometry.arrival_hour`).
    robot_ready_h: dict[str, float] | None = None
    # Interleave CP-SAT's portfolio in a fixed order (``interleave_search``) instead of racing
    # its workers: the same inputs always give the same schedule, whatever ``num_workers``, at
    # some speed. Monte Carlo replans and the relaxation advice solve this way; the production
    # solve races and then settles (module docstring, "Solve strategy").
    deterministic: bool = False


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

    ``locked`` pins tasks to a robot and a start (absolute hours; the graph's duration
    counts). A lock that starts at or before ``opts.start_offset_h`` is the task its robot is
    on: the robot stands in that task's zone and runs it first, with no leg into it, so
    ``opts.robot_positions``, if it places that robot, must place it there (a replan passes
    the frame, which does). A later lock is a stop like any other with its start pinned: the
    robot may run other tasks before it, and its leg into it is timed and charged. A lock its
    robot cannot reach by its start is an ``infeasible`` verdict naming ``robot:<r>``.
    """
    opts = opts or SolveOptions()
    inst = _instance(req, graph, opts, locked or [])
    failure = _precheck(inst)
    if failure is not None:
        return failure

    began = time.monotonic()
    built = _build(inst, soft=False, minimize=True)
    solver, status = _run(built, workers=opts.num_workers, time_limit_s=opts.time_limit_s,
                          deterministic=opts.deterministic)
    if status == cp_model.OPTIMAL:
        first = _extract(inst, built, solver, status)
        if opts.deterministic or opts.num_workers <= 1:
            return first   # its search breaks ties the same way on every call already
        return _settle(inst, first, opts.time_limit_s - (time.monotonic() - began))
    if status == cp_model.FEASIBLE:
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
    robot starts (``robot_positions`` or the depot, at ``start_offset_h`` or its later
    ``robot_ready_h`` hour), so the first leg of every robot is timed and charged as well.
    Without it the start positions are unknown and only the legs between consecutive tasks are
    checked. It knows nothing of locks: a robot :func:`solve_schedule` takes to be on a locked
    task stands in that task's zone, and ``opts`` must place it there (a replan's frame does).
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
    leg at :data:`~moonwright.geometry.TRAVEL_WH_PER_CELL`, rounded up to a milli-Wh as
    :func:`moonwright.geometry.leg_mwh` charges it) and how many of them do not fit between
    their entries in whole hours (``late_legs``: the ``robot:<r>`` defect)."""

    mwh: int = 0
    late_legs: int = 0


def sequence_travel(
    req: Requirements, graph: TaskGraph, schedule: Schedule, opts: SolveOptions | None = None
) -> dict[str, RobotTravel]:
    """Per scheduled robot of ``req``, the travel of its actual sequence of entries (sorted by
    start): with ``opts`` the first leg from where the robot starts (``opts.robot_positions``
    or the depot, free from ``opts.start_offset_h`` or its ``opts.robot_ready_h`` hour), then
    zone to zone; without ``opts`` the start position is unknown and only the legs between
    entries count. Entries naming unknown tasks or robots are skipped (``check_schedule``
    reports them)."""
    robots = {r.id: r for r in req.robots}
    tasks = {t.id: t for t in graph.tasks}
    layout = zone_positions(req.site, (t.zone for t in graph.tasks))
    starts = None if opts is None else start_positions(req, opts.robot_positions)
    origin = 0 if opts is None else max(0, opts.start_offset_h)
    ready = {} if opts is None else opts.robot_ready_h or {}   # fractional in a replan
    by_robot: dict[str, list[ScheduleEntry]] = defaultdict(list)
    for entry in schedule.entries:
        if entry.task in tasks and entry.robot in robots:
            by_robot[entry.robot].append(entry)
    out: dict[str, RobotTravel] = {}
    for robot_id, entries in by_robot.items():
        robot = robots[robot_id]
        here = None if starts is None else starts[robot_id]
        free_h, mwh, late = max(origin, ready.get(robot_id, origin)), 0, 0
        for entry in sorted(entries, key=lambda e: (e.start_h, e.end_h)):
            there = layout[tasks[entry.task].zone]
            if here is not None:
                mwh += leg_mwh(here, there)
                if entry.start_h < arrival_hour(free_h, distance(here, there),
                                                robot.speed_cells_per_h):
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
      travel its robots need to reach every zone of it>, "travel_wh": <that travel>}``: one row
      per type of two robots or more whose work exceeds the pool, or fits it but no split of
      it fits the single batteries, however many of its robots the core names;
    * ``{"kind": "robot", "subject": <robot id>, "robots": [<robot id>], "budget_wh": <its
      battery>, "demanded_wh": <the tasks only it can run, plus the least travel to reach
      them>, "travel_wh": <that travel>}``: one row per robot whose own forced tasks exceed its
      battery;
    * ``{"kind": "order", ...}``, the fields of a "robot" row: the only robot of its type,
      whose tasks fit its battery in their cheapest order (``demanded_wh`` is within
      ``budget_wh``) but not in the order the rest of the core holds it to (phase 3k).

    The travel is the least any order and split of the tasks needs (:func:`_travel_floor`), so
    every ``demanded_wh`` is a lower bound. Wh to the milli-Wh: exactly the figures the hint
    sentences quote (rounded there). ``[]`` when the failure has no energy conflict; conflicts
    naming a robot ``req`` does not have are skipped.
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
class _Route:
    """What one robot can drive (module docstring, "Travel"). Node 0 is ``origin``, where the
    robot is at ``start_offset_h``: the zone of the locked task it is on (a lock that starts at
    or before ``start_offset_h``), else its ``robot_positions`` entry or the depot. Node k >= 1
    is the zone of ``tasks[k - 1]``, the tasks it may run in graph order. ``hours[u][v]`` and
    ``mwh[u][v]`` are the leg from node u to node v, in whole hours at the robot's speed and in
    milli-Wh (0 within one zone), except that ``hours[0][v]`` counts from ``start_offset_h``
    to the robot's arrival, so it holds the wait for a later ``robot_ready_h`` hour as well
    (:func:`moonwright.geometry.arrival_hour`). ``start_h`` is where its timeline begins:
    ``start_offset_h``, or the pinned start of the lock it is on; ``on`` is that lock's node,
    which nothing precedes."""

    origin: Point
    start_h: int
    tasks: list[int]
    nodes: dict[int, int]        # task position -> node
    hours: list[list[int]]
    mwh: list[list[int]]
    on: int | None

    @property
    def on_task(self) -> int | None:
        """The position of the locked task the robot is on (node ``on``), if any."""
        return None if self.on is None else self.tasks[self.on - 1]

    def tails(self, v: int) -> list[int]:
        """The nodes an arc into task node ``v`` can come from: any other node, except that the
        task the robot is on comes from the start and nothing else does."""
        if self.on is not None:
            return [0] if v == self.on else [u for u in self.nodes.values() if u != v]
        return [u for u in range(len(self.tasks) + 1) if u != v]

    def longest_in(self, table: list[list[int]], i: int) -> int:
        """The longest leg (``table``: ``hours`` or ``mwh``) the robot could drive into task
        ``i``'s zone, whatever it ran before: the most that leg can cost in any order."""
        v = self.nodes[i]
        return max(table[u][v] for u in self.tails(v))


@dataclass(frozen=True)
class _Instance:
    """The problem pre-digested into integer data, capability sets, routes and the lock table."""

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
    cells: list[Cell]                   # per task: the centre of its zone
    routes: dict[str, _Route]           # robot id -> what it can drive
    start_lb: int                       # earliest start (start_offset_h)
    start_ub: int                       # latest start that still leaves room for every task

    def first_leg_h(self, i: int, robot_id: str) -> int:
        """Hours after ``start_offset_h`` before ``robot_id`` can be at task ``i``'s zone at all:
        the leg from its origin, from when it may set off (:class:`_Route`); 0 for the locked
        task it is on, whose zone it is in already."""
        route = self.routes[robot_id]
        if i == route.on_task:
            return 0
        return route.hours[0][route.nodes[i]]


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
    cells, routes = _routes(req, tasks, capable, locks, opts, start_lb)
    base = max([start_lb, req.horizon_h, *(e.start_h for e in locks.values())])
    room = sum(t.duration_h + max((routes[r].longest_in(routes[r].hours, i) for r in capable[i]),
                                  default=0)
               for i, t in enumerate(tasks))
    return _Instance(
        req=req, opts=opts, tasks=tasks, index=index, capable=capable, locked=locks, edges=edges,
        edge_groups=edge_groups,
        energy=[to_mwh(t.energy_wh) for t in tasks],
        battery={r.id: to_mwh(r.battery_wh) for r in req.robots},
        cells=cells, routes=routes,
        start_lb=start_lb, start_ub=base + room,
    )


def _routes(
    req: Requirements, tasks: list[Task], capable: list[list[str]],
    locks: dict[int, ScheduleEntry], opts: SolveOptions, start_lb: int,
) -> tuple[list[Cell], dict[str, _Route]]:
    """Every task's zone centre and every robot's :class:`_Route` (module docstring,
    "Travel"): its origin and the legs between it and the zones of the tasks it may run. The
    task a robot is on is its earliest lock that starts at or before ``start_lb``; any other
    lock is a task like any other, with a pinned start."""
    layout = zone_positions(req.site, (t.zone for t in tasks))
    cells = [layout[t.zone] for t in tasks]
    starts = start_positions(req, opts.robot_positions)
    ready = opts.robot_ready_h or {}
    on: dict[str, int] = {}   # robot id -> the locked task it is on
    for i, lock in sorted(locks.items(), key=lambda item: (item[1].start_h, item[0])):
        if lock.start_h <= start_lb:
            on.setdefault(lock.robot, i)
    routes: dict[str, _Route] = {}
    for robot in req.robots:
        mine = [i for i in range(len(tasks)) if robot.id in capable[i]]
        nodes = {i: k for k, i in enumerate(mine, start=1)}
        if robot.id in on:   # working in that zone since the lock's start
            origin, start_h, free_h = cells[on[robot.id]], locks[on[robot.id]].start_h, start_lb
        else:
            origin, start_h = starts[robot.id], start_lb
            free_h = max(start_lb, ready.get(robot.id, start_lb))
        points = [origin, *(cells[i] for i in mine)]
        speed = robot.speed_cells_per_h
        hours = [[travel_hours(distance(p, q), speed) for q in points] for p in points]
        hours[0] = [arrival_hour(free_h, distance(origin, q), speed) - start_lb for q in points]
        routes[robot.id] = _Route(
            origin=origin, start_h=start_h, tasks=mine, nodes=nodes, hours=hours,
            mwh=[[leg_mwh(p, q) for q in points] for p in points],
            on=nodes[on[robot.id]] if robot.id in on else None,
        )
    return cells, routes


def _precheck(inst: _Instance) -> Failure | None:
    """Structural problems the validator normally catches first, and locks their robots
    cannot keep (:func:`_unkept_locks`): reported instead of crashing, or instead of a model
    that a hard constraint alone makes infeasible, which no core could name."""
    conflicts: list[str] = []
    for i, task in enumerate(inst.tasks):
        if not inst.capable[i]:
            conflicts.append(f"capability:{task.id}")
        for pred_id in task.after:
            if pred_id not in inst.index:
                conflicts.append(f"reference:{task.id}.after={pred_id}")
    unkept = _unkept_locks(inst)
    if not conflicts and not unkept:
        return None
    sentences = [_hints(inst, sorted(set(conflicts)))] if conflicts else []
    sentences += [sentence for _, sentence in unkept]
    core = sorted({*conflicts, *(f"robot:{robot_id}" for robot_id, _ in unkept)})
    return Failure(kind="infeasible", conflicts=core, hint=" ".join(sentences))


def _unkept_locks(inst: _Instance) -> list[tuple[str, str]]:
    """``(robot id, sentence)`` for every lock whose pinned start comes before its robot can be
    in the task's zone: a later lock before the robot has set off and driven there
    (:meth:`_Instance.first_leg_h`), the lock a robot is on before the ``robot_ready_h`` hour
    the options give it. No schedule keeps such a lock, and :func:`check_schedule` would call
    any that did ``robot:<r>``, so that is the conflict."""
    out: list[tuple[str, str]] = []
    ready = inst.opts.robot_ready_h or {}
    for i, lock in sorted(inst.locked.items()):
        route = inst.routes[lock.robot]
        if i == route.on_task:
            earliest = arrival_hour(ready.get(lock.robot, lock.start_h), 0.0, 1.0)
        else:
            earliest = inst.start_lb + inst.first_leg_h(i, lock.robot)
        if lock.start_h < earliest:
            sentence = (f"{inst.tasks[i].id} is locked to {lock.robot} from {_h(lock.start_h)}, "
                        f"but {lock.robot} cannot be in its zone before {_h(earliest)}.")
            out.append((lock.robot, sentence))
    return out


# ----------------------------------------------------------------------------- model
@dataclass
class _Model:
    model: cp_model.CpModel
    assign: dict[tuple[int, str], cp_model.IntVar]   # (task position, robot id) -> x[i, r]
    start: list[cp_model.IntVar]                     # s[i]
    groups: dict[str, cp_model.IntVar]               # group name -> literal (soft model only)
    makespan: cp_model.IntVar                        # C_max


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
    if not soft:  # a hard model: twins start in graph order (see _twins)
        for twins in _twins(inst, enabled):
            for i, j in itertools.pairwise(twins):
                model.add(start[i] <= start[j])

    # makespan: the end of the last task (its horizon bound comes last, as a group of its own)
    max_d = max(t.duration_h for t in tasks)
    makespan = model.new_int_var(0, inst.start_ub + max_d, "makespan")
    model.add_max_equality(makespan, [start[i] + tasks[i].duration_h for i in range(len(tasks))])

    # per robot: first legs, then its sequence (module docstring, "Travel"): a circuit through
    # the tasks it runs, timed under robot:<r> and priced under energy:<r>
    for robot in inst.req.robots:
        route = inst.routes[robot.id]
        x = {k: assign[i, robot.id] for i, k in route.nodes.items()}   # task node -> x[i, r]
        for i, k in route.nodes.items():  # no robot is at a zone before it can drive there
            lead = inst.first_leg_h(i, robot.id)
            if lead:
                model.add(start[i] >= inst.start_lb + lead).only_enforce_if(x[k])
        most = sum(inst.energy[i] + route.longest_in(route.mwh, i) for i in route.tasks)
        energy_g = switch(f"energy:{robot.id}") if most > inst.battery[robot.id] else False
        robot_g = switch(f"robot:{robot.id}") if len(route.tasks) >= 2 else False
        if energy_g is False and robot_g is False:
            continue  # nothing reads this robot's order
        # every task it may run in one zone centre: no order to pick and no leg between them
        single = len({inst.cells[i] for i in route.tasks}) == 1
        arcs = {} if single else _circuit(model, route, x, robot.id, tasks)
        if robot_g is not False:
            for (u, v), a in arcs.items():
                if u and v:  # out of the start: the first leg above; back to it: free
                    i, j = route.tasks[u - 1], route.tasks[v - 1]
                    gap = tasks[i].duration_h + route.hours[u][v]
                    model.add(start[j] >= start[i] + gap).only_enforce_if(
                        a if robot_g is True else [a, robot_g])
            # redundant, for the bound: the work and every leg the robot drives, the first one
            # included, end by the makespan (Monte Carlo replans of four_mast_pads run-3 #1
            # prove optimal in half the time, the slowest in 1.1 s instead of 3 s)
            terms = [(x[k], tasks[i].duration_h) for i, k in route.nodes.items()]
            terms += [(a, route.hours[u][v]) for (u, v), a in arcs.items()
                      if v and route.hours[u][v]]
            busy_h = cp_model.LinearExpr.weighted_sum([t for t, _ in terms],
                                                      [c for _, c in terms])
            add(busy_h <= makespan - route.start_h, robot_g)
            intervals = []  # redundant with the timed arcs, but it propagates
            for i, k in route.nodes.items():
                if robot_g is True:
                    present = x[k]
                else:  # present <=> x AND g
                    present = model.new_bool_var(f"p[{tasks[i].id},{robot.id}]")
                    model.add_bool_and([x[k], robot_g]).only_enforce_if(present)
                    model.add_bool_or([x[k].negated(), robot_g.negated(), present])
                d = tasks[i].duration_h
                intervals.append(model.new_optional_interval_var(
                    start[i], d, start[i] + d, present, f"iv[{tasks[i].id},{robot.id}]"))
            model.add_no_overlap(intervals)
        if energy_g is not False:  # every leg driven, the first one included; never the way back
            terms = [(x[k], inst.energy[i]) for i, k in route.nodes.items()]
            terms += [(a, route.mwh[u][v]) for (u, v), a in arcs.items() if v and route.mwh[u][v]]
            if single and route.mwh[0][1]:  # its one leg, paid if it runs anything at all
                busy = model.new_bool_var(f"busy[{robot.id}]")
                for lit in x.values():
                    model.add_implication(lit, busy)
                terms.append((busy, route.mwh[0][1]))
            used = cp_model.LinearExpr.weighted_sum([t for t, _ in terms], [c for _, c in terms])
            add(used <= inst.battery[robot.id], energy_g)

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

    # horizon
    add(makespan <= inst.req.horizon_h, switch("horizon"))
    if minimize:
        model.minimize(makespan)
    return _Model(model=model, assign=assign, start=start, groups=groups, makespan=makespan)


def _twins(inst: _Instance, enabled: Collection[str] | None = None) -> list[list[int]]:
    """Classes of interchangeable tasks, each in graph order: the same robot type, zone,
    duration, energy, predecessors and successors and none of them locked (a phase split in
    equal parts, as the task-graph prompt asks for). Swapping two twins' robots and starts
    keeps every constraint, so any schedule can be relabelled to start them in graph order.
    Only the precedence edges ``enabled`` builds count (every one when None): with one twin's
    edge left out of a deletion-filter trial and the other's kept, they are twins no more."""
    preds: list[set[int]] = [set() for _ in inst.tasks]
    succs: list[set[int]] = [set() for _ in inst.tasks]
    for i, j in inst.edges:
        if enabled is not None and _edge_name(inst.tasks, i, j) not in enabled:
            continue
        succs[i].add(j)
        preds[j].add(i)
    classes: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for i, t in enumerate(inst.tasks):
        if i not in inst.locked:
            classes[t.robot_type, t.zone, t.duration_h, inst.energy[i], frozenset(preds[i]),
                    frozenset(succs[i])].append(i)
    return [twins for twins in classes.values() if len(twins) > 1]


def _circuit(
    model: cp_model.CpModel, route: _Route, x: dict[int, cp_model.IntVar], robot_id: str,
    tasks: list[Task],
) -> dict[tuple[int, int], cp_model.IntVar]:
    """The robot's sequence as one ``AddCircuit`` over its route (module docstring, "Travel"):
    a task node's self-loop is NOT x (the robot does not run it), the start's is idle[r] (it
    runs nothing). Returns the arc literals by (tail, head) node: k -> l puts task node l
    right after k, k -> 0 closes the circuit. The locked task the robot is on is the forced
    first node."""
    names = ["start", *(tasks[i].id for i in route.tasks)]
    arcs: dict[tuple[int, int], cp_model.IntVar] = {}
    for v in route.nodes.values():
        for u in route.tails(v):
            arcs[u, v] = model.new_bool_var(f"a[{robot_id},{names[u]}->{names[v]}]")
    for u in route.nodes.values():
        arcs[u, 0] = model.new_bool_var(f"a[{robot_id},{names[u]}->start]")
    loops = [(k, k, lit.negated()) for k, lit in x.items()]
    if route.on is None:
        idle = model.new_bool_var(f"idle[{robot_id}]")
        loops.append((0, 0, idle))
        for lit in x.values():  # a circuit through tasks alone would skip the start
            model.add_implication(lit, idle.negated())
    else:
        model.add(arcs[0, route.on] == 1)
    model.add_circuit([*loops, *((u, v, a) for (u, v), a in arcs.items())])
    return arcs


def _run(
    built: _Model, *, workers: int, time_limit_s: float, deterministic: bool = False
) -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus]:
    """Solve ``built`` on ``workers`` within ``time_limit_s``; ``deterministic`` interleaves
    the portfolio instead of racing it (:attr:`SolveOptions.deterministic`). One worker is
    deterministic as it is, and faster without interleaving."""
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = workers
    solver.parameters.max_time_in_seconds = time_limit_s
    if deterministic and workers > 1:
        solver.parameters.interleave_search = True
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


def _settle(inst: _Instance, first: Schedule, time_left_s: float) -> Schedule:
    """The settled schedule of the optimal makespan of ``first`` (module docstring, "Solve
    strategy"): the production model with the makespan held there and the sum of the start
    times minimised, by the interleaved portfolio within :data:`SETTLE_DETERMINISTIC_S` of
    deterministic time, so the schedule depends on the inputs alone (the best sum the budget
    reached, not always the least). ``first`` itself, just as optimal, when no settled schedule
    turns up within the budget or ``time_left_s`` (what is left of the solve's time limit)."""
    if time_left_s < SETTLE_MIN_S:
        return first
    built = _build(inst, soft=False)
    built.model.add(built.makespan <= first.makespan_h)
    built.model.minimize(cp_model.LinearExpr.sum(built.start))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = inst.opts.num_workers
    solver.parameters.interleave_search = True
    solver.parameters.max_deterministic_time = SETTLE_DETERMINISTIC_S
    solver.parameters.max_time_in_seconds = time_left_s
    status = solver.solve(built.model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return first
    settled = _extract(inst, built, solver, cp_model.OPTIMAL)
    return settled.model_copy(update={"objective_bound_h": first.objective_bound_h,
                                      "solve_time_s": first.solve_time_s + solver.wall_time})


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


def _likely_cores(inst: _Instance, enabled: Collection[str]) -> list[list[str]]:
    """Group sets that are often a core on their own and cheap to try as hard trials: the
    ``energy:<r>`` groups of each robot type in ``enabled``, together (one robot's tasks that
    no order fits, a type whose work exceeds its pool, a split no battery takes). Their models
    have no timing at all, so a trial settles them in milliseconds where the single-worker
    assumption solve over every group can time out (the 3k review: the run-3 four_mast_pads
    graph twice over, its excavators 650 Wh short, went 15.4 s and a 32-group core with
    precedence edges in it, against 0.05 s for the trial on the two batteries)."""
    by_type: dict[str, list[str]] = defaultdict(list)
    for robot in inst.req.robots:
        if f"energy:{robot.id}" in enabled:
            by_type[robot.type].append(f"energy:{robot.id}")
    return [groups for _, groups in sorted(by_type.items())]


def _find_core(
    inst: _Instance, enabled: list[str], deadline: float, *, proven: bool
) -> list[str] | None:
    """One conflict core among the ``enabled`` groups; None when the model restricted to them
    is feasible or the budget ran out before infeasibility could be confirmed. ``proven``: the
    hard model with these groups is known to be infeasible (the production solve, for the
    first core); otherwise one hard trial on all of them decides first, which tells groups
    that fit together apart far faster than the assumption solve.

    Then the likely cores (:func:`_likely_cores`), each a short hard trial. Then the
    assumption solve, which usually names a small sufficient subset in milliseconds. When it
    times out (one worker cannot always prove what the multi-worker production solve refuted),
    the fallback confirms that a hard trial still proves infeasibility within the budget left
    and lets the deletion filter isolate the core from every group; the fallback always
    minimises because the full set is not an explanation.
    """
    if not proven and (_trial(inst, enabled, time_limit_s=_time_slice(inst, deadline))
                       != cp_model.INFEASIBLE):
        return None  # these groups are satisfiable together (or not provably not, in time)
    for likely in _likely_cores(inst, enabled):
        limit = min(LIKELY_CORE_S, _time_slice(inst, deadline))
        if _trial(inst, likely, time_limit_s=limit) == cp_model.INFEASIBLE:
            if inst.opts.minimize_core:
                return _minimize_core(inst, likely, deadline=deadline)
            return likely
    status, core, assumed = _core_solve(inst, enabled, time_limit_s=_time_slice(inst, deadline))
    if status == cp_model.INFEASIBLE:
        core = core or assumed  # no core reported: start from every group and let deletion shrink
        if inst.opts.minimize_core:
            core = _minimize_core(inst, core, deadline=deadline)
        return core
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None  # these groups are satisfiable together
    if proven and (_trial(inst, assumed, time_limit_s=_time_slice(inst, deadline))
                   != cp_model.INFEASIBLE):
        return None
    return _minimize_core(inst, assumed, deadline=deadline)


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
    all_groups = sorted(_build(inst, soft=True).groups)   # every buildable group
    disabled: set[str] = set()
    for _ in range(MAX_CORES):
        enabled = [g for g in all_groups if g not in disabled]
        if not enabled:
            break
        core = _find_core(inst, enabled, deadline, proven=not cores)
        if core is None:
            break
        cores.append(core)
        disabled.update(core)
        if not core:
            break  # infeasible without any soft group: nothing left to disable
    if not cores:
        return Failure(kind="infeasible", hint="The solver proved the plan infeasible but "
                       "could not isolate the conflicting constraints within the "
                       f"{inst.opts.explain_time_limit_s:g} s explanation budget.")
    union = sorted(set().union(*cores))
    return Failure(kind="infeasible", conflicts=union, hint=_hints(inst, union, cores))


# ----------------------------------------------------------------------------- hints
# Copy rules (phase 3d; a mission planner reads the hint on the trace, the repair prompt reads
# it verbatim): whole-number figures with thousands separators and a unit ("9,930 Wh", "62 h"),
# real plurals, no nested parentheses, and one sentence per conflict, except that the robots of
# one type whose conflict has the same pool-form explanation share one sentence that lists them.
# Every figure a repair needs (budget, demand, travel share, pool) stays in the sentence;
# :func:`budget_rows` hands the energy figures over as numbers, from the very rows the
# sentences are rendered from, so prose and table can never disagree. Every figure is a sound
# lower bound (phase 3k): the travel a sentence counts is the least any order and split of the
# tasks needs (:func:`_travel_floor`), so the demand it belongs to reads "at least".
def _hints(inst: _Instance, core: list[str], cores: list[list[str]] | None = None) -> str:
    """The human hint for ``core``: one sentence per conflict in core order, pool-form energy
    and robot sentences of one type merged into one (emitted where the group's first conflict
    stands), and so is a ``robot:<r>`` conflict that only ties an order to time (no
    ``horizon`` beside it) with the energy sentence that names the order. ``cores``, the
    disjoint cores ``core`` is the union of, tell which groups conflict together; without
    them every group of ``core`` counts as one conflict."""
    if not core:
        return "The plan is infeasible even with every soft constraint relaxed."
    merged: dict[str, tuple[int, str]] = {}   # conflict id -> (group number, its sentence)
    groups = 0
    for budget in _energy_budgets(inst, core, cores):
        groups += 1
        sentence = _energy_sentence(inst, budget)
        for robot_id in budget.named:
            merged[f"energy:{robot_id}"] = (groups, sentence)
        scope = _scope(core, cores, budget.robots)
        if budget.cause and "horizon" not in scope:
            # without a deadline beside it, robot:<r> is there to tie the order to time: the
            # energy sentence that names the order explains it, not a sentence about hours
            for robot_id in budget.robots:
                if f"robot:{robot_id}" in scope:
                    merged[f"robot:{robot_id}"] = (groups, sentence)
    for load in _robot_loads(inst, [c for c in core if c not in merged]):
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
    """', 30 Wh of it travel' when the travel share is worth a Wh, else ''. The share is the
    least travel any order of the tasks needs (:func:`_travel_floor`), so the demand it is part
    of reads "at least"."""
    return f", {_wh(mwh)} of it travel" if _whole_wh(mwh) >= 1 else ""


def _transit_note(hours: int) -> str:
    """' plus at least 7 h of transit': the least driving any order of the tasks needs."""
    return f" plus at least {_h(hours)} of transit" if hours else ""


def _forced_tasks(inst: _Instance, robot_id: str) -> list[int]:
    """Tasks that must run on ``robot_id``: locked to it, or it is the only capable robot."""
    return [i for i in range(len(inst.tasks))
            if inst.capable[i] == [robot_id]
            or (i in inst.locked and inst.locked[i].robot == robot_id)]


def _shared_tasks(inst: _Instance, robot_type: str) -> list[int]:
    """The tasks the robots of ``robot_type`` share: the type's own, except one locked to a
    robot of another type, plus one of another type locked to a robot of this one."""
    return [i for i, t in enumerate(inst.tasks)
            if (inst.req.robot(inst.locked[i].robot).type if i in inst.locked
                else t.robot_type) == robot_type]


def _travel_floor(inst: _Instance, robot_ids: list[str], tasks: list[int], *,
                  hours: bool) -> int:
    """The least travel any schedule needs for the robots ``robot_ids`` to reach the zone of
    every task in ``tasks`` from where they start, whatever order and split CP-SAT picks: in
    whole hours at the fastest of their speeds (``hours``) or in milli-Wh, priced on the
    cheapest chains of legs through every zone they may stop at. One robot: its cheapest
    route through those zones (:func:`moonwright.geometry.route_cost`); several: the cheapest
    tree joining the zones to their origins (:func:`moonwright.geometry.spanning_cost`), since
    the split is theirs to pick. A sound lower bound either way, which is what lets a hint
    state it."""
    routes = [inst.routes[r] for r in robot_ids]
    if hours:
        speed = max(inst.req.robot(r).speed_cells_per_h for r in robot_ids)

        def leg(p: Point, q: Point) -> int:
            return travel_hours(distance(p, q), speed)
    else:
        leg = leg_mwh
    origins = [route.origin for route in routes]
    cost = cheapest_legs([*origins, *(inst.cells[i] for r in routes for i in r.tasks)], leg)
    zones = [inst.cells[i] for i in tasks]
    if len(origins) == 1:
        return route_cost(origins[0], zones, cost)
    return spanning_cost(origins, zones, cost)


def _order_cause(inst: _Instance, core: Collection[str], robot_ids: Collection[str],
                 ) -> str | None:
    """What holds ``robot_ids`` to an order of their tasks that their batteries cannot drive,
    as a clause for "the order ...": the groups of ``core`` that time their arcs, "the
    precedence edges force", "the deadline forces", "the precedence edges and the shared
    zones force", or the pinned start of a task locked to one of them that it is not already
    on. None when ``core`` names none of their ``robot:<r>`` groups, the only ones that tie an
    order to time (module docstring, "Travel"): without them every robot may drive its
    cheapest order, or when nothing else in ``core`` times one."""
    if not any(f"robot:{r}" in core for r in robot_ids):
        return None
    kinds = {c.partition(":")[0] for c in core}
    causes = [(phrase, plural) for kind, phrase, plural in (
        ("precedence", "the precedence edges", True), ("horizon", "the deadline", False),
        ("zone", "the shared zones", True)) if kind in kinds]
    pinned = [inst.tasks[i].id for i, lock in sorted(inst.locked.items())
              if lock.robot in robot_ids and i != inst.routes[lock.robot].on_task]
    if pinned:
        causes.append((f"the locked start of {_join_and(pinned)}", False))
    if not causes:
        return None
    plural = len(causes) > 1 or causes[0][1]
    return f"{_join_and([phrase for phrase, _ in causes])} {'force' if plural else 'forces'}"


# -- energy: the budget rows behind energy:<robot> conflicts, and their sentences
@dataclass(eq=False)
class _Budget:
    """The figures behind one or more ``energy:<robot>`` conflicts, in milli-Wh. ``kind``
    "robot": the tasks only ``subject`` can run (locked to it, or it is the only robot of its
    type), with the least travel to reach them, already exceed its battery. ``kind`` "order":
    ``subject`` is the only robot of its type and its tasks fit its battery in their cheapest
    order, but not in the order ``cause`` holds it to (:func:`_order_cause`). ``kind`` "type":
    the ``subject`` type's work exceeds the pool of its robots' batteries, or fits it but no
    split of it fits the single batteries (in the orders ``cause`` holds them to, when set).
    ``named`` are the robots of the core the row explains; ``robots`` those whose batteries
    make up ``budget_mwh`` (the robot itself, or every robot of the type)."""

    kind: str
    subject: str
    named: list[str]
    robots: list[str]
    budget_mwh: int
    demanded_mwh: int
    travel_mwh: int
    cause: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "robots": list(self.robots),
                "budget_wh": _wh_value(self.budget_mwh),
                "demanded_wh": _wh_value(self.demanded_mwh),
                "travel_wh": _wh_value(self.travel_mwh)}


def _scope(core: list[str], cores: list[list[str]] | None, robot_ids: Collection[str],
           ) -> list[str]:
    """The groups that conflict together with the ``energy:<r>`` groups of ``robot_ids``: the
    cores of ``cores`` that hold one of them, or all of ``core`` without ``cores``."""
    if cores is None:
        return core
    mine = {f"energy:{r}" for r in robot_ids}
    return sorted({c for k in cores if mine.intersection(k) for c in k})


def _energy_budgets(inst: _Instance, core: list[str],
                    cores: list[list[str]] | None = None) -> list[_Budget]:
    """One :class:`_Budget` per robot-form or order-form ``energy:<robot>`` conflict of
    ``core`` and one per robot *type* of the pool-form ones, in order of first appearance;
    conflicts naming a robot the instance does not have are skipped. The figures are the
    model's own milli-Wh, the travel the least any order and split needs
    (:func:`_travel_floor`), so a hint never claims more than the verdict proves. A robot alone
    in its type runs every task it may run, so when they fit its battery in their cheapest
    order the conflict is the order (phase 3k: energy depends on it), never a split; what
    holds it to that order is read from the groups it conflicts with (:func:`_scope`)."""
    rows: list[_Budget] = []
    pools: dict[str, _Budget] = {}
    for conflict in core:
        kind, _, robot_id = conflict.partition(":")
        if kind != "energy" or robot_id not in inst.battery:
            continue
        robot = inst.req.robot(robot_id)
        budget = inst.battery[robot_id]
        forced = _forced_tasks(inst, robot_id)
        travel = _travel_floor(inst, [robot_id], forced, hours=False)
        need = sum(inst.energy[i] for i in forced) + travel
        if need > budget:
            rows.append(_Budget("robot", robot_id, [robot_id], [robot_id], budget, need, travel))
            continue
        peers = [r.id for r in inst.req.robots_of_type(robot.type)]
        if peers == [robot_id]:
            cause = _order_cause(inst, _scope(core, cores, peers), peers)
            rows.append(_Budget("order", robot_id, [robot_id], [robot_id], budget, need, travel,
                                cause=cause))
            continue
        pool = pools.get(robot.type)
        if pool is None:
            shared = _shared_tasks(inst, robot.type)
            travel = _travel_floor(inst, peers, shared, hours=False)
            have = sum(inst.battery[r] for r in peers)
            need = sum(inst.energy[i] for i in shared) + travel
            cause = None if need > have else _order_cause(inst, _scope(core, cores, peers), peers)
            pool = _Budget("type", robot.type, [], peers, have, need, travel, cause=cause)
            pools[robot.type] = pool
            rows.append(pool)
        pool.named.append(robot_id)
    return rows


def _energy_sentence(inst: _Instance, row: _Budget) -> str:
    """Robot form: "PR1 would need at least 4,023 Wh, 33 Wh of it travel, but has 4,000 Wh."
    Order form: "R1 has 327 Wh, which cannot cover its 300 Wh of work and the legs of the order
    the precedence edges force; the cheapest order alone drives 19 Wh." Type form: "EX1 and
    EX2 each have 5,000 Wh; the excavator tasks need at least 9,930 Wh in total, 30 Wh of it
    travel, and cannot be split across 2 excavators (10,000 Wh combined) without exceeding one
    budget.", with ", in the orders the precedence edges force," before "without" when an order
    is part of the conflict. Without a travel share worth a Wh the demand is the work alone
    and reads without "at least"."""
    travel = _travel_clause(row.travel_mwh)
    comma = "," if travel else ""
    least = "at least " if travel else ""
    if row.kind == "robot":
        decimals = _decimals_apart(row.demanded_mwh, row.budget_mwh)
        return (f"{row.subject} would need {least}{_wh(row.demanded_mwh, decimals)}{travel}"
                f"{comma} but has {_wh(row.budget_mwh, decimals)}.")
    if row.kind == "order":
        work = row.demanded_mwh - row.travel_mwh
        decimals = _decimals_apart(row.budget_mwh, work) if row.budget_mwh > work else 0
        legs = f"the legs of the order {row.cause}" if row.cause else "the legs it has to drive"
        cheapest = (f"; the cheapest order alone drives {_wh(row.travel_mwh)}"
                    if _whole_wh(row.travel_mwh) >= 1 else "")
        return (f"{row.subject} has {_wh(row.budget_mwh, decimals)}, which cannot cover its "
                f"{_wh(work, decimals)} of work and {legs}{cheapest}.")
    budgets = [inst.battery[r] for r in row.named]
    if len(row.named) > 1 and len(set(budgets)) == 1:
        who = f"{_join_and(row.named)} each have {_wh(budgets[0])}"
    else:
        who = _join_and([f"{r} has {_wh(b)}" for r, b in zip(row.named, budgets, strict=True)])
    fleet = _plural(len(row.robots), row.subject)
    need = f"the {row.subject} tasks need {least}{_wh(row.demanded_mwh)} in total{travel}"
    if row.demanded_mwh > row.budget_mwh:
        return (f"{who}; {need}, more than the {_wh(row.budget_mwh)} the {fleet} have "
                "combined.")
    orders = f", in the orders {row.cause}," if row.cause else ""
    return (f"{who}; {need}{comma} and cannot be split across {fleet} "
            f"({_wh(row.budget_mwh)} combined){orders} without exceeding one budget.")


# -- robot time: the hours behind robot:<robot> conflicts, and their sentences
@dataclass(eq=False)
class _Load:
    """The hours behind ``robot:<robot>`` conflicts: ``kind`` "robot" when the ``tasks`` only
    ``subject`` can run already take ``hours`` back to back, "type" when the ``subject`` type's
    ``tasks`` take ``hours`` across its ``count`` robots; ``transit_h`` is the least driving on
    top (:func:`_travel_floor`)."""

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
                              _travel_floor(inst, [robot_id], forced, hours=True)))
            continue
        pool = pools.get(robot.type)
        if pool is None:
            peers = [r.id for r in inst.req.robots_of_type(robot.type)]
            shared = _shared_tasks(inst, robot.type)
            pool = _Load("type", robot.type, [], len(peers), len(shared),
                         sum(inst.tasks[i].duration_h for i in shared),
                         _travel_floor(inst, peers, shared, hours=True))
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
            into[i] += min((inst.first_leg_h(i, r) for r in inst.capable[i]), default=0)
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
