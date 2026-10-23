# Demo video shot list (<= 3:00, >= 1:00 of the key modules in action)

Record at 1440x900 or 1920x1080, UI zoomed so labels are readable, no copyrighted music.
Voice-over in English. Target 2:40 so there is slack.

| Time | Shot | Voice-over gist |
| --- | --- | --- |
| 0:00-0:20 | Title card, then the problem: a drawing of the Earth-Moon delay and a robot crew | Robots on the Moon execute plans alone; a plan that breaks a battery budget or a safety zone is dangerous, and language models cannot guarantee either. |
| 0:20-0:40 | The UI: pick the landing-pad brief, "Structure brief", requirements card appears | Nemotron turns the brief into requirements the planner can correct. |
| 0:40-1:25 | "Plan": the progress trace streams structure -> task graph -> validate -> schedule; switch to the tight-battery brief to show a failing schedule, the conflict chips (energy:EX1, energy:EX2) and the repair round | The solver proves the first graph infeasible, names the conflicting constraints, and Nemotron repairs the graph. Only verified plans continue. |
| 1:25-1:50 | Gantt + explanation for the verified plan; hover a task to show precedence arrows | Every bar was checked by CP-SAT: precedence, one task per robot, one robot per zone, battery budgets, the deadline. |
| 1:50-2:25 | Simulator: inject EX2 failure at 30 h, replay, the agent replans, before/after Gantt comparison | When a robot fails, the remaining work is replanned from the task-graph stage and re-verified; here CP-SAT reschedules the remaining work solver-only and the makespan moves from 80 h to 95 h. |
| 2:25-2:40 | Monte Carlo panel: 50 runs, within-deadline probability and histogram | Robustness in one number. |
| 2:40-2:55 | Architecture slide: Token Factory routing (Lightning / Ultra / Super), CP-SAT, simulator; loop on/off table | What runs where, and what the loop is worth: [MEASURE]. |
| 2:55-3:00 | Closing card with repo URL | |

Key-module footage (must total >= 1:00): the streaming trace with a repair round, the Gantt,
the simulator replay with the replan. Keep those segments uncut.
