<!-- Run 2 of the live evaluation (Oct 23 2026, 12:14-15:59 KST), the first after phase 3e (commit b77a942):
volume:<structure> is a hard conflict below 70 % of the brief's dimensions, reasoning calls start at 32,768 tokens
(doubling to 65,536 when cut), validator energy charges transit, and "in scope" needs 95 % of every judged volume and
compaction / sintering area. Same matrix as run 1 (2026-10-23-live-eval-run1.md): 22 briefs x Ultra / Super x loop on x 3.
Run 1 -> run 2, Ultra: verified 85 % -> 85 %, verified in scope 45 % -> 74 %, cut answers 78 of 190 Ultra calls -> 4 of 121,
rejected answers -> 0 of 66. Super: 91 % -> 77 % verified (it no longer shrinks impossible briefs to pass), 38 % -> 58 % in scope.
Known after the run (fixed later, see the README): Lightning sometimes records a brief's "four 10 m segments" as 4 compaction
lifts, which marks correct crater_ramp plans out of scope; and CP-SAT only sees the precedence edges it is given, so nothing
checked that a haul waits for its material. Rescored with the phase-3g supply check (eval.rescored.csv, made by
backend/scripts/rescore_eval.py), 17 of the 56 verified Ultra plans and 14 of the 51 verified Super plans let a haul start
before enough material was dug. Every run's PlanResult is
under plans/ (plan_path in eval.csv is relative to this folder); calls.md is the Token Factory call log summary. -->

# Moonwright eval: 22 brief(s) x 2 condition(s) x 3 repeat(s)

Generated 2026-10-23T06:59:27+00:00; llm `nebius`; briefs from `examples/eval`; 132 run(s) on 3 worker(s); max_repairs 3; feedback `core`; max_answer_retries the planner's own; thinking `auto`.

## How to read

A condition is `<arm>/<loop>`: the arm names the model that proposes the task graph and answers the repair prompts (`ultra` = `nvidia/Nemotron-3-Ultra-550b-a55b`, `super` = `nvidia/nemotron-3-super-120b-a12b`) and the loop is the repair-loop setting. `loop_on` allows up to 3 repair round(s), each fed the solver's minimal infeasibility core with its numeric hints; `loop_off` takes the first valid graph's verdict as final. Briefs are structured by `nvidia/Nemotron-3_5-Lightning`; explanations (only with `--explain`) are written by `nvidia/nemotron-3-super-120b-a12b`. Thinking `auto`: the task-graph and repair calls reason, structure and explain answer directly. Each brief is structured once per repeat and every condition plans from that same requirements object (`requirements_sha` in the rows), so arms and loop settings are a paired comparison on the same first-graph verdict rather than each sampling the structuring model anew; the shared structure step and its tokens appear in every row of the pair. `feasible` is the share of runs that ended with a CP-SAT-verified schedule, with a Wilson 95 % interval; `feasible in scope` the share that is feasible AND whose final graph plans every structure's volume (dug m3, hauled fill) and its compaction and sintering area (footprint x lifts) at 95 % or more of the smallest size the brief allows (the validator already rejects a volume under 70 %, so a plan that verified after trimming the work 15-25 % is feasible but not in scope; `min_volume_share` and `min_area_share` in the rows say how far off; an excess such as a berm hauled twice is counted in `volume_advisories`, not against the scope); `first try` the share verified without a repair round; `repairs` the mean repair rounds per run; `makespan` the mean over feasible runs; latency is wall time per run (p50 / p95, model calls and solver included); tokens are per run; cost is per run at the Token Factory list prices (`-` when the model is not priced). In the per-brief matrix a cell reads `ok <makespan> h / <repairs> rep` for a verified run (with `k/n` when repeated), `ok ... scope!` when it is verified but not in scope, and `fail <conflict families>` for a run that ended without a verified plan. `rejected answers` is the share of runs in which at least one task-graph or repair answer was rejected before any verdict (it failed the schema or the call failed: `schema` / `llm_error`, an answer cut off at max_tokens and re-asked among them; when the re-asks add up to more such answers than runs their number follows in parentheses) and `p50 cost` the median cost of a priced run, next to the mean in `cost / run`. Every run's PlanResult is kept under `/Users/kangmin/Documents/hackathon/Nebius AI builder/backend/out/eval_run2/plans` as `<brief>__<arm>__<loop>__<repeat>.plan.json` (`plan_path` in the rows, relative to the CSV's directory, so a row resumed from a run that kept its plans elsewhere names its own file: the JSON `POST /plan` returns, so `moonwright simulate` and `scripts/replan_eval.py` read it) and each (brief, repeat) job's structuring as `<brief>__<repeat>.structure.json` (`<brief>__<repeat>.2.structure.json` for the second structuring of a job `--resume` completed; its `requirements_sha` pairs it with the rows). The per-kind table groups briefs by their designed kind (`--kinds`, or `kinds.json` next to the briefs; `docs/eval-briefs.md`): A comfortable, B battery-split, C horizon-bound, D impossible, E scope reduction allowed.

## Per condition

| condition | n | feasible (95% CI) | feasible in scope (95% CI) | first try | repairs | makespan | latency p50 / p95 | tokens in / out | cost / run | cost total | rejected answers | p50 cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ultra/loop_on` | 66 | 85% (74%-92%) | 74% (63%-83%) | 62% | 0.77 | 59 h | 54.7 s / 468.7 s | 11592 / 28286 | $0.0921 | $6.08 | 0/66 | $0.0489 |
| `super/loop_on` | 66 | 77% (66%-86%) | 58% (46%-69%) | 45% | 1.14 | 62 h | 96.9 s / 381.1 s | 13165 / 19289 | $0.0202 | $1.33 | 0/66 | $0.0162 |

## Per kind

| kind | condition | n | feasible | feasible in scope | first try | repairs | briefs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `A` | `ultra/loop_on` | 39 | 39/39 | 35/39 | 31/39 | 0.26 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `A` | `super/loop_on` | 39 | 37/39 | 25/39 | 22/39 | 0.64 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `B` | `ultra/loop_on` | 9 | 9/9 | 9/9 | 6/9 | 0.67 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `B` | `super/loop_on` | 9 | 9/9 | 8/9 | 4/9 | 1.11 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `D` | `ultra/loop_on` | 9 | 2/9 | 0/9 | 0/9 | 2.67 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `D` | `super/loop_on` | 9 | 0/9 | 0/9 | 0/9 | 3.00 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `C` | `ultra/loop_on` | 6 | 5/6 | 5/6 | 4/6 | 0.67 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `C` | `super/loop_on` | 6 | 5/6 | 5/6 | 4/6 | 0.67 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `E` | `ultra/loop_on` | 3 | 1/3 | 0/3 | 0/3 | 2.33 | `low_battery_pad` |
| `E` | `super/loop_on` | 3 | 0/3 | 0/3 | 0/3 | 3.00 | `low_battery_pad` |

## Per brief

| brief | ultra/loop_on | super/loop_on |
| --- | --- | --- |
| `access_road` | ok 3/3 55 h / 0 rep | ok 3/3 57 h / 0.7 rep scope! 3/3 |
| `cable_trench` | ok 3/3 19 h / 0 rep | ok 3/3 15 h / 0 rep |
| `crater_ramp` | ok 3/3 69 h / 0.7 rep scope! 2/3 | ok 3/3 82 h / 2 rep scope! 2/3 |
| `depleted_excavators` | ok 3/3 41 h / 0 rep | ok 3/3 36 h / 1 rep |
| `four_mast_pads` | ok 3/3 50 h / 0 rep | ok 3/3 54 h / 0 rep |
| `front_loaded_excavation` | fail 3/3 horizon,horizon_path,zone | fail 3/3 cycle,horizon,volume,zone |
| `habitat_berm_tight_horizon` | ok 3/3 67 h / 0.3 rep | ok 3/3 58 h / 0.3 rep |
| `habitat_foundation` | ok 3/3 39 h / 0 rep | ok 3/3 41 h / 0 rep |
| `instrument_pad` | ok 3/3 17 h / 0 rep | ok 3/3 17 h / 0.3 rep |
| `isru_stockpile` | ok 3/3 59 h / 0 rep | ok 3/3 53 h / 0 rep |
| `landing_pad` | ok 3/3 82 h / 1.7 rep | ok 1/3 80 h / 2 rep scope!; fail 2/3 energy |
| `large_fleet_pad_berm_road` | ok 3/3 149 h / 1.7 rep | ok 3/3 169 h / 1.7 rep scope! 1/3 |
| `low_battery_pad` | ok 1/3 39 h / 1 rep scope!; fail 2/3 energy | fail 3/3 energy,energy_task,volume |
| `one_surveyor_three_structures` | ok 3/3 39 h / 0 rep scope! 1/3 | ok 3/3 38 h / 0.3 rep scope! 3/3 |
| `propellant_pit_and_berm` | ok 3/3 44 h / 0 rep | ok 3/3 49 h / 0.3 rep |
| `road_and_pad` | ok 3/3 111 h / 0.3 rep | ok 3/3 126 h / 0.7 rep |
| `shielding_mound` | ok 3/3 39 h / 0.3 rep | ok 3/3 50 h / 0.7 rep |
| `small_fleet_long_berm` | ok 1/3 75 h / 1 rep scope!; fail 2/3 energy | fail 3/3 energy,energy_task |
| `solar_array_pad` | ok 1/3 97 h / 2 rep scope!; fail 2/3 energy | fail 3/3 energy |
| `three_pads_in_order` | ok 2/3 40 h / 0 rep; fail 1/3 horizon,precedence,robot | ok 2/3 40 h / 0 rep; fail 1/3 horizon,precedence,robot |
| `tight_battery` | ok 3/3 70 h / 0.3 rep | ok 3/3 86 h / 0.7 rep |
| `two_pads_one_printer` | ok 3/3 56 h / 0.3 rep scope! 1/3 | ok 3/3 78 h / 0.7 rep scope! 3/3 |
