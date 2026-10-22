"""F1: natural-language brief -> Requirements (LLM, Nemotron Nano/Super)."""

from __future__ import annotations

import time

from moonwright.llm.base import LLMClient, LLMError
from moonwright.llm.prompts import structure_messages
from moonwright.schemas import Failure, LoopStep, Requirements, schema_conflict


def structure_brief(client: LLMClient, brief: str, *, key: str) -> tuple[Requirements | None, LoopStep]:
    t0 = time.perf_counter()
    system, user = structure_messages(brief)
    try:
        req, usage = client.complete_json(
            role="structure", system=system, user=user, schema=Requirements, key=key
        )
    except LLMError as e:
        errors = list(getattr(e, "errors", []))
        return None, LoopStep(
            stage="structure", ok=False, duration_s=time.perf_counter() - t0,
            usage=getattr(e, "usage", None),
            failure=Failure(kind="schema" if hasattr(e, "errors") else "llm_error",
                            conflicts=[schema_conflict(x) for x in errors],
                            hint=str(e), details=errors or list(getattr(e, "details", []))),
        )
    req.source_brief = brief
    return req, LoopStep(
        stage="structure", ok=True, duration_s=time.perf_counter() - t0, usage=usage,
        message=f"{len(req.structures)} structures, {len(req.robots)} robots, horizon {req.horizon_h}h",
    )
