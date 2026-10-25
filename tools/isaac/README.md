# Moonwright in NVIDIA Isaac Sim: PhysX rovers in the loop

The landing-pad mission the demo shows, run in NVIDIA Isaac Sim 5.1 with Moonwright closing the loop.
**Isaac Sim is the world, Moonwright is the brain.** Seven four-wheeled rovers carry out the
CP-SAT-verified schedule on the rim of Shackleton crater, on terrain NASA's Lunar Orbiter Laser
Altimeter measured, under lunar gravity (1.62 m/s²). At 10 h excavator EX1
suffers a drive fault while it is 78 % through its first dig. The rovers' telemetry (positions,
batteries, task progress, the failed robot) goes to Moonwright's replanner, CP-SAT reschedules the
remaining work without the lost robot, and the rovers carry out the new verified schedule.

| measured in Isaac Sim (`mission.py`, landing_pad, EX1 fault at 10 h) | |
|---|---|
| replan | CP-SAT rescheduled 10 tasks in 0.01 s, Nemotron not needed; makespan 72 h → 79 h |
| Isaac → Moonwright → Isaac round trip | 1 to 4 s, mostly the start of a `uv run` of the backend (the mission clock waits) |
| mission complete | 79.0 h, every task done, the same finish as Moonwright's 2D simulator |
| drives | 6 completed, none reached its zone after the planned start; the tightest arrived 0.025 h (1.5 minutes of mission time) early |
| largest gap to the planner's travel model | +9.7 % on one leg: a 180° turn in place before the drive (see below) |
| physics | 284,397 PhysX steps at 1/30 s, 11 min on an RTX 4070 SUPER |

## The ground: Shackleton's rim

`prep_lola.py` cuts the mission's ground out of NASA LOLA's 5 m/px height map of Site04, the rim of
Shackleton crater ([PGDA](https://pgda.gsfc.nasa.gov/products/78); Barker et al. 2021, Planetary &
Space Science 203, 105119): the gentlest 120 x 40 m north-south strip within 1.5 km of the drop into
the crater (89.766° S, 1,729 m, mean slope 1.8°, steepest 5.2° at 5 m). The physics ground is the map
interpolated to 1 m over ±200 m, with the craters smaller than the map resolves (1-6 m) added off
the site; the two work areas are graded level and the 70 m drive between them runs about 1° downhill.
The renderer adds a 0.5 m skin of regolith roughness and small pits near the site, 4,000 boulders
off the lanes, and the rest of the 16 km tile as the backdrop (cut at 4.5 km: 6 km north the tile
has a gap LOLA filled with a plane). The sun is drawn 6° above the horizon so the site reads on
screen; at 89.8° S it never climbs above about 1.8° (`prep_lola.py` checks that a 1.5° sun clears
the site's horizon at every azimuth: the rim is one of the best-lit places near the pole).

## What is physical and what is not

- **Physical:** driving. Every rover is an articulation (a 240 kg chassis, four 12 kg wheels on
  revolute joints with velocity drives, wheel–regolith friction 0.9/0.7 static/dynamic) steered by a
  heading controller: it turns in place when its heading is off by more than 20°, then drives at the
  plan's speed with an acceleration limit. Arrival times, distances driven and the batteries' travel
  drain (0.5 Wh per 2 m cell, the planner's figure) come from what PhysX did.
- **Not physical:** the work. Digging, hauling, compacting and sintering progress with time as the
  schedule says (the simulator's rule: a task starts once its start hour has come, its predecessors
  are done and nobody else works in its zone); the pad's colour and the berm's height are drawn from
  that progress. The fault is injected (the rover's drives lose power and it reports the fault); a
  health check also declares a rover failed when, while driving, it gains less than 0.5 m on its
  target in a quarter hour (not triggered in this run).
- **Time scale:** 120 simulated seconds per mission hour, so the plan's speed of 12 cells (24 m) per
  hour is a 0.2 m/s drive, RASSOR's class of speed. Work and driving run on the same clock.

Execution follows Moonwright's 2D simulator (`backend/moonwright/sim/simulator.py`): a rover runs its
entries in order, leaves for the next zone as soon as it is free, waits at its spot until it may
start. Each rover has its own lane, so rovers sharing a zone never stack, and every work spot sits
6.5 m south of its zone centre (the berm stands on Z2's centre); the shifts are the same at both
zones, so every leg is as long as the planner's straight line between zone centres. Telemetry speaks
the planner's frame: a rover at its spot is in its zone (the zone's centre cell), a rover on the road
is reported on its lane's centre line.

**What the physics showed.** Moonwright's planner (like its 2D simulator) times a leg as straight-line
distance over speed, rounded up to whole hours. The PhysX rovers matched that on every straight drive
(median −0.1 %, inside the 0.4 m arrival tolerance), but a skid-steer rover that has to reverse its
heading first turns in place on regolith far slower than commanded (its wheels scrub sideways): SV1's
last leg took 0.28 h of mission time (33 s simulated) longer than the straight drive, which the
travel model does not count. In this mission the rounding absorbed it (that leg had 9.9 h of
slack); on a leg with the tightest slack seen here (0.025 h) it would arrive late. A turn allowance
per leg is the obvious next input to the planner. (The same mission on a procedural ground gave the
same finish and arrivals within 0.03 h.)

## The loop

1. `mission.py` (Isaac Sim's Python 3.11) builds the terrain (`lunar_terrain.py`, shared with the
   renderer), the rovers and the plan in force, and steps PhysX.
2. On a fault it writes the frame it measured and the event to `replan_<n>.in.json` and runs
   `replan_bridge.py` with the backend's own environment (`uv run --project backend`, Python 3.13):
   `moonwright.sim.replan.replan_after_event` with the solver-only planner, the same code path the 2D
   simulator's replans take. No Nemotron call is made (the Isaac machine holds no API key; this replan
   does not need one, as the 2D run shows).
3. The verified schedule in `replan_<n>.out.json` replaces the rovers' queues: a task underway on a
   healthy rover continues where it is, the lost rover's dig goes to EX2 in full.

## Run it

On a machine with an RTX GPU (tested: Windows 11, RTX 4070 SUPER 12 GB, driver 610.62), in a virtual
environment made from the **official python.org Python 3.11** (a uv-managed CPython build crashed at
renderer start-up in `rtx.scenedb.plugin`):

    python -m pip install "isaacsim[all,extscache]==5.1.0" --extra-index-url https://pypi.nvidia.com
    set OMNI_KIT_ACCEPT_EULA=YES

The backend next to it (`git archive HEAD backend frontend/public/samples`, then `uv sync --no-dev` in
`backend/`; uv can come from `pip install uv` in a separate venv). Then:

    curl -L -o Site04_surf.tif https://pgda.gsfc.nasa.gov/data/LOLA_5mpp/Site04/Site04_final_adj_5mpp_surf.tif
    uv run --no-project --with tifffile --with scipy --with numpy python prep_lola.py Site04_surf.tif 1238 1502 lola_site.npz
    python mission.py repo/frontend/public/samples/landing_pad.plan.json mission_lola --fail EX1@10 ^
        --ground lola_site.npz --uv <path to uv.exe> --backend repo/backend
    python render_replay.py mission_lola/track.json isaac_lola --assets . --ground lola_site.npz
    ffmpeg -framerate 24 -start_number 1 -i rgb_%04d.png -c:v libx264 -pix_fmt yuv420p -crf 17 isaac_lola.mp4

(`prep_lola.py` runs anywhere, with uv; the 41 MB map and the 3 MB `lola_site.npz` are not committed.
Without `--ground` both scripts use a procedural ground.)

`mission.py` writes `track.json` (480 frames, 20 s at 24 fps: the clip spends more of its time where
rovers drive and around the fault), `mission.json` (legs, events, replans, finish) and the replan's
input and output. `render_replay.py --pick 20,62,200,420` renders a few stills to check the look; the
first written frame is a warm-up and is skipped by `-start_number 1`. Headless rendering and physics
both work over SSH. The evidence of the run above is in `docs/eval/2026-10-25-isaac-closed-loop/`.

The robots are drawn with NASA models from [NASA 3D Resources](https://github.com/nasa/NASA-3D-Resources)
(free and without copyright; no NASA endorsement is implied): RASSOR, NASA's Regolith Advanced Surface
Systems Operations Robot, stands in for every mobile robot, at the size of the physics chassis (a light
on top marks the role: amber excavator, blue hauler, green compactor, white surveyor with a survey
mast); the sintering printer is a gantry truss on the same chassis. Get the model and convert it once:

    B=https://raw.githubusercontent.com/nasa/NASA-3D-Resources/master/3D%20Models
    curl -L -o rassor.glb "$B/Regolith%20Advanced%20Surface%20Systems%20Operations%20Robot%20(RASSOR)/Regolith%20Advanced%20Surface%20Systems%20Operations%20Robot%20(RASSOR).glb"
    python convert_assets.py rassor.glb                      # -> rassor.usd (Kit asset converter)

The NASA glTF's up axis needs a -90° turn about X to stand RASSOR on its wheels (`inspect_asset.py`
renders a model in four orientations). `make_track.py` still turns a 2D simulation into a kinematic
track for the same renderer (no physics), which is how the first clip was made.
