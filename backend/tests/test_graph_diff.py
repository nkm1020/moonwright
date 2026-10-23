"""The repair diff (:func:`graph_diff`) pairs tasks on a stable identity (:func:`match_tasks`)
instead of the id alone, so a repair that splits one task and shifts every later id by one is
reported as ``renumbered`` and not as a re-scoping of every shifted task.

Covered: the recorded tight_battery repair pair, each matching rule (a name and slot, unique
or repeated; a name in another slot; a unique slot; a kept id), the no-double-match rule, an
unchanged graph, quantities compared to two decimals, renames, predecessor lists compared
through the renumbering and written in the answer's ids, a dropped dependency, and a replan's
diff (the scope guards that read ids are not loosened by the pairing).
"""

from __future__ import annotations

from typing import Any

from moonwright.llm.base import FIXTURES_DIR
from moonwright.pipeline.loop import check_locked_tasks, graph_diff, match_tasks
from moonwright.pipeline.validate import check_scope
from moonwright.schemas import ScheduleEntry, Task, TaskGraph

TIGHT = FIXTURES_DIR / "tight_battery"
ONE_PASS = TaskGraph.model_validate_json((TIGHT / "taskgraph_1.json").read_text())
TWO_HALVES = TaskGraph.model_validate_json((TIGHT / "taskgraph_2.json").read_text())
EMPTY: dict[str, Any] = {"added": [], "removed": [], "changed": [], "renumbered": []}


def task(tid: str, name: str, robot_type: str = "excavator", zone: str = "Z1",
         **fields: Any) -> Task:
    fields.setdefault("duration_h", 4)
    fields.setdefault("energy_wh", 100)
    return Task(id=tid, name=name, robot_type=robot_type, zone=zone, **fields)


def graph(*tasks: Task) -> TaskGraph:
    return TaskGraph(tasks=list(tasks))


def lists(diff: dict[str, Any]) -> dict[str, Any]:
    """The four id-level parts of a diff (the totals are checked separately)."""
    return {k: diff[k] for k in EMPTY}


# --------------------------------------------------------------------------- the recorded pair
def test_recorded_tight_battery_repair_is_a_split_and_a_shift_not_a_rescoping():
    """taskgraph_1 -> taskgraph_2: the one-pass bowl T2 became two halves (the new T2 and T3),
    the key trench was cut down (T3, now T4), every later id shifted by one, and no compactor
    or hauler moved anywhere."""
    diff = graph_diff(ONE_PASS, TWO_HALVES)
    assert diff["removed"] == ["T2"]
    assert ONE_PASS.task("T2").name == "Excavate full pad bowl in one pass"
    assert diff["added"] == ["T2", "T3"]
    assert [TWO_HALVES.task(t).name for t in diff["added"]] == [
        "Excavate pad bowl, north half", "Excavate pad bowl, south half"]
    assert diff["renumbered"] == [{"from": f"T{i}", "to": f"T{i + 1}"} for i in range(3, 11)]
    assert diff["changed"] == [   # the borrow pit left the scope: the rename says so
        {"task": "T4", "from": "T3", "field": "name",
         "before": "Cut berm key trench and borrow pit",
         "after": "Cut berm key trench (shallow cut, berm fill from pad spoil)"},
        {"task": "T4", "from": "T3", "field": "duration_h", "before": 24, "after": 14},
        {"task": "T4", "from": "T3", "field": "energy_wh", "before": 4000.0, "after": 2100.0},
    ]
    assert not [row for row in diff["changed"] if row["field"] in ("robot_type", "zone", "after")]
    # the totals never depended on the pairing
    assert diff["per_type_energy_before"] == {"compactor": 3000.0, "excavator": 9900.0,
                                              "hauler": 2000.0, "surveyor": 800.0}
    assert diff["per_type_energy_after"] == {"compactor": 3000.0, "excavator": 8200.0,
                                             "hauler": 2000.0, "surveyor": 800.0}
    assert (diff["total_energy_wh_before"], diff["total_energy_wh_after"]) == (15700.0, 14000.0)
    assert (diff["total_duration_h_before"], diff["total_duration_h_after"]) == (123, 117)
    assert diff["scope_drift_pct"] == -10.8


def test_pairs_of_the_recorded_repair_follow_the_names_then_the_slot():
    pairs = match_tasks(ONE_PASS, TWO_HALVES)
    assert pairs == [("T1", "T1")] + [(f"T{i}", f"T{i + 1}") for i in range(3, 11)]
    for before_id, after_id in pairs:
        if before_id != "T3":   # the key trench was renamed: paired by its slot (rule c)
            assert ONE_PASS.task(before_id).name == TWO_HALVES.task(after_id).name
    # the pairing is symmetric: reading the repair backwards swaps the sides
    assert match_tasks(TWO_HALVES, ONE_PASS) == [(a, b) for b, a in pairs]


# --------------------------------------------------------------------------- the matching rules
def test_a_unique_name_pairs_its_tasks_whatever_else_changed():
    """Spelling, case and punctuation are not a rename, and a task that kept its name pairs
    across a new robot type and zone (rule b), so the move is a change, not a replacement."""
    before = graph(task("T1", "Dig the bowl"), task("T2", "Haul spoil", "hauler", "Z2"))
    after = graph(task("T1", "dig, THE   bowl!", "hauler", "Z3", duration_h=9),
                  task("T2", "Haul spoil", "hauler", "Z2"))
    assert match_tasks(before, after) == [("T1", "T1"), ("T2", "T2")]
    diff = graph_diff(before, after)
    assert lists(diff) == {**EMPTY, "changed": [
        {"task": "T1", "field": "duration_h", "before": 4, "after": 9},
        {"task": "T1", "field": "robot_type", "before": "excavator", "after": "hauler"},
        {"task": "T1", "field": "zone", "before": "Z1", "after": "Z3"},
    ]}   # no "from": the id did not change


def test_repeated_names_that_kept_their_ids_pair_by_id():
    """Two tasks with one name and one slot (a split recorded with one name) pair by id when
    nothing was renumbered, so the second never takes the first's partner."""
    before = graph(task("T1", "Excavate bowl"), task("T2", "Excavate bowl", after=["T1"]))
    after = graph(task("T1", "Excavate bowl", duration_h=19),
                  task("T2", "Excavate bowl", after=["T1"]))
    assert match_tasks(before, after) == [("T1", "T1"), ("T2", "T2")]
    assert graph_diff(before, after)["changed"] == [
        {"task": "T1", "field": "duration_h", "before": 4, "after": 19}]

    # one more of the same, given the next free id and placed between them: by id as well
    before = graph(task("T1", "Haul spoil", "hauler"), task("T2", "Haul spoil", "hauler"))
    after = graph(task("T1", "Haul spoil", "hauler"), task("T3", "Haul spoil", "hauler"),
                  task("T2", "Haul spoil", "hauler"))
    assert match_tasks(before, after) == [("T1", "T1"), ("T2", "T2")]
    assert lists(graph_diff(before, after)) == {**EMPTY, "added": ["T3"]}


def test_repeated_names_shifted_by_an_insertion_pair_in_graph_order():
    """The halves of a split written as "Excavate pad bowl" twice, shifted by one when the
    repair inserts a task before them: old T2, T3 are new T3, T4 (the only pairing consistent
    with a renumbering), never old T3 to new T3 with the halves' figures reported as swapped.
    A hauler fleet's repeated "Haul spoil" in two zones pairs zone by zone the same way."""
    before = graph(task("T1", "Survey", "surveyor"),
                   task("T2", "Excavate pad bowl", duration_h=14, energy_wh=2100, after=["T1"]),
                   task("T3", "Excavate pad bowl", duration_h=10, energy_wh=1500, after=["T2"]),
                   task("T4", "Haul spoil", "hauler", "Z2", after=["T3"]))
    after = graph(task("T1", "Survey", "surveyor"),
                  task("T2", "Stake out apron", "surveyor", "Z4", after=["T1"]),
                  task("T3", "Excavate pad bowl", duration_h=14, energy_wh=2100, after=["T1"]),
                  task("T4", "Excavate pad bowl", duration_h=10, energy_wh=1500, after=["T3"]),
                  task("T5", "Haul spoil", "hauler", "Z2", after=["T4"]))
    assert match_tasks(before, after) == [("T1", "T1"), ("T2", "T3"), ("T3", "T4"), ("T4", "T5")]
    assert lists(graph_diff(before, after)) == {**EMPTY, "added": ["T2"], "renumbered": [
        {"from": "T2", "to": "T3"}, {"from": "T3", "to": "T4"}, {"from": "T4", "to": "T5"}]}

    before = graph(task("T1", "Survey", "surveyor"), task("T2", "Dig", after=["T1"]),
                   task("T3", "Haul spoil", "hauler", "Z2", after=["T2"]),
                   task("T4", "Haul spoil", "hauler", "Z1", after=["T2"]))
    after = graph(task("T1", "Survey", "surveyor"),
                  task("T2", "Stake out", "surveyor", "Z4", after=["T1"]),
                  task("T3", "Dig", after=["T1"]),
                  task("T4", "Haul spoil", "hauler", "Z2", after=["T3"]),
                  task("T5", "Haul spoil", "hauler", "Z1", after=["T3"]))
    assert match_tasks(before, after) == [("T1", "T1"), ("T2", "T3"), ("T3", "T4"), ("T4", "T5")]
    assert lists(graph_diff(before, after)) == {**EMPTY, "added": ["T2"], "renumbered": [
        {"from": "T2", "to": "T3"}, {"from": "T3", "to": "T4"}, {"from": "T4", "to": "T5"}]}


def test_rule_b_pairs_a_unique_name_across_ids_case_whitespace_and_punctuation():
    before = graph(task("T1", "Survey", "surveyor"), task("T4", "Strip approach apron", zone="Z4"))
    after = graph(task("T1", "Survey", "surveyor"), task("T2", "Stake out the apron", "surveyor"),
                  task("T5", "  strip  Approach-Apron! ", zone="Z4"))
    assert match_tasks(before, after) == [("T1", "T1"), ("T4", "T5")]
    assert lists(graph_diff(before, after)) == {
        **EMPTY, "added": ["T2"], "renumbered": [{"from": "T4", "to": "T5"}]}


def test_a_repeated_name_pairs_by_slot_and_an_uneven_count_pairs_nothing():
    """Two before tasks called "Haul spoil" (in different zones) against two after tasks of
    that name: the name alone decides nothing, the name and slot (rule a) pair them zone by
    zone. One "Haul spoil" on the after side against two on the before side is a merge and
    pairs nothing by name."""
    before = graph(task("T1", "Haul spoil", "hauler", "Z1"),
                   task("T2", "Haul spoil", "hauler", "Z2"))
    after = graph(task("T5", "haul spoil", "hauler", "Z2"),
                  task("T6", "HAUL SPOIL", "hauler", "Z1"))
    assert match_tasks(before, after) == [("T2", "T5"), ("T1", "T6")]
    assert lists(graph_diff(before, after)) == {
        **EMPTY, "renumbered": [{"from": "T2", "to": "T5"}, {"from": "T1", "to": "T6"}]}

    after = graph(task("T3", "Haul spoil", "hauler", "Z3"))   # unique here, not there
    assert match_tasks(before, after) == []
    assert lists(graph_diff(before, after)) == {**EMPTY, "added": ["T3"], "removed": ["T1", "T2"]}


def test_rule_c_pairs_a_renamed_task_that_kept_its_slot():
    before = graph(task("T1", "Survey", "surveyor"),
                   task("T3", "Cut berm key trench and borrow pit", zone="Z2", structure="S2",
                        duration_h=24, energy_wh=4000))
    after = graph(task("T1", "Survey", "surveyor"),
                  task("T4", "Cut berm key trench (shallow cut)", zone="Z2", structure="S2",
                       duration_h=14, energy_wh=2100))
    assert match_tasks(before, after) == [("T1", "T1"), ("T3", "T4")]
    assert lists(graph_diff(before, after)) == {
        **EMPTY, "renumbered": [{"from": "T3", "to": "T4"}], "changed": [
            {"task": "T4", "from": "T3", "field": "name",
             "before": "Cut berm key trench and borrow pit",
             "after": "Cut berm key trench (shallow cut)"},
            {"task": "T4", "from": "T3", "field": "duration_h", "before": 24, "after": 14},
            {"task": "T4", "from": "T3", "field": "energy_wh", "before": 4000.0, "after": 2100.0},
        ]}


def test_rule_c_slot_is_robot_type_zone_structure_and_unit():
    """A renamed and renumbered task pairs only with the one that kept all four; another
    structure, unit, zone or robot type is another task. Under the same id the model kept
    on purpose, rule (d) pairs them and the slot field that changed is reported."""
    before = graph(task("T2", "Excavate bowl", structure="S1", quantity=120, unit="m3"))
    for field, value in (("structure", "S2"), ("unit", "m2"), ("zone", "Z2"),
                         ("robot_type", "hauler")):
        slot = {"structure": "S1", "unit": "m3", field: value}
        renumbered = graph(task("T9", "Dig bowl", quantity=120, **slot))
        assert match_tasks(before, renumbered) == [], field
        assert lists(graph_diff(before, renumbered)) == {
            **EMPTY, "added": ["T9"], "removed": ["T2"]}
        same_id = graph(task("T2", "Dig bowl", quantity=120, **slot))
        assert match_tasks(before, same_id) == [("T2", "T2")], field
        assert lists(graph_diff(before, same_id)) == {**EMPTY, "changed": [
            {"task": "T2", "field": "name", "before": "Excavate bowl", "after": "Dig bowl"},
            {"task": "T2", "field": field, "before": before.task("T2").model_dump()[field],
             "after": value}]}
    same_slot = graph(task("T2", "Dig bowl", structure="S1", quantity=130, unit="m3"))
    assert match_tasks(before, same_slot) == [("T2", "T2")]
    assert lists(graph_diff(before, same_slot)) == {**EMPTY, "changed": [
        {"task": "T2", "field": "name", "before": "Excavate bowl", "after": "Dig bowl"},
        {"task": "T2", "field": "quantity", "before": 120.0, "after": 130.0}]}


def test_rule_c_needs_the_slot_to_be_unique_on_both_sides():
    """The halves of a split share the slot of the task they replace: neither takes its
    place, so the split is a removal and two additions (and a merge is the mirror image)."""
    before = graph(task("T1", "Survey", "surveyor"),
                   task("T2", "Excavate full pad bowl in one pass", structure="S1",
                        duration_h=24, energy_wh=4000, after=["T1"]))
    after = graph(task("T1", "Survey", "surveyor"),
                  task("T2", "Excavate pad bowl, north half", structure="S1", duration_h=14,
                       energy_wh=2100, after=["T1"]),
                  task("T3", "Excavate pad bowl, south half", structure="S1", duration_h=14,
                       energy_wh=2100, after=["T2"]))
    assert match_tasks(before, after) == [("T1", "T1")]
    assert lists(graph_diff(before, after)) == {**EMPTY, "added": ["T2", "T3"], "removed": ["T2"]}
    assert lists(graph_diff(after, before)) == {**EMPTY, "added": ["T2"], "removed": ["T2", "T3"]}
    # ... whichever half kept the one-pass task's id: the kept id never pairs across a split
    north = after.task("T2").model_copy(update={"name": "Excavate pad bowl"})
    kept_id = graph(after.tasks[0], north, after.tasks[2])
    assert match_tasks(before, kept_id) == [("T1", "T1")]
    assert lists(graph_diff(before, kept_id)) == {**EMPTY, "added": ["T2", "T3"], "removed": ["T2"]}


def test_rule_d_pairs_a_task_that_kept_its_id_through_a_rename_and_a_move():
    """Renamed and re-zoned under the same id: the model kept the id on purpose, so the diff
    reads "T3 zone Z2 -> Z3" and the rename, not "removed T3, added T3"."""
    before = graph(task("T1", "Survey", "surveyor"),
                   task("T3", "Cut berm key trench", zone="Z2", structure="S2", duration_h=24,
                        energy_wh=4000))
    after = graph(task("T1", "Survey", "surveyor"),
                  task("T3", "Cut berm trench, deeper", zone="Z3", structure="S2",
                       duration_h=24, energy_wh=4000))
    assert match_tasks(before, after) == [("T1", "T1"), ("T3", "T3")]
    assert lists(graph_diff(before, after)) == {**EMPTY, "changed": [
        {"task": "T3", "field": "name", "before": "Cut berm key trench",
         "after": "Cut berm trench, deeper"},
        {"task": "T3", "field": "zone", "before": "Z2", "after": "Z3"}]}


def test_a_repeated_id_appears_once_in_added_and_removed():
    """A graph that repeats an id (the validator's ``duplicate:<id>``) never lists it twice;
    the planner attaches no diff to such an answer at all (see test_loop)."""
    one = graph(task("T1", "a"))
    twice = graph(task("T1", "a"), task("T1", "b", "hauler", "Z2"))
    for left, right in ((one, twice), (twice, one), (twice, twice)):
        diff = graph_diff(left, right)
        for key in ("added", "removed"):
            assert len(diff[key]) == len(set(diff[key])), (key, diff[key])


def test_no_task_is_ever_paired_twice():
    """Each rule consumes the tasks it pairs: a before task paired by its name is not offered
    to the slot rule again, however many after tasks share its slot, and vice versa."""
    before = graph(task("T1", "a"), task("T2", "a"), task("T3", "b", "hauler", "Z2"))
    after = graph(task("T1", "a"), task("T2", "a"), task("T3", "a"),
                  task("T4", "b", "hauler", "Z2"), task("T5", "d", "hauler", "Z2"))
    pairs = match_tasks(before, after)
    assert pairs == [("T1", "T1"), ("T2", "T2"), ("T3", "T4")]
    assert lists(graph_diff(before, after)) == {
        **EMPTY, "added": ["T3", "T5"], "renumbered": [{"from": "T3", "to": "T4"}]}

    for left, right in ((before, after), (after, before), (ONE_PASS, TWO_HALVES),
                        (TWO_HALVES, ONE_PASS), (ONE_PASS, ONE_PASS)):
        pairs = match_tasks(left, right)
        froms, tos = [b for b, _ in pairs], [a for _, a in pairs]
        assert len(set(froms)) == len(froms) and len(set(tos)) == len(tos)
        assert set(froms) <= set(left.ids()) and set(tos) <= set(right.ids())
        diff = graph_diff(left, right)
        assert set(diff["removed"]) == set(left.ids()) - set(froms)
        assert set(diff["added"]) == set(right.ids()) - set(tos)


# --------------------------------------------------------------------------- an unchanged graph
def test_an_unchanged_graph_is_an_empty_diff():
    for g in (ONE_PASS, TWO_HALVES, graph(task("T1", "a"), task("T2", "a"), task("T3", "a"))):
        diff = graph_diff(g, g)
        assert lists(diff) == EMPTY and diff["scope_drift_pct"] == 0.0
        assert match_tasks(g, g) == [(t, t) for t in g.ids()]
        assert diff["per_type_energy_before"] == diff["per_type_energy_after"]
        assert diff["total_energy_wh_before"] == diff["total_energy_wh_after"]
        assert diff["total_duration_h_before"] == diff["total_duration_h_after"]


def test_a_quantity_written_with_more_decimals_is_no_change():
    """Quantities are compared and reported to two decimals: the front_loaded take's third
    repair wrote the pad's 706.86 m2 as 706.858347 m2 and the dig's 141.37 m3 as 141.371669 m3,
    the same work. A change in the second decimal is still reported, rounded."""
    before = graph(task("T1", "Compact pad", "compactor", unit="m2", quantity=706.86),
                   task("T2", "Excavate bowl", unit="m3", quantity=141.37))
    noisy = graph(task("T1", "Compact pad", "compactor", unit="m2", quantity=706.858347),
                  task("T2", "Excavate bowl", unit="m3", quantity=141.371669))
    assert lists(graph_diff(before, noisy)) == EMPTY
    moved = graph(noisy.tasks[0], task("T2", "Excavate bowl", unit="m3", quantity=141.382))
    assert graph_diff(before, moved)["changed"] == [
        {"task": "T2", "field": "quantity", "before": 141.37, "after": 141.38}]


# --------------------------------------------------------------------------- predecessor lists
def test_a_shifted_predecessor_is_not_a_change():
    before = graph(task("T1", "survey", "surveyor"), task("T2", "dig", after=["T1"]),
                   task("T3", "haul", "hauler", after=["T2"]),
                   task("T4", "compact", "compactor", after=["T3"]))
    after = graph(task("T1", "survey", "surveyor"),
                  task("T2", "stake out", "surveyor", after=["T1"]),   # inserted: shifts the rest
                  task("T3", "dig", after=["T1"]), task("T4", "haul", "hauler", after=["T3"]),
                  task("T5", "compact", "compactor", after=["T4"]))
    assert lists(graph_diff(before, after)) == {**EMPTY, "added": ["T2"], "renumbered": [
        {"from": "T2", "to": "T3"}, {"from": "T3", "to": "T4"}, {"from": "T4", "to": "T5"}]}


def test_a_real_edge_change_is_reported_with_both_lists_in_the_answers_ids():
    before = graph(task("T1", "survey", "surveyor"), task("T2", "dig", after=["T1"]),
                   task("T3", "haul", "hauler", after=["T2"]))
    after = graph(task("T1", "survey", "surveyor"), task("T2", "stake out", "surveyor"),
                  task("T3", "dig", after=["T1"]), task("T4", "haul", "hauler", after=["T1"]))
    diff = graph_diff(before, after)
    assert diff["renumbered"] == [{"from": "T2", "to": "T3"}, {"from": "T3", "to": "T4"}]
    assert diff["changed"] == [   # the haul no longer waits for the dig: T2 then, T3 now
        {"task": "T4", "from": "T3", "field": "after", "before": ["T3"], "after": ["T1"]}]


def test_an_edge_that_followed_a_swap_of_ids_reads_in_one_namespace():
    """The model swaps the ids of two digs; the haul that waited for "Dig B" (T3) now waits
    for "Dig A" (the new T3). Written in the before ids the row would read ["T3"] -> ["T3"],
    a change that changes nothing; in the answer's ids it reads ["T2"] -> ["T3"]."""
    before = graph(task("T2", "Dig A"), task("T3", "Dig B", zone="Z2"),
                   task("T4", "Haul", "hauler", "Z2", after=["T3"]))
    after = graph(task("T2", "Dig B", zone="Z2"), task("T3", "Dig A"),
                  task("T4", "Haul", "hauler", "Z2", after=["T3"]))
    assert match_tasks(before, after) == [("T3", "T2"), ("T2", "T3"), ("T4", "T4")]
    assert graph_diff(before, after)["changed"] == [
        {"task": "T4", "field": "after", "before": ["T2"], "after": ["T3"]}]


def test_edges_to_removed_and_added_tasks_belong_to_the_removal_and_the_addition():
    """The dependents of a split task re-point from the one task to its halves: that is the
    split, already reported as removed / added, not a change of the dependents. A new edge to
    a kept task is still a change."""
    before = graph(task("T1", "survey", "surveyor"), task("T2", "dig bowl", after=["T1"]),
                   task("T3", "haul", "hauler", "Z2", after=["T2"]),
                   task("T4", "compact", "compactor", after=["T2"]))
    after = graph(task("T1", "survey", "surveyor"), task("T2", "dig bowl, north", after=["T1"]),
                  task("T3", "dig bowl, south", after=["T2"]),
                  task("T4", "haul", "hauler", "Z2", after=["T3"]),
                  task("T5", "compact", "compactor", after=["T3", "T4"]))
    diff = graph_diff(before, after)
    assert diff["removed"] == ["T2"] and diff["added"] == ["T2", "T3"]
    assert diff["renumbered"] == [{"from": "T3", "to": "T4"}, {"from": "T4", "to": "T5"}]
    assert diff["changed"] == [   # the compaction now also waits for the haul: a real change
        {"task": "T5", "from": "T4", "field": "after", "before": ["T2 (removed)"],
         "after": ["T3", "T4"]}]


def test_a_dependency_dropped_along_with_a_split_task_is_reported():
    """The haul of the bowl spoil waited for the one-pass bowl and the trench; the answer
    splits the bowl and the haul waits for the trench only: it lost its excavation
    dependency without re-pointing to a half, which the forgiveness of a re-point must not
    hide. The mirror image, an edge to an added task taken on without one to a removed task
    given up, is a change as well."""
    before = graph(task("T1", "Survey", "surveyor"),
                   task("T2", "Excavate full pad bowl in one pass", structure="S1",
                        duration_h=24, energy_wh=4000, after=["T1"]),
                   task("T3", "Cut trench", zone="Z2", structure="S2", after=["T1"]),
                   task("T5", "Haul bowl spoil to berm line", "hauler", "Z2", structure="S2",
                        after=["T2", "T3"]))
    after = graph(task("T1", "Survey", "surveyor"),
                  task("T2", "Excavate pad bowl, north half", structure="S1", duration_h=14,
                       energy_wh=2100, after=["T1"]),
                  task("T3", "Excavate pad bowl, south half", structure="S1", duration_h=14,
                       energy_wh=2100, after=["T2"]),
                  task("T4", "Cut trench", zone="Z2", structure="S2", after=["T1"]),
                  task("T6", "Haul bowl spoil to berm line", "hauler", "Z2", structure="S2",
                       after=["T4"]))
    diff = graph_diff(before, after)
    assert diff["removed"] == ["T2"] and diff["added"] == ["T2", "T3"]
    assert diff["changed"] == [
        {"task": "T6", "from": "T5", "field": "after", "before": ["T2 (removed)", "T4"],
         "after": ["T4"]}]

    before = graph(task("T1", "survey", "surveyor"), task("T2", "haul", "hauler", after=["T1"]))
    after = graph(task("T1", "survey", "surveyor"), task("T2", "stake out", "surveyor"),
                  task("T3", "haul", "hauler", after=["T1", "T2"]))
    assert graph_diff(before, after)["changed"] == [
        {"task": "T3", "from": "T2", "field": "after", "before": ["T1"], "after": ["T1", "T2"]}]


# --------------------------------------------------------------------------- a replan's diff
# the remaining work of a replan at 2.5 h: T1 underway (reduced to what is left), T3 done and
# its edge gone, T2 and T4 outstanding
REMAINING = graph(task("T1", "a", "x", duration_h=2, energy_wh=3.75),
                  task("T2", "b", "x", after=["T1"]), task("T4", "d", "y", "Z2"))
LOCKED = [ScheduleEntry(task="T1", robot="R1", start_h=0, end_h=2)]


def test_a_rescoped_replan_answer_diffs_field_by_field():
    rescoped = graph(task("T1", "a", "x", duration_h=2, energy_wh=3.75),
                     task("T2", "b", "x", duration_h=3, after=["T1"]), task("T4", "d", "y", "Z2"))
    diff = graph_diff(REMAINING, rescoped)
    assert lists(diff) == {**EMPTY, "changed": [
        {"task": "T2", "field": "duration_h", "before": 4, "after": 3}]}
    assert diff["scope_drift_pct"] == 0.0
    assert check_scope(rescoped, REMAINING.ids()) is None
    assert check_locked_tasks(rescoped, REMAINING, LOCKED) is None


def test_a_renumbered_replan_answer_is_a_renumbering_in_the_diff_but_the_guards_read_ids():
    """The model hands the remaining work back as T1..T3: the diff pairs d (T4 -> T3) and
    reports no re-scoping, while the scope guard still refuses the answer (``missing:T4``),
    and a running task is judged by its id whatever it was renamed to."""
    renumbered = graph(task("T1", "a", "x", duration_h=2, energy_wh=3.75),
                       task("T2", "b", "x", after=["T1"]), task("T3", "d", "y", "Z2"))
    assert lists(graph_diff(REMAINING, renumbered)) == {
        **EMPTY, "renumbered": [{"from": "T4", "to": "T3"}]}
    verdict = check_scope(renumbered, REMAINING.ids())
    assert verdict is not None and verdict.conflicts == ["missing:T4"]

    renamed = graph(task("T1", "a (finish)", "x", duration_h=1, energy_wh=3.75),
                    task("T2", "b", "x", after=["T1"]), task("T4", "d", "y", "Z2"))
    assert lists(graph_diff(REMAINING, renamed)) == {**EMPTY, "changed": [   # paired by slot
        {"task": "T1", "field": "name", "before": "a", "after": "a (finish)"},
        {"task": "T1", "field": "duration_h", "before": 2, "after": 1}]}
    guard = check_locked_tasks(renamed, REMAINING, LOCKED)
    assert guard is not None and guard.conflicts == ["locked:T1"]
    assert guard.details == ["T1 is underway on R1 and changed: duration_h 1 (needs 2)"]
