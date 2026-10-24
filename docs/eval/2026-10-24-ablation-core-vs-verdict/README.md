<!-- Ablation: what the minimal core is worth (Oct 24 2026, 19:59-20:39 KST). The same code and matrix as live run 4
(commit 3466621, 22 briefs x 3 repeats, Nemotron 3 Ultra for the task graph and the repairs, loop on, reasoning auto), with
one change: --feedback verdict. Each repair round tells the model only that its graph was refused (the verdict's kind), with
no conflict ids, no numeric hint and no details; run 4's Ultra arm (--feedback core) is the comparison. 208 requests, 0
errors, 5 answers cut at the token limit (4 re-asks), $7.56. Both arms are scored with the final checks (each folder's
eval.rescored.csv, commit a8f9419).

| Nemotron 3 Ultra, 66 live runs each | core (run 4) | verdict only (this run) |
| --- | --- | --- |
| first graph verified (no repair) | 36 (55 %) | 38 (58 %) |
| refused first graph, then verified by the repairs | 21 of 30 (70 %, 95 % CI 52-83 %) | 9 of 28 (32 %, 18-51 %) |
| ... and in scope | 19 of 30 (63 %) | 9 of 28 (32 %) |
| verified | 57 (86 %) | 47 (71 %) |
| verified in scope | 53 (80 %) | 47 (71 %) |
| designed-impossible briefs verified (all by trimming work) | 2 of 9 | 0 of 9 |
| cost | $6.43 | $7.56 |

The first graphs are drawn from the same prompt, so first-try rates agree (55 % and 58 %). What differs is the repair: with
the solver's minimal core and its numeric hint (for example "printer tasks need at least 4,095 Wh, ... its 1 robot carries
4,000 Wh"), Ultra turned 70 % of the refused graphs into verified plans; told only "refused", 32 % (Fisher's exact test on
21/30 against 9/28: p = 0.008). The blind repairs also cost more per run (more rounds, longer answers). Without hints the
model never found the trims that let two designed-impossible runs verify in run 4; those trims are out of scope either way.
Every run's PlanResult is under plans/ (plan_path in eval.csv is relative to this folder); calls.md is the call log summary. -->

# Moonwright eval: 22 brief(s) x 1 condition(s) x 3 repeat(s)

Generated 2026-10-24T11:39:07+00:00; llm `nebius`; briefs from `examples/eval`; 66 run(s) on 5 worker(s); max_repairs 3; feedback `verdict`; max_answer_retries the planner's own; thinking `auto`.

## How to read

A condition is `<arm>/<loop>`: the arm names the model that proposes the task graph and answers the repair prompts (`ultra` = `nvidia/Nemotron-3-Ultra-550b-a55b`) and the loop is the repair-loop setting. `loop_on` allows up to 3 repair round(s), each fed only the verdict kind (the ablation arm); `loop_off` takes the first valid graph's verdict as final. Briefs are structured by `nvidia/Nemotron-3_5-Lightning`; explanations (only with `--explain`) are written by `nvidia/nemotron-3-super-120b-a12b`. Thinking `auto`: the task-graph and repair calls reason, structure and explain answer directly. Each brief is structured once per repeat and every condition plans from that same requirements object (`requirements_sha` in the rows), so arms and loop settings are a paired comparison on the same first-graph verdict rather than each sampling the structuring model anew; the shared structure step and its tokens appear in every row of the pair. `feasible` is the share of runs that ended with a CP-SAT-verified schedule, with a Wilson 95 % interval; `feasible in scope` the share that is feasible AND whose final graph plans every structure's volume (dug m3, hauled fill) and its compaction and sintering area (footprint x lifts) at 95 % or more of the smallest size the brief allows (the validator already rejects a volume under 70 %, so a plan that verified after trimming the work 15-25 % is feasible but not in scope; `min_volume_share` and `min_area_share` in the rows say how far off; an excess such as a berm hauled twice is counted in `volume_advisories`, not against the scope); `first try` the share verified without a repair round; `repairs` the mean repair rounds per run; `makespan` the mean over feasible runs; latency is wall time per run (p50 / p95, model calls and solver included); tokens are per run; cost is per run at the Token Factory list prices (`-` when the model is not priced). In the per-brief matrix a cell reads `ok <makespan> h / <repairs> rep` for a verified run (with `k/n` when repeated), `ok ... scope!` when it is verified but not in scope, and `fail <conflict families>` for a run that ended without a verified plan. `rejected answers` is the share of runs in which at least one task-graph or repair answer was rejected before any verdict (it failed the schema or the call failed: `schema` / `llm_error`, an answer cut off at max_tokens and re-asked among them; when the re-asks add up to more such answers than runs their number follows in parentheses) and `p50 cost` the median cost of a priced run, next to the mean in `cost / run`. Every run's PlanResult is kept under `plans/` as `<brief>__<arm>__<loop>__<repeat>.plan.json` (`plan_path` in the rows, relative to the CSV's directory, so a row resumed from a run that kept its plans elsewhere names its own file: the JSON `POST /plan` returns, so `moonwright simulate` and `scripts/replan_eval.py` read it) and each (brief, repeat) job's structuring as `<brief>__<repeat>.structure.json` (`<brief>__<repeat>.2.structure.json` for the second structuring of a job `--resume` completed; its `requirements_sha` pairs it with the rows). The per-kind table groups briefs by their designed kind (`--kinds`, or `kinds.json` next to the briefs; `docs/eval-briefs.md`): A comfortable, B battery-split, C horizon-bound, D impossible, E scope reduction allowed.

## Per condition

| condition | n | feasible (95% CI) | feasible in scope (95% CI) | first try | repairs | makespan | latency p50 / p95 | tokens in / out | cost / run | cost total | rejected answers | p50 cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ultra/loop_on` | 66 | 71% (59%-81%) | 71% (59%-81%) | 58% | 1.09 | 49 h | 67.7 s / 565.7 s | 13318 / 35197 | $0.1145 | $7.56 | 0/66 | $0.0480 |

## Per kind

| kind | condition | n | feasible | feasible in scope | first try | repairs | briefs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `A` | `ultra/loop_on` | 39 | 33/39 | 33/39 | 24/39 | 0.85 | `access_road`, `cable_trench`, `crater_ramp`, `four_mast_pads`, `habitat_foundation`, `instrument_pad`, `isru_stockpile`, `landing_pad`, `one_surveyor_three_structures`, `propellant_pit_and_berm`, `shielding_mound`, `tight_battery`, `two_pads_one_printer` |
| `B` | `ultra/loop_on` | 9 | 5/9 | 5/9 | 5/9 | 1.33 | `depleted_excavators`, `large_fleet_pad_berm_road`, `road_and_pad` |
| `D` | `ultra/loop_on` | 9 | 0/9 | 0/9 | 0/9 | 3.00 | `front_loaded_excavation`, `small_fleet_long_berm`, `solar_array_pad` |
| `C` | `ultra/loop_on` | 6 | 6/6 | 6/6 | 6/6 | 0.00 | `habitat_berm_tight_horizon`, `three_pads_in_order` |
| `E` | `ultra/loop_on` | 3 | 3/3 | 3/3 | 3/3 | 0.00 | `low_battery_pad` |

## Per brief

| brief | ultra/loop_on |
| --- | --- |
| `access_road` | ok 3/3 50 h / 1.7 rep |
| `cable_trench` | ok 2/3 18 h / 1 rep; fail 1/3 supply |
| `crater_ramp` | ok 3/3 76 h / 0 rep |
| `depleted_excavators` | ok 2/3 35 h / 0 rep; fail 1/3 volume |
| `four_mast_pads` | ok 3/3 41 h / 0 rep |
| `front_loaded_excavation` | fail 3/3 horizon,precedence,zone |
| `habitat_berm_tight_horizon` | ok 3/3 69 h / 0 rep |
| `habitat_foundation` | ok 3/3 43 h / 0 rep |
| `instrument_pad` | ok 3/3 13 h / 0 rep |
| `isru_stockpile` | ok 3/3 39 h / 0.3 rep |
| `landing_pad` | fail 3/3 energy |
| `large_fleet_pad_berm_road` | fail 3/3 energy,supply |
| `low_battery_pad` | ok 3/3 29 h / 0 rep |
| `one_surveyor_three_structures` | ok 3/3 38 h / 0.3 rep |
| `propellant_pit_and_berm` | ok 3/3 73 h / 0.7 rep |
| `road_and_pad` | ok 3/3 100 h / 0 rep |
| `shielding_mound` | ok 2/3 40 h / 0.5 rep; fail 1/3 scope |
| `small_fleet_long_berm` | fail 3/3 energy,volume |
| `solar_array_pad` | fail 3/3 energy |
| `three_pads_in_order` | ok 3/3 29 h / 0 rep |
| `tight_battery` | ok 3/3 71 h / 1 rep |
| `two_pads_one_printer` | ok 2/3 57 h / 0 rep; fail 1/3 volume |
