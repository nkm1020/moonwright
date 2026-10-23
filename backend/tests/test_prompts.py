"""Tests for the phase-3e prompt contract (``moonwright.llm.prompts``): the structure prompt's
example hygiene and ``min_<dimension>_m`` rule, the volume rules the task-graph and repair
prompts share with the validator, the repair prompt's per-conflict edits, the advisories
section of :func:`repair_messages`, and a length snapshot so a later edit sees its growth.
Phase 3g adds the supply-order rule of the task-graph prompt, the repair prompt's
``supply:<haul>`` edit and the structure prompt's definition of ``lifts``."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from moonwright.llm.prompts import (
    ADVISORIES_HEADER,
    COVERAGE_RULES,
    JSON_ONLY,
    REPAIR_SYSTEM,
    STRUCTURE_SYSTEM,
    TASKGRAPH_SYSTEM,
    repair_messages,
    structure_messages,
    taskgraph_messages,
)
from moonwright.pipeline.rates import RATE_TABLE_TEXT, VOLUME_HIGH, VOLUME_LOW
from moonwright.schemas import Failure, Requirements, Robot, Site, Task, TaskGraph, Zone

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
BRIEFS = sorted((EXAMPLES / "briefs").glob("*.md")) + sorted((EXAMPLES / "eval").glob("*.md"))

# phase-3d lengths (chars); every phase-3e prompt stays within about 10 % of them and under
# the 5,500-character cap test_coverage_ops holds the three planning prompts to
PHASE_3D = {"STRUCTURE_SYSTEM": 3601, "TASKGRAPH_SYSTEM": 5219, "REPAIR_SYSTEM": 4989}
# the snapshot: prompt text without the rate table (which ``rates.py`` owns), +-2 %; phase 3g
# paid for its rules (supply order, the supply edit, lifts) with cuts, so the totals barely move
SNAPSHOT = {"STRUCTURE_SYSTEM": 3949, "COVERAGE_RULES": 1937, "TASKGRAPH_SYSTEM": 4455,
            "REPAIR_SYSTEM": 4605}

REQ = Requirements(
    site=Site(zones=[Zone(id="Z1", center=(10, 10)), Zone(id="Z2", center=(30, 10))]),
    robots=[Robot(id="EX1", type="excavator", battery_wh=5000),
            Robot(id="HL1", type="hauler", battery_wh=3000)],
    horizon_h=200,
)


def task(id_: str, type_: str, quantity: float, unit: str, zone: str = "Z1") -> Task:
    return Task(id=id_, name=id_, robot_type=type_, duration_h=4, energy_wh=100.0, zone=zone,
                quantity=quantity, unit=unit, structure="S1")


PRIOR = TaskGraph(tasks=[task("T1", "excavator", 60, "m3"), task("T2", "hauler", 60, "m3", "Z2")],
                  notes="n")
BASELINE = TaskGraph(tasks=[task("T1", "excavator", 60, "m3")], notes="remaining")
FAILURE = Failure(kind="validation", conflicts=["volume:S1"],
                  hint="S1: its dimensions give 126 m3 but its excavation tasks total 60 m3 (48%)",
                  details=["volume:S1: 60 of 126 m3"])
ADVISORIES = [
    ("volume:S2: its dimensions give 300 m3 (60 m long, 2.5 m high, 4 m wide at the base, "
     "triangular section) but its haul tasks (T3 300 m3, T4 300 m3) total 600 m3 (200%)"),
    "estimate:T3: model said 10 h / 1000 Wh, rate model gives 27 h / 3000 Wh (+200%)",
]


def flat(text: str) -> str:
    return " ".join(text.split())


# ----------------------------------------------------------------------------- structure
def test_structure_prompt_marks_its_examples_as_other_projects_and_drops_the_leaked_trench():
    text = flat(STRUCTURE_SYSTEM)
    assert "Examples from OTHER projects" in text
    assert "the numbers above are illustrations, copy only the brief's" in text
    assert "trench_width_m and trench_depth_m only when the brief states a key trench" in text
    # the example trench is one no brief has: the figures Lightning copied into a trench-less
    # berm (1.2 m wide, 0.8 m deep) are gone from the prompt entirely
    for leaked in ('"trench_width_m": 1.2', '"trench_depth_m": 0.8', "1.2 m wide", "0.8 m deep"):
        assert leaked not in text, leaked
    assert "on a key trench 0.7 m wide and 0.45 m deep" in text
    assert '"trench_width_m": 0.7, "trench_depth_m": 0.45' in text
    # the phase-3c guards stay
    assert "copy every number the brief states" in text and "is never invented" in text
    assert STRUCTURE_SYSTEM.endswith("Answer with ONE JSON object matching this schema, no prose:")
    system, user = structure_messages(" Build a berm. ")
    assert system.startswith(STRUCTURE_SYSTEM) and system.endswith(JSON_ONLY)
    assert user == "BRIEF:\nBuild a berm."


def test_structure_prompt_example_figures_appear_in_no_brief():
    """A copied example must be recognisable as a copy: none of the trench or min-size
    example figures is a dimension of any shipped brief (examples/briefs, examples/eval)."""
    assert len(BRIEFS) >= 20
    briefs = {p.name: p.read_text() for p in BRIEFS}
    for figure in ("0.7", "0.45", "24", "16", "11", "7"):
        pattern = re.compile(rf"\b{re.escape(figure)} ?m\b")
        hits = [name for name, text in briefs.items() if pattern.search(text)]
        assert not hits, (figure, hits)


def test_structure_prompt_records_the_smallest_size_the_brief_allows():
    text = flat(STRUCTURE_SYSTEM)
    for key in ("min_<dimension>_m", "min_diameter_m", "min_depth_m", "min_length_m",
                "min_width_m", "min_height_m"):
        assert key in STRUCTURE_SYSTEM, key
    assert "when the brief gives a range or allows a reduction" in text
    assert '"16-24 m", "may be reduced to 11 m", "at least 7 m long"' in text
    assert "the plain key holds the full size and min_<dimension>_m beside it" in text
    assert '"diameter_m": 24, "min_diameter_m": 16' in text
    assert "never invent a minimum" in text
    # a pit's wall slope, which the volume check reads (a 1:1 sloped pit priced with vertical
    # walls rejected the right dig); the lifts have a rule of their own (below)
    assert 'wall_slope ("1:1") when stated' in text
    # stockpiles and spoil piles: explicit now, the phase-3c sentence kept for its tests
    assert "a stockpile described only by its position has none" in text
    assert "a stockpile or spoil pile gets a size only when the brief states one" in text


def test_structure_prompt_defines_lifts_as_a_stated_count_of_compaction_layers() -> None:
    """Lightning recorded crater_ramp's "in four 10 m segments" as 4 lifts and access_road's
    "a 20 cm compacted lift" as 0.2; the eval's area check now reads the brief's own count, and
    the prompt says what a lift count is."""
    text = flat(STRUCTURE_SYSTEM)
    assert ("lifts: the number of compaction layers, only when the brief states a count "
            '("in three lifts" -> 3); segments along the length are not lifts, and a thickness '
            '("a 20 cm lift") is not a count.') in text
    assert "lifts and wall_slope" not in text


# ----------------------------------------------------------------------------- volumes
def test_coverage_rules_state_the_volume_contract_for_both_planning_prompts():
    assert COVERAGE_RULES in TASKGRAPH_SYSTEM and COVERAGE_RULES in REPAIR_SYSTEM
    rules = flat(COVERAGE_RULES)
    for text in (
        "an excavator m2 task is a surface layer at most 0.2 m thick",
        "15 cm of fines over 10 x 10 m: strip 100 m2",
        ("a cut deeper than 0.2 m, a bowl, pit, trench or borrow strip is excavation in m3 "
         "for its FULL volume from the dimensions"),
        "Haul each volume once (fill = the placed body's m3, spoil = the dug m3), never a berm twice",
        "Planned m3 must match the dimensions",
        "plan the full work anyway and say so in notes (the solver proves it and proposes the fix)",
        "never shrink it",
        "a min_<dimension>_m is the smaller size the brief allows",
        # the split rule and its sum rule are still the ones test_coverage_ops pins
        "The parts SUM to the structure's total, never repeat it",
    ):
        assert text in rules, text
    # the thresholds quoted are the validator's (rates.VOLUME_LOW / VOLUME_HIGH)
    assert f"under {round(VOLUME_LOW * 100)} % is rejected" in rules
    assert f"over {round(VOLUME_HIGH * 100)} % flagged" in rules


def test_taskgraph_prompt_keeps_its_pricing_rule_and_shape():
    text = flat(TASKGRAPH_SYSTEM)
    assert "or the validator rejects it (quantity:<task>)" in text
    assert "Check with the table before answering" in text
    assert "Order: survey -> excavate -> haul -> grade/compact -> print/sinter -> inspect" in text
    assert "pi*(10 m)^2*0.4 m = 126 m3" in TASKGRAPH_SYSTEM and RATE_TABLE_TEXT in TASKGRAPH_SYSTEM
    # phase 3g: the supply order the validator's supply:<haul> check holds a whole plan to
    assert "Every haul waits (`after`) for the dig that produces its material." in text
    system, user = taskgraph_messages(REQ)
    assert system.startswith(TASKGRAPH_SYSTEM) and system.endswith(JSON_ONLY)
    assert user.startswith("REQUIREMENTS:\n")


# ----------------------------------------------------------------------------- repair
REPAIR_EDITS = [
    ("energy:<robot> / energy:<type>",
     "move or split quantities across robots of the type; `after` edges alone change no energy"),
    ("energy_task:<task>",
     "split it over more tasks of the same robot type, the shares summing to the original quantity"),
    # the validator's pooled sentence ("The fleet's 1 printer carries 4,000 Wh in all, so
    # splitting T1 alone cannot fit it") is the case where splitting is not the edit, and the
    # work stays whole: "trim or move it" verified kind-D briefs after a silent 15-25 % cut
    ("If the type cannot carry the work at all",
     "keep it whole: the solver proves the shortfall and proposes a fix"),
    ("horizon / horizon_path:<chain> / robot:<robot>",
     "parallelise: split across robots, shorter chains; never drop work"),
    ("zone:<zone>", "spread tasks over zones or sequence them"),
    ("precedence:<a>-><b> / cycle:... / reference:...", "fix the edge"),
    ("capability:<task>", "a type the fleet has"),
    ("scope:<structure>",
     ("a phase, a listed operation or a whole structure went missing: add the missing task of "
      "the right robot type (the hint names it), never delete required work")),
    ("quantity:<task>", "state it in a priced unit"),
    ("volume:<structure>",
     ("add the missing m3 as excavator / hauler tasks (the hint says how much), never shrink "
      "the work")),
    ("supply:<haul>", "add `after` edges from the digs that supply it"),
    ("missing:<task> / changed:<task> / locked:<task>", "keep those tasks as they were"),
]


def test_repair_prompt_says_what_each_conflict_kind_needs():
    text = flat(REPAIR_SYSTEM)
    assert "What each conflict id needs:" in text
    for cid, edit in REPAIR_EDITS:
        assert cid in text, cid
        at = text.index(cid)
        assert text.find(edit, at) != -1, (cid, edit)
        assert text.index(edit, at) - at < 140, (cid, "edit is not the sentence after its id")
    # every id a repair can be handed stays an unbroken token of the raw prompt
    for cid in ("energy:<robot>", "energy:<type>", "energy_task:<task>", "horizon_path:<chain>",
                "robot:<robot>", "zone:<zone>", "precedence:<a>-><b>", "capability:<task>",
                "scope:<structure>", "quantity:<task>", "volume:<structure>", "supply:<haul>",
                "missing:<task>", "changed:<task>", "locked:<task>"):
        assert cid in REPAIR_SYSTEM, cid
    # phase 3g: how a supply verdict keeps a deadline without losing the order (live, the
    # repair answering a horizon core deleted the berm hauls' edges to their digs)
    supply = text[text.index("supply:<haul>"):text.index("missing:<task>")]
    for part in ("to hold a deadline",
                 "split hauls so each part waits only for the dig part feeding it",
                 "dig separate sources on separate excavators in parallel",
                 "never delete a dig-to-haul edge to shorten a chain"):
        assert part in supply, part
    # the honest tone: measured figures are kept, a cut needs the brief's permission, and no
    # conflict kind is answered with a trim
    assert "The hint's figures are measured: keep them" in text
    assert "trim" not in text
    assert "Use a smaller quantity only where the brief allows it (a min_<dimension>_m)" in text
    assert "state every scope change in notes" in text
    assert "editing them without changing the quantity changes nothing" in text
    assert REPAIR_SYSTEM.endswith(
        "Answer with ONE JSON object (the full corrected task graph) matching this schema, no prose:")


def test_repair_messages_without_advisories_keeps_failure_as_the_last_block():
    """test_loop reads the FAILURE object as everything after ``FAILURE:\\n`` when there is no
    context; None and an empty list both leave that layout untouched."""
    system, user = repair_messages(REQ, PRIOR, FAILURE)
    assert system.startswith(REPAIR_SYSTEM) and system.endswith(JSON_ONLY)
    assert "ADVISORIES" not in user
    assert json.loads(user.split("FAILURE:\n", 1)[1]) == FAILURE.model_dump()
    assert repair_messages(REQ, PRIOR, FAILURE, None, None, None)[1] == user
    assert repair_messages(REQ, PRIOR, FAILURE, advisories=[])[1] == user
    # the positional call taskgraph.py makes today still works, and the context stays last
    _, with_ctx = repair_messages(REQ, PRIOR, FAILURE, "ctx", BASELINE)
    assert "ADVISORIES" not in with_ctx and with_ctx.endswith("CONTEXT:\nctx")
    assert "REMAINING TASK GRAPH (the work still outstanding" in with_ctx


def test_repair_messages_renders_advisories_under_the_verdict():
    system, user = repair_messages(REQ, PRIOR, FAILURE, "ctx", BASELINE, advisories=ADVISORIES)
    assert system == repair_messages(REQ, PRIOR, FAILURE)[0]   # the system prompt is unchanged
    header = "\n\n" + ADVISORIES_HEADER + "\n"
    assert user.count(ADVISORIES_HEADER) == 1
    assert (user.index("REMAINING TASK GRAPH") < user.index("PREVIOUS TASK GRAPH:")
            < user.index("FAILURE:\n") < user.index(header) < user.index("CONTEXT:\nctx"))
    # the FAILURE object is intact above the section
    failure_block = user.split("FAILURE:\n", 1)[1].split(header, 1)[0]
    assert json.loads(failure_block) == FAILURE.model_dump()
    # one bullet per advisory, verbatim and in order, straight under the header
    section = user.split(header, 1)[1].split("\n\nCONTEXT:", 1)[0]
    assert section == "\n".join(f"- {a}" for a in ADVISORIES)
    # what the header tells the model
    for text in ("warnings on PREVIOUS TASK GRAPH, not conflicts", "resolve them too when the answer can",
                 "a volume percentage moves only when quantities change", "200 % haul",
                 "a shortfall gets the missing m3", "the computed figures stand"):
        assert text in ADVISORIES_HEADER, text
    # without context the section closes the prompt
    _, tail = repair_messages(REQ, PRIOR, FAILURE, advisories=ADVISORIES[:1])
    assert tail.endswith(header + "- " + ADVISORIES[0])


def test_repair_messages_signature_is_the_loop_contract():
    params = inspect.signature(repair_messages).parameters
    assert list(params) == ["req", "prior", "failure", "context", "baseline", "advisories"]
    assert params["advisories"].default is None
    assert params["advisories"].annotation == "list[str] | None"


# ----------------------------------------------------------------------------- lengths
def test_prompt_lengths_snapshot():
    """Within about 10 % of the phase-3d lengths, under the 5,500-character cap, and within
    2 % of the snapshot without the rate table: an edit that grows a prompt updates SNAPSHOT
    on purpose, after checking the cap."""
    prompts = {"STRUCTURE_SYSTEM": STRUCTURE_SYSTEM, "COVERAGE_RULES": COVERAGE_RULES,
               "TASKGRAPH_SYSTEM": TASKGRAPH_SYSTEM, "REPAIR_SYSTEM": REPAIR_SYSTEM}
    for name, base in PHASE_3D.items():
        assert len(prompts[name]) <= base * 1.1 + 1, (name, len(prompts[name]), base)
        assert len(prompts[name]) < 5500, (name, len(prompts[name]))
    for name, expected in SNAPSHOT.items():
        text = prompts[name].replace(RATE_TABLE_TEXT, "")
        assert abs(len(text) - expected) <= expected * 0.02, (name, len(text), expected)
