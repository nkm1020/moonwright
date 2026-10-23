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
| **D** impossible | energy short by 15-25 % or the horizon short by a third, with the scope fixed by the brief | a graph that shrinks the work below 70 % is a `volume:<structure>` conflict (phase 3e), and one that trims it 15-25 % verifies but is not *in scope* (95 % of every volume and compaction / sintering area); the full-scope graph exhausts the repairs and the advice path must name the shortfall (add a robot, extend the window, relax a rule) |
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
  source's excavation; spoil another structure receives is never hauled twice.
* **The canonical graphs are not the model's graphs.** `lazy` is one task per phase per
  structure; `split` applies the prompt's rule (work above 60 m3 / 300 m2 shared equally
  across the type's robots); `fine` shares every physical phase that way; `serial` chains the
  structures one after another; `shrunk` is `lazy` with every dig in m3 planned as stripping
  its footprint in m2, the re-scope the live models made to fit a battery in the first
  evaluation. "Expected first-try outcome" is the solver's verdict on `lazy` and `split`,
  i.e. on a careless and on a rule-following planner; a live Nemotron graph can land anywhere
  in between, which is exactly what the eval measures.
* **Under-scope is a conflict since phase 3e.** The first live evaluation (22 briefs x the
  Ultra and Super arms x 3, 132 runs) verified 85 % and 91 % of the runs of its two arms but
  only 45 % and 38 % in scope: the models satisfied the solver by shrinking the work (a 20 m
  pad dug as a 12 m one, the 360 m3 borrow strip of `small_fleet_long_berm` planned as 600 m2
  of stripping, a 300 m3 berm hauled twice), and the volume advisory never reached the repair
  prompt. The validator now rejects a whole plan whose tasks cover less than 70 % of the
  smallest volume a structure's dimensions allow (`volume:<structure>`, with a hint that says
  how many m3 to add); an excess (hauled or dug 1.5 times over) stays an advisory, and the fill
  of a stockpile is judged neither way. Every `lazy` and `split` graph below covers its volumes
  and is in scope (the script prices the same shapes as `moonwright.pipeline.rates.dims_volumes`,
  and a test holds the two together, the brief's own text included); the `shrunk` graphs fail
  `volume:` wherever a dig is deeper than the 0.2 m an m2 task strips. So a D brief's re-scope
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
* **Lower bounds ignore transit.** CP-SAT adds a whole-hour transit allowance per task and
  0.5 Wh per grid cell, so a verified makespan sits a few hours above `LB`; the `lazy` /
  `split` makespans quoted here include it.
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
remaining charge. `LB` is the parallel lower bound (max of the critical path with the fill
dependency, per-type hours / robots, and the longest one-robot-per-zone serial load);
`serial` is every phase one after another. Margins are `need / fleet Wh` per type (only the
types that matter).

| brief | kind | fleet | horizon | what it tests | margins from the script | expected first-try outcome (canonical graphs) |
| --- | --- | --- | --- | --- | --- | --- |
| `access_road` (demo twin) | A | 1 EX 5000, 1 HL 3000, 2 CP 3500, 1 SV 1500 | 240 h | road in two segments, foundation, berm from foundation spoil; two compactors share 25 h | EX 2816/5000 (+78 %), CP 2416/7000 (+190 %), HL 315/3000; LB 43 h, serial 59 h | feasible: lazy 47 h, split 49 h (fixture plan 64 h) |
| `cable_trench` | A | 1 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 120 h | segment order with surveyor gates, an earth bridge from trench spoil, tiny quantities (1 h floors) | EX 576/5000 (+768 %), CP 400/3500; LB 10 h, serial 14 h | feasible: lazy 18 h, split 18 h |
| `crater_ramp` | A | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 192 h | strict top-down segment chain (cut, check, compact, next), 240 m3 cut hauled as fill | EX 4627/10000 (+116 %), HL 1680/3000 (+79 %), CP 1456/3500; LB 65 h, serial 79 h | feasible: lazy 69 h, split 70 h |
| `depleted_excavators` (**video B**) | B | 2 EX 1750 rem., 1 HL 2000, 1 CP 2000, 1 SV 1000 | 120 h | three digs of 905 / 922 / 922 Wh on two 1750 Wh batteries: any two on one robot overflow it (1827-1844 Wh) | EX 2748/3500 (+27 %) but one task per dig does not pack (heaviest robot 1826.4 Wh = 104 % even in the best pairing; 1844 Wh in the worst); HL 756/2000, CP 1172/2000; LB 29 h, serial 54 h | **infeasible** when the key trench is in the requirements (see the caveat below): lazy and split (nothing is over 60 m3, so the prompt's split rule does not fire) both end in the CP-SAT core `energy:EX1, energy:EX2`; `fine` (bowl and pits halved) is feasible at 41 h |
| `four_mast_pads` | A | 2 EX 5000, 1 HL 3000, 2 CP 3500, 1 SV 1500 | 144 h | four independent structures, a 20-task graph, 20 cm of fines stripped in m2 and a 1 m anchor hole dug in m3 per pad | EX 1280/10000, CP 1024/7000; LB 9 h, serial 36 h | feasible: lazy 34 h, split 34 h |
| `front_loaded_excavation` | D (horizon) | 2 EX 5000, 2 HL 3000, 2 CP 3500, 1 SV 1500 | 60 h | a 30 m bowl in one zone: 41 h of digging and 28 h of compaction cannot overlap; the window is 60 h and the brief forbids sectors and a smaller pad | energy fine (EX 4524/10000, CP 3947/7000); LB 81 h vs 60 h: **horizon short by 21 h (35 %)**; serial 114 h | **infeasible**: lazy fails the validator (`horizon_path`, 81 h); split fails CP-SAT with the core `horizon, zone:Z1` ("Zone Z1 admits one robot at a time, so its 6 tasks (80 h in total) cannot overlap"); advice: extend the window to ~85 h or allow two sectors |
| `habitat_berm_tight_horizon` | C | 2 EX 5000, 2 HL 3000, 2 CP 3500, 1 SV 1500 | 72 h | pit and borrow strip dug in parallel to feed a 280 m3 berm; the brief voices a one-at-a-time preference that yields to the deadline | EX 4224/10000, HL 1960/6000, CP 1696/7000 (all comfortable); LB 59 h, serial 86 h vs 72 h | feasible only in parallel: lazy 63 h, split 64 h; the serial graph fails `horizon_path` (86 h) |
| `habitat_foundation` | A | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1500 | 288 h | slab after compaction and check, trench after the pit, 144 m3 pit split across two excavators | EX 2519/10000, PR 1238/4000, HL 1008/3000; LB 51 h, serial 55 h | feasible: lazy 55 h, split 56 h |
| `instrument_pad` | A | 1 EX 5000, 1 CP 3500, 1 SV 1500 | 72 h | the smallest brief: one structure, four tasks, surveyor before and after; 15 cm of fines stripped in m2 (the live volume false positive of phase 3e) | EX 480/5000, CP 400/3500; LB 10 h | feasible: lazy 14 h |
| `isru_stockpile` | A | 1 EX 5000, 2 HL 3000, 1 CP 3500, 1 SV 1500 | 192 h | 210 m3 of borrow hauled by two haulers to a stockpile (receiving structure), survey per strip | EX 3360/5000 (+49 %), HL 1470/6000; LB 57 h, serial 61 h | feasible: lazy 61 h, split 61 h |
| `landing_pad` (demo twin) | A | 2 EX 5000, 2 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1500 | 336 h | the canonical pad + berm with sintered crest: the coverage contract's reference case | PR 3476/4000 (+15 %, the tightest single battery), CP 2217/3500, HL 2100/6000, EX 2011/10000; LB 65 h, serial 104 h | feasible: lazy 73 h, split 73 h (fixture plan 80 h) |
| `large_fleet_pad_berm_road` | B | 3 EX 5000, 3 HL 3000, 2 CP 3500, 2 PR 4000, 1 SV 1500 | 288 h | eleven robots, three structures, a 22-task split graph; compaction fits only balanced within 6 %, sintering within 17 %, the 562 m3 berm haul must be split | CP 6627/7000 (+6 %), PR 6853/8000 (+17 %), HL 3938/9000 (one 3938 Wh haul > 3000), EX 6924/15000; LB 130 h, serial 249 h | lazy fails the validator (`energy_task`: 6080 Wh pad sinter, 3938 Wh haul); split is feasible at 136 h with compactors at 95 %; an uneven compactor split ends in an `energy:CPx` core |
| `low_battery_pad` | E | 1 EX 1800 rem., 1 HL 2000, 1 CP 2000, 1 SV 1000 | 168 h | the full 20 m bowl is 2513 Wh on an 1800 Wh excavator; the brief allows any diameter from 12 m up (`min_diameter_m: 12`, the volume check's floor: 56.5 of 157.1 m3) | EX 2513/1800 (**short by 713 Wh, 40 %**); at 16 m: 1608/1800 (+12 %), at 12 m: 905/1800 (+99 %) | **infeasible as written** (validator `energy_task:T2`: the one dig is the whole shortfall, so it is one verdict whose hint says splitting cannot fit it); re-scoped to 16 m feasible at 36 h, to 12 m at 26 h; a dig below 70 % of the 12 m floor is a `volume:S1` conflict, and so is a strip of the whole 20 m pad over a token dig |
| `one_surveyor_three_structures` | A | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1200 | 216 h | six surveyor visits on 1200 Wh, an apron sintered and a hardstand that is not | SV 464/1200, CP 1856/3500, PR 1720/4000; LB 35 h, serial 64 h | feasible: lazy 42 h, split 42 h |
| `propellant_pit_and_berm` | A | 2 EX 5000, 2 HL 3000, 1 CP 3500, 1 PR 4000, 1 SV 1500 | 240 h | haul lane before the pit, floor slab after inspection, berm from pit spoil (2 m pit priced here with vertical sides, 100.5 m3; the volume check reads the brief's 1:1 sloped wall and accepts 58.6-159.2 m3, and judges the berm against that spoil) | EX 2569/10000, HL 1313/6000, CP 1601/3500; LB 38 h, serial 61 h | feasible: lazy 42 h, split 43 h |
| `road_and_pad` | B (validator) | 2 EX 5000, 1 HL 3000, 2 CP 3500, 1 PR 4000, 1 SV 1500 | 336 h | 1000 m2 of road compaction is 4000 Wh, more than one 3500 Wh compactor: a one-task graph fails before the solver | CP 5507/7000 (+27 %), EX 5648/10000, PR 1520/4000; LB 79 h, serial 133 h | lazy fails the validator (`energy_task:T2`); split feasible at 84 h |
| `shielding_mound` | A | 1 EX 5000, 3 HL 3000, 1 CP 3500, 1 SV 1500 | 216 h | three compacted lifts, three haulers kept out of the mound zone while the compactor works | EX 3120/5000 (+60 %), HL 840/9000, CP 1344/3500; LB 28 h, serial 52 h | feasible: lazy 34 h, split 34 h |
| `small_fleet_long_berm` | D (energy, 15 %) | 1 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 336 h | 360 m3 of borrow is 5760 Wh for a single 5000 Wh excavator; length, height and borrow depth fixed by the brief | EX 5760/5000 (**short by 760 Wh, 15 %**), HL 2450/3000 (+22 %), CP 2800/3500 (+25 %); LB 60 h | **infeasible**: lazy and split fail the validator with `energy_task:T4` (5,760 Wh of borrow on the single 5,000 Wh excavator, which is also the pool's whole shortfall); the live re-scope (`shrunk`: the strip as 600 m2) is `volume:S2` (120 of 360 m3, "plan the remaining 240 m3") before any energy verdict; advice: a second excavator, or 0.5 m of borrow (4800 Wh) if the rule is relaxed |
| `solar_array_pad` | D (energy, 25 %) | 1 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 240 h | two compaction lifts over 540 m2 plus the trench backfill are 4380 Wh for one 3500 Wh compactor; lifts and area fixed by the brief | CP 4380/3500 (**short by 880 Wh, 25 %**), EX 2712/5000; LB 88 h | **infeasible**: `energy_task:T4, energy:compactor` at the validator (the 4,320 Wh pad compaction exceeds the battery, and the pool is short beyond it); `shrunk` adds `volume:S2` (the anchor trench stripped in m2) and `scope:S1`; advice: a second compactor, or one lift (2220 Wh) if the vendor rule is relaxed |
| `three_pads_in_order` (**video C**) | C | 2 EX 5000, 2 CP 3500, 1 SV 1500 | 48 h | three independent 19 h pad chains; the brief voices an "A, then B, then C" preference that yields to the deadline | EX 2956/10000, CP 1847/7000 (comfortable); LB 19 h, **serial 57 h vs 48 h** | feasible in parallel (lazy 34 h, split 40 h); chained A->B->C the graph fails `horizon_path` (57 h, 60+ with transit) |
| `tight_battery` (demo twin) | B by fixture, A by the table | 2 EX 5000, 1 HL 3000, 1 CP 3500, 1 SV 1500 | 168 h | the offline demo of the repair loop: the recorded first graph (digs of 4000 + 4000 + 1900 Wh) is rejected by CP-SAT with `energy:EX1, energy:EX2` and the recorded repair halves the bowl | by the rate table the quantities (189 + 120 m3, 240 m2; the script prices the width-less key trench at the 4 m base, which the volume check no longer judges) are 6088/10000 Wh (+64 %), HL 2100/3000, CP 2217/3500; LB 65 h, serial 109 h | live: feasible first try is likely (lazy 69 h, split 70 h); the fixture run shows 1 repair, 68 h |
| `two_pads_one_printer` | A | 2 EX 5000, 2 HL 3000, 2 CP 3500, 1 PR 4000, 1 SV 1500 | 384 h | one printer serialising pad A, pad B and the crest; two pads feeding one berm | PR 3555/4000 (+12 %, single), EX 2262/10000, CP 2054/7000; LB 39 h, serial 96 h | feasible: lazy 61 h, split 62 h |

Count per kind: A 13 (including the three demo twins), B 3 (+ `tight_battery` by fixture),
C 2, D 3 (two energy, one horizon), E 1.

## The two video candidates

**B: `depleted_excavators`.** One sentence a viewer gets: "each dig is about 920 Wh and the
excavators have 1750 Wh left, so two digs on one robot is 1840 Wh and the three digs cannot be
shared by two robots without splitting one." The one-task-per-dig graph passes the validator
(every task fits a battery, 2748 Wh fits the 3500 Wh fleet) and CP-SAT proves it infeasible
with the core `energy:EX1, energy:EX2`; the natural repair halves the bowl (2 x 452 Wh), after
which the solver verifies 41 h. The first live graph is *likely* to be the lazy one because
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
the horizon beat to use in the video.

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
  `shrunk` re-scope of a D brief is a `volume:` conflict. A brief
  whose numbers drift out of its kind fails the test; a new brief must be registered in
  `BRIEF_STRUCTURES`, classified, and given its letter in `backend/examples/eval/kinds.json`,
  which `moonwright eval --briefs examples/eval` picks up for its per-kind table (the test
  checks the two agree).
* The registry holds only the structures (dimensions, the operations the brief asks for, the
  spoil source); fleet and deadline are parsed from the brief text, so a battery or deadline
  edit in the `.md` changes the budget without touching the script.
* The three demo twins (`landing_pad`, `access_road`, `tight_battery`) are byte-identical to
  `examples/briefs/`, whose recorded fixtures drive the offline demo and the e2e tests; they
  were not edited.
* Measured outcomes (feasible-plan rate, repairs, scope drift per kind) belong in the README
  once the live eval has run; nothing here is a measurement, every number is the rate table's
  arithmetic.
