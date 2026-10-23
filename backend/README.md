# Moonwright backend

Natural-language construction brief -> Nemotron (Nebius Token Factory) structures it and proposes a
task graph with quantities -> a deterministic rate model turns quantities into hours and Wh ->
validator + OR-Tools CP-SAT verify and schedule it (precedence, one task per robot, one robot per
zone, batteries with a transit allowance, horizon) -> infeasibility cores go back to Nemotron for
repair -> the verified schedule runs in a 2D simulator that reschedules with the solver on robot
failure and escalates to Nemotron only when the remaining fleet provably cannot finish.

See the repository root README for the full story and the limitations. Quick start (the demo
briefs `landing_pad` and `front_loaded` replay live runs recorded on Oct 23; `landing_pad_v1`,
`access_road` and `tight_battery` are the hand-made fixtures of Oct 22, whose arithmetic the
tests check):

    uv sync
    uv run moonwright plan examples/briefs/landing_pad.md --llm fixture --out out/landing_pad.plan.json
    uv run moonwright plan examples/briefs/front_loaded.md --llm fixture    # no plan after 3 repairs: the advice
    uv run moonwright plan examples/briefs/tight_battery.md --llm fixture --max-repairs 0   # loop off: the solver's advice
    uv run moonwright simulate out/landing_pad.plan.json --fail EX2@10 --llm fixture
    uv run moonwright montecarlo out/landing_pad.plan.json --runs 200 --seed 1 --out out/landing_pad.mc.json
    uv run moonwright eval --briefs examples/briefs --llm fixture --out out/eval.json --csv out/eval.csv --save-plans out/plans
    uv run moonwright eval --briefs examples/briefs --llm fixture --models ultra,super --markdown out/eval.md
    uv run moonwright eval --briefs examples/eval --llm nebius --loop off --markdown out/eval_kinds.md   # live: per-kind table from examples/eval/kinds.json
    uv run python scripts/brief_budget.py fixtures/landing_pad/structure.json --graphs lazy
    uv run python scripts/replan_eval.py out/landing_pad.plan.json --llm fixture --all-tasks --sole-of-type solver
    uv run python scripts/llm_log_report.py out/llm_log.jsonl
    uv run moonwright serve                      # GET /health /examples; POST /structure /plan /plan/stream /simulate /montecarlo
    uv run pytest && uv run ruff check .

The per-kind evaluation is a live command: offline, `examples/eval/landing_pad.md` (the 300 m3
berm) would replay the demo take recorded for the 120 m3 brief, and most eval briefs have no
fixture at all.

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
  per robot, `travel_allowance`, the independent re-check), or, when CP-SAT refused the graph over
  a battery, the `budget` rows behind the hint (phase 3d); `repair` steps a diff with
  `scope_drift_pct` (tasks paired across a renumbering, phase 3d); `taskgraph` / `repair` steps
  the rate-model advisories; replan traces an `offset_h` / `time_base` per step.
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
  verdict line; `volume_shares` gives the eval its `min_volume_share`. (Phase 3e makes a
  shortfall a validation conflict, credits m2 stripping, reads the brief's smallest size and
  judges only what the brief states; see below.)
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
  `volume_advisories` / `min_volume_share` / `in_scope` (the "feasible in scope" rate next to
  "feasible"; since phase 3e: verified with every judged volume and area at 95 % or more of
  the brief's, see below), `requirements_sha` and `kind`, and summaries
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

## Pipeline notes (phase 3d)

* **Hint copy** (`pipeline/schedule.py`). Every solver hint is one readable sentence per
  conflict with every figure a repair needs: whole Wh and h with thousands separators and a
  spaced unit, real plurals, no `robot(s)`, no nested parentheses, no milli-Wh (decimals appear
  only when a demand and a budget would otherwise print equal: `1,784.39 Wh but has
  1,784.38 Wh`). The robots of one type share one pool sentence (`EX1 and EX2 each have
  5,000 Wh; the excavator tasks need 9,930 Wh in total, about 30 Wh of it travel, and cannot be
  split across 2 excavators (10,000 Wh combined) without exceeding one budget.`, the recorded
  tight_battery verdict); a robot whose own tasks exceed its battery reads `PR1 would need
  4,031 Wh, about 41 Wh of it travel, but has 4,000 Wh.`; the horizon hint says by how much each
  bound overshoots the deadline. The prose and the numbers come from one table:
  `budget_rows(req, graph, failure, opts, locked)` returns the rows behind the `energy:<robot>`
  conflicts (`kind` `type` | `robot`, `subject`, `robots`, `budget_wh`, `demanded_wh`,
  `travel_wh`, exact to the milli-Wh), and the failed `schedule` step carries them as
  `payload["budget"]` for the UI's budget table. The rows never reach the model: `Failure` is
  unchanged and `--feedback verdict` still blanks only the core. The decimals are chosen and
  printed by one rule (half up on the integer milli-Wh, `moonwright/text.py: wh`), never
  through a float, so a demand above its budget never prints equal to it.
* **One number format per screen** (`moonwright/text.py`). The relaxation advice
  (`EX1 needs 5,950 Wh (has 5,000 Wh): feasible at 62 h`), the validator's energy verdicts
  (`T1 needs 4,200.251 Wh but the largest excavator battery is 4,200.25 Wh`, exact because the
  comparison is) and the simulator's `battery_low` events render their Wh and h with the same
  thousands separators and plurals as the solver hint they sit next to; `fmt_wh` in
  `schemas.py` is the exact figure, `text.wh` the rounded one with the unit.
* **Repair diff across a renumbering** (`pipeline/loop.py: match_tasks`, `graph_diff`). The
  tasks of the graph a repair answered and of its answer are paired by (a) the same
  normalised name and (robot type, zone, structure, unit) slot, a repeated name (the halves
  of a split written twice, a hauler fleet's "Haul spoil") paired in graph order when the
  answer renumbered its tasks and by id otherwise, (b) the same name in another slot when
  both sides carry it equally often, (c) a unique slot (a renamed task that kept its place),
  (d) a kept id (a rename plus a move), never across a split or a merge; the rest are
  `added` / `removed`. The diff carries `renumbered` (`[{from, to}]`, the pairs whose ids
  differ) and `changed[].from` (the before id of a renumbered task, next to `task`, the after
  id). `changed` covers `name` (a rename, compared with case and punctuation ignored, so the
  demo's dropped "borrow pit" is a row), `duration_h`, `energy_wh`, `quantity`, `robot_type`,
  `zone`, `structure`, `unit` and `after`. Predecessor lists are compared through the
  renumbering and both written in the answer's ids (`before` translated, a predecessor that
  no longer exists marked `T2 (removed)`), so a shifted predecessor is not a change and a row
  reads side by side; a dependent that re-points from a removed task to an added one (the
  split's halves) is not a change, one that merely dropped its edge to a removed task is. An
  answer that repeats an id gets no diff (the validate step names the duplicate). The
  recorded tight_battery repair reads as the one-pass bowl `T2` removed, its halves `T2` /
  `T3` added, `T3 -> T4 ... T10 -> T11` renumbered and the key trench (`T4`, from `T3`)
  renamed from "... and borrow pit" to "... (shallow cut, berm fill from pad spoil)" and
  re-scoped from 24 h / 4,000 Wh to 14 h / 2,100 Wh, instead of thirty-three per-id changes.
  Scope, `missing:<task>` and `locked:<task>` verdicts stay id-based.
* **Replan outcome message** (`sim/replan.py`). The closing `replan` step's sentence no longer
  ends with the time-base flag (`... makespan 80 h -> 91 h`; `payload.time_base` still says
  `absolute`), counts read as plurals (`3 tasks`, `1 repair`), and a refused repair says what
  it redefined (`Replan refused: it redefined the running task T1; the previous plan stays in
  force.`).

## Pipeline notes (phase 3e)

The first live evaluation (22 briefs x Ultra / Super x 3, 132 runs, $9.9) verified 85 % and 91 %
of the runs but only 45 % and 38 % in scope: the models satisfied the solver by shrinking the
work, the volume advisory never reached the repair prompt, 78 of 190 Ultra reasoning calls were
cut at 16,384 tokens, and the validator's energy check ignored the transit CP-SAT charges.

* **Volume is a conflict** (`pipeline/validate.py: volume:<structure>`, `pipeline/rates.py:
  volume_coverages`, `volume_shortfalls`). After coverage and before the capacity checks, every
  dug shape (bowl, box, a berm's key trench) and every placed body (berm prism) whose structure
  is not stockpile-like (`stockpile | spoil | pile | depot | dump | heap` in its type, `label`
  or `name`; a berm, a spoil berm included, is never one) must be covered to at least
  `VOLUME_LOW` (70 %) of the smallest volume the brief allows. Covered means the `m3` of the
  structure's excavator digs on a dug shape, or its hauls on a placed one, plus the stripping
  credit of excavator `m2` tasks on a dug shape: the areas summed, capped at the footprint of
  the smallest size, times min(depth, `STRIP_DEPTH_M` = 0.2 m) (the rate table's m2 row now
  reads "strip or grade a surface layer up to 0.2 m; deeper cuts are excavation in m3"; a
  hauler's m2 counts nothing). The review made it judge only what the brief states and close
  the credit's loopholes:
  * the floor comes from `min_<dimension>_m` extras, which the structure prompt records for a
    brief that allows a range or a reduction; a bowl or box whose walls slope (`rates.wall_slope`:
    a `wall_slope` extra, the structure's own entry in the brief, a constraint; "8 m diameter,
    2 m deep, with a 1:1 sloped wall") is judged between its size read at the rim (58.6 m3) and
    at the floor (159.2 m3) instead of with vertical walls (100.5 m3, which rejected the pit
    dug to its real volume); a key trench is judged only with its `trench_width_m` (the berm's
    base width it used to take rejected tight_battery's 1.2 m trench as 30 % of an invented
    4 m one); a berm with no base width holds at least its 1:1 section;
  * an m2 pass after a dig of the shape earns no credit unless it is named a strip (a 12 m dig
    plus a "Level pad base 314 m2" pass passed as 86 % of the 20 m pad), and below the stripped
    layer only digs count, to 70 % of that layer (a strip of the whole kind-E pad plus a 1 m3
    test pit read 113 % of its floor);
  * spoil moved is no dig (`rates.rehandles`: blade, push, windrow, backfill ... in the name
    without excavate / dig): it counts neither for nor against the shape;
  * a placed body whose hauls wait (by `after`, transitively) for another structure's dig is
    judged against the smaller of its own floor and that dig's: landing_pad's berm "built from
    pad spoil" is covered by all 125.7 m3 of the bowl, not 42 % of its 300 m3 prism.

  Detail: `S2 (borrow_strip): dimensions 100 x 6 x 0.6 m = 360 m3 of excavation; excavator
  tasks cover 120 m3 (33 %): T3 600 m2 counts 120 m3 at 0.2 m` (the share rounded down, so a
  69.96 % plan never reads 70 %; a floor reads `bowl 12-20 m across, 0.5 m deep = 56.5-157.1 m3
  ... (44 % of the smallest size the brief allows)`, a berm `... = 300 m3 of fill, more than the
  125.7 m3 of spoil S1 yields; hauler tasks cover 50 m3 (39 % of that spoil)`, a trench `its key
  trench is 60 x 1.2 x 0.8 m = 57.6 m3 of excavation`); hint: `S2 (borrow_strip) plans 120 of
  360 m3: plan the remaining 240 m3 of S2 as excavator tasks in m3 (split above 60 m3) instead
  of shrinking the work; keep the m2 stripping only for the top 0.2 m.` (a berm without a
  source: `..., each after the excavation its fill comes from (a borrow dig in m3 when no
  structure's spoil supplies it), ...`). An excess above `VOLUME_HIGH` (150 %), judged on the
  digs alone (a pad stripped and then dug in full is no excess, nor is spoil bladed into a
  windrow), stays an advisory: `... total 600 m3 (200 %): hauled twice over` / `dug 1.5 times
  over`; the fill of a stockpile-like structure is judged neither way.
  `validate_graph(..., volume=None)` runs the check exactly when `coverage` is on (a whole
  plan), `False` switches it off, `True` forces it; the planner passes `False` for a replan's
  remaining graph and its repairs. Graphs without quantities and structures without a positive
  volume are untouched.
* **Validator transit** (`pipeline/validate.py: transit_allowances`). Both energy checks charge
  every task the transit allowance CP-SAT reserves (the least over the robots of its type, so
  the validator never demands more than the solver; the planner passes a replan's
  `robot_positions`), equal to the solver's `budget_rows` figure to the milli-Wh on the
  tight_battery fixture, and name it: `printer tasks need 4,037 Wh, about 38 Wh of it travel;
  its 1 robot carries 4,000 Wh`, `T1 needs 4,006 Wh, about 11 Wh of it travel, but the largest
  printer battery is 4,000 Wh`. One verdict per cause: a type's overrun that its oversized
  tasks alone explain is reported as `energy_task:<task>` only, with the sentence `The fleet's 1
  printer carries 4,000 Wh in all, so splitting T1 alone cannot fit it: keep the full work unless
  the brief allows less.`; an overrun with a
  cause of its own keeps `energy:<type>` beside them. Every figure goes through
  `moonwright/text.py` (`wh`, and the new `qty` / `pct` for the volume sentences).
* **What a repair is told** (`llm/prompts.py`, `pipeline/taskgraph.py`, `pipeline/loop.py`).
  `generate_task_graph(advisories=...)` passes the advisories of the step that produced the
  graph being repaired to `repair_messages(advisories=...)`, which renders them as an
  `ADVISORIES` block between `FAILURE` and `CONTEXT` (an advisory whose id the verdict already
  carries as a conflict is left out, except a `volume:` excess: a berm hauled twice over keeps
  its note when its own key trench is the verdict's `volume:` shortfall), so a 200 % double haul
  is cut in the same round as an energy verdict. `REPAIR_SYSTEM` says what each conflict id
  needs (energy -> move or split quantities, `after` edges alone change no energy;
  `energy_task` -> split, shares summing to the original, and when the type cannot carry the
  work at all, keep it whole: the solver proves the shortfall and proposes a fix; horizon ->
  parallelise; `scope` -> add the missing task of the right type; `volume` -> add the missing
  m3, never shrink; a smaller quantity only where a `min_<dimension>_m` allows it, stated in the
  notes). No rule tells a repair to trim: "trim or move it" verified kind-D briefs after a
  silent 15-25 % cut. The shared `COVERAGE_RULES` state the volume contract the validator
  checks, and the structure prompt's dimension examples are marked as other projects' numbers
  (its trench example is 0.7 x 0.45 m, which no brief uses); it also asks for `lifts` and a
  pit's `wall_slope` when the brief states them.
* **Token policy** (`llm/tokenfactory.py`, module docstring "Token policy"). Reasoning calls
  start at `THINKING_MAX_TOKENS` = 32,768 (`MOONWRIGHT_THINKING_MAX_TOKENS` overrides; a value
  below the default is warned about once, since a `backend/.env` copied from the pre-3e
  `.env.example` pins 16,384, and `moonwright eval` prints the effective budgets). On
  `finish_reason == "length"` the next ask repeats the question with twice the budget
  (`THINKING_MAX_TOKENS_CAP` = 65,536, timeout scaled with it), reasoning on; only a cut at the
  cap is asked again with reasoning off at `MOONWRIGHT_MAX_TOKENS` with the compact-JSON note.
  Any other failure keeps the old re-ask. The ask count per call is unchanged (two by default),
  and the ladder spans the planner's re-ask of a rejected answer (the next call of the same
  role, key and variant at the next attempt, in the same thread, starts on the rung the last
  ask led to), so an always-cut prompt costs the same six asks as before but climbs [32,768,
  65,536, 4,096 off, 8,192, 16,384, 32,768] instead of repeating [32,768, 4,096] three times;
  the call log records each ask's `max_tokens` and `thinking`.
* **Rejected answers** (`llm/tokenfactory.py` "Rejected answers", `llm/base.py:
  answer_failure`). A call whose asks all failed raises with a `failure` written for a person:
  hint `Nemotron 3 Ultra did not return a valid task graph after 2 asks: the answer was cut off
  at 32,768 tokens, then failed the schema` (`valid requirements` for the structure stage),
  details one line per ask (`ask 1: cut off at 32,768 tokens`, `ask 2: task T3: 'after' must
  be a list`, item ids instead of pydantic paths and pydantic's wording, e.g. `'tasks' must not
  be empty`, `'id' may use only letters, digits and _ . -`), conflicts `schema:json` (cut
  off, not JSON, empty) / `schema:shape` (a schema violation); a request the SDK gave up on
  reads `The Token Factory request to Nemotron 3 Ultra failed: APITimeoutError (Request timed
  out.)` (`text.model_name` gives the short names). The task-graph and structure stages put
  that failure on their step; a client without one (fixtures, tests) keeps its own hint and
  details under the same two ids (`schemas.schema_conflict`; `schema:$` is gone).
* **Eval: plans and resume** (`cli.py eval`). `--save-plans DIR` writes every run's PlanResult
  as `DIR/<brief>__<arm>__<loop>__<repeat>.plan.json` (what `POST /plan` returns; `plan_path`,
  relative to the CSV's directory so a resumed row keeps naming its own file whatever DIR the
  next run uses, is the last CSV column) and each brief x repeat job's structuring as
  `DIR/<brief>__<repeat>.structure.json`, both written the moment they exist (a partial job
  completed by `--resume` writes `<brief>__<repeat>.2.structure.json`, never over the record
  its earlier conditions planned from). `--resume` keeps the rows an earlier run left in
  `--csv` (the last row per brief x condition x repeat wins; a last row cut off mid-append is
  dropped from the file and its job runs again), skips the jobs it holds completely, runs only
  the missing conditions of a partial job (structured anew), appends under one header and
  summarises old and new rows together (`resumed_rows` in the JSON, a `resumed: N rows` line in
  the markdown). Rows gain `rejected_answers` (task-graph / repair steps that ended `schema` or
  `llm_error`) and `min_area_share`, the summary `rejected_runs`, `rejected_answers` and
  `cost_p50_usd`.
* **In scope** (`pipeline/rates.py: scope_gaps`, `area_coverages`, `cli.py: eval_row`). The
  validator's 70 % is a conflict threshold, not a definition of done: a kind-D repair that
  trimmed the borrow dig to 85 % or the two compaction lifts to 78 % verified and was counted
  "feasible in scope". `in_scope` now needs every judged volume and every compaction /
  sintering area (the floor's footprint x `lifts`; a berm's crest and a stockpile are not
  judged) at `IN_SCOPE_SHARE` = 95 % or more; an excess (hauled twice over) is no gap.
  `moonwright plan` prints the gaps under its verdict line and the eval prints `not in scope:
  volume 85 %` per row.
* **Brief budget** (`scripts/brief_budget.py`). Fines up to 0.2 m are stripped in m2 (the m2
  row), deeper fines dug in m3; `min_<dimension>_m` keys reach the requirements (the
  `low_battery_pad` registry entry records `min_diameter_m: 12`) and the budget notes the
  volume floor; the `shrunk` graph variant plans every dig as stripping its footprint in m2
  (the live re-scope), which the volume check now rejects (`low_battery_pad` included: the
  strip of its 20 m pad no longer covers the 12 m floor), while every `lazy` / `split` graph
  covers its volumes and is in scope (`docs/eval-briefs.md`).

## Pipeline notes (phase 3g)

Live run 2 showed CP-SAT verifying plans that haul before anything is dug (it schedules only the
edges it is given), and an in-scope metric that read the structuring model's slips as the brief.

* **Supply order is a conflict** (`pipeline/validate.py: supply:<haul>`, `supply_report`). A haul
  is a hauler task in m3 (`rates.is_haul`); its target is the structure of the zone it names (the
  coverage contract: a haul's zone is where the material is placed), or a pile (a stockpile-like
  structure, a zone no structure claims). Its sources are every dig in m3 that is no rehandle
  (`rates.is_dig`) and an m2 strip or grade (`rates.is_strip`) where its spoil can be what the
  haul moves: into the strip's own structure (a haul lane's grading), into a pile, or in a graph
  that digs nothing in m3 (a berm "built from the hardstand's strip spoil"); a strip makes at
  most 0.2 m of its area available (`rates.supply_m3`). A graph without any source is not judged.
  A haul is the conflict when it waits (transitively, through any task) for no source (`T7 hauls
  280 m3 for S2 (plume berm) but waits for no excavation, so it could start before any material
  is dug`; hint `make T7 and T8 wait for the digs that supply them (S1 foundation pit: T3, T4; S3
  borrow strip: T5, T6)`), or, into a designed body, when it moves with the hauls into that body
  before it more than 1.25 x (+1 m3) what its sources make available while a dig in m3 that
  neither precedes nor waits for it could supply the rest (a 300 m3 berm haul after its own 15 m3
  key-trench cut: `make T7 wait for more of the digs that supply it (S1 landing pad: T2, T3)`).
  Sources are grouped by structure, a placed body's own dig (its key trench) last; ids follow the
  graph. The hint closes with `a haul part may wait for only the dig parts that remove what it
  moves, so split hauls to keep both chains short.`, and the horizon_path hint now says never to
  drop a dig-to-haul edge. After volume, before energy; whole plans only:
  `validate_graph(..., supply=None)` follows the volume switch (`True` / `False` override).
* **Supply balance is an advisory** (`validate.supply_advisories`, on the task-graph / repair
  step of a whole plan): when every source already precedes a body's hauls and they still move
  more than those make available, the plan stands and the note says so once per structure,
  summed per target (chained and parallel parts read the same; a rehandle through a stockpile
  is not counted twice; a pile is never judged): `supply:S2: its hauls (T5 150 m3, T6 150 m3)
  move 300 m3, more than the 125.7 m3 the digs before them remove (S1 landing pad: T3, T4)`.
  Usually the brief's own arithmetic (a berm prism larger than its source's spoil), so the
  repair prompt's ADVISORIES header says a supply note keeps the body's m3 and is answered only
  by waiting for more of the digs that feed it, never by a cut or an invented dig.
* **Prompts** (`llm/prompts.py`). Task graph: "Every haul waits (`after`) for the dig that
  produces its material." Repair, `supply:<haul>`: add `after` edges from the digs that supply
  it; to hold a deadline split hauls so each part waits only for the dig part feeding it and dig
  separate sources on separate excavators in parallel; never delete a dig-to-haul edge to
  shorten a chain. Structure: `lifts` is the number of compaction layers, only when the brief
  states a count. Paid for inside the 5,500-character caps.
* **Lifts are corrected to the brief** (`rates.lifts`, `stated_lifts`, `with_judged_lifts`,
  `structure.correct_lifts`). The count is the one the structure's own entry states (`tags.entry_line`
  over `tags.brief_lines`, so a hard-wrapped entry reads whole; headings skipped): "in three
  lifts", "two compaction lifts", "compacted in 2 layers" (layers only where something is
  compacted, never "sintered in two layers" nor "a two-layer shield"), the smaller count of a
  range ("2-3 lifts" -> 2), "in three 30-cm lifts", "lifts: 3"; an entry that names lifts
  without a readable count keeps a recorded count of 1 to 10; otherwise 1. The structure stage
  writes that count into the requirements (the task-graph model never sees the brief) and says
  so in an amber note: `structure S1: lifts 4 -> 1: the brief's entry for S1 states no lift
  count we can read` (crater_ramp's "four 10 m segments"), `lifts 0.2 -> 1` (access_road's "a
  20 cm compacted lift"), `lifts none -> 2` (small_fleet_long_berm). Without a brief, or a type
  no line names, the recorded count stands.
* **Placed bodies in "in scope"** (`rates.base_width_stated`, `_work_area`). A berm is judged for
  compaction on the base width the brief states (its entry, or the line that states its recorded
  length and height: Lightning typed depleted_excavators' dust berm "borrow_strip"), else on the
  least base it can have, as wide as it is high (length x height): access_road's "20 m long, 1.5
  m high" berm is 30 m2, not the 60 m2 of the 1:1 guess, and a 10 m2 pass over a 40 m x 2 m berm
  is still a gap. Volume judgments are unchanged.
* **Brief budget** (`scripts/brief_budget.py`). A haul waits for its sources' digs or strips
  wherever the registry lists them, `serial` chains sources first (`fill_order`), and the
  phase-order bound (`LB`) reads them in that order (small_fleet_long_berm 60 -> 112 h,
  shielding_mound 28 -> 52 h). `LB` bounds plans that keep each phase whole, as the canonical
  graphs do; a plan that pipelines dig and haul parts can finish below it. road_and_pad's
  windrows ("grading spoil pushed") are an excavator m2 pass, not a haul. Every eval brief's
  lazy / split / fine / serial / shrunk graph passes the supply check.
* **Rescoring a saved eval** (`scripts/rescore_eval.py DIR [--out CSV]`): `in_scope`,
  `min_volume_share`, `min_area_share` and the supply verdicts recomputed from every row's saved
  plan with the current code (no model call; an unreadable plan counts as none); a per-condition
  table, the rows that changed, and `eval.rescored.csv`. Run 2:

  | condition | verified | verified in scope, before -> after | of those, supply clean | verified, rejected by supply | verified, supply note |
  | --- | --- | --- | --- | --- | --- |
  | `ultra/loop_on` | 56 of 66 | 49 -> 52 | 36 | 17 of 56 | 10 of 56 |
  | `super/loop_on` | 51 of 66 | 38 -> 45 | 31 | 14 of 51 | 4 of 51 |

  The in-scope gains are crater_ramp (lifts), access_road's berm (30 m2), one_surveyor's and
  two_pads' width-less berms compacted over at least length x height; no `min_volume_share`
  moved.

## Evaluation flags

    uv run moonwright eval --briefs examples/eval --repeat 3 --loop both --models ultra,super --thinking auto \
      --llm nebius --workers 3 --max-answer-retries 2 --save-plans out/eval_live_plans \
      --out out/eval_live.json --csv out/eval_live.csv --markdown out/eval_live.md   # add --resume to continue
    uv run moonwright eval --briefs examples/eval --repeat 2 --loop on --feedback verdict --llm nebius
    uv run python scripts/replan_eval.py out/live/*.plan.json --llm nebius --all-tasks --sole-of-type solver \
      --max-escalations 10 --out out/replan_eval_live.json --markdown out/replan_eval_live.md
    uv run python scripts/llm_log_report.py out/llm_log.jsonl --since <ISO start> \
      --markdown out/llm_log_report.md --out out/llm_log_report.json
    uv run python scripts/brief_budget.py --all examples/eval --graphs lazy,split --markdown
    uv run python scripts/llm_schedule_check.py out/landing_pad.plan.json --repeat 3 --out out/direct.json
    uv run python scripts/rescore_eval.py ../docs/eval/2026-10-23-live-eval-run2 --out out/run2.rescored.csv

`--workers N` runs brief x repeat jobs on a thread pool (a job structures its brief once and runs
every condition from that requirements object) and flushes each row to the CSV as it completes (the
JSON report keeps brief x condition x repeat order); `--feedback core|verdict` is the ablation (the
repair round gets the minimal infeasibility core with hints, or only the verdict kind);
`--max-answer-retries 0..5` bounds schema / transport re-asks per round in both conditions
(`--max-repairs` counts verdict rounds only); `--save-plans DIR` keeps every run's plan and
each job's structuring (`plan_path` in the rows, relative to the CSV), and `--resume` continues a
killed run from its CSV, running only the missing brief x condition x repeat cells. Rows record the models used, `answer_valid`,
`first_try_feasible`, first/final duration and energy totals, `scope_drift_pct`, the scope of
the final graph (`volume_advisories`, `min_volume_share`, `min_area_share`, `in_scope`), the
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
`MOONWRIGHT_MAX_TOKENS` (4096), `MOONWRIGHT_THINKING_MAX_TOKENS` (32768, the first rung of the
token ladder), `MOONWRIGHT_LLM_TIMEOUT_S`,
`MOONWRIGHT_LLM`, `MOONWRIGHT_LLM_LOG` (one JSON line per Token Factory call in
`out/llm_log.jsonl`: status, model, tokens, latency, request id; never prompt or answer text; `off`
disables it), `MOONWRIGHT_CORS_ORIGINS` / `MOONWRIGHT_CORS_ORIGIN_REGEX` for a deployed UI. All are
documented in `.env.example`.
