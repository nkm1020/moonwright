"""F2 (+F5): Requirements -> TaskGraph, and repair of a rejected graph (LLM, Nemotron Ultra).

Every graph the model answers with (first graph, repair, replan) is grounded by the rate model
before it is returned: :func:`moonwright.pipeline.rates.apply_rates` recomputes duration_h and
energy_wh from each task's quantity and unit and keeps the model's own figures in
``estimated_*``. The step's ``payload`` carries the rate model's advisories
(``{"advisories": [...], "rate_model": True, "grounded": n}``); an answer that states no
quantity at all gets :data:`~moonwright.pipeline.rates.UNGROUNDED_ADVISORY`, and a whole plan
(a first graph and its repairs, never a replan's remaining graph) the ``volume:<structure>``
advisories of :func:`~moonwright.pipeline.rates.volume_advisories` and the
``supply:<structure>`` balance notes of :func:`~moonwright.pipeline.validate.supply_advisories`
(a body's hauls moving more m3 than the digs before them remove). The graph the
model was handed (the remaining graph of a replan, ``prior`` or ``baseline``; the previous graph
of a repair) is passed to ``apply_rates`` as ``keep`` together with the ids of the ``locked``
tasks (underway on a robot), so a task returned as handed over, or underway, keeps its
*remaining* hours and Wh instead of being re-sized to its full quantity, while a task whose
numbers the model edited is grounded from its quantity again.

A repair is told, besides the verdict, the ``advisories`` of the step that produced the graph
it repairs (:func:`~moonwright.llm.prompts.repair_messages` renders them under the FAILURE
block), so a 200 % double haul is cut in the same round as an energy verdict. A rejected
answer (every ask cut off, not JSON or off the schema; a request that failed) ends the step
with the client's own account of it when the client attached one, else one built from the
exception (:func:`~moonwright.llm.base.answer_failure`: ``schema:json`` / ``schema:shape``).
"""

from __future__ import annotations

import time
from collections.abc import Collection, Sequence

from moonwright.llm.base import FixtureMissing, LLMClient, LLMError, answer_failure
from moonwright.llm.prompts import repair_messages, replan_messages, taskgraph_messages
from moonwright.pipeline.rates import (
    UNGROUNDED_ADVISORY,
    apply_rates,
    is_engaged,
    volume_advisories,
)
from moonwright.pipeline.validate import supply_advisories
from moonwright.schemas import Failure, LoopStep, Requirements, TaskGraph


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
    locked: Collection[str] = (),
    advisories: Sequence[str] | None = None,
) -> tuple[TaskGraph | None, LoopStep]:
    """attempt=1 generates; attempt>=2 with ``prior``+``feedback`` repairs.

    ``prior`` without ``feedback`` is a replan: the prompt carries the remaining task graph
    itself (:func:`replan_messages`), so the model re-plans the work that is actually left
    instead of guessing it from task ids; ``context`` says what happened (what is done, which
    robot failed, time now); ``baseline`` is that remaining graph again for the repair rounds
    of a replan, and ``locked`` the ids of its tasks that are underway on a robot (the rate
    model leaves their numbers alone); ``advisories`` the warnings on ``prior`` a repair
    should resolve too (the planner passes those of the step that produced it; only a repair
    uses them). ``variant`` ("replan") selects the replan fixture offline; when that
    fixture is missing the first attempt keeps ``prior`` as-is and a repair attempt fails with
    ``feedback`` (the solver's verdict) rather than falling back to the original run's fixtures.

    Every failed step that asking again cannot help with (a missing or unreadable fixture)
    carries a ``message`` starting with ``"offline:"``: the planner's retry logic treats those
    as deterministic, unlike a live schema mismatch or transport error.
    """
    t0 = time.perf_counter()
    if prior is not None and feedback is not None:
        role = "repair"
        system, user = repair_messages(req, prior, feedback, context, baseline,
                                       advisories=list(advisories) if advisories else None)
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
        return None, LoopStep(
            stage=stage, attempt=attempt, ok=False, duration_s=time.perf_counter() - t0,
            usage=getattr(e, "usage", None), failure=answer_failure(e),
            # a fixture that exists but cannot be read is as deterministic as a missing one
            message=None if getattr(e, "retryable", True) else f"offline: {e}",
        )
    # the graph the model was handed: a replan's remaining graph (its numbers are the work
    # still outstanding) or the graph a repair answered
    handed = baseline if baseline is not None else prior
    graph, advisories = apply_rates(graph, keep=handed, locked=locked)
    if not is_engaged(graph, handed):
        advisories = [*advisories, UNGROUNDED_ADVISORY]
    # the m3 planned per structure against its dimensions (a re-scope made visible), and the
    # m3 a body's hauls move against the m3 dug before them: judged on a whole plan only. A replan
    # (``prior`` without ``feedback``, or a repair round with the remaining graph as
    # ``baseline``) plans the work that is LEFT, so a pad whose first bowl half is dug would
    # read as under-scoped there, and a haul of spoil already dug as unsupplied.
    whole_plan = baseline is None and not (prior is not None and feedback is None)
    if whole_plan:
        advisories = [*advisories, *volume_advisories(req, graph),
                      *supply_advisories(req, graph)]
    grounded = sum(1 for t in graph.tasks if t.estimated_duration_h is not None)
    return graph, LoopStep(
        stage=stage, attempt=attempt, ok=True, duration_s=time.perf_counter() - t0, usage=usage,
        message=f"{len(graph.tasks)} tasks",
        payload={"advisories": advisories, "rate_model": True, "grounded": grounded},
    )
