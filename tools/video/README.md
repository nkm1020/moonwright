# Demo video pipeline

Renders the Moonwright demo video from `storyboard.json`: four HTML slides and the real app,
driven by Playwright in Chromium and recorded with CDP `Page.startScreencast`, narrated with
macOS `say`, captioned and assembled with ffmpeg. Every figure the video quotes comes from
`facts.json`, which is checked before anything is recorded: against the recorded runs in
`frontend/public/samples/`, against the live evaluation in `docs/eval/`, or, for a figure only the
app computes during the recording, against the screen itself. Figures the screen shows are also
waited for on screen during the capture, so the recording and the recorded run must agree.

## What the video shows, and what it does not claim

The recording runs against the backend in fixture mode, so no model is called while recording.
The fixtures are the landing pad's live run, recorded on Nebius Token Factory on Oct 24 on the
final pipeline (`backend/fixtures/landing_pad`, the same take as
`frontend/public/samples/landing_pad.plan.json`): in fixture mode the backend replays its
answers for the structure, task-graph, repair and explanation steps, step for step, while the
validator, CP-SAT, the simulator and Monte Carlo run for real. The narration says so: the brief
scene says the demo replays a run recorded live on Token Factory, and the trace labels every
replayed answer `recorded answer`. The plan scene's refusal, its two repairs and the verified
schedule are that run's Nemotron 3 Ultra answers, checked again by the validator and CP-SAT
during the capture. The Impossible deadline sample is the other recorded live run, loaded as
recorded, and the narration credits it as such. The evidence slide reports run 4 of the live
evaluation, the first on the final pipeline (docs/eval/2026-10-24-live-eval-run4, the Nemotron 3
Ultra arm, `eval.rescored.csv`: scored with the final checks). The failure injection, the replan
and both Monte Carlo runs (the baseline and the spare-robot what-if) are computed live during the
capture; the spares join the fleet in its own order, the fleet the recorded
`landing_pad.whatif.mc.json` was computed with, so the panel shows the README's figure.

## One command

```bash
cd frontend && pnpm install && cd ..   # once: the mirror build links frontend/node_modules
tools/video/render.sh
```

What it does, in order:

1. Mirror-builds the frontend into `out/app` (rsync of `frontend/` without `node_modules` and
   `.next`, a symlink to `frontend/node_modules`, `NEXT_PUBLIC_API_URL=http://127.0.0.1:8074`).
   `frontend/.next` and `frontend/` are never written. `next` runs through `node` directly, not
   `pnpm exec` (pnpm 11 would offer to purge the symlinked `node_modules`).
2. Starts the backend in fixture mode on :8074 (`MOONWRIGHT_LLM=fixture MOONWRIGHT_LLM_LOG=off
   uv run moonwright serve`) and the app on :3074. It refuses to record unless `/health` reports
   `"llm":"fixture"`, so no model is ever called. Both ports must be free.
3. Synthesizes the narration, then records every scene in real time (`caffeinate` keeps the Mac
   awake; a scene during which the machine slept is rejected).
4. Stops both servers (also on Ctrl-C or any error), then assembles everything with ffmpeg.

Everything it prints also goes to `out/logs/render.log`. A full render takes about 5 minutes on
an M-series Mac (synthesized sentences are cached in `out/work/audio/cache`). Needs macOS `say`,
ffmpeg (no libass needed: captions are rendered by Chromium), Node 22, pnpm, uv, and Playwright's
Chromium build 1234 in `~/Library/Caches/ms-playwright`, which matches the pinned
`playwright@1.62.1` (if it is missing: `cd tools/video && pnpm exec playwright install
chromium-headless-shell`).

| Variable | Default | Effect |
| --- | --- | --- |
| `VOICE` | `Samantha` | macOS voice (`say -v '?'` lists them). A downloaded Premium voice sounds much better: System Settings, Accessibility, Spoken Content, System voice, Manage Voices, then e.g. `VOICE="Ava (Premium)"` |
| `RATE` | `180` | speaking rate, words per minute |
| `SKIP_BUILD` | `0` | `1` reuses the last mirror build in `out/app` |
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
| `readme-hero.gif` | 14 s of the plan scene's recorded live repairs (the refusal and both trims, no captions), 1280 px wide, 15 fps, two-pass palette, under 8 MB |
| `timing.txt`, `timing.json` | per-scene start, duration, narration and action seconds, output checks, loudness, the motion check |

Working files live in `out/work` (narration WAVs and their timing, per-scene clips, caption
images) and `out/logs` (render, build, backend and app logs).

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
- Joins: a dip through the console background (#0b0f14) between the slides and the app, hard
  cuts between the app scenes (one continuous session), one short dissolve that skips the seconds
  the spare-robot what-if takes to compute (the panel keeps the real time), plus a fade in and
  out. The narration is limited to -3 dBFS and normalised with two-pass `loudnorm` (linear) to
  -16 LUFS.
- Captions: Geist on a nearly opaque graphite box, bottom centre inside the 90 % safe area (a
  scene can move them to the bottom left or right where the app is empty). Each cue is a
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
| a Monte Carlo count | `samples/landing_pad.mc.json \|misses` (runs that missed the horizon) and `\|nobackup` (the misses that lost a robot with no backup), counted as the panel counts them |
| the screen | `screen`: a figure the app computes during the capture that no recorded file holds (the Monte Carlo MTBF, a panel setting); the gate requires a `waitForText` in the storyboard that quotes it, so the render stops if the screen shows anything else |

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
