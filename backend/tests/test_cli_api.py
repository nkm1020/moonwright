"""Tests for the user-facing entry points: live client (mocked transport), factory, CLI, API.

No network: the Token Factory client is exercised through an ``httpx.MockTransport`` injected
as the SDK's http client. The end-to-end ``plan`` test needs the validator / scheduler modules
and the example fixtures from the other builders and skips until they land.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import inspect
import io
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from moonwright import api as api_module
from moonwright import cli as cli_module
from moonwright.api import (
    EXAMPLES_DIR,
    app,
    brief_title,
    clean_brief_id,
    list_briefs,
    strip_markdown,
    thin_frames,
)
from moonwright.cli import (
    ARM_MODELS,
    EXIT_ERROR,
    EXIT_NO_PLAN,
    EXIT_OK,
    EvalReport,
    EvalRow,
    GraphCapture,
    build_parser,
    condition_name,
    csv_row,
    eval_markdown,
    eval_row,
    format_gantt,
    format_replan,
    format_step,
    format_summary,
    main,
    make_arm_client,
    matrix_cell,
    parse_arms,
    parse_failure,
    plan_file_name,
    plan_stem,
    planner_options,
    positive_float,
    read_csv,
    run_cost_usd,
    structure_file_name,
    summarise,
    thinking_overrides,
    wilson_interval,
)
from moonwright.llm import factory
from moonwright.llm.base import (
    FIXTURES_DIR,
    ExplainText,
    FixtureClient,
    FixtureMissing,
    LLMError,
    SchemaMismatch,
    Usage,
    extract_json,
    fixture_filename,
    strip_thinking,
    validate_into,
)
from moonwright.llm.factory import make_client, resolve_kind
from moonwright.llm.prices import PRICES_USD_PER_M, cost_usd, format_usd, price_for
from moonwright.llm.tokenfactory import (
    DEFAULT_BASE_URL,
    MODEL_DEFAULTS,
    RecordingClient,
    TokenFactoryClient,
)
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.schedule import check_schedule
from moonwright.schemas import (
    Event,
    Failure,
    LoopStep,
    PlanResult,
    ReplanRecord,
    Requirements,
    Robot,
    RobotState,
    Schedule,
    ScheduleEntry,
    SimFrame,
    SimResult,
    Site,
    Suggestion,
    Task,
    TaskGraph,
    Zone,
)
from moonwright.sim.montecarlo import MonteCarloPlanner, MonteCarloResult, SolverOnlyClient
from moonwright.sim.simulator import FailureInjection

Reply = str | Callable[[httpx.Request], httpx.Response]
SCHEDULE_CHECK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "llm_schedule_check.py"


@pytest.fixture(autouse=True)
def llm_log_in_tmp(tmp_path, monkeypatch):
    """Every live client built by these tests logs into the test's tmp dir, never into
    ``backend/out/llm_log.jsonl``."""
    monkeypatch.setenv("MOONWRIGHT_LLM_LOG", str(tmp_path / "llm_log.jsonl"))


# --------------------------------------------------------------------------- fake Token Factory
def completion(text: str, *, prompt_tokens: int = 10, completion_tokens: int = 5,
               finish_reason: str = "stop", model: str = "fake-model") -> dict[str, Any]:
    return {
        "id": "cmpl-1", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


class FakeServer:
    """Mock transport: one scripted reply per request (the last one repeats), bodies recorded."""

    def __init__(self, *replies: Reply):
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=completion(reply))

    def client(self, **kwargs: Any) -> TokenFactoryClient:
        kwargs.setdefault("transport_retries", 0)
        return TokenFactoryClient(
            api_key="test-key", base_url="https://tokenfactory.test/v1/",
            http_client=httpx.Client(transport=httpx.MockTransport(self)), **kwargs,
        )


def http_error(status: int, message: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json={"error": {"message": message}})


def raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow upstream", request=request)


def raise_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def reject_json_mode(request: httpx.Request) -> httpx.Response:
    if "response_format" in json.loads(request.content):
        return httpx.Response(400, json={"error": {
            "message": "response_format is not supported for this model"}})
    return httpx.Response(200, json=completion('{"text": "plain mode"}'))


def ask(client: TokenFactoryClient, role: str = "structure", schema: type = ExplainText, **kw: Any):
    return client.complete_json(role=role, system="SYS", user="USER", schema=schema, **kw)


# --------------------------------------------------------------------------- TokenFactoryClient
class TestTokenFactoryClient:
    def test_strips_thinking_and_fills_usage(self):
        server = FakeServer('<think>reasoning...</think>\n```json\n{"text": "hello"}\n```')
        obj, usage = ask(server.client())
        assert obj == ExplainText(text="hello")
        assert (usage.input_tokens, usage.output_tokens) == (10, 5)
        assert usage.model == "fake-model"
        assert usage.latency_s >= 0

    def test_request_shape_and_role_routing(self, monkeypatch):
        monkeypatch.setenv("MOONWRIGHT_MODEL_TASKGRAPH", "env/taskgraph-model")
        server = FakeServer('{"text": "ok"}')
        client = server.client(models={"structure": "arg/structure-model"})
        ask(client, "structure")
        ask(client, "taskgraph")
        ask(client, "explain")
        structure, taskgraph, explain = server.requests
        assert [m["role"] for m in structure["messages"]] == ["system", "user"]
        assert structure["messages"][0]["content"] == "SYS"
        assert structure["messages"][1]["content"] == "USER"
        # structure answers directly: strict json_schema, thinking switched off, 4096 tokens
        assert structure["response_format"]["type"] == "json_schema"
        assert structure["response_format"]["json_schema"]["name"] == "ExplainText"
        assert structure["response_format"]["json_schema"]["strict"] is True
        assert structure["response_format"]["json_schema"]["schema"]["type"] == "object"
        assert structure["chat_template_kwargs"] == {"enable_thinking": False}
        assert structure["max_tokens"] == 4096
        assert structure["model"] == "arg/structure-model"
        assert structure["temperature"] == 0.2
        assert "top_p" not in structure
        # taskgraph reasons: vendor sampling, bigger budget, no thinking switch
        assert taskgraph["model"] == "env/taskgraph-model"
        assert taskgraph["temperature"] == 1.0 and taskgraph["top_p"] == 0.95
        assert taskgraph["max_tokens"] == 32768   # THINKING_MAX_TOKENS (phase 3e token policy)
        assert "chat_template_kwargs" not in taskgraph
        assert explain["model"] == MODEL_DEFAULTS["explain"]
        assert explain["temperature"] == 0.2

    def test_reasks_once_with_validation_errors(self):
        server = FakeServer('{"text": 5}', '{"text": "fixed"}')
        obj, usage = ask(server.client())
        assert obj.text == "fixed"
        assert len(server.requests) == 2
        retry_user = server.requests[1]["messages"][1]["content"]
        assert retry_user.startswith("USER")
        assert "Your previous answer failed validation" in retry_user
        assert "text: Input should be a valid string" in retry_user
        assert retry_user.rstrip().endswith("Return only corrected JSON.")
        assert usage.input_tokens == 20 and usage.output_tokens == 10

    def test_reasks_when_no_json_in_answer(self):
        server = FakeServer("Sure! Here is my plan in prose.", '{"text": "json now"}')
        obj, _ = ask(server.client())
        assert obj.text == "json now"
        assert "json: no JSON object found" in server.requests[1]["messages"][1]["content"]

    def test_truncated_answer_is_reported(self):
        def cut(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=completion('{"text": "never clo', finish_reason="length"))

        server = FakeServer(cut, '{"text": "short"}')
        ask(server.client())
        assert "cut off at max_tokens=4096" in server.requests[1]["messages"][1]["content"]

    def test_schema_mismatch_after_max_attempts(self):
        server = FakeServer('{"text": 1}', '{"text": 2}')
        with pytest.raises(SchemaMismatch) as info:
            ask(server.client(), max_attempts=2)
        assert len(server.requests) == 2
        assert info.value.errors == ["text: Input should be a valid string"]
        assert info.value.raw == '{"text": 2}'

    def test_single_attempt_when_max_attempts_is_one(self):
        server = FakeServer('{"text": 1}')
        with pytest.raises(SchemaMismatch):
            ask(server.client(), max_attempts=1)
        assert len(server.requests) == 1

    def test_response_format_400_steps_down_the_ladder_and_is_remembered(self):
        server = FakeServer(reject_json_mode)
        client = server.client()
        assert ask(client)[0].text == "plain mode"
        assert ask(client)[0].text == "plain mode"
        assert len(server.requests) == 4
        assert server.requests[0]["response_format"]["type"] == "json_schema"
        assert server.requests[1]["response_format"] == {"type": "json_object"}
        assert "response_format" not in server.requests[2]
        assert "response_format" not in server.requests[3]
        assert client.mode(client.models["structure"]).json_format == "none"

    def test_other_400_is_an_llm_error(self):
        server = FakeServer(http_error(400, "context length exceeded"))
        with pytest.raises(LLMError, match="BadRequestError"):
            ask(server.client())
        assert len(server.requests) == 1

    def test_timeout_and_server_errors_map_to_llm_error(self):
        with pytest.raises(LLMError, match="APITimeoutError"):
            ask(FakeServer(raise_timeout).client())
        with pytest.raises(LLMError, match="InternalServerError"):
            ask(FakeServer(http_error(500, "boom")).client())

    def test_transport_retries(self, monkeypatch):
        monkeypatch.setattr(openai._base_client.SyncAPIClient, "_sleep_for_retry",
                            lambda self, **kw: None)
        server = FakeServer(raise_connect_error)
        with pytest.raises(LLMError, match="APIConnectionError"):
            ask(server.client(transport_retries=2))
        assert len(server.requests) == 3

    def test_empty_completion_is_an_llm_error(self):
        server = FakeServer(lambda request: httpx.Response(200, json=completion("   ")))
        with pytest.raises(LLMError, match="empty completion"):
            ask(server.client())
        assert len(server.requests) == 2                     # silent on every ask

    def test_empty_completion_is_reasked_like_a_truncated_answer(self):
        def spent(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=completion("", finish_reason="length"))

        server = FakeServer(spent, '{"text": "second try"}')
        obj, usage = ask(server.client())
        assert obj.text == "second try" and len(server.requests) == 2
        retry = server.requests[1]["messages"][1]["content"]
        assert "cut off at max_tokens=4096" in retry and "empty completion" in retry
        assert usage.output_tokens == 10

    def test_null_content_from_a_reasoning_parser_is_reasked(self):
        def null_content(request: httpx.Request) -> httpx.Response:
            body = completion("", finish_reason="length")
            body["choices"][0]["message"]["content"] = None
            return httpx.Response(200, json=body)

        server = FakeServer(null_content, '{"text": "ok"}')
        assert ask(server.client())[0].text == "ok" and len(server.requests) == 2

    def test_schema_failure_then_silence_is_a_schema_mismatch(self):
        server = FakeServer('{"text": 1}', lambda request: httpx.Response(200, json=completion("")))
        with pytest.raises(SchemaMismatch) as info:
            ask(server.client())
        assert info.value.errors == ["json: empty completion"] and info.value.raw == ""

    def test_orphan_think_close_tag_does_not_hide_the_answer(self):
        server = FakeServer('I need "after": [] and {"id": "T2"} next.\n</think>\n{"text": "ok"}')
        obj, _ = ask(server.client())
        assert obj.text == "ok" and len(server.requests) == 1

    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
        with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
            TokenFactoryClient()

    def test_env_configuration(self, monkeypatch):
        monkeypatch.setenv("NEBIUS_API_KEY", "k")
        monkeypatch.delenv("NEBIUS_BASE_URL", raising=False)
        assert TokenFactoryClient().base_url == DEFAULT_BASE_URL
        monkeypatch.setenv("NEBIUS_BASE_URL", "https://other.example/v1/")
        assert TokenFactoryClient().base_url == "https://other.example/v1/"


# --------------------------------------------------------------------------- RecordingClient
class StubClient:
    name = "stub"

    def __init__(self, payload: Any):
        self.payload = payload

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        return validate_into(schema, self.payload), Usage(model="stub")


GRAPH = {"tasks": [{"id": "T1", "name": "Survey", "robot_type": "surveyor", "duration_h": 2,
                    "energy_wh": 100, "zone": "Z1"}]}


class TestRecordingClient:
    def test_writes_fixture_files_in_layout(self, tmp_path):
        rec = RecordingClient(StubClient(GRAPH), tmp_path)
        rec.complete_json(role="taskgraph", system="s", user="u", schema=TaskGraph, key="demo")
        rec.complete_json(role="repair", system="s", user="u", schema=TaskGraph, key="demo",
                          attempt=2)
        rec.complete_json(role="taskgraph", system="s", user="u", schema=TaskGraph, key="demo",
                          variant="replan")
        names = sorted(p.name for p in (tmp_path / "demo").iterdir())
        assert names == ["taskgraph_1.json", "taskgraph_2.json", "taskgraph_replan.json"]
        assert len(rec.written) == 3
        assert json.loads((tmp_path / "demo" / "taskgraph_2.json").read_text())["tasks"][0]["id"] == "T1"

    def test_round_trips_through_fixture_client(self, tmp_path):
        rec = RecordingClient(StubClient({"text": "recorded"}), tmp_path)
        obj, usage = rec.complete_json(role="explain", system="s", user="u", schema=ExplainText,
                                       key="demo")
        assert obj.text == "recorded" and usage.model == "stub"
        replayed, _ = FixtureClient(tmp_path).complete_json(
            role="explain", system="s", user="u", schema=ExplainText, key="demo")
        assert replayed == obj

    def test_no_key_means_no_file(self, tmp_path):
        rec = RecordingClient(StubClient({"text": "x"}), tmp_path)
        rec.complete_json(role="explain", system="s", user="u", schema=ExplainText)
        assert list(tmp_path.iterdir()) == [] and rec.written == []

    def test_rounds_are_numbered_per_run_and_variant(self, tmp_path):
        """A solver-first replan's first model call is already a repair (attempt 2) and keeps
        its number; a failed ask shifts only the later numbers of that run; a new run (the
        attempt back at 1) and another variant start afresh."""
        class Flaky:
            name = "flaky"

            def __init__(self, answers):
                self.answers = list(answers)

            def complete_json(self, *, role, system, user, schema, key=None, attempt=1,
                              variant=None, max_attempts=2):
                answer = self.answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return validate_into(schema, answer), Usage(model="flaky")

        rec = RecordingClient(Flaky([GRAPH, LLMError("timeout"), GRAPH, GRAPH, GRAPH]), tmp_path)
        calls = [("repair", 2, "replan"), ("repair", 3, "replan"), ("repair", 4, "replan"),
                 ("taskgraph", 1, None), ("taskgraph", 1, "replan")]
        for role, attempt, variant in calls:
            try:
                rec.complete_json(role=role, system="s", user="u", schema=TaskGraph, key="demo",
                                  attempt=attempt, variant=variant)
            except LLMError:
                assert (role, attempt) == ("repair", 3)
        names = sorted(p.name for p in (tmp_path / "demo").iterdir())
        assert names == ["taskgraph_1.json", "taskgraph_replan.json", "taskgraph_replan_2.json",
                         "taskgraph_replan_3.json"]
        assert len(rec.written) == 4

    def test_a_run_with_re_asks_records_a_replayable_fixture_set(self, tmp_path):
        """The first task-graph answer fails validation (what the live client raises after its
        own asks) and the planner's re-ask succeeds: it is recorded as round 1, so the fixture
        client replays the run as it went; a repair that validated after a re-ask lands in
        round 2 and replays without a wasted round."""
        class Flaky:
            name = "flaky"

            def __init__(self, answers):
                self.answers = answers

            def complete_json(self, *, role, system, user, schema, key=None, attempt=1,
                              variant=None, max_attempts=2):
                answer = self.answers[(role, attempt)]
                if isinstance(answer, Exception):
                    raise answer
                return validate_into(schema, answer), Usage(model="flaky")

        mismatch = SchemaMismatch("answer failed validation after 2 attempt(s)", "{",
                                  ["tasks: Field required"])
        pad = FIXTURES_DIR / "landing_pad"
        req = Requirements.model_validate_json((pad / "structure.json").read_text())
        good = json.loads((pad / "taskgraph_1.json").read_text())
        rec = RecordingClient(Flaky({("taskgraph", 1): mismatch, ("taskgraph", 2): good,
                                     ("explain", 1): {"text": "fine"}}), tmp_path)
        live = Planner(rec, max_repairs=3).plan_from_requirements(req, key="lp")
        assert live.success and [(s.stage, s.attempt, s.ok) for s in live.steps[:2]] == \
            [("taskgraph", 1, False), ("taskgraph", 2, True)]
        assert sorted(p.name for p in (tmp_path / "lp").iterdir()) == ["explain.json",
                                                                        "taskgraph_1.json"]
        replay = Planner(FixtureClient(tmp_path), max_repairs=3).plan_from_requirements(req, key="lp")
        assert replay.success and replay.task_graph == live.task_graph
        assert [(s.stage, s.attempt, s.ok) for s in replay.steps] == [
            ("taskgraph", 1, True), ("validate", 1, True), ("schedule", 1, True),
            ("explain", 1, True)]

        tight = FIXTURES_DIR / "tight_battery"
        req = Requirements.model_validate_json((tight / "structure.json").read_text())
        rec = RecordingClient(Flaky({
            ("taskgraph", 1): json.loads((tight / "taskgraph_1.json").read_text()),
            ("repair", 2): mismatch,
            ("repair", 3): json.loads((tight / "taskgraph_2.json").read_text()),
        }), tmp_path)
        live = Planner(rec, max_repairs=3, explain=False).plan_from_requirements(req, key="tb")
        assert live.success and live.repairs == 1
        assert sorted(p.name for p in (tmp_path / "tb").iterdir()) == ["taskgraph_1.json",
                                                                        "taskgraph_2.json"]
        replay = Planner(FixtureClient(tmp_path), max_repairs=3,
                         explain=False).plan_from_requirements(req, key="tb")
        assert replay.success and replay.repairs == 1 and replay.task_graph == live.task_graph
        asked = [(s.stage, s.attempt) for s in replay.steps if s.stage in ("taskgraph", "repair")]
        assert asked == [("taskgraph", 1), ("repair", 2)]

    def test_replan_repairs_and_later_replans_get_their_own_files(self, tmp_path):
        rec = RecordingClient(StubClient(GRAPH), tmp_path)
        for role, attempt, variant in (("taskgraph", 1, "replan"), ("repair", 2, "replan"),
                                       ("repair", 3, "replan"), ("taskgraph", 1, "replan2")):
            rec.complete_json(role=role, system="s", user="u", schema=TaskGraph, key="demo",
                              attempt=attempt, variant=variant)
        names = sorted(p.name for p in (tmp_path / "demo").iterdir())
        assert names == ["taskgraph_replan.json", "taskgraph_replan2.json",
                         "taskgraph_replan_2.json", "taskgraph_replan_3.json"]
        assert len(rec.written) == 4
        # the fixture client reads each back and never falls back within a variant
        fc = FixtureClient(tmp_path)
        assert fc._resolve("demo", "repair", 2, "replan").name == "taskgraph_replan_2.json"
        assert fc._resolve("demo", "taskgraph", 1, "replan2").name == "taskgraph_replan2.json"
        with pytest.raises(FixtureMissing):
            fc._resolve("demo", "repair", 4, "replan")
        with pytest.raises(FixtureMissing):
            fc._resolve("demo", "repair", 2, "replan2")


def test_fixture_filename_keeps_the_attempt_for_variants():
    assert fixture_filename("taskgraph", 1, "replan") == "taskgraph_replan.json"
    assert fixture_filename("repair", 2, "replan") == "taskgraph_replan_2.json"
    assert fixture_filename("repair", 3, "replan2") == "taskgraph_replan2_3.json"
    assert fixture_filename("repair", 3) == "taskgraph_3.json"
    assert fixture_filename("structure", 2, "replan") == "structure.json"


# --------------------------------------------------------------------------- FixtureClient
class TestFixtureClient:
    def test_falls_back_to_the_highest_attempt_in_numeric_order(self, tmp_path):
        demo = tmp_path / "demo"
        demo.mkdir()
        for name in ("taskgraph_1.json", "taskgraph_2.json", "taskgraph_10.json"):
            (demo / name).write_text(json.dumps(GRAPH))
        (demo / "taskgraph_1x.json").write_text("not an attempt file")  # ignored, not a crash
        fc = FixtureClient(tmp_path)
        assert fc._resolve("demo", "taskgraph", 1, None).name == "taskgraph_1.json"
        assert fc._resolve("demo", "repair", 2, None).name == "taskgraph_2.json"
        assert fc._resolve("demo", "repair", 3, None).name == "taskgraph_2.json"
        assert fc._resolve("demo", "repair", 10, None).name == "taskgraph_10.json"
        assert fc._resolve("demo", "repair", 11, None).name == "taskgraph_10.json"  # not _2
        with pytest.raises(FixtureMissing):
            fc._resolve("demo", "repair", 2, "replan")  # variants never fall back

    def test_corrupt_or_unreadable_fixture_is_an_llm_error(self, tmp_path):
        demo = tmp_path / "demo"
        demo.mkdir()
        (demo / "explain.json").write_text('{"text": "never closed')
        (demo / "structure.json").mkdir()  # exists, cannot be read as a file
        fc = FixtureClient(tmp_path)
        with pytest.raises(LLMError, match="unreadable fixture .*explain.json") as info:
            fc.complete_json(role="explain", system="s", user="u", schema=ExplainText, key="demo")
        assert not isinstance(info.value, FixtureMissing)
        with pytest.raises(LLMError, match="unreadable fixture .*structure.json"):
            fc.complete_json(role="structure", system="s", user="u", schema=Requirements,
                             key="demo")
        assert info.value.retryable is False
        # end to end: the planner reports it as a failed structure step, not a traceback
        result = Planner(fc, explain=False).plan("Build a pad.", brief_id="demo")
        assert not result.success and result.failure is not None
        assert result.failure.kind == "llm_error" and "unreadable fixture" in result.failure.hint
        assert [s.stage for s in result.steps] == ["structure"]

    def test_corrupt_task_graph_fixture_is_read_once_not_once_per_repair_round(self, tmp_path):
        demo = tmp_path / "demo"
        demo.mkdir()
        (demo / "structure.json").write_text(
            (FIXTURES_DIR / "landing_pad" / "structure.json").read_text())
        (demo / "taskgraph_1.json").write_text("{not json")
        result = Planner(FixtureClient(tmp_path), explain=False).plan("Build a pad.",
                                                                       brief_id="demo")
        assert not result.success and result.repairs == 0
        assert [s.stage for s in result.steps] == ["structure", "taskgraph"]
        assert result.steps[1].message is not None
        assert result.steps[1].message.startswith("offline: unreadable fixture")


# --------------------------------------------------------------------------- JSON extraction
SCHEMA_ECHO = json.dumps(ExplainText.model_json_schema())


class TestExtractJson:
    def test_orphan_close_tag_drops_the_reasoning(self):
        assert strip_thinking('I will write {"text": draft}... no.\n</think>\n{"text": "final"}') \
            == '{"text": "final"}'
        assert extract_json('Steps [1] think, [2] answer.\n</think>\n{"text": "final"}') \
            == {"text": "final"}
        assert strip_thinking("<think>a</think> b </think> c <think> cut off") == "c"
        assert strip_thinking("<think>only reasoning") == ""

    def test_prefers_the_answer_over_fragments(self):
        assert extract_json('Answer: {"text": "x"}. See [1] and [2].') == {"text": "x"}
        assert extract_json('Draft {"text": "a"} then {"text": "a", "more": 1}') \
            == {"text": "a", "more": 1}
        assert extract_json('{"a": 1}{"a": 2}') == {"a": 2}
        assert extract_json("[1, 2]") == [1, 2]
        assert extract_json("```json\n" + SCHEMA_ECHO + "\n```\nAnswer:\n```json\n"
                            '{"text": "final"}\n```') == {"text": "final"}
        assert extract_json('```json\n{"text": "use ``` for code"}\n```') \
            == {"text": "use ``` for code"}
        assert extract_json('{"text": "a } b { c"} trailing }') == {"text": "a } b { c"}

    def test_errors_name_the_problem(self):
        with pytest.raises(ValueError, match="no JSON object found"):
            extract_json("Sure, here is prose.")
        with pytest.raises(ValueError, match="unbalanced"):
            extract_json('{"text": "never clo')
        with pytest.raises(ValueError, match="no valid JSON"):
            extract_json("{bad}")


# --------------------------------------------------------------------------- factory
@pytest.fixture
def clean_env(monkeypatch):
    """No .env, no credentials, no kind override: resolution starts from a blank slate."""
    monkeypatch.setattr(factory, "load_env", lambda path=None: False)
    for var in ("NEBIUS_API_KEY", "MOONWRIGHT_LLM"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestFactory:
    def test_fixture_by_name(self, clean_env):
        assert isinstance(make_client("fixture"), FixtureClient)
        assert isinstance(make_client("FIXTURE "), FixtureClient)

    def test_default_without_key_is_fixture(self, clean_env):
        assert resolve_kind() == "fixture"
        assert isinstance(make_client(), FixtureClient)

    def test_default_with_key_is_nebius(self, clean_env):
        clean_env.setenv("NEBIUS_API_KEY", "k")
        assert resolve_kind() == "nebius"
        assert isinstance(make_client(), TokenFactoryClient)

    def test_env_kind_wins_over_key(self, clean_env):
        clean_env.setenv("NEBIUS_API_KEY", "k")
        clean_env.setenv("MOONWRIGHT_LLM", "fixture")
        assert isinstance(make_client(), FixtureClient)

    def test_record_wraps_live_client(self, clean_env):
        clean_env.setenv("NEBIUS_API_KEY", "k")
        client = make_client("record")
        assert isinstance(client, RecordingClient)
        assert isinstance(client.inner, TokenFactoryClient)
        assert client.fixtures_dir == FIXTURES_DIR

    def test_nebius_without_key_raises(self, clean_env):
        with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
            make_client("nebius")

    def test_unknown_kind(self, clean_env):
        with pytest.raises(ValueError, match="unknown LLM client kind"):
            make_client("bogus")


def test_load_env_reads_file_without_overriding_shell(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MOONWRIGHT_TEST_A=from_file\nMOONWRIGHT_TEST_B=from_file\n")
    monkeypatch.setenv("MOONWRIGHT_TEST_A", "from_shell")
    monkeypatch.setenv("MOONWRIGHT_TEST_B", "placeholder")  # registers the restore
    monkeypatch.delenv("MOONWRIGHT_TEST_B")
    assert factory.load_env(env_file) is True
    assert os.environ["MOONWRIGHT_TEST_A"] == "from_shell"
    assert os.environ["MOONWRIGHT_TEST_B"] == "from_file"
    assert factory.load_env(tmp_path / "missing.env") is False


# --------------------------------------------------------------------------- fake simulation
def frame(t: float) -> SimFrame:
    return SimFrame(t_h=t, robots=[RobotState(id="EX1", pos=(0, 0), battery_wh=1)])


def fake_sim_result(requirements: Requirements, graph: TaskGraph, before: Schedule) -> SimResult:
    """Nine frames, one failure event and one successful replan (18h -> 22h)."""
    event = Event(t_h=3.0, kind="robot_failure", robot="EX1", task="T1", detail="motor stall")
    after_schedule = before.model_copy(update={"makespan_h": 22})
    after = PlanResult(requirements=requirements, task_graph=graph, schedule=after_schedule,
                       success=True, repairs=1)
    record = ReplanRecord(event=event, t_h=3.0, remaining_tasks=["T1", "T2"], before=before,
                          after=after, context="EX1 failed at 3h")
    return SimResult(frames=[frame(i * 0.25) for i in range(9)], events=[event],
                     replans=[record], completed=True, finished_at_h=25.0,
                     schedule=after_schedule, task_graph=graph)


def test_format_replan_says_how_the_work_was_rescheduled():
    """The CLI line carries the closing replan step's verdict: solver-only, or escalated."""
    base = fake_sim_result(REQ, GRAPH2, SCHEDULE).replans[0]
    assert format_replan(1, base) == (
        "#1 at t=3.00h after robot_failure EX1: 2 tasks remaining; makespan 18h -> 22h "
        "(optimal, 1 repair(s)); solver-only, Nemotron not asked")
    closing = LoopStep(stage="replan", ok=True, message=(
        "CP-SAT rescheduled 2 tasks in 0.01 s, Nemotron not needed; makespan 18 h -> 22 h "
        "(absolute)"))
    solved = base.model_copy(update={"after": base.after.model_copy(update={"steps": [closing]})})
    assert format_replan(1, solved).endswith(
        "(optimal, 1 repair(s)); CP-SAT rescheduled 2 tasks in 0.01 s, Nemotron not needed")
    closing = LoopStep(stage="replan", ok=True, message=(
        "infeasible (energy:EX1) -> escalated to Nemotron; makespan 18 h -> 22 h (absolute), "
        "verified after 1 repair(s)"))
    escalated = base.model_copy(update={"escalated": True, "after": base.after.model_copy(
        update={"steps": [closing]})})
    assert format_replan(2, escalated).endswith(
        "; infeasible (energy:EX1) -> escalated to Nemotron")
    refused = base.model_copy(update={"escalated": True, "after": base.after.model_copy(
        update={"success": False, "schedule": None,
                "failure": Failure(kind="infeasible", conflicts=["energy:EX1"])})})
    assert format_replan(3, refused).endswith(
        "no feasible replan (infeasible: energy:EX1); escalated to Nemotron")


def make_fake_run(calls: dict[str, Any]):
    """Stand-in for run_with_replanning that records its arguments."""

    def run(planner, plan, injections, *, key, max_replans, dt_h=0.25):
        calls.update(key=key, max_replans=max_replans, injections=list(injections),
                     max_repairs=planner.max_repairs, dt_h=dt_h)
        return fake_sim_result(plan.requirements, plan.task_graph, plan.schedule)

    return run


# --------------------------------------------------------------------------- API
REQ = Requirements(robots=[Robot(id="EX1", type="excavator", battery_wh=5000),
                           Robot(id="HL1", type="hauler", battery_wh=3000)], horizon_h=100)
SCHEDULE = Schedule(entries=[ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=6),
                             ScheduleEntry(task="T2", robot="EX1", start_h=6, end_h=18)],
                    makespan_h=18, status="optimal", solve_time_s=0.05)
GRAPH2 = TaskGraph(tasks=[Task(id="T1", name="Dig", robot_type="excavator", duration_h=6,
                               energy_wh=500, zone="Z1"),
                          Task(id="T2", name="Grade", robot_type="excavator", duration_h=12,
                               energy_wh=900, zone="Z1", after=["T1"])])
VERIFIED = PlanResult(requirements=REQ, task_graph=GRAPH2, schedule=SCHEDULE, success=True)
# the same plan with Z1 declared on the depot, so its schedule needs no travel at all
AT_DEPOT = VERIFIED.model_copy(update={"requirements": REQ.model_copy(update={
    "site": Site(zones=[Zone(id="Z1", center=(20, 1))])})})
LANDING_PAD_BRIEF = EXAMPLES_DIR / "landing_pad.md"


def landing_pad_text() -> str:
    """The landing_pad example brief, or a skip when it or its fixtures are not available."""
    if not LANDING_PAD_BRIEF.exists() or not (FIXTURES_DIR / "landing_pad" / "structure.json").exists():
        pytest.skip("example brief / fixtures for landing_pad not available yet")
    return LANDING_PAD_BRIEF.read_text()


def post_raw_json(api: TestClient, path: str, text: str) -> httpx.Response:
    """POST a hand-written JSON body (httpx refuses to encode NaN / inf itself)."""
    return api.post(path, content=text, headers={"content-type": "application/json"})


@pytest.fixture(scope="module")
def api():
    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------- SSE helpers
def read_sse(response: httpx.Response) -> list[tuple[str, Any]]:
    """``[(event, data)]`` from a text/event-stream response read line by line; keep-alive
    comment lines come back as ``("comment", text)``."""
    events: list[tuple[str, Any]] = []
    event, data = None, []
    for line in response.iter_lines():
        if line.startswith(":"):
            events.append(("comment", line[1:].strip()))
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].strip())
        elif line == "" and event is not None:
            events.append((event, json.loads("\n".join(data))))
            event, data = None, []
    return events


def stream_plan(api: TestClient, body: dict[str, Any]) -> list[tuple[str, Any]]:
    with api.stream("POST", "/plan/stream", json=body) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-accel-buffering"] == "no"
        return read_sse(r)


def planner_streams_steps() -> bool:
    return "on_step" in inspect.signature(Planner.__init__).parameters


class ReplayingPlanner(Planner):
    """Stand-in until loop.py grows ``Planner(on_step=...)``: the real loop, its steps replayed
    through the callback once the outermost call returns (same events, just late)."""

    def __init__(self, client, *, on_step=None, **kwargs):
        super().__init__(client, **kwargs)
        self._on_step = on_step
        self._depth = 0

    def plan(self, brief, *, brief_id=None):
        return self._replayed(super().plan, brief, brief_id=brief_id)

    def plan_from_requirements(self, req, *, key, **kwargs):
        return self._replayed(super().plan_from_requirements, req, key=key, **kwargs)

    def _replayed(self, run, *args, **kwargs):
        self._depth += 1
        try:
            result = run(*args, **kwargs)
        finally:
            self._depth -= 1
        if self._depth == 0 and self._on_step is not None:
            for step in result.steps:
                self._on_step(step)
        return result


@pytest.fixture
def live_steps(monkeypatch):
    """The stream tests against the real pipeline: through ``Planner(on_step=...)`` once it
    exists, through :class:`ReplayingPlanner` until then."""
    if not planner_streams_steps():
        monkeypatch.setattr(api_module, "Planner", ReplayingPlanner)


STEP_A = LoopStep(stage="structure", message="scripted structure")
STEP_B = LoopStep(stage="taskgraph", message="scripted graph")
SCRIPTED_RESULT = VERIFIED.model_copy(update={"steps": [STEP_A, STEP_B]})


def scripted_planner(steps: list[LoopStep], outcome: PlanResult | Exception,
                     hook: Callable[[int], None] | None = None) -> type:
    """A Planner stand-in whose ``plan`` reports ``steps`` through ``on_step`` (``hook(i)``
    runs after step i so a test can sleep or block there) and then returns or raises
    ``outcome``."""

    class Scripted:
        def __init__(self, client, *, on_step=None, **kwargs):
            self.on_step = on_step

        def plan(self, brief, *, brief_id=None):
            for i, step in enumerate(steps):
                if self.on_step is not None:
                    self.on_step(step)
                if hook is not None:
                    hook(i)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    return Scripted


class TestApi:
    def test_health(self, api):
        r = api.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["llm"] in ("fixture", "nebius", "record")
        assert body["examples"] == len(list_briefs())

    def test_cors_for_localhost(self, api):
        r = api.get("/health", headers={"Origin": "http://localhost:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_cors_configuration_is_normalised(self, monkeypatch):
        monkeypatch.setenv("MOONWRIGHT_CORS_ORIGINS",
                           " https://Moonwright.vercel.app/ ,https://ui.example.org, / ")
        assert api_module._cors_origins() == ["https://moonwright.vercel.app",
                                              "https://ui.example.org"]
        monkeypatch.delenv("MOONWRIGHT_CORS_ORIGIN_REGEX", raising=False)
        assert api_module._cors_origin_regex() == api_module.LOCALHOST_ORIGIN_RE
        monkeypatch.setenv("MOONWRIGHT_CORS_ORIGIN_REGEX", r"https://.*\.vercel\.app")
        pattern = api_module._cors_origin_regex()
        import re
        assert re.fullmatch(pattern, "https://preview-1.vercel.app")
        assert re.fullmatch(pattern, "http://localhost:3000")
        assert not re.fullmatch(pattern, "https://evil.example")
        monkeypatch.delenv("MOONWRIGHT_CORS_ORIGINS")
        assert api_module._cors_origins() == []

    def test_unexpected_errors_are_json_500s_with_cors_headers(self, api, monkeypatch):
        """A planner crash must reach a cross-origin UI as the error it is, not as a CORS
        failure (Starlette's own 500 handler sits outside the CORS middleware)."""
        class Exploding:
            name = "exploding"

            def complete_json(self, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(api_module, "make_client", lambda kind=None: Exploding())
        origin = {"Origin": "http://localhost:3000"}
        for path, body in (("/structure", {"brief": "Build a pad."}),
                           ("/plan", {"brief": "Build a pad.", "explain": False})):
            r = api.post(path, json=body, headers=origin)
            assert r.status_code == 500, (path, r.text)
            assert r.json()["detail"] == "RuntimeError: boom"
            assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

        def crash(*args, **kwargs):
            raise RuntimeError("scheduler output failed independent re-verification: x")

        monkeypatch.setattr(api_module, "run_with_replanning", crash)
        r = api.post("/simulate", json={"plan": VERIFIED.model_dump(mode="json"),
                                        "llm": "fixture"}, headers=origin)
        assert r.status_code == 500 and "re-verification" in r.json()["detail"]
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

        monkeypatch.setattr(api_module, "run_monte_carlo", crash)
        r = api.post("/montecarlo", json={"plan": VERIFIED.model_dump(mode="json"), "runs": 2},
                     headers=origin)
        assert r.status_code == 500 and r.json()["detail"].startswith("RuntimeError: ")
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

    def test_exceptions_leaked_by_any_endpoint_are_json_500s_with_cors_headers(
            self, api, monkeypatch):
        """The inner middleware catches what no endpoint wrapper did (here /examples)."""
        def crash():
            raise OSError("examples directory unreadable")

        monkeypatch.setattr(api_module, "list_briefs", crash)
        r = api.get("/examples", headers={"Origin": "http://localhost:5173"})
        assert r.status_code == 500
        assert r.json() == {"detail": "OSError: examples directory unreadable"}
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert r.headers["content-type"].startswith("application/json")

    def test_validation_errors_keep_the_field_path(self, api):
        requirements = REQ.model_dump(mode="json")
        requirements["robots"][0]["battery_wh"] = -1
        r = api.post("/plan", json={"requirements": requirements, "llm": "fixture"})
        assert r.status_code == 422
        errors = r.json()["detail"]
        assert [e["loc"] for e in errors] == [["body", "requirements", "robots", 0, "battery_wh"]]
        assert errors[0]["input"] == -1 and "greater than 0" in errors[0]["msg"]
        requirements["robots"][0]["battery_wh"] = 5000
        requirements["horizon_h"] = 0
        r = api.post("/plan/stream", json={"requirements": requirements})
        assert r.status_code == 422
        assert r.json()["detail"][0]["loc"] == ["body", "requirements", "horizon_h"]
        # a plan nested in a /simulate body keeps the full path too
        plan = VERIFIED.model_dump(mode="json")
        plan["task_graph"]["tasks"][1]["duration_h"] = 0
        r = api.post("/simulate", json={"plan": plan})
        assert r.status_code == 422
        assert r.json()["detail"][0]["loc"] == ["body", "plan", "task_graph", "tasks", 1,
                                                "duration_h"]

    def test_examples(self, api):
        r = api.get("/examples")
        assert r.status_code == 200
        items = r.json()
        assert items == [b.model_dump() for b in list_briefs()]
        assert all(set(i) == {"id", "title", "text", "text_markdown"} for i in items)
        if (EXAMPLES_DIR / "landing_pad.md").exists():
            pad = next(i for i in items if i["id"] == "landing_pad")
            assert pad["text_markdown"] == (EXAMPLES_DIR / "landing_pad.md").read_text()
            assert "**" in pad["text_markdown"] and "**" not in pad["text"]
            assert pad["text"].startswith("LS-1 Landing Pad") and "Site. LS-1" in pad["text"]
            assert pad["text"].count("\n") == pad["text_markdown"].count("\n")
            assert api.get("/examples/landing_pad").json() == pad

    def test_strip_markdown(self):
        assert strip_markdown("# Title\n\n**Site.** Loose _regolith_ to *40 cm*; `EX1`.") == \
            "Title\n\nSite. Loose regolith to 40 cm; EX1."
        assert strip_markdown("__bold__ and ***both***") == "bold and both"
        # identifiers, numbers, list bullets and arithmetic are not emphasis
        assert strip_markdown("brief_id landing_pad 4_000 a*b*c 2 * 3 * 4\n- item\n* item") == \
            "brief_id landing_pad 4_000 a*b*c 2 * 3 * 4\n- item\n* item"
        assert strip_markdown("no markdown\n\n1. kept\n") == "no markdown\n\n1. kept\n"
        assert strip_markdown("#notatag and ## heading\n### Deep") == "#notatag and ## heading\nDeep"
        assert strip_markdown("") == ""

    def test_plan_without_fixtures_reports_failure_not_500(self, api):
        r = api.post("/plan", json={"brief": "Build a 20 m landing pad.",
                                    "brief_id": "zz_no_such_fixture", "llm": "fixture"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body["failure"]["kind"] == "llm_error"
        assert body["steps"][0]["stage"] == "structure" and body["steps"][0]["ok"] is False

    def test_plan_rejects_unknown_llm(self, api):
        r = api.post("/plan", json={"brief": "x", "llm": "bogus"})
        assert r.status_code == 400
        assert "unknown LLM client kind" in r.json()["detail"]

    def test_plan_validates_body(self, api):
        assert api.post("/plan", json={"brief": ""}).status_code == 422
        assert api.post("/plan", json={"brief": "x", "max_repairs": 99}).status_code == 422

    def test_plan_requires_exactly_one_of_brief_or_requirements(self, api):
        requirements = REQ.model_dump(mode="json")
        for path in ("/plan", "/plan/stream"):
            r = api.post(path, json={"brief": "x", "requirements": requirements})
            assert r.status_code == 400 and "exactly one" in r.json()["detail"]
            r = api.post(path, json={"llm": "fixture"})
            assert r.status_code == 400 and "exactly one" in r.json()["detail"]
            assert api.post(path, json={"requirements": {"robots": []}}).status_code == 422

    def test_plan_from_requirements_skips_structure_and_keeps_the_trace_uniform(self, api):
        brief = landing_pad_text()
        structured = api.post("/structure", json={"brief": brief, "brief_id": "landing_pad",
                                                  "llm": "fixture"}).json()
        r = api.post("/plan", json={"requirements": structured["requirements"],
                                    "brief_id": "landing_pad", "llm": "fixture",
                                    "explain": False})
        assert r.status_code == 200, r.text
        body = r.json()
        from_brief = api.post("/plan", json={"brief": brief, "brief_id": "landing_pad",
                                             "llm": "fixture", "explain": False}).json()
        assert body["success"] is True and body["schedule"]["makespan_h"] > 0
        assert body["schedule"]["makespan_h"] == from_brief["schedule"]["makespan_h"]
        assert [s["stage"] for s in body["steps"]] == ["structure", "taskgraph", "validate",
                                                        "schedule"]
        first = body["steps"][0]
        assert first["ok"] is True and first["failure"] is None
        assert first["message"] == ("requirements provided by the user: 2 structures, "
                                    "7 robots, horizon 336h")
        assert body["requirements"]["source_brief"] == brief

    def test_plan_from_requirements_without_fixtures_reports_failure_not_500(self, api):
        r = api.post("/plan", json={"requirements": REQ.model_dump(mode="json"),
                                    "llm": "fixture", "explain": False})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False and body["failure"]["kind"] == "llm_error"
        assert [s["stage"] for s in body["steps"]] == ["structure", "taskgraph"]
        assert body["steps"][0]["ok"] is True and body["steps"][1]["ok"] is False

    def test_structure_offline(self, api):
        brief = landing_pad_text()
        r = api.post("/structure", json={"brief": brief, "brief_id": "Landing Pad",
                                         "llm": "fixture"})
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"requirements", "step", "key"}
        assert body["key"] == "landing_pad"  # sanitised like brief_key
        assert body["step"]["stage"] == "structure" and body["step"]["ok"] is True
        req = Requirements.model_validate(body["requirements"])
        assert len(req.robots) == 7 and req.horizon_h == 336 and req.source_brief == brief

    def test_structure_without_fixtures_reports_failure_not_500(self, api):
        r = api.post("/structure", json={"brief": "Build a 20 m landing pad.",
                                         "brief_id": "zz_no_such_fixture", "llm": "fixture"})
        assert r.status_code == 200
        body = r.json()
        assert body["requirements"] is None and body["key"] == "zz_no_such_fixture"
        assert body["step"]["ok"] is False and body["step"]["failure"]["kind"] == "llm_error"
        hint = body["step"]["failure"]["hint"]   # what the UI shows: no path, no CLI flag
        assert hint.startswith("This backend is running offline") and "example briefs" in hint
        assert str(FIXTURES_DIR) not in hint and "--llm" not in hint
        assert body["step"]["failure"]["details"] == [
            (f"no fixture directory for brief 'zz_no_such_fixture' "
             f"(looked in {FIXTURES_DIR / 'zz_no_such_fixture'})")]
        hashed = api.post("/structure", json={"brief": "Build a berm.", "llm": "fixture"}).json()
        assert hashed["key"].startswith("brief_") and hashed["requirements"] is None

    def test_structure_rejects_bad_input(self, api):
        assert api.post("/structure", json={"brief": ""}).status_code == 422
        assert api.post("/structure", json={}).status_code == 422
        r = api.post("/structure", json={"brief": "x", "llm": "bogus"})
        assert r.status_code == 400 and "unknown LLM client kind" in r.json()["detail"]

    def test_example_by_id(self, api):
        briefs = {b.id: b for b in list_briefs()}
        if "landing_pad" not in briefs:
            pytest.skip("landing_pad example not available")
        assert api.get("/examples/landing_pad").json() == briefs["landing_pad"].model_dump()
        assert api.get("/examples/LANDING_PAD").status_code == 200  # ids are sanitised
        for unknown in ("nope", "%23%23%23", "landing_pad.md"):
            r = api.get(f"/examples/{unknown}")
            assert r.status_code == 404 and "no example brief" in r.json()["detail"]

    def test_simulate_requires_verified_schedule(self, api):
        plan = PlanResult(requirements=REQ).model_dump(mode="json")
        r = api.post("/simulate", json={"plan": plan, "failures": [{"robot": "EX1", "t_h": 3}]})
        assert r.status_code == 400
        assert "no verified schedule" in r.json()["detail"]
        # a schedule alone is not enough: the plan must have been verified (success)
        unverified = VERIFIED.model_copy(update={"success": False}).model_dump(mode="json")
        r = api.post("/simulate", json={"plan": unverified, "llm": "fixture"})
        assert r.status_code == 400 and "no verified schedule" in r.json()["detail"]

    def test_simulate_reverifies_the_incoming_schedule(self, api):
        shortened = SCHEDULE.model_copy(update={"entries": [
            SCHEDULE.entries[0], SCHEDULE.entries[1].model_copy(update={"end_h": 17})]})
        plan = VERIFIED.model_copy(update={"schedule": shortened}).model_dump(mode="json")
        r = api.post("/simulate", json={"plan": plan, "llm": "fixture"})
        assert r.status_code == 400
        assert "fails re-verification" in r.json()["detail"]
        assert "duration:T2" in r.json()["detail"]

    def test_simulate_validates_body(self, api):
        plan = VERIFIED.model_dump(mode="json")
        for bad in ({"dt_h": 0.01}, {"dt_h": 25}, {"frame_stride": 0}, {"max_replans": 6},
                    {"failures": [{"robot": "EX1", "t_h": -1}]},
                    {"failures": [{"robot": "", "t_h": 1}]}):
            assert api.post("/simulate", json={"plan": plan, **bad}).status_code == 422, bad
        # NaN / inf are rejected, and the 422 body (which echoes the input) is still JSON;
        # json.dumps spells them NaN / Infinity, which is what a sloppy client would send
        for bad_time in (float("nan"), float("inf")):
            body = json.dumps({"plan": plan, "failures": [{"robot": "EX1", "t_h": bad_time}]})
            r = post_raw_json(api, "/simulate", body)
            assert r.status_code == 422, r.text
            error = r.json()["detail"][0]
            assert error["loc"] == ["body", "failures", 0, "t_h"] and "finite" in error["msg"]
            assert isinstance(error["input"], str)
        broken = VERIFIED.model_dump(mode="json")
        broken["requirements"]["robots"][0]["battery_wh"] = float("nan")
        r = post_raw_json(api, "/simulate", json.dumps({"plan": broken}))
        assert r.status_code == 422 and "finite" in r.json()["detail"][0]["msg"]

    def test_simulate_rejects_unknown_robot(self, api):
        plan = PlanResult(requirements=REQ, task_graph=GRAPH2, schedule=SCHEDULE,
                          success=True).model_dump(mode="json")
        r = api.post("/simulate", json={"plan": plan, "failures": [{"robot": "ZZ9", "t_h": 3}],
                                        "llm": "fixture"})
        assert r.status_code == 400
        assert "ZZ9" in r.json()["detail"]

    def test_simulate_runs_and_thins_frames(self, api, monkeypatch):
        calls: dict[str, Any] = {}
        monkeypatch.setattr("moonwright.api.run_with_replanning", make_fake_run(calls))
        plan = PlanResult(requirements=REQ, task_graph=GRAPH2, schedule=SCHEDULE,
                          success=True).model_dump(mode="json")
        r = api.post("/simulate", json={"plan": plan, "failures": [{"robot": "EX1", "t_h": 3}],
                                        "llm": "fixture", "brief_id": "demo", "max_replans": 1,
                                        "frame_stride": 2, "dt_h": 0.5})
        assert r.status_code == 200
        body = r.json()
        assert calls["key"] == "demo" and calls["max_replans"] == 1 and calls["dt_h"] == 0.5
        assert calls["injections"] == [FailureInjection("EX1", 3.0)]
        assert body["completed"] is True and body["finished_at_h"] == 25.0
        assert [f["t_h"] for f in body["frames"]] == [0.0, 0.5, 1.0, 1.5, 2.0]
        assert body["replans"][0]["after"]["schedule"]["makespan_h"] == 22

    def test_simulate_defaults_match_the_cli(self, api, monkeypatch):
        calls: dict[str, Any] = {}
        monkeypatch.setattr("moonwright.api.run_with_replanning", make_fake_run(calls))
        r = api.post("/simulate", json={"plan": VERIFIED.model_dump(mode="json"),
                                        "llm": "fixture", "brief_id": "###"})
        assert r.status_code == 200
        assert calls["dt_h"] == 0.25 and calls["max_replans"] == 2 and calls["max_repairs"] == 3
        assert calls["key"].startswith("brief_")  # an unusable brief id falls back to the hash
        assert [f["t_h"] for f in r.json()["frames"]] == [0.0, 1.0, 2.0]  # stride 4 + last

    def test_simulate_turns_simulation_value_errors_into_400(self, api, monkeypatch):
        def explode(*args, **kwargs):
            raise ValueError("every robot has failed; nothing left to replan with")

        monkeypatch.setattr("moonwright.api.run_with_replanning", explode)
        r = api.post("/simulate", json={"plan": VERIFIED.model_dump(mode="json"),
                                        "llm": "fixture"})
        assert r.status_code == 400 and "every robot has failed" in r.json()["detail"]

    def test_plan_landing_pad_offline(self, api):
        brief = landing_pad_text()
        r = api.post("/plan", json={"brief": brief, "brief_id": "landing_pad",
                                    "llm": "fixture", "explain": False})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True and body["schedule"]["makespan_h"] > 0
        assert [s["stage"] for s in body["steps"]] == ["structure", "taskgraph", "validate", "schedule"]
        assert body["explanation"] is None

    # ---- /montecarlo
    def test_montecarlo_runs_and_is_deterministic_for_a_seed(self, api):
        body = {"plan": VERIFIED.model_dump(), "runs": 5, "seed": 1}
        r = api.post("/montecarlo", json=body)
        assert r.status_code == 200, r.text
        assert list(r.json()) == list(MonteCarloResult.model_fields)   # types.ts mirror
        result = MonteCarloResult.model_validate(r.json())
        assert result.runs == 5 and [s.seed for s in result.samples] == [1, 2, 3, 4, 5]
        assert result.horizon_h == REQ.horizon_h and result.completed == 5
        assert result.completion_rate == 1.0 and result.within_horizon_rate == 1.0
        assert result.finish_h.min is not None and result.finish_h.max is not None
        assert SCHEDULE.makespan_h <= result.finish_h.min <= result.finish_h.max
        assert sum(b.count for b in result.histogram) == result.completed
        assert result.histogram[-1].end_h == REQ.horizon_h
        again = MonteCarloResult.model_validate(api.post("/montecarlo", json=body).json())
        assert again.model_dump(exclude={"elapsed_s"}) == result.model_dump(exclude={"elapsed_s"})

    def test_montecarlo_defaults_and_planner_choice(self, api, monkeypatch):
        real_run = api_module.run_monte_carlo
        calls: dict[str, Any] = {}

        def fake_run(plan, **kwargs):
            calls.clear()
            calls.update(kwargs)
            return real_run(plan, runs=1, seed=0)

        monkeypatch.setattr(api_module, "run_monte_carlo", fake_run)
        r = api.post("/montecarlo", json={"plan": VERIFIED.model_dump()})
        assert r.status_code == 200, r.text
        assert {k: v for k, v in calls.items() if k != "planner"} == {
            "runs": 50, "failure_rate_per_robot_h": 0.002, "duration_jitter": 0.15,
            "seed": None, "max_replans": 2}
        planner = calls["planner"]
        assert isinstance(planner, MonteCarloPlanner)
        assert isinstance(planner.client, SolverOnlyClient)   # no model, no fixtures
        assert planner.explain is False and planner.max_repairs == 0
        # an llm kind swaps that client in (offline, fixture replans still fall back to CP-SAT)
        r = api.post("/montecarlo", json={"plan": VERIFIED.model_dump(), "llm": "fixture",
                                          "runs": 3, "seed": 9, "max_replans": 1,
                                          "duration_jitter": 0, "failure_rate_per_robot_h": 0.5})
        assert r.status_code == 200, r.text
        assert isinstance(calls["planner"].client, FixtureClient)
        assert calls["planner"].explain is False and calls["planner"].max_repairs == 3
        assert (calls["runs"], calls["seed"], calls["max_replans"]) == (3, 9, 1)
        assert (calls["duration_jitter"], calls["failure_rate_per_robot_h"]) == (0, 0.5)
        r = api.post("/montecarlo", json={"plan": VERIFIED.model_dump(), "llm": "fixture",
                                          "runs": 1, "max_repairs": 1})
        assert r.status_code == 200, r.text
        assert calls["planner"].max_repairs == 1

    def test_montecarlo_passes_every_result_field_through(self, api):
        """The response is the pydantic model itself, so fields the Monte Carlo module adds
        (outcomes, single points of failure, a CI) reach the UI without an API change."""
        r = api.post("/montecarlo", json={"plan": VERIFIED.model_dump(), "runs": 3, "seed": 2})
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == set(MonteCarloResult.model_fields)
        for field in ("outcomes", "single_points_of_failure", "within_horizon_ci"):
            if field in MonteCarloResult.model_fields:
                assert field in body
        sim = api.post("/simulate", json={"plan": VERIFIED.model_dump(), "llm": "fixture",
                                          "failures": [{"robot": "EX1", "t_h": 3}],
                                          "brief_id": "zz_no_fixture"})
        assert sim.status_code == 200, sim.text
        assert set(sim.json()) == set(SimResult.model_fields)
        if sim.json()["replans"] and "escalated" in ReplanRecord.model_fields:
            assert "escalated" in sim.json()["replans"][0]

    def test_montecarlo_with_a_fixture_client_runs_solver_only_replans(self, api):
        r = api.post("/montecarlo", json={"plan": VERIFIED.model_dump(), "llm": "fixture",
                                          "runs": 4, "seed": 3, "failure_rate_per_robot_h": 0.2})
        assert r.status_code == 200, r.text
        result = MonteCarloResult.model_validate(r.json())
        assert result.failures_mean > 0 and result.replans_mean > 0   # failures were replanned
        assert all(f.robot in {"EX1", "HL1"} for s in result.samples for f in s.failures)

    def test_montecarlo_requires_a_verified_schedule(self, api):
        r = api.post("/montecarlo", json={"plan": PlanResult(requirements=REQ).model_dump()})
        assert r.status_code == 400 and "no verified schedule" in r.json()["detail"]
        truncated = SCHEDULE.model_copy(update={"entries": SCHEDULE.entries[:1]})
        r = api.post("/montecarlo", json={"plan": VERIFIED.model_copy(
            update={"schedule": truncated}).model_dump()})
        assert r.status_code == 400
        assert "re-verification" in r.json()["detail"] and "missing:T2" in r.json()["detail"]

    def test_montecarlo_validates_body(self, api):
        plan = VERIFIED.model_dump()
        for extra in ({"runs": 0}, {"runs": 201}, {"runs": 2.5}, {"failure_rate_per_robot_h": 1.5},
                      {"failure_rate_per_robot_h": -0.1}, {"duration_jitter": 2},
                      {"max_replans": 6}, {"max_replans": -1}, {"seed": "lucky"},
                      {"max_repairs": 11}, {"max_repairs": -1}):
            r = api.post("/montecarlo", json={"plan": plan, **extra})
            assert r.status_code == 422, extra
        # NaN / inf rates are 422s with a serialisable body that names the field
        for field in ("failure_rate_per_robot_h", "duration_jitter"):
            for bad in (float("nan"), float("inf"), -float("inf")):
                r = post_raw_json(api, "/montecarlo", json.dumps({"plan": plan, field: bad}))
                assert r.status_code == 422, (field, bad, r.text)
                error = r.json()["detail"][0]
                assert error["loc"] == ["body", field] and isinstance(error["input"], str)
        assert api.post("/montecarlo", json={}).status_code == 422
        r = api.post("/montecarlo", json={"plan": plan, "llm": "openai"})
        assert r.status_code == 400 and "unknown LLM client kind" in r.json()["detail"]


class TestPlanStream:
    def test_offline_landing_pad_streams_every_step_then_the_result(self, api, live_steps):
        brief = landing_pad_text()
        body = {"brief": brief, "brief_id": "landing_pad", "llm": "fixture", "explain": False}
        events = stream_plan(api, body)
        assert [kind for kind, _ in events] == ["step", "step", "step", "step", "result"]
        steps = [data for kind, data in events if kind == "step"]
        assert [s["stage"] for s in steps] == ["structure", "taskgraph", "validate", "schedule"]
        assert all(s["ok"] for s in steps)
        result = events[-1][1]
        assert result["success"] is True and result["schedule"]["makespan_h"] > 0
        assert [s["stage"] for s in result["steps"]] == [s["stage"] for s in steps]
        # the same plan as the plain endpoint (solver wall time and step durations aside)
        plain = api.post("/plan", json=body).json()
        assert result["schedule"]["entries"] == plain["schedule"]["entries"]
        assert result["schedule"]["makespan_h"] == plain["schedule"]["makespan_h"]
        assert result["task_graph"] == plain["task_graph"]
        assert [(s["stage"], s["ok"], s["message"]) for s in result["steps"]] == [
            (s["stage"], s["ok"], s["message"]) for s in plain["steps"]]

    def test_requirements_body_streams_the_synthetic_structure_step(self, api, live_steps):
        brief = landing_pad_text()
        structured = api.post("/structure", json={"brief": brief, "brief_id": "landing_pad",
                                                  "llm": "fixture"}).json()
        events = stream_plan(api, {"requirements": structured["requirements"],
                                   "brief_id": "landing_pad", "llm": "fixture",
                                   "explain": False})
        assert [kind for kind, _ in events] == ["step", "step", "step", "step", "result"]
        assert events[0][1]["stage"] == "structure"
        assert events[0][1]["message"].startswith("requirements provided by the user: ")
        assert events[-1][1]["success"] is True
        assert events[-1][1]["steps"][0] == events[0][1]

    def test_failing_brief_still_ends_with_a_result_event(self, api, live_steps):
        events = stream_plan(api, {"brief": "Build a 20 m landing pad.",
                                   "brief_id": "zz_no_such_fixture", "llm": "fixture"})
        assert [kind for kind, _ in events] == ["step", "result"]
        step, result = events[0][1], events[1][1]
        assert step["stage"] == "structure" and step["ok"] is False
        assert result["success"] is False and result["failure"]["kind"] == "llm_error"
        assert result["steps"] == [step]

    def test_planner_exception_becomes_one_error_event(self, api, monkeypatch):
        monkeypatch.setattr(api_module, "Planner",
                            scripted_planner([STEP_A], RuntimeError("solver output rejected")))
        events = stream_plan(api, {"brief": "x", "llm": "fixture"})
        assert events == [("step", json.loads(STEP_A.model_dump_json())),
                          ("error", {"detail": "RuntimeError: solver output rejected"})]

    def test_steps_the_callback_did_not_deliver_are_flushed_before_the_result(self, api,
                                                                              monkeypatch):
        class Silent(scripted_planner([], SCRIPTED_RESULT)):
            def __init__(self, client, **kwargs):  # a planner without on_step support
                super().__init__(client)

        monkeypatch.setattr(api_module, "Planner", Silent)
        events = stream_plan(api, {"brief": "x", "llm": "fixture"})
        assert [kind for kind, _ in events] == ["step", "step", "result"]
        assert [data["message"] for kind, data in events[:2]] == ["scripted structure",
                                                                  "scripted graph"]

    def test_pings_while_the_planner_is_silent(self, api, monkeypatch):
        monkeypatch.setattr(api_module, "PING_INTERVAL_S", 0.02)
        monkeypatch.setattr(api_module, "Planner", scripted_planner(
            [STEP_A, STEP_B], SCRIPTED_RESULT, hook=lambda i: time.sleep(0.15) if i == 0 else None))
        events = stream_plan(api, {"brief": "x", "llm": "fixture"})
        kinds = [kind for kind, _ in events]
        assert kinds[0] == "step" and kinds[-1] == "result" and kinds.count("step") == 2
        assert kinds.count("comment") >= 1 and all(
            data == "ping" for kind, data in events if kind == "comment")
        assert kinds.index("comment") < kinds.index("step", 1)  # pinged during the silence

    def test_steps_reach_the_client_while_the_planner_is_still_running(self, api, monkeypatch):
        first_seen = threading.Event()
        timeline: dict[str, float | bool] = {}

        def hook(i: int) -> None:
            if i == 0:
                timeline["released"] = first_seen.wait(timeout=10)
                timeline["finished"] = time.monotonic()

        monkeypatch.setattr(api_module, "Planner",
                            scripted_planner([STEP_A, STEP_B], SCRIPTED_RESULT, hook))
        lines = []
        with api.stream("POST", "/plan/stream", json={"brief": "x", "llm": "fixture"}) as r:
            for line in r.iter_lines():
                lines.append(line)
                if line == "event: step" and not first_seen.is_set():
                    timeline["seen"] = time.monotonic()
                    first_seen.set()
        assert timeline["released"] is True  # the planner was blocked until we read step A
        assert timeline["seen"] < timeline["finished"]
        assert lines.count("event: step") == 2 and lines[-3] == "event: result"

    def test_rejects_unknown_llm_before_streaming(self, api):
        r = api.post("/plan/stream", json={"brief": "x", "llm": "bogus"})
        assert r.status_code == 400 and "unknown LLM client kind" in r.json()["detail"]
        assert r.headers["content-type"].startswith("application/json")


def test_brief_title():
    assert brief_title("# Landing pad\n\nBuild...", "x") == "Landing pad"
    assert brief_title("\n\n  plain first line  \nmore", "x") == "plain first line"
    assert brief_title("", "fallback") == "fallback"


def test_list_briefs(tmp_path):
    (tmp_path / "b_second.md").write_text("# Second\n**Site.** body")
    (tmp_path / "a_first.txt").write_text("first body")
    (tmp_path / "ignored.json").write_text("{}")
    briefs = list_briefs(tmp_path)
    assert [b.id for b in briefs] == ["a_first", "b_second"]
    assert briefs[1].title == "Second" and briefs[1].text_markdown == "# Second\n**Site.** body"
    assert briefs[1].text == "Second\nSite. body"
    assert briefs[0].text == briefs[0].text_markdown == "first body"
    assert list_briefs(tmp_path / "missing") == []


def test_thin_frames():
    result = SimResult(frames=[frame(i * 0.25) for i in range(10)])
    thinned = thin_frames(result, 4)
    assert [f.t_h for f in thinned.frames] == [0.0, 1.0, 2.0, 2.25]
    assert thin_frames(result, 1) is result
    assert len(thin_frames(result, 3).frames) == 4  # 0,3,6,9 -> last already kept


# --------------------------------------------------------------------------- CLI pieces
class TestCliParsing:
    def test_plan_args(self):
        args = build_parser().parse_args(
            ["plan", "brief.md", "--llm", "fixture", "--max-repairs", "2", "--time-limit", "3",
             "--no-explain", "-q"])
        assert (args.command, args.brief, args.llm) == ("plan", "brief.md", "fixture")
        assert (args.max_repairs, args.time_limit, args.no_explain, args.quiet) == (2, 3.0, True, True)
        assert args.out is None and args.brief_id is None

    def test_simulate_args(self):
        args = build_parser().parse_args(
            ["simulate", "out/x.plan.json", "--fail", "EX2@30", "--fail", "HL1@12.5",
             "--max-replans", "1", "--all-frames"])
        assert [(f.robot, f.t_h) for f in args.fail] == [("EX2", 30.0), ("HL1", 12.5)]
        assert args.dt == 0.25 and args.max_replans == 1 and args.all_frames

    def test_montecarlo_args(self):
        args = build_parser().parse_args(
            ["montecarlo", "out/x.plan.json", "--runs", "20", "--failure-rate", "0.01",
             "--jitter", "0.2", "--seed", "1", "--max-replans", "1", "--out", "x.mc.json"])
        assert (args.command, args.plan, args.runs) == ("montecarlo", Path("out/x.plan.json"), 20)
        assert (args.failure_rate, args.jitter, args.seed, args.max_replans) == (0.01, 0.2, 1, 1)
        assert args.out == Path("x.mc.json") and args.llm is None and args.dt == 0.25
        defaults = build_parser().parse_args(["montecarlo", "p.json"])
        assert (defaults.runs, defaults.failure_rate, defaults.jitter) == (50, 0.002, 0.15)
        assert (defaults.seed, defaults.max_replans, defaults.out) == (None, 2, None)
        assert (defaults.max_repairs, defaults.time_limit) == (3, 10.0)

    def test_eval_and_serve_args(self):
        args = build_parser().parse_args(["eval", "--briefs", "examples/briefs", "--loop", "on"])
        assert args.loop == "on" and args.repeat == 1 and args.out == Path("eval.json")
        assert (args.workers, args.feedback, args.max_answer_retries) == (1, "core", None)
        assert (args.models, args.thinking, args.markdown) == (None, None, None)
        assert (args.save_plans, args.resume) == (None, False)
        args = build_parser().parse_args(
            ["eval", "--briefs", "b", "--save-plans", "out/live", "--resume"])
        assert (args.save_plans, args.resume) == (Path("out/live"), True)
        args = build_parser().parse_args(
            ["eval", "--briefs", "b", "--models", "ultra,super", "--thinking", "off",
             "--markdown", "out/eval.md"])
        assert (args.models, args.thinking, args.markdown) == ("ultra,super", "off",
                                                               Path("out/eval.md"))
        args = build_parser().parse_args(
            ["eval", "--briefs", "b", "--workers", "4", "--feedback", "verdict",
             "--max-answer-retries", "0", "--max-repairs", "10"])
        assert (args.workers, args.feedback, args.max_answer_retries) == (4, "verdict", 0)
        assert args.max_repairs == 10
        args = build_parser().parse_args(["serve", "--port", "9000"])
        assert (args.host, args.port) == ("127.0.0.1", 9000)

    def test_bad_values_exit_with_usage_error(self, capsys):
        parser = build_parser()
        for argv in (["plan", "b.md", "--llm", "openai"],
                     ["simulate", "p.json", "--fail", "EX2"],
                     ["simulate", "p.json", "--fail", "EX2@soon"],
                     ["simulate", "p.json", "--fail", "@3"],
                     ["simulate", "p.json", "--fail", "EX2@-1"],
                     ["simulate", "p.json", "--fail", "EX2@nan"],
                     ["simulate", "p.json", "--fail", "EX2@inf"],
                     ["simulate", "p.json", "--dt", "0"],
                     ["simulate", "p.json", "--dt", "-0.5"],
                     ["simulate", "p.json", "--dt", "nan"],
                     ["simulate", "p.json", "--dt", "soon"],
                     ["plan", "b.md", "--time-limit", "0"],
                     ["plan", "b.md", "--time-limit", "inf"],
                     ["plan", "b.md", "--time-limit", "abc"],
                     ["eval", "--briefs", "b", "--time-limit", "-3"],
                     ["montecarlo", "p.json", "--dt", "0"],
                     ["montecarlo", "p.json", "--llm", "openai"],
                     ["montecarlo", "p.json", "--runs", "many"],
                     ["montecarlo", "p.json", "--runs", "0"],
                     ["montecarlo", "p.json", "--failure-rate", "inf"],
                     ["montecarlo", "p.json", "--failure-rate", "nan"],
                     ["montecarlo", "p.json", "--failure-rate", "1.5"],
                     ["montecarlo", "p.json", "--jitter", "-0.1"],
                     ["montecarlo", "p.json", "--max-replans", "6"],
                     ["montecarlo", "p.json", "--max-replans", "-1"],
                     ["montecarlo", "p.json", "--max-repairs", "-1"],
                     ["montecarlo", "p.json", "--time-limit", "nan"],
                     ["simulate", "p.json", "--max-replans", "-1"],
                     ["simulate", "p.json", "--max-replans", "99"],
                     ["simulate", "p.json", "--max-repairs", "-2"],
                     ["plan", "b.md", "--max-repairs", "-1"],
                     ["plan", "b.md", "--max-repairs", "11"],
                     ["simulate", "p.json", "--max-repairs", "11"],
                     ["montecarlo", "p.json", "--max-repairs", "11"],
                     ["eval", "--briefs", "b", "--max-repairs", "11"],
                     ["eval", "--briefs", "b", "--repeat", "0"],
                     ["eval", "--briefs", "b", "--workers", "0"],
                     ["eval", "--briefs", "b", "--workers", "two"],
                     ["eval", "--briefs", "b", "--feedback", "hints"],
                     ["eval", "--briefs", "b", "--max-answer-retries", "-1"],
                     ["eval", "--briefs", "b", "--max-answer-retries", "6"],
                     ["eval", "--briefs", "b", "--thinking", "maybe"],
                     ["montecarlo"],
                     ["eval"]):
            with pytest.raises(SystemExit) as info:
                parser.parse_args(argv)
            assert info.value.code == 2, argv
        assert "Traceback" not in capsys.readouterr().err

    def test_bounded_numeric_types_accept_the_edges(self):
        args = build_parser().parse_args(
            ["montecarlo", "p.json", "--failure-rate", "1", "--jitter", "0", "--max-replans", "5",
             "--runs", "1", "--max-repairs", "0"])
        assert (args.failure_rate, args.jitter, args.max_replans) == (1.0, 0.0, 5)
        assert (args.runs, args.max_repairs) == (1, 0)
        args = build_parser().parse_args(["simulate", "p.json", "--max-replans", "0"])
        assert args.max_replans == 0

    def test_parse_failure(self):
        assert parse_failure("EX2@30") == FailureInjection("EX2", 30.0)
        assert parse_failure("HL1@0") == FailureInjection("HL1", 0.0)
        for spec in ("EX2@nan", "EX2@inf", "EX2@-inf", "EX2@-1", "EX2@1e999", "EX2@x", "EX2"):
            with pytest.raises(argparse.ArgumentTypeError):
                parse_failure(spec)

    def test_positive_float(self):
        assert positive_float("3") == 3.0 and positive_float("0.05") == 0.05
        for text in ("0", "-1", "nan", "inf", "-inf", "1e999", "soon"):
            with pytest.raises(argparse.ArgumentTypeError):
                positive_float(text)

    def test_clean_brief_id(self):
        assert clean_brief_id(None) is None and clean_brief_id("") is None
        assert clean_brief_id("###") is None and clean_brief_id("___") is None
        assert clean_brief_id("_-_") == "-"  # brief_key keeps hyphens
        assert clean_brief_id("Landing Pad!") == "landing_pad"
        assert clean_brief_id("landing_pad") == "landing_pad"

    def test_plan_stem(self):
        assert plan_stem(Path("out/landing_pad.plan.json")) == "landing_pad"
        assert plan_stem(Path("tight.json")) == "tight"
        assert plan_stem(Path("weird.txt")) == "weird"



class TestCliFormatting:
    def test_format_step_ok_and_fail(self):
        ok = LoopStep(stage="schedule", ok=True, duration_s=0.4, message="makespan 18h (optimal)",
                      usage=Usage(model="m", input_tokens=120, output_tokens=30))
        line = format_step(ok)
        assert line.startswith("schedule  #1 ok  ") and "makespan 18h (optimal)" in line
        assert "[m 120->30 tok]" in line
        bad = LoopStep(stage="validate", attempt=2, ok=False, duration_s=0.01,
                       failure=Failure(kind="validation", conflicts=["energy:EX1", "horizon"],
                                       hint="EX1 needs 9000 Wh but has 5000 Wh."))
        line = format_step(bad)
        assert "validate  #2 FAIL" in line and "validation: energy:EX1, horizon" in line
        assert "hint: EX1 needs 9000 Wh" in line

    def test_format_gantt_lists_idle_robots(self):
        text = format_gantt(SCHEDULE, REQ)
        assert text.splitlines()[0] == "EX1: [T1 0-6][T2 6-18]"
        assert text.splitlines()[1] == "HL1: (idle)"
        assert text.splitlines()[-1].startswith("makespan 18h (optimal")

    def test_summarise(self):
        rows = [
            EvalRow(brief="a", condition="loop_off", repeat=1, success=False, repairs=0,
                    latency_s=1.0, llm_latency_s=0.5, input_tokens=100, output_tokens=10,
                    failure_kind="validation", conflicts=["energy:EX1"]),
            EvalRow(brief="a", condition="loop_on", repeat=1, success=True, repairs=1,
                    makespan_h=40, latency_s=2.0, llm_latency_s=1.5, input_tokens=300,
                    output_tokens=40),
            EvalRow(brief="b", condition="loop_on", repeat=1, success=True, repairs=0,
                    makespan_h=20, latency_s=4.0, llm_latency_s=3.0, input_tokens=100,
                    output_tokens=20),
        ]
        off, on = summarise(rows)
        assert (off.condition, off.runs, off.feasible_rate, off.mean_makespan_h) == ("loop_off", 1, 0.0, None)
        assert (on.runs, on.feasible, on.feasible_rate) == (2, 2, 1.0)
        assert (on.mean_repairs, on.mean_makespan_h, on.mean_latency_s) == (0.5, 30.0, 3.0)
        assert (on.mean_input_tokens, on.mean_output_tokens) == (200.0, 30.0)
        # Wilson 95% interval of the feasible rate: 0/1 and 2/2 never collapse to a point
        assert (off.feasible_ci_low, off.feasible_ci_high) == (0.0, pytest.approx(0.7935, abs=1e-3))
        assert (on.feasible_ci_low, on.feasible_ci_high) == (pytest.approx(0.3424, abs=1e-3), 1.0)
        # the new per-row columns default to "unknown" and aggregate to zero / None
        assert (on.first_try_feasible, on.first_try_rate, on.answer_valid_rate) == (0, 0.0, 0.0)
        assert on.mean_scope_drift_pct is None
        # phase-3c columns: wall-latency percentiles, the model calls' share, cost over the
        # priced runs (none here: no row carries a cost)
        assert (on.latency_p50_s, on.latency_p95_s, on.mean_llm_latency_s) == (3.0, 3.9, 2.25)
        assert (on.mean_cost_usd, on.total_cost_usd, on.priced_runs) == (None, None, 0)
        # in scope = verified with no volume advisory: the synthetic rows carry none
        assert (on.in_scope, on.in_scope_rate) == (0, 0.0)
        table = format_summary([off, on])
        assert table.splitlines()[0].split() == [
            "condition", "runs", "feasible", "95%", "CI", "in-scope", "1st-try", "repairs",
            "makespan", "drift", "latency", "p95", "tok", "in/out", "cost/run"]
        assert "0%-79%" in table.splitlines()[1] and "34%-100%" in table.splitlines()[2]
        assert table.splitlines()[2].rstrip().endswith("-")   # no priced run: no dollar figure

    def test_wilson_interval(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)
        assert wilson_interval(0, 1) == (0.0, pytest.approx(0.7935, abs=1e-3))
        assert wilson_interval(2, 2) == (pytest.approx(0.3424, abs=1e-3), 1.0)
        assert wilson_interval(50, 100) == (pytest.approx(0.4038, abs=1e-3),
                                            pytest.approx(0.5962, abs=1e-3))
        assert wilson_interval(3, 3) == (pytest.approx(0.4385, abs=1e-3), 1.0)
        for k, n in ((0, 5), (1, 3), (7, 9), (20, 20)):
            low, high = wilson_interval(k, n)
            assert 0.0 <= low <= k / n <= high <= 1.0
        narrow = wilson_interval(60, 600)
        wide = wilson_interval(6, 60)
        assert narrow[1] - narrow[0] < wide[1] - wide[0]   # more runs, tighter interval


FIRST_GRAPH = TaskGraph(tasks=[
    Task(id="T1", name="Dig", robot_type="excavator", duration_h=10, energy_wh=4000, zone="Z1"),
    Task(id="T2", name="Trench", robot_type="excavator", duration_h=10, energy_wh=4000,
         zone="Z2"),
])
FINAL_GRAPH = FIRST_GRAPH.model_copy(update={"tasks": [
    FIRST_GRAPH.tasks[0],
    FIRST_GRAPH.tasks[1].model_copy(update={"duration_h": 6, "energy_wh": 2100}),
]})


def usage_step(stage: str, model: str, *, attempt: int = 1, ok: bool = True,
               failure: Failure | None = None) -> LoopStep:
    return LoopStep(stage=stage, attempt=attempt, ok=ok, failure=failure,
                    usage=Usage(model=model, input_tokens=100, output_tokens=10, latency_s=1.0))


class TestEvalRow:
    def test_repaired_run_columns(self, monkeypatch):
        verdict = Failure(kind="infeasible", conflicts=["energy:EX1"])
        steps = [
            usage_step("structure", "nvidia/lightning"),
            usage_step("taskgraph", "nvidia/ultra"),
            LoopStep(stage="validate", ok=True),
            LoopStep(stage="schedule", ok=False, failure=verdict),
            usage_step("repair", "nvidia/super", attempt=2),
            LoopStep(stage="validate", attempt=2, ok=True),
            LoopStep(stage="schedule", attempt=2, ok=True),
        ]
        result = PlanResult(requirements=REQ, task_graph=FINAL_GRAPH, schedule=SCHEDULE,
                            success=True, repairs=1, steps=steps)
        row = eval_row("demo", "loop_on", 2, result, 4.5, first_graph=FIRST_GRAPH,
                       thinking="off")
        assert (row.brief, row.condition, row.repeat, row.success, row.repairs) == (
            "demo", "loop_on", 2, True, 1)
        assert row.makespan_h == 18 and row.latency_s == 4.5 and row.llm_latency_s == 3.0
        assert (row.input_tokens, row.output_tokens) == (300, 30)
        assert row.models == "nvidia/ultra,nvidia/super"   # structure / explain are not listed
        assert row.answer_valid is True and row.first_try_feasible is False
        assert (row.first_duration_h, row.first_energy_wh) == (20, 8000.0)
        assert (row.final_duration_h, row.final_energy_wh) == (16, 6100.0)
        assert row.scope_drift_pct == pytest.approx(-23.75)   # the repair re-scoped the trench
        assert row.thinking == "off" and row.failure_kind is None and row.conflicts == []
        # the CSV record spells the list and keeps every column
        record = cli_module.csv_record(row)
        assert list(record) == list(cli_module.CSV_FIELDS) and record["conflicts"] == ""

    def test_schema_reask_is_not_a_repair(self):
        steps = [
            usage_step("structure", "nvidia/lightning"),
            usage_step("taskgraph", "nvidia/ultra", ok=False,
                       failure=Failure(kind="schema", conflicts=["schema:tasks"])),
            usage_step("taskgraph", "nvidia/ultra", attempt=2),
            LoopStep(stage="validate", attempt=2, ok=True),
            LoopStep(stage="schedule", attempt=2, ok=True),
        ]
        result = PlanResult(requirements=REQ, task_graph=FIRST_GRAPH, schedule=SCHEDULE,
                            success=True, repairs=0, steps=steps)
        row = eval_row("demo", "loop_off", 1, result, 1.0, first_graph=FIRST_GRAPH)
        assert row.answer_valid is False      # the first answer failed the schema
        assert row.first_try_feasible is True  # but no verdict round was needed
        assert row.models == "nvidia/ultra" and row.scope_drift_pct == 0.0
        assert row.thinking == "auto"

    def test_failed_runs(self):
        failed_structure = PlanResult(
            requirements=REQ, success=False, failure=Failure(kind="llm_error", hint="x"),
            steps=[LoopStep(stage="structure", ok=False,
                            failure=Failure(kind="llm_error", hint="x"))])
        row = eval_row("demo", "loop_off", 1, failed_structure, 0.5)
        assert row.answer_valid is False and row.first_try_feasible is False
        assert row.models == "" and row.failure_kind == "llm_error"
        assert (row.first_duration_h, row.final_energy_wh, row.scope_drift_pct) == (None, None,
                                                                                     None)
        verdict = Failure(kind="infeasible", conflicts=["energy:EX1", "energy:EX2"], hint="h")
        rejected = PlanResult(
            requirements=REQ, task_graph=FIRST_GRAPH, success=False, failure=verdict, repairs=0,
            steps=[usage_step("structure", "l"), usage_step("taskgraph", "u"),
                   LoopStep(stage="validate", ok=True),
                   LoopStep(stage="schedule", ok=False, failure=verdict)])
        row = eval_row("demo", "loop_off", 1, rejected, 0.5, first_graph=FIRST_GRAPH)
        assert row.success is False and row.first_try_feasible is False
        assert row.answer_valid is True and row.conflicts == ["energy:EX1", "energy:EX2"]
        assert row.failure_kind == "infeasible" and row.scope_drift_pct == 0.0
        assert (row.first_energy_wh, row.final_energy_wh) == (8000.0, 8000.0)

    def test_rate_model_recomputation_is_not_scope_drift(self):
        """A live-style first answer states quantities with optimistic hours and Wh; the rate
        model re-sizes it on its way into the planner. The captured first graph is grounded the
        same way, so a run without a repair round reports zero drift."""
        pad = FIXTURES_DIR / "landing_pad"
        req = Requirements.model_validate_json((pad / "structure.json").read_text())
        data = json.loads((pad / "taskgraph_1.json").read_text())
        scope = {"T2": (300, "m2"), "T3": (126, "m3"), "T4": (98, "m3"), "T5": (150, "m2"),
                 "T6": (120, "m3"), "T7": (188, "m3"), "T8": (70, "m3"), "T9": (314, "m2"),
                 "T10": (350, "m2"), "T11": (314, "m2"), "T12": (80, "m2")}
        for t in data["tasks"]:
            if t["id"] in scope:
                t["quantity"], t["unit"] = scope[t["id"]]
            if t["id"] == "T3":                       # 126 m3: the table says 18 h / 2016 Wh
                t.update(duration_h=10, energy_wh=1000)

        class Scripted:
            name = "scripted"

            def complete_json(self, *, role, system, user, schema, key=None, attempt=1,
                              variant=None, max_attempts=2):
                return validate_into(schema, data), Usage(model="nvidia/ultra")

        capture = GraphCapture(Scripted())
        result = Planner(capture, max_repairs=0, explain=False).plan_from_requirements(req, key="lp")
        assert result.success and result.repairs == 0 and result.task_graph is not None
        assert result.task_graph.task("T3").duration_h == 18
        assert capture.graphs[0].task("T3").estimated_duration_h == 10.0   # grounded capture
        row = eval_row("landing_pad", "loop_off", 1, result, 1.0, first_graph=capture.graphs[0])
        assert row.first_try_feasible and row.scope_drift_pct == 0.0
        assert row.first_energy_wh == row.final_energy_wh == pytest.approx(
            sum(t.energy_wh for t in result.task_graph.tasks))

    def test_thinking_setting_reads_the_environment(self, monkeypatch):
        monkeypatch.delenv("MOONWRIGHT_THINKING", raising=False)
        assert cli_module.thinking_setting() == "auto"
        monkeypatch.setenv("MOONWRIGHT_THINKING", " OFF ")
        assert cli_module.thinking_setting() == "off"
        monkeypatch.setenv("MOONWRIGHT_THINKING", "")
        assert cli_module.thinking_setting() == "auto"


# --------------------------------------------------------------------------- eval planner stand-ins
FAILED_RESULT = PlanResult(
    requirements=REQ, success=False, failure=Failure(kind="llm_error", hint="scripted"),
    steps=[LoopStep(stage="structure", ok=False, failure=Failure(kind="llm_error", hint="s"))],
)


class RecordingPlanner:
    """A Planner that accepts the phase-3 repair-loop options and records what it was built
    with; ``structure`` (what the eval calls once per brief and repeat) answers with the
    scripted structuring failure, or raises ``trip`` on the n-th call."""

    created: ClassVar[list[dict[str, Any]]] = []
    calls: ClassVar[int] = 0
    trip: ClassVar[tuple[int, Exception] | None] = None

    def __init__(self, client, *, max_repairs=3, solver_time_limit_s=10.0, explain=True,
                 feedback_mode="core", max_answer_retries=2, on_step=None):
        type(self).created.append({
            "max_repairs": max_repairs, "solver_time_limit_s": solver_time_limit_s,
            "explain": explain, "feedback_mode": feedback_mode,
            "max_answer_retries": max_answer_retries})
        self.client = client

    def structure(self, brief, *, brief_id=None):
        cls = type(self)
        cls.calls += 1
        if cls.trip is not None and cls.calls == cls.trip[0]:
            raise cls.trip[1]
        return None, FAILED_RESULT.steps[0]

    def plan_from_requirements(self, req, *, key, **kwargs):
        raise AssertionError("the eval never plans a brief it could not structure")

    def plan(self, brief, *, brief_id=None):
        req, _step = self.structure(brief, brief_id=brief_id)
        return FAILED_RESULT if req is None else self.plan_from_requirements(req, key=brief_id)


class LegacyPlanner(RecordingPlanner):
    """A Planner without the phase-3 options (what loop.py looked like before them)."""

    def __init__(self, client, *, max_repairs=3, solver_time_limit_s=10.0, explain=True,
                 on_step=None):
        super().__init__(client, max_repairs=max_repairs, solver_time_limit_s=solver_time_limit_s,
                         explain=explain)


@pytest.fixture
def planner_stub(monkeypatch):
    def install(cls: type) -> type:
        cls.created = []
        cls.calls = 0
        cls.trip = None
        monkeypatch.setattr(cli_module, "Planner", cls)
        return cls

    return install


def briefs_dir(tmp_path: Path, n: int = 2) -> Path:
    directory = tmp_path / "briefs"
    directory.mkdir()
    for i in range(n):
        (directory / f"zz_brief_{i}.md").write_text(f"# Brief {i}\n**Site.** Build pad {i}.")
    return directory


class TestEvalCommand:
    def test_feedback_and_answer_retries_reach_the_planner(self, tmp_path, capsys, planner_stub):
        stub = planner_stub(RecordingPlanner)
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path)), "--llm", "fixture",
                     "--loop", "both", "--feedback", "verdict", "--max-answer-retries", "1",
                     "--max-repairs", "2", "--time-limit", "3",
                     "--out", str(tmp_path / "e.json"), "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_OK
        assert len(stub.created) == 4
        assert all(c["feedback_mode"] == "verdict" and c["max_answer_retries"] == 1
                   for c in stub.created)
        assert all(c["solver_time_limit_s"] == 3.0 and c["explain"] is False
                   for c in stub.created)
        assert sorted(c["max_repairs"] for c in stub.created) == [0, 0, 2, 2]
        report = json.loads((tmp_path / "e.json").read_text())
        assert (report["feedback"], report["max_answer_retries"]) == ("verdict", 1)
        assert (report["workers"], report["max_repairs"], report["thinking"]) == (1, 2, "auto")
        assert all(r["thinking"] == "auto" for r in report["rows"])
        assert "feedback=verdict" in capsys.readouterr().err

    def test_default_feedback_is_passed_when_the_planner_knows_it(self, tmp_path, capsys,
                                                                  planner_stub):
        stub = planner_stub(RecordingPlanner)
        assert main(["eval", "--briefs", str(briefs_dir(tmp_path, 1)), "--llm", "fixture",
                     "--loop", "off", "--out", str(tmp_path / "e.json"),
                     "--csv", str(tmp_path / "e.csv")]) == EXIT_OK
        assert stub.created == [{"max_repairs": 0, "solver_time_limit_s": 10.0, "explain": False,
                                 "feedback_mode": "core", "max_answer_retries": 2}]

    def test_legacy_planner_refuses_the_ablation_arm_but_runs_the_default(
            self, tmp_path, capsys, planner_stub):
        stub = planner_stub(LegacyPlanner)
        briefs = briefs_dir(tmp_path, 1)
        code = main(["eval", "--briefs", str(briefs), "--llm", "fixture", "--feedback", "verdict",
                     "--out", str(tmp_path / "e.json"), "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_ERROR and stub.created == []
        assert "feedback verdict is not supported" in capsys.readouterr().err
        code = main(["eval", "--briefs", str(briefs), "--llm", "fixture",
                     "--max-answer-retries", "1",
                     "--out", str(tmp_path / "e.json"), "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_ERROR and "max-answer-retries is not supported" in capsys.readouterr().err
        code = main(["eval", "--briefs", str(briefs), "--llm", "fixture", "--loop", "on",
                     "--out", str(tmp_path / "e.json"), "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_OK and len(stub.created) == 1
        assert planner_options("core", None) == {}

    def test_rows_are_flushed_to_the_csv_as_they_complete(self, tmp_path, capsys, planner_stub):
        stub = planner_stub(RecordingPlanner)
        stub.trip = (2, ValueError("worker crashed on the second brief"))
        out_csv = tmp_path / "e.csv"
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path, 2)), "--llm", "fixture",
                     "--loop", "both", "--out", str(tmp_path / "e.json"), "--csv", str(out_csv)])
        assert code == EXIT_ERROR
        assert "worker crashed" in capsys.readouterr().err
        rows = list(csv.DictReader(out_csv.open(encoding="utf-8")))
        # the first brief's job (structured once, both conditions) finished, then the crash
        assert [(r["brief"], r["condition"]) for r in rows] == [
            ("zz_brief_0", "loop_off"), ("zz_brief_0", "loop_on")]
        assert not (tmp_path / "e.json").exists()   # the report needs every row

    def test_workers_run_in_parallel_and_keep_job_order(self, tmp_path, capsys, monkeypatch):
        """Four jobs that each sleep 0.2 s finish in well under 0.8 s on 4 workers, and the
        report lists them in brief / condition / repeat order whatever order they finished."""
        active, peak, lock = [0], [0], threading.Lock()

        class Slow(RecordingPlanner):
            def structure(self, brief, *, brief_id=None):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.2)
                with lock:
                    active[0] -= 1
                return None, FAILED_RESULT.steps[0]

        Slow.created = []
        monkeypatch.setattr(cli_module, "Planner", Slow)
        t0 = time.perf_counter()
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path, 2)), "--llm", "fixture",
                     "--workers", "4", "--out", str(tmp_path / "e.json"),
                     "--csv", str(tmp_path / "e.csv")])
        elapsed = time.perf_counter() - t0
        assert code == EXIT_OK and peak[0] >= 2 and elapsed < 0.7
        report = json.loads((tmp_path / "e.json").read_text())
        assert [(r["brief"], r["condition"], r["repeat"]) for r in report["rows"]] == [
            ("zz_brief_0", "loop_off", 1), ("zz_brief_0", "loop_on", 1),
            ("zz_brief_1", "loop_off", 1), ("zz_brief_1", "loop_on", 1)]
        assert report["workers"] == 4
        err = capsys.readouterr().err
        assert err.count("FAIL llm_error") == 4 and "workers=4" in err


# --------------------------------------------------------------------------- eval arms / cost / markdown
ULTRA_ID = ARM_MODELS["ultra"]
SUPER_ID = ARM_MODELS["super"]


def priced_step(stage: str, model: str, tokens_in: int, tokens_out: int, *, attempt: int = 1,
                latency: float = 1.0) -> LoopStep:
    return LoopStep(stage=stage, attempt=attempt, ok=True,
                    usage=Usage(model=model, input_tokens=tokens_in, output_tokens=tokens_out,
                                latency_s=latency))


class TestEvalArms:
    def test_parse_arms(self):
        assert parse_arms(None) is None and parse_arms("  ") is None and parse_arms(",") is None
        assert parse_arms("ultra, super") == {"ultra": ULTRA_ID, "super": SUPER_ID}
        assert parse_arms("NANO") == {"nano": ARM_MODELS["nano"]}
        assert parse_arms("lightning,mini=nvidia/some-model") == {
            "lightning": ARM_MODELS["lightning"], "mini": "nvidia/some-model"}
        for bad, why in (("opus", "unknown arm"), ("ultra,ultra", "listed twice"),
                         ("=x", "name=model-id"), ("a=", "name=model-id"),
                         ("a/b=nvidia/x", "letters, digits")):
            with pytest.raises(ValueError, match=why):
                parse_arms(bad)
        assert condition_name("ultra", "loop_on") == "ultra/loop_on"
        assert condition_name(None, "loop_off") == "loop_off"

    def test_thinking_overrides(self):
        assert thinking_overrides(None) == {} and thinking_overrides("auto") == {}
        assert thinking_overrides("on") == {"taskgraph": True, "repair": True}
        assert thinking_overrides("off") == {"taskgraph": False, "repair": False}
        with pytest.raises(ValueError, match="--thinking"):
            thinking_overrides("maybe")

    def test_make_arm_client_routes_the_graph_roles_only(self, monkeypatch):
        monkeypatch.setattr(cli_module, "load_env", lambda: False)
        monkeypatch.setenv("NEBIUS_API_KEY", "k")
        for var in ("MOONWRIGHT_LLM", "MOONWRIGHT_THINKING", "MOONWRIGHT_MODEL_STRUCTURE",
                    "MOONWRIGHT_MODEL_TASKGRAPH", "MOONWRIGHT_MODEL_REPAIR",
                    "MOONWRIGHT_MODEL_EXPLAIN"):
            monkeypatch.delenv(var, raising=False)
        assert isinstance(make_arm_client("fixture", SUPER_ID, {"taskgraph": False}), FixtureClient)
        client = make_arm_client("nebius", SUPER_ID, thinking_overrides("off"))
        assert isinstance(client, TokenFactoryClient)
        assert client.models == {"structure": MODEL_DEFAULTS["structure"], "taskgraph": SUPER_ID,
                                 "repair": SUPER_ID, "explain": MODEL_DEFAULTS["explain"]}
        assert client.thinking == {"structure": False, "taskgraph": False, "repair": False,
                                   "explain": False}
        client = make_arm_client("nebius", None, thinking_overrides("on"))
        assert client.models == MODEL_DEFAULTS
        assert client.thinking == {"structure": False, "taskgraph": True, "repair": True,
                                   "explain": False}
        # the environment keeps the roles the arm does not name, and a forced env thinking
        # loses to --thinking for the graph roles only
        monkeypatch.setenv("MOONWRIGHT_MODEL_EXPLAIN", "env/explain")
        monkeypatch.setenv("MOONWRIGHT_THINKING", "on")
        client = make_arm_client("nebius", ULTRA_ID, thinking_overrides("off"))
        assert client.models["explain"] == "env/explain" and client.models["repair"] == ULTRA_ID
        assert client.thinking == {"structure": True, "taskgraph": False, "repair": False,
                                   "explain": True}
        recorded = make_arm_client("record", ULTRA_ID)
        assert isinstance(recorded, RecordingClient)
        assert isinstance(recorded.inner, TokenFactoryClient)
        assert recorded.inner.thinking["taskgraph"] is True   # env "on", no override
        with pytest.raises(ValueError, match="unknown LLM client kind"):
            make_arm_client("openai", ULTRA_ID)
        monkeypatch.delenv("NEBIUS_API_KEY")
        with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
            make_arm_client("nebius", ULTRA_ID)

    def test_prices_and_run_cost(self):
        assert set(PRICES_USD_PER_M) == set(ARM_MODELS.values())
        assert price_for(ULTRA_ID) == (1.0, 3.0) and price_for(ULTRA_ID.upper()) == (1.0, 3.0)
        assert price_for("nvidia/Nemotron-3.5-Super-VL") is None and price_for(None) is None
        assert cost_usd(ULTRA_ID, 3000, 10000) == pytest.approx(0.003 + 0.03)
        assert cost_usd(ARM_MODELS["lightning"], 2300, 490) == pytest.approx(0.0002556, abs=5e-7)
        assert cost_usd(ULTRA_ID, None, None) == 0.0 and cost_usd("fixture:x.json", 10, 10) is None
        assert format_usd(None) == "-" and format_usd(0.03312) == "$0.0331"
        assert format_usd(1.2345, decimals=2) == "$1.23"
        # a run sums its steps; fixture runs carry no tokens and cost nothing
        steps = [priced_step("structure", ARM_MODELS["lightning"], 2300, 490),
                 priced_step("taskgraph", ULTRA_ID, 3000, 10000),
                 LoopStep(stage="validate", ok=True), LoopStep(stage="schedule", ok=True),
                 priced_step("repair", ULTRA_ID, 4000, 9000, attempt=2)]
        result = PlanResult(requirements=REQ, task_graph=FIRST_GRAPH, schedule=SCHEDULE,
                            success=True, repairs=1, steps=steps)
        expected = (2300 * 0.06 + 490 * 0.24 + 7000 * 1.0 + 19000 * 3.0) / 1e6
        assert run_cost_usd(result) == pytest.approx(expected, abs=5e-7)   # rounded once, 6 dp
        row = eval_row("demo", "ultra/loop_on", 1, result, 90.0, first_graph=FIRST_GRAPH)
        assert row.cost_usd == pytest.approx(expected, abs=5e-7)
        assert row.condition == "ultra/loop_on"
        assert cost_usd(ULTRA_ID, 1, 1, decimals=None) == pytest.approx(4e-6)
        offline = PlanResult(requirements=REQ, steps=[
            LoopStep(stage="taskgraph", ok=True, usage=Usage(model="fixture:taskgraph_1.json"))])
        assert run_cost_usd(offline) == 0.0
        unpriced = PlanResult(requirements=REQ, steps=[
            priced_step("taskgraph", "nvidia/Nemotron-3.5-Super-VL", 100, 10)])
        assert run_cost_usd(unpriced) is None
        # the CSV keeps its prefix and takes the new columns last (plan_path stays last)
        assert cli_module.CSV_FIELDS[:4] == ("brief", "condition", "repeat", "success")
        assert cli_module.CSV_FIELDS[-9:] == ("cost_usd", "volume_advisories", "min_volume_share",
                                              "in_scope", "requirements_sha", "kind",
                                              "rejected_answers", "min_area_share", "plan_path")

    def test_summarise_percentiles_and_costs(self):
        def row(condition: str, latency: float, cost: float | None, **kw: Any) -> EvalRow:
            return EvalRow(brief="b", condition=condition, repeat=1, success=True, repairs=0,
                           makespan_h=10, latency_s=latency, llm_latency_s=latency / 2,
                           input_tokens=1, output_tokens=1, cost_usd=cost, **kw)

        rows = [row("ultra/loop_on", 10.0, 0.05), row("ultra/loop_on", 20.0, 0.07),
                row("ultra/loop_on", 40.0, None), row("super/loop_on", 5.0, 0.01)]
        ultra, sup = summarise(rows)
        assert ultra.condition == "ultra/loop_on" and sup.condition == "super/loop_on"
        assert (ultra.latency_p50_s, ultra.latency_p95_s) == (20.0, 38.0)
        assert ultra.mean_llm_latency_s == pytest.approx(35 / 3)
        assert (ultra.mean_cost_usd, ultra.total_cost_usd, ultra.priced_runs) == (0.06, 0.12, 2)
        assert (sup.latency_p50_s, sup.latency_p95_s, sup.priced_runs) == (5.0, 5.0, 1)
        table = format_summary([ultra, sup])
        assert table.splitlines()[0].split()[:2] == ["condition", "runs"]
        assert table.splitlines()[1].startswith("ultra/loop_on ")   # widened to the names
        assert "$0.0600" in table.splitlines()[1] and "38.0s" in table.splitlines()[1]

    def test_matrix_cell(self):
        ok = EvalRow(brief="a", condition="c", repeat=1, success=True, repairs=0, makespan_h=76,
                     latency_s=1, llm_latency_s=0, input_tokens=0, output_tokens=0)
        fail = EvalRow(brief="a", condition="c", repeat=2, success=False, repairs=3,
                       latency_s=1, llm_latency_s=0, input_tokens=0, output_tokens=0,
                       failure_kind="infeasible", conflicts=["energy:EX1", "energy:EX2"])
        assert matrix_cell([ok]) == "ok 76 h / 0 rep"
        assert matrix_cell([fail]) == "fail energy"
        assert matrix_cell([]) == "-"
        repaired = ok.model_copy(update={"repairs": 1, "makespan_h": 80})
        scoped = fail.model_copy(update={"failure_kind": "validation",
                                         "conflicts": ["scope:berm", "energy:EX1"]})
        assert matrix_cell([ok, repaired, scoped]) == "ok 2/3 78 h / 0.5 rep; fail 1/3 energy,scope"
        no_conflicts = fail.model_copy(update={"failure_kind": "llm_error", "conflicts": []})
        assert matrix_cell([no_conflicts, no_conflicts]) == "fail 2/2 llm_error"

    def test_eval_markdown_on_a_synthetic_report(self):
        rows = [
            EvalRow(brief="pad", condition="ultra/loop_off", repeat=1, success=True, repairs=0,
                    makespan_h=76, latency_s=40.0, llm_latency_s=38.0, input_tokens=5300,
                    output_tokens=11000, cost_usd=0.036, thinking="auto",
                    first_try_feasible=True, in_scope=True, kind="A"),
            EvalRow(brief="pad", condition="ultra/loop_on", repeat=1, success=True, repairs=0,
                    makespan_h=76, latency_s=42.0, llm_latency_s=40.0, input_tokens=5300,
                    output_tokens=11000, cost_usd=0.036, thinking="auto",
                    first_try_feasible=True, in_scope=True, kind="A"),
            EvalRow(brief="berm", condition="ultra/loop_off", repeat=1, success=False, repairs=0,
                    latency_s=30.0, llm_latency_s=28.0, input_tokens=5000, output_tokens=9000,
                    failure_kind="infeasible", conflicts=["energy:EX1"], cost_usd=0.032,
                    thinking="auto", kind="D"),
            # verified, but by digging a third of the pad: feasible, not in scope
            EvalRow(brief="berm", condition="ultra/loop_on", repeat=1, success=True, repairs=1,
                    makespan_h=90, latency_s=102.0, llm_latency_s=100.0, input_tokens=11000,
                    output_tokens=20000, cost_usd=0.071, thinking="auto",
                    volume_advisories=1, min_volume_share=0.36, in_scope=False, kind="D"),
        ]
        report = EvalReport(
            generated_at="2026-10-25T00:00:00+00:00", llm="nebius", briefs_dir="examples/eval",
            conditions=["ultra/loop_off", "ultra/loop_on"], repeat=1, summary=summarise(rows),
            rows=rows, max_repairs=3, feedback="core", max_answer_retries=2, workers=3,
            thinking="auto", arms={"ultra": ULTRA_ID},
            role_models={"structure": MODEL_DEFAULTS["structure"], "taskgraph": ULTRA_ID,
                         "repair": ULTRA_ID, "explain": MODEL_DEFAULTS["explain"]})
        text = eval_markdown(report)
        assert text.startswith("# Moonwright eval: 2 brief(s) x 2 condition(s) x 1 repeat(s)\n")
        assert "llm `nebius`; briefs from `examples/eval`; 4 run(s) on 3 worker(s)" in text
        assert "max_answer_retries 2; thinking `auto`." in text
        assert "## How to read" in text and "## Per condition" in text and "## Per brief" in text
        assert f"`ultra` = `{ULTRA_ID}`" in text
        assert f"structured by `{MODEL_DEFAULTS['structure']}`" in text
        assert f"written by `{MODEL_DEFAULTS['explain']}`" in text
        assert "Thinking `auto`: the task-graph and repair calls reason" in text
        assert "up to 3 repair round(s), each fed the solver's minimal infeasibility core" in text
        assert "re-asks are capped at 2" in text and "--llm fixture" not in text
        assert ("| `ultra/loop_off` | 2 | 50% (9%-91%) | 50% (9%-91%) | 50% | 0.00 | 76 h | "
                "35.0 s / 39.5 s | 5150 / 10000 | $0.0340 | $0.07 |") in text
        assert ("| `ultra/loop_on` | 2 | 100% (34%-100%) | 50% (9%-91%) | 50% | 0.50 | 83 h | "
                "72.0 s / 99.0 s | 8150 / 15500 | $0.0535 | $0.11 |") in text
        assert "| brief | ultra/loop_off | ultra/loop_on |" in text
        assert "| `pad` | ok 76 h / 0 rep | ok 76 h / 0 rep |" in text
        assert "| `berm` | fail energy | ok 90 h / 1 rep scope! |" in text
        # the per-kind table and the sentences that explain pairing and scope
        assert "## Per kind" in text
        assert "| `A` | `ultra/loop_off` | 1 | 1/1 | 1/1 | 1/1 | 0.00 | `pad` |" in text
        assert "| `D` | `ultra/loop_on` | 1 | 1/1 | 0/1 | 0/1 | 1.00 | `berm` |" in text
        assert "structured once per repeat" in text and "`requirements_sha`" in text
        assert "`feasible in scope` the share that is feasible AND" in text
        assert "at 95 % or more of the smallest size the brief allows" in text
        assert "`min_area_share`" in text and "not against the scope" in text
        without_kinds = report.model_copy(update={"rows": [r.model_copy(update={"kind": ""})
                                                           for r in rows]})
        assert "## Per kind" not in eval_markdown(without_kinds)
        verdict = report.model_copy(update={"feedback": "verdict", "arms": {}, "llm": "fixture",
                                            "role_models": {}, "max_answer_retries": None})
        text = eval_markdown(verdict)
        assert "each fed only the verdict kind (the ablation arm)" in text
        assert "come from the configured model" in text and "--llm fixture" in text
        assert "max_answer_retries the planner's own" in text and "re-asks are capped" not in text


class TestEvalArmsCommand:
    def test_arms_and_thinking_name_the_conditions(self, tmp_path, capsys, planner_stub):
        stub = planner_stub(RecordingPlanner)
        out_json, out_csv, out_md = tmp_path / "e.json", tmp_path / "e.csv", tmp_path / "e.md"
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path, 2)), "--llm", "fixture",
                     "--models", "ultra,super", "--thinking", "off", "--loop", "both",
                     "--max-repairs", "2", "--out", str(out_json), "--csv", str(out_csv),
                     "--markdown", str(out_md)])
        assert code == EXIT_OK
        report = json.loads(out_json.read_text())
        assert report["conditions"] == ["ultra/loop_off", "ultra/loop_on", "super/loop_off",
                                        "super/loop_on"]
        assert report["arms"] == {"ultra": ULTRA_ID, "super": SUPER_ID}
        assert report["role_models"] == {} and report["thinking"] == "off"
        assert report["llm"] == "fixture"
        assert [(r["brief"], r["condition"]) for r in report["rows"]] == [
            (b, c) for b in ("zz_brief_0", "zz_brief_1") for c in report["conditions"]]
        assert all(r["thinking"] == "off" and r["cost_usd"] == 0.0 for r in report["rows"])
        assert sorted(c["max_repairs"] for c in stub.created) == [0, 0, 0, 0, 2, 2, 2, 2]
        assert [s["condition"] for s in report["summary"]] == report["conditions"]
        assert all(s["priced_runs"] == 2 and s["total_cost_usd"] == 0.0 for s in report["summary"])
        text = out_md.read_text()
        assert "| brief | ultra/loop_off | ultra/loop_on | super/loop_off | super/loop_on |" in text
        assert "| `zz_brief_0` | fail llm_error | fail llm_error | fail llm_error | fail llm_error |" in text
        assert "Thinking `off`" in text and "This run used `--llm fixture`" in text
        with out_csv.open(encoding="utf-8") as fh:
            csv_rows = list(csv.DictReader(fh))
        assert {r["condition"] for r in csv_rows} == set(report["conditions"])
        assert list(csv_rows[0])[-1] == "plan_path" and csv_rows[0]["cost_usd"] == "0.0"
        assert all(r["plan_path"] == "" for r in csv_rows)   # no --save-plans
        captured = capsys.readouterr()
        assert f"arms=ultra={ULTRA_ID},super={SUPER_ID}" in captured.err
        assert "thinking=off" in captured.err and "4 condition(s)" in captured.err
        assert captured.err.rstrip().splitlines()[-1].startswith("super/loop_on")   # table on stderr
        assert captured.out.rstrip().endswith(f"-> {out_json}, {out_csv}, {out_md}")

    def test_bad_arm_is_an_input_error(self, tmp_path, capsys, planner_stub):
        stub = planner_stub(RecordingPlanner)
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path, 1)), "--llm", "fixture",
                     "--models", "opus", "--out", str(tmp_path / "e.json"),
                     "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_ERROR and stub.created == []
        assert "unknown arm 'opus'" in capsys.readouterr().err

    def test_live_arms_build_one_client_per_arm(self, tmp_path, capsys, planner_stub, monkeypatch):
        """With a live kind every arm gets its own TokenFactoryClient (no network: the planner
        is a stub), routed to the arm's model for the graph roles, shared by the conditions."""
        stub = planner_stub(RecordingPlanner)
        monkeypatch.setattr(cli_module, "load_env", lambda: False)
        monkeypatch.setenv("NEBIUS_API_KEY", "k")
        for var in ("MOONWRIGHT_LLM", "MOONWRIGHT_THINKING", "MOONWRIGHT_MODEL_TASKGRAPH",
                    "MOONWRIGHT_MODEL_REPAIR"):
            monkeypatch.delenv(var, raising=False)
        clients: list[Any] = []
        original = RecordingPlanner.__init__

        def remember(self, client, **kwargs):
            clients.append(client)
            original(self, client, **kwargs)

        monkeypatch.setattr(RecordingPlanner, "__init__", remember)
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path, 1)), "--llm", "nebius",
                     "--models", "ultra,nano", "--thinking", "on", "--loop", "both",
                     "--out", str(tmp_path / "e.json"), "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_OK and len(stub.created) == 4
        inner = [c.inner for c in clients]          # GraphCapture wraps the arm's client
        assert all(isinstance(c, TokenFactoryClient) for c in inner)
        assert inner[0] is inner[1] and inner[2] is inner[3] and inner[0] is not inner[2]
        assert inner[0].models["taskgraph"] == ULTRA_ID and inner[2].models["repair"] == ARM_MODELS["nano"]
        assert all(c.thinking["taskgraph"] and c.thinking["repair"] for c in inner)
        report = json.loads((tmp_path / "e.json").read_text())
        assert report["llm"] == "nebius" and report["thinking"] == "on"
        assert report["role_models"]["structure"] == MODEL_DEFAULTS["structure"]
        assert report["role_models"]["taskgraph"] == MODEL_DEFAULTS["taskgraph"]
        assert "arms=ultra=" in capsys.readouterr().err


# --------------------------------------------------------------------------- scripts/llm_schedule_check.py
def load_schedule_check():
    spec = importlib.util.spec_from_file_location("llm_schedule_check", SCHEDULE_CHECK_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScheduleStub:
    """Scripted answers to the direct-scheduling prompt: dicts are validated into Schedules,
    exceptions are raised."""

    name = "stub"

    def __init__(self, answers: list[Any]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        self.prompts.append(user)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return validate_into(schema, answer), Usage(model="stub-model", input_tokens=50,
                                                    output_tokens=20, latency_s=0.1)


def entries(*spans: tuple[str, str, int, int]) -> list[dict[str, Any]]:
    return [{"task": t, "robot": r, "start_h": s, "end_h": e} for t, r, s, e in spans]


class TestLlmScheduleCheck:
    def test_recomputes_the_makespan_and_counts_clean_worse_answers(self, tmp_path, monkeypatch,
                                                                    capsys):
        module = load_schedule_check()
        plan_path = tmp_path / "demo.plan.json"
        plan_path.write_text(AT_DEPOT.model_dump_json())
        answers = [
            # 1: valid entries, absurd claimed makespan -> recomputed to 18, equal to the solver
            {"entries": entries(("T1", "EX1", 0, 6), ("T2", "EX1", 6, 18)), "makespan_h": 999,
             "status": "feasible"},
            # 2: clean but 4 h worse than the solver's optimum
            {"entries": entries(("T1", "EX1", 0, 6), ("T2", "EX1", 10, 22)), "makespan_h": 22,
             "status": "feasible"},
            # 3: T2 overlaps T1 on EX1 (and in zone Z1) and starts before T1 ends
            {"entries": entries(("T1", "EX1", 0, 6), ("T2", "EX1", 3, 15)), "makespan_h": 15,
             "status": "feasible"},
            # 4: the model failed to answer at all
            LLMError("structure (m): APITimeoutError"),
        ]
        stub = ScheduleStub(answers)
        monkeypatch.setattr(module, "make_client", lambda kind=None: stub)
        out = tmp_path / "check.json"
        code = module.main([str(plan_path), "--repeat", "4", "--out", str(out)])
        assert code == 0
        report = json.loads(out.read_text())
        rows = report["rows"]
        assert [r["model_makespan_h"] for r in rows] == [18, 22, 15, None]
        assert [r["claimed_makespan_h"] for r in rows] == [999, 22, 15, None]
        assert [r["violations"] for r in rows] == [0, 0, 3, None]
        assert rows[2]["conflicts"] == ["precedence:T1->T2", "robot:EX1", "zone:Z1"]
        assert [r["travel_only"] for r in rows] == [False, False, False, None]
        assert "makespan" not in rows[0]["conflicts"]          # the claim is not a violation
        assert rows[3]["llm_error"].startswith("LLMError") and rows[3]["conflicts"] == []
        assert all(r["solver_makespan_h"] == 18 and r["solver_status"] == "optimal" for r in rows)
        assert [r["makespan_gap_h"] for r in rows] == [0, 4, -3, None]
        assert all(r["plan"] == "demo" and r["model"] == "stub-model" for r in rows[:3])
        summary = report["summary"]
        assert (summary["runs"], summary["answered"], summary["violating"]) == (4, 3, 1)
        assert summary["violation_rate"] == pytest.approx(1 / 3)
        assert summary["travel_only_violating"] == 0
        assert summary["violation_rate_excluding_travel"] == pytest.approx(1 / 3)
        assert "travel" in summary["rules_told_to_the_model"]
        assert summary["conflict_kinds"] == {"precedence": 1, "robot": 1, "zone": 1}
        assert summary["makespan_claim_mismatches"] == 1
        assert (summary["clean"], summary["clean_equal_makespan"], summary["clean_worse_makespan"],
                summary["clean_better_makespan"]) == (2, 1, 1, 0)
        assert summary["clean_worse_makespan_rate"] == 0.5
        assert (summary["makespan_gap_mean_h"], summary["makespan_gap_max_h"]) == (2.0, 4)
        assert summary["makespan_gap_mean_pct"] == pytest.approx(100 * 2 / 18, abs=0.01)
        assert summary["models"] == ["stub-model"]
        # the CSV next to the JSON has one row per run
        csv_rows = list(csv.DictReader((tmp_path / "check.csv").open(encoding="utf-8")))
        assert len(csv_rows) == 4 and list(csv_rows[0]) == list(module.CSV_FIELDS)
        assert csv_rows[2]["conflicts"] == "precedence:T1->T2;robot:EX1;zone:Z1"
        assert csv_rows[0]["claimed_makespan_h"] == "999" and csv_rows[0]["model_makespan_h"] == "18"
        printed = capsys.readouterr().out
        assert "makespan 18 (claimed 999) vs solver 18 optimal" in printed
        assert printed.rstrip().endswith(f"-> {out}, {tmp_path / 'check.csv'}")
        # the prompt carries the requirements, the geometry and the graph, never the brief text
        assert "TASKS:" in stub.prompts[0] and "source_brief" not in stub.prompts[0]
        assert "DEPOT: (20, 1)\nZONE CENTRES: Z1 at (20, 1)" in stub.prompts[0]
        assert "whole hours apart" in module.SYSTEM and "0.5 Wh" in module.SYSTEM

    def test_travel_only_answers_are_counted_apart(self, tmp_path, monkeypatch):
        """Z2 is 12 cells from Z1, an hour at 12 cells/h. An answer that puts T2 right after T1
        breaks only the travel rule: it is a violation (the model was told the rule) but the
        summary sets it apart from scheduling defects proper."""
        module = load_schedule_check()
        site = Site(zones=[Zone(id="Z1", center=(20, 1)), Zone(id="Z2", center=(20, 13))])
        graph = GRAPH2.model_copy(update={"tasks": [
            GRAPH2.tasks[0], GRAPH2.tasks[1].model_copy(update={"zone": "Z2"})]})
        schedule = Schedule(entries=[ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=6),
                                     ScheduleEntry(task="T2", robot="EX1", start_h=7, end_h=19)],
                            makespan_h=19, status="optimal")
        plan = PlanResult(requirements=REQ.model_copy(update={"site": site}), task_graph=graph,
                          schedule=schedule, success=True)
        assert check_schedule(plan.requirements, graph, schedule, module.CHECK_OPTS) == []
        plan_path = tmp_path / "two.plan.json"
        plan_path.write_text(plan.model_dump_json())
        stub = ScheduleStub([
            {"entries": entries(("T1", "EX1", 0, 6), ("T2", "EX1", 6, 18)), "makespan_h": 18,
             "status": "feasible"},                                   # no hour for the drive
            {"entries": entries(("T1", "EX1", 0, 6), ("T2", "EX1", 7, 19)), "makespan_h": 19,
             "status": "feasible"},                                   # the solver's own answer
        ])
        monkeypatch.setattr(module, "make_client", lambda kind=None: stub)
        out = tmp_path / "check.json"
        assert module.main([str(plan_path), "--repeat", "2", "--out", str(out)]) == 0
        report = json.loads(out.read_text())
        rows = report["rows"]
        assert [r["conflicts"] for r in rows] == [["robot:EX1"], []]
        assert [r["travel_only"] for r in rows] == [True, False]
        summary = report["summary"]
        assert (summary["violating"], summary["travel_only_violating"]) == (1, 1)
        assert summary["violation_rate"] == 0.5 and summary["violation_rate_excluding_travel"] == 0.0
        assert "Z2 at (20, 13)" in stub.prompts[0]
        assert module.travel_only(["robot:EX1"], Schedule(
            entries=[ScheduleEntry(task="T1", robot="EX1", start_h=0, end_h=6),
                     ScheduleEntry(task="T2", robot="EX1", start_h=3, end_h=15)],
            makespan_h=15, status="feasible")) is False                # a double booking
        assert module.travel_only([], schedule) is False

    def test_skips_unverified_plans_and_honours_the_csv_flag(self, tmp_path, monkeypatch, capsys):
        module = load_schedule_check()
        unverified = tmp_path / "no.plan.json"
        unverified.write_text(PlanResult(requirements=REQ).model_dump_json())
        broken = SCHEDULE.model_copy(update={"entries": SCHEDULE.entries[:1]})
        edited = tmp_path / "edited.plan.json"
        edited.write_text(VERIFIED.model_copy(update={"schedule": broken}).model_dump_json())
        stub = ScheduleStub([])
        monkeypatch.setattr(module, "make_client", lambda kind=None: stub)
        out, out_csv = tmp_path / "r.json", tmp_path / "rows.csv"
        assert module.main([str(unverified), str(edited), "--out", str(out),
                            "--csv", str(out_csv)]) == 0
        assert stub.prompts == []
        report = json.loads(out.read_text())
        assert report["rows"] == [] and report["summary"]["runs"] == 0
        assert report["summary"]["violation_rate"] is None
        assert out_csv.read_text().splitlines() == [",".join(module.CSV_FIELDS)]
        err = capsys.readouterr().err
        assert "not a verified plan" in err and "fails re-verification" in err

    def test_models_arm(self, tmp_path, monkeypatch):
        module = load_schedule_check()
        assert module.parse_models(None) == {} and module.parse_models("  ") == {}
        assert module.parse_models("nvidia/x") == {"taskgraph": "nvidia/x"}
        assert module.parse_models("repair=a, structure = b,") == {"repair": "a", "structure": "b"}
        with pytest.raises(ValueError, match="unknown role"):
            module.parse_models("scheduler=x")
        with pytest.raises(ValueError, match="no model id"):
            module.parse_models("repair=")
        # the arm is applied to the environment before the client is built
        monkeypatch.setenv("MOONWRIGHT_MODEL_TASKGRAPH", "placeholder")
        seen: dict[str, str] = {}

        def fake_client(kind=None):
            seen["model"] = os.environ["MOONWRIGHT_MODEL_TASKGRAPH"]
            return ScheduleStub([])

        monkeypatch.setattr(module, "make_client", fake_client)
        out = tmp_path / "arm.json"
        plan_path = tmp_path / "demo.plan.json"
        plan_path.write_text(PlanResult(requirements=REQ).model_dump_json())
        assert module.main([str(plan_path), "--models", "nvidia/super", "--out", str(out)]) == 0
        assert seen["model"] == "nvidia/super"
        with pytest.raises(SystemExit):
            module.main([str(plan_path), "--models", "bogus=x", "--out", str(out)])
        with pytest.raises(SystemExit):
            module.main([str(plan_path), "--repeat", "0", "--out", str(out)])


# --------------------------------------------------------------------------- CLI commands
class TestCliCommands:
    def test_plan_without_fixtures_writes_result_and_exits_1(self, tmp_path, capsys):
        brief = tmp_path / "pad.md"
        brief.write_text("# Pad\nBuild a 20 m landing pad with EX1 (5000 Wh).")
        out = tmp_path / "pad.plan.json"
        code = main(["plan", str(brief), "--llm", "fixture", "--brief-id", "zz_no_fixture",
                     "--out", str(out)])
        assert code == EXIT_NO_PLAN
        result = PlanResult.model_validate_json(out.read_text())
        assert result.success is False and result.failure.kind == "llm_error"
        captured = capsys.readouterr()
        assert "structure #1 FAIL" in captured.out
        assert captured.out.rstrip().endswith(str(out))
        assert "planning zz_no_fixture: llm=fixture" in captured.err

    def test_plan_from_stdin_quiet(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("Build a berm with HL1."))
        out = tmp_path / "stdin.plan.json"
        code = main(["plan", "-", "--llm", "fixture", "--out", str(out), "--quiet"])
        assert code == EXIT_NO_PLAN and out.exists()
        captured = capsys.readouterr()
        assert captured.out.count("\n") == 1  # summary line only
        assert captured.out.startswith("no verified plan after 0 repair(s)")

    def test_plan_with_an_unusable_brief_id_falls_back_like_a_missing_one(
            self, tmp_path, capsys, monkeypatch):
        # a file brief: back to the file stem
        brief = tmp_path / "pad.md"
        brief.write_text("# Pad\nBuild a 20 m landing pad with EX1 (5000 Wh).")
        out = tmp_path / "pad.plan.json"
        code = main(["plan", str(brief), "--llm", "fixture", "--brief-id", "###",
                     "--out", str(out), "--quiet"])
        assert code == EXIT_NO_PLAN and out.exists()
        assert "planning pad:" in capsys.readouterr().err
        result = PlanResult.model_validate_json(out.read_text())
        assert "no recording for brief 'pad'" in result.failure.hint
        # the hint is for the person at the UI; the lookup path only goes to the details
        assert "fixtures" not in result.failure.hint and "--llm" not in result.failure.hint
        assert result.failure.details == [
            f"no fixture directory for brief 'pad' (looked in {FIXTURES_DIR / 'pad'})"]
        # stdin has no stem: back to the brief hash, never an empty key
        monkeypatch.setattr(sys, "stdin", io.StringIO("Build a berm with HL1."))
        out = tmp_path / "stdin.plan.json"
        code = main(["plan", "-", "--llm", "fixture", "--brief-id", "###", "--out", str(out),
                     "--quiet"])
        assert code == EXIT_NO_PLAN
        err = capsys.readouterr().err
        assert "planning brief_" in err and "planning :" not in err
        result = PlanResult.model_validate_json(out.read_text())
        assert "no recording for brief 'brief_" in result.failure.hint

    def test_simulate_with_an_unusable_brief_id_falls_back_to_the_plan_stem(
            self, tmp_path, capsys, monkeypatch):
        calls: dict[str, Any] = {}
        monkeypatch.setattr("moonwright.cli.run_with_replanning", make_fake_run(calls))
        plan_path = tmp_path / "demo.plan.json"
        plan_path.write_text(VERIFIED.model_dump_json())
        code = main(["simulate", str(plan_path), "--llm", "fixture", "--brief-id", "###",
                     "--out", str(tmp_path / "demo.sim.json")])
        assert code == EXIT_OK and calls["key"] == "demo"
        assert "simulating demo:" in capsys.readouterr().err

    def test_plan_missing_file_is_a_usage_error(self, tmp_path, capsys):
        assert main(["plan", str(tmp_path / "nope.md"), "--llm", "fixture"]) == EXIT_ERROR
        assert "error:" in capsys.readouterr().err

    def test_plan_empty_brief(self, tmp_path, capsys):
        brief = tmp_path / "empty.md"
        brief.write_text("  \n")
        assert main(["plan", str(brief), "--llm", "fixture"]) == EXIT_ERROR
        assert "brief is empty" in capsys.readouterr().err

    def test_simulate_rejects_plan_without_schedule(self, tmp_path, capsys):
        plan = tmp_path / "x.plan.json"
        plan.write_text(PlanResult(requirements=REQ).model_dump_json())
        assert main(["simulate", str(plan), "--llm", "fixture"]) == EXIT_ERROR
        assert "no verified schedule" in capsys.readouterr().err

    def test_simulate_prints_events_replans_and_thins_frames(self, tmp_path, capsys, monkeypatch):
        calls: dict[str, Any] = {}
        monkeypatch.setattr("moonwright.cli.run_with_replanning", make_fake_run(calls))
        plan_path = tmp_path / "demo.plan.json"
        plan_path.write_text(PlanResult(requirements=REQ, task_graph=GRAPH2, schedule=SCHEDULE,
                                        success=True).model_dump_json())
        out = tmp_path / "demo.sim.json"
        code = main(["simulate", str(plan_path), "--llm", "fixture", "--fail", "EX1@3",
                     "--max-replans", "1", "--max-repairs", "2", "--out", str(out)])
        assert code == EXIT_OK
        assert calls == {"key": "demo", "max_replans": 1, "max_repairs": 2,
                         "injections": [FailureInjection("EX1", 3.0)], "dt_h": 0.25}
        text = capsys.readouterr().out
        assert "events (1):" in text and "robot_failure" in text
        assert "EX1 task T1  motor stall" in text
        assert ("#1 at t=3.00h after robot_failure EX1: 2 tasks remaining; "
                "makespan 18h -> 22h (optimal, 1 repair(s))") in text
        assert "completed at t=25.00h" in text
        assert "(3 of 9 frames)" in text
        saved = SimResult.model_validate_json(out.read_text())
        assert [f.t_h for f in saved.frames] == [0.0, 1.0, 2.0]
        assert saved.replans[0].after.schedule.makespan_h == 22

    def test_simulate_dt_forwarding_and_all_frames(self, tmp_path, capsys, monkeypatch):
        plan_path = tmp_path / "demo.plan.json"
        plan_path.write_text(PlanResult(requirements=REQ, task_graph=GRAPH2, schedule=SCHEDULE,
                                        success=True).model_dump_json())
        out = tmp_path / "demo.sim.json"
        calls: dict[str, Any] = {}
        monkeypatch.setattr("moonwright.cli.run_with_replanning", make_fake_run(calls))
        assert main(["simulate", str(plan_path), "--llm", "fixture", "--dt", "0.5",
                     "--all-frames", "--out", str(out)]) == EXIT_OK
        captured = capsys.readouterr()
        assert calls["dt_h"] == 0.5
        assert "warning" not in captured.err
        assert "(all 9 frames)" in captured.out
        assert len(SimResult.model_validate_json(out.read_text()).frames) == 9

    def test_montecarlo_prints_the_table_and_writes_the_result(self, tmp_path, capsys):
        plan_path = tmp_path / "demo.plan.json"
        plan_path.write_text(VERIFIED.model_dump_json())
        out = tmp_path / "demo.mc.json"
        code = main(["montecarlo", str(plan_path), "--runs", "4", "--seed", "7",
                     "--out", str(out)])
        captured = capsys.readouterr()
        assert code == EXIT_OK, captured.err
        assert "monte carlo demo: 4 runs" in captured.err
        assert "seed=7, replans via solver-only" in captured.err
        assert captured.out.startswith("Monte Carlo robustness: 4 runs, horizon 100 h")
        assert "within horizon" in captured.out and "finished_at_h of completed runs" in captured.out
        assert captured.out.rstrip().endswith(f"-> {out}")
        result = MonteCarloResult.model_validate_json(out.read_text())
        assert result.runs == 4 and [s.seed for s in result.samples] == [7, 8, 9, 10]
        assert result.completed == 4 and result.within_horizon_rate == 1.0

    def test_montecarlo_default_output_path_and_llm_choice(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan_path = tmp_path / "demo.plan.json"
        plan_path.write_text(VERIFIED.model_dump_json())
        real_run = cli_module.run_monte_carlo
        calls: dict[str, Any] = {}

        def fake_run(plan, **kwargs):
            calls.clear()
            calls.update(kwargs)
            return real_run(plan, runs=1, seed=0)

        monkeypatch.setattr(cli_module, "run_monte_carlo", fake_run)
        assert main(["montecarlo", str(plan_path)]) == EXIT_OK
        assert (tmp_path / "out" / "demo.mc.json").exists()
        assert {k: v for k, v in calls.items() if k != "planner"} == {
            "runs": 50, "failure_rate_per_robot_h": 0.002, "duration_jitter": 0.15,
            "seed": None, "max_replans": 2, "dt_h": 0.25}
        planner = calls["planner"]
        assert isinstance(planner, MonteCarloPlanner)
        assert isinstance(planner.client, SolverOnlyClient) and planner.explain is False
        assert "replans via solver-only" in capsys.readouterr().err

        code = main(["montecarlo", str(plan_path), "--llm", "fixture", "--max-repairs", "1",
                     "--time-limit", "3", "--dt", "0.5", "--seed", "4", "--runs", "2"])
        assert code == EXIT_OK
        planner = calls["planner"]
        assert isinstance(planner, MonteCarloPlanner) and isinstance(planner.client, FixtureClient)
        assert (planner.max_repairs, planner.solver_time_limit_s, planner.explain) == (1, 3.0, False)
        assert (calls["dt_h"], calls["seed"], calls["runs"]) == (0.5, 4, 2)
        assert "seed=4, replans via fixture" in capsys.readouterr().err

    def test_montecarlo_rejects_unusable_input(self, tmp_path, capsys):
        plan_path = tmp_path / "x.plan.json"
        plan_path.write_text(PlanResult(requirements=REQ).model_dump_json())
        assert main(["montecarlo", str(plan_path)]) == EXIT_ERROR
        assert "no verified schedule" in capsys.readouterr().err
        plan_path.write_text(VERIFIED.model_dump_json())
        # out-of-range numbers are usage errors (mirroring the API bounds), not runtime ones
        for argv in (["--runs", "0"], ["--jitter", "2"], ["--failure-rate", "inf"],
                     ["--failure-rate", "nan"], ["--max-replans", "99"]):
            with pytest.raises(SystemExit) as info:
                main(["montecarlo", str(plan_path), *argv])
            assert info.value.code == 2, argv
        capsys.readouterr()
        assert main(["montecarlo", str(tmp_path / "nope.plan.json")]) == EXIT_ERROR
        assert "error:" in capsys.readouterr().err

    def test_simulate_and_montecarlo_reverify_the_plan_file(self, tmp_path, capsys):
        """An edited plan whose entries overlap on one robot is refused like the API does."""
        overlapping = SCHEDULE.model_copy(update={"entries": [
            SCHEDULE.entries[0], SCHEDULE.entries[1].model_copy(update={"start_h": 3, "end_h": 15})]})
        plan_path = tmp_path / "edited.plan.json"
        plan_path.write_text(VERIFIED.model_copy(update={"schedule": overlapping}).model_dump_json())
        assert main(["simulate", str(plan_path), "--llm", "fixture", "--fail", "EX1@3"]) == EXIT_ERROR
        err = capsys.readouterr().err
        assert "fails re-verification" in err and "robot:EX1" in err and "precedence:T1->T2" in err
        assert main(["montecarlo", str(plan_path), "--runs", "2", "--seed", "1"]) == EXIT_ERROR
        err = capsys.readouterr().err
        assert "fails re-verification" in err and "makespan" in err

    def test_eval_offline_without_fixtures(self, tmp_path, capsys):
        briefs = tmp_path / "briefs"
        briefs.mkdir()
        (briefs / "zz_one.md").write_text("# One\nBuild a pad.")
        (briefs / "zz_two.md").write_text("# Two\nBuild a road.")
        out_json, out_csv = tmp_path / "eval.json", tmp_path / "eval.csv"
        code = main(["eval", "--briefs", str(briefs), "--llm", "fixture", "--repeat", "2",
                     "--out", str(out_json), "--csv", str(out_csv)])
        assert code == EXIT_OK
        report = json.loads(out_json.read_text())
        assert [s["condition"] for s in report["summary"]] == ["loop_off", "loop_on"]
        assert len(report["rows"]) == 2 * 2 * 2
        assert {r["success"] for r in report["rows"]} == {False}
        csv_lines = out_csv.read_text().splitlines()
        assert len(csv_lines) == 9 and csv_lines[0].startswith("brief,condition,repeat,success")
        table = capsys.readouterr().out
        assert "loop_off" in table and "loop_on" in table and "feasible" in table

    def test_eval_empty_dir(self, tmp_path, capsys):
        assert main(["eval", "--briefs", str(tmp_path), "--llm", "fixture"]) == EXIT_ERROR
        assert "no briefs" in capsys.readouterr().err


# --------------------------------------------------------------------------- end to end
def test_cli_plan_fixture_end_to_end(tmp_path, capsys):
    """Full offline run on the landing_pad example: needs the validator, the scheduler and the
    fixtures from the other builders; skipped until they land."""
    brief = EXAMPLES_DIR / "landing_pad.md"
    if not brief.exists() or not (FIXTURES_DIR / "landing_pad" / "structure.json").exists():
        pytest.skip("example brief / fixtures for landing_pad not available yet")
    out = tmp_path / "landing_pad.plan.json"
    try:
        code = main(["plan", str(brief), "--llm", "fixture", "--out", str(out),
                     "--time-limit", "5"])
    except NotImplementedError as e:  # validate.py / schedule.py still stubs
        pytest.skip(f"pipeline stage not implemented yet: {e}")
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.out
    result = PlanResult.model_validate_json(out.read_text())
    assert result.success and result.schedule is not None
    assert "makespan" in captured.out and "[T" in captured.out


# --------------------------------------------------------------------------- paired eval
class TestEvalPairing:
    def test_every_condition_plans_from_one_structuring(self, tmp_path, capsys, monkeypatch):
        """The structuring model is asked once per (brief, repeat); every condition plans from
        that requirements object and carries its sha, so arms and loop settings are paired."""
        structured: list[str] = []
        planned: list[str] = []

        class Paired(RecordingPlanner):
            def structure(self, brief, *, brief_id=None):
                structured.append(brief_id)
                req = Requirements.model_validate_json(
                    (FIXTURES_DIR / "landing_pad" / "structure.json").read_text())
                req.source_brief = brief
                return req, LoopStep(stage="structure", ok=True, message="scripted")

            def plan_from_requirements(self, req, *, key, **kwargs):
                planned.append(key)
                verdict = Failure(kind="infeasible", conflicts=["energy:EX1"])
                return PlanResult(requirements=req, success=False, failure=verdict,
                                  steps=[LoopStep(stage="validate", ok=True),
                                         LoopStep(stage="schedule", ok=False, failure=verdict)])

        Paired.created = []
        monkeypatch.setattr(cli_module, "Planner", Paired)
        briefs = briefs_dir(tmp_path, 2)
        (briefs / "kinds.json").write_text(json.dumps({"zz_brief_0": "A"}))
        out_json = tmp_path / "e.json"
        code = main(["eval", "--briefs", str(briefs), "--llm", "fixture", "--loop", "both",
                     "--repeat", "2", "--out", str(out_json), "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_OK
        assert sorted(structured) == ["zz_brief_0", "zz_brief_0", "zz_brief_1", "zz_brief_1"]
        assert len(planned) == 8 and set(planned) == {"zz_brief_0", "zz_brief_1"}
        report = json.loads(out_json.read_text())
        rows = report["rows"]
        assert [(r["brief"], r["condition"], r["repeat"]) for r in rows] == [
            (b, c, k) for b in ("zz_brief_0", "zz_brief_1") for c in ("loop_off", "loop_on")
            for k in (1, 2)]
        shas = {r["requirements_sha"] for r in rows}
        assert len(shas) == 1 and all(len(s) == 10 for s in shas)   # one structure.json
        assert [r["kind"] for r in rows] == ["A"] * 4 + [""] * 4
        assert all(r["in_scope"] is False and r["volume_advisories"] == 0 for r in rows)
        assert all(r["failure_kind"] == "infeasible" for r in rows)
        assert report["summary"][0]["in_scope"] == 0
        assert "kinds=1" in capsys.readouterr().err

    def test_kinds_file_must_be_an_object_of_strings(self, tmp_path, capsys, planner_stub):
        stub = planner_stub(RecordingPlanner)
        bad = tmp_path / "kinds.json"
        bad.write_text("[1, 2]")
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path, 1)), "--llm", "fixture",
                     "--kinds", str(bad), "--out", str(tmp_path / "e.json"),
                     "--csv", str(tmp_path / "e.csv")])
        assert code == EXIT_ERROR and stub.created == []
        assert "expected a JSON object" in capsys.readouterr().err
        assert cli_module.load_kinds(None, tmp_path / "nowhere") == {}
        with pytest.raises(ValueError, match="--kinds"):
            cli_module.load_kinds(tmp_path / "missing.json", tmp_path)


# --------------------------------------------------------------------------- eval save / resume
def scripted_result(req: Requirements, *, loop_on: bool, rejected: int) -> PlanResult:
    """A deterministic run: loop off ends on an energy verdict with a verified suggestion,
    loop on is verified after one repair. ``rejected`` answers are cut off before the valid
    one (loop off: schema misses of the first graph, loop on: transport errors of the repair)."""
    verdict = Failure(kind="infeasible", conflicts=["energy:EX1"],
                      hint="EX1 needs 9,000 Wh but has 5,000 Wh.")
    cut = Failure(kind="llm_error" if loop_on else "schema",
                  conflicts=[] if loop_on else ["schema:json"], hint="cut off at max_tokens")
    first = [usage_step("taskgraph", ULTRA_ID, attempt=i + 1, ok=False, failure=cut)
             for i in range(0 if loop_on else rejected)]
    first.append(usage_step("taskgraph", ULTRA_ID, attempt=len(first) + 1))
    steps = [*first, LoopStep(stage="validate", ok=True),
             LoopStep(stage="schedule", ok=False, failure=verdict,
                      payload={"budget": [{"kind": "robot", "subject": "EX1"}]})]
    if not loop_on:
        return PlanResult(
            requirements=req, task_graph=FIRST_GRAPH, success=False, failure=verdict, steps=steps,
            suggestions=[Suggestion(kind="raise_battery", robot_id="EX1", battery_wh=9000,
                                    makespan_h=20, verified=True,
                                    detail="EX1 needs 9,000 Wh (has 5,000 Wh): feasible at 20 h.")])
    repairs = [usage_step("repair", ULTRA_ID, attempt=2, ok=False, failure=cut)
               for _ in range(rejected)]
    repairs.append(usage_step("repair", ULTRA_ID, attempt=2))
    steps += [*repairs, LoopStep(stage="validate", attempt=2, ok=True),
              LoopStep(stage="schedule", attempt=2, ok=True, message="makespan 18h (optimal)")]
    return PlanResult(requirements=req, task_graph=FINAL_GRAPH, schedule=SCHEDULE, success=True,
                      repairs=1, steps=steps)


class ScriptedPlanner(RecordingPlanner):
    """A Planner whose ``structure`` answers with REQ (a warning in the step's payload) and
    whose ``plan_from_requirements`` is :func:`scripted_result` for the loop setting it was
    built with; the first brief's answers are rejected before they are valid (once with the
    loop off, twice with it on), the others never."""

    def __init__(self, client, **kwargs):
        super().__init__(client, **kwargs)
        self.max_repairs = kwargs.get("max_repairs", 3)

    def structure(self, brief, *, brief_id=None):
        type(self).calls += 1
        req = REQ.model_copy(update={"source_brief": brief})
        return req, LoopStep(
            stage="structure", ok=True, message="scripted",
            payload={"warnings": ["scripted warning"]},
            usage=Usage(model=ARM_MODELS["lightning"], input_tokens=10, output_tokens=5,
                        latency_s=0.5))

    def plan_from_requirements(self, req, *, key, **kwargs):
        loop_on = self.max_repairs > 0
        rejected = (2 if loop_on else 1) if key == "zz_brief_0" else 0
        return scripted_result(req, loop_on=loop_on, rejected=rejected)


class TestEvalSaveResume:
    def test_file_names_are_safe(self):
        assert plan_file_name("landing_pad", "ultra/loop_on", 2) == "landing_pad__ultra__loop_on__2.plan.json"
        assert plan_file_name("pad", "loop_off", 1) == "pad__loop_off__1.plan.json"
        assert plan_file_name("my brief/v2", "x=y/loop_on", 3) == "my_brief__v2__x_y__loop_on__3.plan.json"
        assert structure_file_name("landing_pad", 2) == "landing_pad__2.structure.json"
        assert cli_module.file_safe("a/b c:d") == "a__b_c_d"

    def test_eval_row_counts_rejected_answers_and_keeps_the_plan_path(self):
        cut = Failure(kind="schema", conflicts=["schema:json"], hint="cut off at max_tokens")
        down = Failure(kind="llm_error", hint="503")
        verdict = Failure(kind="infeasible", conflicts=["energy:EX1"])
        steps = [
            usage_step("structure", "nvidia/lightning"),
            usage_step("taskgraph", ULTRA_ID, ok=False, failure=cut),
            usage_step("taskgraph", ULTRA_ID, attempt=2),
            LoopStep(stage="validate", attempt=2, ok=False,          # a verdict, not a rejection
                     failure=Failure(kind="validation", conflicts=["scope:pad"])),
            usage_step("repair", ULTRA_ID, attempt=3, ok=False, failure=down),
            usage_step("repair", ULTRA_ID, attempt=3, ok=False, failure=cut),
            usage_step("repair", ULTRA_ID, attempt=3),
            LoopStep(stage="validate", attempt=3, ok=True),
            LoopStep(stage="schedule", attempt=3, ok=False, failure=verdict),
        ]
        result = PlanResult(requirements=REQ, task_graph=FIRST_GRAPH, success=False,
                            failure=verdict, repairs=1, steps=steps)
        row = eval_row("demo", "ultra/loop_on", 1, result, 3.0, first_graph=FIRST_GRAPH,
                       plan_path="demo__ultra__loop_on__1.plan.json")
        assert row.rejected_answers == 3 and row.answer_valid is False
        assert row.plan_path == "demo__ultra__loop_on__1.plan.json"
        plain = eval_row("demo", "loop_off", 1, FAILED_RESULT, 0.5)   # a failed structuring
        assert plain.rejected_answers == 0 and plain.plan_path == ""
        record = cli_module.csv_record(row)
        assert list(record)[-3:] == ["rejected_answers", "min_area_share", "plan_path"]

    def test_summary_rejected_runs_p50_cost_and_the_markdown_columns(self):
        def row(condition: str, cost: float | None, rejected: int) -> EvalRow:
            return EvalRow(brief="b", condition=condition, repeat=1, success=True, repairs=0,
                           makespan_h=10, latency_s=1.0, llm_latency_s=0.5, input_tokens=1,
                           output_tokens=1, cost_usd=cost, rejected_answers=rejected)

        rows = [row("ultra/loop_on", 0.01, 0), row("ultra/loop_on", 0.02, 1),
                row("ultra/loop_on", 0.09, 3), row("super/loop_on", 0.05, 1),
                row("super/loop_on", None, 1)]
        ultra, sup = summarise(rows)
        assert (ultra.rejected_runs, ultra.rejected_answers) == (2, 4)
        assert (ultra.mean_cost_usd, ultra.cost_p50_usd) == (0.04, 0.02)   # the median, not the mean
        assert (sup.rejected_runs, sup.rejected_answers, sup.cost_p50_usd) == (2, 2, 0.05)
        assert summarise([row("c", None, 0)])[0].cost_p50_usd is None
        table = cli_module.condition_table([ultra, sup])
        assert table.splitlines()[0].endswith(
            "| cost / run | cost total | rejected answers | p50 cost |")
        assert "| $0.0400 | $0.12 | 2/3 (4 answers) | $0.0200 |" in table   # re-asks exceed the runs
        assert "| $0.0500 | $0.05 | 2/2 | $0.0500 |" in table
        report = EvalReport(
            generated_at="2026-10-25T00:00:00+00:00", llm="nebius", briefs_dir="examples/eval",
            conditions=["ultra/loop_on", "super/loop_on"], repeat=1, summary=[ultra, sup],
            rows=rows, plans_dir="out/live", resumed_rows=2)
        text = eval_markdown(report)
        assert "\nresumed: 2 rows from a previous run\n" in text
        assert "`rejected answers` is the share of runs" in text
        assert "`p50 cost` the median cost of a priced run" in text
        assert "kept under `out/live`" in text and "`plan_path` in the rows" in text
        assert "2 rows come from an earlier run's CSV" in text
        plain = eval_markdown(report.model_copy(update={"plans_dir": None, "resumed_rows": 0}))
        assert "resumed:" not in plain and "kept under" not in plain and "earlier run" not in plain
        # the text table on stdout / stderr keeps its columns
        assert format_summary([ultra]).splitlines()[0].split()[-1] == "cost/run"

    def test_csv_rows_round_trip(self, tmp_path):
        rich = EvalRow(
            brief="pad", condition="ultra/loop_on", repeat=3, success=True, repairs=1,
            makespan_h=76, latency_s=40.25, llm_latency_s=38.0, input_tokens=5300,
            output_tokens=11000, models="a,b", answer_valid=True, first_try_feasible=False,
            first_duration_h=20, first_energy_wh=8000.0, final_duration_h=16,
            final_energy_wh=6100.5, scope_drift_pct=-23.75, thinking="on", cost_usd=0.036,
            volume_advisories=1, min_volume_share=0.36, in_scope=False,
            requirements_sha="74e00e4b2a", kind="E", rejected_answers=2,
            plan_path="pad__ultra__loop_on__3.plan.json")
        failed = EvalRow(brief="pad", condition="loop_off", repeat=1, success=False, repairs=0,
                         latency_s=1.0, llm_latency_s=0.5, input_tokens=1, output_tokens=0,
                         failure_kind="llm_error", conflicts=["schema:json", "energy:EX1"],
                         cost_usd=None, in_scope=None)
        for row in (rich, failed):
            assert csv_row(cli_module.csv_record(row)) == row
        cli_module.write_csv(tmp_path / "r.csv", [rich, failed])
        header, rows = read_csv(tmp_path / "r.csv")
        assert tuple(header) == cli_module.CSV_FIELDS and rows == [rich, failed]
        # a record from before a column existed, with a column this build does not know
        minimal = {"brief": "b", "condition": "c", "repeat": "1", "success": "True",
                   "repairs": "0", "latency_s": "1", "llm_latency_s": "0", "input_tokens": "0",
                   "output_tokens": "0", "novel": "x"}
        assert csv_row(minimal) == EvalRow(brief="b", condition="c", repeat=1, success=True,
                                           repairs=0, latency_s=1, llm_latency_s=0,
                                           input_tokens=0, output_tokens=0)
        with pytest.raises(ValueError, match="repeat"):
            csv_row({**minimal, "repeat": "x"})
        bad = tmp_path / "bad.csv"
        bad.write_text(",".join(minimal) + "\nb,c,x,True,0,1,0,0,0,x\n", encoding="utf-8")
        with pytest.raises(ValueError, match="bad.csv line 2"):
            read_csv(bad)
        # an older run's CSV: the header is upgraded and the rows re-serialised
        with bad.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(minimal)[:-1])
            writer.writeheader()
            writer.writerow({k: v for k, v in minimal.items() if k != "novel"})
            writer.writerow({**{k: v for k, v in minimal.items() if k != "novel"}, "repairs": "2"})
        rows = cli_module.resume_csv(bad)
        assert [r.repairs for r in rows] == [2]                # the last row of a key wins
        assert bad.read_text().splitlines()[0] == ",".join(cli_module.CSV_FIELDS)
        assert read_csv(bad)[1] == rows

    def test_save_plans_keeps_every_plan_and_structure(self, tmp_path, capsys, planner_stub):
        planner_stub(ScriptedPlanner)
        plans = tmp_path / "plans"
        out_json, out_csv, out_md = tmp_path / "e.json", tmp_path / "e.csv", tmp_path / "e.md"
        briefs = briefs_dir(tmp_path, 2)
        code = main(["eval", "--briefs", str(briefs), "--llm", "fixture", "--models",
                     "ultra,super", "--loop", "both", "--repeat", "2", "--save-plans", str(plans),
                     "--out", str(out_json), "--csv", str(out_csv), "--markdown", str(out_md)])
        assert code == EXIT_OK
        ids = ("zz_brief_0", "zz_brief_1")
        conditions = ("ultra/loop_off", "ultra/loop_on", "super/loop_off", "super/loop_on")
        plan_files = {plan_file_name(b, c, k) for b in ids for c in conditions for k in (1, 2)}
        structure_files = {structure_file_name(b, k) for b in ids for k in (1, 2)}
        assert {p.name for p in plans.iterdir()} == plan_files | structure_files
        assert "zz_brief_0__ultra__loop_on__2.plan.json" in plan_files   # "/" became "__"
        # a plan file is the PlanResult the run produced, steps with payloads and suggestions
        # included (what POST /plan returns): the stub's own result with the structure step first
        off = PlanResult.model_validate_json(
            (plans / "zz_brief_0__ultra__loop_off__1.plan.json").read_text())
        assert off.success is False and off.failure is not None
        assert off.failure.conflicts == ["energy:EX1"] and off.repairs == 0
        assert off.steps[0].stage == "structure"
        assert off.steps[0].payload == {"warnings": ["scripted warning"]}
        assert off.steps[-1].payload == {"budget": [{"kind": "robot", "subject": "EX1"}]}
        assert [s.kind for s in off.suggestions] == ["raise_battery"] and off.suggestions[0].verified
        assert [s.stage for s in off.steps] == ["structure", "taskgraph", "taskgraph", "validate",
                                                 "schedule"]
        on = PlanResult.model_validate_json(
            (plans / "zz_brief_1__super__loop_on__2.plan.json").read_text())
        planner = ScriptedPlanner(None, max_repairs=3)
        req, step = planner.structure((briefs / "zz_brief_1.md").read_text(), brief_id="zz_brief_1")
        expected = planner.plan_from_requirements(req, key="zz_brief_1")
        expected.steps.insert(0, step)
        assert on.model_dump() == expected.model_dump()
        assert on.success and on.repairs == 1 and on.task_graph == FINAL_GRAPH
        # the structuring of a (brief, repeat) job: requirements, the step's warnings, usage, time
        record = json.loads((plans / "zz_brief_0__2.structure.json").read_text())
        assert (record["brief"], record["repeat"], record["ok"]) == ("zz_brief_0", 2, True)
        assert record["payload"] == {"warnings": ["scripted warning"]}
        assert record["usage"] == {"model": ARM_MODELS["lightning"], "input_tokens": 10,
                                   "output_tokens": 5, "latency_s": 0.5}
        assert record["requirements"]["horizon_h"] == 100
        assert record["requirements"]["source_brief"].startswith("# Brief 0")
        assert record["failure"] is None and record["latency_s"] >= 0.0
        report = json.loads(out_json.read_text())
        assert {r["requirements_sha"] for r in report["rows"]} == {record["requirements_sha"]}
        # every row names its plan file, relative to the CSV's directory, in the last column
        assert report["plans_dir"] == str(plans)
        assert all(r["plan_path"] == "plans/" + plan_file_name(r["brief"], r["condition"], r["repeat"])
                   and (out_csv.parent / r["plan_path"]).is_file() for r in report["rows"])
        with out_csv.open(encoding="utf-8") as fh:
            csv_rows = list(csv.DictReader(fh))
        assert list(csv_rows[0])[-2:] == ["min_area_share", "plan_path"]
        assert {r["plan_path"] for r in csv_rows} == {f"plans/{name}" for name in plan_files}
        # rejected answers: the first brief's loop-off graph was cut once, its repair twice
        rows = {(r["brief"], r["condition"], r["repeat"]): r for r in report["rows"]}
        assert rows["zz_brief_0", "ultra/loop_off", 1]["rejected_answers"] == 1
        assert rows["zz_brief_0", "super/loop_on", 2]["rejected_answers"] == 2
        assert rows["zz_brief_1", "ultra/loop_on", 1]["rejected_answers"] == 0
        summary = {s["condition"]: s for s in report["summary"]}
        assert (summary["ultra/loop_off"]["rejected_runs"],
                summary["ultra/loop_off"]["rejected_answers"]) == (2, 2)
        assert (summary["ultra/loop_on"]["rejected_runs"],
                summary["ultra/loop_on"]["rejected_answers"]) == (2, 4)
        assert summary["ultra/loop_on"]["cost_p50_usd"] > 0.0
        text = out_md.read_text()
        assert "| rejected answers | p50 cost |" in text
        assert "| 2/4 | " + format_usd(summary["ultra/loop_off"]["cost_p50_usd"]) + " |" in text
        assert "| 2/4 (4 answers) | " + format_usd(summary["ultra/loop_on"]["cost_p50_usd"]) + " |" in text
        assert f"kept under `{plans}`" in text and "resumed:" not in text
        err = capsys.readouterr().err
        assert f"save_plans={plans}" in err
        assert "1 rejected answer," not in err and err.count(", 1 rejected answer") == 4
        assert err.count(", 2 rejected answers") == 4

    def test_resume_skips_complete_jobs_appends_rows_and_summarises_all(
            self, tmp_path, capsys, planner_stub):
        stub = planner_stub(ScriptedPlanner)
        briefs = briefs_dir(tmp_path, 2)
        out_json, out_csv, out_md = tmp_path / "e.json", tmp_path / "e.csv", tmp_path / "e.md"
        argv = ["eval", "--briefs", str(briefs), "--llm", "fixture", "--loop", "both",
                "--out", str(out_json), "--csv", str(out_csv), "--markdown", str(out_md)]
        assert main(argv) == EXIT_OK
        first = out_csv.read_text().splitlines()
        assert stub.calls == 2 and len(first) == 1 + 4
        assert "resume" not in capsys.readouterr().err
        # a third brief arrives: --resume structures and plans only it
        (briefs / "zz_brief_2.md").write_text("# Brief 2\n**Site.** Build pad 2.")
        stub.calls, stub.created = 0, []
        assert main([*argv, "--resume"]) == EXIT_OK
        err = capsys.readouterr().err
        assert "resume: 4 rows from a previous run" in err
        assert "2 of 3 job(s) already complete, skipped" in err
        assert stub.calls == 1 and len(stub.created) == 2
        lines = out_csv.read_text().splitlines()
        assert len(lines) == 1 + 6 and lines[:5] == first          # old rows kept verbatim
        assert sum(line.startswith("brief,condition,") for line in lines) == 1   # one header
        assert [line.split(",")[0] for line in lines[5:]] == ["zz_brief_2", "zz_brief_2"]
        report = json.loads(out_json.read_text())
        assert report["resumed_rows"] == 4 and len(report["rows"]) == 6
        assert [(r["brief"], r["condition"]) for r in report["rows"]] == [
            (b, c) for b in ("zz_brief_0", "zz_brief_1", "zz_brief_2")
            for c in ("loop_off", "loop_on")]
        assert [s["runs"] for s in report["summary"]] == [3, 3]
        assert [s["rejected_runs"] for s in report["summary"]] == [1, 1]
        text = out_md.read_text()
        assert "\nresumed: 4 rows from a previous run\n" in text and "6 run(s)" in text
        assert "4 rows come from an earlier run's CSV" in text
        # --resume again with nothing to do: no job runs, the report covers the six rows
        stub.calls = 0
        assert main([*argv, "--resume"]) == EXIT_OK
        assert "3 of 3 job(s) already complete" in capsys.readouterr().err
        assert stub.calls == 0 and len(out_csv.read_text().splitlines()) == 1 + 6
        report = json.loads(out_json.read_text())
        assert report["resumed_rows"] == 6 and len(report["rows"]) == 6
        # without --resume the CSV is overwritten, as before
        stub.calls = 0
        assert main(argv) == EXIT_OK
        assert stub.calls == 3 and len(out_csv.read_text().splitlines()) == 1 + 6
        assert json.loads(out_json.read_text())["resumed_rows"] == 0
        assert "resumed:" not in out_md.read_text()

    def test_resume_runs_only_the_missing_conditions_of_a_partial_job(
            self, tmp_path, capsys, planner_stub):
        stub = planner_stub(ScriptedPlanner)
        out_json, out_csv = tmp_path / "e.json", tmp_path / "e.csv"
        common = ["eval", "--briefs", str(briefs_dir(tmp_path, 2)), "--llm", "fixture",
                  "--out", str(out_json), "--csv", str(out_csv)]
        assert main([*common, "--loop", "off"]) == EXIT_OK
        stub.calls, stub.created = 0, []
        assert main([*common, "--loop", "both", "--resume"]) == EXIT_OK
        err = capsys.readouterr().err
        assert "2 rows from a previous run" in err and "0 of 2 job(s) already complete" in err
        assert "2 partial jobs: only the missing conditions run" in err
        assert stub.calls == 2 and [c["max_repairs"] for c in stub.created] == [3, 3]
        lines = out_csv.read_text().splitlines()
        assert len(lines) == 1 + 4
        assert [line.split(",")[1] for line in lines[1:]] == ["loop_off"] * 2 + ["loop_on"] * 2
        report = json.loads(out_json.read_text())
        assert report["resumed_rows"] == 2 and [s["runs"] for s in report["summary"]] == [2, 2]
        assert [(r["brief"], r["condition"]) for r in report["rows"]] == [
            (b, c) for b in ("zz_brief_0", "zz_brief_1") for c in ("loop_off", "loop_on")]

    def test_resume_upgrades_the_header_of_an_older_csv(self, tmp_path, capsys, planner_stub):
        stub = planner_stub(ScriptedPlanner)
        out_json, out_csv = tmp_path / "e.json", tmp_path / "e.csv"
        old_fields = [f for f in cli_module.CSV_FIELDS
                      if f not in ("rejected_answers", "plan_path")]
        old = EvalRow(brief="zz_brief_0", condition="loop_off", repeat=1, success=False,
                      repairs=0, latency_s=1.5, llm_latency_s=1.0, input_tokens=10,
                      output_tokens=2, failure_kind="infeasible",
                      conflicts=["energy:EX1", "horizon"], thinking="off", cost_usd=0.01,
                      in_scope=False, requirements_sha="abc", kind="D")
        with out_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=old_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(cli_module.csv_record(old))
        code = main(["eval", "--briefs", str(briefs_dir(tmp_path, 2)), "--llm", "fixture",
                     "--loop", "off", "--resume", "--out", str(out_json), "--csv", str(out_csv)])
        assert code == EXIT_OK and stub.calls == 1
        err = capsys.readouterr().err
        assert "resume: 1 row from a previous run" in err and "1 of 2 job(s) already complete" in err
        lines = out_csv.read_text().splitlines()
        assert lines[0] == ",".join(cli_module.CSV_FIELDS) and len(lines) == 3
        with out_csv.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert [r["brief"] for r in rows] == ["zz_brief_0", "zz_brief_1"]
        assert (rows[0]["rejected_answers"], rows[0]["plan_path"]) == ("0", "")
        assert rows[0]["conflicts"] == "energy:EX1;horizon" and rows[0]["latency_s"] == "1.5"
        report = json.loads(out_json.read_text())
        assert report["rows"][0]["conflicts"] == ["energy:EX1", "horizon"]
        assert (report["rows"][0]["kind"], report["rows"][0]["thinking"]) == ("D", "off")
        assert report["resumed_rows"] == 1

    def test_resume_with_a_broken_csv_is_an_input_error(self, tmp_path, capsys, planner_stub):
        stub = planner_stub(ScriptedPlanner)
        out_json, out_csv = tmp_path / "e.json", tmp_path / "e.csv"
        out_csv.write_text("brief,condition,repeat,success,repairs,latency_s,llm_latency_s,"
                           "input_tokens,output_tokens\nzz_brief_0,loop_off,one,True,0,1,0,0,0\n")
        argv = ["eval", "--briefs", str(briefs_dir(tmp_path, 1)), "--llm", "fixture", "--resume",
                "--out", str(out_json), "--csv", str(out_csv)]
        assert main(argv) == EXIT_ERROR and stub.calls == 0
        assert "e.csv line 2" in capsys.readouterr().err
        assert not out_json.exists()
        # --resume without a CSV to resume from is a plain run
        out_csv.unlink()
        assert main(argv) == EXIT_OK
        assert "resume: no earlier rows" in capsys.readouterr().err
        assert len(out_csv.read_text().splitlines()) == 1 + 2 and stub.calls == 1

    def test_resume_drops_a_row_cut_off_while_it_was_appended(self, tmp_path, capsys, planner_stub):
        """Review: a run killed mid-append leaves a last line without its newline. It parsed
        as a finished run (missing columns at their defaults: success True, in_scope None),
        its job was skipped and the line stayed in the CSV for good, moving the summary. It
        is left out now, cut from the file, and its job runs again."""
        stub = planner_stub(ScriptedPlanner)
        out_json, out_csv = tmp_path / "e.json", tmp_path / "e.csv"
        argv = ["eval", "--briefs", str(briefs_dir(tmp_path, 2)), "--llm", "fixture", "--loop",
                "both", "--out", str(out_json), "--csv", str(out_csv)]
        assert main(argv) == EXIT_OK
        full = out_csv.read_text(encoding="utf-8")
        summary = json.loads(out_json.read_text())["summary"]
        lines = full.splitlines()
        # killed while writing the last row: ten of its columns made it to the file
        out_csv.write_text("\n".join(lines[:4]) + "\n" + ",".join(lines[4].split(",")[:10]),
                           encoding="utf-8")
        assert [r.brief for r in read_csv(out_csv)[1]] == ["zz_brief_0"] * 2 + ["zz_brief_1"]
        stub.calls = 0
        assert main([*argv, "--resume"]) == EXIT_OK
        err = capsys.readouterr().err
        assert "resume: 3 rows from a previous run" in err and "1 partial job" in err
        assert stub.calls == 1
        rows = read_csv(out_csv)[1]
        assert len(rows) == 4 and len(out_csv.read_text().splitlines()) == 1 + 4
        def counts(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
            return [(s["condition"], s["runs"], s["feasible"], s["in_scope"],
                     s["first_try_feasible"], s["mean_repairs"]) for s in rows]

        assert counts(json.loads(out_json.read_text())["summary"]) == counts(summary)
        # a cut-off header-only file is no row at all
        out_csv.write_text("brief,condition,rep", encoding="utf-8")
        assert read_csv(out_csv) == (["brief", "condition", "rep"], [])

    def test_resumed_rows_keep_naming_their_own_plan_files(self, tmp_path, capsys, planner_stub):
        """Review: after ``--resume --save-plans DIR2`` the old rows' plan_path only existed
        under the first DIR, while the report named DIR2; and resuming a partial job into the
        same DIR rewrote its structure record, losing the requirements the earlier conditions
        planned from. plan_path is relative to the CSV now, and the second structuring of a
        job gets its own file."""
        planner_stub(ScriptedPlanner)
        out_json, out_csv = tmp_path / "e.json", tmp_path / "e.csv"
        common = ["eval", "--briefs", str(briefs_dir(tmp_path, 1)), "--llm", "fixture",
                  "--out", str(out_json), "--csv", str(out_csv)]
        assert main([*common, "--loop", "off", "--save-plans", str(tmp_path / "a")]) == EXIT_OK
        assert main([*common, "--loop", "both", "--resume", "--save-plans", str(tmp_path / "a")]) == EXIT_OK
        assert sorted(p.name for p in (tmp_path / "a").glob("*.structure.json")) == [
            "zz_brief_0__1.2.structure.json", "zz_brief_0__1.structure.json"]
        assert main([*common, "--loop", "both", "--repeat", "2", "--resume",
                     "--save-plans", str(tmp_path / "b")]) == EXIT_OK
        report = json.loads(out_json.read_text())
        paths = {(r["condition"], r["repeat"]): r["plan_path"] for r in report["rows"]}
        assert paths == {("loop_off", 1): "a/zz_brief_0__loop_off__1.plan.json",
                         ("loop_on", 1): "a/zz_brief_0__loop_on__1.plan.json",
                         ("loop_off", 2): "b/zz_brief_0__loop_off__2.plan.json",
                         ("loop_on", 2): "b/zz_brief_0__loop_on__2.plan.json"}
        assert all((out_csv.parent / p).is_file() for p in paths.values())
        assert report["plans_dir"] == str(tmp_path / "b")
        text = eval_markdown(EvalReport.model_validate(report))
        assert "relative to the CSV's directory" in text


def test_a_verified_row_out_of_scope_says_how_far_off(capsys):
    """The progress line of a verified run that is not in scope names its smallest shares
    (under the 95 % the eval's in-scope rate needs), not an advisory count that an in-range
    trim would not even have."""
    row = EvalRow(brief="small_fleet_long_berm", condition="loop_on", repeat=1, success=True,
                  repairs=1, makespan_h=64, latency_s=1.0, llm_latency_s=0.5, input_tokens=1,
                  output_tokens=1, min_volume_share=0.85, min_area_share=0.999, in_scope=False)
    cli_module.report_row(row)
    assert "makespan 64h (not in scope: volume 85 %), 1 repair(s)" in capsys.readouterr().err


def test_the_eval_banner_names_the_live_clients_token_budgets():
    """Review: an old backend/.env pins 16,384 tokens and nothing showed the budget a run
    actually used. The eval prints the live client's budgets, found through its wrappers."""
    from types import SimpleNamespace

    live = SimpleNamespace(name="nebius", budgets="reasoning 16,384 tokens (x2 when cut, up to "
                           "65,536), direct 4,096")
    wrapped = cli_module.NarratedClient(cli_module.GraphCapture(live))
    assert cli_module.token_budgets(wrapped) == live.budgets
    assert cli_module.token_budgets(FixtureClient()) is None
