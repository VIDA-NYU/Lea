// Pure derivations for the chat thread (docs/PLAN-ui-redesign.md, chat restyle).
// No React, no DOM: everything here runs under `node --test` (chatModel.test.mjs).
// JSDoc types are what TypeScript sees for this module (allowJs, checkJs off).
//
// What the run stream gives us to build tool rows from (adapter bridge.py):
//   status{status:'tool_call', message:'Running <name>', turn, created_at}
//       when the model issues a call. No arguments, no result.
//   status{status:'lean_check', check_status, check_detail, execution, turn}
//       when a lean_check finishes (check_detail = first error line only).
//   code_step  when a write/edit (or a bash that touched the proof) lands a .lean file.
//   approval_requested/approval_resolved  for gated tools, WITH the arguments.
// There is no generic "tool finished" event, so a bash / read / search call ends
// at the next thing the run does (see deriveToolCalls). A status event with
// status 'tool_result' (name, turn, created_at, ok?, exit_code?, hits?, preview?)
// is honoured if the adapter starts sending one; it makes those ends exact.
import { parseLeanProblems, sorryLines } from './canvasModel.js';

/**
 * @typedef {'write' | 'edit' | 'read' | 'check' | 'verify' | 'bash' | 'search' | 'session' | 'mcp' | 'custom'} ToolKind
 * @typedef {'file' | 'edit' | 'play' | 'shield' | 'terminal' | 'search' | 'blueprint' | 'tool'} ToolIcon
 * @typedef {'proved' | 'sorry' | 'error' | 'running' | 'waiting' | 'muted' | 'added'} Tone
 * @typedef {{ name: string, args?: Record<string, unknown> | null, target?: string | null }} CallLike
 * @typedef {{
 *   ok?: boolean | null, denied?: boolean, stopped?: boolean, queued?: boolean, waiting?: boolean,
 *   checkStatus?: string | null, checkDetail?: string | null, code?: string | null,
 *   previousCode?: string | null, added?: number, removed?: number,
 *   exitCode?: number | null, hits?: number | null, lines?: number | null,
 *   verifyStatus?: string | null, turn?: number | null, title?: string | null,
 *   label?: string | null, tone?: Tone | null, preview?: string | null
 * }} ResultLike
 * @typedef {{ kind: ToolKind, icon: ToolIcon, verb: string, target: string, result: string, tone: Tone, live: boolean, firstError: string | null }} ToolRowView
 */

// ── Elapsed time ───────────────────────────────────────────────────────────

/**
 * `420 ms` under a second, `12 s` under a minute, `1m 05s` under an hour, then
 * `1h 02m`. `fine` gives one decimal under ten seconds (`4.2 s`) for frozen
 * durations; a counter that ticks once a second wants whole seconds.
 * @param {number | null | undefined} ms
 * @param {{ fine?: boolean }} [options]
 */
export function formatElapsed(ms, { fine = false } = {}) {
  const v = typeof ms === 'number' && Number.isFinite(ms) && ms > 0 ? ms : 0;
  if (v < 1000) return `${Math.floor(v)} ms`;
  if (v < 60_000) {
    if (fine && v < 10_000) return `${(Math.floor(v / 100) / 10).toFixed(1)} s`;
    return `${Math.floor(v / 1000)} s`;
  }
  if (v < 3_600_000) {
    const m = Math.floor(v / 60_000);
    const s = Math.floor((v % 60_000) / 1000);
    return `${m}m ${String(s).padStart(2, '0')}s`;
  }
  const h = Math.floor(v / 3_600_000);
  const m = Math.floor((v % 3_600_000) / 60_000);
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

/** @param {unknown} iso @returns {number | null} */
export function parseTime(iso) {
  if (typeof iso === 'number') return Number.isFinite(iso) ? iso : null;
  if (typeof iso !== 'string' || !iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

// ── Line deltas ────────────────────────────────────────────────────────────

/** @param {unknown} code @returns {string[]} */
function splitLines(code) {
  if (typeof code !== 'string' || !code) return [];
  const lines = code.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/**
 * Lines added and removed going from `before` to `after` (LCS on lines, after
 * trimming the common head and tail). Past ~2M cells it falls back to a
 * multiset count, which is exact for appends and close for everything else.
 * @param {unknown} before
 * @param {unknown} after
 * @returns {{ added: number, removed: number }}
 */
export function lineDelta(before, after) {
  const a = splitLines(before);
  const b = splitLines(after);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;
  const x = a.slice(head, a.length - tail);
  const y = b.slice(head, b.length - tail);
  if (!x.length || !y.length) return { added: y.length, removed: x.length };
  if (x.length * y.length > 2_000_000) {
    /** @type {Map<string, number>} */
    const counts = new Map();
    for (const line of x) counts.set(line, (counts.get(line) ?? 0) + 1);
    let common = 0;
    for (const line of y) {
      const n = counts.get(line) ?? 0;
      if (n > 0) { common += 1; counts.set(line, n - 1); }
    }
    return { added: y.length - common, removed: x.length - common };
  }
  // One row of the LCS table at a time.
  let prev = new Array(y.length + 1).fill(0);
  for (let i = 1; i <= x.length; i += 1) {
    const row = new Array(y.length + 1).fill(0);
    for (let j = 1; j <= y.length; j += 1) {
      row[j] = x[i - 1] === y[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], row[j - 1]);
    }
    prev = row;
  }
  const lcs = prev[y.length];
  return { added: y.length - lcs, removed: x.length - lcs };
}

/** `+11`, `+3 −2`, `−4`, `no change`. @param {{ added: number, removed: number }} d */
export function formatDelta({ added, removed }) {
  if (!added && !removed) return 'no change';
  if (!removed) return `+${added}`;
  if (!added) return `−${removed}`;
  return `+${added} −${removed}`;
}

// ── Tool names and targets ─────────────────────────────────────────────────

/** The tool a `tool_call` status event names. @param {{ tool?: unknown, message?: unknown }} event */
export function toolNameOf(event) {
  if (typeof event?.tool === 'string' && event.tool) return event.tool;
  const m = typeof event?.message === 'string' ? event.message : '';
  return m.replace(/^Running\s+/, '').trim() || 'tool';
}

/** @param {string} name @returns {ToolKind} */
export function toolKind(name) {
  const n = String(name || '').toLowerCase();
  if (n === 'write_file') return 'write';
  if (n === 'edit_file') return 'edit';
  if (n === 'read_file') return 'read';
  if (n === 'lean_check') return 'check';
  if (n === 'safe_verify' || n === 'safeverify' || n === 'verify') return 'verify';
  if (n === 'bash') return 'bash';
  if (n === 'search_mathlib' || n === 'suggest_imports') return 'search';
  if (n === 'spawn_subagent' || n === 'session') return 'session';
  if (n.includes('__')) return 'mcp';
  return 'custom';
}

/** @type {Record<ToolKind, ToolIcon>} */
const ICON = {
  write: 'file', read: 'file', edit: 'edit', check: 'play', verify: 'shield',
  bash: 'terminal', search: 'search', session: 'blueprint', mcp: 'tool', custom: 'tool',
};

/** @param {string} name @param {ToolKind} kind */
function verbOf(name, kind) {
  switch (kind) {
    case 'write': return 'write';
    case 'edit': return 'edit';
    case 'read': return 'read';
    case 'check': return 'lean_check';
    case 'verify': return 'verify';
    case 'bash': return 'bash';
    case 'search': return name === 'suggest_imports' ? 'imports' : 'search';
    case 'session': return 'session';
    case 'mcp': return name.slice(name.indexOf('__') + 2) || name;
    default: return name || 'tool';
  }
}

/** @param {unknown} p */
export function baseName(p) {
  if (typeof p !== 'string' || !p) return '';
  const clean = p.replace(/\\/g, '/').replace(/\/+$/, '');
  return clean.slice(clean.lastIndexOf('/') + 1);
}

/** @param {unknown} text @param {number} [max] */
export function firstLine(text, max = 80) {
  if (typeof text !== 'string') return '';
  const line = text.split('\n').map((l) => l.trim()).find(Boolean) || '';
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

/** @param {Record<string, unknown> | null | undefined} args @param {string[]} keys */
function pick(args, keys) {
  for (const k of keys) {
    const v = args?.[k];
    if (typeof v === 'string' && v.trim()) return v;
  }
  return '';
}

/**
 * The mono part of the row: a file name, a command, a query.
 * @param {string} name
 * @param {Record<string, unknown> | null | undefined} args
 */
export function toolTarget(name, args) {
  const kind = toolKind(name);
  if (!args || typeof args !== 'object') return kind === 'mcp' ? name.slice(0, name.indexOf('__')) : '';
  switch (kind) {
    case 'write': case 'edit': case 'read': case 'check': case 'verify':
      return baseName(pick(args, ['path', 'file', 'file_path', 'filename']));
    case 'bash':
      return firstLine(pick(args, ['command', 'cmd']), 80);
    case 'search':
      return firstLine(pick(args, ['query', 'q', 'pattern', 'name']), 60);
    case 'session':
      return firstLine(pick(args, ['title', 'node', 'subagent_type', 'task']), 60);
    default: {
      const s = Object.values(args).find((v) => typeof v === 'string' && v.trim());
      const first = firstLine(s, 60);
      if (first) return first;
      return kind === 'mcp' ? name.slice(0, name.indexOf('__')) : '';
    }
  }
}

// ── The row ────────────────────────────────────────────────────────────────

/** @param {number} n @param {string} one @param {string} [many] */
function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * Error count when the detail is a real compiler transcript; `null` when it is
 * the single "first error line" the adapter stores (the count is then unknown).
 * @param {unknown} detail
 */
export function errorCount(detail) {
  if (typeof detail !== 'string' || !detail.trim()) return null;
  if (!detail.trim().includes('\n')) return null;
  const n = parseLeanProblems(detail).filter((p) => p.severity === 'error').length;
  return n > 0 ? n : null;
}

/** The first error line, for a failed row's inline note. @param {unknown} detail */
export function firstErrorLine(detail) {
  if (typeof detail !== 'string') return null;
  const lines = detail.split('\n').map((l) => l.trim()).filter(Boolean);
  const line = lines.find((l) => /\berror\b/i.test(l)) || lines[0];
  if (!line) return null;
  return line.length > 200 ? `${line.slice(0, 199)}…` : line;
}

/**
 * `{icon, verb, target, result, tone}` for one tool call. `result` null means
 * the call is still in flight (the row is live; its counter fills the right).
 * @param {CallLike} call
 * @param {ResultLike | null} result
 * @returns {ToolRowView}
 */
export function toolRowModel(call, result) {
  const name = call?.name || 'tool';
  const kind = toolKind(name);
  const args = call?.args ?? null;
  const view = {
    kind,
    icon: ICON[kind],
    verb: verbOf(name, kind),
    target: call?.target || toolTarget(name, args),
    result: '',
    /** @type {Tone} */ tone: 'muted',
    live: false,
    /** @type {string | null} */ firstError: null,
  };
  if (!result) return { ...view, live: true, tone: 'running' };
  if (result.waiting) return { ...view, result: 'needs approval', tone: 'waiting' };
  if (result.queued) return { ...view, result: 'queued', tone: 'muted' };
  if (result.denied) return { ...view, result: 'denied', tone: 'error' };
  if (result.label) return { ...view, result: result.label, tone: result.tone || 'muted' };

  if (kind === 'check' && result.checkStatus) {
    if (result.checkStatus === 'error') {
      const n = errorCount(result.checkDetail);
      return { ...view, result: n ? plural(n, 'error') : 'failed', tone: 'error', firstError: firstErrorLine(result.checkDetail) };
    }
    const sorries = sorryLines(result.code ?? '').length;
    if (sorries) return { ...view, result: `${sorries} sorry`, tone: 'sorry' };
    return { ...view, result: '0 errors', tone: 'proved' };
  }
  if (kind === 'verify' && result.verifyStatus) {
    if (result.verifyStatus === 'ok') return { ...view, result: 'passed', tone: 'proved' };
    if (result.verifyStatus === 'rejected') return { ...view, result: 'rejected', tone: 'error' };
    return { ...view, result: result.verifyStatus, tone: result.verifyStatus === 'error' ? 'error' : 'muted' };
  }
  if (result.stopped) return { ...view, result: 'stopped', tone: 'muted' };
  if (kind === 'write' || kind === 'edit') {
    const delta = typeof result.added === 'number' || typeof result.removed === 'number'
      ? { added: result.added ?? 0, removed: result.removed ?? 0 }
      : typeof result.code === 'string'
      ? lineDelta(result.previousCode ?? '', result.code)
      : null;
    if (result.ok === false) return { ...view, result: 'failed', tone: 'error', firstError: firstErrorLine(result.preview) };
    if (delta) return { ...view, result: formatDelta(delta), tone: delta.removed ? 'muted' : delta.added ? 'added' : 'muted' };
    return view;
  }
  if (kind === 'bash' && typeof result.exitCode === 'number') {
    return { ...view, result: `exit ${result.exitCode}`, tone: result.exitCode === 0 ? 'muted' : 'error',
      firstError: result.exitCode === 0 ? null : firstErrorLine(result.preview) };
  }
  if (kind === 'search' && typeof result.hits === 'number') {
    return { ...view, result: plural(result.hits, 'hit'), tone: 'muted' };
  }
  if (kind === 'read' && typeof result.lines === 'number') {
    return { ...view, result: plural(result.lines, 'line'), tone: 'muted' };
  }
  if (kind === 'session') {
    if (typeof result.turn === 'number') return { ...view, result: `turn ${result.turn}`, tone: 'running' };
  }
  if (result.ok === false) return { ...view, result: 'error', tone: 'error', firstError: firstErrorLine(result.preview) };
  return view;
}

// ── Pairing calls with what they produced ──────────────────────────────────

/**
 * @typedef {{ id?: string, run_id?: string | null, status?: string | null, message?: string, turn?: number | null,
 *   created_at?: string, check_status?: string | null, check_detail?: string | null, tool?: string,
 *   args?: Record<string, unknown> | null, ok?: boolean | null, exit_code?: number | null, hits?: number | null,
 *   lines?: number | null, preview?: string | null, execution?: any }} StatusLike
 * @typedef {{ id: string, run_id?: string | null, turn?: number | null, author?: string, path: string,
 *   code?: string, created_at?: string, check_status?: string | null, check_detail?: string | null }} StepLike
 * @typedef {{ approval_id: string, run_id?: string | null, tool_name: string, args?: Record<string, unknown> | null,
 *   decision?: string | null, actionable?: boolean, created_at?: string, resolved_at?: string | null, seq?: number }} ApprovalLike
 * @typedef {{ id?: string, run_id?: string | null, role?: string, created_at?: string }} MessageLike
 * @typedef {{ id?: string, run_id?: string | null, created_at?: string }} DiagnosticLike
 * @typedef {'live' | 'queued' | 'waiting' | 'done' | 'denied' | 'stopped' | 'unknown'} CallState
 * @typedef {{
 *   key: string, runId: string | null, turn: number | null, name: string, kind: ToolKind, order: number,
 *   calledAt: number | null, startedAt: number | null, endedAt: number | null, exact: boolean,
 *   state: CallState, args: Record<string, unknown> | null,
 *   approval: ApprovalLike | null, check: StatusLike | null, step: StepLike | null, result: StatusLike | null,
 *   checkedStep: StepLike | null, synthetic: boolean
 * }} ToolCall
 */

const WRITERS = new Set(['write_file', 'edit_file']);

/** @param {StatusLike} e */
function isCallEvent(e) { return e?.status === 'tool_call'; }

/** @param {unknown} a @param {unknown} b */
function samePathLoose(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || !a || !b) return true;
  const x = a.replace(/\\/g, '/');
  const y = b.replace(/\\/g, '/');
  return x === y || x.endsWith(`/${y}`) || y.endsWith(`/${x}`);
}

/**
 * Every tool call the run stream reported, paired with what it produced, in
 * the order they were issued. Pairing, per run:
 *   lean_check status → the earliest unpaired lean_check call before it (same turn when known);
 *   approval → the earliest unpaired call of that tool before it (gives the arguments);
 *   agent code step → the earliest unpaired write/edit call before it (same turn, same path);
 *   tool_result status (if the adapter sends one) → the earliest unpaired call of that tool.
 * Tools run one after another inside a turn, so a call starts when it was issued,
 * its approval was granted, or the previous call of its turn ended, whichever is
 * last. A call with no paired result ends at the next thing the run did (an
 * inexact end that may include the model's time to respond).
 *
 * @param {{
 *   statusEvents?: StatusLike[], codeSteps?: StepLike[], approvals?: ApprovalLike[],
 *   messages?: MessageLike[], diagnostics?: DiagnosticLike[],
 *   liveRunId?: string | null, runStatusById?: Record<string, string | null | undefined>,
 * }} input
 * @returns {{ calls: ToolCall[], pairedStepIds: Set<string>, pairedApprovalIds: Set<string> }}
 */
export function deriveToolCalls({
  statusEvents = [], codeSteps = [], approvals = [], messages = [], diagnostics = [],
  liveRunId = null, runStatusById = {},
} = {}) {
  /** @type {ToolCall[]} */
  const calls = [];
  const pairedStepIds = new Set();
  const pairedApprovalIds = new Set();
  let order = 0;

  /** @type {Map<string, StatusLike[]>} */
  const eventsByRun = new Map();
  statusEvents.forEach((e) => {
    const run = e?.run_id ?? '';
    if (!eventsByRun.has(run)) eventsByRun.set(run, []);
    eventsByRun.get(run)?.push(e);
  });

  for (const [run, events] of eventsByRun) {
    const runId = run || null;
    const sorted = events
      .map((e, i) => ({ e, i, t: parseTime(e.created_at) }))
      .sort((p, q) => ((p.t ?? 0) - (q.t ?? 0)) || (p.i - q.i));
    /** @type {ToolCall[]} */
    const runCalls = [];
    const newCall = (/** @type {string} */ name, /** @type {number | null} */ t, /** @type {number | null} */ turn, /** @type {string} */ key, synthetic = false, /** @type {StatusLike | null} */ event = null) => {
      /** @type {ToolCall} */
      const call = {
        key, runId, turn, name, kind: toolKind(name), order: order++,
        calledAt: t, startedAt: t, endedAt: null, exact: false, state: 'unknown',
        args: event?.args && typeof event.args === 'object' ? event.args : null,
        approval: null, check: null, step: null, result: null, checkedStep: null, synthetic,
      };
      runCalls.push(call);
      return call;
    };
    const before = (/** @type {ToolCall} */ c, /** @type {number | null} */ t) => c.calledAt == null || t == null || c.calledAt <= t;
    const sameTurn = (/** @type {ToolCall} */ c, /** @type {number | null | undefined} */ turn) => c.turn == null || turn == null || c.turn === turn;

    for (const { e, t, i } of sorted) {
      if (isCallEvent(e)) {
        newCall(toolNameOf(e), t, e.turn ?? null, `tc:${e.id ?? `${run}:${i}`}`, false, e);
      } else if (e.status === 'lean_check') {
        const call = runCalls.find((c) => c.name === 'lean_check' && !c.check && before(c, t) && sameTurn(c, e.turn))
          ?? runCalls.find((c) => c.name === 'lean_check' && !c.check && before(c, t));
        (call ?? newCall('lean_check', t, e.turn ?? null, `lc:${e.id ?? `${run}:${i}`}`, true)).check = e;
      } else if (e.status === 'tool_result') {
        const name = toolNameOf({ tool: e.tool, message: (e.message || '').replace(/^Finished\s+/, '') });
        const call = runCalls.find((c) => c.name === name && !c.result && before(c, t) && sameTurn(c, e.turn));
        if (call) call.result = e;
      }
    }

    const runApprovals = approvals
      .filter((a) => (a.run_id ?? '') === run)
      .map((a, i) => ({ a, i, t: parseTime(a.created_at) }))
      .sort((p, q) => ((p.t ?? Infinity) - (q.t ?? Infinity)) || (p.i - q.i));
    for (const { a, t } of runApprovals) {
      const call = runCalls.find((c) => !c.approval && c.name === a.tool_name && before(c, t));
      if (!call) continue;
      call.approval = a;
      if (!call.args && a.args && typeof a.args === 'object') call.args = a.args;
      pairedApprovalIds.add(a.approval_id);
    }

    const runSteps = codeSteps
      .filter((s) => (s.run_id ?? '') === run && run && s.author !== 'user')
      .map((s, i) => ({ s, i, t: parseTime(s.created_at) }))
      .sort((p, q) => ((p.t ?? 0) - (q.t ?? 0)) || (p.i - q.i));
    for (const { s, t } of runSteps) {
      const call = runCalls.find((c) => WRITERS.has(c.name) && !c.step && before(c, t) && sameTurn(c, s.turn)
        && samePathLoose(typeof c.args?.path === 'string' ? c.args.path : null, s.path));
      if (!call) continue;
      call.step = s;
      pairedStepIds.add(s.id);
    }

    // A check names no file: it checked the newest snapshot that existed then.
    const allSteps = codeSteps
      .map((s, i) => ({ s, i, t: parseTime(s.created_at) }))
      .sort((p, q) => ((p.t ?? 0) - (q.t ?? 0)) || (p.i - q.i));
    for (const c of runCalls) {
      if (!c.check) continue;
      const t = parseTime(c.check.created_at);
      if (t == null) continue;
      let found = null;
      for (const { s, t: st } of allSteps) {
        if (st != null && st <= t) found = s;
      }
      c.checkedStep = found;
    }

    // Everything the run did, as times, to end calls that have no paired result.
    /** @type {{ t: number, owner: ToolCall | null, callTurn: number | null | undefined }[]} */
    const evidence = [];
    const owners = new Map();
    for (const c of runCalls) {
      if (c.check) owners.set(c.check, c);
      if (c.step) owners.set(c.step, c);
      if (c.result) owners.set(c.result, c);
      if (c.approval) owners.set(c.approval, c);
    }
    for (const { e, t } of sorted) {
      if (t == null || e.status === 'queued') continue;
      evidence.push({ t, owner: owners.get(e) ?? null, callTurn: isCallEvent(e) ? (e.turn ?? null) : undefined });
    }
    for (const m of messages) {
      const t = parseTime(m.created_at);
      if (t != null && (m.run_id ?? '') === run) evidence.push({ t, owner: null, callTurn: undefined });
    }
    for (const s of codeSteps) {
      const t = parseTime(s.created_at);
      if (t != null && (s.run_id ?? '') === run) evidence.push({ t, owner: owners.get(s) ?? null, callTurn: undefined });
    }
    for (const a of approvals) {
      if ((a.run_id ?? '') !== run) continue;
      for (const t of [parseTime(a.created_at), parseTime(a.resolved_at)]) {
        if (t != null) evidence.push({ t, owner: owners.get(a) ?? null, callTurn: undefined });
      }
    }
    for (const d of diagnostics) {
      const t = parseTime(d.created_at);
      if (t != null && (d.run_id ?? '') === run) evidence.push({ t, owner: null, callTurn: undefined });
    }
    evidence.sort((p, q) => p.t - q.t);

    const live = Boolean(runId) && runId === liveRunId;
    const settledStatus = runId ? runStatusById[runId] : undefined;
    /** @type {Map<string, ToolCall>} */
    const lastInTurn = new Map();
    for (const c of runCalls) {
      const turnKey = c.turn == null ? `solo:${c.key}` : `t:${c.turn}`;
      const previous = lastInTurn.get(turnKey) ?? null;
      lastInTurn.set(turnKey, c);
      const approved = c.approval?.decision && c.approval.decision !== 'deny' ? parseTime(c.approval.resolved_at) : null;
      c.startedAt = Math.max(c.calledAt ?? -Infinity, approved ?? -Infinity, previous?.endedAt ?? -Infinity);
      if (!Number.isFinite(c.startedAt)) c.startedAt = null;

      if (c.approval && c.approval.decision === 'deny') {
        c.state = 'denied';
        c.endedAt = parseTime(c.approval.resolved_at) ?? c.startedAt;
        c.exact = true;
        continue;
      }
      if (c.approval && !c.approval.decision && c.approval.actionable !== false && live) {
        c.state = 'waiting';
        continue;
      }
      if (previous && previous.endedAt == null && live) {
        c.state = 'queued';
        continue;
      }
      const exactEnd = c.check ? parseTime(c.check.created_at)
        : c.result ? parseTime(c.result.created_at)
        : c.step ? parseTime(c.step.created_at)
        : null;
      if (exactEnd != null) {
        c.endedAt = exactEnd;
        c.exact = true;
        c.state = 'done';
        continue;
      }
      if (c.check || c.result || c.step) {
        c.state = 'done';
        continue;
      }
      const from = c.startedAt ?? c.calledAt;
      const next = from == null ? undefined : evidence.find((ev) => ev.t > from && ev.owner !== c
        && !(ev.callTurn !== undefined && c.turn != null && ev.callTurn === c.turn));
      if (next) {
        c.endedAt = next.t;
        c.state = 'done';
      } else if (live) {
        c.state = 'live';
      } else {
        c.state = settledStatus === 'cancelled' ? 'stopped' : 'unknown';
      }
    }
    calls.push(...runCalls);
  }
  calls.sort((p, q) => ((p.calledAt ?? 0) - (q.calledAt ?? 0)) || (p.order - q.order));
  return { calls, pairedStepIds, pairedApprovalIds };
}

/**
 * The display result for a derived call (null while it is in flight).
 * @param {ToolCall} call
 * @param {StepLike | null} [previousStep]  the step before `call.step` for the same path
 * @returns {ResultLike | null}
 */
export function callResult(call, previousStep = null) {
  switch (call.state) {
    case 'live': return null;
    case 'waiting': return { waiting: true };
    case 'queued': return { queued: true };
    case 'denied': return { denied: true };
    default: break;
  }
  if (call.check) {
    return { checkStatus: call.check.check_status ?? null, checkDetail: call.check.check_detail ?? null, code: call.checkedStep?.code ?? null };
  }
  if (call.result) {
    const r = call.result;
    return { ok: r.ok ?? null, exitCode: r.exit_code ?? null, hits: r.hits ?? null, lines: r.lines ?? null, preview: r.preview ?? null,
      stopped: call.state === 'stopped' };
  }
  if (call.step && typeof call.step.code === 'string') {
    return { code: call.step.code, previousCode: previousStep?.code ?? '' };
  }
  if ((call.kind === 'write' || call.kind === 'edit') && call.args) {
    const args = call.args;
    if (call.kind === 'write' && typeof args.content === 'string') return { added: splitLines(args.content).length, removed: 0, stopped: call.state === 'stopped' };
    if (call.kind === 'edit' && (typeof args.old_string === 'string' || typeof args.new_string === 'string')) {
      return { ...lineDelta(args.old_string ?? '', args.new_string ?? ''), stopped: call.state === 'stopped' };
    }
  }
  return { stopped: call.state === 'stopped' };
}

/**
 * Code step id → the previous step for the same path (in pane order), for
 * the `+a −b` of a write. @param {StepLike[]} sortedSteps
 */
export function previousStepByPath(sortedSteps = []) {
  /** @type {Map<string, StepLike>} */
  const last = new Map();
  /** @type {Map<string, StepLike | null>} */
  const out = new Map();
  for (const s of sortedSteps) {
    out.set(s.id, last.get(s.path) ?? null);
    last.set(s.path, s);
  }
  return out;
}

// ── Approvals ──────────────────────────────────────────────────────────────

/**
 * "Lea wants to …" for a pending approval.
 * @param {{ tool_name: string, args?: Record<string, unknown> | null }} approval
 */
export function approvalAsk(approval) {
  const name = approval?.tool_name || 'tool';
  const args = approval?.args ?? {};
  const kind = toolKind(name);
  const file = baseName(pick(args, ['path', 'file', 'file_path']));
  if (kind === 'bash') return 'Lea wants to run a shell command';
  if (kind === 'write') {
    const n = typeof args.content === 'string' ? splitLines(args.content).length : null;
    return `Lea wants to write ${file || 'a file'}${n != null ? ` (${plural(n, 'line')})` : ''}`;
  }
  if (kind === 'edit') {
    const d = typeof args.old_string === 'string' || typeof args.new_string === 'string'
      ? lineDelta(args.old_string ?? '', args.new_string ?? '') : null;
    return `Lea wants to edit ${file || 'a file'}${d ? ` (${formatDelta(d)})` : ''}`;
  }
  return `Lea wants to use ${name}`;
}

/** @param {string | null | undefined} decision */
export function decisionLabel(decision) {
  if (decision === 'allow') return 'approved once';
  if (decision === 'always_session') return 'approved for this session';
  if (decision === 'deny') return 'denied';
  return decision ? 'resolved' : '';
}

// ── The live run ───────────────────────────────────────────────────────────

/**
 * Where the live run stands: when it started, when it last did something the
 * user could see, which model turn it is on, and whether text is streaming.
 * `seenAt` maps message id → when the UI last saw its text grow (a streamed
 * message's created_at is when it STARTED).
 * @param {{
 *   runId: string | null | undefined, calls?: ToolCall[],
 *   messages?: (MessageLike & { content?: string, live?: boolean, stream_state?: string })[],
 *   codeSteps?: StepLike[], statusEvents?: StatusLike[], approvals?: ApprovalLike[],
 *   seenAt?: Record<string, number>, fallbackStart?: number | null,
 * }} input
 */
export function runClock({ runId, calls = [], messages = [], codeSteps = [], statusEvents = [], approvals = [], seenAt = {}, fallbackStart = null }) {
  const run = runId ?? '';
  /** @type {number | null} */
  let startedAt = null;
  /** @type {number | null} */
  let last = null;
  /** @type {number | null} */
  let streamingSince = null;
  let streaming = false;
  const bump = (/** @type {number | null} */ t) => { if (t != null && (last == null || t > last)) last = t; };
  const first = (/** @type {number | null} */ t) => { if (t != null && (startedAt == null || t < startedAt)) startedAt = t; };
  for (const m of messages) {
    if ((m.run_id ?? '') !== run) continue;
    const t = parseTime(m.created_at);
    if (m.role === 'user') first(t);
    const seen = m.id ? seenAt[m.id] : undefined;
    bump(seen != null && (t == null || seen > t) ? seen : t);
    if ((m.live || m.stream_state === 'streaming') && (m.content || '').trim()) {
      streaming = true;
      const at = seen ?? t;
      if (at != null && (streamingSince == null || at > streamingSince)) streamingSince = at;
    }
  }
  if (startedAt == null) {
    for (const e of statusEvents) if ((e.run_id ?? '') === run) first(parseTime(e.created_at));
    for (const s of codeSteps) if ((s.run_id ?? '') === run) first(parseTime(s.created_at));
  }
  if (startedAt == null && fallbackStart != null) startedAt = fallbackStart;
  for (const e of statusEvents) if ((e.run_id ?? '') === run && e.status !== 'queued') bump(parseTime(e.created_at));
  for (const s of codeSteps) if ((s.run_id ?? '') === run) bump(parseTime(s.created_at));
  for (const a of approvals) {
    if ((a.run_id ?? '') !== run) continue;
    bump(parseTime(a.created_at));
    bump(parseTime(a.resolved_at));
  }
  const runCalls = calls.filter((c) => (c.runId ?? '') === run);
  for (const c of runCalls) if (c.exact) bump(c.endedAt);
  bump(startedAt);

  let turn = 1;
  const turns = runCalls.map((c) => c.turn).filter((t) => typeof t === 'number');
  if (turns.length) {
    const lastTurn = Math.max(.../** @type {number[]} */ (turns));
    const open = runCalls.some((c) => c.turn === lastTurn && c.endedAt == null && c.state !== 'stopped' && c.state !== 'unknown');
    turn = open ? lastTurn : lastTurn + 1;
  }
  const busy = runCalls.some((c) => c.state === 'live' || c.state === 'waiting' || c.state === 'queued');
  return { startedAt, lastActivityAt: last, turn, streaming, streamingSince, busy };
}

/**
 * The composer's status line.
 * @param {{
 *   live: boolean, pending?: boolean, stopRequested?: boolean, waitingApproval?: boolean,
 *   proposalInReview?: boolean, turn?: number | null, startedAt?: number | null, subagentsRunning?: number,
 *   outcome?: { status?: string | null, resultKind?: string | null, stopReason?: string | null,
 *     proof?: string | null, sorries?: number, errors?: number | null } | null,
 * }} input
 * @returns {{ tone: Tone | 'stopping', label: string, since: number | null, stop: boolean } | null}
 */
export function composerStatus({ live, pending = false, stopRequested = false, waitingApproval = false,
  proposalInReview = false, turn = null, startedAt = null, subagentsRunning = 0, outcome = null }) {
  if (live) {
    if (stopRequested) return { tone: 'stopping', label: 'Stopping… waiting for the current step', since: null, stop: false };
    if (waitingApproval) return { tone: 'waiting', label: 'Waiting for your approval', since: null, stop: true };
    if (pending) return { tone: 'running', label: 'Waiting for a free run slot', since: startedAt, stop: true };
    const parts = ['Lea is working'];
    if (turn) parts.push(`turn ${turn}`);
    if (subagentsRunning) parts.push(plural(subagentsRunning, 'sub-agent'));
    return { tone: 'running', label: parts.join(' · '), since: startedAt, stop: true };
  }
  if (proposalInReview) return { tone: 'waiting', label: 'Proposal waiting for your review', since: null, stop: false };
  if (!outcome?.status) return null;
  const { status, resultKind, stopReason, proof, sorries = 0, errors = null } = outcome;
  if (status === 'cancelled') {
    return { tone: 'muted', label: stopReason === 'timeout' ? 'Stopped · time limit' : 'Stopped', since: null, stop: false };
  }
  if (status === 'max_turns') return { tone: 'error', label: 'Stopped · turn limit', since: null, stop: false };
  if (status === 'disproved') return { tone: 'sorry', label: 'Done · disproved', since: null, stop: false };
  if (proof === 'failed') return { tone: 'error', label: `Done · ${errors ? plural(errors, 'error') : 'check failed'}`, since: null, stop: false };
  if (status === 'failed') return { tone: 'error', label: 'Run failed', since: null, stop: false };
  if (status === 'proved' || status === 'success') {
    if (proof === 'stubbed') return { tone: 'sorry', label: `Done · ${sorries || 1} sorry`, since: null, stop: false };
    if (resultKind === 'defined' || proof === 'defined') return { tone: 'proved', label: 'Done · defined', since: null, stop: false };
    if (proof === 'proved') return { tone: 'proved', label: 'Done · proved', since: null, stop: false };
  }
  if (proof === 'stubbed') return { tone: 'sorry', label: `Done · ${sorries || 1} sorry`, since: null, stop: false };
  return { tone: 'muted', label: 'Done', since: null, stop: false };
}

// ── User bubbles ───────────────────────────────────────────────────────────

const CONTEXT_RE = /^\[File in context: (.+?) · (ask about this file|prove the sorries here)\][ \t]*(?:\r?\n)*/;
const SLASH_RE = /^\/([a-zA-Z][\w-]*)(?=\s|$)([^\n]*)(?:\n([\s\S]*))?$/;
const BLUEPRINT_DRAFT = 'Draft the proof blueprint for this project';
const BLUEPRINT_NODE_RE = /^Prove the blueprint node `([^`]+)`/;

/**
 * Splits a user message into what the bubble shows: a leading slash command
 * (or the prompt a `/blueprint` command expanded into) as a chip, the
 * `[File in context: …]` header App prepends as a chip under the bubble, and
 * the text itself.
 * @param {unknown} content
 * @returns {{
 *   command: string | null, prompt: string | null, body: string,
 *   context: { path: string, line: number | null, label: string | null, intent: 'ask' | 'prove-sorries', chip: string, note: string } | null
 * }}
 */
export function userBubbleModel(content) {
  let text = typeof content === 'string' ? content : '';
  /** @type {ReturnType<typeof userBubbleModel>['context']} */
  let context = null;
  const ctx = text.match(CONTEXT_RE);
  if (ctx) {
    const where = ctx[1];
    const labelMatch = where.match(/^(.*) \((.+)\)$/);
    const loc = labelMatch ? labelMatch[1] : where;
    const label = labelMatch ? labelMatch[2] : null;
    const lineMatch = loc.match(/^(.*):(\d+)$/);
    const path = lineMatch ? lineMatch[1] : loc;
    const line = lineMatch ? Number(lineMatch[2]) : null;
    const name = label || baseName(path) || path;
    const intent = ctx[2] === 'prove the sorries here' ? 'prove-sorries' : 'ask';
    context = { path, line, label, intent, chip: line ? `${name}:${line}` : name, note: intent === 'prove-sorries' ? 'prove the sorries' : 'in context' };
    text = text.slice(ctx[0].length);
  }
  const trimmed = text.replace(/^\s+/, '');
  const slash = trimmed.match(SLASH_RE);
  if (slash) {
    return { command: `/${slash[1]}${slash[2].trimEnd()}`, prompt: null, body: (slash[3] ?? '').trim(), context };
  }
  if (trimmed.startsWith(BLUEPRINT_DRAFT)) {
    return { command: '/blueprint', prompt: trimmed, body: '', context };
  }
  const node = trimmed.match(BLUEPRINT_NODE_RE);
  if (node) {
    return { command: `/blueprint prove ${node[1]}`, prompt: trimmed, body: '', context };
  }
  return { command: null, prompt: null, body: text, context };
}
