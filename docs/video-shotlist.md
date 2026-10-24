# Demo video shot list (<= 3:00, >= 1:00 of the key modules in action)

Rendered by `tools/video/render.sh` from `tools/video/storyboard.json`: 1920x1080, 30 fps, macOS
voice, captions burned in, no music. Every figure on screen and in the voice comes from
`tools/video/facts.json`, checked against its source before anything is recorded: the Oct 24
live takes in `frontend/public/samples` (`landing_pad_final1`, `front_loaded_final2`, which the
backend's fixtures replay), the simulation and Monte Carlo files computed from them, run 4 of the
live evaluation (`docs/eval/2026-10-24-live-eval-run4/eval.rescored.csv`, Nemotron 3 Ultra arm), or,
for the Monte Carlo MTBF (a panel setting no recorded file holds), the screen itself. The figures
the app computes during the capture (the replan's +7 h, the 118 of 126 misses, the what-if's 86 %
and +49 pts) are checked against the recorded files and waited for on screen. Total 2:34 (limit
2:55); the app is on screen for 1:47 of it.

| Time | Scene | Shot | Voice-over gist |
| --- | --- | --- | --- |
| 0:00-0:08 | title | Title card | A planning agent for robot crews on the Moon: Nemotron proposes the plan, a solver proves it. |
| 0:08-0:20 | problem | Three failure chips: `energy:EX1` battery, `zone:Z1` safety, `horizon` deadline | Language models write plans that look right but quietly break what matters. |
| 0:20-0:33 | brief | The landing-pad brief, "Structure brief", the requirements card and its `recorded answer` badge; a battery field clicked; "Plan with these requirements" | Nemotron 3.5 Lightning on Nebius Token Factory turns the brief into requirements; the demo replays a run recorded live there; every field stays editable. |
| 0:33-1:04 | plan | The trace of the Oct 24 take, replayed: Nemotron 3 Ultra's first graph refused with `energy:printer`, punch-in on at least 4,095 Wh for the printer's 4,000; repair round 1 trims the berm crest's sintering 160 -> 150 m2 (diff chip), the second core is still 9 Wh over (4,009 Wh, 100.2 % of budget); repair round 2 trims it to 145 m2 and its ring holds for a beat; then the verification receipt over the verified Gantt, 72 of 336 h | Nothing Nemotron says is trusted. The refusal's proof becomes the repair prompt; Ultra trims, is still 9 Wh over, trims again, and CP-SAT proves precedence, robots, zones, batteries and the 336-hour deadline: done in 72 hours. |
| 1:04-1:22 | simulate | EX1 picked and 10 typed into the failure row; Run; the replay holds on the failure (78 % into T3) with the banner "CP-SAT rescheduled 10 tasks"; the replan card and its before/after Gantt fill the view, 72 h -> 79 h, +7 h | The plan meets the Moon: excavator one breaks at hour ten, CP-SAT reschedules the remaining ten tasks in milliseconds with no model call, done at 79 hours instead of 72, still verified. |
| 1:22-1:41 | robustness | Monte Carlo, 200 runs at MTBF 500 h: 37 % within the deadline (95 % CI 31-44 %), punch-in; 118 of 126 misses from a robot with no backup (printer, surveyor, compactor); + add PR2, SV2, CP2 | Only 37 % finish by the deadline; the misses trace to three robots with no backup, so add a spare of each and replay the same draws. |
| 1:41-1:47 | whatif | One dissolve past the seconds the what-if takes to compute: 86 % (81-90 %), +49 pts vs baseline | Now 86 % finish: a replan can hand the lost work to a spare. |
| 1:47-2:07 | impossible | The Impossible deadline sample (recorded live run): Lightning dropped the pad's compaction and the structure stage restored it ("operation restored", quoting "subgrade compacted and inspected"); repair rounds at 0 % scope drift; CP-SAT-verified advice: finishing needs 85 h, 25 h past the 60 h deadline, punch-in on 85 h | Three repairs kept every task, yet CP-SAT proves the 60-hour deadline cannot hold and finds the one it can meet: 85 hours. |
| 2:07-2:30 | evidence | Architecture slide (three Nemotron roles on Token Factory inside the solver loop), then run 4 tile by tile: 66 live runs on 22 briefs, 86 % verified, 80 % with all of the work in it, +31 pts from the repair loop (55 % on the first graph) | On the final pipeline Nemotron 3 Ultra ended verified 86 % of the time, 80 % in scope; the repair loop adds 31 points to the first try. |
| 2:30-2:34 | close | Closing card; the repository line appears once `repo_url` is set (public Oct 29-30) | Plans you can prove. |

Key-module footage (must total >= 1:00), each segment one uncut capture: the trace with both
repair rounds, the receipt and the Gantt (31 s); the simulator replay with the solver-only replan
(19 s); Monte Carlo and the spare what-if (25 s); the impossible run's advice (20 s). The README
hero GIF (`tools/video/out/readme-hero.gif`) is 14 s of the plan scene, from the refusal to the
second trim, ending on its ring.
