"""F8: event -> replan the remaining work -> continue the simulation.

Contract (implemented by the simulation agent):

    req2, graph2, context = build_replan_inputs(req, graph, schedule, frame, event,
                                                originals=None, layout=None)
    record = replan_after_event(planner, req, graph, schedule, frame, event, key=key)
    result = run_with_replanning(planner, plan, injections, key=key, max_replans=2, dt_h=0.25)

build_replan_inputs:
  * robots: drop failed robots; battery_wh := remaining battery from the frame (floor 1 Wh;
    the frame rounds batteries *down* to 0.01 Wh, so the budget never exceeds reality)
  * horizon_h := original horizon - ceil(t_h)  (floor 1), see the time convention below
  * site: every zone the simulator has laid out (``layout``, :attr:`Simulator.layout`; by
    default the layout of the graph in force) is declared with its cell, so the planner's
    own layout of the *remaining* graph (:func:`moonwright.geometry.zone_positions` sorts the
    undeclared ids and hands them the first lattice cells) cannot drift from where the robots
    actually are: with undeclared zones a drifted layout made the transit allowance unsound
    and a locked robot's first leg non-zero, which the planner's re-verification raised on.
  * tasks: drop done tasks; tasks in progress on a healthy robot stay with remaining duration
    (ceil) and are passed as `locked` ScheduleEntry(robot, start=0) so they continue; tasks in
    progress on the failed robot are re-added in full; `after` edges to done tasks are removed.
    "Remaining" and "full" refer to the task's *original* definition (``originals``, the graph
    the plan started with plus anything a replan added or an escalated repair re-scoped),
    never to the already-reduced copy an earlier replan left in the graph: ``frame.progress``
    is overall progress, so ``original * (1 - progress)`` is exact however many replans a task
    lives through. :func:`run_with_replanning` keeps ``originals`` current: after a verified
    replan every task that was not underway takes the definition the replan settled on (a
    re-scoped bowl is the bowl from then on), while a task underway keeps its full original,
    because the graph only carries its reduced copy.
  * context: one paragraph for the LLM: time now, what finished, which robot failed and what it
    was doing, remaining budgets. The planner is called with prior_graph=graph2, context,
    variant="replan", start_offset_h=0, required_tasks=graph2.ids(), start_with_prior=True
    and the surviving robots' frame positions (sim converts back by adding floor(t_h)).

Solver first, Nemotron on infeasibility
  :func:`replan_after_event` is solver-first: the remaining graph goes straight to the
  validator and CP-SAT with the failed robot removed and the running tasks locked
  (``start_with_prior=True``), which takes milliseconds and needs no model. Only when that is
  provably infeasible (not enough battery or hours left for the work as it stands) does the
  planner's repair loop ask Nemotron to re-scope the work, with the solver's conflict core; the
  record says so (``ReplanRecord.escalated``) and the trace ends with one ``replan`` step
  whose message reads "CP-SAT rescheduled 11 tasks in 0.02 s, Nemotron not needed" or
  "infeasible (energy:EX1) -> escalated to Nemotron; ...". A planner whose client has no
  answer (the solver-only Monte Carlo planner, an offline fixture client without a recorded
  repair) ends the escalation with the solver's verdict and the previous plan stays in force.

run_with_replanning: simulate -> on a robot failure replan -> apply_new_plan -> continue, until
  completed or max_replans exhausted; returns the final SimResult with all frames/events/replans
  merged. Only event kinds in ``replan_on`` (default: robot_failure, which includes battery
  depletion) trigger a replan; task_overrun and battery_low are recorded and the run goes on.
  A robot failure only triggers a replan when the robot still owned work (a current task or
  queued entries): the failure of an idle, finished or never-scheduled robot changes nothing
  the plan relies on, so it is recorded and the run simply continues (see
  :func:`affects_work`).

Time convention (one place: :func:`replan_offset_h` and :func:`remaining_horizon_h`)
  The planner works in *relative* hours: ``start_offset_h=0``, locked entries starting at 0,
  ``horizon_h`` shrunk to the time left. :func:`replan_after_event` converts the schedule it
  gets back to absolute simulator time by adding ``replan_offset_h(t_h) == floor(t_h)``, so a
  locked entry starts at or before ``t_h`` and the robot working on it never pauses. The
  simulation itself resumes at ``t_h``, up to an hour after planner time 0, which is why the
  horizon is shrunk by ``ceil(t_h)``: a schedule that fits the shrunken horizon still meets the
  original deadline in simulator time. ``ReplanRecord.after.schedule`` therefore holds absolute
  times and can be handed to :meth:`Simulator.apply_new_plan` as-is, while
  ``ReplanRecord.after.requirements.horizon_h`` stays the *relative* budget the planner was
  given (the hours left at the event, rounded down).
  The trace (``ReplanRecord.after.steps``) says which clock each step speaks: every step's
  payload carries ``offset_h`` (planner time 0 in simulator hours) and ``time_base``; an ok
  ``schedule`` step and the final ``replan`` step quote absolute hours (the makespan figures
  are shifted and the receipt's ``horizon_h`` is the brief's original deadline), every other
  step (a validator or solver verdict whose hint quotes hours) is ``"relative"`` to that
  offset.
  A task "in progress" is one with ``frame.progress > 0`` whose robot (``current_task``) is not
  failed; its remaining energy is ``original energy_wh * (1 - progress)``.

Scope guard
  A replan must keep every remaining task (``missing:<task>``) and leave running tasks on their
  robot and start. A planner answer that was not escalated (the solver-first path, or a
  planner that hands back the remaining graph without a repair round) must also keep every
  task as it was handed over (``changed:<task>``, see
  :func:`moonwright.pipeline.validate.check_scope`). An escalated replan is allowed to
  re-type, re-zone or re-scope tasks (that is what the model was asked for; the repair step's
  ``diff`` payload shows every change), except the running ones: a locked task whose
  definition changed is refused as ``locked:<task>``
  (:func:`moonwright.pipeline.loop.check_locked_tasks`). The Planner enforces the same rules
  round by round; :func:`replan_after_event` re-checks the result so that
  :func:`run_with_replanning` never applies a plan that silently lost or redefined work,
  whatever planner produced it: such a replan is recorded as failed and the previous plan
  stays in force.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

from moonwright.geometry import zone_positions
from moonwright.pipeline.loop import check_locked_tasks
from moonwright.pipeline.validate import check_scope
from moonwright.schemas import (
    Event,
    Failure,
    LoopStep,
    PlanResult,
    ReplanRecord,
    Requirements,
    Schedule,
    ScheduleEntry,
    SimFrame,
    SimResult,
    Site,
    Task,
    TaskGraph,
    Zone,
    to_mwh,
)
from moonwright.sim.simulator import Simulator

if TYPE_CHECKING:  # the Planner protocol only; keeps the sim package free of pipeline types
    from moonwright.pipeline.loop import Planner
    from moonwright.sim.simulator import FailureInjection

_PROGRESS_EPS = 1e-9
_TIME_EPS = 1e-9
DEFAULT_REPLAN_ON: tuple[str, ...] = ("robot_failure",)
_RECORD_HAS_ESCALATED = "escalated" in ReplanRecord.model_fields


def replan_offset_h(t_h: float) -> int:
    """Whole hours already elapsed: the shift between planner time (0 = now) and sim time."""
    return math.floor(t_h)


def remaining_horizon_h(horizon_h: int, t_h: float) -> int:
    """Hours the replanner may use: the deadline minus ``t_h`` rounded *up* to whole hours
    (floor 1), because the simulation resumes at ``t_h`` while planner time 0 maps back to
    ``floor(t_h)``."""
    return max(1, horizon_h - math.ceil(t_h - _TIME_EPS))


def shift_schedule(schedule: Schedule, offset_h: int) -> Schedule:
    """The same schedule with every time moved later by ``offset_h`` hours."""
    if offset_h == 0:
        return schedule
    bound = schedule.objective_bound_h
    return schedule.model_copy(update={
        "entries": [e.model_copy(update={"start_h": e.start_h + offset_h,
                                         "end_h": e.end_h + offset_h})
                    for e in schedule.entries],
        "makespan_h": schedule.makespan_h + offset_h,
        "objective_bound_h": None if bound is None else bound + offset_h,
    })


def _failed_robots(frame: SimFrame, event: Event | None = None) -> set[str]:
    """Robots that must not be planned with: every robot the frame shows as failed plus the
    robot of a ``robot_failure`` event, in case the frame predates its status change. The one
    place this set is derived, so a lock is never placed on the failed robot."""
    failed = {rs.id for rs in frame.robots if rs.status == "failed"}
    if event is not None and event.kind == "robot_failure" and event.robot:
        failed.add(event.robot)
    return failed


def _in_progress_workers(frame: SimFrame, failed: set[str]) -> dict[str, str]:
    """Task id -> healthy robot currently working on it (tasks with progress > 0 only)."""
    return {
        rs.current_task: rs.id for rs in frame.robots
        if rs.current_task and rs.id not in failed
        and frame.progress.get(rs.current_task, 0.0) > _PROGRESS_EPS
    }


def locked_entries(graph2: TaskGraph, frame: SimFrame,
                   event: Event | None = None) -> list[ScheduleEntry]:
    """Pins for tasks underway on healthy robots: same robot, start 0, remaining duration.

    ``graph2`` must be the graph from :func:`build_replan_inputs` (durations already reduced);
    pass the same ``event`` so both agree on which robots have failed.
    """
    workers = _in_progress_workers(frame, _failed_robots(frame, event))
    return [ScheduleEntry(task=t.id, robot=workers[t.id], start_h=0, end_h=t.duration_h)
            for t in graph2.tasks if t.id in workers]


def robot_positions(frame: SimFrame, event: Event | None = None) -> dict[str, tuple[float, float]]:
    """Where every surviving robot is now (robot id -> grid cell), for the travel allowance."""
    failed = _failed_robots(frame, event)
    return {rs.id: (float(rs.pos[0]), float(rs.pos[1]))
            for rs in frame.robots if rs.id not in failed}


def declared_site(site: Site, layout: Mapping[str, tuple[int, int]]) -> Site:
    """``site`` with every zone of ``layout`` declared: the zones the site declares keep their
    geometry, the auto-placed ones are added at the cell the simulator put them in (a small
    radius; the planner only uses centres), so planner and simulator agree on every zone."""
    known = {z.id for z in site.zones}
    radius = min(Zone.model_fields["radius"].default, Site.max_zone_radius(site.grid))
    extra = [Zone(id=zone_id, center=(int(cell[0]), int(cell[1])), radius=radius)
             for zone_id, cell in sorted(layout.items()) if zone_id not in known]
    if not extra:
        return site
    return site.model_copy(update={"zones": [*site.zones, *extra]})


def build_replan_inputs(
    req: Requirements, graph: TaskGraph, schedule: Schedule, frame: SimFrame, event: Event,
    originals: Mapping[str, Task] | None = None,
    layout: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[Requirements, TaskGraph, str]:
    """Requirements, task graph and LLM context for planning the remaining work from ``frame``.

    ``originals`` maps task id -> the task as first planned; a task missing from it (or
    ``None``) counts as its own original, which is exact before the first replan. ``layout``
    is the simulator's zone layout (:attr:`Simulator.layout`); without it the layout of
    ``graph`` is used, which is the simulator's as long as no earlier replan added a zone.
    Raises ``ValueError`` when nothing can be planned (no surviving robot or no remaining
    task).
    """
    failed = _failed_robots(frame, event)
    state = {rs.id: rs for rs in frame.robots}
    robots = [
        r.model_copy(update={"battery_wh": max(1.0, state[r.id].battery_wh)})
        if r.id in state else r
        for r in req.robots if r.id not in failed
    ]
    if not robots:
        raise ValueError("every robot has failed; nothing left to replan with")

    done = set(frame.done_tasks)
    workers = _in_progress_workers(frame, failed)
    lost = {rs.current_task for rs in frame.robots if rs.id in failed and rs.current_task}
    full = originals or {}
    tasks = []
    for task in graph.tasks:
        if task.id in done:
            continue
        original = full.get(task.id, task)
        update: dict[str, object] = {"after": [a for a in task.after if a not in done]}
        if task.id in workers:
            progress = frame.progress[task.id]
            update["duration_h"] = max(1, math.ceil(original.duration_h * (1.0 - progress)
                                                    - _PROGRESS_EPS))
            update["energy_wh"] = round(original.energy_wh * (1.0 - progress), 3)
        elif task.id in lost:
            update["duration_h"] = original.duration_h
            update["energy_wh"] = original.energy_wh
        tasks.append(task.model_copy(update=update))
    if not tasks:
        raise ValueError("every task is done; nothing left to replan")

    horizon = remaining_horizon_h(req.horizon_h, frame.t_h)
    cells = layout if layout is not None else zone_positions(req.site, (t.zone for t in graph.tasks))
    req2 = req.model_copy(update={"robots": robots, "horizon_h": horizon,
                                  "site": declared_site(req.site, cells)})
    graph2 = TaskGraph(tasks=tasks, notes=f"replan at {frame.t_h:g} h after {event.kind}")
    context = _context(req, req2, graph2, frame, event, failed, workers)
    return req2, graph2, context


def _context(req: Requirements, req2: Requirements, graph2: TaskGraph, frame: SimFrame,
             event: Event, failed: set[str], workers: dict[str, str]) -> str:
    """One paragraph telling the LLM where the mission stands."""
    t = frame.t_h
    parts = [f"Replanning at {t:g} h of the {req.horizon_h} h horizon; {req2.horizon_h} h remain."]
    if event.kind == "robot_failure" and event.robot:
        doing = "idle"
        if event.task:
            pct = frame.progress.get(event.task, 0.0)
            doing = (f"on {event.task} ({pct:.0%} done; that work is lost and {event.task} "
                     f"must be redone in full by another robot)")
        parts.append(f"Robot {event.robot} has failed and must not be used again; it was {doing}.")
    elif event.detail:
        parts.append(f"Event {event.kind}: {event.detail}.")
    else:
        parts.append(f"Event {event.kind} on {event.robot or 'the crew'}.")
    parts.append("Finished tasks (do not re-add): " + (", ".join(frame.done_tasks) or "none") + ".")
    remaining = []
    for task in graph2.tasks:
        if task.id in workers:
            remaining.append(f"{task.id} (underway on {workers[task.id]}, {task.duration_h} h "
                             f"left, locked to that robot from time 0)")
        else:
            remaining.append(task.id)
    parts.append("Remaining tasks: " + ", ".join(remaining) + ".")
    budgets = ", ".join(f"{r.id} ({r.type}) {r.battery_wh:.0f} Wh" for r in req2.robots)
    parts.append(f"Surviving robots and remaining battery: {budgets}.")
    if failed:
        parts.append("Failed robots (unavailable): " + ", ".join(sorted(failed)) + ".")
    parts.append("Time 0 of the new schedule is now; keep every remaining task with its id, "
                 "robot_type, zone, after edges, duration_h (the remaining hours) and energy_wh "
                 "(the remaining Wh) exactly as given, and the plan must fit the remaining "
                 "horizon and batteries.")
    return " ".join(parts)


def _scope_failure(graph2: TaskGraph, locked: list[ScheduleEntry], result: PlanResult,
                   *, strict: bool = True) -> Failure | None:
    """Why a "successful" replan must be refused: it dropped remaining tasks (``missing:<id>``),
    redefined one (``changed:<id>`` when ``strict``, ``locked:<id>`` for a running task
    otherwise; module docstring, "Scope guard") or moved a running task off its robot or start
    (planner time). None when it is sound."""
    if not result.success or result.schedule is None or result.task_graph is None:
        return None
    scheduled = {e.task: e for e in result.schedule.entries}
    scope = check_scope(result.task_graph, graph2.ids(), graph2 if strict else None)
    conflicts = list(scope.conflicts) if scope else []
    details = list(scope.details) if scope else []
    if not strict:
        missing_ids = [c.removeprefix("missing:") for c in conflicts]
        locks = check_locked_tasks(result.task_graph, graph2, locked, ignore_after=missing_ids)
        if locks is not None:
            conflicts += locks.conflicts
            details += locks.details
    unscheduled = [t for t in graph2.ids() if t in set(result.task_graph.ids())
                   and t not in scheduled]
    moved = [e.task for e in locked
             if e.task in scheduled
             and (scheduled[e.task].robot, scheduled[e.task].start_h) != (e.robot, e.start_h)]
    if not conflicts and not unscheduled and not moved:
        return None
    reasons = []
    missing = [c.removeprefix("missing:") for c in conflicts if c.startswith("missing:")]
    changed = [c.removeprefix("changed:") for c in conflicts if c.startswith("changed:")]
    relocked = [c.removeprefix("locked:") for c in conflicts if c.startswith("locked:")]
    if missing or unscheduled:
        reasons.append(f"dropped {', '.join(missing + unscheduled)}")
        conflicts += [f"missing:{t}" for t in unscheduled]
        details += [f"{t} was remaining but is not in the replanned schedule" for t in unscheduled]
    if changed:
        reasons.append(f"redefined the remaining task(s) {', '.join(changed)}")
    if relocked:
        reasons.append(f"redefined the running task(s) {', '.join(relocked)}")
    if moved:
        reasons.append(f"moved the running task(s) {', '.join(moved)} off their robot or start")
        details += [f"{t} must continue on its robot from time 0" for t in moved]
    return Failure(
        kind="validation",
        conflicts=conflicts,
        hint="Replan refused: it " + " and ".join(reasons) + "; the previous plan stays in force.",
        details=details,
    )


def _shift_step(step: LoopStep, offset_h: int, horizon_h: int) -> LoopStep:
    """A step of the replan trace annotated with the clock it speaks (module docstring, time
    convention): an ok ``schedule`` step is moved to simulator (absolute) hours so it agrees
    with the shifted ``ReplanRecord.after.schedule`` and its receipt quotes the brief's
    deadline ``horizon_h``; every other step is marked relative."""
    payload: dict[str, Any] = dict(step.payload or {})
    payload["offset_h"] = offset_h
    if step.stage == "schedule" and step.ok and isinstance(payload.get("makespan_h"), int):
        makespan = payload["makespan_h"] + offset_h
        payload["makespan_h"] = makespan
        receipt = payload.get("verification")
        if isinstance(receipt, dict):
            payload["verification"] = {
                **receipt,
                "horizon_used_h": receipt.get("horizon_used_h", makespan - offset_h) + offset_h,
                "horizon_h": horizon_h,
            }
        payload["time_base"] = "absolute"
        status = payload.get("status", "")
        message = f"makespan {makespan}h ({status})" if status else f"makespan {makespan}h"
        return step.model_copy(update={"payload": payload, "message": message})
    payload["time_base"] = "relative"
    return step.model_copy(update={"payload": payload})


def _seconds(seconds: float) -> str:
    """Solver wall time for a trace message: two decimals, never a misleading "0.00 s"."""
    return f"{seconds:.2f} s" if seconds >= 0.005 else "under 0.01 s"


def _first_verdict(result: PlanResult) -> Failure | None:
    """The first validator / solver verdict of the trace: what made the run escalate."""
    return next((s.failure for s in result.steps
                 if s.stage in ("validate", "schedule") and not s.ok and s.failure is not None),
                None)


def _outcome_step(result: PlanResult, before: Schedule, *, escalated: bool, offset_h: int,
                  remaining: int) -> LoopStep:
    """The closing ``replan`` step of the trace: how the remaining work was rescheduled (module
    docstring, "Solver first"), with makespans in absolute simulator hours."""
    verdict = _first_verdict(result)
    core = ", ".join(verdict.conflicts) if verdict and verdict.conflicts else (
        verdict.kind if verdict else "infeasible")
    schedule = result.schedule if result.success else None
    if escalated:
        head = f"infeasible ({core}) -> escalated to Nemotron"
        answered = any(s.stage == "repair" and s.ok for s in result.steps)
        if not answered:
            head += " (no answer: " + (
                "offline, no recorded repair" if any(
                    (s.message or "").startswith("offline:") for s in result.steps
                    if s.stage == "repair") else "every repair answer was rejected") + ")"
    elif schedule is not None:
        head = (f"CP-SAT rescheduled {remaining} tasks in {_seconds(schedule.solve_time_s)}, "
                "Nemotron not needed")
    else:
        head = f"infeasible ({core}), Nemotron not asked (no repair budget)"
    if schedule is not None:
        tail = f"makespan {before.makespan_h} h -> {schedule.makespan_h} h (absolute)"
        if escalated:
            tail += f", verified after {result.repairs} repair(s)"
        failure = None
    else:
        failure = result.failure
        if failure is not None and failure.hint.startswith("Replan refused"):
            tail = failure.hint
        else:
            what = (f"{failure.kind}: {', '.join(failure.conflicts) or '-'}" if failure
                    else "no plan")
            tail = f"no verified replan ({what}); the previous plan stays in force"
    return LoopStep(
        stage="replan", ok=schedule is not None, failure=failure, message=f"{head}; {tail}",
        payload={"escalated": escalated, "offset_h": offset_h, "time_base": "absolute",
                 "remaining_tasks": remaining, "before_makespan_h": before.makespan_h,
                 "after_makespan_h": schedule.makespan_h if schedule else None,
                 "solve_time_s": round(schedule.solve_time_s, 3) if schedule else None},
    )


def replan_after_event(
    planner: Planner, req: Requirements, graph: TaskGraph, schedule: Schedule,
    frame: SimFrame, event: Event, *, key: str,
    originals: Mapping[str, Task] | None = None, variant: str = "replan",
    layout: Mapping[str, tuple[int, int]] | None = None,
) -> ReplanRecord:
    """Reschedule the remaining work solver-first and return the record with absolute times.

    The planner is called with ``start_with_prior=True``, ``start_offset_h=0``, ``locked``
    pins for tasks underway on healthy robots, ``required_tasks`` = every remaining id and the
    surviving robots' positions; CP-SAT reschedules the remaining graph as it is and the model
    is asked only when that is infeasible (``ReplanRecord.escalated``). The result is checked
    against the scope rules (module docstring, "Scope guard"), its trace annotated with the
    clock it speaks and closed with a ``replan`` outcome step, and its schedule shifted by
    :func:`replan_offset_h` so ``record.after.schedule`` is in simulator time. ``variant``
    names the fixture family of this replan's LLM calls; ``layout`` is the simulator's zone
    layout (see :func:`build_replan_inputs`).
    """
    req2, graph2, context = build_replan_inputs(req, graph, schedule, frame, event, originals,
                                                layout)
    locked = locked_entries(graph2, frame, event)
    result = planner.plan_from_requirements(
        req2, key=key, prior_graph=graph2, context=context, variant=variant,
        start_offset_h=0, locked=locked, required_tasks=graph2.ids(),
        start_with_prior=True, robot_positions=robot_positions(frame, event),
    )
    escalated = any(step.stage == "repair" for step in result.steps)
    refused = _scope_failure(graph2, locked, result, strict=not escalated)
    if refused is not None:
        result = result.model_copy(update={"success": False, "schedule": None,
                                           "failure": refused})
    offset = replan_offset_h(frame.t_h)
    steps = [_shift_step(step, offset, req.horizon_h) for step in result.steps]
    if result.schedule is not None:
        result = result.model_copy(update={"schedule": shift_schedule(result.schedule, offset)})
    outcome = _outcome_step(result, schedule, escalated=escalated, offset_h=offset,
                            remaining=len(graph2.tasks))
    result = result.model_copy(update={"steps": [*steps, outcome]})
    extra = {"escalated": escalated} if _RECORD_HAS_ESCALATED else {}
    return ReplanRecord(event=event, t_h=frame.t_h, remaining_tasks=graph2.ids(),
                        before=schedule, after=result, context=context, **extra)


def _pick_event(events: list[Event]) -> Event:
    """A step can fire several events at once; a robot failure matters most."""
    return next((e for e in events if e.kind == "robot_failure"), events[0])


def _redefined(was: Task, now: Task) -> bool:
    """Whether a replan settled on another definition of a task (type, zone, scope or size)."""
    return (was.robot_type, was.zone, was.quantity, was.unit, was.duration_h,
            to_mwh(was.energy_wh)) != (now.robot_type, now.zone, now.quantity, now.unit,
                                       now.duration_h, to_mwh(now.energy_wh))


def affects_work(event: Event, schedule: Schedule, frame: SimFrame) -> bool:
    """Whether ``event`` takes work away from the plan in force. Every kind but a robot failure
    counts; a robot failure only when the robot was on a task or still had unfinished entries
    in ``schedule``: losing an idle, finished or never-scheduled robot changes nothing, and
    replanning on it would only re-baseline the remaining work and spend the replan budget."""
    if event.kind != "robot_failure" or not event.robot:
        return True
    if event.task:
        return True
    done = set(frame.done_tasks)
    return any(e.robot == event.robot and e.task not in done for e in schedule.entries)


def run_with_replanning(planner: Planner, plan: PlanResult,
                        injections: list[FailureInjection], *, key: str,
                        max_replans: int = 2, dt_h: float = 0.25,
                        replan_on: Collection[str] = DEFAULT_REPLAN_ON) -> SimResult:
    """Simulate ``plan``; after each triggering event replan once (up to ``max_replans``) and
    continue.

    ``dt_h`` is the simulator step. Only events whose kind is in ``replan_on`` trigger a
    replan, and a robot failure only when that robot still owned work (:func:`affects_work`);
    the others are recorded and the simulation simply carries on. A replan that fails
    (or is refused by the scope guard) keeps the current plan in force. Once the replan budget
    is spent, or when no healthy robot is left to replan with, the simulation runs on without
    stopping on further events. Frames, events and replan records of every leg are merged into
    one result whose ``schedule``/``task_graph`` are the ones in force at the end. The n-th
    replan of the run tags its LLM calls with the variant ``"replan"`` (n = 1) or
    ``"replan<n>"``, so recordings of several replans never overwrite each other.
    """
    if not plan.success or plan.schedule is None or plan.task_graph is None:
        raise ValueError("run_with_replanning needs a successful PlanResult with a schedule")
    req = plan.requirements
    graph, schedule = plan.task_graph, plan.schedule
    originals: dict[str, Task] = {t.id: t for t in graph.tasks}
    triggers = set(replan_on)
    sim = Simulator(req, graph, schedule, dt_h=dt_h, injections=injections)
    frames = []
    events = []
    replans = []
    while True:
        leg = sim.run(stop_on_event=True)
        frames.extend(leg.frames)
        events.extend(leg.events)
        if leg.completed or not leg.events:
            break
        frame = sim.state()
        triggering = [e for e in leg.events
                      if e.kind in triggers and affects_work(e, schedule, frame)]
        if not triggering:
            continue
        if len(replans) >= max_replans or all(rs.status == "failed" for rs in frame.robots):
            leg = sim.run(stop_on_event=False)
            frames.extend(leg.frames)
            events.extend(leg.events)
            break
        variant = "replan" if not replans else f"replan{len(replans) + 1}"
        event = _pick_event(triggering)
        record = replan_after_event(planner, req, graph, schedule, frame, event, key=key,
                                    originals=originals, variant=variant, layout=sim.layout)
        replans.append(record)
        after = record.after
        if after.success and after.schedule is not None and after.task_graph is not None:
            graph, schedule = after.task_graph, after.schedule
            underway = _in_progress_workers(frame, _failed_robots(frame, event))
            for task in graph.tasks:
                if task.id in underway:
                    originals.setdefault(task.id, task)   # the graph holds its reduced copy
                elif task.id not in originals or _redefined(originals[task.id], task):
                    originals[task.id] = task             # added, or re-scoped by the repair
            sim.apply_new_plan(schedule, graph)
    return SimResult(frames=frames, events=events, replans=replans, completed=leg.completed,
                     finished_at_h=leg.finished_at_h, schedule=sim.schedule,
                     task_graph=sim.task_graph)
