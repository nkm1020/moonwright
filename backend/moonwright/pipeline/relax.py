"""Solver-grounded advice when the fleet provably cannot finish (phase 3c).

Contract:

    suggest_relaxations(req, graph, failure, opts=None, *, locked=None, kinds=None,
                        reserved_ids=None, budget_s=6.0) -> list[Suggestion]
    format_suggestions(result) -> list[str]        # CLI lines; [] when there is nothing to say

When a plan ends without a verified schedule (repairs exhausted, the repair loop off, a brief
the fleet cannot serve) the planner says which *resource* change would make the LAST graph it
was answered feasible. Nothing is guessed: every candidate is a modified copy of the
requirements (the inputs are never mutated) that CP-SAT
(:func:`moonwright.pipeline.schedule.solve_schedule`) solves for real, on one worker so ties
between equally good schedules break the same way on every call, and only a candidate the
solver actually scheduled becomes a :class:`moonwright.schemas.Suggestion` with
``verified=True``. Each solve gets at most :data:`SOLVE_LIMIT_S`; the whole call stops at
``budget_s`` (candidates left untried are simply not reported).

Candidates are derived from the conflict ids of ``failure`` (the verdict on ``graph``):

  extend_horizon   when the verdict names the deadline (``horizon``, ``horizon_path:<chain>``)
                   or a timing group (``precedence:<i>-><j>``, ``zone:<z>``, ``robot:<r>``), or
                   names nothing at all: solve with the horizon lifted (:data:`HORIZON_LIFT` x,
                   at least long enough for a fully serial schedule); the makespan found is the
                   deadline that works -> "finishing needs 96 h, 24 h past the 72 h deadline".
  add_robot        one more robot of a type the verdict blames: the type of every
                   ``energy:<robot>`` / ``energy:<type>`` / ``robot:<robot>`` id, the busiest
                   :data:`BUSIEST_TYPES` types (hours per robot) on ``horizon``, the task's type
                   on ``capability:<task>``. The new robot clones the type's largest battery
                   (ties: first declared; in a replan the batteries are what is left, so the
                   clone is a conservative spare), its id is the type's id prefix plus the next
                   number free in the fleet and in ``reserved_ids`` (the robots a replan has
                   lost: EX1, EX2 -> EX3), and it is solved under the ORIGINAL horizon ->
                   "one more excavator (EX3, 5000 Wh like EX1): feasible at 62 h". A type the
                   fleet lacks gets a robot sized to what its tasks charge it.
  raise_battery    for every robot an energy conflict names (``energy:<robot>``; the type's
                   largest battery for the validator's ``energy:<type>`` and
                   ``energy_task:<task>``): solve with that battery lifted to the schema's cap,
                   read back what that schedule charges the robot *in the planning model*
                   (work plus the transit allowance of every task it took, rounded up to
                   :data:`BATTERY_STEP_WH`: a battery that is certainly enough), then search
                   the :data:`BATTERY_STEP_WH` grid between the battery it has and that figure
                   for the smallest one CP-SAT still schedules (feasibility is monotone in a
                   battery, so a binary search is exact) -> "EX1 needs 5950 Wh (has 5000 Wh):
                   feasible at 62 h". The makespan-optimal lifted schedule often piles more
                   work on the lifted robot than it needs, so without the search the sentence
                   over-claimed by whole tasks ("EX2 needs 7800 Wh" where 5200 Wh works).
                   When the search runs out of time the last proven figure is reported as
                   "a battery of X Wh on EX2 is enough", never as a need.
  reduce_scope     not a solver matter: when every candidate above was tried and refuted, or
                   none applied, a single unverified suggestion names the largest work items
                   per structure so a human knows what to cut.

The result holds at most :data:`MAX_SUGGESTIONS`, best first: changes that keep the original
deadline (``raise_battery``, ``add_robot``) before ``extend_horizon``, the smaller relative
change first (a battery raised by 19 % before one more robot in a fleet of two), then the
shorter makespan. ``kinds`` restricts what is tried: a replan passes ``("extend_horizon",
"add_robot")`` because a battery cannot be raised mid-mission and the remaining work is not
up for re-scoping here. The call returns ``[]`` when the graph is broken beyond resources
(cycles, references, duplicates, quantities, structure coverage, the scope guards of a
replan, a schema or transport failure, a solver timeout): no resource change fixes a graph.

Three outcomes per verification solve, kept apart: a schedule (the candidate is proven), an
infeasibility (the candidate is refuted and counts as *tried*), and a timeout (CP-SAT answered
UNKNOWN within the time it had: the candidate is neither, it is not blamed, and
``reduce_scope`` is never emitted on a call that had one, because "none of the changes tried
makes this graph feasible" would be a claim nothing proved). A solve is not started with less
than :data:`MIN_SOLVE_S` of the budget left.

A replan runs the planner in relative hours (time 0 = the event, ``horizon_h`` = the hours
left); :func:`rebase_suggestion` moves a suggestion's figures and sentence to absolute
simulator hours against the brief's deadline and states a spare with a full battery, which is
sound because every figure moves the same way (``moonwright.sim.replan`` applies it).
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import Any

from moonwright.geometry import travel_hours
from moonwright.pipeline.schedule import (
    SolveOptions,
    _instance,  # the planning model's own energy accounting (work + transit allowance)
    check_schedule,
    solve_schedule,
)
from moonwright.schemas import (
    MAX_ENERGY_WH,
    MAX_HORIZON_H,
    MWH_PER_WH,
    Failure,
    PlanResult,
    Requirements,
    Robot,
    Schedule,
    ScheduleEntry,
    Suggestion,
    TaskGraph,
    fmt_wh,
    to_mwh,
)

KINDS: tuple[str, ...] = ("extend_horizon", "add_robot", "raise_battery", "reduce_scope")
SOLVE_LIMIT_S = 2.0        # CP-SAT budget per verification solve
MIN_SOLVE_S = 0.1          # no solve is started with less of the call's budget left
MAX_SUGGESTIONS = 4
HORIZON_LIFT = 20          # extend_horizon probes with the horizon multiplied by this
BATTERY_STEP_WH = 50       # raise_battery figures are rounded up to this
BUSIEST_TYPES = 2          # robot types proposed for add_robot on a bare ``horizon`` verdict

# verdict kinds a resource change can answer; anything else means the graph itself is wrong
RESOURCE_CONFLICTS = frozenset({"energy", "energy_task", "horizon", "horizon_path", "robot",
                                "zone", "precedence", "capability"})
TIMING_CONFLICTS = frozenset({"horizon", "horizon_path", "precedence", "zone", "robot"})
ADVISABLE_FAILURES = frozenset({"infeasible", "validation"})

_TYPE_PREFIX = {"excavator": "EX", "hauler": "HL", "compactor": "CP", "printer": "PR",
                "surveyor": "SV", "crane": "CR"}
_ID_TAIL = re.compile(r"^(.*?)(\d+)$")


# ----------------------------------------------------------------------------- public API
def suggest_relaxations(
    req: Requirements,
    graph: TaskGraph,
    failure: Failure,
    opts: SolveOptions | None = None,
    *,
    locked: list[ScheduleEntry] | None = None,
    kinds: Collection[str] | None = None,
    reserved_ids: Collection[str] | None = None,
    budget_s: float = 6.0,
) -> list[Suggestion]:
    """Resource changes that would make ``graph`` feasible, each proven by a CP-SAT solve on a
    modified copy of ``req`` (module docstring). ``failure`` is the verdict on ``graph``;
    ``opts`` the options the planner solved with (time limit replaced, one worker); ``locked``
    the pinned entries of a replan; ``kinds`` the suggestion kinds allowed (default: all);
    ``reserved_ids`` robot ids a spare must not take (the robots a replan has lost);
    ``budget_s`` the wall-clock budget of the whole call. Deterministic for a given input as
    long as the budget is not hit."""
    wanted = set(KINDS if kinds is None else kinds)
    unknown = wanted - set(KINDS)
    if unknown:
        raise ValueError(f"unknown suggestion kind(s): {sorted(unknown)}")
    if failure.kind not in ADVISABLE_FAILURES:
        return []
    if any(c.partition(":")[0] not in RESOURCE_CONFLICTS for c in failure.conflicts):
        return []   # the graph is wrong beyond resources: no battery or spare fixes that
    opts = opts or SolveOptions()
    pins = list(locked or [])
    candidates = _candidates(req, graph, failure.conflicts, wanted, reserved_ids or ())

    deadline = time.monotonic() + budget_s
    found: list[_Found] = []
    tried: list[str] = []      # candidates CP-SAT proved or refuted
    exhausted = False          # the budget ran out before every candidate had its solve
    unknown = False            # a solve timed out: that candidate is neither proven nor refuted
    for cand in candidates:
        if _limit(deadline) < MIN_SOLVE_S:
            exhausted = True
            break
        try:
            hit = _VERIFIERS[cand.kind](req, graph, opts, pins, deadline, cand)
        except _Unknown:
            unknown = True
            continue
        tried.append(cand.label)
        if hit is not None:
            found.append(hit)
    found.sort(key=_rank)
    out = [f.suggestion for f in found[:MAX_SUGGESTIONS]]
    if not out and not exhausted and not unknown and "reduce_scope" in wanted:
        out = [_reduce_scope(graph, tried)]
    return out


def format_suggestions(result: PlanResult) -> list[str]:
    """CLI lines for a failed plan: a heading plus one bullet per suggestion; ``[]`` for a
    successful plan or when there is nothing to say."""
    if result.success or not result.suggestions:
        return []
    lines = ["what would make it feasible:"]
    for s in result.suggestions:
        proof = "CP-SAT verified" if s.verified else "not solver-verified"
        lines.append(f"  - {s.kind.replace('_', ' ')}: {s.detail} [{proof}]")
    return lines


# ----------------------------------------------------------------------------- candidates
@dataclass(frozen=True)
class _Candidate:
    kind: str
    target: str = ""     # robot id (raise_battery) or robot type (add_robot)
    label: str = ""      # how the reduce_scope sentence names it
    robot_id: str = ""   # add_robot: the id the spare would get


@dataclass(frozen=True)
class _Found:
    """A verified suggestion with its rank: tier 0 keeps the original deadline, tier 1 moves
    it; ``change`` is the relative size of the change (smaller first)."""

    suggestion: Suggestion
    tier: int
    change: float


def _rank(found: _Found) -> tuple[int, float, int, str, str]:
    s = found.suggestion
    return (found.tier, found.change, s.makespan_h or 0, s.kind, s.robot_id or s.robot_type or "")


def _candidates(req: Requirements, graph: TaskGraph, conflicts: list[str], wanted: set[str],
                reserved: Collection[str]) -> list[_Candidate]:
    """The changes worth a solve for these conflict ids, in evaluation order: the deadline
    first (one solve, the most telling figure), then batteries, then spares."""
    robots = {r.id: r for r in req.robots}
    tasks = {t.id: t for t in graph.tasks}
    timing = not conflicts   # a verdict without a core: the deadline is the one thing to probe
    raise_ids: list[str] = []
    add_types: list[str] = []
    for conflict in conflicts:
        kind, _, arg = conflict.partition(":")
        if kind in TIMING_CONFLICTS:
            timing = True
        if kind == "horizon":
            add_types += _busiest_types(req, graph)
        elif kind == "robot" and arg in robots:
            add_types.append(robots[arg].type)
        elif kind == "energy":
            if arg in robots:                      # the solver names a robot
                raise_ids.append(arg)
                add_types.append(robots[arg].type)
            elif req.robots_of_type(arg):          # the validator names a type
                raise_ids.append(_largest(req, arg).id)
                add_types.append(arg)
        elif kind == "energy_task" and arg in tasks and req.robots_of_type(tasks[arg].robot_type):
            raise_ids.append(_largest(req, tasks[arg].robot_type).id)
        elif kind == "capability" and arg in tasks:
            add_types.append(tasks[arg].robot_type)
    out: list[_Candidate] = []
    if timing and "extend_horizon" in wanted:
        out.append(_Candidate("extend_horizon", label="a later deadline"))
    if "raise_battery" in wanted:
        out += [_Candidate("raise_battery", rid, f"a bigger battery on {rid}")
                for rid in dict.fromkeys(raise_ids)]
    if "add_robot" in wanted:
        out += [_Candidate("add_robot", typ, f"one more {typ}", new_robot_id(req, typ, reserved))
                for typ in dict.fromkeys(add_types)]
    return out


def _busiest_types(req: Requirements, graph: TaskGraph) -> list[str]:
    """The :data:`BUSIEST_TYPES` staffed robot types with the most hours per robot."""
    hours: dict[str, int] = {}
    for t in graph.tasks:
        hours[t.robot_type] = hours.get(t.robot_type, 0) + t.duration_h
    loads = [(-math.ceil(h / len(req.robots_of_type(typ))), typ)
             for typ, h in hours.items() if req.robots_of_type(typ)]
    return [typ for _, typ in sorted(loads)[:BUSIEST_TYPES]]


def _largest(req: Requirements, robot_type: str) -> Robot:
    """The robot of ``robot_type`` with the largest battery (ties: first declared)."""
    return max(req.robots_of_type(robot_type), key=lambda r: r.battery_wh)


def new_robot_id(req: Requirements, robot_type: str, reserved: Collection[str] = ()) -> str:
    """The id of one more robot of ``robot_type``: the prefix its peers use most (EX1, EX2 ->
    EX3; a type without peers or numbered ids takes a conventional prefix such as PR for a
    printer) plus the next number free in the whole fleet and in ``reserved`` (ids a replan
    must not hand out again, such as the robot that failed)."""
    taken = {r.id for r in req.robots} | set(reserved)
    peers = req.robots_of_type(robot_type)
    tails = [m for r in peers if (m := _ID_TAIL.match(r.id)) is not None]
    prefixes = Counter(m.group(1) for m in tails)
    if prefixes:
        prefix = max(prefixes, key=lambda p: (prefixes[p], -list(prefixes).index(p)))
        numbers = [int(m.group(2)) for m in tails if m.group(1) == prefix]
        n = max(numbers) + 1
    else:
        prefix = _TYPE_PREFIX.get(robot_type) or re.sub(r"[^A-Za-z0-9_.-]", "",
                                                        robot_type[:2].upper()) or "R"
        n = 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


# ----------------------------------------------------------------------------- verification
class _Unknown(Exception):
    """A verification solve ran out of time: the candidate is neither proven nor refuted."""


def _limit(deadline: float) -> float:
    """Seconds the next solve may take: :data:`SOLVE_LIMIT_S`, or what is left of the budget."""
    return min(SOLVE_LIMIT_S, deadline - time.monotonic())


def _solve(req: Requirements, graph: TaskGraph, opts: SolveOptions, locked: list[ScheduleEntry],
           limit: float) -> Schedule | None:
    """One verification solve: the planner's options with ``limit`` seconds, one worker (ties
    break the same way every time) and no infeasibility explanation (a refuted candidate needs
    no core). The schedule when CP-SAT found one, None when it proved there is none, and
    :class:`_Unknown` when it ran out of time (a ``solver_timeout`` verdict proves nothing)."""
    out = solve_schedule(req, graph, replace(opts, time_limit_s=limit, num_workers=1,
                                             explain_infeasibility=False), locked=locked)
    if isinstance(out, Schedule):
        return out
    if out.kind == "solver_timeout":
        raise _Unknown(out.hint or "the verification solve timed out")
    return None


def _needed_wh(req: Requirements, graph: TaskGraph, opts: SolveOptions,
               locked: list[ScheduleEntry], schedule: Schedule, robot_id: str) -> float:
    """What ``schedule`` charges ``robot_id`` in the planning model, work plus the transit
    allowance of every task it took, rounded up to :data:`BATTERY_STEP_WH`: the smallest
    budget under which CP-SAT admits that schedule again."""
    inst = _instance(req, graph, opts, locked)
    use = 0
    for entry in schedule.entries:
        i = inst.index.get(entry.task)
        if entry.robot != robot_id or i is None:
            continue
        transit = inst.transit.get((i, robot_id))
        use += inst.energy[i] + (transit.allowance_mwh if transit is not None else 0)
    step = BATTERY_STEP_WH * MWH_PER_WH
    return float(max(1, -(-use // step)) * BATTERY_STEP_WH)


def _with_battery(req: Requirements, robot_id: str, battery_wh: float) -> Requirements:
    robots = [r.model_copy(update={"battery_wh": battery_wh}) if r.id == robot_id else r
              for r in req.robots]
    return req.model_copy(update={"robots": robots})


def _serial_hours(req: Requirements, graph: TaskGraph, opts: SolveOptions,
                  locked: list[ScheduleEntry]) -> int:
    """Hours a fully serial schedule needs at most: every task after a leg across the whole
    grid at the slowest speed, from the latest start the inputs impose."""
    diagonal = math.hypot(req.site.grid[0], req.site.grid[1])
    slowest = min(r.speed_cells_per_h for r in req.robots)
    base = max([max(0, opts.start_offset_h), *(e.start_h for e in locked)])
    return base + sum(t.duration_h + travel_hours(diagonal, slowest) for t in graph.tasks)


def _extend_horizon(req: Requirements, graph: TaskGraph, opts: SolveOptions,
                    locked: list[ScheduleEntry], deadline: float, _cand: _Candidate) -> _Found | None:
    lifted = min(MAX_HORIZON_H, max(HORIZON_LIFT * req.horizon_h,
                                    _serial_hours(req, graph, opts, locked)))
    if lifted <= req.horizon_h:
        return None
    schedule = _solve(req.model_copy(update={"horizon_h": lifted}), graph, opts, locked,
                      _limit(deadline))
    if schedule is None or schedule.makespan_h <= req.horizon_h:
        return None
    late = schedule.makespan_h - req.horizon_h
    proof = ("CP-SAT optimal" if schedule.status == "optimal"
             else "feasible; a shorter deadline may exist")
    detail = _extend_detail(schedule.makespan_h, req.horizon_h, proof)
    return _Found(Suggestion(kind="extend_horizon", detail=detail, makespan_h=schedule.makespan_h,
                             horizon_h=schedule.makespan_h, verified=True),
                  tier=1, change=late / req.horizon_h)


def _add_robot(req: Requirements, graph: TaskGraph, opts: SolveOptions,
               locked: list[ScheduleEntry], deadline: float, cand: _Candidate) -> _Found | None:
    robot_type, new_id = cand.target, cand.robot_id
    peers = req.robots_of_type(robot_type)
    if peers:
        base = _largest(req, robot_type)
        clone = base.model_copy(update={"id": new_id})
        schedule = _solve(req.model_copy(update={"robots": [*req.robots, clone]}), graph, opts,
                          locked, _limit(deadline))
        if schedule is None:
            return None
        battery = clone.battery_wh
        detail = (f"one more {robot_type} ({new_id}, {fmt_wh(to_mwh(battery))} Wh like "
                  f"{base.id}): feasible at {schedule.makespan_h} h")
        change = 1.0 / len(peers)
    else:   # a type the fleet lacks: probe with an unlimited battery, then size it to its use
        probe = Robot(id=new_id, type=robot_type, battery_wh=MAX_ENERGY_WH)
        req2 = req.model_copy(update={"robots": [*req.robots, probe]})
        schedule = _solve(req2, graph, opts, locked, _limit(deadline))
        if schedule is None:
            return None
        battery = _needed_wh(req2, graph, opts, locked, schedule, new_id)
        if check_schedule(_with_battery(req2, new_id, battery), graph, schedule, opts):
            return None
        detail = (f"a {robot_type} ({new_id}, {fmt_wh(to_mwh(battery))} Wh; the fleet has none): "
                  f"feasible at {schedule.makespan_h} h")
        change = 1.0
    return _Found(Suggestion(kind="add_robot", detail=detail, makespan_h=schedule.makespan_h,
                             robot_type=robot_type, robot_id=new_id, battery_wh=battery,
                             verified=True),
                  tier=0, change=change)


def _raise_battery(req: Requirements, graph: TaskGraph, opts: SolveOptions,
                   locked: list[ScheduleEntry], deadline: float, cand: _Candidate) -> _Found | None:
    robot_id = cand.target
    robot = req.robot(robot_id)
    lifted = _with_battery(req, robot_id, MAX_ENERGY_WH)
    schedule = _solve(lifted, graph, opts, locked, _limit(deadline))
    if schedule is None:
        return None
    enough = _needed_wh(lifted, graph, opts, locked, schedule, robot_id)
    if to_mwh(enough) <= to_mwh(robot.battery_wh):
        return None
    # the lifted solve charged the robot whatever its makespan-optimal schedule put on it,
    # often whole tasks more than the smallest battery that works: search down to it
    needed, schedule, minimal = _smallest_battery(req, graph, opts, locked, robot_id,
                                                  robot.battery_wh, enough, schedule, deadline)
    if check_schedule(_with_battery(req, robot_id, needed), graph, schedule, opts):
        return None   # the independent checker disagrees: never claim it
    have = fmt_wh(to_mwh(robot.battery_wh))
    if minimal:
        detail = (f"{robot_id} needs {fmt_wh(to_mwh(needed))} Wh (has {have} Wh): feasible at "
                  f"{schedule.makespan_h} h")
    else:
        detail = (f"a battery of {fmt_wh(to_mwh(needed))} Wh on {robot_id} is enough (has {have} "
                  f"Wh; a smaller one may be, the search ran out of time): feasible at "
                  f"{schedule.makespan_h} h")
    return _Found(Suggestion(kind="raise_battery", detail=detail, makespan_h=schedule.makespan_h,
                             robot_type=robot.type, robot_id=robot_id, battery_wh=needed,
                             verified=True),
                  tier=0, change=(needed - robot.battery_wh) / robot.battery_wh)


def _smallest_battery(req: Requirements, graph: TaskGraph, opts: SolveOptions,
                      locked: list[ScheduleEntry], robot_id: str, have_wh: float,
                      enough_wh: float, schedule: Schedule, deadline: float,
                      ) -> tuple[float, Schedule, bool]:
    """Binary search on the :data:`BATTERY_STEP_WH` grid between ``have_wh`` (refuted by the
    verdict) and ``enough_wh`` (proven by ``schedule``): the smallest grid value CP-SAT still
    schedules, with that schedule, and whether the search finished. A bigger battery admits
    every schedule a smaller one does, so the search is exact; a probe that runs out of time
    (or would start with less than :data:`MIN_SOLVE_S`) ends it and the last proven figure
    stands as "enough", not as the need."""
    step = BATTERY_STEP_WH * MWH_PER_WH
    lo_k = to_mwh(have_wh) // step          # at or below the battery it has: refuted
    hi_k = to_mwh(enough_wh) // step        # proven
    best = schedule
    while hi_k - lo_k > 1:
        if _limit(deadline) < MIN_SOLVE_S:
            return hi_k * BATTERY_STEP_WH, best, False
        mid_k = (lo_k + hi_k) // 2
        try:
            probe = _solve(_with_battery(req, robot_id, mid_k * BATTERY_STEP_WH), graph, opts,
                           locked, _limit(deadline))
        except _Unknown:
            return hi_k * BATTERY_STEP_WH, best, False
        if probe is None:
            lo_k = mid_k
        else:
            hi_k, best = mid_k, probe
    return hi_k * BATTERY_STEP_WH, best, True


def _extend_detail(makespan_h: int, deadline_h: int, proof: str) -> str:
    return (f"finishing needs {makespan_h} h, {makespan_h - deadline_h} h past the {deadline_h} h "
            f"deadline ({proof})")


_EXTEND_RE = re.compile(r"^finishing needs \d+ h, \d+ h past the \d+ h deadline \((?P<proof>.*)\)$")
_FEASIBLE_AT_RE = re.compile(r"feasible at \d+ h$")
_SPARE_RE = re.compile(r"\((?P<id>[A-Za-z0-9_.-]+), (?P<wh>[\d.]+) Wh"
                       r"(?P<how> like [A-Za-z0-9_.-]+|; the fleet has none)\)")


def rebase_suggestion(s: Suggestion, *, shift_h: int, deadline_h: int,
                      spare_batteries: Mapping[str, float] | None = None) -> Suggestion:
    """``s`` as a replan reports it: the planner's relative hours moved ``shift_h`` later
    (absolute simulator hours), the deadline an ``extend_horizon`` names replaced by the
    brief's ``deadline_h`` (its ``horizon_h`` becomes the absolute deadline that works) and,
    with ``spare_batteries`` (robot type -> the battery a fresh robot of that type ships with,
    the original fleet's), an ``add_robot`` stated with that full battery instead of the
    remaining charge the replan's fleet was cloned from. Every rewrite is sound: a schedule
    feasible at m relative hours is feasible at m + shift absolute hours, and a spare with a
    bigger battery admits every schedule the smaller one did (the battery is only ever
    raised). ``reduce_scope`` and an unrecognised sentence are returned as they are."""
    update: dict[str, Any] = {}
    detail = s.detail
    if s.makespan_h is not None:
        makespan = s.makespan_h + shift_h
        update["makespan_h"] = makespan
        detail = _FEASIBLE_AT_RE.sub(f"feasible at {makespan} h", detail)
        if s.kind == "extend_horizon":
            m = _EXTEND_RE.match(s.detail)
            detail = _extend_detail(makespan, deadline_h,
                                    m.group("proof") if m else "CP-SAT verified")
            update["horizon_h"] = makespan
    if (s.kind == "add_robot" and spare_batteries and s.robot_type in spare_batteries
            and s.battery_wh is not None):
        full = float(spare_batteries[s.robot_type])
        if to_mwh(full) >= to_mwh(s.battery_wh):
            update["battery_wh"] = full
            wh = fmt_wh(to_mwh(full))
            detail = _SPARE_RE.sub(
                lambda m: f"({m.group('id')}, {wh} Wh, a full battery"
                          f"{'; the fleet has none' if m.group('how').startswith(';') else ''})",
                detail, count=1)
    return s.model_copy(update={**update, "detail": detail}) if update or detail != s.detail else s


_VERIFIERS = {
    "extend_horizon": _extend_horizon,
    "add_robot": _add_robot,
    "raise_battery": _raise_battery,
}


# ----------------------------------------------------------------------------- reduce_scope
def _reduce_scope(graph: TaskGraph, tried: list[str]) -> Suggestion:
    head = (f"none of the changes tried ({_join_and(tried)}) makes this graph feasible on its "
            "own" if tried else "no single resource change answers this verdict")
    return Suggestion(kind="reduce_scope", verified=False,
                      detail=f"{head[0].upper()}{head[1:]}; the largest work items are "
                             f"{_largest_work(graph)}, so cut or defer scope there.")


def _largest_work(graph: TaskGraph, structures: int = 3) -> str:
    """The biggest task of each of the ``structures`` most energy-hungry structures (by the
    tasks' ``structure`` tag), quantities and units included."""
    groups: dict[str, list] = {}
    for t in graph.tasks:
        groups.setdefault(t.structure or "untagged work", []).append(t)
    ordered = sorted(groups, key=lambda s: (-sum(to_mwh(t.energy_wh) for t in groups[s]), s))
    items = []
    for structure in ordered[:structures]:
        top = max(groups[structure], key=lambda t: (to_mwh(t.energy_wh), t.duration_h))
        size = f"{top.quantity:g} {top.unit}, " if top.quantity is not None and top.unit else ""
        items.append(f"{top.id} for {structure} ({size}{top.duration_h} h, "
                     f"{fmt_wh(to_mwh(top.energy_wh))} Wh)")
    return _join_and(items)


def _join_and(parts: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]
