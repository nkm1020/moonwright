"""Tests for the rate model (``moonwright.pipeline.rates``), the schema fields it relies on, and
the prompts that carry the rate table and the prior graph."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from moonwright.llm.prompts import (
    EXPLAIN_SYSTEM,
    JSON_ONLY,
    REPAIR_SYSTEM,
    REPLAN_SYSTEM,
    STRUCTURE_SYSTEM,
    TASKGRAPH_SYSTEM,
    repair_messages,
    replan_messages,
    structure_messages,
    taskgraph_messages,
)
from moonwright.pipeline.rates import (
    ADVISORY_THRESHOLD,
    BASE_POWER_W,
    RATE_TABLE_TEXT,
    RATED_TYPES,
    RATES,
    STRIP_DEPTH_M,
    UNGROUNDED_ADVISORY,
    Rate,
    apply_rates,
    is_engaged,
    rate_for,
    rated_units,
)
from moonwright.schemas import (
    MAX_DURATION_H,
    MAX_ENERGY_WH,
    MAX_QUANTITY,
    Failure,
    ReplanRecord,
    Requirements,
    Robot,
    Schedule,
    Site,
    Task,
    TaskGraph,
    Zone,
)


# ----------------------------------------------------------------------------- builders
def task(id_: str, *, type_: str = "excavator", dur: int = 10, energy: float = 1000.0,
         quantity: float | None = None, unit: str | None = None, name: str | None = None,
         after: tuple[str, ...] = (), **extra: object) -> Task:
    return Task(id=id_, name=name or id_, robot_type=type_, duration_h=dur, energy_wh=energy,
                zone="Z1", after=list(after), quantity=quantity, unit=unit, **extra)


def graph(*tasks: Task) -> TaskGraph:
    return TaskGraph(tasks=list(tasks), notes="n")


REQ = Requirements(
    site=Site(zones=[Zone(id="Z1", center=(10, 10)), Zone(id="Z2", center=(30, 10))]),
    robots=[Robot(id="EX1", type="excavator", battery_wh=5000),
            Robot(id="SV1", type="surveyor", battery_wh=1500)],
    horizon_h=200,
)


# ----------------------------------------------------------------------------- calibration
@pytest.mark.parametrize(("robot_type", "unit", "per_h", "wh", "quantity", "hours", "energy"), [
    ("excavator", "m3", 7.0, 16.0, 126, 18, 2016.0),     # pad bowl: 18 h / ~2000 Wh
    ("excavator", "m2", 25.0, 4.8, 314, 13, 1507.2),     # pad strip: 12-13 h / ~1500 Wh
    ("hauler", "m3", 12.0, 7.0, 188, 16, 1316.0),        # bowl spoil: 16 h / ~1300 Wh
    ("compactor", "m2", 26.0, 4.0, 314, 13, 1256.0),     # subgrade: ~12 h / ~1300 Wh
    ("printer", "m2", 14.0, 8.6, 314, 23, 2700.4),       # sinter: ~22 h / ~2700 Wh
    ("surveyor", "m2", 150.0, 0.5, 314, 3, 157.0),
])
def test_calibration_rows(robot_type, unit, per_h, wh, quantity, hours, energy) -> None:
    rate = rate_for(robot_type, unit)
    assert rate is not None and (rate.per_h, rate.wh_per_unit) == (per_h, wh)
    assert rate.duration_h(quantity) == hours
    assert rate.energy_wh(quantity) == energy
    assert "assumption" in rate.note and rate.work


def test_table_shape_and_text() -> None:
    assert len(RATES) == 6 and len({(r.robot_type, r.unit) for r in RATES}) == 6
    assert BASE_POWER_W == {"excavator": 110.0, "hauler": 80.0, "compactor": 105.0,
                            "printer": 120.0, "surveyor": 75.0}
    assert rate_for("hauler", "m2") is None and rate_for("crane", "m3") is None
    assert rate_for("excavator", None) is None and rate_for("excavator", "ea") is None
    assert RATED_TYPES == {"excavator", "hauler", "compactor", "printer", "surveyor"}
    assert rated_units("excavator") == ["m3", "m2"] and rated_units("crane") == []
    assert UNGROUNDED_ADVISORY.startswith("ungrounded: no task states a quantity")
    for line in ("excavator   m3    7 m3/h    16 Wh/m3", "printer     m2    14 m2/h   8.6 Wh/m2",
                 "duration_h = ceil(quantity / rate), at least 1 h; energy_wh = quantity x Wh/unit.",
                 "excavator 110 W, hauler 80 W, compactor 105 W, printer 120 W, surveyor 75 W",
                 "assumptions, not measurements"):
        assert line in RATE_TABLE_TEXT
    assert "\n\n" not in RATE_TABLE_TEXT  # compact: one row per line


def test_the_m2_row_means_a_surface_layer_up_to_the_strip_depth() -> None:
    """Phase 3e: the excavator's m2 row is stripping or grading of a surface layer up to
    ``STRIP_DEPTH_M``; the volume check credits an m2 task on a dug shape with its area x
    min(depth, STRIP_DEPTH_M), so the table says so where the model reads it."""
    assert STRIP_DEPTH_M == 0.2
    row = rate_for("excavator", "m2")
    assert row is not None
    assert row.work == "strip or grade a surface layer up to 0.2 m; deeper cuts are excavation in m3"
    assert ("excavator   m2    25 m2/h   4.8 Wh/m2  strip or grade a surface layer up to 0.2 m; "
            "deeper cuts are excavation in m3") in RATE_TABLE_TEXT
    assert f"up to {STRIP_DEPTH_M} m" in RATE_TABLE_TEXT


def test_duration_rounding_and_bounds() -> None:
    r = Rate("excavator", "m3", 0.7, 1.0, "w", "n")
    assert r.duration_h(2.1) == 3          # 2.1 / 0.7 is 3.0000000000000004 in floats
    assert r.duration_h(2.1001) == 4
    assert r.duration_h(0) == 1 and r.energy_wh(0) == 0.0
    assert r.duration_h(0.01) == 1
    big = rate_for("excavator", "m3")
    assert big is not None
    assert big.duration_h(MAX_QUANTITY) == MAX_DURATION_H     # capped, never an invalid Task
    assert big.energy_wh(MAX_QUANTITY) == 1.6e6 <= MAX_ENERGY_WH
    assert Rate("x", "ea", 1.0, 1e3, "w", "n").energy_wh(MAX_QUANTITY) == MAX_ENERGY_WH


# ----------------------------------------------------------------------------- apply_rates
def test_quantity_tasks_are_overridden_and_estimates_kept() -> None:
    g = graph(task("T1", dur=10, energy=1000, quantity=126, unit="m3",
                   basis="pi*(10 m)^2*0.4 m = 126 m3"),
              task("T2", type_="printer", dur=22, energy=2700, quantity=314, unit="m2"))
    out, advisories = apply_rates(g)
    t1, t2 = out.tasks
    assert (t1.duration_h, t1.energy_wh) == (18, 2016.0)
    assert (t1.estimated_duration_h, t1.estimated_energy_wh) == (10.0, 1000.0)
    assert t1.basis == "pi*(10 m)^2*0.4 m = 126 m3" and t1.quantity == 126 and t1.unit == "m3"
    assert (t2.duration_h, t2.energy_wh) == (23, 2700.4)
    assert (t2.estimated_duration_h, t2.estimated_energy_wh) == (22.0, 2700.0)
    assert out.notes == "n" and out is not g and g.tasks[0].duration_h == 10  # pure
    assert advisories == [
        "estimate:T1: model said 10 h / 1000 Wh, rate model gives 18 h / 2016 Wh (+102%)"]


def test_tasks_without_quantity_keep_hours_and_get_the_base_power_floor() -> None:
    g = graph(task("T1", dur=10, energy=1000, quantity=50, unit="m3"),          # engages
              task("T2", type_="surveyor", dur=4, energy=100),                  # 4 h x 75 W = 300
              task("T3", type_="surveyor", dur=4, energy=300),                  # at the floor
              task("T4", type_="hauler", dur=6, energy=900),                    # above it
              task("T5", type_="crane", dur=2, energy=0))                       # unknown type
    out, advisories = apply_rates(g)
    by = {t.id: t for t in out.tasks}
    assert (by["T2"].duration_h, by["T2"].energy_wh, by["T2"].estimated_energy_wh) == (4, 300.0, 100.0)
    assert by["T2"].estimated_duration_h is None
    assert by["T3"] == g.tasks[2] and by["T4"] == g.tasks[3] and by["T5"] == g.tasks[4]
    assert advisories == []  # 50 m3 -> 8 h / 800 Wh is within 50 % of 10 h / 1000 Wh


def test_graph_without_any_quantity_is_returned_untouched() -> None:
    g = graph(task("T1", dur=6, energy=600), task("T2", type_="surveyor", dur=3, energy=200))
    out, advisories = apply_rates(g)
    assert out is g and advisories == []   # fixtures and solver-only replans never change
    unit_only = graph(task("T1", dur=6, energy=600, unit="m3"))  # a unit without a quantity
    assert apply_rates(unit_only)[0] is unit_only


def test_unknown_robot_type_unit_pairs_keep_the_models_numbers() -> None:
    g = graph(task("T1", type_="hauler", dur=5, energy=400, quantity=100, unit="m2"),
              task("T2", type_="crane", dur=5, energy=400, quantity=3, unit="ea"),
              task("T3", dur=5, energy=400, quantity=12, unit="ea"))
    out, advisories = apply_rates(g)
    assert out.tasks == g.tasks and advisories == []
    assert all(t.estimated_duration_h is None for t in out.tasks)


def test_advisories_fire_above_fifty_percent_either_way() -> None:
    assert ADVISORY_THRESHOLD == 0.5
    cases = {
        "T1": task("T1", dur=18, energy=2016, quantity=126, unit="m3"),         # exact: none
        "T2": task("T2", dur=12, energy=1400, quantity=126, unit="m3"),         # +50 % / +44 %: none
        "T3": task("T3", dur=11, energy=1400, quantity=126, unit="m3"),         # +64 % hours
        "T4": task("T4", dur=18, energy=5000, quantity=126, unit="m3"),         # -60 % energy
        "T5": task("T5", dur=18, energy=0, quantity=126, unit="m3"),            # model said 0
        "T6": task("T6", type_="surveyor", dur=1, energy=50, quantity=314, unit="m2"),
    }
    out, advisories = apply_rates(graph(*cases.values()))
    assert advisories == [
        "estimate:T3: model said 11 h / 1400 Wh, rate model gives 18 h / 2016 Wh (+64%)",
        "estimate:T4: model said 18 h / 5000 Wh, rate model gives 18 h / 2016 Wh (-60%)",
        "estimate:T5: model said 18 h / 0 Wh, rate model gives 18 h / 2016 Wh (model said 0)",
        "estimate:T6: model said 1 h / 50 Wh, rate model gives 3 h / 157 Wh (+214%)",
    ]
    assert all(t.duration_h == 18 for t in out.tasks[:5])


def test_apply_rates_is_idempotent_and_pure() -> None:
    g = graph(task("T1", dur=10, energy=1000, quantity=126, unit="m3"),
              task("T2", type_="surveyor", dur=4, energy=100))
    once, adv1 = apply_rates(g)
    twice, adv2 = apply_rates(once)
    assert twice.tasks[0].duration_h == once.tasks[0].duration_h == 18
    assert twice.tasks[1].energy_wh == once.tasks[1].energy_wh == 300.0
    assert adv1 and adv2 == []   # the second pass sees the grounded numbers as the estimate
    assert twice.tasks[0].estimated_duration_h == 18.0  # the estimate is what was said last


def test_keep_leaves_remaining_work_alone_but_grounds_rescoped_and_new_tasks() -> None:
    remaining = graph(task("T1", dur=7, energy=800.5, quantity=126, unit="m3"),  # underway
                      task("T2", dur=18, energy=2016, quantity=126, unit="m3"),
                      task("T3", type_="surveyor", dur=2, energy=90))
    answer = graph(
        task("T1", dur=7, energy=800.5, quantity=126, unit="m3"),      # returned unchanged
        task("T2", dur=9, energy=1008, quantity=63, unit="m3"),        # re-scoped: half the bowl
        task("T4", dur=9, energy=1008, quantity=63, unit="m3"),        # the other half, new
        task("T3", type_="surveyor", dur=2, energy=90),                # unchanged pure-time task
        task("T5", type_="surveyor", dur=1, energy=10),                # new pure-time task
    )
    out, advisories = apply_rates(answer, keep=remaining, locked=["T1"])
    by = {t.id: t for t in out.tasks}
    assert (by["T1"].duration_h, by["T1"].energy_wh) == (7, 800.5)   # not re-sized to 18 h
    assert by["T1"].estimated_duration_h is None
    assert (by["T2"].duration_h, by["T2"].energy_wh) == (9, 1008.0)
    assert (by["T4"].duration_h, by["T4"].energy_wh) == (9, 1008.0)
    assert (by["T3"].duration_h, by["T3"].energy_wh) == (2, 90.0)     # kept: no floor applied
    assert (by["T5"].duration_h, by["T5"].energy_wh) == (1, 75.0)     # new: floored
    assert advisories == []
    # a kept task that changed robot type is grounded afresh
    retyped = graph(task("T1", type_="hauler", dur=7, energy=800.5, quantity=126, unit="m3"))
    out, _ = apply_rates(retyped, keep=remaining)
    assert (out.tasks[0].duration_h, out.tasks[0].energy_wh) == (11, 882.0)
    # without keep the underway task would be re-sized to its full quantity
    out, _ = apply_rates(answer)
    assert {t.id: t.duration_h for t in out.tasks}["T1"] == 18


def test_a_kept_task_with_edited_numbers_is_grounded_again_unless_it_is_locked() -> None:
    """The repair prompt says editing duration_h or energy_wh without changing the quantity
    changes nothing; that must hold on the replan path too, where the remaining graph is the
    ``keep`` graph: a task returned with its quantity but 1 h / 1 Wh is re-sized from the
    quantity (and the advisory names the attempt). Only a locked task keeps whatever numbers
    it came back with, because the scope guard judges those."""
    remaining = graph(task("T1", dur=7, energy=800.5, quantity=126, unit="m3"),    # underway
                      task("T2", dur=18, energy=2016, quantity=126, unit="m3"))
    cheat = graph(task("T1", dur=7, energy=800.5, quantity=126, unit="m3"),
                  task("T2", dur=1, energy=1, quantity=126, unit="m3"))      # same bowl, 1 h / 1 Wh
    out, advisories = apply_rates(cheat, keep=remaining, locked=["T1"])
    by = {t.id: t for t in out.tasks}
    assert (by["T1"].duration_h, by["T1"].energy_wh) == (7, 800.5)
    assert (by["T2"].duration_h, by["T2"].energy_wh) == (18, 2016.0)
    assert (by["T2"].estimated_duration_h, by["T2"].estimated_energy_wh) == (1.0, 1.0)
    assert advisories == [
        "estimate:T2: model said 1 h / 1 Wh, rate model gives 18 h / 2016 Wh (+201500%)"]
    # the locked task's numbers are the scope guard's business, not the rate model's ...
    edited_lock = graph(task("T1", dur=3, energy=300, quantity=126, unit="m3"))
    out, advisories = apply_rates(edited_lock, keep=remaining, locked=["T1"])
    assert (out.tasks[0].duration_h, out.tasks[0].energy_wh) == (3, 300.0) and advisories == []
    # ... but the same edit on a task that is not underway is re-sized like any other
    out, _ = apply_rates(edited_lock, keep=remaining)
    assert (out.tasks[0].duration_h, out.tasks[0].energy_wh) == (18, 2016.0)


def test_a_kept_task_that_lost_its_quantity_inherits_it() -> None:
    """Dropping every quantity of the graph the model was handed does not leave the rate model
    behind: the graph counts as engaged through ``keep`` and each task gets its scope back."""
    remaining = graph(task("T1", dur=18, energy=2016, quantity=126, unit="m3", basis="pi r^2 h"),
                      task("T2", type_="surveyor", dur=2, energy=150))
    stripped = graph(task("T1", dur=6, energy=500), task("T2", type_="surveyor", dur=2, energy=150))
    assert is_engaged(stripped) is False and is_engaged(stripped, remaining) is True
    assert is_engaged(remaining) is True
    out, advisories = apply_rates(stripped, keep=remaining)
    t1 = out.tasks[0]
    assert (t1.quantity, t1.unit, t1.basis) == (126, "m3", "pi r^2 h")
    assert (t1.duration_h, t1.energy_wh) == (18, 2016.0)
    assert advisories == [
        "estimate:T1: model said 6 h / 500 Wh, rate model gives 18 h / 2016 Wh (+303%)"]
    assert out.tasks[1] == stripped.tasks[1]       # handed over as it was: no floor applied
    assert apply_rates(stripped)[0] is stripped    # nothing handed over: nothing to inherit
    # a retyped task is a different task: no inheritance, grounded on its own terms
    retyped = graph(task("T1", type_="hauler", dur=6, energy=500))
    assert apply_rates(retyped, keep=remaining)[0].tasks[0].energy_wh == 500.0


# ----------------------------------------------------------------------------- schema
def test_task_quantity_fields_are_bounded_and_optional() -> None:
    t = Task(id="T1", name="n", robot_type="excavator", duration_h=1, energy_wh=1, zone="Z1")
    assert (t.quantity, t.unit, t.basis, t.estimated_duration_h, t.estimated_energy_wh) == \
        (None, None, None, None, None)
    for bad in ({"quantity": -1}, {"quantity": float("nan")}, {"quantity": MAX_QUANTITY + 1},
                {"unit": "kg"}, {"estimated_duration_h": -1}, {"estimated_energy_wh": float("inf")}):
        with pytest.raises(ValidationError):
            Task(id="T1", name="n", robot_type="excavator", duration_h=1, energy_wh=1, zone="Z1",
                 **bad)
    for unit in ("m3", "m2", "m", "ea"):
        assert Task(id="T1", name="n", robot_type="excavator", duration_h=1, energy_wh=1,
                    zone="Z1", quantity=0, unit=unit).unit == unit
    schema = TaskGraph.model_json_schema()
    props = schema["$defs"]["Task"]["properties"]
    assert set(props) >= {"quantity", "unit", "basis", "estimated_duration_h", "estimated_energy_wh"}
    assert "Leave null" in props["estimated_duration_h"]["description"]
    assert "126 m3" in props["basis"]["description"]


def test_replan_record_escalated_defaults_to_false() -> None:
    assert ReplanRecord.model_fields["escalated"].default is False


# ----------------------------------------------------------------------------- prompts
def test_taskgraph_prompt_asks_for_quantities_and_carries_the_rate_table() -> None:
    flat = " ".join(TASKGRAPH_SYSTEM.split())
    for text in (RATE_TABLE_TEXT, "quantity", "unit", "basis", "6 to 24 tasks", "COMPUTED",
                 "more than 60 m3", "300 m2", "pi*(10 m)^2*0.4 m = 126 m3"):
        assert text in TASKGRAPH_SYSTEM
    for text in ("at least as many tasks as there are robots of that type",
                 "landing pad = survey, excavate or grade",
                 "one task must fit one robot's battery"):
        assert text in flat
    assert TASKGRAPH_SYSTEM.endswith("Answer with ONE JSON object matching this schema, no prose:")
    system, user = taskgraph_messages(REQ)
    assert system.startswith(TASKGRAPH_SYSTEM) and system.endswith(JSON_ONLY)
    assert '"quantity"' in system and '"estimated_duration_h"' in system  # the schema block
    assert user.startswith("REQUIREMENTS:\n") and "PRIOR TASK GRAPH" not in user


def test_taskgraph_prompt_includes_the_prior_graph_whenever_given() -> None:
    prior = graph(task("T1", dur=18, energy=2016, quantity=126, unit="m3"),
                  task("T2", type_="surveyor", dur=3, energy=225, after=("T1",)))
    _, user = taskgraph_messages(REQ, context="EX2 failed at 12 h", prior=prior)
    assert "PRIOR TASK GRAPH" in user
    assert user.index("REQUIREMENTS:") < user.index("PRIOR TASK GRAPH") < user.index("CONTEXT:")
    for t in prior.tasks:   # the full definitions, not just the ids
        assert f'"id": "{t.id}"' in user
        assert f'"duration_h": {t.duration_h}' in user and f'"energy_wh": {t.energy_wh}' in user
    assert '"quantity": 126.0' in user and user.endswith("CONTEXT:\nEX2 failed at 12 h")
    # the replan prompt carries it as the remaining task graph and asks for quantities back
    _, user = replan_messages(REQ, prior, "HL1 failed")
    assert "REMAINING TASK GRAPH:" in user and '"quantity": 126.0' in user
    assert "quantity, unit" in REPLAN_SYSTEM and "rate table" in REPLAN_SYSTEM


def test_repair_prompt_talks_about_splitting_not_lowering_numbers() -> None:
    assert "lower energy_wh to a realistic value" not in REPAIR_SYSTEM
    for text in (RATE_TABLE_TEXT, "split", "smaller quantity only", "scope change",
                 "changes nothing", "scope:<structure>"):
        assert text in REPAIR_SYSTEM
    assert REPAIR_SYSTEM.endswith("matching this schema, no prose:")
    failure = Failure(kind="infeasible", conflicts=["energy:EX1"], hint="EX1 over budget")
    prior = graph(task("T1", quantity=126, unit="m3"))
    baseline = graph(task("T1", dur=7, energy=800, quantity=126, unit="m3"))
    system, user = repair_messages(REQ, prior, failure, "ctx", baseline)
    assert system.endswith(JSON_ONLY)
    assert "REMAINING TASK GRAPH (the work still outstanding" in user
    assert "quantity, unit, zone, after" in user
    assert user.index("REMAINING TASK GRAPH") < user.index("PREVIOUS TASK GRAPH") \
        < user.index("FAILURE:") < user.index("CONTEXT:\nctx")


def test_structure_prompt_states_the_zone_geometry_rules() -> None:
    for text in ("40 x 40", "0 <= x < 40", "radius 3 to 6", "2 x radius", "depot on an edge",
                 "copy the fleet exactly", "stockpile or haul lane", "own zone"):
        assert text in STRUCTURE_SYSTEM
    assert STRUCTURE_SYSTEM.endswith("Answer with ONE JSON object matching this schema, no prose:")
    system, user = structure_messages("  Build a pad.  ")
    assert system.endswith(JSON_ONLY) and user == "BRIEF:\nBuild a pad."


def test_explain_prompt_mentions_the_rate_table() -> None:
    assert "rate table" in EXPLAIN_SYSTEM and "quantities" in EXPLAIN_SYSTEM.lower()
    assert EXPLAIN_SYSTEM.endswith('{"text": "..."} and nothing else: no markdown fences, no\n'
                                   "commentary, no reasoning text.")
    assert Schedule  # the explain user message is unchanged (covered by the e2e tests)
