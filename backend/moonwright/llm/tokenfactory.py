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
    MOONWRIGHT_THINKING_MAX_TOKENS   reasoning + answer budget for thinking-on calls (16384)
    MOONWRIGHT_LLM_TIMEOUT_S         per-request timeout for every role (default 120 / 300)

An answer cut off by ``max_tokens`` is reported as such and re-asked. That includes the empty
answer a reasoning parser returns when the whole budget went into reasoning (``content`` null,
``finish_reason`` "length"); only a model that stays silent on every ask is an
:class:`LLMError`. The token usage of a failed stage is attached to the exception (``usage``)
so the evaluation still counts it.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

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
from moonwright.schemas import Role, Usage

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
THINKING_MAX_TOKENS: Final = 16384
MAX_TOKENS_ENV_VAR: Final = "MOONWRIGHT_MAX_TOKENS"
THINKING_MAX_TOKENS_ENV_VAR: Final = "MOONWRIGHT_THINKING_MAX_TOKENS"
PREVIOUS_ANSWER_CHARS: Final = 6000

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
    request depending on whether the role thinks.
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
        self._api_key = key
        self._transport_retries = transport_retries
        self._http = http_client
        self._sdks: dict[str, openai.OpenAI] = {}
        self._modes: dict[str, RequestMode] = {}

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
        """Ask ``role``'s model for JSON matching ``schema``; re-ask with the validation errors.

        ``key``/``attempt``/``variant`` are fixture-naming hints for the recording wrapper and
        are ignored here. Usage is summed over all asks.
        """
        model = self.models[role]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        total = Usage(model=model)
        asks = max(1, max_attempts)
        reply = _Reply(text="", usage=total, finish_reason=None, max_tokens=self.max_tokens)
        errors: list[str] = []
        empty = 0
        for _ in range(asks):
            reply = self._chat(role, model, messages, schema)
            total.input_tokens += reply.usage.input_tokens
            total.output_tokens += reply.usage.output_tokens
            total.latency_s += reply.usage.latency_s
            total.model = reply.usage.model
            if not reply.text.strip():
                empty += 1
                errors = [self._json_error(ValueError("empty completion"), reply)]
            else:
                try:
                    obj = validate_into(schema, extract_json(reply.text))
                except SchemaMismatch as e:
                    errors = e.errors
                except ValueError as e:
                    errors = [self._json_error(e, reply)]
                else:
                    return obj, total
            messages[1] = {"role": "user", "content": user + self._retry_suffix(errors, reply)}
        exc: LLMError
        if empty == asks:
            exc = LLMError(
                f"{role} ({model}): empty completion (finish_reason={reply.finish_reason}) "
                f"on every one of {asks} attempt(s)"
            )
        else:
            exc = SchemaMismatch(
                f"{role} ({model}): answer failed validation after {asks} attempt(s)",
                reply.text,
                errors,
            )
        exc.usage = total  # type: ignore[attr-defined]  # so a failed stage still counts tokens
        raise exc

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
    ) -> tuple[dict[str, Any], bool]:
        """Request parameters for one attempt; also whether the thinking switch is in them."""
        thinking = self.thinking[role]
        params: dict[str, Any] = {"model": model, "messages": messages}
        if thinking:
            params.update(THINKING_SAMPLING)
            params["max_tokens"] = self.thinking_max_tokens
            params["timeout"] = self.thinking_timeout_s
        else:
            params["temperature"] = TEMPERATURES[role]
            params["max_tokens"] = self.max_tokens
            params["timeout"] = self.timeout_s
        fmt = response_format_for(mode.json_format, schema)
        if fmt is not None:
            params["response_format"] = fmt
        sent_switch = not thinking and mode.thinking_control
        if sent_switch:
            params["extra_body"] = THINKING_OFF_BODY
        return params, sent_switch

    def _chat(
        self, role: Role, model: str, messages: list[dict[str, str]], schema: type
    ) -> _Reply:
        mode = self._modes.get(model, RequestMode())
        t0 = time.perf_counter()
        response: ChatCompletion | None = None
        for _ in range(len(JSON_LEVELS) + 3):  # bounded: json rungs + thinking switch + region
            params, sent_switch = self._params(role, model, messages, schema, mode)
            endpoint = self.regional_base_url if mode.regional else self.base_url
            try:
                response = self._sdk(endpoint).chat.completions.create(**params)
            except openai.NotFoundError as e:
                if mode.regional or self.regional_base_url is None:
                    raise LLMError(f"{role} ({model}): {type(e).__name__}: {e}") from e
                mode = replace(mode, regional=True)
            except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
                message = f"{e.message} {e.body}"
                nxt = None
                if isinstance(e, openai.UnprocessableEntityError) or _FEATURE_WORDS.search(message):
                    nxt = mode.downgraded(message, sent_thinking_switch=sent_switch)
                if nxt is None:
                    raise LLMError(f"{role} ({model}): {type(e).__name__}: {e}") from e
                mode = nxt
            except openai.APIError as e:
                raise LLMError(f"{role} ({model}): {type(e).__name__}: {e}") from e
            else:
                self._modes[model] = mode
                break
            self._modes[model] = mode
        if response is None:
            raise LLMError(f"{role} ({model}): request rejected on every fallback")
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
            text=text, usage=usage, finish_reason=finish_reason, max_tokens=params["max_tokens"]
        )

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
    """

    name = "record"

    def __init__(self, inner: LLMClient, fixtures_dir: Path | None = None):
        self.inner = inner
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else FIXTURES_DIR
        self.written: list[Path] = []

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
        obj, usage = self.inner.complete_json(
            role=role, system=system, user=user, schema=schema,
            key=key, attempt=attempt, variant=variant, max_attempts=max_attempts,
        )
        if key:
            path = self.fixtures_dir / key / fixture_filename(role, attempt, variant)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(obj.model_dump_json(indent=1) + "\n", encoding="utf-8")
            self.written.append(path)
        return obj, usage
