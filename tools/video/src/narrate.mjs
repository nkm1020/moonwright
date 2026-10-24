// Narration: one WAV per scene (48 kHz mono) plus its timing (sentence boundaries measured from
// the synthesized audio, word times by syllable share inside each sentence).
//
// Each sentence is synthesized on its own with `say -v $VOICE -r $RATE` (AIFF, then 48 kHz WAV
// through ffmpeg) and the clips are joined with a fixed gap, so sentence starts are known exactly
// instead of guessed. macOS `say` treats sentences independently anyway, so the joined clip
// sounds the same as one long call (measured: 6.97 s vs 7.02 s for the title narration).
//
// A human voice-over overrides `say`: drop tools/video/voice/<scene>.wav (any rate/channels).
//
// TTS=openai (the default when OPENAI_API_KEY is set, e.g. in tools/video/.env) synthesizes the
// same sentences with OpenAI's speech endpoint instead of `say` (OPENAI_TTS_MODEL, default
// gpt-4o-mini-tts; OPENAI_TTS_VOICE, default ash; OPENAI_TTS_SPEED, default 1.4, since the voice
// reads about 125 words a minute at 1.0 against say's 180; OPENAI_TTS_INSTRUCTIONS steers the
// delivery, not the pace).
// Sentences stay separate calls, so the timing below is measured the same way for either voice.
import crypto from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { AUDIO_DIR, VOICE_DIR } from "./lib/paths.mjs";
import { ensureDir, log, run, writeJson } from "./lib/util.mjs";
import { innerPauses, readWav, silenceEdges, writeWav } from "./lib/wav.mjs";
import { splitSentences, timeSentences, timeWordsAligned, tokenize } from "./lib/text.mjs";

const RATE_HZ = 48000;

export async function checkVoice(voice) {
  const { stdout } = await run("say", ["-v", "?"]);
  const names = stdout.split("\n").map((l) => l.split(/\s{2,}|\s+(?=[a-z]{2}_[A-Z]{2})/)[0].trim());
  const ok = names.some((n) => n === voice || n.startsWith(`${voice} (`));
  if (!ok) throw new Error(`macOS voice "${voice}" is not installed (say -v '?' lists the installed ones)`);
}

async function toWav48(src, dst) {
  await run("ffmpeg", ["-y", "-v", "error", "-i", src, "-ac", "1", "-ar", String(RATE_HZ), "-c:a", "pcm_s16le", dst]);
}

async function sayToWav(text, { voice, rate, cacheDir }) {
  const key = crypto.createHash("sha1").update(`${voice}|${rate}|${text}`).digest("hex").slice(0, 16);
  const wav = path.join(cacheDir, `${key}.wav`);
  if (!fs.existsSync(wav)) {
    const txt = path.join(cacheDir, `${key}.txt`);
    const aiff = path.join(cacheDir, `${key}.aiff`);
    await fsp.writeFile(txt, text);
    // `say` occasionally stalls inside the speech service (seen once: four calls blocked for
    // 17 minutes, then all returned together), so every call has a deadline and is retried.
    for (let attempt = 1; ; attempt++) {
      try {
        await run("say", ["-v", voice, "-r", String(rate), "-f", txt, "-o", aiff], { timeoutMs: 45000 });
        break;
      } catch (e) {
        if (!e.timedOut || attempt >= 3) throw e;
        log(`say timed out on "${text.slice(0, 40)}...", retrying (${attempt}/3)`);
      }
    }
    await toWav48(aiff, wav);
    await fsp.rm(aiff, { force: true });
  }
  return readWav(wav);
}

const OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech";

// OpenAI clips end on up to half a second of silence; `say`'s do not, so each clip keeps only
// this much at either edge and the sentence gap stays the storyboard's
const OPENAI_EDGE_S = 0.06;

async function openaiToWav(text, { openai, cacheDir }) {
  const { model, voice, instructions, speed } = openai;
  const key = crypto.createHash("sha1").update(`openai|${model}|${voice}|${speed}|${instructions}|${text}`).digest("hex").slice(0, 16);
  const wav = path.join(cacheDir, `${key}.wav`);
  if (!fs.existsSync(wav)) {
    const raw = path.join(cacheDir, `${key}.openai.wav`);
    const body = { model, voice, input: text, response_format: "wav", speed };
    if (instructions && model !== "tts-1" && model !== "tts-1-hd") body.instructions = instructions;
    for (let attempt = 1; ; attempt++) {
      const res = await fetch(OPENAI_SPEECH_URL, {
        method: "POST",
        headers: { Authorization: `Bearer ${process.env.OPENAI_API_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(60000),
      }).catch((e) => ({ ok: false, status: 0, text: async () => String(e) }));
      if (res.ok) {
        await fsp.writeFile(raw, Buffer.from(await res.arrayBuffer()));
        break;
      }
      // the key never appears in the message: only the status and the endpoint's own words
      const detail = (await res.text()).slice(0, 300);
      if (attempt >= 3 || (res.status >= 400 && res.status < 500 && res.status !== 429)) {
        throw new Error(`OpenAI speech failed (HTTP ${res.status}) on "${text.slice(0, 40)}...": ${detail}`);
      }
      log(`OpenAI speech HTTP ${res.status} on "${text.slice(0, 40)}...", retrying (${attempt}/3)`);
      await new Promise((r) => setTimeout(r, 2000 * attempt));
    }
    await toWav48(raw, wav);
    await fsp.rm(raw, { force: true });
    const clip = await readWav(wav);
    const edges = silenceEdges(clip);
    const from = Math.max(0, Math.round((edges.lead - OPENAI_EDGE_S) * clip.rate));
    const to = clip.samples.length - Math.max(0, Math.round((edges.trail - OPENAI_EDGE_S) * clip.rate));
    await writeWav(wav, { rate: clip.rate, samples: clip.samples.slice(from, Math.max(from, to)) });
  }
  return readWav(wav);
}

function synthesize(text, opts) {
  return opts.engine === "openai" ? openaiToWav(text, opts) : sayToWav(text, opts);
}

function humanVoiceFile(id) {
  for (const ext of [".wav", ".aiff", ".m4a", ".mp3"]) {
    const f = path.join(VOICE_DIR, `${id}${ext}`);
    if (fs.existsSync(f)) return f;
  }
  return null;
}

async function narrateScene(scene, opts) {
  const out = path.join(AUDIO_DIR, `${scene.id}.wav`);
  const sentences = splitSentences(scene.narration);
  const human = humanVoiceFile(scene.id);
  if (human) {
    await toWav48(human, out);
    const clip = await readWav(out);
    const edges = silenceEdges(clip);
    const timed = timeSentences(sentences, edges.lead, edges.length - edges.trail, scene.pronounce);
    return {
      id: scene.id,
      source: `voice/${path.basename(human)}`,
      wav: out,
      duration: edges.length,
      speechStart: edges.lead,
      speechEnd: edges.length - edges.trail,
      sentences: timed.map((s) => ({ ...s, start: s.speechStart, end: s.speechEnd })),
    };
  }
  if (!sentences.length) {
    await writeWav(out, { rate: RATE_HZ, samples: new Int16Array(0) });
    return { id: scene.id, source: "none", wav: out, duration: 0, speechStart: 0, speechEnd: 0, sentences: [] };
  }
  const parts = [];
  const timed = [];
  let offset = 0;
  // scene.pauseBefore: {"<sentence index>": extra seconds of silence before that sentence}, a
  // deliberate beat while the screen catches up (e.g. a computation the next sentence reports)
  const extra = scene.pauseBefore || {};
  for (let i = 0; i < sentences.length; i++) {
    if (i > 0) {
      const gap = new Int16Array(Math.round((opts.sentenceGap + Number(extra[i] ?? 0)) * RATE_HZ));
      parts.push(gap);
      offset += gap.length;
    }
    const clip = await synthesize(scene.ttsSentences[i], opts);
    const edges = silenceEdges(clip);
    const start = offset / RATE_HZ;
    const len = clip.samples.length / RATE_HZ;
    const speechStart = start + edges.lead;
    const speechEnd = start + len - edges.trail;
    // pauses the voice makes inside the sentence (after commas, colons), in scene time
    const pauses = innerPauses(clip, edges.lead, len - edges.trail).map((p) => ({ start: start + p.start, end: start + p.end }));
    timed.push({
      text: sentences[i],
      spoken: scene.ttsSentences[i],
      start,
      end: start + len,
      speechStart,
      speechEnd,
      pauses,
      words: timeWordsAligned(tokenize(sentences[i]), speechStart, speechEnd, scene.pronounce, pauses),
    });
    parts.push(clip.samples);
    offset += clip.samples.length;
  }
  const samples = new Int16Array(offset);
  let o = 0;
  for (const p of parts) {
    samples.set(p, o);
    o += p.length;
  }
  await writeWav(out, { rate: RATE_HZ, samples });
  return {
    id: scene.id,
    source: opts.engine === "openai" ? `openai ${opts.openai.model} ${opts.openai.voice}` : `say -v ${opts.voice} -r ${opts.rate}`,
    wav: out,
    duration: samples.length / RATE_HZ,
    speechStart: timed[0].speechStart,
    speechEnd: timed.at(-1).speechEnd,
    sentences: timed,
  };
}

export async function narrate(scenes, opts) {
  await ensureDir(AUDIO_DIR);
  const cacheDir = await ensureDir(path.join(AUDIO_DIR, "cache"));
  const needsTts = scenes.some((s) => s.narration && !humanVoiceFile(s.id));
  if (needsTts && opts.engine === "openai" && !process.env.OPENAI_API_KEY) {
    throw new Error("TTS=openai needs OPENAI_API_KEY (put it in tools/video/.env)");
  }
  if (needsTts && opts.engine !== "openai") await checkVoice(opts.voice);
  const timings = {};
  const queue = [...scenes];
  const worker = async () => {
    for (let s = queue.shift(); s; s = queue.shift()) {
      timings[s.id] = await narrateScene(s, { ...opts, cacheDir });
      await writeJson(path.join(AUDIO_DIR, `${s.id}.json`), timings[s.id]);
    }
  };
  await Promise.all([worker(), worker()]);
  for (const s of scenes) {
    const t = timings[s.id];
    log(`narration ${s.id.padEnd(11)} ${t.duration.toFixed(2).padStart(6)} s  ${t.sentences.length} sentence(s)  ${t.source}`);
  }
  return timings;
}
