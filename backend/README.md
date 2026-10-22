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
    uv run moonwright simulate out/landing_pad.plan.json --fail EX2@30 --llm fixture
    uv run moonwright montecarlo out/landing_pad.plan.json --runs 50 --seed 1 --out out/landing_pad.mc.json
    uv run moonwright eval --briefs examples/briefs --llm fixture --out out/eval.json --csv out/eval.csv
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

## Evaluation flags

    uv run moonwright eval --briefs examples/eval --repeat 3 --loop both --llm nebius --workers 4 \
      --out out/eval_live.json --csv out/eval_live.csv
    uv run moonwright eval --briefs examples/eval --repeat 2 --loop on --feedback verdict --llm nebius
    uv run python scripts/llm_schedule_check.py out/landing_pad.plan.json --repeat 3 --out out/direct.json

`--workers N` runs brief x condition x repeat on a thread pool and flushes each row to the CSV as it
completes (the JSON report keeps brief x condition x repeat order); `--feedback core|verdict` is the
ablation (the repair round gets the minimal infeasibility core with hints, or only the verdict kind);
`--max-answer-retries 0..5` bounds schema / transport re-asks per round in both conditions
(`--max-repairs` counts verdict rounds only). Rows record the models used, `answer_valid`,
`first_try_feasible`, first/final duration and energy totals and `scope_drift_pct`; the summary adds a
Wilson 95 % CI on the feasible rate. `llm_schedule_check.py` asks the model for a schedule directly,
recomputes the makespan from the entries and counts the violations the checker finds (`--models`
picks the arm, `--csv` writes the rows).

## Environment

`NEBIUS_API_KEY`, `NEBIUS_BASE_URL` (the us-central1 endpoint is tried automatically on a 404),
`MOONWRIGHT_MODEL_STRUCTURE|TASKGRAPH|REPAIR|EXPLAIN`, `MOONWRIGHT_THINKING` (`auto` | `off`),
`MOONWRIGHT_MAX_TOKENS`, `MOONWRIGHT_THINKING_MAX_TOKENS`, `MOONWRIGHT_LLM_TIMEOUT_S`,
`MOONWRIGHT_LLM`, `MOONWRIGHT_LLM_LOG` (one JSON line per Token Factory call in
`out/llm_log.jsonl`: status, model, tokens, latency, request id; never prompt or answer text; `off`
disables it), `MOONWRIGHT_CORS_ORIGINS` / `MOONWRIGHT_CORS_ORIGIN_REGEX` for a deployed UI. All are
documented in `.env.example`.
