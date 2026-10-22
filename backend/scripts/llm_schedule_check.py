"""Secondary measurement from the PRD: let Nemotron write the schedule itself, then verify it.

For every PlanResult given, the requirements and the (already verified) task graph are sent to
the task-graph model with the scheduling rules spelled out, and the model's schedule is run
through ``check_schedule`` (the same independent verifier the pipeline applies to CP-SAT
output). The violation rate is the evidence for "why a solver is in the loop".

Usage (from backend/, needs NEBIUS_API_KEY unless --llm fixture is forced, which has no
schedule fixtures and will simply report llm failures)::

    uv run python scripts/llm_schedule_check.py out/landing_pad.plan.json out/access_road.plan.json \
        --repeat 3 --out out/direct_schedule_check.json

Output: one row per (plan, repeat) with the conflict ids, and a summary with the share of
answers that violated at least one constraint, the conflict kinds, and the model's makespan
against the solver's optimum.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from moonwright.llm.base import LLMError
from moonwright.llm.factory import make_client
from moonwright.pipeline.schedule import check_schedule
from moonwright.schemas import PlanResult, Schedule

SYSTEM = """You are a construction scheduler for a multi-robot crew on the lunar surface.
Assign every task to exactly one robot of its robot_type and give it an integer start hour so
that: a task starts only after all tasks in its `after` list have ended; a robot never runs two
tasks at the same time; at most one task is active in a zone at any time; the total energy_wh
of the tasks assigned to a robot never exceeds that robot's battery_wh; everything ends by
horizon_h. end_h = start_h + duration_h. Minimise the makespan (latest end).
Answer with ONE JSON object: {"entries": [{"task": "...", "robot": "...", "start_h": int,
"end_h": int}, ...], "makespan_h": int, "status": "feasible"}. No prose, no markdown fences."""


def ask_once(client, plan: PlanResult) -> tuple[Schedule | None, str | None, float, dict]:
    user = ("REQUIREMENTS:\n" + plan.requirements.model_dump_json(indent=1, exclude={"source_brief"})
            + "\n\nTASKS:\n" + plan.task_graph.model_dump_json(indent=1))
    t0 = time.perf_counter()
    try:
        schedule, usage = client.complete_json(
            role="taskgraph", system=SYSTEM, user=user, schema=Schedule, key=None
        )
    except LLMError as e:
        return None, f"{type(e).__name__}: {e}", time.perf_counter() - t0, {}
    return schedule, None, time.perf_counter() - t0, usage.model_dump()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plans", nargs="+", type=Path, help="PlanResult JSON files (verified plans)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--llm", choices=["fixture", "nebius", "record"], default=None)
    ap.add_argument("--out", type=Path, default=Path("out/direct_schedule_check.json"))
    args = ap.parse_args(argv)

    client = make_client(args.llm)
    rows: list[dict] = []
    kinds: Counter[str] = Counter()
    for path in args.plans:
        plan = PlanResult.model_validate_json(path.read_text())
        if not plan.success or plan.task_graph is None or plan.schedule is None:
            print(f"skip {path}: not a verified plan", file=sys.stderr)
            continue
        for rep in range(1, args.repeat + 1):
            schedule, error, latency, usage = ask_once(client, plan)
            conflicts = check_schedule(plan.requirements, plan.task_graph, schedule) if schedule else []
            for c in conflicts:
                kinds[c.split(":")[0]] += 1
            row = {
                "plan": path.stem, "repeat": rep, "llm_error": error, "latency_s": round(latency, 2),
                "usage": usage, "conflicts": conflicts,
                "violations": len(conflicts) if schedule else None,
                "model_makespan_h": schedule.makespan_h if schedule else None,
                "solver_makespan_h": plan.schedule.makespan_h,
            }
            rows.append(row)
            verdict = error or (f"{len(conflicts)} violation(s)" if conflicts else "clean")
            print(f"{path.stem:28s} #{rep}  {verdict:40s} makespan {row['model_makespan_h']} "
                  f"vs solver {plan.schedule.makespan_h}  ({latency:.1f}s)")

    answered = [r for r in rows if r["llm_error"] is None]
    violating = [r for r in answered if r["violations"]]
    summary = {
        "runs": len(rows), "answered": len(answered), "violating": len(violating),
        "violation_rate": (len(violating) / len(answered)) if answered else None,
        "conflict_kinds": dict(kinds),
        "clean_runs_with_worse_makespan": sum(
            1 for r in answered if not r["violations"]
            and r["model_makespan_h"] > r["solver_makespan_h"]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print("\nsummary:", json.dumps(summary))
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
