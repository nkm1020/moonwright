/**
 * Server-sent events over fetch(), for POST /plan/stream.
 *
 * The parser follows the EventSource framing: events are separated by a blank line, each line
 * is `field: value`, lines starting with ":" are comments (the server's keep-alive pings),
 * several `data:` lines join with "\n", and chunk boundaries may fall anywhere, including
 * inside a line or between "\r" and "\n".
 */
import { ApiError, API_BASE, NO_BACKEND, STATIC_DEMO, isAbortError, networkMessage, postPlan, readDetail } from "./api";
import type { LoopStep, PlanRequest, PlanResult } from "./types";

export interface SseMessage {
  event: string;
  data: string;
  id: string | null;
}

/** One block (the lines between two blank lines) -> message, or null when it has no data. */
export function parseSseBlock(block: string): SseMessage | null {
  let event = "message";
  let id: string | null = null;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }
  if (data.length === 0) return null;
  return { event, data: data.join("\n"), id };
}

/** Incremental parser: feed decoded text chunks, get complete messages back. */
export class SseParser {
  private buffer = "";
  private pendingCr = false;

  push(text: string): SseMessage[] {
    let chunk = (this.pendingCr ? "\r" : "") + text;
    this.pendingCr = false;
    if (chunk.endsWith("\r")) {
      // a CRLF may be split across chunks: hold the CR back until we see what follows
      this.pendingCr = true;
      chunk = chunk.slice(0, -1);
    }
    this.buffer += chunk.replace(/\r\n|\r/g, "\n");
    const out: SseMessage[] = [];
    let idx = this.buffer.indexOf("\n\n");
    while (idx >= 0) {
      const block = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 2);
      const msg = parseSseBlock(block);
      if (msg) out.push(msg);
      idx = this.buffer.indexOf("\n\n");
    }
    return out;
  }

  /** End of stream: a last event the server did not terminate with a blank line. */
  end(): SseMessage | null {
    const rest = (this.buffer + (this.pendingCr ? "\n" : "")).replace(/\r\n|\r/g, "\n");
    this.buffer = "";
    this.pendingCr = false;
    return rest.trim() ? parseSseBlock(rest) : null;
  }
}

/** Iterate the messages of a fetch() body. Breaking out of the loop cancels the stream. */
export async function* readSse(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<SseMessage, void, undefined> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  let finished = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const msg of parser.push(decoder.decode(value, { stream: true }))) yield msg;
    }
    const tail = decoder.decode();
    if (tail) for (const msg of parser.push(tail)) yield msg;
    const last = parser.end();
    if (last) yield last;
    finished = true;
  } finally {
    if (!finished) reader.cancel().catch(() => undefined);
    else reader.releaseLock();
  }
}

function parseJson<T>(text: string, what: string): T {
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(0, `malformed ${what} event from /plan/stream`);
  }
}

/**
 * Plan with live progress: every `step` event is passed to `onStep` as it arrives; resolves
 * with the final PlanResult, rejects with an ApiError on an `error` event or a broken stream.
 * A backend without /plan/stream (404/405) falls back to POST /plan and replays its steps.
 */
export async function planStream(
  req: PlanRequest,
  onStep: (step: LoopStep) => void,
  signal?: AbortSignal,
): Promise<PlanResult> {
  if (STATIC_DEMO) throw new ApiError(0, NO_BACKEND);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/plan/stream`, {
      method: "POST",
      headers: { accept: "text/event-stream", "content-type": "application/json" },
      body: JSON.stringify(req),
      signal,
    });
  } catch (e) {
    if (isAbortError(e)) throw e;
    throw new ApiError(0, networkMessage(e));
  }

  if (res.status === 404 || res.status === 405) {
    const result = await postPlan(req, signal);
    for (const step of result.steps) onStep(step);
    return result;
  }
  if (!res.ok) throw new ApiError(res.status, await readDetail(res));

  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    // a proxy or an older server answered with the plain PlanResult
    const result = (await res.json()) as PlanResult;
    for (const step of result.steps) onStep(step);
    return result;
  }
  if (!res.body) throw new ApiError(0, "/plan/stream answered without a body");

  for await (const msg of readSse(res.body)) {
    if (msg.event === "step") {
      onStep(parseJson<LoopStep>(msg.data, "step"));
    } else if (msg.event === "result") {
      return parseJson<PlanResult>(msg.data, "result");
    } else if (msg.event === "error") {
      const { detail } = parseJson<{ detail?: string }>(msg.data, "error");
      throw new ApiError(500, detail || "planning failed on the server");
    }
  }
  throw new ApiError(0, "/plan/stream ended without a result");
}
