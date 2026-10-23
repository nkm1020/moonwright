# Deployment

The public demo is a static export of the frontend on GitHub Pages, at
**https://nkm1020.github.io/moonwright/**. It has no backend: it replays the live runs recorded on
Nebius Token Factory on Oct 23 2026 (the files under `frontend/public/samples/`: the live
PlanResults, and the simulation and Monte Carlo the CLI computed offline from the landing-pad
plan), so it costs nothing to host, needs no API key and cannot go down mid-review. Live planning
needs the backend; the last section covers hosting one.

## What the static demo does

The first view is the landing-pad brief on step 01. Each stage's primary action replays the next
step of the recorded run, so one button at a time walks the whole loop:

| Step | Primary action | What it shows |
| --- | --- | --- |
| 01 Brief | Replay structuring | the requirements Nemotron 3.5 Lightning structured (model, tokens, latency) |
| 02 Requirements | Replay planning | the trace: Nemotron 3 Ultra's graph, the `energy:printer` core, the repair diff, the receipt, the verified Gantt |
| 04 Simulate | Replay the simulation | EX1 fails at 10 h, CP-SAT replans alone, the run finishes at 79 h |
| 05 Robustness | Replay Monte Carlo | 200 runs, the single points of failure, and the recorded what-if with one spare of each |

The top bar loads a whole sample in one click: **Landing pad** (everything above at once) and
**Impossible deadline** (the 60 h brief: three repair rounds, no plan, and the CP-SAT-proven
advice of an 85 h deadline). Every figure on the example cards and in the tooltips is read from
those files.

Whatever would need a new request is locked, with a one-line reason: structuring or planning a
brief of your own, editing the requirements (Re-structure is not offered), applying a proven
change, injecting another failure, other Monte Carlo parameters or another fleet. Once the walk
has moved on, the loaded example card reads "start again" and restarts it from the brief. The
page makes no request to any backend; the only fetches are the sample files and the app's own
assets.

A sample is one line in `SAMPLES` (`frontend/src/lib/api.ts`) plus its files in
`frontend/public/samples/`: `<id>.plan.json` always, and `<id>.sim.json`, `<id>.mc.json`,
`<id>.whatif.mc.json` where the line declares them (declared, not probed, so the static host never
answers a 404). Only the plan is required: a declared file that does not load leaves a one-line
note where its panel would be (the simulation, the Monte Carlo, the what-if button), and the rest
of the walk works.

## Build it locally

Three variables switch the build to the static demo:

| Variable | Value | Effect |
| --- | --- | --- |
| `MOONWRIGHT_STATIC` | `1` | `next.config.ts`: `output: "export"`, the base path, `trailingSlash`, unoptimized images; Cache Components and Partial Prefetching off (an export cannot carry them) |
| `NEXT_PUBLIC_STATIC_DEMO` | `1` | the console replays the samples and never calls a backend |
| `NEXT_PUBLIC_BASE_PATH` | `/moonwright` | the path the site is served under (the repo name on Pages); sample fetches add it too |

```bash
cd frontend
MOONWRIGHT_STATIC=1 NEXT_PUBLIC_STATIC_DEMO=1 NEXT_PUBLIC_BASE_PATH=/moonwright pnpm build
# -> frontend/out/ (index.html, samples/, _next/)
```

Serve it under the same path Pages uses:

```bash
mkdir -p /tmp/pages && ln -sfn "$PWD/out" /tmp/pages/moonwright
python3 -m http.server 3076 -d /tmp/pages        # http://localhost:3076/moonwright/
```

Without the variables, `pnpm dev` and `pnpm build` are the usual live console with today's
config. `NEXT_PUBLIC_STATIC_DEMO=1 pnpm dev` shows the static demo's behaviour on the dev server
(at `/`, no base path).

## The workflow

`.github/workflows/pages.yml` builds and deploys the demo:

1. checkout, pnpm (the version from `frontend/package.json` `packageManager`), Node 22 with the pnpm
   store cached;
2. `pnpm install --frozen-lockfile` in `frontend/`, then `pnpm build` with the three variables;
3. `out/dev` is removed (the `/dev/gantt` and `/dev/sim` component galleries are not part of the
   demo); `actions/upload-pages-artifact` uploads `frontend/out`, `actions/deploy-pages` publishes
   it (both on the Node 24 action runtime).

Every action is pinned to a commit SHA (the tag in a comment). A concurrency group keeps one
deployment at a time. It runs on `workflow_dispatch` only for now; once the repo is public, add
`push: branches: [main]` (the comment in the file marks the place) so every merge redeploys.

## Turning on Pages (once the repo is public, Oct 29-30)

Pages on a private repository needs a paid plan, so the demo goes live with the repo:

1. Make the repository public.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Actions → Pages → Run workflow** on `main` (or add the push trigger and push).
4. Open https://nkm1020.github.io/moonwright/ and check: the landing-pad brief on the first
   view, the walk through to the Monte Carlo what-if, the Impossible deadline button, no errors
   in the browser console, and a phone width without sideways scrolling.

The deploy job's `github-pages` environment shows the URL of each deployment.

## Optional: a live backend

The backend is a single FastAPI process (`moonwright.api:app`) with OR-Tools; CPU only, cold start
a few seconds. `backend/Dockerfile` builds it; without `NEBIUS_API_KEY` it replays the recorded
answers of the example briefs.

```bash
cd backend
docker build -t moonwright-backend .
docker run --rm -p 8000:8000 -e NEBIUS_API_KEY=... moonwright-backend
curl http://localhost:8000/health
```

- **Host.** Any container host (Nebius, Fly.io, Railway, Render, Cloud Run). Planning with
  Nemotron 3 Ultra reasoning takes minutes: the host must allow long requests, or the UI's
  `POST /plan/stream` keeps the connection alive with pings.
- **Settings.** `NEBIUS_API_KEY` on the backend only, never in the frontend's environment;
  `PORT` if the platform injects one; `MOONWRIGHT_THINKING=off` for a faster demo;
  `MOONWRIGHT_CORS_ORIGINS` with the frontend's exact origin (localhost is always allowed).
- **Frontend.** A live console is the normal build (no static variables) with
  `NEXT_PUBLIC_API_URL=https://<backend-host>`, inlined at build time. The Pages demo cannot call
  a backend: the page is HTTPS and static, and the static build makes no request by design.

Commands, endpoints and every setting: [reference.md](reference.md).
