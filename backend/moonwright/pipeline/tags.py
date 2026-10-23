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
statement's subject from the same name (:func:`entry_head`). :func:`entry_operations` reads
the operations a structure's own entry states (phase 3l), for the structure stage, which
restores the ones the structuring omitted, and for the eval's scope test, which judges a saved
plan against them.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Sequence
from itertools import pairwise

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
# the words of an operation an entry states (:func:`entry_operations`): the participles and
# nouns briefs write ("subgrade compacted and inspected", "a sintered crest", "built from the
# pit spoil"), the bare verb only as an order on what follows it ("compact the subgrade",
# "then sinter a 30 cm surface"), never as an adjective ("a compact pile"). Grade is never
# read: "a 15 percent grade" is a slope and "compacted level" a property (33 of the 42 omitted
# operations a crude probe of runs 2 and 3 found were such a "grade" or "level"); "built from"
# names the material a body is built from, never its parts ("built from two 60 m segments")
_ORDER = r"(?=\s+(?:the|a|an|its|their|each|every|all|both)\b)"
_BUILT_FROM = r"built\s+(?:up\s+)?from(?!\s+(?:\d|one|two|three|four|five|six|several)\b)"
_OPERATION_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("survey", re.compile(rf"\b(?:inspected|inspections?|surveyed|staked|checked|"
                          rf"(?:inspect|survey|stake|check){_ORDER})\b", re.IGNORECASE)),
    ("excavate", re.compile(rf"\b(?:excavated|excavating|dug|digging|(?:excavate|dig){_ORDER})\b",
                            re.IGNORECASE)),
    ("haul", re.compile(rf"\b(?:hauled|hauling|{_BUILT_FROM}|haul{_ORDER})\b", re.IGNORECASE)),
    ("compact", re.compile(rf"\b(?:compacted|compaction|compacting|compact{_ORDER})\b",
                           re.IGNORECASE)),
    ("sinter", re.compile(rf"\b(?:sintered|sintering|sinter{_ORDER})\b", re.IGNORECASE)),
)
# a negation or a deferral leaves its part of a clause unasked: "Sintering is deferred to a
# later sortie", "Sintering is a later job", "(not sintered)", "No sintering this sortie",
# "nothing is hauled", "compaction optional", "the bowl cannot be excavated as parallel
# sectors", "the existing track, already graded", "Compaction is out of scope for this
# sortie", "Compaction follows in the next sortie", "compaction excluded", "Sintering
# postponed to phase 2"
_NOT_ASKED = re.compile(r"\b(?:no|not|cannot|never|neither|nor|without|none|nothing|unless|"
                        r"instead|rather|already|defer\w*|postpon\w*|exclud\w*|later|"
                        r"optional\w*)\b|n't\b|\bout\s+of\s+scope\b|"
                        r"\bnext\s+(?:sortie|mission|phase|campaign)\b", re.IGNORECASE)
# what a negation that heads its part denies: a participle or an operation word after it ("not
# to be excavated", "already graded", "No sintering"), never anything else ("without blasting")
_PARTICIPLE = re.compile(r"\b(?:\w+ed|dug|built|laid|done)\b", re.IGNORECASE)
_LIST_END = re.compile(r"\b(?:or|nor)\b", re.IGNORECASE)   # "No sintering, compaction or ..."
_OPERATION_CLAUSE = re.compile(r"\.(?=\s|$)|[;,()]")   # what a note quotes; "0.4 m" stays whole
# a head that is a sentence, not a name: "- The pad is one work zone: plume and dust safety
# allows only one robot inside it at a time, so the bowl cannot be excavated as parallel
# sectors" is a constraint shaped like an entry; its words name no structure ("one", "work",
# "zone" read as the pit's names cost a berm's "compacted in one lift" its compact) and its
# body is no structure's entry
_SENTENCE_HEAD = re.compile(r"\b(?:is|are|was|were|be|must|may|can|cannot|will|shall|should|"
                            r"has|have)\b", re.IGNORECASE)
# a clause of time or condition: what it says is done is said of its own subject ("hauled only
# after the bowl is excavated and compacted" in a berm's entry is the bowl's dig)
_SUBORDINATE = frozenset({"after", "before", "once", "until", "when", "while"})
# the words a brief calls a whole structure by in passing; in a clause of time, or as what a
# relative clause is about, one that is none of this structure's names is another's, even when
# no structure is typed with it ("the bowl" of a pad typed landing_pad)
_STRUCTURE_NOUNS = frozenset({"pad", "bowl", "pit", "berm", "trench", "road", "ramp", "apron",
                              "mound", "stockpile", "foundation", "hardstand", "lane", "strip",
                              "track"})
_RELATIVE = re.compile(r"\s*(?:which|whose|where)\b", re.IGNORECASE)   # "..., which is ..."
# where a clause turns to another action or subject: "dug | after the pit walls are stable |
# and backfilled with compacted spoil | once the cable bed is inspected", "lightly compacted |
# so it does not blow away"; never "or" ("no sintering or compaction" denies both)
_OPERATION_CUT = re.compile(r"\b(?:and|but|while|whereas|though|although|yet|after|before|once|"
                            r"until|when|then|so|plus)\b", re.IGNORECASE)
# the words that may stand between an operation and the structure it is about: "excavated from
# the pit", "the pad is compacted", "compact the berm"
_LINKS = frozenset({"the", "a", "an", "its", "their", "of", "from", "in", "into", "on", "at",
                    "out", "inside", "within", "is", "are", "was", "were", "be", "been", "being"})
# a dig word on material says where the material came from, not that this structure is dug
# ("the excavated material placed on the plant's stockpile pad", "built from spoil excavated
# nearby", "built from regolith dug from a borrow strip"); compacted spoil is compacted where it
# lies ("backfilled with compacted spoil")
_MATERIAL = frozenset({"material", "spoil", "regolith", "fill", "soil", "fines", "rock", "cut"})
_QUOTE_MAX = 80


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


def entry_operations(text: str | None, structure: Structure,
                     structures: Sequence[Structure]) -> list[tuple[str, str]]:
    """``(operation, quote)`` for every operation ``structure``'s own entry in ``text`` (a
    brief) states, in the order the entry states them, each with the clause that states it
    first. Its own entry is a line :func:`entry_owners` gives it alone, read after its name;
    a shared entry ("1. Cargo pad A, 2. cargo pad B, 3. cargo pad C: each ...") is no one's,
    and a constraint whose head is a sentence ("- The pad is one work zone: ... so the bowl
    cannot be excavated as parallel sectors", :data:`_SENTENCE_HEAD`) is no entry. The entry
    is read clause by clause (cut at a full stop, a semicolon, a comma and a bracket, and
    within a clause where it turns to another action or subject, "and", "after", "once", "so",
    ...): an operation word (:data:`_OPERATION_WORDS`: "compacted", "compaction", "compact the
    ...", "inspected", "staked", "checked", "excavated", "dug", "hauled", "built from",
    "sintered", ...; grade never) states nothing in a part a negation or a deferral leaves
    unasked (:func:`_denials`: "no", "not", "cannot", "without", "already", "deferred", "a
    later sortie", "the next sortie", "out of scope", "instead of", "rather than", "unless",
    ...; the rest of a list one heads, "No sintering, compaction or inspection this sortie";
    a participle coordinated with one it heads, "must not be excavated and compacted"), nor
    when it is said of another structure: one named next to it ("excavated from the pit",
    "the pad is compacted") or before it in its part ("placing starts once the pad subgrade is
    compacted"), or in the part an "and ..." item is coordinated with ("the access road beside
    it is graded and compacted"), outside a "from ..." material phrase; a structure word none of
    this structure's names in a clause of time ("hauled only after the bowl is excavated");
    the noun a relative clause follows ("from the depot to the pad, which is compacted
    first"). The names of the others are their types' words and the names their own entries
    give them. A dig word on material states nothing either ("the excavated material placed
    on the plant's stockpile pad", "built from spoil excavated nearby": dug elsewhere). A haul
    is always this structure's, whatever it names: "built from the pit spoil" in the berm's
    entry is the berm's haul (and never an ask to excavate it), "hauled to the berm" in a
    borrow pit's entry the pit's material carried off. The quote is the clause, or, past
    :data:`_QUOTE_MAX` characters, the part that states the operation and the parts after it
    that fit (:func:`_clause_quote`). [] without a brief or an entry of its own. Seen in run 3:
    front_loaded_excavation's "1. Heavy-lander pad: circular, 30 m diameter, excavated 0.4 m
    to firm regolith across the full footprint (about 283 cubic metres), subgrade compacted
    and inspected. Sintering is deferred to a later sortie; no printer is on site." states
    excavate, compact and survey, never sinter."""
    if not text:
        return []
    own = _name_words(text, structure, structures)
    others = set().union(*(_name_words(text, s, structures) for s in structures
                           if s is not structure)) - own
    found: dict[str, tuple[int, str]] = {}   # operation -> (where in the entries, quote)
    offset = 0
    for line in _entries(text, structure, structures, alone=True):
        body = line[entry_head(line) or 0:]
        cuts = [0, *(i for m in _OPERATION_CLAUSE.finditer(body) for i in (m.start(), m.end())),
                len(body)]
        # the entry's sentences (and asides in brackets), each the spans of its comma clauses
        sentences: list[list[tuple[int, int]]] = []
        for start, end in zip(cuts[::2], cuts[1::2], strict=True):
            if not sentences or body[start - 1:start] != ",":
                sentences.append([])
            sentences[-1].append((start, end))
        previous = ""   # the clause before, for "..., which is compacted first"
        for sentence in sentences:
            clauses = [body[start:end] for start, end in sentence]
            bounds = [[0, *(m.start() for m in _OPERATION_CUT.finditer(c)), len(c)]
                      for c in clauses]
            parts = [[c[a:b] for a, b in pairwise(edges)]
                     for c, edges in zip(clauses, bounds, strict=True)]
            denied = _denials(parts)
            for (start, _), clause, edges, pieces, unasked in zip(sentence, clauses, bounds,
                                                                  parts, denied, strict=True):
                relative = _relative_to_other(clause, previous, own, others)
                for i, part in enumerate(pieces):
                    if unasked[i]:
                        continue
                    prior = pieces[i - 1] if i else ""
                    for where, op, m in _hits(part):
                        if op in found or (op != "haul" and (
                                relative or _about_other(part, m, op, others)
                                or _said_of_other(part, where, prior, own, others))):
                            continue
                        found[op] = (offset + start + edges[i] + where,
                                     _clause_quote(clause, edges, edges[i]))
                previous = clause
        offset += len(body) + 1
    return [(op, quote) for op, (_, quote) in sorted(found.items(), key=lambda kv: kv[1][0])]


def _entries(text: str, structure: Structure, structures: Sequence[Structure], *,
             alone: bool) -> list[str]:
    """The lines of ``text`` that are ``structure``'s entries by :func:`entry_owners`: those it
    gives ``structure`` alone, or, unless ``alone``, with others too ("Pit and berm: ...");
    never a constraint whose head is a sentence (:data:`_SENTENCE_HEAD`)."""
    out: list[str] = []
    for line in brief_lines(text):
        end = entry_head(line)
        if end is None or _SENTENCE_HEAD.search(line[:end]):
            continue
        owners = entry_owners(line, structures)
        if (len(owners) == 1 if alone else bool(owners)) and any(s is structure for s in owners):
            out.append(line)
    return out


def _hits(part: str) -> list[tuple[int, str, re.Match[str]]]:
    """``(where, operation, match)`` for every operation word of ``part``
    (:data:`_OPERATION_WORDS`), in the order it states them."""
    return sorted(((m.start(), op, m) for op, pattern in _OPERATION_WORDS
                   for m in pattern.finditer(part)), key=lambda hit: hit[0])


def _denials(clauses: Sequence[Sequence[str]]) -> list[list[bool]]:
    """Which parts of a sentence (its comma clauses, each cut at :data:`_OPERATION_CUT`) a
    negation or a deferral leaves unasked: a part with one in it ("Sintering is deferred to a
    later sortie", "(not sintered)", "compaction optional"); the items of a list one heads, up
    to the one with its "or" ("No sintering, compaction or inspection this sortie", "not to be
    excavated, compacted or sintered this sortie"); an item coordinated by "and" with a part
    one heads ("must not be excavated and compacted before the lander survey", "the existing
    track, already graded and compacted"); and an operation named as a noun before an item
    whose deferral trails it ("Sintering and compaction are deferred"). A negation that heads
    nothing but what it names reaches no further ("excavated without blasting and compacted"
    keeps its compact)."""
    denied = [[_NOT_ASKED.search(p) is not None for p in c] for c in clauses]
    heads = [[_heads(p) for p in c] for c in clauses]
    listing = False
    for i, c in enumerate(clauses):
        if listing and _bare_lead(c[0]):
            denied[i] = [True] * len(c)
            heads[i] = [True] * len(c)
            listing = not any(_LIST_END.search(p) for p in c)
            continue
        for j in range(1, len(c)):
            if heads[i][j - 1] and _coordinated(c[j]):
                denied[i][j] = heads[i][j] = True
        listing = (heads[i][-1] and not _LIST_END.search(c[-1])
                   and any(_LIST_END.search(p) for later in clauses[i + 1:] for p in later))
    trails = [[no and _trails(p) for no, p in zip(nos, c, strict=True)]
              for nos, c in zip(denied, clauses, strict=True)]
    places = [(i, j) for i, c in enumerate(clauses) for j in range(len(c))]
    for (i, j), (k, n) in reversed(list(pairwise(places))):
        if (not denied[i][j] and trails[k][n] and _nominal(clauses[i][j])
                and _bare_lead(clauses[k][n])):
            denied[i][j] = trails[i][j] = True
    return denied


def _heads(part: str) -> bool:
    """Whether a negation in ``part`` heads what it denies, a participle or an operation word
    after it ("must not be excavated", "already graded", "No sintering"), and so reaches the
    items coordinated with it; "without blasting" denies only what it names."""
    m = _NOT_ASKED.search(part)
    return m is not None and (_PARTICIPLE.search(part, m.end()) is not None
                              or any(where >= m.end() for where, _, _ in _hits(part)))


def _trails(part: str) -> bool:
    """Whether ``part`` names an operation and then defers or denies it ("and compaction are
    deferred to a later sortie", "compaction optional")."""
    hits = _hits(part)
    return bool(hits) and _NOT_ASKED.search(part, hits[0][0]) is not None


def _bare_lead(part: str) -> bool:
    """Whether ``part`` opens on an operation word with nothing before it but a conjunction,
    link words and adverbs ("and compacted", " compaction or inspection this sortie", "and
    lightly compacted"): an item of the clause before it, with no subject of its own."""
    hits = _hits(part)
    return bool(hits) and all(w in _LINKS or w in ("and", "or", "nor") or w.endswith("ly")
                              for w in WORDS.findall(part[:hits[0][0]].lower()))


def _coordinated(part: str) -> bool:
    """Whether ``part`` is an item joined to the part before it by "and" (:func:`_bare_lead`):
    "subgrade compacted | and inspected", "must not be excavated | and compacted"."""
    return _bare_lead(part) and WORDS.findall(part.lower())[:1] == ["and"]


def _nominal(part: str) -> bool:
    """Whether ``part`` names operations as nouns and nothing else ("Sintering", " compaction
    "): a subject that shares the predicate of the item after it."""
    hits = _hits(part)
    named = {m.group(0).lower() for _, _, m in hits}
    return bool(hits) and all(w.endswith(("ing", "ion", "ions")) for w in named) and all(
        w in _LINKS or w in ("and", "or", "nor") or w in named
        for w in WORDS.findall(part.lower()))


def _singular(word: str) -> str:
    """A plural name word read as its singular, as the size reader of ``rates`` reads one."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _name_words(text: str, structure: Structure, structures: Sequence[Structure]) -> set[str]:
    """The words a brief names ``structure`` by: those of its type and of the name before the
    colon of each entry :func:`entry_owners` gives it (singular, no figures; a head that is a
    sentence names nothing, :func:`_entries`)."""
    names = [structure.type.lower()]
    names += [line[:entry_head(line) or 0].lower()
              for line in _entries(text, structure, structures, alone=False)]
    return {_singular(w) for name in names for w in WORDS.findall(name)
            if not w.isdigit() and w not in _LINKS and w != "and"}


def _about_other(part: str, m: re.Match[str], op: str, others: Collection[str]) -> bool:
    """Whether the operation word ``m`` of ``part`` describes another structure, named next to
    it through :data:`_LINKS` only ("excavated from the pit", "the pad is compacted"), or, for
    a dig, the material it names ("excavated material", "spoil excavated nearby")."""
    after = WORDS.findall(part[m.end():].lower())
    before = WORDS.findall(part[:m.start()].lower())[::-1]
    nxt = next((w for w in after if w not in _LINKS), None)
    prev = next((w for w in before if w not in _LINKS), None)
    if op == "excavate" and any(w is not None and _singular(w) in _MATERIAL for w in (nxt, prev)):
        return True
    return any(w is not None and _singular(w) in others for w in (nxt, prev))


def _said_of_other(part: str, where: int, prior: str, own: Collection[str],
                   others: Collection[str]) -> bool:
    """Whether the operation word at ``where`` in ``part`` is said of another structure: one
    named before it in its part ("placing starts once the pad subgrade is compacted"), or, for
    an item coordinated with the part before (:func:`_coordinated`), one named there ("the
    access road beside it is graded and compacted", "until the pad is inspected and
    compacted"), each read by :func:`_names_other`."""
    return (_names_other(part[:where], own, others)
            or (_coordinated(part) and _names_other(prior, own, others)))


def _names_other(text: str, own: Collection[str], others: Collection[str]) -> bool:
    """Whether ``text``, the words before an operation word, name another structure outside a
    "from ..." phrase, which names the material ("built from the pit spoil and compacted" is
    the berm's compaction), or, as a clause of time, a structure word none of this
    structure's names (:data:`_STRUCTURE_NOUNS`: "after the bowl is excavated" in a berm's
    entry)."""
    words = WORDS.findall(text.lower())
    subject = [_singular(w) for w in (words[:words.index("from")] if "from" in words else words)]
    if any(w in others for w in subject):
        return True
    return bool(words) and words[0] in _SUBORDINATE and any(
        w in _STRUCTURE_NOUNS and w not in own for w in subject)


def _relative_to_other(clause: str, previous: str, own: Collection[str],
                       others: Collection[str]) -> bool:
    """Whether ``clause`` is a relative clause ("..., which is compacted first") about another
    structure: the noun that ends the clause before it is one of its names, or a structure word
    none of this structure's ("from the depot to the pad, which is compacted first" in the
    road's entry)."""
    if not _RELATIVE.match(clause):
        return False
    words = [_singular(w) for w in WORDS.findall(previous.lower()) if w not in _LINKS]
    return bool(words) and (words[-1] in others
                            or (words[-1] in _STRUCTURE_NOUNS and words[-1] not in own))


def _clause_quote(clause: str, parts: Sequence[int], start: int) -> str:
    """The clause as a note quotes it: whole, or, past :data:`_QUOTE_MAX` characters, the part
    at ``start`` (one of ``parts``, the clause's cuts) without its leading conjunction and as
    many of the parts after it as fit ("dug after the pit walls are stable and backfilled with
    compacted spoil"), cut with an ellipsis only when the part alone is too long."""
    whole = " ".join(clause.split()).strip(" *_")
    if len(whole) <= _QUOTE_MAX:
        return whole
    lead = _OPERATION_CUT.match(clause, start)
    if lead is not None:
        start = lead.end()
    later = [p for p in parts if p > start]
    end = later[0]
    for cut in later[1:]:
        if len(" ".join(clause[start:cut].split())) > _QUOTE_MAX:
            break
        end = cut
    quote = " ".join(clause[start:end].split()).strip(" *_")
    return quote if len(quote) <= _QUOTE_MAX else quote[:_QUOTE_MAX - 3].rstrip() + "..."
