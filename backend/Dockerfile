# Moonwright backend: FastAPI + OR-Tools CP-SAT, served by uvicorn.
# Build:  docker build -t moonwright-backend .
# Run:    docker run --rm -p 8000:8000 -e NEBIUS_API_KEY=... moonwright-backend
# Without NEBIUS_API_KEY the server runs in offline fixture mode (the three example briefs).
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first (cached layer), then the package.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY moonwright ./moonwright
COPY fixtures ./fixtures
COPY examples ./examples
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    MOONWRIGHT_CORS_ORIGINS=""

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "uvicorn moonwright.api:app --host 0.0.0.0 --port ${PORT}"]
