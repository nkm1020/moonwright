"""F1: natural-language brief -> Requirements (LLM, Nemotron Nano/Super).

Hard geometry rules (zone centres and depot inside the grid, radius bounds, unique zone ids)
live in :class:`moonwright.schemas.Site`, so a bad answer is re-asked by the client with the
validation error. What survives the schema is checked here for the soft rules the prompt
states, and the findings go into the step's ``payload["warnings"]`` for the UI; they never
fail the stage because the simulator copes with all of them.
"""

from __future__ import annotations

import math
import time

from moonwright.llm.base import LLMClient, LLMError
from moonwright.llm.prompts import structure_messages
from moonwright.schemas import Failure, LoopStep, Requirements, schema_conflict


def site_warnings(req: Requirements) -> list[str]:
    """Soft geometry problems of a structured brief (empty when the layout is clean):
    overlapping zones, a structure whose zone is not declared (the simulator will place it),
    a structure without a zone, no depot (the simulator uses the grid's bottom centre)."""
    out: list[str] = []
    zones = req.site.zones
    for i, a in enumerate(zones):
        for b in zones[i + 1:]:
            gap = math.dist(a.center, b.center)
            if gap < a.radius + b.radius:
                out.append(f"zones {a.id} and {b.id} overlap (centres {gap:.1f} cells apart, "
                           f"radii {a.radius} + {b.radius})")
    declared = {z.id for z in zones}
    for s in req.structures:
        if s.zone is None:
            out.append(f"structure {s.id} has no zone")
        elif s.zone not in declared:
            out.append(f"structure {s.id} names zone {s.zone}, which the site does not declare")
    if req.site.depot is None:
        out.append("no depot given; robots start at the bottom centre of the grid")
    return out


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
    warnings = site_warnings(req)
    return req, LoopStep(
        stage="structure", ok=True, duration_s=time.perf_counter() - t0, usage=usage,
        message=f"{len(req.structures)} structures, {len(req.robots)} robots, horizon {req.horizon_h}h",
        payload={"zones": len(req.site.zones), "grid": list(req.site.grid),
                 "depot": None if req.site.depot is None else list(req.site.depot),
                 "warnings": warnings},
    )
