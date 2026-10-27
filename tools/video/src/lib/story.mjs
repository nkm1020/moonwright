// storyboard.json + facts.json: {{placeholders}}, the pronunciation map and the facts check.
import fs from "node:fs";
import path from "node:path";
import { FACTS, REPO_DIR, SAMPLES_DIR, STORYBOARD } from "./paths.mjs";
import { numberWords, splitSentences } from "./text.mjs";

export function loadFacts() {
  const raw = JSON.parse(fs.readFileSync(FACTS, "utf8"));
  const values = {};
  for (const [k, v] of Object.entries(raw)) if (!k.startsWith("_")) values[k] = v;
  return { values, sources: raw._sources || {} };
}

const FILTERS = {
  /** {{n|words}}: 3 -> "three" (for narration that reads better spelled out); TBD stays TBD */
  words: (v) => (/^-?[\d,]+$/.test(String(v)) ? numberWords(Number(String(v).replace(/,/g, ""))) : String(v)),
  Words: (v) => {
    const w = FILTERS.words(v);
    return w.charAt(0).toUpperCase() + w.slice(1);
  },
};

/** Replace {{name}} / {{name|filter}} with facts[name]; an unknown name is an error, never a
 *  silent blank. */
export function fill(text, facts, where = "") {
  return String(text).replace(/\{\{\s*([\w.-]+)\s*(?:\|\s*(\w+)\s*)?\}\}/g, (_, name, filter) => {
    if (!(name in facts)) throw new Error(`unknown placeholder {{${name}}}${where ? ` in ${where}` : ""} (add it to facts.json)`);
    if (filter && !FILTERS[filter]) throw new Error(`unknown filter |${filter} in {{${name}|${filter}}}${where ? ` in ${where}` : ""}`);
    return filter ? FILTERS[filter](facts[name]) : String(facts[name]);
  });
}

function fillDeep(value, facts, where) {
  if (typeof value === "string") return fill(value, facts, where);
  if (Array.isArray(value)) return value.map((v, i) => fillDeep(v, facts, `${where}[${i}]`));
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = fillDeep(v, facts, `${where}.${k}`);
    return out;
  }
  return value;
}

/** Whole-word replacements for the TTS only (captions keep the written form). */
export function applyPronounce(text, map) {
  let out = text;
  for (const [from, to] of Object.entries(map || {})) {
    const esc = from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    out = out.replace(new RegExp(`(?<![\\w-])${esc}(?![\\w-])`, "g"), to);
  }
  return out;
}

export function loadStoryboard() {
  const facts = loadFacts();
  const raw = JSON.parse(fs.readFileSync(STORYBOARD, "utf8"));
  const defaults = raw.defaults || {};
  const pronounce = raw.pronounce || {};
  const ids = new Set();
  const scenes = raw.scenes.map((s, i) => {
    if (!s.id || !/^[a-z0-9_-]+$/.test(s.id)) throw new Error(`scene ${i}: id must be [a-z0-9_-]+`);
    if (ids.has(s.id)) throw new Error(`duplicate scene id ${s.id}`);
    ids.add(s.id);
    if (!["slide", "ui"].includes(s.kind)) throw new Error(`scene ${s.id}: kind must be slide or ui`);
    const narration = fill(s.narration || "", facts.values, `${s.id}.narration`).replace(/\s+/g, " ").trim();
    const caption = s.caption ? fill(s.caption, facts.values, `${s.id}.caption`).replace(/\s+/g, " ").trim() : null;
    const map = { ...pronounce, ...(s.pronounce || {}) };
    return {
      ...s,
      index: i,
      narration,
      caption,
      pronounce: map,
      ttsSentences: splitSentences(narration).map((t) => applyPronounce(t, map)),
      setup: fillDeep(s.setup || [], facts.values, `${s.id}.setup`),
      actions: fillDeep(s.actions || [], facts.values, `${s.id}.actions`),
      voDelay: s.voDelay ?? (s.kind === "slide" ? defaults.slideVoDelay ?? 0.5 : defaults.uiVoDelay ?? 0.3),
      tail: s.tail ?? defaults.tail ?? 0.6,
      minDuration: s.minDuration ?? 0,
      transition: s.transition || defaults.transition || { type: "cut" },
    };
  });
  return { raw, defaults, scenes, facts: facts.values, sources: facts.sources, pronounce };
}

// ----------------------------------------------------------------- facts check
/** `col=val,col2!=val2` -> row predicate (CSV values compare as strings). */
function rowFilter(args) {
  const conds = String(args || "")
    .split(",")
    .filter(Boolean)
    .map((c) => {
      const m = /^([^!=]+)(!?=)(.*)$/.exec(c.trim());
      if (!m) throw new Error(`bad row filter "${c}" (want col=value or col!=value)`);
      return [m[1].trim(), m[2] === "!=", m[3].trim()];
    });
  return (row) =>
    conds.every(([col, not, val]) => {
      if (!(col in row)) throw new Error(`no column ${col}`);
      return (String(row[col]) === val) !== not;
    });
}

/** The value at a dotted path ("replans.0.before.makespan_h"); a missing key is an error. */
function dig(value, jsonPath, where) {
  let v = value;
  for (const key of String(jsonPath || "").split(".").filter(Boolean)) {
    if (v == null) throw new Error(`${where}: ${key} not found`);
    v = v[/^\d+$/.test(key) ? Number(key) : key];
  }
  if (v === undefined) throw new Error(`${where}: ${jsonPath} not found`);
  return v;
}

/** `<file> [json.path]`: a recorded sample (samples/...) or any JSON file in the repository. */
function readJson(head) {
  const [file, jsonPath] = head.split(/\s+/);
  const abs = file.startsWith("samples/") ? path.join(SAMPLES_DIR, file.slice(8)) : path.join(REPO_DIR, file);
  return dig(JSON.parse(fs.readFileSync(abs, "utf8")), jsonPath, head);
}

/** Runs of a Monte Carlo result that finished within the horizon (the samples when they are all
 *  there, else the rate), like the panel. */
function withinHorizon(mc) {
  if (Array.isArray(mc.samples) && mc.samples.length === mc.runs) return mc.samples.filter((s) => s.within_horizon).length;
  return Math.round(mc.within_horizon_rate * mc.runs);
}

const FORMATTERS = {
  len: (v) => v.length,
  round: (v) => Math.round(v),
  /** one or two decimals, as a page prints a fractional hour (72.6967 -> 72.7, 1.7308 -> 1.73) */
  round1: (v) => Math.round(Number(v) * 10) / 10,
  round2: (v) => Math.round(Number(v) * 100) / 100,
  thousands: (v) => Number(v).toLocaleString("en-US"),
  words: (v) => numberWords(Number(v)),
  pct: (v) => `${Math.round(Number(v) * 100)}%`,
  /** 0.835 -> "83.5%" (one decimal only when needed, like the Monte Carlo panel) */
  pct1: (v) => `${(Number(v) * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`,
  pctrange: (v) => `${Math.round(v[0] * 100)}–${Math.round(v[1] * 100)}%`,
  join: (v) => v.join(", "),
  abs: (v) => Math.abs(Number(v)),
  /** a number already in percent: 10.8 -> "10.8%" */
  pctn: (v) => `${Number(v)}%`,
  /** text -> the first match of a regular expression: its capture group, or the array of its
   *  groups when it has several (a verdict's "printer tasks need at least 4,095 Wh, ..."
   *  |re:at least ([\d,]+) Wh -> "4,095"); no match is an error. The pattern cannot hold a "|". */
  re: (v, pattern) => {
    const m = new RegExp(pattern).exec(String(v));
    if (!m) throw new Error(`/${pattern}/ does not match ${JSON.stringify(String(v).slice(0, 80))}`);
    return m.length > 2 ? m.slice(1) : (m[1] ?? m[0]);
  },
  /** "4,095" -> 4095 (each item of an array) */
  num: (v) => (Array.isArray(v) ? v.map((x) => Number(String(x).replace(/,/g, ""))) : Number(String(v).replace(/,/g, ""))),
  /** [a, b] -> a - b (4,009 Wh needed of a 4,000 Wh battery -> 9) */
  diff: (v) => Number(v[0]) - Number(v[1]),
  /** an object -> the values at these dotted paths, as a list (a replan's two makespans:
   *  |pick:after.schedule.makespan_h,before.makespan_h |diff -> the delay) */
  pick: (v, args) =>
    String(args || "")
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean)
      .map((p) => dig(v, p, `|pick:${args}`)),
  /** a number -> it minus a figure of another JSON file (`<file> <json.path>`, as in a source):
   *  the what-if's rate minus the baseline's */
  minus: (v, args) => Number(v) - Number(readJson(String(args || "").trim())),
  /** a difference of two rates -> percentage points, at most one decimal, as the Monte Carlo
   *  panel prints "+49 pts vs baseline" (0.86 - 0.37 -> 49) */
  pts: (v) => Number((Number(v) * 100).toFixed(1)),
  /** a Monte Carlo result -> its misses: the runs that did not finish within the horizon, counted
   *  as the panel counts them (withinHorizonCount in frontend OutcomeBar.tsx) */
  misses: (mc) => mc.runs - withinHorizon(mc),
  /** a Monte Carlo result -> the runs that finished within the horizon (the panel's "74 of 200") */
  within: (mc) => withinHorizon(mc),
  /** a Monte Carlo result -> the misses that came from a robot with no backup: the lost_only
   *  outcomes that lost one of its single_points_of_failure, as the panel's "118 of 126 misses"
   *  counts them (attributeOutcomes in OutcomeBar.tsx) */
  nobackup: (mc) => {
    const spof = new Set(mc.single_points_of_failure ?? []);
    let n = 0;
    for (const [key, count] of Object.entries(mc.outcomes ?? {})) {
      const i = key.indexOf(":");
      if (i < 0 || key.slice(0, i) !== "lost_only") continue;
      if (key.slice(i + 1).split(",").some((t) => spof.has(t.trim()))) n += count;
    }
    return n;
  },
  /** rows -> share of them matching every col=value */
  share: (rows, args) => rows.filter(rowFilter(args)).length / rows.length,
  /** rows -> percentage points the first filter's share is ahead of the second's ("a;b"), each
   *  share rounded to a whole percent first, as the slides print them (86% - 55% -> 31) */
  gain: (rows, args) => {
    const [a, b] = String(args || "").split(";");
    if (!a || !b) throw new Error(`|gain needs two filters, "a;b" (got "${args}")`);
    const pct = (f) => Math.round((100 * rows.filter(rowFilter(f)).length) / rows.length);
    return pct(a) - pct(b);
  },
  /** rows -> how many distinct values a column takes */
  distinct: (rows, col) => new Set(rows.map((r) => r[col])).size,
};

/** Minimal RFC 4180 reader (quoted fields, embedded commas and quotes); first row = header. */
function readCsv(file) {
  const text = fs.readFileSync(file, "utf8");
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"';
        i++;
      } else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      if (row.some((x) => x !== "")) rows.push(row);
      row = [];
    } else field += c;
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  const [head, ...body] = rows;
  return body.map((r) => Object.fromEntries(head.map((h, k) => [h, r[k] ?? ""])));
}

/**
 * A source spec, `<head> |formatter |formatter:args ...`. Heads:
 *   samples/<file> <json.path>        a recorded sample under frontend/public/samples
 *   <repo path>.json <json.path>      any JSON file in the repository
 *   count <dir>/*.<ext>               number of files
 *   csv <repo path>.csv [col=val,col!=val,...] the rows matching every filter (then |len, |share:..., |gain:...;..., |distinct:col)
 * A figure inside a sentence (a verdict's details) is read with |re:<pattern> (then |num, |diff);
 * two figures of one file with |pick:a,b (then |diff); a figure of another file with
 * |minus:<file> <json.path>; a Monte Carlo result's misses with |misses and |nobackup.
 */
function evalSource(spec) {
  const [head, ...pipes] = spec.split("|").map((p) => p.trim());
  let value;
  if (head.startsWith("count ")) {
    const pattern = head.slice(6).trim();
    const dir = path.join(REPO_DIR, path.dirname(pattern));
    const ext = path.extname(pattern);
    value = fs.readdirSync(dir).filter((f) => f.endsWith(ext)).length;
  } else if (head.startsWith("csv ")) {
    const [file, filter] = head.slice(4).trim().split(/\s+/);
    value = readCsv(path.join(REPO_DIR, file)).filter(filter ? rowFilter(filter) : () => true);
    if (!value.length) throw new Error(`${spec}: no row matches`);
  } else {
    value = readJson(head);
  }
  for (const p of pipes) {
    const k = p.indexOf(":");
    const name = k < 0 ? p : p.slice(0, k);
    const args = k < 0 ? undefined : p.slice(k + 1);
    if (!FORMATTERS[name]) throw new Error(`${spec}: unknown formatter |${name}`);
    value = FORMATTERS[name](value, args);
  }
  return value;
}

/** The waitForText actions (setup or recorded) whose text quotes {{name}}: a live figure the
 *  render checks on screen before the voice says it. */
function screenChecks(raw, name) {
  const re = new RegExp(`\\{\\{\\s*${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*(\\|[^}]*)?\\}\\}`);
  const hits = [];
  for (const s of raw?.scenes ?? []) {
    for (const a of [...(s.setup ?? []), ...(s.actions ?? [])]) {
      if (a.action === "waitForText" && re.test(String(a.text ?? ""))) hits.push(s.id);
    }
  }
  return hits;
}

/**
 * Every fact with a source must equal what its source says; TBD facts are skipped. A `screen`
 * source names a figure computed live during the capture (a what-if Monte Carlo): it passes when
 * the storyboard waits for it on screen, so the render stops if the app shows anything else.
 */
export function checkFacts({ values, sources, raw }) {
  const problems = [];
  const checked = [];
  const screen = [];
  for (const [name, spec] of Object.entries(sources)) {
    if (!(name in values)) {
      problems.push(`${name}: listed in _sources but has no value`);
      continue;
    }
    if (values[name] === "TBD") continue;
    if (/^screen\b/.test(spec)) {
      const hits = screenChecks(raw, name);
      if (hits.length) {
        checked.push(name);
        screen.push(`${name} (${hits.join(", ")})`);
      } else problems.push(`${name}: source is "screen" but no waitForText in the storyboard quotes {{${name}}}`);
      continue;
    }
    let actual;
    try {
      actual = evalSource(spec);
    } catch (e) {
      problems.push(`${name}: cannot read ${spec} (${e.message})`);
      continue;
    }
    if (String(actual) !== String(values[name])) problems.push(`${name}: facts.json says ${JSON.stringify(values[name])}, ${spec} gives ${JSON.stringify(actual)}`);
    else checked.push(name);
  }
  const unsourced = Object.keys(values).filter((k) => !(k in sources) && values[k] !== "TBD");
  return { problems, checked, screen, unsourced };
}
