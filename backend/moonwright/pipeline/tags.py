"""Which tasks serve which structure: one resolution shared by the coverage check
(``moonwright.pipeline.validate``) and the volume advisories (``moonwright.pipeline.rates``).

A task's ``structure`` tag is free text the model fills with an id, a type, a label or another
case, so every reader of it must resolve it the same way, or a graph the validator accepts
escapes the volume check (seen in review: a pad tagged ``Landing pad S1`` or ``s1`` passed
coverage and got no ``volume:S1`` advisory). This module is import-free of both readers, so
``rates`` and ``validate`` can share it without a cycle. The same goes for reading the brief:
:func:`name_score` says how well a line names a structure (the coverage hints quote the
structure's own entry with it) and :func:`entry_line` picks that entry (the volume check reads
a pit's sloped walls from it).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence

from moonwright.schemas import Structure, Task, TaskGraph

WORDS = re.compile(r"[a-z0-9]+")   # the words of a lower-cased line or structure type


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


def name_score(low: str, words: Collection[str], structure: Structure) -> tuple[int, int] | None:
    """How well a brief line (lower-cased, with its word set) names ``structure``: each word
    of its type counts once and the type's head word ("berm" of "blast_berm") twice, the
    earlier the first mention the better; None when no word of the type occurs."""
    type_words = list(dict.fromkeys(WORDS.findall(structure.type.lower())))
    hits = [w for w in type_words if w in words]
    if not hits:
        return None
    score = sum(2 if w == type_words[-1] else 1 for w in hits)
    starts = [m.start() for w in hits if (m := re.search(rf"\b{re.escape(w)}\b", low)) is not None]
    return score, -(min(starts) if starts else len(low))


def entry_line(text: str | None, structure: Structure) -> str | None:
    """The line of ``text`` (a brief) that names ``structure`` best by :func:`name_score`: its
    own entry ("1. Storage pit: 8 m diameter, 2 m deep, ..."), which names it with more of
    its type's words, or as many words earlier, than a site paragraph that mentions it in
    passing. None without a brief or a line that names it."""
    if not text:
        return None
    best: tuple[tuple[int, int], str] | None = None
    for line in text.splitlines():
        low = line.lower()
        key = name_score(low, set(WORDS.findall(low)), structure)
        if key is not None and (best is None or key > best[0]):
            best = (key, line)
    return best[1] if best else None
