"""Replanning evaluation: inject robot failures into verified plans and measure the recovery.

For every verified plan given, every robot that owns scheduled work is failed systematically
at 10 % and at 50 % of its first task (``--fractions``; ``--all-tasks`` repeats that for every
task of the robot) and the solver-first replanner (:func:`moonwright.sim.replan.run_with_replanning`)
is run on the injection. Each injection is classified from the replan record of that failure:

    solver_only           CP-SAT rescheduled the remaining work and the result was verified;
                          Nemotron was not asked
    escalated_verified    the remaining work was provably infeasible, Nemotron was asked and
                          its repair was solver-verified
    escalated_failed      Nemotron was asked and every answer was rejected (the failure kind
                          and the conflicts are kept)
    escalated_no_answer   escalated, but the client had no answer: ``--llm fixture`` without a
                          recorded repair for that plan (counted apart, never a crash)
    not_asked_budget      the solver could not finish and Nemotron was not asked because the
                          ``--max-escalations`` budget was already spent
    solver_failed         not escalated and not verified for another reason (a refused replan)
    no_replan             the failure took no work away (the robot had finished) or never fired
    error                 the simulation raised (every robot failed, ...)

Per injection the row records the solve time of the verified schedule, the model calls'
latency, tokens and cost (``moonwright.llm.prices``), the makespan before and after, the tasks
remaining at the event and whether the mission then completed within the horizon. The summary
(JSON ``--out``, markdown ``--markdown``) gives per plan and overall the solver-only rate,
the escalation rate, the escalated-verified rate and the recovered rate (verified replan),
each with a Wilson 95 % interval, plus solve / LLM latency percentiles and the cost.

    not_asked_sole_type   the failed robot was the only one of its type and still owned work,
                          so the remaining graph can only fail on ``capability:<task>``; with
                          ``--sole-of-type solver`` such injections run without a repair budget
                          (``skip`` leaves them out, ``ask`` escalates them like any other)

Denominators. The headline rates (solver-only, escalated, recovered, within horizon) are over
the *askable* injections (:data:`ASKABLE`): the replanned ones minus ``not_asked_sole_type``
(nobody can recover the work of a lost sole robot without re-typing it, and the model was not
asked) and ``not_asked_budget`` (the model was not asked because the escalation budget was
spent, so the outcome is unknown). Both are counted in their own column and in
``summary["not_asked"]``, so ``solver_only + escalated + solver_failed == askable`` and
``askable + not asked == replanned`` always hold. With ``--all-tasks`` a robot with many
tasks weighs more (one injection per task and fraction); the default (first task only)
weighs every robot with work the same, so it is the headline setting and ``--all-tasks`` the
sensitivity run.

Live calls stop after ``--max-escalations`` escalations (default 10): later injections run
with a repair budget of zero, land in ``not_asked_budget`` when the solver cannot finish, and
the summary says so. The budget counts escalations of every client kind (an offline miss too).
Every escalated row keeps ``verdict_kinds``, the conflict families (``energy``, ``capability``,
``horizon``, ...) of the verdict that made the solver-first replan escalate, and the summary
counts them, so a reader sees how many escalations were lost-only-robot-of-type cases.

Usage (from backend/)::

    uv run python scripts/replan_eval.py out/landing_pad.plan.json out/tight_battery.plan.json \\
        --llm nebius --max-escalations 10 --out out/replan_eval.json --markdown out/replan_eval.md
    uv run python scripts/replan_eval.py out/landing_pad.plan.json --llm fixture --all-tasks

Fixture keys: the replan's model calls are tagged with the plan's stem (``landing_pad`` for
``out/landing_pad.plan.json``), the same key ``moonwright simulate`` uses, so a recorded
``fixtures/<stem>/taskgraph_replan_2.json`` answers the escalation offline. ``--llm record``
writes that file for every escalation of a plan, each overwriting the last: record one
injection per plan at a time (``--robots`` / ``--fractions`` select it).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moonwright.cli import load_verified_plan, plan_stem, wilson_interval
from moonwright.llm.factory import KINDS, make_client
from moonwright.llm.prices import cost_usd, format_usd
from moonwright.pipeline.loop import Planner
from moonwright.schemas import PlanResult, ReplanRecord, SimResult
from moonwright.sim.montecarlo import percentile
from moonwright.sim.replan import run_with_replanning
from moonwright.sim.simulator import FailureInjection

DEFAULT_FRACTIONS = (0.1, 0.5)
DEFAULT_MAX_ESCALATIONS = 10
CATEGORIES = ("solver_only", "escalated_verified", "escalated_failed", "escalated_no_answer",
              "not_asked_budget", "not_asked_sole_type", "solver_failed", "no_replan", "error")
ESCALATED = ("escalated_verified", "escalated_failed", "escalated_no_answer")
NOT_ASKED = ("not_asked_sole_type", "not_asked_budget")
ASKABLE = ("solver_only", *ESCALATED, "solver_failed")   # the headline rates' denominator
REPLANNED = (*ASKABLE, *NOT_ASKED)
SOLE_MODES = ("ask", "solver", "skip")
OUTCOME_CHARS = 160
_EPS = 1e-9


@dataclass(frozen=True)
class Injection:
    plan: str
    robot: str
    robot_type: str
    task: str
    fraction: float
    t_h: float
    sole_of_type: bool = False   # the only robot of its type: its loss leaves work nobody can do


def say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- injections
def parse_fractions(spec: str) -> tuple[float, ...]:
    """``0.1,0.5`` -> fractions of a task's scheduled length, each in [0, 1)."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            raise ValueError(f"--fractions: expected numbers, got {part!r}") from None
        if not 0.0 <= value < 1.0:
            raise ValueError(f"--fractions: {value:g} is outside [0, 1)")
        out.append(value)
    if not out:
        raise ValueError("--fractions: no fraction given")
    return tuple(out)


def injections_for(plan: PlanResult, stem: str, fractions: Sequence[float], *,
                   all_tasks: bool = False, robots: Sequence[str] | None = None,
                   ) -> tuple[list[Injection], list[str]]:
    """One injection per (robot, task, fraction): the robot fails ``fraction`` of the way
    through the scheduled task (its first one unless ``all_tasks``). Returns the injections
    in robot order and the robots that own no scheduled work (nothing to take away)."""
    assert plan.schedule is not None
    by_robot = plan.schedule.by_robot()
    of_type = Counter(r.type for r in plan.requirements.robots)
    wanted = set(robots) if robots else None
    out: list[Injection] = []
    idle: list[str] = []
    for robot in plan.requirements.robots:
        if wanted is not None and robot.id not in wanted:
            continue
        entries = by_robot.get(robot.id, [])
        if not entries:
            idle.append(robot.id)
            continue
        for entry in entries if all_tasks else entries[:1]:
            for fraction in fractions:
                t_h = round(entry.start_h + fraction * (entry.end_h - entry.start_h), 2)
                out.append(Injection(stem, robot.id, robot.type, entry.task, fraction, t_h,
                                     sole_of_type=of_type[robot.type] == 1))
    return out, idle


# --------------------------------------------------------------------------- classification
def record_for(sim: SimResult, robot: str) -> ReplanRecord | None:
    """The replan record of the injected failure (the first one on that robot)."""
    return next((r for r in sim.replans if r.event.robot == robot), None)


def classify(record: ReplanRecord | None, *, budget_exhausted: bool,
             sole_not_asked: bool = False) -> str:
    if record is None:
        return "no_replan"
    after = record.after
    if record.escalated:
        if after.success:
            return "escalated_verified"
        offline = any((s.message or "").startswith("offline:") for s in after.steps
                      if s.stage == "repair")
        return "escalated_no_answer" if offline else "escalated_failed"
    if after.success:
        return "solver_only"
    if sole_not_asked:
        return "not_asked_sole_type"
    return "not_asked_budget" if budget_exhausted else "solver_failed"


def verdict_kinds(record: ReplanRecord | None) -> list[str]:
    """The conflict families of the verdict the solver-first replan opened with (the reason it
    escalated, or would have): ``energy``, ``capability``, ``horizon``, ..."""
    if record is None:
        return []
    verdict = next((s.failure for s in record.after.steps
                    if s.stage in ("validate", "schedule") and not s.ok and s.failure), None)
    if verdict is None:
        return []
    return sorted({c.split(":", 1)[0] for c in verdict.conflicts}) or [verdict.kind]


def row_for(inj: Injection, sim: SimResult | None, record: ReplanRecord | None,
            category: str, horizon_h: int, wall_s: float, error: str | None = None,
            ) -> dict[str, Any]:
    after = record.after if record is not None else None
    usage = after.total_usage() if after is not None else None
    schedule = after.schedule if after is not None and after.success else None
    models = [s.usage.model for s in (after.steps if after else []) if s.usage and s.usage.model]
    cost: float | None = 0.0
    for step in (after.steps if after else []):
        if step.usage and (step.usage.input_tokens or step.usage.output_tokens):
            step_cost = cost_usd(step.usage.model, step.usage.input_tokens,
                                 step.usage.output_tokens, decimals=None)
            cost = None if (cost is None or step_cost is None) else cost + step_cost
    closing = after.steps[-1] if after is not None and after.steps else None
    finished = sim.finished_at_h if sim is not None and sim.completed else None
    return {
        "plan": inj.plan, "robot": inj.robot, "robot_type": inj.robot_type, "task": inj.task,
        "fraction": inj.fraction, "t_h": inj.t_h, "sole_of_type": inj.sole_of_type,
        "category": category,
        "escalated": bool(record is not None and record.escalated),
        "verdict_kinds": verdict_kinds(record),
        "verified": schedule is not None,
        "replans": len(sim.replans) if sim is not None else 0,
        "tasks_remaining": len(record.remaining_tasks) if record is not None else None,
        "makespan_before_h": record.before.makespan_h if record is not None else None,
        "makespan_after_h": schedule.makespan_h if schedule is not None else None,
        "solve_time_s": round(schedule.solve_time_s, 3) if schedule is not None else None,
        "repairs": after.repairs if after is not None else 0,
        "llm_latency_s": round(usage.latency_s, 3) if usage is not None else 0.0,
        "input_tokens": usage.input_tokens if usage is not None else 0,
        "output_tokens": usage.output_tokens if usage is not None else 0,
        "models": ",".join(dict.fromkeys(models)),
        "cost_usd": round(cost, 6) if cost is not None else None,
        "failure_kind": after.failure.kind if after is not None and after.failure else None,
        "conflicts": list(after.failure.conflicts) if after is not None and after.failure else [],
        "completed": bool(sim is not None and sim.completed),
        "finished_at_h": finished,
        "within_horizon": (finished is not None and finished <= horizon_h + _EPS)
                          if sim is not None else None,
        "outcome": closing.message if closing is not None else error,
        "latency_s": round(wall_s, 3),
        "error": error,
    }


# --------------------------------------------------------------------------- summaries
def _rate(k: int, n: int) -> dict[str, Any]:
    low, high = wilson_interval(k, n)
    return {"count": k, "of": n, "rate": (k / n) if n else None,
            "ci_low": round(low, 4), "ci_high": round(high, 4)}


def _stats(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"median": round(percentile(values, 0.5), 3), "p95": round(percentile(values, 0.95), 3),
            "max": round(max(values), 3), "mean": round(statistics.fmean(values), 3)}


def summarise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per-plan / overall figures (module docstring, "Denominators"): the headline rates are
    over the askable injections; the not-asked ones are counted apart."""
    counts = Counter(r["category"] for r in rows)
    replanned = [r for r in rows if r["category"] in REPLANNED]
    askable = [r for r in rows if r["category"] in ASKABLE]
    escalated = [r for r in rows if r["category"] in ESCALATED]
    kinds = Counter(",".join(r["verdict_kinds"]) or "-" for r in escalated)
    recovered = [r for r in askable if r["verified"]]
    costs = [r["cost_usd"] for r in rows]
    deltas = [r["makespan_after_h"] - r["makespan_before_h"] for r in recovered
              if r["makespan_after_h"] is not None and r["makespan_before_h"] is not None]
    return {
        "injections": len(rows),
        "categories": {c: counts.get(c, 0) for c in CATEGORIES},
        "replanned": len(replanned),
        "askable": len(askable),
        "not_asked": {"sole_type": counts.get("not_asked_sole_type", 0),
                      "budget": counts.get("not_asked_budget", 0)},
        "solver_only": _rate(counts.get("solver_only", 0), len(askable)),
        "escalated": _rate(len(escalated), len(askable)),
        "escalation_kinds": dict(sorted(kinds.items())),
        "escalated_sole_of_type": sum(bool(r["sole_of_type"]) for r in escalated),
        "escalated_verified": _rate(counts.get("escalated_verified", 0), len(escalated)),
        "recovered": _rate(len(recovered), len(askable)),
        "completed_within_horizon": _rate(sum(bool(r["within_horizon"]) for r in askable),
                                          len(askable)),
        "solve_time_s": _stats([r["solve_time_s"] for r in rows if r["solve_time_s"] is not None]),
        "llm_latency_s": _stats([r["llm_latency_s"] for r in escalated]),
        "makespan_delta_h": _stats(deltas),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cost_usd": None if any(c is None for c in costs) else round(sum(costs), 6),
    }


def _pct(rate: dict[str, Any]) -> str:
    if not rate["of"]:
        return "-"
    return (f"{rate['count']}/{rate['of']} ({rate['rate']:.0%}, CI "
            f"{rate['ci_low']:.0%}-{rate['ci_high']:.0%})")


def _median(stats: dict[str, float] | None) -> str:
    return f"{stats['median']:.2f} s" if stats else "-"


def summary_table(per_plan: dict[str, dict[str, Any]], overall: dict[str, Any]) -> str:
    """The summary table: every rate is ``k/askable``; the not-asked injections (sole of type
    / escalation budget) have their own column so the categories visibly add up."""
    header = ["plan", "injections", "replanned", "askable", "not asked (sole / budget)",
              "solver-only", "escalated", "escalation cores", "esc. verified", "recovered",
              "within horizon", "median solve", "median LLM", "cost"]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    for name, s in [*per_plan.items(), ("**all**", overall)]:
        kinds = ", ".join(f"{k} {v}" for k, v in s["escalation_kinds"].items()) or "-"
        not_asked = s["not_asked"]
        cells = [name if name.startswith("**") else f"`{name}`", str(s["injections"]),
                 str(s["replanned"]), str(s["askable"]),
                 f"{not_asked['sole_type']} / {not_asked['budget']}",
                 _pct(s["solver_only"]), _pct(s["escalated"]), kinds,
                 _pct(s["escalated_verified"]), _pct(s["recovered"]),
                 _pct(s["completed_within_horizon"]), _median(s["solve_time_s"]),
                 _median(s["llm_latency_s"]), format_usd(s["cost_usd"])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def injection_table(rows: Sequence[dict[str, Any]]) -> str:
    header = ["plan", "robot", "task", "at", "category", "remaining", "makespan", "solve",
              "LLM", "repairs", "outcome"]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    for r in rows:
        makespan = (f"{r['makespan_before_h']} h -> {r['makespan_after_h']} h"
                    if r["makespan_after_h"] is not None
                    else (f"{r['makespan_before_h']} h -> none" if r["makespan_before_h"]
                          is not None else "-"))
        solve = f"{r['solve_time_s']:.2f} s" if r["solve_time_s"] is not None else "-"
        llm = f"{r['llm_latency_s']:.1f} s" if r["llm_latency_s"] else "-"
        outcome = (r["outcome"] or "-").replace("|", "/").replace("\n", " ")
        if len(outcome) > OUTCOME_CHARS:
            outcome = outcome[:OUTCOME_CHARS - 3] + "..."
        robot = r["robot"] + (" (sole of type)" if r["sole_of_type"] else "")
        lines.append("| " + " | ".join([
            f"`{r['plan']}`", robot, r["task"], f"{r['t_h']:g} h ({r['fraction']:.0%})",
            r["category"], str(r["tasks_remaining"] if r["tasks_remaining"] is not None else "-"),
            makespan, solve, llm, str(r["repairs"]), outcome]) + " |")
    return "\n".join(lines)


def markdown(report: dict[str, Any]) -> str:
    s = report["settings"]
    budget = report["budget"]
    parts = [
        "# Replanning evaluation: injected robot failures, solver-first recovery",
        "",
        (f"Generated {report['generated_at']}; llm `{report['llm']}`; "
         f"{len(report['plans'])} plan(s); failures at {', '.join(f'{f:.0%}' for f in s['fractions'])}"
         f" of {'every task' if s['all_tasks'] else 'the first task'} of every robot with work; "
         f"max_repairs {s['max_repairs']}, max_replans {s['max_replans']}, dt {s['dt_h']:g} h, "
         f"CP-SAT time limit {s['time_limit_s']:g} s; sole-of-type robots: {s['sole_of_type']}."),
        "",
        "## How to read",
        "",
        ("Every injection fails one robot mid-task; the agent locks the tasks underway, drops the "
         "robot and asks CP-SAT to reschedule the remaining work first. `solver-only` is the "
         "share of replans CP-SAT verified on its own (Nemotron not asked); `escalated` the share "
         "whose remaining work was provably infeasible, so the solver's conflict core went to "
         "Nemotron; `esc. verified` how many escalations came back with a repair the solver "
         "verified; `recovered` the share of replans that ended with a verified schedule either "
         "way; `within horizon` the share whose mission then finished by the deadline in the "
         "simulator. Every one of these rates is over the `askable` injections: `replanned` "
         "minus `not asked`, which is `not_asked_sole_type` (the failed robot was the only one "
         "of its type and still owned work, so the remaining graph can only fail on "
         "`capability` and, under `--sole-of-type solver`, Nemotron is deliberately not asked: "
         "nobody can recover that work without re-typing it) plus `not_asked_budget` (the "
         "`--max-escalations` budget was spent before this injection, so its outcome is "
         "unknown); the `not asked (sole / budget)` column counts them, so `solver-only + "
         "escalated + solver_failed = askable` and `askable + not asked = replanned`. "
         "Intervals are Wilson 95 %. `median solve` is CP-SAT's time for the verified "
         "schedule, `median LLM` the model calls' latency of an escalation; cost is at the Token "
         "Factory list prices. An escalation without an answer (`escalated_no_answer`) is an "
         "offline run without a recorded repair, not a model failure. `escalation cores` counts "
         "the conflict families behind the escalations: `capability` means the failed robot "
         "was the only one of its type (a repair can only re-type that work), `energy` / "
         "`horizon` that the surviving fleet lacks battery or hours for the work as scoped. "
         "With `--all-tasks` a robot with many tasks weighs more (one injection per task and "
         "fraction) and a late-task injection is easy; the first-task default weighs every "
         "robot with work the same."),
        "",
    ]
    if budget["exhausted_after"] is not None:
        parts += [(f"**Escalation budget**: the budget of {budget['max_escalations']} escalations "
                   f"was reached after injection {budget['exhausted_after']}; the remaining "
                   f"{budget['not_asked']} injection(s) ran solver-only and Nemotron was not asked "
                   "(`not_asked_budget`)."), ""]
    else:
        parts += [(f"Escalations: {budget['escalations']} of a budget of "
                   f"{budget['max_escalations']}."), ""]
    if report["idle_robots"]:
        idle = "; ".join(f"`{p}`: {', '.join(r)}" for p, r in report["idle_robots"].items())
        parts += [f"Robots without scheduled work were not injected: {idle}.", ""]
    parts += ["## Summary", "", summary_table(report["summary"]["per_plan"],
                                                report["summary"]["overall"]), "",
              "## Injections", "", injection_table(report["rows"]), ""]
    return "\n".join(parts)


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plans", nargs="+", type=Path, help="PlanResult JSON files (verified plans)")
    ap.add_argument("--llm", choices=KINDS, default=None,
                    help="LLM backend for escalations (default: MOONWRIGHT_LLM / key / fixture)")
    ap.add_argument("--max-escalations", type=int, default=DEFAULT_MAX_ESCALATIONS, metavar="N",
                    help=f"stop asking the model after N escalations (default {DEFAULT_MAX_ESCALATIONS})")
    ap.add_argument("--fractions", default=",".join(f"{f:g}" for f in DEFAULT_FRACTIONS),
                    help="where in the task the robot fails, fractions of its scheduled length")
    ap.add_argument("--all-tasks", action="store_true",
                    help="inject into every task of every robot, not only the first")
    ap.add_argument("--robots", default=None, help="only these robot ids (comma-separated)")
    ap.add_argument("--sole-of-type", choices=SOLE_MODES, default="ask", dest="sole_of_type",
                    help="a robot that is the only one of its type: escalate like any other "
                         "(ask), replan without asking the model (solver) or leave it out (skip)")
    ap.add_argument("--max-repairs", type=int, default=3, help="repair rounds per escalation")
    ap.add_argument("--max-replans", type=int, default=2, help="replans per simulation run")
    ap.add_argument("--time-limit", type=float, default=10.0, metavar="S",
                    help="CP-SAT time limit per replan")
    ap.add_argument("--dt", type=float, default=0.25, help="simulation step in hours")
    ap.add_argument("--out", type=Path, default=Path("out/replan_eval.json"))
    ap.add_argument("--markdown", type=Path, default=None, metavar="PATH")
    args = ap.parse_args(argv)
    if args.max_escalations < 0 or args.max_repairs < 0 or args.max_replans < 1:
        ap.error("--max-escalations and --max-repairs must be >= 0, --max-replans >= 1")
    if args.time_limit <= 0 or args.dt <= 0:
        ap.error("--time-limit and --dt must be positive")
    try:
        fractions = parse_fractions(args.fractions)
    except ValueError as e:
        ap.error(str(e))
    robots = [r.strip() for r in args.robots.split(",") if r.strip()] if args.robots else None

    client = make_client(args.llm)
    planners = {
        True: Planner(client, max_repairs=args.max_repairs, solver_time_limit_s=args.time_limit,
                      explain=False),
        False: Planner(client, max_repairs=0, solver_time_limit_s=args.time_limit, explain=False),
    }
    rows: list[dict[str, Any]] = []
    idle_robots: dict[str, list[str]] = {}
    plans: list[str] = []
    escalations = 0
    exhausted_after: int | None = None
    for path in args.plans:
        try:
            plan = load_verified_plan(path)
        except (OSError, ValueError) as e:
            say(f"skip {path}: {e}")
            continue
        stem = plan_stem(path)
        plans.append(stem)
        injections, idle = injections_for(plan, stem, fractions, all_tasks=args.all_tasks,
                                          robots=robots)
        if args.sole_of_type == "skip":
            injections = [i for i in injections if not i.sole_of_type]
        if idle:
            idle_robots[stem] = idle
        say(f"{stem}: {len(injections)} injection(s) over {len(plan.requirements.robots) - len(idle)}"
            f" robot(s) with work" + (f", idle {', '.join(idle)}" if idle else ""))
        for inj in injections:
            sole_quiet = inj.sole_of_type and args.sole_of_type == "solver"
            may_ask = escalations < args.max_escalations and not sole_quiet
            t0 = time.perf_counter()
            sim: SimResult | None = None
            record = None
            error = None
            try:
                sim = run_with_replanning(
                    planners[may_ask], plan, [FailureInjection(inj.robot, inj.t_h)],
                    key=stem, max_replans=args.max_replans, dt_h=args.dt)
                record = record_for(sim, inj.robot)
                category = classify(record, budget_exhausted=escalations >= args.max_escalations,
                                    sole_not_asked=sole_quiet)
            except ValueError as e:
                category, error = "error", f"{type(e).__name__}: {e}"
            if record is not None and record.escalated:
                escalations += 1
                if escalations >= args.max_escalations and exhausted_after is None:
                    exhausted_after = len(rows) + 1
            row = row_for(inj, sim, record, category, plan.requirements.horizon_h,
                          time.perf_counter() - t0, error)
            rows.append(row)
            makespan = (f"{row['makespan_before_h']}h -> {row['makespan_after_h']}h"
                        if row["makespan_after_h"] is not None else "no verified replan")
            extra = f" llm {row['llm_latency_s']:.1f}s" if row["llm_latency_s"] else ""
            core = f" ({','.join(row['verdict_kinds'])})" if row["verdict_kinds"] else ""
            say(f"  {inj.robot}@{inj.t_h:g}h ({inj.task} {inj.fraction:.0%}): {category}{core}, "
                f"{makespan}, {row['latency_s']:.1f}s{extra}"
                + (f" [{error}]" if error else ""))

    per_plan = {stem: summarise([r for r in rows if r["plan"] == stem]) for stem in plans}
    overall = summarise(rows)
    not_asked = sum(1 for r in rows if r["category"] == "not_asked_budget")
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "llm": client.name,
        "plans": plans,
        "settings": {"fractions": list(fractions), "all_tasks": args.all_tasks,
                     "robots": robots, "sole_of_type": args.sole_of_type,
                     "max_repairs": args.max_repairs,
                     "max_replans": args.max_replans, "dt_h": args.dt,
                     "time_limit_s": args.time_limit, "max_escalations": args.max_escalations},
        "budget": {"max_escalations": args.max_escalations, "escalations": escalations,
                   "exhausted_after": exhausted_after, "not_asked": not_asked},
        "idle_robots": idle_robots,
        "summary": {"overall": overall, "per_plan": per_plan},
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    outputs = [str(args.out)]
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(report), encoding="utf-8")
        outputs.append(str(args.markdown))
    print(summary_table(per_plan, overall))
    if exhausted_after is not None:
        print(f"\nescalation budget of {args.max_escalations} reached after injection "
              f"{exhausted_after}: {not_asked} later injection(s) ran without asking Nemotron")
    else:
        print(f"\nescalations: {escalations} of {args.max_escalations}")
    print("-> " + ", ".join(outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
