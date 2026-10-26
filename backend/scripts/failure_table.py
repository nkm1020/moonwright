"""Every single-robot failure of a recorded plan, replanned: the table the site's "break a robot" panel
reads when no backend is running.

    uv run python scripts/failure_table.py ../frontend/public/samples/landing_pad.plan.json \
        ../design/landing-1/data/landing_pad.failures.json --brief-id landing_pad --step 1

For each robot of the plan and each whole hour from 0 to the makespan (every `--step` hours), it
runs the same code as `POST /simulate` with the backend in fixture mode (`run_with_replanning`, the
fixture client, 3 repair rounds, 2 replans): CP-SAT reschedules the remaining work first, and an
infeasible replan escalates to Nemotron, which in fixture mode has no recorded answer, so the
previous plan stays in force. A live backend on Token Factory would ask Nemotron to re-scope there;
the table records the solver's verdict, which is what decides it. Rows:

    robot, t_h, task (the task it was working on or heading to), progress (0..1 of that task),
    outcome: "replanned" (a verified replan, finished_at_h set), "infeasible" (the surviving fleet
    provably cannot do the rest: core lists the constraint groups), "no_effect" (the robot had no
    work left, nothing to replan), plus finished_at_h, delay_h against the plan's makespan,
    remaining (tasks rescheduled), solve_s, message (the replan step's own sentence).

`--frames out.json` also writes every case's motion for the site's 3D replay, in the shape the page
reads (`design/landing-1/break.js`): {"robots": [ids], "cases": {"HL2@24": [[t, [[x, y, status,
task], ... one per robot]], ...]}}, hourly frames (frame_stride 4 at the 0.25 h step, as the live
call asks for), status 0 idle, 1 moving, 2 working, 3 failed, 4 done, positions in grid cells. A
frame identical to both its neighbours is left out (the page interpolates between the ones kept).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from moonwright.api import SimulateRequest, simulate
from moonwright.schemas import PlanResult


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("--brief-id", default=None)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--frames", default=None, help="also write every case's hourly frames here")
    args = ap.parse_args()

    plan = PlanResult.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    makespan = plan.schedule.makespan_h
    robots = [r.id for r in plan.requirements.robots]
    rows = []
    cases = {}
    status_code = {"idle": 0, "moving": 1, "working": 2, "failed": 3, "done": 4}
    t0 = time.perf_counter()
    for rid in robots:
        for t in range(0, makespan, args.step):
            req = SimulateRequest(plan=plan, failures=[{"robot": rid, "t_h": float(t)}], brief_id=args.brief_id,
                                  llm="fixture", frame_stride=4 if args.frames else 10_000)
            res = simulate(req)
            if args.frames:
                fr = [[round(f.t_h, 2), [[round(r.pos[0], 2), round(r.pos[1], 2), status_code[r.status], r.current_task]
                                         for r in f.robots]] for f in res.frames]
                cases[f"{rid}@{t}"] = [f for i, f in enumerate(fr)
                                       if i in (0, len(fr) - 1) or f[1] != fr[i - 1][1] or f[1] != fr[i + 1][1]]
            ev = next((e for e in res.events if e.kind == "robot_failure" and e.robot == rid), None)
            task = ev.task if ev else None
            m = re.search(r"\((\d+)% done\)", (ev.detail or "") if ev else "")
            row = {"robot": rid, "t_h": t, "task": task, "progress": int(m.group(1)) / 100 if m else 0.0}
            if not res.replans:
                row.update(outcome="no_effect", finished_at_h=res.finished_at_h, delay_h=(
                    res.finished_at_h - makespan if res.finished_at_h is not None else None))
            else:
                rec = res.replans[0]
                msg = rec.after.steps[-1].message if rec.after.steps else ""
                solve = re.search(r"in ([\d.]+) s", msg)   # "in under 0.01 s" stays None: the page shows it so
                if rec.after.success and rec.after.schedule is not None and res.completed:
                    row.update(outcome="replanned", finished_at_h=res.finished_at_h,
                               delay_h=res.finished_at_h - makespan)
                else:
                    core = re.findall(r"([a-z_]+:[A-Za-z0-9_>\-]+)", msg.split("->")[0])
                    row.update(outcome="infeasible", finished_at_h=None, delay_h=None, core=core)
                row.update(remaining=len(rec.remaining_tasks), solve_s=float(solve.group(1)) if solve else None,
                           message=msg)
            rows.append(row)
        print(f"{rid}: {sum(r['robot'] == rid for r in rows)} hours", file=sys.stderr, flush=True)
    out = {"plan": args.plan.rsplit("/", 1)[-1], "makespan_h": makespan,
           "horizon_h": plan.requirements.horizon_h, "step_h": args.step, "robots": robots,
           "computed_with": "moonwright POST /simulate code path, fixture mode (CP-SAT replans; no model call)",
           "rows": rows}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    if args.frames:
        with open(args.frames, "w", encoding="utf-8") as f:
            json.dump({"robots": robots, "cases": cases}, f, separators=(",", ":"))
    print(f"{len(rows)} failures in {time.perf_counter() - t0:.0f} s -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
