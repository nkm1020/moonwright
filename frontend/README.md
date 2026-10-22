# Moonwright web UI

The mission console for Moonwright: verified multi-robot construction planning for the lunar
surface. A single scrolling page walks through the PRD flow:

1. **Brief** – type a construction brief or pick one of the example briefs (`GET /examples`),
   then *Structure brief* (`POST /structure`) asks Nemotron for structured requirements.
2. **Requirements** – site, grid, horizon, structures (type + dimensions), robots (battery,
   speed) and constraints are editable; numbers are validated client-side before planning.
3. **Plan** – *Plan with these requirements* streams the loop over `POST /plan/stream`
   (server-sent events) into a live progress trace: structure → task graph → validate →
   schedule → repair *n* → explain, with conflict chips and hints on failed steps. The
   verified result shows the Gantt chart, the task table, a plan summary (per-robot load and
   battery budget) and the model's explanation.
4. **Simulate** – inject robot failures and run `POST /simulate`; the 2D replay, the event
   list and one before/after comparison per replan show how the agent recovered.
5. **Robustness** – `POST /montecarlo` replays the plan many times with random failures and
   duration jitter; the panel reports the probability of finishing within the horizon and a
   finish-time histogram.

The sticky top bar shows the stage stepper and the backend mode from `GET /health`
(*offline fixtures* vs *Nemotron live*). **Load sample result** loads the recorded
`public/samples/landing_pad.plan.json` and `landing_pad.sim.json`, so the whole page can be
reviewed without a backend.

## Running it

```bash
pnpm install
cp .env.example .env.local        # NEXT_PUBLIC_API_URL=http://localhost:8000
pnpm dev                          # http://localhost:3000
```

Start the backend next to it (offline fixtures need no API key):

```bash
cd ../backend
MOONWRIGHT_LLM=fixture uv run moonwright serve --port 8000
```

With `NEBIUS_API_KEY` set in the backend's environment the same UI runs against Nemotron on
Nebius Token Factory; the top-bar badge reflects which client the server uses.

## Environment

| Variable              | Default                 | Meaning                                              |
| --------------------- | ----------------------- | ---------------------------------------------------- |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Base URL of the FastAPI backend (CORS allows localhost) |

`NEXT_PUBLIC_*` values are inlined into the browser bundle at build time: restart `pnpm dev`
or rebuild after changing them.

## Code map

```
src/app/            layout (fonts, metadata), page (renders <App/>), globals.css (tokens)
src/lib/types.ts    data contract mirrored from backend/moonwright/schemas.py
src/lib/api.ts      typed fetch helpers for every endpoint (ApiError carries the server detail)
src/lib/sse.ts      SSE parser + planStream() with a POST /plan fallback when /plan/stream is missing
src/lib/format.ts   hours / seconds / tokens / dimension formatting
src/lib/palette.ts  shared colours (robot types, zones, statuses, events, stage labels)
src/lib/tasks.ts    task helpers (allTasks / tasksById / zoneOrder)
src/components/     App (state machine), TopBar, BriefForm, RequirementsCard, ProgressTrace,
                    ExplanationCard, MonteCarloPanel, Section (primitives), Gantt, TaskTable,
                    SimReplay, SimControls, EventList, BeforeAfter
```

## Checks

```bash
pnpm exec tsc --noEmit
pnpm lint
pnpm build
```

Interactive UI lives in client components rendered from the server page files
(`cacheComponents` is enabled in `next.config.ts`); there is no server-side data fetching.
