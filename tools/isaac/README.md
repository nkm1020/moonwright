# Isaac Sim replay of a verified schedule

A kinematic replay, in NVIDIA Isaac Sim 5.1, of the landing-pad run the demo shows: the CP-SAT-verified
schedule as Moonwright's own simulator executed it, including EX1 failing at 10 h and the solver-only
replan. Every robot position and every task's progress comes from the recorded simulation; Isaac Sim
renders it (RTX, low south-polar sun, procedural regolith and craters). It is a visualisation: robots
are block proxies by type, nothing is dug or hauled physically, and no physics decides anything.

What changes on screen: the pad darkens as it is dug, compacted and sintered; the berm grows with the
hauled volume; survey stakes appear once a zone is surveyed; a failed robot turns red with a beacon and
is pulled aside while its replacement works. Idle robots wait in parking slots west of the pad (the
depot shares its cell with the pad, so the simulator stacks them there).

## Run it

1. The dense simulation (every quarter hour), from `backend/`:

       uv run moonwright simulate ../frontend/public/samples/landing_pad.plan.json --fail EX1@10 \
         --llm fixture --brief-id landing_pad --all-frames --out out/landing_pad.dense.sim.json

2. The render track (no Isaac Sim needed): 432 frames, 18 s at 24 fps, slowed around the failure:

       python3 tools/isaac/make_track.py backend/out/landing_pad.dense.sim.json \
         frontend/public/samples/landing_pad.plan.json tools/isaac/out/track.json

3. On a machine with an RTX GPU (tested: Windows 11, RTX 4070 SUPER 12 GB, driver 610.62), in a
   virtual environment made from the **official python.org Python 3.11** (a uv-managed CPython build
   crashed at renderer start-up in `rtx.scenedb.plugin`):

       python -m pip install "isaacsim[all,extscache]==5.1.0" --extra-index-url https://pypi.nvidia.com
       set OMNI_KIT_ACCEPT_EULA=YES
       python render_replay.py track.json full          # PNGs under ~/omni.replicator_out/full
       ffmpeg -framerate 24 -start_number 1 -i rgb_%04d.png -c:v libx264 -pix_fmt yuv420p -crf 17 isaac_replay.mp4

   `--pick 20,62,200,420` renders a few stills to check the look; the first written frame is a
   warm-up and is skipped by `-start_number 1`. Headless rendering works over SSH.
