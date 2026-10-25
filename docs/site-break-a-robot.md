# Site panel spec: "Break a robot" (for the UI session)

A panel on `design/landing-1/run.html` where a visitor (a judge) picks any robot of the landing pad
run and any hour, breaks it, and sees Moonwright's replan. It replaces the old Next.js app scene in
the demo video, so the video stays in the site's look while still showing a computation, not a
replay. The data side is done; this file is what the page needs.

## Where

The landing pad run, a new scene between `failure` (Isaac Sim, EX1 at hour 10) and `odds`: the
failure scene shows one breakdown, this one lets the visitor choose another. Suggested step label
"Your turn" and nav button "Next: break one yourself".

## Data

Two sources, same shape of answer:

1. **Precomputed (always available, Pages included):** `data/landing_pad.failures.json` (115 KB),
   every robot x every whole hour 0-71, made by `backend/scripts/failure_table.py` with the same
   code as `POST /simulate` in fixture mode. Top level: `makespan_h` 72, `horizon_h` 336,
   `robots` [EX1, EX2, HL1, HL2, CP1, PR1, SV1], `rows`. Each row:

   | field | meaning |
   |---|---|
   | `robot`, `t_h` | the failure |
   | `task`, `progress` | what it was doing (null when idle or driving), 0..1 done |
   | `outcome` | `replanned`, `infeasible` or `no_effect` |
   | `finished_at_h`, `delay_h` | for `replanned` (and `no_effect`: 72, 0) |
   | `remaining`, `solve_s`, `message` | tasks rescheduled, CP-SAT seconds, the replan step's sentence |
   | `core` | for `infeasible`: the constraint groups, e.g. `["capability:T7", "capability:T8"]` |

2. **Live (when a backend is running, e.g. the video's capture, or `uv run moonwright serve`):**
   `POST http://127.0.0.1:8074/simulate` with JSON
   `{"plan": <data/landing_pad.plan.json>, "failures": [{"robot": "HL2", "t_h": 24}], "brief_id": "landing_pad", "frame_stride": 10000}`.
   CORS is open for `http://127.0.0.1:*` and `http://localhost:*` (checked from :3074). Answer
   (SimResult): `finished_at_h`, `completed`, `events[0].task` / `.detail` ("HL2 failed at 24 h
   while working on T5 (60% done)"), `replans[0].remaining_tasks` (list), `replans[0].after.success`,
   `replans[0].after.steps[-1].message`. No replan in `replans` = no effect. 0.07 s round trip.
   Suggested detection: `GET http://127.0.0.1:8074/health` with a ~600 ms timeout on load; if it
   answers, use live, else the table. Say which on screen ("computed just now by CP-SAT" /
   "precomputed by the same replanner").

## What it shows (all three outcomes occur)

- `replanned`: the new finish as the scene's big figure (`79 H`), a delay chip (`+7 h`, or "no
  delay" in green), and the sentence: "CP-SAT rescheduled 10 tasks in 0.01 s; Nemotron not needed."
  Examples: EX1 at 10 h -> 79 h (+7, the failure scene's case); HL2 at 24 h -> 72 h (the other
  hauler takes over).
- `infeasible` (CP1, PR1, SV1 at almost any hour: they have no backup): a `No replan` pill and
  "No surviving robot can do T7, T8 (`capability:T7`, `capability:T8`). The plan cannot finish
  without a spare compactor." This is the odds scene's point, so it leads into it.
- `no_effect`: "EX2 has no work left at hour 30: nothing to replan." EX2 and HL1 are never needed
  in this plan (the second excavator and hauler are spares until a failure).

## Hooks the video needs (keep these names, or tell me yours)

- robot picker: a `<select>` with accessible name "Robot to break", option values = robot ids
- hour: an input (number or range) with accessible name "Hour of the failure", 0-71
- action: a button "Break it"
- result: an element with `id="break-result"` whose text includes "Finished at 72 h" for a
  replanned run (or "No replan" / "nothing to replan"), and the replan sentence
- the scene's nav: "Next: break one yourself" from the failure scene

The video will pick HL2, type 24, press "Break it" and wait for "Finished at 72 h" with the backend
running, so the shot is a live CP-SAT replan in the site's own design.
