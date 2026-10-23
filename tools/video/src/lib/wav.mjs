// Minimal PCM WAV reader/writer (16-bit little-endian, any channel count): narration clips are
// joined and placed on the timeline sample-exactly in Node, so the caption timing and the audio
// can never drift apart.
import fsp from "node:fs/promises";

export async function readWav(file) {
  const buf = await fsp.readFile(file);
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error(`${file}: not a RIFF/WAVE file`);
  }
  let off = 12;
  let fmt = null;
  let data = null;
  while (off + 8 <= buf.length) {
    const id = buf.toString("ascii", off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    const body = off + 8;
    if (id === "fmt ") {
      fmt = {
        format: buf.readUInt16LE(body),
        channels: buf.readUInt16LE(body + 2),
        rate: buf.readUInt32LE(body + 4),
        bits: buf.readUInt16LE(body + 14),
      };
    } else if (id === "data") {
      data = buf.subarray(body, Math.min(buf.length, body + size));
    }
    off = body + size + (size % 2);
  }
  if (!fmt || !data) throw new Error(`${file}: missing fmt or data chunk`);
  if (fmt.format !== 1 || fmt.bits !== 16) throw new Error(`${file}: expected 16-bit PCM, got format ${fmt.format}/${fmt.bits} bit`);
  const samples = new Int16Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.length - (data.length % 2)));
  return { rate: fmt.rate, channels: fmt.channels, samples };
}

export async function writeWav(file, { rate, channels = 1, samples }) {
  const dataBytes = samples.length * 2;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0, "ascii");
  header.writeUInt32LE(36 + dataBytes, 4);
  header.write("WAVE", 8, "ascii");
  header.write("fmt ", 12, "ascii");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * channels * 2, 28);
  header.writeUInt16LE(channels * 2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36, "ascii");
  header.writeUInt32LE(dataBytes, 40);
  const body = Buffer.from(samples.buffer, samples.byteOffset, dataBytes);
  await fsp.writeFile(file, Buffer.concat([header, body]));
}

/** Pauses inside [from, to] s of a mono clip: runs of 5 ms windows whose peak stays under the
 *  threshold for at least `minGap` s (clause pauses of `say` run 100-300 ms; stop consonants
 *  close for 30-70 ms, so they stay under the default). */
export function innerPauses({ rate, samples }, from, to, { threshold = 0.012, minGap = 0.09 } = {}) {
  const t = Math.round(threshold * 32768);
  const win = Math.max(1, Math.round(rate * 0.005));
  const a = Math.max(0, Math.floor(from * rate));
  const b = Math.min(samples.length, Math.ceil(to * rate));
  const out = [];
  let runStart = -1;
  for (let i = a; i < b; i += win) {
    let peak = 0;
    for (let k = i; k < Math.min(b, i + win); k++) peak = Math.max(peak, Math.abs(samples[k]));
    if (peak < t) {
      if (runStart < 0) runStart = i;
    } else if (runStart >= 0) {
      if ((i - runStart) / rate >= minGap) out.push({ start: runStart / rate, end: i / rate });
      runStart = -1;
    }
  }
  return out;
}

/** Seconds of near-silence at the start and at the end of a mono clip. */
export function silenceEdges({ rate, samples }, threshold = 0.012) {
  const t = Math.round(threshold * 32768);
  let a = 0;
  while (a < samples.length && Math.abs(samples[a]) < t) a++;
  let b = samples.length - 1;
  while (b > a && Math.abs(samples[b]) < t) b--;
  return { lead: a / rate, trail: (samples.length - 1 - b) / rate, length: samples.length / rate };
}
