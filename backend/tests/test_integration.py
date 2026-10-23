"""Cross-module integration tests for the offline demo flows (``FixtureClient``, no network).

* CLI ``plan`` -> ``simulate`` on the landing_pad example, the demo's own commands: the
  recorded live run plans with one repair at 89 h, and EX2 lost at 10 h is replanned by CP-SAT
  alone (the robot_failure event, before/after makespan, completion at 91 h, every schedule
  the pipeline produced re-verified with :func:`check_schedule`).
* API ``POST /plan`` -> ``POST /simulate`` with EX2 failing at 30 h, and the UI flow
  ``GET /examples/{id}`` -> ``POST /structure`` -> ``POST /plan`` (user-corrected requirements)
  -> ``POST /plan/stream`` -> ``POST /simulate`` with the API defaults.
* CLI ``eval`` over the example briefs: the repair loop turns 1/4 feasible briefs into 3/4, and
  the designed-impossible front_loaded fails in both conditions. The eval's mechanics run over
  the three hand-made fixtures' briefs (landing_pad_v1, access_road, tight_battery): with
  ``--models ultra,super`` the arms replay the same fixtures under their own condition names
  and ``--markdown`` writes the results document with the per-brief matrix.
* Monte Carlo (F9) over the landing_pad plan, CLI and API: solver-only replans, a reproducible
  seed, and a sample's failures replayable through ``POST /simulate`` unchanged.
* Offline replan repairs: the repair round carries ``variant="replan"`` and, without a canned
  answer, ends with the solver's verdict instead of a stale fixture of the original run.
* Replan triggers: a plain replay of every fixture plan raises no event at all (travel drift
  is not an overrun) and never replans, and two robot failures are both replanned within the
  default budget.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moonwright.api import EXAMPLES_DIR, app
from moonwright.cli import ARM_MODELS, EXIT_OK, main
from moonwright.llm.base import FixtureClient, FixtureMissing
from moonwright.pipeline.loop import Planner
from moonwright.pipeline.schedule import check_schedule
from moonwright.schemas import (
    PlanResult,
    Requirements,
    Robot,
    SimResult,
    Task,
    TaskGraph,
)
from moonwright.sim.montecarlo import MonteCarloResult
from moonwright.sim.replan import replan_offset_h, run_with_replanning, shift_schedule
from moonwright.sim.simulator import FailureInjection
from tests.test_cli_api import live_steps, stream_plan  # noqa: F401  (fixture + SSE reader)

BRIEF = EXAMPLES_DIR / "landing_pad.md"
V1_BRIEF = Path(__file__).resolve().parent / "data" / "landing_pad_v1.md"


# --------------------------------------------------------------------------- helpers
def _assert_replans_verified(sim: SimResult) -> None:
    """Every replan succeeded, and its schedule passes check_schedule in planner time."""
    for record in sim.replans:
        after = record.after
        assert after.success and after.schedule is not None and after.task_graph is not None
        relative = shift_schedule(after.schedule, -replan_offset_h(record.t_h))
        assert check_schedule(after.requirements, after.task_graph, relative) == []
        assert relative.makespan_h <= after.requirements.horizon_h
        if record.event.kind == "robot_failure":
            assert record.event.robot not in {r.id for r in after.requirements.robots}


def _assert_ex2_failure_recovered(sim: SimResult, plan: PlanResult, t_h: float = 30.0) -> None:
    assert plan.task_graph is not None and plan.schedule is not None
    assert sim.completed and sim.finished_at_h is not None
    assert sim.finished_at_h <= plan.requirements.horizon_h
    failure = sim.events[0]
    assert (failure.kind, failure.robot, failure.t_h) == ("robot_failure", "EX2", t_h)
    first = sim.replans[0]
    assert first.event.kind == "robot_failure" and first.t_h == t_h
    assert first.before.makespan_h == plan.schedule.makespan_h
    assert first.after.schedule is not None
    assert first.after.schedule.makespan_h >= first.before.makespan_h
    # offline, the replan never asks a model for a first graph: the remaining graph is reused
    # (a "taskgraph" step saying so) or goes straight to the verifier (solver-first replanning)
    opening = first.after.steps[0]
    assert opening.ok and (opening.stage == "validate" or (
        opening.message is not None and opening.message.startswith("offline: reused prior graph")))
    # the trace reports the schedule in simulator hours, like the shifted schedule itself
    scheduled = [s for s in first.after.steps if s.stage == "schedule" and s.ok][-1]
    assert scheduled.payload is not None
    assert scheduled.payload["makespan_h"] == first.after.schedule.makespan_h
    assert scheduled.message == f"makespan {first.after.schedule.makespan_h}h (optimal)"
    assert sim.schedule is not None and all(e.robot != "EX2" for e in sim.schedule.entries)
    assert set(sim.frames[-1].done_tasks) == set(plan.task_graph.ids())
    assert all(r.status == "failed" for f in sim.frames if f.t_h >= t_h
               for r in f.robots if r.id == "EX2")
    _assert_replans_verified(sim)


def _chain_h(graph: TaskGraph, duration: Callable[[Task], int]) -> int:
    """The graph's longest precedence chain, each task taking ``duration(task)`` hours."""
    memo: dict[str, int] = {}

    def longest(task_id: str) -> int:
        if task_id not in memo:
            task = graph.task(task_id)
            memo[task_id] = duration(task) + max((longest(p) for p in task.after), default=0)
        return memo[task_id]

    return max(longest(t.id) for t in graph.tasks)


def _hand_made_briefs(tmp_path) -> Path:
    """A briefs directory with the hand-made fixtures' briefs (landing_pad_v1, access_road,
    tight_battery): the eval's mechanics, replayed on the numbers these tests were written for."""
    briefs = tmp_path / "hand_made"
    briefs.mkdir()
    shutil.copy(V1_BRIEF, briefs)
    for name in ("access_road.md", "tight_battery.md"):
        shutil.copy(EXAMPLES_DIR / name, briefs)
    return briefs


class _NoFixtures:
    """LLM client stub that records every call and never has a canned answer."""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        self.calls.append((role, attempt, variant))
        raise FixtureMissing("stub has no fixtures")


def _unpackable() -> tuple[Requirements, TaskGraph]:
    """Passes the validator (9900 <= 10000 Wh) but no split fits two 5000 Wh batteries."""
    req = Requirements(robots=[Robot(id="EX1", type="excavator", battery_wh=5000),
                               Robot(id="EX2", type="excavator", battery_wh=5000)],
                       horizon_h=100)
    graph = TaskGraph(tasks=[
        Task(id="T1", name="dig bowl", robot_type="excavator", duration_h=10, energy_wh=4000,
             zone="Z1"),
        Task(id="T2", name="cut trench", robot_type="excavator", duration_h=10, energy_wh=4000,
             zone="Z2"),
        Task(id="T3", name="strip apron", robot_type="excavator", duration_h=6, energy_wh=1900,
             zone="Z3"),
    ])
    return req, graph


@pytest.fixture(scope="module")
def api():
    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------- CLI
def test_cli_plan_then_simulate_landing_pad_with_ex2_failure(tmp_path, capsys):
    """The README's offline commands (the UI's sample is the same run): EX2 fails 44 % into
    its first dig, CP-SAT reschedules everything left without a model call, and the mission
    finishes at 91 h instead of 89 h."""
    plan_path = tmp_path / "landing_pad.plan.json"
    sim_path = tmp_path / "landing_pad.sim.json"
    code = main(["plan", str(BRIEF), "--llm", "fixture", "--out", str(plan_path), "-q"])
    assert code == EXIT_OK
    plan = PlanResult.model_validate_json(plan_path.read_text())
    assert plan.success and plan.schedule is not None and plan.task_graph is not None
    assert (plan.schedule.makespan_h, plan.repairs) == (89, 1)
    assert check_schedule(plan.requirements, plan.task_graph, plan.schedule) == []
    capsys.readouterr()

    code = main(["simulate", str(plan_path), "--fail", "EX2@10", "--llm", "fixture",
                 "--out", str(sim_path)])
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert ("t=  10.00h  robot_failure  EX2 task T5  EX2 failed at 10 h while working on T5 "
            "(44% done)") in out
    assert ("#1 at t=10.00h after robot_failure EX2: 13 tasks remaining; makespan 89h -> 91h "
            "(optimal, 0 repair(s))") in out
    assert "; CP-SAT rescheduled 13 tasks in " in out and "Nemotron not needed" in out
    assert "completed at t=91.00h" in out

    sim = SimResult.model_validate_json(sim_path.read_text())
    _assert_ex2_failure_recovered(sim, plan, 10.0)
    assert sim.finished_at_h == 91.0


def test_cli_eval_over_example_briefs(tmp_path, capsys):
    """The demo briefs as shipped: with the loop on, the recorded repairs of landing_pad (the
    printer's Wh) and tight_battery (the excavators' core) verify; front_loaded, designed
    impossible, fails both ways, after three repairs with the loop on."""
    out_json, out_csv = tmp_path / "eval.json", tmp_path / "eval.csv"
    code = main(["eval", "--briefs", str(EXAMPLES_DIR), "--llm", "fixture",
                 "--out", str(out_json), "--csv", str(out_csv)])
    assert code == EXIT_OK
    report = json.loads(out_json.read_text())
    summary = {s["condition"]: s for s in report["summary"]}
    assert (summary["loop_off"]["feasible"], summary["loop_on"]["feasible"]) == (1, 3)
    assert summary["loop_on"]["mean_repairs"] == pytest.approx(5 / 4)
    rows = {(r["brief"], r["condition"]): r for r in report["rows"]}
    assert rows["tight_battery", "loop_off"]["conflicts"] == ["energy:EX1", "energy:EX2"]
    assert rows["tight_battery", "loop_on"]["repairs"] == 1
    assert rows["tight_battery", "loop_on"]["makespan_h"] > 0
    assert rows["landing_pad", "loop_off"]["conflicts"] == ["volume:S2", "energy:printer"]
    assert rows["landing_pad", "loop_on"]["repairs"] == 1
    assert rows["landing_pad", "loop_on"]["makespan_h"] == 89
    assert rows["front_loaded", "loop_off"]["conflicts"] == ["horizon_path:T1->T2->T3->T6->T7->T8"]
    assert rows["front_loaded", "loop_on"]["conflicts"] == ["horizon", "zone:Z1"]
    assert rows["front_loaded", "loop_on"]["repairs"] == 3
    assert not rows["front_loaded", "loop_on"]["success"]
    # phase-3 columns: fixture answers are valid, only the road is feasible first try, the
    # other briefs need a repair and every verified repair changed the graph's total energy
    assert all(r["answer_valid"] for r in report["rows"])
    assert [b for (b, c), r in rows.items() if c == "loop_on" and r["first_try_feasible"]] == [
        "access_road"]
    assert rows["tight_battery", "loop_off"]["first_try_feasible"] is False
    assert rows["tight_battery", "loop_on"]["models"] == "fixture:taskgraph_1.json,fixture:taskgraph_2.json"
    assert rows["landing_pad", "loop_off"]["models"] == "fixture:taskgraph_1.json"
    assert rows["tight_battery", "loop_on"]["scope_drift_pct"] is not None
    assert rows["tight_battery", "loop_on"]["scope_drift_pct"] != 0.0
    assert rows["landing_pad", "loop_on"]["scope_drift_pct"] == pytest.approx(-8.3, abs=0.05)
    assert rows["access_road", "loop_on"]["scope_drift_pct"] == 0.0
    assert all(r["first_energy_wh"] and r["final_energy_wh"] for r in report["rows"])
    loop_on = summary["loop_on"]
    assert (loop_on["first_try_feasible"], loop_on["answer_valid_rate"]) == (1, 1.0)
    assert loop_on["feasible_ci_low"] < loop_on["feasible_rate"] <= loop_on["feasible_ci_high"]
    assert (report["workers"], report["feedback"], report["thinking"]) == (1, "core", "auto")
    assert len(out_csv.read_text().splitlines()) == 1 + 8
    table = capsys.readouterr().out
    assert "loop_on" in table and "95% CI" in table


def test_cli_eval_arms_replay_the_same_fixtures_and_write_markdown(tmp_path, capsys):
    """``--models ultra,super`` with the fixture client: four conditions named after the arms,
    identical rows per arm (the recordings do not know the arm), zero cost, and a markdown
    document whose per-brief matrix reads from the rows themselves."""
    out_json, out_csv, out_md = tmp_path / "e.json", tmp_path / "e.csv", tmp_path / "e.md"
    code = main(["eval", "--briefs", str(_hand_made_briefs(tmp_path)), "--llm", "fixture",
                 "--models", "ultra,super", "--thinking", "auto", "--out", str(out_json),
                 "--csv", str(out_csv), "--markdown", str(out_md)])
    assert code == EXIT_OK
    report = json.loads(out_json.read_text())
    conditions = ["ultra/loop_off", "ultra/loop_on", "super/loop_off", "super/loop_on"]
    assert report["conditions"] == conditions
    assert report["arms"] == {"ultra": ARM_MODELS["ultra"], "super": ARM_MODELS["super"]}
    assert report["role_models"] == {} and report["thinking"] == "auto"
    rows = {(r["brief"], r["condition"]): r for r in report["rows"]}
    assert len(rows) == 3 * 4

    def stable(r: dict) -> dict:
        return {k: v for k, v in r.items() if k not in ("latency_s", "llm_latency_s", "condition")}

    for brief in ("access_road", "landing_pad_v1", "tight_battery"):
        for loop in ("loop_off", "loop_on"):
            assert stable(rows[brief, f"ultra/{loop}"]) == stable(rows[brief, f"super/{loop}"])
    assert all(r["cost_usd"] == 0.0 and r["thinking"] == "auto" for r in rows.values())
    summary = {s["condition"]: s for s in report["summary"]}
    assert (summary["ultra/loop_off"]["feasible"], summary["ultra/loop_on"]["feasible"]) == (2, 3)
    assert summary["ultra/loop_on"]["priced_runs"] == 3
    assert summary["ultra/loop_on"]["total_cost_usd"] == 0.0
    assert summary["ultra/loop_on"]["latency_p50_s"] <= summary["ultra/loop_on"]["latency_p95_s"]
    assert summary["ultra/loop_on"]["mean_llm_latency_s"] <= summary["ultra/loop_on"]["mean_latency_s"]
    text = out_md.read_text()
    assert "| brief | ultra/loop_off | ultra/loop_on | super/loop_off | super/loop_on |" in text
    pad = rows["landing_pad_v1", "ultra/loop_on"]
    assert f"| `landing_pad_v1` | ok {pad['makespan_h']} h / 0 rep | ok {pad['makespan_h']} h / 0 rep |" in text
    tight_off, tight_on = rows["tight_battery", "ultra/loop_off"], rows["tight_battery", "ultra/loop_on"]
    kinds = ",".join(sorted({c.split(":")[0] for c in tight_off["conflicts"]}))
    assert f"| `tight_battery` | fail {kinds} | ok {tight_on['makespan_h']} h / 1 rep |" in text
    assert f"`ultra` = `{ARM_MODELS['ultra']}`" in text and "Thinking `auto`" in text
    assert "This run used `--llm fixture`" in text
    # the fixture graphs carry no quantities, so every verified run is also in scope
    assert "| `ultra/loop_on` | 3 | 100% (44%-100%) | 100% (44%-100%) | 67% | 0.33 |" in text
    assert "## Per kind" not in text   # no kinds.json next to these briefs
    header = out_csv.read_text().splitlines()[0]
    assert header.startswith("brief,condition,repeat,success")
    assert header.endswith(",cost_usd,volume_advisories,min_volume_share,in_scope,requirements_sha,kind,"
                           "rejected_answers,min_area_share,plan_path")
    captured = capsys.readouterr()
    assert "95% CI" in captured.out and captured.out.rstrip().endswith(str(out_md))
    assert "ultra/loop_on" in captured.err.splitlines()[-3]   # the table is echoed to stderr


def test_cli_eval_workers_produce_the_same_rows(tmp_path, capsys):
    """``--workers 3`` yields the same report as the sequential run (latencies aside), in job
    order; the CSV holds the same rows in whatever order they finished."""
    briefs = _hand_made_briefs(tmp_path)

    def run(workers: int) -> tuple[dict, list[dict]]:
        out_json, out_csv = tmp_path / f"eval_{workers}.json", tmp_path / f"eval_{workers}.csv"
        code = main(["eval", "--briefs", str(briefs), "--llm", "fixture",
                     "--workers", str(workers), "--out", str(out_json), "--csv", str(out_csv)])
        assert code == EXIT_OK
        with out_csv.open(encoding="utf-8") as fh:
            return json.loads(out_json.read_text()), list(csv.DictReader(fh))

    def stable(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in r.items() if k not in ("latency_s", "llm_latency_s")}
                for r in rows]

    sequential, sequential_csv = run(1)
    parallel, parallel_csv = run(3)
    assert stable(sequential["rows"]) == stable(parallel["rows"])
    assert [(r["brief"], r["condition"]) for r in parallel["rows"]] == [
        (b, c) for b in ("access_road", "landing_pad_v1", "tight_battery")
        for c in ("loop_off", "loop_on")]
    assert (sequential["workers"], parallel["workers"]) == (1, 3)
    def key(r: dict) -> tuple[str, str, str]:
        return r["brief"], r["condition"], r["repeat"]

    assert sorted(stable(sequential_csv), key=key) == sorted(stable(parallel_csv), key=key)
    for field in ("feasible", "feasible_ci_low", "feasible_ci_high", "first_try_feasible",
                  "mean_scope_drift_pct"):
        assert [s[field] for s in sequential["summary"]] == [s[field] for s in parallel["summary"]]
    err = capsys.readouterr().err
    assert err.count("tight_battery") == 4 and "workers=3" in err


def test_cli_montecarlo_over_the_landing_pad_plan(tmp_path, capsys):
    plan_path = tmp_path / "landing_pad.plan.json"
    assert main(["plan", str(BRIEF), "--llm", "fixture", "--out", str(plan_path), "-q"]) == EXIT_OK
    capsys.readouterr()
    out = tmp_path / "landing_pad.mc.json"
    code = main(["montecarlo", str(plan_path), "--runs", "10", "--seed", "1", "--out", str(out)])
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    assert captured.out.startswith("Monte Carlo robustness: 10 runs, horizon 336 h")
    assert "replans via solver-only" in captured.err
    result = MonteCarloResult.model_validate_json(out.read_text())
    assert result.runs == 10 and [s.seed for s in result.samples] == list(range(1, 11))
    assert result.horizon_h == 336 and 0 <= result.completed <= 10
    assert result.within_horizon_rate <= result.completion_rate <= 1.0
    assert sum(b.count for b in result.histogram) == result.completed
    assert len(result.histogram) >= 12 and result.histogram[11].end_h == 336
    if result.finish_h.min is not None:
        # jitter can make a run beat the plan's 89 h, never its longest precedence chain with
        # every task at its shortest jittered duration (default jitter 0.15: at least
        # floor(0.85 d), never under 1 h)
        graph = PlanResult.model_validate_json(plan_path.read_text()).task_graph
        assert graph is not None
        assert result.finish_h.min >= _chain_h(graph, lambda t: max(1, math.floor(0.85 * t.duration_h)))
    assert all(f.robot in {"EX1", "EX2", "HL1", "HL2", "CP1", "PR1", "SV1"}
               for s in result.samples for f in s.failures)
    assert all(0 <= f.t_h < 336 for s in result.samples for f in s.failures)


# --------------------------------------------------------------------------- API
def test_api_montecarlo_sample_failures_replay_through_simulate(api):
    plan = api.post("/plan", json={"brief": BRIEF.read_text(), "brief_id": "landing_pad",
                                   "llm": "fixture", "explain": False}).json()
    assert plan["success"] is True
    r = api.post("/montecarlo", json={"plan": plan, "runs": 10, "seed": 1})
    assert r.status_code == 200, r.text
    result = MonteCarloResult.model_validate(r.json())
    assert result.runs == 10 and result.horizon_h == 336
    assert [s.seed for s in result.samples] == list(range(1, 11))
    assert result.replans_mean <= 2 and result.failures_mean <= 7
    # the same seed through the CLI's module gives the same experiment
    again = api.post("/montecarlo", json={"plan": plan, "runs": 10, "seed": 1}).json()
    assert MonteCarloResult.model_validate(again).samples == result.samples
    # a sample's failures are a /simulate body as they are ("replay this run" in the UI)
    sample = next((s for s in result.samples if s.failures), None)
    assert sample is not None
    r = api.post("/simulate", json={"plan": plan, "brief_id": "landing_pad", "llm": "fixture",
                                    "failures": [f.model_dump() for f in sample.failures]})
    assert r.status_code == 200, r.text
    sim = SimResult.model_validate(r.json())
    end_h = sim.frames[-1].t_h
    fired = [e for e in sim.events if e.kind == "robot_failure"]
    for f in sample.failures:
        if f.t_h > end_h:
            continue   # nominal durations finish earlier than the jittered run did
        # an injection fires at the next simulator step (dt 0.25 h) after its time
        assert any(e.robot == f.robot and 0 <= e.t_h - f.t_h <= 0.25 for e in fired), (f, fired)


def test_api_plan_then_simulate_landing_pad(api):
    r = api.post("/plan", json={"brief": BRIEF.read_text(), "brief_id": "landing_pad",
                                "llm": "fixture", "explain": False})
    assert r.status_code == 200 and r.json()["success"] is True
    plan = PlanResult.model_validate(r.json())

    r = api.post("/simulate", json={"plan": r.json(), "failures": [{"robot": "EX2", "t_h": 30}],
                                    "brief_id": "landing_pad", "llm": "fixture",
                                    "frame_stride": 8, "dt_h": 0.5})
    assert r.status_code == 200, r.text
    sim = SimResult.model_validate(r.json())
    _assert_ex2_failure_recovered(sim, plan)
    assert all(f.t_h % 0.5 == 0 for f in sim.frames)  # dt_h honoured


@pytest.mark.usefixtures("live_steps")
def test_api_ui_flow_example_structure_corrected_plan_stream_simulate(api):
    """The web UI's path: pick an example, structure it, correct the requirements, plan from
    them (plain and streamed), then simulate with the API defaults."""
    example = api.get("/examples/landing_pad").json()
    assert example["text_markdown"] == BRIEF.read_text() and example["title"]
    assert "**" not in example["text"] and example["text"]   # plain prose for the textarea

    structured = api.post("/structure", json={"brief": example["text"],
                                              "brief_id": example["id"], "llm": "fixture"})
    assert structured.status_code == 200 and structured.json()["key"] == "landing_pad"
    requirements = structured.json()["requirements"]
    assert requirements is not None and structured.json()["step"]["ok"] is True

    # the user tightens the horizon on the requirements card; the plan must honour it
    requirements["horizon_h"] = 120
    body = {"requirements": requirements, "brief_id": "landing_pad", "llm": "fixture"}
    r = api.post("/plan", json=body)
    assert r.status_code == 200, r.text
    plan = PlanResult.model_validate(r.json())
    assert plan.success and plan.schedule is not None and plan.task_graph is not None
    assert plan.requirements.horizon_h == 120 and 0 < plan.schedule.makespan_h <= 120
    assert [s.stage for s in plan.steps] == ["structure", "taskgraph", "validate", "repair",
                                             "validate", "schedule", "explain"]
    assert plan.steps[0].message is not None
    assert plan.steps[0].message.startswith("requirements provided by the user: ")
    assert plan.explanation
    assert check_schedule(plan.requirements, plan.task_graph, plan.schedule) == []

    events = stream_plan(api, body)
    assert [kind for kind, _ in events] == ["step"] * 7 + ["result"]
    assert [data["stage"] for kind, data in events[:7]] == [s.stage for s in plan.steps]
    streamed = PlanResult.model_validate(events[-1][1])
    assert streamed.success and streamed.schedule is not None
    assert streamed.schedule.entries == plan.schedule.entries
    assert streamed.schedule.makespan_h == plan.schedule.makespan_h

    r = api.post("/simulate", json={"plan": r.json(), "failures": [{"robot": "EX2", "t_h": 30}],
                                    "brief_id": "landing_pad", "llm": "fixture"})
    assert r.status_code == 200, r.text
    sim = SimResult.model_validate(r.json())
    _assert_ex2_failure_recovered(sim, plan)
    assert all(round(f.t_h % 1.0, 9) in (0.0, 0.25, 0.5, 0.75) for f in sim.frames)
    assert [f.t_h for f in sim.frames[:3]] == [0.0, 1.0, 2.0]   # default stride 4 at dt 0.25


# --------------------------------------------------------------------------- offline replan repairs
def test_offline_replan_repair_ends_with_the_solver_verdict():
    req, graph = _unpackable()
    client = _NoFixtures()
    result = Planner(client, max_repairs=3, explain=False).plan_from_requirements(
        req, key="k", prior_graph=graph, context="EX3 failed at 12 h", variant="replan")

    assert client.calls == [("taskgraph", 1, "replan"), ("repair", 2, "replan")]
    assert not result.success and result.repairs == 1 and result.task_graph == graph
    assert result.failure is not None and result.failure.kind == "infeasible"
    assert set(result.failure.conflicts) == {"energy:EX1", "energy:EX2"}
    assert [s.stage for s in result.steps] == ["taskgraph", "validate", "schedule", "repair"]
    assert result.steps[0].ok and result.steps[0].message is not None
    assert result.steps[0].message.startswith("offline: reused prior graph")
    assert not result.steps[-1].ok and result.steps[-1].failure == result.failure


def test_fixture_client_without_a_fixture_directory_behaves_the_same(tmp_path):
    req, graph = _unpackable()
    result = Planner(FixtureClient(tmp_path), explain=False).plan_from_requirements(
        req, key="nowhere", prior_graph=graph, variant="replan")
    assert not result.success and result.repairs == 1
    assert result.failure is not None and result.failure.kind == "infeasible"
    assert set(result.failure.conflicts) == {"energy:EX1", "energy:EX2"}


# --------------------------------------------------------------------------- replan triggers
@pytest.mark.parametrize("brief_id", ["landing_pad", "access_road", "tight_battery"])
def test_no_failure_replay_of_each_fixture_plan_raises_no_event(brief_id):
    """A verified plan replayed as planned: travel and the step grain make every chain finish a
    little after the makespan, but no task overruns and nothing is replanned."""
    planner = Planner(FixtureClient(), explain=False)
    plan = planner.plan((EXAMPLES_DIR / f"{brief_id}.md").read_text(), brief_id=brief_id)
    assert plan.success and plan.task_graph is not None and plan.schedule is not None
    sim = run_with_replanning(planner, plan, [], key=brief_id)
    assert sim.completed and sim.replans == [] and sim.events == []
    assert sim.finished_at_h is not None and sim.finished_at_h <= plan.requirements.horizon_h
    assert plan.schedule.makespan_h <= sim.finished_at_h <= plan.schedule.makespan_h + 4
    assert set(sim.frames[-1].done_tasks) == set(plan.task_graph.ids())
    assert all(r.battery_wh >= 0 for f in sim.frames for r in f.robots)


def test_two_robot_failures_are_both_replanned_within_the_default_budget():
    """EX2 fails at 30 h; HL2 fails at a moment the replanned schedule has it working (found
    from a first replay, so the test does not depend on one particular schedule). The hand-made
    plan: in the live demo plan HL2's hauls are done by 29 h."""
    plan = Planner(FixtureClient(), explain=False).plan(V1_BRIEF.read_text(),
                                                        brief_id="landing_pad_v1")
    assert plan.task_graph is not None
    first = run_with_replanning(Planner(FixtureClient(), explain=False), plan,
                                [FailureInjection("EX2", 30)], key="landing_pad_v1", max_replans=2)
    assert first.completed and len(first.replans) == 1
    t_hl2 = next(f.t_h for f in first.frames if f.t_h > 31
                 for r in f.robots if r.id == "HL2" and r.status == "working")
    injections = [FailureInjection("EX2", 30), FailureInjection("HL2", t_hl2)]
    sim = run_with_replanning(Planner(FixtureClient(), explain=False), plan, injections,
                              key="landing_pad_v1", max_replans=2)
    assert [(r.t_h, r.event.kind, r.event.robot) for r in sim.replans] == \
        [(30.0, "robot_failure", "EX2"), (t_hl2, "robot_failure", "HL2")]
    assert [e.kind for e in sim.events] == ["robot_failure", "robot_failure"]
    assert sim.completed and sim.finished_at_h is not None
    assert sim.finished_at_h <= plan.requirements.horizon_h
    assert set(sim.frames[-1].done_tasks) == set(plan.task_graph.ids())
    _assert_replans_verified(sim)


# --------------------------------------------------------------------------- eval save / resume
def _without_timings(plan: PlanResult) -> dict:
    """A PlanResult's dump with every wall-clock figure zeroed, so two runs of one fixture
    brief compare equal."""
    data = plan.model_dump()
    if data["schedule"] is not None:
        data["schedule"]["solve_time_s"] = 0.0
    for step in data["steps"]:
        step["duration_s"] = 0.0
        if step["usage"] is not None:
            step["usage"]["latency_s"] = 0.0
        receipt = (step["payload"] or {}).get("verification")
        if isinstance(receipt, dict):
            receipt["solve_time_s"] = 0.0
    return data


def test_cli_eval_save_plans_and_resume_over_example_briefs(tmp_path, capsys, api):
    """``--save-plans`` keeps every run's PlanResult (the JSON ``POST /plan`` returns) and each
    job's structuring; after a kill, ``--resume`` skips the jobs the CSV already holds, appends
    the rest under the one header and summarises old and new rows together."""
    plans = tmp_path / "plans"
    out_json, out_csv, out_md = tmp_path / "e.json", tmp_path / "e.csv", tmp_path / "e.md"
    argv = ["eval", "--briefs", str(_hand_made_briefs(tmp_path)), "--llm", "fixture",
            "--save-plans", str(plans), "--out", str(out_json), "--csv", str(out_csv),
            "--markdown", str(out_md)]
    assert main(argv) == EXIT_OK
    briefs = ("access_road", "landing_pad_v1", "tight_battery")
    assert {p.name for p in plans.iterdir()} == (
        {f"{b}__{c}__1.plan.json" for b in briefs for c in ("loop_off", "loop_on")}
        | {f"{b}__1.structure.json" for b in briefs})
    saved = PlanResult.model_validate_json(
        (plans / "landing_pad_v1__loop_on__1.plan.json").read_text())
    r = api.post("/plan", json={"brief": V1_BRIEF.read_text(), "brief_id": "landing_pad_v1",
                                "llm": "fixture", "explain": False})
    assert r.status_code == 200
    assert _without_timings(saved) == _without_timings(PlanResult.model_validate(r.json()))
    assert saved.success and saved.steps[0].stage == "structure"
    assert "warnings" in (saved.steps[0].payload or {})
    assert saved.steps[-1].payload is not None and "verification" in saved.steps[-1].payload
    failed = PlanResult.model_validate_json(
        (plans / "tight_battery__loop_off__1.plan.json").read_text())
    assert failed.success is False and failed.failure is not None
    assert failed.failure.conflicts == ["energy:EX1", "energy:EX2"]
    assert failed.suggestions and "budget" in (failed.steps[-1].payload or {})
    record = json.loads((plans / "tight_battery__1.structure.json").read_text())
    assert record["ok"] is True and isinstance(record["payload"]["warnings"], list)
    assert record["requirements"]["robots"] and record["usage"]["model"] == "fixture:structure.json"
    assert record["failure"] is None and record["latency_s"] >= 0.0
    lines = out_csv.read_text().splitlines()
    assert len(lines) == 1 + 6 and lines[0].endswith(",kind,rejected_answers,min_area_share,plan_path")
    with out_csv.open(encoding="utf-8") as fh:
        rows = {(r["brief"], r["condition"]): r for r in csv.DictReader(fh)}
    assert rows["tight_battery", "loop_on"]["plan_path"] == "plans/tight_battery__loop_on__1.plan.json"
    assert rows["tight_battery", "loop_on"]["requirements_sha"] == record["requirements_sha"]
    assert all(r["rejected_answers"] == "0" for r in rows.values())
    assert "| rejected answers | p50 cost |" in out_md.read_text()
    # the run is killed after two jobs: the CSV ends at landing_pad_v1, tight_battery's are gone
    out_csv.write_text("\n".join(lines[:5]) + "\n", encoding="utf-8")
    for name in ("tight_battery__loop_off__1.plan.json", "tight_battery__loop_on__1.plan.json",
                 "tight_battery__1.structure.json"):
        (plans / name).unlink()
    capsys.readouterr()
    assert main([*argv, "--resume"]) == EXIT_OK
    err = capsys.readouterr().err
    assert "resume: 4 rows from a previous run" in err
    assert "2 of 3 job(s) already complete, skipped" in err
    assert err.count("tight_battery") == 2 and "landing_pad" not in err   # only that job ran
    lines = out_csv.read_text().splitlines()
    assert len(lines) == 1 + 6 and sum(line.startswith("brief,") for line in lines) == 1
    assert [line.split(",")[0] for line in lines[5:]] == ["tight_battery", "tight_battery"]
    assert (plans / "tight_battery__loop_on__1.plan.json").is_file()
    assert (plans / "tight_battery__1.structure.json").is_file()
    report = json.loads(out_json.read_text())
    summary = {s["condition"]: s for s in report["summary"]}
    assert report["resumed_rows"] == 4 and len(report["rows"]) == 6
    assert report["plans_dir"] == str(plans)
    assert [(r["brief"], r["condition"]) for r in report["rows"]] == [
        (b, c) for b in briefs for c in ("loop_off", "loop_on")]
    assert (summary["loop_off"]["feasible"], summary["loop_on"]["feasible"]) == (2, 3)
    assert summary["loop_on"]["mean_repairs"] == pytest.approx(1 / 3)
    assert (summary["loop_on"]["rejected_runs"], summary["loop_on"]["cost_p50_usd"]) == (0, 0.0)
    text = out_md.read_text()
    assert "\nresumed: 4 rows from a previous run\n" in text and "6 run(s)" in text
    assert "| 0/3 | $0.0000 |" in text and f"kept under `{plans}`" in text
