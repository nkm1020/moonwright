"""Token Factory client: the learned request ladder, thinking control and endpoint fallback,
the token policy (a cut reasoning ask is doubled, then asked without reasoning) and the copy
a rejected call carries (the model's short name, one line per ask, the two conflict ids).

No network: an ``httpx.MockTransport`` plays the server and records every request URL and body.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai.types.chat import ChatCompletion

from moonwright.llm import tokenfactory
from moonwright.llm.base import ExplainText, LLMError, SchemaMismatch
from moonwright.llm.tokenfactory import (
    DEFAULT_BASE_URL,
    DEFAULT_LOG_PATH,
    DEFAULT_MAX_TOKENS,
    MODEL_DEFAULTS,
    REGIONAL_BASE_URL,
    THINKING_DEFAULTS,
    THINKING_MAX_TOKENS,
    THINKING_MAX_TOKENS_CAP,
    AskOutcome,
    CallLog,
    RequestMode,
    Rung,
    TokenFactoryClient,
    describe_schema_errors,
    log_path_from_env,
    rejection_summary,
    response_format_for,
    thinking_from_env,
)
from moonwright.schemas import Failure, TaskGraph
from moonwright.text import model_name

GLOBAL = "https://global.test/v1/"
REGIONAL = "https://regional.test/v1/"
Handler = Callable[[httpx.Request, dict[str, Any]], httpx.Response]
ENV_VARS = (
    "MOONWRIGHT_THINKING", "MOONWRIGHT_MAX_TOKENS", "MOONWRIGHT_THINKING_MAX_TOKENS",
    "MOONWRIGHT_LLM_TIMEOUT_S", "NEBIUS_BASE_URL", "NEBIUS_REGIONAL_BASE_URL",
    "MOONWRIGHT_MODEL_STRUCTURE", "MOONWRIGHT_MODEL_TASKGRAPH", "MOONWRIGHT_MODEL_REPAIR",
    "MOONWRIGHT_MODEL_EXPLAIN",
)


def completion(text: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}})


def ok(_request: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=completion('{"text": "ok"}'))


class Server:
    """Scripted server: ``handler(request, body)`` decides; every (url, body) is recorded."""

    def __init__(self, handler: Handler):
        self.handler = handler
        self.urls: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.urls.append(str(request.url))
        self.bodies.append(body)
        return self.handler(request, body)

    @property
    def hosts(self) -> list[str]:
        return [httpx.URL(u).host for u in self.urls]

    def client(self, **kwargs: Any) -> TokenFactoryClient:
        kwargs.setdefault("transport_retries", 0)
        kwargs.setdefault("regional_base_url", REGIONAL)
        return TokenFactoryClient(
            api_key="k", base_url=GLOBAL,
            http_client=httpx.Client(transport=httpx.MockTransport(self)), **kwargs,
        )


def ask(client: TokenFactoryClient, role: str = "structure", **kw: Any) -> tuple[ExplainText, Any]:
    return client.complete_json(role=role, system="S", user="U", schema=ExplainText, **kw)


def scripted(*replies: dict[str, Any]) -> Server:
    """A server that answers the scripted completions in order."""
    queue = iter(replies)
    return Server(lambda _r, _b: httpx.Response(200, json=next(queue)))


def cut(text: str = "") -> dict[str, Any]:
    """A completion stopped by ``max_tokens``: empty when the budget went into reasoning."""
    return completion(text, "length")


OK = completion('{"text": "ok"}')
WRONG = completion('{"text": 1}')
ULTRA, LIGHTNING = MODEL_DEFAULTS["taskgraph"], MODEL_DEFAULTS["structure"]


def fmt(body: dict[str, Any]) -> str | None:
    return body.get("response_format", {}).get("type")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A clean environment, with the call log pointed at the test's tmp dir so no test ever
    appends to ``backend/out/llm_log.jsonl``."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOONWRIGHT_LLM_LOG", str(tmp_path / "llm_log.jsonl"))


# --------------------------------------------------------------------------- catalogue facts
def test_defaults_are_the_verified_catalogue_ids():
    assert DEFAULT_BASE_URL == "https://api.tokenfactory.nebius.com/v1/"
    assert REGIONAL_BASE_URL == "https://api.tokenfactory.us-central1.nebius.com/v1/"
    assert MODEL_DEFAULTS == {
        "structure": "nvidia/Nemotron-3_5-Lightning",
        "taskgraph": "nvidia/Nemotron-3-Ultra-550b-a55b",
        "repair": "nvidia/Nemotron-3-Ultra-550b-a55b",
        "explain": "nvidia/nemotron-3-super-120b-a12b",
    }
    retired = ("Llama-3_1-Nemotron-Ultra-253B", "Nemotron-Nano-9B-v2", "Nano-Omni")
    assert not any(r in m for m in MODEL_DEFAULTS.values() for r in retired)
    assert THINKING_DEFAULTS == {
        "structure": False, "taskgraph": True, "repair": True, "explain": False,
    }
    # the token policy: 16,384 cut 35 % of the reasoning calls of the first live evaluation
    assert (DEFAULT_MAX_TOKENS, THINKING_MAX_TOKENS, THINKING_MAX_TOKENS_CAP) == (4096, 32768, 65536)


def test_response_format_ladder_payloads():
    strict = response_format_for("json_schema", ExplainText)
    assert strict["type"] == "json_schema"
    assert strict["json_schema"]["name"] == "ExplainText"
    assert strict["json_schema"]["strict"] is True
    assert strict["json_schema"]["schema"]["properties"]["text"]["type"] == "string"
    assert response_format_for("json_object", ExplainText) == {"type": "json_object"}
    assert response_format_for("none", ExplainText) is None


# --------------------------------------------------------------------------- the ladder
def test_thinking_switch_rejection_drops_only_the_switch():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if "chat_template_kwargs" in body:
            return error(400, "Unexpected keyword argument: chat_template_kwargs")
        return ok(request, body)

    server = Server(handler)
    client = server.client()
    assert ask(client)[0].text == "ok"
    assert ask(client)[0].text == "ok"
    assert len(server.bodies) == 3
    assert "chat_template_kwargs" in server.bodies[0]
    assert all("chat_template_kwargs" not in b for b in server.bodies[1:])
    assert all(fmt(b) == "json_schema" for b in server.bodies)
    mode = client.mode(client.models["structure"])
    assert mode == RequestMode(json_level=0, thinking_control=False, regional=False)


def test_422_steps_down_one_rung_without_feature_words():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if fmt(body) == "json_schema":
            return error(422, "validation error")
        return ok(request, body)

    server = Server(handler)
    client = server.client()
    assert ask(client)[0].text == "ok"
    assert [fmt(b) for b in server.bodies] == ["json_schema", "json_object"]
    assert client.mode(client.models["structure"]).json_format == "json_object"


def test_schema_rejection_message_steps_down():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if fmt(body) == "json_schema":
            return error(400, "guided decoding backend does not support this schema")
        return ok(request, body)

    server = Server(handler)
    assert ask(server.client())[0].text == "ok"
    assert [fmt(b) for b in server.bodies] == ["json_schema", "json_object"]


def test_unrelated_400_is_final_after_one_request():
    server = Server(lambda _r, _b: error(400, "This model's maximum context length is 4096"))
    with pytest.raises(LLMError, match="BadRequestError"):
        ask(server.client())
    assert len(server.bodies) == 1


def test_ladder_exhausted_is_an_llm_error():
    server = Server(lambda _r, _b: error(400, "response_format, chat_template_kwargs: unsupported"))
    with pytest.raises(LLMError, match="BadRequestError"):
        ask(server.client())
    # switch dropped first (the message names it), then json_schema -> json_object -> none
    assert [("chat_template_kwargs" in b, fmt(b)) for b in server.bodies] == [
        (True, "json_schema"), (False, "json_schema"), (False, "json_object"), (False, None),
    ]


# --------------------------------------------------------------------------- regional fallback
def test_404_on_the_global_endpoint_retries_regionally_and_remembers():
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if request.url.host == "global.test":
            return error(404, "The model `nvidia/Nemotron-3-Ultra-550b-a55b` does not exist")
        return ok(request, body)

    server = Server(handler)
    client = server.client()
    assert ask(client, "taskgraph")[0].text == "ok"
    assert ask(client, "taskgraph")[0].text == "ok"
    assert server.hosts == ["global.test", "regional.test", "regional.test"]
    assert client.mode(client.models["taskgraph"]).regional is True
    # another model is still tried on the global endpoint first
    assert ask(client, "structure")[0].text == "ok"
    assert server.hosts[3:] == ["global.test", "regional.test"]


def test_404_without_a_regional_endpoint_is_final():
    server = Server(lambda _r, _b: error(404, "model not found"))
    with pytest.raises(LLMError, match="NotFoundError"):
        ask(server.client(regional_base_url=""))
    assert server.hosts == ["global.test"]


def test_404_on_the_regional_endpoint_is_final():
    server = Server(lambda _r, _b: error(404, "model not found"))
    with pytest.raises(LLMError, match="NotFoundError"):
        ask(server.client())
    assert server.hosts == ["global.test", "regional.test"]


def test_regional_fallback_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    assert TokenFactoryClient().regional_base_url == REGIONAL_BASE_URL
    monkeypatch.setenv("NEBIUS_REGIONAL_BASE_URL", "")
    assert TokenFactoryClient().regional_base_url is None
    monkeypatch.setenv("NEBIUS_REGIONAL_BASE_URL", REGIONAL_BASE_URL)
    monkeypatch.setenv("NEBIUS_BASE_URL", REGIONAL_BASE_URL)   # already regional: no fallback
    assert TokenFactoryClient().regional_base_url is None


# --------------------------------------------------------------------------- thinking + budgets
def test_thinking_env_switch(monkeypatch: pytest.MonkeyPatch):
    assert thinking_from_env() == THINKING_DEFAULTS
    monkeypatch.setenv("MOONWRIGHT_THINKING", "off")
    assert thinking_from_env() == dict.fromkeys(THINKING_DEFAULTS, False)
    monkeypatch.setenv("MOONWRIGHT_THINKING", "on")
    assert thinking_from_env() == dict.fromkeys(THINKING_DEFAULTS, True)
    assert thinking_from_env({"explain": False})["explain"] is False
    monkeypatch.setenv("MOONWRIGHT_THINKING", "auto")
    assert thinking_from_env() == THINKING_DEFAULTS


def test_thinking_off_everywhere_sends_the_switch_to_the_reasoning_roles(monkeypatch):
    monkeypatch.setenv("MOONWRIGHT_THINKING", "off")
    server = Server(ok)
    ask(server.client(), "taskgraph")
    body = server.bodies[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS and body["temperature"] == 0.4
    assert "top_p" not in body


def test_a_cut_reasoning_ask_is_doubled_then_asked_without_reasoning(tmp_path: Path):
    """cut -> doubled -> cut -> reasoning off: the whole ladder of a three-ask call, every ask
    logged with its own budget and thinking flag."""
    server = scripted(cut(), cut('{"text": "still thi'), OK)
    client = server.client(log_path=tmp_path / "calls.jsonl")
    obj, usage = ask(client, "repair", max_attempts=3)
    assert obj.text == "ok" and usage.output_tokens == 15 and usage.input_tokens == 30
    first, second, third = server.bodies
    assert [b["max_tokens"] for b in server.bodies] == [THINKING_MAX_TOKENS,
                                                        2 * THINKING_MAX_TOKENS,
                                                        DEFAULT_MAX_TOKENS]
    # the doubled ask is the same question again, reasoning on
    assert second["messages"] == first["messages"]
    assert second["top_p"] == 0.95 and "chat_template_kwargs" not in second
    # the last ask: reasoning off, the role's direct temperature, the compact-JSON note that
    # quotes the budget that was cut and the fragment that came back
    assert third["chat_template_kwargs"] == {"enable_thinking": False}
    assert third["temperature"] == 0.4 and "top_p" not in third
    note = third["messages"][1]["content"]
    assert note.startswith("U") and f"cut off at max_tokens={2 * THINKING_MAX_TOKENS}" in note
    assert "unbalanced JSON" in note and "compact JSON only, no reasoning" in note
    assert '{"text": "still thi' in note
    records = read_log(tmp_path / "calls.jsonl")
    assert [(r["ask"], r["max_tokens"], r["thinking"], r["thinking_switch"]) for r in records] == [
        (1, THINKING_MAX_TOKENS, True, False), (2, 2 * THINKING_MAX_TOKENS, True, False),
        (3, DEFAULT_MAX_TOKENS, False, True)]
    assert [r["finish_reason"] for r in records] == ["length", "length", "stop"]


def test_a_cut_reasoning_ask_of_a_two_ask_call_is_asked_again_at_twice_the_budget():
    """cut -> ok on the second ask: with the default two asks the second is the doubled one,
    reasoning on (seen in review: the second ask of a default call went straight to the
    reasoning-off 4,096, so the 65,536 rung was reachable in no shipped call path)."""
    server = scripted(cut(), OK)
    obj, _ = ask(server.client(), "repair")
    assert obj.text == "ok"
    assert [b["max_tokens"] for b in server.bodies] == [THINKING_MAX_TOKENS,
                                                        2 * THINKING_MAX_TOKENS]
    assert all("chat_template_kwargs" not in b and b["top_p"] == 0.95 for b in server.bodies)
    assert server.bodies[1]["messages"] == server.bodies[0]["messages"]


def test_doubling_stops_at_the_cap_and_a_budget_at_the_cap_drops_reasoning_at_once():
    server = scripted(cut(), cut(), OK)
    obj, _ = ask(server.client(thinking_max_tokens=40000), "taskgraph", max_attempts=4)
    assert obj.text == "ok"
    assert [b["max_tokens"] for b in server.bodies] == [40000, THINKING_MAX_TOKENS_CAP,
                                                        DEFAULT_MAX_TOKENS]
    assert [("chat_template_kwargs" in b) for b in server.bodies] == [False, False, True]

    server = scripted(cut(), OK)
    ask(server.client(thinking_max_tokens=THINKING_MAX_TOKENS_CAP), "taskgraph", max_attempts=3)
    assert [b["max_tokens"] for b in server.bodies] == [THINKING_MAX_TOKENS_CAP, DEFAULT_MAX_TOKENS]
    assert server.bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_cut_direct_ask_is_doubled_with_the_note():
    server = scripted(cut('{"text": "never clo'), OK)
    assert ask(server.client())[0].text == "ok"
    assert [b["max_tokens"] for b in server.bodies] == [DEFAULT_MAX_TOKENS, 2 * DEFAULT_MAX_TOKENS]
    assert all(b["chat_template_kwargs"] == {"enable_thinking": False} for b in server.bodies)
    assert f"cut off at max_tokens={DEFAULT_MAX_TOKENS}" in server.bodies[1]["messages"][1]["content"]


def test_a_schema_error_keeps_the_budget_and_the_reasoning():
    server = scripted(WRONG, OK)
    assert ask(server.client(), "repair")[0].text == "ok"
    assert [b["max_tokens"] for b in server.bodies] == [THINKING_MAX_TOKENS] * 2
    assert all(b["top_p"] == 0.95 and "chat_template_kwargs" not in b for b in server.bodies)
    retry = server.bodies[1]["messages"][1]["content"]
    assert "text: Input should be a valid string" in retry and '{"text": 1}' in retry

    # after a doubled ask that fails the schema, the third ask keeps the doubled budget
    server = scripted(cut(), WRONG, OK)
    assert ask(server.client(), "repair", max_attempts=3)[0].text == "ok"
    assert [b["max_tokens"] for b in server.bodies] == [THINKING_MAX_TOKENS,
                                                        2 * THINKING_MAX_TOKENS,
                                                        2 * THINKING_MAX_TOKENS]
    assert all("chat_template_kwargs" not in b for b in server.bodies)


def test_the_doubled_ask_gets_proportionally_more_time():
    client = Server(ok).client(thinking_timeout_s=300, timeout_s=120)
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    doubled, sent = client._params("repair", ULTRA, messages, ExplainText, RequestMode(),
                                   Rung(thinking=True, max_tokens=2 * THINKING_MAX_TOKENS))
    assert (doubled["timeout"], doubled["max_tokens"], sent) == (600.0, 2 * THINKING_MAX_TOKENS, False)
    base, _ = client._params("repair", ULTRA, messages, ExplainText, RequestMode(),
                             Rung(thinking=True, max_tokens=THINKING_MAX_TOKENS))
    assert base["timeout"] == 300.0 and base["top_p"] == 0.95
    direct, sent = client._params("repair", ULTRA, messages, ExplainText, RequestMode(),
                                  Rung(thinking=False, max_tokens=DEFAULT_MAX_TOKENS))
    assert (direct["timeout"], direct["temperature"], sent) == (120.0, 0.4, True)
    assert direct["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "top_p" not in direct


def test_env_override_of_the_reasoning_budget_feeds_the_ladder(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOONWRIGHT_THINKING_MAX_TOKENS", "2048")
    server = scripted(cut(), cut(), OK)
    assert ask(server.client(), "repair", max_attempts=3)[0].text == "ok"
    assert [b["max_tokens"] for b in server.bodies] == [2048, 4096, 8192]
    assert not any("chat_template_kwargs" in b for b in server.bodies)


def test_a_budget_below_the_default_from_the_environment_is_warned_about_once(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    """Review: a backend/.env made with ``cp .env.example .env`` before phase 3e pins
    MOONWRIGHT_THINKING_MAX_TOKENS=16384 and silently undoes the 32,768 default, the budget
    that was cut on 78 of 190 Ultra calls. The client says so once; an explicit argument is
    the caller's choice and is not warned about. The eval prints the effective budgets."""
    tokenfactory._LOW_BUDGET_WARNED.discard(16384)
    monkeypatch.setenv("MOONWRIGHT_THINKING_MAX_TOKENS", "16384")
    with caplog.at_level(logging.WARNING, logger="moonwright.llm.tokenfactory"):
        client = Server(ok).client()
        Server(ok).client()
    warned = [r.getMessage() for r in caplog.records
              if "MOONWRIGHT_THINKING_MAX_TOKENS" in r.getMessage()]
    assert warned == [
        ("MOONWRIGHT_THINKING_MAX_TOKENS=16384 is below the 32,768-token default: the first live "
         "evaluation had 35 % of its reasoning calls cut at 16,384 tokens. A backend/.env copied "
         "from the pre-phase-3e .env.example pins 16384; delete that line (or raise it) to use "
         "the default.")]
    assert client.thinking_max_tokens == 16384
    assert client.budgets == "reasoning 16,384 tokens (x2 when cut, up to 65,536), direct 4,096"
    caplog.clear()
    monkeypatch.delenv("MOONWRIGHT_THINKING_MAX_TOKENS")
    with caplog.at_level(logging.WARNING, logger="moonwright.llm.tokenfactory"):
        assert Server(ok).client(thinking_max_tokens=1024).thinking_max_tokens == 1024
        assert Server(ok).client().thinking_max_tokens == THINKING_MAX_TOKENS
    assert not caplog.records
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert "\nMOONWRIGHT_THINKING_MAX_TOKENS=\n" in example   # empty: the code's default
    assert "delete or raise it" in example


def test_budgets_and_timeouts_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOONWRIGHT_MAX_TOKENS", "512")
    monkeypatch.setenv("MOONWRIGHT_THINKING_MAX_TOKENS", "2048")
    monkeypatch.setenv("MOONWRIGHT_LLM_TIMEOUT_S", "7")
    client = Server(ok).client()
    assert (client.max_tokens, client.thinking_max_tokens) == (512, 2048)
    assert (client.timeout_s, client.thinking_timeout_s) == (7.0, 7.0)
    monkeypatch.setenv("MOONWRIGHT_MAX_TOKENS", "lots")
    with pytest.raises(LLMError, match="MOONWRIGHT_MAX_TOKENS"):
        Server(ok).client()


def test_explicit_arguments_beat_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOONWRIGHT_LLM_TIMEOUT_S", "7")
    client = Server(ok).client(timeout_s=1.5, thinking_timeout_s=9, max_tokens=100,
                               thinking_max_tokens=200, thinking={"structure": True})
    assert (client.timeout_s, client.thinking_timeout_s) == (1.5, 9)
    assert (client.max_tokens, client.thinking_max_tokens) == (100, 200)
    assert client.thinking["structure"] is True and client.thinking["explain"] is False


# --------------------------------------------------------------------------- usage on failure
def test_failed_stage_keeps_its_token_usage():
    server = Server(lambda _r, _b: httpx.Response(200, json=completion('{"text": 1}')))
    with pytest.raises(SchemaMismatch) as info:
        ask(server.client())
    assert (info.value.usage.input_tokens, info.value.usage.output_tokens) == (20, 10)


def test_silent_model_keeps_its_token_usage():
    server = Server(lambda _r, _b: httpx.Response(200, json=completion("")))
    with pytest.raises(LLMError, match="empty completion") as info:
        ask(server.client())
    assert info.value.usage.output_tokens == 10


# --------------------------------------------------------------------------- rejected answers
def test_a_rejected_call_carries_the_copy_a_person_reads():
    """Every ask failed: the exception's text is the hint, ``failure`` the Failure the planner
    shows (one line per ask, ``schema:json`` / ``schema:shape``), ``errors`` the last ask's
    raw lines, ``asks`` the outcomes, ``usage`` the tokens of every ask."""
    server = scripted(cut(), cut(), WRONG)
    with pytest.raises(SchemaMismatch) as info:
        ask(server.client(), "taskgraph", max_attempts=3)
    exc = info.value
    hint = ("Nemotron 3 Ultra did not return a valid task graph after 3 asks: the answer was "
            "cut off at 32,768 tokens, then at 65,536 tokens, then failed the schema")
    assert str(exc) == hint
    assert exc.failure == Failure(
        kind="schema", conflicts=["schema:json", "schema:shape"], hint=hint,
        details=["ask 1: cut off at 32,768 tokens", "ask 2: cut off at 65,536 tokens",
                 "ask 3 (reasoning off): 'text' must be a string"])
    assert exc.errors == ["text: Input should be a valid string"] and exc.raw == '{"text": 1}'
    assert exc.usage.output_tokens == 15 and [a.kind for a in exc.asks] == ["cut", "cut", "shape"]
    assert [(a.max_tokens, a.thinking, a.reasoning_off) for a in exc.asks] == [
        (32768, True, False), (65536, True, False), (4096, False, True)]


def test_the_summary_names_what_came_back_and_counts_repeats():
    server = scripted(completion("Sure! Here is my plan in prose."), WRONG)
    with pytest.raises(SchemaMismatch) as info:
        ask(server.client())
    assert str(info.value) == ("Nemotron 3.5 Lightning did not return valid requirements after "
                               "2 asks: the answer was not JSON, then failed the schema")
    assert info.value.failure.details == ["ask 1: no JSON object found in model output",
                                          "ask 2: 'text' must be a string"]
    assert info.value.failure.conflicts == ["schema:json", "schema:shape"]

    server = scripted(WRONG, completion('{"text": 2}'))
    with pytest.raises(SchemaMismatch) as info:
        ask(server.client())
    assert str(info.value).endswith(": the answer failed the schema twice")
    assert info.value.failure.conflicts == ["schema:shape"]

    # a direct role already at the cap is cut at the same figure twice
    server = scripted(cut('{"text": "x'), cut('{"text": "x'))
    with pytest.raises(SchemaMismatch) as info:
        ask(server.client(max_tokens=THINKING_MAX_TOKENS_CAP))
    assert str(info.value).endswith(": the answer was cut off at 65,536 tokens twice")
    assert info.value.failure.conflicts == ["schema:json"]
    assert info.value.failure.details == ["ask 1: cut off at 65,536 tokens",
                                          "ask 2: cut off at 65,536 tokens"]

    def outcome(ask: int, kind: str, max_tokens: int = 4096) -> AskOutcome:
        return AskOutcome(ask=ask, kind=kind, max_tokens=max_tokens, thinking=False)  # type: ignore[arg-type]

    assert rejection_summary([outcome(k, "shape") for k in range(1, 5)]) == \
        "the answer failed the schema 4 times"
    assert rejection_summary([outcome(1, "cut", 1), outcome(2, "cut", 2), outcome(3, "cut", 2),
                              outcome(4, "empty"), outcome(5, "shape")]) == \
        "the answer was cut off at 1 token, then at 2 tokens twice, then was empty, then failed the schema"
    assert rejection_summary([outcome(1, "json"), outcome(2, "json"), outcome(3, "json")]) == \
        "the answer was not JSON three times"


def test_a_model_silent_on_every_ask_is_an_llm_error_with_the_copy():
    server = Server(lambda _r, _b: httpx.Response(200, json=completion("")))
    with pytest.raises(LLMError, match="empty completion") as info:
        ask(server.client())
    assert not isinstance(info.value, SchemaMismatch)
    assert str(info.value) == ("Nemotron 3.5 Lightning returned an empty completion on each of "
                               "2 asks")
    assert info.value.failure == Failure(
        kind="llm_error", hint=str(info.value),
        details=["ask 1: empty completion", "ask 2: empty completion"])

    # every ask cut with nothing after the reasoning: the budgets tell the story
    server = scripted(cut(), cut())
    with pytest.raises(LLMError, match="empty completion") as info:
        ask(server.client(), "repair")
    assert str(info.value) == ("Nemotron 3 Ultra returned an empty completion on each of 2 asks: "
                               "the answer was cut off at 32,768 tokens, then at 65,536 tokens")
    assert info.value.failure.details == ["ask 1: cut off at 32,768 tokens",
                                          "ask 2: cut off at 65,536 tokens"]
    assert info.value.failure.conflicts == [] and info.value.usage.output_tokens == 10

    server = scripted(cut())
    with pytest.raises(LLMError, match="on its single ask: the answer was cut off at 32,768 tokens"):
        ask(server.client(), "repair", max_attempts=1)


def test_a_failed_request_names_the_error_and_the_model():
    def timeout(request: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=request)

    with pytest.raises(LLMError, match="APITimeoutError") as info:
        ask(Server(timeout).client())
    hint = ("The Token Factory request to Nemotron 3.5 Lightning failed: APITimeoutError "
            "(Request timed out.)")
    assert str(info.value) == hint
    assert info.value.failure == Failure(kind="llm_error", hint=hint, details=[
        "APITimeoutError: Request timed out.", f"structure ({LIGHTNING}) on {GLOBAL}"])
    assert info.value.usage.output_tokens == 0 and info.value.asks == ()

    # the server's own message is the reason, the request id is kept for support
    server = Server(lambda _r, _b: tagged("This model's maximum context length is 4096",
                                         request_id="req-400", status=400))
    with pytest.raises(LLMError, match="BadRequestError") as info:
        ask(server.client(), "taskgraph")
    assert str(info.value) == ("The Token Factory request to Nemotron 3 Ultra failed: "
                               "BadRequestError (This model's maximum context length is 4096)")
    assert info.value.failure.details[1] == f"taskgraph ({ULTRA}) on {GLOBAL}, request id req-400"
    assert info.value.failure.details[0].startswith("BadRequestError: Error code: 400")

    with pytest.raises(LLMError, match=r"NotFoundError \(model not found\)"):
        ask(Server(lambda _r, _b: error(404, "model not found")).client())

    # a failure on the second ask keeps the first ask's line and its tokens
    replies = iter([httpx.Response(200, json=WRONG)])

    def then_timeout(request: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
        try:
            return next(replies)
        except StopIteration:
            raise httpx.ReadTimeout("slow upstream", request=request) from None

    with pytest.raises(LLMError, match="APITimeoutError") as info:
        ask(Server(then_timeout).client())
    assert info.value.failure.details == [
        "ask 1: 'text' must be a string", "APITimeoutError: Request timed out.",
        f"structure ({LIGHTNING}) on {GLOBAL}"]
    assert info.value.usage.output_tokens == 5 and [a.kind for a in info.value.asks] == ["shape"]


def test_schema_errors_are_described_by_the_item_they_name():
    data = {"tasks": [{"id": "T1", "unit": "kg"}, {"id": "T3", "after": "T1"}, {"name": "x"}],
            "robots": [{"id": "HL1", "battery_wh": "lots"}],
            "site": {"zones": [{"id": "Z1"}, {"id": "Z2", "radius": 0}]}}
    assert describe_schema_errors([
        "tasks.1.after: Input should be a valid list",
        "tasks.2.id: Field required",
        "tasks.1.duration_h: Input should be greater than 0",
        ": Input should be a valid dictionary or instance of TaskGraph",
        "tasks: Field required",
        "tasks.1.after.0: Input should be a valid string",
        "robots.0.battery_wh: Input should be a valid number, unable to parse string as a number",
        "site.zones.1.radius: Input should be greater than or equal to 1",
        "tasks.0.unit: Input should be 'm3', 'm2' or 'm'",
        "tasks.0.foo: Extra inputs are not permitted",
        "tasks.0.name: String should have at least 1 character",
        "tasks.1: Input should be a valid dictionary or instance of Task",
    ], data) == [
        "task T3: 'after' must be a list",
        "tasks[2]: 'id' is missing",
        "task T3: 'duration_h' must be greater than 0",
        "the answer must be an object",
        "'tasks' is missing",
        "task T3: 'after[0]' must be a string",
        "robot HL1: 'battery_wh' must be a number",
        "zone Z2: 'radius' must be greater than or equal to 1",
        "task T1: 'unit' must be 'm3', 'm2' or 'm'",
        "task T1: 'foo' is not a field of the schema",
        "task T1: 'name' string should have at least 1 character",
        "task T3 must be an object",
    ]
    # an answer that is not even an object, or lacks the list, is described by position
    assert describe_schema_errors(["tasks.0.after: Input should be a valid list"], []) == \
        ["tasks[0]: 'after' must be a list"]
    # review: pydantic wording that still leaked (a regex shown to the user, "after
    # validation", a model validator's "Value error, ")
    assert describe_schema_errors([
        "tasks: List should have at least 1 item after validation, not 0",
        "robots: List should have at least 2 items after validation, not 1",
        "tasks.0.id: String should match pattern '^[A-Za-z0-9_.-]+$'",
        "tasks.0.zone: String should match pattern '^Z[0-9]+$'",
        "site.zones.1: Value error, radius must be a finite number",
        "tasks.0: Assertion failed, the task has no robot",
    ], {"tasks": [{"id": "T 1"}], "site": {"zones": [{"id": "Z1"}, {"id": "Z2"}]}}) == [
        "'tasks' must not be empty",
        "'robots' must have at least 2 items",
        "task T 1: 'id' may use only letters, digits and _ . -",
        "task T 1: 'zone' has an invalid format",
        "zone Z2: radius must be a finite number",
        "task T 1: the task has no robot",
    ]
    # the real thing: a task graph whose third task has a string for `after`, then the bare
    # list of tasks the live UI showed as ": Input should be a valid dictionary or instance of
    # TaskGraph". A schema violation keeps the rung (reasoning on), so ask 2 is no
    # "(reasoning off)" ask; only a cut answer moves down the ladder
    server = scripted(completion(json.dumps({"tasks": [
        {"id": "T1", "name": "Survey", "robot_type": "surveyor", "duration_h": 1, "energy_wh": 10,
         "zone": "Z1"},
        {"id": "T2", "name": "Dig", "robot_type": "excavator", "duration_h": 4, "energy_wh": 800,
         "zone": "Z1", "after": ["T1"]},
        {"id": "T3", "name": "Haul", "robot_type": "hauler", "duration_h": 2, "energy_wh": 300,
         "zone": "Z1", "after": "T2"}]})), completion('[{"id": "T1", "name": "Survey"}]'))
    with pytest.raises(SchemaMismatch) as info:
        server.client().complete_json(role="repair", system="S", user="U", schema=TaskGraph)
    assert info.value.failure.details == ["ask 1: task T3: 'after' must be a list",
                                          "ask 2: the answer must be an object"]
    assert info.value.failure.conflicts == ["schema:shape"]
    assert all("chat_template_kwargs" not in b for b in server.bodies)


def test_the_detail_line_names_at_most_three_violations():
    outcome = AskOutcome(ask=2, kind="shape", max_tokens=4096, thinking=False,
                         reasons=tuple(f"task T{i}: 'after' must be a list" for i in range(1, 6)))
    assert outcome.line == ("ask 2: task T1: 'after' must be a list; task T2: 'after' must be a "
                            "list; task T3: 'after' must be a list; and 2 more")
    assert outcome.conflict == "schema:shape" and outcome.phrase == "failed the schema"
    cut_off = AskOutcome(ask=1, kind="cut", max_tokens=32768, thinking=True, empty=True)
    assert cut_off.line == "ask 1: cut off at 32,768 tokens" and cut_off.conflict == "schema:json"


def test_model_short_names():
    assert model_name("nvidia/Nemotron-3-Ultra-550b-a55b") == "Nemotron 3 Ultra"
    assert model_name("nvidia/nemotron-3-super-120b-a12b") == "Nemotron 3 Super"
    assert model_name("nvidia/Nemotron-3_5-Lightning") == "Nemotron 3.5 Lightning"
    assert model_name("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B") == "Nemotron 3 Nano"
    assert model_name("fixture:taskgraph_1.json") == "fixture:taskgraph_1.json"
    assert model_name("scripted") == "scripted"
    assert {model_name(m) for m in MODEL_DEFAULTS.values()} == {
        "Nemotron 3.5 Lightning", "Nemotron 3 Ultra", "Nemotron 3 Super"}


# --------------------------------------------------------------------------- call log
def tagged(text: str, request_id: str = "req-abc", status: int = 200) -> httpx.Response:
    body = completion(text) if status == 200 else {"error": {"message": text}}
    return httpx.Response(status, json=body, headers={"x-request-id": request_id})


def read_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_log_path_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("MOONWRIGHT_LLM_LOG")
    assert log_path_from_env() == DEFAULT_LOG_PATH
    assert DEFAULT_LOG_PATH.parts[-2:] == ("out", "llm_log.jsonl")
    monkeypatch.setenv("MOONWRIGHT_LLM_LOG", " OFF ")
    assert log_path_from_env() is None
    monkeypatch.setenv("MOONWRIGHT_LLM_LOG", str(tmp_path / "calls.jsonl"))
    assert log_path_from_env() == tmp_path / "calls.jsonl"
    # an explicit argument beats the environment
    assert log_path_from_env("off") is None
    assert log_path_from_env(tmp_path / "x.jsonl") == tmp_path / "x.jsonl"
    assert log_path_from_env("") == DEFAULT_LOG_PATH


def test_every_request_is_logged_without_prompt_or_answer_text(tmp_path: Path):
    server = Server(lambda _r, _b: tagged('{"text": "the answer"}'))
    path = tmp_path / "calls.jsonl"
    client = server.client(log_path=path)
    obj, _ = client.complete_json(role="taskgraph", system="SECRET SYSTEM PROMPT",
                                  user="SECRET USER BRIEF", schema=ExplainText,
                                  key="demo", attempt=2, variant="replan")
    assert obj.text == "the answer"
    records = read_log(path)
    assert len(records) == 1 and client.log.records == 1 and client.log.enabled
    rec = records[0]
    assert rec["role"] == "taskgraph" and rec["model"] == MODEL_DEFAULTS["taskgraph"]
    assert rec["endpoint"] == GLOBAL and rec["regional"] is False
    assert rec["json_format"] == "json_schema"
    assert rec["thinking"] is True and rec["thinking_switch"] is False
    assert rec["max_tokens"] == THINKING_MAX_TOKENS
    assert rec["status"] == 200 and rec["request_id"] == "req-abc"
    assert rec["finish_reason"] == "stop"
    assert (rec["prompt_tokens"], rec["completion_tokens"]) == (10, 5)
    assert rec["latency_s"] >= 0 and rec["error"] is None and rec["error_message"] is None
    assert (rec["key"], rec["attempt"], rec["variant"], rec["ask"]) == ("demo", 2, "replan", 1)
    assert rec["ts"].endswith("+00:00")
    raw = path.read_text(encoding="utf-8")
    assert "SECRET" not in raw and "the answer" not in raw


def test_structure_role_logs_the_thinking_switch_and_the_ask_number(tmp_path: Path):
    replies = iter(['{"text": 1}', '{"text": "fixed"}'])
    server = Server(lambda _r, _b: tagged(next(replies)))
    client = server.client(log_path=tmp_path / "calls.jsonl")
    assert ask(client)[0].text == "fixed"
    records = read_log(tmp_path / "calls.jsonl")
    assert [r["ask"] for r in records] == [1, 2]
    assert all(r["thinking"] is False and r["thinking_switch"] is True for r in records)
    assert all(r["max_tokens"] == DEFAULT_MAX_TOKENS and r["role"] == "structure"
               for r in records)


def test_rejected_rungs_and_transport_failures_are_logged(tmp_path: Path):
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if fmt(body) == "json_schema":
            return tagged("response_format is not supported", request_id="req-400", status=400)
        return tagged('{"text": "ok"}', request_id="req-200")

    server = Server(handler)
    client = server.client(log_path=tmp_path / "calls.jsonl")
    assert ask(client)[0].text == "ok"
    first, second = read_log(tmp_path / "calls.jsonl")
    assert (first["status"], first["request_id"], first["error"]) == (400, "req-400",
                                                                     "BadRequestError")
    assert "not supported" in first["error_message"] and first["json_format"] == "json_schema"
    assert first["finish_reason"] is None and first["prompt_tokens"] is None
    assert (second["status"], second["request_id"], second["error"]) == (200, "req-200", None)
    assert second["json_format"] == "json_object"

    def timeout(request: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=request)

    server = Server(timeout)
    client = server.client(log_path=tmp_path / "timeouts.jsonl")
    with pytest.raises(LLMError, match="APITimeoutError"):
        ask(client)
    (rec,) = read_log(tmp_path / "timeouts.jsonl")
    assert rec["status"] is None and rec["request_id"] is None
    assert rec["error"] == "APITimeoutError" and rec["latency_s"] >= 0


def test_regional_fallback_logs_both_endpoints(tmp_path: Path):
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if request.url.host == "global.test":
            return tagged("model does not exist", request_id="req-404", status=404)
        return tagged('{"text": "ok"}')

    client = Server(handler).client(log_path=tmp_path / "calls.jsonl")
    assert ask(client, "taskgraph")[0].text == "ok"
    first, second = read_log(tmp_path / "calls.jsonl")
    assert (first["endpoint"], first["regional"], first["status"]) == (GLOBAL, False, 404)
    assert first["error"] == "NotFoundError"
    assert (second["endpoint"], second["regional"], second["status"]) == (REGIONAL, True, 200)


def test_log_off_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client = Server(ok).client(log_path="off")
    assert client.log.enabled is False and client.log.path is None
    assert ask(client)[0].text == "ok"
    assert client.log.records == 0 and list(tmp_path.iterdir()) == []
    monkeypatch.setenv("MOONWRIGHT_LLM_LOG", "off")
    assert Server(ok).client().log.path is None
    # the default from the (test) environment is used when nothing is passed
    monkeypatch.setenv("MOONWRIGHT_LLM_LOG", str(tmp_path / "from_env.jsonl"))
    client = Server(ok).client()
    ask(client)
    assert (tmp_path / "from_env.jsonl").exists() and client.log.records == 1


def test_unwritable_log_disables_itself_and_never_fails_the_call(tmp_path: Path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    client = Server(ok).client(log_path=blocker / "calls.jsonl")
    with caplog.at_level(logging.WARNING, logger="moonwright.llm.tokenfactory"):
        assert ask(client)[0].text == "ok"
        assert ask(client)[0].text == "ok"
    assert client.log.enabled is False and client.log.records == 0
    assert sum("LLM call log disabled" in r.message for r in caplog.records) == 1


def test_plain_create_path_when_the_sdk_has_no_raw_response(tmp_path: Path):
    def create(**params: Any) -> ChatCompletion:
        return ChatCompletion.model_validate(completion('{"text": "plain"}'))

    stub_sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = Server(ok).client(log_path=tmp_path / "calls.jsonl")
    client._sdk = lambda endpoint: stub_sdk  # type: ignore[method-assign]
    assert ask(client)[0].text == "plain"
    (rec,) = read_log(tmp_path / "calls.jsonl")
    assert rec["status"] is None and rec["request_id"] is None
    assert rec["finish_reason"] == "stop" and rec["completion_tokens"] == 5


def test_call_log_is_safe_under_threads(tmp_path: Path):
    client = Server(lambda _r, _b: tagged('{"text": "ok"}')).client(log_path=tmp_path / "c.jsonl")

    def work() -> None:
        for _ in range(5):
            ask(client)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = read_log(tmp_path / "c.jsonl")
    assert len(records) == 40 and client.log.records == 40
    assert all(r["status"] == 200 for r in records)


def test_call_log_write_returns_whether_it_wrote(tmp_path: Path):
    assert CallLog(None).write({"a": 1}) is False
    sink = CallLog(tmp_path / "nested" / "dir" / "log.jsonl")
    assert sink.write({"a": 1, "when": Path("x")}) is True   # non-JSON values are stringified
    assert read_log(sink.path) == [{"a": 1, "when": "x"}]


# --------------------------------------------------------------------------- the ladder spans re-asks
def test_the_planners_re_ask_starts_where_the_failed_call_left_the_ladder():
    """A call of two asks reaches 32,768 and 65,536; when both are cut the planner's re-ask
    (same role, key and variant, the next attempt, same thread) answers without reasoning at
    the normal budget with the compact-JSON note, instead of starting at 32,768 again. Any
    other call starts at the default."""
    server = scripted(cut(), cut(), OK, OK, OK)
    client = server.client()
    with pytest.raises(LLMError):
        ask(client, "taskgraph", key="pad", attempt=1)
    assert ask(client, "taskgraph", key="pad", attempt=2)[0].text == "ok"
    third = server.bodies[2]
    assert third["max_tokens"] == DEFAULT_MAX_TOKENS
    assert third["chat_template_kwargs"] == {"enable_thinking": False}
    note = third["messages"][1]["content"]
    assert note.startswith("U") and f"cut off at max_tokens={THINKING_MAX_TOKENS_CAP}" in note
    assert "compact JSON only, no reasoning" in note
    # the carry is spent: the next call of the run starts at the default again
    ask(client, "taskgraph", key="pad", attempt=3)
    assert server.bodies[3]["max_tokens"] == THINKING_MAX_TOKENS
    # another key, another role or a later attempt never picks a carry up
    server = scripted(cut(), cut(), OK, cut(), cut(), OK, cut(), cut(), OK)
    client = server.client()
    for follow in ({"key": "road", "attempt": 2}, {"key": "pad", "attempt": 3}):
        with pytest.raises(LLMError):
            ask(client, "repair", key="pad", attempt=1)
        ask(client, "repair", **follow)
        assert server.bodies[-1]["max_tokens"] == THINKING_MAX_TOKENS, follow
    with pytest.raises(LLMError):
        ask(client, "repair", key="pad", attempt=1)
    ask(client, "taskgraph", key="pad", attempt=2)
    assert server.bodies[-1]["max_tokens"] == THINKING_MAX_TOKENS


def test_a_doubled_budget_that_failed_the_schema_is_where_the_re_ask_starts():
    """32,768 cut, 65,536 answered off the schema: the re-ask keeps the budget that held the
    reasoning, with reasoning on and without the client's note (the planner's own context
    quotes the failure)."""
    server = scripted(cut(), WRONG, OK)
    client = server.client()
    with pytest.raises(SchemaMismatch):
        ask(client, "repair", key="pad", attempt=4)
    ask(client, "repair", key="pad", attempt=5)
    assert server.bodies[2]["max_tokens"] == THINKING_MAX_TOKENS_CAP
    assert "chat_template_kwargs" not in server.bodies[2]
    assert server.bodies[2]["messages"][1]["content"] == "U"
    # a failure at the starting rung carries nothing
    server = scripted(WRONG, WRONG, OK)
    client = server.client()
    with pytest.raises(SchemaMismatch):
        ask(client, "repair", key="pad", attempt=1)
    ask(client, "repair", key="pad", attempt=2)
    assert server.bodies[2]["max_tokens"] == THINKING_MAX_TOKENS
    assert server.bodies[2]["messages"][1]["content"] == "U"


def test_the_carry_is_per_thread():
    """The eval runs repeats of one brief (one fixture key) in parallel threads: a carry left
    by one thread's failed call is never picked up by another's."""
    server = scripted(cut(), cut(), OK)
    client = server.client()
    with pytest.raises(LLMError):
        ask(client, "taskgraph", key="pad", attempt=1)
    done: list[int] = []
    worker = threading.Thread(target=lambda: done.append(
        ask(client, "taskgraph", key="pad", attempt=2)[1].output_tokens))
    worker.start()
    worker.join()
    assert done == [5] and server.bodies[2]["max_tokens"] == THINKING_MAX_TOKENS


def test_a_planner_whose_every_reasoning_ask_is_cut_walks_the_whole_ladder_in_six_asks():
    """The review's always-cut server: the first graph used to cost 6 asks at [32768, 4096,
    32768, 4096, 32768, 4096], three of them paying for 32k reasoning tokens and the 65,536
    rung never asked. Now the same 6 asks (the count is unchanged) climb the ladder once."""
    from moonwright.pipeline.loop import Planner
    from moonwright.schemas import Requirements

    server = Server(lambda _r, _b: httpx.Response(200, json=cut()))
    req = Requirements.model_validate_json(
        (Path(__file__).resolve().parents[1] / "fixtures/landing_pad/structure.json").read_text())
    planner = Planner(server.client(), max_repairs=3, explain=False, suggest_budget_s=0)
    result = planner.plan_from_requirements(req, key="landing_pad")
    assert not result.success and [s.stage for s in result.steps] == ["taskgraph"] * 3
    assert [b["max_tokens"] for b in server.bodies] == [
        THINKING_MAX_TOKENS, THINKING_MAX_TOKENS_CAP, DEFAULT_MAX_TOKENS,
        2 * DEFAULT_MAX_TOKENS, 4 * DEFAULT_MAX_TOKENS, 8 * DEFAULT_MAX_TOKENS]
    assert [("chat_template_kwargs" in b) for b in server.bodies] == [False, False] + [True] * 4
