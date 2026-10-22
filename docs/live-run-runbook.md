# Live run runbook (Nebius Token Factory)

Everything below needs a Token Factory account with credits and `NEBIUS_API_KEY`. Nothing in
the repo has been run against the live service yet; the fixture mode is what the tests and the
offline demo use. Credits: promo code `NEBIUS-DEVPOST-GLOBAL26` (USD 25) at
https://nebius.com/promo-code plus the Builders Program (USD 25). Billing needs a bank card;
whether Korea-issued cards are accepted is unconfirmed, so do step 1 first.

## 1. Account and key (day 1 risk check)

```bash
cd backend
cp .env.example .env            # put the key in NEBIUS_API_KEY=...
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

## 2. First live plans and fixture refresh

```bash
uv run moonwright plan examples/briefs/landing_pad.md --llm record     # live + saves fixtures/landing_pad/
uv run moonwright plan examples/briefs/tight_battery.md --llm record
uv run moonwright plan examples/briefs/access_road.md --llm record
uv run pytest                                                          # fixtures changed: make sure the suite agrees
```

`--llm record` overwrites `fixtures/<brief>/*.json`; the explanation texts in the e2e tests
quote the makespans 80/64/68 h (transit allowance included), so if the live graphs differ, update
`tests/test_pipeline_e2e.py` or keep the old fixtures (copy them aside first).

Fast demo setting when Ultra with thinking is slow: `MOONWRIGHT_THINKING=off` (every call
answers directly; expect lower first-try feasibility, which the repair loop should absorb).

## 3. The loop on/off experiment (PRD evaluation plan)

```bash
uv run moonwright eval --briefs examples/eval --repeat 3 --loop both --llm nebius \
  --out out/eval_live.json --csv out/eval_live.csv
```

20 briefs x 2 conditions x 3 repeats = 120 planning runs. Rough cost with the default routing:
each run is one Lightning call plus one to four Ultra calls of about 3-6k input tokens and
1-3k output tokens (more with thinking), so budget roughly USD 5-15 for the whole table and
check the usage columns of the CSV as it runs. Report feasible-plan rate, repairs, makespan,
latency and tokens per condition (the CLI prints the summary table; the CSV has every run).

Secondary measurement (why a solver is needed): let Nemotron write the schedule itself and
verify it with the same checker:

```bash
uv run python scripts/llm_schedule_check.py out/landing_pad.plan.json out/access_road.plan.json \
  out/tight_battery.plan.json --repeat 3 --out out/direct_schedule_check.json
```

It prints one line per answer (clean / n violations / llm error) and a summary with the
violation rate and the conflict kinds.

## 4. Replanning and robustness numbers

```bash
uv run moonwright simulate out/landing_pad.plan.json --fail EX2@30 --llm nebius
uv run moonwright montecarlo out/landing_pad.plan.json --runs 200 --seed 1
```

Record: replans that produced a verified plan / replans attempted, time per replan, and the
within-horizon probability per example brief.
