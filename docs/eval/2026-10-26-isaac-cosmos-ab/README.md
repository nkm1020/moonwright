# The same rock, with and without eyes: Cosmos Reason 2 in the Isaac Sim loop (Oct 26 2026)

One landing-pad mission (the plan the demo shows, `frontend/public/samples/landing_pad.plan.json`,
72 h) on NASA LOLA ground at Shackleton's rim, with a 3.6 x 2.4 x 1.4 m boulder in the lane of the
site's only compactor, CP1: a rock too small for the 5 m map the plan was made from. CP1 leaves for
Z2 at hour 34 and drives into it. The health check sees a rover that is told to drive and gains
nothing for 15 minutes. What happens next depends on what the rover can see. How the loop works:
[tools/isaac/README.md](../../../tools/isaac/README.md), `mission.py`, `cosmos_bridge.py`.

| | without perception (`without/`) | with Cosmos Reason 2 (`with/`) |
|---|---|---|
| stall | CP1 at 35.60 h, 38.1 m short of Z2 | the same |
| reading | none: a stalled rover is a lost rover | NVIDIA Cosmos Reason 2-8B on its navigation camera, 3 of 3 votes: "A large rock taller than the rover's wheel radius is directly in front of the nose, blocking the path" |
| action | Moonwright replans without CP1 | none needed: CP1 is sound; it backs up 3 m and goes around on its right |
| proof | CP-SAT: infeasible, `capability:T8`: no surviving robot can compact | the health check keeps watching; CP1 makes headway |
| outcome | stops at 61.13 h: T8, T10 and T12 can never be done, the pad is never finished | CP1 reaches Z2 at 37.99 h (an hour after T8's planned start, 81.2 m driven for 70.9 m), every task done at **72.0 h** |

T8 had 16 h of slack before the printer needed it, so the hour the detour cost does not reach the
finish. Without the camera, Moonwright did what it should with what it was told: a lost compactor
cannot be replaced, and the solver proves it. (The Isaac bridge is solver-only; a live backend would
then ask Nemotron to re-scope, which changes the brief.) The perception round trip, 76.5 s (18.5 s
to load the model, 54.9 s to answer), is time the mission clock waits; 7.1 GB of GPU memory at its
peak beside Isaac Sim on one RTX 4070 SUPER.

## Cosmos proposes, physics confirms

The same shape as the rest of Moonwright: Nemotron proposes a plan, CP-SAT proves it. Here Cosmos
proposes the cause of a stall and the physics checks it. "Blocked" makes the rover back up and go
around, under the same health check; a rover that stalls again on its way around is lost after all.
`safety/` is that case: HL2 runs into a rock at hour 1.3 and its drive fails at 1.42 h. Cosmos sees
the rock (3 of 3, right), HL2 is sent around it, its wheels do not turn, and 15 minutes later it is
lost; CP-SAT hands T5 and T6 to HL1 in 0.01 s and the new plan still ends at 72 h. A wrong "blocked"
costs one more health-check window; a wrong "clear" costs what no perception costs. The model card
says why the check is there: "its outputs directly influence physical actuation. Planning errors or
misinterpretations of the environment carry inherent life-safety risks".

## How good is the reading

`perception/`: the question `cosmos_bridge.py` asks, run by `cosmos_eval.py` on mast-camera pictures
from Isaac whose answer is known: a rover stopped against a rock, or a rover stopped by a drive fault
(`mission.py --lock`) on a clear road. The last ten were made after the question was fixed and
never used to shape it: new places, rocks 1.2 to 1.6 m in all four shapes, a fault beside a rock in
the next lane (`final-set.jpg`).

| | rocks found | drive faults cleared | all |
|---|---|---|---|
| Cosmos Reason 2-8B, 4-bit (`eval-8b.json`) | 4 of 5 | 5 of 5 | 9 of 10 |
| Cosmos Reason 2-2B (`eval-2b.json`) | 1 of 5 | 5 of 5 | 6 of 10 |

8B's miss: SV1 half up a 1.6 m rock, its nose in the air; its camera sees only sky. 2B takes a
lamp-lit rock for flat ground. Ten pictures of one site are a small test; what it supports is that
the reading is right where the camera can see the rock, and that it does not invent rocks.

It took five tries to get there, on two labelled sets of 11 (none of the final ten):

| question | model | rocks | faults | what went wrong |
|---|---|---|---|---|
| "Is the path ahead blocked?", told the rover has stalled | 2B | 3/4 | 5/7 | a 1.4 m boulder "low-lying"; far rocks and the rover's own shadow taken for obstacles. Greedy decoding looped once for 4,096 tokens; sampling as the card sets it fixed that |
| the same, asked about the rover's nose | 2B | 3/4 | 7/7 | on new pictures it then found 1 of the first 4 rocks: the 11 had been learnt |
| plus a frame from 24 min back | 2B | 4/6 | 3/5 | the same CP1 pictures read 3-0 one run and 1-3 the next |
| the same | 8B | 6/6 | 0/5 | "it has not moved, so something must be in the way"; its own long shadow read as "a large triangular rock". Adding "a stall can also be a drive fault" changed nothing |
| sight only: not told of the stall, its own shadow named | 8B | 6/6 | 5/5 | (2B: 2/6 and 5/5) |

What fixed it was taking the inference out of the perception: the camera question asks only what
is at the nose; `mission.py` knows the rover has stalled and puts the two together. Up to five
sampled answers vote, until three agree.

## Reproduce

Windows 11, RTX 4070 SUPER, Isaac Sim 5.1; the Cosmos venv as in `cosmos_bridge.py` (Cosmos Reason
2-8B is gated: accept its terms on Hugging Face).

    python mission.py landing_pad.plan.json without --ground lola_site.npz --boulder=3.2,-8,3.6,2.4,1.4 --window 34,38 --clip-seconds 10 --uv <uv> --backend <repo>/backend
    python mission.py landing_pad.plan.json with    --ground lola_site.npz --boulder=3.2,-8,3.6,2.4,1.4 --window 34,38 --clip-seconds 10 --cosmos-python <cosmos venv python> --uv <uv> --backend <repo>/backend
    python mission.py landing_pad.plan.json safety  --ground lola_site.npz --boulder=-1,-8,3.6,2.4,1.4 --lock HL2@1.42 --until 3 --cosmos-python <cosmos venv python> --uv <uv> --backend <repo>/backend
    cd perception && <cosmos venv python> ../../../../tools/isaac/cosmos_eval.py labels.json out.json
    python render_replay.py with/track.json clip_with --ground lola_site.npz --shots boulder --sun-el 10        (without: --shots boulder-hold)

The clips (`with.webm`, `without.webm`, mission hours 34 to 38 in 10 s, the middle of the frame)
raise the sun to 10 degrees so the slope the rock sits on reads; the mission's camera sees it at 6,
and the real sun there never climbs above about 1.8.
