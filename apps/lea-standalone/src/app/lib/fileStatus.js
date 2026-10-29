// lib/fileStatus.js — per-file status for the files rail, breadcrumbs and ⌘P
// (work package "files", docs/PLAN-ui-redesign.md §2 "Per-file status").
//
// The project tree carries no status today, so it is derived client-side:
//
//   running    a live run is editing the file (the latest code step of the
//              current run names it)
//   error      the latest code step for the path has check_status 'error' and
//              that check is still current
//   sorry      the file's text (the latest step's code, else the fetched file)
//              contains a `sorry` outside comments
//   proved     the latest check is 'ok', current, and there is no sorry
//   unchecked  anything else — no step, a stale check, a file nobody checked
//
// Only the SELECTED session's code steps are known to the client, so a file
// another session proved shows as unchecked (or sorry) until the backend adds
// `check_status` per tree entry; see the report. Non-Lean files get no status.

import { isScratchPath } from './canvasFiles.mjs';
import { baseName, isLeanPath } from './filesTree.js';

/** @typedef {'proved' | 'sorry' | 'error' | 'unchecked' | 'running'} FileStatus */

/**
 * @typedef {Object} FileStatusInfo
 * @property {FileStatus | null} status   null for a non-Lean file
 * @property {number} sorries             0 when unknown
 * @property {number} errors              0 unless status is 'error'
 * @property {boolean} stale              the latest check no longer matches the content
 * @property {boolean} known              some evidence exists (a step or the file text)
 */

/** Human names for a status, for titles and the legend. */
export const STATUS_NAMES = {
  proved: 'proved',
  sorry: 'sorry',
  error: 'error',
  unchecked: 'unchecked',
  running: 'Lea is editing',
};

/** Strip `--` line comments and `/- … -/` block comments (nested, non-greedy). */
export function stripComments(source) {
  const text = String(source || '');
  let out = '';
  let i = 0;
  let depth = 0;
  while (i < text.length) {
    const two = text.slice(i, i + 2);
    if (depth > 0) {
      if (two === '/-') { depth += 1; i += 2; continue; }
      if (two === '-/') { depth -= 1; i += 2; continue; }
      if (text[i] === '\n') out += '\n';
      i += 1;
      continue;
    }
    if (two === '/-') { depth = 1; i += 2; continue; }
    if (two === '--') {
      const nl = text.indexOf('\n', i);
      if (nl < 0) break;
      i = nl;
      continue;
    }
    out += text[i];
    i += 1;
  }
  return out;
}

/** Count `sorry` tokens outside comments. */
export function countSorries(source) {
  const m = stripComments(source).match(/\bsorry\b/g);
  return m ? m.length : 0;
}

/** Count the errors in a lean_check detail blob (at least 1 when the check failed). */
export function countErrors(checkDetail) {
  const m = String(checkDetail || '').match(/(^|\n)[^\n]*:\d+:\d+:\s*error/g);
  return m ? m.length : 0;
}

/**
 * Line delta between two texts: `added` lines in `after` not in `before` and
 * `removed` the reverse, by a longest-common-subsequence on lines. Very large
 * pairs fall back to the length difference.
 */
export function lineDelta(before, after) {
  const a = before ? String(before).split('\n') : [];
  const b = after ? String(after).split('\n') : [];
  if (a.length === 0 || b.length === 0 || a.length * b.length > 4_000_000) {
    return { added: Math.max(0, b.length - a.length), removed: Math.max(0, a.length - b.length) };
  }
  let prev = new Uint32Array(b.length + 1);
  let cur = new Uint32Array(b.length + 1);
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      cur[j] = a[i - 1] === b[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], cur[j - 1]);
    }
    [prev, cur] = [cur, prev];
  }
  const lcs = prev[b.length];
  return { added: b.length - lcs, removed: a.length - lcs };
}

/**
 * The latest code step per path, from steps already in timeline order.
 * @template {{ path: string }} S
 * @param {S[]} codeSteps
 * @returns {Map<string, S>}
 */
export function latestStepsByPath(codeSteps) {
  const map = new Map();
  for (const step of codeSteps || []) if (step?.path) map.set(step.path, step);
  return map;
}

/**
 * The file a live run is editing: the latest non-scratch code step of the
 * current run (any run when the run id is unknown). null when nothing runs or
 * the run has not written yet. Status events carry no path, so the step is
 * the only evidence.
 * @param {{ codeSteps: { path: string, run_id?: string | null }[], isRunning: boolean, currentRunId?: string | null }} input
 * @returns {string | null}
 */
export function editingPath({ codeSteps, isRunning, currentRunId }) {
  if (!isRunning) return null;
  const steps = codeSteps || [];
  for (let i = steps.length - 1; i >= 0; i--) {
    const step = steps[i];
    if (!step?.path || isScratchPath(step.path)) continue;
    if (currentRunId && step.run_id && step.run_id !== currentRunId) continue;
    return step.path;
  }
  return null;
}

/**
 * Derive a status per path.
 * @param {{
 *   paths: string[],
 *   codeSteps?: { path: string, code?: string, content_lost?: boolean, check_status?: string | null, check_current?: boolean, check_detail?: string | null }[],
 *   contents?: Map<string, string> | null,
 *   editingPath?: string | null,
 * }} input
 * @returns {Map<string, FileStatusInfo>}
 */
export function deriveFileStatuses({ paths, codeSteps = [], contents = null, editingPath: editing = null }) {
  const latest = latestStepsByPath(codeSteps);
  /** @type {Map<string, FileStatusInfo>} */
  const out = new Map();
  for (const path of paths || []) {
    if (!isLeanPath(path)) {
      out.set(path, { status: null, sorries: 0, errors: 0, stale: false, known: false });
      continue;
    }
    const step = latest.get(path);
    const stepCode = step && !step.content_lost && typeof step.code === 'string' ? step.code : null;
    const text = stepCode ?? contents?.get(path) ?? null;
    const sorries = text == null ? 0 : countSorries(text);
    const stale = !!step && step.check_current === false;
    const checked = !!step && !stale && (step.check_status === 'ok' || step.check_status === 'error');
    let status;
    let errors = 0;
    if (editing && path === editing) status = 'running';
    else if (checked && step.check_status === 'error') {
      status = 'error';
      errors = Math.max(1, countErrors(step.check_detail));
    } else if (sorries > 0) status = 'sorry';
    else if (checked && step.check_status === 'ok') status = 'proved';
    else status = 'unchecked';
    out.set(path, { status, sorries, errors, stale, known: !!step || text != null });
  }
  return out;
}

/**
 * @typedef {Object} SessionFile
 * @property {string} path
 * @property {string} name
 * @property {number} added     net lines added across the session's steps
 * @property {number} removed   net lines removed
 * @property {number} steps
 */

/**
 * The files the session touched, in first-seen order, with +/− line deltas
 * summed over consecutive snapshots (the first snapshot counts as all added:
 * a step carries the after-state only; `baseline_blob_id` would give the true
 * before-state). Scratch probes are left out.
 * @param {{ path: string, code?: string, content_lost?: boolean }[]} codeSteps
 * @returns {SessionFile[]}
 */
export function sessionFiles(codeSteps) {
  /** @type {Map<string, SessionFile & { last: string }>} */
  const byPath = new Map();
  for (const step of codeSteps || []) {
    const path = step?.path;
    if (!path || isScratchPath(path)) continue;
    const code = !step.content_lost && typeof step.code === 'string' ? step.code : null;
    let entry = byPath.get(path);
    if (!entry) {
      entry = { path, name: baseName(path), added: 0, removed: 0, steps: 0, last: '' };
      byPath.set(path, entry);
    }
    entry.steps += 1;
    if (code == null) continue;
    const delta = lineDelta(entry.last, code);
    entry.added += delta.added;
    entry.removed += delta.removed;
    entry.last = code;
  }
  return [...byPath.values()].map(({ last: _last, ...rest }) => rest);
}

/**
 * The short label shown beside a file: counts for error/sorry, 'unchecked',
 * 'Lea' (tree) or 'Lea · editing' (lists) while a run edits it, and for a
 * proved file nothing in the tree, 'proved' in lists.
 * @param {FileStatusInfo | null | undefined} info
 * @param {'tree' | 'list'} [context]
 */
export function statusLabel(info, context = 'tree') {
  if (!info || !info.status) return '';
  switch (info.status) {
    case 'running':
      return context === 'tree' ? 'Lea' : 'Lea · editing';
    case 'error':
      return `${info.errors || 1} error${(info.errors || 1) === 1 ? '' : 's'}`;
    case 'sorry':
      return `${info.sorries} sorry`;
    case 'proved':
      return context === 'tree' ? '' : 'proved';
    default:
      return 'unchecked';
  }
}
