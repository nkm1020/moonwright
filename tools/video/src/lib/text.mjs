// Text side of the narration: sentence splitting, a syllable model for word timing, caption
// cue layout (max 2 lines x 42 chars, 1.2-6 s each) and SRT output.

export function splitSentences(text) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  if (!t) return [];
  return t.split(/(?<=[.!?]["”’)]?)\s+(?=["“‘(]?[A-Z0-9])/);
}

export const tokenize = (sentence) => sentence.split(/\s+/).filter(Boolean);

/** "won't" -> "wont", "3.5" -> "35", "OR-Tools," -> "ortools": the key cue phrases match on. */
export const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "");

const ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"];
const TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"];

/** 9930 -> "nine thousand nine hundred thirty" (also the |words formatter of facts.json). */
export function numberWords(n) {
  n = Math.round(Math.abs(n));
  if (n < 20) return ONES[n];
  if (n < 100) return TENS[Math.floor(n / 10)] + (n % 10 ? ` ${ONES[n % 10]}` : "");
  if (n < 1000) return `${ONES[Math.floor(n / 100)]} hundred` + (n % 100 ? ` ${numberWords(n % 100)}` : "");
  if (n < 1e6) return `${numberWords(Math.floor(n / 1000))} thousand` + (n % 1000 ? ` ${numberWords(n % 1000)}` : "");
  return `${numberWords(Math.floor(n / 1e6))} million` + (n % 1e6 ? ` ${numberWords(n % 1e6)}` : "");
}

function wordSyllables(word) {
  let w = word.toLowerCase().replace(/[^a-z]/g, "");
  if (!w) return 0;
  if (w.length <= 3) return 1;
  w = w.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/, "").replace(/^y/, "");
  const groups = w.match(/[aeiouy]{1,2}/g);
  return Math.max(1, groups ? groups.length : 1);
}

/** How long a token takes to say, in syllables, after the pronunciation map (spoken form). */
export function tokenSyllables(token, pronounce = {}) {
  const core = token.replace(/^[^\w<]+|[^\w>%]+$/g, "");
  const spoken = pronounce[core] ?? core;
  let total = 0;
  for (const part of spoken.split(/[\s\-–—/]+/)) {
    if (!part) continue;
    if (/^\d[\d,]*(\.\d+)?%?$/.test(part)) {
      const [int, dec] = part.replace(/[,%]/g, "").split(".");
      let words = numberWords(parseInt(int, 10));
      if (dec) words += " point " + dec.split("").map((c) => ONES[+c]).join(" ");
      if (part.endsWith("%")) words += " percent";
      total += words.split(" ").reduce((a, w) => a + wordSyllables(w), 0);
    } else if (/^[A-Z0-9]+$/.test(part) && (!/[AEIOUY]/.test(part) || /\d/.test(part))) {
      for (const ch of part) total += /\d/.test(ch) ? wordSyllables(ONES[+ch]) : ch === "W" ? 3 : 1;
    } else {
      total += wordSyllables(part) + (part.match(/\d/g) || []).length;
    }
  }
  return Math.max(1, total);
}

const PAUSE_AFTER = { ",": 0.12, ":": 0.2, ";": 0.2, "—": 0.2 };

/** Spread [t0, t1] over the tokens by syllable share, leaving the TTS's clause pauses. */
export function timeWords(tokens, t0, t1, pronounce = {}) {
  if (!tokens.length) return [];
  const syl = tokens.map((t) => tokenSyllables(t, pronounce));
  let pauses = tokens.map((t, i) => (i < tokens.length - 1 ? (PAUSE_AFTER[t.slice(-1)] ?? 0) : 0));
  const span = Math.max(0.05, t1 - t0);
  let pauseSum = pauses.reduce((a, b) => a + b, 0);
  if (pauseSum > span * 0.35) {
    pauses = pauses.map((p) => (p * span * 0.35) / pauseSum);
    pauseSum = span * 0.35;
  }
  const perSyl = (span - pauseSum) / syl.reduce((a, b) => a + b, 0);
  let t = t0;
  return tokens.map((w, i) => {
    const start = t;
    const end = start + syl[i] * perSyl;
    t = end + pauses[i];
    return { w, start, end };
  });
}

/**
 * timeWords, then snapped to the pauses measured inside the sentence: each clause boundary (a
 * token ending in , ; : —) takes the measured pause nearest its estimate (within 0.45 s, in
 * order), so the clause after it starts when the voice resumes instead of a syllable estimate
 * early. Words inside each clause are spread by syllables again.
 */
export function timeWordsAligned(tokens, t0, t1, pronounce = {}, pauses = []) {
  const est = timeWords(tokens, t0, t1, pronounce);
  if (!pauses.length || tokens.length < 2) return est;
  const bounds = [];
  let after = t0;
  for (let i = 0; i < tokens.length - 1; i++) {
    if (!/[,;:—]$/.test(tokens[i])) continue;
    const want = est[i].end;
    let best = null;
    for (const p of pauses) {
      if (p.start <= after + 0.05 || p.end >= t1) continue;
      const d = Math.abs(p.start - want);
      if (d <= 0.45 && (!best || d < Math.abs(best.start - want))) best = p;
    }
    if (best) {
      bounds.push({ i, pause: best });
      after = best.end;
    }
  }
  if (!bounds.length) return est;
  const out = [];
  let from = 0;
  let start = t0;
  for (const { i, pause } of [...bounds, { i: tokens.length - 1, pause: { start: t1, end: t1 } }]) {
    const seg = timeWords(tokens.slice(from, i + 1), start, pause.start, pronounce);
    out.push(...seg);
    from = i + 1;
    start = pause.end;
  }
  return out;
}

/** Sentences without measured boundaries (a human voice-over, or a caption override whose
 *  sentence count differs from the narration): syllable share over the whole speech span. */
export function timeSentences(sentences, t0, t1, pronounce = {}, gap = 0.3) {
  const toks = sentences.map(tokenize);
  const syl = toks.map((ts) => ts.reduce((a, t) => a + tokenSyllables(t, pronounce), 0));
  const totalSyl = syl.reduce((a, b) => a + b, 0) || 1;
  const g = sentences.length > 1 ? Math.min(gap, ((t1 - t0) * 0.2) / (sentences.length - 1)) : 0;
  const speech = Math.max(0.1, t1 - t0 - g * (sentences.length - 1));
  let t = t0;
  return sentences.map((text, i) => {
    const start = t;
    const end = start + (speech * syl[i]) / totalSyl;
    t = end + g;
    return { text, speechStart: start, speechEnd: end, words: timeWords(toks[i], start, end, pronounce) };
  });
}

// ----------------------------------------------------------------- caption cues
const textOf = (words) => words.map((w) => w.w).join(" ");

// subtitle style: a line should not end on an article, preposition or conjunction
const WEAK_END = new Set(["a", "an", "the", "each", "every", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "by", "its", "it", "that", "this", "into", "from", "as", "is", "are", "no", "per", "can", "can't", "won't"]);

/** Best split of a cue into at most `maxLines` lines of `maxChars` (with its cost): break after
 *  a sentence or a clause when possible, never after a weak word if avoidable, else balanced.
 *  Returns null when it cannot fit. */
export function layoutCost(text, maxChars = 42, maxLines = 2) {
  if (text.length <= maxChars) return { lines: [text], cost: 0 };
  if (maxLines < 2) return null;
  const words = text.split(" ");
  let best = null;
  for (let k = 1; k < words.length; k++) {
    const a = words.slice(0, k).join(" ");
    const b = words.slice(k).join(" ");
    if (a.length > maxChars || b.length > maxChars) continue;
    const last = words[k - 1];
    const bare = last.toLowerCase().replace(/[^a-z']/g, "");
    let cost = Math.abs(a.length - b.length) / 3;
    if (/[.!?]["”’)]?$/.test(last)) cost -= 12;
    else if (/[,;:—]$/.test(last)) cost -= 6;
    else if (WEAK_END.has(bare)) cost += 14;
    if (!best || cost < best.cost) best = { cost, lines: [a, b] };
  }
  return best ? { lines: best.lines, cost: Math.max(0, best.cost) } : null;
}

export function layoutLines(text, maxChars = 42, maxLines = 2) {
  return layoutCost(text, maxChars, maxLines)?.lines ?? null;
}

/**
 * Turn timed sentences into caption cues. Inside a sentence the chunking is a small dynamic
 * programme: every chunk must fit 2 x 42 chars and 6 s; breaks after a comma/colon are cheap,
 * mid-clause breaks and orphan words are expensive. Cues shorter than `minDur` are merged with a
 * neighbour when the result still fits, else held on screen longer.
 */
export function buildCues(sentences, { maxChars = 42, maxLines = 2, minDur = 1.2, maxDur = 6, end = Infinity } = {}) {
  const chunks = [];
  for (const s of sentences) {
    const w = s.words;
    const n = w.length;
    if (!n) continue;
    const best = new Array(n + 1).fill(Infinity);
    const prev = new Array(n + 1).fill(-1);
    best[0] = 0;
    for (let j = 1; j <= n; j++) {
      for (let i = j - 1; i >= 0; i--) {
        const text = textOf(w.slice(i, j));
        if (text.length > maxChars * maxLines) break;
        const lay = layoutCost(text, maxChars, maxLines);
        if (!lay) continue;
        const dur = w[j - 1].end - w[i].start;
        if (dur > maxDur) break;
        let cost = 1;
        if (j < n) cost += /[,;:—]$/.test(w[j - 1].w) ? 0 : 2.5;
        if (j < n && WEAK_END.has(w[j - 1].w.toLowerCase().replace(/[^a-z']/g, ""))) cost += 3; // "...name the" | "robots"
        if (j < n && /^(and|but|or|so|while|which|then)$/i.test(w[j].w)) cost -= 0.8; // break before a conjunction
        if (j < n && j - i < 3) cost += 2;
        if (i > 0 && j - i < 3) cost += 1.5;
        if (dur < minDur + 0.4) cost += 1.5; // short cues flash by; under minDur is worse still
        if (dur < minDur) cost += 2.5;
        cost += ((maxChars * maxLines - text.length) / (maxChars * maxLines)) * 0.6;
        cost += lay.cost / 12; // an awkward line break inside the cue (e.g. ending on "each")
        if (best[i] + cost < best[j]) {
          best[j] = best[i] + cost;
          prev[j] = i;
        }
      }
    }
    if (!Number.isFinite(best[n])) {
      // a single word longer than a line: hard-wrap word by word
      for (const word of w) chunks.push([word]);
      continue;
    }
    const cuts = [];
    for (let j = n; j > 0; j = prev[j]) cuts.unshift([prev[j], j]);
    for (const [i, j] of cuts) chunks.push(w.slice(i, j));
  }

  // merge chunks that would flash by
  for (let i = 0; i < chunks.length; i++) {
    const dur = chunks[i].at(-1).end - chunks[i][0].start;
    if (dur >= minDur) continue;
    const tryMerge = (a, b) => {
      const words = [...chunks[a], ...chunks[b]];
      const text = textOf(words);
      return layoutLines(text, maxChars, maxLines) && words.at(-1).end - words[0].start <= maxDur ? words : null;
    };
    const withNext = i + 1 < chunks.length ? tryMerge(i, i + 1) : null;
    if (withNext) {
      chunks.splice(i, 2, withNext);
      i--;
      continue;
    }
    const withPrev = i > 0 ? tryMerge(i - 1, i) : null;
    if (withPrev) {
      chunks.splice(i - 1, 2, withPrev);
      i -= 2;
    }
  }

  const cues = chunks.map((words) => ({
    start: words[0].start,
    end: words.at(-1).end + 0.3,
    text: textOf(words),
    lines: layoutLines(textOf(words), maxChars, maxLines),
  }));
  for (let i = 0; i < cues.length; i++) {
    const next = cues[i + 1];
    const limit = next ? next.start : end;
    if (next && next.start - cues[i].end < 0.6) cues[i].end = next.start; // no flicker in short gaps
    if (cues[i].end - cues[i].start < minDur) cues[i].end = cues[i].start + minDur;
    cues[i].end = Math.min(cues[i].end, limit, cues[i].start + maxDur);
  }
  return cues.filter((c) => c.end - c.start > 0.05);
}

export function toSrt(cues, fmt) {
  return cues.map((c, i) => `${i + 1}\n${fmt(c.start)} --> ${fmt(c.end)}\n${c.lines.join("\n")}\n`).join("\n");
}
