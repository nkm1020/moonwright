# Deployment notes

The PRD leaves the backend host undecided (Nebius Serverless Endpoint preferred). The frontend
goes to Vercel. Nothing below has been executed yet; it is the plan plus the pieces that are
already in the repo.

## Frontend (Vercel)

- Import the GitHub repo in Vercel, set **Root Directory** to `frontend` (Next.js is detected,
  pnpm from `packageManager`).
- Environment variable: `NEXT_PUBLIC_API_URL=https://<backend-host>` (no trailing slash
  needed; both forms work).
- The UI also works without a backend for review: the "Load sample result" link reads the JSON
  under `frontend/public/samples/`.

## Backend options

The backend is a single FastAPI process (`moonwright.api:app`) with OR-Tools; CPU only, ~300 MB
image, cold start a few seconds. Planning requests take seconds offline and minutes with
Nemotron 3 Ultra reasoning, so the host must allow request timeouts of at least 5 minutes
(or use `POST /plan/stream`, which keeps the connection alive with pings).

1. **Nebius Serverless Endpoint** (preferred by the PRD; to be checked in the Nebius console
   whether custom containers are accepted and what the request timeout is). Build with the
   Dockerfile in `backend/`.
2. **Any container host** (Fly.io, Railway, Render, Google Cloud Run): the same image.
   Set `PORT` if the platform injects one (the image honours it), `NEBIUS_API_KEY`, optional
   `MOONWRIGHT_THINKING=off` for a snappier demo, and
   `MOONWRIGHT_CORS_ORIGINS=https://<your-vercel-app>.vercel.app` (comma-separated list;
   localhost origins are always allowed).
3. **Local backend + tunnel** as a fallback for the demo video (not for the demo URL).

Build and run locally:

```bash
cd backend
docker build -t moonwright-backend .
docker run --rm -p 8000:8000 --env-file .env moonwright-backend      # or without --env-file for fixture mode
curl http://localhost:8000/health
```

## Checklist before the demo URL goes into the submission

- [ ] `GET /health` from the Vercel origin returns 200 and `llm: nebius`.
- [ ] The three example briefs plan end to end from the deployed UI (fixture fallback off).
- [ ] A simulate run with `EX2@30` on the landing pad completes with one replan.
- [ ] Request timeout on the host is >= 300 s or the UI uses the streaming endpoint.
- [ ] `NEBIUS_API_KEY` is set only on the backend host, never in the frontend env.
