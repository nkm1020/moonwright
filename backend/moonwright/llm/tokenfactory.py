"""Live LLM client for Nebius Token Factory (OpenAI-compatible chat completions).

:class:`TokenFactoryClient` implements :class:`moonwright.llm.base.LLMClient` against the live
service: per-role model routing, structured output with a learned per-model fallback ladder,
per-role thinking control, schema-driven re-asking, transport retries and mapping of SDK
errors to :class:`~moonwright.llm.base.LLMError`. :class:`RecordingClient` wraps any client and
saves each validated answer in the fixture layout, so one live run produces the offline demo
files.

What the defaults encode (checked against the Token Factory docs and model catalogue on
2026-10-22; everything can be overridden through the environment):

* Endpoint ``https://api.tokenfactory.nebius.com/v1/``. Nemotron 3 Super/Ultra are served from
  ``us-central1``; when the global endpoint answers 404 for a model, the same request is
  repeated once on the regional endpoint and that choice is remembered for the model.
* Model ids are case-exact: ``nvidia/Nemotron-3_5-Lightning`` (structure: cheap and fast),
  ``nvidia/Nemotron-3-Ultra-550b-a55b`` (task graph and repair: the reasoning model) and
  ``nvidia/nemotron-3-super-120b-a12b`` (explanation prose).
* Structured output: ``response_format`` ``json_schema`` (strict) first, ``json_object`` next,
  plain text last. No Nemotron model carries the catalogue's "JSON mode" tag, so every answer
  is parsed and validated client-side regardless (``extract_json`` + pydantic).
* Thinking is on by default server-side. It is switched off per request with
  ``chat_template_kwargs.enable_thinking=false`` for the roles that do not need it (structure,
  explain). Task graph and repair keep it, with a larger ``max_tokens`` because reasoning and
  answer share the budget, and the recommended sampling (temperature 1.0, top_p 0.95); the
  output grammar applies after ``</think>``. ``MOONWRIGHT_THINKING=off`` makes every role
  answer directly (fast demos), ``on`` makes every role think.
* Rate limiting is HTTP 429 with ``Retry-After``; the SDK's retries honour it.

Call log
--------
Every request the client sends (each rung of the fallback ladder, each re-ask, failures
included) appends one JSON line to ``MOONWRIGHT_LLM_LOG`` (default ``backend/out/llm_log.jsonl``;
``off`` disables it): timestamp, role, model, endpoint, json level, thinking flag, whether the
thinking switch was sent, ``max_tokens``, HTTP status, ``x-request-id`` (read through
``chat.completions.with_raw_response``; the plain ``create`` path is kept when the SDK object
has no raw-response accessor), ``finish_reason``, prompt / completion tokens, latency, the
error type for a failed request, plus the fixture hints (``key``, ``attempt``, ``variant``) and
the ask number. The prompt and the answer text are never written (privacy, size). A log file
that cannot be written disables the log with one warning; it never fails a model call. The
log is the evidence behind the measured feedback report and the per-step model badges.

Environment (``moonwright.llm.factory`` loads ``.env`` first)::

    NEBIUS_API_KEY                   required for the live client
    NEBIUS_BASE_URL                  default DEFAULT_BASE_URL
    NEBIUS_REGIONAL_BASE_URL         default REGIONAL_BASE_URL; empty disables the fallback
    MOONWRIGHT_MODEL_STRUCTURE       model id per role; defaults in MODEL_DEFAULTS
    MOONWRIGHT_MODEL_TASKGRAPH
    MOONWRIGHT_MODEL_REPAIR
    MOONWRIGHT_MODEL_EXPLAIN
    MOONWRIGHT_THINKING              on | off | auto (per-role defaults in THINKING_DEFAULTS)
    MOONWRIGHT_MAX_TOKENS            answer budget for thinking-off calls (default 4096)
    MOONWRIGHT_THINKING_MAX_TOKENS   reasoning + answer budget for thinking-on calls (32768)
    MOONWRIGHT_LLM_TIMEOUT_S         per-request timeout for every role (default 120 / 300)
    MOONWRIGHT_LLM_LOG               JSONL call log path (default DEFAULT_LOG_PATH; "off" = none)

Token policy
------------
Reasoning and answer share ``max_tokens``. The first live evaluation (Oct 23 2026, 132 runs)
cut 35 % of the 16,384-token reasoning calls (Ultra 78 of 190, Super 36 of 139), and the
re-ask at the same budget was usually cut again, so a reasoning call now starts at
:data:`THINKING_MAX_TOKENS` (32,768; ``MOONWRIGHT_THINKING_MAX_TOKENS`` still overrides, and a
value below the default is warned about once: a ``backend/.env`` copied from the pre-3e
``.env.example`` pins 16,384) and every ask adapts to how the previous one ended:

* ``finish_reason == "length"`` on a reasoning ask: the next ask repeats the same question
  with twice the budget (capped at :data:`THINKING_MAX_TOKENS_CAP`, 65,536, and given
  proportionally more time) and reasoning on. Only a cut at the cap (a budget already
  doubled) makes the next ask answer with reasoning OFF (``enable_thinking: false``) at the
  normal ``max_tokens`` with the "compact JSON only" note. A direct (thinking-off) ask that
  is cut is asked again with twice its budget and the same note.
* Any other failure (an answer that is not JSON, a schema violation, an empty answer at
  ``finish_reason == "stop"``) keeps the re-ask as it was: the same budget and reasoning
  setting, the validation errors and the previous answer appended to the user message.

The ladder spans the planner's re-ask of a rejected answer: one call has ``max_attempts``
asks (2 by default, so the ask count is unchanged), and when its asks all fail, the next call
of the same role, fixture key and variant at the next ``attempt`` (the planner's re-ask, in the
same thread) starts on the rung the last ask led to instead of the default. So a prompt that
needs more than 32,768 reasoning tokens is asked at 32,768 and 65,536, and the re-ask, if the
65,536 was cut too, answers without reasoning; before, every call went 32,768 -> 4,096 without
reasoning and the doubled rung was never reached (seen in review: a server that always cuts
cost 6 asks at [32768, 4096, 32768, 4096, 32768, 4096]).

The call log shows each ask with its own ``max_tokens`` and ``thinking`` values. The empty
answer a reasoning parser returns when the whole budget went into reasoning (``content``
null, ``finish_reason`` "length") counts as a cut answer.

Rejected answers
----------------
When every ask of a call fails, the exception carries the copy a person reads, written here
because only the client knows what each ask did. :class:`SchemaMismatch` (an answer came back
but never validated) or :class:`LLMError` (every ask was empty, or the request itself failed)
carry ``failure``, a :class:`~moonwright.schemas.Failure`: the hint "Nemotron 3 Ultra did not
return a valid task graph after 3 asks: the answer was cut off at 32,768 tokens, then at
65,536 tokens, then failed the schema", one detail line per ask ("ask 1: cut off at 32,768
tokens", "ask 2: task T3: 'after' must be a list") and the conflict ids
``schema:json`` (an ask cut off, not JSON or empty) and ``schema:shape`` (a schema violation);
``asks``, one :class:`AskOutcome` per failed ask; and ``usage``, the tokens of every ask, so a
failed stage still counts them. ``SchemaMismatch.errors`` stays the last ask's raw error lines
(what its retry note quoted) and ``raw`` its text. A request the SDK gave up on reads "The
Token Factory request to Nemotron 3 Ultra failed: APITimeoutError (Request timed out.)"; a
model silent on every ask "Nemotron 3 Ultra returned an empty completion on each of 2 asks"
(with the cut-off clause when the asks were cut). The model's short names come from
:func:`moonwright.text.model_name`; the planner
(``moonwright.pipeline.loop``) shows ``failure`` as the failed step's verdict.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import httpx
import openai
from openai.types.chat import ChatCompletion

from moonwright.llm.base import (
    FIXTURES_DIR,
    LLMClient,
    LLMError,
    SchemaMismatch,
    T,
    extract_json,
    fixture_filename,
    validate_into,
)
from moonwright.schemas import Failure, Role, Usage
from moonwright.text import model_name, plural

DEFAULT_BASE_URL: Final = "https://api.tokenfactory.nebius.com/v1/"
REGIONAL_BASE_URL: Final = "https://api.tokenfactory.us-central1.nebius.com/v1/"

# Token Factory catalogue ids (case-exact). The Llama-3.1-Nemotron-Ultra-253B and Nemotron
# Nano Omni ids were retired on 2026-08-31 and must not be used.
MODEL_DEFAULTS: Final[dict[Role, str]] = {
    "structure": "nvidia/Nemotron-3_5-Lightning",
    "taskgraph": "nvidia/Nemotron-3-Ultra-550b-a55b",
    "repair": "nvidia/Nemotron-3-Ultra-550b-a55b",
    "explain": "nvidia/nemotron-3-super-120b-a12b",
}
MODEL_ENV_VARS: Final[dict[Role, str]] = {
    "structure": "MOONWRIGHT_MODEL_STRUCTURE",
    "taskgraph": "MOONWRIGHT_MODEL_TASKGRAPH",
    "repair": "MOONWRIGHT_MODEL_REPAIR",
    "explain": "MOONWRIGHT_MODEL_EXPLAIN",
}
THINKING_DEFAULTS: Final[dict[Role, bool]] = {
    "structure": False,
    "taskgraph": True,
    "repair": True,
    "explain": False,
}
THINKING_ENV_VAR: Final = "MOONWRIGHT_THINKING"
# Sampling: near-greedy for direct answers, the vendor recommendation when reasoning.
TEMPERATURES: Final[dict[Role, float]] = {
    "structure": 0.2,
    "taskgraph": 0.4,
    "repair": 0.4,
    "explain": 0.2,
}
THINKING_SAMPLING: Final[dict[str, float]] = {"temperature": 1.0, "top_p": 0.95}
THINKING_OFF_BODY: Final[dict[str, Any]] = {"chat_template_kwargs": {"enable_thinking": False}}
JSON_LEVELS: Final = ("json_schema", "json_object", "none")

DEFAULT_TIMEOUT_S: Final = 120.0
THINKING_TIMEOUT_S: Final = 300.0
TIMEOUT_ENV_VAR: Final = "MOONWRIGHT_LLM_TIMEOUT_S"
DEFAULT_TRANSPORT_RETRIES: Final = 3
DEFAULT_MAX_TOKENS: Final = 4096
# 16,384 cut 35 % of the reasoning calls of the first live evaluation (module docstring,
# "Token policy"); a cut reasoning ask is repeated with twice the budget up to the cap
THINKING_MAX_TOKENS: Final = 32768
THINKING_MAX_TOKENS_CAP: Final = 65536
_LOW_BUDGET_WARNED: set[int] = set()   # budgets below the default already warned about
_LOW_BUDGET_LOCK = threading.Lock()
MAX_TOKENS_ENV_VAR: Final = "MOONWRIGHT_MAX_TOKENS"
THINKING_MAX_TOKENS_ENV_VAR: Final = "MOONWRIGHT_THINKING_MAX_TOKENS"
PREVIOUS_ANSWER_CHARS: Final = 6000
REASON_CHARS: Final = 120      # of a failed request's reason inside the hint sentence
REASONS_PER_ASK: Final = 3     # schema violations named in one ask's detail line

# What a role is asked for, in the failure sentence ("did not return a valid task graph")
WANTED: Final[dict[str, str]] = {
    "structure": "valid requirements",
    "taskgraph": "a valid task graph",
    "repair": "a valid task graph",
    "explain": "a valid explanation",
}

LOG_ENV_VAR: Final = "MOONWRIGHT_LLM_LOG"
LOG_OFF: Final = "off"
DEFAULT_LOG_PATH: Final = Path(__file__).resolve().parents[2] / "out" / "llm_log.jsonl"
LOG_ERROR_CHARS: Final = 200   # of a failed request's error message (never prompt or answer)
REQUEST_ID_HEADER: Final = "x-request-id"

log = logging.getLogger(__name__)

RETRY_SUFFIX: Final = (
    "\n\nYour previous answer failed validation:\n{errors}\n\n"
    "Previous answer (truncated):\n{previous}\n\n"
    "Return only corrected JSON."
)

# A 400 whose text mentions one of these rejected a request *feature* (structured output or the
# thinking switch) and is retried one rung down the ladder; any other 400 is final. A 422 is a
# request-validation error by definition and always steps down.
_FEATURE_WORDS = re.compile(
    r"response_format|json|guided|grammar|schema|structured|chat_template|thinking|reasoning"
    r"|extra_body|unknown|unexpected|unrecognized|unsupported|not supported|extra (?:field|input)",
    re.IGNORECASE,
)
_THINKING_WORDS = re.compile(r"chat_template|thinking|reasoning", re.IGNORECASE)


def models_from_env(overrides: Mapping[str, str] | None = None) -> dict[Role, str]:
    """Per-role model ids: ``overrides`` > ``MOONWRIGHT_MODEL_*`` > :data:`MODEL_DEFAULTS`."""
    given = dict(overrides or {})
    return {
        role: given.get(role) or os.environ.get(env_var) or MODEL_DEFAULTS[role]
        for role, env_var in MODEL_ENV_VARS.items()
    }


def thinking_from_env(overrides: Mapping[str, bool] | None = None) -> dict[Role, bool]:
    """Per-role thinking switch: ``overrides`` > ``MOONWRIGHT_THINKING`` (on/off) > defaults."""
    forced = {"on": True, "off": False}.get(os.environ.get(THINKING_ENV_VAR, "").strip().lower())
    given = dict(overrides or {})
    return {
        role: given.get(role, default if forced is None else forced)
        for role, default in THINKING_DEFAULTS.items()
    }


def _env_number(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as e:
        raise LLMError(f"{name} must be a number, got {raw!r}") from e


def log_path_from_env(given: str | Path | None = None) -> Path | None:
    """Where the call log goes: ``given`` > ``MOONWRIGHT_LLM_LOG`` > :data:`DEFAULT_LOG_PATH`;
    the value ``off`` (any case) means no log at all and returns None."""
    raw = os.environ.get(LOG_ENV_VAR, "") if given is None else str(given)
    raw = raw.strip()
    if raw.lower() == LOG_OFF:
        return None
    return Path(raw) if raw else DEFAULT_LOG_PATH


class CallLog:
    """Append-only JSONL log of model requests (module docstring, "Call log").

    ``path=None`` disables it. Writes are serialised with a lock (the eval runs planners in
    threads) and the first ``OSError`` switches the log off with one warning, so a read-only
    directory never breaks a live call. ``records`` counts the lines written.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.records = 0
        self._lock = threading.Lock()
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self.path is not None and not self._failed

    def write(self, record: Mapping[str, Any]) -> bool:
        """Append ``record`` as one line; False when the log is off or could not be written."""
        if self.path is None or self._failed:
            return False
        line = json.dumps(record, separators=(",", ":"), default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as e:
                self._failed = True
                log.warning("LLM call log disabled: cannot write %s (%s)", self.path, e)
                return False
            self.records += 1
        return True


def call_record(
    *,
    role: str,
    model: str,
    endpoint: str,
    mode: RequestMode,
    thinking: bool,
    thinking_switch: bool,
    max_tokens: int,
    latency_s: float,
    response: ChatCompletion | None,
    status: int | None,
    request_id: str | None,
    error: BaseException | None,
    tag: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One call-log line for a request that answered (``response``) or failed (``error``).

    Carries identifiers and figures only: no message content, no answer text. ``tag`` adds the
    caller's fixture hints (``key``, ``attempt``, ``variant``) and the ask number.
    """
    choice = response.choices[0] if response is not None and response.choices else None
    usage = response.usage if response is not None else None
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "role": role,
        "model": model,
        "endpoint": endpoint,
        "regional": mode.regional,
        "json_format": mode.json_format,
        "thinking": thinking,
        "thinking_switch": thinking_switch,
        "max_tokens": max_tokens,
        "status": status,
        "request_id": request_id,
        "finish_reason": choice.finish_reason if choice is not None else None,
        "prompt_tokens": usage.prompt_tokens if usage is not None else None,
        "completion_tokens": usage.completion_tokens if usage is not None else None,
        "latency_s": round(latency_s, 3),
        "error": type(error).__name__ if error is not None else None,
        "error_message": str(error)[:LOG_ERROR_CHARS] if error is not None else None,
    }
    record.update(tag or {})
    return record


@dataclass(frozen=True)
class RequestMode:
    """What one model has been seen to accept: the rung of the structured-output ladder,
    whether the thinking switch may be sent, and which endpoint serves it. Learned from 4xx
    answers and remembered per model id for the life of the client."""

    json_level: int = 0
    thinking_control: bool = True
    regional: bool = False

    @property
    def json_format(self) -> str:
        return JSON_LEVELS[self.json_level]

    def downgraded(self, message: str, *, sent_thinking_switch: bool) -> RequestMode | None:
        """The mode to retry with after a rejected feature, or None when nothing is left."""
        if sent_thinking_switch and _THINKING_WORDS.search(message):
            return replace(self, thinking_control=False)
        if self.json_level < len(JSON_LEVELS) - 1:
            return replace(self, json_level=self.json_level + 1)
        if sent_thinking_switch:
            return replace(self, thinking_control=False)
        return None


@dataclass(frozen=True)
class _Reply:
    text: str
    usage: Usage
    finish_reason: str | None
    max_tokens: int
    thinking: bool


@dataclass(frozen=True)
class Rung:
    """What one ask sends (module docstring, "Token policy"): whether the model reasons and
    its ``max_tokens``."""

    thinking: bool
    max_tokens: int


@dataclass(frozen=True)
class _Carry:
    """Where a failed call left the ladder (module docstring, "Token policy"): the rung its
    next call starts on, the note that ask carries (the compact-JSON note of a reasoning-off
    rung) and the ``attempt`` of the failed call, so only the planner's very next re-ask
    picks it up."""

    attempt: int
    rung: Rung
    suffix: str | None


def warn_low_budget(budget: int) -> bool:
    """Log once per process and budget that ``MOONWRIGHT_THINKING_MAX_TOKENS`` is below
    :data:`THINKING_MAX_TOKENS`; True when this call logged it."""
    with _LOW_BUDGET_LOCK:
        if budget in _LOW_BUDGET_WARNED:
            return False
        _LOW_BUDGET_WARNED.add(budget)
    log.warning(
        "%s=%d is below the %s-token default: the first live evaluation had 35 %% of its "
        "reasoning calls cut at 16,384 tokens. A backend/.env copied from the pre-phase-3e "
        ".env.example pins 16384; delete that line (or raise it) to use the default.",
        THINKING_MAX_TOKENS_ENV_VAR, budget, f"{THINKING_MAX_TOKENS:,}")
    return True


@dataclass(frozen=True)
class AskOutcome:
    """How one ask of a :meth:`TokenFactoryClient.complete_json` call failed (module docstring,
    "Rejected answers"): ``cut`` (``finish_reason`` "length"), ``json`` (the answer did not
    parse), ``empty`` (nothing came back at ``finish_reason`` "stop") or ``shape`` (the JSON
    violated the schema). ``reasons`` are the violations as a person reads them, ``errors``
    the raw lines the retry note quoted, ``empty`` whether any text came back at all, and
    ``reasoning_off`` marks the ask of a reasoning role that answered without reasoning (the
    ladder's last rung)."""

    ask: int
    kind: Literal["cut", "json", "empty", "shape"]
    max_tokens: int
    thinking: bool
    reasons: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    empty: bool = False
    reasoning_off: bool = False

    @property
    def conflict(self) -> str:
        """``schema:shape`` for a schema violation, ``schema:json`` for an answer that never
        reached the schema (cut off, not JSON, empty)."""
        return "schema:shape" if self.kind == "shape" else "schema:json"

    @property
    def phrase(self) -> str:
        """The ask in the hint sentence: 'was cut off at 32,768 tokens', 'failed the schema'."""
        if self.kind == "cut":
            return f"was cut off at {plural(self.max_tokens, 'token')}"
        if self.kind == "json":
            return "was not JSON"
        if self.kind == "empty":
            return "was empty"
        return "failed the schema"

    @property
    def line(self) -> str:
        """The ask's detail line: 'ask 1: cut off at 32,768 tokens', 'ask 3 (reasoning off):
        task T3: 'after' must be a list; 'tasks' is missing'."""
        head = f"ask {self.ask}" + (" (reasoning off)" if self.reasoning_off else "")
        if self.kind == "cut":
            return f"{head}: cut off at {plural(self.max_tokens, 'token')}"
        if self.kind == "empty":
            return f"{head}: empty completion"
        shown = list(self.reasons[:REASONS_PER_ASK])
        if len(self.reasons) > REASONS_PER_ASK:
            shown.append(f"and {len(self.reasons) - REASONS_PER_ASK} more")
        return f"{head}: " + "; ".join(shown)


# pydantic's messages as a person reads them, by prefix; any other message is kept
_MESSAGES: Final = (
    ("Input should be a valid list", "must be a list"),
    ("Input should be a valid string", "must be a string"),
    ("Input should be a valid integer", "must be a whole number"),
    ("Input should be a valid number", "must be a number"),
    ("Input should be a valid boolean", "must be true or false"),
    ("Input should be a valid dictionary", "must be an object"),
    ("Input should be an object", "must be an object"),
    ("Field required", "is missing"),
    ("Extra inputs are not permitted", "is not a field of the schema"),
)
_INPUT_SHOULD: Final = "Input should be "
_AT_LEAST_RE: Final = re.compile(r"^List should have at least (\d+) items? after validation")
_PATTERN_RE: Final = re.compile(r"^String should match pattern '(.*)'$")
# the id pattern of the schemas (moonwright.schemas.Id), as a person reads it
_ID_PATTERN: Final = "^[A-Za-z0-9_.-]+$"
_PREFIXES_DROPPED: Final = ("Value error, ", "Assertion failed, ")
# the list fields whose items carry an id, and the noun that names one item
_ITEM_NOUNS: Final = {"tasks": "task", "robots": "robot", "structures": "structure",
                      "zones": "zone", "entries": "entry"}


def describe_schema_errors(errors: Sequence[str], data: Any) -> list[str]:
    """Validation error lines (``"<loc>: <msg>"``, :func:`moonwright.llm.base.validate_into`)
    as a person reads them, one per line: the location named by the item's id when the answer
    ``data`` carries one ("task T3: 'after' must be a list", "robot HL1: 'battery_wh' must be
    a number"), by its index otherwise ("tasks[2]: 'after' must be a list"), "the answer" for
    the whole answer ("the answer must be an object"); pydantic's wording becomes "must be
    ...", "is missing", "must not be empty", "may use only letters, digits and _ . -" (an id),
    a model validator's sentence loses its "Value error, " prefix, and anything else is
    kept."""
    return [_describe_error(error, data) for error in errors]


def _describe_error(error: str, data: Any) -> str:
    loc, _, msg = error.partition(":")
    msg = msg.strip()
    where = _describe_location(loc.strip(), data)
    if msg.startswith(_PREFIXES_DROPPED):   # a sentence of its own: "structure S1: depth_m ..."
        return f"{where}: {_describe_message(msg)}"
    return f"{where} {_describe_message(msg)}"


def _describe_message(msg: str) -> str:
    for prefix, human in _MESSAGES:
        if msg.startswith(prefix):
            return human
    if msg.startswith(_INPUT_SHOULD):
        return "must be " + msg[len(_INPUT_SHOULD):]
    if m := _AT_LEAST_RE.match(msg):
        n = int(m[1])
        return "must not be empty" if n == 1 else f"must have at least {n} items"
    if m := _PATTERN_RE.match(msg):
        return ("may use only letters, digits and _ . -" if m[1] == _ID_PATTERN
                else "has an invalid format")
    for prefix in _PREFIXES_DROPPED:   # a model validator's own sentence
        msg = msg.removeprefix(prefix)
    return msg[:1].lower() + msg[1:] if msg else "is invalid"


def _describe_location(loc: str, data: Any) -> str:
    if not loc:
        return "the answer"
    parts = loc.split(".")
    node: Any = data
    owner: str | None = None
    field: list[str] = []
    for i, part in enumerate(parts):
        if part.isdigit():
            index = int(part)
            item = node[index] if isinstance(node, list) and index < len(node) else None
            parent = parts[i - 1] if i else ""
            if parent in _ITEM_NOUNS:
                ident = item.get("id") if isinstance(item, dict) else None
                owner = (f"{_ITEM_NOUNS[parent]} {ident}" if isinstance(ident, str) and ident
                         else f"{parent}[{index}]")
                field = []
            elif field:
                field[-1] += f"[{index}]"
            else:
                field.append(f"[{index}]")
            node = item
            continue
        node = node.get(part) if isinstance(node, dict) else None
        field.append(part)
    name = ".".join(field)
    if owner and name:
        return f"{owner}: '{name}'"
    return owner or f"'{name}'"


def rejection_summary(outcomes: Sequence[AskOutcome]) -> str:
    """The asks of a rejected call in one clause: 'the answer was cut off at 32,768 tokens,
    then at 65,536 tokens, then failed the schema'; a repeated outcome is counted ('the answer
    failed the schema twice')."""
    parts: list[str] = []
    i = 0
    while i < len(outcomes):
        phrase = outcomes[i].phrase
        j = i
        while j + 1 < len(outcomes) and outcomes[j + 1].phrase == phrase:
            j += 1
        if parts and outcomes[i].kind == "cut" and outcomes[i - 1].kind == "cut":
            phrase = phrase.removeprefix("was cut off ")   # "then at 65,536 tokens"
        parts.append(phrase + _times(j - i + 1))
        i = j + 1
    return "the answer " + ", then ".join(parts)


def _times(count: int) -> str:
    return {1: "", 2: " twice", 3: " three times"}.get(count, f" {count} times")


def _short_reason(error: BaseException) -> str:
    """The reason of a failed request on one line, for the hint sentence: the server's own
    message when the SDK kept it (``body.error.message``), else the exception's."""
    body = getattr(error, "body", None)
    message: str | None = None
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            message = inner["message"]
        elif isinstance(body.get("message"), str):
            message = body["message"]
    if not message:
        message = str(getattr(error, "message", "") or error) or type(error).__name__
    message = " ".join(message.split())
    return message if len(message) <= REASON_CHARS else message[:REASON_CHARS - 3] + "..."


def _attach(exc: LLMError, usage: Usage, outcomes: Sequence[AskOutcome]) -> None:
    """Complete a request failure raised mid-call: the tokens of the asks before it and their
    detail lines ahead of the request's own."""
    exc.usage = usage  # type: ignore[attr-defined]
    exc.asks = tuple(outcomes)  # type: ignore[attr-defined]
    failure = getattr(exc, "failure", None)
    if outcomes and isinstance(failure, Failure):
        exc.failure = failure.model_copy(  # type: ignore[attr-defined]
            update={"details": [o.line for o in outcomes] + failure.details})


def response_format_for(kind: str, schema: type) -> dict[str, Any] | None:
    """The ``response_format`` payload for one rung of the ladder (None = plain text)."""
    if kind == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
                "strict": True,
            },
        }
    if kind == "json_object":
        return {"type": "json_object"}
    return None


class TokenFactoryClient:
    """OpenAI-compatible chat client with the Moonwright JSON contract on top.

    Everything not passed explicitly is read from the environment. ``http_client`` lets tests
    inject an ``httpx.Client`` with a mock transport. ``transport_retries`` is handed to the SDK,
    which retries connection errors, timeouts and 408/409/429/5xx responses with exponential
    backoff (honouring ``Retry-After``); ``timeout_s`` / ``thinking_timeout_s`` apply per
    request depending on whether the role thinks. ``log_path`` is the JSONL call log
    (:func:`log_path_from_env`: None reads ``MOONWRIGHT_LLM_LOG``, ``"off"`` disables it).
    """

    name = "nebius"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        regional_base_url: str | None = None,
        models: Mapping[str, str] | None = None,
        thinking: Mapping[str, bool] | None = None,
        timeout_s: float | None = None,
        thinking_timeout_s: float | None = None,
        transport_retries: int = DEFAULT_TRANSPORT_RETRIES,
        max_tokens: int | None = None,
        thinking_max_tokens: int | None = None,
        http_client: httpx.Client | None = None,
        log_path: str | Path | None = None,
    ):
        key = api_key or os.environ.get("NEBIUS_API_KEY")
        if not key:
            raise LLMError(
                "NEBIUS_API_KEY is not set: export it, put it in .env, or run with --llm fixture"
            )
        self.base_url = base_url or os.environ.get("NEBIUS_BASE_URL") or DEFAULT_BASE_URL
        if regional_base_url is None:
            regional_base_url = os.environ.get("NEBIUS_REGIONAL_BASE_URL", REGIONAL_BASE_URL)
        regional_base_url = regional_base_url.strip()
        self.regional_base_url = (
            regional_base_url if regional_base_url and regional_base_url != self.base_url else None
        )
        self.models = models_from_env(models)
        self.thinking = thinking_from_env(thinking)
        env_timeout = _env_number(TIMEOUT_ENV_VAR)
        self.timeout_s = timeout_s if timeout_s is not None else (env_timeout or DEFAULT_TIMEOUT_S)
        self.thinking_timeout_s = (
            thinking_timeout_s if thinking_timeout_s is not None
            else (env_timeout or THINKING_TIMEOUT_S)
        )
        env_max = _env_number(MAX_TOKENS_ENV_VAR)
        env_thinking_max = _env_number(THINKING_MAX_TOKENS_ENV_VAR)
        self.max_tokens = max_tokens if max_tokens is not None else int(env_max or DEFAULT_MAX_TOKENS)
        self.thinking_max_tokens = (
            thinking_max_tokens if thinking_max_tokens is not None
            else int(env_thinking_max or THINKING_MAX_TOKENS)
        )
        if thinking_max_tokens is None and self.thinking_max_tokens < THINKING_MAX_TOKENS:
            warn_low_budget(self.thinking_max_tokens)   # an old .env, most likely
        self._api_key = key
        self._transport_retries = transport_retries
        self._http = http_client
        self._sdks: dict[str, openai.OpenAI] = {}
        self._modes: dict[str, RequestMode] = {}
        self._carries = threading.local()   # per thread: (role, key, variant) -> _Carry
        self.log = CallLog(log_path_from_env(log_path))

    @property
    def budgets(self) -> str:
        """The token budgets in one line, for a run's banner: 'reasoning 32,768 tokens (x2 when
        cut, up to 65,536), direct 4,096'."""
        return (f"reasoning {self.thinking_max_tokens:,} tokens (x2 when cut, up to "
                f"{THINKING_MAX_TOKENS_CAP:,}), direct {self.max_tokens:,}")

    # ------------------------------------------------------------------ LLMClient
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
        """Ask ``role``'s model for JSON matching ``schema``: up to ``max_attempts`` asks on
        the ladder of the module docstring ("Token policy"); a call whose asks all fail raises
        with the failure copy ("Rejected answers").

        ``key``/``attempt``/``variant`` are fixture-naming hints for the recording wrapper; here
        they tag the call log and identify the planner's re-ask of a failed call, which starts
        on the rung that call left (module docstring, "Token policy"). Usage is summed over
        all asks.
        """
        model = self.models[role]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        total = Usage(model=model)
        asks = max(1, max_attempts)
        reasoning_role = self.thinking[role]
        rung = Rung(thinking=reasoning_role,
                    max_tokens=self.thinking_max_tokens if reasoning_role else self.max_tokens)
        carried = self._take_carry(role, key, variant, attempt)
        if carried is not None:   # the planner's re-ask: start where the failed call left off
            rung = carried.rung
            if carried.suffix is not None:
                messages[1] = {"role": "user", "content": user + carried.suffix}
        outcomes: list[AskOutcome] = []
        reply = _Reply(text="", usage=total, finish_reason=None, max_tokens=rung.max_tokens,
                       thinking=rung.thinking)
        for ask in range(1, asks + 1):
            tag = {"key": key, "attempt": attempt, "variant": variant, "ask": ask}
            try:
                reply = self._chat(role, model, messages, schema, rung, tag=tag)
            except LLMError as e:
                _attach(e, total, outcomes)   # so a failed stage still counts tokens
                raise
            total.input_tokens += reply.usage.input_tokens
            total.output_tokens += reply.usage.output_tokens
            total.latency_s += reply.usage.latency_s
            total.model = reply.usage.model
            obj, outcome = self._judge(ask, reply, schema, reasoning_role=reasoning_role)
            if obj is not None:
                return obj, total
            outcomes.append(outcome)
            nxt, suffix = self._next_rung(rung, outcome, reply)
            if ask == asks:   # the call is over: its re-ask starts on the next rung
                self._keep_carry(role, key, variant, attempt, nxt, suffix, rung)
                break
            rung = nxt
            if suffix is not None:
                messages[1] = {"role": "user", "content": user + suffix}
        raise self._rejected(role, model, outcomes, reply, total)

    # ------------------------------------------------------------------ introspection
    def mode(self, model: str) -> RequestMode:
        """What the client has learned about ``model`` so far (defaults before the first call)."""
        return self._modes.get(model, RequestMode())

    # ------------------------------------------------------------------ internals
    def _sdk(self, base_url: str) -> openai.OpenAI:
        if base_url not in self._sdks:
            self._sdks[base_url] = openai.OpenAI(
                api_key=self._api_key,
                base_url=base_url,
                timeout=self.timeout_s,
                max_retries=self._transport_retries,
                http_client=self._http,
            )
        return self._sdks[base_url]

    def _params(
        self,
        role: Role,
        model: str,
        messages: list[dict[str, str]],
        schema: type,
        mode: RequestMode,
        rung: Rung,
    ) -> tuple[dict[str, Any], bool]:
        """Request parameters for one ask on ``rung``; also whether the thinking switch is in
        them."""
        params: dict[str, Any] = {"model": model, "messages": messages,
                                  "max_tokens": rung.max_tokens}
        if rung.thinking:
            params.update(THINKING_SAMPLING)
            params["timeout"] = self._thinking_timeout(rung.max_tokens)
        else:
            params["temperature"] = TEMPERATURES[role]
            params["timeout"] = self.timeout_s
        fmt = response_format_for(mode.json_format, schema)
        if fmt is not None:
            params["response_format"] = fmt
        sent_switch = not rung.thinking and mode.thinking_control
        if sent_switch:
            params["extra_body"] = THINKING_OFF_BODY
        return params, sent_switch

    def _thinking_timeout(self, max_tokens: int) -> float:
        """The per-request timeout of a reasoning ask: the configured one, scaled up with the
        budget when a cut ask is repeated with a bigger one (the doubled rung needs the time
        to use what it was given)."""
        return self.thinking_timeout_s * max(1.0, max_tokens / max(1, self.thinking_max_tokens))

    def _create(
        self, endpoint: str, params: dict[str, Any]
    ) -> tuple[ChatCompletion, int | None, str | None]:
        """One SDK call: the completion plus the HTTP status and ``x-request-id`` when the SDK
        exposes the raw response (``with_raw_response``); the plain ``create`` path, without
        them, when it does not (a stub SDK in tests)."""
        completions = self._sdk(endpoint).chat.completions
        raw_api = getattr(completions, "with_raw_response", None)
        if raw_api is None:
            return completions.create(**params), None, None
        raw = raw_api.create(**params)
        return raw.parse(), raw.status_code, raw.headers.get(REQUEST_ID_HEADER)

    def _request(
        self,
        role: Role,
        model: str,
        endpoint: str,
        params: dict[str, Any],
        mode: RequestMode,
        sent_switch: bool,
        thinking: bool,
        tag: Mapping[str, Any] | None,
    ) -> tuple[ChatCompletion | None, openai.APIError | None]:
        """One request, logged whatever happens (with this ask's own ``thinking`` and
        ``max_tokens``): ``(completion, None)`` or ``(None, error)``."""
        t0 = time.perf_counter()
        response: ChatCompletion | None = None
        error: openai.APIError | None = None
        status: int | None = None
        request_id: str | None = None
        try:
            response, status, request_id = self._create(endpoint, params)
        except openai.APIError as e:
            error = e
            status = getattr(e, "status_code", None)
            request_id = getattr(e, "request_id", None)
        self.log.write(call_record(
            role=role, model=model, endpoint=endpoint, mode=mode, thinking=thinking,
            thinking_switch=sent_switch, max_tokens=params["max_tokens"],
            latency_s=time.perf_counter() - t0, response=response, status=status,
            request_id=request_id, error=error, tag=tag,
        ))
        return response, error

    def _chat(
        self,
        role: Role,
        model: str,
        messages: list[dict[str, str]],
        schema: type,
        rung: Rung,
        *,
        tag: Mapping[str, Any] | None = None,
    ) -> _Reply:
        mode = self._modes.get(model, RequestMode())
        t0 = time.perf_counter()
        response: ChatCompletion | None = None
        endpoint = self.base_url
        for _ in range(len(JSON_LEVELS) + 3):  # bounded: json rungs + thinking switch + region
            params, sent_switch = self._params(role, model, messages, schema, mode, rung)
            endpoint = self.regional_base_url if mode.regional else self.base_url
            response, error = self._request(role, model, endpoint, params, mode, sent_switch,
                                            rung.thinking, tag)
            if error is None:
                self._modes[model] = mode
                break
            if isinstance(error, openai.NotFoundError):
                if mode.regional or self.regional_base_url is None:
                    raise self._request_failed(role, model, error, endpoint) from error
                mode = replace(mode, regional=True)
            elif isinstance(error, (openai.BadRequestError, openai.UnprocessableEntityError)):
                message = f"{error.message} {error.body}"
                nxt = None
                if (isinstance(error, openai.UnprocessableEntityError)
                        or _FEATURE_WORDS.search(message)):
                    nxt = mode.downgraded(message, sent_thinking_switch=sent_switch)
                if nxt is None:
                    raise self._request_failed(role, model, error, endpoint) from error
                mode = nxt
            else:
                raise self._request_failed(role, model, error, endpoint) from error
            self._modes[model] = mode
        if response is None:
            raise self._request_failed(role, model, None, endpoint)
        latency = time.perf_counter() - t0

        # An empty answer (content null, no choices) is returned, not raised: complete_json
        # re-asks for it exactly like an answer that failed to parse.
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "") if choice else ""
        finish_reason = choice.finish_reason if choice else None
        usage = Usage(
            model=response.model or model,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            latency_s=latency,
        )
        return _Reply(
            text=text, usage=usage, finish_reason=finish_reason, max_tokens=params["max_tokens"],
            thinking=rung.thinking,
        )

    def _judge(
        self, ask: int, reply: _Reply, schema: type[T], *, reasoning_role: bool
    ) -> tuple[T | None, AskOutcome | None]:
        """Validate one reply: ``(object, None)`` when it matched ``schema``, ``(None,
        outcome)`` when it did not. ``reasoning_role`` says whether the role reasons, so an
        ask it answered without reasoning is marked in its line."""
        cut = reply.finish_reason == "length"
        common: dict[str, Any] = {"ask": ask, "max_tokens": reply.max_tokens,
                                  "thinking": reply.thinking,
                                  "reasoning_off": reasoning_role and not reply.thinking}
        if not reply.text.strip():
            errors = (self._json_error(ValueError("empty completion"), reply),)
            return None, AskOutcome(kind="cut" if cut else "empty", errors=errors, empty=True,
                                    **common)
        try:
            data = extract_json(reply.text)
        except ValueError as e:
            return None, AskOutcome(kind="cut" if cut else "json", reasons=(str(e),),
                                    errors=(self._json_error(e, reply),), **common)
        try:
            return validate_into(schema, data), None
        except SchemaMismatch as e:
            reasons = tuple(describe_schema_errors(e.errors, data))
            if cut:   # what parsed is a fragment of a longer answer: the budget is the fault
                errors = (self._json_error(ValueError("incomplete answer"), reply), *e.errors)
                return None, AskOutcome(kind="cut", reasons=reasons, errors=errors, **common)
            return None, AskOutcome(kind="shape", reasons=reasons, errors=tuple(e.errors),
                                    **common)

    def _next_rung(
        self, rung: Rung, outcome: AskOutcome, reply: _Reply
    ) -> tuple[Rung, str | None]:
        """The rung of the ask after a failed one and the suffix its user message carries,
        None meaning the same message again (module docstring, "Token policy"): a cut
        reasoning ask doubles its budget up to the cap, a cut at the cap turns reasoning off
        with the compact-JSON note, a cut direct ask doubles; any other failure is asked
        again as it was, with its errors."""
        suffix = self._retry_suffix(list(outcome.errors), reply)
        if outcome.kind != "cut":
            return rung, suffix                                   # today's re-ask
        if rung.thinking and rung.max_tokens < THINKING_MAX_TOKENS_CAP:
            return Rung(True, min(rung.max_tokens * 2, THINKING_MAX_TOKENS_CAP)), None
        if rung.thinking:
            return Rung(False, self.max_tokens), suffix           # cut at the cap: reasoning off
        return Rung(False, min(rung.max_tokens * 2, THINKING_MAX_TOKENS_CAP)), suffix

    def _carry_store(self) -> dict[tuple[str, str | None, str | None], _Carry]:
        store = getattr(self._carries, "store", None)
        if store is None:
            store = self._carries.store = {}
        return store

    def _take_carry(self, role: Role, key: str | None, variant: str | None,
                    attempt: int) -> _Carry | None:
        """The rung a failed call of ``(role, key, variant)`` left for this one, when this is
        its next ``attempt`` in the same thread (the planner's re-ask); any older carry is
        dropped either way."""
        carry = self._carry_store().pop((role, key, variant), None)
        return carry if carry is not None and carry.attempt + 1 == attempt else None

    def _keep_carry(self, role: Role, key: str | None, variant: str | None, attempt: int,
                    rung: Rung, suffix: str | None, was: Rung) -> None:
        """Remember where a failed call left the ladder for its re-ask, when that is not the
        role's starting rung: a doubled budget, or a direct ask with its compact-JSON note
        (after a cut at the cap, or a cut direct ask). The errors of any other failure reach
        the re-ask through the planner's own context, so a rung kept as it was carries no
        note."""
        reasons = self.thinking[role]
        start = Rung(reasons, self.thinking_max_tokens if reasons else self.max_tokens)
        if rung == start:
            return
        note = suffix if not rung.thinking and rung != was else None
        self._carry_store()[(role, key, variant)] = _Carry(attempt=attempt, rung=rung,
                                                           suffix=note)

    @staticmethod
    def _rejected(
        role: Role, model: str, outcomes: Sequence[AskOutcome], reply: _Reply, total: Usage
    ) -> LLMError:
        """The exception of a call whose every ask failed (module docstring, "Rejected
        answers"): a model silent on every ask is an :class:`LLMError`, anything else a
        :class:`SchemaMismatch` with the last ask's raw errors."""
        name = model_name(model)
        lines = [o.line for o in outcomes]
        n = len(outcomes)
        summary = rejection_summary(outcomes)
        exc: LLMError
        if all(o.empty for o in outcomes):
            each = "its single ask" if n == 1 else f"each of {n} asks"
            tail = f": {summary}" if any(o.kind == "cut" for o in outcomes) else ""
            hint = f"{name} returned an empty completion on {each}{tail}"
            exc = LLMError(hint)
            failure = Failure(kind="llm_error", hint=hint, details=lines)
        else:
            hint = (f"{name} did not return {WANTED.get(role, 'a valid answer')} after "
                    f"{plural(n, 'ask')}: {summary}")
            exc = SchemaMismatch(hint, reply.text, list(outcomes[-1].errors))
            failure = Failure(kind="schema", hint=hint, details=lines,
                              conflicts=list(dict.fromkeys(o.conflict for o in outcomes)))
        exc.failure = failure  # type: ignore[attr-defined]
        exc.usage = total  # type: ignore[attr-defined]
        exc.asks = tuple(outcomes)  # type: ignore[attr-defined]
        return exc

    @staticmethod
    def _request_failed(
        role: Role, model: str, error: openai.APIError | None, endpoint: str
    ) -> LLMError:
        """The exception for a request the SDK gave up on (``error``) or that every fallback
        rejected (None), with its ``failure`` (module docstring, "Rejected answers")."""
        name = model_name(model)
        where = f"{role} ({model}) on {endpoint}"
        if error is None:
            hint = f"The Token Factory request to {name} failed: every request format was rejected"
            details = [f"{where}: json_schema, json_object and plain text were all refused"]
        else:
            kind = type(error).__name__
            hint = f"The Token Factory request to {name} failed: {kind} ({_short_reason(error)})"
            request_id = getattr(error, "request_id", None)
            details = [f"{kind}: {' '.join(str(error).split())[:LOG_ERROR_CHARS]}",
                       where + (f", request id {request_id}" if request_id else "")]
        exc = LLMError(hint)
        exc.failure = Failure(kind="llm_error", hint=hint, details=details)  # type: ignore[attr-defined]
        return exc

    @staticmethod
    def _json_error(exc: ValueError, reply: _Reply) -> str:
        if reply.finish_reason == "length":
            return (
                f"json: the answer was cut off at max_tokens={reply.max_tokens} ({exc}); "
                "answer with compact JSON only, no reasoning"
            )
        return f"json: {exc}"

    @staticmethod
    def _retry_suffix(errors: list[str], reply: _Reply) -> str:
        return RETRY_SUFFIX.format(
            errors="\n".join(f"- {e}" for e in errors),
            previous=reply.text[:PREVIOUS_ANSWER_CHARS],
        )


class RecordingClient:
    """Delegate to ``inner`` and save every validated answer under ``fixtures/<key>/``.

    File names follow :func:`moonwright.llm.base.fixture_filename`, so a recorded run is read
    back verbatim by :class:`~moonwright.llm.base.FixtureClient`. Calls without a ``key`` are
    passed through unrecorded.

    Numbering: the planner's ``attempt`` is the running count of graph answers asked for,
    re-asks included, while the offline client replays a run without re-asks (a recorded
    answer always validates), asking for attempts 1, 2, 3, ... round by round. Task-graph files
    are therefore numbered by *round*: every ask of a run that fails (an exception from the
    live client, which the planner answers with a re-ask of the same round) shifts the numbers
    of that run's later answers down by one, so a first graph that validated only on the
    planner's re-ask lands in ``taskgraph_1.json`` and the repair after it in
    ``taskgraph_2.json``; a solver-first replan whose first call is already a repair keeps its
    ``taskgraph_replan_2.json``. A run is the sequence of calls for one ``(key, variant)``; it
    restarts when the attempt number does. Recording the same key from several threads at once
    is not supported (the files would overwrite each other anyway).
    """

    name = "record"

    def __init__(self, inner: LLMClient, fixtures_dir: Path | None = None):
        self.inner = inner
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else FIXTURES_DIR
        self.written: list[Path] = []
        self._runs: dict[tuple[str, str | None], tuple[int, int]] = {}  # (last attempt, failed asks)
        self._lock = threading.Lock()

    def _round(self, key: str, variant: str | None, attempt: int) -> int:
        """The round ``attempt`` belongs to in this run of ``(key, variant)``: the attempt
        number minus the asks of the run that failed before it (each was re-asked)."""
        with self._lock:
            last, failed = self._runs.get((key, variant), (0, 0))
            if attempt <= last:
                failed = 0   # the planner's count restarted: a new run of this brief
            self._runs[(key, variant)] = (attempt, failed)
            return max(1, attempt - failed)

    def _failed(self, key: str, variant: str | None) -> None:
        with self._lock:
            last, failed = self._runs.get((key, variant), (0, 0))
            self._runs[(key, variant)] = (last, failed + 1)

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
        numbered = bool(key) and role in ("taskgraph", "repair")
        number = self._round(key, variant, attempt) if numbered else attempt
        try:
            obj, usage = self.inner.complete_json(
                role=role, system=system, user=user, schema=schema,
                key=key, attempt=attempt, variant=variant, max_attempts=max_attempts,
            )
        except LLMError:
            if numbered:
                self._failed(key, variant)   # the planner re-asks this round
            raise
        if key:
            path = self.fixtures_dir / key / fixture_filename(role, number, variant)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(obj.model_dump_json(indent=1) + "\n", encoding="utf-8")
            self.written.append(path)
        return obj, usage
