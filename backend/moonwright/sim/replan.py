"""F8: event -> replan the remaining work -> continue the simulation.

Contract (implemented by the simulation agent):

    req2, graph2, context = build_replan_inputs(req, graph, schedule, frame, event)
    record = replan_after_event(planner, req, graph, schedule, frame, event, key=key)
    result = run_with_replanning(planner, plan, injections, key=key, max_replans=2, dt_h=0.25)

build_replan_inputs:
  * robots: drop failed robots; battery_wh := remaining battery from the frame (floor 1 Wh)
  * horizon_h := original horizon - ceil(t_h)  (floor 1), see the time convention below
  * tasks: drop done tasks; tasks in progress on a healthy robot stay with remaining duration
    (ceil) and are passed as `locked` ScheduleEntry(robot, start=0) so they continue; tasks in
    progress on the failed robot are re-added in full; `after` edges to done tasks are removed.
    "Remaining" and "full" refer to the task's *original* definition (``originals``, the graph
    the plan started with plus anything a replan added), never to the already-reduced copy an
    earlier replan left in the graph: ``frame.progress`` is overall progress, so
    ``original * (1 - progress)`` is exact however many replans a task lives through.
  * context: one paragraph for the LLM: time now, what finished, which robot failed and what it
    was doing, remaining budgets. The planner is called with prior_graph=graph2, context,
    variant="replan", start_offset_h=0, required_tasks=graph2.ids() (sim converts back by
    adding floor(t_h)).
run_with_replanning: simulate -> on a robot failure replan -> apply_new_plan -> continue, until
  completed or max_replans exhausted; returns the final SimResult with all frames/events/replans
  merged. Only event kinds in ``replan_on`` (default: robot_failure, which includes battery
  depletion) trigger a replan; task_overrun and battery_low are recorded and the run goes on.
  A robot failure only triggers a replan when the robot still owned work (a current task or
  queued entries): the failure of an idle or finished robot changes nothing the plan relies on,
  so it is recorded and the run simply continues (see :func:`affects_work`).

Time convention (one place: :func:`replan_offset_h` and :func:`remaining_horizon_h`)
  The planner works in *relative* hours: ``start_offset_h=0``, locked entries starting at 0,
  ``horizon_h`` shrunk to the time left. :func:`replan_after_event` converts the schedule it
  gets back to absolute simulator time by adding ``replan_offset_h(t_h) == floor(t_h)``, so a
  locked entry starts at or before ``t_h`` and the robot working on it never pauses. The
  simulation itself resumes at ``t_h``, up to an hour after planner time 0, which is why the
  horizon is shrunk by ``ceil(t_h)``: a schedule that fits the shrunken horizon still meets the
  original deadline in simulator time. ``ReplanRecord.after.schedule`` therefore holds absolute
  times and can be handed to :meth:`Simulator.apply_new_plan` as-is.
  A task "in progress" is one with ``frame.progress > 0`` whose robot (``current_task``) is not
  failed; its remaining energy is ``original energy_wh * (1 - progress)``.

Scope guard
  A replan must keep every remaining task as it was handed over (same robot type, zone and
  predecessors, at least the remaining duration and energy: see
  :func:`moonwright.pipeline.validate.check_scope`) and leave running tasks on their robot and
  start. The Planner enforces both (``required_tasks`` + ``prior_graph`` feed ``missing:<task>``
  / ``changed:<task>`` back to the LLM, the solver pins locked tasks); :func:`replan_after_event`
  re-checks the result so that :func:`run_with_replanning` never applies a plan that silently
  lost or redefined work, whatever planner produced it: such a replan is recorded as failed and
  the previous plan stays in force.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

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
    Task,
    TaskGraph,
)
from moonwright.sim.simulator import Simulator

if TYPE_CHECKING:  # avoid import cycle at runtime
    from moonwright.pipeline.loop import Planner
    from moonwright.sim.simulator import FailureInjection

_PROGRESS_EPS = 1e-9
_TIME_EPS = 1e-9
DEFAULT_REPLAN_ON: tuple[str, ...] = ("robot_failure",)


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


def build_replan_inputs(
    req: Requirements, graph: TaskGraph, schedule: Schedule, frame: SimFrame, event: Event,
    originals: Mapping[str, Task] | None = None,
) -> tuple[Requirements, TaskGraph, str]:
    """Requirements, task graph and LLM context for planning the remaining work from ``frame``.

    ``originals`` maps task id -> the task as first planned; a task missing from it (or
    ``None``) counts as its own original, which is exact before the first replan. Raises
    ``ValueError`` when nothing can be planned (no surviving robot or no remaining task).
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
    req2 = req.model_copy(update={"robots": robots, "horizon_h": horizon})
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


def _scope_failure(graph2: TaskGraph, locked: list[ScheduleEntry],
                   result: PlanResult) -> Failure | None:
    """Why a "successful" replan must be refused: it dropped remaining tasks (``missing:<id>``),
    redefined one (``changed:<id>``, see :func:`check_scope`) or moved a running task off its
    robot or start (planner time). None when it is sound."""
    if not result.success or result.schedule is None or result.task_graph is None:
        return None
    scheduled = {e.task: e for e in result.schedule.entries}
    scope = check_scope(result.task_graph, graph2.ids(), graph2)
    unscheduled = [t for t in graph2.ids() if t in set(result.task_graph.ids())
                   and t not in scheduled]
    moved = [e.task for e in locked
             if e.task in scheduled
             and (scheduled[e.task].robot, scheduled[e.task].start_h) != (e.robot, e.start_h)]
    if scope is None and not unscheduled and not moved:
        return None
    conflicts = list(scope.conflicts) if scope else []
    details = list(scope.details) if scope else []
    reasons = []
    missing = [c.removeprefix("missing:") for c in conflicts if c.startswith("missing:")]
    changed = [c.removeprefix("changed:") for c in conflicts if c.startswith("changed:")]
    if missing or unscheduled:
        reasons.append(f"dropped {', '.join(missing + unscheduled)}")
        conflicts += [f"missing:{t}" for t in unscheduled]
        details += [f"{t} was remaining but is not in the replanned schedule" for t in unscheduled]
    if changed:
        reasons.append(f"redefined the remaining task(s) {', '.join(changed)}")
    if moved:
        reasons.append(f"moved the running task(s) {', '.join(moved)} off their robot or start")
        details += [f"{t} must continue on its robot from time 0" for t in moved]
    return Failure(
        kind="validation",
        conflicts=conflicts,
        hint="Replan refused: it " + " and ".join(reasons) + "; the previous plan stays in force.",
        details=details,
    )


def _shift_step(step: LoopStep, offset_h: int) -> LoopStep:
    """A schedule step of the replan trace with its makespan in simulator (absolute) hours, so
    the trace agrees with the shifted ``ReplanRecord.after.schedule``."""
    payload = step.payload or {}
    if step.stage != "schedule" or not step.ok or not isinstance(payload.get("makespan_h"), int):
        return step
    makespan = payload["makespan_h"] + offset_h
    status = payload.get("status", "")
    return step.model_copy(update={
        "payload": {**payload, "makespan_h": makespan},
        "message": f"makespan {makespan}h ({status})" if status else f"makespan {makespan}h",
    })


def replan_after_event(
    planner: Planner, req: Requirements, graph: TaskGraph, schedule: Schedule,
    frame: SimFrame, event: Event, *, key: str,
    originals: Mapping[str, Task] | None = None, variant: str = "replan",
) -> ReplanRecord:
    """Ask the planner for the remaining work and return the record with absolute times.

    The planner is called with ``start_offset_h=0``, ``locked`` pins for tasks underway on
    healthy robots and ``required_tasks`` = every remaining id; the schedule it returns is
    checked against both (see "Scope guard" in the module docstring) and shifted by
    :func:`replan_offset_h` so ``record.after.schedule`` is in simulator time. ``variant``
    names the fixture family of this replan's LLM calls.
    """
    req2, graph2, context = build_replan_inputs(req, graph, schedule, frame, event, originals)
    locked = locked_entries(graph2, frame, event)
    result = planner.plan_from_requirements(
        req2, key=key, prior_graph=graph2, context=context, variant=variant,
        start_offset_h=0, locked=locked, required_tasks=graph2.ids(),
    )
    refused = _scope_failure(graph2, locked, result)
    if refused is not None:
        step = LoopStep(stage="replan", ok=False, failure=refused, message=refused.hint)
        result = result.model_copy(update={"success": False, "schedule": None,
                                           "failure": refused, "steps": [*result.steps, step]})
    offset = replan_offset_h(frame.t_h)
    if result.schedule is not None and offset:
        result = result.model_copy(update={
            "schedule": shift_schedule(result.schedule, offset),
            "steps": [_shift_step(step, offset) for step in result.steps],
        })
    return ReplanRecord(event=event, t_h=frame.t_h, remaining_tasks=graph2.ids(),
                        before=schedule, after=result, context=context)


def _pick_event(events: list[Event]) -> Event:
    """A step can fire several events at once; a robot failure matters most."""
    return next((e for e in events if e.kind == "robot_failure"), events[0])


def affects_work(event: Event, schedule: Schedule, frame: SimFrame) -> bool:
    """Whether ``event`` takes work away from the plan in force. Every kind but a robot failure
    counts; a robot failure only when the robot was on a task or still had unfinished entries
    in ``schedule``: losing an idle or finished robot changes nothing, and replanning on it
    would only re-baseline the remaining work and spend the replan budget (and LLM calls)."""
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
        record = replan_after_event(planner, req, graph, schedule, frame, _pick_event(triggering),
                                    key=key, originals=originals, variant=variant)
        replans.append(record)
        after = record.after
        if after.success and after.schedule is not None and after.task_graph is not None:
            graph, schedule = after.task_graph, after.schedule
            for task in graph.tasks:
                originals.setdefault(task.id, task)
            sim.apply_new_plan(schedule, graph)
    return SimResult(frames=frames, events=events, replans=replans, completed=leg.completed,
                     finished_at_h=leg.finished_at_h, schedule=sim.schedule,
                     task_graph=sim.task_graph)
