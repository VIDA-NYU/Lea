// lib/fuzzy.js — pure fuzzy matching for the ⌘P palette (work package "files").
//
// A query matches a text when every query character appears in the text in
// order (case-insensitive). The score rewards what people type for: a match at
// the start, consecutive characters, characters on a word boundary (after `/`,
// `.`, `_`, `-`, a space, or a camelCase hump) and short texts; it penalises
// gaps between matched characters. For paths the basename is tried first and a
// basename hit outranks one spread across the directories.
//
// The matcher is a small dynamic programme — O(query × text) — so it stays cheap
// for a few hundred files and declarations per keystroke. No dependencies.

const MATCH = 10;
const START_BONUS = 10;
const BOUNDARY_BONUS = 8;
const CONSECUTIVE_BONUS = 10;
const GAP_PENALTY = 1;
const LATE_START_PENALTY = 0.5;
const LENGTH_PENALTY = 0.05;
const BASENAME_BONUS = 20;

const SEPARATORS = new Set(['/', '.', '_', '-', ' ', ':', '\\']);

function isLower(ch) {
  return ch !== ch.toUpperCase() && ch === ch.toLowerCase();
}

function isUpper(ch) {
  return ch !== ch.toLowerCase() && ch === ch.toUpperCase();
}

/** True when `text[i]` starts a word: index 0, after a separator, or a camelCase hump. */
export function isWordBoundary(text, i) {
  if (i <= 0) return true;
  const prev = text[i - 1];
  if (SEPARATORS.has(prev)) return true;
  return isUpper(text[i]) && isLower(prev);
}

/**
 * @typedef {Object} FuzzyHit
 * @property {number} score       higher is better; 0 for an empty query
 * @property {number[]} positions indices into `text` of the matched characters
 */

/**
 * Match `query` against `text`. Returns null when the query is not a
 * subsequence of the text. An empty query matches everything with score 0.
 * @param {string} query
 * @param {string} text
 * @returns {FuzzyHit | null}
 */
export function fuzzyMatch(query, text) {
  const q = String(query ?? '');
  const t = String(text ?? '');
  const n = q.length;
  const m = t.length;
  if (n === 0) return { score: 0, positions: [] };
  if (m === 0 || n > m) return null;
  const ql = q.toLowerCase();
  const tl = t.toLowerCase();
  const NEG = -Infinity;

  /** @type {Float64Array | null} */
  let prev = null;
  /** @type {Int32Array[]} */
  const back = [];
  for (let qi = 0; qi < n; qi++) {
    const row = new Float64Array(m).fill(NEG);
    const from = new Int32Array(m).fill(-1);
    // Running max over tj < ti of prev[tj] + GAP_PENALTY·tj, which turns the gap
    // penalty into an O(1) transition (penalty = GAP_PENALTY·(ti − tj − 1)).
    let runBest = NEG;
    let runArg = -1;
    let any = false;
    for (let ti = 0; ti < m; ti++) {
      if (prev && ti > 0 && prev[ti - 1] > NEG) {
        const v = prev[ti - 1] + GAP_PENALTY * (ti - 1);
        if (v > runBest) {
          runBest = v;
          runArg = ti - 1;
        }
      }
      if (tl[ti] !== ql[qi]) continue;
      let base = MATCH;
      if (isWordBoundary(t, ti)) base += BOUNDARY_BONUS;
      if (ti === 0) base += START_BONUS;
      if (!prev) {
        row[ti] = base - LATE_START_PENALTY * ti;
        from[ti] = -1;
        any = true;
        continue;
      }
      let best = NEG;
      let arg = -1;
      if (runBest > NEG) {
        best = runBest - GAP_PENALTY * (ti - 1) + base;
        arg = runArg;
      }
      if (ti > 0 && prev[ti - 1] > NEG) {
        const v = prev[ti - 1] + base + CONSECUTIVE_BONUS;
        if (v > best) {
          best = v;
          arg = ti - 1;
        }
      }
      if (best === NEG) continue;
      row[ti] = best;
      from[ti] = arg;
      any = true;
    }
    if (!any) return null;
    back.push(from);
    prev = row;
  }

  let bestTi = -1;
  let bestScore = NEG;
  for (let ti = 0; ti < m; ti++) {
    if (prev[ti] > bestScore) {
      bestScore = prev[ti];
      bestTi = ti;
    }
  }
  const positions = new Array(n);
  let ti = bestTi;
  for (let qi = n - 1; qi >= 0; qi--) {
    positions[qi] = ti;
    ti = back[qi][ti];
  }
  return { score: bestScore - LENGTH_PENALTY * (m - n), positions };
}

/**
 * Match a query against a slash-separated path: the basename first (a hit there
 * scores a bonus), then the whole path. A query containing `/` always matches
 * the whole path. Positions index into the full path.
 * @param {string} query
 * @param {string} path
 * @returns {FuzzyHit | null}
 */
export function fuzzyMatchPath(query, path) {
  const q = String(query ?? '');
  const p = String(path ?? '');
  if (!q.includes('/')) {
    const cut = p.lastIndexOf('/') + 1;
    const base = p.slice(cut);
    const hit = fuzzyMatch(q, base);
    if (hit) {
      return {
        score: hit.score + (q ? BASENAME_BONUS : 0),
        positions: hit.positions.map((i) => i + cut),
      };
    }
  }
  return fuzzyMatch(q, p);
}

/**
 * Split `text` into segments for rendering, each flagged as matched or not.
 * @param {string} text
 * @param {number[]} positions
 * @returns {{ text: string, hit: boolean }[]}
 */
export function highlightMatch(text, positions) {
  const t = String(text ?? '');
  const hits = new Set(positions || []);
  /** @type {{ text: string, hit: boolean }[]} */
  const out = [];
  for (let i = 0; i < t.length; i++) {
    const hit = hits.has(i);
    const last = out[out.length - 1];
    if (last && last.hit === hit) last.text += t[i];
    else out.push({ text: t[i], hit });
  }
  return out;
}

/**
 * Rank `items` by fuzzy score against `query`. Items that do not match are
 * dropped; ties keep the input order (so a caller's own ordering — recency,
 * alphabetical — breaks ties). `getText` picks the string to match; pass
 * `{ path: true }` to use the basename-first path matcher.
 * @template T
 * @param {string} query
 * @param {T[]} items
 * @param {(item: T) => string} getText
 * @param {{ limit?: number, path?: boolean }} [options]
 * @returns {{ item: T, score: number, positions: number[] }[]}
 */
export function rankMatches(query, items, getText, options = {}) {
  const match = options.path ? fuzzyMatchPath : fuzzyMatch;
  /** @type {{ item: T, score: number, positions: number[], index: number }[]} */
  const hits = [];
  items.forEach((item, index) => {
    const hit = match(query, getText(item));
    if (hit) hits.push({ item, score: hit.score, positions: hit.positions, index });
  });
  hits.sort((a, b) => b.score - a.score || a.index - b.index);
  const limited = typeof options.limit === 'number' ? hits.slice(0, options.limit) : hits;
  return limited.map(({ item, score, positions }) => ({ item, score, positions }));
}
