"""Moonwright's side of the Isaac Sim loop: one telemetry frame in, one verified replan out.

Isaac Sim runs on Python 3.11 and the backend needs 3.13, so mission.py writes what it measured to
a JSON file and runs this script in the backend's environment:

    uv run --project backend --no-dev python tools/isaac/replan_bridge.py in.json out.json

in.json:  {"plan": PlanResult the mission started from, "graph": TaskGraph in force,
           "schedule": Schedule in force, "frame": SimFrame measured in Isaac, "event": Event}
out.json: the ReplanRecord from moonwright.sim.replan.replan_after_event (record.after.schedule in
          absolute mission hours), plus "wall_s", the time the replan took.

The planner is the solver-only one the Monte Carlo uses: CP-SAT reschedules the remaining work with
the failed robot removed and running tasks locked, which is what the 2D simulator's replans do when
no repair is needed. No Nemotron call is made here (Isaac's machine holds no API key)."""
import json
import sys
import time

from moonwright.schemas import Event, PlanResult, Schedule, SimFrame, TaskGraph
from moonwright.sim.montecarlo import solver_only_planner
from moonwright.sim.replan import replan_after_event

src, dst = sys.argv[1], sys.argv[2]
data = json.load(open(src, encoding="utf-8"))
plan = PlanResult.model_validate(data["plan"])
graph = TaskGraph.model_validate(data["graph"])
schedule = Schedule.model_validate(data["schedule"])
frame = SimFrame.model_validate(data["frame"])
event = Event.model_validate(data["event"])

t0 = time.perf_counter()
record = replan_after_event(solver_only_planner(), plan.requirements, graph, schedule, frame, event,
                            key="isaac")
out = record.model_dump(mode="json")
out["wall_s"] = round(time.perf_counter() - t0, 3)
json.dump(out, open(dst, "w", encoding="utf-8"))
after = record.after
print(f"replan at {frame.t_h:.2f} h: success={after.success} "
      f"{after.steps[-1].message if after.steps else ''} ({out['wall_s']} s)", flush=True)
