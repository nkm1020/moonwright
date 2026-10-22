# Devpost submission draft (English; fill the [MEASURE] slots only with measured numbers)

**Project name:** Moonwright (working title; decide before submission)
**Track:** Physical AI (simulation-only entry)
**Tagline:** Verified multi-robot construction planning for the lunar surface: Nemotron proposes, CP-SAT checks, the agent repairs and replans.

## Inspiration

Lunar construction robots cannot be teleoperated step by step: the Earth-Moon round trip is
several seconds and communication windows are limited. A crew of excavators, haulers,
compactors and a regolith printer has to execute a plan autonomously, and a plan that violates
a battery budget or puts two robots in the same blast zone is not a small mistake. Large
language models are good at turning a construction brief into a work breakdown, and bad at
guaranteeing that the breakdown is executable. Moonwright pairs the two: the model plans, a
constraint solver proves the plan, and the model repairs what the solver rejects.

## What it does

1. Takes a natural-language brief ("build a 20 m landing pad and a 60 m blast berm with two
   excavators, two haulers, a compactor and a printer within 14 days").
2. Nemotron structures it into requirements (site, structures, robots with battery budgets,
   horizon) that the planner can correct in the UI.
3. Nemotron proposes a task graph (duration, energy, zone, precedence, robot type per task).
4. A deterministic validator and an OR-Tools CP-SAT model verify and schedule it: precedence,
   one task per robot at a time, one robot per safety zone, per-robot battery budgets, the
   deadline, makespan minimised.
5. When the solver proves the graph infeasible, the minimal conflicting constraint set (for
   example `energy:EX1`, `precedence:T4->T2`) goes back to Nemotron with a plain-language hint
   and the graph is repaired, up to three rounds. Only solver-verified plans reach the user,
   as a Gantt chart with a Nemotron-written explanation.
6. The plan runs in a 2D simulator. When a robot fails, the agent replans the remaining work
   from the task-graph stage, re-verifies, and shows the schedule before and after.
7. A Monte Carlo panel runs the plan N times with random failures and duration jitter and
   reports the probability of finishing within the deadline.

## How we built it

- **Models (Nebius Token Factory, OpenAI-compatible API):** `nvidia/Nemotron-3_5-Lightning`
  for the short structuring call, `nvidia/Nemotron-3-Ultra-550b-a55b` with reasoning for task
  graph generation and repair, `nvidia/nemotron-3-super-120b-a12b` for the plan explanation.
  Structured output uses `response_format` (JSON schema, falling back to JSON mode) and every
  answer is validated with pydantic; thinking is switched off per request for the calls that
  do not need it.
- **Verification:** Python validator for graph-level defects (cycles, unknown references,
  per-type capacity, critical path), then CP-SAT with optional intervals per (task, robot),
  `AddNoOverlap` per robot and per zone, linear energy budgets and a makespan objective.
  Infeasibility explanations use assumption literals per constraint group,
  `SufficientAssumptionsForInfeasibility`, and a deletion filter to make the core minimal.
- **Simulation and replanning:** a grid simulator with travel, battery drain and event
  injection; replanning passes finished/remaining/in-progress tasks and the failed robot back
  to the same pipeline with the running task locked.
- **Stack:** Python 3.13, FastAPI, OR-Tools, pydantic; Next.js 16 + TypeScript + Tailwind for
  the UI (Gantt, 2D replay, before/after comparison, Monte Carlo histogram).

## Challenges

- Making solver feedback useful to a language model: a raw infeasibility proof is not a
  repair instruction. We named every constraint group, extracted a minimal core, and wrote
  one-sentence hints with the actual numbers ("EX1 would need 6200 Wh but has 5000 Wh").
- Single-worker core extraction can time out on dense graphs that the multi-worker solve
  refutes instantly; we added a multi-worker deletion-filter fallback with a shared deadline.
- Replanning after a failure has subtle accounting: remaining work must be derived from the
  original task definitions, the in-progress task must stay locked to its robot, and a replan
  that silently drops a task must be rejected.
- [Add live-model observations here after the first real runs: JSON-mode behaviour, thinking
  length, latency of Ultra, rate limits.]

## What we learned

- [MEASURE] With the repair loop off, X % of first-try task graphs were solver-feasible; with
  the loop on, Y % reached a verified plan in a mean of Z repair rounds (20 briefs x 3 repeats).
- [MEASURE] Nemotron-written schedules checked by the same verifier violated at least one
  constraint in W % of cases; this is why the solver is in the loop.
- [MEASURE] Replanning after an injected robot failure produced a verified plan in V % of
  runs in a median of U seconds.

## What's next

- Travel time and travel energy inside the CP-SAT model (today the simulator pays them).
- Recharging and shift structure, multiple depots, and a real construction cost model.
- Deploy the backend on Nebius (Serverless Endpoint) and run Monte Carlo as a batch job.
- Tavily-backed landing-site briefing to enrich the requirements with site facts.

## Built with

Nebius Token Factory, NVIDIA Nemotron 3.5 Lightning, Nemotron 3 Ultra, Nemotron 3 Super,
Google OR-Tools CP-SAT, Python, FastAPI, pydantic, Next.js, TypeScript, Tailwind CSS.

## Feedback on Nebius Token Factory and NVIDIA Nemotron (required section)

See `docs/feedback-draft.md`; paste the final version here.

## Links

- Demo URL: [TODO]
- Video (<= 3 min, >= 1 min of the pipeline, simulator and replanning in action): [TODO]
- Repository (public, MIT): [TODO]
