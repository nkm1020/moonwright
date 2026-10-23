import { spawn } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";

export const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));
export const nowS = () => Date.now() / 1000;
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function log(...args) {
  console.log(`[video] ${args.join(" ")}`);
}

export async function ensureDir(dir) {
  await fsp.mkdir(dir, { recursive: true });
  return dir;
}

export async function emptyDir(dir) {
  await fsp.rm(dir, { recursive: true, force: true });
  await fsp.mkdir(dir, { recursive: true });
  return dir;
}

export const readJson = async (file) => JSON.parse(await fsp.readFile(file, "utf8"));
export const writeJson = (file, data) => fsp.writeFile(file, JSON.stringify(data, null, 2) + "\n");
export const exists = (file) => fs.existsSync(file);

/** Run a command; resolves with {stdout, stderr}, rejects with the stderr tail on a non-zero exit. */
export function run(cmd, args, { cwd, input, quiet = true, timeoutMs = 0 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd, stdio: ["pipe", "pipe", "pipe"] });
    let timedOut = false;
    const timer =
      timeoutMs > 0
        ? setTimeout(() => {
            timedOut = true;
            child.kill("SIGKILL");
          }, timeoutMs)
        : null;
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => {
      stdout += d;
      if (!quiet) process.stdout.write(d);
    });
    child.stderr.on("data", (d) => {
      stderr += d;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      if (timedOut) return reject(Object.assign(new Error(`${cmd} timed out after ${timeoutMs / 1000} s`), { timedOut: true }));
      if (code === 0) resolve({ stdout, stderr });
      else {
        const tail = stderr.split("\n").slice(-25).join("\n");
        reject(new Error(`${cmd} exited ${code}\n${tail}`));
      }
    });
    if (input !== undefined) child.stdin.end(input);
    else child.stdin.end();
  });
}

export async function ffprobeJson(file, extra = []) {
  const { stdout } = await run("ffprobe", ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", ...extra, file]);
  return JSON.parse(stdout);
}

export async function mediaDuration(file) {
  const info = await ffprobeJson(file);
  return Number(info.format.duration);
}

/** 83.4 -> "1:23.4" */
export function fmtClock(s) {
  const sign = s < 0 ? "-" : "";
  s = Math.abs(s);
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${sign}${m}:${rest.toFixed(1).padStart(4, "0")}`;
}

/** 83.4 -> "00:01:23,400" */
export function srtTime(s) {
  const ms = Math.max(0, Math.round(s * 1000));
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const sec = Math.floor((ms % 60000) / 1000);
  const milli = ms % 1000;
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(h)}:${p(m)}:${p(sec)},${p(milli, 3)}`;
}
