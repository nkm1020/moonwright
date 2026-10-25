// Browser side: renders slides and drives the app through Chromium, recording every scene with
// CDP Page.startScreencast (frame timestamps kept) at a native 1920x1080.
//
// Why --force-device-scale-factor instead of Playwright's deviceScaleFactor: the screencast is
// capped at the view's DIP size, so an emulated DSF of 1.25 still yields 1536x864 frames (then
// upscaled = soft text). Forcing the browser's real scale factor with a 1536x864 window keeps the
// app's CSS layout at 1536x864 while the compositor, and so the screencast, works at 1920x1080.
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { chromium } from "playwright";
import { APP_DIR, APP_URL, CSS_H, CSS_W, CURSOR_JS, DSF, FRAMES_DIR, OUT_H, OUT_W, SLIDES_DIR } from "./lib/paths.mjs";
import { clamp, emptyDir, log, nowS, sleep, writeJson } from "./lib/util.mjs";
import { norm, tokenize } from "./lib/text.mjs";
import { fill } from "./lib/story.mjs";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".woff2": "font/woff2",
  ".json": "application/json",
  ".webm": "video/webm",
};

export async function launchBrowser() {
  const browser = await chromium.launch({
    headless: true,
    args: [
      `--force-device-scale-factor=${DSF}`,
      `--window-size=${CSS_W},${CSS_H}`,
      "--hide-scrollbars",
      "--force-color-profile=srgb",
      "--disable-background-timer-throttling",
      "--disable-renderer-backgrounding",
      "--disable-backgrounding-occluded-windows",
    ],
  });
  const context = await browser.newContext({ viewport: null, colorScheme: "dark", reducedMotion: "no-preference" });
  return { browser, context };
}

/** The app build's own @font-face rules (Geist, Geist Mono), pointed at the running app, so the
 *  slides and captions use the console's typeface without fetching anything from outside. */
function appFontsCss() {
  const root = path.join(APP_DIR, ".next", "static");
  const files = [];
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.endsWith(".css")) files.push(p);
    }
  };
  if (fs.existsSync(root)) walk(root);
  const rules = new Set();
  for (const f of files) {
    for (const m of fs.readFileSync(f, "utf8").matchAll(/@font-face\s*\{[^}]*\}/g)) {
      rules.add(m[0].replace(/url\((['"]?)\.\.\/media\//g, "url($1/_next/static/media/"));
    }
  }
  if (!rules.size) log("warning: no @font-face rules found in the app build; slides fall back to Helvetica/Menlo");
  return [...rules].join("\n");
}

const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** Offline origin for pages rendered after the servers are gone (the caption images): every
 *  request to it is answered by interception, fonts straight from the app build. */
export const OFFLINE_ORIGIN = "http://moonwright.video";

/** Slides, the caption page and fonts are served under <origin>/__video/ by request
 *  interception: during the capture the origin is the app's (fonts load same-origin from the
 *  running server, no third port); offline, /_next/static/media is read from the build. */
export async function installRoutes(context, facts, { origin = APP_URL } = {}) {
  const fontsCss = appFontsCss();
  const safeFacts = Object.fromEntries(Object.entries(facts).map(([k, v]) => [k, escapeHtml(v)]));
  if (origin !== APP_URL) {
    const media = path.join(APP_DIR, ".next", "static", "media");
    await context.route(`${origin}/_next/static/media/**`, async (route) => {
      const name = path.basename(new URL(route.request().url()).pathname);
      const file = path.join(media, name);
      if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: "not found" });
      return route.fulfill({ status: 200, contentType: "font/woff2", body: await fsp.readFile(file) });
    });
  }
  await context.route(`${origin}/__video/**`, async (route) => {
    const url = new URL(route.request().url());
    const rel = decodeURIComponent(url.pathname.replace(/^\/__video\//, ""));
    if (rel === "fonts.css") return route.fulfill({ status: 200, contentType: MIME[".css"], body: fontsCss });
    const file = path.resolve(SLIDES_DIR, rel.replace(/^slides\//, ""));
    if (!file.startsWith(SLIDES_DIR + path.sep) || !fs.existsSync(file)) return route.fulfill({ status: 404, body: `not found: ${rel}` });
    const ext = path.extname(file);
    let body = await fsp.readFile(file);
    if (ext === ".html") {
      try {
        body = fill(body.toString("utf8"), safeFacts, `slides/${path.basename(file)}`);
      } catch (e) {
        log(`slide error: ${e.message}`);
        return route.fulfill({ status: 500, body: e.message });
      }
    }
    return route.fulfill({ status: 200, contentType: MIME[ext] ?? "application/octet-stream", body });
  });
}

// ----------------------------------------------------------------- screencast recorder
class Recorder {
  constructor(cdp, { format, quality }) {
    this.cdp = cdp;
    this.format = format;
    this.quality = quality;
    this.active = false;
    cdp.on("Page.screencastFrame", (f) => this.onFrame(f));
  }

  onFrame(f) {
    this.cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }).catch(() => {});
    if (!this.active) return;
    const ts = f.metadata?.timestamp ?? nowS();
    const name = `${String(this.frames.length).padStart(6, "0")}.${this.format === "png" ? "png" : "jpg"}`;
    this.frames.push({ name, ts });
    this.writes.push(fsp.writeFile(path.join(this.dir, name), Buffer.from(f.data, "base64")));
  }

  async start(dir) {
    this.dir = dir;
    this.frames = [];
    this.writes = [];
    this.active = true;
    const params = { format: this.format, maxWidth: OUT_W, maxHeight: OUT_H, everyNthFrame: 1 };
    if (this.format === "jpeg") params.quality = this.quality;
    await this.cdp.send("Page.startScreencast", params);
    const t = Date.now();
    while (!this.frames.length) {
      if (Date.now() - t > 5000) throw new Error("no screencast frame within 5 s");
      await sleep(4);
    }
    return this.frames[0].ts;
  }

  async stop() {
    await this.cdp.send("Page.stopScreencast").catch(() => {});
    await sleep(60);
    this.active = false;
    await Promise.all(this.writes);
    return [...this.frames].sort((a, b) => a.ts - b.ts);
  }
}

// ----------------------------------------------------------------- scene runner
const DEFAULT_LEAD = { hover: 0.45, moveTo: 0.45, click: 0.5, clickText: 0.5, clickRole: 0.5, select: 0.5, scrollTo: 0.55, scrollBy: 0.55, reveal: 0.12, highlight: 0.05, zoomIn: 0.25, zoomOut: 0.45 };

function estimate(a) {
  switch (a.action) {
    case "hover":
    case "moveTo":
      return 0.68;
    case "click":
    case "clickText":
    case "clickRole":
    case "select":
      return 0.85;
    case "scrollTo":
    case "scrollBy":
      return (a.duration ?? 1100) / 1000 + 0.05;
    case "typeText":
      return (String(a.value ?? a.text ?? "").length * (a.delay ?? 45)) / 1000 + (a.role || a.text !== undefined || a.selector ? 0.9 : 0.05);
    case "waitForScroll":
      return 0.35;
    case "zoomIn":
    case "zoomOut":
      return 0.5;
    case "goto":
      return 1.5;
    default:
      return 0.05;
  }
}

/** Scene time (s) at which an action may start: its cue in the narration minus its lead. */
function cueTime(a, scene, timing) {
  if (a.cue === undefined || a.cue === null) return null;
  const lead = a.lead ?? DEFAULT_LEAD[a.action] ?? 0.2;
  if (typeof a.cue === "number") return Math.max(0, a.cue - lead);
  if (!timing || !timing.sentences.length) throw new Error(`scene ${scene.id}: cue "${a.cue}" but the scene has no narration`);
  if (typeof a.cue === "object" && a.cue.sentence !== undefined) {
    const s = timing.sentences[a.cue.sentence];
    if (!s) throw new Error(`scene ${scene.id}: no sentence ${a.cue.sentence}`);
    return Math.max(0, scene.voDelay + s.speechStart - lead);
  }
  const words = timing.sentences.flatMap((s) => s.words);
  const want = tokenize(String(a.cue)).map(norm).filter(Boolean);
  for (let i = 0; i + want.length <= words.length; i++) {
    if (want.every((t, k) => norm(words[i + k].w) === t)) return Math.max(0, scene.voDelay + words[i].start - lead);
  }
  throw new Error(`scene ${scene.id}: cue "${a.cue}" is not in the narration`);
}

const smoother = (t) => t * t * t * (t * (t * 6 - 15) + 10);

/** Actions that move the pointer or the page: refused while a punch-in zoom is on, because the
 *  pointer works in layout coordinates and the pinch-zoomed screen does not. */
const MOVES = new Set(["hover", "click", "clickText", "clickRole", "select", "moveTo", "scrollTo", "scrollBy", "typeText", "cursor"]);

class Driver {
  constructor(page, cdp) {
    this.page = page;
    this.cdp = cdp;
    this.mouse = { x: CSS_W * 0.62, y: CSS_H * 0.55 };
    this.bow = 1;
    this.zoom = null;
  }

  /** Punch-in: a pinch on the visual viewport (Chromium re-rasterises at the new scale, so text
   *  stays sharp), anchored so the target lands at `to` (fractions of the frame, default the
   *  centre). The layout, the scroll position and the pointer are untouched. */
  async zoomIn(a) {
    if (this.zoom) throw new Error("zoomIn: already zoomed in");
    const loc = this.locate(a);
    try {
      await loc.waitFor({ state: "visible", timeout: a.timeout ?? 10000 });
    } catch {
      throw new Error(`zoomIn: no visible element for ${this.describe(a)}`);
    }
    const r = await this.rectOf(loc, a);
    const z = a.scale ?? 1.5;
    const [fx, fy] = a.at ?? [0.5, 0.5];
    const [tx, ty] = a.to ?? [0.5, 0.5];
    const T = { x: r.x + r.width * fx, y: r.y + r.height * fy };
    const C = { x: tx * CSS_W, y: ty * CSS_H };
    // a pinch at A maps P to A + z (P - A); solve for the anchor that sends T to C
    // (kept 60 px inside the frame: the synthetic gesture puts its two pointers around the anchor)
    const A = { x: clamp((z * T.x - C.x) / (z - 1), 60, CSS_W - 60), y: clamp((z * T.y - C.y) / (z - 1), 60, CSS_H - 60) };
    await this.cdp.send("Input.synthesizePinchGesture", { x: A.x, y: A.y, scaleFactor: z, relativeSpeed: a.speed ?? 240, gestureSourceType: "mouse" });
    this.zoom = { A, z };
  }

  async zoomOut(a = {}) {
    if (!this.zoom) return;
    const s = await this.page.evaluate(() => visualViewport.scale);
    if (Math.abs(s - 1) > 0.002) {
      // a pinch-out starts with its pointers apart, so it is anchored at the frame centre (an
      // anchor near the edge would start them outside the view); at scale 1 the view is whole anyway
      await this.cdp.send("Input.synthesizePinchGesture", { x: CSS_W / 2, y: CSS_H / 2, scaleFactor: 1 / s, relativeSpeed: a.speed ?? 240, gestureSourceType: "mouse" });
    }
    const back = await this.page.evaluate(() => visualViewport.scale);
    if (Math.abs(back - 1) > 0.01) throw new Error(`zoomOut: the page scale is ${back.toFixed(3)}, not 1`);
    this.zoom = null;
  }

  async injectCursor() {
    const ok = await this.page.evaluate(() => Boolean(window.__mw && document.getElementById("__mw_cursor_root"))).catch(() => false);
    if (!ok) await this.page.evaluate(fs.readFileSync(CURSOR_JS, "utf8"));
    await this.page.mouse.move(this.mouse.x, this.mouse.y);
    await this.page.evaluate(() => window.__mw.show());
  }

  async hideCursor() {
    await this.page.evaluate(() => window.__mw && window.__mw.hide()).catch(() => {});
  }

  locate(a) {
    const root = a.within ? this.page.locator(a.within).first() : this.page;
    let loc;
    if (a.role) loc = root.getByRole(a.role, a.name !== undefined ? { name: a.name, exact: a.exact ?? false } : {});
    else if (a.text !== undefined) loc = root.getByText(String(a.text), { exact: a.exact ?? false });
    else if (a.selector) loc = root.locator(a.selector);
    else throw new Error(`${a.action} needs role(+name), text or selector`);
    return loc.filter({ visible: true }).nth(a.nth ?? 0);
  }

  describe(a) {
    return a.role ? `role=${a.role}${a.name !== undefined ? ` name="${a.name}"` : ""}` : a.text !== undefined ? `text="${a.text}"` : `selector=${a.selector}`;
  }

  async header() {
    return this.page.evaluate(() => (window.__mw ? window.__mw.header() : 0));
  }

  async scrollToY(y, duration) {
    const cur = await this.page.evaluate(() => scrollY);
    const ms = duration ?? clamp(450 + Math.abs(y - cur) * 0.42, 600, 1600);
    await this.page.evaluate(([to, d]) => window.__mw.scrollTo(to, d), [y, ms]);
  }

  async waitScrollIdle(maxMs = 4000) {
    const t0 = Date.now();
    let last = await this.page.evaluate(() => scrollY);
    let stable = Date.now();
    while (Date.now() - t0 < maxMs) {
      await sleep(50);
      const y = await this.page.evaluate(() => scrollY);
      if (Math.abs(y - last) > 0.5) {
        last = y;
        stable = Date.now();
      } else if (Date.now() - stable > 300) return;
    }
  }

  /** The rectangle `at` applies to. A text target is measured by its text (a Range over the
   *  element's contents), not by its box: getByText often matches a full-width block, whose box
   *  centre or right edge lands in empty space far from the words. `rect: "box"` overrides. */
  async rectOf(loc, a) {
    const mode = a.rect ?? (a.text !== undefined ? "text" : "box");
    if (mode === "box") return loc.boundingBox();
    return loc.evaluate((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      const r = range.getBoundingClientRect();
      const b = r.width > 0 && r.height > 0 ? r : el.getBoundingClientRect();
      return { x: b.x, y: b.y, width: b.width, height: b.height };
    });
  }

  /** Point inside the target (fractions `at`, default centre, plus `offset` in CSS px), scrolled
   *  into view smoothly if needed. For a figure, `at: [1, 1]` (or past it) keeps the arrow,
   *  whose tip is its top-left corner, off the digits. */
  async point(a) {
    const loc = this.locate(a);
    try {
      await loc.waitFor({ state: "visible", timeout: a.timeout ?? 10000 });
    } catch {
      throw new Error(`${a.action}: no visible element for ${this.describe(a)}`);
    }
    let box = await this.rectOf(loc, a);
    const [fx, fy] = a.at ?? [0.5, 0.5];
    const [ox, oy] = a.offset ?? [0, 0];
    const top = (await this.header()) + 12;
    const y = box.y + box.height * fy + oy;
    // things in the sticky top bar (the demo buttons) or another pinned panel never need a scroll
    const pinned = await loc.evaluate((el) => {
      for (let n = el; n; n = n.parentElement) {
        const p = getComputedStyle(n).position;
        if (p === "fixed" || p === "sticky") return true;
      }
      return false;
    });
    if (!pinned && (y < top + 16 || y > CSS_H - 28)) {
      // the target is off screen: scroll it in (logged, since it often means an ambiguous selector)
      log(`  ${a.action} ${this.describe(a)} was off screen; scrolling it into view`);
      const sy = await this.page.evaluate(() => scrollY);
      await this.scrollToY(sy + box.y + box.height / 2 - (top + (CSS_H - top) / 2));
      box = await this.rectOf(loc, a);
    }
    return { x: box.x + box.width * fx + ox, y: box.y + box.height * fy + oy };
  }

  /** Eased, slightly bowed pointer path (500-800 ms by distance): never a teleport. */
  async moveMouse(x, y, duration) {
    const from = { ...this.mouse };
    const dx = x - from.x;
    const dy = y - from.y;
    const dist = Math.hypot(dx, dy);
    if (dist < 1.5) return;
    const ms = duration ?? clamp(420 + dist * 0.45, 500, 800);
    this.bow = -this.bow;
    const bow = Math.min(36, dist * 0.07) * this.bow;
    const nx = -dy / dist;
    const ny = dx / dist;
    const t0 = performance.now();
    for (;;) {
      const t = Math.min(1, (performance.now() - t0) / ms);
      const e = smoother(t);
      const off = Math.sin(Math.PI * e) * bow;
      await this.page.mouse.move(from.x + dx * e + nx * off, from.y + dy * e + ny * off);
      if (t >= 1) break;
      await sleep(10);
    }
    this.mouse = { x, y };
  }

  async perform(a, elasticMs) {
    const p = this.page;
    if (this.zoom && MOVES.has(a.action)) throw new Error(`${a.action} while zoomed in: add a zoomOut before it`);
    switch (a.action) {
      case "zoomIn":
        await this.zoomIn(a);
        return;
      case "zoomOut":
        await this.zoomOut(a);
        return;
      case "goto":
        await p.goto(new URL(a.url, APP_URL).href, { waitUntil: "networkidle" });
        await this.injectCursor();
        return;
      case "waitForText":
      case "waitForSelector":
      case "waitFor":
        try {
          await this.locate(a).waitFor({ state: "visible", timeout: a.timeout ?? 15000 });
        } catch {
          throw new Error(`${a.action}: ${this.describe(a)} did not appear (the screen does not show what the narration says)`);
        }
        // `by`: the voice names this figure at that cue, so it must be on screen first
        if (a._byS !== undefined && this.sceneNow && this.sceneNow() > a._byS + 0.15) {
          throw new Error(
            `${a.action}: ${this.describe(a)} appeared ${(this.sceneNow() - a._byS).toFixed(2)} s after the voice says "${a.by}"; give the narration more lead time before it`,
          );
        }
        return;
      case "waitForScroll":
        await this.waitScrollIdle(a.timeout ?? 4000);
        return;
      case "pause":
        await sleep(elasticMs);
        return;
      case "cursor":
        this.mouse = { x: a.x, y: a.y };
        await this.injectCursor();
        return;
      case "hideCursor":
        await this.hideCursor();
        return;
      case "showCursor":
        await this.injectCursor();
        return;
      case "moveTo":
        await this.moveMouse(a.x, a.y, a.duration);
        return;
      case "hover": {
        const pt = await this.point(a);
        await this.moveMouse(pt.x, pt.y, a.duration);
        return;
      }
      case "click":
      case "clickText":
      case "clickRole": {
        const pt = await this.point(a);
        await this.moveMouse(pt.x, pt.y, a.duration);
        await sleep(90);
        await p.mouse.down();
        await sleep(80);
        await p.mouse.up();
        await sleep(a.after ?? 120);
        return;
      }
      case "select": {
        // a native <select> popup is not part of the screencast, so the option is set directly
        // after the pointer arrives and pulses on the control: the closed select shows the change
        const pt = await this.point(a);
        await this.moveMouse(pt.x, pt.y, a.duration);
        await sleep(90);
        await p.evaluate(([x, y]) => window.__mw.pulse(x, y), [pt.x, pt.y]);
        await sleep(120);
        await this.locate(a).selectOption(String(a.value));
        await sleep(a.after ?? 150);
        return;
      }
      case "highlight": {
        const loc = this.locate(a);
        try {
          await loc.waitFor({ state: "visible", timeout: a.timeout ?? 10000 });
        } catch {
          throw new Error(`highlight: no visible element for ${this.describe(a)}`);
        }
        await loc.evaluate((el) => el.setAttribute("data-mw-ring", ""));
        const mode = a.rect ?? (a.text !== undefined ? "text" : "box");
        const ok = await p.evaluate(([m, pad]) => window.__mw.ring(m, pad), [mode, a.pad ?? 5]);
        if (!ok) throw new Error(`highlight: could not ring ${this.describe(a)}`);
        return;
      }
      case "unhighlight":
        await p.evaluate(() => window.__mw.unring());
        return;
      case "scrollTo": {
        let y = a.y;
        if (y === undefined) {
          const loc = this.locate(a);
          try {
            await loc.waitFor({ state: "visible", timeout: a.timeout ?? 10000 });
          } catch {
            throw new Error(`scrollTo: no visible element for ${this.describe(a)}`);
          }
          const box = await loc.boundingBox();
          const sy = await p.evaluate(() => scrollY);
          const head = await this.header();
          const docTop = box.y + sy;
          const align = a.align ?? "top";
          if (align === "top") y = docTop - head - (a.offset ?? 16);
          else if (align === "center") y = docTop + box.height / 2 - (head + (CSS_H - head) / 2) + (a.offset ?? 0);
          else if (align === "bottom") y = docTop + box.height - CSS_H + (a.offset ?? 16);
          else throw new Error(`scrollTo: align must be top, center or bottom`);
        }
        await this.scrollToY(y, a.duration);
        return;
      }
      case "scrollBy": {
        const sy = await p.evaluate(() => scrollY);
        await this.scrollToY(sy + a.dy, a.duration);
        return;
      }
      case "typeText": {
        if (a.role || a.text !== undefined || a.selector) {
          const pt = await this.point(a);
          await this.moveMouse(pt.x, pt.y);
          await p.mouse.click(pt.x, pt.y);
        }
        await p.keyboard.type(String(a.value ?? a.text ?? ""), { delay: a.delay ?? 45 });
        return;
      }
      case "press":
        await p.keyboard.press(a.key);
        return;
      case "reveal":
        await p.evaluate(([sel, cls]) => document.querySelectorAll(sel).forEach((e) => e.classList.add(cls)), [a.selector, a.className ?? "in"]);
        return;
      default:
        throw new Error(`unknown action "${a.action}"`);
    }
  }
}

async function runList(driver, list, label) {
  for (const [i, a] of list.entries()) {
    try {
      await driver.perform(a, a.ms ?? 0);
    } catch (e) {
      throw new Error(`${label} action ${i} (${a.action}): ${e.message}`);
    }
  }
}

async function pngSize(file) {
  const fd = await fsp.open(file, "r");
  const buf = Buffer.alloc(24);
  await fd.read(buf, 0, 24, 0);
  await fd.close();
  return buf.toString("ascii", 1, 4) === "PNG" ? { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) } : null;
}

export async function captureScenes(scenes, timings, opts = {}) {
  const format = opts.format ?? "png";
  const { browser, context } = await launchBrowser();
  try {
    await installRoutes(context, opts.facts);
    const page = await context.newPage();
    page.on("pageerror", (e) => log(`page error: ${e.message}`));
    const geo = await page.evaluate(() => ({ w: innerWidth, h: innerHeight, dpr: devicePixelRatio }));
    if (geo.w !== CSS_W || geo.h !== CSS_H || geo.dpr !== DSF) {
      throw new Error(`browser viewport is ${geo.w}x${geo.h}@${geo.dpr}, expected ${CSS_W}x${CSS_H}@${DSF}`);
    }
    const cdp = await context.newCDPSession(page);
    const recorder = new Recorder(cdp, { format, quality: opts.quality ?? 95 });
    const driver = new Driver(page, cdp);
    const results = {};

    for (const scene of scenes) {
      const timing = timings[scene.id];
      const narr = timing?.duration ?? 0;
      const targetEnd = Math.max(scene.voDelay + narr + (narr > 0 ? scene.tail : 0), scene.minDuration || 0);
      const actions = scene.actions;
      const cues = actions.map((a) => cueTime(a, scene, timing));
      // `by` on a wait: the scene time at which the voice reaches that phrase (no lead)
      for (const a of actions) {
        if (a.by !== undefined) a._byS = cueTime({ action: "by", cue: a.by, lead: 0 }, scene, timing);
      }

      // ---- setup (not recorded); a scene that ended punched in is zoomed back out off camera
      await driver.zoomOut();
      if (scene.kind === "slide") {
        const resp = await page.goto(`${APP_URL}/__video/slides/${scene.slide}`, { waitUntil: "load" });
        if (!resp || !resp.ok()) throw new Error(`slide ${scene.slide}: HTTP ${resp?.status()} ${resp ? await resp.text() : ""}`);
        await page.evaluate(() => document.fonts.ready);
        await driver.hideCursor();
      } else {
        await runList(driver, scene.setup, `${scene.id} setup`);
        await driver.injectCursor();
      }
      await sleep(scene.settleMs ?? 250);

      // ---- recorded part
      const dir = await emptyDir(path.join(FRAMES_DIR, scene.id));
      const t0 = await recorder.start(dir);
      const wall0 = Date.now();
      const mono0 = performance.now();
      const now = () => nowS() - t0;
      driver.sceneNow = now;
      if (scene.kind === "slide") await page.evaluate(() => document.documentElement.classList.add("play"));
      let ownS = 0;
      const late = [];
      for (let i = 0; i < actions.length; i++) {
        const a = actions[i];
        if (cues[i] !== null) {
          const wait = cues[i] - now();
          if (wait > 0) await sleep(wait * 1000);
          else if (wait < -0.35) late.push({ action: i, cue: a.cue, lateS: +(-wait).toFixed(2) });
        }
        let ms = a.ms ?? 0;
        if (a.action === "pause" && a.elastic !== false) {
          let k = i + 1;
          while (k < actions.length && cues[k] === null) k++;
          const anchor = k < actions.length ? cues[k] : targetEnd;
          let fixed = 0;
          let flex = a.ms;
          for (let j = i + 1; j < k; j++) {
            const b = actions[j];
            if (b.action === "pause") {
              if (b.elastic === false) fixed += b.ms / 1000;
              else flex += b.ms;
            } else fixed += estimate(b);
          }
          const avail = (anchor - now() - fixed) * 1000;
          if (avail > flex) ms = a.ms * (avail / flex);
        }
        const started = performance.now();
        try {
          await driver.perform(a, ms);
        } catch (e) {
          throw new Error(`scene ${scene.id} action ${i} (${a.action}): ${e.message}`);
        }
        ownS += a.action === "pause" ? a.ms / 1000 : (performance.now() - started) / 1000;
      }
      const actEnd = now();
      const duration = Math.max(targetEnd, actEnd);
      while (now() < duration) await sleep(Math.min(40, (duration - now()) * 1000));
      const frames = (await recorder.stop()).filter((f) => f.ts - t0 <= duration + 1e-6);
      // A capture is real time: if the machine slept (wall clock ran on, the monotonic clock did
      // not) or stalled, the scene is useless. render.sh runs under caffeinate to prevent this.
      const slept = (Date.now() - wall0 - (performance.now() - mono0)) / 1000;
      if (slept > 1) throw new Error(`scene ${scene.id}: the system slept or froze for ${slept.toFixed(0)} s during the capture; run again (keep the Mac awake)`);
      if (actEnd > targetEnd + 15) throw new Error(`scene ${scene.id}: actions ran ${actEnd.toFixed(1)} s for a ${targetEnd.toFixed(1)} s scene; something stalled`);
      // every frame must be a native 1920x1080 image: the clip encoder would silently rescale one
      if (format === "png") {
        for (const f of frames) {
          const size = await pngSize(path.join(dir, f.name));
          if (!size || size.w !== OUT_W || size.h !== OUT_H) {
            throw new Error(`scene ${scene.id}: screencast frame ${f.name} is ${size ? `${size.w}x${size.h}` : "not a PNG"}, expected ${OUT_W}x${OUT_H}`);
          }
        }
      }
      const list = frames.map((f) => ({ file: f.name, t: +(f.ts - t0).toFixed(4) }));
      await writeJson(path.join(dir, "frames.json"), { duration, frames: list });
      const gaps = list.slice(1).map((f, i) => f.t - list[i].t);
      results[scene.id] = {
        id: scene.id,
        kind: scene.kind,
        duration,
        narration: narr,
        voDelay: scene.voDelay,
        targetEnd,
        actionsOwn: +ownS.toFixed(2),
        actionsEnd: +actEnd.toFixed(2),
        frames: list.length,
        maxGapS: gaps.length ? +Math.max(...gaps).toFixed(3) : 0,
        late,
        framesDir: dir,
      };
      log(
        `captured ${scene.id.padEnd(11)} ${duration.toFixed(2).padStart(6)} s  narration ${narr.toFixed(2)} s  actions ${actEnd.toFixed(2)} s  ${list.length} frames` +
          (late.length ? `  LATE cues: ${late.map((l) => `"${l.cue}" +${l.lateS}s`).join(", ")}` : ""),
      );
    }
    return { results, page, context, browser };
  } catch (e) {
    await browser.close().catch(() => {});
    throw e;
  }
}
