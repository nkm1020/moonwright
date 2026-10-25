# Isaac Sim closed loop on Shackleton's rim: landing pad, EX1 lost at 10 h (Oct 25 2026)

The landing-pad plan the demo shows (`frontend/public/samples/landing_pad.plan.json`, verified at
72 h) carried out by PhysX rovers in NVIDIA Isaac Sim 5.1 on real south-pole terrain, with excavator
EX1's drive fault injected at 10 h and Moonwright replanning from the rovers' telemetry. How it works
and what is physical: [tools/isaac/README.md](../../../tools/isaac/README.md).

    python mission.py landing_pad.plan.json mission_lola --fail EX1@10 --ground lola_site.npz --uv <uv> --backend <repo>/backend

The ground is NASA LOLA's 5 m/px map of Shackleton's rim (Site04; Barker et al. 2021), cut around the
gentlest 120 x 40 m strip within 1.5 km of the drop into the crater: 89.766° S, 1,729 m, mean slope
1.8°. Windows 11, RTX 4070 SUPER, Isaac Sim 5.1 (python.org Python 3.11); the backend in its own uv
environment (Python 3.13). 284,397 PhysX steps at 1/30 s, 120 simulated seconds per mission hour,
649 s of wall time.

| | |
|---|---|
| fault | EX1 at 10.00 h, 78 % through T3 (its first dig) |
| what went to Moonwright | `replan_1.in.json`: the frame the rovers reported (positions in the planner's cells, batteries, statuses, progress) and the event |
| what came back | `replan_1.out.json`: "CP-SAT rescheduled 10 tasks in 0.01 s, Nemotron not needed; makespan 72 h -> 79 h"; T3 and T4 go to EX2 from 10 h |
| round trip | 3.9 s, most of it a cold `uv run` start (the mission clock waits) |
| finish | 79.0 h, every task done (the 2D simulator's run of the same failure: 79 h) |
| drives | 6 completed, 0 reached the zone after the planned start; 1 more was rerouted by the replan |

The six completed drives (`mission.json` legs; hours are mission hours, `ideal_h` is the straight
leg at the plan's speed, `slack_h` the planned start minus the arrival):

| rover | to | depart | arrive | ideal | overhead | slack |
|---|---|---|---|---|---|---|
| HL2 | Z2 (T5) | 0.00 | 2.91 | 2.92 | -0.4 % | 18.10 |
| SV1 | Z2 (T2) | 3.00 | 5.92 | 2.92 | 0.0 % | 0.08 |
| SV1 | Z1 (T11, after the reroute) | 10.00 | 11.29 | 1.27 | +2.0 % | 52.71 |
| CP1 | Z2 (T8) | 41.00 | 43.96 | 2.96 | -0.2 % | 0.04 |
| PR1 | Z2 (T10) | 64.00 | 66.98 | 2.99 | -0.4 % | 0.025 |
| SV1 | Z2 (T12) | 65.00 | 68.12 | 2.84 | +9.7 % | 9.88 |

Straight drives arrive a hair early (the 0.4 m arrival tolerance). SV1's last leg starts facing the
wrong way: the skid-steer rover turns in place on regolith much slower than commanded, which cost
0.28 h of mission time (33 s simulated). The planner's travel model (straight line over speed,
rounded up to whole hours) has no term for it. The rounding absorbed it on this leg, but the three
tight straight drives had 0.025 to 0.08 h to spare, less than that turn.

The same mission on a procedural ground (an earlier run the same day) gave the same finish, replan
and arrivals within 0.03 h; the drive between the zones here runs about 1° downhill.

Files: `mission.json` (legs, events, replans, finish, the ground), `replan_1.in.json`,
`replan_1.out.json`.
