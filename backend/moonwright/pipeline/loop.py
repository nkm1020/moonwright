"""Orchestrator: structure -> task graph -> validate -> schedule -> (repair x N) -> explain.

This is the F5 loop from the PRD. Every stage appends a :class:`LoopStep` so the UI can render
"구조화 -> 그래프 -> 검증 -> 스케줄 -> 수정 n회".

Progress callback
-----------------
``Planner(on_step=...)`` is called synchronously with every :class:`LoopStep` the moment it
exists, in production order: the ``structure`` step before the task-graph stage starts, then
``taskgraph`` / ``repair``, ``validate``, ``schedule`` and ``explain`` as they happen. The same
objects end up in ``PlanResult.steps`` in the same order. An exception raised by the callback
propagates out of the planner. ``validate`` and ``schedule`` steps carry ``attempt`` =
``repairs + 1``, the round of the graph they judged, so a verdict can be paired with the
``taskgraph`` / ``repair`` step that produced that graph.

Repair budget
-------------
``max_repairs`` bounds the LLM rounds after the first graph (``PlanResult.repairs`` counts
them). A round normally answers a validator or solver verdict with a repaired graph. A round
whose answer never reaches the verifier (schema mismatch after the client's own re-ask, a
transport error) is retried while budget remains, with the same verdict plus a note about the
rejection, so one flaky answer cannot end a run; a first graph that is rejected is regenerated
the same way. Offline fixture misses are deterministic (their ``LoopStep.message`` starts with
``"offline:"``) and are never retried: the solver's verdict, or the miss itself, stands.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Collection

from moonwright.llm.base import LLMClient
from moonwright.pipeline.explain import explain_schedule
from moonwright.pipeline.schedule import SolveOptions, check_schedule, solve_schedule
from moonwright.pipeline.structure import structure_brief
from moonwright.pipeline.taskgraph import generate_task_graph
from moonwright.pipeline.validate import check_scope, validate_graph
from moonwright.schemas import (
    Failure,
    LoopStep,
    PlanResult,
    Requirements,
    Schedule,
    ScheduleEntry,
    TaskGraph,
)

RETRYABLE_KINDS = frozenset({"schema", "llm_error"})


def brief_key(brief: str, brief_id: str | None = None) -> str:
    """Stable key used for fixture lookup and recording."""
    if brief_id:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", brief_id).strip("_").lower()
    return "brief_" + hashlib.sha1(brief.strip().encode()).hexdigest()[:10]


class Planner:
    def __init__(
        self,
        client: LLMClient,
        *,
        max_repairs: int = 3,
        solver_time_limit_s: float = 10.0,
        explain: bool = True,
        on_step: Callable[[LoopStep], None] | None = None,
    ):
        self.client = client
        self.max_repairs = max_repairs
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
    ) -> PlanResult:
        """Generate (or, with ``prior_graph``, adapt) a task graph and schedule it.

        ``prior_graph`` + ``context`` are used by replanning: the LLM is asked to adapt the
        remaining tasks. ``variant`` (``"replan"``) tags every LLM call of the run, repairs
        included, so the offline client never answers a replan with the original run's
        ``taskgraph_<n>`` fixtures. ``start_offset_h`` shifts all start times (time already
        elapsed) and ``locked`` pins tasks that are already running to their robot/start.
        ``required_tasks`` are ids every graph must keep (the remaining work of a replan): a
        graph that drops one fails verification with ``missing:<task>`` conflicts, and when
        ``prior_graph`` is given as well, one that redefines a required task (another robot
        type or zone, a dropped predecessor, less than the remaining duration or energy) fails
        with ``changed:<task>``; both go back to the LLM for repair like any other verdict. See
        the module docstring for how the repair budget treats rejected answers.
        """
        steps: list[LoopStep] = []
        max_repairs = self.max_repairs if max_repairs is None else max_repairs
        opts = SolveOptions(time_limit_s=self.solver_time_limit_s, start_offset_h=start_offset_h)
        required = list(required_tasks or [])
        repairs = 0

        graph, step = generate_task_graph(
            self.client, req, key=key, attempt=1, prior=prior_graph,
            feedback=None, context=context, variant=variant,
        )
        self._push(steps, step)
        while graph is None and repairs < max_repairs and _retryable(step):
            repairs += 1
            graph, step = generate_task_graph(
                self.client, req, key=key, attempt=repairs + 1, prior=prior_graph,
                feedback=None, context=_regeneration_context(context, step.failure),
                variant=variant,
            )
            self._push(steps, step)
        if graph is None:
            return PlanResult(requirements=req, steps=steps, repairs=repairs, success=False,
                              failure=step.failure)

        verdict, schedule = self._check(req, graph, opts, locked, required, steps,
                                        attempt=repairs + 1, baseline=prior_graph)
        # `feedback` is what the next repair round is told (the verdict, plus a note when the
        # last answer was rejected); `reported` is the failure the caller sees, which never
        # carries that note because it is addressed to the model, not to the user
        feedback = reported = verdict
        while verdict is not None and repairs < max_repairs:
            repairs += 1
            new_graph, step = generate_task_graph(
                self.client, req, key=key, attempt=repairs + 1, prior=graph,
                feedback=feedback, context=context, variant=variant, baseline=prior_graph,
            )
            self._push(steps, step)
            if new_graph is None:
                if not _retryable(step):
                    reported = step.failure  # offline: the verdict (or the miss) stands
                    break
                feedback = _with_rejection(verdict, step.failure)
                continue
            graph = new_graph
            verdict, schedule = self._check(req, graph, opts, locked, required, steps,
                                            attempt=repairs + 1, baseline=prior_graph)
            feedback = reported = verdict

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
    ) -> tuple[Failure | None, Schedule | None]:
        """Run scope check + validator, then the solver, then re-verify its output; append steps.

        Returns ``(failure, None)`` when the graph was rejected and ``(None, schedule)`` when it
        was scheduled and the schedule passed :func:`check_schedule` (``payload["verified"]``).
        ``attempt`` (the round of ``graph``) is stamped on both steps; ``baseline`` is the
        graph the ``required`` tasks must still match (a replan's remaining work).
        """
        t0 = time.perf_counter()
        failure = _merge(check_scope(graph, required, baseline), validate_graph(req, graph))
        self._push(steps, LoopStep(stage="validate", attempt=attempt, ok=failure is None,
                                   failure=failure, duration_s=time.perf_counter() - t0,
                                   message=None if failure else f"{len(graph.tasks)} tasks valid"))
        if failure is not None:
            return failure, None

        t0 = time.perf_counter()
        out = solve_schedule(req, graph, opts, locked=locked)
        if isinstance(out, Failure):
            self._push(steps, LoopStep(stage="schedule", attempt=attempt, ok=False, failure=out,
                                       duration_s=time.perf_counter() - t0))
            return out, None
        conflicts = check_schedule(req, graph, out)
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
                                            "verified": True}))
        return None, out


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
