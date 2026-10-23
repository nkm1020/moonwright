// ffmpeg side: constant-30-fps clips from the timestamped screencast frames, the joins (cut,
// cross-dissolve, dip through the console background), burned captions, the narration mix
// (two-pass loudnorm to -16 LUFS), the three MP4s and the output checks.
import { spawn } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { CAPTIONS_DIR, CLIPS_DIR, FPS, MAX_TOTAL_S, OUT_DIR, OUT_H, OUT_W, OUTPUTS, WORK_DIR } from "./lib/paths.mjs";
import { ensureDir, ffprobeJson, fmtClock, log, readJson, run } from "./lib/util.mjs";
import { readWav, writeWav } from "./lib/wav.mjs";

/** The console background: dips between scenes pass through it, not through pure black. */
const DIP_COLOR = "0x0b0f14";

const COLOR = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"];
// ffmpeg 8 takes the colour description from the frames, not from the -color_* output options,
// so the frames are tagged here; without it the SPS says "unspecified" for primaries and transfer.
const TAG_709 = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv";
const TO_YUV = `scale=${OUT_W}:${OUT_H}:flags=lanczos:out_color_matrix=bt709:out_range=tv,setsar=1,format=yuv420p,${TAG_709}`;

/**
 * One concat-demuxer entry per image. `option framerate 1000` matters: every image opened by the
 * concat demuxer otherwise gets the image2 default time base of 1/25 s, so each frame time is
 * rounded to 40 ms before the fps filter sees it and roughly one frame in six of every scroll and
 * cursor glide comes out as a repeat (an effective 25 fps). At 1/1000 s the screencast's own
 * timestamps survive.
 */
function concatEntry(file, duration) {
  let s = `file '${file}'\noption framerate 1000\n`;
  if (duration !== undefined) s += `duration ${duration.toFixed(6)}\n`;
  return s;
}

/** One scene: variable-rate screencast frames -> exact-length CFR 30 fps clip (concat demuxer
 *  with per-frame durations, then the fps filter; the last frame is held to the scene end). */
export async function sceneClip(id, framesDir, duration) {
  const n = Math.round(duration * FPS);
  const out = path.join(CLIPS_DIR, `${id}.mp4`);
  if (!fs.existsSync(path.join(framesDir, "frames.json"))) {
    // the frames were deleted after an earlier assemble (KEEP_FRAMES unset); its clip still holds them
    if (fs.existsSync(out)) return { file: out, frames: n, reused: true };
    throw new Error(`scene ${id}: no captured frames and no clip; run the capture again`);
  }
  // an up-to-date clip (same length, same encoder version, newer than the capture) is reused
  const stamp = path.join(CLIPS_DIR, `${id}.json`);
  const ENCODER = 3;
  if (fs.existsSync(out) && fs.existsSync(stamp)) {
    const prev = JSON.parse(fs.readFileSync(stamp, "utf8"));
    if (prev.frames === n && prev.encoder === ENCODER && fs.statSync(stamp).mtimeMs > fs.statSync(path.join(framesDir, "frames.json")).mtimeMs) {
      return { file: out, frames: n, reused: true };
    }
  }
  const { frames } = await readJson(path.join(framesDir, "frames.json"));
  let list = "ffconcat version 1.0\n";
  frames.forEach((f, i) => {
    const next = i + 1 < frames.length ? frames[i + 1].t : duration;
    list += concatEntry(f.file, Math.max(0.0005, next - f.t));
  });
  list += concatEntry(frames.at(-1).file);
  const listFile = path.join(framesDir, "frames.ffconcat");
  await fsp.writeFile(listFile, list);
  await run("ffmpeg", [
    "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listFile,
    "-vf", `fps=${FPS}:round=near,${TO_YUV},tpad=stop_mode=clone:stop_duration=2`,
    "-frames:v", String(n), "-r", String(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-g", String(FPS), ...COLOR, "-an", out,
  ]);
  await fsp.writeFile(stamp, JSON.stringify({ frames: n, duration, encoder: ENCODER }));
  return { file: out, frames: n };
}

async function captionLayer(cues, images, total) {
  let list = "ffconcat version 1.0\n";
  let t = 0;
  const add = (file, d) => {
    if (d <= 0.0005) return;
    list += concatEntry(path.basename(file), d);
  };
  cues.forEach((c, i) => {
    add(images.blank, c.start - t);
    add(images.files[i], c.end - c.start);
    t = c.end;
  });
  add(images.blank, total - t + 0.5);
  list += concatEntry(path.basename(images.blank));
  const file = path.join(CAPTIONS_DIR, "captions.ffconcat");
  await fsp.writeFile(file, list);
  return file;
}

/** Narration placed sample-exactly on the timeline (scene start + its voice delay). */
async function narrationMix(scenes, timings, timeline, file) {
  const rate = 48000;
  const mix = new Int16Array(Math.ceil((timeline.total + 0.5) * rate));
  for (const s of scenes) {
    const t = timings[s.id];
    if (!t || !t.duration) continue;
    const slot = timeline.scenes.find((x) => x.id === s.id);
    const { samples, rate: r } = await readWav(t.wav);
    if (r !== rate) throw new Error(`${t.wav}: ${r} Hz, expected ${rate}`);
    const off = Math.round((slot.start + s.voDelay) * rate);
    for (let i = 0; i < samples.length && off + i < mix.length; i++) {
      mix[off + i] = Math.max(-32768, Math.min(32767, mix[off + i] + samples[i]));
    }
  }
  await writeWav(file, { rate, samples: mix.subarray(0, Math.round(timeline.total * rate)) });
  return file;
}

function lastJson(text) {
  const m = text.match(/\{[\s\S]*?\}(?![\s\S]*\{)/);
  return m ? JSON.parse(m[0]) : null;
}

/** Two-pass EBU R128 normalisation to -16 LUFS. `say` output peaks high for its loudness (peak
 *  to loudness ~15 dB), so a look-ahead limiter at -3 dBFS first lets the second pass be a pure
 *  (linear) gain instead of loudnorm's dynamic mode. */
async function loudnorm(src, dst, target = { I: -16, TP: -1.5, LRA: 11 }) {
  const base = `highpass=f=70,alimiter=limit=0.7:attack=5:release=60:level=false,loudnorm=I=${target.I}:TP=${target.TP}:LRA=${target.LRA}`;
  const p1 = await run("ffmpeg", ["-hide_banner", "-nostats", "-i", src, "-af", `${base}:print_format=json`, "-f", "null", "-"]);
  const m = lastJson(p1.stderr);
  if (!m) throw new Error("loudnorm pass 1 printed no measurement");
  const p2 = `${base}:measured_I=${m.input_i}:measured_TP=${m.input_tp}:measured_LRA=${m.input_lra}:measured_thresh=${m.input_thresh}:offset=${m.target_offset}:linear=true:print_format=json`;
  const r2 = await run("ffmpeg", ["-y", "-hide_banner", "-nostats", "-i", src, "-af", p2, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", dst]);
  const m2 = lastJson(r2.stderr);
  return { before: Number(m.input_i), after: m2 ? Number(m2.output_i) : null, mode: m2?.normalization_type ?? "?" };
}

export { loudnorm };

export async function integratedLoudness(file) {
  const { stderr } = await run("ffmpeg", ["-hide_banner", "-nostats", "-i", file, "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"]);
  const i = stderr.match(/Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+) LUFS/);
  const tp = stderr.match(/True peak:\s*\n\s*Peak:\s*(-?[\d.]+) dBFS/);
  return { I: i ? Number(i[1]) : null, TP: tp ? Number(tp[1]) : null };
}

/** Per-scene joins: dips are a fade to the console background at the end of the outgoing clip
 *  and a fade in from it at the start of the incoming one, then a plain cut. */
function sceneChain(i, slot, next) {
  const f = [`[${i}:v]settb=1/${FPS},setpts=N`];
  if (slot.transition === "dip") f.push(`fade=t=in:st=0:d=${slot.dip.toFixed(4)}:color=${DIP_COLOR}`);
  if (next && next.transition === "dip") {
    f.push(`fade=t=out:st=${(slot.duration - next.dip).toFixed(4)}:d=${next.dip.toFixed(4)}:color=${DIP_COLOR}`);
  }
  return `${f.join(",")}[v${i}]`;
}

export async function assemble({ scenes, timings, captured, timeline, cues, captionImages, defaults }) {
  await ensureDir(CLIPS_DIR);
  log("encoding scene clips (CFR 30 fps)");
  const clips = {};
  const queue = [...timeline.scenes];
  const worker = async () => {
    for (let slot = queue.shift(); slot; slot = queue.shift()) {
      clips[slot.id] = await sceneClip(slot.id, captured[slot.id].framesDir, slot.duration);
    }
  };
  await Promise.all([worker(), worker(), worker()]);

  // ---- video: joins, captions, fade in/out; one pass writes the captioned and the clean picture
  const capList = await captionLayer(cues, captionImages, timeline.total);
  const inputs = [];
  timeline.scenes.forEach((slot) => inputs.push("-i", clips[slot.id].file));
  inputs.push("-f", "concat", "-safe", "0", "-i", capList);
  const n = timeline.scenes.length;
  const parts = timeline.scenes.map((slot, i) => sceneChain(i, slot, timeline.scenes[i + 1]));
  let acc = "v0";
  for (let i = 1; i < n; i++) {
    const slot = timeline.scenes[i];
    const out = `x${i}`;
    if (slot.transition === "fade") {
      parts.push(`[${acc}][v${i}]xfade=transition=fade:duration=${slot.overlap.toFixed(4)}:offset=${slot.start.toFixed(4)}[${out}]`);
    } else {
      parts.push(`[${acc}][v${i}]concat=n=2:v=1:a=0,settb=1/${FPS}[${out}]`);
    }
    acc = out;
  }
  const fadeIn = defaults.fadeIn ?? 0.6;
  const fadeOut = defaults.fadeOut ?? 1.0;
  const ends = `fade=t=in:st=0:d=${fadeIn},fade=t=out:st=${(timeline.total - fadeOut).toFixed(4)}:d=${fadeOut},format=yuv420p,${TAG_709}`;
  parts.push(`[${acc}]split=2[forcap][clean]`);
  parts.push(`[${n}:v]fps=${FPS},settb=1/${FPS},setpts=N,format=rgba[cap]`);
  parts.push(`[forcap][cap]overlay=0:0:format=auto:eof_action=pass,${ends}[vout]`);
  parts.push(`[clean]${ends}[vclean]`);
  const videoOnly = path.join(WORK_DIR, "video.mp4");
  const videoClean = path.join(WORK_DIR, "video-clean.mp4");
  const frames = String(Math.round(timeline.total * FPS));
  const enc = ["-frames:v", frames, "-r", String(FPS), "-fps_mode", "cfr", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", String(FPS * 2), "-bf", "2", ...COLOR, "-an"];
  log(`encoding the final picture (${fmtClock(timeline.total)}, H.264 CRF 18, with and without captions)`);
  await run("ffmpeg", ["-y", "-v", "error", ...inputs, "-filter_complex", parts.join(";"), "-map", "[vout]", ...enc, videoOnly, "-map", "[vclean]", ...enc, videoClean]);

  // ---- audio
  const mixFile = await narrationMix(scenes, timings, timeline, path.join(WORK_DIR, "narration-mix.wav"));
  const normFile = path.join(WORK_DIR, "narration-norm.wav");
  const ln = await loudnorm(mixFile, normFile);
  log(`narration loudness ${ln.before.toFixed(1)} -> ${ln.after?.toFixed(1)} LUFS (${ln.mode})`);

  // ---- mux the three deliverables
  const meta = ["-metadata", "title=Moonwright: Nemotron proposes the plan, a solver proves it"];
  const t = timeline.total.toFixed(3);
  const voiced = (video, out) =>
    run("ffmpeg", ["-y", "-v", "error", "-i", video, "-i", normFile, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
      "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", t, ...meta, "-movflags", "+faststart", out]);
  await voiced(videoOnly, OUTPUTS.main);
  await voiced(videoClean, OUTPUTS.clean);
  await run("ffmpeg", [
    "-y", "-v", "error", "-i", videoOnly, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0",
    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-t", t, ...meta, "-movflags", "+faststart", OUTPUTS.silent,
  ]);
  return { clips, loudness: ln };
}

/**
 * Repeated frames inside motion, per clip: a frame identical to the one before it while the frames
 * on both sides move is a hole in the motion (what the 1/25 s concat time base produced at about
 * one frame in six). Decoded at 480x270 grey; "moving" = more than 20 pixels changed by more than 2.
 */
export function motionHoles(clip) {
  const W = 480;
  const H = 270;
  const size = W * H;
  return new Promise((resolve, reject) => {
    const ff = spawn("ffmpeg", ["-v", "error", "-i", clip, "-vf", `scale=${W}:${H}:flags=area,format=gray`, "-f", "rawvideo", "-"]);
    const changed = [];
    let prev = null;
    let buf = Buffer.alloc(0);
    ff.stdout.on("data", (d) => {
      buf = buf.length ? Buffer.concat([buf, d]) : d;
      while (buf.length >= size) {
        const frame = buf.subarray(0, size);
        if (prev) {
          let c = 0;
          for (let i = 0; i < size; i++) if (Math.abs(frame[i] - prev[i]) > 2) c++;
          changed.push(c);
        } else changed.push(-1);
        prev = Buffer.from(frame);
        buf = buf.subarray(size);
      }
    });
    ff.on("error", reject);
    ff.on("close", (code) => {
      if (code !== 0) return reject(new Error(`ffmpeg exited ${code} decoding ${clip}`));
      let moving = 0;
      const holes = [];
      for (let i = 1; i < changed.length; i++) {
        if (changed[i] > 20) moving++;
        if (i + 1 < changed.length && changed[i] === 0 && changed[i - 1] > 20 && changed[i + 1] > 20) holes.push(i);
      }
      resolve({ frames: changed.length, moving, holes: holes.length, at: holes.slice(0, 12) });
    });
  });
}

/** Facts about a finished file, for the report (and a hard check of the delivery spec). */
export async function verifyOutput(file) {
  const info = await ffprobeJson(file, ["-count_packets"]);
  const v = info.streams.find((s) => s.codec_type === "video");
  const a = info.streams.find((s) => s.codec_type === "audio");
  const res = {
    file: path.relative(OUT_DIR, file),
    size_mb: +(Number(info.format.size) / 1e6).toFixed(1),
    duration: +Number(info.format.duration).toFixed(3),
    video: v && `${v.codec_name} ${v.profile} ${v.width}x${v.height} ${v.pix_fmt} ${v.r_frame_rate} (avg ${v.avg_frame_rate}) ${v.nb_read_packets} frames`,
    color: v && `${v.color_space ?? "?"}/${v.color_primaries ?? "?"}/${v.color_transfer ?? "?"}/${v.color_range ?? "?"}`,
    audio: a && `${a.codec_name} ${a.sample_rate} Hz ${a.channels} ch`,
    faststart: false,
  };
  const head = await fsp.open(file, "r");
  const buf = Buffer.alloc(64 * 1024);
  await head.read(buf, 0, buf.length, 0);
  await head.close();
  const moov = buf.indexOf("moov");
  const mdat = buf.indexOf("mdat");
  res.faststart = moov !== -1 && (mdat === -1 || moov < mdat);
  const problems = [];
  if (!v || v.width !== OUT_W || v.height !== OUT_H) problems.push(`video is ${v?.width}x${v?.height}`);
  if (v && v.r_frame_rate !== `${FPS}/1`) problems.push(`frame rate ${v.r_frame_rate}`);
  if (v && v.pix_fmt !== "yuv420p") problems.push(`pix_fmt ${v.pix_fmt}`);
  if (v && (v.color_space !== "bt709" || v.color_primaries !== "bt709" || v.color_transfer !== "bt709")) problems.push(`colour tags ${res.color}`);
  if (!a || a.codec_name !== "aac") problems.push("no AAC audio");
  if (!res.faststart) problems.push("moov atom not at the front");
  if (res.duration > MAX_TOTAL_S + 0.05) problems.push(`duration ${res.duration}s over ${MAX_TOTAL_S}s`);
  res.problems = problems;
  return res;
}
