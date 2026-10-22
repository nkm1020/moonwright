"""F7: 2D grid simulator that replays a Schedule and emits Events.

Contract (implemented by the simulation agent):

    layout = zone_positions(req, graph)      # zone id -> (x, y); deterministic grid layout
                                             # for zones missing from req.site.zones
    sim = Simulator(req, graph, schedule, dt_h=0.25, injections=[FailureInjection("EX2", 30)],
                    seed=0)
    result = sim.run(stop_on_event=True)     # SimResult with frames + events (+ completed flag)
    frame = sim.state()                      # current SimFrame (robots, done, active, progress)

Behaviour:
  * robots start at the depot, travel in straight lines at speed_cells_per_h to the task zone,
    then work for duration_h; battery drops by energy_wh * (dt / duration_h) while working and by
    a small travel cost (``moonwright.geometry.TRAVEL_WH_PER_CELL``, 0.5 Wh per cell). A task
    starts at max(scheduled start, arrival).
  * events: robot_failure when an injection time is reached (robot status -> failed, its current
    task progress is kept in frame.progress) and when a robot's battery cannot cover its next
    step of work or travel (detail "ran out of battery"; it stops for good the same way, so a
    plan that overdraws a battery can never be reported complete), battery_low when a robot's
    battery can no longer cover what the plan in force still asks of it (the rest of its
    current task, its queued tasks and the legs between them: a forecast that the robot will
    run dry, see "Battery" below), task_overrun when a task's work takes > 2 h longer than its
    schedule entry allots (see "Lateness" below).
  * stop_on_event=True returns as soon as the first *new* event fires, so the replanner can act;
    run() can be called again after `apply_new_plan(schedule, graph)` to continue from the
    current state. `completed` is True when every task of every graph applied so far is done
    (a replanned graph that drops unfinished work therefore cannot complete the run).
  * dt_h default 0.25; frames recorded every step (keep SimFrame small).

Implementation notes
--------------------
* A step covers [t, t + dt). Robots are advanced in ``req.robots`` order. A robot follows its
  schedule entries strictly in schedule order: it leaves for the next entry's zone as soon as it
  is free and starts working once ``start_h <= t``, every ``after`` task was done before the
  step began and nobody else works in the zone. Arriving ends the robot's step; work begins
  next step.
* Travel and the planner: the solver reserves a per-task transit allowance (an upper bound over
  the site, see ``moonwright.pipeline.schedule``), not terrain; the simulator pays the actual
  straight-line distance, which is never longer. Geometry is shared through
  ``moonwright.geometry`` (:func:`depot_position`, :func:`zone_positions`), so planner and
  simulator place every zone in the same cell. A robot that leaves as soon as it is free
  therefore arrives on or before the scheduled start of a solver schedule, and a
  disturbance-free replay of a verified plan starts every task on time and raises no event
  (with a ``dt_h`` that divides an hour; a coarser step would delay starts to its grain).
* Zone safety: two robots never have status "working" in the same zone during the same step; a
  robot that is ready while its zone is busy waits at the zone with status "idle".
* Battery: the verifier budgets each robot's work plus a transit allowance against its whole
  battery, so a verified plan may legitimately drain a robot to a few percent; an alarm at a
  fixed reserve would therefore fire on plans the solver proved sound. ``battery_low`` is
  instead the simulator's own forecast under the plan in force: it fires when the battery
  left is below what the robot's remaining entries still need (the rest of the current task
  pro rata, every queued task in full, and the straight-line legs from where the robot is at
  0.5 Wh per cell). On a verified plan that never happens without a disturbance; with longer
  tasks (Monte Carlo jitter) or a replan solved from stale numbers it warns before the robot
  actually runs dry (``robot_failure``, "ran out of battery").
* Events are evaluated after the clock advances: completions (and task_overrun), then failure
  injections whose time has been reached, then battery_low. battery_low fires at most once per
  robot per plan in force (``apply_new_plan`` re-arms it), task_overrun at most once per task,
  every injection at most once. Injections and battery checks are also evaluated once before
  the initial frame is recorded, so an injection at t = 0 fires at t = 0 (and the first
  ``run`` with ``stop_on_event`` returns right away).
* Frames report batteries rounded *down* to 0.01 Wh: a replan budgets a robot with the frame's
  figure, and a budget that exceeded reality by half a milli-Wh could strand the robot on the
  last step of a task the verifier had just accepted.
* Progress of a task is ``base + (1 - base) * worked_h / duration_h``; ``base`` is non-zero only
  for a task that continued on the same robot across :meth:`Simulator.apply_new_plan` (the
  replanned graph carries its *remaining* duration and energy, see ``moonwright.sim.replan``).
* Lateness: a hand-written schedule, a replan solved from stale robot positions or a plan
  whose tasks run longer than planned can still start work late, and that lag compounds down
  a chain. A ``task_overrun`` therefore measures the task itself, not the upstream drift: the
  time from the step the task first worked to its completion, against the length of its
  schedule entry (``end_h - start_h``). A nominal replay never overruns; a task whose real
  duration is longer than planned (Monte Carlo jitter) does, which makes the event a genuine
  anomaly signal. After ``apply_new_plan`` a continuing task is measured from its first step
  under the new plan against its remaining entry.
* ``run`` stops when the graph is complete, when ``until_h`` (absolute hours) is reached, after
  the first new event with ``stop_on_event``, or when the simulation is stuck: nothing moved or
  worked during a step and no robot is waiting for a future ``start_h`` (typically every
  remaining task depends on a task owned by a failed robot). A hard cap of three horizons plus a
  day guards the loop in all other cases.
* The model is fully deterministic. ``seed`` is kept on the instance (``Simulator.seed``) so the
  contract signature stays stable for stochastic extensions; nothing random happens today.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from moonwright import geometry
from moonwright.geometry import TRAVEL_WH_PER_CELL, depot_position, place_zones
from moonwright.schemas import (
    Event,
    Requirements,
    Robot,
    RobotState,
    Schedule,
    ScheduleEntry,
    SimFrame,
    SimResult,
    Task,
    TaskGraph,
)

__all__ = ["FailureInjection", "Simulator", "depot_position", "zone_positions"]

_EPS = 1e-9
_BATTERY_EPS_WH = 1e-6              # forecast vs battery: float noise is not a shortfall
_CLOCK_DECIMALS = 9                 # the clock is k * dt rounded here, so it never drifts

RobotStatus = Literal["idle", "moving", "working", "failed", "done"]


@dataclass(frozen=True)
class FailureInjection:
    robot: str
    t_h: float


# --------------------------------------------------------------------------- layout
def zone_positions(req: Requirements, graph: TaskGraph) -> dict[str, tuple[int, int]]:
    """Zone id -> centre cell, for every site zone and every zone the graph references
    (:func:`moonwright.geometry.zone_positions` over the graph's zones: the planner lays the
    site out the same way)."""
    return geometry.zone_positions(req.site, (t.zone for t in graph.tasks))


def _at(pos: tuple[float, float], cell: tuple[int, int]) -> bool:
    return abs(pos[0] - cell[0]) < _EPS and abs(pos[1] - cell[1]) < _EPS


def _floor_wh(wh: float) -> float:
    """``wh`` rounded down to 0.01 Wh (a frame figure never exceeds the battery it reports)."""
    return math.floor(wh * 100 + _EPS) / 100


# --------------------------------------------------------------------------- simulator
@dataclass
class _RobotRT:
    """Mutable per-robot simulation state."""

    spec: Robot
    pos: tuple[float, float]
    battery_wh: float
    status: RobotStatus = "idle"
    current_task: str | None = None
    queue: list[ScheduleEntry] = field(default_factory=list)  # remaining entries, in order
    battery_low_fired: bool = False

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def has_work(self) -> bool:
        return self.current_task is not None or bool(self.queue)


class Simulator:
    """Replays a verified Schedule on the site grid (module docstring has the rules)."""

    def __init__(self, req: Requirements, graph: TaskGraph, schedule: Schedule, *,
                 dt_h: float = 0.25,
                 injections: list[FailureInjection] | tuple[FailureInjection, ...] = (),
                 seed: int = 0, overrun_tolerance_h: float = 2.0,
                 travel_wh_per_cell: float = TRAVEL_WH_PER_CELL):
        if dt_h <= 0:
            raise ValueError("dt_h must be positive")
        self.dt_h = float(dt_h)
        self.seed = seed
        self.overrun_tolerance_h = overrun_tolerance_h
        self.travel_wh_per_cell = travel_wh_per_cell

        self._req = req
        self._depot = depot_position(req.site)
        self._layout = zone_positions(req, graph)
        start = (float(self._depot[0]), float(self._depot[1]))
        self._robots: dict[str, _RobotRT] = {
            r.id: _RobotRT(spec=r, pos=start, battery_wh=r.battery_wh) for r in req.robots
        }
        for inj in injections:
            if not math.isfinite(inj.t_h):
                raise ValueError(
                    f"failure injection for {inj.robot!r} needs a finite time, got {inj.t_h!r}"
                )
        self._injections = sorted(injections, key=lambda i: (i.t_h, i.robot))
        unknown = sorted({i.robot for i in self._injections} - self._robots.keys())
        if unknown:
            raise ValueError(f"failure injection names unknown robot(s): {unknown}")
        self._next_injection = 0

        self._k = 0  # steps taken; the clock is k * dt
        self._t = 0.0
        self._started = False
        self._required: set[str] = set()  # every task id any applied graph has asked for
        self._done: dict[str, float] = {}  # task id -> completion time
        self._worked_h: dict[str, float] = {}
        self._started_at: dict[str, float] = {}  # task id -> clock when it first worked
        self._base_progress: dict[str, float] = {}
        self._zone_busy: dict[str, str] = {}  # zone id -> robot working there
        self._overrun_fired: set[str] = set()
        self._events: list[Event] = []
        self._run_events: list[Event] = []

        self._graph = graph
        self._schedule = schedule
        self._tasks: dict[str, Task] = {}
        self.apply_new_plan(schedule, graph)

    # ------------------------------------------------------------------ read-only views
    @property
    def t_h(self) -> float:
        return self._t

    @property
    def depot(self) -> tuple[int, int]:
        return self._depot

    @property
    def layout(self) -> dict[str, tuple[int, int]]:
        """Zone id -> centre cell, including zones added by replanned graphs."""
        return dict(self._layout)

    @property
    def events(self) -> list[Event]:
        """Every event fired so far, across all ``run`` calls."""
        return list(self._events)

    @property
    def schedule(self) -> Schedule:
        return self._schedule

    @property
    def task_graph(self) -> TaskGraph:
        return self._graph

    # ------------------------------------------------------------------ public API
    def run(self, *, stop_on_event: bool = True, until_h: float | None = None) -> SimResult:
        """Advance the simulation; the result holds the frames and events of *this* call.

        Stops when every task of the graph is done, when the clock reaches ``until_h``
        (absolute hours), right after the step that fired the first new event when
        ``stop_on_event`` is set, or when nothing can make progress any more.
        """
        frames: list[SimFrame] = []
        self._run_events = []
        if not self._started:
            self._started = True
            self._fire_injections()  # an injection at t = 0 fires before the first frame
            self._check_batteries()
            frames.append(self.state())
        cap = self._default_cap_h() if until_h is None else until_h
        while (not self._completed() and self._t < cap - _EPS
               and not (stop_on_event and self._run_events)):
            progressed, waiting = self._step()
            frames.append(self.state())
            if not progressed and not waiting:
                break  # stuck: nothing moves, works or waits for a later start
        completed = self._completed()
        return SimResult(
            frames=frames, events=list(self._run_events), completed=completed,
            finished_at_h=self._finished_at() if completed else None,
            schedule=self._schedule, task_graph=self._graph,
        )

    def state(self) -> SimFrame:
        """Snapshot of the world at the current clock."""
        robots = [
            RobotState(id=r.spec.id, pos=(round(r.pos[0], 3), round(r.pos[1], 3)),
                       battery_wh=_floor_wh(r.battery_wh), status=r.status,
                       current_task=r.current_task)
            for r in self._robots.values()
        ]
        done = sorted(self._done, key=lambda tid: (self._done[tid], tid))
        active = [r.current_task for r in self._robots.values()
                  if r.status == "working" and r.current_task is not None]
        progress = {tid: 1.0 for tid in done}
        for tid in self._tasks:
            if tid not in self._done:
                p = self._progress_of(tid)
                if p > 0:
                    progress[tid] = round(p, 4)
        return SimFrame(t_h=self._t, robots=robots, done_tasks=done, active_tasks=active,
                        progress=progress)

    def apply_new_plan(self, schedule: Schedule, graph: TaskGraph) -> None:
        """Replace the plan in force; robots keep position, battery and failure status.

        A task keeps its progress only when it continues on the very robot that is working on
        it (the replanner passes such tasks as locked entries carrying their remaining duration
        and energy): its progress so far becomes the baseline of the shorter task. Every other
        unfinished task restarts from zero. Done tasks stay done even if the new graph repeats
        them, and entries assigned to failed robots are dropped because they can never run.
        """
        tasks = {t.id: t for t in graph.tasks}
        queues = self._queues(schedule, tasks)
        self._required.update(tasks)
        new_zones = sorted({t.zone for t in graph.tasks} - self._layout.keys())
        self._layout.update(place_zones(self._req.site, new_zones, set(self._layout.values())))

        base: dict[str, float] = {}
        busy: dict[str, str] = {}
        for r in self._robots.values():
            tid = r.current_task
            queue = [] if r.status == "failed" else queues.get(r.id, [])
            r.queue = queue
            r.battery_low_fired = False   # a new plan in force: forecast it afresh
            if (tid is not None and r.status == "working" and queue
                    and queue[0].task == tid):
                base[tid] = self._progress_of(tid)  # old graph's duration: before the swap
                busy[tasks[tid].zone] = r.id
                continue
            r.current_task = None
            if r.status != "failed":
                r.status = "idle" if queue else "done"
        self._worked_h = {}
        # unfinished tasks are measured afresh under the new plan (see "Lateness")
        self._started_at = {tid: t for tid, t in self._started_at.items() if tid in self._done}
        self._base_progress = base
        self._zone_busy = busy
        self._schedule, self._graph, self._tasks = schedule, graph, tasks

    # ------------------------------------------------------------------ internals
    def _queues(self, schedule: Schedule,
                tasks: dict[str, Task]) -> dict[str, list[ScheduleEntry]]:
        """Per-robot entries in schedule order, minus tasks already done."""
        queues: dict[str, list[ScheduleEntry]] = {}
        seen: set[str] = set()
        for robot_id, entries in schedule.by_robot().items():
            if robot_id not in self._robots:
                raise ValueError(f"schedule assigns unknown robot {robot_id!r}")
            for e in entries:
                if e.task not in tasks:
                    raise ValueError(f"schedule entry refers to unknown task {e.task!r}")
                if e.task in seen:
                    raise ValueError(f"task {e.task!r} is scheduled more than once")
                seen.add(e.task)
                if e.task not in self._done:
                    queues.setdefault(robot_id, []).append(e)
        return queues

    def _step(self) -> tuple[bool, bool]:
        """Advance every robot by one dt. Returns (progressed, waiting_for_future_start)."""
        t0 = self._t
        t1 = round((self._k + 1) * self.dt_h, _CLOCK_DECIMALS)
        done_before = frozenset(self._done)
        busy_before = dict(self._zone_busy)
        progressed = waiting = False
        for r in self._robots.values():
            p, w = self._advance_robot(r, t0, t1, done_before, busy_before)
            progressed = progressed or p
            waiting = waiting or w
        self._k += 1
        self._t = t1
        self._fire_injections()
        self._check_batteries()
        return progressed, waiting

    def _advance_robot(self, r: _RobotRT, t0: float, t1: float, done_before: frozenset[str],
                       busy_before: dict[str, str]) -> tuple[bool, bool]:
        """Move or work for one step. Returns (made progress, waiting for a future start)."""
        if r.status == "failed":
            return False, False
        if r.current_task is None:
            if not r.queue:
                r.status = "done"
                return False, False
            r.current_task = r.queue[0].task
        entry = r.queue[0]
        task = self._tasks[entry.task]
        target = self._layout[task.zone]
        if not _at(r.pos, target):
            pos, travelled = self._travel(r, target, t1 - t0)
            if not self._drain(r, self.travel_wh_per_cell * travelled):
                self._deplete(r, t1, f"while heading to {task.id}")
                return False, False
            r.pos = pos
            r.status = "moving"
            return True, False
        if entry.start_h > t0 + _EPS:
            r.status = "idle"
            return False, True
        preds_done = all(a in done_before for a in task.after)
        zone_free = (busy_before.get(task.zone, r.id) == r.id
                     and self._zone_busy.get(task.zone, r.id) == r.id)
        if not (preds_done and zone_free):
            r.status = "idle"
            return False, False
        worked = self._worked_h.get(task.id, 0.0)
        work_h = min(t1 - t0, task.duration_h - worked)
        if not self._drain(r, task.energy_wh * work_h / task.duration_h):
            self._deplete(r, t1, f"while working on {task.id} "
                                 f"({self._progress_of(task.id):.0%} done)")
            return False, False
        r.status = "working"
        self._zone_busy[task.zone] = r.id
        self._started_at.setdefault(task.id, t0)
        self._worked_h[task.id] = worked + work_h
        if self._worked_h[task.id] >= task.duration_h - _EPS:
            self._complete(r, entry, task, t1)
        return True, False

    @staticmethod
    def _travel(r: _RobotRT, target: tuple[int, int], dt: float) -> tuple[tuple[float, float], float]:
        """Where ``r`` gets to in ``dt`` hours heading for ``target``, and the cells covered."""
        x, y = r.pos
        tx, ty = float(target[0]), float(target[1])
        dist = math.hypot(tx - x, ty - y)
        reach = r.spec.speed_cells_per_h * dt
        if dist <= reach + _EPS:
            return (tx, ty), dist
        f = reach / dist
        return (x + (tx - x) * f, y + (ty - y) * f), reach

    @staticmethod
    def _drain(r: _RobotRT, wh: float) -> bool:
        """Take ``wh`` from the battery; False (and nothing taken) when it cannot cover it."""
        if wh > r.battery_wh + _EPS:
            return False
        r.battery_wh = max(0.0, r.battery_wh - wh)
        return True

    def _deplete(self, r: _RobotRT, t_h: float, doing: str) -> None:
        """Battery exhausted: the robot stops for good, exactly like an injected failure."""
        task = r.current_task
        if task is not None and self._zone_busy.get(self._tasks[task].zone) == r.id:
            del self._zone_busy[self._tasks[task].zone]
        r.status = "failed"
        self._emit(Event(t_h=t_h, kind="robot_failure", robot=r.id, task=task,
                         detail=f"{r.id} ran out of battery at {t_h:g} h {doing}"))

    def _complete(self, r: _RobotRT, entry: ScheduleEntry, task: Task, t_end: float) -> None:
        self._worked_h[task.id] = float(task.duration_h)
        self._done[task.id] = t_end
        if self._zone_busy.get(task.zone) == r.id:
            del self._zone_busy[task.zone]
        r.queue.pop(0)
        r.current_task = None
        r.status = "idle" if r.queue else "done"
        planned = entry.end_h - entry.start_h
        took = t_end - self._started_at.get(task.id, entry.start_h)
        late = took - planned
        if late > self.overrun_tolerance_h + _EPS and task.id not in self._overrun_fired:
            self._overrun_fired.add(task.id)
            self._emit(Event(
                t_h=t_end, kind="task_overrun", robot=r.id, task=task.id,
                detail=(f"{task.id} took {took:g} h against the {planned} h scheduled, "
                        f"finishing at {t_end:g} h on {r.id} ({late:g} h over)"),
            ))

    def _fire_injections(self) -> None:
        while (self._next_injection < len(self._injections)
               and self._injections[self._next_injection].t_h <= self._t + _EPS):
            inj = self._injections[self._next_injection]
            self._next_injection += 1
            r = self._robots[inj.robot]
            if r.status == "failed":
                continue
            task = r.current_task
            if task is None:
                what = "while idle"
            else:
                verb = "working on" if r.status == "working" else "heading to"
                what = f"while {verb} {task} ({self._progress_of(task):.0%} done)"
                if self._zone_busy.get(self._tasks[task].zone) == r.id:
                    del self._zone_busy[self._tasks[task].zone]
            r.status = "failed"
            self._emit(Event(t_h=self._t, kind="robot_failure", robot=r.id, task=task,
                             detail=f"{r.id} failed at {self._t:g} h {what}"))

    def _remaining_need_wh(self, r: _RobotRT) -> float:
        """What the plan in force still asks of ``r``: the rest of its current task pro rata,
        every queued task in full and the legs from where it stands, zone to zone."""
        need = 0.0
        pos = r.pos
        for entry in r.queue:
            task = self._tasks[entry.task]
            target = self._layout[task.zone]
            need += self.travel_wh_per_cell * geometry.distance(pos, target)
            pos = target
            worked = self._worked_h.get(task.id, 0.0) if entry.task == r.current_task else 0.0
            need += task.energy_wh * max(0.0, task.duration_h - worked) / task.duration_h
        return need

    def _check_batteries(self) -> None:
        for r in self._robots.values():
            if r.status == "failed" or r.battery_low_fired or not r.has_work:
                continue
            need = self._remaining_need_wh(r)
            if r.battery_wh < need - _BATTERY_EPS_WH:
                r.battery_low_fired = True
                tasks = [e.task for e in r.queue]
                what = ", ".join(tasks[:3]) + (f" and {len(tasks) - 3} more" if len(tasks) > 3
                                               else "")
                self._emit(Event(
                    t_h=self._t, kind="battery_low", robot=r.id, task=r.current_task,
                    detail=(f"{r.id} has {r.battery_wh:.0f} Wh left but its remaining work "
                            f"({what}, travel included) needs {need:.0f} Wh; it will run dry "
                            "under the current plan"),
                ))

    def _emit(self, event: Event) -> None:
        self._events.append(event)
        self._run_events.append(event)

    def _progress_of(self, task_id: str) -> float:
        if task_id in self._done:
            return 1.0
        task = self._tasks.get(task_id)
        if task is None:
            return 0.0
        base = self._base_progress.get(task_id, 0.0)
        worked = self._worked_h.get(task_id, 0.0)
        return min(1.0, base + (1.0 - base) * worked / task.duration_h)

    def _completed(self) -> bool:
        return all(tid in self._done for tid in self._required)

    def _finished_at(self) -> float:
        return max(self._done[tid] for tid in self._required)

    def _default_cap_h(self) -> float:
        latest_end = max((e.end_h for e in self._schedule.entries), default=0)
        return 3.0 * max(self._req.horizon_h, self._schedule.makespan_h, latest_end) + 24.0
