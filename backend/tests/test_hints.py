"""Phase 3d: the copy of the solver's infeasibility hints, the numbers behind them, and the
replan outcome message.

A mission planner reads the hint on the trace in the first minute of the demo and the repair
prompt reads it verbatim, so every sentence must be readable *and* keep every figure a repair
needs: whole Wh and h with thousands separators and a unit, real plurals, no nested
parentheses, no milli-Wh, the robots of one type sharing one pool sentence, and
:func:`moonwright.pipeline.schedule.budget_rows` handing the same figures over as numbers.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

from moonwright.geometry import leg_mwh
from moonwright.pipeline.schedule import (
    SolveOptions,
    _decimals_apart,
    _instance,
    _travel_floor,
    _wh,
    budget_rows,
    solve_schedule,
)
from moonwright.pipeline.validate import validate_graph
from moonwright.schemas import (
    Failure,
    Requirements,
    Robot,
    Schedule,
    ScheduleEntry,
    Site,
    Task,
    TaskGraph,
    Zone,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FAST = SolveOptions(time_limit_s=5.0, num_workers=4, core_time_limit_s=3.0)
# every zone on the depot cell: no travel, so the figures are the task energies alone
FLAT = Site(depot=(20, 20), zones=[Zone(id=f"Z{i}", center=(20, 20)) for i in range(1, 6)])
# two zones 36 cells apart in the far corners, 41.15 cells from the depot (20, 1): at
# 12 cells/h a first leg takes 4 h and 20.574 Wh (rounded up to the milli-Wh), the leg between
# them 3 h and 18 Wh, so reaching both costs at least 38.574 Wh whatever the order
FAR = Site(grid=(40, 40), zones=[Zone(id="Z1", center=(2, 38)), Zone(id="Z2", center=(38, 38))])
LEG_WH = leg_mwh((20, 1), (2, 38)) / 1000         # 20.574 Wh
BOTH_WH = LEG_WH + 18                              # depot -> one zone -> the other


# ----------------------------------------------------------------------------- helpers
def robot(robot_id: str, robot_type: str, battery_wh: float) -> Robot:
    return Robot(id=robot_id, type=robot_type, battery_wh=battery_wh, speed_cells_per_h=12.0)


def task(task_id: str, robot_type: str, duration_h: int, energy_wh: float, zone: str,
         after: tuple[str, ...] = ()) -> Task:
    return Task(id=task_id, name=task_id, robot_type=robot_type, duration_h=duration_h,
                energy_wh=energy_wh, zone=zone, after=list(after))


def reqs(robots: list[Robot], horizon_h: int, site: Site = FLAT) -> Requirements:
    return Requirements(site=site, robots=robots, horizon_h=horizon_h)


def fixture(brief: str, graph: str) -> tuple[Requirements, TaskGraph]:
    req = Requirements.model_validate(json.loads((FIXTURES / brief / "structure.json").read_text()))
    return req, TaskGraph.model_validate(json.loads((FIXTURES / brief / graph).read_text()))


def infeasible(req: Requirements, graph: TaskGraph, opts: SolveOptions = FAST,
               locked: list[ScheduleEntry] | None = None) -> Failure:
    out = solve_schedule(req, graph, opts, locked=locked)
    assert isinstance(out, Failure) and out.kind == "infeasible", out
    return out


NESTED_PARENS = re.compile(r"\([^()]*\(")
UNSEPARATED = re.compile(r"\d{4,}")       # 10000 Wh must read 10,000 Wh
MILLI_WH = re.compile(r"\.\d{3}\b")       # 9929.649 Wh must read 9,930 Wh
GLUED_UNIT = re.compile(r"\d(?:Wh|h)\b")  # 5000Wh: the unit is spaced


def assert_readable(text: str) -> None:
    """The copy rules a judge notices: real plurals, flat parentheses, separated thousands,
    no milli-Wh, spaced units, one full stop per sentence."""
    assert "(s)" not in text and "incl." not in text and "~" not in text, text
    assert not NESTED_PARENS.search(text), text
    assert not UNSEPARATED.search(text), text
    assert not MILLI_WH.search(text), text
    assert not GLUED_UNIT.search(text), text
    assert text.endswith(".") and ".." not in text and "  " not in text, text


# ----------------------------------------------------------------------------- energy
def test_tight_battery_core_gets_one_pool_sentence_with_whole_figures():
    """The recorded demo: EX1 and EX2 used to get one 190-character sentence each, differing
    only in the robot id, with a milli-Wh total, nested parentheses and "robot(s)"."""
    req, graph = fixture("tight_battery", "taskgraph_1.json")
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:EX1", "energy:EX2"]
    assert failure.hint == (
        "EX1 and EX2 each have 5,000 Wh; the excavator tasks need at least 9,915 Wh in total, "
        "15 Wh of it travel, and cannot be split across 2 excavators (10,000 Wh combined) "
        "without exceeding one budget.")
    assert_readable(failure.hint)
    assert failure.details == []   # unchanged: the solver's details stay empty

    # the numbers behind the sentence, exactly the model's: the type's work plus the least
    # travel any split and order needs to reach its three zones (depot -> Z4, depot -> Z1 ->
    # Z2: 15.449 Wh; the per-task allowances made it 29.649 Wh), against both batteries
    inst = _instance(req, graph, FAST, [])
    typed = [i for i, t in enumerate(inst.tasks) if t.robot_type == "excavator"]
    travel = _travel_floor(inst, ["EX1", "EX2"], typed, hours=False)
    [row] = budget_rows(req, graph, failure, FAST)
    assert row == {"kind": "type", "subject": "excavator", "robots": ["EX1", "EX2"],
                   "budget_wh": 10000.0,
                   "demanded_wh": pytest.approx((sum(inst.energy[i] for i in typed) + travel) / 1000),
                   "travel_wh": pytest.approx(travel / 1000)}
    assert row["demanded_wh"] == pytest.approx(9915.449)
    assert row["travel_wh"] == pytest.approx(15.449)
    # the sentence quotes the rows, rounded
    for key in ("budget_wh", "demanded_wh", "travel_wh"):
        assert f"{round(row[key]):,} Wh" in failure.hint


def test_per_robot_sentence_names_demand_travel_share_and_budget():
    req = reqs([robot("PR1", "printer", 4000), robot("EX1", "excavator", 5000)], 100, FAR)
    graph = TaskGraph(tasks=[task("T1", "printer", 10, 2000, "Z1"),
                             task("T2", "printer", 10, 1990, "Z2"),
                             task("T3", "excavator", 2, 100, "Z1")])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:PR1"]
    assert failure.hint == ("PR1 would need at least 4,029 Wh, 39 Wh of it travel, but has "
                            "4,000 Wh.")
    assert_readable(failure.hint)
    [row] = budget_rows(req, graph, failure, FAST)
    assert row == {"kind": "robot", "subject": "PR1", "robots": ["PR1"], "budget_wh": 4000.0,
                   "demanded_wh": pytest.approx(3990 + BOTH_WH, abs=1e-9),
                   "travel_wh": pytest.approx(BOTH_WH, abs=1e-9)}


def test_without_travel_the_sentence_is_demand_against_budget():
    req = reqs([robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000)], 100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 3000, "Z1"),
                             task("T2", "excavator", 3, 3200, "Z2"),
                             task("T3", "hauler", 2, 100, "Z2")])
    failure = infeasible(req, graph)
    assert failure.hint == "EX1 would need 6,200 Wh but has 5,000 Wh."
    assert budget_rows(req, graph, failure, FAST) == [
        {"kind": "robot", "subject": "EX1", "robots": ["EX1"], "budget_wh": 5000.0,
         "demanded_wh": 6200.0, "travel_wh": 0.0}]


def test_pool_sentence_lists_unequal_budgets_and_every_robot_of_the_type():
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 4000)], 100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 1, 3000, "Z1"),
                             task("T2", "excavator", 1, 3000, "Z2"),
                             task("T3", "excavator", 1, 2900, "Z3")])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:EX1", "energy:EX2"]
    assert failure.hint == (
        "EX1 has 5,000 Wh and EX2 has 4,000 Wh; the excavator tasks need 8,900 Wh in total "
        "and cannot be split across 2 excavators (9,000 Wh combined) without exceeding one "
        "budget.")
    assert budget_rows(req, graph, failure, FAST) == [
        {"kind": "type", "subject": "excavator", "robots": ["EX1", "EX2"], "budget_wh": 9000.0,
         "demanded_wh": 8900.0, "travel_wh": 0.0}]

    # three robots: the pool counts them all, the sentence lists the ones the core names
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000),
                robot("EX3", "excavator", 1000)], 100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 1, 4000, "Z1"),
                             task("T2", "excavator", 1, 4000, "Z2"),
                             task("T3", "excavator", 1, 1900, "Z3")])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:EX1", "energy:EX2", "energy:EX3"]
    assert failure.hint == (
        "EX1 has 5,000 Wh, EX2 has 5,000 Wh and EX3 has 1,000 Wh; the excavator tasks need "
        "9,900 Wh in total and cannot be split across 3 excavators (11,000 Wh combined) "
        "without exceeding one budget.")
    assert_readable(failure.hint)
    [row] = budget_rows(req, graph, failure, FAST)
    assert row["robots"] == ["EX1", "EX2", "EX3"] and row["budget_wh"] == 11000.0


def test_pool_sentence_says_when_the_work_exceeds_the_pool_itself():
    """Three 100 Wh tasks on two 125 Wh robots in the far corners: with the least travel that
    reaches both zones the type's work is more than the two batteries together."""
    req = reqs([robot("R1", "excavator", 125), robot("R2", "excavator", 125)], 50, FAR)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 4, 100, "Z2"),
                             task("T3", "excavator", 4, 100, "Z1")])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:R1", "energy:R2"]
    assert failure.hint == ("R1 and R2 each have 125 Wh; the excavator tasks need at least "
                            "339 Wh in total, 39 Wh of it travel, more than the 250 Wh the "
                            "2 excavators have combined.")
    [row] = budget_rows(req, graph, failure, FAST)
    assert row["demanded_wh"] == pytest.approx(300 + BOTH_WH, abs=1e-9)
    assert row["travel_wh"] == pytest.approx(BOTH_WH, abs=1e-9) and row["budget_wh"] == 250.0


def over(need_wh: float, battery_wh: float) -> Failure:
    """The verdict on one excavator whose two tasks need ``need_wh`` against ``battery_wh``."""
    req = reqs([robot("EX1", "excavator", battery_wh)], 100)
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, need_wh - 892.19, "Z1"),
                             task("T2", "excavator", 3, 892.19, "Z2")])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:EX1"]
    [row] = budget_rows(req, graph, failure, FAST)
    assert row["demanded_wh"] == pytest.approx(need_wh, abs=1e-9)
    assert row["budget_wh"] == pytest.approx(battery_wh, abs=1e-9)
    return failure


def test_fractional_budgets_get_only_the_decimals_that_keep_the_figures_apart():
    """Replans hand over 3-dp task energies and 2-dp remaining batteries: whole Wh would read
    "needs 1,784 Wh but has 1,784 Wh", so the sentence keeps the fewest decimals that still
    show the overshoot, and the rows stay exact to the milli-Wh."""
    assert over(1784.381, 1784.38).hint == "EX1 would need 1,784.381 Wh but has 1,784.38 Wh."
    assert over(1784.39, 1784.38).hint == "EX1 would need 1,784.39 Wh but has 1,784.38 Wh."
    assert over(1784.4, 1784.3).hint == "EX1 would need 1,784.4 Wh but has 1,784.3 Wh."
    assert over(1784.6, 1784.3).hint == "EX1 would need 1,785 Wh but has 1,784 Wh."
    assert over(1800, 1784.38).hint == "EX1 would need 1,800 Wh but has 1,784 Wh."


def test_the_decimals_chosen_are_the_decimals_printed():
    """The chooser counts half up on the milli-Wh and the renderer must round the same way:
    through the binary float of the quotient, 1,784.385 Wh against 1,784.38 Wh printed as
    "1,784.38 Wh but has 1,784.38 Wh" and 1,000.005 Wh against 1,000 Wh as "1,000 Wh but
    has 1,000 Wh", the two decimals chosen undone. A need above its budget never prints
    equal to it, whatever the milli-Wh."""
    assert over(1784.385, 1784.38).hint == "EX1 would need 1,784.39 Wh but has 1,784.38 Wh."
    assert over(1000.005, 1000.0).hint == "EX1 would need 1,000.01 Wh but has 1,000 Wh."
    assert over(1000.125, 1000.12).hint == "EX1 would need 1,000.13 Wh but has 1,000.12 Wh."
    for budget in (*range(2_000), *range(999_000, 1_021_000, 5)):   # mWh
        for need in (budget + 1, budget + 5, budget + 50, budget + 500):
            decimals = _decimals_apart(need, budget)
            assert _wh(need, decimals) != _wh(budget, decimals), (need, budget, decimals)
    assert (_wh(1000050, 1), _wh(1000005, 2), _wh(500), _wh(499), _wh(9929649)) == (
        "1,000.1 Wh", "1,000.01 Wh", "1 Wh", "0 Wh", "9,930 Wh")


# ----------------------------------------------------------------------------- time
def test_robot_sentences_use_real_plurals_and_share_one_sentence_per_type():
    req = reqs([robot("EX1", "excavator", 5000)], 15)
    graph = TaskGraph(tasks=[task("T1", "excavator", 10, 100, "Z1"),
                             task("T2", "excavator", 10, 100, "Z2")])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["horizon", "robot:EX1"]
    assert failure.hint == (
        "The plan cannot finish within the 15 h horizon: the longest precedence chain alone "
        "needs 10 h; the busiest robot type (excavator) needs at least 20 h, 5 h past the "
        "15 h deadline. EX1 runs one task at a time, and the 2 tasks only it can do already "
        "take 20 h back to back.")
    assert_readable(failure.hint)

    # two excavators sharing four 10 h tasks: one sentence names both instead of two copies
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], 15)
    graph = TaskGraph(tasks=[task(f"T{i}", "excavator", 10, 100, f"Z{i}") for i in range(1, 5)])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["horizon", "robot:EX1", "robot:EX2"]
    assert failure.hint.endswith(
        ". EX1 and EX2 each run one task at a time; the excavator tasks total 40 h across "
        "2 excavators.")
    assert failure.hint.count("one task at a time") == 1
    assert_readable(failure.hint)


def test_horizon_sentence_says_how_far_past_the_deadline():
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], 20)
    graph = TaskGraph(tasks=[task("T1", "excavator", 10, 100, "Z1"),
                             task("T2", "excavator", 10, 100, "Z2", after=("T1",)),
                             task("T3", "excavator", 10, 100, "Z3", after=("T2",))])
    failure = infeasible(req, graph)
    assert failure.hint.startswith(
        "The plan cannot finish within the 20 h horizon: the longest precedence chain alone "
        "needs 30 h, 10 h past the 20 h deadline; the busiest robot type (excavator) needs "
        "at least 15 h. T2 cannot start until T1 finishes (T1 takes 10 h); the precedence "
        "chain through this edge is 30 h long.")
    assert_readable(failure.hint)

    # hours already elapsed count against the deadline
    chain = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1"),
                             task("T2", "excavator", 3, 100, "Z2", after=("T1",)),
                             task("T3", "excavator", 5, 100, "Z3")])
    failure = infeasible(reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], 16),
                         chain, SolveOptions(time_limit_s=5.0, num_workers=4,
                                            core_time_limit_s=3.0, start_offset_h=10))
    assert failure.hint.startswith(
        "The plan cannot finish within the 16 h horizon after the 10 h already elapsed: the "
        "longest precedence chain alone needs 7 h, 1 h past the 16 h deadline;")

    # a locked task that ends past the deadline, and the transit to a far first zone
    lock = ScheduleEntry(task="T3", robot="EX2", start_h=8, end_h=13)
    failure = infeasible(reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], 10),
                         chain, locked=[lock])
    assert failure.hint == (
        "The plan cannot finish within the 10 h horizon: the locked task T3 cannot end before "
        "13 h, 3 h past the 10 h deadline; the longest chain of work, locked starts included, "
        "needs 13 h, 3 h past the 10 h deadline; the busiest robot type (excavator) needs at "
        "least 6 h.")
    failure = infeasible(reqs([robot("R1", "excavator", 300)], 7, FAR),
                         TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")]))
    assert failure.hint == (
        "The plan cannot finish within the 7 h horizon: the longest precedence chain alone "
        "needs 8 h including the transit to its first zone, 1 h past the 7 h deadline; the "
        "busiest robot type (excavator) needs at least 4 h.")


def test_zone_capability_and_reference_sentences():
    req = reqs([robot("EX1", "excavator", 5000), robot("EX2", "excavator", 5000)], 15)
    graph = TaskGraph(tasks=[task("T1", "excavator", 10, 100, "Z1"),
                             task("T2", "excavator", 10, 100, "Z1")])
    failure = infeasible(req, graph)
    assert failure.hint.endswith(
        ". Zone Z1 admits one robot at a time, so its 2 tasks (20 h in total) cannot overlap.")
    graph = TaskGraph(tasks=[task("T1", "crane", 4, 100, "Z1"),
                             task("T2", "excavator", 3, 100, "Z2", after=("T9",))])
    failure = infeasible(req, graph)
    assert failure.hint == ("Task T1 needs a crane but no robot of that type exists. "
                            "Task T2 waits for unknown task T9.")
    graph = TaskGraph(tasks=[task("T1", "excavator", 4, 100, "Z1")])
    failure = infeasible(reqs([robot("HL1", "hauler", 3000)], 15), graph)
    assert failure.hint == "Task T1 needs an excavator but no robot of that type exists."


def test_every_sentence_of_a_mixed_core_is_readable_and_counted_once():
    req = reqs([robot("EX1", "excavator", 5000), robot("HL1", "hauler", 3000),
                robot("SV1", "surveyor", 1500)], 15)
    graph = TaskGraph(tasks=[
        task("T1", "excavator", 4, 3000, "Z1"),
        task("T2", "excavator", 3, 3200, "Z2"),
        task("T3", "hauler", 10, 100, "Z3"),
        task("T4", "hauler", 10, 100, "Z4"),
        task("T5", "surveyor", 2, 2000, "Z5"),
    ])
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:EX1", "energy:SV1", "horizon", "robot:HL1"]
    assert failure.hint == (
        "EX1 would need 6,200 Wh but has 5,000 Wh. SV1 would need 2,000 Wh but has 1,500 Wh. "
        "The plan cannot finish within the 15 h horizon: the longest precedence chain alone "
        "needs 10 h; the busiest robot type (hauler) needs at least 20 h, 5 h past the 15 h "
        "deadline. HL1 runs one task at a time, and the 2 tasks only it can do already take "
        "20 h back to back.")
    assert_readable(failure.hint)
    assert budget_rows(req, graph, failure, FAST) == [
        {"kind": "robot", "subject": "EX1", "robots": ["EX1"], "budget_wh": 5000.0,
         "demanded_wh": 6200.0, "travel_wh": 0.0},
        {"kind": "robot", "subject": "SV1", "robots": ["SV1"], "budget_wh": 1500.0,
         "demanded_wh": 2000.0, "travel_wh": 0.0}]


# ----------------------------------------------------------------------------- orders (3k)
# Three zones around a depot (the 3k review's first repro): one robot cannot branch, so the
# least it drives is its cheapest route through them, not the tree that joins them
AROUND = Site(grid=(40, 40), depot=(20, 20), zones=[
    Zone(id="Z1", center=(0, 0)), Zone(id="Z2", center=(39, 0)), Zone(id="Z3", center=(20, 39))])
# The second repro: Z1 4 cells from the depot (20, 1), Z2 33 cells further on
LINE = Site(grid=(40, 40), depot=(20, 1), zones=[
    Zone(id="Z1", center=(20, 5)), Zone(id="Z2", center=(20, 38))])


def test_one_robot_is_charged_its_cheapest_route_not_a_tree():
    """3k review: one 345 Wh excavator, three 100 Wh tasks in three zones around the depot.
    The tree joining the zones to the depot costs 37.437 Wh, the cheapest route the robot can
    drive 50.692 Wh. Priced on the tree, the validator passed the graph and the hint read
    "the excavator tasks need at least 337 Wh in total, 37 Wh of it travel, and cannot be
    split across 1 excavator (345 Wh combined)": sound figures, a false reason. Priced on the
    route (``geometry.route_cost``), both name the shortfall."""
    graph = TaskGraph(tasks=[task(f"T{k}", "excavator", 2, 100, z)
                             for k, z in ((1, "Z1"), (2, "Z2"), (3, "Z3"))])
    req = reqs([robot("EX1", "excavator", 345)], 1000, AROUND)
    zones = [(0, 0), (39, 0), (20, 39)]
    route = min(sum(leg_mwh(p, q) for p, q in itertools.pairwise([(20, 20), *order]))
                for order in itertools.permutations(zones))
    assert route == 50692   # depot, Z3, Z2, Z1 (the star the tree prices is 37,437 mWh)
    inst = _instance(req, graph, FAST, [])
    assert _travel_floor(inst, ["EX1"], [0, 1, 2], hours=False) == route
    verdict = validate_graph(req, graph)
    assert verdict is not None and verdict.conflicts == ["energy:excavator"]
    assert verdict.details == [
        "excavator tasks need at least 351 Wh, 51 Wh of it travel; its 1 robot carries 345 Wh"]
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:EX1"]
    assert failure.hint == "EX1 would need at least 351 Wh, 51 Wh of it travel, but has 345 Wh."
    assert budget_rows(req, graph, failure, FAST) == [
        {"kind": "robot", "subject": "EX1", "robots": ["EX1"], "budget_wh": 345.0,
         "demanded_wh": 350.692, "travel_wh": 50.692}]
    # the figure is the solver's own: the work and that route, to the milli-Wh, fit
    exact = reqs([robot("EX1", "excavator", 350.692)], 1000, AROUND)
    assert isinstance(solve_schedule(exact, graph, FAST), Schedule)


def test_an_order_the_edges_force_is_the_conflict_not_a_split():
    """3k review: one robot runs T1 in Z1, then T2 in Z2, then T3 back in Z1. In its cheapest
    order (Z1, Z1, Z2) its 300 Wh of work and 18.5 Wh of legs fit 326.75 Wh; the order the
    edges force drives 35 Wh. Energy depends on the order since phase 3k, and only robot:R1
    ties the order to time, so the core is the battery, the edge that forces the return and
    robot:R1. The hint used to blame "a split across 1 excavator" and add a sentence about
    hours for robot:R1 with no deadline in the core; it now names the order."""
    graph = TaskGraph(tasks=[task("T1", "excavator", 2, 100, "Z1"),
                             task("T2", "excavator", 2, 100, "Z2", after=("T1",)),
                             task("T3", "excavator", 2, 100, "Z1", after=("T2",))])
    req = reqs([robot("R1", "excavator", 326.75)], 1000, LINE)
    assert validate_graph(req, graph) is None   # the cheapest order fits: no verdict before
    failure = infeasible(req, graph)
    assert failure.conflicts == ["energy:R1", "precedence:T2->T3", "robot:R1"]
    assert failure.hint == (
        "R1 has 327 Wh, which cannot cover its 300 Wh of work and the legs of the order the "
        "precedence edges force; the cheapest order alone drives 19 Wh. T3 cannot start until "
        "T2 finishes (T2 takes 2 h); the precedence chain through this edge is 6 h long.")
    assert "split" not in failure.hint and "transit" not in failure.hint
    assert_readable(failure.hint)
    assert budget_rows(req, graph, failure, FAST) == [
        {"kind": "order", "subject": "R1", "robots": ["R1"], "budget_wh": 326.75,
         "demanded_wh": 318.5, "travel_wh": 18.5}]
    # the forced order itself (2 + 16.5 + 16.5 Wh of legs) fits 335 Wh
    assert isinstance(solve_schedule(reqs([robot("R1", "excavator", 335)], 1000, LINE), graph,
                                     FAST), Schedule)
    # a deadline in another core (the hauler's 20 h against 15 h) leaves this one as it was;
    # the hauler's robot group keeps its sentence about hours
    hauled = TaskGraph(tasks=[*graph.tasks, task("T4", "hauler", 10, 100, "Z1"),
                              task("T5", "hauler", 10, 100, "Z1")])
    failure = infeasible(reqs([robot("R1", "excavator", 326.75), robot("HL1", "hauler", 3000)],
                              15, LINE), hauled)
    assert failure.conflicts == ["energy:R1", "horizon", "precedence:T2->T3", "robot:HL1",
                                 "robot:R1"]
    assert failure.hint.startswith(
        "R1 has 327 Wh, which cannot cover its 300 Wh of work and the legs of the order the "
        "precedence edges force; the cheapest order alone drives 19 Wh.")
    assert "R1 runs one task" not in failure.hint and "HL1 runs one task at a time" in failure.hint


# ----------------------------------------------------------------------------- budget rows
def test_budget_rows_ignore_failures_without_energy_conflicts_or_with_unknown_robots():
    req, graph = fixture("tight_battery", "taskgraph_1.json")
    assert budget_rows(req, graph, Failure(kind="infeasible", conflicts=["horizon"])) == []
    assert budget_rows(req, graph, Failure(kind="solver_timeout")) == []
    # a validator verdict names a type, not a robot: nothing to tabulate, nothing raised
    assert budget_rows(req, graph, Failure(kind="validation",
                                           conflicts=["energy:excavator", "energy:GHOST"])) == []
    # and the rows survive a JSON round trip as the step payload will carry them
    failure = infeasible(req, graph)
    rows = budget_rows(req, graph, failure, FAST)
    assert json.loads(json.dumps(rows)) == rows


def test_recorded_fixtures_other_than_tight_battery_still_schedule():
    """The copy changes touch hints only: the accepted graphs still solve."""
    for brief, graph_file in (("landing_pad_v1", "taskgraph_1.json"),
                              ("access_road", "taskgraph_1.json"),
                              ("tight_battery", "taskgraph_2.json"),
                              ("landing_pad", "taskgraph_3.json")):   # the live take's last repair
        req, graph = fixture(brief, graph_file)
        out = solve_schedule(req, graph, FAST)
        assert not isinstance(out, Failure), (brief, out)


# ----------------------------------------------------------------------------- fixtures
def test_landing_pad_explanation_names_battery_as_the_tighter_constraint():
    text = json.loads((FIXTURES / "landing_pad_v1" / "explain.json").read_text())["text"]
    assert "Battery is the tighter constraint: PR1 spends about 3,400 of its 4,000 Wh" in text
    assert "Battery is:" not in text
