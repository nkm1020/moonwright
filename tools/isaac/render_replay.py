"""Moonwright: replay a CP-SAT-verified schedule in NVIDIA Isaac Sim (kinematic, no digging physics).

Reads track.json (make_track.py: positions recorded by Moonwright's simulator, task progress) and
renders one PNG per frame with Replicator. Usage:
  python render_replay.py track.json out_dir [--frames 0:432] [--width 1920 --height 1080]"""
import argparse, json, math, os, sys

ap = argparse.ArgumentParser()
ap.add_argument("track"); ap.add_argument("out")
ap.add_argument("--frames", default=None, help="a:b slice of render frames")
ap.add_argument("--width", type=int, default=1920); ap.add_argument("--height", type=int, default=1080)
ap.add_argument("--subframes", type=int, default=6)
ap.add_argument("--pick", default=None, help="comma-separated frame indices (test stills)")
args = ap.parse_args()

os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
from isaacsim import SimulationApp
app = SimulationApp({"headless": True, "width": args.width, "height": args.height,
                     "renderer": "RaytracedLighting", "anti_aliasing": 3})

import numpy as np
import omni.usd
import omni.replicator.core as rep
import carb
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade, Vt

track = json.load(open(args.track))
frames = track["frames"]
a, b = (0, len(frames))
if args.frames:
    a, b = (int(x) for x in args.frames.split(":"))
os.makedirs(args.out, exist_ok=True)

ctx = omni.usd.get_context()
ctx.new_stage()
stage = ctx.get_stage()
UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
UsdGeom.SetStageMetersPerUnit(stage, 1.0)
world = UsdGeom.Xform.Define(stage, "/World")

# ---------------------------------------------------------------- materials (UsdPreviewSurface)
def material(name, color, rough=0.85, metal=0.0, emissive=(0, 0, 0)):
    path = f"/World/Looks/{name}"
    m = UsdShade.Material.Define(stage, path)
    s = UsdShade.Shader.Define(stage, path + "/Shader")
    s.CreateIdAttr("UsdPreviewSurface")
    s.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
    s.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
    s.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metal)
    s.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*emissive))
    m.CreateSurfaceOutput().ConnectToSource(s.ConnectableAPI(), "surface")
    return m, s

def bind(prim, m):
    UsdShade.MaterialBindingAPI.Apply(prim)
    UsdShade.MaterialBindingAPI(prim).Bind(m)

M = {
    "regolith": material("regolith", (0.17, 0.168, 0.165), 0.97),
    "body_ex": material("body_ex", (0.78, 0.62, 0.22), 0.6, 0.1),
    "body_hl": material("body_hl", (0.62, 0.64, 0.66), 0.55, 0.2),
    "body_cp": material("body_cp", (0.45, 0.52, 0.44), 0.6, 0.1),
    "body_pr": material("body_pr", (0.80, 0.82, 0.85), 0.45, 0.3),
    "body_sv": material("body_sv", (0.92, 0.92, 0.90), 0.5, 0.1),
    "dark": material("dark", (0.08, 0.08, 0.09), 0.7),
    "glass": material("glass", (0.12, 0.16, 0.22), 0.2, 0.4),
    "failed": material("failed", (0.45, 0.07, 0.07), 0.6),
    "beacon": material("beacon", (1.0, 0.1, 0.08), 0.3, 0.0, (40.0, 2.0, 1.0)),
    "stake": material("stake", (0.9, 0.55, 0.1), 0.5, 0.0, (2.0, 1.0, 0.1)),
}
pad_mat, pad_shader = material("pad", (0.24, 0.235, 0.23), 0.95)
berm_mat, berm_shader = material("berm", (0.21, 0.205, 0.2), 0.95)

def smooth(u): return u * u * (3 - 2 * u)
SHOTS = [  # (clip fraction, eye, target)
    (0.00, (30.0, -78.0, 16.0), (0.0, -38.0, 1.5)),
    (0.28, (24.0, -66.0, 12.0), (0.0, -38.0, 1.5)),
    (0.62, (78.0, -64.0, 46.0), (0.0, -6.0, 0.0)),
    (1.00, (96.0, -14.0, 58.0), (0.0, -2.0, 0.0)),
]
# ---------------------------------------------------------------- terrain: gentle noise + a few craters
def terrain():
    size, n = 260.0, 261
    xs = np.linspace(-size / 2, size / 2, n)
    X, Y = np.meshgrid(xs, xs)
    rng = np.random.default_rng(7)
    def value_noise(cells, amp):
        g = rng.normal(0, 1, (cells + 1, cells + 1))
        u = (X + size / 2) / size * cells; v = (Y + size / 2) / size * cells
        i, j = np.floor(u).astype(int).clip(0, cells - 1), np.floor(v).astype(int).clip(0, cells - 1)
        fu, fv = u - i, v - j
        fu, fv = fu * fu * (3 - 2 * fu), fv * fv * (3 - 2 * fv)
        a = g[j, i] * (1 - fu) + g[j, i + 1] * fu
        b = g[j + 1, i] * (1 - fu) + g[j + 1, i + 1] * fu
        return amp * (a * (1 - fv) + b * fv)
    Z = value_noise(6, 0.9) + value_noise(18, 0.25) + value_noise(60, 0.06)
    for _ in range(38):   # shallow impact craters of many sizes, none on the graded site
        cx, cy = rng.uniform(-125, 125, 2)
        if abs(cx) < 30 and abs(cy) < 55: continue
        if any(math.hypot(cx - e[0], cy - e[1]) < 45 for _, e, _ in SHOTS): continue   # none in front of the lens
        r = float(np.exp(rng.uniform(np.log(2.5), np.log(16))))
        d = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) / r
        Z += -0.18 * r * np.clip(1 - d ** 2, 0, None) + 0.05 * r * np.exp(-((d - 1.0) ** 2) / 0.03)
    site = np.exp(-((X / 30) ** 2 + (Y / 55) ** 2) ** 2)
    Z = Z * (1 - site)
    mesh = UsdGeom.Mesh.Define(stage, "/World/Terrain")
    pts = np.stack([X.ravel(), Y.ravel(), Z.ravel()], axis=1)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pts.astype(np.float32)))
    idx = []
    for j in range(n - 1):
        for i in range(n - 1):
            v = j * n + i
            idx += [v, v + 1, v + n + 1, v + n]
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([4] * ((n - 1) ** 2)))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(idx))
    gy, gx = np.gradient(Z, xs, xs)
    nrm = np.stack([-gx.ravel(), -gy.ravel(), np.ones(n * n)], axis=1)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
    mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(nrm.astype(np.float32)))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    bind(mesh.GetPrim(), M["regolith"][0])
terrain()

# ---------------------------------------------------------------- primitives
def xform(path, t=(0, 0, 0), rz=0.0):
    x = UsdGeom.Xform.Define(stage, path)
    x.AddTranslateOp().Set(Gf.Vec3d(*t))
    x.AddRotateZOp().Set(rz)
    x.AddScaleOp().Set(Gf.Vec3f(1.6, 1.6, 1.6))
    return x

def cube(path, size, t, mat, rz=0.0):
    c = UsdGeom.Cube.Define(stage, path)
    c.CreateSizeAttr(1.0)
    c.AddTranslateOp().Set(Gf.Vec3d(*t))
    c.AddRotateZOp().Set(rz)
    c.AddScaleOp().Set(Gf.Vec3f(*size))
    bind(c.GetPrim(), mat)
    return c

def cyl(path, r, h, t, mat, axis="Z"):
    c = UsdGeom.Cylinder.Define(stage, path)
    c.CreateRadiusAttr(r); c.CreateHeightAttr(h); c.CreateAxisAttr(axis)
    c.AddTranslateOp().Set(Gf.Vec3d(*t))
    bind(c.GetPrim(), mat)
    return c

# ---------------------------------------------------------------- structures
zones = track["zones"]
z1, z2 = zones["Z1"]["c"], zones["Z2"]["c"]
def disc(path, r, t, mat, seg=72):
    m = UsdGeom.Mesh.Define(stage, path)
    pts = [(0.0, 0.0, 0.0)] + [(r * math.cos(2 * math.pi * k / seg), r * math.sin(2 * math.pi * k / seg), 0.0) for k in range(seg)]
    m.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in pts]))
    m.CreateFaceVertexCountsAttr(Vt.IntArray([3] * seg))
    m.CreateFaceVertexIndicesAttr(Vt.IntArray([v for k in range(seg) for v in (0, 1 + k, 1 + (k + 1) % seg)]))
    m.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 1)] * len(pts))); m.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    m.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    m.AddTranslateOp().Set(Gf.Vec3d(*t))
    bind(m.GetPrim(), mat)
    return m
pad = disc("/World/Pad", 10.0, (z1[0], z1[1], 0.06), pad_mat)
berm = cube("/World/Berm", (24.0, 4.0, 0.05), (z2[0], z2[1], 0.0), berm_mat)
stakes = {}
for zid, z in zones.items():
    for k in range(4):
        ang = math.pi / 4 + k * math.pi / 2
        p = (z["c"][0] + math.cos(ang) * (z["r"] + 6), z["c"][1] + math.sin(ang) * (z["r"] + 6), 0.6)
        s = cyl(f"/World/Stake_{zid}_{k}", 0.08, 1.2, p, M["stake"][0])
        stakes.setdefault(zid, []).append(s)

# ---------------------------------------------------------------- robots (proxies by type, ~2x scale for legibility)
TYPE_BODY = {"excavator": "body_ex", "hauler": "body_hl", "compactor": "body_cp", "printer": "body_pr", "surveyor": "body_sv"}
robots = {}
for rid, typ in track["types"].items():
    root = f"/World/Robots/{rid}"
    x = xform(root)
    body = M[TYPE_BODY[typ]][0]
    parts = []
    if typ == "excavator":
        parts.append(cube(root + "/TrackL", (5.0, 1.0, 1.0), (0, 1.4, 0.5), M["dark"][0]))
        parts.append(cube(root + "/TrackR", (5.0, 1.0, 1.0), (0, -1.4, 0.5), M["dark"][0]))
        parts.append(cube(root + "/Body", (3.6, 3.0, 1.6), (-0.3, 0, 1.8), body))
        parts.append(cube(root + "/Cab", (1.4, 1.4, 1.4), (0.8, 0.7, 3.2), M["glass"][0]))
        parts.append(cube(root + "/Boom", (4.0, 0.5, 0.5), (2.6, -0.6, 3.0), body, 0))
        parts.append(cube(root + "/Bucket", (0.9, 1.2, 1.0), (4.6, -0.6, 2.0), M["dark"][0]))
    elif typ == "hauler":
        for sx in (-1.6, 1.6):
            for sy in (-1.4, 1.4):
                parts.append(cyl(root + f"/W{sx}{sy}".replace(".", "").replace("-", "m"), 0.7, 0.5, (sx, sy, 0.7), M["dark"][0], "Y"))
        parts.append(cube(root + "/Chassis", (5.0, 2.6, 0.6), (0, 0, 1.2), body))
        parts.append(cube(root + "/Cab", (1.2, 2.2, 1.4), (2.0, 0, 2.2), M["glass"][0]))
        parts.append(cube(root + "/Bed", (3.2, 2.6, 1.3), (-0.8, 0, 2.15), body))
    elif typ == "compactor":
        parts.append(cyl(root + "/Drum", 1.0, 2.8, (2.0, 0, 1.0), M["dark"][0], "Y"))
        parts.append(cube(root + "/Body", (3.0, 2.2, 1.4), (-0.6, 0, 1.4), body))
        parts.append(cube(root + "/Cab", (1.2, 1.4, 1.2), (-0.6, 0, 2.7), M["glass"][0]))
    elif typ == "printer":
        for sy in (-1.6, 1.6):
            parts.append(cube(root + f"/Leg{'L' if sy > 0 else 'R'}", (0.4, 0.4, 3.0), (0, sy, 1.5), body))
        parts.append(cube(root + "/Gantry", (0.6, 3.8, 0.5), (0, 0, 3.1), body))
        parts.append(cube(root + "/Head", (0.8, 0.8, 0.9), (0, 0, 2.5), M["dark"][0]))
        parts.append(cube(root + "/Base", (3.0, 3.6, 0.5), (0, 0, 0.25), M["dark"][0]))
    else:  # surveyor
        parts.append(cube(root + "/Body", (2.2, 1.6, 0.8), (0, 0, 0.8), body))
        parts.append(cyl(root + "/Mast", 0.08, 2.0, (0, 0, 2.2), M["dark"][0]))
        parts.append(cube(root + "/Scanner", (0.5, 0.5, 0.35), (0, 0, 3.3), M["glass"][0]))
    beacon = UsdGeom.Sphere.Define(stage, root + "/Beacon")
    beacon.CreateRadiusAttr(0.45)
    beacon.AddTranslateOp().Set(Gf.Vec3d(0, 0, 4.6))
    bind(beacon.GetPrim(), M["beacon"][0])
    UsdGeom.Imageable(beacon).MakeInvisible()
    robots[rid] = {"x": x, "type": typ, "parts": parts, "beacon": beacon, "failed": False}

# ---------------------------------------------------------------- light and sky: low south-polar sun, near-black sky
sun = UsdLux.DistantLight.Define(stage, "/World/Sun")
sun.CreateIntensityAttr(3500.0); sun.CreateAngleAttr(0.53)
sun.CreateColorAttr(Gf.Vec3f(1.0, 0.97, 0.92))
sx = UsdGeom.Xformable(sun)
sx.AddRotateZOp().Set(-35.0)   # azimuth
sx.AddRotateXOp().Set(78.0)    # 12 degrees above the horizon
dome = UsdLux.DomeLight.Define(stage, "/World/Sky")
dome.CreateIntensityAttr(6.0); dome.CreateColorAttr(Gf.Vec3f(0.55, 0.6, 0.7))

# ---------------------------------------------------------------- camera
cam = UsdGeom.Camera.Define(stage, "/World/Cam")
cam.CreateFocalLengthAttr(24.0); cam.CreateHorizontalApertureAttr(36.0)
cam.CreateVerticalApertureAttr(36.0 * args.height / args.width)
cam.CreateClippingRangeAttr(Gf.Vec2f(0.5, 2000.0))
cam_op = UsdGeom.Xformable(cam).AddTransformOp()

def place_camera(k, n):
    u = k / max(1, n - 1)
    for (u0, e0, t0), (u1, e1, t1) in zip(SHOTS, SHOTS[1:]):
        if u0 <= u <= u1:
            w = smooth((u - u0) / (u1 - u0))
            eye = Gf.Vec3d(*[a + (b - a) * w for a, b in zip(e0, e1)])
            target = Gf.Vec3d(*[a + (b - a) * w for a, b in zip(t0, t1)])
            break
    m = Gf.Matrix4d().SetLookAt(eye, target, Gf.Vec3d(0, 0, 1)).GetInverse()
    cam_op.Set(m)

# ---------------------------------------------------------------- per-frame state
def lerp(c0, c1, t): return tuple(a + (b - a) * t for a, b in zip(c0, c1))
def set_color(shader, c, rough=None):
    shader.GetInput("diffuseColor").Set(Gf.Vec3f(*c))
    if rough is not None: shader.GetInput("roughness").Set(rough)

tasks = track["tasks"]
def zone_progress(prog, zone, rtype):
    vals = [prog.get(t, 0.0) for t, d in tasks.items() if d["zone"] == zone and d["type"] == rtype]
    return sum(vals) / len(vals) if vals else 0.0

def apply(frame):
    for rid, row in frame["robots"].items():
        r = robots[rid]
        ops = r["x"].GetOrderedXformOps()
        ops[0].Set(Gf.Vec3d(row["x"], row["y"], 0.0))
        ops[1].Set(math.degrees(row["h"]))
        failed = row["status"] == "failed"
        if failed != r["failed"]:
            r["failed"] = failed
            img = UsdGeom.Imageable(r["beacon"])
            img.MakeVisible() if failed else img.MakeInvisible()
            for p in r["parts"]:
                name = p.GetPrim().GetName()
                if name in ("Body", "Chassis", "Bed"):
                    bind(p.GetPrim(), M["failed"][0] if failed else M[TYPE_BODY[r["type"]]][0])
    prog = frame["progress"]
    dig = zone_progress(prog, "Z1", "excavator")
    comp = zone_progress(prog, "Z1", "compactor")
    sint = zone_progress(prog, "Z1", "printer")
    c = lerp((0.24, 0.235, 0.23), (0.15, 0.147, 0.144), dig)
    c = lerp(c, (0.12, 0.118, 0.115), comp)
    c = lerp(c, (0.05, 0.05, 0.055), sint)
    set_color(pad_shader, c, 0.95 - 0.6 * sint)
    pad.GetOrderedXformOps()[0].Set(Gf.Vec3d(z1[0], z1[1], 0.04))   # progress is shown by colour
    haul = zone_progress(prog, "Z2", "hauler")
    bcomp = zone_progress(prog, "Z2", "compactor")
    bsint = zone_progress(prog, "Z2", "printer")
    h = max(0.05, 1.5 * haul)
    ops = berm.GetOrderedXformOps()
    ops[0].Set(Gf.Vec3d(z2[0], z2[1], h / 2)); ops[2].Set(Gf.Vec3f(24.0, 4.0 + 2.0 * haul, h))
    bc = lerp((0.21, 0.205, 0.2), (0.15, 0.147, 0.144), bcomp)
    set_color(berm_shader, lerp(bc, (0.06, 0.06, 0.065), bsint), 0.95 - 0.5 * bsint)
    for zid, ss in stakes.items():
        done = zone_progress(prog, zid, "surveyor") > 0.0
        for s in ss:
            UsdGeom.Imageable(s).MakeVisible() if done else UsdGeom.Imageable(s).MakeInvisible()

# ---------------------------------------------------------------- render
settings = carb.settings.get_settings()
settings.set("/rtx/post/aa/op", 3)
rp = rep.create.render_product("/World/Cam", (args.width, args.height))
writer = rep.WriterRegistry.get("BasicWriter")
writer.initialize(output_dir=args.out, rgb=True, frame_padding=4)
writer.attach([rp])
rep.orchestrator.set_capture_on_play(False)
for _ in range(20):
    app.update()
n = len(frames)
order = [int(x) for x in args.pick.split(",")] if args.pick else list(range(a, b))
apply(frames[order[0]]); place_camera(order[0], n)
rep.orchestrator.step(rt_subframes=args.subframes, delta_time=0.0)   # warm-up frame, discarded below
for k in order:
    apply(frames[k])
    place_camera(k, n)
    rep.orchestrator.step(rt_subframes=args.subframes, delta_time=0.0)
    if k % 24 == 0:
        print(f"frame {k}/{b} t={frames[k]['t']} h", flush=True)
rep.orchestrator.wait_until_complete()
print("DONE", flush=True)
app.close()
