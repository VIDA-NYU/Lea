// Pure derivations for the canvas surface (docs/PLAN-ui-redesign.md §3 "canvas").
// No React, no DOM: everything here runs under `node --test` (canvasModel.test.mjs).
// JSDoc types are what TypeScript sees for this module (allowJs, checkJs off).
import { deriveCodeStepProofStatus } from './proofDisplay.mjs';

/**
 * @typedef {'error' | 'warning' | 'info'} Severity
 * @typedef {{ path?: string | null, line: number | null, column: number | null, severity: Severity, message: string, synthetic?: boolean }} Problem
 * @typedef {'idle' | 'running' | 'error' | 'proved' | 'verified' | 'sorry' | 'unchecked'} ChipKind
 * @typedef {{ kind: ChipKind, label: string, line: number | null }} StatusChip
 * @typedef {{ status: string, detail?: string | null, current?: boolean }} VerifyLike
 * @typedef {{ kind: string, line?: string, newLineNumber?: number, oldLineNumber?: number }} DiffRowLike
 * @typedef {'error' | 'sorry' | 'proved' | 'neutral'} DotTone
 * @typedef {{ index: number, id: string, path: string | null, failed: boolean, user: boolean, current: boolean, tone: DotTone }} TimelineDot
 * @typedef {'goals' | 'problems' | 'verify'} DrawerTabName
 */

// ── Problems ─────────────────────────────────────────────────────────────────
// lean_check output (the LSP daemon's `_format` in prover/lea/lsp_daemon.py and
// the cold `lake env lean` path alike) prints one header per diagnostic,
//   <path>:<line>:<col>: <severity>: <message>
// followed by any number of continuation lines (goal states, hints). The path is
// whatever the checker was given (often absolute). A body with no header at all
// ("error: lake build failed …", "Error: … timed out") is one problem without a
// line. "OK — no errors, no warnings." yields nothing.
const HEADER_RE = /^(.+?):(\d+):(\d+):\s*(error|warning|info|information)\s*:\s*(.*)$/;
const BARE_RE = /^(error|warning)\s*:\s*(.*)$/i;

/** @param {string} raw @returns {Severity} */
function severityOf(raw) {
  const s = String(raw).toLowerCase();
  return s === 'error' ? 'error' : s === 'warning' ? 'warning' : 'info';
}

/**
 * @param {unknown} detail
 * @returns {Problem[]}
 */
export function parseLeanProblems(detail) {
  if (typeof detail !== 'string' || !detail.trim()) return [];
  /** @type {Problem[]} */
  const problems = [];
  /** @type {Problem | null} */
  let current = null;
  for (const raw of detail.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    const header = HEADER_RE.exec(line);
    if (header) {
      current = {
        path: header[1],
        line: Number(header[2]),
        column: Number(header[3]),
        severity: severityOf(header[4]),
        message: header[5].trim(),
      };
      problems.push(current);
      continue;
    }
    const bare = problems.length === 0 ? BARE_RE.exec(line.trim()) : null;
    if (bare) {
      current = { path: null, line: null, column: null, severity: severityOf(bare[1]), message: bare[2].trim() };
      problems.push(current);
      continue;
    }
    if (current && line.trim()) current.message = current.message ? `${current.message}\n${line}` : line;
  }
  return problems;
}

/**
 * The problems a persisted step carries. A failed check whose detail has no
 * parseable header still yields one (position-less) error so nothing is hidden.
 * @param {any} step
 * @returns {Problem[]}
 */
export function problemsForStep(step) {
  if (!step) return [];
  const parsed = parseLeanProblems(step.check_detail);
  if (parsed.length === 0 && step.check_status === 'error') {
    const text = String(step.check_detail || '').trim();
    return [{ path: step.path ?? null, line: null, column: null, severity: 'error', message: text || 'lean_check reported an error.' }];
  }
  return parsed;
}

/**
 * Add a synthetic "declaration uses 'sorry'" warning per sorry line when the
 * check output did not already report one (a truncated detail, or no check yet).
 * Sorted by line; position-less problems last.
 * @param {Problem[]} [problems]
 * @param {number[]} [sorries]
 * @param {string | null} [path]
 * @returns {Problem[]}
 */
export function withSorryWarnings(problems = [], sorries = [], path = null) {
  const reported = problems.some((p) => /\b(sorry|admit)\b/i.test(p.message || ''));
  /** @type {Problem[]} */
  const extra = reported
    ? []
    : sorries.map((line) => ({ path, line, column: 1, severity: /** @type {Severity} */ ('warning'), message: "declaration uses 'sorry'", synthetic: true }));
  return [...problems, ...extra].sort(
    (a, b) => (a.line ?? Number.MAX_SAFE_INTEGER) - (b.line ?? Number.MAX_SAFE_INTEGER) || (a.column ?? 0) - (b.column ?? 0),
  );
}

/**
 * Distinct 1-based lines that carry an error, ascending.
 * @param {Problem[]} [problems]
 * @returns {number[]}
 */
export function errorLines(problems = []) {
  /** @type {number[]} */
  const lines = [];
  for (const p of problems) {
    if (p?.severity === 'error' && Number.isFinite(p.line) && !lines.includes(/** @type {number} */ (p.line))) lines.push(/** @type {number} */ (p.line));
  }
  return lines.sort((a, b) => a - b);
}

/**
 * 1-based line numbers that contain `sorry` / `admit` outside comments.
 * @param {unknown} code
 * @returns {number[]}
 */
export function sorryLines(code) {
  if (typeof code !== 'string' || !code) return [];
  /** @type {number[]} */
  const found = [];
  let depth = 0; // nesting of /- … -/ block comments
  const lines = code.split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    let text = '';
    let j = 0;
    while (j < line.length) {
      if (depth === 0 && line.startsWith('--', j)) break;
      if (line.startsWith('/-', j)) {
        depth += 1;
        j += 2;
        continue;
      }
      if (depth > 0 && line.startsWith('-/', j)) {
        depth -= 1;
        j += 2;
        continue;
      }
      if (depth === 0) text += line[j];
      j += 1;
    }
    if (/\b(?:sorry|admit)\b/.test(text)) found.push(i + 1);
  }
  return found;
}

// ── Diff rows (from codeDiff.diffForStep) ────────────────────────────────────
/**
 * True when every row is an addition: the step created the file, so "changed
 * lines" would be every line and carry no information.
 * @param {DiffRowLike[]} [rows]
 */
export function isFreshFile(rows = []) {
  return rows.length > 0 && rows.every((r) => r?.kind === 'added');
}

/**
 * Lines this step changed, for the gutter marks. Empty for a freshly created
 * file (see isFreshFile).
 * @param {DiffRowLike[]} [rows]
 * @returns {number[]}
 */
export function changedLines(rows = []) {
  if (isFreshFile(rows)) return [];
  return rows
    .filter((r) => r?.kind === 'added' && r.newLineNumber != null)
    .map((r) => /** @type {number} */ (r.newLineNumber));
}

/**
 * The `+a −b this step` counts.
 * @param {DiffRowLike[]} [rows]
 */
export function stepDelta(rows = []) {
  let added = 0;
  let removed = 0;
  for (const r of rows) {
    if (r?.kind === 'added') added += 1;
    else if (r?.kind === 'removed') removed += 1;
  }
  return { added, removed };
}

/**
 * Where Lea's "caret" sits while a run is live: the last line this step added.
 * The run stream carries tool names but not positions, so the newest change is
 * the best evidence of where Lea is working.
 * @param {DiffRowLike[]} [rows]
 * @returns {number | null}
 */
export function caretLine(rows = []) {
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const r = rows[i];
    if (r?.kind === 'added' && r.newLineNumber != null) return r.newLineNumber;
  }
  return null;
}

// ── Timeline strip ───────────────────────────────────────────────────────────
/**
 * One dot per code step: red for a failed check, a ring on the current one, an
 * outline for the user's own edits.
 * @param {any[]} [steps]
 * @param {number} [currentIndex]
 * @returns {TimelineDot[]}
 */
export function timelineDots(steps = [], currentIndex = -1) {
  return steps.map((step, index) => {
    const failed = step?.check_status === 'error';
    const proof = deriveCodeStepProofStatus(step, { historical: true });
    /** @type {DotTone} */
    const tone = failed
      ? 'error'
      : proof === 'stubbed'
        ? 'sorry'
        : proof === 'proved' || proof === 'defined' || proof === 'checked'
          ? 'proved'
          : 'neutral';
    return {
      index,
      id: String(step?.id ?? index),
      path: step?.path ?? null,
      failed,
      user: step?.author === 'user',
      current: index === currentIndex,
      tone,
    };
  });
}

/**
 * "Step n of m" and "turn t · Lea · now".
 * @param {{ index: number, total: number, step: any, latest?: boolean, now?: number }} args
 */
export function stepLabel({ index, total, step, latest = false, now = Date.now() }) {
  const author = step?.author === 'user' ? 'you' : step?.author === 'environment' ? 'environment' : 'Lea';
  const parts = [];
  if (step?.turn != null) parts.push(`turn ${step.turn}`);
  parts.push(author);
  const created = Date.parse(step?.created_at || '');
  if (latest && Number.isFinite(created) && now - created >= 0 && now - created < 120_000) parts.push('now');
  return { head: `Step ${index + 1} of ${total}`, tail: parts.join(' · ') };
}

/**
 * The right-hand note of the timeline strip. Editor states win; then a pointer
 * to the user's own edit just before the shown step ("Step 8 was your edit");
 * then "Editable".
 * @param {{
 *   following: boolean,
 *   step?: any,
 *   previous?: any,
 *   previousIndex?: number,
 *   live: 'none' | 'loading' | 'error' | 'ready',
 *   liveError?: string | null,
 *   running?: boolean,
 *   save?: { state: 'clean' | 'dirty' | 'saving' | 'saved', detail: string },
 *   hasSession?: boolean,
 * }} args
 * @returns {string}
 */
export function editorNote({ following, step, previous, previousIndex = -1, live, liveError = null, running = false, save, hasSession = true }) {
  if (!following) return step?.author === 'user' ? 'Your edit · read-only' : 'Read-only · older step';
  if (live === 'loading') return 'Opening editor…';
  if (live === 'error') return `Editor unavailable${liveError ? ` · ${liveError}` : ''}`;
  if (live === 'none') return hasSession ? '' : 'Send a message to start editing';
  if (running) return 'Read-only while Lea works';
  if (save?.state === 'dirty') return save.detail ? `Unsaved · ${save.detail}` : 'Editing…';
  if (save?.state === 'saving') return 'Saving…';
  if (save?.state === 'saved') return `Saved${save.detail ? ` · ${save.detail}` : ''}`;
  if (save?.detail) return save.detail;
  if (previous?.author === 'user' && step?.author !== 'user' && previousIndex >= 0) return `Step ${previousIndex + 1} was your edit`;
  return 'Editable';
}

// ── Status chip ──────────────────────────────────────────────────────────────
/**
 * One chip merges compile, sorry and SafeVerify. `kind` picks the colour and
 * the icon (never hue alone); `label` is the text; `line` is a jump target.
 * @param {{
 *   step?: any,
 *   historical?: boolean,
 *   running?: boolean,
 *   checking?: boolean,
 *   dirty?: boolean,
 *   verify?: VerifyLike | null,
 *   problems?: Problem[],
 *   sorries?: number[],
 * }} [args]
 * @returns {StatusChip}
 */
export function deriveStatusChip({
  step,
  historical = false,
  running = false,
  checking = false,
  dirty = false,
  verify = null,
  problems = [],
  sorries = [],
} = {}) {
  if (!step) return { kind: 'idle', label: 'No theorem yet', line: null };
  if (dirty) return { kind: 'unchecked', label: 'Editing…', line: null };
  if (checking) return { kind: 'running', label: 'Checking…', line: null };
  if (verify?.status === 'running') return { kind: 'running', label: 'Verifying…', line: null };

  const proof = deriveCodeStepProofStatus(step, { historical });
  const errors = problems.filter((p) => p.severity === 'error');
  const sorryCount = sorries.length;
  const sorryTail = sorryCount ? ` · ${sorryCount} ${sorryCount === 1 ? 'sorry' : 'sorries'}` : '';

  if (proof === 'failed') {
    const first = errors.find((p) => p.line != null) || problems.find((p) => p.line != null);
    const n = Math.max(errors.length, 1);
    const where = first?.line != null ? ` · line ${first.line}` : '';
    return { kind: 'error', label: `${n} ${n === 1 ? 'error' : 'errors'}${where}${sorryTail}`, line: first?.line ?? null };
  }
  if (proof === 'unchecked') {
    if (running) return { kind: 'running', label: 'Checking…', line: null };
    return { kind: 'unchecked', label: step.check_stale_reason ? 'Recheck required' : 'Not checked', line: null };
  }
  if (proof === 'stubbed') {
    return { kind: 'sorry', label: `Checked${sorryTail || ' · sorry'}`, line: sorries[0] ?? null };
  }
  const noun = proof === 'defined' ? 'Defined' : proof === 'checked' ? 'Checked' : 'Proved';
  if (verify?.status === 'ok') return { kind: 'verified', label: `${noun} · verified`, line: null };
  if (verify?.status === 'rejected') return { kind: 'error', label: 'Rejected by SafeVerify', line: null };
  if (verify?.status === 'error') return { kind: 'error', label: `${noun} · SafeVerify error`, line: null };
  // 'unavailable' / 'pending' say nothing about the proof: keep the compile verdict.
  return { kind: 'proved', label: `${noun} · checked`, line: null };
}

/**
 * The drawer tab a click on the chip opens.
 * @param {StatusChip} chip
 * @returns {DrawerTabName}
 */
export function chipTab(chip) {
  if (chip?.kind === 'error' || chip?.kind === 'sorry') return 'problems';
  if (chip?.kind === 'verified') return 'verify';
  return 'goals';
}

/**
 * Whether SafeVerify may run on the shown step, and why not.
 * @param {{
 *   hasStep: boolean,
 *   latest: boolean,
 *   running?: boolean,
 *   busy?: boolean,
 *   draftPending?: boolean,
 *   proof: string,
 * }} args
 * @returns {{ canRun: boolean, reason: string }}
 */
export function verifyAvailability({ hasStep, latest, running = false, busy = false, draftPending = false, proof }) {
  const ok = 'Kernel-audit the saved proof (SafeVerify)';
  if (!hasStep) return { canRun: false, reason: 'Nothing to verify yet' };
  if (running) return { canRun: false, reason: 'Wait for Lea to finish' };
  if (busy) return { canRun: false, reason: 'SafeVerify is running' };
  if (draftPending) return { canRun: false, reason: 'Save first — SafeVerify checks the saved file' };
  if (!latest) return { canRun: false, reason: 'Only the latest snapshot can be verified' };
  if (proof === 'failed') return { canRun: false, reason: 'Fix the errors first' };
  if (proof === 'stubbed') return { canRun: false, reason: 'Remove the sorries first' };
  if (proof === 'unchecked') return { canRun: false, reason: 'Run lean_check first' };
  return { canRun: true, reason: ok };
}

// ── Header ───────────────────────────────────────────────────────────────────
const DECL_RE = /^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*(theorem|lemma|def|instance|abbrev|structure|inductive|class|example)\s+([^\s:(\[{]+)/;

/**
 * The name to show over the crumbs: the first theorem/lemma, else the first
 * other declaration, else the file's base name without `.lean`.
 * @param {unknown} code
 * @param {string | null} [path]
 * @returns {string | null}
 */
export function theoremName(code, path) {
  if (typeof code === 'string' && code) {
    /** @type {string | null} */
    let fallback = null;
    for (const line of code.split('\n')) {
      const m = DECL_RE.exec(line);
      if (!m) continue;
      if (m[1] === 'theorem' || m[1] === 'lemma') return m[2];
      if (!fallback && m[1] !== 'example') fallback = m[2];
    }
    if (fallback) return fallback;
  }
  if (path) return String(path).split('/').pop()?.replace(/\.lean$/, '') || null;
  return null;
}

// ── Paths and the reveal-line contract ───────────────────────────────────────
/** @param {string | null | undefined} p */
function normalizePath(p) {
  return String(p ?? '').replace(/\\/g, '/').replace(/^\.\//, '').replace(/^\/+/, '');
}

/**
 * Repo-relative path equality, tolerant of a leading `./` or `/`.
 * @param {string | null | undefined} a
 * @param {string | null | undefined} b
 */
export function samePath(a, b) {
  if (!a || !b) return false;
  return normalizePath(a) === normalizePath(b);
}

/**
 * The line to reveal for a pending request (useWorkbench().pendingLine) once
 * the document at `docPath` is ready, or null when the request is for another
 * document. A request without a path applies to whatever is shown.
 * @param {{ line: number, path?: string | null } | null | undefined} pending
 * @param {string | null | undefined} docPath
 * @returns {number | null}
 */
export function revealTarget(pending, docPath) {
  if (!pending || !Number.isFinite(pending.line) || pending.line < 1) return null;
  if (pending.path && !samePath(pending.path, docPath)) return null;
  return Math.floor(pending.line);
}

// ── Drawer ───────────────────────────────────────────────────────────────────
/**
 * The newest lean_check execution in the status stream (for "lean_check · 1.2 s").
 * @param {any[]} [statusEvents]
 * @returns {{ ms: number, backend: string | null, status: string | null } | null}
 */
export function latestCheckTiming(statusEvents = []) {
  for (let i = statusEvents.length - 1; i >= 0; i -= 1) {
    const e = statusEvents[i];
    const total = e?.execution?.timings_ms?.total;
    if (Number.isFinite(total)) return { ms: total, backend: e.execution.backend ?? null, status: e.check_status ?? null };
  }
  return null;
}

/** @param {number | null | undefined} ms */
export function formatSeconds(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '';
  return ms >= 10_000 ? `${Math.round(ms / 1000)} s` : `${(ms / 1000).toFixed(1)} s`;
}

/**
 * The one-line goal shown in the collapsed drawer: the first `⊢` line, else
 * the first non-empty line, truncated.
 * @param {unknown} text
 * @param {number} [max]
 * @returns {string | null}
 */
export function goalHeadline(text, max = 60) {
  if (typeof text !== 'string') return null;
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return null;
  const goal = lines.find((l) => l.startsWith('⊢')) || lines[0];
  return goal.length > max ? `${goal.slice(0, max - 1)}…` : goal;
}

/**
 * The collapsed drawer's summary: "⊢ Even a · checking · 1 sorry".
 * @param {{ goal?: string | null, chip?: StatusChip | null, errors?: number, sorries?: number }} [args]
 */
export function drawerSummary({ goal = null, chip = null, errors = 0, sorries = 0 } = {}) {
  const parts = [];
  if (goal) parts.push(goal);
  if (chip?.kind === 'running') parts.push('checking');
  else if (chip?.kind === 'error' && errors > 0) parts.push(`${errors} ${errors === 1 ? 'error' : 'errors'}`);
  else if (chip?.kind === 'verified') parts.push('verified');
  else if (chip?.kind === 'proved') parts.push('proved');
  if (sorries > 0) parts.push(`${sorries} ${sorries === 1 ? 'sorry' : 'sorries'}`);
  return parts.join(' · ');
}
