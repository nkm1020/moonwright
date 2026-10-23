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

/** The static demo build (GitHub Pages, NEXT_PUBLIC_STATIC_DEMO=1): there is no backend at all,
 *  and every stage replays the live runs recorded under public/samples. */
export const STATIC_DEMO = process.env.NEXT_PUBLIC_STATIC_DEMO === "1";

/** The path the app is served under ("/moonwright" on GitHub Pages, "" otherwise), without a
 *  trailing slash. next.config.ts applies it to the pages and assets; fetches of the files
 *  under public/ add it themselves. */
export const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/+$/, "");

/** The detail of the ApiError a backend request raises in the static demo, before any fetch:
 *  the console replays recordings there, so no path may reach for a server. */
export const NO_BACKEND = "the static demo has no backend";

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
  // the static demo replays recordings: no request ever leaves for a backend
  if (STATIC_DEMO) throw new ApiError(0, NO_BACKEND);
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
/** Files a sample can ship next to its `<id>.plan.json`: the simulation (`.sim.json`), the
 *  Monte Carlo result (`.mc.json`) and the recorded what-if (`.whatif.mc.json`: the same Monte
 *  Carlo with one spare of each single-point robot type). */
export type SampleFile = "sim" | "mc" | "whatif";

export interface SampleSpec {
  id: string;
  /** The short title of the TopBar button and the example card; every figure about the run
   *  is read from its files (format.ts runOutcome). */
  title: string;
  /** The companion files it ships. Declared, not probed: a static host answers a missing file
   *  with a 404 the browser logs as an error. */
  files: readonly SampleFile[];
  /** The brief is one of the backend's example briefs (backend/examples/briefs), so it stands
   *  in for that example card when the backend is unreachable. */
  example?: boolean;
}

/** The recorded live runs under public/samples (Nemotron on Nebius Token Factory, Oct 24
 *  2026, the final pipeline), in display order: the landing pad, repaired twice and then
 *  stressed (simulation, Monte Carlo, spare what-if), and an impossible 60 h brief that ends
 *  with solver-proven advice. A new sample is one line here plus its files. */
export const SAMPLES = [
  { id: "landing_pad", title: "Landing pad", files: ["sim", "mc", "whatif"], example: true },
  { id: "front_loaded", title: "Impossible deadline", files: [], example: true },
] as const satisfies readonly SampleSpec[];

export type SampleId = (typeof SAMPLES)[number]["id"];
export const SAMPLE_IDS: readonly SampleId[] = SAMPLES.map((s) => s.id);

/** The day the samples were recorded live, as the static demo names it. */
export const RECORDED_ON = "Oct 24 2026";

export function sampleSpec(id: SampleId): SampleSpec {
  return (SAMPLES as readonly SampleSpec[]).find((s) => s.id === id) ?? { id, title: id, files: [] };
}

export function isSampleId(id: string | null | undefined): id is SampleId {
  return (SAMPLE_IDS as readonly (string | null | undefined)[]).includes(id);
}

/** URL of a sample file under the base path: "/moonwright/samples/landing_pad.sim.json". */
export function sampleUrl(id: SampleId, file: "plan" | SampleFile): string {
  return `${BASE_PATH}/samples/${id}.${file === "whatif" ? "whatif.mc" : file}.json`;
}

async function loadStatic<T>(path: string, what: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, `could not load ${what} (${errorMessage(e)})`);
  }
  if (!res.ok) throw new ApiError(res.status, `could not load ${what}`);
  return (await res.json()) as T;
}

/** The sample files never change while the page is open: each is fetched once and shared (the
 *  parsed object is read, never mutated); a failed fetch is forgotten, so the next call asks
 *  again. */
const loadedFiles = new Map<string, Promise<unknown>>();

function loadStaticOnce<T>(path: string, what: string): Promise<T> {
  let load = loadedFiles.get(path) as Promise<T> | undefined;
  if (!load) {
    load = loadStatic<T>(path, what);
    loadedFiles.set(path, load);
    load.catch(() => loadedFiles.delete(path));
  }
  return load;
}

/** Recorded PlanResult served from public/samples (no backend needed). */
export function loadSamplePlan(id: SampleId): Promise<PlanResult> {
  return loadStaticOnce<PlanResult>(sampleUrl(id, "plan"), "the recorded plan");
}

const FILE_WHAT: Record<SampleFile, string> = {
  sim: "the recorded simulation",
  mc: "the recorded Monte Carlo result",
  whatif: "the recorded what-if",
};

/** One companion file of a sample, or null when the sample does not ship it. */
function loadSampleFile<T>(id: SampleId, file: SampleFile): Promise<T | null> {
  if (!sampleSpec(id).files.includes(file)) return Promise.resolve(null);
  return loadStaticOnce<T>(sampleUrl(id, file), FILE_WHAT[file]);
}

export interface SampleBundle {
  id: SampleId;
  plan: PlanResult;
  sim: SimResult | null;
  mc: MonteCarloResult | null;
  /** The Monte Carlo of the same plan with one spare of each single-point type. */
  whatIf: MonteCarloResult | null;
  /** The declared companion files that did not load, each with why ("could not load the
   *  recorded what-if (HTTP 404)"). */
  missing: Partial<Record<SampleFile, string>>;
}

/** Everything a sample ships: the plan always (without it the sample fails), the other files
 *  where SAMPLES declares them (a plan-only sample such as front_loaded loads with sim = mc =
 *  whatIf = null). A declared file that does not load is null as well and named in `missing`:
 *  it costs its own panel, never the walk through the rest of the run. */
export async function loadSampleBundle(id: SampleId): Promise<SampleBundle> {
  const missing: Partial<Record<SampleFile, string>> = {};
  const companion = <T>(file: SampleFile): Promise<T | null> =>
    loadSampleFile<T>(id, file).catch((e: unknown) => {
      missing[file] = errorMessage(e);
      return null;
    });
  const [plan, sim, mc, whatIf] = await Promise.all([
    loadSamplePlan(id),
    companion<SimResult>("sim"),
    companion<MonteCarloResult>("mc"),
    companion<MonteCarloResult>("whatif"),
  ]);
  return { id, plan, sim, mc, whatIf, missing };
}
