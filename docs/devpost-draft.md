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
   battery budgets including a transit allowance, the deadline, makespan minimised and proven.
4. **Repair with reasons.** When the model's graph is infeasible, CP-SAT returns a minimal set of
   conflicting constraints (for example `energy:EX1`, `precedence:T4->T2`) with the numbers behind
   them ("the excavator tasks need 9,900 Wh and cannot be split across two 5,000 Wh batteries").
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
  (task, robot), `AddNoOverlap` per robot (with a transit allowance) and per zone, linear energy
  budgets and a makespan objective. Infeasibility explanations use assumption literals per named
  constraint group, `SufficientAssumptionsForInfeasibility`, a deletion filter to make the core
  minimal, and a multi-worker fallback for instances the single-worker proof cannot finish.
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

[MEASURE] 20 briefs x 3 repeats, repair loop off vs on: feasible-plan rate, repairs to
feasibility, makespan, latency, tokens, cost per verified plan (Wilson 95% CI).
[MEASURE] Core-vs-verdict ablation: repairs fed the minimal core vs only the verdict.
[MEASURE] Nemotron-written schedules checked by the same verifier: violation rate and kinds.
[MEASURE] Replanning after injected failures: solver-only success rate, escalation rate, time.
[MEASURE] Monte Carlo on the landing-pad sortie: P(on time) and the effect of one spare per
single-robot type.

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
- [Add live-model observations from the eval: structured-output behaviour, thinking length,
  latency of Ultra, rate limits.]

## What we learned

[MEASURE] Fill from the Results table: how often the repair loop turned an infeasible first
graph into a verified plan, what the minimal core was worth over a bare verdict, how often
Nemotron-written schedules violated a constraint, and how robust the landing-pad sortie is.

## Limitations and known failures

- Travel is a per-task transit allowance (an upper bound over the site), not terrain; the
  simulator pays the actual distance. The simulator is 2D and kinematic.
- Durations and energies come from an illustrative rate model calibrated to the briefs' battery
  units, not from a specific robot's datasheet; quantities are the model's estimates from the
  structure dimensions and are shown with their basis.
- No recharging, no night cycle, one depot, no material-flow coupling between hauling and
  placement beyond precedence.
- The repair loop can re-scope work; scope changes are flagged in the UI, and the coverage check
  refuses graphs that drop a structure's phase, but a shallower bowl is still a planner decision.
- [List the eval's failure cases here: briefs where no verified plan was found in three rounds,
  and why.]

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
