# Demo video shot list (<= 3:00, >= 1:00 of the key modules in action)

Record at 1440x900 or 1920x1080, UI zoomed so labels are readable, no copyrighted music.
Voice-over in English. Target 2:40 so there is slack.

| Time | Shot | Voice-over gist |
| --- | --- | --- |
| 0:00-0:20 | Title card, then the problem: a drawing of the Earth-Moon delay and a robot crew | Robots on the Moon execute plans alone; a plan that breaks a battery budget or a safety zone is dangerous, and language models cannot guarantee either. |
| 0:20-0:40 | The UI: the landing-pad brief, "Structure brief", requirements card appears (Nemotron 3.5 Lightning, 7.7 s) | Nemotron turns the brief into requirements the planner can correct. |
| 0:40-1:25 | "Plan": the progress trace streams structure -> task graph -> validate; the validator refuses Nemotron 3 Ultra's first graph with `energy:printer` (4,112 Wh of printer work and travel for PR1's 4,000 Wh), one repair round trims the berm sintering (160 -> 146 m2) and halves the doubled hauls (2 x 60 m3), and CP-SAT proves 89 h optimal. Cut to "Impossible deadline": three repairs, no plan, CP-SAT-proven advice "finishing needs 85 h" | The verifier names the conflict in numbers and Nemotron repairs the graph; only verified plans continue, and an impossible brief ends with proven advice instead of shrunken work. |
| 1:25-1:50 | Gantt + explanation for the verified plan; hover a task to show precedence arrows | Every bar was checked by CP-SAT: precedence, one task per robot, one robot per zone, battery budgets, the deadline. |
| 1:50-2:25 | Simulator: EX2 fails at 10 h, 44 % into T5; the replay holds on the event, the agent replans, before/after Gantt comparison | When a robot fails, the remaining work is replanned and re-verified; here CP-SAT reschedules the 13 remaining tasks solver-only and the run finishes at 91 h instead of 89 h. |
| 2:25-2:40 | Monte Carlo panel: 200 runs, 37.5 % within the deadline (95 % CI 31-44 %), compactor, printer and surveyor as single points of failure; one spare of each: 84 % (78-88 %) | Robustness in one number, and what buys it. |
| 2:40-2:55 | Architecture slide: Token Factory routing (Lightning / Ultra / Super), CP-SAT, simulator; loop on/off table | What runs where, and what the loop is worth: [MEASURE]. |
| 2:55-3:00 | Closing card with repo URL | |

Key-module footage (must total >= 1:00): the streaming trace with a repair round, the Gantt,
the simulator replay with the replan. Keep those segments uncut.
