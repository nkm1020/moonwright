# Moonwright: reference

Commands, endpoints and settings. See the [README](../README.md) for the overview and
[design.md](design.md) for how each stage works.

## Setup

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
cp .env.example .env            # then set NEBIUS_API_KEY (an .env from before phase 3e: delete
                                # its MOONWRIGHT_THINKING_MAX_TOKENS=16384 line)
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
| `moonwright eval --briefs examples/eval --loop both` | runs every brief with the repair loop on and off and reports feasible-plan rate (Wilson 95 % CI), first-try feasibility, repairs, makespan, scope drift, p50/p95 latency, tokens and cost at Token Factory list prices; `--models ultra,super` adds one set of conditions per model arm for the task-graph and repair roles (named `ultra/loop_on`, ...), `--thinking on\|off\|auto`, `--markdown PATH` writes the results document (per-condition table, per-brief matrix, how to read); `--repeat N`, `--workers N` (parallel runs, rows flushed to `--csv` as they finish), `--feedback core\|verdict` (ablation: the repair round gets the minimal core with hints, or only the verdict), `--max-answer-retries N` (schema / transport re-asks, both conditions), `--save-plans DIR` (every run's PlanResult as `DIR/<brief>__<arm>__<loop>__<repeat>.plan.json`, `plan_path` in the rows, plus each job's structuring), `--resume` (keep the rows an interrupted run left in `--csv`, run only the missing brief x condition x repeat cells, summarise old and new together) |
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
