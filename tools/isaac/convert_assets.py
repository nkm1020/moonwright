"""Convert the downloaded glTF models (NASA 3D Resources, public domain) to USD with Kit's asset converter.
Usage: python convert_assets.py rassor.glb perseverance.glb  -> rassor.usd, perseverance.usd"""
import asyncio, os, sys
os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
from isaacsim import SimulationApp
app = SimulationApp({"headless": True})
from isaacsim.core.utils.extensions import enable_extension
enable_extension("omni.kit.asset_converter")
for _ in range(10): app.update()
import omni.kit.asset_converter as converter

async def convert(src, dst):
    ctx = converter.AssetConverterContext()
    ctx.ignore_animations = True; ctx.ignore_camera = True; ctx.ignore_light = True
    ctx.single_mesh = False; ctx.use_meter_as_world_unit = True
    task = converter.get_instance().create_converter_task(src, dst, None, ctx)
    ok = await task.wait_until_finished()
    print("CONVERT", src, "->", dst, "ok" if ok else f"FAILED {task.get_status()} {task.get_detailed_error()}", flush=True)

for src in sys.argv[1:]:
    dst = os.path.splitext(os.path.abspath(src))[0] + ".usd"
    fut = asyncio.ensure_future(convert(os.path.abspath(src), dst))
    while not fut.done():
        app.update()
app.close()
