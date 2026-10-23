<!-- Run 3 of the live evaluation (Oct 23 2026, 19:26-20:32 KST), the first after phase 3g (commit 40bd686): supply:<haul>
is a hard conflict (a haul must wait for the digs that supply it), the structure stage corrects lift counts to the brief's own
entry, and a berm without a stated base is judged on its least base. Same matrix as runs 1 and 2 (22 briefs x Ultra / Super x
loop on x 3), 5 workers. Run 2 -> run 3, Ultra: verified 85 % -> 89 %, verified in scope 74 % -> 76 %, first try 62 % -> 50 %
(the supply check now rejects first graphs that haul before the dig; the repair loop recovers them, 50 % -> 89 %). Super: 77 %
-> 85 % verified, 58 % -> 67 % in scope. 325 requests, 0 errors, 7 answers cut at the token limit, $8.23.
eval.rescored.csv rescores every row with the checks of main at commit df5d79d (backend/scripts/rescore_eval.py): the lift
fix ("a compacted 10 cm bedding layer" is one lift), square footprints Lightning recorded as diameters (habitat_foundation,
instrument_pad, habitat_berm_tight_horizon dug 113 of 144 m3) and the double-count conflict. Ultra: verified in scope 74 %,
71 % with no supply or double-count conflict (run 2 under the same checks: 48 %); 0 of 59 verified Ultra plans haul before
their material is dug (run 2: 17 of 56). Super: 61 % and 59 %. Known after the run: Lightning misses a minimum the brief states
outside the structure's entry (low_battery_pad's "any diameter between 12 m and 20 m"), so its re-scoped plans read out of
scope. Every run's PlanResult is under plans/ (plan_path in eval.csv is relative to this folder); calls.md is the Token
Factory call log summary. -->

# Moonwright eval: 22 brief(s) x 2 condition(s) x 3 repeat(s)

Generated 2026-10-23T11:32:52+00:00; llm `nebius`; briefs from `examples/eval`; 132 run(s) on 5 worker(s); max_repairs 3; feedback `core`; max_answer_retries the planner's own; thinking `auto`.

## How to read

A condition is `<arm>/<loop>`: the arm names the model that proposes the task graph and answers the repair prompts (`ultra` = `nvidia/Nemotron-3-Ultra-550b-a55b`, `super` = `nvidia/nemotron-3-super-120b-a12b`) and the loop is the repair-loop setting. `loop_on` allows up to 3 repair round(s), each fed the solver's minimal infeasibility core with its numeric hints; `loop_off` takes the first valid graph's verdict as final. Briefs are structured by `nvidia/Nemotron-3_5-Lightning`; explanations (only with `--explain`) are written by `nvidia/nemotron-3-super-120b-a12b`. Thinking `auto`: the task-graph and repair calls reason, structure and explain answer directly. Each brief is structured once per repeat and every condition plans from that same requirements object (`requirements_sha` in the rows), so arms and loop settings are a paired comparison on the same first-graph verdict rather than each sampling the structuring model anew; the shared structure step and its tokens appear in every row of the pair. `feasible` is the share of runs that ended with a CP-SAT-verified schedule, with a Wilson 95 % interval; `feasible in scope` the share that is feasible AND whose final graph plans every structure's volume (dug m3, hauled fill) and its compaction and sintering area (footprint x lifts) at 95 % or more of the smallest size the brief allows (the validator already rejects a volume under 70 %, so a plan that verified after trimming the work 15-25 % is feasible but not in scope; `min_volume_share` and `min_area_share` in the rows say how far off; an excess such as a berm hauled twice is counted in `volume_advisories`, not against the scope); `first try` the share verified without a repair round; `repairs` the mean repair rounds per run; `makespan` the mean over feasible runs; latency is wall time per run (p50 / p95, model calls and solver included); tokens are per run; cost is per run at the Token Factory list prices (`-` when the model is not priced). In the per-brief matrix a cell reads `ok <makespan> h / <repairs> rep` for a verified run (with `k/n` when repeated), `ok ... scope!` when it is verified but not in scope, and `fail <conflict families>` for a run that ended without a verified plan. `rejected answers` is the share of runs in which at least one task-graph or repair answer was rejected before any verdict (it failed the schema or the call failed: `schema` / `llm_error`, an answer cut off at max_tokens and re-asked among them; when the re-asks add up to more such answers than runs their number follows in parentheses) and `p50 cost` the median cost of a priced run, next to the mean in `cost / run`. Every run's PlanResult is kept under `plans/` as `<brief>__<arm>__<loop>__<repeat>.plan.json` (`plan_path` in the rows, relative to the CSV's directory, so a row resumed from a run that kept its plans elsewhere names its own file: the JSON `POST /plan` returns, so `moonwright simulate` and `scripts/replan_eval.py` read it) and each (brief, repeat) job's structuring as `<brief>__<repeat>.structure.json` (`<brief>__<repeat>.2.structure.json` for the second structuring of a job `--resume` completed; its `requirements_sha` pairs it with the rows). The per-kind table groups briefs by their designed kind (`--kinds`, or `kinds.json` next to the briefs; `docs/eval-briefs.md`): A comfortable, B battery-split, C horizon-bound, D impossible, E scope reduction allowed.

## Per condition

| condition | n | feasible (95% CI) | feasible in scope (95% CI) | first try | repairs | makespan | latency p50 / p95 | tokens in / out | cost / run | cost total | rejected answers | p50 cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ultra/loop_on` | 66 | 89% (80%-95%) | 76% (64%-84%) | 50% | 0.82 | 61 h | 73.7 s / 527.8 s | 12043 / 31820 | $0.1031 | $6.80 | 0/66 | $0.0654 |
| `super/loop_on` | 66 | 85% (74%-92%) | 67% (55%-77%) | 45% | 0.98 | 62 h | 117.8 s / 344.4 s | 12804 / 21451 | $0.0220 | $1.45 | 1/66 | $0.0172 |

## Per kind

| kind | condition | n | feasible | feasible in scope | first try | repairs | briefs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `A` | `ultra/loop_on` | 39 | 39/39 | 35/39 | 24/39 | 0.41 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `A` | `super/loop_on` | 39 | 39/39 | 30/39 | 23/39 | 0.51 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `B` | `ultra/loop_on` | 9 | 9/9 | 9/9 | 5/9 | 0.78 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `B` | `super/loop_on` | 9 | 8/9 | 7/9 | 4/9 | 1.33 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `D` | `ultra/loop_on` | 9 | 3/9 | 0/9 | 0/9 | 2.56 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `D` | `super/loop_on` | 9 | 1/9 | 1/9 | 0/9 | 2.78 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `C` | `ultra/loop_on` | 6 | 6/6 | 6/6 | 4/6 | 0.50 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `C` | `super/loop_on` | 6 | 6/6 | 6/6 | 3/6 | 0.50 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `E` | `ultra/loop_on` | 3 | 2/3 | 0/3 | 0/3 | 1.67 | `low_battery_pad` |
| `E` | `super/loop_on` | 3 | 2/3 | 0/3 | 0/3 | 1.67 | `low_battery_pad` |

## Per brief

| brief | ultra/loop_on | super/loop_on |
| --- | --- | --- |
| `access_road` | ok 3/3 53 h / 0 rep | ok 3/3 57 h / 0 rep scope! 1/3 |
| `cable_trench` | ok 3/3 18 h / 0.7 rep scope! 2/3 | ok 3/3 21 h / 0.7 rep scope! 2/3 |
| `crater_ramp` | ok 3/3 74 h / 0 rep | ok 3/3 91 h / 0.3 rep |
| `depleted_excavators` | ok 3/3 42 h / 0.7 rep | ok 2/3 42 h / 0 rep; fail 1/3 energy |
| `four_mast_pads` | ok 3/3 51 h / 0 rep | ok 3/3 50 h / 0 rep |
| `front_loaded_excavation` | fail 3/3 horizon,volume,zone | ok 1/3 60 h / 1 rep; fail 2/3 horizon_path |
| `habitat_berm_tight_horizon` | ok 3/3 61 h / 1 rep | ok 3/3 60 h / 1 rep |
| `habitat_foundation` | ok 3/3 41 h / 0 rep | ok 3/3 43 h / 0 rep |
| `instrument_pad` | ok 3/3 13 h / 0 rep scope! 2/3 | ok 3/3 13 h / 0 rep |
| `isru_stockpile` | ok 3/3 43 h / 0.3 rep | ok 3/3 29 h / 0.3 rep |
| `landing_pad` | ok 3/3 89 h / 1.3 rep | ok 3/3 92 h / 2.3 rep scope! 2/3 |
| `large_fleet_pad_berm_road` | ok 3/3 165 h / 1.7 rep | ok 3/3 180 h / 2.7 rep scope! 1/3 |
| `low_battery_pad` | ok 2/3 45 h / 1 rep scope! 2/2; fail 1/3 energy | ok 2/3 42 h / 1 rep scope! 2/2; fail 1/3 energy |
| `one_surveyor_three_structures` | ok 3/3 42 h / 0.7 rep | ok 3/3 58 h / 1 rep scope! 1/3 |
| `propellant_pit_and_berm` | ok 3/3 64 h / 0.7 rep | ok 3/3 58 h / 0 rep scope! 1/3 |
| `road_and_pad` | ok 3/3 105 h / 0 rep | ok 3/3 108 h / 0.3 rep |
| `shielding_mound` | ok 3/3 46 h / 0.7 rep | ok 3/3 46 h / 0.3 rep |
| `small_fleet_long_berm` | ok 1/3 126 h / 1 rep scope!; fail 2/3 energy,volume | fail 3/3 cycle,energy,volume |
| `solar_array_pad` | ok 2/3 81 h / 2 rep scope! 2/2; fail 1/3 energy | fail 3/3 energy |
| `three_pads_in_order` | ok 3/3 38 h / 0 rep | ok 3/3 38 h / 0 rep |
| `tight_battery` | ok 3/3 68 h / 0.3 rep | ok 3/3 82 h / 0.7 rep |
| `two_pads_one_printer` | ok 3/3 59 h / 0.7 rep | ok 3/3 62 h / 1 rep scope! 2/3 |

## Side measurements on run 3's verified plans

Both use the first verified Ultra plan of each brief (21 plans) and the solver of commit 40bd686, whose travel model was a
per-task transit allowance (the longest leg a robot could drive), replaced by exact sequence-dependent travel afterwards.

* **Nemotron writes the schedule itself** (`direct-schedule/`, `backend/scripts/llm_schedule_check.py`, Nemotron 3 Ultra
  with reasoning, $0.87). Given the requirements and the verified task graph, 21 of 21 answers passed `check_schedule`, the
  same independent checker the pipeline applies to CP-SAT. 9 were shorter than the solver's "optimal" makespan (four_mast_pads
  43 h against 60 h), because the allowance charged every task the longest possible leg, even between tasks in the same zone.
  The language model's failures are in the decomposition, not in sequencing a verified graph: 50 % of Ultra's first task
  graphs in this run were refused (energy, supply order, horizon, scope).
* **Robot failures replanned by CP-SAT alone** (`replan/`, `backend/scripts/replan_eval.py --llm fixture --sole-of-type
  skip`). Every robot that owns scheduled work and has a peer of its type was failed at 10 % and 50 % of its first task: 110
  injections. CP-SAT rescheduled the remaining work in 67 of them (61 %), with a median solve of 3 ms and no model call; in
  the other 43 the surviving robots provably could not carry the work (energy), which is where Moonwright asks Nemotron to
  re-scope.
