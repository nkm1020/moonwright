"""F6: schedule -> natural-language explanation (LLM, Nemotron Super; template fallback)."""

from __future__ import annotations

import time

from moonwright.llm.base import ExplainText, LLMClient, LLMError
from moonwright.llm.prompts import explain_messages
from moonwright.schemas import LoopStep, Requirements, Schedule, TaskGraph


def template_explanation(req: Requirements, graph: TaskGraph, schedule: Schedule) -> str:
    by_robot = schedule.by_robot()
    busiest = max(by_robot.items(), key=lambda kv: sum(e.end_h - e.start_h for e in kv[1]))
    built = ", ".join(s.id for s in req.structures) or "the requested structures"
    parts = [
        f"The plan builds {built} with {len(req.robots)} robots in {len(graph.tasks)} tasks.",
        (f"Makespan is {schedule.makespan_h} h against a {req.horizon_h} h horizon "
         f"({schedule.status})."),
        f"{busiest[0]} carries the most work ({sum(e.end_h - e.start_h for e in busiest[1])} h).",
        ("The solver verified precedence, one task per robot at a time, one robot per zone, and "
         "per-robot battery budgets."),
    ]
    return " ".join(parts)


def explain_schedule(
    client: LLMClient, req: Requirements, graph: TaskGraph, schedule: Schedule, *, key: str
) -> tuple[str, LoopStep]:
    t0 = time.perf_counter()
    system, user = explain_messages(req, graph, schedule)
    try:
        out, usage = client.complete_json(
            role="explain", system=system, user=user, schema=ExplainText, key=key
        )
        return out.text, LoopStep(stage="explain", ok=True, usage=usage,
                                  duration_s=time.perf_counter() - t0)
    except LLMError as e:
        text = template_explanation(req, graph, schedule)
        return text, LoopStep(stage="explain", ok=True, duration_s=time.perf_counter() - t0,
                              message=f"template fallback ({type(e).__name__})")
