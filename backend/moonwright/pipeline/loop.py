"""Orchestrator: structure -> task graph -> validate -> schedule -> (repair x N) -> explain.

This is the F5 loop from the PRD. Every stage appends a :class:`LoopStep` so the UI can render
"구조화 -> 그래프 -> 검증 -> 스케줄 -> 수정 n회".

Progress callback
-----------------
``Planner(on_step=...)`` is called synchronously with every :class:`LoopStep` the moment it
exists, in production order: the ``structure`` step before the task-graph stage starts, then
``taskgraph`` / ``repair``, ``validate``, ``schedule`` and ``explain`` as they happen. The same
objects end up in ``PlanResult.steps`` in the same order. An exception raised by the callback
propagates out of the planner. ``validate`` and ``schedule`` steps carry the ``attempt`` of the
graph they judged (the round that produced it), so a verdict can be paired with the
``taskgraph`` / ``repair`` step that produced that graph.

Budgets
-------
Two budgets, kept apart so that "the model could not produce JSON" is never confused with
"the solver rejected the plan":

* ``max_repairs`` bounds the *verdict rounds*: repair prompts that answer a validator or
  solver verdict with a repaired graph. ``PlanResult.repairs`` counts them. ``max_repairs=0``
  is the "repair loop off" condition of the eval: the first valid graph's verdict is final.
* ``max_answer_retries`` bounds the *re-asks* after an answer that never reaches the verifier
  (schema mismatch after the client's own re-ask, a transport error): the same request is
  asked again, with a note about the rejection, up to that many times per round, in both
  conditions. A first graph that is rejected is regenerated the same way. Offline fixture
  misses are deterministic (their ``LoopStep.message`` starts with ``"offline:"``) and are
  never retried: the solver's verdict, or the miss itself, stands. A round whose re-asks are
  exhausted ends the run with the verdict it was answering.

``LoopStep.attempt`` is the running count of graph answers asked for (re-asks included), so
every LLM call of a run has its own number; ``PlanResult.repairs`` only counts verdict rounds.

Feedback modes
--------------
``feedback_mode="core"`` (default) feeds the repair prompt the full verdict: the conflict ids,
the numeric hint and the details. ``"verdict"`` blanks ``conflicts`` / ``hint`` / ``details``
and keeps only ``kind`` (the model learns that the plan was infeasible, not why): the ablation
arm of the eval that measures what the minimal core is worth. Whatever the mode, the failure
the caller sees (``PlanResult.failure``) is the full verdict, and it never carries the note
that tells the model its previous answer was rejected.

Solver-first runs
-----------------
``plan_from_requirements(..., prior_graph=g, start_with_prior=True)`` skips the first model
call: the prior graph (a replan's remaining work) goes straight to the validator and CP-SAT,
and the repair loop (the model) is entered only when that fails. The trace of such a run
starts with the ``validate`` step of attempt 1; its repair rounds are attempts 2, 3, ...

Scope of a replan
-----------------
``required_tasks`` are ids every graph must keep (the remaining work of a replan): a graph that
drops one fails verification with ``missing:<task>`` conflicts. With ``prior_graph`` as the
baseline, the model's *first* answer of a replan (the legacy path that asks the model to hand
back the remaining graph) must also keep every required task as it was (``changed:<task>``,
see :func:`moonwright.pipeline.validate.check_scope`). Repair rounds are different: a repair
is asked for because the remaining work is provably infeasible as it stands, so re-typing,
re-zoning or re-scoping tasks is exactly what the model is there for. Those rounds enforce
``missing:<task>`` only, surface every change in the repair step's ``diff`` payload, and refuse
one thing: a *locked* task (underway on a robot) whose definition changed, reported as
``locked:<task>`` (the robot working on it cannot become another type or move zone, and the
remaining hours and Wh are physical facts), which is what used to reach the solver and fail
its independent re-verification.

Step payloads
-------------
``schedule`` (ok): ``{"makespan_h", "status", "verified": True, "verification": {...}}``; the
``verification`` receipt is built by :func:`verification_receipt` (constraint counts, battery
use per robot as work plus the travel of its sequence, tightest robot, horizon use, solver
status / bound / time, whether travel was inside the model, and the independent re-check
note). ``repair`` (an answer that validated): ``{"diff": {...}}`` from :func:`graph_diff`
(added / removed / changed fields, energy per robot type before and after, scope drift).
``validate``: ``{"advisories": [...]}`` when the step that produced the graph carried
advisories (the rate model's estimate warnings, or its note that nothing was grounded).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import fields
from typing import Any, Literal

from moonwright.llm.base import LLMClient
from moonwright.pipeline.explain import explain_schedule
from moonwright.pipeline.schedule import (
    SolveOptions,
    check_schedule,
    sequence_travel,
    solve_schedule,
)
from moonwright.pipeline.structure import structure_brief
from moonwright.pipeline.taskgraph import generate_task_graph
from moonwright.pipeline.validate import check_scope, task_changes, validate_graph
from moonwright.schemas import (
    Failure,
    LoopStep,
    PlanResult,
    Requirements,
    Schedule,
    ScheduleEntry,
    Task,
    TaskGraph,
    to_mwh,
)

RETRYABLE_KINDS = frozenset({"schema", "llm_error"})
FEEDBACK_MODES = ("core", "verdict")
FeedbackMode = Literal["core", "verdict"]
ScopeMode = Literal["strict", "repair"]
Positions = Mapping[str, tuple[float, float]]

DIFF_FIELDS = ("duration_h", "energy_wh", "quantity", "robot_type", "zone", "after")
RECHECK_NOTE = "independent check_schedule passed"


def brief_key(brief: str, brief_id: str | None = None) -> str:
    """Stable key used for fixture lookup and recording."""
    if brief_id:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", brief_id).strip("_").lower()
    return "brief_" + hashlib.sha1(brief.strip().encode()).hexdigest()[:10]


def travel_modelled() -> bool:
    """Whether CP-SAT budgets travel (the travel allowance adds ``robot_positions`` to
    :class:`SolveOptions`); the verification receipt reports it honestly either way."""
    return any(f.name == "robot_positions" for f in fields(SolveOptions))


class Planner:
    def __init__(
        self,
        client: LLMClient,
        *,
        max_repairs: int = 3,
        max_answer_retries: int = 2,
        feedback_mode: FeedbackMode = "core",
        solver_time_limit_s: float = 10.0,
        explain: bool = True,
        on_step: Callable[[LoopStep], None] | None = None,
    ):
        if max_repairs < 0:
            raise ValueError("max_repairs must be >= 0")
        if max_answer_retries < 0:
            raise ValueError("max_answer_retries must be >= 0")
        if feedback_mode not in FEEDBACK_MODES:
            raise ValueError(f"feedback_mode must be one of {FEEDBACK_MODES}, got {feedback_mode!r}")
        self.client = client
        self.max_repairs = max_repairs
        self.max_answer_retries = max_answer_retries
        self.feedback_mode: FeedbackMode = feedback_mode
        self.solver_time_limit_s = solver_time_limit_s
        self.explain = explain
        self.on_step = on_step

    # ------------------------------------------------------------------ public API
    def plan(self, brief: str, *, brief_id: str | None = None) -> PlanResult:
        key = brief_key(brief, brief_id)
        req, step = structure_brief(self.client, brief, key=key)
        self._emit(step)  # reported now, before the task-graph stage starts
        if req is None:
            return PlanResult(
                requirements=Requirements(robots=[{"id": "none", "type": "none", "battery_wh": 1}],
                                          horizon_h=1, source_brief=brief),
                steps=[step], success=False, failure=step.failure,
            )
        result = self.plan_from_requirements(req, key=key)
        result.steps.insert(0, step)
        return result

    def plan_from_requirements(
        self,
        req: Requirements,
        *,
        key: str,
        prior_graph: TaskGraph | None = None,
        context: str | None = None,
        variant: str | None = None,
        start_offset_h: int = 0,
        locked: list[ScheduleEntry] | None = None,
        max_repairs: int | None = None,
        required_tasks: Collection[str] | None = None,
        start_with_prior: bool = False,
        robot_positions: Positions | None = None,
    ) -> PlanResult:
        """Generate (or, with ``prior_graph``, adapt) a task graph and schedule it.

        ``prior_graph`` + ``context`` are used by replanning: the LLM is asked to adapt the
        remaining tasks. ``variant`` (``"replan"``) tags every LLM call of the run, repairs
        included, so the offline client never answers a replan with the original run's
        ``taskgraph_<n>`` fixtures. ``start_offset_h`` shifts all start times (time already
        elapsed) and ``locked`` pins tasks that are already running to their robot/start.
        ``required_tasks`` are ids every graph must keep (the remaining work of a replan); see
        "Scope of a replan" in the module docstring for what is refused and when.
        ``start_with_prior`` makes the run solver-first: no model call for the first graph,
        ``prior_graph`` is verified as it is and the model is asked only to repair a verdict.
        ``robot_positions`` (robot id -> grid cell) tells the travel allowance where each robot
        is now; it is passed on only when :class:`SolveOptions` models travel. See the module
        docstring for how the two budgets treat verdicts and rejected answers.
        """
        steps: list[LoopStep] = []
        max_repairs = self.max_repairs if max_repairs is None else max_repairs
        opts = self._solve_options(start_offset_h, robot_positions)
        required = list(required_tasks or [])
        underway = [e.task for e in (locked or [])]   # the rate model leaves their numbers alone
        repairs = 0   # verdict rounds (PlanResult.repairs)
        rounds = 0    # graph answers asked for, re-asks included (LoopStep.attempt)

        if start_with_prior:
            if prior_graph is None:
                raise ValueError("start_with_prior needs a prior_graph")
            graph: TaskGraph | None = prior_graph
            rounds = 1
            advisories: list[Any] | None = None
        else:
            rounds = 1
            graph, step = generate_task_graph(
                self.client, req, key=key, attempt=rounds, prior=prior_graph,
                feedback=None, context=context, variant=variant, locked=underway,
            )
            self._push(steps, step)
            retries = 0
            while graph is None and _retryable(step) and retries < self.max_answer_retries:
                retries += 1
                rounds += 1
                graph, step = generate_task_graph(
                    self.client, req, key=key, attempt=rounds, prior=prior_graph,
                    feedback=None, context=_regeneration_context(context, step.failure),
                    variant=variant, locked=underway,
                )
                self._push(steps, step)
            if graph is None:
                return PlanResult(requirements=req, steps=steps, repairs=0, success=False,
                                  failure=step.failure)
            advisories = _advisories(step)

        # structure coverage judges a whole plan: a replan's remaining graph legitimately lacks
        # the phases already built, so only a fresh plan (and its repairs) is checked for it
        coverage = prior_graph is None
        verdict, schedule = self._check(req, graph, opts, locked, required, steps,
                                        attempt=rounds, baseline=prior_graph, scope="strict",
                                        advisories=advisories, coverage=coverage)
        # `reported` is the failure the caller sees: always the full verdict, never the note
        # addressed to the model about a rejected answer, never the blanked "verdict" copy
        reported = verdict
        while verdict is not None and repairs < max_repairs:
            repairs += 1
            feedback = self._feedback(verdict)
            retries = 0
            while True:
                rounds += 1
                new_graph, step = generate_task_graph(
                    self.client, req, key=key, attempt=rounds, prior=graph,
                    feedback=feedback, context=context, variant=variant, baseline=prior_graph,
                    locked=underway,
                )
                if new_graph is not None:
                    _attach(step, "diff", graph_diff(graph, new_graph))
                self._push(steps, step)
                if (new_graph is not None or not _retryable(step)
                        or retries >= self.max_answer_retries):
                    break
                retries += 1
                feedback = _with_rejection(self._feedback(verdict), step.failure)
            if new_graph is None:
                # an offline miss of its own kind is what the caller sees; a step that merely
                # echoes the verdict it was answering, or a live answer that stayed broken,
                # leaves the verdict standing
                failure = step.failure
                if failure is not None and failure.kind in RETRYABLE_KINDS and not _retryable(step):
                    reported = failure
                else:
                    reported = verdict
                break
            previous, graph = graph, new_graph
            verdict, schedule = self._check(req, graph, opts, locked, required, steps,
                                            attempt=rounds, baseline=prior_graph,
                                            scope="repair", advisories=_advisories(step),
                                            coverage=coverage, previous=previous)
            reported = verdict

        if schedule is None:
            return PlanResult(requirements=req, task_graph=graph, steps=steps, repairs=repairs,
                              success=False, failure=reported)

        explanation = None
        if self.explain:
            explanation, step = explain_schedule(self.client, req, graph, schedule, key=key)
            self._push(steps, step)
        return PlanResult(requirements=req, task_graph=graph, schedule=schedule,
                          explanation=explanation, steps=steps, repairs=repairs, success=True)

    # ------------------------------------------------------------------ internals
    def _emit(self, step: LoopStep) -> None:
        """Report ``step`` to ``on_step`` (synchronously; its exceptions propagate)."""
        if self.on_step is not None:
            self.on_step(step)

    def _push(self, steps: list[LoopStep], step: LoopStep) -> None:
        """Record ``step`` in the trace and report it."""
        steps.append(step)
        self._emit(step)

    def _feedback(self, verdict: Failure) -> Failure:
        """What a repair round is told: the verdict itself (``core``) or only its kind."""
        if self.feedback_mode == "verdict":
            return Failure(kind=verdict.kind)
        return verdict

    def _solve_options(self, start_offset_h: int, robot_positions: Positions | None) -> SolveOptions:
        kwargs: dict[str, Any] = {"time_limit_s": self.solver_time_limit_s,
                                  "start_offset_h": start_offset_h}
        if robot_positions is not None and travel_modelled():
            kwargs["robot_positions"] = {r: (float(x), float(y))
                                         for r, (x, y) in robot_positions.items()}
        return SolveOptions(**kwargs)

    def _check(
        self,
        req: Requirements,
        graph: TaskGraph,
        opts: SolveOptions,
        locked: list[ScheduleEntry] | None,
        required: list[str],
        steps: list[LoopStep],
        *,
        attempt: int = 1,
        baseline: TaskGraph | None = None,
        scope: ScopeMode = "strict",
        advisories: list[Any] | None = None,
        coverage: bool = True,
        previous: TaskGraph | None = None,
    ) -> tuple[Failure | None, Schedule | None]:
        """Run scope check + validator, then the solver, then re-verify its output; append steps.

        Returns ``(failure, None)`` when the graph was rejected and ``(None, schedule)`` when it
        was scheduled and the schedule passed :func:`check_schedule` under the same options
        (``payload["verified"]``). ``attempt`` (the round of ``graph``) is stamped on both
        steps; ``baseline`` is the graph the ``required`` tasks must still match, under the
        rules of ``scope`` (module docstring, "Scope of a replan"); ``advisories`` ride on the
        validate step's payload; ``coverage`` enables the validator's structure coverage check
        (fresh plans only) and ``previous`` is the graph a repair answered, so a structure the
        repair stopped serving is reported with the tasks it lost.
        """
        t0 = time.perf_counter()
        failure = _merge(scope_check(graph, required, baseline, locked, scope),
                         validate_graph(req, graph, previous, coverage=coverage))
        self._push(steps, LoopStep(stage="validate", attempt=attempt, ok=failure is None,
                                   failure=failure, duration_s=time.perf_counter() - t0,
                                   message=None if failure else f"{len(graph.tasks)} tasks valid",
                                   payload={"advisories": list(advisories)} if advisories else None))
        if failure is not None:
            return failure, None

        t0 = time.perf_counter()
        out = solve_schedule(req, graph, opts, locked=locked)
        if isinstance(out, Failure):
            self._push(steps, LoopStep(stage="schedule", attempt=attempt, ok=False, failure=out,
                                       duration_s=time.perf_counter() - t0))
            return out, None
        conflicts = check_schedule(req, graph, out, opts)
        if conflicts:
            # Independent re-verification of the solver's own output. A violation here is a
            # scheduler defect, not a task-graph problem: it must never reach the user as a
            # plan and must not be sent to the LLM as something to "repair".
            raise RuntimeError(
                "scheduler output failed independent re-verification: " + ", ".join(conflicts)
            )
        self._push(steps, LoopStep(stage="schedule", attempt=attempt, ok=True,
                                   duration_s=time.perf_counter() - t0,
                                   message=f"makespan {out.makespan_h}h ({out.status})",
                                   payload={"makespan_h": out.makespan_h, "status": out.status,
                                            "verified": True,
                                            "verification": verification_receipt(req, graph, out,
                                                                                 opts)}))
        return None, out


# ---------------------------------------------------------------------- scope
def scope_check(graph: TaskGraph, required: Collection[str], baseline: TaskGraph | None,
                locked: list[ScheduleEntry] | None, scope: ScopeMode) -> Failure | None:
    """The scope verdict on ``graph`` (module docstring, "Scope of a replan"): ``strict`` is
    :func:`check_scope` with the baseline (``missing`` + ``changed``); ``repair`` enforces
    ``missing`` and :func:`check_locked_tasks` only."""
    if scope == "strict":
        return check_scope(graph, required, baseline)
    missing = check_scope(graph, required, None)
    ignore = [c.removeprefix("missing:") for c in missing.conflicts] if missing else []
    return _merge(missing, check_locked_tasks(graph, baseline, locked, ignore_after=ignore))


def check_locked_tasks(graph: TaskGraph, baseline: TaskGraph | None,
                       locked: list[ScheduleEntry] | None,
                       ignore_after: Collection[str] = ()) -> Failure | None:
    """``locked:<task>`` for every locked task (underway on a robot) that ``graph`` redefines
    against ``baseline`` (:func:`task_changes`: robot type, zone, a dropped predecessor, less
    than the remaining duration or energy). None when every running task is intact. Edges to
    ``ignore_after`` tasks (ones already reported missing) are not counted."""
    if baseline is None or not locked:
        return None
    present = {t.id: t for t in graph.tasks}
    was = {t.id: t for t in baseline.tasks}
    bad: list[tuple[str, str, str]] = []
    for entry in locked:
        if entry.task in present and entry.task in was:
            why = task_changes(was[entry.task], present[entry.task], ignore_after=ignore_after)
            if why:
                bad.append((entry.task, entry.robot, "; ".join(why)))
    if not bad:
        return None
    ids = [t for t, _, _ in bad]
    them = "it" if len(ids) == 1 else "them"
    return Failure(
        kind="validation",
        conflicts=[f"locked:{t}" for t in ids],
        hint=(f"The graph redefined the running task(s) {', '.join(ids)}; a task underway on "
              "its robot keeps its robot_type, zone and after edges and at least its remaining "
              f"duration_h and energy_wh, so restore {them} and re-scope or split the other "
              "tasks instead."),
        details=[f"{t} is underway on {r} and changed: {why}" for t, r, why in bad],
    )


# ---------------------------------------------------------------------- payloads
def verification_receipt(req: Requirements, graph: TaskGraph, schedule: Schedule,
                         opts: SolveOptions | None = None) -> dict[str, Any]:
    """The ``verification`` payload of an ok ``schedule`` step: what CP-SAT proved and the
    independent re-check confirmed, in the figures the UI's receipt prints.

    A robot's ``used_wh`` is its ``work_wh`` plus the ``travel_wh`` of the legs its sequence
    actually drives (the first one from where ``opts`` says it starts, the depot by default,
    then zone to zone, at 0.5 Wh per cell): the figure a disturbance-free replay spends, and
    at most what CP-SAT reserved with its transit allowance.
    """
    tasks = {t.id: t for t in graph.tasks}
    work = {r.id: 0 for r in req.robots}  # milli-Wh of work per robot
    for entry in schedule.entries:
        if entry.robot in work and entry.task in tasks:
            work[entry.robot] += to_mwh(tasks[entry.task].energy_wh)
    travel = sequence_travel(req, graph, schedule, opts or SolveOptions())
    battery = []
    for robot in req.robots:
        budget = to_mwh(robot.battery_wh)
        legs = travel[robot.id].mwh if robot.id in travel else 0
        used = work[robot.id] + legs
        battery.append({
            "robot": robot.id,
            "used_wh": round(used / 1000, 3),
            "work_wh": round(work[robot.id] / 1000, 3),
            "travel_wh": round(legs / 1000, 3),
            "budget_wh": robot.battery_wh,
            "pct": round(100.0 * used / budget, 1) if budget else 0.0,
        })
    tightest = max(battery, key=lambda b: b["pct"])["robot"] if battery else None
    edges = {(p, t.id) for t in graph.tasks for p in t.after if p in tasks}
    return {
        "precedence_edges": len(edges),
        "robot_rows": len(req.robots),
        "zone_groups": len({t.zone for t in graph.tasks}),
        "battery": battery,
        "tightest_robot": tightest,
        "horizon_used_h": schedule.makespan_h,
        "horizon_h": req.horizon_h,
        "status": schedule.status,
        "objective_bound_h": schedule.objective_bound_h,
        "solve_time_s": round(schedule.solve_time_s, 3),
        "travel_allowance": travel_modelled(),
        "recheck": RECHECK_NOTE,
    }


def graph_diff(before: TaskGraph, after: TaskGraph) -> dict[str, Any]:
    """The ``diff`` payload of a repair step: what the model changed between ``before`` (the
    graph it was given) and ``after`` (its answer). ``changed`` lists one entry per task and
    field (:data:`DIFF_FIELDS`); ``scope_drift_pct`` is the signed change of the total
    ``energy_wh`` (the physical work the graph asks for) in percent of ``before``."""
    was = {t.id: t for t in before.tasks}
    now = {t.id: t for t in after.tasks}
    changed = []
    for tid, new in now.items():
        old = was.get(tid)
        if old is None:
            continue
        for field in DIFF_FIELDS:
            a, b = _diff_value(old, field), _diff_value(new, field)
            if a != b:
                changed.append({"task": tid, "field": field, "before": a, "after": b})
    energy_before, energy_after = _energy_by_type(before), _energy_by_type(after)
    total_before = sum(to_mwh(t.energy_wh) for t in before.tasks)
    total_after = sum(to_mwh(t.energy_wh) for t in after.tasks)
    drift = round(100.0 * (total_after - total_before) / total_before, 1) if total_before else 0.0
    return {
        "added": [t for t in after.ids() if t not in was],
        "removed": [t for t in before.ids() if t not in now],
        "changed": changed,
        "per_type_energy_before": energy_before,
        "per_type_energy_after": energy_after,
        "scope_drift_pct": drift,
        "total_energy_wh_before": total_before / 1000,
        "total_energy_wh_after": total_after / 1000,
        "total_duration_h_before": sum(t.duration_h for t in before.tasks),
        "total_duration_h_after": sum(t.duration_h for t in after.tasks),
    }


def _diff_value(task: Task, field: str) -> Any:
    value = getattr(task, field, None)  # `quantity` lands with the rate model
    if field == "after":
        return sorted(value or [])
    if field == "energy_wh":
        return round(float(value), 3)
    return value


def _energy_by_type(graph: TaskGraph) -> dict[str, float]:
    totals: dict[str, int] = {}
    for task in graph.tasks:
        totals[task.robot_type] = totals.get(task.robot_type, 0) + to_mwh(task.energy_wh)
    return {typ: totals[typ] / 1000 for typ in sorted(totals)}


def _attach(step: LoopStep, key: str, value: Any) -> None:
    """Add ``key`` to a step's payload before it is reported."""
    step.payload = {**(step.payload or {}), key: value}


def _advisories(step: LoopStep) -> list[Any] | None:
    advisories = (step.payload or {}).get("advisories")
    return list(advisories) if advisories else None


# ---------------------------------------------------------------------- helpers
def _merge(first: Failure | None, second: Failure | None) -> Failure | None:
    """One validation Failure out of two (conflicts and details stay aligned)."""
    if first is None:
        return second
    if second is None:
        return first
    return Failure(kind="validation", conflicts=first.conflicts + second.conflicts,
                   hint=f"{first.hint} {second.hint}".strip(),
                   details=first.details + second.details)


def _retryable(step: LoopStep) -> bool:
    """A rejected answer worth asking for again: a live schema mismatch or transport error.
    Offline fixture misses (message ``"offline: ..."``) are deterministic and are not."""
    return (step.failure is not None and step.failure.kind in RETRYABLE_KINDS
            and not (step.message or "").startswith("offline:"))


def _why(failure: Failure) -> str:
    why = failure.hint or failure.kind
    if failure.details:
        why += " (" + "; ".join(failure.details[:5]) + ")"
    return why


def _regeneration_context(context: str | None, failure: Failure | None) -> str:
    """Context for regenerating a rejected first graph: the original context plus the reason."""
    note = "The previous answer was rejected"
    if failure is not None:
        note += f": {_why(failure)}"
    note += ". Answer again with one valid JSON object."
    return f"{context}\n\n{note}" if context else note


def _with_rejection(verdict: Failure, rejection: Failure | None) -> Failure:
    """The verdict again, its hint extended with why the last repair answer was rejected, so
    the next round sees both. ``conflicts``/``details`` stay the verdict's (they are aligned);
    the note is built from the original verdict each time, so it never stacks."""
    if rejection is None:
        return verdict
    note = (f"Your previous repair answer was rejected and discarded: {_why(rejection)}. "
            "Repair the task graph above again and answer with one valid JSON object.")
    return verdict.model_copy(update={"hint": f"{verdict.hint} {note}".strip()})
