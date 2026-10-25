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

# time warp: the clip spends more of its time where robots drive (so a drive reads as a drive, not a
# jump) and slows down for the hour around the failure; long stretches of work pass quickly
def moving(f): return any(r["status"] == "moving" for r in f["robots"])
cum = [0.0]
for a, b in zip(frames, frames[1:]):
    mid = (a["t_h"] + b["t_h"]) / 2
    w = 1.0
    if moving(a) or moving(b): w = 5.0
    if fail_t is not None and fail_t - 0.5 <= mid <= fail_t + 1.0: w = max(w, 4.0)
    cum.append(cum[-1] + (b["t_h"] - a["t_h"]) * w)
n_out = int(FPS * SECONDS)
def sim_time_at(u):
    for i in range(len(cum) - 1):
        if cum[i] <= u <= cum[i + 1]:
            return i, ((u - cum[i]) / (cum[i + 1] - cum[i]) if cum[i + 1] > cum[i] else 0.0)
    return len(frames) - 2, 1.0

def slot_pos(rid):
    i = robots.index(rid)
    return (depot[0] - 22 - (i % 4) * 10.0, depot[1] + 2 + (i // 4) * 12.0)   # west of the pad, away from the camera

def target(fr, r):
    st, task = r["status"], r["current_task"]
    p = world(r["pos"])
    if st == "failed":             # left where it broke down, pulled aside so its replacement can work
        return (p[0] - 11.0, p[1] + 7.0)
    if st == "moving":
        return p
    if st == "working" or (task and fr["active_tasks"] and task in fr["active_tasks"]):
        return p
    if math.dist(p, depot) < 3.0:
        return slot_pos(r["id"])
    i = robots.index(r["id"])
    for z in zones.values():
        if math.dist(p, z["c"]) <= z["r"] + 1:
            a = 2 * math.pi * i / len(robots)
            return (z["c"][0] + math.cos(a) * (z["r"] + 11), z["c"][1] + math.sin(a) * (z["r"] + 11))
    return p

# 1a. each robot's target at every recorded frame; a stay in the parking slot shorter than PARK_MIN
#     recorded frames is no stay at all (the robot waits where it is instead of a round trip)
PARK_MIN = 6   # 1.5 h at the quarter-hour step
TGT = [{r["id"]: target(f, r) for r in f["robots"]} for f in frames]
for rid in robots:
    parked = [TGT[i][rid] == slot_pos(rid) for i in range(len(frames))]
    i = 0
    while i < len(frames):
        if parked[i] and i > 0:
            j = i
            while j < len(frames) and parked[j]: j += 1
            if j - i < PARK_MIN and j < len(frames):
                for m in range(i, j): TGT[m][rid] = TGT[i - 1][rid]
            i = j
        else:
            i += 1

# 1b. the warped target of every robot at every render frame, and the frame's state
samples = []
for k in range(n_out):
    u = cum[-1] * k / (n_out - 1)
    i, a = sim_time_at(u)
    f0, f1 = frames[i], frames[i + 1]
    t = f0["t_h"] + (f1["t_h"] - f0["t_h"]) * a
    rows = {}
    for r0 in f0["robots"]:
        r1 = next(x for x in f1["robots"] if x["id"] == r0["id"])
        p0, p1 = TGT[i][r0["id"]], TGT[i + 1][r0["id"]]
        rr = r1 if a >= 0.5 else r0
        rows[r0["id"]] = {"tgt": (p0[0] + (p1[0] - p0[0]) * a, p0[1] + (p1[1] - p0[1]) * a),
                          "status": rr["status"], "task": rr["current_task"], "battery": rr["battery_wh"]}
    prog = {tid: round(f0["progress"].get(tid, 0) + (f1["progress"].get(tid, 0) - f0["progress"].get(tid, 0)) * a, 4) for tid in tasks}
    samples.append({"t": round(t, 3), "rows": rows, "progress": prog})

# 2. per robot, each drive becomes one leg along the recorded path, re-timed with an S-curve
#    (smoothstep: still at both ends, fastest in the middle); a jump of the target (into or out of a
#    parking slot) becomes a short drive of MIN_LEG frames
MIN_LEG = 16
def smoothstep(u): return u * u * (3 - 2 * u)
def along(path, s):
    """the point at fraction s of a polyline's length"""
    seg = [math.dist(a, b) for a, b in zip(path, path[1:])]
    total = sum(seg)
    if total < 1e-9: return path[-1], None
    d = min(max(s, 0.0), 1.0) * total
    for i, L in enumerate(seg):
        a, b = path[i], path[i + 1]
        if d <= L or i == len(seg) - 1:
            w = min(max(d / L, 0.0), 1.0) if L > 0 else 1.0
            return (a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w), math.atan2(b[1] - a[1], b[0] - a[0])
        d -= L
    return path[-1], None

pos = {rid: [None] * n_out for rid in robots}
head = {rid: [None] * n_out for rid in robots}
for rid in robots:
    T = [samples[k]["rows"][rid]["tgt"] for k in range(n_out)]
    k = 0
    legs = []
    while k < n_out - 1:
        if math.dist(T[k], T[k + 1]) > 1e-3:
            a = k
            while k < n_out - 1 and math.dist(T[k], T[k + 1]) > 1e-3: k += 1
            legs.append([a, k])            # frames a..k: T[a] -> T[k]
        else:
            k += 1
    # stretch short legs (jumps) to MIN_LEG frames when the next leg leaves room; a jump with no room
    # for even 8 frames (the clip ends, or the next drive follows at once) is dropped: hold position
    kept = []
    for j, leg in enumerate(legs):
        nxt = legs[j + 1][0] if j + 1 < len(legs) else n_out - 1
        if leg[1] - leg[0] < MIN_LEG: leg[1] = min(leg[0] + MIN_LEG, nxt)
        if leg[1] - leg[0] < 8 and math.dist(T[leg[0]], T[leg[1]]) > 3.0:
            continue
        kept.append(leg)
    legs = kept
    last_h = math.pi / 2
    cur = T[0]
    kk = 0
    for a, b in legs:
        for f in range(kk, a + 1):          # standing still before the leg
            pos[rid][f] = cur; head[rid][f] = last_h
        path = [cur] + [T[f] for f in range(a + 1, b + 1) if math.dist(T[f], T[f - 1]) > 1e-3]
        if len(path) < 2: path = [cur, T[min(b, n_out - 1)]]
        for f in range(a, b + 1):
            s_ = smoothstep((f - a) / max(1, b - a))
            p, h = along(path, s_)
            pos[rid][f] = p
            if h is not None:
                d = (h - last_h + math.pi) % (2 * math.pi) - math.pi   # turn gradually toward the path
                last_h = last_h + d * 0.35
            head[rid][f] = last_h
        cur = path[-1]; kk = b + 1
    for f in range(kk, n_out):
        pos[rid][f] = cur; head[rid][f] = last_h

out = []
for k, smp in enumerate(samples):
    rows = {}
    for rid in robots:
        r = smp["rows"][rid]
        rows[rid] = {"x": round(pos[rid][k][0], 3), "y": round(pos[rid][k][1], 3), "h": round(head[rid][k], 4),
                     "status": r["status"], "task": r["task"], "battery": r["battery"]}
    out.append({"t": smp["t"], "robots": rows, "progress": smp["progress"]})

json.dump({"fps": FPS, "cell": CELL, "zones": zones, "depot": depot, "types": types, "fail_t": fail_t,
           "tasks": {tid: {"zone": t["zone"], "type": t["robot_type"]} for tid, t in tasks.items()},
           "structures": [{"id": s["id"], "type": s["type"], "zone": s.get("zone")} for s in req["structures"]],
           "frames": out}, open(sys.argv[3], "w"))
print(f"{len(out)} render frames, {T_END} h of mission, failure at {fail_t} h")
