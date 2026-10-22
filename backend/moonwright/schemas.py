"""Shared data contract for the Moonwright pipeline.

Every stage boundary in the PRD exchanges one of these models:

    brief (str) --F1--> Requirements --F2--> TaskGraph --F3/F4--> Schedule | Failure
    Failure --F5--> (TaskGraph repaired by the LLM)
    Schedule --F7--> simulation frames + Event --F8--> replanned PlanResult

Conventions
-----------
* Time is integer hours in the planner (CP-SAT), float hours in the simulator.
* Energy is Wh. A robot's `battery_wh` is its whole budget for the plan; no recharge. Every
  energy comparison (validator, CP-SAT model, schedule checker) is made in integer milli-Wh
  (:func:`to_mwh`), so the three agree to 1e-3 Wh however the floats were produced (replans
  hand over 3-dp task energies and 2-dp remaining batteries).
* Ids of tasks, robots, zones and structures, and the ``zone`` / ``after`` references that name
  them, match :data:`ID_PATTERN` (letters, digits, ``_``, ``.``, ``-``): no whitespace, no
  ``->``, no ``:``, so every conflict id below can be split back into its parts.
* Numbers are finite (NaN / inf never validate) and bounded (see the ``MAX_*`` limits), so a
  wildly wrong LLM answer becomes a ``schema`` Failure instead of a crash further down.
* Physical grounding (phase 3a): the model proposes WHAT and HOW MUCH (``Task.quantity`` +
  ``Task.unit`` + ``Task.basis``); the deterministic rate model
  (``moonwright.pipeline.rates.apply_rates``) decides HOW LONG and HOW MUCH ENERGY for every
  task that carries a quantity, keeping the model's own guess in ``estimated_duration_h`` /
  ``estimated_energy_wh`` for the UI. Tasks without a quantity keep the model's numbers (the
  hand-made fixtures have none). The rate model's warnings travel as advisory strings
  ``estimate:<task_id>: model said ... (+170%)`` in ``LoopStep.payload["advisories"]`` of the
  ``taskgraph`` / ``repair`` step, plus ``ungrounded: ...`` when an answer states no quantity
  at all; they are never conflicts.
* Site geometry is validated at the schema boundary (:class:`Site`): zone centres and the
  depot inside the grid, ``1 <= radius <= min(grid) // 4``, unique zone ids. A violation is a
  ``ValueError`` naming the zone, so the structure stage re-asks the model with it.
* Conflict identifiers (``Failure.conflicts``) are machine-readable strings shared by the
  validator, the scheduler, the repair prompt and the UI. Formats:

    precedence:<task_i>-><task_j>   edge i -> j cannot be honoured
    energy:<robot_id>               robot's battery budget exceeded
    zone:<zone_id>                  one-robot-per-zone safety no-overlap infeasible
    robot:<robot_id>                per-robot no-overlap (robot double-booked / overloaded)
    horizon                         plan cannot finish within Requirements.horizon_h
    capability:<task_id>            no robot of the task's robot_type exists
    quantity:<task_id>              a physical task of a rated robot type states no quantity,
                                    a quantity of 0 or a unit the rate table does not price,
                                    in a graph that states quantities (see
                                    ``moonwright.pipeline.validate``)
    scope:<structure_id>            a structure of a known type lacks a required phase, a
                                    structure has no task at all, or a repair dropped every
                                    task the structure had (heuristic documented in
                                    ``moonwright.pipeline.validate``)
    reference:<task_id>.after=<id>  `after` points to an unknown task id
    duplicate:<id>                  duplicated task / robot / zone id
    cycle:<a>-><b>->...-><a>        precedence cycle
    energy_task:<task_id>           a single task needs more energy than any capable robot has
    horizon_path:<a>-><b>->...      critical path alone exceeds the horizon
    schema:<field>                  LLM output failed schema validation (``schema:$`` when the
                                    whole answer, not a field, was wrong; see
                                    :func:`schema_conflict`)

  The scope guard of a replan (``moonwright.pipeline.validate.check_scope``) reports:

    missing:<task_id>               a remaining task the new graph dropped
    changed:<task_id>               a remaining task the new graph redefined (robot_type, zone,
                                    a dropped `after` edge, less than the remaining duration or
                                    energy)

  A repair round of a replan (CP-SAT could not finish the remaining work, so Nemotron was
  asked; ``moonwright.pipeline.loop.check_locked_tasks``) may re-type, re-zone or re-scope
  work (the repair diff shows it) but is held to ``missing:<task_id>`` plus:

    locked:<task_id>                a task underway at the event was redefined (robot_type,
                                    zone, a dropped `after` edge, shorter duration or less
                                    energy); its robot is on it, so the answer is refused and
                                    the previous plan stays in force

  ``moonwright.pipeline.schedule.check_schedule`` (independent re-verification of a schedule)
  additionally reports defects a solver output never has but a hand-written schedule can:

    missing:<task_id>               task of the graph that is not scheduled
    unknown:<task_id>               scheduled task that is not in the graph
    duration:<task_id>              end_h - start_h != the task's duration_h
    makespan                        makespan_h is not the latest end_h of the entries
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Stage = Literal[
    "structure", "taskgraph", "validate", "schedule", "repair", "explain", "simulate", "replan"
]
Role = Literal["structure", "taskgraph", "repair", "explain"]

# --------------------------------------------------------------------------- limits / units
ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
Id = Annotated[str, Field(pattern=ID_PATTERN)]

MAX_ENERGY_WH = 1e7          # battery_wh and energy_wh
MAX_DURATION_H = 10_000      # one task
MAX_HORIZON_H = 100_000      # whole plan
MAX_SPEED_CELLS_PER_H = 1e4
MAX_QUANTITY = 1e5           # one task's quantity (m3 / m2 / m / ea)
MWH_PER_WH = 1000

Unit = Literal["m3", "m2", "m", "ea"]


def to_mwh(wh: float) -> int:
    """Wh as integer milli-Wh: the fixed-point unit of every energy comparison."""
    return round(wh * MWH_PER_WH)


def fmt_wh(mwh: int) -> str:
    """Milli-Wh as a Wh figure with only the decimals it needs: 6200000 -> '6200',
    4200250 -> '4200.25', 4200251 -> '4200.251'."""
    whole, frac = divmod(mwh, MWH_PER_WH)
    return str(whole) if frac == 0 else f"{whole}.{frac:03d}".rstrip("0")


def schema_conflict(error: str) -> str:
    """``schema:<field path>`` for one validation error line formatted as ``"<loc>: <msg>"``
    (see ``moonwright.llm.base.validate_into``); ``schema:$`` when the location is empty, i.e.
    the answer as a whole was not what the schema expects."""
    loc = error.split(":", 1)[0].strip()
    return f"schema:{loc or '$'}"


class _Model(BaseModel):
    """Base of every contract model: NaN and +-inf never validate, whatever the field."""

    model_config = ConfigDict(allow_inf_nan=False)


# --------------------------------------------------------------------------- F1 output
class Zone(_Model):
    """A named work area on the site grid. Tasks reference zones; at most one robot may
    work inside a zone at a time (safety constraint)."""

    id: Id
    center: tuple[int, int]
    radius: int = Field(default=3, ge=1)


class Site(_Model):
    """The site grid, its work zones and the depot.

    Geometry rules (enforced here so a bad structure answer is re-asked, not simulated):
    every zone centre inside the grid (``0 <= x < grid_x``, ``0 <= y < grid_y``),
    ``1 <= radius <= max(1, min(grid) // 4)`` (a quarter of the shorter side: a zone is a work
    area, not the whole site), unique zone ids, and the depot inside the grid when given.
    Overlapping zones are allowed (the structure prompt discourages them).
    """

    name: str = "LS-1"
    grid: tuple[int, int] = (40, 40)
    zones: list[Zone] = Field(default_factory=list)
    depot: tuple[int, int] | None = None  # where robots start; defaults to grid centre bottom

    @staticmethod
    def max_zone_radius(grid: tuple[int, int]) -> int:
        return max(1, min(grid) // 4)

    @model_validator(mode="after")
    def _geometry(self) -> Site:
        gx, gy = self.grid
        problems: list[str] = []
        if gx < 1 or gy < 1:
            problems.append(f"grid {gx}x{gy} must be at least 1x1")
        max_radius = self.max_zone_radius(self.grid)
        seen: set[str] = set()
        for z in self.zones:
            if z.id in seen:
                problems.append(f"zone {z.id}: id is used by more than one zone")
            seen.add(z.id)
            x, y = z.center
            if not (0 <= x < gx and 0 <= y < gy):
                problems.append(
                    f"zone {z.id}: centre ({x}, {y}) is outside the {gx}x{gy} grid "
                    f"(0 <= x < {gx}, 0 <= y < {gy})"
                )
            if not (1 <= z.radius <= max_radius):
                problems.append(
                    f"zone {z.id}: radius {z.radius} is outside 1..{max_radius} "
                    f"(at most a quarter of the grid's shorter side)"
                )
        if self.depot is not None:
            dx, dy = self.depot
            if not (0 <= dx < gx and 0 <= dy < gy):
                problems.append(f"depot ({dx}, {dy}) is outside the {gx}x{gy} grid")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Structure(_Model):
    """Extra keys such as diameter_m / length_m / height_m are allowed (BIM-like JSON)."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    id: Id
    type: str  # landing_pad, blast_berm, access_road, habitat_foundation, ...
    zone: Id | None = None

    @model_validator(mode="after")
    def _finite_extras(self) -> Structure:
        # extras are not typed, so the config above cannot catch a NaN / inf dimension
        for key, value in (self.model_extra or {}).items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number")
        return self

    def dims(self) -> dict[str, float]:
        return {k: float(v) for k, v in (self.model_extra or {}).items()
                if isinstance(v, (int, float))}


class Robot(_Model):
    id: Id
    type: str  # excavator, hauler, compactor, printer, surveyor, crane ...
    battery_wh: float = Field(gt=0, le=MAX_ENERGY_WH)
    speed_cells_per_h: float = Field(default=12.0, gt=0, le=MAX_SPEED_CELLS_PER_H)


class Requirements(_Model):
    """Structured brief (F1 output)."""

    site: Site = Field(default_factory=Site)
    structures: list[Structure] = Field(default_factory=list)
    robots: list[Robot] = Field(min_length=1)
    horizon_h: int = Field(gt=0, le=MAX_HORIZON_H)
    constraints: list[str] = Field(default_factory=list)  # free-text constraints from the brief
    source_brief: str | None = None

    def robots_of_type(self, robot_type: str) -> list[Robot]:
        return [r for r in self.robots if r.type == robot_type]

    def robot(self, robot_id: str) -> Robot:
        for r in self.robots:
            if r.id == robot_id:
                return r
        raise KeyError(robot_id)


# --------------------------------------------------------------------------- F2 output
class Task(_Model):
    """One unit of robot work.

    ``quantity`` / ``unit`` / ``basis`` are the model's scope statement; for a task that has
    them, ``duration_h`` and ``energy_wh`` are computed by the rate model
    (``moonwright.pipeline.rates``) and the model's own figures are kept in
    ``estimated_duration_h`` / ``estimated_energy_wh``. A task without a quantity (a pure time
    task, or a graph produced before the rate model) keeps the model's numbers.
    """

    id: Id
    name: str
    robot_type: str
    duration_h: int = Field(gt=0, le=MAX_DURATION_H)
    energy_wh: float = Field(ge=0, le=MAX_ENERGY_WH)
    zone: Id
    after: list[Id] = Field(default_factory=list)
    structure: str | None = None
    quantity: float | None = Field(
        default=None, ge=0, le=MAX_QUANTITY,
        description="How much work: m3 excavated or hauled, m2 stripped / graded / compacted / "
                    "sintered / surveyed, m of linear work, ea for discrete items; null for a "
                    "pure time task. Durations and energies are computed from it.",
    )
    unit: Unit | None = Field(default=None, description="Unit of quantity.")
    basis: str | None = Field(
        default=None,
        description="How quantity was derived from the structure's dimensions, "
                    "e.g. 'pi*(10 m)^2*0.4 m = 126 m3'.",
    )
    estimated_duration_h: float | None = Field(
        default=None, ge=0, le=MAX_DURATION_H,
        description="Filled by the rate model: the planner's own duration_h before it was "
                    "recomputed. Leave null.",
    )
    estimated_energy_wh: float | None = Field(
        default=None, ge=0, le=MAX_ENERGY_WH,
        description="Filled by the rate model: the planner's own energy_wh before it was "
                    "recomputed. Leave null.",
    )


class TaskGraph(_Model):
    tasks: list[Task] = Field(min_length=1)
    notes: str | None = None

    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def ids(self) -> list[str]:
        return [t.id for t in self.tasks]


# --------------------------------------------------------------------------- F4 output
class ScheduleEntry(_Model):
    task: str
    robot: str
    start_h: int = Field(ge=0)
    end_h: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> ScheduleEntry:
        if self.end_h < self.start_h:
            raise ValueError("end_h must be >= start_h")
        return self


class Schedule(_Model):
    entries: list[ScheduleEntry]
    makespan_h: int = Field(ge=0)
    status: Literal["optimal", "feasible"]
    objective_bound_h: int | None = None
    solve_time_s: float = 0.0

    def by_robot(self) -> dict[str, list[ScheduleEntry]]:
        out: dict[str, list[ScheduleEntry]] = {}
        for e in sorted(self.entries, key=lambda e: (e.start_h, e.task)):
            out.setdefault(e.robot, []).append(e)
        return out

    def entry(self, task_id: str) -> ScheduleEntry:
        for e in self.entries:
            if e.task == task_id:
                return e
        raise KeyError(task_id)


# --------------------------------------------------------------------------- F3/F4 -> F5
class Failure(_Model):
    """Why a plan was rejected. Fed back to the LLM verbatim (as JSON)."""

    kind: Literal["validation", "infeasible", "solver_timeout", "schema", "llm_error"]
    conflicts: list[str] = Field(default_factory=list)
    hint: str = ""
    details: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- F7 -> F8
class Event(_Model):
    t_h: float = Field(ge=0)  # finite: see _Model
    kind: Literal["robot_failure", "battery_low", "task_overrun"]
    robot: str | None = None
    task: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------- traces
class Usage(_Model):
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0


class LoopStep(_Model):
    """One entry of the visible progress trace (structure -> graph -> validate -> ...).

    ``attempt`` is the LLM round: 1 for the first task graph, n for the graph produced by
    repair round n-1. A ``validate`` / ``schedule`` step carries the round of the graph it
    judged, so a UI can pair every verdict with the repair that answered it.
    """

    stage: Stage
    attempt: int = 1
    ok: bool = True
    failure: Failure | None = None
    duration_s: float = 0.0
    usage: Usage | None = None
    message: str | None = None
    payload: dict[str, Any] | None = None  # small stage-specific extras for the UI


class PlanResult(_Model):
    requirements: Requirements
    task_graph: TaskGraph | None = None
    schedule: Schedule | None = None
    explanation: str | None = None
    steps: list[LoopStep] = Field(default_factory=list)
    repairs: int = 0
    success: bool = False
    failure: Failure | None = None

    def total_usage(self) -> Usage:
        u = Usage()
        for s in self.steps:
            if s.usage:
                u.input_tokens += s.usage.input_tokens
                u.output_tokens += s.usage.output_tokens
                u.latency_s += s.usage.latency_s
        return u


# --------------------------------------------------------------------------- simulation
class RobotState(_Model):
    id: str
    pos: tuple[float, float]
    battery_wh: float
    status: Literal["idle", "moving", "working", "failed", "done"] = "idle"
    current_task: str | None = None


class SimFrame(_Model):
    t_h: float
    robots: list[RobotState]
    done_tasks: list[str] = Field(default_factory=list)
    active_tasks: list[str] = Field(default_factory=list)
    progress: dict[str, float] = Field(default_factory=dict)  # task id -> 0..1


class ReplanRecord(_Model):
    """One replan of a simulation. ``after.schedule`` is in absolute simulator hours (shifted
    by ``floor(t_h)``), while ``after.requirements`` is what the planner was given: the
    surviving robots with their remaining batteries and ``horizon_h`` = the hours *left* at the
    event (relative); the ok ``schedule`` step's receipt quotes the brief's deadline."""

    event: Event
    t_h: float
    remaining_tasks: list[str]
    before: Schedule
    after: PlanResult
    context: str
    escalated: bool = False  # True when CP-SAT alone could not finish and Nemotron was asked


class SimResult(_Model):
    frames: list[SimFrame]
    events: list[Event] = Field(default_factory=list)
    replans: list[ReplanRecord] = Field(default_factory=list)
    completed: bool = False
    finished_at_h: float | None = None
    schedule: Schedule | None = None  # the schedule in force at the end
    task_graph: TaskGraph | None = None
