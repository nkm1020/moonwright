"""Planner repair-loop tests with scripted LLM clients (no fixtures, no network).

The tight_battery fixtures supply a graph CP-SAT rejects (``taskgraph_1``: energy core on
EX1/EX2) and its feasible repair (``taskgraph_2``); the scripted client decides what the
"model" answers, or raises, on each (role, attempt).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from moonwright.llm.base import (
    FIXTURES_DIR,
    FixtureMissing,
    LLMError,
    SchemaMismatch,
    Usage,
    validate_into,
)
from moonwright.pipeline.loop import Planner
from moonwright.schemas import (
    LoopStep,
    PlanResult,
    Requirements,
    ScheduleEntry,
    TaskGraph,
    fmt_wh,
    to_mwh,
)

TIGHT = FIXTURES_DIR / "tight_battery"
REQ = Requirements.model_validate_json((TIGHT / "structure.json").read_text())
STRUCTURE: dict[str, Any] = json.loads((TIGHT / "structure.json").read_text())
INFEASIBLE: dict[str, Any] = json.loads((TIGHT / "taskgraph_1.json").read_text())
FEASIBLE: dict[str, Any] = json.loads((TIGHT / "taskgraph_2.json").read_text())
ENERGY_CORE = {"energy:EX1", "energy:EX2"}


class Scripted:
    """Answers per (role, attempt): a payload dict, or an exception instance to raise."""

    name = "scripted"

    def __init__(self, script: dict[tuple[str, int], Any]):
        self.script = script
        self.calls: list[tuple[str, int]] = []
        self.prompts: list[str] = []

    def complete_json(self, *, role, system, user, schema, key=None, attempt=1, variant=None,
                      max_attempts=2):
        self.calls.append((role, attempt))
        self.prompts.append(user)
        answer = self.script[(role, attempt)]
        if isinstance(answer, Exception):
            raise answer
        return validate_into(schema, answer), Usage(model="scripted")


def schema_error(role: str = "repair") -> SchemaMismatch:
    return SchemaMismatch(f"{role} (m): answer failed validation after 2 attempt(s)", "{}",
                          ["tasks.0.duration_h: Input should be greater than 0"])


def without(graph: dict[str, Any], task_id: str) -> dict[str, Any]:
    tasks = [dict(t, after=[a for a in t.get("after", []) if a != task_id])
             for t in graph["tasks"] if t["id"] != task_id]
    return {"tasks": tasks}


def trace(result: PlanResult) -> list[tuple[str, int, bool]]:
    return [(s.stage, s.attempt, s.ok) for s in result.steps]


def planner(client: Scripted, max_repairs: int = 3) -> Planner:
    return Planner(client, max_repairs=max_repairs, explain=False)


# --------------------------------------------------------------------------- rejected answers
def test_rejected_repair_answer_is_retried_with_the_same_verdict():
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error(),
                       ("repair", 3): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")

    assert result.success and result.repairs == 2 and result.failure is None
    assert client.calls == [("taskgraph", 1), ("repair", 2), ("repair", 3)]
    # validate / schedule carry the round of the graph they judged (1, then round 3's answer)
    assert trace(result) == [("taskgraph", 1, True), ("validate", 1, True), ("schedule", 1, False),
                             ("repair", 2, False), ("repair", 3, True), ("validate", 3, True),
                             ("schedule", 3, True)]
    rejected = result.steps[3]
    assert rejected.failure is not None and rejected.failure.kind == "schema"
    assert rejected.failure.conflicts == ["schema:tasks.0.duration_h"]
    # round three saw the solver's verdict again, the last valid graph, and why round two died
    third = client.prompts[2]
    assert '"energy:EX1"' in third and "PREVIOUS TASK GRAPH" in third and "one pass" in third
    assert "rejected and discarded" in third and "Input should be greater than 0" in third
    assert result.task_graph == TaskGraph.model_validate(FEASIBLE)


def test_transport_error_in_a_repair_round_is_retried():
    client = Scripted({("taskgraph", 1): INFEASIBLE,
                       ("repair", 2): LLMError("repair (m): APITimeoutError: Request timed out."),
                       ("repair", 3): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success and result.repairs == 2
    assert result.steps[3].failure is not None and result.steps[3].failure.kind == "llm_error"
    assert "Request timed out" in client.prompts[2]


def test_budget_bounds_the_retries_and_the_verdict_is_what_gets_reported():
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error(),
                       ("repair", 3): schema_error()})
    result = planner(client, max_repairs=2).plan_from_requirements(REQ, key="k")

    assert not result.success and result.repairs == 2
    assert client.calls == [("taskgraph", 1), ("repair", 2), ("repair", 3)]
    assert result.task_graph == TaskGraph.model_validate(INFEASIBLE)   # last valid graph
    assert result.failure is not None and result.failure.kind == "infeasible"
    assert set(result.failure.conflicts) == ENERGY_CORE
    # the rejection note is addressed to the model: the prompt carries it once (it never
    # stacks), the failure the user sees is the bare solver verdict
    assert "rejected and discarded" not in result.failure.hint
    assert client.prompts[2].count("rejected and discarded") == 1


def test_rejected_first_graph_is_regenerated_within_the_budget():
    client = Scripted({("taskgraph", 1): schema_error("taskgraph"), ("taskgraph", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success and result.repairs == 1
    assert client.calls == [("taskgraph", 1), ("taskgraph", 2)]
    assert trace(result)[:2] == [("taskgraph", 1, False), ("taskgraph", 2, True)]
    assert "CONTEXT:" in client.prompts[1] and "Input should be greater than 0" in client.prompts[1]
    assert "CONTEXT:" not in client.prompts[0]


def test_loop_off_gives_no_second_chance():
    client = Scripted({("taskgraph", 1): schema_error("taskgraph")})
    result = planner(client, max_repairs=0).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 0 and client.calls == [("taskgraph", 1)]
    assert result.failure is not None and result.failure.kind == "schema"

    client = Scripted({("taskgraph", 1): INFEASIBLE})
    result = planner(client, max_repairs=0).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 0
    assert result.failure is not None and set(result.failure.conflicts) == ENERGY_CORE


def test_offline_fixture_misses_are_not_retried():
    client = Scripted({("taskgraph", 1): INFEASIBLE,
                       ("repair", 2): FixtureMissing("missing fixture taskgraph_2.json")})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 1
    assert client.calls == [("taskgraph", 1), ("repair", 2)]
    assert result.failure is not None and result.failure.kind == "llm_error"
    assert result.steps[-1].message is not None
    assert result.steps[-1].message.startswith("offline:")

    client = Scripted({("taskgraph", 1): FixtureMissing("no fixtures for brief 'k'")})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 0 and len(client.calls) == 1


# --------------------------------------------------------------------------- required tasks
def test_required_tasks_are_fed_back_as_missing_conflicts():
    """Replanning: a graph that drops a remaining task is rejected and repaired."""
    required = [t["id"] for t in FEASIBLE["tasks"]]
    client = Scripted({("taskgraph", 1): without(FEASIBLE, "T7"), ("repair", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=TaskGraph.model_validate(FEASIBLE), context="HL2 failed",
        variant="replan", required_tasks=required)

    assert result.success and result.repairs == 1
    verdict = result.steps[1]
    assert verdict.stage == "validate" and not verdict.ok and verdict.failure is not None
    assert verdict.failure.kind == "validation" and verdict.failure.conflicts == ["missing:T7"]
    assert len(verdict.failure.details) == 1 and "dropped T7" in verdict.failure.hint
    assert '"missing:T7"' in client.prompts[1]


def test_missing_tasks_merge_with_the_validator_verdict_in_check_order():
    dropped = without(FEASIBLE, "T7")
    dropped["tasks"][1]["energy_wh"] = 9000                   # T2: no excavator can power it
    client = Scripted({("taskgraph", 1): dropped})
    result = planner(client, max_repairs=0).plan_from_requirements(
        REQ, key="k", required_tasks=[t["id"] for t in FEASIBLE["tasks"]])
    failure = result.failure
    assert failure is not None and failure.kind == "validation"
    assert failure.conflicts == ["missing:T7", "energy_task:T2", "energy:excavator"]
    assert len(failure.details) == 3 and failure.hint.startswith("The graph dropped T7")


@pytest.mark.parametrize("required", [None, [], ["T1", "T2"]])
def test_required_tasks_present_do_not_change_the_verdict(required):
    client = Scripted({("taskgraph", 1): FEASIBLE})
    result = planner(client, max_repairs=0).plan_from_requirements(REQ, key="k",
                                                                   required_tasks=required)
    assert result.success and [s.stage for s in result.steps] == ["taskgraph", "validate", "schedule"]
    assert result.steps[-1].payload == {"makespan_h": 65, "status": "optimal",
                                        "verified": True}


# --------------------------------------------------------------------------- replanning (live)
REMAINING = TaskGraph.model_validate(FEASIBLE)   # stands in for a replan's remaining graph


def invented(graph: dict[str, Any]) -> dict[str, Any]:
    """Keeps the ids but makes every task a 1 h / 1 Wh stub in zone Z9 with no predecessors."""
    return {"tasks": [dict(t, duration_h=1, energy_wh=1, zone="Z9", after=[])
                      for t in graph["tasks"]]}


def test_replan_prompt_carries_the_remaining_graph():
    client = Scripted({("taskgraph", 1): FEASIBLE})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL2 failed at 12 h", variant="replan",
        required_tasks=REMAINING.ids())
    assert result.success and client.calls == [("taskgraph", 1)]
    prompt = client.prompts[0]
    assert "REMAINING TASK GRAPH:" in prompt and prompt.endswith("CONTEXT:\nHL2 failed at 12 h")
    for task in REMAINING.tasks:   # every definition is in the prompt, not just the ids
        assert f'"id": "{task.id}"' in prompt
        assert f'"duration_h": {task.duration_h}' in prompt and f'"zone": "{task.zone}"' in prompt
    assert "PREVIOUS TASK GRAPH" not in prompt and "FAILURE:" not in prompt


def test_invented_replan_graph_is_sent_back_as_changed_conflicts():
    """A live model that keeps the ids but guesses durations, zones and edges is rejected by
    the scope guard like any other verdict, repaired, and never scheduled as it was."""
    client = Scripted({("taskgraph", 1): invented(FEASIBLE), ("repair", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL2 failed", variant="replan",
        required_tasks=REMAINING.ids())

    assert result.success and result.repairs == 1
    assert result.task_graph == REMAINING and result.schedule is not None
    verdict = result.steps[1]
    assert verdict.stage == "validate" and not verdict.ok and verdict.failure is not None
    assert verdict.failure.kind == "validation"
    assert verdict.failure.conflicts == [f"changed:{t}" for t in REMAINING.ids()]
    assert len(verdict.failure.details) == len(REMAINING.tasks)
    t2 = REMAINING.task("T2")
    assert verdict.failure.details[1] == (
        f"T2 changed: zone Z9 (was {t2.zone}); dropped after {', '.join(t2.after)}; "
        f"duration_h 1 (needs {t2.duration_h}); "
        f"energy_wh 1 (needs {fmt_wh(to_mwh(t2.energy_wh))})")
    assert "redefined T1, T2" in verdict.failure.hint
    repair = client.prompts[1]
    assert '"changed:T2"' in repair and "PREVIOUS TASK GRAPH" in repair
    assert "REMAINING TASK GRAPH (the work still outstanding" in repair


def test_replan_that_retypes_a_running_task_is_a_verdict_not_a_crash():
    """A task underway on EX1 whose robot_type the model changed used to reach the solver,
    which pinned it anyway, and the re-verification then raised; the scope guard now rejects
    the graph before the solver like any other verdict."""
    retyped = {"tasks": [dict(t, robot_type="hauler") if t["id"] == "T2" else t
                         for t in FEASIBLE["tasks"]]}
    client = Scripted({("taskgraph", 1): retyped, ("repair", 2): retyped})
    locked = [ScheduleEntry(task="T2", robot="EX1", start_h=0, end_h=REMAINING.task("T2").duration_h)]
    result = planner(client, max_repairs=1).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL1 failed", variant="replan",
        locked=locked, required_tasks=REMAINING.ids())
    assert not result.success and result.repairs == 1
    assert result.failure is not None and result.failure.kind == "validation"
    # the scope verdict comes first; the validator's own view of the retyped task follows
    assert result.failure.conflicts[0] == "changed:T2"
    assert result.failure.details[0] == "T2 changed: robot_type hauler (was excavator)"
    assert "energy:hauler" in result.failure.conflicts
    assert [s.stage for s in result.steps] == ["taskgraph", "validate", "repair", "validate"]


def test_replan_may_grow_tasks_and_add_work_but_not_drop_edges():
    grown = {"tasks": [dict(t, duration_h=t["duration_h"] + 1, energy_wh=t["energy_wh"] + 1)
                       for t in FEASIBLE["tasks"]]
             + [{"id": "T99", "name": "re-survey", "robot_type": "surveyor", "duration_h": 1,
                 "energy_wh": 10, "zone": "Z1", "after": ["T1"]}]}
    client = Scripted({("taskgraph", 1): grown})
    result = planner(client, max_repairs=0).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, variant="replan", required_tasks=REMAINING.ids())
    assert result.success and result.task_graph is not None
    assert "T99" in result.task_graph.ids()

    freed = {"tasks": [dict(t, after=[]) for t in FEASIBLE["tasks"]]}
    client = Scripted({("taskgraph", 1): freed})
    result = planner(client, max_repairs=0).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, variant="replan", required_tasks=REMAINING.ids())
    assert not result.success and result.failure is not None
    with_edges = [t.id for t in REMAINING.tasks if t.after]
    assert result.failure.conflicts == [f"changed:{t}" for t in with_edges]


# --------------------------------------------------------------------------- re-verification
def test_verified_schedule_step_carries_the_flag():
    client = Scripted({("taskgraph", 1): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="t")
    assert result.success
    step = [s for s in result.steps if s.stage == "schedule"][-1]
    assert step.payload["verified"] is True and step.payload["makespan_h"] == 65


def test_scheduler_output_is_re_verified(monkeypatch):
    """A schedule that violates the constraints must never leave the planner as a plan."""
    from moonwright.pipeline import loop as loop_module
    from moonwright.schemas import Schedule, ScheduleEntry

    def bogus(req, graph, opts, locked=None):
        entries = [ScheduleEntry(task=t.id, robot=req.robots_of_type(t.robot_type)[0].id,
                                 start_h=0, end_h=t.duration_h) for t in graph.tasks]
        return Schedule(entries=entries, makespan_h=max(e.end_h for e in entries),
                        status="feasible")

    monkeypatch.setattr(loop_module, "solve_schedule", bogus)
    client = Scripted({("taskgraph", 1): FEASIBLE})
    with pytest.raises(RuntimeError, match="re-verification"):
        planner(client).plan_from_requirements(REQ, key="t")


# --------------------------------------------------------------------------- on_step
def test_on_step_reports_each_step_as_it_happens_with_its_round():
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE,
                       ("explain", 1): {"text": "two excavators share the dig"}})
    seen: list[tuple[str, int, bool, int]] = []
    reported: list[LoopStep] = []

    def on_step(step: LoopStep) -> None:
        seen.append((step.stage, step.attempt, step.ok, len(client.calls)))  # calls made so far
        reported.append(step)

    result = Planner(client, on_step=on_step).plan_from_requirements(REQ, key="k")

    assert result.success and result.repairs == 1
    assert seen == [("taskgraph", 1, True, 1), ("validate", 1, True, 1), ("schedule", 1, False, 1),
                    ("repair", 2, True, 2), ("validate", 2, True, 2), ("schedule", 2, True, 2),
                    ("explain", 1, True, 3)]
    assert len(reported) == len(result.steps)
    assert all(a is b for a, b in zip(reported, result.steps))  # same objects, same order


def test_plan_reports_the_structure_step_before_asking_for_the_task_graph():
    client = Scripted({("structure", 1): STRUCTURE, ("taskgraph", 1): FEASIBLE})
    seen: list[tuple[str, int]] = []
    result = Planner(client, explain=False,
                     on_step=lambda s: seen.append((s.stage, len(client.calls)))
                     ).plan("Build a pad.", brief_id="k")

    assert result.success and result.requirements.source_brief == "Build a pad."
    assert seen == [("structure", 1), ("taskgraph", 2), ("validate", 2), ("schedule", 2)]
    assert [s.stage for s in result.steps] == ["structure", "taskgraph", "validate", "schedule"]

    # a brief the model cannot structure is reported too, and the run ends there
    client = Scripted({("structure", 1): schema_error("structure")})
    reported: list[LoopStep] = []
    result = Planner(client, on_step=reported.append).plan("Build a pad.", brief_id="k")
    assert not result.success and result.failure is not None and result.failure.kind == "schema"
    assert [s.stage for s in reported] == ["structure"] and reported == result.steps


def test_on_step_exceptions_propagate():
    client = Scripted({("taskgraph", 1): FEASIBLE})

    def boom(step: LoopStep) -> None:
        raise RuntimeError(f"listener gone at {step.stage}")

    with pytest.raises(RuntimeError, match="listener gone at taskgraph"):
        Planner(client, explain=False, on_step=boom).plan_from_requirements(REQ, key="k")


# --------------------------------------------------------------------------- schema conflict ids
def test_top_level_schema_errors_get_a_dollar_conflict_id():
    whole_answer_wrong = SchemaMismatch(
        "taskgraph (m): answer failed validation after 2 attempt(s)", "[]",
        [": Input should be a valid dictionary", "tasks: Field required"])
    client = Scripted({("taskgraph", 1): whole_answer_wrong})
    result = planner(client, max_repairs=0).plan_from_requirements(REQ, key="k")
    assert result.failure is not None and result.failure.kind == "schema"
    assert result.failure.conflicts == ["schema:$", "schema:tasks"]

    client = Scripted({("structure", 1): SchemaMismatch(
        "structure (m): answer failed validation", "42", [": Input should be a valid dictionary"])})
    result = planner(client).plan("Build a pad.", brief_id="k")
    assert result.steps[0].failure is not None
    assert result.steps[0].failure.conflicts == ["schema:$"]
