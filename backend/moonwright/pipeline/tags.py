"""Which tasks serve which structure: one resolution shared by the coverage check
(``moonwright.pipeline.validate``) and the volume advisories (``moonwright.pipeline.rates``).

A task's ``structure`` tag is free text the model fills with an id, a type, a label or another
case, so every reader of it must resolve it the same way, or a graph the validator accepts
escapes the volume check (seen in review: a pad tagged ``Landing pad S1`` or ``s1`` passed
coverage and got no ``volume:S1`` advisory). This module is import-free of both readers, so
``rates`` and ``validate`` can share it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from moonwright.schemas import Structure, Task, TaskGraph


def tag_names(tag: str, structure: Structure) -> bool:
    """Whether a task's ``structure`` tag names ``structure``: its id (any case), its type, or
    a label that contains the id as a word ("Landing pad S1")."""
    low = tag.strip().lower()
    if low == structure.id.lower() or low == structure.type.strip().lower():
        return True
    return structure.id.lower() in re.findall(r"[a-z0-9_.-]+", low)


def structure_tasks(structure: Structure, graph: TaskGraph,
                    structures: Sequence[Structure] | None = None) -> list[Task]:
    """Tasks that serve ``structure``: tagged with something that names it
    (:func:`tag_names`), or in its zone while tagged with nothing that names any structure of
    ``structures`` (default: ``structure`` alone), the tag being free text the model may fill
    with a type, a label or a different case."""
    known = list(structures) if structures is not None else [structure]

    def serves(t: Task) -> bool:
        if t.structure is not None and tag_names(t.structure, structure):
            return True
        if structure.zone is None or t.zone != structure.zone:
            return False
        return t.structure is None or not any(tag_names(t.structure, s) for s in known)

    return [t for t in graph.tasks if serves(t)]


def hauls_into(structure: Structure, graph: TaskGraph) -> list[Task]:
    """Hauler tasks placed in ``structure``'s zone whatever their tag: material a hauler
    drops in the berm's zone builds the berm, even when the model tagged the task with the
    structure the spoil came from ("haul pad spoil to berm line" tagged S1). Empty for a
    structure without a zone."""
    if structure.zone is None:
        return []
    return [t for t in graph.tasks if t.robot_type == "hauler" and t.zone == structure.zone]


def serving_tasks(structure: Structure, graph: TaskGraph,
                  structures: Sequence[Structure] | None = None) -> list[Task]:
    """:func:`structure_tasks` plus the hauls placed in the structure's zone
    (:func:`hauls_into`), in graph order without repeats: everything that counts for the
    structure's coverage and for its volume."""
    seen = {t.id for t in structure_tasks(structure, graph, structures)}
    seen |= {t.id for t in hauls_into(structure, graph)}
    return [t for t in graph.tasks if t.id in seen]
