# Moonwright backend

Natural-language construction brief -> Nemotron (Nebius Token Factory) structures it and proposes a
task graph with quantities -> a deterministic rate model turns quantities into hours and Wh ->
validator + OR-Tools CP-SAT verify and schedule it (precedence, one task per robot, one robot per
zone, batteries with a transit allowance, horizon) -> infeasibility cores go back to Nemotron for
repair -> the verified schedule runs in a 2D simulator that reschedules with the solver on robot
failure and escalates to Nemotron only when the remaining fleet provably cannot finish.

See the repository root README for the full story and the limitations. Quick start:

    uv sync
    uv run moonwright plan examples/briefs/landing_pad.md --llm fixture --out out/landing_pad.plan.json
    uv run moonwright plan examples/briefs/tight_battery.md --llm fixture --max-repairs 0   # loop off: the solver's advice
    uv run moonwright simulate out/landing_pad.plan.json --fail EX2@30 --llm fixture
    uv run moonwright montecarlo out/landing_pad.plan.json --runs 200 --seed 1 --out out/landing_pad.mc.json
    uv run moonwright eval --briefs examples/briefs --llm fixture --out out/eval.json --csv out/eval.csv
    uv run moonwright eval --briefs examples/briefs --llm fixture --models ultra,super --markdown out/eval.md
    uv run moonwright eval --briefs examples/eval --llm fixture --loop off --markdown out/eval_kinds.md   # per-kind table from examples/eval/kinds.json
    uv run python scripts/brief_budget.py fixtures/landing_pad/structure.json --graphs lazy
    uv run python scripts/replan_eval.py out/landing_pad.plan.json --llm fixture --all-tasks --sole-of-type solver
    uv run python scripts/llm_log_report.py out/llm_log.jsonl
    uv run moonwright serve                      # GET /health /examples; POST /structure /plan /plan/stream /simulate /montecarlo
    uv run pytest && uv run ruff check .

Commands: `plan` (brief -> verified schedule), `simulate` (replay with failures and replanning),
`montecarlo` (N replays with random failures and duration jitter; solver-only replans unless
`--llm` is given), `eval` (repair loop off/on over a directory of briefs), `serve` (FastAPI).
Every command answers `--help`; `--llm fixture|nebius|record` picks the model client (default:
`MOONWRIGHT_LLM`, else `nebius` when `NEBIUS_API_KEY` is set, else `fixture`).

## Pipeline notes (phase 3a)

* **Rate model** (`moonwright/pipeline/rates.py`). The task-graph prompt asks for `quantity`,
  `unit` (`m3 | m2 | m | ea`) and `basis` per task; `apply_rates` computes `duration_h =
  max(1, ceil(quantity / rate))` and `energy_wh = quantity x Wh/unit` from a six-row table
  (excavator m3 and m2, hauler m3, compactor m2, printer m2, surveyor m2; illustrative lunar-rated
  low-power units, every row an assumption with its note) and keeps the model's own figures in
  `estimated_duration_h` / `estimated_energy_wh`. Tasks without a quantity keep the model's hours
  with a base-power energy floor; a graph with no quantity at all (the hand-made fixtures, a
  solver-only replan) is returned unchanged. A gap of more than 50 % between the estimate and the
  computed figure is an `estimate:<task>` advisory in the step payload (a warning, never a conflict).
* **Transit allowance** (`moonwright/pipeline/schedule.py`, `moonwright/geometry.py`). For every
  (task, robot) the solver reserves `T = ceil(max distance to the task's zone over the robot's start
  and every zone it can be sent to / speed)` whole hours before the task and `0.5 Wh` per cell in the
  robot's energy sum; the first task cannot start before the first leg. Group names are unchanged, so
  cores and repair hints read as before; `check_schedule` re-verifies the actual legs of every robot's
  sequence. A disturbance-free replay of a verified plan fires no `task_overrun` event.
* **Structure coverage** (`moonwright/pipeline/validate.py`). `scope:<structure>` when a landing pad,
  berm, road or foundation lacks a required phase for which the fleet has the robot type, when a
  structure has no task, or when a repair dropped every task of a structure (fresh plans and their
  repairs only; replans are guarded by the scope check instead).
* **Solver-first replanning** (`moonwright/sim/replan.py`, `moonwright/pipeline/loop.py`). After an
  event the tasks underway are locked and `plan_from_requirements(..., start_with_prior=True,
  robot_positions=<frame>)` verifies the remaining graph without a model call; only a Failure opens
  the repair loop (`ReplanRecord.escalated`). Repair rounds of a replan may re-type, re-zone or
  re-scope work (shown in the repair diff) but a task underway may not change (`locked:<task>`).
  The closing `replan` step of the trace says what happened in one sentence.
* **Trace payloads**. `schedule` steps carry a verification receipt (constraint counts, battery use
  per robot, `travel_allowance`, the independent re-check); `repair` steps a diff with
  `scope_drift_pct`; `taskgraph` / `repair` steps the rate-model advisories; replan traces an
  `offset_h` / `time_base` per step.
* **Monte Carlo** (`moonwright/sim/montecarlo.py`). Every sample has an `outcome` (`completed`,
  `late`, `lost_only:<type>`, `replan_infeasible:<kind>`, `stuck`); the result carries
  `within_horizon_ci` (Wilson 95 %), `outcomes` counts and `single_points_of_failure`.

## Pipeline notes (phase 3c)

* **Brief-declared operations** (`schemas.Structure.operations`, `pipeline/validate.py`). The
  structure prompt asks Lightning for the phases the brief explicitly asks for per structure
  (closed vocabulary `survey | excavate | grade | haul | compact | sinter`); the coverage check
  requires, on top of the structure-type phases, a task of the matching robot type for each one
  (`performs`), so a graph that drops the berm's sintered crest or the key trench is a
  `scope:<structure>` conflict whose hint quotes the brief line (`brief_evidence`: the phase's own
  verbs, never a noun that names another structure or a robot, and never another structure's
  entry). An operation no robot type in the fleet can perform is a `structure` step warning
  (`unstaffed_operations`), never a conflict; so is one alternative of a kind group the brief
  does not support while it supports the other (`unsupported_operations`: `excavate` on a berm
  "built from a borrow strip ... placed and compacted"), so a structuring over-declaration never
  forces invented work. The pad kind accepts excavate OR grade (a stripped instrument pad is
  graded in m2), a hauler task placed in a structure's zone counts as its haul whatever its tag
  (`tags.hauls_into`), and `tags.py` holds the tag resolution shared with the volume advisories.
  The task-graph and repair prompts share `COVERAGE_RULES` (phases per kind, listed operations,
  a haul belongs to the receiving structure and its placement zone, inspection last, spoil
  reuse, the split rule whose parts SUM to the structure's total) so the model and the validator
  read the same contract; the unit rule prices what the rate table prices (trenches in m3, m and
  ea for surveyor work only).
* **Structure-stage guards** (`pipeline/structure.py`). `recover_operations` salvages operations
  that strict JSON-schema decoding scrambled into extra keys (`"operations': [": "excavate",
  "compact": true` and `"operations excavate": [...]`, both seen live from Lightning) and reports
  it in the step's warnings; `dimension_warnings` flags a structure without a numeric dimension
  (the task-graph model never sees the brief, so a missing dimension becomes an invented
  quantity). The structure prompt gives worked examples of the dimension fields, and the
  `Structure` docstring is short because pydantic copies it into the schema the model decodes
  against.
* **Volume advisories** (`pipeline/rates.py: volume_advisories`, `dims_volumes`). For a structure
  whose dimensions form a volume (bowl, box, berm prism, plus a berm's key trench from
  `trench_width_m` x `trench_depth_m` as a second dug shape; a shape with no positive volume,
  `depth_m: 0`, is simply none) the m3 its excavator (dug) or hauler (placed) tasks plan are
  compared with that volume; below 70 % or above 150 % the taskgraph / repair step carries
  `volume:<structure>: ...` next to the `estimate:<task>` advisories (a warning, never a
  conflict: a brief may allow a smaller scope, but the re-scope must be visible). The structure's
  tasks are resolved exactly as the coverage check resolves them (`tags.serving_tasks`: id, type,
  label, zone, hauls placed in the zone), only whole plans are judged (a replan's remaining graph
  is the work left, not the structure), and `moonwright plan` prints the advisories under its
  verdict line; `volume_shares` gives the eval its `min_volume_share`.
* **Advice on failure** (`pipeline/relax.py`, `schemas.Suggestion`, `PlanResult.suggestions`).
  `suggest_relaxations(req, graph, failure, opts)` derives candidates from the verdict's conflict
  ids (extend_horizon on horizon / timing cores, raise_battery for `energy:<robot>` and the
  validator's `energy:<type>` / `energy_task:<task>`, add_robot for the blamed types or a type the
  fleet lacks) and verifies each with a real CP-SAT solve on a modified copy of the requirements
  (at most 2 s per solve, one worker, 6 s per call, no solve started with less than 0.1 s left;
  `Planner(suggest_budget_s=...)`, 0 disables). A `raise_battery` figure is the smallest battery
  on the 50 Wh grid that CP-SAT still schedules (binary search between the battery the robot has
  and what the lifted solve charged it; a search cut short reports "is enough", never "needs").
  Three outcomes per solve are kept apart: proven, refuted, and timed out (UNKNOWN), the last
  never counted as tried; `reduce_scope` (unverified) names the largest work items only when
  every single change was refuted. The planner attaches the list as plain dicts to the final
  failed step's payload before the step is emitted (so `/plan/stream` clients see it with the
  step) and as models on `PlanResult.suggestions`; a replan gets `extend_horizon` / `add_robot`
  only, a spare never takes a lost robot's id (`reserved_ids` from `sim/replan.py`), and
  `sim/replan.py` rebases the advice (`relax.rebase_suggestion`) to absolute simulator hours
  against the brief's deadline (shifted by `ceil(t_h)`) with a full battery for the spare, in
  `ReplanRecord.after.suggestions` and in the failed step's payload (`suggestions_time_base:
  "absolute"`). Monte Carlo planners run with the advice off. `moonwright plan` prints `what
  would make it feasible:` lines; `simulate` prints them under a failed replan.
* **Evaluation tooling** (`cli.py eval`, `llm/prices.py`, `scripts/`). `--models ARM[,ARM]`
  (ultra, super, nano, lightning or `name=model-id`) gives each arm its own client for the
  task-graph and repair roles and names the conditions `<arm>/<loop>`; `--thinking on|off|auto`
  switches reasoning for those two roles; `--markdown PATH` writes the results document. Rows
  gain `cost_usd` (list prices in `llm/prices.py`, 0 offline, null for an unpriced model),
  `volume_advisories` / `min_volume_share` / `in_scope` (verified with no volume advisory: the
  "feasible in scope" rate next to "feasible"), `requirements_sha` and `kind`, and summaries
  p50/p95 latency, cost and the in-scope rate with its Wilson CI. Each brief is structured ONCE
  per repeat (`Planner.structure`) and every condition plans from the same requirements, so arms
  and loop settings are paired on one first-graph verdict; `--kinds PATH` (default: `kinds.json`
  next to the briefs, shipped for `examples/eval`) adds a per-kind table to the markdown.
  `scripts/replan_eval.py` injects robot failures into verified plans and classifies the recovery
  (solver-only, escalated verified / failed / no answer, not asked: budget or sole-of-type); its
  headline rates are over the *askable* injections and the not-asked ones have their own
  column, so the categories visibly add up; `scripts/llm_log_report.py` summarises the call log
  per model and role (calls, errors, re-asks, json_format rung, thinking, tokens, tok/s, p50/p95,
  cost, notable requests with ids); `scripts/brief_budget.py` prices a brief, a structure fixture
  or a plan with the rate table and runs canonical graphs through validator + CP-SAT
  (`docs/eval-briefs.md` documents the 22 briefs by kind).

## Evaluation flags

    uv run moonwright eval --briefs examples/eval --repeat 3 --loop both --models ultra,super --thinking auto \
      --llm nebius --workers 3 --max-answer-retries 2 \
      --out out/eval_live.json --csv out/eval_live.csv --markdown out/eval_live.md
    uv run moonwright eval --briefs examples/eval --repeat 2 --loop on --feedback verdict --llm nebius
    uv run python scripts/replan_eval.py out/live/*.plan.json --llm nebius --all-tasks --sole-of-type solver \
      --max-escalations 10 --out out/replan_eval_live.json --markdown out/replan_eval_live.md
    uv run python scripts/llm_log_report.py out/llm_log.jsonl --since <ISO start> \
      --markdown out/llm_log_report.md --out out/llm_log_report.json
    uv run python scripts/brief_budget.py --all examples/eval --graphs lazy,split --markdown
    uv run python scripts/llm_schedule_check.py out/landing_pad.plan.json --repeat 3 --out out/direct.json

`--workers N` runs brief x repeat jobs on a thread pool (a job structures its brief once and runs
every condition from that requirements object) and flushes each row to the CSV as it completes (the
JSON report keeps brief x condition x repeat order); `--feedback core|verdict` is the ablation (the
repair round gets the minimal infeasibility core with hints, or only the verdict kind);
`--max-answer-retries 0..5` bounds schema / transport re-asks per round in both conditions
(`--max-repairs` counts verdict rounds only). Rows record the models used, `answer_valid`,
`first_try_feasible`, first/final duration and energy totals, `scope_drift_pct`, the volume
advisories on the final graph (`volume_advisories`, `min_volume_share`, `in_scope`), the
`requirements_sha` shared by a brief's conditions and the brief's `kind`; the summary adds a Wilson
95 % CI on the feasible and the in-scope rate. `--models` runs one client per arm (shared by the workers)
and keeps the bare `loop_off` / `loop_on` names when absent, so older reports still parse;
`--thinking` records the setting in the report; the markdown explains the models per role, the
thinking setting and every column. `replan_eval.py` fails every robot with work at 10 % and 50 % of
its first task (`--fractions`, `--all-tasks` for every task, `--robots` to filter), runs the
simulator with solver-first replanning and stops asking the model after `--max-escalations`
(`--sole-of-type ask|solver|skip` decides what to do when the only robot of a type is lost; record
mode writes one `fixtures/<stem>/taskgraph_replan_2.json` per plan). `llm_log_report.py` reads the
call log (`--since ISO`, `--slow S`). `llm_schedule_check.py` asks the model for a schedule
directly, recomputes the makespan from the entries and counts the violations the checker finds
(`--models` picks the arm, `--csv` writes the rows).

## Environment

`NEBIUS_API_KEY`, `NEBIUS_BASE_URL` (the us-central1 endpoint is tried automatically on a 404),
`MOONWRIGHT_MODEL_STRUCTURE|TASKGRAPH|REPAIR|EXPLAIN`, `MOONWRIGHT_THINKING` (`auto` | `off`),
`MOONWRIGHT_MAX_TOKENS`, `MOONWRIGHT_THINKING_MAX_TOKENS`, `MOONWRIGHT_LLM_TIMEOUT_S`,
`MOONWRIGHT_LLM`, `MOONWRIGHT_LLM_LOG` (one JSON line per Token Factory call in
`out/llm_log.jsonl`: status, model, tokens, latency, request id; never prompt or answer text; `off`
disables it), `MOONWRIGHT_CORS_ORIGINS` / `MOONWRIGHT_CORS_ORIGIN_REGEX` for a deployed UI. All are
documented in `.env.example`.
