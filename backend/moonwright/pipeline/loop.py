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
note). ``repair`` (an answer that parsed): ``{"diff": {...}}`` from :func:`graph_diff` (tasks
paired across a renumbering by :func:`match_tasks`: ``renumbered`` id pairs, added / removed
ids, changed fields under the after id with ``from`` when the task was renumbered, renames
included, energy per robot type before and after, scope drift); an answer that repeats a task
id gets no diff, the validate step names the duplicate.
``schedule`` (failed, ``energy:<robot>`` in the core): ``{"budget": [...]}`` from
:func:`moonwright.pipeline.schedule.budget_rows`, one row per robot type whose work no split
across its batteries fits, one per robot whose own tasks exceed its battery and one per robot
alone in its type whose tasks fit only in an order the core rules out (``kind`` "type",
"robot" or "order", ``subject``, ``robots``, ``budget_wh``, ``demanded_wh``, ``travel_wh``,
exact Wh): the figures the hint sentence quotes rounded, for the UI's budget table; the model
never sees them.
``validate``: ``{"advisories": [...]}`` when the step that produced the graph carried
advisories (the rate model's estimate warnings, or its note that nothing was grounded).
The *final failed step* of a run that ends without a schedule (the verdict the repair budget
could not answer, or the rejected answer that ended the last round) carries
``{"suggestions": [...]}``: the solver-grounded resource changes of
:func:`moonwright.pipeline.relax.suggest_relaxations` as plain dicts (one per
:class:`~moonwright.schemas.Suggestion`), computed for the verdict on the last graph with the
planner's own :class:`SolveOptions` *before* the step is reported, so a ``/plan/stream``
client sees them with the step; ``PlanResult.suggestions`` carries the same list as models.
A replan (``prior_graph`` given) is only offered ``extend_horizon`` and ``add_robot``, and a
spare never takes the id of a robot the replan lost (``reserved_ids``; until the caller
passes them, the "Failed robots (unavailable): ..." sentence of the replan context is read).
The advice runs within ``suggest_budget_s`` seconds (0 disables it) and can never fail a run.

Rejected answers
----------------
A live answer the client could not turn into a graph (every ask cut off, not JSON or off the
schema; a request the SDK gave up on) ends its stage with a ``schema`` / ``llm_error`` step
whose failure is the client's own account (``moonwright.llm.tokenfactory``, "Rejected
answers"; the stages take it from the exception, :func:`moonwright.llm.base.answer_failure`):
the hint "Nemotron 3 Ultra did not return a valid task graph after 2 asks: the answer was cut
off at 32,768 tokens, then failed the schema" or "The Token Factory request to Nemotron 3
Ultra failed: APITimeoutError (Request timed out.)", one detail line per ask ("ask 1: cut off
at 32,768 tokens", "ask 2: task T3: 'after' must be a list"; an ask answered without
reasoning says "(reasoning off)") and the conflict ids ``schema:json`` (cut off, not JSON,
empty) and ``schema:shape`` (a schema violation), which replace the old ``schema:$`` /
``schema:<field>``. A client without that account (offline fixtures, scripted tests) keeps its
own hint and details under the same two ids. The planner's re-ask quotes the failure
(:func:`_regeneration_context` / the repair note), so the model is told how its last answer
ended, and the live client starts it on the rung of its token ladder the failed call left
(a doubled budget, or reasoning off after a cut at the cap). Offline fixture misses (``"offline:"`` steps)
are deterministic and never re-asked. The ``structure`` step gets the same treatment; the
explain stage never fails a run (template fallback).

What a repair is told
---------------------
Besides the verdict, a repair round sees the advisories of the step that produced the graph
it repairs (the rate model's ``estimate:<task>`` gaps, ``ungrounded``, the ``volume:<id>``
excess), passed as ``generate_task_graph(advisories=...)``, which renders them under the
FAILURE block (``repair_messages(advisories=...)``), so a 160 % haul is cut in the same round
as the energy verdict. An advisory whose id is also a conflict of the verdict (a
``volume:<id>`` shortfall is both, and since phase 3i a double count: an excess of 175 % or
more of the largest volume the brief allows, its text closing ", a double count") is left
out: the verdict already says it, the berm's triangular section included; any other
``volume:<id>`` excess is always kept, since the shortfall under the same id can be another
shape of the structure (a berm hauled 1.6 times over on a key trench dug short), and so is
every ``supply:<structure>`` balance note (a body's hauls move more m3 than the digs before
them remove): it is keyed by the structure, the verdict's ``supply:<haul>`` by a haul, and a
structure one of whose hauls is in the verdict gets no note at all.
``verdict`` feedback mode passes none. The validator's volume and supply checks judge a whole
plan only: ``validate_graph(volume=False)`` (which switches the supply check off with it) for
the remaining graph of a replan and its repairs, ``True`` otherwise; it also gets the
replan's ``robot_positions``, so its energy checks start the robots where the solver does.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import fields
from typing import Any, Literal

from moonwright.llm.base import LLMClient
from moonwright.pipeline.explain import explain_schedule
from moonwright.pipeline.rates import is_double_count, is_shortfall
from moonwright.pipeline.relax import suggest_relaxations
from moonwright.pipeline.schedule import (
    SolveOptions,
    budget_rows,
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
    Suggestion,
    Task,
    TaskGraph,
    to_mwh,
)

RETRYABLE_KINDS = frozenset({"schema", "llm_error"})
REPLAN_SUGGESTION_KINDS = ("extend_horizon", "add_robot")   # a replan cannot raise a battery
# the sentence of a replan context (moonwright.sim.replan._context) that lists the lost robots
_FAILED_ROBOTS_RE = re.compile(r"Failed robots \(unavailable\): (.+?)\.(?:\s|$)")
FEEDBACK_MODES = ("core", "verdict")
FeedbackMode = Literal["core", "verdict"]
ScopeMode = Literal["strict", "repair"]
Positions = Mapping[str, tuple[float, float]]

DIFF_FIELDS = ("name", "duration_h", "energy_wh", "quantity", "robot_type", "zone",
               "structure", "unit", "after")
RECHECK_NOTE = "independent check_schedule passed"
log = logging.getLogger(__name__)


def brief_key(brief: str, brief_id: str | None = None) -> str:
    """Stable key used for fixture lookup and recording."""
    if brief_id:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", brief_id).strip("_").lower()
    return "brief_" + hashlib.sha1(brief.strip().encode()).hexdigest()[:10]


def unstructured_result(step: LoopStep, brief: str) -> PlanResult:
    """The failed :class:`PlanResult` of a run whose brief the model could not structure:
    the failed ``structure`` step alone, its failure, and placeholder requirements."""
    return PlanResult(
        requirements=Requirements(robots=[{"id": "none", "type": "none", "battery_wh": 1}],
                                  horizon_h=1, source_brief=brief),
        steps=[step], success=False, failure=step.failure,
    )


def travel_modelled() -> bool:
    """Whether CP-SAT times and charges travel (the travel model adds ``robot_positions`` to
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
        suggest_budget_s: float = 6.0,
    ):
        if max_repairs < 0:
            raise ValueError("max_repairs must be >= 0")
        if max_answer_retries < 0:
            raise ValueError("max_answer_retries must be >= 0")
        if feedback_mode not in FEEDBACK_MODES:
            raise ValueError(f"feedback_mode must be one of {FEEDBACK_MODES}, got {feedback_mode!r}")
        if suggest_budget_s < 0:
            raise ValueError("suggest_budget_s must be >= 0")
        self.client = client
        self.max_repairs = max_repairs
        self.max_answer_retries = max_answer_retries
        self.feedback_mode: FeedbackMode = feedback_mode
        self.solver_time_limit_s = solver_time_limit_s
        self.explain = explain
        self.on_step = on_step
        # wall-clock budget of the solver-grounded advice on a failed run (module docstring,
        # "Step payloads"); 0 switches it off
        self.suggest_budget_s = suggest_budget_s

    # ------------------------------------------------------------------ public API
    def structure(self, brief: str, *, brief_id: str | None = None,
                  ) -> tuple[Requirements | None, LoopStep]:
        """F1 alone: the structured brief (None when the model failed) and its ``structure``
        step, reported to ``on_step`` as it happens. The first half of :meth:`plan`, exposed
        so a caller can plan several times from ONE structuring: the eval pairs its
        conditions this way, so arms and loop settings answer the same requirements instead
        of each sampling Lightning anew."""
        req, step = structure_brief(self.client, brief, key=brief_key(brief, brief_id))
        self._emit(step)  # reported now, before the task-graph stage starts
        return req, step

    def plan(self, brief: str, *, brief_id: str | None = None) -> PlanResult:
        key = brief_key(brief, brief_id)
        req, step = self.structure(brief, brief_id=brief_id)
        if req is None:
            return unstructured_result(step, brief)
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
        robot_ready_h: Mapping[str, float] | None = None,
        reserved_ids: Collection[str] | None = None,
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
        ``robot_positions`` (robot id -> grid cell) tells the travel model where each robot is
        now; it is passed on only when :class:`SolveOptions` models travel, and so is
        ``robot_ready_h`` (robot id -> the hour it may set off from there, a replan at a
        fractional hour). See the module docstring for how the two budgets treat verdicts and
        rejected answers.
        ``reserved_ids`` are robot ids the advice on a failed run must not hand to a spare (the
        robots a replan has lost); when None they are read from ``context``.
        """
        steps: list[LoopStep] = []
        max_repairs = self.max_repairs if max_repairs is None else max_repairs
        opts = self._solve_options(start_offset_h, robot_positions, robot_ready_h)
        replan = prior_graph is not None   # advised with REPLAN_SUGGESTION_KINDS only
        reserved = (list(reserved_ids) if reserved_ids is not None
                    else _failed_robots_in(context))
        required = list(required_tasks or [])
        underway = [e.task for e in (locked or [])]   # the rate model leaves their numbers alone
        repairs = 0   # verdict rounds (PlanResult.repairs)
        rounds = 0    # graph answers asked for, re-asks included (LoopStep.attempt)

        if start_with_prior:
            if prior_graph is None:
                raise ValueError("start_with_prior needs a prior_graph")
            graph: TaskGraph | None = prior_graph
            rounds = 1
            advisories: list[Any] | None = None   # the notes on the current graph's step
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

        # structure coverage and the volume check judge a whole plan: a replan's remaining
        # graph legitimately lacks the phases already built and the m3 already moved, so only
        # a fresh plan (and its repairs) is checked for them
        whole = prior_graph is None
        verdict, schedule = self._check(req, graph, opts, locked, required, steps,
                                        attempt=rounds, baseline=prior_graph, scope="strict",
                                        advisories=advisories, coverage=whole, volume=whole,
                                        final=max_repairs == 0, replan=replan,
                                        reserved=reserved)
        # `reported` is the failure the caller sees: always the full verdict, never the note
        # addressed to the model about a rejected answer, never the blanked "verdict" copy
        reported = verdict
        while verdict is not None and repairs < max_repairs:
            repairs += 1
            feedback = self._feedback(verdict)
            notes = self._repair_notes(advisories, feedback)
            retries = 0
            while True:
                rounds += 1
                new_graph, step = generate_task_graph(
                    self.client, req, key=key, attempt=rounds, prior=graph,
                    feedback=feedback, context=context, variant=variant, baseline=prior_graph,
                    locked=underway, advisories=notes,
                )
                if new_graph is not None and len(set(new_graph.ids())) == len(new_graph.tasks):
                    _attach(step, "diff", graph_diff(graph, new_graph))
                last = new_graph is None and (not _retryable(step)
                                              or retries >= self.max_answer_retries)
                if last:   # the run ends on this rejected answer: the verdict on `graph` stands
                    self._advise(step, req, graph, verdict, opts, locked, replan=replan,
                                 reserved=reserved)
                self._push(steps, step)
                if new_graph is not None or last:
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
            advisories = _advisories(step)
            verdict, schedule = self._check(req, graph, opts, locked, required, steps,
                                            attempt=rounds, baseline=prior_graph,
                                            scope="repair", advisories=advisories,
                                            coverage=whole, volume=whole, previous=previous,
                                            final=repairs >= max_repairs, replan=replan,
                                            reserved=reserved)
            reported = verdict

        if schedule is None:
            return PlanResult(requirements=req, task_graph=graph, steps=steps, repairs=repairs,
                              success=False, failure=reported,
                              suggestions=_suggestions_of(steps[-1]) if steps else [])

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

    def _repair_notes(self, advisories: Sequence[Any] | None, feedback: Failure,
                      ) -> list[str] | None:
        """The advisories a repair round is told besides the verdict (module docstring, "What a
        repair is told"): those of the step that produced the graph it repairs, minus any the
        verdict already says (:func:`_repeats_verdict`: a ``volume:<id>`` shortfall or double
        count is both, any other ``volume:<id>`` excess and a ``supply:<structure>`` balance
        note never are); none in ``verdict`` feedback mode, where the model learns the kind
        alone."""
        if not advisories or self.feedback_mode != "core":
            return None
        conflicts = set(feedback.conflicts)
        notes = [str(a) for a in advisories if not _repeats_verdict(str(a), conflicts)]
        return notes or None

    def _solve_options(self, start_offset_h: int, robot_positions: Positions | None,
                       robot_ready_h: Mapping[str, float] | None = None) -> SolveOptions:
        kwargs: dict[str, Any] = {"time_limit_s": self.solver_time_limit_s,
                                  "start_offset_h": start_offset_h}
        if robot_positions is not None and travel_modelled():
            kwargs["robot_positions"] = {r: (float(x), float(y))
                                         for r, (x, y) in robot_positions.items()}
        if robot_ready_h and travel_modelled():
            kwargs["robot_ready_h"] = {r: float(h) for r, h in robot_ready_h.items()}
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
        advisories: Sequence[Any] | None = None,
        coverage: bool = True,
        volume: bool = True,
        previous: TaskGraph | None = None,
        final: bool = False,
        replan: bool = False,
        reserved: Collection[str] = (),
    ) -> tuple[Failure | None, Schedule | None]:
        """Run scope check + validator, then the solver, then re-verify its output; append steps.

        Returns ``(failure, None)`` when the graph was rejected and ``(None, schedule)`` when it
        was scheduled and the schedule passed :func:`check_schedule` under the same options
        (``payload["verified"]``). ``attempt`` (the round of ``graph``) is stamped on both
        steps; ``baseline`` is the graph the ``required`` tasks must still match, under the
        rules of ``scope`` (module docstring, "Scope of a replan"); ``advisories`` ride on the
        validate step's payload; ``coverage`` enables the validator's structure coverage check
        and ``volume`` its volume check (both for fresh plans only, module docstring "What a
        repair is told"); the validator's energy checks start the robots at the positions in
        ``opts``, as the solver does; ``previous`` is the graph a repair
        answered, so a structure the repair stopped serving is reported with the tasks it
        lost. ``final`` says no repair round can follow this verdict, so a failed step gets
        the solver-grounded advice in its payload before it is reported (:meth:`_advise`);
        ``replan`` restricts that advice to what a replan can act on and ``reserved`` names
        robot ids a spare must not take.
        """
        t0 = time.perf_counter()
        failure = _merge(scope_check(graph, required, baseline, locked, scope),
                         validate_graph(req, graph, previous, coverage=coverage, volume=volume,
                                        robot_positions=opts.robot_positions))
        step = LoopStep(stage="validate", attempt=attempt, ok=failure is None,
                        failure=failure, duration_s=time.perf_counter() - t0,
                        message=None if failure else f"{len(graph.tasks)} tasks valid",
                        payload={"advisories": list(advisories)} if advisories else None)
        if failure is not None and final:
            self._advise(step, req, graph, failure, opts, locked, replan=replan,
                         reserved=reserved)
        self._push(steps, step)
        if failure is not None:
            return failure, None

        t0 = time.perf_counter()
        out = solve_schedule(req, graph, opts, locked=locked)
        if isinstance(out, Failure):
            budget = _budget(req, graph, out, opts, locked)
            step = LoopStep(stage="schedule", attempt=attempt, ok=False, failure=out,
                            duration_s=time.perf_counter() - t0,
                            payload={"budget": budget} if budget else None)
            if final:
                self._advise(step, req, graph, out, opts, locked, replan=replan,
                             reserved=reserved)
            self._push(steps, step)
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

    def _advise(
        self,
        step: LoopStep,
        req: Requirements,
        graph: TaskGraph,
        verdict: Failure | None,
        opts: SolveOptions,
        locked: list[ScheduleEntry] | None,
        *,
        replan: bool,
        reserved: Collection[str] = (),
    ) -> None:
        """Attach the solver-grounded advice for ``verdict`` (the failure on ``graph``) to the
        final failed ``step`` as ``payload["suggestions"]`` (plain dicts), before the step is
        reported, so a stream client sees it with the step. Computed with the planner's own
        ``opts`` and ``locked`` pins within ``suggest_budget_s`` (``reserved`` ids are never
        given to a spare); nothing is attached when there is nothing to say, and an error in
        the advice never costs the verdict."""
        if self.suggest_budget_s <= 0 or verdict is None:
            return
        try:
            found = suggest_relaxations(
                req, graph, verdict, opts, locked=locked,
                kinds=REPLAN_SUGGESTION_KINDS if replan else None,
                reserved_ids=list(reserved), budget_s=self.suggest_budget_s)
        except Exception:   # advice is a courtesy: the verdict stands whatever happens here
            log.warning("relaxation advice failed; the verdict is reported without it",
                        exc_info=True)
            return
        if found:
            _attach(step, "suggestions", [s.model_dump(mode="json") for s in found])


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
    then zone to zone, at 0.5 Wh per cell, each leg rounded up to a milli-Wh): exactly what
    CP-SAT charged that sequence, and what a disturbance-free replay spends. The key
    ``travel_allowance`` keeps its pre-3k name for the saved plans the UI reads: true when the
    solver times and charges travel.
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
    graph it was given) and ``after`` (its answer).

    Tasks are paired by :func:`match_tasks` (a stable identity, not the id alone), so a repair
    that splits one task and shifts every later id by one is reported as ``renumbered``
    (``[{"from": "T4", "to": "T5"}, ...]``: the pairs whose ids differ) and not as a
    re-scoping of every shifted task. ``added`` / ``removed`` are the after / before ids that
    found no partner, each id once. ``changed`` lists one entry per pair and field
    (:data:`DIFF_FIELDS`) under the *after* id, with ``from`` (the before id) when the pair
    was renumbered. A ``name`` entry is a rename (names are compared with case, whitespace and
    punctuation ignored and quoted as written), so a replacement or a scope cut that lives in
    the wording ("... and borrow pit" dropped) is visible. The ``after`` (predecessor) lists
    are compared through the renumbering and an entry's two lists are both written in the
    answer's ids (``before`` is the old list translated, a predecessor that no longer exists
    marked ``"T2 (removed)"``), so they read side by side. A dependent of a removed task that
    now waits for an added task has re-pointed (the dependents of a split task necessarily
    re-point to its halves): those edges belong to the removal / addition, not to the
    dependent. A dependent that merely lost its edge to a removed task (a haul that no longer
    waits for any excavation), or gained an edge to an added task without having lost one, has
    changed and is reported. ``scope_drift_pct`` is the signed change of the total
    ``energy_wh`` (the physical work the graph asks for) in percent of ``before``; it and the
    per-type / total figures do not depend on the pairing."""
    was = {t.id: t for t in before.tasks}
    now = {t.id: t for t in after.tasks}
    pairs = match_tasks(before, after)
    renumbering = dict(pairs)                                   # before id -> after id
    kept = set(renumbering.values())
    removed = [t for t in dict.fromkeys(before.ids()) if t not in renumbering]
    added = [t for t in dict.fromkeys(after.ids()) if t not in kept]
    gone, fresh = set(removed), set(added)
    changed = []
    for b_id, a_id in pairs:
        old, new = was[b_id], now[a_id]
        moved = {"from": b_id} if b_id != a_id else {}
        for field in DIFF_FIELDS:
            if field == "after":
                same = _same_edges(old, new, renumbering, gone, fresh)
                a = sorted(_after_id(p, renumbering, gone) for p in old.after)
                b = sorted(new.after)
            elif field == "name":
                same, a, b = _name_key(old) == _name_key(new), old.name, new.name
            else:
                a, b = _diff_value(old, field), _diff_value(new, field)
                same = a == b
            if not same:
                changed.append({"task": a_id, **moved, "field": field, "before": a, "after": b})
    energy_before, energy_after = _energy_by_type(before), _energy_by_type(after)
    total_before = sum(to_mwh(t.energy_wh) for t in before.tasks)
    total_after = sum(to_mwh(t.energy_wh) for t in after.tasks)
    drift = round(100.0 * (total_after - total_before) / total_before, 1) if total_before else 0.0
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "renumbered": [{"from": b, "to": a} for b, a in pairs if b != a],
        "per_type_energy_before": energy_before,
        "per_type_energy_after": energy_after,
        "scope_drift_pct": drift,
        "total_energy_wh_before": total_before / 1000,
        "total_energy_wh_after": total_after / 1000,
        "total_duration_h_before": sum(t.duration_h for t in before.tasks),
        "total_duration_h_after": sum(t.duration_h for t in after.tasks),
    }


def match_tasks(before: TaskGraph, after: TaskGraph) -> list[tuple[str, str]]:
    """Pair the tasks of ``before`` with those of ``after`` on a stable identity: ``(before id,
    after id)`` pairs in the order of ``after``, every task in at most one pair. Four rules,
    each applied to the tasks the earlier ones left unpaired:

    (a) the same normalised name (case, whitespace and punctuation ignored) and the same slot
        ``(robot_type, zone, structure, unit)``. A name one task on each side carries pairs
        the two whatever their ids. The tasks of a repeated name (the halves of a split
        written as "Excavate pad bowl" twice, a hauler fleet's "Haul spoil" tasks) pair in
        graph order when the answer renumbered its tasks (old T2, T3 -> new T3, T4 after an
        insertion, the only pairing consistent with a renumbering) and by id otherwise; the
        surplus of an uneven count is left over as added / removed.
    (b) the same normalised name in another slot (a task moved to another zone or re-typed),
        when both sides carry it equally often, paired like (a); an uneven count is a split
        or a merge and pairs nothing.
    (c) the same slot, when exactly one task on each side carries it: a task that was renamed
        and renumbered but kept its place.
    (d) the same id: the model kept an id on purpose through a rename and a move. Not when
        the before task's slot is shared by two or more unpaired tasks of the answer (a split,
        whichever half kept the id) or the after task's slot by two or more unpaired tasks of
        the given graph (a merge).

    Whatever remains unpaired was added or removed: two tasks that merely share a slot (the
    halves of a split) never take the place of one task, and two before tasks never share
    one after task."""
    was = {t.id: t for t in before.tasks}
    now = {t.id: t for t in after.tasks}
    partner: dict[str, str] = {}                                # after id -> before id
    free_before, free_after = dict.fromkeys(was), dict.fromkeys(now)   # in graph order

    def pair(b_id: str, a_id: str) -> None:
        partner[a_id] = b_id
        del free_before[b_id], free_after[a_id]

    shifted = False   # evidence of a renumbering: a unique name found under another id
    for key_of, uneven in ((_identity_key, True), (_name_key, False)):
        by_before = _grouped(key_of, [was[i] for i in free_before])
        by_after = _grouped(key_of, [now[i] for i in free_after])
        groups = [(b_ids, a_ids) for key, a_ids in by_after.items()
                  if (b_ids := by_before.get(key)) and (uneven or len(b_ids) == len(a_ids))]
        for b_ids, a_ids in groups:              # the unique names first: they show a shift
            if len(b_ids) == 1 == len(a_ids):
                pair(b_ids[0], a_ids[0])
                shifted = shifted or b_ids[0] != a_ids[0]
        for b_ids, a_ids in groups:
            if len(b_ids) == 1 == len(a_ids):
                continue
            b_ids, a_ids = list(b_ids), list(a_ids)
            if not shifted:
                for tid in [a for a in a_ids if a in b_ids]:
                    pair(tid, tid)
                    b_ids.remove(tid)
                    a_ids.remove(tid)
            for b_id, a_id in zip(b_ids, a_ids, strict=False):
                pair(b_id, a_id)
    by_before = _unique_by(_slot_key, [was[i] for i in free_before])
    by_after = _unique_by(_slot_key, [now[i] for i in free_after])
    for key, a_id in by_after.items():
        b_id = by_before.get(key)
        if b_id is not None:
            pair(b_id, a_id)
    for tid in [a for a in free_after if a in free_before]:
        halves = [a for a in free_after if _slot_key(now[a]) == _slot_key(was[tid])]
        merged = [b for b in free_before if _slot_key(was[b]) == _slot_key(now[tid])]
        if len(halves) < 2 and len(merged) < 2:
            pair(tid, tid)
    return [(partner[a_id], a_id) for a_id in now if a_id in partner]


_NON_WORD_RE = re.compile(r"[\W_]+")


def _name_key(task: Task) -> str | None:
    """The task's name with case, whitespace and punctuation taken out; None when nothing is
    left (a name that identifies nothing)."""
    return " ".join(_NON_WORD_RE.sub(" ", task.name).casefold().split()) or None


def _slot_key(task: Task) -> tuple[str, str, str | None, str | None]:
    return (task.robot_type, task.zone, task.structure, task.unit)


def _identity_key(task: Task) -> tuple[str, tuple[str, str, str | None, str | None]] | None:
    name = _name_key(task)
    return (name, _slot_key(task)) if name is not None else None


def _grouped(key_of: Callable[[Task], Any], tasks: Iterable[Task]) -> dict[Any, list[str]]:
    """``key -> task ids`` in the order given (None keys never count)."""
    groups: dict[Any, list[str]] = {}
    for task in tasks:
        key = key_of(task)
        if key is not None:
            groups.setdefault(key, []).append(task.id)
    return groups


def _unique_by(key_of: Callable[[Task], Any], tasks: Iterable[Task]) -> dict[Any, str]:
    """``key -> task id`` for the keys exactly one of ``tasks`` carries."""
    return {key: ids[0] for key, ids in _grouped(key_of, tasks).items() if len(ids) == 1}


def _after_id(pred: str, renumbering: Mapping[str, str], gone: Collection[str]) -> str:
    """A predecessor named by the given graph, in the answer's ids: renumbered, or marked
    when the task it names was removed; an id unknown to both graphs stays as written."""
    if pred in renumbering:
        return renumbering[pred]
    return f"{pred} (removed)" if pred in gone else pred


def _same_edges(old: Task, new: Task, renumbering: Mapping[str, str], gone: Collection[str],
                fresh: Collection[str]) -> bool:
    """Whether a pair's predecessor lists agree once read in the answer's ids. The edges of a
    re-point (an edge to a removed task given up, an edge to an added task taken on) belong
    to the removal / addition and are left out of the comparison, but only when the pair
    carries both, so a dependency that was simply dropped, or simply added, is never
    forgiven."""
    repointed = any(p in gone for p in old.after) and any(p in fresh for p in new.after)
    before = {_after_id(p, renumbering, gone) for p in old.after if not (repointed and p in gone)}
    after = {p for p in new.after if not (repointed and p in fresh)}
    return before == after


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


def _budget(req: Requirements, graph: TaskGraph, verdict: Failure, opts: SolveOptions,
            locked: list[ScheduleEntry] | None) -> list[dict[str, Any]]:
    """The budget rows behind a solver verdict's ``energy:<robot>`` conflicts
    (:func:`moonwright.pipeline.schedule.budget_rows`) for the failed ``schedule`` step's
    payload: a UI-only channel, never part of what a repair round is told. ``[]`` when the
    verdict has no energy conflict, and an error here never costs the verdict."""
    try:
        return budget_rows(req, graph, verdict, opts, locked)
    except Exception:   # the table is a courtesy: the verdict stands whatever happens here
        log.warning("budget rows failed; the verdict is reported without them", exc_info=True)
        return []


def _advisories(step: LoopStep) -> list[Any] | None:
    advisories = (step.payload or {}).get("advisories")
    return list(advisories) if advisories else None


def _repeats_verdict(advisory: str, conflicts: Collection[str]) -> bool:
    """Whether an advisory says what a conflict of the verdict already says: its id is one of
    ``conflicts``, except a ``volume:<id>`` excess that is no double count, which no verdict
    carries (a berm hauled 1.6 times over shares ``volume:S2`` with the shortfall of its own
    key trench, seen in review: the excess went untold in the round that had to cut it; a
    double count, :func:`~moonwright.pipeline.rates.is_double_count`, is the verdict's own
    since phase 3i, its detail quoting the same dimensions), and a ``supply:<structure>``
    balance note (a body's hauls move more than the digs before them remove), which shares
    only the prefix with the verdict's ``supply:<haul>`` (a haul with no dig before it, or
    too little while another dig could supply it)."""
    aid = _advisory_id(advisory)
    if aid not in conflicts or aid.startswith("supply:"):
        return False
    return not aid.startswith("volume:") or is_shortfall(advisory) or is_double_count(advisory)


def _advisory_id(advisory: str) -> str:
    """'volume:S2' of 'volume:S2: its dimensions give ...', 'ungrounded' of 'ungrounded: ...':
    the conflict-shaped id an advisory starts with."""
    head, sep, rest = advisory.partition(":")
    if not sep:
        return advisory
    second, sep2, _ = rest.partition(":")
    return f"{head}:{second}" if sep2 and second and " " not in second else head


def _suggestions_of(step: LoopStep) -> list[Suggestion]:
    """The suggestions a final failed step carries (``payload["suggestions"]``) as models."""
    return [Suggestion.model_validate(s) for s in (step.payload or {}).get("suggestions") or []]


def _failed_robots_in(context: str | None) -> list[str]:
    """The robots a replan context reports lost ("Failed robots (unavailable): EX2, HL1."),
    so a spare suggested for the remaining fleet never takes one of their ids; [] when the
    context has no such sentence."""
    m = _FAILED_ROBOTS_RE.search(context or "")
    return [p.strip() for p in m.group(1).split(",") if p.strip()] if m else []


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
