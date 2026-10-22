"""Token Factory client: the learned request ladder, thinking control and endpoint fallback.

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

from moonwright.llm.base import ExplainText, LLMError, SchemaMismatch
from moonwright.llm.tokenfactory import (
    DEFAULT_BASE_URL,
    DEFAULT_LOG_PATH,
    DEFAULT_MAX_TOKENS,
    MODEL_DEFAULTS,
    REGIONAL_BASE_URL,
    THINKING_DEFAULTS,
    THINKING_MAX_TOKENS,
    CallLog,
    RequestMode,
    TokenFactoryClient,
    log_path_from_env,
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
