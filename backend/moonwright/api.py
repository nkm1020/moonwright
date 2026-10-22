"""HTTP surface for the web UI.

Endpoints
---------
``GET /health``          ``{status, version, llm, examples}``
``GET /examples``        ``ExampleBrief[]``; ``GET /examples/{id}`` one brief (404 when unknown).
                         ``text`` is the brief as plain prose (markdown emphasis, inline code
                         and heading markers stripped, for the UI textarea); ``text_markdown``
                         is the file as written
``POST /structure``      brief -> ``StructureResponse``: ``requirements`` is null when the model's
                         answer could not be structured and ``step`` says why; ``key`` is the
                         fixture / recording key of the brief
``POST /plan``           brief *or* user-corrected requirements -> ``PlanResult`` (200 even when
                         ``success`` is false; only configuration problems are HTTP errors)
``POST /plan/stream``    same body, ``text/event-stream``: one ``step`` event per
                         :class:`LoopStep` as the loop produces it, a ``: ping`` comment after
                         :data:`PING_INTERVAL_S` of silence, then exactly one terminal event:
                         ``result`` (a PlanResult) or ``error`` (``{"detail": "..."}``)
``POST /simulate``       verified ``PlanResult`` + failure injections -> ``SimResult``
``POST /montecarlo``     verified ``PlanResult`` + Monte Carlo parameters -> ``MonteCarloResult``:
                         the plan replayed N times with random robot failures and duration
                         jitter (F9); replans are solver-only unless an ``llm`` kind is given

Request and response bodies are the pipeline's own Pydantic models, so the UI reads exactly
what the CLI writes to disk. Planning is CPU- and network-bound, so the endpoints are plain
``def`` functions and FastAPI runs them in its thread pool; the stream runs the planner in a
worker thread and forwards its steps through an asyncio queue. Input problems are 4xx with a
reason: 422 when the body fails validation, 400 when it is well-formed but unusable (both or
neither of brief/requirements, a plan without a verified schedule or whose schedule fails
re-verification, an unknown robot), 503 when the live client cannot be built; anything
unexpected is a 500 with a JSON ``detail`` *with CORS headers*: the four pipeline endpoints
turn their exceptions into an ``HTTPException`` (:func:`_reported`) and
:class:`JsonServerErrors`, mounted inside the CORS middleware, catches whatever any other
endpoint leaks. Starlette's own server-error handler (and an ``@app.exception_handler(Exception)``)
sits *outside* the CORS middleware, so its 500 would reach a cross-origin UI as a bare
"failed to fetch". 422 bodies keep FastAPI's shape, so a bad field inside ``requirements``
is reported with its full path (``["body", "requirements", "robots", 0, "battery_wh"]``).
CORS is open for localhost development; add other origins with the comma-separated
``MOONWRIGHT_CORS_ORIGINS`` variable (exact origins, scheme + host [+ port]; whitespace and a
trailing slash, the natural copy-paste of a deployment URL, are tolerated) and/or a pattern
in ``MOONWRIGHT_CORS_ORIGIN_REGEX`` (e.g. ``https://.*[.]vercel[.]app`` for preview
deployments).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, MutableMapping
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from moonwright.llm.base import LLMClient, LLMError
from moonwright.llm.factory import load_env, make_client, resolve_kind
from moonwright.pipeline.loop import Planner, brief_key
from moonwright.pipeline.schedule import check_schedule
from moonwright.pipeline.structure import structure_brief
from moonwright.schemas import LoopStep, PlanResult, Requirements, SimResult
from moonwright.sim.montecarlo import (
    MonteCarloPlanner,
    MonteCarloResult,
    run_monte_carlo,
    solver_only_planner,
)
from moonwright.sim.replan import run_with_replanning
from moonwright.sim.simulator import FailureInjection

EXAMPLES_DIR: Final = Path(__file__).resolve().parents[1] / "examples" / "briefs"
BRIEF_SUFFIXES: Final = (".md", ".txt")
LOCALHOST_ORIGIN_RE: Final = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
PING_INTERVAL_S: float = 10.0  # SSE keep-alive comment after this much silence (tests shrink it)
STREAM_HEADERS: Final = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

StepCallback = Callable[[LoopStep], None]
log = logging.getLogger(__name__)

try:
    VERSION: Final = version("moonwright")
except PackageNotFoundError:  # running from a plain checkout without an install
    VERSION = "0.0.0"


# --------------------------------------------------------------------------- request models
class PlanRequest(BaseModel):
    """Exactly one of ``brief`` / ``requirements`` (checked by the endpoint: 400 otherwise)."""

    brief: str | None = Field(
        default=None, min_length=1, description="natural-language construction brief"
    )
    requirements: Requirements | None = Field(
        default=None,
        description="structured brief already corrected by the user; skips the structure stage",
    )
    brief_id: str | None = Field(
        default=None, description="stable id; selects fixtures offline and names recordings"
    )
    max_repairs: int = Field(default=3, ge=0, le=10)
    llm: str | None = Field(default=None, description="fixture | nebius | record (default: env)")
    solver_time_limit_s: float = Field(default=10.0, gt=0, le=120)
    explain: bool = True


class StructureRequest(BaseModel):
    brief: str = Field(min_length=1, description="natural-language construction brief")
    brief_id: str | None = Field(
        default=None, description="stable id; selects fixtures offline and names recordings"
    )
    llm: str | None = Field(default=None, description="fixture | nebius | record (default: env)")


class StructureResponse(BaseModel):
    requirements: Requirements | None = Field(
        description="null when the model's answer could not be structured (see step.failure)"
    )
    step: LoopStep
    key: str = Field(description="fixture / recording key derived from brief_id or the brief")


class FailureSpec(BaseModel):
    robot: str = Field(min_length=1)
    t_h: float = Field(
        ge=0, allow_inf_nan=False, description="simulation time (hours) at which the robot fails"
    )


class SimulateRequest(BaseModel):
    plan: PlanResult
    failures: list[FailureSpec] = Field(default_factory=list)
    max_replans: int = Field(default=2, ge=0, le=5)
    max_repairs: int = Field(default=3, ge=0, le=10, description="repair rounds per replan")
    brief_id: str | None = Field(default=None, description="fixture key for offline replans")
    llm: str | None = None
    frame_stride: int = Field(default=4, ge=1, description="keep every n-th frame plus the last")
    dt_h: float = Field(default=0.25, ge=0.05, le=24, allow_inf_nan=False,
                        description="simulation step in hours")


class MonteCarloRequest(BaseModel):
    """Bounds as in ``frontend/src/lib/types.ts``; ``brief_id`` is accepted for symmetry with
    ``/simulate`` but has no effect (Monte Carlo replans never read fixtures). The rates are
    finite numbers in [0, 1] (NaN / inf are 422s, like every bound here)."""

    plan: PlanResult
    runs: int = Field(default=50, ge=1, le=200)
    failure_rate_per_robot_h: float = Field(
        default=0.002, ge=0, le=1, allow_inf_nan=False,
        description="per-robot failure probability per hour",
    )
    duration_jitter: float = Field(
        default=0.15, ge=0, le=1, allow_inf_nan=False, description="relative task-duration spread"
    )
    seed: int | None = None
    max_replans: int = Field(default=2, ge=0, le=5)
    max_repairs: int = Field(
        default=3, ge=0, le=10, description="repair rounds per replan (only with llm)"
    )
    brief_id: str | None = None
    llm: str | None = Field(
        default=None,
        description="replan through this LLM client instead of the solver-only replanner",
    )


class ExampleBrief(BaseModel):
    """An example brief: ``text`` as plain prose for a textarea (:func:`strip_markdown`),
    ``text_markdown`` as the file is written (what the CLI plans from)."""

    id: str
    title: str
    text: str
    text_markdown: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    llm: str
    examples: int


# --------------------------------------------------------------------------- helpers
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_MD_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_MD_EM_STAR = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")
_MD_EM_UNDERSCORE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_MD_CODE = re.compile(r"`([^`\n]+)`")


def strip_markdown(text: str) -> str:
    """``text`` as plain prose for a textarea: ``**bold**`` / ``__bold__``, ``*italic*`` /
    ``_italic_`` (word-bounded, so ``landing_pad`` or ``4_000`` survive), inline ``code`` and
    ATX heading markers are removed; lists, numbering, line breaks and everything else stay."""
    text = _MD_HEADING.sub("", text)
    text = _MD_STRONG.sub(r"\2", text)
    text = _MD_EM_STAR.sub(r"\1", text)
    text = _MD_EM_UNDERSCORE.sub(r"\1", text)
    return _MD_CODE.sub(r"\1", text)


def brief_title(text: str, fallback: str) -> str:
    """First non-empty line, markdown heading markers removed, capped at 80 characters."""
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:80]
    return fallback


def list_briefs(directory: Path = EXAMPLES_DIR) -> list[ExampleBrief]:
    """Example briefs (``*.md`` / ``*.txt``) in ``directory``, sorted by id; [] if absent."""
    if not directory.is_dir():
        return []
    briefs = []
    for path in sorted(directory.iterdir()):
        if path.suffix in BRIEF_SUFFIXES and path.is_file():
            text = path.read_text(encoding="utf-8")
            briefs.append(ExampleBrief(id=path.stem, title=brief_title(text, path.stem),
                                       text=strip_markdown(text), text_markdown=text))
    return briefs


def clean_brief_id(raw: str | None) -> str | None:
    """``raw`` in :func:`brief_key`'s sanitised form, or None when nothing survives (``"###"``),
    so callers fall back to the brief hash or the file stem instead of an empty key."""
    if not raw:
        return None
    return brief_key("", raw) or None


def thin_frames(result: SimResult, stride: int) -> SimResult:
    """Keep every ``stride``-th frame plus the final one; playback needs far fewer than dt gives."""
    if stride <= 1 or len(result.frames) <= 2:
        return result
    frames = result.frames[::stride]
    if frames[-1] is not result.frames[-1]:
        frames.append(result.frames[-1])
    return result.model_copy(update={"frames": frames})


def _client(kind: str | None) -> LLMClient:
    try:
        return make_client(kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


def _cors_origins() -> list[str]:
    """``MOONWRIGHT_CORS_ORIGINS`` as exact origins: whitespace and a trailing slash (the
    natural copy-paste of a deployment URL) stripped, lower-cased like the ``Origin`` header."""
    raw = os.environ.get("MOONWRIGHT_CORS_ORIGINS", "")
    return [o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip().rstrip("/")]


def _cors_origin_regex() -> str:
    """The localhost pattern, extended with ``MOONWRIGHT_CORS_ORIGIN_REGEX`` when set."""
    extra = os.environ.get("MOONWRIGHT_CORS_ORIGIN_REGEX", "").strip()
    return f"(?:{LOCALHOST_ORIGIN_RE})|(?:{extra})" if extra else LOCALHOST_ORIGIN_RE


@contextmanager
def _reported(what: str) -> Iterator[None]:
    """Answer an unexpected exception with a JSON 500 instead of Starlette's bare one.

    ``HTTPException`` is handled inside the CORS middleware, so the browser gets the error
    with its CORS headers; Starlette's own server-error handler sits outside that middleware
    and a cross-origin UI would only see "failed to fetch".
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as e:
        log.exception("%s failed", what)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class JsonServerErrors:
    """ASGI middleware mounted *inside* the CORS middleware: an exception an endpoint leaks
    becomes a JSON 500 (``{"detail": "<Type>: <message>"}``) whose response still passes
    through CORS, where Starlette's own server-error handler, which sits outside every user
    middleware, would answer without the CORS headers and a browser would report a CORS
    failure instead of the error. A response that has already started (a stream) cannot be
    replaced, so the exception propagates as before."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def guarded_send(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except Exception as e:
            if started:
                raise
            log.exception("%s %s failed", scope.get("method", "?"), scope.get("path", "?"))
            response = JSONResponse(status_code=500,
                                    content={"detail": f"{type(e).__name__}: {e}"})
            await response(scope, receive, send)


def _check_plan_request(req: PlanRequest) -> None:
    if (req.brief is None) == (req.requirements is None):
        raise HTTPException(
            status_code=400, detail="give exactly one of 'brief' or 'requirements'"
        )


def _provided_step(req: Requirements) -> LoopStep:
    """The structure step of a plan that starts from user-corrected requirements."""
    return LoopStep(
        stage="structure", ok=True,
        message=(f"requirements provided by the user: {len(req.structures)} structures, "
                 f"{len(req.robots)} robots, horizon {req.horizon_h}h"),
    )


def _run_plan(req: PlanRequest, client: LLMClient,
              on_step: StepCallback | None = None) -> PlanResult:
    """The loop for ``req``: from the brief, or from user-corrected requirements with a
    synthetic structure step prepended so the trace is uniform. ``on_step`` (streaming) sees
    every step as it is produced, that synthetic step included."""
    options: dict[str, Any] = {}
    if on_step is not None:
        options["on_step"] = on_step
    planner = Planner(client, max_repairs=req.max_repairs,
                      solver_time_limit_s=req.solver_time_limit_s, explain=req.explain,
                      **options)
    brief_id = clean_brief_id(req.brief_id)
    if req.requirements is None:
        return planner.plan(req.brief or "", brief_id=brief_id)
    step = _provided_step(req.requirements)
    if on_step is not None:
        on_step(step)
    key = brief_key(req.requirements.source_brief or "", brief_id)
    result = planner.plan_from_requirements(req.requirements, key=key)
    result.steps.insert(0, step)
    return result


def _verified_plan(plan: PlanResult) -> None:
    """400 unless ``plan`` is a successful plan whose schedule still passes re-verification
    (the body comes from the client, so it may have been edited or truncated on the way)."""
    if not plan.success or plan.schedule is None or plan.task_graph is None:
        raise HTTPException(status_code=400, detail="plan has no verified schedule to simulate")
    conflicts = check_schedule(plan.requirements, plan.task_graph, plan.schedule)
    if conflicts:
        raise HTTPException(
            status_code=400,
            detail="plan schedule fails re-verification: " + ", ".join(conflicts),
        )


def _json_safe(value: Any) -> Any:
    """``value`` with non-finite floats spelled out, so an error report can be serialised:
    FastAPI echoes the offending input in 422 bodies and ``NaN`` is not JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _sse(event: str, data: str) -> str:
    """One server-sent event; ``data`` must be a single line (compact JSON is)."""
    return f"event: {event}\ndata: {data}\n\n"


async def _plan_events(req: PlanRequest, client: LLMClient) -> AsyncIterator[str]:
    """SSE body of ``POST /plan/stream``: the planner runs in a worker thread and posts its
    steps and outcome to a queue; this generator forwards them, pinging while it waits.

    Steps the callback did not deliver (a planner without ``on_step``) are flushed from the
    result before the ``result`` event, so the event order is the same either way.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def post(kind: str, payload: Any) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))
        except RuntimeError:  # loop closed: the client is gone and nobody is listening
            pass

    def work() -> None:
        try:
            result = _run_plan(req, client, on_step=lambda step: post("step", step))
        except Exception as e:  # whatever failed, the stream must end with a terminal event
            log.exception("plan stream failed")
            post("error", f"{type(e).__name__}: {e}")
        else:
            post("result", result)

    threading.Thread(target=work, name="moonwright-plan-stream", daemon=True).start()
    sent = 0
    while True:
        try:
            kind, payload = await asyncio.wait_for(queue.get(), PING_INTERVAL_S)
        except TimeoutError:
            yield ": ping\n\n"
            continue
        if kind == "step":
            sent += 1
            yield _sse("step", payload.model_dump_json())
        elif kind == "result":
            for step in payload.steps[sent:]:
                yield _sse("step", step.model_dump_json())
            yield _sse("result", payload.model_dump_json())
            return
        else:
            yield _sse("error", json.dumps({"detail": payload}))
            return


# --------------------------------------------------------------------------- app
load_env()
app = FastAPI(
    title="Moonwright",
    version=VERSION,
    description="Natural-language lunar construction briefs -> solver-verified multi-robot "
    "schedules (Nemotron on Nebius Token Factory + OR-Tools CP-SAT).",
)
app.add_middleware(JsonServerErrors)   # added first = innermost: its 500s pass through CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422 body, made serialisable when the rejected input holds NaN or inf
    (the default handler would fail to encode it and answer 500)."""
    return JSONResponse(status_code=422,
                        content={"detail": _json_safe(jsonable_encoder(exc.errors()))})


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok", version=VERSION, llm=resolve_kind(), examples=len(list_briefs())
    )


@app.get("/examples", response_model=list[ExampleBrief])
def examples() -> list[ExampleBrief]:
    return list_briefs()


@app.get("/examples/{brief_id}", response_model=ExampleBrief)
def example(brief_id: str) -> ExampleBrief:
    """One example brief; the id is matched in its sanitised (``brief_key``) form."""
    key = clean_brief_id(brief_id)
    for brief in list_briefs():
        if key is not None and clean_brief_id(brief.id) == key:
            return brief
    raise HTTPException(status_code=404, detail=f"no example brief {brief_id!r}")


@app.post("/structure", response_model=StructureResponse)
def structure(req: StructureRequest) -> StructureResponse:
    """F1 only: structure the brief so the user can correct it before planning.

    A brief the model cannot structure still returns 200 with ``requirements`` null and the
    failed step; only configuration problems are HTTP errors.
    """
    client = _client(req.llm)
    key = brief_key(req.brief, clean_brief_id(req.brief_id))
    with _reported("structure"):
        requirements, step = structure_brief(client, req.brief, key=key)
    return StructureResponse(requirements=requirements, step=step, key=key)


@app.post("/plan", response_model=PlanResult)
def plan(req: PlanRequest) -> PlanResult:
    """Run the full loop (structure -> graph -> validate -> schedule -> repairs -> explain).

    With ``requirements`` instead of ``brief`` the structure stage is skipped and a synthetic
    structure step is prepended. A brief that cannot be planned still returns 200 with
    ``success=false`` and ``failure`` filled in; only configuration problems are HTTP errors.
    """
    _check_plan_request(req)
    client = _client(req.llm)
    with _reported("plan"):
        return _run_plan(req, client)


@app.post("/plan/stream", response_class=StreamingResponse)
async def plan_stream(req: PlanRequest) -> StreamingResponse:
    """``/plan`` as server-sent events: ``step`` per LoopStep, ``: ping`` while waiting, then
    one ``result`` or ``error`` event (see the module docstring)."""
    _check_plan_request(req)
    client = _client(req.llm)
    return StreamingResponse(
        _plan_events(req, client), media_type="text/event-stream", headers=STREAM_HEADERS
    )


@app.post("/simulate", response_model=SimResult)
def simulate(req: SimulateRequest) -> SimResult:
    """Replay a verified plan with injected robot failures, replanning on each event."""
    plan_ = req.plan
    _verified_plan(plan_)
    known = {r.id for r in plan_.requirements.robots}
    unknown = sorted({f.robot for f in req.failures} - known)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown robot(s) in failures: {unknown}")
    planner = Planner(_client(req.llm), max_repairs=req.max_repairs, explain=False)
    key = brief_key(plan_.requirements.source_brief or "", clean_brief_id(req.brief_id))
    injections = [FailureInjection(robot=f.robot, t_h=f.t_h) for f in req.failures]
    with _reported("simulate"):
        try:
            result = run_with_replanning(
                planner, plan_, injections, key=key, max_replans=req.max_replans, dt_h=req.dt_h
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return thin_frames(result, req.frame_stride)


@app.post("/montecarlo", response_model=MonteCarloResult)
def montecarlo(req: MonteCarloRequest) -> MonteCarloResult:
    """F9: replay a verified plan ``runs`` times with random robot failures and duration
    jitter and report how often it finishes within the horizon.

    Replans are solver-only (CP-SAT re-schedules the remaining work, no model calls) unless
    ``llm`` names a client kind; the response carries no frames, only per-run samples.
    """
    plan_ = req.plan
    _verified_plan(plan_)
    planner = (solver_only_planner() if req.llm is None
               else MonteCarloPlanner(_client(req.llm), max_repairs=req.max_repairs,
                                      explain=False, suggest_budget_s=0.0))
    with _reported("montecarlo"):
        try:
            return run_monte_carlo(
                plan_, runs=req.runs, failure_rate_per_robot_h=req.failure_rate_per_robot_h,
                duration_jitter=req.duration_jitter, seed=req.seed, max_replans=req.max_replans,
                planner=planner,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
