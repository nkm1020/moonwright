<!-- Run 1 of the live evaluation (Oct 23 2026, 06:16-07:55 KST), recorded BEFORE phase 3e: volume was an advisory,
thinking max tokens 16384 (35 % of reasoning calls were cut and re-asked), validator energy without transit.
Kept verbatim as the "before" of the volume-conflict / token-policy changes; the rerun after phase 3e is the
"after". Rows: 2026-10-23-live-eval-run1.csv; call log summary: 2026-10-23-live-eval-run1-calls.md. -->

# Moonwright eval: 22 brief(s) x 2 condition(s) x 3 repeat(s)

Generated 2026-10-22T22:55:52+00:00; llm `nebius`; briefs from `examples/eval`; 132 run(s) on 3 worker(s); max_repairs 3; feedback `core`; max_answer_retries the planner's own; thinking `auto`.

## How to read

A condition is `<arm>/<loop>`: the arm names the model that proposes the task graph and answers the repair prompts (`ultra` = `nvidia/Nemotron-3-Ultra-550b-a55b`, `super` = `nvidia/nemotron-3-super-120b-a12b`) and the loop is the repair-loop setting. `loop_on` allows up to 3 repair round(s), each fed the solver's minimal infeasibility core with its numeric hints; `loop_off` takes the first valid graph's verdict as final. Briefs are structured by `nvidia/Nemotron-3_5-Lightning`; explanations (only with `--explain`) are written by `nvidia/nemotron-3-super-120b-a12b`. Thinking `auto`: the task-graph and repair calls reason, structure and explain answer directly. Each brief is structured once per repeat and every condition plans from that same requirements object (`requirements_sha` in the rows), so arms and loop settings are a paired comparison on the same first-graph verdict rather than each sampling the structuring model anew; the shared structure step and its tokens appear in every row of the pair. `feasible` is the share of runs that ended with a CP-SAT-verified schedule, with a Wilson 95 % interval; `feasible in scope` the share that is feasible AND whose final graph carries no `volume:<structure>` advisory (every structure's planned m3 within 70-150 % of what its dimensions imply; a plan that digs a third of the pad is feasible but not in scope, and `min_volume_share` in the rows says how far off); `first try` the share verified without a repair round; `repairs` the mean repair rounds per run; `makespan` the mean over feasible runs; latency is wall time per run (p50 / p95, model calls and solver included); tokens are per run; cost is per run at the Token Factory list prices (`-` when the model is not priced). In the per-brief matrix a cell reads `ok <makespan> h / <repairs> rep` for a verified run (with `k/n` when repeated), `ok ... scope!` when it is verified but not in scope, and `fail <conflict families>` for a run that ended without a verified plan. The per-kind table groups briefs by their designed kind (`--kinds`, or `kinds.json` next to the briefs; `docs/eval-briefs.md`): A comfortable, B battery-split, C horizon-bound, D impossible, E scope reduction allowed.

## Per condition

| condition | n | feasible (95% CI) | feasible in scope (95% CI) | first try | repairs | makespan | latency p50 / p95 | tokens in / out | cost / run | cost total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ultra/loop_on` | 66 | 85% (74%-92%) | 45% (34%-57%) | 70% | 0.62 | 56 h | 50.0 s / 430.6 s | 18370 / 34012 | $0.1137 | $7.50 |
| `super/loop_on` | 66 | 91% (82%-96%) | 38% (27%-50%) | 64% | 0.61 | 56 h | 83.2 s / 605.7 s | 14420 / 23527 | $0.0245 | $1.61 |

## Per kind

| kind | condition | n | feasible | feasible in scope | first try | repairs | briefs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `A` | `ultra/loop_on` | 39 | 37/39 | 21/39 | 33/39 | 0.26 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `A` | `super/loop_on` | 39 | 38/39 | 15/39 | 30/39 | 0.31 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `B` | `ultra/loop_on` | 9 | 5/9 | 3/9 | 4/9 | 1.33 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `B` | `super/loop_on` | 9 | 5/9 | 4/9 | 4/9 | 0.67 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `D` | `ultra/loop_on` | 9 | 5/9 | 2/9 | 3/9 | 1.67 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `D` | `super/loop_on` | 9 | 8/9 | 1/9 | 3/9 | 1.56 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `C` | `ultra/loop_on` | 6 | 6/6 | 3/6 | 6/6 | 0.00 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `C` | `super/loop_on` | 6 | 6/6 | 4/6 | 5/6 | 0.17 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `E` | `ultra/loop_on` | 3 | 3/3 | 1/3 | 0/3 | 1.33 | `low_battery_pad` |
| `E` | `super/loop_on` | 3 | 3/3 | 1/3 | 0/3 | 2.33 | `low_battery_pad` |

## Per brief

| brief | ultra/loop_on | super/loop_on |
| --- | --- | --- |
| `access_road` | ok 3/3 54 h / 0 rep | ok 3/3 55 h / 0 rep scope! 3/3 |
| `cable_trench` | ok 3/3 19 h / 0 rep | ok 3/3 17 h / 0 rep |
| `crater_ramp` | ok 3/3 73 h / 0.7 rep | ok 3/3 85 h / 1 rep |
| `depleted_excavators` | ok 3/3 43 h / 0 rep scope! 2/3 | ok 3/3 35 h / 0 rep scope! 1/3 |
| `four_mast_pads` | ok 3/3 39 h / 0 rep scope! 3/3 | ok 3/3 40 h / 0 rep scope! 2/3 |
| `front_loaded_excavation` | fail 3/3 energy,horizon,quantity,scope,zone | ok 3/3 51 h / 2.3 rep scope! 3/3 |
| `habitat_berm_tight_horizon` | ok 3/3 65 h / 0 rep scope! 3/3 | ok 3/3 65 h / 0.3 rep scope! 2/3 |
| `habitat_foundation` | ok 3/3 43 h / 0 rep | ok 3/3 47 h / 0.3 rep |
| `instrument_pad` | ok 3/3 12 h / 0 rep scope! 3/3 | ok 3/3 12 h / 0 rep scope! 3/3 |
| `isru_stockpile` | ok 3/3 48 h / 0 rep | ok 3/3 55 h / 0 rep |
| `landing_pad` | ok 1/3 87 h / 1 rep scope!; fail 2/3 energy | ok 2/3 92 h / 0.5 rep scope! 2/2; fail 1/3 energy |
| `large_fleet_pad_berm_road` | fail 3/3 energy,energy_task,schema,scope | fail 3/3 scope |
| `low_battery_pad` | ok 3/3 48 h / 1.3 rep scope! 2/3 | ok 3/3 49 h / 2.3 rep scope! 2/3 |
| `one_surveyor_three_structures` | ok 3/3 40 h / 0 rep scope! 1/3 | ok 3/3 48 h / 0.3 rep scope! 2/3 |
| `propellant_pit_and_berm` | ok 3/3 52 h / 0 rep scope! 1/3 | ok 3/3 54 h / 0 rep scope! 3/3 |
| `road_and_pad` | ok 2/3 131 h / 1.5 rep; fail 1/3 energy | ok 2/3 122 h / 1.5 rep; fail 1/3 llm_error |
| `shielding_mound` | ok 3/3 73 h / 0 rep scope! 2/3 | ok 3/3 77 h / 0.7 rep scope! 2/3 |
| `small_fleet_long_berm` | ok 3/3 100 h / 0 rep scope! 3/3 | ok 3/3 87 h / 0 rep scope! 3/3 |
| `solar_array_pad` | ok 2/3 92 h / 1.5 rep; fail 1/3 energy | ok 2/3 81 h / 2 rep scope! 1/2; fail 1/3 energy |
| `three_pads_in_order` | ok 3/3 37 h / 0 rep | ok 3/3 37 h / 0 rep |
| `tight_battery` | ok 3/3 73 h / 0.3 rep scope! 2/3 | ok 3/3 61 h / 0 rep scope! 3/3 |
| `two_pads_one_printer` | ok 3/3 56 h / 0 rep scope! 3/3 | ok 3/3 57 h / 0.3 rep scope! 3/3 |
