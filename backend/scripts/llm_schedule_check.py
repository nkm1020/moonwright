"""Secondary measurement from the PRD: let Nemotron write the schedule itself, then verify it.

For every PlanResult given, the requirements and the (already verified) task graph are sent to
the task-graph model with the scheduling rules spelled out, and the model's schedule is run
through ``check_schedule`` (the same independent verifier the pipeline applies to CP-SAT
output). The violation rate is the evidence for "why a solver is in the loop".

The rules the model is told are exactly the ones the verifier holds it to: precedence, one
task per robot, one robot per zone, battery (work plus 0.5 Wh per cell driven), the horizon,
and travel: consecutive tasks of a robot in different zones must be ``ceil(distance / speed)``
whole hours apart, the first one that far after hour 0 from the depot. The prompt therefore
carries every zone centre (declared or laid out by the planner), the depot and each robot's
speed, and ``check_schedule`` runs with ``SolveOptions()`` so the first leg counts too. Because
a ``robot:<r>`` conflict can be a double booking or a travel gap, each one is classified by
re-checking the robot's entries for overlap, and the summary reports the violation rate both
over every rule (the headline) and with travel-only answers set aside
(``violation_rate_excluding_travel``), so a reader can see how much of the gap is scheduling
proper and how much is driving time.

The model's own ``makespan_h`` is not trusted: it is recomputed from the entries before the
check (so ``makespan`` never shows up as a violation and a wrong claim cannot hide a late
finish), and the claimed figure is kept in ``claimed_makespan_h`` with the count of mismatches
in the summary. A clean answer is then compared with the solver's makespan: equal, worse, or
better (only possible when the solver's own schedule was a time-limited ``feasible`` one, not
``optimal``), with the mean and maximum gap in hours and percent.

Usage (from backend/, needs NEBIUS_API_KEY unless --llm fixture is forced, which has no
schedule fixtures and will simply report llm failures)::

    uv run python scripts/llm_schedule_check.py out/landing_pad.plan.json out/access_road.plan.json \
        --repeat 3 --out out/direct_schedule_check.json

    # model arm: a bare id for the scheduling role, or role=id pairs (comma-separated)
    uv run python scripts/llm_schedule_check.py out/*.plan.json \
        --models nvidia/nemotron-3-super-120b-a12b --out out/direct_schedule_check_super.json

Output: one row per (plan, repeat) with the conflict ids, written as JSON (``--out``) and as a
CSV next to it (``--csv``, default: the JSON path with ``.csv``), plus a summary with the share
of answers that violated at least one constraint, the conflict kinds, and the makespan
comparison described above.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from moonwright.geometry import TRAVEL_WH_PER_CELL, depot_position, zone_positions
from moonwright.llm.base import LLMClient, LLMError
from moonwright.llm.factory import make_client
from moonwright.llm.tokenfactory import MODEL_ENV_VARS
from moonwright.pipeline.schedule import SolveOptions, check_schedule
from moonwright.schemas import PlanResult, Schedule

SCHEDULING_ROLE = "taskgraph"   # the role (and model) asked to write the schedule

SYSTEM = f"""You are a construction scheduler for a multi-robot crew on the lunar surface.
Assign every task to exactly one robot of its robot_type and give it an integer start hour so
that: a task starts only after all tasks in its `after` list have ended; a robot never runs two
tasks at the same time; at most one task is active in a zone at any time; the energy a robot
spends never exceeds its battery_wh; everything ends by horizon_h. end_h = start_h + duration_h.
Travel: robots start at the DEPOT and drive straight lines between zone centres (ZONE CENTRES
below, grid cells) at their speed_cells_per_h. Two consecutive tasks of one robot in different
zones must be at least ceil(distance_in_cells / speed_cells_per_h) whole hours apart (the gap
between the first one's end_h and the next one's start_h), and a robot's first task cannot
start before ceil(distance from the depot / speed) hours. Every cell driven costs
{TRAVEL_WH_PER_CELL:g} Wh of the robot's battery on top of the energy_wh of its tasks.
Minimise the makespan (latest end).
Answer with ONE JSON object: {{"entries": [{{"task": "...", "robot": "...", "start_h": int,
"end_h": int}}, ...], "makespan_h": int, "status": "feasible"}}. No prose, no markdown fences."""

CSV_FIELDS = (
    "plan", "repeat", "model", "llm_error", "latency_s", "input_tokens", "output_tokens",
    "violations", "conflicts", "travel_only", "model_makespan_h", "claimed_makespan_h",
    "solver_makespan_h", "solver_status", "makespan_gap_h",
)
CHECK_OPTS = SolveOptions()   # robots start at the depot at hour 0: the first leg counts too


def recompute_makespan(schedule: Schedule) -> tuple[Schedule, int]:
    """``schedule`` with ``makespan_h`` set to the latest ``end_h`` of its entries, plus the
    figure the model claimed (the entries are the schedule; the claim is just a number)."""
    actual = max((e.end_h for e in schedule.entries), default=0)
    return schedule.model_copy(update={"makespan_h": actual}), schedule.makespan_h


def parse_models(spec: str | None) -> dict[str, str]:
    """``--models``: a bare model id applies to the scheduling role, ``role=id`` pairs
    (comma-separated) set those roles; an unknown role is a ``ValueError``."""
    if not spec or not spec.strip():
        return {}
    overrides: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        role, sep, model = part.partition("=")
        if not sep:
            role, model = SCHEDULING_ROLE, part
        role, model = role.strip(), model.strip()
        if role not in MODEL_ENV_VARS:
            raise ValueError(f"--models: unknown role {role!r}; expected one of "
                             f"{', '.join(MODEL_ENV_VARS)}")
        if not model:
            raise ValueError(f"--models: no model id for role {role!r}")
        overrides[role] = model
    return overrides


def apply_model_arm(overrides: dict[str, str]) -> None:
    """Route the roles in ``overrides`` to the given models for this process: the live client
    reads ``MOONWRIGHT_MODEL_<ROLE>`` when it is built, and a set variable beats ``.env``."""
    for role, model in overrides.items():
        os.environ[MODEL_ENV_VARS[role]] = model


def geometry_block(plan: PlanResult) -> str:
    """The depot and every zone centre the schedule can use, laid out as the planner and the
    simulator lay them out (declared zones keep their centre, the rest are placed)."""
    assert plan.task_graph is not None
    site = plan.requirements.site
    layout = zone_positions(site, (t.zone for t in plan.task_graph.tasks))
    depot = depot_position(site)
    centres = ", ".join(f"{zone} at ({x}, {y})" for zone, (x, y) in sorted(layout.items()))
    return f"DEPOT: ({depot[0]}, {depot[1]})\nZONE CENTRES: {centres}"


def ask_once(client: LLMClient, plan: PlanResult) -> tuple[Schedule | None, str | None, float, dict]:
    assert plan.task_graph is not None
    user = ("REQUIREMENTS:\n" + plan.requirements.model_dump_json(indent=1, exclude={"source_brief"})
            + "\n\n" + geometry_block(plan)
            + "\n\nTASKS:\n" + plan.task_graph.model_dump_json(indent=1))
    t0 = time.perf_counter()
    try:
        schedule, usage = client.complete_json(
            role=SCHEDULING_ROLE, system=SYSTEM, user=user, schema=Schedule, key=None
        )
    except LLMError as e:
        usage = getattr(e, "usage", None)
        return None, f"{type(e).__name__}: {e}", time.perf_counter() - t0, (
            usage.model_dump() if usage is not None else {})
    return schedule, None, time.perf_counter() - t0, usage.model_dump()


def overlapping_robots(schedule: Schedule) -> set[str]:
    """Robots with two entries that share a moment: a ``robot:<r>`` conflict that is a double
    booking rather than a travel gap."""
    out: set[str] = set()
    for robot, entries in schedule.by_robot().items():
        latest = -1
        for e in sorted(entries, key=lambda e: (e.start_h, e.end_h)):
            if e.start_h < latest:
                out.add(robot)
            latest = max(latest, e.end_h)
    return out


def travel_only(conflicts: list[str], schedule: Schedule) -> bool:
    """Whether every conflict is a ``robot:<r>`` whose robot is not double-booked, i.e. the
    answer broke only the travel rule (legs shorter than the whole hours they take)."""
    if not conflicts:
        return False
    busy = overlapping_robots(schedule)
    return all(c.startswith("robot:") and c.removeprefix("robot:") not in busy for c in conflicts)


def check_one(plan: PlanResult, path: Path, rep: int, schedule: Schedule | None, error: str | None,
              latency: float, usage: dict) -> dict[str, Any]:
    """One result row: the model's schedule re-verified (first legs from the depot included)
    with its makespan recomputed, and whether only the travel rule was broken."""
    assert plan.task_graph is not None and plan.schedule is not None
    conflicts: list[str] = []
    model_makespan = claimed = None
    only_travel = None
    if schedule is not None:
        schedule, claimed = recompute_makespan(schedule)
        model_makespan = schedule.makespan_h
        conflicts = check_schedule(plan.requirements, plan.task_graph, schedule, CHECK_OPTS)
        only_travel = travel_only(conflicts, schedule)
    solver_makespan = plan.schedule.makespan_h
    return {
        "plan": path.stem.removesuffix(".plan"),
        "repeat": rep,
        "model": usage.get("model") or None,
        "llm_error": error,
        "latency_s": round(latency, 2),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "usage": usage,
        "conflicts": conflicts,
        "violations": len(conflicts) if schedule is not None else None,
        "travel_only": only_travel,
        "model_makespan_h": model_makespan,
        "claimed_makespan_h": claimed,
        "solver_makespan_h": solver_makespan,
        "solver_status": plan.schedule.status,
        "makespan_gap_h": (model_makespan - solver_makespan
                           if model_makespan is not None else None),
    }


def summarise(rows: list[dict[str, Any]], kinds: Counter[str]) -> dict[str, Any]:
    """Violation rate over the answers that parsed (headline: every rule the model was told,
    travel included; ``violation_rate_excluding_travel`` counts answers whose only defect was
    the travel rule as clean), and the makespan comparison over the clean ones (a clean answer
    with a worse makespan is the "feasible but not optimal" story; a better one can only beat
    a time-limited solver run)."""
    answered = [r for r in rows if r["llm_error"] is None]
    violating = [r for r in answered if r["violations"]]
    only_travel = [r for r in violating if r.get("travel_only")]
    clean = [r for r in answered if not r["violations"]]
    gaps = [r["makespan_gap_h"] for r in clean]
    gap_pcts = [100.0 * r["makespan_gap_h"] / r["solver_makespan_h"]
                for r in clean if r["solver_makespan_h"]]
    return {
        "runs": len(rows),
        "answered": len(answered),
        "violating": len(violating),
        "violation_rate": (len(violating) / len(answered)) if answered else None,
        "travel_only_violating": len(only_travel),
        "violation_rate_excluding_travel": (
            (len(violating) - len(only_travel)) / len(answered)) if answered else None,
        "rules_told_to_the_model": "precedence, robot no-overlap, zone no-overlap, battery incl. "
                                   f"{TRAVEL_WH_PER_CELL:g} Wh/cell of travel, horizon, whole-hour "
                                   "travel between zones and from the depot",
        "conflict_kinds": dict(sorted(kinds.items())),
        "makespan_claim_mismatches": sum(
            1 for r in answered if r["claimed_makespan_h"] != r["model_makespan_h"]),
        "clean": len(clean),
        "clean_rate": (len(clean) / len(answered)) if answered else None,
        "clean_equal_makespan": sum(1 for g in gaps if g == 0),
        "clean_worse_makespan": sum(1 for g in gaps if g > 0),
        "clean_better_makespan": sum(1 for g in gaps if g < 0),
        "clean_worse_makespan_rate": (sum(1 for g in gaps if g > 0) / len(clean)) if clean else None,
        "makespan_gap_mean_h": round(statistics.fmean(gaps), 2) if gaps else None,
        "makespan_gap_max_h": max(gaps) if gaps else None,
        "makespan_gap_mean_pct": round(statistics.fmean(gap_pcts), 2) if gap_pcts else None,
        "models": sorted({r["model"] for r in rows if r["model"]}),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "conflicts": ";".join(row["conflicts"])})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plans", nargs="+", type=Path, help="PlanResult JSON files (verified plans)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--llm", choices=["fixture", "nebius", "record"], default=None)
    ap.add_argument("--models", default=None, metavar="ARM",
                    help="model arm: a bare model id for the scheduling role, or role=id pairs")
    ap.add_argument("--out", type=Path, default=Path("out/direct_schedule_check.json"))
    ap.add_argument("--csv", type=Path, default=None,
                    help="CSV of the rows (default: --out with a .csv suffix)")
    args = ap.parse_args(argv)
    if args.repeat < 1:
        ap.error("--repeat must be >= 1")
    try:
        overrides = parse_models(args.models)
    except ValueError as e:
        ap.error(str(e))
    csv_path = args.csv or args.out.with_suffix(".csv")

    apply_model_arm(overrides)
    client = make_client(args.llm)
    if overrides:
        print("model arm: " + ", ".join(f"{r}={m}" for r, m in overrides.items()), file=sys.stderr)
    rows: list[dict[str, Any]] = []
    kinds: Counter[str] = Counter()
    for path in args.plans:
        plan = PlanResult.model_validate_json(path.read_text(encoding="utf-8"))
        if not plan.success or plan.task_graph is None or plan.schedule is None:
            print(f"skip {path}: not a verified plan", file=sys.stderr)
            continue
        if check_schedule(plan.requirements, plan.task_graph, plan.schedule, CHECK_OPTS):
            print(f"skip {path}: the solver schedule fails re-verification", file=sys.stderr)
            continue
        for rep in range(1, args.repeat + 1):
            schedule, error, latency, usage = ask_once(client, plan)
            row = check_one(plan, path, rep, schedule, error, latency, usage)
            for c in row["conflicts"]:
                kinds[c.split(":")[0]] += 1
            rows.append(row)
            verdict = error or (f"{len(row['conflicts'])} violation(s)"
                                + (" (travel only)" if row["travel_only"] else "")
                                if row["conflicts"] else "clean")
            print(f"{row['plan']:28s} #{rep}  {verdict:40s} makespan {row['model_makespan_h']} "
                  f"(claimed {row['claimed_makespan_h']}) vs solver {row['solver_makespan_h']} "
                  f"{row['solver_status']}  ({latency:.1f}s)")

    summary = summarise(rows, kinds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1),
                        encoding="utf-8")
    write_csv(csv_path, rows)
    print("\nsummary:", json.dumps(summary))
    print(f"-> {args.out}, {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
