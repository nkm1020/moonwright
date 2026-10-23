// Every path the pipeline touches, resolved from this file so the scripts run from any cwd.
import path from "node:path";
import { fileURLToPath } from "node:url";

export const VIDEO_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const REPO_DIR = path.resolve(VIDEO_DIR, "..", "..");
export const SAMPLES_DIR = path.join(REPO_DIR, "frontend", "public", "samples");
export const API_HEALTH = "http://127.0.0.1:8074/health";

export const STORYBOARD = path.join(VIDEO_DIR, "storyboard.json");
export const FACTS = path.join(VIDEO_DIR, "facts.json");
export const SLIDES_DIR = path.join(VIDEO_DIR, "slides");
export const VOICE_DIR = path.join(VIDEO_DIR, "voice");
export const CURSOR_JS = path.join(VIDEO_DIR, "src", "cursor.js");

export const OUT_DIR = path.join(VIDEO_DIR, "out");
export const APP_DIR = path.join(OUT_DIR, "app");
export const WORK_DIR = path.join(OUT_DIR, "work");
export const AUDIO_DIR = path.join(WORK_DIR, "audio");
export const FRAMES_DIR = path.join(WORK_DIR, "frames");
export const CLIPS_DIR = path.join(WORK_DIR, "clips");
export const CAPTIONS_DIR = path.join(WORK_DIR, "captions");
export const STILLS_DIR = path.join(WORK_DIR, "stills");
export const STATE_FILE = path.join(WORK_DIR, "capture.json");

export const OUTPUTS = {
  main: path.join(OUT_DIR, "moonwright-demo.mp4"),
  clean: path.join(OUT_DIR, "moonwright-demo-clean.mp4"),
  silent: path.join(OUT_DIR, "moonwright-demo-silent.mp4"),
  srt: path.join(OUT_DIR, "captions.srt"),
  contactSheet: path.join(OUT_DIR, "contact-sheet.png"),
  thumbnail: path.join(OUT_DIR, "thumbnail.png"),
  gif: path.join(OUT_DIR, "readme-hero.gif"),
  timingTxt: path.join(OUT_DIR, "timing.txt"),
  timingJson: path.join(OUT_DIR, "timing.json"),
};

// The two ports this pipeline may use (the user's own dev server lives on 3000).
export const APP_URL = "http://127.0.0.1:3074";
export const API_URL = "http://127.0.0.1:8074";

// Output geometry: the app keeps its 1536x864 CSS-px recording layout and is rasterised at a
// device scale factor of 1.25, so every frame is a native 1920x1080 image.
export const CSS_W = 1536;
export const CSS_H = 864;
export const DSF = 1.25;
export const OUT_W = 1920;
export const OUT_H = 1080;
export const FPS = 30;
export const MAX_TOTAL_S = 175; // 2:55
