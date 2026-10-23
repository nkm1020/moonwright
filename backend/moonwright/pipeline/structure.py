"""F1: natural-language brief -> Requirements (LLM, Nemotron Nano/Super).

Hard geometry rules (zone centres and depot inside the grid, radius bounds, unique zone ids)
live in :class:`moonwright.schemas.Site`, so a bad answer is re-asked by the client with the
validation error. What survives the schema is checked here for the soft rules the prompt
states, and the findings go into the step's ``payload["warnings"]`` for the UI; they never
fail the stage because the simulator copes with all of them.

An operation a structure lists (``Structure.operations``, phase 3c) that no robot of the
fleet can perform is dropped from the requirements (:func:`correct_operations`, phase 3j).
It used to stay listed beside a warning that "no sinter task will be required of it": in the
live habitat berm demo (tries 2 and 3) Lightning listed ``sinter`` on the berm, which the
brief never asks for and no printer of the fleet could do, the task-graph model planned a
printer task for it and the validator answered ``capability:T14`` twice. The planner never
sees the brief, so what the requirements list is what it plans; the note keeps what the
warning said ("structure S2: operations sinter dropped: the fleet has no printer, so no
sinter task will be required of it (add a printer and the operation again to plan it)").

The opposite slip is corrected too (:func:`restore_operations`, phase 3l): an operation a
structure's own entry states that the structuring omitted is written into the requirements
(:func:`moonwright.pipeline.validate.restored_operations`, the entry read clause by clause by
:func:`moonwright.pipeline.tags.entry_operations`: compacted, compaction, "compact the ...";
inspected, staked, checked; excavated, dug; hauled, "built from"; sintered; grade never, a
clause with a negation or a deferral never, nor the list or the item one heads, a word said of
another structure never, a constraint shaped like an entry never, an operation the fleet cannot
staff never) with an amber note ("structure S1: operations compact restored: its entry states
"subgrade compacted and inspected""). In run 3
Lightning typed front_loaded_excavation's pad ``excavator`` with operations [excavate, survey]
in 1 of 3 structurings and ``pad`` with the same two in the others: the 28 h of pad compaction
were never required, the one-robot-per-zone deadline no longer bit, and Super #1 "verified" the
designed-impossible brief at 60 h, in scope. The type stays as recorded: retyping is not this
stage's to do, and a garbled type has no kind rules (``validate.structure_kind``), so the
operations its entry states are its whole contract. It runs after :func:`recover_operations`,
which may read some of them out of malformed keys, and before :func:`correct_operations`, whose
drop rule never meets a restored operation; 3j's softening rule counts the entry's own words
as evidence (:func:`moonwright.pipeline.validate.unsupported_operations`), so the note that
restores an operation is never followed by one that makes it optional again. The eval judges a
saved plan against the same reading (:func:`moonwright.pipeline.validate.in_scope_gaps`).

Two defences against a small model's malformed answers (seen live in phase 3c) run before the
soft checks: :func:`recover_operations` salvages ``operations`` that strict JSON-schema
decoding scrambled into extra keys (``"operations': [": "excavate"``, ``"operations excavate":
[...]``) and reports it, and :func:`dimension_warnings` flags a structure without a single
numeric dimension, because the task-graph model sizes every task from those fields and never
sees the brief.

A recorded compaction lift count the brief does not state is corrected (:func:`correct_lifts`,
phase 3g): Lightning wrote crater_ramp's "in four 10 m segments" as ``lifts: 4`` and
access_road's "a 20 cm compacted lift" as ``lifts: 0.2``, and left small_fleet_long_berm's
"placed and compacted in two lifts" unrecorded; the task-graph model never sees the brief, so
it compacted the ramp four times over and the berm once. The structure stage now writes the
count the structure's own entry states into the requirements
(:func:`moonwright.pipeline.rates.with_judged_lifts`; the entry read whole, a recorded count
kept when the entry names lifts without a count we can read) and says so in a warning the
requirements card shows in amber ("structure S1: lifts 4 -> 1: the brief's entry for S1 states
no lift count we can read"), so the planner plans the count the eval's area check judges
(:func:`moonwright.pipeline.rates.lifts`).

A square or rectangular footprint recorded as a diameter is corrected the same way
(:func:`correct_footprints`, phase 3i): in the saved structurings of live runs 2 and 3
Lightning wrote the "12 m by 12 m" pit of habitat_foundation and habitat_berm_tight_horizon,
instrument_pad's "10 m by 10 m" pad and four_mast_pads' "each 8 m by 8 m" pads as
``diameter_m`` (beside a ``length_m``, once with an invented ``min_diameter_m``), and a 12 m
pit read as a bowl is 113.1 m3 of 144 (78.5 %), above the 70 % conflict, so the planner
under-dug it. The structure stage writes the rectangle the structure's own entry states into
the requirements (:func:`moonwright.pipeline.rates.with_judged_footprint`: ``diameter_m`` and
``min_diameter_m`` dropped, ``length_m`` x ``width_m`` the brief's, ``min_length_m`` x
``min_width_m`` only where a recorded minimum and the entry state a smaller rectangle; an entry
that calls the structure round, or states the diameter apart from the pair, keeps it) with an
amber warning ("structure S1: diameter_m 12 -> 12 m x 12 m: the brief's entry for S1 states a
12 m by 12 m footprint"), and the volume and area checks read the same rectangle from the
brief at judge time.

So are the sizes the brief states (:func:`correct_sizes`, phase 3j). A smaller size the brief
allows anywhere is read and tied to its structure: low_battery_pad (kind E) allows "any
diameter between 12 m and 20 m that fits" in a constraint, Lightning recorded no
``min_diameter_m`` in 6 of 6 structurings of runs 2 and 3, and the eval judged the plans that
dug a 16.8 m bowl, inside the allowed range, against 20 m (0 of 6 in scope in run 3). The
structure stage writes ``min_diameter_m: 12`` with an amber note ("structure S1:
min_diameter_m none -> 12: the brief allows "any diameter between 12 m and 20 m that
fits""). A figure the structure's entry gives only as a distance is dropped: run 3 recorded
the stockpile "30 m north of the pad" as a 30 m diameter ("structure S2: diameter_m 30
dropped: the brief's entry for S2 states 30 m only as a distance ("30 m north of the
pad")"), and the note stands for the one :func:`dimension_warnings` would add, since the
brief gives the stockpile no size to add. The volume and area checks read both from the brief
at judge time (:func:`moonwright.pipeline.rates.with_brief_sizes`).
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Collection
from typing import get_args

from moonwright.llm.base import LLMClient, LLMError, answer_failure
from moonwright.llm.prompts import structure_messages
from moonwright.pipeline.rates import (
    distance_figures,
    footprint_note,
    lift_note,
    size_notes,
    with_brief_sizes,
    with_judged_footprint,
    with_judged_lifts,
)
from moonwright.pipeline.validate import (
    restored_operations,
    unstaffed_operations,
    unsupported_operations,
    with_restored_operations,
)
from moonwright.schemas import LoopStep, Operation, Requirements
from moonwright.text import an, join_and

OPERATIONS: tuple[str, ...] = get_args(Operation)
_OPERATION_SET = frozenset(OPERATIONS)
_OPERATIONS_KEY = re.compile(r"^\W*operations?(?![A-Za-z])", re.IGNORECASE)
_WORDS = re.compile(r"[a-z]+")
_YES = frozenset({"yes", "true", "required", "asked", "y"})


def site_warnings(req: Requirements) -> list[str]:
    """Soft problems of a structured brief (empty when it is clean): overlapping zones, a
    structure whose zone is not declared (the simulator will place it), a structure without a
    zone, an operation a structure lists that the brief does not ask for while it asks for the
    alternative (``excavate`` on a berm "built from a borrow strip"; not required, see
    :func:`moonwright.pipeline.validate.unsupported_operations`), no depot (the simulator uses
    the grid's bottom centre). An operation the fleet has no robot type for is no longer a
    warning here: :func:`correct_operations` drops it and says so."""
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
        for op, instead in unsupported_operations(req, s):
            out.append(f"structure {s.id} lists {op} but the brief line about it asks for "
                       f"{instead}, not {op}: no {op} task will be required of it (drop the "
                       "operation on the requirements card if the brief does not want it)")
    if req.site.depot is None:
        out.append("no depot given; robots start at the bottom centre of the grid")
    return out


def operation_warnings(req: Requirements) -> list[str]:
    """One note per structure that lists an operation no robot type of the fleet can perform
    (:func:`moonwright.pipeline.validate.unstaffed_operations`): "structure S2: operations
    sinter dropped: the fleet has no printer, so no sinter task will be required of it (add a
    printer and the operation again to plan it)"; "structure S1: operations survey and sinter
    dropped: the fleet has no surveyor and no printer, so no survey or sinter task will be
    required of it (add a surveyor and a printer and the operations again to plan them)".
    Empty when the fleet staffs every listed operation; never a failure. Reads only
    (:func:`correct_operations` applies them)."""
    out: list[str] = []
    for s in req.structures:
        dropped = unstaffed_operations(req, s)
        if not dropped:
            continue
        ops = [op for op, _ in dropped]
        types = list(dict.fromkeys(robot_type for _, robot_type in dropped))
        lacks = " and ".join(f"no {robot_type}" for robot_type in types)
        them = ("the operations again to plan them" if len(ops) > 1
                else "the operation again to plan it")
        out.append(f"structure {s.id}: operations {join_and(ops)} dropped: the fleet has "
                   f"{lacks}, so no {' or '.join(ops)} task will be required of it (add "
                   f"{join_and([an(t) for t in types])} and {them})")
    return out


def correct_operations(req: Requirements) -> list[str]:
    """Drop from ``req`` every operation a structure lists that no robot type of the fleet can
    perform (module docstring) and return the notes of :func:`operation_warnings` for the
    structures changed; the coverage check and the requirements card then read the list the
    planner plans from."""
    warnings = operation_warnings(req)
    if warnings:
        req.structures = [
            s.model_copy(update={"operations": [op for op in s.operations if op not in dropped]})
            if (dropped := {op for op, _ in unstaffed_operations(req, s)}) else s
            for s in req.structures]
    return warnings


def restore_warnings(req: Requirements) -> list[str]:
    """One note per structure whose own entry in the brief states an operation it does not list
    and a robot of the fleet can perform
    (:func:`moonwright.pipeline.validate.restored_operations`): "structure S1: operations
    compact restored: its entry states "subgrade compacted and inspected"", "structure BERM:
    operations haul restored: its entry states "built from a borrow strip along its north
    side"". Empty without a brief or when every structure lists what its entry states; never a
    failure. Reads only (:func:`restore_operations` applies them)."""
    out: list[str] = []
    for s in req.structures:
        restored = restored_operations(req, s)
        if not restored:
            continue
        quotes = list(dict.fromkeys(f'"{quote}"' for _, quote in restored))
        out.append(f"structure {s.id}: operations {join_and([op for op, _ in restored])} "
                   f"restored: its entry states {join_and(quotes)}")
    return out


def restore_operations(req: Requirements) -> list[str]:
    """Append to ``req`` every operation a structure's own entry states that the structuring
    omitted (module docstring) and return the notes of :func:`restore_warnings` for the
    structures changed; the coverage check and the requirements card then read the list the
    planner plans from. Nothing changes without a brief."""
    warnings = restore_warnings(req)
    if warnings:
        req.structures = with_restored_operations(req).structures
    return warnings


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


def dimension_warnings(req: Requirements, placed: Collection[str] = ()) -> list[str]:
    """One warning per structure without a single numeric dimension. The task-graph model
    sizes every task from these fields and never sees the brief, so a structure without them
    gets invented quantities; never a failure, the user can add the numbers in the
    requirements card. A structure of ``placed`` (the ids whose recorded figures
    :func:`correct_sizes` dropped as distances) gets none: its entry gives no size to add, and
    the note on the drop already says so (seen in review: run 3 #1's stockpile, "30 m north of
    the pad", read "add the brief's numbers before planning" under an amber lead)."""
    return [f"structure {s.id} ({s.type}) has no dimensions: the planner sizes its tasks from "
            "numeric fields such as diameter_m, length_m, width_m, depth_m or height_m and "
            "never sees the brief, so add the brief's numbers before planning"
            for s in req.structures if not s.dims() and s.id not in placed]


def lift_warnings(req: Requirements) -> list[str]:
    """One warning per structure whose recorded compaction lifts the brief corrects
    (:func:`moonwright.pipeline.rates.lift_note`): "structure S1: lifts 4 -> 1: the brief's
    entry for S1 states no lift count we can read", "structure BERM: lifts none -> 2: the
    brief's entry for BERM states 2 compaction lifts". Empty without a brief or when every
    count stands; never a failure. Reads only (:func:`correct_lifts` applies them)."""
    return [f"structure {s.id}: {note}" for s in req.structures
            if (note := lift_note(s, req)) is not None]


def correct_lifts(req: Requirements) -> list[str]:
    """Write the lift count each structure's entry in the brief states into ``req``
    (:func:`moonwright.pipeline.rates.with_judged_lifts`) and return the warnings of
    :func:`lift_warnings` for the corrections made; nothing changes without a brief."""
    warnings = lift_warnings(req)
    if warnings:
        req.structures = [with_judged_lifts(s, req) for s in req.structures]
    return warnings


def footprint_warnings(req: Requirements) -> list[str]:
    """One warning per structure whose recorded diameter the brief corrects to a rectangle
    (:func:`moonwright.pipeline.rates.footprint_note`): "structure S1: diameter_m 12 -> 12 m x
    12 m: the brief's entry for S1 states a 12 m by 12 m footprint". Empty without a brief or
    when no entry states a rectangle for a recorded diameter; never a failure. Reads only
    (:func:`correct_footprints` applies them)."""
    return [f"structure {s.id}: {note}" for s in req.structures
            if (note := footprint_note(s, req)) is not None]


def correct_footprints(req: Requirements) -> list[str]:
    """Write the rectangle each structure's entry in the brief states over its recorded
    diameter into ``req`` (:func:`moonwright.pipeline.rates.with_judged_footprint`) and return
    the warnings of :func:`footprint_warnings` for the corrections made; nothing changes
    without a brief."""
    warnings = footprint_warnings(req)
    if warnings:
        req.structures = [with_judged_footprint(s, req) for s in req.structures]
    return warnings


def size_warnings(req: Requirements) -> list[str]:
    """One warning per size the brief states that the record lacks or contradicts
    (:func:`moonwright.pipeline.rates.size_notes`): "structure S1: min_diameter_m none -> 12:
    the brief allows "any diameter between 12 m and 20 m that fits"", "structure S2:
    diameter_m 30 dropped: the brief's entry for S2 states 30 m only as a distance ("30 m north
    of the pad")". Empty without a brief or when every size stands; never a failure. Reads
    only (:func:`correct_sizes` applies them)."""
    return [f"structure {s.id}: {note}" for s in req.structures for note in size_notes(s, req)]


def correct_sizes(req: Requirements) -> list[str]:
    """Write the sizes the brief states into ``req`` (module docstring;
    :func:`moonwright.pipeline.rates.with_brief_sizes`: a figure an entry gives only as a
    distance dropped, a smaller size the brief allows written as ``min_<dimension>_m``) and
    return the warnings of :func:`size_warnings` for the corrections made; nothing changes
    without a brief."""
    warnings = size_warnings(req)
    if warnings:
        req.structures = [with_brief_sizes(s, req) for s in req.structures]
    return warnings


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
        # the client's own account of the rejected answer when it attached one (module
        # docstring of moonwright.llm.tokenfactory, "Rejected answers")
        return None, LoopStep(
            stage="structure", ok=False, duration_s=time.perf_counter() - t0,
            usage=getattr(e, "usage", None), failure=answer_failure(e),
        )
    req.source_brief = brief
    warnings = [*recover_operations(req), *restore_operations(req), *correct_operations(req),
                *site_warnings(req), *correct_lifts(req), *correct_footprints(req)]
    placed = {s.id for s in req.structures if distance_figures(s, req)}
    warnings += [*correct_sizes(req), *dimension_warnings(req, placed)]
    return req, LoopStep(
        stage="structure", ok=True, duration_s=time.perf_counter() - t0, usage=usage,
        message=f"{len(req.structures)} structures, {len(req.robots)} robots, horizon {req.horizon_h}h",
        payload={"zones": len(req.site.zones), "grid": list(req.site.grid),
                 "depot": None if req.site.depot is None else list(req.site.depot),
                 "warnings": warnings},
    )
