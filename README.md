# Moonwright

**Natural-language construction briefs → solver-verified multi-robot schedules for the lunar surface.**

Moonwright is a planning agent for crews of autonomous construction robots. A mission planner writes a
brief in plain English ("build a 20 m landing pad and a 60 m blast berm with two excavators, two
haulers, a compactor and a printer within 14 days"). NVIDIA **Nemotron** (served by **Nebius Token
Factory**) turns the brief into structured requirements and proposes a task graph. A deterministic
validator and an **OR-Tools CP-SAT** model then prove the plan is executable: every precedence edge,
one task at a time per robot, one robot per safety zone, per-robot battery budgets and the deadline.
When the solver says *infeasible*, it names the conflicting constraint groups (an infeasibility core)
and Nemotron repairs the graph. Only plans the solver has verified against the planning model reach
the user. The verified plan runs in a 2D simulator; when a robot fails mid-mission, the agent detects
it, reschedules the remaining work with the solver and escalates to Nemotron only when the remaining
fleet provably cannot finish.

Why this matters: Earth–Moon latency (~1.3 s one way) rules out teleoperating a construction crew. The
plan has to be right *before* execution, and it has to be repaired autonomously when reality diverges.
LLMs alone produce plausible but unexecutable plans; solvers alone need an expert to write the model.
Moonwright puts the LLM where it is strong (structuring, decomposition, repair) and the solver where it
is strong (proof of feasibility).

> Built for the [Nebius × NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/)
> (Physical AI track, simulation-based). Submission deadline: Oct 30 2026 10:00 PDT.

## How it works

```
brief (text)
   │  F1  Nemotron (structure)          → Requirements JSON (site, zones, structures, robots, horizon)
   ▼
Requirements
   │  F2  Nemotron (task graph)         → tasks: what and how much (quantity, unit, basis), robot type,
   │      + deterministic rate model      zone, precedence → hours and Wh computed from the rate table
   ▼
TaskGraph ──► F3 validator (cycles, references, capability, energy, critical path, coverage: phases + the brief's operations)
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
* **Rate model (physical grounding).** The model states *what* and *how much* for every task
  (`quantity`, `unit`, `basis`, e.g. `126 m3 = pi*(10 m)^2*0.4 m`); a six-row rate table in
  `backend/moonwright/pipeline/rates.py` turns the quantity into whole hours and Wh (7 m3/h and
  16 Wh/m3 for bulk excavation, 14 m2/h and 8.6 Wh/m2 for sintering, ...), so a plan cannot be made
  feasible by shrinking numbers: an under-scoped graph becomes a real solver conflict and a repair has
  to split or re-scope work. The units are illustrative lunar-rated low-power figures, every row an
  assumption with its note (see Limitations). The model's own estimate is kept beside the computed
  figure and an `estimate:<task>` advisory flags a gap of more than 50 %; tasks without a quantity
  keep the model's hours. A `volume:<structure>` advisory compares the m3 a structure's tasks plan
  with the volume its dimensions imply (a 360 m3 borrow strip planned as 600 m2 of stripping, a
  300 m3 berm hauled twice over, a 57.6 m3 key trench "repaired" with two 5 m3 tasks), so a
  re-scope the model made to fit a battery is visible in the trace instead of silent. It resolves
  structure tags exactly as the coverage check does (id, type, label, zone; a haul placed in the
  berm's zone builds the berm), prices a berm's key trench (`trench_width_m` x `trench_depth_m`)
  as its own dug shape, judges whole plans only (never a replan's remaining work), and
  `moonwright plan` prints it under its verdict line (`verified against the planning model;
  scope differs from the dimensions`).
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
  schedule replays in the simulator without overrunning itself. Each constraint group sits behind an
  assumption literal; on infeasibility the solver's sufficient-assumption core is shrunk by deletion
  to a minimal set and returned as machine-readable conflict ids such as `energy:EX1` or
  `precedence:T4->T2`, and an independent checker re-verifies every schedule (the verification
  receipt in the trace).
* **Repair loop.** Conflict ids plus a human hint go back to Nemotron with the previous graph. The
  loop stops at the first solver-verified plan or after three repairs. The validator's structure
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

## Quick start

### Backend (Python 3.13, [uv](https://docs.astral.sh/uv/))

```bash
cd backend
uv sync
# offline demo (no API key needed; uses recorded Nemotron outputs under fixtures/)
uv run moonwright plan examples/briefs/landing_pad.md --llm fixture
uv run moonwright plan examples/briefs/tight_battery.md --llm fixture      # shows one repair round
uv run moonwright simulate out/landing_pad.plan.json --fail EX2@30         # failure + replan
uv run moonwright montecarlo out/landing_pad.plan.json --runs 50 --seed 1  # robustness: P(finish in time)
uv run pytest
```

Live mode against Nebius Token Factory:

```bash
cp .env.example .env            # then set NEBIUS_API_KEY
uv run moonwright plan examples/briefs/landing_pad.md --llm nebius
uv run moonwright plan examples/briefs/landing_pad.md --llm record   # live + saves fixtures
uv run moonwright serve                                              # FastAPI on :8000
```

Environment variables: `NEBIUS_API_KEY`, `NEBIUS_BASE_URL` (default
`https://api.tokenfactory.nebius.com/v1/`; the us-central1 endpoint is tried automatically when a
model answers 404 there), `MOONWRIGHT_MODEL_STRUCTURE|TASKGRAPH|REPAIR|EXPLAIN`,
`MOONWRIGHT_THINKING` (`auto` = only task-graph generation and repair reason; `off` is the fast
demo setting), `MOONWRIGHT_LLM` (`fixture` | `nebius` | `record`). Prices, token budgets and
timeouts are documented in `backend/.env.example`.

### Frontend (Next.js 16, pnpm)

```bash
cd backend && uv run moonwright serve        # FastAPI on http://localhost:8000 (offline fixtures without a key)
cd frontend
pnpm install
cp .env.example .env.local                  # NEXT_PUBLIC_API_URL=http://localhost:8000 (the backend's address)
pnpm dev                                    # http://localhost:3000
```

`NEXT_PUBLIC_API_URL` is inlined into the browser bundle at build time, so restart `pnpm dev` after
changing it (it can also be set in the shell: `NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 pnpm dev`).
The page runs the whole flow: brief (or one of the three example briefs) -> editable requirements
card -> streamed progress trace (structure -> task graph -> validate -> schedule -> repair n) ->
verified plan (Gantt, task table, explanation) -> 2D replay with injected failures and
before/after replans -> Monte Carlo robustness. "Load sample result" shows a recorded run without a
backend. `/dev/gantt` and `/dev/sim` are component galleries over the bundled sample payloads.

## CLI

| Command | What it does |
| --- | --- |
| `moonwright plan <brief.md>` | brief → requirements → task graph → validate → CP-SAT → repair loop → explanation; writes `out/<id>.plan.json`; when no plan is found it prints the solver's advice (`what would make it feasible:`); `--max-repairs 0` is the loop-off setting |
| `moonwright simulate <plan.json> --fail EX2@30` | replays the plan in the 2D simulator, injects a robot failure at t = 30 h, replans, continues; writes `out/<id>.sim.json` |
| `moonwright montecarlo <plan.json> --runs 50 --seed 1` | replays the plan N times with random robot failures (`--failure-rate` per robot-hour, default 0.002) and duration jitter (`--jitter`, default 0.15), `--max-replans` per run; prints completion / within-horizon rates, finish percentiles and a histogram; writes `out/<id>.mc.json` |
| `moonwright eval --briefs examples/eval --loop both` | runs every brief with the repair loop on and off and reports feasible-plan rate (Wilson 95 % CI), first-try feasibility, repairs, makespan, scope drift, p50/p95 latency, tokens and cost at Token Factory list prices; `--models ultra,super` adds one set of conditions per model arm for the task-graph and repair roles (named `ultra/loop_on`, ...), `--thinking on\|off\|auto`, `--markdown PATH` writes the results document (per-condition table, per-brief matrix, how to read); `--repeat N`, `--workers N` (parallel runs, rows flushed to `--csv` as they finish), `--feedback core\|verdict` (ablation: the repair round gets the minimal core with hints, or only the verdict), `--max-answer-retries N` (schema / transport re-asks, both conditions) |
| `moonwright serve` | FastAPI on :8000 (endpoints below); offline fixtures when `NEBIUS_API_KEY` is unset |

## HTTP API (`backend/moonwright/api.py`)

| Endpoint | Body -> response |
| --- | --- |
| `GET /health` | `{status, version, llm, examples}`; `llm` is the client kind the server uses (`fixture` offline) |
| `GET /examples`, `GET /examples/{id}` | the example briefs `{id, title, text, text_markdown}` (`text` is plain prose, `text_markdown` the file as written; 404 for an unknown id) |
| `POST /structure` | `{brief, brief_id?, llm?}` -> `{requirements, step, key}`; F1 only, so the user can correct the requirements before planning (200 with `requirements: null` when the model fails) |
| `POST /plan` | `{brief}` **or** `{requirements}` (+ `brief_id?, max_repairs?, llm?, solver_time_limit_s?, explain?`) -> `PlanResult` (200 with `success: false` when no verified plan was found) |
| `POST /plan/stream` | same body, `text/event-stream`: one `step` event per loop step as it happens, `: ping` keep-alives, then exactly one `result` or `error` event |
| `POST /simulate` | `{plan, failures: [{robot, t_h}], max_replans?, max_repairs?, dt_h?, frame_stride?, brief_id?, llm?}` -> `SimResult` (frames, events, replans); 400 when the plan has no verified schedule |
| `POST /montecarlo` | `{plan, runs? (1-200), failure_rate_per_robot_h?, duration_jitter?, seed?, max_replans? (0-5), max_repairs?, llm?, brief_id?}` -> `MonteCarloResult` (rates with a Wilson 95 % CI, finish statistics, histogram, outcomes, single points of failure, per-run samples whose `failures` can be posted to `/simulate` to replay that run) |

Bodies that fail validation answer 422; well-formed but unusable input (a tampered schedule, an
unknown robot) answers 400 with a `detail` string; anything unexpected is a JSON 500 with a
`detail` (sent with CORS headers, so the UI shows the error instead of "cannot reach the
backend").

CORS allows every `http://localhost:*` / `http://127.0.0.1:*` origin out of the box. A deployed
UI needs its origin allowed explicitly, with either or both of:

```bash
# exact origins: scheme + host [+ port], comma-separated, no path (a trailing slash is tolerated)
MOONWRIGHT_CORS_ORIGINS="https://moonwright.vercel.app,https://moonwright-staging.vercel.app"
# a pattern, e.g. every Vercel preview deployment (each preview has its own *.vercel.app origin)
MOONWRIGHT_CORS_ORIGIN_REGEX='https://.*\.vercel\.app'
```

A page served over `https://` cannot call a plain `http://` backend (browsers block mixed
content), so a Vercel-hosted frontend needs the backend behind TLS and `NEXT_PUBLIC_API_URL`
set to that `https://` URL at build time.

## Evaluation

The core experiment toggles one variable, the repair loop (F5), over 22 test briefs
(`backend/examples/eval/`, five kinds documented with their arithmetic in
[`docs/eval-briefs.md`](docs/eval-briefs.md): comfortable, battery-split, horizon-bound,
impossible, scope reduction allowed), with Nemotron 3 Ultra and Nemotron 3 Super as model arms.
Metrics: feasible-plan rate (Wilson 95 % CI) and the share that is also *in scope* (no
`volume:<structure>` advisory on the final graph, so an impossible brief "verified" by digging a
third of the pad never counts as a plain success), first-try feasibility, repairs to feasibility,
makespan, p50/p95 latency, tokens and cost per run, per condition and per brief kind
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
  --thinking auto --llm nebius --workers 3 --max-answer-retries 2 \
  --out out/eval_live.json --csv out/eval_live.csv --markdown out/eval_live.md
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
failures. Every Token Factory call is appended to `out/llm_log.jsonl` (`MOONWRIGHT_LLM_LOG`; status,
model, tokens, latency and request id, never prompt or answer text).

## Limitations and known failures

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
* **Under-scoping is flagged, not refused.** A graph that digs 36 % of a structure's volume, or
  hauls twice it, verifies when the batteries and the deadline allow it; the `volume:<structure>`
  advisory makes it visible (in the trace, under the CLI's verdict line and as the eval's
  `feasible in scope` rate) and the user decides. The advice on a failed run explores single
  changes only (one battery, one spare, one deadline): a battery figure is the smallest one, to
  50 Wh, under which CP-SAT still finds a schedule for that one change, in the planning model's
  accounting (work plus transit allowance); a candidate whose verification solve times out is
  reported as untried, never as refuted.
* **Strict JSON-schema decoding and small models.** Under `response_format: json_schema` Lightning
  occasionally drifts into Python-style quoting and the grammar keeps the output valid JSON with
  scrambled keys; the structure stage salvages the `operations` it can recognise and reports it,
  but a scrambled dimension would be lost (it is then flagged as a structure without dimensions).

## Repository layout

```
backend/
  moonwright/schemas.py        shared data contract (Requirements, TaskGraph, Schedule, Failure, Event …)
  moonwright/llm/              Token Factory client, offline fixture client, prompts
  moonwright/pipeline/         structure → taskgraph → validate → schedule (CP-SAT) → loop → explain
  moonwright/sim/              2D simulator, replanning and Monte Carlo robustness
  moonwright/cli.py, api.py    command line and FastAPI
  examples/briefs/             example briefs; fixtures/ holds recorded model outputs for offline runs
  examples/eval/               the 22 evaluation briefs (docs/eval-briefs.md: kind, margins, expected outcome)
  scripts/                     brief_budget, replan_eval, llm_log_report, llm_schedule_check (evaluation tooling)
  tests/
docs/                          phase plan, eval briefs, live-run runbook, architecture diagram, Devpost draft
frontend/                      Next.js UI: brief input, requirements card, streamed trace, Gantt, 2D replay,
                               before/after replans, Monte Carlo panel (src/components), API client (src/lib)
```

## Hackathon compliance

* Runtime calls to the Nebius Token Factory inference API (OpenAI-compatible) with NVIDIA Nemotron
  open models; see `backend/moonwright/llm/tokenfactory.py`.
* Physical AI track, simulation-based (allowed by the official FAQ); the demo video shows the key
  modules (planning, solver verification, repair, simulated failure and replanning) in action.
* Open-source license in the repository root; README with setup instructions (this file).

## License

MIT (see `LICENSE`).
