"""Prompt templates for the four LLM roles. Kept data-only so they are easy to tune.

Every prompt ends with the JSON schema the stage expects and the instruction to answer with a
single JSON object and nothing else. Nemotron-specific tuning (reasoning on/off tokens,
sampling) lives in ``tokenfactory.py``, not here.

Physical grounding (phase 3a): the task-graph and repair prompts carry the rate table of
``moonwright.pipeline.rates`` and tell the model that durations and energies are COMPUTED from
the quantity and unit it states per task, so the model plans scope and splitting against the
batteries instead of inventing numbers that fit them.

Coverage contract (phase 3c): the structure prompt asks for ``Structure.operations`` (the
phases the brief explicitly asks for per structure, closed vocabulary, nothing invented) and
for dimensions copied rather than guessed; the task-graph and repair prompts share
:data:`COVERAGE_RULES` (every listed operation needs a task tagged with its structure, a haul
belongs to the receiving structure and its zone, inspections when the brief asks for them,
spoil reuse, the split rule) so the validator's ``scope:<structure>`` check and the model
read from the same page.

Volume contract (phase 3e): the first live evaluation showed the models satisfying the solver
by shrinking the work (a 20 m pad dug as a 12 m one, a 360 m3 borrow strip planned as 600 m2
of stripping, a berm hauled twice) and copying the structure prompt's worked example into
briefs that have no trench. So the structure prompt marks its dimension examples as other
projects' numbers (the trench figures are ones no brief uses), records the smallest size a
brief allows as ``min_<dimension>_m`` beside the full size, the compaction ``lifts`` and a
pit's ``wall_slope`` when the brief states them (the volume check and the eval's area check
read both), and gives no stockpile a size the brief does not state; :data:`COVERAGE_RULES` states what the validator's ``volume:<structure>``
check measures (an excavator m2 task is a surface layer of at most 0.2 m, anything deeper is
m3 for its full volume, every volume is hauled once, the planned m3 must match the dimensions,
work is never shrunk to fit the fleet); the repair prompt says which edit each conflict kind
needs (an energy verdict is never answered with edges alone) and :func:`repair_messages`
renders the step's advisories under the verdict so a 200 % double haul is fixed too. No rule
tells a repair to trim: an energy verdict the fleet cannot carry keeps the full work (seen in
review: "trim or move it" verified kind-D briefs after a silent 15-25 % cut). Every
prompt stays under the 5,500-character cap the tests hold it to (Ultra reads it with
thinking on), so each addition here is paid for by a cut elsewhere.

Supply order and lifts (phase 3g): CP-SAT schedules only the edges it is given, and in the
second live evaluation a horizon core made a repair drop a berm haul's edges to its digs, so
280 m3 of fill was hauled from hour 7 while the first dig ended at hour 13. The task-graph
prompt says every haul waits (`after`) for the dig that produces its material, and
:data:`REPAIR_SYSTEM` says what the validator's ``supply:<haul>`` verdict needs (the missing
edges; for a deadline, haul parts that each wait for the dig part feeding them and separate
sources dug in parallel; never a deleted dig-to-haul edge). The structure prompt defines
``lifts`` as the number of compaction layers the brief states, never segments along the length
nor a thickness (Lightning recorded "in four 10 m segments" as 4 lifts and "a 20 cm compacted
lift" as 0.2). Paid for by trimming the domain preamble (the teleoperation aside), glosses
that the verdict's own hint repeats and an example list the snake_case examples already show.
The supply balance note (``supply:<structure>``: a body's hauls move more m3 than the digs
before them remove) reaches a repair under :data:`ADVISORIES_HEADER`, which is outside the
capped system prompt and says how to read it: a berm "built from pad spoil" whose prism holds
more than the pad yields is the brief's own arithmetic, so the body keeps its m3 (what
:data:`COVERAGE_RULES` asks) and no dig is invented; the note is answered only by waiting for
more of the digs that feed it.
"""

from __future__ import annotations

import json

from moonwright.pipeline.rates import RATE_TABLE_TEXT
from moonwright.schemas import Failure, Requirements, Schedule, TaskGraph

DOMAIN = """You are Moonwright, a construction-planning assistant for autonomous multi-robot
crews on the lunar surface: every plan must run without human correction.

Robot types you may use: excavator (digs, grades), hauler (moves regolith), compactor (compacts
pads and berms), printer (sinters regolith surfaces), surveyor (surveys, inspects).
Battery budgets are per robot for the whole plan; there is no recharging."""

RATE_TABLE_BLOCK = """
RATE TABLE (deterministic; durations and energies are computed from it, never from your
numbers):
""" + RATE_TABLE_TEXT

STRUCTURE_SYSTEM = DOMAIN + """

Task: convert a natural-language construction brief into structured requirements.
Rules:
- Robots: copy the fleet exactly as the brief states it, every robot with its id, type and
  battery (Wh); never add, drop or rename a robot. Invent ids (EX1, HL1, ...) only when the
  brief gives counts without ids. Default battery when unspecified: excavator 5000,
  hauler 3000, compactor 3500, printer 4000, surveyor 1500 Wh.
- Structures: one entry per thing to build, with the id of its zone and EVERY dimension the
  brief gives, as numeric fields in metres (diameter_m, length_m, width_m, height_m, depth_m,
  thickness_m; trench_width_m and trench_depth_m only when the brief states a key trench;
  convert cm to m; wall_slope ("1:1") when stated). Examples from OTHER projects: "20 m
  diameter, a bowl 0.4 m deep, then a 30 cm sintered surface" -> "diameter_m": 20,
  "depth_m": 0.4, "thickness_m": 0.3; "a 60 m arc, 2.5 m high and 4 m wide at the base, on
  a key trench 0.7 m wide and 0.45 m deep" -> "length_m": 60, "height_m": 2.5, "width_m": 4,
  "trench_width_m": 0.7, "trench_depth_m": 0.45; the numbers above are illustrations, copy
  only the brief's. The planner sizes every
  task from these fields and never sees the brief, so copy every number the brief states; a
  number the brief does not state is never invented, and a structure the brief gives no
  numbers for gets no numeric field at all (a stockpile described only by its position has
  none: a stockpile or spoil pile gets a size only when the brief states one). Spell the type
  with the brief's own noun: a "Spoil berm" is spoil_berm (never stockpile), a "Borrow
  strip" is borrow_strip, a stockpile is "stockpile".
- Smaller sizes: when the brief gives a range or allows a reduction ("16-24 m", "may be
  reduced to 11 m", "at least 7 m long"), the plain key holds the full size and
  min_<dimension>_m beside it the smallest value allowed (min_diameter_m, min_depth_m,
  min_length_m, min_width_m, min_height_m; "diameter_m": 24, "min_diameter_m": 16; "at
  least 7 m" is 7 in both); never invent a minimum.
- lifts: the number of compaction layers, only when the brief states a count ("in three
  lifts" -> 3); segments along the length are not lifts, and a thickness ("a 20 cm lift") is
  not a count.
- operations: per structure, a JSON array of the work the brief explicitly asks for on it,
  using only the closed list survey, excavate, grade, haul, compact, sinter (inspection and
  staking are survey; stripping and levelling are grade). Example: a berm "built from pad
  spoil, compacted, with a sintered crest" -> "operations": ["haul", "compact", "sinter"]; a
  key trench is excavate; "inspected" or "signed off" is survey.
  Never add an operation the brief does not ask for (the planner must schedule each one);
  "operations": [] when the brief names none.
- Site geometry: the site is a grid of cells, 40 x 40 unless the brief implies otherwise.
  Give every structure its own zone (ids Z1, Z2, ...) and add one extra zone for the
  stockpile or haul lane whenever hauling is implied. Zone centres must lie inside the
  grid (0 <= x < 40 and 0 <= y < 40 by default), radius 3 to 6 cells, and centres at least
  2 x radius apart so zones do not overlap. Put the depot on an edge of the grid, for
  example [20, 2], away from every zone.
- horizon_h: the deadline in hours (days x 24). If none is given use 336.
- constraints: copy any explicit operational constraints as short sentences.
Answer with ONE JSON object matching this schema, no prose:"""

COVERAGE_RULES = """
- Every structure of a known type needs its phases: landing pad = survey, excavate or grade,
  compact and sinter (sinter only when a printer exists); blast berm / protective berm =
  excavate or haul, then compact; access road = grade and compact; habitat foundation =
  excavate and compact; any other structure needs at least one task. On top of that, every
  operation listed in a structure's `operations` needs a task tagged with that structure
  (the validator's scope:<structure> rule).
- A haul task's zone is the zone where the material is PLACED (the berm, the stockpile, the
  fill) and its structure is the receiving one. When structure B is built from structure A's
  spoil, B needs a haul task, and its quantity follows from B's volume, not from A's.
- When the brief asks for inspection or sign-off, add one surveyor inspection task per
  structure as the last task of that structure.
- Split a structure's excavation, hauling or compaction over at least as many tasks as there
  are robots of that type whenever its total is more than 60 m3 or more than 300 m2, so no
  task exceeds a battery. The parts SUM to the structure's total, never repeat it: 300 m3 of
  berm fill on two haulers is 2 x 150 m3, each part's basis stating its share ("300 m3 / 2 =
  150 m3"). Haul each volume once (fill = the placed body's m3, spoil = the dug m3), never a
  berm twice.
- Volumes: an excavator m2 task is a surface layer at most 0.2 m thick (strip, grade; 15 cm
  of fines over 10 x 10 m: strip 100 m2); a cut deeper than 0.2 m, a bowl, pit, trench or
  borrow strip is excavation in m3 for its FULL volume from the dimensions. Planned m3 must
  match the dimensions (under 70 % is rejected, over 150 % flagged); when the fleet cannot
  carry the full volume, plan the full work anyway and say so in notes (the solver proves
  it and proposes the fix), never shrink it; a min_<dimension>_m is the smaller size the
  brief allows."""

TASKGRAPH_SYSTEM = DOMAIN + """

Task: decompose the requirements into a task graph for the robots.
Rules:
- 6 to 24 tasks (more only when the split rule below needs them). Each task has: id (T1, T2,
  ...), name (start it with the phase verb: survey, strip/grade, excavate, haul, compact,
  sinter/print, inspect), robot_type (a type in requirements.robots), quantity + unit + basis
  (the scope, see below), duration_h and energy_wh (your estimate; recomputed), zone (an id
  from requirements.site.zones; tasks on the same physical area share one), after (ids of
  the tasks that must finish first), structure (the structure it serves; set it on every
  task).
- quantity and unit: m3 for excavation and hauling (volume moved; a trench is length x width
  x depth in m3, never m), m2 for strip, grade, compact, sinter and survey (area treated); m
  and ea (a cable run, a marker) only for surveyor tasks; null for a pure time task (an
  inspection pass). Every excavator, hauler, compactor and printer task that moves,
  shapes or sinters regolith MUST state a positive quantity in a unit the rate table prices
  for its type (m3 or m2) or the validator rejects it (quantity:<task>). basis says how the
  quantity follows from the dimensions ("pi*(10 m)^2*0.4 m = 126 m3", "60 m x 4 m = 240 m2").
- duration_h and energy_wh are COMPUTED from quantity and unit by the rate table below; a
  task without a quantity keeps your duration_h and is charged at least the base power. So
  plan SCOPE and SPLITTING against the stated batteries: one task must fit one robot's
  battery, and the sum of a robot's tasks must fit it too. Check with the table before
  answering.""" + COVERAGE_RULES + """
- Order: survey -> excavate -> haul -> grade/compact -> print/sinter -> inspect. Every haul
  waits (`after`) for the dig that produces its material. Tasks run one at a time per robot
  and one robot at a time per zone.
- The whole plan must fit inside horizon_h: keep the longest precedence chain well below it.
""" + RATE_TABLE_BLOCK + """
Answer with ONE JSON object matching this schema, no prose:"""

REPAIR_SYSTEM = DOMAIN + """

Task: the validator or solver rejected the previous task graph; fix it so the listed
conflicts disappear with the scope complete.
Rules:
- duration_h and energy_wh are COMPUTED from each task's quantity and unit by the rate table
  below: editing them without changing the quantity changes nothing, nor does dropping a
  quantity. The hint's figures are measured: keep them.
- What each conflict id needs:
  energy:<robot> / energy:<type> -> move or split quantities across robots of the type;
  `after` edges alone change no energy.
  energy_task:<task> (larger than any battery of its type) -> split it over more tasks of
  the same robot type, the shares summing to the original quantity. If the type cannot
  carry the work at all, keep it whole: the solver proves the shortfall and proposes a fix.
  horizon / horizon_path:<chain> / robot:<robot> -> parallelise: split across robots,
  shorter chains; never drop work.
  zone:<zone> -> spread tasks over zones or sequence them. precedence:<a>-><b> / cycle:...
  / reference:... -> fix the edge. capability:<task> -> a type the fleet has.
  scope:<structure> -> a phase, a listed operation or a whole structure went missing: add the
  missing task of the right robot type (the hint names it), never delete required work.
  quantity:<task> -> state it in a priced unit.
  volume:<structure> (planned m3 below the dimensions) -> add the missing m3 as excavator /
  hauler tasks (the hint says how much), never shrink the work.
  supply:<haul> (too little dug before it) -> add `after` edges from the digs that supply it; to
  hold a deadline, split hauls so each part waits only for the dig part feeding it, and dig
  separate sources on separate excavators in parallel; never delete a dig-to-haul edge to
  shorten a chain.
  missing:<task> / changed:<task> / locked:<task> -> keep those tasks as they were.
- Use a smaller quantity only where the brief allows it (a min_<dimension>_m) and state every
  scope change in notes (smaller, dropped, merged or re-typed work) and why the brief allows
  it; keep ids stable.""" + COVERAGE_RULES + """
""" + RATE_TABLE_BLOCK + """
Answer with ONE JSON object (the full corrected task graph) matching this schema, no prose:"""

REPLAN_SYSTEM = DOMAIN + """

Task: an event (a robot failure, a drained battery) hit the crew in the middle of execution.
REMAINING TASK GRAPH below is the work still outstanding: finished tasks are gone, `after`
edges to them have been removed, and a task that is underway carries its *remaining* hours and
Wh (the solver pins it to the robot already working on it). Return that remaining task graph
so the solver can re-schedule it on the surviving robots.
Rules:
- Return every task of REMAINING TASK GRAPH with its id, robot_type, quantity, unit,
  duration_h, energy_wh, zone and after unchanged: they describe work that is physically still
  outstanding. Never drop, shorten, re-zone or free a task from a predecessor, never re-add a
  finished task, and never renumber.
- Add a task only when the event creates extra work (for example re-surveying a zone the failed
  robot left half done): give it a new id, a robot_type that exists in requirements.robots,
  the `after` edges it needs, and a quantity and unit when it is physical work (its duration
  and energy are then computed from the rate table).
- A robot type that no longer exists in requirements.robots cannot be used; if remaining work
  needs it, keep the task as it is and let the solver report the conflict.
Answer with ONE JSON object (the full remaining task graph) matching this schema, no prose:"""

EXPLAIN_SYSTEM = DOMAIN + """

Task: write the plan summary a mission planner reads before approving execution.
Write 120-200 words of plain English: what gets built, how the robots split the work, the
makespan versus the deadline, the tightest constraint (battery, zone or precedence), and what
the solver verified. Task quantities (m3, m2) were proposed by the planning model; durations
and energies were computed from them by a fixed rate table, so quote them as computed
figures, not as the model's estimates. No bullet lists, no headings.
Answer with ONE JSON object {"text": "..."} and nothing else: no markdown fences, no
commentary, no reasoning text."""


JSON_ONLY = ("Return only that JSON object itself: no markdown fences, no commentary, no "
             "reasoning text before or after it.")

ADVISORIES_HEADER = (
    "ADVISORIES (warnings on PREVIOUS TASK GRAPH, not conflicts; resolve them too when the "
    "answer can: a volume percentage moves only when quantities change, so a 200 % haul is "
    "cut to the body's m3 and a shortfall gets the missing m3; an estimate gap is informational, "
    "the computed figures stand; a supply note (a body's hauls move more m3 than the digs "
    "before them remove, often the brief's own arithmetic) keeps the body's m3: never invent "
    "a dig or cut the body for it, only wait for more of the digs that feed it):"
)


def _schema_block(model: type) -> str:
    return ("\n```json\n" + json.dumps(model.model_json_schema(), indent=1) + "\n```\n"
            + JSON_ONLY)


def structure_messages(brief: str) -> tuple[str, str]:
    system = STRUCTURE_SYSTEM + _schema_block(Requirements)
    user = "BRIEF:\n" + brief.strip()
    return system, user


def taskgraph_messages(req: Requirements, context: str | None = None,
                       prior: TaskGraph | None = None) -> tuple[str, str]:
    """First task graph. ``prior`` (the plan so far, e.g. the remaining work of a replan) is
    always included in full so the model never has to guess durations, zones or energies."""
    system = TASKGRAPH_SYSTEM + _schema_block(TaskGraph)
    user = "REQUIREMENTS:\n" + req.model_dump_json(indent=1, exclude={"source_brief"})
    if prior is not None:
        user += ("\n\nPRIOR TASK GRAPH (the plan so far; keep its ids, zones, quantities, "
                 "durations and energies unless the context says otherwise):\n"
                 + prior.model_dump_json(indent=1))
    if context:
        user += "\n\nCONTEXT:\n" + context
    return system, user


def replan_messages(req: Requirements, prior: TaskGraph, context: str | None = None) -> tuple[str, str]:
    """Replan prompt: the remaining task graph (``prior``) is the thing to return, re-scheduled;
    ``context`` says what happened and where the mission stands."""
    system = REPLAN_SYSTEM + _schema_block(TaskGraph)
    user = (
        "REQUIREMENTS:\n" + req.model_dump_json(indent=1, exclude={"source_brief"})
        + "\n\nREMAINING TASK GRAPH:\n" + prior.model_dump_json(indent=1)
    )
    if context:
        user += "\n\nCONTEXT:\n" + context
    return system, user


def repair_messages(
    req: Requirements, prior: TaskGraph, failure: Failure, context: str | None = None,
    baseline: TaskGraph | None = None, advisories: list[str] | None = None,
) -> tuple[str, str]:
    """Repair prompt. ``baseline`` (a replan's remaining task graph) is included so the model
    can restore a task it redefined (``changed:<task>``) from the definition that binds.
    ``advisories`` (the warnings the step that produced ``prior`` carried: a ``volume:<id>``
    over 150 %, an ``estimate:<task>`` gap) are rendered as a bullet list under the FAILURE
    block, before CONTEXT, with the instruction to resolve them when the answer can; None or
    an empty list adds nothing, so FAILURE stays the last block of a context-free prompt."""
    system = REPAIR_SYSTEM + _schema_block(TaskGraph)
    user = "REQUIREMENTS:\n" + req.model_dump_json(indent=1, exclude={"source_brief"})
    if baseline is not None:
        user += ("\n\nREMAINING TASK GRAPH (the work still outstanding; every task must keep "
                 "this id, robot_type, quantity, unit, zone, after, and at least this "
                 "duration_h and energy_wh):\n" + baseline.model_dump_json(indent=1))
    user += (
        "\n\nPREVIOUS TASK GRAPH:\n" + prior.model_dump_json(indent=1)
        + "\n\nFAILURE:\n" + failure.model_dump_json(indent=1)
    )
    if advisories:
        user += "\n\n" + ADVISORIES_HEADER + "\n" + "\n".join(f"- {a}" for a in advisories)
    if context:
        user += "\n\nCONTEXT:\n" + context
    return system, user


def explain_messages(req: Requirements, graph: TaskGraph, schedule: Schedule) -> tuple[str, str]:
    system = EXPLAIN_SYSTEM
    user = (
        "REQUIREMENTS:\n" + req.model_dump_json(indent=1, exclude={"source_brief"})
        + "\n\nTASKS:\n" + graph.model_dump_json(indent=1)
        + "\n\nSCHEDULE:\n" + schedule.model_dump_json(indent=1)
    )
    return system, user
