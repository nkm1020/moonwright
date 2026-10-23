"""Which tasks serve which structure: one resolution shared by the coverage check
(``moonwright.pipeline.validate``) and the volume advisories (``moonwright.pipeline.rates``).

A task's ``structure`` tag is free text the model fills with an id, a type, a label or another
case, so every reader of it must resolve it the same way, or a graph the validator accepts
escapes the volume check (seen in review: a pad tagged ``Landing pad S1`` or ``s1`` passed
coverage and got no ``volume:S1`` advisory). This module is import-free of both readers, so
``rates`` and ``validate`` can share it without a cycle. The same goes for reading the brief:
:func:`name_score` says how well a line names a structure (the coverage hints quote the
structure's own entry with it) and :func:`entry_line` picks that entry (the volume check reads
a pit's sloped walls from it, the eval's area check a stated lift count and a berm's base width).
:func:`haul_target` reads where a haul places its material, zone first, for the supply check and
for the volume check's hauls staged in a pile. Both brief readers read the brief by
:func:`brief_lines`, so an entry hard-wrapped over several lines (as
editors and users write markdown) is read whole: phase 3g read only its first line and told
the user a brief that states "placed and compacted in two\n   lifts" states no lift count.
:func:`entry_owners` says whose entry a line is by the name before its colon, or, when that
name is no type's, by the recorded figures the line states (phase 3j: the borrow strip's
coverage hint quoted the berm's entry, "2. Plume berm: ... (about 280 cubic metres of fill),
built from the pit spoil plus the borrow strip", because the strip's two type words outscored
the berm's one in the body of that line); the size reader of ``rates`` looks for a
statement's subject from the same name (:func:`entry_head`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Sequence

from moonwright.schemas import Structure, Task, TaskGraph

WORDS = re.compile(r"[a-z0-9]+")   # the words of a lower-cased line or structure type
FIGURE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(cm|m)\b", re.IGNORECASE)   # "1.2 m", "30 cm"
_HEADING = re.compile(r"^\s*#")      # a markdown heading names the document, never an entry
# a structure's own entry: an optional list marker, a short name, a colon ("1. Protective
# berm: 100 m long ...", "- Borrow pit: ..."); a sentence ("- The berm length ... firm
# layer: none ...") has a longer head or a full stop in it
_ENTRY_HEAD = re.compile(r"^\s*(?:(?:\d+[.)]|[-*\u2022])\s+)?[*_]*[^:.;]{1,60}:")
# the figures that size a body whatever else was recorded beside them: an entry whose name
# is no type's is still the structure's when it states these (:func:`entry_owners`)
_MAIN_FIGURES = ("length_m", "height_m")
# a line that starts a block of its own: a list item ("1. ...", "- ..."), a bold paragraph
# label ("**Fleet.** ...") or a table row; any other non-blank line continues the one before
_BLOCK_START = re.compile(r"^\s*(?:(?:\d+[.)]|[-*+\u2022])\s|\*\*|__|\|)")


def brief_lines(text: str | None) -> list[str]:
    """The logical lines of a brief: each list item, paragraph or heading with the lines that
    continue it joined on (a hard-wrapped entry, its continuation lines indented or not), so
    "1. Protective berm: 100 m long, ... placed and compacted in two\n   lifts and inspected."
    is one line. A blank line, a heading, a list item, a bold paragraph label and a table row
    each start a new one; a brief with one line per entry reads exactly as ``splitlines``."""
    out: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            out.append("")
        elif (out and out[-1].strip() and not _HEADING.match(out[-1])
              and not _HEADING.match(line) and not _BLOCK_START.match(line)):
            out[-1] = f"{out[-1].rstrip()} {line.strip()}"
        else:
            out.append(line)
    return [line for line in out if line.strip()]


def figures_m(line: str) -> list[float]:
    """Every length figure of a line in metres ("1.2 m", "30 cm")."""
    return [float(m[1]) / (100 if m[2].lower() == "cm" else 1) for m in FIGURE.finditer(line)]


def states_figures(line: str, structure: Structure,
                   keys: Collection[str] | None = None) -> bool:
    """Whether a brief line states every length figure the structure records (its ``*_m``
    keys, the ``min_`` ones aside, or only ``keys``): its entry under a type whose words name
    no line."""
    d = structure.dims()
    if keys is None:
        keys = [k for k in d if k.endswith("_m") and not k.startswith("min_")]
    wanted = [d[k] for k in keys if d.get(k, 0.0) > 0]
    figures = figures_m(line)
    return bool(wanted) and all(any(math.isclose(f, w, abs_tol=1e-6) for f in figures)
                                for w in wanted)


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


def haul_target(haul: Task, structures: Sequence[Structure]) -> Structure | None:
    """The structure a haul places its material in, read from its zone first as the coverage
    contract states it ("a haul task's zone is the zone where the material is PLACED";
    :func:`hauls_into`): the structure whose zone it is in, the one its tag names when several
    share that zone; None when no structure claims the zone (a stockpile or dump the
    requirements do not list: "Haul strip spoil to stockpile"). The supply check's target of a
    haul, and the volume check's test of a haul staged in a pile (phase 3i)."""
    here = [s for s in structures if s.zone is not None and s.zone == haul.zone]
    if haul.structure:
        named = [s for s in here if tag_names(haul.structure, s)]
        if named:
            return named[0]
    return here[0] if here else None


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


def entry_head(line: str) -> int | None:
    """Where the name of a line shaped like an entry ends, just past its colon (19 for "1.
    Protective berm: 100 m long ..."); None for a sentence, a bold paragraph label such as
    "**Site.**" or a heading."""
    if _HEADING.match(line):
        return None
    m = _ENTRY_HEAD.match(line)
    return None if m is None else m.end()


def entry_owners(line: str, structures: Sequence[Structure]) -> list[Structure]:
    """The structures whose entry ``line`` is, read from the name before its colon ("2. Plume
    berm: 70 m straight berm, ... built from the pit spoil plus the borrow strip" is the berm's
    whatever its body names): those of ``structures`` that name scores best (:func:`name_score`,
    the score alone, so a shared entry, "Pit and berm: ...", is each one's). A name that is
    none of their types' words stands for the structures whose recorded size the line states
    (:func:`states_figures`: Lightning typed crater_ramp's "3. Floor landing: a compacted 8 m
    by 8 m pad ..." ``pad``), else for those whose recorded length and height it states (run 3
    typed propellant_pit_and_berm's "2. Protective berm: a 30 m arc ..., 2.5 m high"
    ``borrow_strip`` and gave it a 4 m width the brief never states). [] when the line is not
    shaped like an entry (:func:`entry_head`) or is no entry of theirs ("Note: ...")."""
    end = entry_head(line)
    if end is None:
        return []
    head = line[:end].lower()
    words = set(WORDS.findall(head))
    named = [(s, key[0]) for s in structures if (key := name_score(head, words, s)) is not None]
    if not named:
        return ([s for s in structures if states_figures(line, s)]
                or [s for s in structures if states_figures(line, s, _MAIN_FIGURES)])
    best = max(score for _, score in named)
    return [s for s, score in named if score == best]


def entry_line(text: str | None, structure: Structure) -> str | None:
    """The line of ``text`` (a brief) that names ``structure`` best by :func:`name_score`: its
    own entry ("1. Storage pit: 8 m diameter, 2 m deep, ..."), which names it with more of
    its type's words than a site paragraph that mentions it in passing. On a tie a line
    shaped like an entry (a list marker, a short name and a colon) beats a sentence, then the
    earlier mention wins; a markdown heading is never an entry (seen in phase 3g: "# Access
    Ramp into Crater C-4" tied with the ramp's own entry and won on position, and a type of
    one matching word, "spool_berm", picked the constraint "- The berm length and height are
    fixed ..." over "1. Protective berm: ... compacted in two lifts"). Lines are the brief's
    logical lines (:func:`brief_lines`): a hard-wrapped entry is returned whole. None without
    a brief or a line that names it."""
    if not text:
        return None
    best: tuple[tuple[int, bool, int], str] | None = None
    for line in brief_lines(text):
        if _HEADING.match(line):
            continue
        low = line.lower()
        key = name_score(low, set(WORDS.findall(low)), structure)
        if key is None:
            continue
        ranked = (key[0], _ENTRY_HEAD.match(line) is not None, key[1])
        if best is None or ranked > best[0]:
            best = (ranked, line)
    return best[1] if best else None
