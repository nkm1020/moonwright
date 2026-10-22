"""Token Factory client: the learned request ladder, thinking control and endpoint fallback.

No network: an ``httpx.MockTransport`` plays the server and records every request URL and body.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from moonwright.llm.base import ExplainText, LLMError, SchemaMismatch
from moonwright.llm.tokenfactory import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    MODEL_DEFAULTS,
    REGIONAL_BASE_URL,
    THINKING_DEFAULTS,
    THINKING_MAX_TOKENS,
    RequestMode,
    TokenFactoryClient,
    response_format_for,
    thinking_from_env,
)

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


def ask(client: TokenFactoryClient, role: str = "structure") -> tuple[ExplainText, Any]:
    return client.complete_json(role=role, system="S", user="U", schema=ExplainText)


def fmt(body: dict[str, Any]) -> str | None:
    return body.get("response_format", {}).get("type")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


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


def test_thinking_on_truncation_names_the_bigger_budget():
    replies = iter([completion("", "length"), completion('{"text": "ok"}')])
    server = Server(lambda _r, _b: httpx.Response(200, json=next(replies)))
    obj, _ = ask(server.client(), "repair")
    assert obj.text == "ok"
    assert server.bodies[0]["max_tokens"] == THINKING_MAX_TOKENS
    retry = server.bodies[1]["messages"][1]["content"]
    assert f"cut off at max_tokens={THINKING_MAX_TOKENS}" in retry


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
