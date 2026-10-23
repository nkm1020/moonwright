// Review artefacts: contact sheet (one still per scene, labelled), README hero GIF, timing table.
import fsp from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { CLIPS_DIR, MAX_TOTAL_S, OUTPUTS, STILLS_DIR, WORK_DIR } from "./lib/paths.mjs";
import { emptyDir, fmtClock, log, run } from "./lib/util.mjs";

/** When in a scene its still is taken: storyboard `still` (seconds), else 60 % in, kept clear
 *  of the dissolves at both ends. */
function stillTime(slot, next, scene) {
  const lo = slot.start + Math.max(slot.overlap, slot.dip ?? 0) + 0.25;
  const hi = slot.end - (next ? Math.max(next.overlap, next.dip ?? 0) : 0) - 0.25;
  const want = slot.start + (scene.still ?? slot.duration * 0.6);
  return Math.min(hi, Math.max(lo, want));
}

export async function contactSheet(scenes, timeline, video) {
  await emptyDir(STILLS_DIR);
  const tiles = [];
  for (const [i, slot] of timeline.scenes.entries()) {
    const scene = scenes.find((s) => s.id === slot.id);
    const t = stillTime(slot, timeline.scenes[i + 1], scene);
    const file = path.join(STILLS_DIR, `${String(i + 1).padStart(2, "0")}-${slot.id}.png`);
    await run("ffmpeg", ["-y", "-v", "error", "-ss", t.toFixed(3), "-i", video, "-frames:v", "1", "-vf", "scale=640:-2:flags=lanczos", file]);
    tiles.push({ file, id: slot.id, kind: scene.kind, n: i + 1, start: slot.start, duration: slot.duration, at: t });
  }
  const cols = 4;
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    body{margin:0;background:#0b0f14;color:#e6edf3;font:14px/1.4 "Helvetica Neue",Helvetica,Arial,sans-serif}
    .wrap{padding:28px 32px 32px}
    h1{margin:0 0 4px;font-size:20px;font-weight:600;letter-spacing:-.01em}
    .sub{margin:0 0 22px;color:#9aa7b4;font:13px/1.4 Menlo,monospace}
    .grid{display:grid;grid-template-columns:repeat(${cols},440px);gap:18px}
    figure{margin:0;background:#121821;border:1px solid rgba(255,255,255,.1);border-radius:10px;overflow:hidden}
    img{display:block;width:440px;height:auto}
    figcaption{display:flex;justify-content:space-between;align-items:baseline;padding:8px 12px;font:13px/1.3 Menlo,monospace;color:#9aa7b4}
    figcaption b{color:#e6edf3;font-weight:600}
    .n{color:#f59e0b;margin-right:8px}
  </style></head><body><div class="wrap">
    <h1>Moonwright demo · contact sheet</h1>
    <p class="sub">${timeline.scenes.length} scenes · total ${fmtClock(timeline.total)} · still taken at the time shown in brackets</p>
    <div class="grid">${tiles
      .map(
        (t) => `<figure><img src="file://${t.file}"><figcaption><span><span class="n">${String(t.n).padStart(2, "0")}</span><b>${t.id}</b> · ${t.kind}</span><span>${fmtClock(t.start)} (+${(t.at - t.start).toFixed(1)}s) · ${t.duration.toFixed(1)}s</span></figcaption></figure>`,
      )
      .join("")}</div></div></body></html>`;
  const htmlFile = path.join(WORK_DIR, "contact-sheet.html");
  await fsp.writeFile(htmlFile, html);
  const browser = await chromium.launch({ headless: true, args: ["--force-color-profile=srgb"] });
  try {
    const page = await browser.newPage({ viewport: { width: 64 + cols * 440 + (cols - 1) * 18, height: 400 }, deviceScaleFactor: 1 });
    await page.goto(`file://${htmlFile}`, { waitUntil: "load" });
    await page.screenshot({ path: OUTPUTS.contactSheet, fullPage: true });
  } finally {
    await browser.close();
  }
  return tiles;
}

/** A full-size poster for the upload (YouTube, Devpost, a README <video>): the first scene at its
 *  contact-sheet time, from the clean video, since frame 0 of the film is the fade from black. */
export async function thumbnail(scenes, timeline, video, file) {
  const slot = timeline.scenes[0];
  const scene = scenes.find((s) => s.id === slot.id);
  const t = stillTime(slot, timeline.scenes[1], scene);
  await run("ffmpeg", ["-y", "-v", "error", "-ss", t.toFixed(3), "-i", video, "-frames:v", "1", file]);
  return { file, t };
}

/** README hero GIF: a window of one scene's clean clip (no captions), 1280 px wide, two-pass
 *  palette; frame rate steps down until it fits the size budget. */
export async function heroGif(spec, startS) {
  const clip = path.join(CLIPS_DIR, `${spec.scene}.mp4`);
  const palette = path.join(WORK_DIR, "gif-palette.png");
  const width = spec.width ?? 1280;
  const budget = (spec.maxMB ?? 8) * 1e6;
  for (const fps of [spec.fps ?? 12, 10, 8]) {
    const filters = `fps=${fps},scale=${width}:-1:flags=lanczos`;
    const window = ["-ss", startS.toFixed(3), "-t", String(spec.duration)];
    await run("ffmpeg", ["-y", "-v", "error", ...window, "-i", clip, "-vf", `${filters},palettegen=max_colors=256:stats_mode=diff`, palette]);
    await run("ffmpeg", [
      "-y", "-v", "error", ...window, "-i", clip, "-i", palette,
      "-lavfi", `${filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`, "-loop", "0", OUTPUTS.gif,
    ]);
    const { size } = await fsp.stat(OUTPUTS.gif);
    if (size <= budget) return { fps, size, start: startS, duration: spec.duration, width };
    log(`gif at ${fps} fps is ${(size / 1e6).toFixed(1)} MB, trying lower`);
  }
  throw new Error(`hero GIF does not fit ${spec.maxMB ?? 8} MB even at 8 fps; shorten gif.duration`);
}

export function timingTable(scenes, captured, timeline) {
  const rows = timeline.scenes.map((slot) => {
    const c = captured[slot.id];
    const s = scenes.find((x) => x.id === slot.id);
    return {
      scene: slot.id,
      kind: s.kind,
      start: slot.start,
      duration: slot.duration,
      narration: c.narration,
      voStart: s.voDelay,
      actionsOwn: c.actionsOwn,
      actionsEnd: c.actionsEnd,
      boundBy: c.actionsEnd > c.targetEnd + 0.01 ? "actions" : c.narration > 0 ? "narration" : "minimum",
      transition: slot.transition === "fade" ? `fade ${slot.overlap.toFixed(2)}s` : slot.transition === "dip" ? `dip ${(2 * slot.dip).toFixed(2)}s` : "cut",
      late: c.late,
    };
  });
  const pad = (v, n) => String(v).padStart(n);
  const lines = [];
  lines.push(`${"scene".padEnd(11)} ${pad("start", 7)} ${pad("dur s", 6)} ${pad("narr s", 6)} ${pad("act s", 6)} ${pad("act end", 7)}  bound by   in`);
  for (const r of rows) {
    lines.push(
      `${r.scene.padEnd(11)} ${pad(fmtClock(r.start), 7)} ${pad(r.duration.toFixed(2), 6)} ${pad(r.narration.toFixed(2), 6)} ${pad(r.actionsOwn.toFixed(2), 6)} ${pad(r.actionsEnd.toFixed(2), 7)}  ${r.boundBy.padEnd(9)}  ${r.transition}` +
        (r.late.length ? `  late cues: ${r.late.map((l) => `"${l.cue}" +${l.lateS}s`).join(", ")}` : ""),
    );
  }
  const over = timeline.total - MAX_TOTAL_S;
  lines.push("");
  lines.push(
    `total ${fmtClock(timeline.total)} (${timeline.total.toFixed(2)} s) · limit ${fmtClock(MAX_TOTAL_S)} · ` +
      (over > 0 ? `OVER by ${over.toFixed(2)} s` : `${(-over).toFixed(2)} s to spare`),
  );
  lines.push("narr s = narration audio length; act s = the actions' own time (pauses at their written length, no cue waits);");
  lines.push("act end = scene time when the last action finished; a scene lasts max(voice start + narration + tail, act end).");
  return { rows, text: lines.join("\n"), total: timeline.total, over };
}
