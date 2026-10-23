# Moonwright

**Language models propose. Solvers prove.**

Moonwright is a planning agent for crews of autonomous construction robots on the Moon. A mission
planner writes a brief in plain English. NVIDIA Nemotron models on **Nebius Token Factory** turn it
into requirements and a task graph with quantities. A deterministic validator and **OR-Tools CP-SAT**
then check the plan against the physics that matters: precedence, one task per robot, one robot per
safety zone, battery budgets including travel, material that exists before it is hauled, and the
deadline. When the solver says no, it returns a minimal infeasibility core with the numbers behind
it, and Nemotron repairs the plan from that proof. When a brief is impossible, Moonwright does not
shrink the work to pass: it says so, and the solver finds what would make it feasible
(`finishing needs 85 h, 25 h past the 60 h deadline`). A 2D simulator then breaks robots mid-mission,
CP-SAT replans the remaining work in milliseconds, and Monte Carlo puts a number on robustness.

<!-- TODO(video): hero GIF docs/img/hero.gif + links: demo video, live demo -->

## Why

A robot crew on the Moon works 1.3 light-seconds from Earth: nobody teleoperates an excavator
through that, and nobody on Earth can catch a bad plan in time. The plan has to be right before
execution and repaired autonomously when reality diverges. Language models write plans that look
right and quietly break what matters: a battery that runs dry halfway through a dig, two robots in
one blast zone, a haul scheduled before the material is dug, a deadline no schedule can meet.
Solvers catch all of that, but someone has to write the model. Moonwright puts each where it is
strong: Nemotron reads the brief, decomposes the work and repairs it; CP-SAT proves it.

The same loop fits autonomous earthmoving on Earth, where a fleet of electric machines shares
batteries, exclusion zones and a hard handover date.

## How it works

```mermaid
flowchart LR
  B["Brief<br/>plain English"] --> S
  subgraph TF["Nebius Token Factory · NVIDIA Nemotron"]
    S["Structure<br/>Nemotron 3.5 Lightning"]
    G["Task graph and repair<br/>Nemotron 3 Ultra, reasoning"]
    E["Explanation<br/>Nemotron 3 Super"]
  end
  subgraph VER["Deterministic verification"]
    R["Rate table<br/>quantity to hours and Wh"]
    C["Validator<br/>coverage, volume, supply, energy"]
    X["OR-Tools CP-SAT<br/>precedence, robots, zones,<br/>batteries, horizon"]
  end
  S -- requirements --> G
  G -- "tasks with quantities" --> R --> C --> X
  C -. "conflicts + hint" .-> G
  X -. "infeasibility core" .-> G
  X -- "verified schedule" --> E
  X -- "verified schedule" --> SIM["2D simulator<br/>failures, replans, Monte Carlo"]
  SIM -. "robot failure" .-> X
  X -. "no plan in 3 repairs" .-> A["CP-SAT-proven advice<br/>deadline, battery, spare robot"]
```

1. **Structure.** Nemotron 3.5 Lightning turns the brief into requirements: site and safety zones,
   structures with their dimensions and the operations the brief asks for, robots with batteries,
   the deadline. The requirements card is editable; the planner only ever sees the card.
2. **Task graph.** Nemotron 3 Ultra (reasoning on) proposes tasks with a robot type, zone,
   precedence and a quantity (`126 m3 = pi * (10 m)^2 * 0.4 m`). A rate table turns quantities into
   hours and Wh, so a plan cannot be made feasible by shrinking numbers.
3. **Validate.** A deterministic validator checks references, cycles, robot capability, coverage of
   every operation the brief asks for, planned volume against the structure's dimensions (a 20 m pad
   dug as a 12 m one is a conflict), that every haul waits for the digs that supply its material,
   and battery budgets with travel.
4. **Schedule.** CP-SAT minimises the makespan with one interval per task and robot, no-overlap per
   robot and per safety zone, battery budgets per robot and the deadline. If it is infeasible, every
   constraint group sits behind an assumption literal, so the solver returns a minimal core:
   `energy:EX1`, `precedence:T4->T2`, `horizon`, with a hint in numbers.
5. **Repair.** The core, the hint and the previous graph go back to Nemotron. Up to three rounds;
   every accepted repair carries a diff (added, removed, renumbered and changed tasks, scope drift
   in %), so a re-scope is visible instead of silent.
6. **Advise.** If no repair verifies, CP-SAT tries single changes on the last graph and returns only
   the ones it can prove: a later deadline, a bigger battery, one more robot.
7. **Simulate and replan.** The verified schedule runs in a 2D simulator that moves robots, drains
   batteries and injects failures. CP-SAT reschedules the remaining work first (milliseconds);
   Nemotron is asked only when the remaining fleet provably cannot finish. Monte Carlo replays the
   plan 200 times with random failures and reports the probability of finishing on time, with a
   95 % interval, and the robots the plan cannot afford to lose.

Details, edge cases and the reasoning behind each rule: [docs/design.md](docs/design.md).

## Nemotron on Nebius Token Factory

| Role | Model | Why this model |
| --- | --- | --- |
| Structure the brief | `nvidia/Nemotron-3_5-Lightning`, reasoning off | short, frequent, schema-bound; 3 s median, a fraction of a cent |
| Task graph and repair | `nvidia/Nemotron-3-Ultra-550b-a55b`, reasoning on | measured: more first-try plans, faster per token than Super, and honest on impossible briefs |
| Explanation | `nvidia/nemotron-3-super-120b-a12b` | readable summaries of a verified plan at low cost |

Every call goes through the OpenAI-compatible Token Factory API with `response_format: json_schema`
(strict) and `chat_template_kwargs.enable_thinking` per role. Reasoning and answer share
`max_tokens`, so reasoning calls start at 32,768 tokens and a cut answer is asked again at 65,536
before falling back to a direct answer. Every request is logged (status, tokens, latency, request
id, never prompt text) and summarised per model and role. Routing is one environment variable per
role (`backend/.env.example`).

## Evidence

Live runs on Token Factory, 22 test briefs of five designed kinds (comfortable, battery-split,
deadline-bound, impossible, re-scope allowed; the arithmetic of each is in
[docs/eval-briefs.md](docs/eval-briefs.md)), three repeats, both model arms on the same
requirements. Every run's plan is saved in the repository, and each run was scored again with the
final checks, so the columns are judged alike.

| Nemotron 3 Ultra, 66 live runs each | Run 2 | Run 3 | Run 4 (final pipeline) |
| --- | --- | --- | --- |
| Ended with a CP-SAT-verified plan | 85 % | 89 % | 86 % (76-93 %) |
| ... with the full work in scope | 68 % | 77 % | 80 % (69-88 %) |
| ... and no haul before its material is dug | 48 % | 74 % | 80 % |
| Verified on the first graph (no repair) | 62 % | 50 % | 55 % |
| Reasoning answers cut at the token limit | 4 of 121 | 5 of 125 | 5 of 127 |
| Cost per plan (median / mean) | $0.05 / $0.09 | $0.07 / $0.10 | $0.05 / $0.10 |

* **The repair loop is worth 31 points**: in run 4, 55 % of runs verify on the first graph and 86 %
  after at most three repairs fed the solver's core.
* **Impossible briefs fail honestly.** On the three designed-impossible briefs, 7 of 9 Ultra runs
  ended without a plan rather than shrink the work, 5 of them with CP-SAT-proven advice; the other
  two verified only by cutting 10-40 % of the work, which the scope row counts as out of scope.
* **Each run found what the checks could not see yet.** Run 1's plans passed by shrinking the work
  (a 20 m pad dug as a 12 m one), so volume became a hard conflict. In run 2, 17 of 56 verified
  Ultra plans hauled material before it was dug, because CP-SAT schedules only the edges it is
  given; the supply-order check made that a conflict, and none of run 4's 57 verified plans does
  it. Run 3 exposed the structuring model's slips (a smaller pad the brief allows read as the full
  one, a distance recorded as a diameter, operations the fleet cannot perform, a compaction the
  brief asks for left out); the structure stage now corrects them from the brief, and the re-scope
  brief went from 0 of 3 to 3 of 3 in scope.
* **Optimal means optimal.** Given a verified task graph, Nemotron 3 Ultra writes a valid schedule
  itself (21 of 21 passed the same independent checker), but CP-SAT with exact travel matches or
  beats every one and proves it: shorter on 5, by up to 36 h. When no schedule exists, only the
  solver can say why.
* **Robot failures are absorbed by the solver.** On run 4's plans, CP-SAT alone replanned 82 of 102
  injected failures (80 %) in a median 9 ms; the other 20 are provably beyond the surviving
  batteries, which is when Nemotron is asked to re-scope.

Runs: [run 1](docs/eval/2026-10-23-live-eval-run1.md) (before the volume check),
[run 2](docs/eval/2026-10-23-live-eval-run2/README.md), [run 3](docs/eval/2026-10-23-live-eval-run3/README.md),
[run 4](docs/eval/2026-10-24-live-eval-run4/README.md), each with its saved plans. Method:
[docs/design.md#evaluation](docs/design.md#evaluation).

## Run it

Offline, no API key: the demo briefs replay live Nemotron runs recorded on Token Factory.

```bash
cd backend && uv sync
uv run moonwright plan examples/briefs/landing_pad.md --llm fixture       # two repairs, verified at 72 h
uv run moonwright plan examples/briefs/front_loaded.md --llm fixture      # no plan; CP-SAT-proven advice: 85 h
uv run moonwright simulate out/landing_pad.plan.json --fail EX1@10        # the digging robot fails at hour 10
uv run moonwright montecarlo out/landing_pad.plan.json --runs 200 --seed 1
uv run moonwright serve                                                   # API on :8000
cd ../frontend && pnpm install && pnpm dev                                # UI on :3000
```

Live: put `NEBIUS_API_KEY` in `backend/.env` (copy `backend/.env.example`), then
`uv run moonwright plan examples/briefs/landing_pad.md --llm nebius`. The UI's top bar replays
recorded runs without a backend. Commands, endpoints and settings: [docs/reference.md](docs/reference.md).
`uv run pytest` runs the backend suite (no network).

## Limitations

* **Verified means verified against the planning model**: precedence, robots, zones, battery budgets
  with the legs each robot drives, supply order and the deadline. Travel is exact for that model
  (straight lines between zone centres in whole hours, the order chosen by CP-SAT), not terrain;
  the simulator is 2D and kinematic; failures are injected, not modelled.
* **The rates are assumptions.** The rate table (`backend/moonwright/pipeline/rates.py`) uses
  illustrative low-power figures; replace them with measured ones before trusting an hour.
* **Checks only judge what the brief states.** A structure recorded without dimensions has no volume
  to judge, and a repair may still re-scope work the brief leaves open (a berm crest's width); the
  repair diff shows it.
* **Repairs can fail.** A model can answer with an unchanged graph or a re-scoped one; the evaluation
  counts these cases. Full list: [docs/design.md](docs/design.md#limitations-and-known-failures-full-list).

## Repository

```
backend/    Python 3.13 + uv: pipeline (structure, task graph, rates, validator, CP-SAT, repair loop,
            advice), simulator, replanning, Monte Carlo, CLI, FastAPI, Token Factory client, tests
frontend/   Next.js 16 UI: brief, editable requirements, streamed trace, Gantt, 2D replay, Monte Carlo
docs/       design notes, reference, evaluation runs with saved plans, eval brief arithmetic
```

## Built for

The [Nebius × NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/), Physical AI
track (simulation-based). Runtime calls go to Nebius Token Factory with NVIDIA Nemotron open models
(`backend/moonwright/llm/tokenfactory.py`).

## License

MIT, see [LICENSE](LICENSE).
