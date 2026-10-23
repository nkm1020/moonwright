# Evaluation briefs: what each one tests, with the arithmetic

The 22 briefs under `backend/examples/eval/` are the test set of the live evaluation
(`moonwright eval --briefs examples/eval`, repair loop off and on). Every brief is
self-contained (site, structures with dimensions, fleet with Wh, deadline, the one-robot-per-zone
rule) and is designed for one of five kinds, so the eval can report feasible-plan rate, repairs
and scope drift *per kind* instead of over a blur of easy briefs:

| kind | what the brief does | what should happen |
| --- | --- | --- |
| **A** comfortable | every robot type has 25 % or more battery to spare and even a fully serial plan fits the deadline | first graph feasible; measures coverage, precedence and tokens, not repairs |
| **B** battery-split | the per-type Wh fits the fleet only when the work is spread across that type's robots | a one-task-per-phase graph is infeasible with an `energy:<robot>` core (or `energy_task:<task>` from the validator); the repair is to split |
| **C** horizon-bound | the deadline fits only with parallel zones; a serial graph exceeds it | a serial graph fails `horizon_path` / `horizon`; the repair is to parallelise |
| **D** impossible | energy short by 15-25 % or the horizon short by a third, with the scope fixed by the brief | a graph that shrinks the work below 70 % is a `volume:<structure>` conflict (phase 3e), and one that trims it 15-25 % verifies but is not *in scope* (95 % of every volume and compaction / sintering area, and every operation a structure's own entry states, phase 3l: run 3's Super #1 verified front_loaded_excavation at 60 h without the pad compaction its entry states); the full-scope graph exhausts the repairs and the advice path must name the shortfall (add a robot, extend the window, relax a rule) |
| **E** scope reduction allowed | as D, but the brief allows a smaller scope | the repair re-scopes inside the allowed range (the structure stage records it as `min_<dimension>_m`, the floor of the volume check) and the plan verifies with visible scope drift |

The margins below come from `backend/scripts/brief_budget.py`, which prices every structure's
phases with the planner's own rate table (`moonwright/pipeline/rates.py`: 7 m3/h and 16 Wh/m3
for excavation, 12 m3/h and 7 Wh/m3 hauling, 26 m2/h and 4 Wh/m2 compaction, 14 m2/h and
8.6 Wh/m2 sintering, 25 m2/h and 4.8 Wh/m2 stripping, 150 m2/h and 0.5 Wh/m2 surveying) and
then runs its canonical graphs through the real validator and CP-SAT. Read the numbers with
the script's assumptions in mind:

* **A bowl is `pi r^2 d`, a berm `length x 0.5 x height x base` (or `height^2` with 1:1 slopes
  when only the height is given), a road or apron `length x width`, a pit `area x depth`,
  compaction and sintering the surface area (times the lifts).** A haul belongs to the
  structure that receives the fill, is sized by that structure's volume and waits for its
  source's excavation wherever the registry lists the source, a source stripped in m2
  included (since phase 3g: the berm of `small_fleet_long_berm`, the mound of
  `shielding_mound` and the berms of `habitat_berm_tight_horizon` and `depleted_excavators`
  come before a source of theirs, and `one_surveyor_three_structures`' berm is built from the
  hardstand's strip; their hauls used to wait for nothing, which the validator's
  `supply:<haul>` check now rejects); spoil another structure receives is never hauled twice.
* **The canonical graphs are not the model's graphs.** `lazy` is one task per phase per
  structure; `split` applies the prompt's rule (work above 60 m3 / 300 m2 shared equally
  across the type's robots); `fine` shares every physical phase that way; `serial` chains the
  structures one after another, fill sources first; `shrunk` is `lazy` with every dig in m3
  planned as stripping its footprint in m2, the re-scope the live models made to fit a
  battery in the first evaluation. "Expected first-try outcome" is the solver's verdict on `lazy` and `split`,
  i.e. on a careless and on a rule-following planner; a live Nemotron graph can land anywhere
  in between, which is exactly what the eval measures.
* **Under-scope is a conflict since phase 3e.** The first live evaluation (22 briefs x the
  Ultra and Super arms x 3, 132 runs) verified 85 % and 91 % of the runs of its two arms but
  only 45 % and 38 % in scope: the models satisfied the solver by shrinking the work (a 20 m
  pad dug as a 12 m one, the 360 m3 borrow strip of `small_fleet_long_berm` planned as 600 m2
  of stripping, a 300 m3 berm hauled twice), and the volume advisory never reached the repair
  prompt. The validator now rejects a whole plan whose tasks cover less than 70 % of the
  smallest volume a structure's dimensions allow (`volume:<structure>`, with a hint that says
  how many m3 to add); an excess (hauled or dug 1.5 times over) stays an advisory up to 175 %
  (phase 3i, below), and the fill of a stockpile is judged neither way. Every `lazy` and
  `split` graph below covers its volumes and is in scope (the script prices the same shapes as
  `moonwright.pipeline.rates.dims_volumes`, and a test holds the two together, the brief's own
  text included); the `shrunk` graphs fail `volume:` wherever a dig is deeper than the 0.2 m an
  m2 task strips. So a D brief's re-scope
  ends in a `volume:` conflict before any energy verdict, the repair prompt tells a type that
  cannot carry its work to keep it whole (it used to say "trim or move it"), and the run ends on
  the energy verdict and the advice: the outcome D is designed for. A repair that trims the
  work only 15-25 % (the borrow dig of `small_fleet_long_berm` at 85 %, the two lifts of
  `solar_array_pad` at 78 %) passes the 70 % conflict and CP-SAT, but the eval no longer counts
  it in scope: that takes 95 % of every judged volume and of every compaction and sintering
  area (the floor's footprint x `lifts`).
* **Only what the brief states is judged.** The review of phase 3e found three guesses that had
  become conflicts: a key trench without a stated width took the berm's base (`tight_battery`
  rejected a correct 1.2 m trench as 30 % of an invented 4 m one), every pit had vertical walls
  (`propellant_pit_and_berm`'s "1:1 sloped wall" pit, dug to its real 58.6 m3, read 58 % of
  100.5 m3), and a berm's prism ignored where its fill comes from (`landing_pad`'s berm "built
  from pad spoil" holds 300 m3, the pad yields 125.7 m3, so hauling all of it read 42 %; the
  same on `large_fleet_pad_berm_road`, `propellant_pit_and_berm` and
  `one_surveyor_three_structures`). A trench is judged only with its width, sloped walls stated
  in the brief give a range (rim to floor reading), and a berm whose hauls wait for another
  structure's dig is judged against the smaller of its prism and that spoil. The stripping
  credit no longer stands in for a dig: a levelling pass after the dig earns none, the layer
  below 0.2 m needs digs, and a kind-E pad stripped at full size earns only its floor's top
  layer (`low_battery_pad` stripped over 20 m plus a 1 m3 test pit is now `volume:S1`).
  Phase 3g did the same for the eval's area check: the lift count is the one the structure's
  own entry states ("in three compacted lifts", "two full lifts", "in one lift", the smaller
  count of a range; a hard-wrapped entry is read whole), never `crater_ramp`'s "in four 10 m
  segments" nor `access_road`'s "a 20 cm compacted lift" (Lightning recorded them as 4 and
  0.2 lifts, and a ramp compacted once per segment read 25 %), and the structure stage writes
  that count into the requirements with an amber note ("structure S1: lifts 4 -> 1: the
  brief's entry for S1 states no lift count we can read"), so the planner compacts what the
  check judges (`small_fleet_long_berm`'s unrecorded two lifts are added). A placed body is
  judged on the base width the brief states, else on the least base it can have, as wide as it
  is high (`access_road`'s berm "20 m long, 1.5 m high" read 33-50 % of a 1:1 base the brief
  never gave and is 30 m2 now; the 4 m Lightning copied onto `propellant_pit_and_berm`'s berm
  is not taken as stated; a 10 m2 pass over `two_pads_one_printer`'s 40 m x 2 m berm stays a
  gap). Rescored, the run-2 plans gain 3 verified-in-scope runs for Ultra and 7 for Super
  (`scripts/rescore_eval.py`; no `min_volume_share` moved).
* **Every haul waits for its material (phase 3g).** CP-SAT schedules only the edges it is
  given; in run 2, e.g., `habitat_berm_tight_horizon`'s berm fill was hauled from hour 7 while
  the first dig ended at hour 13. On a whole plan, a haul in m3 is the validation conflict
  `supply:<haul>` when it waits (through any chain of `after` edges) for no source of its
  material, or, into a designed body, moves with the hauls into that body before it more than
  1.25 x (+1 m3) what its sources make available while a dig in m3 beside it could supply the
  rest (a 300 m3 berm haul after its own 15 m3 key-trench cut alone). A source is every dig in
  m3, and an m2 strip where its spoil can be what the haul moves: into the strip's own
  structure (`propellant_pit_and_berm`'s haul lane), into a pile (a stockpile, a zone no
  structure claims) or in a graph that digs nothing in m3 (`one_surveyor_three_structures`'
  berm from the hardstand's strip spoil); a strip makes at most 0.2 m of its area available.
  Rescored, 17 of 56 verified Ultra plans and 14 of 51 Super plans have such a haul. When
  every source already precedes a body's hauls and they still move more, the plan stands and
  a `supply:<structure>` advisory notes the brief's own arithmetic (`landing_pad`'s 300 m3
  berm prism "built from pad spoil" after 125.7 m3 of bowl). Every canonical graph (`lazy`,
  `split`, `fine`, `serial`, `shrunk`) passes the check; `road_and_pad`'s windrows are
  "grading spoil pushed" by the grader, which the registry plans as an excavator m2 pass
  instead of a haul.
* **A square is never a circle (phase 3i).** Lightning recorded the "12 m by 12 m" pit of
  `habitat_foundation` and `habitat_berm_tight_horizon`, `instrument_pad`'s "10 m by 10 m" pad
  and `four_mast_pads`' "each 8 m by 8 m" pads as `diameter_m` in runs 2 and 3, and a 12 m
  bowl is 113.1 m3 of the brief's 144 (78.5 %, above the 70 % conflict): plans dug the bowl
  and counted in scope. The structure stage now writes the rectangle the structure's own entry
  states (two length figures joined by "by", "x" or "×", never a size and a depth, a count or
  the parts of the work, "3 x 50 m segments"; never in an entry that calls the structure
  circular, round, a bowl or by its diameter or radius; and only when the recorded diameter is
  one of the figures and stands nowhere else in the entry) as `length_m` x `width_m`, with
  `min_length_m` x `min_width_m` only where the entry states a smaller rectangle for a recorded
  minimum, and an amber note; the volume and area checks read it from the brief whenever they
  judge a plan, saved ones included. Every other brief's recorded structures are untouched (the
  circular pads of `landing_pad`, `depleted_excavators`, `front_loaded_excavation` and the
  rest, `road_and_pad`'s pad beside a "30 m by 30 m level patch"). Rescored, the six habitat
  plans per arm read 78 % and leave the in-scope count, and so does one `four_mast_pads` plan
  per arm that compacted each 8 m square pad over its inscribed circle. `four_mast_pads`'
  recorded `depth_m: 1` is its anchor hole's depth, so its pads are judged as 64 m3 boxes (they
  were 50.3 m3 bowls): both readings are the structuring's error, not the brief's 20 cm of
  fines.
* **A double-counted dig or haul is a conflict (phase 3i).** The landing-pad demo's first graph
  hauled 2 x 120 m3 into its 120 m3 berm in 3 of 3 live tries: 240 m3 is the berm's 40 x 1.5 x
  4 m box, twice the triangular section the volume check measures (and the briefs state:
  `habitat_berm_tight_horizon`'s 70 x 2 x 4 m berm is "about 280 cubic metres of fill"), and
  two repairs answered the notes with a 114 m3 borrow pit the brief never mentions. 12 of the
  14 verified plans of runs 2 and 3 that hauled a berm at 200 % sized it the same way
  ("560 m3 / 2 = 280 m3"). The task-graph prompts now give a berm's fill (length x height x
  base width / 2), and dig or haul parts summing to 175 % (+1 m3) of the largest volume the
  brief allows are `volume:<structure>` on a whole plan, the detail quoting the section and
  the hint the cut ("its fill is length x height x base width / 2 (40 x 1.5 x 4 / 2 = 120 m3),
  not the box; cut the S2 hauls to 120 m3 in total (for example 2 x 60 m3) ... never add a dig
  the brief does not ask for"); 150-175 % stays the advisory. The largest volume is the
  dimensions', or a larger one the brief states on the structure's line (`isru_stockpile`'s
  "about 200 cubic metres" over the one 70 m3 strip Lightning recorded); nothing is judged on a
  berm whose base width the brief does not state (`propellant_pit_and_berm`'s, recorded 4 m
  wide from the structure prompt's example), a stockpile, or a haul staged in a pile. Every
  canonical graph passes; rescored, 3 of 56 verified Ultra and 3 of 51 Super plans of run 2 are
  double counts (`habitat_berm_tight_horizon`'s 560 m3 box for its 280 m3 berm, one
  `large_fleet_pad_berm_road` plan's 1,125 m3 for 562.5 m3).
* **A smaller size is read wherever the brief states it (phase 3j).** `low_battery_pad` allows
  "any diameter between 12 m and 20 m that fits" in a constraint, not in the pad's entry, and
  Lightning recorded no `min_diameter_m` in 6 of 6 structurings of runs 2 and 3: the plans that
  dug a 16.8 m bowl (111 m3, inside the range) were judged against 20 m, 70 %, and none of run
  3's was in scope, while the validator rejected 16 re-scopes the brief allows as `volume:S1` in
  8 of the 12 runs (the 12 m bowl read 36 % of the 20 m one). The structure stage now reads a
  range ("between A m and B m", "A to B m", "A-B m") or a smallest size ("may be reduced to A
  m", "down to A m", "at least A m", "no smaller than A m") bound to the nearest dimension
  word, ties it to the structure its subject names ("the apron is at least 5 m wide" sizes no
  pad), the nearest structure its sentence names, the entry it stands in, or (when its
  sentence names none) the only structure its range tops out at, and writes
  `min_<dimension>_m` only below the recorded size, with an amber note; it also drops a length
  the structure's own entry gives only as a distance ("30 m north of the pad", which run 3
  recorded as the stockpile's 30 m diameter and judged as 707 m2 of berm compaction). It reads
  nothing in the Site, Fleet and Deadline paragraphs, and a clause about a crest, a key trench,
  the ground, a compaction depth or a haul length sizes no structure that is not one: a judge's
  brief that says "loose regolith at least 40 cm deep" or "with a crest at least 2 m wide" keeps
  the full-size floor. The volume and area checks read both from the brief, so the registry's
  `min_diameter_m: 12` is what the structure stage writes for all 6 recorded structurings (not
  yet measured live) and what the eval judges. Rescored, the five verified `low_battery_pad`
  plans (one in run 2, four in run 3) are in scope; no other brief states either, and every
  other brief's recorded structures are unchanged, read alone or beside the structures they
  were recorded with.
* **The structure stage drops an operation the fleet cannot perform (phase 3j).** Lightning
  lists `sinter` where no printer exists (`shielding_mound`, `solar_array_pad`,
  `four_mast_pads`, `tight_battery`, the habitat berm) and a haul where no hauler does
  (`instrument_pad`); 9 of the 31 plans that started from such requirements hit `capability:`
  verdicts and 4 final graphs sinter or haul with a stand-in robot. The operation is now dropped
  with a note, so the planner never sees it; a dug structure's listed `haul` (a borrow strip, a
  body its dimensions dig) is met by the hauls that wait for its digs, tagged with the berm
  they fill, while a mound, an earth bridge or a berm still needs its fill hauled onto it; and
  a coverage hint never quotes an entry its name or its figures give another structure.
* **The structure stage restores an operation the entry states (phase 3l).** Lightning typed
  `front_loaded_excavation`'s pad `excavator` with operations [excavate, survey] in run 3 #1
  (`pad` with the same two in #2 and #3), dropping the compaction its entry states ("subgrade
  compacted and inspected"): nothing required the 28 h of pad compaction, the one-robot-per-zone
  deadline no longer bit, and Super #1 verified this kind-D brief at 60 h, in scope. The stage
  now reads each structure's own entry clause by clause and appends what it states and the
  structure omits (compacted, inspected, staked, checked, excavated, dug, hauled, "built from",
  sintered; never grade, never a negated or deferred clause such as "Sintering is deferred to a
  later sortie" or the list a negation heads, "No sintering, compaction or inspection this
  sortie", never a word said of another structure, "placing starts once the pad subgrade is
  compacted", never a constraint shaped like an entry, "- The pad is one work zone: ... the bowl
  cannot be excavated as parallel sectors", never an operation the fleet cannot staff), with an
  amber note; the eval judges a saved plan against the same reading, so a verified plan that
  never performs a restored operation is out of scope. Over the 132 saved structurings of runs
  2 and 3 it restores nine operations: the front_loaded pad's compaction (3),
  `small_fleet_long_berm`'s berm haul "built from a borrow strip" (2),
  `large_fleet_pad_berm_road`'s road compaction (typed `stockpile`), `solar_array_pad`'s
  anchor-trench compaction, `habitat_berm_tight_horizon`'s berm inspection and
  `habitat_foundation`'s trench excavation (typed `berm`). Rescored, four verified plans leave
  their scope: run 3 front_loaded super #1, run 3 habitat_foundation super #2 (its trench never
  dug) and run 2 large_fleet_pad_berm_road ultra and super #2 (its road never compacted). Run
  4's 66 restore nine more (the front_loaded pad's compaction in all three, the
  habitat_berm berm's inspection twice, `small_fleet_long_berm` #3's berm haul, compaction and
  inspection, listed as [grade] alone, and `cable_trench` #2's "compacted 10 cm bedding layer"),
  and two of its verified plans leave their scope: cable_trench #2 in both conditions, whose
  bedding is never compacted.
* **Bounds ignore transit.** CP-SAT adds the whole hours and the 0.5 Wh per grid cell of every
  leg a robot drives (since phase 3k exactly the legs of the order it picks; before it a
  transit allowance per task, which made several of these makespans 2-14 h longer), so a
  verified makespan of the canonical graphs sits a few hours above `LB`; the `lazy` / `split`
  makespans quoted here are phase 3k's.
* **The registry carries keys the structure prompt does not.** The script's structures use
  `trench_depth_m`, `trench_width_m`, `fines_depth_m`, `crest_width_m`, `lifts`, `fill_m3`,
  `volume_m3` and `area_m2` as well as the prompt's `diameter_m`, `length_m`, `width_m`,
  `height_m`, `depth_m`, `thickness_m` and `min_<dimension>_m`. The structure prompt asks for
  the two trench keys when the brief states a key trench (its worked example is a 0.7 m by 0.45
  m trench no brief uses, so the example cannot leak into an answer), for the smallest size
  a brief allows (`low_battery_pad`: `"diameter_m": 20, "min_diameter_m": 12`, which the
  registry records the same way) and, since the review, for `lifts` and a pit's `wall_slope`
  when the brief states them (the eval's area check multiplies compaction by the lifts; the
  volume check also reads a slope from the brief itself), and the volume check prices the
  trench, but a pad's fines depth and a berm's crest width still reach the planner only through
  the brief's constraints text, which the task-graph model does see. An "expected first-try
  outcome" that leans on those keys is computed on information the live pipeline may strip:
  before promising one, price the live requirements (`scripts/brief_budget.py
  out/live/<brief>.plan.json`) rather than the registry.
* **A surface layer is stripped in m2, a deeper one dug in m3.** The rate table's excavator
  m2 row is "strip or grade a surface layer up to 0.2 m; deeper cuts are excavation in m3",
  and the volume check credits an m2 task on a dug shape with its area x min(depth, 0.2 m),
  capped at the footprint. The script prices fines the same way: `instrument_pad` (15 cm) and
  `four_mast_pads` (20 cm) strip their fines in m2, `solar_array_pad` (30 cm) and the
  `one_surveyor_three_structures` hardstand (25 cm) dig theirs in m3 (a "Strip fines" task
  that is grade by name and excavate by unit). The two phase-3e false positives are gone:
  a 10 x 10 x 0.15 m instrument pad planned as strip 100 m2 + grade 100 m2 reads 15 of 15 m3
  (it read 0 % when only m3 counted), and a mast pad stripped over its 8 x 8 m footprint
  covers its 20 cm layer. The prompt caps a graph at 24 tasks, more only when the split rule
  needs them: `large_fleet_pad_berm_road`'s split graph has 22.
* Every rate is an illustrative lunar-rated figure, not a measurement (see the README's
  Limitations); the kinds hold for any table with the same ratios between types.

## The set

Fleet shorthand: `2 EX 5000` = two excavators at 5000 Wh each; depleted briefs state the
remaining charge. `LB` is the phase-order bound: the max of the critical path of one task per
phase with each haul after its source's whole excavation, per-type hours / robots, and the
longest one-robot-per-zone serial load. It bounds a plan that keeps every phase whole, as the
canonical graphs do; a plan that pipelines dig and haul parts (a haul part after the dig
part that feeds it, the supply verdict's advice) can finish below it;
`serial` is every phase one after another. Margins are `need / fleet Wh` per type (only the
types that matter).

| brief | kind | fleet | horizon | what it tests | margins from the script | expected first-try outcome (canonical graphs) |
| --- | --- | --- | --- | --- | --- | --- |
| `access_road` (demo twin) | A | 1 EX 5000, 1 HL 3000, 2 CP 3500, 1 SV 1500 | 240 h | road in two segments, foundation, berm from foundation spoil; two compactors share 25 h | EX 2816/5000 (+78 %), CP 2416/7000 (+190 %), HL 315/3000; LB 43 h, serial 59 h | feasible: lazy 47 h, split 48 h (fixture plan 63 h) |
| `cable_trench` | A | 1 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 120 h | segment order with surveyor gates, an earth bridge from trench spoil, tiny quantities (1 h floors) | EX 576/5000 (+768 %), CP 400/3500; LB 10 h, serial 14 h | feasible: lazy 16 h, split 16 h |
| `crater_ramp` | A | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 192 h | strict top-down segment chain (cut, check, compact, next), 240 m3 cut hauled as fill | EX 4627/10000 (+116 %), HL 1680/3000 (+79 %), CP 1456/3500; LB 65 h, serial 79 h | feasible: lazy 69 h, split 70 h |
| `depleted_excavators` (**video B**) | B | 2 EX 1750 rem., 1 HL 2000, 1 CP 2000, 1 SV 1000 | 120 h | three digs of 905 / 922 / 922 Wh on two 1750 Wh batteries: any two on one robot overflow it (1827-1844 Wh) | EX 2748/3500 (+27 %) but one task per dig does not pack (heaviest robot 1826.4 Wh = 104 % even in the best pairing; 1844 Wh in the worst); HL 756/2000, CP 1172/2000; LB 29 h, serial 54 h | **infeasible** when the key trench is in the requirements (see the caveat below): lazy and split (nothing is over 60 m3, so the prompt's split rule does not fire) both end in the CP-SAT core `energy:EX1, energy:EX2`; `fine` (bowl and pits halved) is feasible at 39 h |
| `four_mast_pads` | A | 2 EX 5000, 1 HL 3000, 2 CP 3500, 1 SV 1500 | 144 h | four independent structures, a 20-task graph, 20 cm of fines stripped in m2 and a 1 m anchor hole dug in m3 per pad | EX 1280/10000, CP 1024/7000; LB 9 h, serial 36 h | feasible: lazy 20 h, split 20 h |
| `front_loaded_excavation` (demo twin `front_loaded`) | D (horizon) | 2 EX 5000, 2 HL 3000, 2 CP 3500, 1 SV 1500 | 60 h | a 30 m bowl in one zone: 41 h of digging and 28 h of compaction cannot overlap; the window is 60 h and the brief forbids sectors and a smaller pad | energy fine (EX 4524/10000, CP 3947/7000); LB 81 h vs 60 h: **horizon short by 21 h (35 %)**; serial 114 h | **infeasible**: lazy fails the validator (`horizon_path`, 81 h); split fails CP-SAT with the core `horizon, zone:Z1` ("Zone Z1 admits one robot at a time, so its 6 tasks (80 h in total) cannot overlap"); advice: extend the window to ~85 h or allow two sectors (the demo's recorded live run: no plan after three repairs, CP-SAT-verified 85 h) |
| `habitat_berm_tight_horizon` | C | 2 EX 5000, 2 HL 3000, 2 CP 3500, 1 SV 1500 | 72 h | pit and borrow strip dug in parallel to feed a 280 m3 berm; the brief voices a one-at-a-time preference that yields to the deadline | EX 4224/10000, HL 1960/6000, CP 1696/7000 (all comfortable); LB 59 h, serial 86 h vs 72 h | feasible only in parallel: lazy 63 h, split 64 h; the serial graph fails `horizon_path` (86 h) |
| `habitat_foundation` | A | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1500 | 288 h | slab after compaction and check, trench after the pit, 144 m3 pit split across two excavators | EX 2519/10000, PR 1238/4000, HL 1008/3000; LB 51 h, serial 55 h | feasible: lazy 55 h, split 56 h |
| `instrument_pad` | A | 1 EX 5000, 1 CP 3500, 1 SV 1500 | 72 h | the smallest brief: one structure, four tasks, surveyor before and after; 15 cm of fines stripped in m2 (the live volume false positive of phase 3e) | EX 480/5000, CP 400/3500; LB 10 h | feasible: lazy 14 h |
| `isru_stockpile` | A | 1 EX 5000, 2 HL 3000, 1 CP 3500, 1 SV 1500 | 192 h | 210 m3 of borrow hauled by two haulers to a stockpile (receiving structure), survey per strip | EX 3360/5000 (+49 %), HL 1470/6000; LB 57 h, serial 61 h | feasible: lazy 61 h, split 61 h |
| `landing_pad` (demo twin, 300 m3 berm) | A | 2 EX 5000, 2 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1500 | 336 h | the canonical pad + berm with sintered crest: the coverage contract's reference case | PR 3476/4000 (+15 %, the tightest single battery), CP 2217/3500, HL 2100/6000, EX 2011/10000; LB 65 h, serial 104 h | feasible: lazy 71 h, split 71 h (hand-made `landing_pad_v1` fixture plan 80 h) |
| `large_fleet_pad_berm_road` | B | 3 EX 5000, 3 HL 3000, 2 CP 3500, 2 PR 4000, 1 SV 1500 | 288 h | eleven robots, three structures, a 22-task split graph; compaction fits only balanced within 6 %, sintering within 17 %, the 562 m3 berm haul must be split | CP 6627/7000 (+6 %), PR 6853/8000 (+17 %), HL 3938/9000 (one 3938 Wh haul > 3000), EX 6924/15000; LB 130 h, serial 249 h | lazy fails the validator (`energy_task`: 6080 Wh pad sinter, 3938 Wh haul); split is feasible at 136 h with compactors at 95 %; an uneven compactor split ends in an `energy:CPx` core |
| `low_battery_pad` | E | 1 EX 1800 rem., 1 HL 2000, 1 CP 2000, 1 SV 1000 | 168 h | the full 20 m bowl is 2513 Wh on an 1800 Wh excavator; the brief allows any diameter from 12 m up (`min_diameter_m: 12`, the volume check's floor: 56.5 of 157.1 m3) | EX 2513/1800 (**short by 713 Wh, 40 %**); at 16 m: 1608/1800 (+12 %), at 12 m: 905/1800 (+99 %) | **infeasible as written** (validator `energy_task:T2`: the one dig is the whole shortfall, so it is one verdict whose hint says splitting cannot fit it); re-scoped to 16 m feasible at 34 h, to 12 m at 24 h; a dig below 70 % of the 12 m floor is a `volume:S1` conflict, and so is a strip of the whole 20 m pad over a token dig |
| `one_surveyor_three_structures` | A | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1200 | 216 h | six surveyor visits on 1200 Wh, an apron sintered and a hardstand that is not | SV 464/1200, CP 1856/3500, PR 1720/4000; LB 35 h, serial 64 h | feasible: lazy 39 h, split 39 h |
| `propellant_pit_and_berm` | A | 2 EX 5000, 2 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1500 | 240 h | haul lane before the pit, floor slab after inspection, berm from pit spoil (2 m pit priced here with vertical sides, 100.5 m3; the volume check reads the brief's 1:1 sloped wall and accepts 58.6-159.2 m3, and judges the berm against that spoil) | EX 2569/10000, HL 1313/6000, CP 1601/3500; LB 38 h, serial 61 h | feasible: lazy 42 h, split 43 h |
| `road_and_pad` | B (validator) | 2 EX 5000, 1 HL 3000, 2 CP 3500, 1 PR 4000, 1 SV 1500 | 336 h | 1000 m2 of road compaction is 4000 Wh, more than one 3500 Wh compactor: a one-task graph fails before the solver | CP 5507/7000 (+27 %), EX 6608/10000, PR 1520/4000; LB 79 h, serial 132 h | lazy fails the validator (`energy_task:T2`); split feasible at 84 h |
| `shielding_mound` | A | 1 EX 5000, 3 HL 3000, 1 CP 3500, 1 SV 1500 | 216 h | three compacted lifts, three haulers kept out of the mound zone while the compactor works | EX 3120/5000 (+60 %), HL 840/9000, CP 1344/3500; LB 52 h, serial 52 h | feasible: lazy 55 h, split 57 h |
| `small_fleet_long_berm` | D (energy, 15 %) | 1 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 336 h | 360 m3 of borrow is 5760 Wh for a single 5000 Wh excavator; length, height and borrow depth fixed by the brief | EX 5760/5000 (**short by 760 Wh, 15 %**), HL 2450/3000 (+22 %), CP 2800/3500 (+25 %); LB 112 h | **infeasible**: lazy and split fail the validator with `energy_task:T4` (5,760 Wh of borrow on the single 5,000 Wh excavator, which is also the pool's whole shortfall); the live re-scope (`shrunk`: the strip as 600 m2) is `volume:S2` (120 of 360 m3, "plan the remaining 240 m3") before any energy verdict; advice: a second excavator, or 0.5 m of borrow (4800 Wh) if the rule is relaxed |
| `solar_array_pad` | D (energy, 25 %) | 1 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 240 h | two compaction lifts over 540 m2 plus the trench backfill are 4380 Wh for one 3500 Wh compactor; lifts and area fixed by the brief | CP 4380/3500 (**short by 880 Wh, 25 %**), EX 2712/5000; LB 88 h | **infeasible**: `energy_task:T4, energy:compactor` at the validator (the 4,320 Wh pad compaction exceeds the battery, and the pool is short beyond it); `shrunk` adds `volume:S2` (the anchor trench stripped in m2) and `scope:S1`; advice: a second compactor, or one lift (2220 Wh) if the vendor rule is relaxed |
| `three_pads_in_order` (**video C**) | C | 2 EX 5000, 2 CP 3500, 1 SV 1500 | 48 h | three independent 19 h pad chains; the brief voices an "A, then B, then C" preference that yields to the deadline | EX 2956/10000, CP 1847/7000 (comfortable); LB 19 h, **serial 57 h vs 48 h** | feasible in parallel (lazy 32 h, split 32 h); chained A->B->C the graph fails `horizon_path` (57 h, 60+ with transit) |
| `tight_battery` (demo twin) | B by fixture, A by the table | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 168 h | the offline demo of the repair loop: the recorded first graph (digs of 4000 + 4000 + 1900 Wh) is rejected by CP-SAT with `energy:EX1, energy:EX2` and the recorded repair halves the bowl | by the rate table the quantities (189 + 120 m3, 240 m2; the script prices the width-less key trench at the 4 m base, which the volume check no longer judges) are 6088/10000 Wh (+64 %), HL 2100/3000, CP 2217/3500; LB 65 h, serial 109 h | live: feasible first try is likely (lazy 69 h, split 70 h); the fixture run shows 1 repair, 66 h |
| `two_pads_one_printer` | A | 2 EX 5000, 2 HL 3000, 2 CP 3500, 1 PR 4000, 1 SV 1500 | 384 h | one printer serialising pad A, pad B and the crest; two pads feeding one berm | PR 3555/4000 (+12 %, single), EX 2262/10000, CP 2054/7000; LB 39 h, serial 96 h | feasible: lazy 57 h, split 58 h |

Count per kind: A 13 (including three of the four demo twins), B 3 (+ `tight_battery` by
fixture), C 2, D 3 (two energy, one horizon: `front_loaded_excavation`, the fourth twin), E 1.

## The two video candidates

**B: `depleted_excavators`.** One sentence a viewer gets: "each dig is about 920 Wh and the
excavators have 1750 Wh left, so two digs on one robot is 1840 Wh and the three digs cannot be
shared by two robots without splitting one." The one-task-per-dig graph passes the validator
(every task fits a battery, 2748 Wh fits the 3500 Wh fleet) and CP-SAT proves it infeasible
with the core `energy:EX1, energy:EX2`; the natural repair halves the bowl (2 x 452 Wh), after
which the solver verifies 44 h. The first live graph is *likely* to be the lazy one because
none of the digs exceeds the prompt's 60 m3 split threshold; a model that checks the packing
on its own will split up front and verify first try, which is a fine outcome for the eval and
a reason to keep `tight_battery`'s fixture as the offline fallback for the video.

*Caveat (live runs 3 and 6, Oct 23).* The third dig is the berm's key trench (60 x 1.2 x 0.8 m
= 57.6 m3), and the core above exists only when that volume reaches the task-graph model. In
run 6 Lightning structured the berm with length, height and width only, Ultra planned no
trench, and the plan verified first try at 43 h ("comfortable" when the live requirements are
priced); in run 3 Lightning listed `excavate` for the berm, the `scope:S2` conflict fired and
Ultra satisfied it with two 5 m3 tasks, because coverage is binary. Three things changed since:
the structure prompt asks for `trench_width_m` / `trench_depth_m`, so the trench has a channel
to the planner; the volume check prices the trench; and since phase 3e a shortfall is a
validation conflict, not an advisory, so a 10 m3 token repair is rejected as `volume:S2` ("its
key trench is 60 x 1.2 x 0.8 m = 57.6 m3 of excavation; excavator tasks cover 10 m3 (17 %)", hint
"plan the remaining 47.6 m3 of the S2 key trench as excavator tasks in m3") and the next repair
has to plan the trench for real. Whether Lightning copies the keys reliably is what the eval
measures; if it does not, a B brief whose three digs are plain bowls and boxes (a pad bowl plus
two 8 x 8 x 0.9 m borrow pits) carries the same mechanism with no hidden key, and
`tight_battery`'s recorded fixture stays the offline fallback for the video.

**C: `three_pads_in_order`.** One sentence: "done one after the other the three pads take
57 h and the lander arrives at 48 h; done side by side on their own zones they take 34 h." The
brief states a one-pad-at-a-time preference and says it yields to the deadline, so a model
that encodes the preference as `after` edges produces a chain the validator rejects with
`horizon_path` (57 h > 48 h); dropping the two edges is the whole repair, and CP-SAT then
verifies 34 h with the two excavators and two compactors in parallel. If the model ignores the
preference the first graph verifies at once, which the table records as the expected parallel
outcome. `habitat_berm_tight_horizon` is the second C with the same shape (serial 86 h vs 72 h).

*What C measures.* Both C briefs tell the model, in the same sentence, that the preference
yields to the deadline and that the structures may be worked in parallel; Lightning copies the
constraints into the requirements the task-graph model reads, and that prompt asks for a
critical path well below the horizon, so the rational first graph is the parallel one. Live
(`three_pads_in_order`, Oct 23) that is what happened: 37 h, verified first try, no serial
edge, and the constraint sentence had not even survived structuring. C therefore measures
preference handling (does the model drop a stated preference when the deadline requires it,
and does it say so), not the repair loop; the `horizon_path` -> repair -> advice beat the loop
is meant to show comes from the D brief `front_loaded_excavation` (live: `horizon_path` in
round 1, three repairs, then `finishing needs 86 h, 26 h past the 60 h deadline`), which is
the horizon beat to use in the video. It ships as the demo brief `front_loaded`, whose
recorded run (final prompts) ends the same way at 85 h, with a `scope:S1` refusal on the way.

## Commands

All from `backend/`. The script needs no API key and makes no model call.

```bash
# the whole set, one line per brief (verdict, margins, canonical-graph outcomes)
uv run python scripts/brief_budget.py --all examples/eval --graphs lazy,split
# the rows of the table above
uv run python scripts/brief_budget.py --all examples/eval --markdown --graphs lazy,split

# the live re-scope next to the canonical graphs: `shrunk` plans every dig as m2 of stripping
uv run python scripts/brief_budget.py --all examples/eval --graphs lazy,split,shrunk

# one brief in full: per-phase quantities, hours, Wh, basis, per-type packing, bounds, verdict
uv run python scripts/brief_budget.py examples/eval/depleted_excavators.md --graphs lazy,fine
uv run python scripts/brief_budget.py examples/eval/three_pads_in_order.md --graphs lazy,serial
uv run python scripts/brief_budget.py examples/eval/front_loaded_excavation.md --graphs lazy,split
uv run python scripts/brief_budget.py examples/eval/small_fleet_long_berm.md --graphs lazy,shrunk

# the E path: the brief as written, then at the diameters it allows
uv run python scripts/brief_budget.py examples/eval/low_battery_pad.md --graphs lazy
uv run python scripts/brief_budget.py examples/eval/low_battery_pad.md --set S1.diameter_m=16 --graphs lazy
uv run python scripts/brief_budget.py examples/eval/low_battery_pad.md --set S1.diameter_m=12 --graphs lazy

# a recorded structure answer or a plan's requirements instead of the registry
uv run python scripts/brief_budget.py fixtures/landing_pad/structure.json
uv run python scripts/brief_budget.py out/landing_pad.plan.json --json

# tuning a brief: edit numbers before computing (structure dims, a battery, the horizon)
uv run python scripts/brief_budget.py examples/eval/habitat_berm_tight_horizon.md \
  --set horizon_h=60 --set S2.length_m=80 --graphs lazy,serial

# an inline spec (the Requirements shape: structures with dims and operations, robots, horizon_h)
uv run python scripts/brief_budget.py - --spec '{"horizon_h": 48, "robots": [{"id": "EX1", "type": "excavator", "battery_wh": 5000}], "structures": [{"id": "S1", "type": "borrow_strip", "length_m": 10, "width_m": 6, "depth_m": 0.5, "operations": ["excavate"]}]}'
```

`--json` prints the same budget as JSON (per structure phases with `basis`, per type
`need_wh / fleet_wh / margin_pct / balance_pct / pack_feasible`, bounds, verdict, graph
outcomes); `--no-solve` skips the graphs; `--time-limit` bounds each CP-SAT solve.

## Keeping the set honest

* `backend/tests/test_brief_budget.py` pins every brief to its kind (`EXPECTED_KIND`) and
  checks that each parses (fleet, deadline, registry entry), that the two video candidates
  fail and recover exactly as described above, through the real validator and CP-SAT, and
  that every `lazy` and `split` graph covers the volumes the validator measures while the
  `shrunk` re-scope of a D brief is a `volume:` conflict, and that every `lazy`, `split`,
  `fine`, `serial` and `shrunk` graph passes the supply check (each haul after the digs or
  strips its fill comes from); `backend/tests/test_double_count.py` that none of them is a
  double count and `backend/tests/test_footprints.py` that only the four rectangle briefs'
  recorded diameters are corrected (every structure Lightning recorded in runs 2 and 3 is in
  `backend/tests/data/recorded_structures.json`); `backend/tests/test_sizes.py` that only
  `low_battery_pad`'s recorded structures gain a minimum or lose a distance, each read alone
  and beside the structures it was recorded with (every distinct structuring is in
  `backend/tests/data/recorded_structurings.json`), and
  `backend/tests/test_unstaffed_operations.py` that only the operations a brief's own fleet
  cannot perform are dropped. A brief
  whose numbers drift out of its kind fails the test; a new brief must be registered in
  `BRIEF_STRUCTURES`, classified, and given its letter in `backend/examples/eval/kinds.json`,
  which `moonwright eval --briefs examples/eval` picks up for its per-kind table (the test
  checks the two agree).
* The registry holds only the structures (dimensions, the operations the brief asks for, the
  spoil source); fleet and deadline are parsed from the brief text, so a battery or deadline
  edit in the `.md` changes the budget without touching the script.
* Four briefs have a demo twin in `examples/briefs/`, whose recorded fixtures drive the
  offline demo and the e2e tests. `access_road`, `tight_battery` and `front_loaded_excavation`
  (the demo's `front_loaded`) are byte-identical to theirs. `landing_pad` differs on purpose:
  the demo's berm is 40 m x 1.5 m x 4 m = 120 m3, so the pad's 125.7 m3 of spoil builds it,
  while this brief keeps the 300 m3 berm so live runs 1-3 stay comparable
  (`scripts/brief_budget.py` prices the demo brief from its own entry in `DEMO_STRUCTURES`).
* Measured outcomes (feasible-plan rate, repairs, scope drift per kind) belong in the README
  once the live eval has run; nothing here is a measurement, every number is the rate table's
  arithmetic.
