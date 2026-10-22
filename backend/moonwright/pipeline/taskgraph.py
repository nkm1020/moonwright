"""F2 (+F5): Requirements -> TaskGraph, and repair of a rejected graph (LLM, Nemotron Ultra)."""

from __future__ import annotations

import time

from moonwright.llm.base import FixtureMissing, LLMClient, LLMError
from moonwright.llm.prompts import repair_messages, replan_messages, taskgraph_messages
from moonwright.schemas import Failure, LoopStep, Requirements, TaskGraph, schema_conflict


def generate_task_graph(
    client: LLMClient,
    req: Requirements,
    *,
    key: str,
    attempt: int = 1,
    prior: TaskGraph | None = None,
    feedback: Failure | None = None,
    context: str | None = None,
    variant: str | None = None,
    baseline: TaskGraph | None = None,
) -> tuple[TaskGraph | None, LoopStep]:
    """attempt=1 generates; attempt>=2 with ``prior``+``feedback`` repairs.

    ``prior`` without ``feedback`` is a replan: the prompt carries the remaining task graph
    itself (:func:`replan_messages`), so the model re-plans the work that is actually left
    instead of guessing it from task ids; ``context`` says what happened (what is done, which
    robot failed, time now); ``baseline`` is that remaining graph again for the repair rounds
    of a replan. ``variant`` ("replan") selects the replan fixture offline; when that
    fixture is missing the first attempt keeps ``prior`` as-is and a repair attempt fails with
    ``feedback`` (the solver's verdict) rather than falling back to the original run's fixtures.

    Every failed step that asking again cannot help with (a missing or unreadable fixture)
    carries a ``message`` starting with ``"offline:"``: the planner's retry logic treats those
    as deterministic, unlike a live schema mismatch or transport error.
    """
    t0 = time.perf_counter()
    if prior is not None and feedback is not None:
        role = "repair"
        system, user = repair_messages(req, prior, feedback, context, baseline)
    elif prior is not None:
        role = "taskgraph"  # same fixture family as a first graph: taskgraph_<variant>.json
        system, user = replan_messages(req, prior, context)
    else:
        role = "taskgraph"
        system, user = taskgraph_messages(req, context)
    stage = "repair" if role == "repair" else "taskgraph"
    try:
        graph, usage = client.complete_json(
            role=role, system=system, user=user, schema=TaskGraph,
            key=key, attempt=attempt, variant=variant,
        )
    except FixtureMissing as e:
        if prior is not None and variant and feedback is None:
            # offline replan: keep the remaining graph as-is
            return prior, LoopStep(stage=stage, attempt=attempt, ok=True,
                                   duration_s=time.perf_counter() - t0,
                                   message=f"offline: reused prior graph ({e})")
        if prior is not None and variant:
            # offline replan repair: nothing canned to try, so the solver's verdict stands
            return None, LoopStep(stage=stage, attempt=attempt, ok=False,
                                  duration_s=time.perf_counter() - t0, failure=feedback,
                                  message=f"offline: no repair fixture ({e})")
        return None, LoopStep(stage=stage, attempt=attempt, ok=False,
                              duration_s=time.perf_counter() - t0,
                              failure=Failure(kind="llm_error", hint=str(e),
                                              details=list(getattr(e, "details", []))),
                              message=f"offline: {e}")
    except LLMError as e:
        errors = list(getattr(e, "errors", []))
        return None, LoopStep(
            stage=stage, attempt=attempt, ok=False, duration_s=time.perf_counter() - t0,
            usage=getattr(e, "usage", None),
            failure=Failure(kind="schema" if errors else "llm_error",
                            conflicts=[schema_conflict(x) for x in errors],
                            hint=str(e), details=errors),
            # a fixture that exists but cannot be read is as deterministic as a missing one
            message=None if getattr(e, "retryable", True) else f"offline: {e}",
        )
    return graph, LoopStep(
        stage=stage, attempt=attempt, ok=True, duration_s=time.perf_counter() - t0, usage=usage,
        message=f"{len(graph.tasks)} tasks",
    )
