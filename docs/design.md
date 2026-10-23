# Moonwright: design notes

The detailed account of how each stage works and why, written as the project evolved. The
[README](../README.md) is the short version; [reference.md](reference.md) lists the CLI, the HTTP API and
the environment variables; [eval/](eval/) holds every live evaluation run.

## Pipeline

```
brief (text)
   │  F1  Nemotron (structure)          → Requirements JSON (site, zones, structures, robots, horizon)
   ▼
Requirements
   │  F2  Nemotron (task graph)         → tasks: what and how much (quantity, unit, basis), robot type,
   │      + deterministic rate model      zone, precedence → hours and Wh computed from the rate table
   ▼
TaskGraph ──► F3 validator (cycles, references, capability, energy + transit, critical path, coverage: phases + the brief's operations, volume)
   │               │ conflicts
   ▼               ▼
F4 CP-SAT ───► infeasible? ── F5 Nemotron repair (≤ 3 rounds, fed the infeasibility core) ──┐
   │ feasible (precedence, robots, zones, batteries, transit allowance, horizon)             │
   ▼                                                                                        │
Schedule + F6 explanation ──► F7 2D simulator ──► event (robot failure, low battery) ──► F8 replan ─┘
                                                      CP-SAT reschedules first; Nemotron is asked only
                                                      when the remaining fleet provably cannot finish
```

* **Models (Nebius Token Factory, OpenAI-compatible API).** Short, frequent structuring calls go to
  `nvidia/Nemotron-3_5-Lightning`; task-graph generation and repair go to
  `nvidia/Nemotron-3-Ultra-550b-a55b`; plan explanations use `nvidia/nemotron-3-super-120b-a12b`.
  Routing is configurable through environment variables (see `backend/.env.example`).
* **Token policy.** Reasoning and answer share `max_tokens`. The first live evaluation cut 35 % of
  the 16,384-token reasoning calls (Ultra 78 of 190) and the re-ask at the same budget was
  usually cut again, so a reasoning call now starts at 32,768 tokens and every ask adapts to how
  the last one ended: a cut answer is asked again with twice the budget (65,536, with
  proportionally more time), and only a cut at 65,536 is asked again without reasoning at the
  normal 4,096 with a "compact JSON only" note; any other failure is re-asked as before (same
  budget, the validation errors appended). The ask count is unchanged (two per call by
  default) and the ladder carries over into the planner's re-ask of a rejected answer, so the
  doubled rung is reached on the second ask and the reasoning-off fallback on the next call. A
  budget below the default set in the environment (an old `backend/.env` pins 16,384) is
  warned about, and `moonwright eval` prints the effective budgets. An answer that never
  validates is shown as the person would say it:
  `Nemotron 3 Ultra did not return a valid task graph after 2 asks: the answer was cut off at
  32,768 tokens, then failed the schema`, one line per ask (`ask 2: task T3: 'after' must be a
  list`), conflict ids `schema:json` (cut off, not JSON, empty) and
  `schema:shape` (a schema violation), and a failed request as `The Token Factory request to
  Nemotron 3 Ultra failed: APITimeoutError (Request timed out.)`. The call log records each
  ask's own budget and thinking flag.
* **Rate model (physical grounding).** The model states *what* and *how much* for every task
  (`quantity`, `unit`, `basis`, e.g. `126 m3 = pi*(10 m)^2*0.4 m`); a six-row rate table in
  `backend/moonwright/pipeline/rates.py` turns the quantity into whole hours and Wh (7 m3/h and
  16 Wh/m3 for bulk excavation, 14 m2/h and 8.6 Wh/m2 for sintering, ...), so a plan cannot be made
  feasible by shrinking numbers: an under-scoped graph becomes a real solver conflict and a repair has
  to split or re-scope work. The units are illustrative lunar-rated low-power figures, every row an
  assumption with its note (see Limitations). The model's own estimate is kept beside the computed
  figure and an `estimate:<task>` advisory flags a gap of more than 50 %; tasks without a quantity
  keep the model's hours.
* **Volume: the work cannot be shrunk to fit.** In the first live evaluation the models satisfied
  the solver by shrinking the work (a 20 m pad dug as a 12 m one, a 360 m3 borrow strip planned
  as 600 m2 of stripping, a 300 m3 berm hauled twice): 85-91 % of the runs verified, only
  38-45 % in scope. The validator now compares the m3 a structure's tasks plan with the volume
  its dimensions imply (bowl, box, berm prism, a berm's key trench as its own dug shape) and
  rejects a whole plan below 70 % of it as a `volume:<structure>` conflict that shows the
  arithmetic and says what to add (`S2 (borrow_strip): dimensions 100 x 6 x 0.6 m = 360 m3 of
  excavation; excavator tasks cover 120 m3 (33 %): T3 600 m2 counts 120 m3 at 0.2 m`, hint
  `plan the remaining 240 m3 of S2 as excavator tasks in m3 (split above 60 m3) instead of
  shrinking the work; keep the m2 stripping only for the top 0.2 m`). An excavator m2 task is a
  surface layer up to 0.2 m (the rate table's m2 row), so it counts area x min(depth, 0.2 m),
  capped at the footprint: a 10 x 10 x 0.15 m pad stripped and graded in m2 is fully covered
  (the old rule read it as 0 %); a levelling pass after the dig earns nothing, and below the
  stripped layer only digs in m3 count. A brief that allows a smaller size ("any diameter
  between 12 m and 20 m") is recorded by the structure stage as `min_diameter_m` (likewise
  `min_depth_m`, `min_length_m`, `min_width_m`, `min_height_m`) and the check judges the
  smallest allowed volume, so the allowed re-scope stays plannable. Only what the brief states
  is judged: a pit's sloped walls ("with a 1:1 sloped wall") are read from the brief, a key
  trench needs its stated width, and a berm "built from pad spoil" is covered by all of that
  spoil even when its prism would hold more. Spoil bladed into a windrow or backfilled is no
  dig; the fill of a stockpile is judged neither way; an excess (`hauled twice over`) stays an
  advisory; a replan's remaining graph is never judged. Structure tags resolve exactly as the
  coverage check resolves them (id, type, label, zone; a haul placed in the berm's zone
  builds the berm).
* **Structuring guards.** The structure stage is told to copy every dimension the brief states
  (the task-graph model never sees the brief, only the requirements), salvages `operations` that
  strict JSON-schema decoding scrambled into stray keys (seen live with Lightning:
  `"operations': [": "excavate"`) and says so on the requirements card, and warns there when a
  structure has no dimensions, when zones overlap or when a listed operation has no robot type in
  the fleet.
* **Solver.** CP-SAT minimises makespan with optional intervals per (task, robot), `AddNoOverlap` per
  robot and per safety zone, linear battery budgets per robot and a horizon bound. Travel between
  zones is a per-task transit allowance (whole hours and 0.5 Wh per grid cell, an upper bound over
  the zones a robot can be sent to) inside the robot intervals and the energy sums, so a verified
  schedule replays in the simulator without overrunning itself. The validator charges every task
  the same allowance (the least any robot of its type would be charged, from the robots' actual
  positions on a replan), so a graph it passes at 3,999 of 4,000 Wh is no longer one CP-SAT
  rejects at 4,037 Wh, and its energy verdicts name the travel share (`printer tasks need
  4,037 Wh, about 38 Wh of it travel; its 1 robot carries 4,000 Wh`); one verdict per cause (a
  type's overrun that one oversized task explains is reported as that task). Each constraint group sits behind an
  assumption literal; on infeasibility the solver's sufficient-assumption core is shrunk by deletion
  to a minimal set and returned as machine-readable conflict ids such as `energy:EX1` or
  `precedence:T4->T2`, and an independent checker re-verifies every schedule (the verification
  receipt in the trace).
* **Repair loop.** Conflict ids plus a human hint go back to Nemotron with the previous graph,
  together with the advisories on that graph (a 200 % double haul is cut in the same round as
  the energy verdict), and the repair prompt says what each conflict kind needs (an energy
  verdict moves or splits quantities, `after` edges alone change no energy, and work a robot
  type cannot carry at all stays whole for the solver to prove; `volume:` adds the missing m3,
  never shrinks the work; `scope:` adds the missing task of the right robot type; a smaller
  quantity only where the brief allows it, stated in the notes). The loop stops at
  the first solver-verified plan or after three repairs. The validator's structure
  coverage check (`scope:<structure>`) refuses a repair that drops a required phase or a whole
  structure, and every accepted repair carries a diff (added, removed, renumbered and changed
  tasks, energy per robot type, scope drift in %) so a re-scope is visible instead of silent;
  tasks are paired across a renumbering, so a split that shifts every later id reads as a split
  plus the one task that was re-scoped, not as a dozen re-scoped tasks. Lightning also records,
  per structure, the operations the brief explicitly asks for (`sintered crest` -> sinter, `built
  from pad spoil` -> haul, `key trench` -> excavate, `inspected` -> survey); the coverage check
  requires a task of the right robot type for each one, so a live graph that silently omits them
  is a `scope:<structure>` conflict whose hint quotes the brief (`the brief asks for "on a key
  trench 1.2 m wide and 0.8 m deep cut into firm material"`), and an operation the fleet cannot
  staff is a warning on the requirements card, not a conflict. So is an operation the brief does
  not support while it supports the alternative (`excavate` listed on a berm the brief says is
  "built from a borrow strip ... placed and compacted"): the quote is always the phase's own verb,
  never a noun that names another structure or a robot, so an over-declaration by the
  structuring model cannot force invented work (seen live: a 35 m3 "foundation" dig added to
  satisfy one).
* **Advice when no repair can work.** When the repairs are exhausted, the loop is off or the brief
  is impossible, the planner returns `PlanResult.suggestions`: resource changes each proven by a
  real CP-SAT solve on the last graph (`EX1 needs 5,950 Wh (has 5,000 Wh): feasible at 62 h`, the
  smallest battery to 50 Wh that CP-SAT still schedules; `one more excavator (EX3, 5,000 Wh like
  EX1): feasible at 62 h`; `finishing needs 86 h, 26 h past the 60 h deadline (CP-SAT optimal)`),
  changes that keep the deadline first, or one unverified `reduce_scope` naming the largest work
  items when every single change was refuted (a verification solve that times out counts as
  untried, never as refuted). The list rides on the final failed step of the trace too, so the UI
  and `moonwright plan` show it with the verdict; a replan is offered only a later deadline or a
  spare robot, in absolute mission hours against the brief's deadline and with a full battery
  (`one more excavator (EX3, 5,000 Wh, a full battery): feasible at 85 h` next to the 80 h plan
  it replaces).
* **Simulation and replanning (solver first).** The simulator moves robots on the site grid, drains
  batteries and injects failures. On an event the tasks underway are locked and CP-SAT reschedules
  the remaining work in milliseconds; only when the remaining fleet provably cannot finish does the
  infeasibility core go to Nemotron, which re-scopes the work for the solver to re-verify
  (`ReplanRecord.escalated`). A repair that redefines a task underway is refused (`locked:<task>`)
  and the previous plan stays in force; the UI shows the before/after schedules.
* **Robustness (Monte Carlo, F9).** The verified plan is replayed N times with random robot
  failures (exponential per-robot hazard) and task-duration jitter, replanning on every event, and
  reports the probability of finishing within the horizon with a Wilson 95 % interval, finish-time
  percentiles, a histogram, the outcome of every miss (`lost_only:<type>`: the only robot of a type
  died, `replan_infeasible:<kind>`, `late`, `stuck`) and the fleet's single points of failure.
  Replans are solver-only (CP-SAT re-schedules the remaining work), so 50 runs take about a second.

## Evaluation

The core experiment toggles one variable, the repair loop (F5), over 22 test briefs
(`backend/examples/eval/`, five kinds documented with their arithmetic in
[`docs/eval-briefs.md`](docs/eval-briefs.md): comfortable, battery-split, horizon-bound,
impossible, scope reduction allowed), with Nemotron 3 Ultra and Nemotron 3 Super as model arms.
Metrics: feasible-plan rate (Wilson 95 % CI) and the share that is also *in scope* (every
structure's planned volume and its compaction / sintering area at 95 % or more of the smallest
size the brief allows: the validator rejects a volume under 70 %, and a plan that verified after
trimming the work 15-25 % is a re-scope, not a success; an excess such as a berm hauled twice is
reported, not counted against it), first-try
feasibility, rejected answers (task-graph / repair answers cut off or off the schema), repairs
to feasibility, makespan, p50/p95 latency, tokens and cost per run, per condition and per brief kind
(`backend/examples/eval/kinds.json`). Each brief is structured once per repeat and every
condition plans from that same requirements object (`requirements_sha` in the rows), so model
arms and loop settings are a paired comparison rather than four samples of the structuring
model. Three further measurements: robot failures injected into every verified plan to measure
solver-only recovery versus escalation (`backend/scripts/replan_eval.py`; rates over the
*askable* injections, a lost sole-of-type robot nobody can replace being counted apart),
Nemotron-written schedules (no solver) fed to the same checker to count constraint violations
(`backend/scripts/llm_schedule_check.py`), and the Token Factory call log summarised per model and
role (`backend/scripts/llm_log_report.py`: calls, errors, re-asks, tokens, tok/s, latency, cost,
request ids). `backend/scripts/brief_budget.py` prices any brief with the planner's own rate table
(per-type Wh need versus fleet, packing, bounds, canonical graphs through validator and CP-SAT), so
the test set's kinds are arithmetic, not opinion. Results will be published here once measured;
numbers are never quoted before they exist. The exact commands, budgets and wall times are in
[`docs/live-run-runbook.md`](docs/live-run-runbook.md).

```bash
cd backend
uv run moonwright eval --briefs examples/eval --repeat 3 --loop both --models ultra,super \
  --thinking auto --llm nebius --workers 3 --max-answer-retries 2 --save-plans out/eval_live_plans \
  --out out/eval_live.json --csv out/eval_live.csv --markdown out/eval_live.md
# killed or rate-limited part-way: the same command with --resume runs only the missing cells
uv run python scripts/replan_eval.py out/live/*.plan.json --llm nebius --all-tasks \
  --sole-of-type solver --max-escalations 10 --out out/replan_eval_live.json --markdown out/replan_eval_live.md
uv run python scripts/llm_log_report.py out/llm_log.jsonl --since 2026-10-23T00:00:00+00:00 --markdown out/llm_log_report.md
uv run python scripts/brief_budget.py --all examples/eval --graphs lazy,split
uv run moonwright eval --briefs examples/eval --repeat 2 --loop on --feedback verdict --llm nebius   # ablation arm
```

`--workers` runs brief x condition x repeat in parallel and flushes each row to the CSV as it
finishes; `--feedback verdict` tells the repair round only that the plan was infeasible (no core, no
hints), which isolates what the minimal infeasibility core is worth; `--max-answer-retries` bounds
schema / transport re-asks in both conditions so the loop-off condition is not conflated with JSON
failures. `--save-plans DIR` keeps every run's plan (what `POST /plan` returns) for the replan
evaluation and the write-up, and `--resume` continues a killed run from its CSV. Every Token
Factory call is appended to `out/llm_log.jsonl` (`MOONWRIGHT_LLM_LOG`; status, model, tokens,
latency, request id, the ask's `max_tokens` and thinking flag, never prompt or answer text).

## Limitations and known failures (full list)

* **Travel is a per-task transit allowance (an upper bound over the site), not terrain; the simulator
  pays the actual distance.** No slope cost, path planning or congestion; a leg of any length costs a
  whole hour in the planner, so verified makespans are a few hours above the work-only optimum.
* **The rates are assumptions.** `backend/moonwright/pipeline/rates.py` uses illustrative lunar-rated
  low-power units calibrated to the example briefs; the regolith-excavation literature (RASSOR,
  Chariot/LANCE) is inspiration for the order of magnitude only. Replace the rows with measured ones
  before trusting an hour figure. Tasks without a quantity keep the model's own estimate.
* **"Verified" means verified against the planning model**: precedence, one task per robot at a
  time, one robot per zone, battery budgets with the transit allowance, the horizon. A repair can
  re-scope work (the shipped tight-battery fixture cuts a trench from 4000 to 2100 Wh): the repair
  diff and scope-drift figure make it visible and the coverage check refuses a repair that drops a
  phase or a structure, but a smaller scope that keeps every phase is accepted for the planner to
  judge.
* **The simulator is 2D and kinematic**: straight-line travel on a grid, no recharge, no night cycle,
  no material-flow coupling between hauling and the work that consumes the fill; failures are injected,
  not modelled.
* **Repair-loop failures.** The model can answer with an unchanged graph, a schema-invalid one or a
  re-scoped one; a replan whose remaining work has no feasible re-scope ends as
  `replan_infeasible` in the Monte Carlo outcomes rather than inventing a plan. The eval counts these
  cases and the measured rates will be reported here, never before they exist.
* **The brief's operations are read by a model.** Lightning's `operations` list gates the coverage
  contract: in the live smoke it declared a berm's key trench in one run (the omission was caught
  and repaired) and left it out in the next (the trench was planned away unnoticed). Over-declared
  operations become warnings, not conflicts, when the fleet cannot staff them (sinter with no
  printer) or when the brief supports the alternative instead (excavate on a berm built from a
  borrow strip); any other listed operation is strict about the robot type that performs it, so
  an over-declaration the brief text cannot disprove still costs a task.
* **The volume check is only as good as the structured dimensions.** A whole plan below 70 % of
  a structure's volume is refused (`volume:<structure>`), but a structure the structure stage
  recorded without dimensions has no volume to judge (nor does a key trench without its width,
  or a stockpile), the 0.2 m stripping credit, the 1:1 section of a berm given by its height
  alone and the vertical walls of a pit whose slope the brief does not state are model
  assumptions, task names decide what a levelling pass or a rehandle is, a replan's remaining
  work is not judged, and an excess (hauled twice over) is only an advisory: it verifies, and
  the CLI's verdict line (`scope differs from the dimensions`) reports it. Between 70 % and
  95 % a plan verifies but is not counted in scope. The advice on a failed run explores single
  changes only (one battery, one spare, one deadline): a battery figure is the smallest one, to
  50 Wh, under which CP-SAT still finds a schedule for that one change, in the planning model's
  accounting (work plus transit allowance); a candidate whose verification solve times out is
  reported as untried, never as refuted.
* **Strict JSON-schema decoding and small models.** Under `response_format: json_schema` Lightning
  occasionally drifts into Python-style quoting and the grammar keeps the output valid JSON with
  scrambled keys; the structure stage salvages the `operations` it can recognise and reports it,
  but a scrambled dimension would be lost (it is then flagged as a structure without dimensions).
