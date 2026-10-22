"""F1: natural-language brief -> Requirements (LLM, Nemotron Nano/Super).

Hard geometry rules (zone centres and depot inside the grid, radius bounds, unique zone ids)
live in :class:`moonwright.schemas.Site`, so a bad answer is re-asked by the client with the
validation error. What survives the schema is checked here for the soft rules the prompt
states, and the findings go into the step's ``payload["warnings"]`` for the UI; they never
fail the stage because the simulator copes with all of them. The same list carries an
operation a structure lists (``Structure.operations``, phase 3c) that no robot of the fleet
can perform: the coverage check skips it rather than ask for the impossible, so the planner
is told here that the brief's ask will go unplanned unless the fleet changes.

Two defences against a small model's malformed answers (seen live in phase 3c) run before the
soft checks: :func:`recover_operations` salvages ``operations`` that strict JSON-schema
decoding scrambled into extra keys (``"operations': [": "excavate"``, ``"operations excavate":
[...]``) and reports it, and :func:`dimension_warnings` flags a structure without a single
numeric dimension, because the task-graph model sizes every task from those fields and never
sees the brief.
"""

from __future__ import annotations

import math
import re
import time
from typing import get_args

from moonwright.llm.base import LLMClient, LLMError
from moonwright.llm.prompts import structure_messages
from moonwright.pipeline.validate import unstaffed_operations, unsupported_operations
from moonwright.schemas import Failure, LoopStep, Operation, Requirements, schema_conflict

OPERATIONS: tuple[str, ...] = get_args(Operation)
_OPERATION_SET = frozenset(OPERATIONS)
_OPERATIONS_KEY = re.compile(r"^\W*operations?(?![A-Za-z])", re.IGNORECASE)
_WORDS = re.compile(r"[a-z]+")
_YES = frozenset({"yes", "true", "required", "asked", "y"})


def site_warnings(req: Requirements) -> list[str]:
    """Soft problems of a structured brief (empty when it is clean): overlapping zones, a
    structure whose zone is not declared (the simulator will place it), a structure without a
    zone, an operation a structure lists that the fleet has no robot type for (the coverage
    check will not require it), an operation a structure lists that the brief does not ask
    for while it asks for the alternative (``excavate`` on a berm "built from a borrow strip";
    not required either, see :func:`moonwright.pipeline.validate.unsupported_operations`), no
    depot (the simulator uses the grid's bottom centre)."""
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
        for op, robot_type in unstaffed_operations(req, s):
            out.append(f"structure {s.id} lists {op} but the fleet has no {robot_type}: no "
                       f"{op} task will be required of it (add a {robot_type} or drop the "
                       "operation)")
        for op, instead in unsupported_operations(req, s):
            out.append(f"structure {s.id} lists {op} but the brief line about it asks for "
                       f"{instead}, not {op}: no {op} task will be required of it (drop the "
                       "operation on the requirements card if the brief does not want it)")
    if req.site.depot is None:
        out.append("no depot given; robots start at the bottom centre of the grid")
    return out


def recover_operations(req: Requirements) -> list[str]:
    """Salvage ``operations`` a small model scrambled into extra keys, in place; one note per
    structure touched (empty when every structure was well-formed).

    Seen live with strict ``json_schema`` decoding (Nemotron 3.5 Lightning, phase 3c): the
    model drifts into Python-style quoting or writes the operation words into the key, and
    the grammar keeps the output valid JSON, so a structure arrives with extras such as
    ``"operations': [": "excavate", "grade": null, "compact": true, "sinter": true`` or
    ``"operations excavate": ["excavate", "grade", "compact"]`` and an empty ``operations``
    list that disarms the coverage contract. Every extra key that starts with ``operations``
    is read for operation words (in the key and in a string or list value) and dropped; a bare
    operation word used as a key with a boolean / null / yes-no value is read as that flag and
    dropped; numeric extras (the dimensions) and other keys are never touched. The recovered
    words are appended to ``operations`` in the order found, without repeats, and the note
    names the keys so the requirements card shows what was read into the list.
    """
    notes: list[str] = []
    for s in req.structures:
        extras = s.model_extra
        if not extras:
            continue
        found: list[str] = []
        used: list[str] = []
        for key in list(extras):
            value = extras[key]
            words = _WORDS.findall(key.lower())
            if _OPERATIONS_KEY.match(key):
                found += [w for w in words[1:] if w in _OPERATION_SET] + _operation_words(value)
            elif len(words) == 1 and words[0] in _OPERATION_SET and _is_flag(value):
                if _truthy(value):
                    found.append(words[0])
            else:
                continue
            used.append(key)
            extras.pop(key)
        if used:
            s.operations = list(dict.fromkeys([*s.operations, *found]))
            keys = ", ".join(repr(k) for k in used)
            notes.append(f"structure {s.id}: operations [{', '.join(s.operations)}] recovered "
                         f"from malformed keys ({keys}); check them against the brief")
    return notes


def dimension_warnings(req: Requirements) -> list[str]:
    """One warning per structure without a single numeric dimension. The task-graph model
    sizes every task from these fields and never sees the brief, so a structure without them
    gets invented quantities; never a failure, the user can add the numbers in the
    requirements card."""
    return [f"structure {s.id} ({s.type}) has no dimensions: the planner sizes its tasks from "
            "numeric fields such as diameter_m, length_m, width_m, depth_m or height_m and "
            "never sees the brief, so add the brief's numbers before planning"
            for s in req.structures if not s.dims()]


def _operation_words(value: object) -> list[str]:
    if isinstance(value, str):
        return [w for w in _WORDS.findall(value.lower()) if w in _OPERATION_SET]
    if isinstance(value, list):
        return [w for item in value for w in _operation_words(item)]
    return []


def _is_flag(value: object) -> bool:
    return value is None or isinstance(value, bool | str)


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in _YES)


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
    warnings = [*recover_operations(req), *site_warnings(req), *dimension_warnings(req)]
    return req, LoopStep(
        stage="structure", ok=True, duration_s=time.perf_counter() - t0, usage=usage,
        message=f"{len(req.structures)} structures, {len(req.robots)} robots, horizon {req.horizon_h}h",
        payload={"zones": len(req.site.zones), "grid": list(req.site.grid),
                 "depot": None if req.site.depot is None else list(req.site.depot),
                 "warnings": warnings},
    )
