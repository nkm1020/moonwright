#!/usr/bin/env node
// Entry point. render.sh runs `capture` while the fixture backend and the app are up, stops the
// servers, then runs `assemble` (ffmpeg only). Either phase can be re-run on its own:
//   node src/render.mjs capture    narration + browser capture + caption images (needs servers)
//   node src/render.mjs assemble   clips, final MP4s, SRT, contact sheet, GIF, timing table
import fsp from "node:fs/promises";
import { API_HEALTH, APP_URL, CLIPS_DIR, FPS, FRAMES_DIR, OUT_DIR, OUTPUTS, STATE_FILE, WORK_DIR } from "./lib/paths.mjs";
import path from "node:path";
import { checkFacts, loadStoryboard } from "./lib/story.mjs";
import { ensureDir, fmtClock, log, readJson, writeJson } from "./lib/util.mjs";
import { norm, tokenize } from "./lib/text.mjs";
import { buildTimeline } from "./lib/timeline.mjs";
import { narrate } from "./narrate.mjs";
import { captureScenes } from "./capture.mjs";
import { renderCaptionImages, timelineCues, writeSrt } from "./captions.mjs";
import { assemble, integratedLoudness, motionHoles, verifyOutput } from "./assemble.mjs";
import { contactSheet, heroGif, thumbnail, timingTable } from "./extras.mjs";

// tools/video/.env (gitignored) may hold OPENAI_API_KEY for the narration; it is loaded into this
// process only, never into the fixture backend, and its values are never logged
const VIDEO_DIR = path.join(path.dirname(new URL(import.meta.url).pathname), "..");
for (const dotenv of [path.join(VIDEO_DIR, ".env"), path.join(VIDEO_DIR, "voice", ".env")]) {
  try {
    process.loadEnvFile(dotenv);
  } catch {
    // no such file: macOS say narrates unless the other one sets a key
  }
}

const env = {
  tts: process.env.TTS || (process.env.OPENAI_API_KEY ? "openai" : "say"),
  openai: {
    model: process.env.OPENAI_TTS_MODEL || "gpt-4o-mini-tts",
    voice: process.env.OPENAI_TTS_VOICE || "ash",
    instructions:
      process.env.OPENAI_TTS_INSTRUCTIONS ||
      "A calm, confident documentary narrator explaining an engineering demo. Clear diction, an even, " +
        "unhurried pace, warm but precise; read numbers and technical terms plainly, no dramatic emphasis.",
  },
  voice: process.env.VOICE || "Samantha",
  rate: Number(process.env.RATE || 180),
  frameFormat: process.env.FRAME_FORMAT === "jpeg" ? "jpeg" : "png",
  factsCheck: process.env.FACTS_CHECK || "strict",
};

function factsGate(story) {
  const { problems, checked, screen, unsourced } = checkFacts({ values: story.facts, sources: story.sources, raw: story.raw });
  if (problems.length) {
    const msg = `facts.json disagrees with its sources:\n  - ${problems.join("\n  - ")}`;
    if (env.factsCheck === "warn") log(`WARNING ${msg}`);
    else throw new Error(`${msg}\n(set FACTS_CHECK=warn to render anyway)`);
  }
  const tbd = Object.entries(story.facts).filter(([, v]) => v === "TBD").map(([k]) => k);
  log(
    `facts: ${checked.length - screen.length} checked against the samples and the evaluation, ${screen.length} checked on screen during the capture` +
      (screen.length ? ` [${screen.join("; ")}]` : "") +
      (tbd.length ? `; TBD: ${tbd.join(", ")}` : "") +
      (unsourced.length ? `; no source: ${unsourced.join(", ")}` : ""),
  );
}

async function capturePhase() {
  await ensureDir(WORK_DIR);
  const story = loadStoryboard();
  factsGate(story);
  const health = await fetch(API_HEALTH).then((r) => r.json());
  if (health.llm !== "fixture") throw new Error(`backend reports llm=${health.llm}; the video is recorded against the fixture backend only`);
  log(`narration: ${env.tts === "openai" ? `OpenAI ${env.openai.model}, voice ${env.openai.voice}` : `macOS say, voice ${env.voice}, rate ${env.rate}`}`);
  const timings = await narrate(story.scenes, {
    engine: env.tts,
    openai: env.openai,
    voice: env.voice,
    rate: env.rate,
    sentenceGap: story.defaults.sentenceGap ?? 0.12,
  });
  const { results, browser } = await captureScenes(story.scenes, timings, { facts: story.facts, format: env.frameFormat });
  await browser.close();
  const durations = Object.fromEntries(Object.values(results).map((r) => [r.id, r.duration]));
  await writeJson(STATE_FILE, {
    app: APP_URL,
    voice: env.voice,
    rate: env.rate,
    sceneIds: story.scenes.map((s) => s.id),
    timings,
    captured: results,
    timeline: buildTimeline(story.scenes, durations, FPS),
  });
  log(`capture done -> ${STATE_FILE}`);
}

function gifStart(spec, story, timings) {
  if (typeof spec.from === "number") return spec.from;
  const scene = story.scenes.find((s) => s.id === spec.scene);
  const words = timings[spec.scene].sentences.flatMap((s) => s.words);
  const want = tokenize(String(spec.from)).map(norm);
  for (let i = 0; i + want.length <= words.length; i++) {
    if (want.every((t, k) => norm(words[i + k].w) === t)) return Math.max(0, scene.voDelay + words[i].start + (spec.offset ?? 0));
  }
  throw new Error(`gif.from "${spec.from}" is not in the ${spec.scene} narration`);
}

async function assemblePhase() {
  const story = loadStoryboard();
  const state = await readJson(STATE_FILE);
  const ids = story.scenes.map((s) => s.id).join(",");
  if (ids !== state.sceneIds.join(",")) throw new Error(`storyboard scenes changed since the capture (${state.sceneIds.join(",")}); run the capture again`);
  const { timings, captured, timeline } = state;
  for (const s of story.scenes) {
    const said = (timings[s.id]?.sentences ?? []).map((x) => x.text).join(" ");
    if (said !== s.narration) throw new Error(`scene ${s.id}: the narration changed since the capture; run the capture again`);
  }
  // captions are built here, not at capture time: caption text, overrides and layout can change
  // without recording again (the narration and the scene lengths come from the capture)
  const cues = timelineCues(story.scenes, timings, timeline);
  log(`rendering ${cues.length} caption images`);
  const captionImages = await renderCaptionImages(cues, story.facts);
  await assemble({ scenes: story.scenes, timings, captured, timeline, cues, captionImages, defaults: story.defaults });
  await writeSrt(cues, OUTPUTS.srt);

  log("contact sheet");
  await contactSheet(story.scenes, timeline, OUTPUTS.main);
  const poster = await thumbnail(story.scenes, timeline, OUTPUTS.clean, OUTPUTS.thumbnail);
  const gifSpec = story.raw.gif;
  let gif = null;
  if (gifSpec) {
    log("hero gif");
    gif = await heroGif(gifSpec, gifStart(gifSpec, story, timings));
  }

  // judder check: a frame repeated inside motion is a hole (the old 1/25 s concat time base made
  // one in six); a few can be genuine (the compositor skipped a beat), many mean a regression
  log("motion check (repeated frames inside scrolls and pointer glides)");
  const motion = {};
  for (const slot of timeline.scenes) motion[slot.id] = await motionHoles(path.join(CLIPS_DIR, `${slot.id}.mp4`));
  const moving = Object.values(motion).reduce((a, m) => a + m.moving, 0);
  const holes = Object.values(motion).reduce((a, m) => a + m.holes, 0);
  const worst = Object.entries(motion).filter(([, m]) => m.moving >= 30 && m.holes / m.moving > 0.04);

  const table = timingTable(story.scenes, captured, timeline);
  const main = await verifyOutput(OUTPUTS.main);
  const clean = await verifyOutput(OUTPUTS.clean);
  const silent = await verifyOutput(OUTPUTS.silent);
  if (worst.length) main.problems.push(`repeated frames inside motion: ${worst.map(([id, m]) => `${id} ${m.holes}/${m.moving}`).join(", ")}`);
  const loud = await integratedLoudness(OUTPUTS.main);
  const report = {
    total_s: +timeline.total.toFixed(3),
    total: fmtClock(timeline.total),
    over_limit_s: table.over > 0 ? +table.over.toFixed(2) : 0,
    voice: `${state.voice} @ ${state.rate}`,
    loudness_lufs: loud.I,
    true_peak_dbfs: loud.TP,
    captions: cues.length,
    captions_burned: cues.filter((c) => c.burn !== false).length,
    outputs: { main, clean, silent },
    thumbnail: { file: path.relative(OUT_DIR, poster.file), at_s: +poster.t.toFixed(2) },
    motion: { moving_frames: moving, repeated_in_motion: holes, per_scene: motion },
    gif: gif && { fps: gif.fps, mb: +(gif.size / 1e6).toFixed(2), start_s: +gif.start.toFixed(2), duration_s: gif.duration, width: gif.width },
    scenes: table.rows,
  };
  await writeJson(OUTPUTS.timingJson, report);
  await fsp.writeFile(OUTPUTS.timingTxt, `${table.text}\n`);
  console.log(`\n${table.text}\n`);
  for (const o of [main, clean, silent]) {
    console.log(`${o.file}: ${o.video} · ${o.color} · ${o.audio} · ${o.duration}s · ${o.size_mb} MB · faststart ${o.faststart}${o.problems.length ? `  PROBLEMS: ${o.problems.join("; ")}` : ""}`);
  }
  console.log(`loudness ${loud.I} LUFS integrated, true peak ${loud.TP} dBFS · ${cues.length} caption cues (${report.captions_burned} burned in)`);
  console.log(`motion: ${holes} repeated frames in ${moving} moving frames (${moving ? ((100 * holes) / moving).toFixed(1) : 0}%)`);
  if (gif) console.log(`readme-hero.gif: ${report.gif.mb} MB, ${gif.fps} fps, ${gif.width}px, ${gif.duration}s from ${report.gif.start_s}s of ${gifSpec.scene}`);
  console.log(`thumbnail.png: ${report.thumbnail.at_s}s of the clean video`);
  console.log(`outputs in ${OUT_DIR}`);
  if (main.problems.length || clean.problems.length || silent.problems.length) process.exitCode = 1;
  if (process.env.KEEP_FRAMES !== "1") {
    // the screencast frames (PNG, several GB) live on in the per-scene clips under out/work/clips
    await fsp.rm(FRAMES_DIR, { recursive: true, force: true });
  }
}

const phase = process.argv[2] || "all";
try {
  if (phase === "capture" || phase === "all") await capturePhase();
  if (phase === "assemble" || phase === "all") await assemblePhase();
  if (!["capture", "assemble", "all"].includes(phase)) throw new Error(`unknown phase ${phase} (capture | assemble | all)`);
} catch (e) {
  console.error(`\n[video] FAILED: ${e.message}`);
  process.exit(1);
}
