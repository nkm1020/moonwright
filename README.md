# Moonwright

**Natural-language construction briefs → solver-verified multi-robot schedules for the lunar surface.**

Moonwright is a planning agent for crews of autonomous construction robots. A mission planner writes a
brief in plain English ("build a 20 m landing pad and a 60 m blast berm with two excavators, two
haulers, a compactor and a printer within 14 days"). NVIDIA **Nemotron** (served by **Nebius Token
Factory**) turns the brief into structured requirements and proposes a task graph. A deterministic
validator and an **OR-Tools CP-SAT** model then prove the plan is executable: every precedence edge,
one task at a time per robot, one robot per safety zone, per-robot battery budgets and the deadline.
When the solver says *infeasible*, it names the conflicting constraint groups (an infeasibility core)
and Nemotron repairs the graph. Only solver-verified plans reach the user. The verified plan runs in a
2D simulator; when a robot fails mid-mission, the agent detects it and replans the remaining work.

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
   │  F1  Nemotron (structure)          → Requirements JSON (site, structures, robots, horizon)
   ▼
Requirements
   │  F2  Nemotron (task graph)         → tasks: duration, energy, robot type, zone, precedence
   ▼
TaskGraph ──► F3 validator (cycles, references, capability, energy, critical path)
   │               │ conflicts
   ▼               ▼
F4 CP-SAT ───► infeasible? ── F5 Nemotron repair (≤ 3 rounds, fed the infeasibility core) ──┐
   │ feasible                                                                               │
   ▼                                                                                        │
Schedule + F6 explanation ──► F7 2D simulator ──► event (robot failure, low battery) ──► F8 replan ─┘
```

* **Models (Nebius Token Factory, OpenAI-compatible API).** Short, frequent structuring calls go to
  `nvidia/Nemotron-3_5-Lightning`; task-graph generation and repair go to
  `nvidia/Nemotron-3-Ultra-550b-a55b`; plan explanations use `nvidia/nemotron-3-super-120b-a12b`.
  Routing is configurable through environment variables (see `backend/.env.example`).
* **Solver.** CP-SAT minimises makespan with optional intervals per (task, robot), `AddNoOverlap` per
  robot and per safety zone, linear battery budgets per robot and a horizon bound. Each constraint
  group sits behind an assumption literal; on infeasibility the solver's sufficient-assumption core is
  shrunk by deletion to a minimal set and returned as machine-readable conflict ids such as
  `energy:EX1` or `precedence:T4->T2`.
* **Repair loop.** Conflict ids plus a human hint go back to Nemotron with the previous graph. The
  loop stops at the first solver-verified plan or after three repairs.
* **Simulation and replanning.** The simulator moves robots on the site grid, drains batteries and
  injects failures. On an event the remaining work is re-planned from the task-graph stage and
  re-verified by the solver; the UI shows the before/after schedules.
* **Robustness (Monte Carlo, F9).** The verified plan is replayed N times with random robot
  failures (exponential per-robot hazard) and task-duration jitter, replanning on every event, and
  reports the probability of finishing within the horizon, finish-time percentiles and a histogram.
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
| `moonwright plan <brief.md>` | brief → requirements → task graph → validate → CP-SAT → repair loop → explanation; writes `out/<id>.plan.json` |
| `moonwright simulate <plan.json> --fail EX2@30` | replays the plan in the 2D simulator, injects a robot failure at t = 30 h, replans, continues; writes `out/<id>.sim.json` |
| `moonwright montecarlo <plan.json> --runs 50 --seed 1` | replays the plan N times with random robot failures (`--failure-rate` per robot-hour, default 0.002) and duration jitter (`--jitter`, default 0.15), `--max-replans` per run; prints completion / within-horizon rates, finish percentiles and a histogram; writes `out/<id>.mc.json` |
| `moonwright eval --briefs examples/briefs --loop both` | runs every brief with the repair loop on and off and reports feasible-plan rate, repairs, makespan, latency, tokens |
| `moonwright serve` | FastAPI on :8000 (endpoints below); offline fixtures when `NEBIUS_API_KEY` is unset |

## HTTP API (`backend/moonwright/api.py`)

| Endpoint | Body -> response |
| --- | --- |
| `GET /health` | `{status, version, llm, examples}`; `llm` is the client kind the server uses (`fixture` offline) |
| `GET /examples`, `GET /examples/{id}` | the example briefs `{id, title, text}` (404 for an unknown id) |
| `POST /structure` | `{brief, brief_id?, llm?}` -> `{requirements, step, key}`; F1 only, so the user can correct the requirements before planning (200 with `requirements: null` when the model fails) |
| `POST /plan` | `{brief}` **or** `{requirements}` (+ `brief_id?, max_repairs?, llm?, solver_time_limit_s?, explain?`) -> `PlanResult` (200 with `success: false` when no verified plan was found) |
| `POST /plan/stream` | same body, `text/event-stream`: one `step` event per loop step as it happens, `: ping` keep-alives, then exactly one `result` or `error` event |
| `POST /simulate` | `{plan, failures: [{robot, t_h}], max_replans?, max_repairs?, dt_h?, frame_stride?, brief_id?, llm?}` -> `SimResult` (frames, events, replans); 400 when the plan has no verified schedule |
| `POST /montecarlo` | `{plan, runs? (1-200), failure_rate_per_robot_h?, duration_jitter?, seed?, max_replans? (0-5), brief_id?}` -> `MonteCarloResult` (rates, finish statistics, histogram, per-run samples whose `failures` can be posted to `/simulate` to replay that run) |

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

The core experiment toggles one variable, the repair loop (F5), over a set of test briefs with
different task counts, fleet sizes and battery margins. Metrics: feasible-plan rate, repairs to
feasibility, makespan, latency and tokens per run. A second measurement feeds Nemotron-written
schedules (no solver) to the same checker to count constraint violations. Results will be published
here once measured; numbers are never quoted before they exist.

## Repository layout

```
backend/
  moonwright/schemas.py        shared data contract (Requirements, TaskGraph, Schedule, Failure, Event …)
  moonwright/llm/              Token Factory client, offline fixture client, prompts
  moonwright/pipeline/         structure → taskgraph → validate → schedule (CP-SAT) → loop → explain
  moonwright/sim/              2D simulator, replanning and Monte Carlo robustness
  moonwright/cli.py, api.py    command line and FastAPI
  examples/briefs/             example briefs; fixtures/ holds recorded model outputs for offline runs
  tests/
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
