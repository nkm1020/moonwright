/**
 * Typed fetch helpers for the Moonwright backend (backend/moonwright/api.py).
 *
 * Every helper resolves with the parsed JSON body or rejects with an {@link ApiError} that
 * carries the server's `detail` (FastAPI's error shape) and the HTTP status; a network failure
 * or timeout is an ApiError with status 0. A caller's AbortSignal is forwarded and its
 * AbortError is rethrown untouched so the UI can tell a cancel from a failure.
 */
import type {
  ExampleBrief,
  Health,
  MonteCarloRequest,
  MonteCarloResult,
  PlanRequest,
  PlanResult,
  SimResult,
  SimulateRequest,
  StructureRequest,
  StructureResponse,
} from "./types";

const DEFAULT_API_URL = "http://localhost:8000";

/** Backend base URL without a trailing slash; inlined at build time from NEXT_PUBLIC_API_URL. */
export const API_BASE = (process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL).replace(/\/+$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(status > 0 ? `${detail} (HTTP ${status})` : detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
}

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message || e.name;
  return String(e);
}

export function networkMessage(e: unknown): string {
  const reason = e instanceof Error && e.message ? ` (${e.message})` : "";
  return `cannot reach the backend at ${API_BASE}${reason}`;
}

/** FastAPI errors are {detail: string | [{loc, msg, type}]}; fall back to the body text. */
export async function readDetail(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  if (!text) return res.statusText || `HTTP ${res.status}`;
  try {
    const body: unknown = JSON.parse(text);
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const parts = detail.map((item) => {
        const d = item as { loc?: unknown[]; msg?: string };
        const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body").join(".") : "";
        return loc ? `${loc}: ${d.msg ?? "invalid"}` : (d.msg ?? "invalid");
      });
      if (parts.length) return parts.join("; ");
    }
    if (detail !== undefined) return JSON.stringify(detail);
  } catch {
    // not JSON: fall through to the raw text
  }
  return text.slice(0, 300);
}

interface FetchOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  /** Abort with an ApiError(0) when the server does not answer in time. */
  timeoutMs?: number;
}

export async function apiFetch<T>(path: string, opts: FetchOptions = {}): Promise<T> {
  const { method = "GET", body, signal, timeoutMs = 0 } = opts;
  const ctrl = new AbortController();
  const forward = () => ctrl.abort(signal?.reason);
  if (signal?.aborted) forward();
  else signal?.addEventListener("abort", forward, { once: true });
  const timer =
    timeoutMs > 0
      ? setTimeout(
          () => ctrl.abort(new DOMException(`no answer within ${timeoutMs / 1000} s`, "TimeoutError")),
          timeoutMs,
        )
      : undefined;
  try {
    let res: Response;
    try {
      res = await fetch(`${API_BASE}${path}`, {
        method,
        headers:
          body === undefined
            ? { accept: "application/json" }
            : { accept: "application/json", "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
      });
    } catch (e) {
      if (signal?.aborted) throw e; // the caller cancelled
      if (e instanceof DOMException && e.name === "TimeoutError") {
        throw new ApiError(0, `${API_BASE} did not answer within ${timeoutMs / 1000} s`);
      }
      throw new ApiError(0, networkMessage(e));
    }
    if (!res.ok) throw new ApiError(res.status, await readDetail(res));
    return (await res.json()) as T;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    signal?.removeEventListener("abort", forward);
  }
}

// ------------------------------------------------------------------ endpoints
export function getHealth(signal?: AbortSignal): Promise<Health> {
  return apiFetch<Health>("/health", { signal, timeoutMs: 5000 });
}

export function getExamples(signal?: AbortSignal): Promise<ExampleBrief[]> {
  return apiFetch<ExampleBrief[]>("/examples", { signal, timeoutMs: 8000 });
}

export function postStructure(req: StructureRequest, signal?: AbortSignal): Promise<StructureResponse> {
  return apiFetch<StructureResponse>("/structure", { method: "POST", body: req, signal });
}

/** Blocking variant of planning; `planStream` in sse.ts is what the UI uses first. */
export function postPlan(req: PlanRequest, signal?: AbortSignal): Promise<PlanResult> {
  return apiFetch<PlanResult>("/plan", { method: "POST", body: req, signal });
}

export function postSimulate(req: SimulateRequest, signal?: AbortSignal): Promise<SimResult> {
  return apiFetch<SimResult>("/simulate", { method: "POST", body: req, signal });
}

export function postMonteCarlo(req: MonteCarloRequest, signal?: AbortSignal): Promise<MonteCarloResult> {
  return apiFetch<MonteCarloResult>("/montecarlo", { method: "POST", body: req, signal });
}

// ------------------------------------------------------------------ bundled samples
export type SampleId = "landing_pad" | "access_road" | "tight_battery";
export const SAMPLE_IDS: readonly SampleId[] = ["landing_pad", "access_road", "tight_battery"];

async function loadStatic<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, `could not load ${path} (${errorMessage(e)})`);
  }
  if (!res.ok) throw new ApiError(res.status, `could not load ${path}`);
  return (await res.json()) as T;
}

/** Recorded PlanResult served from public/samples (no backend needed). */
export function loadSamplePlan(id: SampleId): Promise<PlanResult> {
  return loadStatic<PlanResult>(`/samples/${id}.plan.json`);
}

/** Recorded SimResult (only landing_pad ships one: an EX2 failure at 30 h and one replan). */
export function loadSampleSim(id: SampleId): Promise<SimResult> {
  return loadStatic<SimResult>(`/samples/${id}.sim.json`);
}
