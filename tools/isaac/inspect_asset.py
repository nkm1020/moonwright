"""Render one asset in several orientations to check which way is up. Usage: inspect_asset.py <asset.usd> <out_dir>"""
import math, os, sys
os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
from isaacsim import SimulationApp
app = SimulationApp({"headless": True, "width": 1280, "height": 720, "renderer": "RaytracedLighting"})
import omni.usd, omni.replicator.core as rep
from pxr import Gf, Usd, UsdGeom, UsdLux
asset, out = os.path.abspath(sys.argv[1]), sys.argv[2]
ctx = omni.usd.get_context(); ctx.new_stage(); st = ctx.get_stage()
UsdGeom.SetStageUpAxis(st, UsdGeom.Tokens.z)
src = Usd.Stage.Open(asset)
print("upAxis", UsdGeom.GetStageUpAxis(src), "mpu", UsdGeom.GetStageMetersPerUnit(src), flush=True)
for p in list(src.GetPseudoRoot().GetChildren())[:5]: print("root child", p.GetPath(), p.GetTypeName(), flush=True)
g = UsdGeom.Mesh.Define(st, "/Ground")
g.CreatePointsAttr([(-20, -20, 0), (20, -20, 0), (20, 20, 0), (-20, 20, 0)]); g.CreateFaceVertexCountsAttr([4]); g.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
sun = UsdLux.DistantLight.Define(st, "/Sun"); sun.CreateIntensityAttr(3000)
UsdGeom.Xformable(sun).AddRotateXYZOp().Set(Gf.Vec3f(40, 0, 30))
UsdLux.DomeLight.Define(st, "/Sky").CreateIntensityAttr(300)
variants = [("rx+90", 90.0, 0.0), ("rx-90", -90.0, 0.0), ("none", 0.0, 0.0), ("rx+90_rz90", 90.0, 90.0)]
for i, (name, rx, rz) in enumerate(variants):
    x = UsdGeom.Xform.Define(st, f"/V{i}")
    x.AddTranslateOp().Set(Gf.Vec3d(-9 + i * 6, 0, 0)); x.AddRotateZOp().Set(rz); x.AddRotateXOp().Set(rx)
    k = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    x.AddScaleOp().Set(Gf.Vec3f(k, k, k))
    x.GetPrim().GetReferences().AddReference(asset)
cam = UsdGeom.Camera.Define(st, "/Cam"); cam.CreateFocalLengthAttr(24); cam.CreateHorizontalApertureAttr(36); cam.CreateVerticalApertureAttr(20.25)
UsdGeom.Xformable(cam).AddTransformOp().Set(Gf.Matrix4d().SetLookAt(Gf.Vec3d(0, -14, 5), Gf.Vec3d(0, 0, 0.8), Gf.Vec3d(0, 0, 1)).GetInverse())
rp = rep.create.render_product("/Cam", (1280, 720))
w = rep.WriterRegistry.get("BasicWriter"); w.initialize(output_dir=out, rgb=True); w.attach([rp])
for _ in range(10): app.update()
rep.orchestrator.step(rt_subframes=8); rep.orchestrator.step(rt_subframes=8)
rep.orchestrator.wait_until_complete()
print("variants left to right:", [v[0] for v in variants], flush=True)
app.close()
