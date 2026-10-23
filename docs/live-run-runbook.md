# Live run runbook (Nebius Token Factory)

Everything below needs a Token Factory account with credits and `NEBIUS_API_KEY` in
`backend/.env` (`load_env` reads it; pass `--llm nebius` explicitly so a run never falls back to
the fixtures by accident). Run every command from `backend/`. Every call is appended to
`out/llm_log.jsonl` (`MOONWRIGHT_LLM_LOG`: status, model, tokens, latency, request id, never the
prompt or the answer), so note the start time first; the call-log report takes `--since`:

```bash
cd backend
date -u +%Y-%m-%dT%H:%M:%S+00:00 | tee out/eval_start.txt
```

## Measured so far (list prices, from `scripts/llm_log_report.py`)

* Phase 3a, Oct 22, 3 plans: Ultra task graph 2.9k in / 10.4k out tokens, 297 tok/s, p50 34 s,
  $0.034 per call; Lightning structure 2.9 s, $0.0002; Super explanation 2.5 s, $0.0011.
* Phase 3c smoke, Oct 22-23, 9 plans (landing_pad x2, tight_battery, depleted_excavators x2,
  three_pads_in_order, small_fleet_long_berm, low_battery_pad, front_loaded_excavation): 34
  requests, 0 errors, 2 re-asks (Ultra answered `finish_reason=length` once on a task graph and
  once on a repair; the client re-asked and both succeeded). Ultra task graph with thinking: 3.4k
  in / 10.6k out tokens, p50 53 s / p95 132 s, $0.035 per call; Ultra repair: 5.2k in / 9.3k out,
  p50 31 s / p95 71 s, $0.033 per call; Lightning structure 3.3 s p50, $0.0003; Super explanation
  2.2 s p50, $0.0013. Total $0.60 for the nine runs; 4 of 9 runs needed a repair round (2 x
  `scope:<structure>` from the brief's operations, 1 x `energy:PR1`, 1 x three rounds ending in
  `horizon, zone:Z1` with the advice `finishing needs 86 h, 26 h past the 60 h deadline`).
* First live evaluation, Oct 23 (22 briefs x Ultra / Super x 3, 132 runs, $9.9): verified 85 %
  and 91 % of the runs, but in scope only 45 % and 38 % (the models shrank the work to satisfy
  the solver), and Ultra with reasoning hit `max_tokens=16384` on 78 of 190 calls. Phase 3e
  answers both: an under-scoped whole plan is now a `volume:<structure>` conflict the repair
  must fix (the README, "Volume"; "in scope" now needs 95 % of every volume and compaction /
  sintering area, so a trimmed kind-D plan no longer counts), and reasoning calls start at
  32,768 tokens with a cut ask repeated at 65,536 and only a cut at 65,536 asked again without
  reasoning, the ladder continuing into the planner's re-ask (`.env.example`, "Token
  budgets"). Expect more output tokens per Ultra call (the budget doubled) and fewer re-asks;
  the call log records each ask's `max_tokens` and `thinking`.

## 1. Account and key (smoke)

```bash
cd backend
cp .env.example .env            # put the key in NEBIUS_API_KEY=...
# an .env made before phase 3e pins MOONWRIGHT_THINKING_MAX_TOKENS=16384, the budget that was cut
# on 78 of 190 Ultra calls: delete that line (empty = the 32,768 default) or raise it
grep -n MOONWRIGHT_THINKING_MAX_TOKENS .env
uv run python - <<'PY'
import os, openai
from dotenv import load_dotenv; load_dotenv()
c = openai.OpenAI(api_key=os.environ["NEBIUS_API_KEY"], base_url="https://api.tokenfactory.nebius.com/v1/")
ids = [m.id for m in c.models.list().data]
for want in ["nvidia/Nemotron-3_5-Lightning", "nvidia/Nemotron-3-Ultra-550b-a55b", "nvidia/nemotron-3-super-120b-a12b"]:
    print(("OK  " if want in ids else "MISSING ") + want)
r = c.chat.completions.create(model="nvidia/Nemotron-3_5-Lightning", max_tokens=64,
    messages=[{"role": "user", "content": "Reply with the single word pong."}],
    extra_body={"chat_template_kwargs": {"enable_thinking": False}})
print(r.choices[0].message.content, r.usage)
PY
```

If a model id is missing, check https://tokenfactory.nebius.com/api/public/models_info and set
the `MOONWRIGHT_MODEL_*` variables. If the Ultra request answers 404, the client retries on
`https://api.tokenfactory.us-central1.nebius.com/v1/` automatically.

## 2. Demo plans and the fixture refresh

The demo briefs' fixtures are the live runs of Oct 24 (the final pipeline): `landing_pad`
(`energy:printer` twice, two repairs, 72 h; 1.9 min of model time, 26k output tokens, $0.09) and
`front_loaded` (no plan after three repairs, advice 85 h; 5.0 min, 70k tokens, $0.23). Record a
new take under its own id, so it never overwrites the fixtures the demo and the tests replay:

```bash
uv run moonwright plan examples/briefs/landing_pad.md --llm record --brief-id landing_pad_next \
  --out out/demo/landing_pad_next.plan.json                               # writes fixtures/landing_pad_next/
uv run moonwright plan examples/briefs/front_loaded.md --llm record --brief-id front_loaded_next \
  --out out/demo/front_loaded_next.plan.json
```

To adopt a take, move its directory to `fixtures/<brief>/` (the replay keys on the brief's file
stem), check that `uv run moonwright plan examples/briefs/<brief>.md --llm fixture` gives the
take's verdicts per step, repairs and schedule, and update the tests that pin the old take's
story (`tests/test_pipeline_e2e.py: test_<brief>_replays_its_live_run`, and the demo flows in
`tests/test_integration.py` and `tests/test_cli_api.py`, which quote its steps, makespans and
landing_pad's replan at 10 h) until `uv run pytest` passes. Never re-record `landing_pad_v1`
(brief: `tests/data/landing_pad_v1.md`), `access_road` or `tight_battery`: those are the
hand-made fixtures the arithmetic tests are written against. Then regenerate
`frontend/public/samples` from the take itself: copy the take's own PlanResult
(`out/demo/<take>.plan.json`, never a `--llm fixture` replay, whose steps name `fixture:`
models), then `simulate --fail EX1@10 --llm fixture`, `montecarlo --runs 200 --seed 1` and the
what-if (the plan with one spare per single-point type, same seed and runs) on that file
([reference.md](reference.md#samples-frontendpublicsamples) has the commands).
`test_ui_samples_are_the_live_runs_their_fixtures_replay` fails on a sample that is a replay or
that its fixtures no longer replay. Fast demo
setting when Ultra with thinking is slow: `MOONWRIGHT_THINKING=off` (lower first-try
feasibility, which the repair loop should absorb).

## 3. Main evaluation: 22 briefs x 2 model arms x loop off/on x 3 repeats

```bash
uv run moonwright eval --briefs examples/eval --repeat 3 --loop both --models ultra,super \
  --thinking auto --llm nebius --workers 3 --max-answer-retries 2 --save-plans out/eval_live_plans \
  --out out/eval_live.json --csv out/eval_live.csv --markdown out/eval_live.md 2>&1 | tee out/eval_live.log
# interrupted (Ctrl-C, a 429 storm, a dead laptop)? the same command plus --resume:
uv run moonwright eval --briefs examples/eval --repeat 3 --loop both --models ultra,super \
  --thinking auto --llm nebius --workers 3 --max-answer-retries 2 --save-plans out/eval_live_plans \
  --out out/eval_live.json --csv out/eval_live.csv --markdown out/eval_live.md --resume 2>&1 | tee -a out/eval_live.log
```

264 planning runs (22 briefs x `ultra/loop_off`, `ultra/loop_on`, `super/loop_off`,
`super/loop_on` x 3). Arms re-route only the task-graph and repair roles (structure stays
Lightning, explanations are off in the eval); `--thinking auto` means Ultra / Super reason on
those two roles and Lightning answers directly. Each brief is structured once per repeat and all
four conditions plan from that same requirements object (`requirements_sha` in the rows), so the
arms and the loop settings are a paired comparison and only 66 Lightning calls are made. Rows
flush to the CSV as they finish and the stderr progress line shows each run's cost (and `not in
scope: volume 85 %` when a verified plan covers less than 95 % of a structure's volume or area);
the eval's first lines print the effective token budgets (`tokens: reasoning 32,768 tokens (x2
when cut, up to 65,536), direct 4,096`): stop and fix `backend/.env` if they say 16,384. The
markdown has the
per-condition table (feasible rate with Wilson CI, feasible *in scope*, first try, repairs,
makespan, p50 / p95 latency, tokens, cost per run and in total), the per-kind table
(`examples/eval/kinds.json` is picked up automatically) and the per-brief matrix (`ok 76 h /
0 rep`, `ok 90 h / 1 rep scope!`, `fail energy`, `k/n` on repeats). The `models` column lists
the model ids that actually answered, so an arm's routing is auditable per row.

Cost at list prices with the measured per-call figures: Ultra arm 132 runs x ($0.035 task graph +
$0.0003 structure) = $4.7, plus about $0.033 per repair round on the 66 `loop_on` runs ($2.2 per
mean repair round r); Super arm 132 x about $0.011 = $1.5 plus $0.011 x 66 x r. About $9-12 for
r around 1, a hard upper bound of about $22 if every `loop_on` run spends all 3 repairs; schema
re-asks add a few percent. Since phase 3e a reasoning ask that used to be cut at 16,384 tokens
runs on to 32,768 (about three times a typical call's output), so expect the Ultra arm to cost
somewhat more per call and to need fewer re-asks; a volume conflict can also add repair rounds
on briefs whose first graph shrank the work. Wall time: Ultra with thinking p50 53 s / p95 132 s per task graph, so
a `loop_off` run takes about 60 s and a `loop_on` run about 60 s + 35-70 s per repair; Super is
roughly half. About 80 minutes of work on 3 workers; plan 2 hours with 429 backoff (the SDK
retries 429 / 5xx with exponential backoff and honours `Retry-After`).

`--resume` keeps the rows an earlier run left in `--csv` (the last row per brief x condition x
repeat wins; a CSV with an older header is upgraded, and a last row cut off mid-append is dropped
so its job runs again), skips every brief x repeat job it already
holds completely, runs only the missing conditions of a partial job (structured anew, so its new
rows carry their own `requirements_sha`), appends under one header and builds `--out` and
`--markdown` from all rows (the markdown says `resumed: N rows from a previous run`). Rows of a
job that was still running when the run died were never flushed, so they run again. Without
`--resume` the CSV is overwritten. `--save-plans DIR` writes every run's PlanResult the moment
it exists as `DIR/<brief>__<arm>__<loop>__<repeat>.plan.json` (exactly what `POST /plan`
returns; `plan_path` in the rows, relative to the CSV's directory, so a resume into another DIR
keeps every row's file reachable) and each job's structuring as
`DIR/<brief>__<repeat>.structure.json` (`.2.structure.json` for the second structuring of a job a
resume completed); the per-condition table gains a `rejected answers` column
(runs with a task-graph / repair answer cut off or off the schema, `k/n`) and the median cost.

Ablation (optional, same shape, $4-6): `--loop on --feedback verdict` tells the repair round only
that the plan was infeasible, which isolates what the minimal infeasibility core is worth.

## 4. Replanning evaluation (needs live plan files first)

The eval's `--save-plans` directory already holds a live plan per brief x condition x repeat
(`replan_eval.py` skips the unverified ones), so no extra planning is needed after section 3:

```bash
mkdir -p out/live
for b in landing_pad tight_battery access_road low_battery_pad habitat_berm_tight_horizon; do
  cp out/eval_live_plans/${b}__ultra__loop_on__1.plan.json out/live/$b.plan.json
done
# or, without an eval run, plan them afresh (5 x about $0.04, about 5 min):
for b in landing_pad tight_battery access_road low_battery_pad habitat_berm_tight_horizon; do
  uv run moonwright plan examples/eval/$b.md --llm nebius --no-explain -q --out out/live/$b.plan.json
done
uv run python scripts/replan_eval.py out/live/*.plan.json --llm nebius --all-tasks --sole-of-type solver \
  --max-escalations 10 --out out/replan_eval_live.json --markdown out/replan_eval_live.md 2>&1 | tee out/replan_eval_live.log
```

For every verified plan the script fails every robot with work at 10 % and 50 % of each of its
tasks, runs `run_with_replanning` solver-first and classifies each injection: `solver_only`,
`escalated_verified`, `escalated_failed` (kind and conflicts kept), `escalated_no_answer`,
`not_asked_budget`, `not_asked_sole_type`, `solver_failed`, `no_replan`, `error`, with solve
time, LLM latency, tokens, cost, makespan before / after, tasks remaining, completion within the
horizon and the verdict's conflict families; it stops asking the model after
`--max-escalations` and says so. The headline rates (solver-only, escalated, recovered, within
horizon) are over the *askable* injections: the not-asked ones (a lost sole-of-type robot under
`--sole-of-type solver`, or the budget already spent) have their own column, so `solver-only +
escalated + solver_failed = askable` and the 14 of 28 sole-of-type injections of the offline
landing_pad run no longer read as replanner failures. `--all-tasks` weighs a robot by its task
count; the first-task default is the headline setting. Cost: at most 10 escalations x about $0.04 (an Ultra repair with
the remaining graph and core) = about $0.40; time: the simulations take seconds, each escalation
40-90 s, so 10-15 minutes. `--sole-of-type solver` keeps the escalation budget for energy /
horizon cores; add a second run with `--sole-of-type ask --max-escalations 4 --robots SV1,CP1` on
one plan (about $0.15) if a "Nemotron re-types the work of a lost sole robot" example is wanted.
To record the demo escalation fixture instead:
`uv run python scripts/replan_eval.py out/live/landing_pad.plan.json --llm record --robots EX2 --fractions 0.1`
writes `fixtures/landing_pad/taskgraph_replan_2.json` (one injection per plan in record mode).

## 5. Call-log report (free, seconds), after 3 and 4

```bash
uv run python scripts/llm_log_report.py out/llm_log.jsonl --since "$(cat out/eval_start.txt)" \
  --markdown out/llm_log_report.md --out out/llm_log_report.json
```

Per model and role: calls, HTTP 200 count, errors by status, re-asks, `json_format` rung,
thinking, tokens in / out, completion tok/s, latency p50 / p95, cost, finish reasons, plus the
notable list (non-200, re-asks, requests slower than `--slow 60` seconds) with request ids. The
markdown is written for the README / Devpost results and the Token Factory feedback section.

## 6. Secondary measurements

```bash
uv run python scripts/llm_schedule_check.py out/landing_pad.plan.json out/access_road.plan.json \
  out/tight_battery.plan.json --repeat 3 --out out/direct_schedule_check.json   # Nemotron writes the schedule itself
uv run moonwright simulate out/landing_pad.plan.json --fail EX2@30 --llm nebius
uv run moonwright montecarlo out/landing_pad.plan.json --runs 200 --seed 1
```

`llm_schedule_check.py` prints one line per answer (clean / n violations / llm error) and a
summary with the violation rate and the conflict kinds. Record: replans that produced a verified
plan / replans attempted, time per replan, and the within-horizon probability per example brief.
