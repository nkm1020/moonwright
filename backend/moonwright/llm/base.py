"""LLM client contract + offline fixture client + JSON extraction helpers.

Two implementations:

* :class:`FixtureClient` (this file) - offline. Returns canned JSON from ``fixtures/<key>/``
  so the whole pipeline, the tests and the UI work without an API key.
* ``moonwright.llm.tokenfactory.TokenFactoryClient`` - live Nebius Token Factory
  (OpenAI-compatible) with per-role model routing.

Both expose :meth:`LLMClient.complete_json`. Stage code never imports the openai SDK directly.

Fixture layout (``key`` is the brief id, e.g. ``landing_pad``)::

    fixtures/<key>/structure.json            Requirements JSON           role=structure
    fixtures/<key>/taskgraph_1.json          first TaskGraph             role=taskgraph, attempt=1
    fixtures/<key>/taskgraph_2.json          graph after 1st repair      role=repair,    attempt=2
    fixtures/<key>/taskgraph_replan.json     graph after a replan        role=taskgraph, attempt=1, variant="replan"
    fixtures/<key>/taskgraph_replan_2.json   1st repair of that replan   role=repair,    attempt=2, variant="replan"
    fixtures/<key>/explain.json              {"text": "..."}             role=explain

Lookup falls back to the highest available attempt for that role (so a 3rd repair reuses
``taskgraph_2.json``); calls with a ``variant`` never fall back, so every attempt of a variant
has its own file (``taskgraph_<variant>.json`` for attempt 1, ``taskgraph_<variant>_<n>.json``
for repair attempt n). The second and later replans of one simulation run use the variants
``replan2``, ``replan3``, ... If nothing matches, :class:`FixtureMissing` is raised; during
replanning the task-graph stage catches it and keeps the prior graph (first attempt) or ends a
repair round with the solver's verdict.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from moonwright.schemas import Failure, Role, Usage, schema_conflict

T = TypeVar("T", bound=BaseModel)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


class LLMError(RuntimeError):
    """Transport / model failure. The planner turns it into Failure(kind='llm_error').

    ``retryable`` says whether asking again could help (a live timeout: yes; an unreadable
    fixture file: no), which is what the planner's repair budget keys on.
    """

    retryable: bool = True


class FixtureMissing(LLMError):
    """FixtureClient has no canned answer for this (key, role, attempt).

    The message is written for the person reading the UI or the CLI summary; ``details``
    carries the lookup facts (which directory was searched) for logs and ``Failure.details``.
    """

    retryable = False

    def __init__(self, message: str, details: Sequence[str] = ()):
        super().__init__(message)
        self.details = list(details)


class SchemaMismatch(LLMError):
    """The model answered but the JSON did not validate after all attempts."""

    def __init__(self, message: str, raw: str, errors: list[str]):
        super().__init__(message)
        self.raw = raw
        self.errors = errors


def answer_failure(error: LLMError) -> Failure:
    """The :class:`~moonwright.schemas.Failure` of a stage whose model call raised ``error``.

    A client that knows what each of its asks did attaches the failure a person reads to the
    exception (``error.failure``; the Token Factory client's "Rejected answers": the model's
    short name, one detail line per ask, ``schema:json`` / ``schema:shape``) and that account
    is the stage's failure. Any other client (offline fixtures, a scripted test client) gets
    one built from the exception: ``schema`` for an answer that came back
    (:class:`SchemaMismatch`, or any exception with error lines), its hint the message, its
    details the error lines and its conflict ids those lines' two kinds
    (:func:`~moonwright.schemas.schema_conflict`; ``schema:shape`` when no line says more);
    ``llm_error`` for a request that failed, with the exception's ``details`` when it has
    them (:class:`FixtureMissing`)."""
    attached = getattr(error, "failure", None)
    if isinstance(attached, Failure):
        return attached
    errors = [str(e) for e in getattr(error, "errors", None) or []]
    if errors or isinstance(error, SchemaMismatch):
        conflicts = list(dict.fromkeys(schema_conflict(e) for e in errors)) or ["schema:shape"]
        return Failure(kind="schema", conflicts=conflicts, hint=str(error), details=errors)
    return Failure(kind="llm_error", hint=str(error),
                   details=[str(d) for d in getattr(error, "details", None) or []])


class LLMClient(Protocol):
    name: str

    def complete_json(
        self,
        *,
        role: Role,
        system: str,
        user: str,
        schema: type[T],
        key: str | None = None,
        attempt: int = 1,
        variant: str | None = None,
        max_attempts: int = 2,
    ) -> tuple[T, Usage]:
        """Ask for a JSON object matching ``schema`` and return the validated model + usage.

        ``key``/``attempt``/``variant`` only matter to FixtureClient (fixture lookup) and to the
        recording wrapper (fixture naming); live clients ignore them.
        ``max_attempts`` > 1 lets a live client re-ask once with the validation errors appended.
        """
        ...


# ----------------------------------------------------------------------------- JSON helpers
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_NO_JSON = object()  # sentinel: no candidate parsed to an object or array


def strip_thinking(text: str) -> str:
    """Remove reasoning (``<think>`` blocks, Nemotron / DeepSeek style) from a completion.

    Three shapes occur: complete ``<think>...</think>`` blocks; an opening tag without a close
    (the answer was cut off inside the reasoning, so everything from the tag onwards goes);
    and a closing tag without an open, which reasoning-mode chat templates produce because the
    opening tag sits in the prompt: the content *starts* with the reasoning, so everything up
    to and including the last orphan ``</think>`` goes.
    """
    text = _THINK_RE.sub("", text)
    if _THINK_CLOSE in text:
        text = text.rsplit(_THINK_CLOSE, 1)[1]
    if _THINK_OPEN in text:
        text = text.split(_THINK_OPEN, 1)[0]
    return text.strip()


def _balanced_spans(text: str) -> tuple[list[str], bool]:
    """Top-level balanced ``{...}`` / ``[...]`` regions of ``text`` (string-aware inside a
    region), plus whether the text ends inside an unclosed region (a truncated answer)."""
    spans: list[str] = []
    start = -1
    depth = 0
    in_str = esc = False
    for i, ch in enumerate(text):
        if start < 0:
            if ch in "{[":
                start, depth, in_str, esc = i, 1, False, False
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                spans.append(text[start : i + 1])
                start = -1
    return spans, start >= 0


def _best_json(candidates: list[str], *, prefer_last: bool = False) -> Any:
    """The best candidate that parses to an object or array, or :data:`_NO_JSON`.

    Objects beat arrays. Among balanced regions of free text, longer beats shorter and then
    later beats earlier (a fragment quoted in prose or a draft loses to the answer); among
    fenced blocks (``prefer_last``) the last one wins, since a model that fences several
    blocks (a schema echo, then the answer) puts the answer last.
    """
    best, best_key = _NO_JSON, None
    for i, src in enumerate(candidates):
        try:
            value = json.loads(src)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, (dict, list)):
            continue
        key = (isinstance(value, dict), i) if prefer_last else (isinstance(value, dict), len(src), i)
        if best_key is None or key > best_key:
            best, best_key = value, key
    return best


def extract_json(text: str) -> Any:
    """Best-effort JSON from a model answer: thinking stripped -> fenced block(s) -> the whole
    text -> balanced ``{...}`` / ``[...]`` regions, picking the best candidate when several
    parse (see :func:`_best_json`). Raises ``ValueError`` when nothing does."""
    text = strip_thinking(text)
    fenced = _best_json([m.group(1).strip() for m in _FENCE_RE.finditer(text)], prefer_last=True)
    if fenced is not _NO_JSON:
        return fenced
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(whole, (dict, list)):
            return whole
    spans, truncated = _balanced_spans(text)
    best = _best_json(spans)
    if best is not _NO_JSON:
        return best
    if truncated:
        raise ValueError("unbalanced JSON in model output")
    if spans:
        raise ValueError("no valid JSON object in model output")
    raise ValueError("no JSON object found in model output")


def validate_into[M: BaseModel](schema: type[M], data: Any) -> M:
    """pydantic validation with a readable error list on failure."""
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        errors = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise SchemaMismatch("output failed schema validation", json.dumps(data)[:2000], errors)


# ----------------------------------------------------------------------------- fixtures
_ATTEMPT_FIXTURE_RE = re.compile(r"^taskgraph_(\d+)\.json$")


def fixture_filename(role: Role, attempt: int = 1, variant: str | None = None) -> str:
    if role == "structure":
        return "structure.json"
    if role == "explain":
        return "explain.json"
    if variant:
        return (f"taskgraph_{variant}.json" if attempt <= 1
                else f"taskgraph_{variant}_{attempt}.json")
    return f"taskgraph_{attempt}.json"


class FixtureClient:
    """Offline client backed by ``fixtures/<key>/``. See module docstring for the layout."""

    name = "fixture"

    def __init__(self, fixtures_dir: Path | None = None):
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else FIXTURES_DIR

    def _resolve(self, key: str | None, role: Role, attempt: int, variant: str | None) -> Path:
        if not key:
            raise FixtureMissing(f"fixture lookup for role={role} needs a brief key")
        d = self.fixtures_dir / key
        if not d.is_dir():
            raise FixtureMissing(
                "This backend is running offline on recorded Nemotron answers and has no "
                f"recording for brief '{key}'. Pick one of the example briefs, or start the "
                "backend with NEBIUS_API_KEY set to plan new briefs live.",
                details=[f"no fixture directory for brief '{key}' (looked in {d})"],
            )
        exact = d / fixture_filename(role, attempt, variant)
        if exact.exists():
            return exact
        if role in ("taskgraph", "repair") and not variant:
            # fall back to the highest attempt we have (numeric order: 10 comes after 2)
            attempts = sorted(
                (int(m.group(1)), path) for path in d.iterdir()
                if (m := _ATTEMPT_FIXTURE_RE.match(path.name)) and path.is_file()
            )
            lower = [path for n, path in attempts if n <= attempt]
            if lower:
                return lower[-1]
        raise FixtureMissing(f"missing fixture {exact.name} for brief '{key}'")

    @staticmethod
    def _read(path: Path) -> Any:
        """The fixture's JSON; a file that cannot be read or parsed is an :class:`LLMError`
        that is not retryable (reading it again gives the same answer)."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            error = LLMError(f"unreadable fixture {path}: {e}")
            error.retryable = False
            raise error from e

    def complete_json(
        self,
        *,
        role: Role,
        system: str,
        user: str,
        schema: type[T],
        key: str | None = None,
        attempt: int = 1,
        variant: str | None = None,
        max_attempts: int = 2,
    ) -> tuple[T, Usage]:
        t0 = time.perf_counter()
        path = self._resolve(key, role, attempt, variant)
        data = self._read(path)
        obj = validate_into(schema, data)
        return obj, Usage(model=f"fixture:{path.name}", latency_s=time.perf_counter() - t0)


class ExplainText(BaseModel):
    """Schema used by the explain role: {"text": "..."}"""

    text: str
