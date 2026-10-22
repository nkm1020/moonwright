"""Tests for the user-facing entry points: live client (mocked transport), factory, CLI, API.

No network: the Token Factory client is exercised through an ``httpx.MockTransport`` injected
as the SDK's http client. The end-to-end ``plan`` test needs the validator / scheduler modules
and the example fixtures from the other builders and skips until they land.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
    thin_frames,
)
from moonwright.cli import (
    EXIT_ERROR,
    EXIT_NO_PLAN,
    EXIT_OK,
    EvalRow,
    build_parser,
    format_gantt,
    format_step,
    main,
    parse_failure,
    plan_stem,
    positive_float,
    summarise,
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
from moonwright.llm.tokenfactory import (
    DEFAULT_BASE_URL,
    MODEL_DEFAULTS,
    RecordingClient,
    TokenFactoryClient,
)
from moonwright.pipeline.loop import Planner
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
    Task,
    TaskGraph,
)
from moonwright.sim.montecarlo import MonteCarloPlanner, MonteCarloResult, SolverOnlyClient
from moonwright.sim.simulator import FailureInjection

Reply = str | Callable[[httpx.Request], httpx.Response]


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
        assert taskgraph["max_tokens"] == 16384
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

    def test_examples(self, api):
        r = api.get("/examples")
        assert r.status_code == 200
        items = r.json()
        assert items == [b.model_dump() for b in list_briefs()]
        assert all(set(i) == {"id", "title", "text"} for i in items)
        if (EXAMPLES_DIR / "landing_pad.md").exists():
            assert "landing_pad" in [i["id"] for i in items]

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
        assert body["success"] is True and body["schedule"]["makespan_h"] == 77
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
        assert calls["planner"].explain is False
        assert (calls["runs"], calls["seed"], calls["max_replans"]) == (3, 9, 1)
        assert (calls["duration_jitter"], calls["failure_rate_per_robot_h"]) == (0, 0.5)

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
                      {"max_replans": 6}, {"max_replans": -1}, {"seed": "lucky"}):
            r = api.post("/montecarlo", json={"plan": plan, **extra})
            assert r.status_code == 422, extra
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
        assert result["success"] is True and result["schedule"]["makespan_h"] == 77
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
    (tmp_path / "b_second.md").write_text("# Second\nbody")
    (tmp_path / "a_first.txt").write_text("first body")
    (tmp_path / "ignored.json").write_text("{}")
    briefs = list_briefs(tmp_path)
    assert [b.id for b in briefs] == ["a_first", "b_second"]
    assert briefs[1].title == "Second" and briefs[1].text == "# Second\nbody"
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
                     ["eval", "--briefs", "b", "--repeat", "0"],
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
