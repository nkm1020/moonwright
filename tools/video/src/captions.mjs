// Caption cues from the narration timing (or a scene's caption override), the SRT file and the
// burned-in caption images: each cue is rendered by Chromium (Albert Sans, like the site) as a
// transparent 1920x1080 PNG and overlaid by ffmpeg (this ffmpeg build has no libass/drawtext).
import fsp from "node:fs/promises";
import path from "node:path";
import { CAPTIONS_DIR, OUT_H, OUT_W } from "./lib/paths.mjs";
import { OFFLINE_ORIGIN, installRoutes, launchBrowser } from "./capture.mjs";
import { buildCues, splitSentences, timeSentences, timeWordsAligned, tokenize, toSrt } from "./lib/text.mjs";
import { emptyDir, srtTime } from "./lib/util.mjs";

const LAYOUT = { maxChars: 42, maxLines: 2, minDur: 1.2, maxDur: 6 };

/** Cues of one scene, in scene time (narration starts at scene.voDelay). */
export function sceneCues(scene, timing, sceneDuration) {
  if (!timing || !timing.sentences.length) return [];
  let sentences = timing.sentences;
  if (scene.caption) {
    const caps = splitSentences(scene.caption);
    sentences =
      caps.length === timing.sentences.length
        ? caps.map((text, i) => {
            const s = timing.sentences[i];
            return { text, words: timeWordsAligned(tokenize(text), s.speechStart, s.speechEnd, scene.pronounce, s.pauses ?? []) };
          })
        : timeSentences(caps, timing.speechStart, timing.speechEnd, scene.pronounce);
  }
  const shifted = sentences.map((s) => ({
    ...s,
    words: s.words.map((w) => ({ ...w, start: w.start + scene.voDelay, end: w.end + scene.voDelay })),
  }));
  return buildCues(shifted, { ...LAYOUT, end: sceneDuration });
}

/** Typographic apostrophes and quotes (can’t, “verified”), as on the slides. */
export function typeset(s) {
  return String(s)
    .replace(/(\w)'(\w)/g, "$1’$2")
    .replace(/(^|[\s(])'/g, "$1‘")
    .replace(/'/g, "’")
    .replace(/(^|[\s(])"/g, "$1“")
    .replace(/"/g, "”");
}

/** All cues on the final timeline, never overlapping, each inside its own scene. A scene with
 *  `burnCaptions: false` (a slide whose text is the narration) keeps its cues in the SRT only. */
export function timelineCues(scenes, timings, timeline) {
  const all = [];
  for (const s of scenes) {
    const slot = timeline.scenes.find((t) => t.id === s.id);
    const align = s.captionAlign ?? "center";
    if (!["left", "center", "right"].includes(align)) throw new Error(`scene ${s.id}: captionAlign must be left, center or right`);
    for (const c of sceneCues(s, timings[s.id], slot.duration)) {
      all.push({
        ...c,
        text: typeset(c.text),
        lines: c.lines.map(typeset),
        align,
        burn: s.burnCaptions !== false,
        scene: s.id,
        start: slot.start + c.start,
        end: Math.min(slot.end, slot.start + c.end),
      });
    }
  }
  all.sort((a, b) => a.start - b.start);
  for (let i = 0; i + 1 < all.length; i++) if (all[i].end > all[i + 1].start) all[i].end = all[i + 1].start;
  return all.filter((c) => c.end - c.start > 0.1);
}

export async function writeSrt(cues, file) {
  await fsp.writeFile(file, toSrt(cues, srtTime));
}

/** One transparent 1920x1080 PNG per cue (plus a blank one), rendered by Chromium at the same
 *  forced 1.25 scale factor as the capture, with the site's Albert Sans; no server needed. */
export async function renderCaptionImages(cues, facts) {
  await emptyDir(CAPTIONS_DIR);
  const { browser, context } = await launchBrowser();
  try {
    await installRoutes(context, facts, { origin: OFFLINE_ORIGIN });
    const page = await context.newPage();
    return await renderWith(page, cues);
  } finally {
    await browser.close();
  }
}

async function renderWith(page, cues) {
  await page.goto(`${OFFLINE_ORIGIN}/__video/slides/_caption.html`, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => window.setCaption(["Albert Sans"]));
  await page.evaluate(() => document.fonts.ready);
  const loaded = await page.evaluate(() => [...document.fonts].some((f) => f.family.replace(/["']/g, "") === "Albert Sans" && f.status === "loaded"));
  if (!loaded) throw new Error("caption font Albert Sans did not load (the captions fetch it from Google Fonts)");
  const shot = async (file) => {
    await page.screenshot({ path: file, omitBackground: true, animations: "disabled" });
    return file;
  };
  await page.evaluate(() => window.setCaption(null));
  const blank = await shot(path.join(CAPTIONS_DIR, "blank.png"));
  const files = [];
  for (const [i, cue] of cues.entries()) {
    if (cue.burn === false) {
      files.push(blank); // SRT only: the slide already shows the words
      continue;
    }
    await page.evaluate(([lines, align]) => window.setCaption(lines, align), [cue.lines, cue.align ?? "center"]);
    files.push(await shot(path.join(CAPTIONS_DIR, `cue-${String(i + 1).padStart(3, "0")}.png`)));
  }
  return { blank, files, size: { w: OUT_W, h: OUT_H } };
}
