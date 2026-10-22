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
  regional base URL for Ultra. It would help if the global endpoint documented this, or if
  the error on a wrong region named the right URL.
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

## From live runs (fill in)

- Structured-output behaviour per model (did `json_schema` work, how often did validation
  fail, what did the repair rounds fix):
- Latency and throughput of Ultra with reasoning for 5-6k token prompts:
- Rate limits hit (429 frequency, Retry-After values):
- Anything the Nemotron models did particularly well or badly on the planning task:
