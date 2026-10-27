# A drive fault on a clear road: Cosmos Reason 2 sees nothing in the way, CP-SAT replans (Oct 27 2026)

The other answer to the question of [the A/B run](../2026-10-26-isaac-cosmos-ab/README.md). The same
landing-pad plan (`frontend/public/samples/landing_pad.plan.json`, 72 h) on the same NASA LOLA ground
at Shackleton's rim, no rock anywhere. At 1.50 h hauler HL2's drive fails on its way to Z2
(`--lock HL2@1.5`: the wheels hold while the rover still thinks it drives). The health check sees a
rover that is told to drive and gains nothing; Cosmos Reason 2 is asked what is at its nose.

    python mission.py landing_pad.plan.json hl2_clear --ground lola_site.npz --lock HL2@1.5 --cosmos-python <cosmos venv python> --uv <uv> --backend <repo>/backend

| | |
|---|---|
| stall | HL2 at 1.73 h, 34.1 m short of Z2: no headway for 15 min |
| reading | NVIDIA Cosmos Reason 2-8B, 4-bit, on its navigation camera (`perception_1.png`), 3 of 3 votes clear: "The ground directly in front of the rover's nose is flat and grey, with no visible obstacles or objects taller than the wheel radius (0.4 m)." 20.1 s to load the model, 64.2 s to answer, 7.12 GB of GPU memory at its peak |
| action | nothing at the nose, so the fault is the rover's own: HL2 is lost and Moonwright replans without it |
| replan | `replan_1.out.json`: "CP-SAT rescheduled 12 tasks in 0.01 s, Nemotron not needed; makespan 72 h -> 72 h", 4.09 s round trip; T5 and T6 go to HL1, which sets off at once and reaches Z2 at 4.65 h, 16.4 h before T5 is due |
| finish | every task done at 72.70 h (wall 714 s, 261,708 PhysX steps) |

The long dark wedge ahead in the camera picture is HL2's own shadow, the sun low behind it: the
thing that 8B, asked a question that told it the rover had stalled, once called "a large triangular
rock". Asked only what it sees, it does not. This picture is in neither of the labelled sets the
question was shaped or scored on.

## Why 72.7 h and not 72

The plan counts whole hours; the stall came at 1.73 h. Surveyor SV1 was 58 % through its survey T1.
The replanner maps planner hour 0 to the whole hour before the stall (1 h) and gives the rest of the survey,
1.27 h of work, as 2 whole hours, so the plan has it end at 3 h. The rovers, like Moonwright's 2D
simulator, work a locked task's planned hours from the moment of the replan, so SV1 finishes at 3.73
h and reaches Z2 0.66 h after T2's planned start. The slip runs down the survey, compact and print
chain (`mission.json`, `late_legs`: SV1 0.66 h, CP1 0.68 h, PR1 0.70 h) to the last task. The backend
states it (`moonwright/sim/replan.py`, time convention: "a robot on a locked task still finishes it up
to that fraction of an hour after its planned end"). A failure at a whole hour has no slip: the
Oct 25 run lost EX1 at 10.00 h and finished at exactly the replanned 79 h.

## The repeat, and the clip

`repeat/` is the same command run again with `--window 0.5,5`, which only changes how `track.json`
samples the run: the 20 s clip on the site covers hours 0.5 to 5 at an even pace (HL2 stops at
4.5 s, turns red at 5.5 s as HL1 sets off, HL1 reaches Z2 at 18.4 s). The physics repeated to the
step: 261,708 PhysX steps, the same legs, the stall at 1.73 h, the same replan, 72.70 h. The
renderer did not: the second camera picture differs from the first by a little noise (PSNR 42 dB),
and Cosmos, sampling, needed all five answers: 3 clear, 1 blocked, 1 with no answer it could read,
168.3 s. The reading held; the margin and the time are what vary.

    render_replay.py hl2_clear2/track.json isaac_hl2w --assets . --ground lola_site.npz
    ffmpeg -framerate 24 -start_number 1 -i rgb_%04d.png -frames:v 480 -c:v libvpx-vp9 -b:v 0 -crf 38 -row-mt 1 -pix_fmt yuv420p -an isaac-hl2-clear.webm

## Files

`mission.json` (legs, events, the perception, the replan, the finish), `perception_1.png` and
`perception_1.json` (the picture, the question word for word, all three sampled answers),
`replan_1.in.json` (the frame the rovers reported) and `replan_1.out.json` (what came back),
`track.json` (the run's motion over all 72 h); `repeat/` holds the same files for the second run.
