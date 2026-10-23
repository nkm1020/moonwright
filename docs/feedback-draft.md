# Feedback for Nebius Token Factory and NVIDIA Nemotron (draft)

Written for the "Most Valuable Feedback" section of the submission. Keep only what we
actually experienced; the first block is what we found while reading the documentation and
catalogue before the first live call, the second block is to be filled from live runs.

## From the documentation and catalogue (observed Oct 22 2026)

- The model catalogue marks which models support "JSON mode", and none of the Nemotron
  entries carry the tag, while the API accepts `response_format` with `json_schema` and
  `json_object`. A short note per model on what structured-output options are honoured (and
  whether the grammar is applied after the reasoning block) would save a round of trial and
  error; we built a per-model fallback ladder to cope.
- Nemotron 3 Super and Ultra are pinned to one region and the cookbook recommends the
  regional base URL for Ultra. (In practice the global endpoint served both for us, see
  below; the documentation could say which to use.)
- Thinking is on by default and shares `max_tokens` with the answer; a truncated answer shows
  up as an empty `content`. Documenting the recommended `max_tokens` with thinking on, and
  exposing a per-request switch consistently across the Nemotron family, would avoid silent
  empty answers.
- Several Nemotron ids were retired on Aug 31 2026 (Llama-3.1-Nemotron-Ultra-253B,
  Nemotron-3-Nano-Omni). A machine-readable deprecation field in `models_info` would let
  clients fail early with a clear message.
- The hackathon promo credits are easy to find, but the list of supported billing countries
  is not published; participants outside the main regions cannot tell in advance whether
  they can open an account.

## From live runs (measured Oct 22-24 2026)

Four evaluation runs: 1,494 requests to Nemotron 3.5 Lightning, Nemotron 3 Super and Nemotron 3
Ultra over about seven and a half hours, three to five concurrent clients, all on the global endpoint. Call
logs: `docs/eval/2026-10-23-live-eval-run1-calls.md` and `calls.md` in each of
`docs/eval/2026-10-23-live-eval-run2/`, `-run3/` and `docs/eval/2026-10-24-live-eval-run4/`.

- **Reliability was excellent.** Zero non-200 responses and zero 429s in 1,494 requests. Super and
  Ultra answered on the global base URL; the regional fallback we built for Ultra (the cookbook
  recommends the us-central1 URL) never fired. `served_as` always echoed the requested id.
- **Strict `json_schema` works on all three models.** Every answer that was not cut off parsed and
  matched the schema. One quirk: under strict decoding Lightning occasionally wrote a key with
  Python-style quoting inside it (`"operations': [": "excavate"`), valid JSON with the wrong shape.
  A note on whether the grammar constrains key names or only JSON syntax would help.
- **The reasoning budget is the main thing to document.** Reasoning and answer share `max_tokens`.
  On our task-graph and repair prompts (3-6k tokens in), Ultra reasoned for 12-19k tokens on
  average; at `max_tokens` 16,384, 78 of 190 Ultra reasoning calls (and 36 of 139 Super calls)
  were cut (`finish_reason: length`) with an empty or partial answer, and re-asking at the same
  budget was usually cut again. At 32,768, with one re-ask at 65,536, it fell to 4 or 5 of about 125 per run (runs 2-4). A
  separate reasoning budget (or `reasoning_effort` levels the Nemotron chat template honours), and
  typical reasoning lengths per model in the catalogue, would save every team this lesson.
- **Ultra was faster than Super for us.** Same task-graph prompts, same day: Ultra produced
  263-295 output tokens per second against Super's 134-157 (198-305 against 150-156 in runs 3
  and 4), and its median task-graph answer
  came back sooner (33-46 s against 55-78 s). Worth stating in the catalogue; we had assumed the opposite.
- **Model behaviour on a constrained planning task.** Asked to plan work that could not fit its
  deadline, Super satisfied the solver by quietly shrinking the work (it dug about half of a
  landing pad's volume, 3 runs of 3), while Ultra kept the work, failed honestly and took the
  solver's advice ("finishing needs 85 h"). Both models often left a haul without an edge to the
  dig that feeds it, and repairs under deadline pressure sometimes deleted that edge (our later
  supply check rejects 17 of 56 verified Ultra plans and 14 of 51 Super plans from that run). Neither is a
  complaint: it is why we pair the models with a verifier, and model cards that mention the
  tendency would help other builders.
- **Cost.** A full Ultra plan cost $0.05-0.07 median, $0.09-0.10 mean (runs 2-4); a Lightning structuring call about
  $0.0003. The price spread across the family made per-role routing the obvious design. A usage
  field with the request's cost would let clients stop pricing tokens themselves.
