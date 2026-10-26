"""Moonwright in NVIDIA Isaac Sim: PhysX rovers carry out a verified schedule; a fault is injected,
Moonwright replans from the rovers' telemetry, and the rovers carry out the new verified schedule.

Isaac Sim is the world. Every robot of the plan is a four-wheeled skid-steer rover (an articulation:
a chassis and four driven wheels on revolute joints) on the lunar terrain of lunar_terrain.py under
lunar gravity (1.62 m/s^2). Moonwright is the brain: the schedule comes from a PlanResult that CP-SAT
verified, and when a rover fails, the frame the rovers report (positions, batteries, progress) goes
to Moonwright's replanner (replan_bridge.py, run in the backend's environment), whose verified
schedule the surviving rovers then carry out.

Execution follows the rules of Moonwright's 2D simulator (backend/moonwright/sim/simulator.py): a
rover runs its schedule entries in order, leaves for the next entry's zone as soon as it is free,
starts work once the entry's start hour has come, every `after` task is done and nobody else works
in the zone, and works for the task's duration. What changes is the drive: wheel torques, friction
and terrain, steered by a heading controller toward the rover's work spot, at the plan's speed
(speed_cells_per_h x 2 m per cell). Mission time runs at --sec-per-h simulated seconds per hour
(120 by default: the plan's 12 cells/h, 24 m/h, is a 0.2 m/s drive).

Work spots: each rover has its own lane (an x offset) so rovers that share a zone never stack, and
every spot sits 6.5 m south of its zone centre (the berm stands on Z2's centre). Both shifts are the
same at every zone, so each leg a rover drives is exactly as long as the planner's straight line
between zone centres. Telemetry speaks the planner's frame: a rover at its spot is in the zone (the
zone's centre cell), a rover on the road is reported in its lane's centre line.

A rover can also stop for a reason no map shows: --boulder puts a rock in a lane. A health check
watches every driving rover; one that gains less than 0.5 m on its way-point in 15 min has stalled.
Without perception a stalled rover is lost (Moonwright replans without it). With --cosmos-python,
NVIDIA Cosmos Reason 2 looks through its navigation camera first (cosmos_bridge.py) and proposes the
cause; "blocked" means
the rover is sound, so it backs up 3 m, steps 5 m aside and rejoins its leg 10 m on, and the health
check keeps watching: a rover that stalls again on its way around is lost after all. --lock injects
the other kind of stall, a drive fault on a clear road (the wheels hold, the rover still thinks it
drives). When everything left waits on a lost rover, the mission stops: the plan cannot finish.

Usage (Isaac Sim 5.1 Python):
  python mission.py plan.json out_dir --fail EX1@10 --uv <uv.exe> --backend <repo>/backend
  python mission.py plan.json out_dir --boulder=3.2,-8,3.6,2.4,1.4 [--cosmos-python <venv python>] [--lock HL2@1.2] ...
Writes out_dir/track.json (frames for render_replay.py), out_dir/mission.json (legs, events,
replans, finish) and out_dir/replan_<n>.in.json / .out.json (what went to Moonwright and back)."""
import argparse
import json
import math
import os
import subprocess
import time

ap = argparse.ArgumentParser()
ap.add_argument("plan"); ap.add_argument("out")
ap.add_argument("--fail", default=None, help="ROBOT@HOUR: a fault injected into that rover")
ap.add_argument("--uv", required=True, help="uv executable that runs the backend")
ap.add_argument("--backend", required=True, help="the repository's backend/ folder")
ap.add_argument("--sec-per-h", type=float, default=120.0, help="simulated seconds per mission hour")
ap.add_argument("--dt", type=float, default=1 / 30, help="physics step (s)")
ap.add_argument("--until", type=float, default=None, help="stop at this mission hour (tests)")
ap.add_argument("--clip-seconds", type=float, default=20.0)
ap.add_argument("--ground", default=None, help="lola_site.npz from prep_lola.py (default: procedural ground)")
ap.add_argument("--boulder", default=None, help="x,y,width,depth,height[,shape 0-3] (m): a rock in the way, too small for the map")
ap.add_argument("--cosmos-python", default=None,
                help="python of the venv that runs cosmos_bridge.py; when set, a stalled rover's camera is "
                     "shown to Cosmos Reason 2, whose answer decides: go around, or lost")
ap.add_argument("--lamp", type=float, default=2.5e5, help="each of the two lamps beside the navigation camera (nits)")
ap.add_argument("--lock", default=None, help="RID@H[,RID@H...]: a drive fault that stops the wheels while the rover still thinks it drives")
ap.add_argument("--snap", default=None, help="RID@H[,RID@H...]: save that rover's camera at that hour (needs --cosmos-python)")
ap.add_argument("--window", default=None, help="a,b: the clip covers mission hours a..b at an even pace")
args = ap.parse_args()

os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
from isaacsim import SimulationApp
app = SimulationApp({"headless": True})

import carb
import numpy as np
import omni.usd
from isaacsim.core.api import World
from isaacsim.core.prims import Articulation, RigidPrim
from pxr import Gf, PhysxSchema, Sdf, UsdGeom, UsdPhysics, UsdShade, Vt

from lunar_terrain import boulder_mesh, height_at, heightfield, load_lola, mesh_arrays, work_flats

HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(args.out, exist_ok=True)
plan = json.load(open(args.plan, encoding="utf-8"))
req = plan["requirements"]; site = req["site"]
gx, gy = site["grid"]
CELL = 2.0                                   # metres per grid cell (the brief's 80 m site, 40 x 40)
K = args.sec_per_h
GRAVITY = 1.62
def world_xy(c): return ((c[0] - gx / 2) * CELL, (c[1] - gy / 2) * CELL)
def cell_xy(p): return (p[0] / CELL + gx / 2, p[1] / CELL + gy / 2)
ZONES = {z["id"]: world_xy(z["center"]) for z in site["zones"]}
ROBOTS = [r["id"] for r in req["robots"]]
SPEC = {r["id"]: r for r in req["robots"]}
TYPES = {r["id"]: r["type"] for r in req["robots"]}
SPOT_DY = -6.5
LANE, edge = {}, 0.0
for rid in ROBOTS:                           # 4.2 m lanes (a rover is 2.8 m wide), the printer's gantry 6.2 m
    w = 6.2 if TYPES[rid] == "printer" else 4.2
    LANE[rid] = edge + w / 2; edge += w
LANE = {rid: x - edge / 2 for rid, x in LANE.items()}
def spot(rid, zone):
    c = ZONES[zone]
    return (c[0] + LANE[rid], c[1] + SPOT_DY)

# ---------------------------------------------------------------- world: terrain, gravity, materials
world = World(stage_units_in_meters=1.0, physics_dt=args.dt, rendering_dt=args.dt)
stage = omni.usd.get_context().get_stage()
world.get_physics_context().set_gravity(-GRAVITY)

def phys_material(path, mu_s, mu_d):
    m = UsdShade.Material.Define(stage, path)
    api = UsdPhysics.MaterialAPI.Apply(m.GetPrim())
    api.CreateStaticFrictionAttr(mu_s); api.CreateDynamicFrictionAttr(mu_d); api.CreateRestitutionAttr(0.0)
    return m
def bind_phys(prim, m):
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(m, UsdShade.Tokens.weakerThanDescendants, "physics")
regolith = phys_material("/World/Physics/regolith", 0.8, 0.65)
tyre = phys_material("/World/Physics/wheel", 0.9, 0.7)

LOLA = load_lola(args.ground) if args.ground else None
GROUND = str(LOLA["meta"][0]) if LOLA is not None else "procedural"
xs, Z = heightfield(flats=work_flats(list(ZONES.values())), lola=LOLA)
pts, counts, idx, nrm = mesh_arrays(xs, Z)
ground = UsdGeom.Mesh.Define(stage, "/World/Terrain")
ground.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pts.astype(np.float32)))
ground.CreateFaceVertexCountsAttr(Vt.IntArray(counts))
ground.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(idx.astype(np.int32)))
UsdPhysics.CollisionAPI.Apply(ground.GetPrim())
UsdPhysics.MeshCollisionAPI.Apply(ground.GetPrim()).CreateApproximationAttr("none")
bind_phys(ground.GetPrim(), regolith)

# a boulder the 5 m map cannot show: a static convex rock, a little buried
BOULDERS = []
if args.boulder:
    bx, by, bw, bd, bh, *bk = (float(v) for v in args.boulder.split(","))
    kind = int(bk[0]) if bk else 2
    P, F = boulder_mesh(kind, bw, bd, bh)
    bz = height_at(xs, Z, bx, by) + bh / 2 - 0.15
    rock = UsdGeom.Mesh.Define(stage, "/World/Boulder")
    rock.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(float(p[0]) + bx, float(p[1]) + by, float(p[2]) + bz) for p in P]))
    rock.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(F)))
    rock.CreateFaceVertexIndicesAttr(Vt.IntArray([i for f in F for i in f]))
    UsdPhysics.CollisionAPI.Apply(rock.GetPrim())
    UsdPhysics.MeshCollisionAPI.Apply(rock.GetPrim()).CreateApproximationAttr("convexHull")
    bind_phys(rock.GetPrim(), regolith)
    BOULDERS.append({"x": bx, "y": by, "z": round(bz, 3), "w": bw, "d": bd, "h": bh, "kind": kind})

# ---------------------------------------------------------------- rovers: chassis + four driven wheels
L, W, H = 3.0, 1.6, 0.5        # chassis box (m)
R_WHEEL, WX, WY = 0.4, 1.1, 1.0
WHEEL_Z = -0.25                # wheel centres at the chassis' underside
CH_Z = R_WHEEL - WHEEL_Z       # chassis centre above the ground at rest
M_CHASSIS, M_WHEEL = 240.0, 12.0
WHEELS = (("fl", 1, 1), ("fr", 1, -1), ("rl", -1, 1), ("rr", -1, -1))   # +y is the left side

def rotate(q, v):
    """rotate vector v by quaternion q (w, x, y, z)"""
    w, x, y, z = q
    r = Gf.Rotation(Gf.Quatd(w, x, y, z))
    return r.TransformDir(Gf.Vec3d(*v))

def rover(rid, x, y, yaw):
    root = f"/World/Rovers/{rid}"
    UsdGeom.Xform.Define(stage, root)
    UsdPhysics.ArticulationRootAPI.Apply(stage.GetPrimAtPath(root))
    PhysxSchema.PhysxArticulationAPI.Apply(stage.GetPrimAtPath(root)).CreateEnabledSelfCollisionsAttr(False)
    q = (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2))
    qf = Gf.Quatf(*q)
    z0 = height_at(xs, Z, x, y) + CH_Z + 0.03
    ch = UsdGeom.Xform.Define(stage, root + "/chassis")
    ch.AddTranslateOp().Set(Gf.Vec3d(x, y, z0)); ch.AddOrientOp().Set(qf)
    UsdPhysics.RigidBodyAPI.Apply(ch.GetPrim())
    UsdPhysics.MassAPI.Apply(ch.GetPrim()).CreateMassAttr(M_CHASSIS)
    box = UsdGeom.Cube.Define(stage, root + "/chassis/box")
    box.CreateSizeAttr(1.0); box.AddScaleOp().Set(Gf.Vec3f(L, W, H))
    UsdPhysics.CollisionAPI.Apply(box.GetPrim())
    for name, sx, sy in WHEELS:
        off = (sx * WX, sy * WY, WHEEL_Z)
        d = rotate(q, off)
        wp = Gf.Vec3d(x + d[0], y + d[1], z0 + d[2])
        wpath = f"{root}/wheel_{name}"
        wh = UsdGeom.Sphere.Define(stage, wpath)
        wh.CreateRadiusAttr(R_WHEEL)
        wh.AddTranslateOp().Set(wp); wh.AddOrientOp().Set(qf)
        UsdPhysics.RigidBodyAPI.Apply(wh.GetPrim()); UsdPhysics.CollisionAPI.Apply(wh.GetPrim())
        UsdPhysics.MassAPI.Apply(wh.GetPrim()).CreateMassAttr(M_WHEEL)
        bind_phys(wh.GetPrim(), tyre)
        j = UsdPhysics.RevoluteJoint.Define(stage, f"{root}/joint_{name}")
        j.CreateBody0Rel().SetTargets([root + "/chassis"]); j.CreateBody1Rel().SetTargets([wpath])
        j.CreateAxisAttr("Y")
        j.CreateLocalPos0Attr().Set(Gf.Vec3f(*off)); j.CreateLocalRot0Attr().Set(Gf.Quatf(1, 0, 0, 0))
        j.CreateLocalPos1Attr().Set(Gf.Vec3f(0, 0, 0)); j.CreateLocalRot1Attr().Set(Gf.Quatf(1, 0, 0, 0))
        drv = UsdPhysics.DriveAPI.Apply(j.GetPrim(), "angular")
        drv.CreateTypeAttr("force"); drv.CreateStiffnessAttr(0.0); drv.CreateDampingAttr(1e5)
        drv.CreateMaxForceAttr(400.0); drv.CreateTargetVelocityAttr(0.0)

def aimed(prim, at, direction):
    """place a camera or a light at `at` (chassis frame), its -Z along `direction`"""
    m = Gf.Matrix4d().SetLookAt(Gf.Vec3d(0, 0, 0), Gf.Vec3d(*direction), Gf.Vec3d(0, 0, 1)).GetInverse()
    m.SetTranslateOnly(Gf.Vec3d(*at))
    UsdGeom.Xformable(prim).AddTransformOp().Set(m)

def front_camera(root):
    """the rover's navigation camera: on a mast about 2 m above the ground, looking ahead and 33 deg
    down (its own nose at the bottom of the frame), between two lamps: at the pole the sun stays low
    and the rover's own shadow lies on whatever is in front of it"""
    from pxr import UsdLux
    cam = UsdGeom.Camera.Define(stage, root + "/chassis/cam")
    cam.CreateFocalLengthAttr(14.0); cam.CreateHorizontalApertureAttr(36.0); cam.CreateVerticalApertureAttr(27.0)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.05, 600.0))
    aimed(cam, (1.0, 0, H / 2 + 1.25), (1, 0, -0.65))
    for side in (1, -1):
        lamp = UsdLux.DiskLight.Define(stage, f"{root}/chassis/lamp{'lr'[side < 0]}")
        lamp.CreateRadiusAttr(0.08); lamp.CreateIntensityAttr(args.lamp)
        lamp.CreateColorAttr(Gf.Vec3f(1.0, 0.96, 0.9))
        aimed(lamp, (1.0, 0.55 * side, H / 2 + 1.1), (1, -0.15 * side, -0.7))

def looks(prim, name, color, rough):
    """a visual material for the camera (the clip's own, render_replay.py); physics keeps its own"""
    m = UsdShade.Material.Define(stage, f"/World/Looks/{name}")
    sh = UsdShade.Shader.Define(stage, f"/World/Looks/{name}/Shader")
    sh.CreateIdAttr("UsdPreviewSurface")
    sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
    sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
    m.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(m)

if args.cosmos_python:   # the camera sees what the clip shows: its low sun, a little fill, faceted rock
    from pxr import UsdLux
    ground.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(nrm.astype(np.float32))); ground.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    ground.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none); looks(ground.GetPrim(), "regolith", (0.17, 0.168, 0.165), 0.97)
    if BOULDERS:
        rock.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none); looks(rock.GetPrim(), "rock", (0.13, 0.128, 0.125), 0.9)
    sun = UsdLux.DistantLight.Define(stage, "/World/Sun")
    sun.CreateIntensityAttr(4500.0); sun.CreateAngleAttr(0.53)
    el, az = math.radians(6.0), math.radians(255.0)
    to_sun = Gf.Vec3d(math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))
    q = Gf.Rotation(Gf.Vec3d(0, 0, 1), to_sun).GetQuat()
    UsdGeom.Xformable(sun).AddOrientOp().Set(Gf.Quatf(q.GetReal(), *q.GetImaginary()))
    dome = UsdLux.DomeLight.Define(stage, "/World/Sky")   # as render_replay.py: fill on the ground, a black sky
    dome.CreateIntensityAttr(3.0); dome.CreateColorAttr(Gf.Vec3f(0.55, 0.6, 0.7))
    dome.GetPrim().CreateAttribute("visibleInPrimaryRay", Sdf.ValueTypeNames.Bool).Set(False)
    _rtx = carb.settings.get_settings()
    _rtx.set("/rtx/indirectDiffuse/enabled", True); _rtx.set("/rtx/ambientOcclusion/enabled", True)

start_zone = {}
for rid in ROBOTS:   # every rover starts at the depot zone (Z1 shares the depot cell), facing north
    depot = tuple(site["depot"])
    zid = next((z for z, c in ((z["id"], tuple(z["center"])) for z in site["zones"]) if c == depot), None)
    start_zone[rid] = zid
    sx_, sy_ = spot(rid, zid) if zid else world_xy(depot)
    rover(rid, sx_, sy_, math.pi / 2)
    if args.cosmos_python:
        front_camera(f"/World/Rovers/{rid}")

rovers = Articulation(prim_paths_expr="/World/Rovers/.*", name="rovers")
chassis = RigidPrim(prim_paths_expr="/World/Rovers/.*/chassis", name="chassis")
world.scene.add(rovers); world.scene.add(chassis)
world.reset()
ORDER = [p.split("/")[3] for p in rovers.prim_paths]
CH_ORDER = [p.split("/")[3] for p in chassis.prim_paths]
assert ORDER == CH_ORDER, (ORDER, CH_ORDER)
DOF = list(rovers.dof_names)
print("rovers", ORDER, "dofs", DOF, flush=True)
def dof_col(name): return DOF.index(f"joint_{name}")

# ---------------------------------------------------------------- the plan in force
tasks = {t["id"]: dict(t) for t in plan["task_graph"]["tasks"]}
graph = plan["task_graph"]; schedule = plan["schedule"]
done = set(); progress = {}; shown = {}
events = []; replans = []; legs = []

class Bot:
    def __init__(self, rid):
        self.id = rid; self.type = TYPES[rid]
        self.speed = SPEC[rid]["speed_cells_per_h"] * CELL / K       # m/s
        self.battery = float(SPEC[rid]["battery_wh"])
        self.queue = []; self.entry = None
        self.status = "idle"; self.failed = False
        self.worked = 0.0; self.base = 0.0; self.dur = 1.0; self.energy = 0.0
        self.target = None; self.leg = None; self.zone = start_zone[rid]
        self.goal = None; self.route = []; self.rev = False; self.wp_t = 0.0; self.detour_h = None
        self.v = 0.0; self.last = None; self.track = []
bots = {rid: Bot(rid) for rid in ROBOTS}
def load_queues(entries):
    for b in bots.values():
        b.queue = sorted((dict(e) for e in entries if e["robot"] == b.id), key=lambda e: e["start_h"])
load_queues(schedule["entries"])

def yaw_of(q):
    w, x, y, z = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
def wrap(a): return (a + math.pi) % (2 * math.pi) - math.pi

ARRIVE = 0.4          # m
TURN_IN_PLACE = 0.35  # rad of heading error above which the rover stops and turns
W_MAX, A_MAX = 0.5, 0.1
STALL_H, STALL_M = 0.25, 0.5   # health check: a driving rover that gains < 0.5 m on its target in 0.25 h has stalled

DETOUR = (3.0, 5.0, 10.0)      # going around: back up 3 m, step 5 m aside, rejoin 10 m past the stop

def drive(b, pos, yaw, dt_s):
    """heading controller toward b.target: (v, w) for the chassis, or None once arrived"""
    dx, dy = b.target[0] - pos[0], b.target[1] - pos[1]
    dist = math.hypot(dx, dy)
    if dist < ARRIVE:
        b.v = 0.0
        return None
    if b.rev:                                   # backing straight out: the heading holds
        r = -(dx * math.cos(yaw) + dy * math.sin(yaw))   # still to go, along the heading
        if r < 0.1:
            b.v = 0.0
            return None
        v_cmd = -min(b.speed, 0.4 * r + 0.02)
        b.v = max(b.v - A_MAX * dt_s, min(b.v + A_MAX * dt_s, v_cmd))
        return b.v, 0.0
    e = wrap(math.atan2(dy, dx) - yaw)
    if abs(e) > TURN_IN_PLACE:
        v_cmd, w = 0.0, max(-W_MAX, min(W_MAX, 1.2 * e))
    else:
        v_cmd, w = min(b.speed, 0.4 * dist + 0.02), max(-0.4, min(0.4, 1.5 * e))
    b.v = max(b.v - A_MAX * dt_s, min(b.v + A_MAX * dt_s, v_cmd))
    return b.v, w

def frame_pos(b, pos):
    """telemetry in the planner's frame: in the zone -> its centre cell; on the road -> lane centre"""
    if b.status != "moving" and b.zone is not None:
        return tuple(round(v, 3) for v in cell_xy(ZONES[b.zone]))
    return tuple(round(v, 3) for v in cell_xy((pos[0] - LANE[b.id], pos[1] - SPOT_DY)))

def busy(zone, me):
    return any(o is not me and o.status == "working" and tasks[o.entry["task"]]["zone"] == zone for o in bots.values())

stalled = []
def tick(t_h, dt_h, poses):
    """one control step of the executor (the simulator's rules); returns wheel targets (rad/s)"""
    targets = np.zeros((len(ORDER), len(DOF)), dtype=np.float32)
    for rid in ROBOTS:
        b = bots[rid]
        k = ORDER.index(rid)
        pos, q = poses[0][k], poses[1][k]
        here = (float(pos[0]), float(pos[1]))
        if b.last is not None and b.status == "moving":
            step = math.dist(here, b.last)
            b.battery -= 0.5 * step / CELL
            if b.leg: b.leg["driven_m"] += step
        b.last = here
        if b.failed:
            continue
        if b.entry is None:
            if not b.queue:
                b.status = "done"; continue
            b.entry = b.queue.pop(0)
            t = tasks[b.entry["task"]]
            b.target = b.goal = spot(rid, t["zone"])
            b.route, b.rev, b.wp_t, b.detour_h = [], False, t_h, None
            b.worked, b.base = 0.0, 0.0
            b.dur, b.energy = float(t["duration_h"]), float(t["energy_wh"])
            if b.zone != t["zone"]:
                b.status, b.zone = "moving", None
                b.track = []
                b.leg = {"robot": rid, "task": t["id"], "to": t["zone"], "depart_h": round(t_h, 4),
                         "straight_m": round(math.dist(here, b.target), 2), "driven_m": 0.0,
                         "planned_start_h": b.entry["start_h"]}
            else:
                b.status, b.zone = "idle", t["zone"]
        t = tasks[b.entry["task"]]
        if b.status == "moving":
            gap = math.dist(here, b.target)
            b.track = [(th, g) for th, g in b.track if th >= t_h - STALL_H] + [(t_h, gap)]
            if b.leg and t_h - b.wp_t >= STALL_H and b.track[0][1] - gap < STALL_M:
                stalled.append((rid, t["zone"], math.dist(here, b.goal)))
            cmd = drive(b, here, yaw_of(q), dt_h * K)
            while cmd is None and b.route:             # a way-point of a detour: on to the next
                b.target, b.rev = b.route.pop(0)
                b.wp_t, b.track = t_h, []
                cmd = drive(b, here, yaw_of(q), dt_h * K)
            if cmd is None:
                b.status, b.zone = "idle", t["zone"]
                lg = b.leg; b.leg = None
                lg["arrive_h"] = round(t_h, 4)
                lg["ideal_h"] = round(lg["straight_m"] / (b.speed * K), 4)
                lg["slack_h"] = round(lg["planned_start_h"] - t_h, 4)
                lg["driven_m"] = round(lg["driven_m"], 2)
                legs.append(lg)
            else:
                v, w = cmd
                vl, vr = (v - w * WY) / R_WHEEL, (v + w * WY) / R_WHEEL
                if rid in locks and t_h >= locks[rid]:   # the injected drive fault: commanded, but the wheels hold
                    vl = vr = 0.0
                for name, _sx, sy in WHEELS:
                    targets[k, dof_col(name)] = vl if sy > 0 else vr
                continue
        if b.status == "idle":
            if (b.entry["start_h"] <= t_h + 1e-9 and all(a in done for a in t["after"])
                    and not busy(t["zone"], b)):
                b.status = "working"
        if b.status == "working":
            b.worked += dt_h
            frac = min(1.0, b.worked / b.dur)
            progress[t["id"]] = b.base + (1 - b.base) * frac
            shown[t["id"]] = max(shown.get(t["id"], 0.0), progress[t["id"]])
            b.battery -= b.energy * dt_h / b.dur
            if b.worked >= b.dur - 1e-9:
                done.add(t["id"]); progress[t["id"]] = 1.0; shown[t["id"]] = 1.0
                b.entry = None; b.status = "idle"
    return targets

def telemetry(t_h, poses):
    robots = []
    for rid in ROBOTS:
        b = bots[rid]; k = ORDER.index(rid)
        robots.append({"id": rid, "pos": frame_pos(b, poses[0][k]),
                       "battery_wh": math.floor(max(0.0, b.battery) * 100) / 100,
                       "status": "failed" if b.failed else b.status,
                       "current_task": b.entry["task"] if b.entry else None})
    return {"t_h": round(t_h, 4), "robots": robots, "done_tasks": sorted(done),
            "active_tasks": sorted(b.entry["task"] for b in bots.values() if b.status == "working"),
            "progress": {k: round(v, 4) for k, v in progress.items()}}

def replan(t_h, poses, event):
    """send the measured frame to Moonwright; apply the verified schedule it returns"""
    global graph, schedule
    n = len(replans) + 1
    src = os.path.join(args.out, f"replan_{n}.in.json"); dst = os.path.join(args.out, f"replan_{n}.out.json")
    frame = telemetry(t_h, poses)
    json.dump({"plan": plan, "graph": graph, "schedule": schedule, "frame": frame, "event": event},
              open(src, "w", encoding="utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    t0 = time.perf_counter()
    res = subprocess.run([args.uv, "run", "--project", args.backend, "--no-dev", "python",
                          os.path.join(HERE, "replan_bridge.py"), src, dst],
                         env=env, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    print(res.stdout.strip(), res.stderr.strip()[-2000:] if res.returncode else "", flush=True)
    if res.returncode != 0:
        replans.append({"t_h": t_h, "ok": False, "error": res.stderr[-500:]}); return
    rec = json.load(open(dst, encoding="utf-8"))
    after = rec["after"]
    msg = after["steps"][-1]["message"] if after.get("steps") else ""
    replans.append({"t_h": round(t_h, 4), "ok": bool(after["success"]), "message": msg,
                    "solver_wall_s": rec.get("wall_s"), "roundtrip_s": round(wall, 2),
                    "makespan_before_h": schedule.get("makespan_h"),
                    "makespan_after_h": (after.get("schedule") or {}).get("makespan_h")})
    if not after["success"] or not after.get("schedule"):
        return
    schedule = after["schedule"]; graph = after["task_graph"]
    for tk in graph["tasks"]:
        tasks[tk["id"]] = dict(tk)
    entries = schedule["entries"]
    for b in bots.values():
        if b.failed: continue
        mine = sorted((dict(e) for e in entries if e["robot"] == b.id), key=lambda e: e["start_h"])
        if b.status == "working" and mine and mine[0]["task"] == b.entry["task"]:
            cur = mine.pop(0)                       # a locked task continues where it is
            tk = tasks[cur["task"]]
            b.entry = cur; b.base = progress.get(cur["task"], 0.0); b.worked = 0.0
            b.dur, b.energy = float(tk["duration_h"]), float(tk["energy_wh"])
        elif b.status != "working":
            if b.leg is not None:                   # the road it was on may no longer lead anywhere
                b.leg["rerouted_h"] = round(t_h, 4); b.leg["driven_m"] = round(b.leg["driven_m"], 2)
                legs.append(b.leg); b.leg = None
            b.entry = None
            if b.status == "moving": b.status = "idle"
        b.queue = mine

# ---------------------------------------------------------------- perception: Cosmos Reason 2 on the front camera
perceptions, cams = [], {}
def nav_image(rid, img):
    """save what the rover's navigation camera sees now"""
    import omni.replicator.core as rep
    from PIL import Image
    if rid not in cams:   # one render product per rover, kept: Replicator's step and destroy drop the physics views
        rp = rep.create.render_product(f"/World/Rovers/{rid}/chassis/cam", (768, 576))
        cams[rid] = rep.AnnotatorRegistry.get_annotator("rgb")
        cams[rid].attach([rp])
    for _ in range(3):    # render only: World.render() holds the physics while the frame settles
        for _ in range(12):
            world.render()
        rgb = np.asarray(cams[rid].get_data())[..., :3]
        if rgb.max() > 16:    # now and then the first frames come back black: render again
            break
    Image.fromarray(rgb).save(img)

def perceive(rid, t_h):
    """show Cosmos Reason 2 (cosmos_bridge.py, its own venv) what the rover's camera sees; the
    mission clock waits"""
    n = len(perceptions) + 1
    img = os.path.join(args.out, f"perception_{n}.png")
    nav_image(rid, img)
    out = os.path.join(args.out, f"perception_{n}.json")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    t0 = time.perf_counter()
    res = subprocess.run([args.cosmos_python, os.path.join(HERE, "cosmos_bridge.py"), img, out],
                         env=env, capture_output=True, text=True)
    wall = round(time.perf_counter() - t0, 1)
    print(res.stdout.strip()[-600:], res.stderr.strip()[-1500:] if res.returncode else "", flush=True)
    rec = json.load(open(out, encoding="utf-8")) if res.returncode == 0 and os.path.exists(out) else {"error": res.stderr[-800:]}
    rec.update(t_h=round(t_h, 4), robot=rid, image=os.path.basename(img), roundtrip_s=wall)
    perceptions.append(rec)
    return rec.get("answer") or {}

# ---------------------------------------------------------------- run
def fail(rid, t_h, poses, what, seen=""):
    """a rover is lost: record the event, tell Moonwright, carry on with what it returns"""
    b = bots[rid]
    b.failed, b.status = True, "failed"
    task = b.entry["task"] if b.entry else None
    pct = round(100 * progress.get(task, 0.0)) if task else 0
    ev = {"t_h": round(t_h, 4), "kind": "robot_failure", "robot": rid, "task": task,
          "detail": f"{rid} {what} at {t_h:.2f} h in Isaac Sim"
                    + (f" while working on {task} ({pct}% done)" if task and progress.get(task) else "") + seen}
    events.append(ev)
    print("EVENT", ev["detail"], flush=True)
    replan(t_h, poses, ev)

def go_around(rid, t_h, poses, what, cause):
    """Cosmos says the way is blocked, so the rover is taken to be sound: it backs up, steps aside to
    the side with more room and rejoins its leg past the obstacle. Physics checks the reading: the
    health check keeps watching, and a rover that stalls again on its way around is lost after all"""
    b = bots[rid]; k = ORDER.index(rid)
    p = np.asarray(poses[0][k][:2], float); yaw = yaw_of(poses[1][k])
    f, left = np.array([math.cos(yaw), math.sin(yaw)]), np.array([-math.sin(yaw), math.cos(yaw)])
    back, aside, ahead = DETOUR
    others = [np.asarray(poses[0][ORDER.index(o)][:2], float) for o in ROBOTS if o != rid]
    def room(side):
        return min((np.linalg.norm(p + aside * side * left + d * f - q) for q in others for d in (0.0, ahead)), default=1e9)
    side = 1 if room(1) >= room(-1) else -1
    b.route = [(tuple(p + 1.0 * f + aside * side * left), False), (tuple(p + ahead * f + aside * side * left), False),
               (b.goal, False)]
    b.target, b.rev, b.wp_t, b.track = tuple(p - back * f), True, t_h, []
    b.detour_h = t_h
    if b.leg: b.leg["detour_h"] = round(t_h, 4)
    ev = {"t_h": round(t_h, 4), "kind": "blocked", "robot": rid, "task": b.entry["task"] if b.entry else None,
          "detail": f"{rid} {what} at {t_h:.2f} h in Isaac Sim; Cosmos Reason 2 on its camera: {cause}; "
                    f"the rover is sound, so it backs up {back:.0f} m and goes around on its {'left' if side > 0 else 'right'}"}
    events.append(ev)
    print("EVENT", ev["detail"], flush=True)

def stalled_rover(rid, t_h, poses, gap, zone):
    """the health check's finding. Without perception a stalled rover is lost. With it, Cosmos
    Reason 2 proposes the cause from the camera: blocked -> go around (once), else lost"""
    b = bots[rid]
    what = f"stalled {gap:.1f} m short of {zone}, no headway for {60 * STALL_H:.0f} min"
    if b.detour_h is not None:
        fail(rid, t_h, poses, what + " on its way around"); return
    if not args.cosmos_python:
        fail(rid, t_h, poses, what); return
    ans = perceive(rid, t_h)
    if ans.get("blocked") is True:
        go_around(rid, t_h, poses, what, ans.get("cause") or "blocked"); return
    fail(rid, t_h, poses, what, f"; Cosmos Reason 2 on its camera sees nothing in the way ({ans.get('cause') or 'no answer'})")

def unfinishable(remaining):
    """the remaining tasks no rover will ever do: held by a lost rover in the plan in force, or
    after one that is"""
    owner = {e["task"]: e["robot"] for e in schedule["entries"]}
    lost = {tid for tid in remaining if owner.get(tid) in bots and bots[owner[tid]].failed}
    grew = True
    while grew:
        grew = False
        for tid in remaining:
            if tid not in lost and any(a in lost for a in tasks[tid]["after"]):
                lost.add(tid); grew = True
    return lost

fail_rid, fail_t = (args.fail.split("@")[0], float(args.fail.split("@")[1])) if args.fail else (None, None)
snaps = sorted(((float(h), r) for r, h in (x.split("@") for x in args.snap.split(","))), reverse=True) if args.snap else []
locks = {r: float(h) for r, h in (x.split("@") for x in args.lock.split(","))} if args.lock else {}
CTRL_EVERY = max(1, round(0.1 / args.dt))     # 10 Hz control
REC_EVERY = max(1, round(0.5 / args.dt))      # a recorded sample every 0.5 s
dt_ctrl_h = args.dt * CTRL_EVERY / K
rec = []
step, t_wall = 0, time.perf_counter()
finish_h = stuck_h = None
targets = np.zeros((len(ORDER), len(DOF)), dtype=np.float32)
while True:
    t_h = step * args.dt / K
    if step % CTRL_EVERY == 0:
        pos, quat = chassis.get_world_poses()
        poses = (np.asarray(pos), np.asarray(quat))
        if fail_rid and not bots[fail_rid].failed and t_h >= fail_t:
            fail(fail_rid, t_h, poses, "drive fault")
        while snaps and t_h >= snaps[-1][0]:      # test pictures: what a camera sees on a clear road
            h_, r_ = snaps.pop()
            nav_image(r_, os.path.join(args.out, f"snap_{r_}_{h_:g}h.png"))
        targets = tick(t_h, dt_ctrl_h, poses)
        while stalled:
            rid_, zone_, gap_ = stalled.pop(0)
            if not bots[rid_].failed:
                stalled_rover(rid_, t_h, poses, gap_, zone_)
                stalled.clear()
                targets = tick(t_h, 0.0, poses)
        rovers.set_joint_velocity_targets(targets)
        remaining = [tk["id"] for tk in graph["tasks"] if tk["id"] not in done]
        if not remaining and finish_h is None and all(b.status != "moving" for b in bots.values()):
            finish_h = t_h
        if (remaining and stuck_h is None and set(remaining) == unfinishable(remaining)
                and all(b.status not in ("moving", "working") for b in bots.values())):
            stuck_h = t_h                      # what is left waits on a lost rover: the plan cannot finish
    if step % REC_EVERY == 0:
        pos, quat = chassis.get_world_poses()
        rows = {}
        for rid in ROBOTS:
            k = ORDER.index(rid); b = bots[rid]
            p, q = pos[k], quat[k]
            up = rotate(tuple(float(v) for v in q), (0, 0, -CH_Z))     # the ground point under the chassis
            rows[rid] = {"x": round(float(p[0]) + up[0], 3), "y": round(float(p[1]) + up[1], 3),
                         "z": round(float(p[2]) + up[2], 3), "q": [round(float(v), 5) for v in q],
                         "h": round(yaw_of(q), 4), "status": "failed" if b.failed else b.status,
                         "task": b.entry["task"] if b.entry else None, "battery": round(b.battery, 1)}
        rec.append({"t": round(t_h, 4), "robots": rows, "progress": {k: round(v, 4) for k, v in shown.items()}})
    if finish_h is not None or stuck_h is not None or (args.until is not None and t_h >= args.until):
        break
    world.step(render=False)
    step += 1
    if step % (REC_EVERY * 1200) == 0:
        print(f"t={t_h:.1f} h, wall {time.perf_counter() - t_wall:.0f} s, done {sorted(done)}", flush=True)

wall = time.perf_counter() - t_wall
print(f"finished at {finish_h} h" + (f", stuck at {stuck_h:.2f} h: {sorted(unfinishable([tk['id'] for tk in graph['tasks'] if tk['id'] not in done]))} cannot be done"
      if stuck_h is not None else "") + f" (wall {wall:.0f} s, {step} physics steps)", flush=True)

# ---------------------------------------------------------------- outputs
arrived = [lg for lg in legs if "arrive_h" in lg]
over = [(lg["arrive_h"] - lg["depart_h"]) / lg["ideal_h"] - 1 for lg in arrived if lg["ideal_h"] > 0.05]
unfinished = sorted(tk["id"] for tk in graph["tasks"] if tk["id"] not in done)
summary = {"ground": GROUND, "finish_h": finish_h, "stuck_h": stuck_h, "unfinished": unfinished,
           "perception": "Cosmos Reason 2" if args.cosmos_python else None, "locks": locks, "sec_per_h": K, "gravity": GRAVITY, "physics_dt": args.dt,
           "physics_steps": step, "wall_s": round(wall, 1), "legs": legs, "events": events,
           "replans": replans, "perceptions": perceptions, "boulders": BOULDERS,
           "leg_overhead_pct": {"median": round(100 * float(np.median(over)), 1) if over else None,
                                "max": round(100 * max(over), 1) if over else None},
           "legs_arrived": len(arrived), "legs_late": sum(lg["slack_h"] < -1e-6 for lg in arrived),
           "late_legs": [lg for lg in arrived if lg["slack_h"] < -1e-6]}
json.dump(summary, open(os.path.join(args.out, "mission.json"), "w"), indent=1)

FPS = 24
n_out = int(FPS * args.clip_seconds)
def moving(s): return any(r["status"] == "moving" for r in s["robots"].values())
cum = [0.0]
for a_, b_ in zip(rec, rec[1:]):
    w = 5.0 if (moving(a_) or moving(b_)) else 1.0
    if fail_t is not None and fail_t - 0.5 <= b_["t"] <= fail_t + 1.0: w = max(w, 4.0)
    cum.append(cum[-1] + (b_["t"] - a_["t"]) * w)
frames, j = [], 0
if args.window:   # an even pace over a window of mission hours (the cold open: the stall and the replan)
    wa, wb = (float(v) for v in args.window.split(","))
    for k in range(n_out):
        tt = wa + (wb - wa) * k / max(1, n_out - 1)
        while j < len(rec) - 2 and rec[j + 1]["t"] <= tt: j += 1
        frames.append(rec[j])
else:
    for k in range(n_out):
        u = cum[-1] * k / max(1, n_out - 1)
        while j < len(cum) - 2 and cum[j + 1] < u: j += 1
        frames.append(rec[j])
track = {"fps": FPS, "cell": CELL, "physics": True, "ground": GROUND,
         "zones": {z["id"]: {"c": world_xy(z["center"]), "r": z.get("radius", 3) * CELL} for z in site["zones"]},
         "depot": world_xy(site["depot"]), "types": TYPES, "fail_t": fail_t,
         "tasks": {tid: {"zone": t["zone"], "type": t["robot_type"]} for tid, t in tasks.items()},
         "structures": [{"id": s["id"], "type": s["type"], "zone": s.get("zone")} for s in req["structures"]],
         "events": events, "replans": replans, "boulders": BOULDERS, "perceptions": perceptions, "frames": frames}
json.dump(track, open(os.path.join(args.out, "track.json"), "w"))
print(json.dumps({k: summary[k] for k in ("finish_h", "leg_overhead_pct", "replans")}, indent=1), flush=True)
print(f"late legs: {len(summary['late_legs'])} of {len(arrived)}", flush=True)
app.close()
