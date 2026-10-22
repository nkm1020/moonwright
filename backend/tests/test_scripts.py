"""Tests for the evaluation scripts under ``scripts/`` (no network).

* ``replan_eval.py`` on the offline landing_pad plan: every robot with work is failed at 10 %
  and 50 % of its tasks; the haulers' failures are rescheduled by CP-SAT alone, an excavator
  lost early makes the remaining excavator work provably exceed one battery (escalated, and
  offline the escalation has no recorded answer), the single compactor / printer / surveyor
  are flagged as sole-of-type, and the escalation budget stops the asking.
* ``llm_log_report.py`` on a synthetic call log with a 422, a re-ask, a slow call, a corrupt
  line and an unpriced model, and on the recorded ``out/llm_log.jsonl`` when it exists.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from moonwright.api import EXAMPLES_DIR
from moonwright.cli import EXIT_NO_PLAN, EXIT_OK, main
from moonwright.llm.tokenfactory import DEFAULT_LOG_PATH
from moonwright.schemas import PlanResult

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
SUPER = "nvidia/nemotron-3-super-120b-a12b"


def load_script(name: str):
    """Import ``scripts/<name>.py`` as a module (registered in ``sys.modules`` first: a
    dataclass with postponed annotations looks its module up there)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def landing_pad_plan(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("plans") / "landing_pad.plan.json"
    code = main(["plan", str(EXAMPLES_DIR / "landing_pad.md"), "--llm", "fixture",
                 "--out", str(path), "-q"])
    assert code == EXIT_OK
    return path


# --------------------------------------------------------------------------- replan_eval.py
class TestReplanEval:
    def test_all_tasks_offline_landing_pad(self, landing_pad_plan, tmp_path, capsys):
        module = load_script("replan_eval")
        out, md = tmp_path / "r.json", tmp_path / "r.md"
        code = module.main([str(landing_pad_plan), "--llm", "fixture", "--all-tasks",
                            "--sole-of-type", "solver", "--out", str(out), "--markdown", str(md)])
        assert code == 0
        report = json.loads(out.read_text())
        rows = report["rows"]
        plan = PlanResult.model_validate_json(landing_pad_plan.read_text())
        assert plan.schedule is not None
        by_robot = plan.schedule.by_robot()
        # two fractions per scheduled task of every robot with work; idle robots are listed
        assert len(rows) == sum(2 * len(entries) for entries in by_robot.values())
        idle = [r.id for r in plan.requirements.robots if r.id not in by_robot]
        assert idle and report["idle_robots"] == {"landing_pad": idle}
        assert all(0 <= r["fraction"] < 1 and r["t_h"] >= 0 for r in rows)
        for r in rows:
            entry = plan.schedule.entry(r["task"])
            assert entry.robot == r["robot"]
            assert r["t_h"] == pytest.approx(
                entry.start_h + r["fraction"] * (entry.end_h - entry.start_h), abs=0.01)
        # the second hauler's failures: CP-SAT reschedules the remaining work, nobody is asked
        hl2 = [r for r in rows if r["robot"] == "HL2"]
        assert hl2 and all(r["category"] == "solver_only" for r in hl2)
        assert all(r["verified"] and not r["escalated"] and r["repairs"] == 0 for r in hl2)
        assert all(r["makespan_before_h"] == plan.schedule.makespan_h for r in hl2)
        assert all(r["makespan_after_h"] >= r["makespan_before_h"] for r in hl2)
        assert all(r["completed"] and r["within_horizon"] for r in hl2)
        assert all(r["solve_time_s"] is not None and r["llm_latency_s"] == 0 for r in hl2)
        assert all(r["cost_usd"] == 0.0 and r["verdict_kinds"] == [] for r in hl2)
        assert all("Nemotron not needed" in r["outcome"] for r in hl2)
        # an excavator lost early: the remaining excavator work exceeds one battery, the
        # solver's core says so, the escalation finds no recorded answer offline
        energy = [r for r in rows if r["verdict_kinds"] == ["energy"]]
        assert energy and all(r["robot_type"] == "excavator" for r in energy)
        assert all(r["category"] == "escalated_no_answer" and r["escalated"] for r in energy)
        assert all(r["repairs"] == 1 and not r["verified"] for r in energy)
        assert all(r["makespan_after_h"] is None and r["conflicts"] for r in energy)
        assert all("escalated to Nemotron (no answer: offline, no recorded repair)" in r["outcome"]
                   for r in energy)
        # later excavator tasks are rescheduled by the solver alone
        assert any(r["robot"] == "EX2" and r["category"] == "solver_only" for r in rows)
        # the only compactor / printer / surveyor: flagged, never asked under --sole-of-type solver
        sole = [r for r in rows if r["sole_of_type"]]
        assert {r["robot_type"] for r in sole} == {"compactor", "printer", "surveyor"}
        assert all(r["category"] == "not_asked_sole_type" for r in sole)
        assert all(r["verdict_kinds"] == ["capability"] and r["repairs"] == 0 for r in sole)
        assert all(not r["sole_of_type"] for r in rows if r["robot_type"] in ("excavator", "hauler"))
        # summary: counts add up, rates carry Wilson intervals, nothing was spent
        s = report["summary"]["per_plan"]["landing_pad"]
        assert s == report["summary"]["overall"]
        assert s["injections"] == s["replanned"] == len(rows)
        assert s["categories"]["solver_only"] == sum(r["category"] == "solver_only" for r in rows)
        assert s["categories"]["escalated_no_answer"] == len(energy)
        assert s["categories"]["not_asked_sole_type"] == len(sole)
        assert s["solver_only"]["count"] + s["escalated"]["count"] + len(sole) == len(rows)
        # the headline rates are over the askable injections: the sole-of-type ones (nobody
        # can recover a lost sole robot's work, and the model was not asked) are counted
        # apart, so a replanner that solved 10 of 14 askable cases is not shown as 36 %
        askable = len(rows) - len(sole)
        assert s["askable"] == askable and s["not_asked"] == {"sole_type": len(sole), "budget": 0}
        assert s["solver_only"]["of"] == s["recovered"]["of"] == askable
        assert s["escalated"] == {"count": len(energy), "of": askable,
                                  "rate": pytest.approx(len(energy) / askable),
                                  "ci_low": s["escalated"]["ci_low"],
                                  "ci_high": s["escalated"]["ci_high"]}
        assert 0 < s["escalated"]["ci_low"] < s["escalated"]["rate"] < s["escalated"]["ci_high"] < 1
        assert s["solver_only"]["count"] + s["escalated"]["count"] == askable
        assert s["escalation_kinds"] == {"energy": len(energy)}
        assert s["escalated_sole_of_type"] == 0
        assert s["escalated_verified"]["count"] == 0 and s["escalated_verified"]["of"] == len(energy)
        assert s["recovered"]["count"] == s["solver_only"]["count"]
        assert s["completed_within_horizon"]["count"] == s["solver_only"]["count"]
        assert s["completed_within_horizon"]["of"] == askable
        assert s["cost_usd"] == 0.0 and s["input_tokens"] == 0
        assert s["solve_time_s"]["median"] >= 0 and s["llm_latency_s"]["median"] == 0.0
        assert s["makespan_delta_h"]["max"] >= 0
        assert report["budget"] == {"max_escalations": 10, "escalations": len(energy),
                                    "exhausted_after": None, "not_asked": 0}
        assert report["settings"]["sole_of_type"] == "solver" and report["llm"] == "fixture"
        # the markdown document
        text = md.read_text()
        assert text.startswith("# Replanning evaluation")
        assert "| `landing_pad` |" in text and "escalation cores" in text and "energy" in text
        assert "| HL2 | " in text and "(sole of type)" in text
        assert f"Robots without scheduled work were not injected: `landing_pad`: {', '.join(idle)}." in text
        # the summary row shows the askable denominator and the not-asked column, and the
        # how-to-read names not_asked_sole_type (so 10 + 4 of 28 is not a mystery)
        assert "| not asked (sole / budget) |" in text
        assert f"| `landing_pad` | {len(rows)} | {len(rows)} | {askable} | {len(sole)} / 0 |" in text
        assert "`askable + not asked = replanned`" in text
        assert "## Injections" in text
        assert text.count("not_asked_sole_type") == len(sole) + 1   # the rows + the how-to-read
        captured = capsys.readouterr()
        assert f"escalations: {len(energy)} of 10" in captured.out
        assert captured.out.rstrip().endswith(f"-> {out}, {md}")
        assert "escalated_no_answer (energy)" in captured.err
        assert "not_asked_sole_type (capability)" in captured.err

    def test_escalation_budget_stops_the_asking(self, landing_pad_plan, tmp_path, capsys):
        module = load_script("replan_eval")
        out = tmp_path / "b.json"
        code = module.main([str(landing_pad_plan), "--llm", "fixture", "--max-escalations", "1",
                            "--out", str(out)])
        assert code == 0
        report = json.loads(out.read_text())
        rows = report["rows"]
        escalated = [r for r in rows if r["escalated"]]
        assert len(escalated) == 1 and escalated[0]["category"] == "escalated_no_answer"
        first = rows.index(escalated[0]) + 1
        later = [r for r in rows if r["category"] == "not_asked_budget"]
        assert later and all(r["verdict_kinds"] and r["repairs"] == 0 for r in later)
        assert all(rows.index(r) + 1 > first for r in later)
        assert report["budget"] == {"max_escalations": 1, "escalations": 1,
                                    "exhausted_after": first, "not_asked": len(later)}
        solved = [r for r in rows if r["category"] == "solver_only"]
        assert len(rows) == 1 + len(later) + len(solved)
        # the default fractions hit the first task of every robot with work, twice
        assert sorted({r["fraction"] for r in rows}) == [0.1, 0.5]
        assert all(sum(1 for r in rows if r["robot"] == rid) == 2 for rid in {r["robot"] for r in rows})
        assert f"escalation budget of 1 reached after injection {first}" in capsys.readouterr().out
        # the markdown says so too
        md = tmp_path / "b.md"
        module.main([str(landing_pad_plan), "--llm", "fixture", "--max-escalations", "1",
                     "--out", str(out), "--markdown", str(md)])
        assert "**Escalation budget**" in md.read_text()

    def test_filters_and_usage_errors(self, landing_pad_plan, tmp_path, capsys):
        module = load_script("replan_eval")
        out = tmp_path / "f.json"
        code = module.main([str(landing_pad_plan), "--llm", "fixture", "--sole-of-type", "skip",
                            "--robots", "HL2,SV1", "--fractions", "0.25", "--out", str(out)])
        assert code == 0
        rows = json.loads(out.read_text())["rows"]
        assert [(r["robot"], r["fraction"]) for r in rows] == [("HL2", 0.25)]   # SV1 is sole
        assert module.parse_fractions(" 0.1, 0.5 ") == (0.1, 0.5)
        for bad in ("1.0", "-0.1", "x", ""):
            with pytest.raises(ValueError):
                module.parse_fractions(bad)
        for argv in (["--max-escalations", "-1"], ["--max-replans", "0"], ["--dt", "0"],
                     ["--fractions", "2"], ["--sole-of-type", "never"]):
            with pytest.raises(SystemExit) as info:
                module.main([str(landing_pad_plan), "--llm", "fixture", *argv])
            assert info.value.code == 2, argv
        # an unverified plan file is skipped, not a crash
        unverified = tmp_path / "no.plan.json"
        plan = PlanResult.model_validate_json(landing_pad_plan.read_text())
        unverified.write_text(plan.model_copy(update={"success": False}).model_dump_json())
        capsys.readouterr()
        assert module.main([str(unverified), "--llm", "fixture", "--out", str(out)]) == 0
        report = json.loads(out.read_text())
        assert report["plans"] == [] and report["rows"] == []
        assert report["summary"]["overall"]["injections"] == 0
        assert "no verified schedule" in capsys.readouterr().err

    def test_classify_and_summary_helpers(self):
        module = load_script("replan_eval")
        assert module.classify(None, budget_exhausted=False) == "no_replan"
        empty = module.summarise([])
        assert empty["injections"] == 0 and empty["solver_only"]["rate"] is None
        assert (empty["solver_only"]["ci_low"], empty["solver_only"]["ci_high"]) == (0.0, 1.0)
        assert empty["cost_usd"] == 0.0 and empty["solve_time_s"] is None
        assert empty["askable"] == 0 and empty["not_asked"] == {"sole_type": 0, "budget": 0}
        table = module.summary_table({}, empty)
        assert table.splitlines()[2].startswith("| **all** | 0 | 0 | 0 | 0 / 0 | - | - | - | - |")
        # categories add up to the askable count and the askable count to the replanned one
        rows = [{"category": c, "verified": c in ("solver_only", "escalated_verified"),
                 "verdict_kinds": ["energy"] if c.startswith("escalated") else [],
                 "sole_of_type": c == "not_asked_sole_type", "cost_usd": 0.0,
                 "makespan_after_h": None, "makespan_before_h": 10, "solve_time_s": None,
                 "llm_latency_s": 0.0, "within_horizon": c == "solver_only",
                 "input_tokens": 0, "output_tokens": 0}
                for c in ("solver_only", "solver_only", "escalated_verified", "escalated_failed",
                          "solver_failed", "not_asked_sole_type", "not_asked_sole_type",
                          "not_asked_budget", "no_replan")]
        s = module.summarise(rows)
        assert (s["injections"], s["replanned"], s["askable"]) == (9, 8, 5)
        assert s["not_asked"] == {"sole_type": 2, "budget": 1}
        assert (s["solver_only"]["count"], s["solver_only"]["of"]) == (2, 5)
        assert (s["escalated"]["count"], s["recovered"]["count"]) == (2, 3)
        assert s["completed_within_horizon"] == {"count": 2, "of": 5, "rate": 0.4,
                                                 "ci_low": s["completed_within_horizon"]["ci_low"],
                                                 "ci_high": s["completed_within_horizon"]["ci_high"]}


# --------------------------------------------------------------------------- llm_log_report.py
def record(**over: Any) -> dict[str, Any]:
    base = {
        "ts": "2026-10-24T01:00:00.000+00:00", "role": "structure", "model": LIGHTNING,
        "endpoint": "https://api.tokenfactory.nebius.com/v1/", "regional": False,
        "json_format": "json_schema", "thinking": False, "thinking_switch": True,
        "max_tokens": 4096, "status": 200, "request_id": "req-1", "finish_reason": "stop",
        "prompt_tokens": 2000, "completion_tokens": 400, "latency_s": 2.5, "error": None,
        "error_message": None, "key": "landing_pad", "attempt": 1, "variant": None, "ask": 1,
    }
    base.update(over)
    return base


SYNTHETIC = [
    record(ts="2026-10-20T00:00:00+00:00", request_id="old"),             # before --since
    record(),
    record(ts="2026-10-24T01:00:10+00:00", role="taskgraph", model=ULTRA, thinking=True,
           thinking_switch=False, max_tokens=16384, status=422, request_id="req-422",
           finish_reason=None, prompt_tokens=None, completion_tokens=None, latency_s=0.8,
           error="UnprocessableEntityError", error_message="response_format not supported"),
    record(ts="2026-10-24T01:00:11+00:00", role="taskgraph", model=ULTRA, thinking=True,
           thinking_switch=False, max_tokens=16384, json_format="json_object",
           request_id="req-slow", prompt_tokens=3000, completion_tokens=9000, latency_s=70.0),
    record(ts="2026-10-24T01:01:30+00:00", role="taskgraph", model=ULTRA, thinking=True,
           thinking_switch=False, max_tokens=16384, request_id="req-reask", ask=2,
           finish_reason="length", prompt_tokens=3500, completion_tokens=8000, latency_s=40.0),
    record(ts="2026-10-24T01:02:15+00:00", role="explain", model=SUPER, request_id="req-x",
           prompt_tokens=2500, completion_tokens=300, latency_s=3.0),
]


def write_log(path: Path, records: list[dict[str, Any]], *, corrupt: bool = True) -> Path:
    lines = [json.dumps(r) for r in records]
    if corrupt:
        lines[2:2] = ["", "{not json", json.dumps([1, 2])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLlmLogReport:
    def test_groups_errors_reasks_and_costs(self, tmp_path, capsys):
        module = load_script("llm_log_report")
        log = write_log(tmp_path / "log.jsonl", SYNTHETIC)
        records, skipped = module.read_log(log)
        assert len(records) == 6 and skipped == 2              # blank lines are not counted
        since = module.parse_since("2026-10-24")
        records, _ = module.read_log(log, since)
        assert [r["request_id"] for r in records] == ["req-1", "req-422", "req-slow", "req-reask",
                                                      "req-x"]
        report = module.build_report(records, source="log.jsonl", skipped=skipped, since=since)
        groups = {(g["model"], g["role"]): g for g in report["groups"]}
        assert set(groups) == {(LIGHTNING, "structure"), (ULTRA, "taskgraph"), (SUPER, "explain")}
        ultra = groups[ULTRA, "taskgraph"]
        assert (ultra["calls"], ultra["ok"], ultra["reasks"]) == (3, 2, 1)
        assert ultra["errors"] == {"422 UnprocessableEntityError": 1}
        assert ultra["json_format"] == {"json_schema": 2, "json_object": 1}
        assert ultra["thinking"] == {"on": 3} and ultra["thinking_switch_sent"] == 0
        assert ultra["prompt_tokens"] == {"sum": 6500, "mean": 3250.0}
        assert ultra["completion_tokens"] == {"sum": 17000, "mean": 8500.0}
        assert ultra["completion_tokens_per_s"] == pytest.approx(17000 / 110, abs=0.1)
        assert ultra["latency_s"]["p50"] == 40.0 and ultra["latency_s"]["p95"] == pytest.approx(67.0)
        assert ultra["latency_s"]["max"] == 70.0
        assert ultra["cost_usd"] == pytest.approx(6500 * 1.0 / 1e6 + 17000 * 3.0 / 1e6)
        assert ultra["finish_reasons"] == {"None": 1, "length": 1, "stop": 1}
        lightning = groups[LIGHTNING, "structure"]
        assert lightning["thinking"] == {"off": 1} and lightning["thinking_switch_sent"] == 1
        assert lightning["cost_usd"] == pytest.approx((2000 * 0.06 + 400 * 0.24) / 1e6)
        overall = report["overall"]
        assert overall["calls"] == 5 and overall["ok"] == 4
        assert overall["cost_usd"] == pytest.approx(
            ultra["cost_usd"] + lightning["cost_usd"] + (2500 * 0.3 + 300 * 0.9) / 1e6)
        assert overall["prompt_tokens"]["sum"] == 2000 + 6500 + 2500
        assert report["window"] == {"first": "2026-10-24T01:00:00+00:00",
                                    "last": "2026-10-24T01:02:15+00:00"}
        assert report["models"] == sorted([LIGHTNING, ULTRA, SUPER])
        notable = {n["request_id"]: n["reasons"] for n in report["notable"]}
        assert notable == {"req-422": ["status 422 UnprocessableEntityError"],
                           "req-slow": ["70 s"], "req-reask": ["re-ask #2"]}
        text = module.markdown(report)
        assert text.startswith("# Token Factory call log")
        assert ("| `nvidia/Nemotron-3-Ultra-550b-a55b` | taskgraph | 3 | 2 | "
                "422 UnprocessableEntityError 1 | 1 | json_object 1, json_schema 2 | on 3 | "
                "6500 / 3250 | 17000 / 8500 | 154 | 40.0 s / 67.0 s | $0.0575 | "
                "None 1, length 1, stop 1 |") in text
        assert "| **all** |  | 5 | 4 | 422 UnprocessableEntityError 1 | 1 |" in text
        assert "## Notable requests" in text and "2 unreadable line(s) skipped" in text
        assert "re-ask #2" in text and "status 422" in text and "`response_format not supported`" in text
        # the command line: markdown on stdout, files on request, --since honoured
        md, out = tmp_path / "r.md", tmp_path / "r.json"
        code = module.main([str(log), "--since", "2026-10-24T00:00:00", "--markdown", str(md),
                            "--out", str(out)])
        assert code == 0
        written = md.read_text()
        assert written.startswith("# Token Factory call log\n") and f"Source `{log}`" in written
        assert written.split("## Per model and role", 1)[1] == text.split("## Per model and role", 1)[1]
        saved = json.loads(out.read_text())
        assert saved["overall"]["calls"] == 5 and saved["since"] == "2026-10-24T00:00:00+00:00"
        captured = capsys.readouterr()
        assert "## Per model and role" in captured.out and f"-> {md}, {out}" in captured.err
        assert module.main([str(log)]) == 0
        assert "| **all** |  | 6 |" in capsys.readouterr().out   # without --since: the old call too

    def test_unpriced_model_and_bad_input(self, tmp_path, capsys, monkeypatch):
        module = load_script("llm_log_report")
        log = write_log(tmp_path / "log.jsonl", [
            record(), record(model="nvidia/Nemotron-3.5-Super-VL", role="explain",
                             prompt_tokens=100, completion_tokens=10)], corrupt=False)
        records, skipped = module.read_log(log)
        report = module.build_report(records, source="x")
        assert skipped == 0
        vl = next(g for g in report["groups"] if g["model"].endswith("Super-VL"))
        assert vl["cost_usd"] is None and report["overall"]["cost_usd"] is None
        assert "| - | " in module.markdown(report)
        assert report["notable"] == []
        assert "None: every request answered 200" in module.markdown(report)
        with pytest.raises(ValueError, match="ISO"):
            module.parse_since("yesterday")
        assert module.parse_since(None) is None
        with pytest.raises(SystemExit):
            module.main([str(log), "--since", "yesterday"])
        assert module.main([str(tmp_path / "missing.jsonl")]) == 2
        assert "no call log" in capsys.readouterr().err
        # the default path is the configured log (conftest points it at a tmp file)
        monkeypatch.setenv("MOONWRIGHT_LLM_LOG", str(log))
        assert module.main([]) == 0
        assert "| **all** |  | 2 |" in capsys.readouterr().out
        monkeypatch.setenv("MOONWRIGHT_LLM_LOG", "off")
        with pytest.raises(SystemExit):
            module.main([])

    def test_recorded_log_if_present(self):
        if not DEFAULT_LOG_PATH.is_file():
            pytest.skip("no recorded call log")
        module = load_script("llm_log_report")
        records, skipped = module.read_log(DEFAULT_LOG_PATH)
        assert records and skipped == 0
        report = module.build_report(records, source=str(DEFAULT_LOG_PATH))
        assert report["overall"]["calls"] == len(records)
        assert sum(g["calls"] for g in report["groups"]) == len(records)
        assert all({"model", "role", "calls", "latency_s", "cost_usd"} <= set(g)
                   for g in report["groups"])
        text = module.markdown(report)
        assert "## Per model and role" in text and "## Notable requests" in text


# --------------------------------------------------------------------------- replan advice (CLI)
def test_simulate_prints_replan_advice_in_absolute_hours(landing_pad_plan, tmp_path, capsys):
    """EX2 lost at 10 h: the remaining excavator work is provably infeasible and, offline, the
    escalation has no answer. The advice used to read "feasible at 75 h" under "makespan
    80h" (planner-relative hours, a spare cloned from EX1's remaining 4995 Wh); it is 85 h
    with a full 5000 Wh battery now, in the record and in the step payload alike."""
    out = tmp_path / "ex2.sim.json"
    code = main(["simulate", str(landing_pad_plan), "--fail", "EX2@10", "--llm", "fixture",
                 "--out", str(out)])
    assert code == EXIT_NO_PLAN
    printed = capsys.readouterr().out
    assert "no feasible replan (validation: energy:excavator)" in printed
    assert ("- add robot: one more excavator (EX3, 5000 Wh, a full battery): feasible at 85 h "
            "[CP-SAT verified]") in printed
    sim = json.loads(out.read_text())
    record = sim["replans"][0]
    assert record["before"]["makespan_h"] == 80
    assert record["after"]["requirements"]["horizon_h"] == 326     # relative, as documented
    (s,) = record["after"]["suggestions"]
    assert (s["kind"], s["makespan_h"], s["battery_wh"], s["robot_id"]) == (
        "add_robot", 85, 5000.0, "EX3")
    final = next(st for st in reversed(record["after"]["steps"])
                 if (st.get("payload") or {}).get("suggestions"))
    assert final["payload"]["time_base"] == "relative"
    assert final["payload"]["suggestions_time_base"] == "absolute"
    assert final["payload"]["suggestions"] == [s]
