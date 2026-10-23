"""Planner loop tests with scripted LLM clients (no fixtures, no network).

The tight_battery fixtures supply a graph CP-SAT rejects (``taskgraph_1``: energy core on
EX1/EX2) and its feasible repair (``taskgraph_2``); the scripted client decides what the
"model" answers, or raises, on each (role, attempt). Covered: the two budgets (verdict rounds
versus answer re-asks) in both loop conditions, the feedback modes, solver-first runs
(``start_with_prior``) with escalation, the scope rules of a replan (strict first answer,
re-scoping repairs, locked tasks), the step payloads (verification receipt, repair diff,
advisories), the solver-grounded suggestions a failed run ends with
(``PlanResult.suggestions`` and the final failed step's ``suggestions`` payload), the copy
of a rejected answer's step (the client's own account when it attached one, the
``schema:json`` / ``schema:shape`` ids otherwise), what a repair round is told about the
graph it repairs (its advisories) and the whole-plan flags the validator gets.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from moonwright.llm import prompts
from moonwright.llm.base import (
    FIXTURES_DIR,
    FixtureMissing,
    LLMError,
    SchemaMismatch,
    Usage,
    answer_failure,
    validate_into,
)
from moonwright.pipeline import loop as loop_module
from moonwright.pipeline.loop import (
    RECHECK_NOTE,
    Planner,
    check_locked_tasks,
    graph_diff,
    verification_receipt,
)
from moonwright.pipeline.rates import UNGROUNDED_ADVISORY
from moonwright.pipeline.schedule import SolveOptions, budget_rows, sequence_travel
from moonwright.pipeline.taskgraph import generate_task_graph
from moonwright.schemas import (
    Failure,
    LoopStep,
    PlanResult,
    Requirements,
    ScheduleEntry,
    Suggestion,
    Task,
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


def planner(client: Scripted, max_repairs: int = 3, **options: Any) -> Planner:
    return Planner(client, max_repairs=max_repairs, explain=False, **options)


def failure_block(prompt: str) -> dict[str, Any]:
    """The FAILURE object of a repair prompt: the JSON (indented, never a blank line inside)
    up to the next block (ADVISORIES, CONTEXT) or the end."""
    return json.loads(prompt.split("FAILURE:\n", 1)[1].split("\n\n", 1)[0])


def attached(kind: str, hint: str, details: list[str], conflicts: list[str] | None = None
             ) -> LLMError:
    """A live client's exception with the failure it wrote itself riding on it (the Token
    Factory client's "Rejected answers" contract)."""
    exc: LLMError = (SchemaMismatch(hint, "{}", ["tasks.2.after: Input should be a valid list"])
                     if kind == "schema" else LLMError(hint))
    exc.failure = Failure(kind=kind, hint=hint, details=details,  # type: ignore[attr-defined]
                          conflicts=conflicts or [])
    return exc


ULTRA_CUT = ("Nemotron 3 Ultra did not return a valid task graph after 3 asks: the answer was "
             "cut off at 32,768 tokens, then at 65,536 tokens, then failed the schema")
ULTRA_LINES = ["ask 1: cut off at 32,768 tokens", "ask 2: cut off at 65,536 tokens",
               "ask 3 (reasoning off): task T3: 'after' must be a list"]
TIMED_OUT = ("The Token Factory request to Nemotron 3 Ultra failed: APITimeoutError (Request "
             "timed out.)")


# --------------------------------------------------------------------------- rejected answers
def test_rejected_repair_answer_is_retried_with_the_same_verdict():
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error(),
                       ("repair", 3): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")

    assert result.success and result.failure is None
    assert result.repairs == 1   # one verdict round, answered after one re-ask
    assert client.calls == [("taskgraph", 1), ("repair", 2), ("repair", 3)]
    # validate / schedule carry the round of the graph they judged (1, then round 3's answer)
    assert trace(result) == [("taskgraph", 1, True), ("validate", 1, True), ("schedule", 1, False),
                             ("repair", 2, False), ("repair", 3, True), ("validate", 3, True),
                             ("schedule", 3, True)]
    rejected = result.steps[3]
    assert rejected.failure is not None and rejected.failure.kind == "schema"
    assert rejected.failure.conflicts == ["schema:shape"]   # a schema violation, by kind
    assert rejected.failure.details == ["tasks.0.duration_h: Input should be greater than 0"]
    # round three saw the solver's verdict again, the last valid graph, and why round two died
    third = client.prompts[2]
    assert '"energy:EX1"' in third and "PREVIOUS TASK GRAPH" in third
    assert "rejected and discarded" in third and "Input should be greater than 0" in third
    assert result.task_graph == TaskGraph.model_validate(FEASIBLE)


def test_transport_error_in_a_repair_round_is_retried():
    client = Scripted({("taskgraph", 1): INFEASIBLE,
                       ("repair", 2): LLMError("repair (m): APITimeoutError: Request timed out."),
                       ("repair", 3): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success and result.repairs == 1
    assert result.steps[3].failure is not None and result.steps[3].failure.kind == "llm_error"
    assert "Request timed out" in client.prompts[2]


def test_answer_retries_are_bounded_per_round_and_the_verdict_is_what_gets_reported():
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error(),
                       ("repair", 3): schema_error(), ("repair", 4): schema_error()})
    result = planner(client, max_repairs=3).plan_from_requirements(REQ, key="k")

    # the round was re-asked twice (the default) and never produced a graph: the run ends
    # there, no further verdict round is opened, and the round counts as the one repair spent
    assert not result.success and result.repairs == 1
    assert client.calls == [("taskgraph", 1), ("repair", 2), ("repair", 3), ("repair", 4)]
    assert result.task_graph == TaskGraph.model_validate(INFEASIBLE)   # last valid graph
    assert result.failure is not None and result.failure.kind == "infeasible"
    assert set(result.failure.conflicts) == ENERGY_CORE
    # the rejection note is addressed to the model: every re-ask carries it once (it never
    # stacks), the failure the user sees is the bare solver verdict
    assert "rejected and discarded" not in result.failure.hint
    assert [p.count("rejected and discarded") for p in client.prompts] == [0, 0, 1, 1]

    # the re-ask budget is a planner option, independent of the repair budget
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error()})
    result = planner(client, max_repairs=3, max_answer_retries=0).plan_from_requirements(
        REQ, key="k")
    assert not result.success and result.repairs == 1 and len(client.calls) == 2
    assert result.failure is not None and set(result.failure.conflicts) == ENERGY_CORE


def test_rejected_first_graph_is_regenerated_without_spending_a_repair():
    client = Scripted({("taskgraph", 1): schema_error("taskgraph"), ("taskgraph", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success and result.repairs == 0
    assert client.calls == [("taskgraph", 1), ("taskgraph", 2)]
    assert trace(result) == [("taskgraph", 1, False), ("taskgraph", 2, True),
                             ("validate", 2, True), ("schedule", 2, True)]
    assert "CONTEXT:" in client.prompts[1] and "Input should be greater than 0" in client.prompts[1]
    assert "CONTEXT:" not in client.prompts[0]


def test_loop_off_re_asks_rejected_answers_but_never_repairs():
    # max_repairs=0 is the "repair loop off" condition: a broken answer is still re-asked,
    # because a JSON failure is not infeasibility ...
    client = Scripted({("taskgraph", 1): schema_error("taskgraph"), ("taskgraph", 2): FEASIBLE})
    result = planner(client, max_repairs=0).plan_from_requirements(REQ, key="k")
    assert result.success and result.repairs == 0
    assert client.calls == [("taskgraph", 1), ("taskgraph", 2)]

    # ... up to max_answer_retries times
    client = Scripted({("taskgraph", k): schema_error("taskgraph") for k in (1, 2, 3)})
    result = planner(client, max_repairs=0).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 0 and len(client.calls) == 3
    assert result.failure is not None and result.failure.kind == "schema"

    # a verdict, on the other hand, is final
    client = Scripted({("taskgraph", 1): INFEASIBLE})
    result = planner(client, max_repairs=0).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 0 and client.calls == [("taskgraph", 1)]
    assert result.failure is not None and set(result.failure.conflicts) == ENERGY_CORE


def test_planner_options_are_validated():
    with pytest.raises(ValueError):
        Planner(Scripted({}), max_repairs=-1)
    with pytest.raises(ValueError):
        Planner(Scripted({}), max_answer_retries=-1)
    with pytest.raises(ValueError, match="feedback_mode"):
        Planner(Scripted({}), feedback_mode="hint")  # type: ignore[arg-type]
    assert Planner(Scripted({})).max_answer_retries == 2
    assert Planner(Scripted({})).feedback_mode == "core"


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


# --------------------------------------------------------------------------- feedback modes
def test_verdict_feedback_mode_blanks_the_core_for_the_model_only():
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    result = planner(client, feedback_mode="verdict").plan_from_requirements(REQ, key="k")
    assert result.success and result.repairs == 1
    assert failure_block(client.prompts[1]) == {"kind": "infeasible", "conflicts": [],
                                                "hint": "", "details": []}
    assert "energy:EX1" not in client.prompts[1]

    # the core mode (default) sends the conflict ids and the numbers behind them
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    planner(client).plan_from_requirements(REQ, key="k")
    block = failure_block(client.prompts[1])
    assert set(block["conflicts"]) == ENERGY_CORE and "Wh" in block["hint"]

    # whatever the mode, the user sees the full verdict, also when the round is lost
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error()})
    result = planner(client, max_repairs=1, feedback_mode="verdict",
                     max_answer_retries=0).plan_from_requirements(REQ, key="k")
    assert not result.success and result.failure is not None
    assert set(result.failure.conflicts) == ENERGY_CORE and "Wh" in result.failure.hint
    assert "rejected and discarded" not in result.failure.hint
    # the verdict step of the trace keeps the core as well
    verdict = next(s for s in result.steps if s.stage == "schedule")
    assert verdict.failure is not None and set(verdict.failure.conflicts) == ENERGY_CORE


def test_a_failed_schedule_step_hands_the_ui_the_budget_rows_behind_its_hint():
    """The figures the energy hint quotes ride on the failed ``schedule`` step's payload as
    :func:`budget_rows` (``budget``): on every energy verdict, whether or not a repair follows,
    next to the advice when the step is final (the suggestions tests). No other step carries
    them, and they are a UI channel: the repair prompt gets the Failure only, and in
    ``verdict`` mode the model learns the kind while the trace still shows the rows."""
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success
    verdict = result.steps[2]
    assert verdict.stage == "schedule" and not verdict.ok and verdict.failure is not None
    assert verdict.payload == {"budget": budget_rows(REQ, PRIOR_INFEASIBLE, verdict.failure)}
    [row] = verdict.payload["budget"]
    assert row == {"kind": "type", "subject": "excavator", "robots": ["EX1", "EX2"],
                   "budget_wh": 10000.0, "demanded_wh": 9929.649, "travel_wh": 29.649}
    for key in ("budget_wh", "demanded_wh", "travel_wh"):   # the sentence quotes them, rounded
        assert f"{round(row[key]):,} Wh" in verdict.failure.hint
    assert all("budget" not in (s.payload or {}) for s in result.steps if s is not verdict)
    assert "demanded_wh" not in client.prompts[1]
    assert PlanResult.model_validate_json(result.model_dump_json()) == result

    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    result = planner(client, feedback_mode="verdict").plan_from_requirements(REQ, key="k")
    assert failure_block(client.prompts[1]) == {"kind": "infeasible", "conflicts": [],
                                                "hint": "", "details": []}
    assert "demanded_wh" not in client.prompts[1]
    assert set(result.steps[2].payload or {}) == {"budget"}


# --------------------------------------------------------------------------- required tasks
REMAINING = TaskGraph.model_validate(FEASIBLE)   # stands in for a replan's remaining graph


def test_required_tasks_are_fed_back_as_missing_conflicts():
    """Replanning: a graph that drops a remaining task is rejected and repaired."""
    required = [t["id"] for t in FEASIBLE["tasks"]]
    client = Scripted({("taskgraph", 1): without(FEASIBLE, "T7"), ("repair", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL2 failed",
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
        REQ, key="k", prior_graph=REMAINING, variant="replan",
        required_tasks=[t["id"] for t in FEASIBLE["tasks"]])
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
    payload = result.steps[-1].payload
    assert payload is not None and result.schedule is not None
    assert payload["makespan_h"] == result.schedule.makespan_h
    assert payload["status"] == "optimal" and payload["verified"] is True
    assert payload["verification"]["recheck"] == RECHECK_NOTE


# --------------------------------------------------------------------------- replanning (live)
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
    assert "REMAINING TASK GRAPH:" in prompt and "CONTEXT:\nHL2 failed at 12 h" in prompt
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


def test_retyping_a_running_task_is_a_verdict_on_both_paths_never_a_crash():
    """A task underway on EX1 whose robot_type the model changed used to reach the solver,
    which pinned it anyway, and the re-verification then raised. The model's first answer is
    held to the remaining graph (``changed:``); a repair may re-scope the other tasks but a
    locked one is refused as ``locked:``, before the solver either way."""
    retyped = {"tasks": [dict(t, robot_type="hauler") if t["id"] == "T2" else t
                         for t in FEASIBLE["tasks"]]}
    client = Scripted({("taskgraph", 1): retyped, ("repair", 2): retyped})
    locked = [ScheduleEntry(task="T2", robot="EX1", start_h=0, end_h=REMAINING.task("T2").duration_h)]
    result = planner(client, max_repairs=1).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL1 failed", variant="replan",
        locked=locked, required_tasks=REMAINING.ids())
    assert not result.success and result.repairs == 1
    assert [s.stage for s in result.steps] == ["taskgraph", "validate", "repair", "validate"]
    first = result.steps[1].failure
    assert first is not None and first.conflicts[0] == "changed:T2"
    assert first.details[0] == "T2 changed: robot_type hauler (was excavator)"
    # the repair round: the scope verdict names the lock; the validator's own view follows
    assert result.failure is not None and result.failure.kind == "validation"
    assert result.failure.conflicts[0] == "locked:T2"
    assert result.failure.details[0] == ("T2 is underway on EX1 and changed: robot_type hauler "
                                         "(was excavator)")
    assert "energy:hauler" in result.failure.conflicts
    assert "running task(s) T2" in result.failure.hint


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


# --------------------------------------------------------------------------- solver-first
PRIOR_INFEASIBLE = TaskGraph.model_validate(INFEASIBLE)


def test_start_with_prior_schedules_a_feasible_prior_without_any_model_call():
    client = Scripted({})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL2 failed", variant="replan",
        required_tasks=REMAINING.ids(), start_with_prior=True)
    assert result.success and result.repairs == 0 and client.calls == []
    assert trace(result) == [("validate", 1, True), ("schedule", 1, True)]
    assert result.task_graph == REMAINING and result.schedule is not None
    assert result.steps[0].message == f"{len(REMAINING.tasks)} tasks valid"

    with pytest.raises(ValueError, match="prior_graph"):
        planner(client).plan_from_requirements(REQ, key="k", start_with_prior=True)


def test_start_with_prior_escalates_only_on_a_verdict_and_lets_the_repair_rescope():
    client = Scripted({("repair", 2): FEASIBLE})
    result = planner(client, max_repairs=2).plan_from_requirements(
        REQ, key="k", prior_graph=PRIOR_INFEASIBLE, context="EX3 failed", variant="replan",
        required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True)

    assert result.success and result.repairs == 1
    assert client.calls == [("repair", 2)]   # attempt 1 was the prior graph itself
    assert trace(result) == [("validate", 1, True), ("schedule", 1, False), ("repair", 2, True),
                             ("validate", 2, True), ("schedule", 2, True)]
    verdict = result.steps[1].failure
    assert verdict is not None and set(verdict.conflicts) == ENERGY_CORE
    prompt = client.prompts[0]
    assert "PREVIOUS TASK GRAPH" in prompt and '"energy:EX1"' in prompt and "EX3 failed" in prompt
    assert "REMAINING TASK GRAPH" in prompt
    # the repair re-scoped (and renumbered) the work, which is what it was asked for: it is
    # accepted, and the repair step's diff says exactly what changed: the one-pass bowl (T2)
    # became two halves, the key trench (T3, now T4) was cut down, every later id shifted by
    # one, and no compactor or hauler moved
    diff = result.steps[2].payload["diff"]
    assert diff["removed"] == ["T2"] and diff["added"] == ["T2", "T3"]
    assert diff["renumbered"] == [{"from": f"T{i}", "to": f"T{i + 1}"} for i in range(3, 11)]
    assert diff["scope_drift_pct"] == -10.8
    assert diff["per_type_energy_before"]["excavator"] == 9900.0
    assert diff["per_type_energy_after"]["excavator"] == 8200.0
    assert diff["changed"] == [
        {"task": "T4", "from": "T3", "field": "name",
         "before": "Cut berm key trench and borrow pit",
         "after": "Cut berm key trench (shallow cut, berm fill from pad spoil)"},
        {"task": "T4", "from": "T3", "field": "duration_h", "before": 24, "after": 14},
        {"task": "T4", "from": "T3", "field": "energy_wh", "before": 4000.0, "after": 2100.0},
    ]
    assert diff["total_energy_wh_before"] == 15700.0 and diff["total_energy_wh_after"] == 14000.0
    assert result.task_graph == TaskGraph.model_validate(FEASIBLE)


def test_a_repair_may_not_redefine_a_locked_task():
    locked = [ScheduleEntry(task="T2", robot="EX1", start_h=0, end_h=24)]
    client = Scripted({("repair", 2): FEASIBLE})
    result = planner(client, max_repairs=1).plan_from_requirements(
        REQ, key="k", prior_graph=PRIOR_INFEASIBLE, variant="replan",
        required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True, locked=locked)
    assert not result.success and result.repairs == 1
    assert trace(result) == [("validate", 1, True), ("schedule", 1, False), ("repair", 2, True),
                             ("validate", 2, False)]
    assert result.failure is not None and result.failure.kind == "validation"
    assert result.failure.conflicts == ["locked:T2"]
    assert result.failure.details == [
        "T2 is underway on EX1 and changed: duration_h 14 (needs 24); energy_wh 2,100 (needs 4,000)"]
    assert "running task(s) T2" in result.failure.hint

    # the guard on its own: intact locks pass, a lock on a redefined task does not
    assert check_locked_tasks(REMAINING, REMAINING, locked) is None
    assert check_locked_tasks(REMAINING, PRIOR_INFEASIBLE, []) is None
    assert check_locked_tasks(REMAINING, None, locked) is None
    guard = check_locked_tasks(REMAINING, PRIOR_INFEASIBLE, locked)
    assert guard is not None and guard.conflicts == ["locked:T2"]


def test_a_locked_task_is_judged_by_its_id_however_the_diff_pairs_it():
    """The scope guards read ids, the diff pairs tasks on a stable identity: a running task
    the repair renamed and cut is ``locked:T2`` whether the diff pairs the two definitions by
    their slot (same id, new name, same place) or by the id the model kept (same id, new
    name, new zone); either way the rename, the cut and the move are ``changed`` rows."""
    locked = [ScheduleEntry(task="T2", robot="EX1", start_h=0, end_h=24)]
    renamed = {"tasks": [dict(t, name="Excavate pad bowl (full depth)", duration_h=20,
                              energy_wh=3300) if t["id"] == "T2" else t
                         for t in INFEASIBLE["tasks"]]}
    rezoned = {"tasks": [dict(t, zone="Z4") if t["id"] == "T2" else t
                         for t in renamed["tasks"]]}
    cut = ["duration_h 20 (needs 24)", "energy_wh 3,300 (needs 4,000)"]
    rows = [{"task": "T2", "field": "name",
             "before": next(t["name"] for t in INFEASIBLE["tasks"] if t["id"] == "T2"),
             "after": "Excavate pad bowl (full depth)"},
            {"task": "T2", "field": "duration_h", "before": 24, "after": 20},
            {"task": "T2", "field": "energy_wh", "before": 4000.0, "after": 3300.0}]
    for answer, why, diff_lists in (
        (renamed, cut, {"added": [], "removed": [], "renumbered": [], "changed": rows}),
        (rezoned, ["zone Z4 (was Z1)", *cut],
         {"added": [], "removed": [], "renumbered": [], "changed": [
             *rows, {"task": "T2", "field": "zone", "before": "Z1", "after": "Z4"}]}),
    ):
        client = Scripted({("repair", 2): answer})
        result = planner(client, max_repairs=1).plan_from_requirements(
            REQ, key="k", prior_graph=PRIOR_INFEASIBLE, variant="replan",
            required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True, locked=locked)
        assert not result.success and result.failure is not None
        assert result.failure.conflicts[0] == "locked:T2"
        assert result.failure.details[0] == ("T2 is underway on EX1 and changed: "
                                             + "; ".join(why))
        diff = result.steps[2].payload["diff"]
        assert {k: diff[k] for k in diff_lists} == diff_lists
        guard = check_locked_tasks(TaskGraph.model_validate(answer), PRIOR_INFEASIBLE, locked)
        assert guard is not None and guard.conflicts == ["locked:T2"]


def test_an_answer_that_repeats_an_id_carries_no_diff():
    """The diff is attached before validation; an answer with two ``T2`` would list the id as
    kept and added at once, so such an answer gets no diff and the validate step speaks."""
    doubled = {"tasks": [dict(t, id="T2") if t["id"] == "T3" else t for t in INFEASIBLE["tasks"]]}
    client = Scripted({("repair", 2): doubled})
    result = planner(client, max_repairs=1).plan_from_requirements(
        REQ, key="k", prior_graph=PRIOR_INFEASIBLE, variant="replan",
        required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True)
    repair = result.steps[2]
    assert repair.stage == "repair" and repair.ok and "diff" not in (repair.payload or {})
    assert not result.success and result.failure is not None
    assert "duplicate:T2" in result.failure.conflicts


# --------------------------------------------------------------------------- payloads
def test_verification_receipt_describes_what_was_proved():
    client = Scripted({("taskgraph", 1): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    step = result.steps[-1]
    assert step.stage == "schedule" and step.ok and step.payload is not None
    graph, schedule = result.task_graph, result.schedule
    assert graph is not None and schedule is not None
    receipt = step.payload["verification"]
    assert set(receipt) == {"precedence_edges", "robot_rows", "zone_groups", "battery",
                            "tightest_robot", "horizon_used_h", "horizon_h", "status",
                            "objective_bound_h", "solve_time_s", "travel_allowance", "recheck"}
    assert receipt["precedence_edges"] == sum(len(set(t.after)) for t in graph.tasks) == 11
    assert receipt["robot_rows"] == len(REQ.robots) == 5
    assert receipt["zone_groups"] == len({t.zone for t in graph.tasks})
    assert [b["robot"] for b in receipt["battery"]] == [r.id for r in REQ.robots]
    # used = work + the legs the robot's sequence drives (first one from the depot), the
    # figure a replay spends; CP-SAT reserved at least that much
    travel = sequence_travel(REQ, graph, schedule, SolveOptions())
    for row in receipt["battery"]:
        robot = REQ.robot(row["robot"])
        work = sum(graph.task(e.task).energy_wh for e in schedule.entries if e.robot == robot.id)
        legs = travel[robot.id].mwh / 1000 if robot.id in travel else 0.0
        assert row["work_wh"] == pytest.approx(work) and row["travel_wh"] == pytest.approx(legs)
        assert row["used_wh"] == pytest.approx(work + legs) and row["budget_wh"] == robot.battery_wh
        assert row["pct"] == pytest.approx(100 * (work + legs) / robot.battery_wh, abs=0.05)
    cp1 = next(b for b in receipt["battery"] if b["robot"] == "CP1")  # the only compactor
    assert (cp1["work_wh"], cp1["budget_wh"]) == (3000.0, 3500.0) and 0 < cp1["travel_wh"] < 40
    assert cp1["pct"] == round(100 * cp1["used_wh"] / 3500, 1) > 85.7
    assert receipt["tightest_robot"] == max(receipt["battery"], key=lambda b: b["pct"])["robot"]
    assert receipt["horizon_used_h"] == schedule.makespan_h and receipt["horizon_h"] == REQ.horizon_h
    assert receipt["status"] == schedule.status == "optimal"
    assert receipt["objective_bound_h"] == schedule.objective_bound_h
    assert receipt["solve_time_s"] == round(schedule.solve_time_s, 3)
    assert isinstance(receipt["travel_allowance"], bool) and receipt["recheck"] == RECHECK_NOTE
    assert step.payload["verified"] is True and step.payload["makespan_h"] == schedule.makespan_h
    assert verification_receipt(REQ, graph, schedule) == receipt


def test_repair_diff_lists_every_changed_field():
    before = TaskGraph(tasks=[
        Task(id="T1", name="dig", robot_type="excavator", duration_h=10, energy_wh=4000, zone="Z1"),
        Task(id="T2", name="haul", robot_type="hauler", duration_h=6, energy_wh=900, zone="Z1",
             after=["T1"]),
        Task(id="T3", name="gone", robot_type="hauler", duration_h=2, energy_wh=100, zone="Z2"),
    ])
    after = TaskGraph(tasks=[
        Task(id="T1", name="dig", robot_type="excavator", duration_h=5, energy_wh=2000, zone="Z1",
             quantity=60, unit="m3", basis="60 m3 bowl"),
        Task(id="T2", name="haul", robot_type="excavator", duration_h=6, energy_wh=900.0004,
             zone="Z3"),
        Task(id="T4", name="new", robot_type="hauler", duration_h=1, energy_wh=50, zone="Z3"),
    ])   # T4 is not T3 renamed: a new name in another zone is an added task (test_graph_diff)
    diff = graph_diff(before, after)
    assert diff["added"] == ["T4"] and diff["removed"] == ["T3"] and diff["renumbered"] == []
    assert diff["changed"] == [
        {"task": "T1", "field": "duration_h", "before": 10, "after": 5},
        {"task": "T1", "field": "energy_wh", "before": 4000.0, "after": 2000.0},
        {"task": "T1", "field": "quantity", "before": None, "after": 60.0},
        {"task": "T1", "field": "unit", "before": None, "after": "m3"},
        {"task": "T2", "field": "robot_type", "before": "hauler", "after": "excavator"},
        {"task": "T2", "field": "zone", "before": "Z1", "after": "Z3"},
        {"task": "T2", "field": "after", "before": ["T1"], "after": []},
    ]   # 900.0004 Wh is 900 Wh to the milli-Wh the solver uses: not a change
    assert diff["per_type_energy_before"] == {"excavator": 4000.0, "hauler": 1000.0}
    assert diff["per_type_energy_after"] == {"excavator": 2900.0, "hauler": 50.0}
    assert diff["total_energy_wh_before"] == 5000.0 and diff["total_energy_wh_after"] == 2950.0
    assert diff["scope_drift_pct"] == -41.0
    assert diff["total_duration_h_before"] == 18 and diff["total_duration_h_after"] == 12

    same = graph_diff(before, before)
    assert (same["added"], same["removed"], same["changed"], same["renumbered"],
            same["scope_drift_pct"]) == ([], [], [], [], 0.0)


def test_advisories_of_the_graph_step_ride_on_its_validate_step(monkeypatch):
    real = loop_module.generate_task_graph

    def noisy(*args, **kwargs):
        graph, step = real(*args, **kwargs)
        if graph is not None:
            note = f"estimate:{graph.tasks[0].id}: model said 4 h, rate model 6 h (+50%)"
            step.payload = {**(step.payload or {}), "advisories": [note]}
        return graph, step

    monkeypatch.setattr(loop_module, "generate_task_graph", noisy)
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success
    validates = [s.payload for s in result.steps if s.stage == "validate"]
    assert validates == [{"advisories": ["estimate:T1: model said 4 h, rate model 6 h (+50%)"]}] * 2

    # a graph that states no quantity is marked ungrounded, on its validate step as well
    monkeypatch.setattr(loop_module, "generate_task_graph", real)
    client = Scripted({("taskgraph", 1): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert [s.payload for s in result.steps if s.stage == "validate"] == \
        [{"advisories": [UNGROUNDED_ADVISORY]}]
    assert result.steps[0].payload == {"advisories": [UNGROUNDED_ADVISORY], "rate_model": True,
                                       "grounded": 0}

    # without advisories the validate step carries no payload
    def quiet(*args, **kwargs):
        graph, step = real(*args, **kwargs)
        if step.payload is not None:
            step.payload = {**step.payload, "advisories": []}
        return graph, step

    monkeypatch.setattr(loop_module, "generate_task_graph", quiet)
    result = planner(Scripted({("taskgraph", 1): FEASIBLE})).plan_from_requirements(REQ, key="k")
    assert [s.payload for s in result.steps if s.stage == "validate"] == [None]


# --------------------------------------------------------------------------- re-verification
def test_verified_schedule_step_carries_the_flag():
    client = Scripted({("taskgraph", 1): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="t")
    assert result.success and result.schedule is not None
    step = [s for s in result.steps if s.stage == "schedule"][-1]
    assert step.payload is not None
    assert step.payload["verified"] is True
    assert step.payload["makespan_h"] == result.schedule.makespan_h


def test_scheduler_output_is_re_verified(monkeypatch):
    """A schedule that violates the constraints must never leave the planner as a plan."""
    from moonwright.schemas import Schedule

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


def test_on_step_sees_the_diff_and_the_receipt_when_the_step_is_reported():
    seen: dict[str, Any] = {}

    def on_step(step: LoopStep) -> None:
        if step.stage == "repair":
            seen["diff"] = (step.payload or {}).get("diff")
        if step.stage == "schedule" and step.ok:
            seen["receipt"] = (step.payload or {}).get("verification")

    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    Planner(client, explain=False, on_step=on_step).plan_from_requirements(REQ, key="k")
    assert seen["diff"]["added"] == ["T2", "T3"] and seen["receipt"]["recheck"] == RECHECK_NOTE


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


# --------------------------------------------------------------------------- rejected answers
def test_schema_failures_of_a_plain_client_get_the_json_or_shape_conflict_id():
    """A client that raises a bare SchemaMismatch (offline, scripted) keeps its hint and
    details; its conflict ids are the two kinds the UI chips know, ``schema:shape`` for a
    schema violation (``schema:$`` and ``schema:<field>`` were the old ids) and
    ``schema:json`` for a detail about the JSON itself."""
    whole_answer_wrong = SchemaMismatch(
        "taskgraph (m): answer failed validation after 2 attempt(s)", "[]",
        [": Input should be a valid dictionary", "tasks: Field required"])
    client = Scripted({("taskgraph", 1): whole_answer_wrong})
    result = planner(client, max_repairs=0, max_answer_retries=0).plan_from_requirements(
        REQ, key="k")
    assert result.failure is not None and result.failure.kind == "schema"
    assert result.failure.conflicts == ["schema:shape"]
    assert result.failure.hint == "taskgraph (m): answer failed validation after 2 attempt(s)"
    assert result.failure.details == [": Input should be a valid dictionary", "tasks: Field required"]

    client = Scripted({("structure", 1): SchemaMismatch(
        "structure (m): answer failed validation", "42", [": Input should be a valid dictionary"])})
    result = planner(client).plan("Build a pad.", brief_id="k")
    assert result.steps[0].failure is not None
    assert result.steps[0].failure.conflicts == ["schema:shape"]

    cut = SchemaMismatch("taskgraph (m): answer failed validation after 2 attempt(s)", "",
                         ["json: the answer was cut off at max_tokens=4096 (empty completion)"])
    client = Scripted({("taskgraph", 1): cut})
    result = planner(client, max_answer_retries=0).plan_from_requirements(REQ, key="k")
    assert result.failure is not None and result.failure.conflicts == ["schema:json"]

    # the stages' mapping on its own (moonwright.llm.base.answer_failure): every kind once, in
    # order; nothing to go on means a violation; a failed request keeps its words (and an
    # offline miss its lookup facts); the account a client that knows its asks attached wins
    mixed = SchemaMismatch("m", "", ["json: no JSON object found in model output",
                                     "tasks: Field required", ": Input should be a valid dictionary"])
    assert answer_failure(mixed).conflicts == ["schema:json", "schema:shape"]
    assert answer_failure(mixed).details == mixed.errors and answer_failure(mixed).hint == "m"
    assert answer_failure(SchemaMismatch("m", "", [])).conflicts == ["schema:shape"]
    assert answer_failure(LLMError("repair (m): APITimeoutError: Request timed out.")) == Failure(
        kind="llm_error", hint="repair (m): APITimeoutError: Request timed out.")
    assert answer_failure(FixtureMissing("no fixture", ["searched fixtures/k"])) == Failure(
        kind="llm_error", hint="no fixture", details=["searched fixtures/k"])
    own = attached("schema", ULTRA_CUT, ULTRA_LINES, ["schema:json", "schema:shape"])
    assert answer_failure(own) == own.failure


def test_a_client_that_accounts_for_its_asks_writes_the_failed_step():
    """The Token Factory client attaches the failure a person reads to its exception (the
    model's short name, one line per ask, the two conflict ids); the planner shows it on the
    rejected step, in the result of a run that never got a graph, in the note the model gets
    on the re-ask, and on the structure step alike. The failure the run reports after a lost
    repair round stays the verdict it was answering."""
    error = attached("schema", ULTRA_CUT, ULTRA_LINES, ["schema:json", "schema:shape"])
    client = Scripted({("taskgraph", k): error for k in (1, 2, 3)})
    seen: list[str] = []
    result = planner(client, on_step=lambda s: seen.append(s.failure.hint if s.failure else "")
                     ).plan_from_requirements(REQ, key="k")
    assert not result.success and result.task_graph is None
    assert result.failure == Failure(kind="schema", conflicts=["schema:json", "schema:shape"],
                                     hint=ULTRA_CUT, details=ULTRA_LINES)
    assert [s.failure for s in result.steps] == [result.failure] * 3
    assert seen == [ULTRA_CUT] * 3   # the account is on the step when on_step sees it
    assert "cut off at 32,768 tokens" in client.prompts[1] and ULTRA_LINES[2] in client.prompts[1]
    assert PlanResult.model_validate_json(result.model_dump_json()) == result

    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): error})
    result = planner(client, max_answer_retries=0).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 1
    assert result.steps[3].failure == error.failure
    assert result.failure is not None and set(result.failure.conflicts) == ENERGY_CORE

    timed_out = attached("llm_error", TIMED_OUT, [
        "APITimeoutError: Request timed out.",
        "repair (nvidia/Nemotron-3-Ultra-550b-a55b) on https://api.tokenfactory.nebius.com/v1/"])
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): timed_out, ("repair", 3): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success and result.steps[3].failure == timed_out.failure
    assert result.steps[3].failure is not None and result.steps[3].failure.kind == "llm_error"
    assert TIMED_OUT in client.prompts[2]

    structure = attached("schema", "Nemotron 3.5 Lightning did not return valid requirements "
                         "after 2 asks: the answer failed the schema twice",
                         ["ask 1: 'robots' is missing",
                          "ask 2: robot HL1: 'battery_wh' must be a number"], ["schema:shape"])
    client = Scripted({("structure", 1): structure})
    result = planner(client).plan("Build a pad.", brief_id="k")
    assert not result.success and [s.stage for s in result.steps] == ["structure"]
    assert result.steps[0].failure == structure.failure and result.failure == structure.failure

    # an offline miss is deterministic and keeps its own words
    client = Scripted({("taskgraph", 1): INFEASIBLE,
                       ("repair", 2): FixtureMissing("missing fixture taskgraph_2.json")})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.failure is not None and result.failure.kind == "llm_error"
    assert result.failure.hint == "missing fixture taskgraph_2.json"


# --------------------------------------------------------------------------- what a repair is told
VOLUME_NOTE = "volume:S2: 600 m3 planned for the 300 m3 berm (200 %)"


def with_note(real):
    """``generate_task_graph`` with one volume advisory on the step of every graph it returns."""
    def noisy(*args, **kwargs):
        graph, step = real(*args, **kwargs)
        if graph is not None:
            step.payload = {**(step.payload or {}), "advisories": [VOLUME_NOTE]}
        return graph, step
    return noisy


def test_repair_rounds_are_told_the_advisories_of_the_graph_they_repair(monkeypatch):
    """The advisories of the step that produced the graph a repair answers reach the repair
    prompt as the ADVISORIES block under FAILURE (``generate_task_graph(advisories=...)`` ->
    ``repair_messages``), on the re-ask after a rejected repair answer too, never on the first
    graph's prompt; a replan's own context follows them."""
    monkeypatch.setattr(loop_module, "generate_task_graph", with_note(loop_module.generate_task_graph))
    block = prompts.ADVISORIES_HEADER + "\n- " + VOLUME_NOTE
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error(),
                       ("repair", 3): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success and result.repairs == 1
    assert VOLUME_NOTE not in client.prompts[0]
    for prompt in client.prompts[1:]:
        assert prompt.endswith("\n\n" + block)                   # no context: the last block
        assert failure_block(prompt)["kind"] == "infeasible"     # the FAILURE block is intact
    assert "rejected and discarded" in failure_block(client.prompts[2])["hint"]
    # the validate steps still carry them for the UI
    assert [s.payload for s in result.steps if s.stage == "validate"] == \
        [{"advisories": [VOLUME_NOTE]}] * 2

    # a replan: the advisories, then its own context
    client = Scripted({("taskgraph", 1): without(FEASIBLE, "T7"), ("repair", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL2 failed", variant="replan",
        required_tasks=[t["id"] for t in FEASIBLE["tasks"]])
    assert result.success
    assert client.prompts[1].endswith("\n\n" + block + "\n\nCONTEXT:\nHL2 failed")


def test_the_stage_renders_the_advisories_of_a_repair_only():
    """``generate_task_graph(advisories=...)`` is real end to end: a repair's prompt carries
    them as the ADVISORIES block between FAILURE and CONTEXT; a first graph has no previous
    graph to warn about, so the argument changes nothing there."""
    client = Scripted({("taskgraph", 1): FEASIBLE, ("repair", 2): FEASIBLE})
    graph, _ = generate_task_graph(client, REQ, key="k", advisories=[VOLUME_NOTE])
    assert graph is not None and VOLUME_NOTE not in client.prompts[0]
    graph, step = generate_task_graph(
        client, REQ, key="k", attempt=2, prior=PRIOR_INFEASIBLE,
        feedback=Failure(kind="infeasible", conflicts=["energy:EX1"]), context="HL2 failed",
        advisories=[VOLUME_NOTE, "estimate:T3: model said 10 h"])
    assert graph is not None and step.stage == "repair"
    assert client.prompts[1].endswith(
        f"\n\n{prompts.ADVISORIES_HEADER}\n- {VOLUME_NOTE}\n- estimate:T3: model said 10 h"
        "\n\nCONTEXT:\nHL2 failed")
    assert failure_block(client.prompts[1])["conflicts"] == ["energy:EX1"]


def test_the_stage_gets_the_advisories_as_its_argument(monkeypatch):
    """The planner hands ``generate_task_graph`` the advisories as ``advisories=``: none on a
    first graph, the producing step's on every ask of a repair round, none in ``verdict``
    feedback mode, none on a solver-first replan's first repair (no step produced its graph),
    and never one whose id the verdict already carries as a conflict (a ``volume:<id>``
    shortfall is an advisory and a conflict at once)."""
    real = loop_module.generate_task_graph
    seen: list[Any] = []

    def spy(*args, advisories=None, **kwargs):
        seen.append(advisories)
        graph, step = real(*args, advisories=advisories, **kwargs)
        if graph is not None:
            step.payload = {**(step.payload or {}), "advisories": [VOLUME_NOTE]}
        return graph, step

    monkeypatch.setattr(loop_module, "generate_task_graph", spy)
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): schema_error(),
                       ("repair", 3): FEASIBLE})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert result.success and seen == [None, [VOLUME_NOTE], [VOLUME_NOTE]]

    seen.clear()
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    planner(client, feedback_mode="verdict").plan_from_requirements(REQ, key="k")
    assert seen == [None, None] and VOLUME_NOTE not in client.prompts[1]

    seen.clear()
    client = Scripted({("repair", 2): FEASIBLE})
    planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=PRIOR_INFEASIBLE, context="EX3 failed", variant="replan",
        required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True)
    assert seen == [None]

    verdict = Failure(kind="validation", conflicts=["volume:S1", "energy:excavator"])
    notes = ["volume:S1: its dimensions give 125.7 m3 (...) total only 63 m3 (50 %)",
             "volume:S2: its dimensions give 300 m3 (...) total 600 m3 (200 %): hauled twice over",
             "estimate:T3: model said 10 h / 1000 Wh, rate model gives 28 h / 3040 Wh (+204%)",
             UNGROUNDED_ADVISORY]
    assert planner(Scripted({}))._repair_notes(notes, verdict) == notes[1:]
    assert planner(Scripted({}))._repair_notes(notes[:1], verdict) is None
    # review: a berm on a key trench is two shapes under ONE id. The trench is short (the
    # verdict's volume:S2) while the berm is hauled twice over (an advisory volume:S2): the
    # excess is no part of the verdict and drives its energy:hauler, so it is kept
    berm = Failure(kind="validation", conflicts=["volume:S2", "energy:hauler"])
    pair = ["volume:S2: its dimensions give 300 m3 (...) total 600 m3 (200 %): hauled twice over",
            "volume:S2: its dimensions give 36 m3 (key trench ...) total only 10 m3 (27 %)"]
    assert planner(Scripted({}))._repair_notes(pair, berm) == pair[:1]


def test_the_validator_is_told_whether_the_volume_check_applies(monkeypatch):
    """``validate_graph(volume=...)``: True for a fresh plan and its repairs, False for a
    replan's remaining graph (the m3 already moved are not in it) and its repairs. A replan's
    robot positions reach the validator too, so its energy checks charge the transit the
    solver charges."""
    real = loop_module.validate_graph
    seen: list[tuple[bool, bool | None, Any]] = []

    def spy(req, graph, previous=None, *, coverage=False, volume=None, robot_positions=None):
        seen.append((coverage, volume, robot_positions))
        return real(req, graph, previous, coverage=coverage, volume=volume,
                    robot_positions=robot_positions)

    monkeypatch.setattr(loop_module, "validate_graph", spy)
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE})
    assert planner(client).plan_from_requirements(REQ, key="k").success
    assert seen == [(True, True, None), (True, True, None)]

    seen.clear()
    positions = {"EX1": (20.0, 18.0), "EX2": (8.0, 22.0)}
    client = Scripted({("repair", 2): FEASIBLE})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=PRIOR_INFEASIBLE, context="EX3 failed", variant="replan",
        required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True, robot_positions=positions)
    assert result.success and seen == [(False, False, positions), (False, False, positions)]

    seen.clear()
    client = Scripted({("taskgraph", 1): FEASIBLE})
    result = planner(client).plan_from_requirements(
        REQ, key="k", prior_graph=REMAINING, context="HL2 failed", variant="replan",
        required_tasks=REMAINING.ids())
    assert result.success and seen == [(False, False, None)]


# --------------------------------------------------------------------------- suggestions
def payloads(seen: list[tuple[str, bool, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [p for _, _, p in seen]


def test_exhausted_run_attaches_solver_grounded_suggestions_before_the_step_is_reported():
    """The repair loop off: the first verdict is final, and the step that carries it already
    holds the advice when ``on_step`` sees it, so a stream client needs no result event."""
    seen: list[tuple[str, bool, dict[str, Any]]] = []
    client = Scripted({("taskgraph", 1): INFEASIBLE})
    result = planner(client, max_repairs=0,
                     on_step=lambda s: seen.append((s.stage, s.ok, dict(s.payload or {})))
                     ).plan_from_requirements(REQ, key="k")

    assert not result.success and result.failure is not None
    assert set(result.failure.conflicts) == ENERGY_CORE
    assert [(s.kind, s.robot_id) for s in result.suggestions] == [
        ("raise_battery", "EX1"), ("raise_battery", "EX2"), ("add_robot", "EX3")]
    for s in result.suggestions:
        assert s.verified and s.makespan_h is not None and s.makespan_h <= REQ.horizon_h
        assert s.robot_type == "excavator" and "Wh" in s.detail and " h" in s.detail
    ex1, ex2, spare = result.suggestions
    assert ex1.battery_wh == ex2.battery_wh and ex1.battery_wh is not None and ex1.battery_wh > 5000
    assert spare.battery_wh == 5000.0
    # the final failed step carries the same list as plain dicts ...
    final = result.steps[-1]
    assert final.stage == "schedule" and not final.ok and final.payload is not None
    dumped = [s.model_dump(mode="json") for s in result.suggestions]
    budget = budget_rows(REQ, PRIOR_INFEASIBLE, result.failure)
    assert final.payload == {"budget": budget, "suggestions": dumped}
    # ... and had it the moment it was reported; no other step was touched
    assert seen[-1] == ("schedule", False, {"budget": budget, "suggestions": dumped})
    assert all("suggestions" not in p for p in payloads(seen)[:-1])
    assert PlanResult.model_validate_json(result.model_dump_json()) == result


def test_replan_infeasibility_is_advised_with_a_spare_or_a_later_deadline_only():
    """Solver-first replan that CP-SAT refuses: a battery cannot be raised mid-mission and the
    remaining work is not re-scoped here, so only extend_horizon / add_robot are tried; the
    energy core makes the spare the one answer."""
    client = Scripted({})
    result = planner(client, max_repairs=0).plan_from_requirements(
        REQ, key="k", prior_graph=PRIOR_INFEASIBLE, context="EX3 failed", variant="replan",
        required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True)
    assert not result.success and client.calls == []
    assert trace(result) == [("validate", 1, True), ("schedule", 1, False)]
    assert [(s.kind, s.robot_type, s.robot_id) for s in result.suggestions] == [
        ("add_robot", "excavator", "EX3")]
    assert result.suggestions[0].verified
    assert result.steps[-1].payload == {
        "budget": budget_rows(REQ, PRIOR_INFEASIBLE, result.failure),
        "suggestions": [result.suggestions[0].model_dump(mode="json")]}

    # the spare never takes the id of a robot the replan lost: the ids come from the caller,
    # or from the "Failed robots (unavailable): ..." sentence of the replan context
    def spare(**kwargs: Any) -> str:
        result = planner(Scripted({}), max_repairs=0).plan_from_requirements(
            REQ, key="k", prior_graph=PRIOR_INFEASIBLE, variant="replan",
            required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True, **kwargs)
        (s,) = result.suggestions
        assert s.kind == "add_robot" and s.robot_id is not None
        return s.robot_id

    assert spare(reserved_ids=["EX3", "EX4"]) == "EX5"
    assert spare(context="Robot EX3 has failed and must not be used again. Failed robots "
                         "(unavailable): EX3, HL2. Time 0 of the new schedule is now.") == "EX4"
    assert spare(context="Failed robots (unavailable): EX3, HL2.", reserved_ids=[]) == "EX3"

    # with repairs allowed, the advice waits for the last verdict of the run
    client = Scripted({("repair", 2): INFEASIBLE})
    result = planner(client, max_repairs=1).plan_from_requirements(
        REQ, key="k", prior_graph=PRIOR_INFEASIBLE, context="EX3 failed", variant="replan",
        required_tasks=PRIOR_INFEASIBLE.ids(), start_with_prior=True)
    assert not result.success and result.repairs == 1
    assert [s.kind for s in result.suggestions] == ["add_robot"]
    verdicts = [s for s in result.steps if s.stage == "schedule"]
    assert len(verdicts) == 2 and set(verdicts[0].payload or {}) == {"budget"}
    assert verdicts[1].payload is not None and "suggestions" in verdicts[1].payload


def test_a_rejected_last_repair_answer_still_ends_with_advice_on_its_step():
    """The run ends on an offline fixture miss: the failure the caller sees is the miss, the
    advice answers the solver's verdict on the last valid graph, and it rides on the final
    failed step (the rejected repair), not on the earlier verdict step."""
    client = Scripted({("taskgraph", 1): INFEASIBLE,
                       ("repair", 2): FixtureMissing("missing fixture taskgraph_2.json")})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert not result.success and result.failure is not None
    assert result.failure.kind == "llm_error" and result.task_graph == PRIOR_INFEASIBLE
    assert [(s.kind, s.robot_id) for s in result.suggestions] == [
        ("raise_battery", "EX1"), ("raise_battery", "EX2"), ("add_robot", "EX3")]
    assert [s.stage for s in result.steps] == ["taskgraph", "validate", "schedule", "repair"]
    assert "suggestions" not in (result.steps[2].payload or {})
    assert result.steps[3].payload == {
        "suggestions": [s.model_dump(mode="json") for s in result.suggestions]}

    # a schema failure before any graph existed has nothing to advise on
    client = Scripted({("taskgraph", k): schema_error("taskgraph") for k in (1, 2, 3)})
    result = planner(client).plan_from_requirements(REQ, key="k")
    assert not result.success and result.task_graph is None and result.suggestions == []
    assert all("suggestions" not in (s.payload or {}) for s in result.steps)


def test_nothing_is_attached_on_success(monkeypatch):
    calls: list[Any] = []
    real = loop_module.suggest_relaxations
    monkeypatch.setattr(loop_module, "suggest_relaxations",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    for script in ({("taskgraph", 1): FEASIBLE},
                   {("taskgraph", 1): INFEASIBLE, ("repair", 2): FEASIBLE}):
        result = planner(Scripted(script)).plan_from_requirements(REQ, key="k")
        assert result.success and result.suggestions == []
        assert all("suggestions" not in (s.payload or {}) for s in result.steps)
    assert calls == []   # a verdict that gets repaired is never advised on


def test_advice_is_computed_once_for_the_final_verdict_with_the_planners_options(monkeypatch):
    calls: list[tuple[Any, ...]] = []
    real = loop_module.suggest_relaxations

    def spy(req, graph, failure, opts=None, **kwargs):
        calls.append((req, graph, failure, opts, kwargs))
        return real(req, graph, failure, opts, **kwargs)

    monkeypatch.setattr(loop_module, "suggest_relaxations", spy)
    client = Scripted({("taskgraph", 1): INFEASIBLE, ("repair", 2): INFEASIBLE})
    result = planner(client, max_repairs=1, solver_time_limit_s=3.0,
                     suggest_budget_s=2.5).plan_from_requirements(REQ, key="k")
    assert not result.success and result.repairs == 1
    assert len(calls) == 1
    req, graph, failure, opts, kwargs = calls[0]
    assert req == REQ and graph == result.task_graph and failure == result.failure
    assert isinstance(opts, SolveOptions) and opts.time_limit_s == 3.0
    assert kwargs == {"locked": None, "kinds": None, "reserved_ids": [], "budget_s": 2.5}
    verdicts = [s for s in result.steps if s.stage == "schedule"]
    assert set(verdicts[0].payload or {}) == {"budget"}
    assert "suggestions" in (verdicts[1].payload or {})
    assert len(result.suggestions) == 3


def test_suggest_budget_zero_disables_the_advice_and_its_errors_never_surface(monkeypatch):
    result = planner(Scripted({("taskgraph", 1): INFEASIBLE}), max_repairs=0,
                     suggest_budget_s=0).plan_from_requirements(REQ, key="k")
    assert not result.success and result.suggestions == []
    assert set(result.steps[-1].payload or {}) == {"budget"}   # the rows ride on the verdict
    with pytest.raises(ValueError, match="suggest_budget_s"):
        Planner(Scripted({}), suggest_budget_s=-1)
    assert Planner(Scripted({})).suggest_budget_s == 6.0

    def boom(*args: Any, **kwargs: Any) -> list[Suggestion]:
        raise RuntimeError("the advice solver exploded")

    monkeypatch.setattr(loop_module, "suggest_relaxations", boom)
    result = planner(Scripted({("taskgraph", 1): INFEASIBLE}), max_repairs=0
                     ).plan_from_requirements(REQ, key="k")
    assert not result.success and result.failure is not None
    assert set(result.failure.conflicts) == ENERGY_CORE and result.suggestions == []
    assert set(result.steps[-1].payload or {}) == {"budget"}   # the rows ride on the verdict


def test_plan_result_round_trips_with_suggestions_empty_by_default():
    bare = PlanResult(requirements=REQ)
    assert bare.suggestions == []
    assert PlanResult.model_validate_json(bare.model_dump_json()) == bare
    data = json.loads(bare.model_dump_json())
    assert data["suggestions"] == []
    del data["suggestions"]   # a result recorded before the field existed still loads
    assert PlanResult.model_validate(data).suggestions == []

    advised = PlanResult(requirements=REQ, success=False, suggestions=[
        Suggestion(kind="raise_battery", detail="EX1 needs 5,950 Wh (has 5,000 Wh): feasible at 62 h",
                   robot_type="excavator", robot_id="EX1", battery_wh=5950, makespan_h=62,
                   verified=True),
        Suggestion(kind="reduce_scope", detail="cut the bowl (24 h, 4000 Wh)"),
    ])
    again = PlanResult.model_validate_json(advised.model_dump_json())
    assert again == advised and again.suggestions[1].verified is False
    assert again.suggestions[1].model_dump(exclude_none=True) == {
        "kind": "reduce_scope", "detail": "cut the bowl (24 h, 4000 Wh)", "verified": False}
