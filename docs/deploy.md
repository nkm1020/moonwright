# Deployment

The public demo is the site in `design/landing-1` on GitHub Pages, at
**https://nkm1020.github.io/moonwright/**. It is plain files with no build and no backend: it
replays the three runs recorded live on Nebius Token Factory on Oct 24 2026, scene by scene, so it
costs nothing to host, needs no API key and cannot go down mid-review. Live planning needs the
backend; the last section covers hosting one.

## What the site does

- `index.html`, the overview: the landing pad run's verified schedule as a board, the three runs,
  how it works (with the crest slider that recomputes PR1's energy check in the page), the
  evaluation figures and why the Moon.
- `run.html`, the runs: each recorded run scene by scene (the brief, the refusal, the repairs, the
  proof, the failure with the Isaac Sim clip, the odds), with a Picture/Numbers switch.
- `run.html#landing_pad/break`, "Your turn": pick any robot and any hour, break it, and watch the
  replan in 3D. On Pages it reads `data/landing_pad.failures.json` and
  `data/landing_pad.failures.frames.json`, all 504 breakdowns computed ahead by the same
  `/simulate` code path (`backend/scripts/failure_table.py`), and says so. Opened from
  localhost with the backend running on :8074, it calls `/simulate` and CP-SAT replans the
  breakdown on the spot.

Every figure on the site is read from the recorded runs (`frontend/public/samples/`) and the
evaluation (`docs/eval/`).

## Check it locally

```bash
cd design/landing-1 && python3 -m http.server 3074        # http://127.0.0.1:3074/
# optional, for the live "Your turn": in another shell
cd backend && MOONWRIGHT_LLM=fixture uv run moonwright serve --port 8074
```

The Next.js app in `frontend/` is the full pipeline's console (editable requirements, the
streamed trace, live planning on Token Factory). It is not deployed; run it with the backend
(README, "Run it"). It still builds as a static export with `MOONWRIGHT_STATIC=1
NEXT_PUBLIC_STATIC_DEMO=1 NEXT_PUBLIC_BASE_PATH=/moonwright pnpm build` if needed.

## The workflow

`.github/workflows/pages.yml` uploads `design/landing-1` as the Pages artifact with
`actions/upload-pages-artifact` and publishes it with `actions/deploy-pages`; there is no build
step. Every action is pinned to a commit SHA (the tag in a comment). A concurrency group keeps one
deployment at a time. It runs on `workflow_dispatch` only for now; once the repo is public, add
`push: branches: [main]` (the comment in the file marks the place) so every merge redeploys.

## Turning on Pages (once the repo is public, Oct 29-30)

Pages on a private repository needs a paid plan, so the demo goes live with the repo:

1. Make the repository public.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Actions → Pages → Run workflow** on `main` (or add the push trigger and push).
4. Open https://nkm1020.github.io/moonwright/ in a private window and check: the overview, each
   run's scenes, "Your turn" (break HL2 at hour 24: finished at 72 h, the 3D replay plays), no
   errors in the browser console, and a phone width without sideways scrolling.

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
