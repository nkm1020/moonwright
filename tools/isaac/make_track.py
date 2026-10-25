"""Turn a Moonwright SimResult (all frames) into a render track for the Isaac Sim replay.

Positions only replay what the simulator recorded: robots move along the recorded positions;
robots that are idle, done or failed are drawn in parking slots so they do not overlap at the
depot, which shares its cell with zone Z1. Usage: make_track.py <sim.json> <plan.json> <out.json>"""
import json, math, sys

sim = json.load(open(sys.argv[1])); plan = json.load(open(sys.argv[2]))
CELL = 2.0                     # metres per grid cell (the brief's 80 m site on a 40 x 40 grid)
FPS, SECONDS = 24, 18.0        # the clip
req = plan["requirements"]; site = req["site"]
gx, gy = site["grid"]
def world(p): return ((p[0] - gx / 2) * CELL, (p[1] - gy / 2) * CELL)
zones = {z["id"]: {"c": world(z["center"]), "r": z.get("radius", 3) * CELL} for z in site["zones"]}
depot = world(site["depot"])
robots = [r["id"] for r in req["robots"]]
types = {r["id"]: r["type"] for r in req["robots"]}
tasks = {t["id"]: t for t in sim["task_graph"]["tasks"]}
frames = sim["frames"]; T_END = frames[-1]["t_h"]
fail_t = next((e["t_h"] for e in sim["events"] if e["kind"] == "robot_failure"), None)

# time warp: the clip slows to a third of its pace for the hour around the failure
def warp_weights():
    import itertools
    def rate(t): return 3.0 if fail_t is not None and fail_t - 0.5 <= t <= fail_t + 1.0 else 1.0
    steps = [(f["t_h"], rate(f["t_h"])) for f in frames]
    return steps
n_out = int(FPS * SECONDS)
# cumulative "clip time" per sim time, slowed around the failure
cum = [0.0]
for a, b in zip(frames, frames[1:]):
    mid = (a["t_h"] + b["t_h"]) / 2
    slow = 3.0 if fail_t is not None and fail_t - 0.5 <= mid <= fail_t + 1.0 else 1.0
    cum.append(cum[-1] + (b["t_h"] - a["t_h"]) * slow)
def sim_time_at(u):  # u in [0, cum[-1]]
    for i in range(len(cum) - 1):
        if cum[i] <= u <= cum[i + 1]:
            k = (u - cum[i]) / (cum[i + 1] - cum[i]) if cum[i + 1] > cum[i] else 0
            return i, k
    return len(frames) - 2, 1.0

def slot_pos(rid):
    i = robots.index(rid)
    return (depot[0] - 22 - (i % 4) * 10.0, depot[1] + 2 + (i // 4) * 12.0)   # west of the pad, away from the camera

def target(fr, r):
    st, task = r["status"], r["current_task"]
    p = world(r["pos"])
    if st == "failed":             # left where it broke down, pulled aside so its replacement can work
        return (p[0] - 11.0, p[1] + 7.0)
    if st in ("moving",):
        return p
    if st == "working" or (task and fr["active_tasks"] and task in fr["active_tasks"]):
        return p
    # waiting or parked: near a zone if it is away from the depot, else in its slot
    if math.dist(p, depot) < 3.0:
        return slot_pos(r["id"])
    i = robots.index(r["id"])
    for z in zones.values():
        if math.dist(p, z["c"]) <= z["r"] + 1:
            a = 2 * math.pi * i / len(robots)
            return (z["c"][0] + math.cos(a) * (z["r"] + 11), z["c"][1] + math.sin(a) * (z["r"] + 11))
    return p

out = []; pos = {}; head = {}
for k in range(n_out):
    u = cum[-1] * k / (n_out - 1)
    i, a = sim_time_at(u)
    f0, f1 = frames[i], frames[i + 1]
    t = f0["t_h"] + (f1["t_h"] - f0["t_h"]) * a
    fr = f0 if a < 0.5 else f1
    rows = {}
    for r0 in f0["robots"]:
        r1 = next(x for x in f1["robots"] if x["id"] == r0["id"])
        p0, p1 = target(f0, r0), target(f1, r1)
        tgt = (p0[0] + (p1[0] - p0[0]) * a, p0[1] + (p1[1] - p0[1]) * a)
        cur = pos.get(r0["id"], tgt)
        new = (cur[0] + (tgt[0] - cur[0]) * 0.35, cur[1] + (tgt[1] - cur[1]) * 0.35)
        v = (new[0] - cur[0], new[1] - cur[1])
        if math.hypot(*v) > 0.05: head[r0["id"]] = math.atan2(v[1], v[0])
        pos[r0["id"]] = new
        rr = r1 if a >= 0.5 else r0
        rows[r0["id"]] = {"x": round(new[0], 3), "y": round(new[1], 3), "h": round(head.get(r0["id"], math.pi / 2), 4),
                          "status": rr["status"], "task": rr["current_task"], "battery": rr["battery_wh"]}
    prog = {tid: round(f0["progress"].get(tid, 0) + (f1["progress"].get(tid, 0) - f0["progress"].get(tid, 0)) * a, 4) for tid in tasks}
    out.append({"t": round(t, 3), "robots": rows, "progress": prog})

json.dump({"fps": FPS, "cell": CELL, "zones": zones, "depot": depot, "types": types, "fail_t": fail_t,
           "tasks": {tid: {"zone": t["zone"], "type": t["robot_type"]} for tid, t in tasks.items()},
           "structures": [{"id": s["id"], "type": s["type"], "zone": s.get("zone")} for s in req["structures"]],
           "frames": out}, open(sys.argv[3], "w"))
print(f"{len(out)} render frames, {T_END} h of mission, failure at {fail_t} h")
