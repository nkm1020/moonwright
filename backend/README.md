# Moonwright backend

Natural-language construction brief -> Nemotron (Nebius Token Factory) proposes a task graph ->
deterministic validator + OR-Tools CP-SAT verify and schedule it -> infeasibility cores go back to
Nemotron for repair -> verified schedule runs in a 2D simulator that replans on robot failure.

See the repository root README for the full story. Quick start:

    uv sync
    uv run moonwright plan examples/briefs/landing_pad.md --llm fixture --out out/landing_pad.plan.json
    uv run moonwright simulate out/landing_pad.plan.json --fail EX2@30 --llm fixture
    uv run moonwright montecarlo out/landing_pad.plan.json --runs 50 --seed 1 --out out/landing_pad.mc.json
    uv run moonwright eval --briefs examples/briefs --llm fixture --out out/eval.json --csv out/eval.csv
    uv run moonwright serve                      # GET /health /examples; POST /structure /plan /plan/stream /simulate /montecarlo
    uv run pytest && uv run ruff check .

Commands: `plan` (brief -> verified schedule), `simulate` (replay with failures and replanning),
`montecarlo` (N replays with random failures and duration jitter; solver-only replans unless
`--llm` is given), `eval` (repair loop off/on over a directory of briefs), `serve` (FastAPI).
Every command answers `--help`; `--llm fixture|nebius|record` picks the model client (default:
`MOONWRIGHT_LLM`, else `nebius` when `NEBIUS_API_KEY` is set, else `fixture`).
