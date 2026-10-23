# Devpost submission draft (English). Numbers in [MEASURE] slots are filled only from measured runs.

**Project name:** Moonwright
**Track:** Physical AI (simulation-only entry; an embodied-agent planning runtime)
**Tagline:** Nemotron proposes the construction plan, CP-SAT proves it, Nemotron repairs what the solver rejects, and the agent replans when a robot fails.

## Inspiration

A lunar construction crew (excavators, haulers, a compactor, a regolith printer, a surveyor) will
work with limited communication windows and no operator in the loop. A plan that overdraws a
battery or puts two robots in the same blast zone is not a small bug on the Moon. Large language
models are very good at turning a construction brief into a work breakdown and very bad at
guaranteeing that the breakdown is executable. We wanted the two halves to talk: the model plans,
a constraint solver proves the plan against the physical budgets, and when the solver says no it
tells the model exactly which constraints collided, so the model can repair the plan instead of
guessing again.

## What it does

1. **Brief to requirements.** A plain-English brief ("build a 20 m landing pad and a 60 m blast
   berm with two excavators, two haulers, a compactor, a printer and a surveyor within 14 days")
   becomes structured requirements (site grid and work zones, structures with dimensions, fleet
   with battery budgets, deadline) that the planner can correct before anything is scheduled.
2. **Task graph with quantities.** Nemotron proposes the tasks: what to do, on which structure, in
   which zone, how much (cubic metres excavated, square metres compacted or sintered), in what
   order, with which robot type. Durations and energies are not the model's guess: a documented
   rate model turns the quantities into hours and watt-hours, so the model cannot make a plan
   feasible by shrinking numbers.
3. **Proof.** A deterministic validator catches structural defects (cycles, unknown references,
   missing phases for a structure, work no robot can do), then an OR-Tools CP-SAT model schedules
   the graph: precedence, one task per robot at a time, one robot per safety zone, per-robot
   battery budgets including every leg the robot drives (travel is exact: CP-SAT picks each
   robot's order and pays the legs of that order), the deadline, makespan minimised and proven.
4. **Repair with reasons.** When the model's graph is infeasible, CP-SAT returns a minimal set of
   conflicting constraints (for example `energy:EX1`, `precedence:T4->T2`) with the numbers behind
   them ("EX1 and EX2 each have 5,000 Wh; the excavator tasks need at least 9,915 Wh in total,
   15 Wh of it travel, and cannot be split across 2 excavators (10,000 Wh combined) without
   exceeding one budget").
   That goes back to Nemotron, up to three rounds; every change the model makes is shown as a
   diff, and scope reductions are flagged for the planner's approval. Only solver-verified
   schedules reach the Gantt chart, together with a Nemotron-written explanation.
5. **Execution and replanning.** The plan runs in a 2D site simulator with travel, battery drain
   and injected failures. When a robot fails, the agent locks the in-progress work, reschedules the
   remaining tasks with the solver in milliseconds, and escalates to Nemotron only when the
   remaining fleet provably cannot finish, so the work has to be re-scoped. Before and after are
   shown side by side.
6. **Robustness.** A Monte Carlo panel replays the plan hundreds of times with random robot
   failures and duration jitter and reports the probability of finishing within the deadline, which
   robots are single points of failure, and what a spare would buy.

## How we built it

- **Models on Nebius Token Factory (OpenAI-compatible API):** `nvidia/Nemotron-3_5-Lightning` for
  the short structuring call (thinking off, ~4 s), `nvidia/Nemotron-3-Ultra-550b-a55b` with
  reasoning for task-graph generation and repair (the step that needs multi-step decomposition),
  `nvidia/nemotron-3-super-120b-a12b` for the explanation prose. Each size does the job its size
  fits; the UI shows which model answered each step with tokens and latency. Structured output
  uses `response_format` (JSON schema first, JSON mode as fallback) and every answer is validated
  with pydantic and re-asked with the validation errors when needed.
- **Verification:** Python validator plus a CP-SAT model with optional intervals per
  (task, robot), an `AddCircuit` per robot whose arcs time and charge the leg between two
  consecutive tasks (exact, sequence-dependent travel), `AddNoOverlap` per robot and per zone,
  linear energy budgets and a makespan objective. Infeasibility explanations use assumption
  literals per named constraint group, `SufficientAssumptionsForInfeasibility`, a deletion
  filter to make the core minimal, and a multi-worker fallback for instances the single-worker
  proof cannot finish.
  Every schedule is re-verified by an independent checker before it is shown.
- **Simulation and replanning:** a grid simulator with travel time and energy, battery depletion as
  a failure, event detection, and a replanning path that derives the remaining work from the
  original task definitions, locks the running task to its robot, and refuses replans that drop
  work.
- **Stack:** Python 3.13, FastAPI with server-sent events for the live trace, OR-Tools, pydantic;
  Next.js 16, TypeScript and Tailwind for the UI (Gantt, replay canvas, before/after, Monte Carlo).
  Fully offline demo mode on recorded Nemotron answers so reviewers can run it without a key.

## How this differs from "an LLM that writes a schedule"

The model never emits a schedule. It emits scope: tasks, quantities, precedence, robot types.
Hours and watt-hours come from a rate model, the assignment and timing come from the solver, and
only a proven schedule reaches the robots. The novelty is not the verifier (verify-and-repair for
robot task allocation exists in the literature); it is the loop: minimal infeasibility cores with
numeric hints as repair instructions, execution-time replanning with locked in-progress work and
a scope guard, and Monte Carlo robustness with replanning inside the loop.

## Results

Every number below is a live run on Nebius Token Factory: 22 test briefs of five designed kinds
x 3 repeats x two model arms planning from the same requirements, 66 runs per arm, every saved
plan in the repository (`docs/eval/`).

- **Final pipeline (run 4).** Nemotron 3 Ultra ended with a CP-SAT-verified plan in 86 % of runs
  (Wilson 95 % CI 76-93 %) and with the full work in scope in 82 % (71-89 %). 55 % verified on
  the first graph, so the repair loop, fed the solver's minimal core, added 31 points at 0.85
  repair rounds per run. Nemotron 3 Super: 82 % verified, 77 % in scope, 45 % first try, at about
  a fifth of the cost ($0.02 against $0.10 per plan, mean; Ultra's median $0.05). Median wall
  time per plan with Ultra: 58 s. 326 requests, 0 errors.
- **Honest failure.** On the three designed-impossible briefs, 7 of 9 Ultra runs ended without a
  plan, 5 of them with CP-SAT-proven advice ("finishing needs 85 h, 25 h past the 60 h
  deadline"); the two that verified had cut 10-40 % of the work and are counted out of scope.
- **Measure, find, fix.** Each run exposed something the checks could not see yet: run 1 plans
  that passed by shrinking the work (now a volume conflict), run 2 hauls scheduled before their
  material was dug (17 of 56 verified Ultra plans; now a supply-order conflict, 0 of 57 in run
  4), run 3 slips of the structuring model (a smaller pad the brief allows read as the full one:
  that brief went from 0 of 3 to 3 of 3 in scope). Scored with the final checks, Ultra's
  in-scope rate went 70 % -> 77 % -> 82 % over runs 2-4.
- **Nemotron-written schedules.** Given the 21 verified task graphs of run 3, Nemotron 3 Ultra
  wrote schedules that all passed the same independent checker (0 violations in 21). CP-SAT with
  exact travel matched or beat every one and proved it optimal: shorter on 5, by up to 36 h.
- **Replanning.** 102 robot failures injected into run 4's verified plans: CP-SAT alone recovered
  82 (80 %, CI 72-87 %) in a median 9 ms; the other 20 provably exceed the surviving batteries,
  the case that goes back to Nemotron.
- [MEASURE] Core-vs-verdict ablation: repairs fed the minimal core vs only the verdict.
- [MEASURE] Monte Carlo on the landing-pad sortie (re-recorded on the final code): P(on time)
  and the effect of one spare per single-robot type.

## Challenges

- Making solver feedback useful to a language model: a raw infeasibility proof is not a repair
  instruction. We named every constraint group, extracted a minimal core, and wrote one-sentence
  hints with the actual numbers.
- Keeping the model honest: the live models sized energies to fit whatever battery they were told
  about, so every first-try plan "fit". Grounding durations and energies in quantities and a rate
  model made infeasibility real again and turned repairs into visible scope decisions.
- Single-worker core extraction times out on dense graphs that the multi-worker solve refutes
  instantly; a multi-worker deletion-filter fallback with a shared deadline fixed it.
- Replanning accounting: remaining work must come from the original task definitions, the running
  task must stay locked, and a replan that silently drops a task has to be rejected.
- Reasoning shares `max_tokens` with the answer: at 16,384 tokens, 78 of 190 Ultra reasoning calls
  were cut off before answering; at 32,768 with one re-ask at 65,536, 4 or 5 of about 125 per
  run. Strict `json_schema` decoding held on all three models, and 1,494 requests over four runs
  saw no error and no rate limit. Ultra produced 200-300 output tokens per second against
  Super's 150, so the larger model was also the faster one for us.

## What we learned

- The repair loop turned a refused first graph into a verified plan in 31 of every 100 runs of
  the final pipeline.
- Nemotron sequences a verified graph validly; its failures are in the decomposition (45 % of
  Ultra's first graphs in run 4 were refused: energy, supply order, horizon, scope), which is
  exactly what the validator and the core can name.
- Scoring a planner by "a schedule was found" overstates it: until volume, supply and the
  structuring itself were checked against the brief, plans passed by shrinking or reordering the
  work. Saving every plan made every new check re-scorable for free.

## Limitations and known failures

- Travel is exact for the planning model (straight legs between zone centres in whole hours, in
  the order CP-SAT picks), not terrain. The simulator is 2D and kinematic.
- Durations and energies come from an illustrative rate model calibrated to the briefs' battery
  units, not from a specific robot's datasheet; quantities are the model's estimates from the
  structure dimensions and are shown with their basis.
- No recharging, no night cycle, one depot, no material-flow coupling between hauling and
  placement beyond precedence.
- The repair loop can re-scope work; scope changes are flagged in the UI, and the coverage check
  refuses graphs that drop a structure's phase, but a shallower bowl is still a planner decision.
- Failure cases in run 4: `large_fleet_pad_berm_road` (eleven robots, sintering within 17 % of
  two printer batteries) failed 2 of 3 Ultra runs on the printer budget, one of them ending on a
  repair that referenced a task it had deleted; a repair can also answer with an unchanged graph
  (Super `landing_pad` #2, the same 4,782 Wh verdict three times); and the structuring model can
  drop an operation the brief asks for, which only the coverage check against its own
  requirements catches.

## Who would use this

Site-preparation planning for lunar surface construction programs (NASA MMPACT's pad and
infrastructure work, ICON's Project Olympus, Lunar Outpost, KICT's lunar construction research)
and, on Earth, remote and hazardous earthworks where a crew must execute a plan with little
supervision: mine sites, disaster recovery, offshore. [Add one sentence from a practitioner who
tried the UI.]

## Track fit

Moonwright is an embodied-agent runtime for a robot crew: it senses simulator telemetry (positions,
batteries, failures), decides (verified rescheduling, escalation to the reasoning model when the
fleet cannot finish) and acts (new per-robot task lists). The verified schedule is the task list a
ROS 2 executor or an Isaac/GR00T stack would consume; the 2D simulator is enough to show the
planning depth, and the path to a physics simulator is the first item in What's next.

## What's next

- Isaac Sim / ROS 2 execution of the per-robot task lists; terrain-aware travel; recharging and
  night cycles; multiple depots.
- Rate tables from real robot datasheets and a material-flow model (haul cycles tied to placement).
- Monte Carlo as a batch job and a hosted backend on Nebius.

## Built with

Nebius Token Factory, NVIDIA Nemotron 3.5 Lightning, Nemotron 3 Ultra, Nemotron 3 Super,
Google OR-Tools CP-SAT, Python, FastAPI, pydantic, Next.js, TypeScript, Tailwind CSS.

## Feedback on Nebius Token Factory and NVIDIA Nemotron (required section)

See `docs/feedback-draft.md`; paste the measured version here.

## Links

- Video (<= 3 min): [TODO]
- Repository (public, MIT): [TODO]
- Demo URL (sample mode works without a backend): [TODO]
