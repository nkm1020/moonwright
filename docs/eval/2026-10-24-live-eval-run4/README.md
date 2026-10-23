<!-- Run 4 of the live evaluation (Oct 24 2026, 02:06-03:02 KST), the first on the final pipeline (commit 3466621):
phase 3j (an operation the fleet cannot perform is dropped at the structure stage, a dug structure's haul is met by the hauls
its digs feed, brief quotes come only from a structure's own entry, a smaller size the brief allows and a figure it gives only
as a distance are read from the brief) and phase 3k (exact, order-dependent travel in CP-SAT; the validator and the hints use
sound lower bounds). Same matrix as runs 1-3 (22 briefs x Ultra / Super x loop on x 3), 5 workers. As the run scored itself, Ultra
was 86 % verified and 82 % verified in scope. Phase 3l (an operation a structure's own entry states is restored when the
structuring omitted it), merged after the run, takes one more plan out of scope: cable_trench ultra #2 never compacts the
"compacted 10 cm bedding layer" its entry states. Run 3 -> run 4, both rescored with the final checks (eval.rescored.csv,
commit a8f9419), Ultra: verified 89 % -> 86 %, verified in scope 77 % -> 80 %, in scope with no supply or double-count
conflict 74 % -> 80 %, first try 50 % -> 55 %. Super: verified 85 % -> 82 %, in scope 61 % -> 76 %. Kind E (low_battery_pad,
a smaller pad allowed): Ultra 2 of 3 runs in scope under the final checks in run 3 (0 of 3 as scored then) -> 3 of 3. Kind D
(designed impossible): 7 of 9 Ultra runs ended without a plan, 5 of them with CP-SAT-proven advice; the other 2 verified only
by trimming 10-40 % of the work, so neither is in scope (Super: 8 of 9 without a plan, 4 with advice). 326 requests, 0 errors,
5 answers cut at the token limit (all Ultra repair calls, each asked again), $7.85. eval.rescored.csv adds the rescore
columns (backend/scripts/rescore_eval.py at a8f9419: two rows leave scope, Ultra and Super cable_trench #2). Every run's PlanResult is under plans/ (plan_path in
eval.csv is relative to this folder); calls.md is the Token Factory call log summary; replan/ is the replanning measurement
below. -->

# Moonwright eval: 22 brief(s) x 2 condition(s) x 3 repeat(s)

Generated 2026-10-23T18:02:43+00:00; llm `nebius`; briefs from `examples/eval`; 132 run(s) on 5 worker(s); max_repairs 3; feedback `core`; max_answer_retries the planner's own; thinking `auto`.

## How to read

A condition is `<arm>/<loop>`: the arm names the model that proposes the task graph and answers the repair prompts (`ultra` = `nvidia/Nemotron-3-Ultra-550b-a55b`, `super` = `nvidia/nemotron-3-super-120b-a12b`) and the loop is the repair-loop setting. `loop_on` allows up to 3 repair round(s), each fed the solver's minimal infeasibility core with its numeric hints; `loop_off` takes the first valid graph's verdict as final. Briefs are structured by `nvidia/Nemotron-3_5-Lightning`; explanations (only with `--explain`) are written by `nvidia/nemotron-3-super-120b-a12b`. Thinking `auto`: the task-graph and repair calls reason, structure and explain answer directly. Each brief is structured once per repeat and every condition plans from that same requirements object (`requirements_sha` in the rows), so arms and loop settings are a paired comparison on the same first-graph verdict rather than each sampling the structuring model anew; the shared structure step and its tokens appear in every row of the pair. `feasible` is the share of runs that ended with a CP-SAT-verified schedule, with a Wilson 95 % interval; `feasible in scope` the share that is feasible AND whose final graph plans every structure's volume (dug m3, hauled fill) and its compaction and sintering area (footprint x lifts) at 95 % or more of the smallest size the brief allows (the validator already rejects a volume under 70 %, so a plan that verified after trimming the work 15-25 % is feasible but not in scope; `min_volume_share` and `min_area_share` in the rows say how far off; an excess such as a berm hauled twice is counted in `volume_advisories`, not against the scope); `first try` the share verified without a repair round; `repairs` the mean repair rounds per run; `makespan` the mean over feasible runs; latency is wall time per run (p50 / p95, model calls and solver included); tokens are per run; cost is per run at the Token Factory list prices (`-` when the model is not priced). In the per-brief matrix a cell reads `ok <makespan> h / <repairs> rep` for a verified run (with `k/n` when repeated), `ok ... scope!` when it is verified but not in scope, and `fail <conflict families>` for a run that ended without a verified plan. `rejected answers` is the share of runs in which at least one task-graph or repair answer was rejected before any verdict (it failed the schema or the call failed: `schema` / `llm_error`, an answer cut off at max_tokens and re-asked among them; when the re-asks add up to more such answers than runs their number follows in parentheses) and `p50 cost` the median cost of a priced run, next to the mean in `cost / run`. Every run's PlanResult is kept under `plans/` as `<brief>__<arm>__<loop>__<repeat>.plan.json` (`plan_path` in the rows, relative to the CSV's directory, so a row resumed from a run that kept its plans elsewhere names its own file: the JSON `POST /plan` returns, so `moonwright simulate` and `scripts/replan_eval.py` read it) and each (brief, repeat) job's structuring as `<brief>__<repeat>.structure.json` (`<brief>__<repeat>.2.structure.json` for the second structuring of a job `--resume` completed; its `requirements_sha` pairs it with the rows). The per-kind table groups briefs by their designed kind (`--kinds`, or `kinds.json` next to the briefs; `docs/eval-briefs.md`): A comfortable, B battery-split, C horizon-bound, D impossible, E scope reduction allowed.

## Per condition

| condition | n | feasible (95% CI) | feasible in scope (95% CI) | first try | repairs | makespan | latency p50 / p95 | tokens in / out | cost / run | cost total | rejected answers | p50 cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ultra/loop_on` | 66 | 86% (76%-93%) | 82% (71%-89%) | 55% | 0.85 | 54 h | 58.4 s / 369.7 s | 12386 / 29818 | $0.0974 | $6.43 | 0/66 | $0.0535 |
| `super/loop_on` | 66 | 82% (71%-89%) | 77% (66%-86%) | 45% | 1.00 | 56 h | 102.9 s / 429.4 s | 12697 / 20836 | $0.0215 | $1.42 | 0/66 | $0.0146 |

## Per kind

| kind | condition | n | feasible | feasible in scope | first try | repairs | briefs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `A` | `ultra/loop_on` | 39 | 39/39 | 38/39 | 25/39 | 0.49 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `A` | `super/loop_on` | 39 | 37/39 | 35/39 | 20/39 | 0.64 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `B` | `ultra/loop_on` | 9 | 7/9 | 7/9 | 6/9 | 0.78 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `B` | `super/loop_on` | 9 | 7/9 | 7/9 | 5/9 | 1.11 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `D` | `ultra/loop_on` | 9 | 2/9 | 0/9 | 0/9 | 2.78 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `D` | `super/loop_on` | 9 | 1/9 | 0/9 | 0/9 | 2.78 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `C` | `ultra/loop_on` | 6 | 6/6 | 6/6 | 5/6 | 0.17 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `C` | `super/loop_on` | 6 | 6/6 | 6/6 | 5/6 | 0.50 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `E` | `ultra/loop_on` | 3 | 3/3 | 3/3 | 0/3 | 1.33 | `low_battery_pad` |
| `E` | `super/loop_on` | 3 | 3/3 | 3/3 | 0/3 | 1.00 | `low_battery_pad` |

## Per brief

| brief | ultra/loop_on | super/loop_on |
| --- | --- | --- |
| `access_road` | ok 3/3 50 h / 1 rep | ok 3/3 58 h / 0.3 rep scope! 1/3 |
| `cable_trench` | ok 3/3 17 h / 0.3 rep | ok 3/3 16 h / 0.3 rep |
| `crater_ramp` | ok 3/3 71 h / 0 rep | ok 3/3 84 h / 0.7 rep |
| `depleted_excavators` | ok 3/3 34 h / 0 rep | ok 3/3 37 h / 0.7 rep |
| `four_mast_pads` | ok 3/3 38 h / 0 rep | ok 3/3 39 h / 0 rep |
| `front_loaded_excavation` | fail 3/3 horizon,horizon_path,zone | fail 3/3 horizon,supply,volume,zone |
| `habitat_berm_tight_horizon` | ok 3/3 59 h / 0.3 rep | ok 3/3 55 h / 1 rep |
| `habitat_foundation` | ok 3/3 45 h / 0 rep | ok 3/3 44 h / 0 rep |
| `instrument_pad` | ok 3/3 12 h / 0 rep | ok 3/3 13 h / 0 rep |
| `isru_stockpile` | ok 3/3 44 h / 0.3 rep | ok 3/3 37 h / 0.3 rep |
| `landing_pad` | ok 3/3 90 h / 2 rep | ok 1/3 84 h / 2 rep; fail 2/3 reference,scope,supply |
| `large_fleet_pad_berm_road` | ok 1/3 158 h / 1 rep; fail 2/3 energy,reference,volume | ok 1/3 139 h / 2 rep; fail 2/3 energy,horizon_path,reference,scope |
| `low_battery_pad` | ok 3/3 40 h / 1.3 rep | ok 3/3 41 h / 1 rep |
| `one_surveyor_three_structures` | ok 3/3 42 h / 0.7 rep scope! 1/3 | ok 3/3 51 h / 1.3 rep |
| `propellant_pit_and_berm` | ok 3/3 63 h / 0.7 rep | ok 3/3 79 h / 0.3 rep |
| `road_and_pad` | ok 3/3 103 h / 0 rep | ok 3/3 113 h / 0 rep |
| `shielding_mound` | ok 3/3 41 h / 0 rep | ok 3/3 47 h / 0.7 rep |
| `small_fleet_long_berm` | ok 1/3 106 h / 2 rep scope!; fail 2/3 energy,volume | ok 1/3 108 h / 1 rep scope!; fail 2/3 energy |
| `solar_array_pad` | ok 1/3 104 h / 2 rep scope!; fail 2/3 energy | fail 3/3 energy |
| `three_pads_in_order` | ok 3/3 30 h / 0 rep | ok 3/3 31 h / 0 rep |
| `tight_battery` | ok 3/3 71 h / 0.3 rep | ok 3/3 85 h / 0.7 rep |
| `two_pads_one_printer` | ok 3/3 58 h / 1 rep | ok 3/3 74 h / 1 rep scope! 1/3 |


## Side measurements on run 4's verified plans

* **Robot failures replanned by CP-SAT alone** (`replan/`, `backend/scripts/replan_eval.py --llm fixture --sole-of-type
  skip --max-escalations 1000`, the first verified Ultra plan of each of the 21 briefs that have one). Every robot that owns
  scheduled work and has a peer of its type was failed at 10 % and 50 % of its first task: 102 injections. CP-SAT
  rescheduled the remaining work on its own in 82 of them (80 %, 95 % CI 72-87 %), with a median solve of 9 ms (p95 52 ms)
  and a median makespan cost of 4 h, and no model call. In the other 20 the surviving robots provably cannot carry the
  work (an energy core), which is where Moonwright asks Nemotron to re-scope. The same measurement on run 3's 21 plans
  recovered 67 of 110 (61 %) with the solver of the time, whose travel model reserved the longest leg a robot could
  drive before every task, and 76 of 110 (69 %) with exact travel: the allowance alone cost 9 recoveries.
* **Escalations to Nemotron, live** (`replan/run4_ultra_live.*`, the same 102 injections with `--llm nebius
  --max-escalations 10`, Oct 24 04:21-04:37 KST, $1.13). Nemotron 3 Ultra was asked to re-scope the first 10
  injections whose remaining work the surviving robots provably could not carry (energy cores: depleted_excavators'
  excavators, large_fleet_pad_berm_road's compactors and printer); 8 came back with a replan CP-SAT verified (1-3
  repair rounds, median model time 103 s), and in the other 2 the previous plan stayed in force. With the 82
  solver-only replans, 90 of the 92 injections that were decided ended with a verified schedule; the last 10 were not
  asked (budget). A re-scope trades work for time by design: two of large_fleet_pad_berm_road's verified replans
  finish before the original plan (141 h and 146 h against 158 h) because the remaining compaction or sintering was
  cut, which the replan's repair diff records.
* **Nemotron writing the schedule itself** (run 3's `direct-schedule/`, re-solved with exact travel). Ultra's own
  schedules for the 21 verified run-3 graphs all pass `check_schedule`, but CP-SAT now finds one at least as short for
  all 21 and proves it optimal: equal on 16, shorter on 5 (large_fleet_pad_berm_road 197 h against 233 h, crater_ramp 69
  against 80 h, four_mast_pads 41 against 43 h, three_pads_in_order 31 against 33 h, depleted_excavators 31 against 32
  h). With the allowance, 9 of the 21 language-model schedules had been shorter than the solver's "optimal" one.
