# Demo video pipeline

Renders the Moonwright demo video from `storyboard.json`: five HTML slides, the site
(`design/landing-1`: the overview and the recorded runs, served as files) and one scene of the
working app (`frontend/`, with the backend in fixture mode), driven by Playwright in
Chromium and recorded with CDP `Page.startScreencast`, narrated (OpenAI TTS or macOS `say`),
captioned and assembled with ffmpeg. Every figure the video quotes comes from `facts.json`, which
is checked before anything is recorded: against the recorded runs in `frontend/public/samples/`,
against the live evaluation and the Isaac Sim run in `docs/eval/`. Figures the screen shows are
also waited for on screen during the capture, so the recording and the recorded runs must agree.

## What the video shows, and what it does not claim

The site replays the three runs recorded live on Nebius Token Factory on Oct 24 on the final
pipeline (`frontend/public/samples`: the landing pad, the impossible deadline, the tight deadline),
scene by scene; nothing is planned or solved while recording. The landing pad's refusal, its two
repairs, the verified schedule, the replan after EX1's failure and the 200-run Monte Carlo figures
(as planned and with three spares) are that recorded run's results, and the narration says the
run was recorded live. The crest slider computes PR1's energy check in the page from the run's
own numbers. The failure scene plays the Isaac Sim mission (`tools/isaac`: PhysX rovers on NASA
LOLA terrain of Shackleton's rim, the replan from their telemetry), whose figures come from
`docs/eval/2026-10-25-isaac-closed-loop`; the cold open plays the same clip at the fault. The
live scene is the one place something is computed while recording: the app plans the landing pad
from its recorded Token Factory answers (the backend in fixture mode replays them; the validator
and CP-SAT run for real), a failure the site does not show (hauler HL2 at hour 24) is injected
on camera, and CP-SAT replans it during the capture; its figures are checked against
`runs/landing_pad.hl2-at-24.sim.json`, the CLI's run of the same failure, and on screen. The
evidence slide reports run 4 of the live evaluation
(docs/eval/2026-10-24-live-eval-run4, the Nemotron 3 Ultra arm, `eval.rescored.csv`) and the
core-vs-verdict ablation.

## One command

```bash
tools/video/render.sh
```

What it does, in order:

1. Mirror-builds the frontend into `out/app` (rsync of `frontend/` without `node_modules` and
   `.next`, a symlink to `frontend/node_modules`; `frontend/` is never written), unless
   `SKIP_BUILD=1` and a build is there.
2. Serves `design/landing-1` on :3074 (`python3 -m http.server`), starts the backend in fixture
   mode on :8074 (it refuses to record unless `/health` reports `"llm":"fixture"`, so no model is
   called) and the app on :3076. All three ports must be free.
3. Synthesizes the narration, then records every scene in real time (`caffeinate` keeps the Mac
   awake; a scene during which the machine slept is rejected).
4. Stops the servers (also on Ctrl-C or any error), then assembles everything with ffmpeg.

Everything it prints also goes to `out/logs/render.log`. A full render takes a few minutes on an
M-series Mac (synthesized sentences are cached in `out/work/audio/cache`). Needs ffmpeg (no libass
needed: captions are rendered by Chromium), Node 22, pnpm, uv, python3, rsync, macOS `say` (the
fallback voice), `frontend/node_modules` (`pnpm install` in `frontend/` once), network access for
the site's Google Fonts, and
Playwright's Chromium build 1234 in `~/Library/Caches/ms-playwright`, which matches the pinned
`playwright@1.62.1` (if it is missing: `cd tools/video && pnpm exec playwright install
chromium-headless-shell`). The slides use the site's type (Imbue, Albert Sans) and its pictures
(`slides/site` links to `design/landing-1/assets`).

| Variable | Default | Effect |
| --- | --- | --- |
| `SKIP_BUILD` | `0` | `1` reuses the last mirror build in `out/app` |
| `TTS` | `openai` when `OPENAI_API_KEY` is set, else `say` | narration engine. Put `OPENAI_API_KEY=...` in `tools/video/.env` or `tools/video/voice/.env` (both gitignored; loaded by the render's node process only and never logged). Sentences are still synthesized one by one, so the timing and captions work the same; synthesized sentences are cached in `out/work/audio/cache` like `say`'s |
| `OPENAI_TTS_MODEL` | `gpt-4o-mini-tts` | OpenAI speech model (`tts-1`, `tts-1-hd` ignore the instructions) |
| `OPENAI_TTS_VOICE` | `ash` | OpenAI voice |
| `OPENAI_TTS_SPEED` | `1.4` | OpenAI speaking speed (0.25-4); at 1.0 the voice reads about 125 words a minute, `say` 180, and the instructions do not change the pace |
| `OPENAI_TTS_INSTRUCTIONS` | a calm documentary narrator | how the voice should speak (gpt-4o-mini-tts) |
| `VOICE` | `Samantha` | macOS voice (`say -v '?'` lists them). A downloaded Premium voice sounds much better: System Settings, Accessibility, Spoken Content, System voice, Manage Voices, then e.g. `VOICE="Ava (Premium)"` |
| `RATE` | `180` | speaking rate, words per minute |
| `ASSEMBLE_ONLY` | `0` | `1` re-runs captions + ffmpeg on the last capture, no servers (caption edits, layout) |
| `KEEP_FRAMES` | `0` | `1` keeps the raw screencast PNGs (2-3 GB) in `out/work/frames` |
| `FACTS_CHECK` | `strict` | `warn` renders even if `facts.json` disagrees with its sources |

## Outputs (`out/`)

| File | What |
| --- | --- |
| `moonwright-demo.mp4` | 1920x1080, 30 fps CFR, H.264 High (CRF 18, yuv420p, BT.709 primaries, transfer and matrix tagged, faststart), AAC 48 kHz stereo, voice at -16 LUFS, captions burned in (not on the title, problem and close slides, whose text is the narration) |
| `moonwright-demo-clean.mp4` | the same with the voice but no burned captions, for platforms that take `captions.srt` (YouTube) |
| `moonwright-demo-silent.mp4` | the captioned picture with a silent AAC track (for a recorded voice-over or music) |
| `captions.srt` | sidecar captions for the whole narration, slides included: at most 2 lines x 42 characters, 1.2-6 s per cue, typographic apostrophes |
| `thumbnail.png` | 1920x1080 poster (the title slide), since frame 0 is the fade from black |
| `contact-sheet.png` | one still per scene, labelled with scene id, start time and length |
| `readme-hero.gif` | 12 s from the refusal into the repairs (no captions), 1280 px wide, 15 fps, two-pass palette, under 8 MB |
| `timing.txt`, `timing.json` | per-scene start, duration, narration and action seconds, output checks, loudness, the motion check |

Working files live in `out/work` (narration WAVs and their timing, per-scene clips, caption
images) and `out/logs` (render, build, site, backend and app logs).

## How it is recorded

- The app keeps its 1536x864 CSS-px recording layout but is rasterised at 1920x1080: Chromium
  runs with `--force-device-scale-factor=1.25` and a 1536x864 window. (Playwright's
  `deviceScaleFactor` emulation would not do: the screencast is capped at the view's DIP size,
  so it delivers 1536x864 frames.) Frames are lossless PNGs with their compositor timestamps;
  the capture checks that every one is 1920x1080. Each scene becomes a constant-30-fps clip
  through ffmpeg's concat demuxer (one `duration` per frame) and the `fps` filter. Each entry
  carries `option framerate 1000`: without it every image gets the image2 demuxer's 1/25 s time
  base, frame times are rounded to 40 ms, and about one frame in six of every scroll and glide
  is a repeat. The assembly measures repeated frames inside motion (`motion` in `timing.json`)
  and flags a scene above 4 %.
- A white arrow cursor with a dark outline is drawn over the page (`src/cursor.js`) and follows
  real mouse events; clicks pulse. The pointer moves on eased, slightly bowed paths of 500-800 ms
  and every scroll is eased (`window.scrollTo` per animation frame); nothing jumps. A figure the
  voice names gets an amber ring (`highlight`) and the arrow rests beside it, never on it.
  Text targets are measured by their glyphs (a DOM Range), not by their element's box.
- Punch-ins (`zoomIn` / `zoomOut`) pinch the visual viewport with CDP
  `Input.synthesizePinchGesture`: Chromium re-rasterises at the new scale, so the figure is
  sharp, and the layout, scroll position and pointer stay put. The pointer and the page do not
  move while zoomed (the capture refuses it); a scene that ends zoomed is zoomed back out off
  camera before the next one.
- Each narration sentence is synthesized separately (`say -v $VOICE -r $RATE` to AIFF, then
  48 kHz WAV), so every sentence start is measured, not guessed. Inside a sentence, the pauses
  the voice makes after commas and colons are measured too and each clause is snapped to them;
  words inside a clause are spread by syllables. A scene lasts `max(voice delay + narration +
  tail, the actions' own time)`. Actions can wait for a `cue` (a phrase of the narration), so the
  cursor reaches what is being named as it is said; `pause` actions stretch to fill the time
  before the next cue, the narration is never cut, and a page that is done early is simply held.
- Joins: a dip through the site's ground (#111) between the slides and the site, hard cuts
  between the site's scenes (one continuous session on run.html), plus a fade in and out. The narration is limited to -3 dBFS and normalised with two-pass `loudnorm` (linear) to
  -16 LUFS.
- Captions: Albert Sans on a nearly opaque #111 box, bottom centre inside the 90 % safe area (a
  scene can move them to the bottom left or right where the page is empty). Each cue is a
  transparent PNG rendered by Chromium and overlaid by ffmpeg.
- The total must stay within 2:55; `timing.txt` reports an overrun instead of dropping scenes.

## Editing the storyboard

`storyboard.json` holds `defaults`, the `pronounce` map (text-to-speech only; captions keep the
written form), the `gif` window and the `scenes`. The pronounce map respells `Nemotron` as
`Neemo-tron` and `Nebius` as `Nebbee-us`: compared by MFCC distance, Samantha's "Nemotron" is
sample-identical to "Nemmatron" (NEM-uh-tron) and her "Nebius" is closest to NEE-bee-us; listen
once with any new voice. A scene has:

- `id`, `kind` (`slide` or `ui`) and `narration`.
- Optional: `caption` (shown instead of the narration, timed sentence by sentence, e.g. "4,095 Wh"
  where the voice says the number in words), `voDelay` (seconds before the voice starts),
  `pauseBefore` (`{"<sentence index>": seconds}` of extra silence before a sentence),
  `minDuration`, `transition` (`{"type": "dip" | "fade", "duration": 0.5}`, default a cut),
  `captionAlign` (`left`, `center`, `right`), `burnCaptions: false` (SRT only, for a slide whose
  text is the narration), `still` (contact-sheet time in seconds).
- Slides: `slide` (a file in `slides/`); their actions are usually `reveal` (adds class `in`).
- UI scenes: `setup` actions run before recording starts; `actions` are recorded.

| Action | Fields |
| --- | --- |
| `goto` | `url`, relative to the app |
| `waitForText`, `waitForSelector` | `text` (+ `exact`) or `selector`; the render fails if the screen does not show it, which keeps the narration honest. `by`: a narration phrase the text must be on screen before (the render fails if the screen is late) |
| `clickText`, `clickRole`, `click` | `text`, `role` + `name`, or `selector`; optional `within`, `nth`, `at: [fx, fy]` (point inside the element), `offset: [dx, dy]` (CSS px), `rect: "text" \| "box"` |
| `hover` | same targeting as the clicks |
| `select` | a `<select>` target and its `value` (the native popup is not in the screencast, so the pointer pulses on the control and the value is set) |
| `highlight`, `unhighlight` | an amber ring that follows the target (`pad`: px, or `[x, top, bottom]`) / removes it |
| `zoomIn`, `zoomOut` | punch-in on a target: `scale` (1.5), `at` (point of the target), `to` (where it lands, fractions of the frame), `speed` / back to 1x |
| `scrollTo` | a target or `y`; `align` (`top`, `center`, `bottom`), `offset`, `duration` in ms |
| `scrollBy` | `dy`, `duration` |
| `typeText` | a target to click first, `value`, `delay` ms per key |
| `pause` | `ms`; stretches to the next cue unless `"elastic": false` |
| `moveTo`, `cursor` | `x`, `y` in CSS px (eased move / instant placement) |
| `reveal`, `press`, `waitForScroll`, `hideCursor`, `showCursor` | slide reveals, key presses, waiting for the app's own smooth scroll |

Any action may carry `cue` (a narration phrase, `{"sentence": n}` or seconds) and `lead` (how
many seconds before the cue it starts). Targets use the app's visible text, roles and
aria-labels, never generated class names. Scene text changes need a new capture (the narration
is part of it); caption-only changes need just `ASSEMBLE_ONLY=1`.

## Editing the facts

`facts.json` maps names to values. `{{name}}` in the storyboard and in `slides/*.html` is
replaced by the value; `{{name|words}}` and `{{name|Words}}` spell a number out for the voice
("three", "Two hundred"). `_sources` records where each value comes from, and the render stops
when a value disagrees with it:

| Source | Example |
| --- | --- |
| a recorded sample | `samples/landing_pad.mc.json within_horizon_rate \|pct` |
| any JSON file | `<repo path>.json <json.path> \|formatters` |
| a file count | `count backend/examples/eval/*.md` |
| CSV rows | `csv docs/eval/2026-10-24-live-eval-run4/eval.rescored.csv condition=ultra/loop_on \|share:success=True \|pct` (also `\|len`, `\|distinct:col`, and `\|gain:success=True;first_try_feasible=True`: the points one share is ahead of another, each rounded to a whole percent first) |
| a figure inside a sentence | `samples/landing_pad.plan.json steps.2.failure.details.0 \|re:at least ([\d,]+) Wh`: the first match of a pattern (its group, or the list of its groups); `\|num` reads "4,009" as 4009 and `\|diff` subtracts the second of two figures from the first (how far a demand is over its battery) |
| two figures of one file, or of two | `samples/landing_pad.sim.json replans.0 \|pick:after.schedule.makespan_h,before.makespan_h \|diff` (the replan's delay); `samples/landing_pad.whatif.mc.json within_horizon_rate \|minus:samples/landing_pad.mc.json within_horizon_rate \|pts` (the what-if's gain in points, as the panel prints it) |
| a Monte Carlo count | `samples/landing_pad.mc.json \|misses` (runs that missed the horizon), `\|within` (runs on time, the site's "74 of 200") and `\|nobackup` (the misses that lost a robot with no backup) |
| the screen | `screen`: a figure only the page shows, which no recorded file holds (none at present); the gate requires a `waitForText` in the storyboard that quotes it, so the render stops if the screen shows anything else |

`TBD` marks a value that is not known yet: `repo_url` is TBD until the public repository exists,
and the close slide leaves the repository line out until it is set.

## A human voice-over

Drop `voice/<scene-id>.wav` (any sample rate or channel count; `.aiff`, `.m4a` and `.mp3` work
too) and it replaces `say` for that scene: the scene is timed to its length, captions are spread
over its speech by syllables, cues land proportionally. Keep the scene's `narration` equal to what
is spoken, since it is also the caption text. Then run `render.sh` again. For one continuous
recording instead, use `moonwright-demo-silent.mp4` and `captions.srt` as the timing guide.

## Files

- `render.sh`: the one command; `src/render.mjs capture | assemble` are its two stages.
- `src/narrate.mjs` TTS and timing, `src/capture.mjs` browser, screencast and actions,
  `src/cursor.js` the injected cursor, ring and scroller, `src/captions.mjs` cues, SRT and caption
  images, `src/assemble.mjs` ffmpeg and the motion check, `src/extras.mjs` contact sheet,
  thumbnail, GIF and timing table, `src/prepare-app.mjs` the mirror's Turbopack root patch,
  `src/lib/` shared helpers (facts gate in `story.mjs`).
- `slides/`: title, problem, evidence (architecture and evaluation) and close, plus the caption
  page and the shared stylesheet. Open a slide with `?preview` in a browser to play it.
