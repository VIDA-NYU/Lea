import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  caretLine,
  changedLines,
  chipTab,
  deriveStatusChip,
  drawerSummary,
  editorNote,
  errorLines,
  formatSeconds,
  goalHeadline,
  isFreshFile,
  latestCheckTiming,
  parseLeanProblems,
  problemsForStep,
  revealTarget,
  samePath,
  sorryLines,
  stepDelta,
  stepLabel,
  theoremName,
  timelineDots,
  verifyAvailability,
  withSorryWarnings,
} from './lib/canvasModel.js';

const DETAIL = `proofs/Sqrt2.lean:15:33: error: omega could not prove the goal
a b : ℤ
key : a ^ 2 = 2 * b ^ 2
⊢ a ^ 2 = b ^ 2 + b ^ 2
proofs/Sqrt2.lean:19:2: warning: declaration uses 'sorry'`;

test('parseLeanProblems reads headers and keeps continuation lines in the message', () => {
  const problems = parseLeanProblems(DETAIL);
  assert.equal(problems.length, 2);
  assert.deepEqual(
    { ...problems[0], message: problems[0].message.split('\n')[0] },
    { path: 'proofs/Sqrt2.lean', line: 15, column: 33, severity: 'error', message: 'omega could not prove the goal' },
  );
  assert.match(problems[0].message, /⊢ a \^ 2 = b \^ 2 \+ b \^ 2$/);
  assert.equal(problems[1].severity, 'warning');
  assert.equal(problems[1].line, 19);
});

test('parseLeanProblems handles a headerless error and the OK text', () => {
  const bare = parseLeanProblems('error: lake build failed for Sqrt2 (exit 1):\nsome output');
  assert.equal(bare.length, 1);
  assert.equal(bare[0].line, null);
  assert.match(bare[0].message, /^lake build failed/);
  assert.match(bare[0].message, /some output$/);
  assert.deepEqual(parseLeanProblems('OK — no errors, no warnings.'), []);
  assert.deepEqual(parseLeanProblems(null), []);
});

test('problemsForStep falls back to one error when a failed check has no header', () => {
  const [p] = problemsForStep({ path: 'a.lean', check_status: 'error', check_detail: 'lean_check timed out' });
  assert.equal(p.severity, 'error');
  assert.equal(p.line, null);
  assert.equal(p.message, 'lean_check timed out');
  assert.deepEqual(problemsForStep({ check_status: 'ok', check_detail: null }), []);
  assert.deepEqual(problemsForStep(null), []);
});

test('withSorryWarnings adds a warning per sorry line only when the check did not report one', () => {
  const added = withSorryWarnings([], [20], 'a.lean');
  assert.equal(added.length, 1);
  assert.equal(added[0].line, 20);
  assert.equal(added[0].synthetic, true);
  const reported = withSorryWarnings([{ line: 19, column: 2, severity: 'warning', message: "declaration uses 'sorry'" }], [20]);
  assert.equal(reported.length, 1);
  // sorted by line, position-less problems last
  const sorted = withSorryWarnings([{ line: null, severity: 'error', message: 'x' }, { line: 3, column: 1, severity: 'error', message: 'y' }], []);
  assert.deepEqual(sorted.map((p) => p.line), [3, null]);
});

test('errorLines lists distinct error lines in order', () => {
  assert.deepEqual(
    errorLines([
      { severity: 'error', line: 9 },
      { severity: 'warning', line: 2 },
      { severity: 'error', line: 4 },
      { severity: 'error', line: 9 },
      { severity: 'error', line: null },
    ]),
    [4, 9],
  );
});

test('sorryLines ignores comments and finds sorry/admit lines', () => {
  const code = [
    'theorem t : True := by', // 1
    '  -- sorry in a line comment', // 2
    '  /- sorry in a block', // 3
    '     comment -/ trivial', // 4
    '  sorry', // 5
    'theorem u : True := by admit', // 6
    '  have : sorrydone := rfl', // 7 (not a word match)
  ].join('\n');
  assert.deepEqual(sorryLines(code), [5, 6]);
  assert.deepEqual(sorryLines(''), []);
  assert.deepEqual(sorryLines(undefined), []);
});

const rows = [
  { kind: 'unchanged', line: 'a', oldLineNumber: 1, newLineNumber: 1 },
  { kind: 'removed', line: 'b', oldLineNumber: 2 },
  { kind: 'added', line: 'c', newLineNumber: 2 },
  { kind: 'added', line: 'd', newLineNumber: 3 },
  { kind: 'removed', line: 'e', oldLineNumber: 3 },
];

test('changedLines, stepDelta and caretLine derive from diff rows', () => {
  assert.deepEqual(changedLines(rows), [2, 3]);
  assert.deepEqual(stepDelta(rows), { added: 2, removed: 2 });
  assert.equal(caretLine(rows), 3);
  assert.equal(caretLine([]), null);
});

test('a freshly created file has no gutter marks but keeps its delta and caret', () => {
  const fresh = [
    { kind: 'added', line: 'import Mathlib', newLineNumber: 1 },
    { kind: 'added', line: '', newLineNumber: 2 },
    { kind: 'added', line: 'theorem t : True := trivial', newLineNumber: 3 },
  ];
  assert.equal(isFreshFile(fresh), true);
  assert.equal(isFreshFile(rows), false);
  assert.equal(isFreshFile([]), false);
  assert.deepEqual(changedLines(fresh), []);
  assert.deepEqual(stepDelta(fresh), { added: 3, removed: 0 });
  assert.equal(caretLine(fresh), 3);
});

test('timelineDots marks failed checks, user edits and the current step', () => {
  const steps = [
    { id: 'a', check_status: 'ok', check_current: true, code: 'theorem x : True := trivial', author: 'agent' },
    { id: 'b', check_status: 'error', author: 'agent' },
    { id: 'c', check_status: 'ok', code: 'theorem x : True := by sorry', author: 'user' },
  ];
  const dots = timelineDots(steps, 2);
  assert.deepEqual(dots.map((d) => d.tone), ['proved', 'error', 'sorry']);
  assert.deepEqual(dots.map((d) => d.failed), [false, true, false]);
  assert.deepEqual(dots.map((d) => d.user), [false, false, true]);
  assert.deepEqual(dots.map((d) => d.current), [false, false, true]);
});

test('stepLabel names the turn and author, and says "now" only for a fresh latest step', () => {
  const now = Date.parse('2026-06-16T00:01:00Z');
  const fresh = { turn: 3, author: 'agent', created_at: '2026-06-16T00:00:30Z' };
  assert.deepEqual(stepLabel({ index: 5, total: 6, step: fresh, latest: true, now }), { head: 'Step 6 of 6', tail: 'turn 3 · Lea · now' });
  assert.equal(stepLabel({ index: 5, total: 6, step: fresh, latest: false, now }).tail, 'turn 3 · Lea');
  const old = { turn: 1, author: 'user', created_at: '2026-06-15T00:00:00Z' };
  assert.equal(stepLabel({ index: 0, total: 6, step: old, latest: true, now }).tail, 'turn 1 · you');
  assert.equal(stepLabel({ index: 0, total: 1, step: { author: 'environment' }, now }).tail, 'environment');
});

test('deriveStatusChip merges compile, sorry and verify into one chip', () => {
  assert.deepEqual(deriveStatusChip({ step: null }), { kind: 'idle', label: 'No theorem yet', line: null });
  const proved = { check_status: 'ok', check_current: true, code: 'theorem t : True := trivial' };
  assert.equal(deriveStatusChip({ step: proved }).label, 'Proved · checked');
  assert.equal(deriveStatusChip({ step: proved }).kind, 'proved');
  assert.equal(deriveStatusChip({ step: proved, verify: { status: 'ok', current: true } }).label, 'Proved · verified');
  assert.equal(deriveStatusChip({ step: proved, verify: { status: 'ok', current: true } }).kind, 'verified');
  assert.deepEqual(deriveStatusChip({ step: proved, verify: { status: 'rejected' } }), { kind: 'error', label: 'Rejected by SafeVerify', line: null });
  assert.equal(deriveStatusChip({ step: proved, verify: { status: 'error' } }).label, 'Proved · SafeVerify error');
  // unavailable / pending say nothing about the proof
  assert.equal(deriveStatusChip({ step: proved, verify: { status: 'unavailable' } }).label, 'Proved · checked');
  assert.equal(deriveStatusChip({ step: proved, verify: { status: 'pending' } }).kind, 'proved');
  assert.equal(deriveStatusChip({ step: proved, verify: { status: 'running' } }).label, 'Verifying…');
  assert.equal(deriveStatusChip({ step: proved, checking: true }).label, 'Checking…');
  assert.equal(deriveStatusChip({ step: proved, dirty: true }).label, 'Editing…');
  assert.equal(deriveStatusChip({ step: { ...proved, artifact_kind: 'definition' } }).label, 'Defined · checked');
});

test('deriveStatusChip reports errors with the first line and counts sorries', () => {
  const failed = { check_status: 'error', check_current: true, check_detail: DETAIL, code: '' };
  const problems = parseLeanProblems(DETAIL);
  const chip = deriveStatusChip({ step: failed, problems, sorries: [20] });
  assert.deepEqual(chip, { kind: 'error', label: '1 error · line 15 · 1 sorry', line: 15 });
  const stub = { check_status: 'ok', check_current: true, code: 'theorem t : True := by sorry' };
  assert.deepEqual(deriveStatusChip({ step: stub, sorries: [1] }), { kind: 'sorry', label: 'Checked · 1 sorry', line: 1 });
  const unchecked = { check_status: 'unchecked', check_current: true, code: 'x' };
  assert.equal(deriveStatusChip({ step: unchecked }).label, 'Not checked');
  assert.equal(deriveStatusChip({ step: unchecked, running: true }).label, 'Checking…');
  assert.equal(deriveStatusChip({ step: { ...unchecked, check_stale_reason: 'moved' } }).label, 'Recheck required');
  // a historical step keeps its recorded verdict even though check_current is not set
  const historical = { check_status: 'ok', code: 'theorem t : True := trivial' };
  assert.equal(deriveStatusChip({ step: historical, historical: true }).label, 'Proved · checked');
  assert.equal(deriveStatusChip({ step: historical }).label, 'Not checked');
});

test('theoremName prefers theorem/lemma, then another declaration, then the file name', () => {
  assert.equal(theoremName('import Mathlib\n\ndef helper := 1\n\n/-- doc -/\ntheorem sqrt_two_irrational : True := trivial'), 'sqrt_two_irrational');
  assert.equal(theoremName('def helper (n : Nat) := n\nexample : True := trivial'), 'helper');
  assert.equal(theoremName('@[simp] lemma foo_bar : True := trivial'), 'foo_bar');
  assert.equal(theoremName('-- nothing yet', 'Lea/Sqrt2/Parity.lean'), 'Parity');
  assert.equal(theoremName('', null), null);
});

test('latestCheckTiming and formatSeconds read the newest lean_check execution', () => {
  const events = [
    { id: '1', message: 'Running edit_file' },
    { id: '2', message: 'lean_check', check_status: 'error', execution: { backend: 'lsp', timings_ms: { total: 1234 } } },
    { id: '3', message: 'Running bash' },
  ];
  assert.deepEqual(latestCheckTiming(events), { ms: 1234, backend: 'lsp', status: 'error' });
  assert.equal(latestCheckTiming([]), null);
  assert.equal(formatSeconds(1234), '1.2 s');
  assert.equal(formatSeconds(88_400), '88 s');
  assert.equal(formatSeconds(undefined), '');
});

test('goalHeadline and drawerSummary build the collapsed drawer line', () => {
  assert.equal(goalHeadline('a b : ℤ\nkey : a ^ 2 = 2 * b ^ 2\n⊢ Even a'), '⊢ Even a');
  assert.equal(goalHeadline('No goals at this position.'), 'No goals at this position.');
  assert.equal(goalHeadline(''), null);
  assert.equal(goalHeadline('⊢ ' + 'x'.repeat(100), 20).length, 20);
  assert.equal(drawerSummary({ goal: '⊢ Even a', chip: { kind: 'running' }, sorries: 1 }), '⊢ Even a · checking · 1 sorry');
  assert.equal(drawerSummary({ chip: { kind: 'error' }, errors: 2 }), '2 errors');
  assert.equal(drawerSummary({ chip: { kind: 'verified' } }), 'verified');
  assert.equal(drawerSummary({}), '');
});

test('chipTab opens Problems for errors and sorries, Verify once verified, else Goals', () => {
  assert.equal(chipTab({ kind: 'error', label: '1 error', line: 3 }), 'problems');
  assert.equal(chipTab({ kind: 'sorry', label: 'Checked · 1 sorry', line: 3 }), 'problems');
  assert.equal(chipTab({ kind: 'verified', label: 'Proved · verified', line: null }), 'verify');
  assert.equal(chipTab({ kind: 'running', label: 'Checking…', line: null }), 'goals');
});

test('verifyAvailability explains why SafeVerify cannot run, in priority order', () => {
  const base = { hasStep: true, latest: true, proof: 'proved' };
  assert.deepEqual(verifyAvailability(base), { canRun: true, reason: 'Kernel-audit the saved proof (SafeVerify)' });
  assert.equal(verifyAvailability({ ...base, hasStep: false }).reason, 'Nothing to verify yet');
  assert.equal(verifyAvailability({ ...base, running: true, proof: 'failed' }).reason, 'Wait for Lea to finish');
  assert.equal(verifyAvailability({ ...base, draftPending: true }).reason, 'Save first — SafeVerify checks the saved file');
  assert.equal(verifyAvailability({ ...base, latest: false }).reason, 'Only the latest snapshot can be verified');
  assert.equal(verifyAvailability({ ...base, proof: 'failed' }).canRun, false);
  assert.equal(verifyAvailability({ ...base, proof: 'stubbed' }).reason, 'Remove the sorries first');
  assert.equal(verifyAvailability({ ...base, proof: 'unchecked' }).reason, 'Run lean_check first');
  assert.equal(verifyAvailability({ ...base, proof: 'defined' }).canRun, true);
});

test('editorNote: editor states first, then the user edit pointer, then Editable', () => {
  const agent = { author: 'agent' };
  const user = { author: 'user' };
  const clean = { state: 'clean', detail: '' };
  assert.equal(editorNote({ following: false, step: user, live: 'ready' }), 'Your edit · read-only');
  assert.equal(editorNote({ following: false, step: agent, live: 'ready' }), 'Read-only · older step');
  assert.equal(editorNote({ following: true, step: agent, live: 'loading' }), 'Opening editor…');
  assert.equal(editorNote({ following: true, step: agent, live: 'error', liveError: 'no worker' }), 'Editor unavailable · no worker');
  assert.equal(editorNote({ following: true, step: agent, live: 'none', hasSession: false }), 'Send a message to start editing');
  assert.equal(editorNote({ following: true, step: agent, live: 'ready', running: true, save: clean }), 'Read-only while Lea works');
  assert.equal(editorNote({ following: true, step: agent, live: 'ready', save: { state: 'dirty', detail: '' } }), 'Editing…');
  assert.equal(editorNote({ following: true, step: agent, live: 'ready', save: { state: 'saved', detail: 'lean_check: 0 errors' } }), 'Saved · lean_check: 0 errors');
  assert.equal(editorNote({ following: true, step: agent, previous: user, previousIndex: 7, live: 'ready', save: clean }), 'Step 8 was your edit');
  assert.equal(editorNote({ following: true, step: agent, previous: agent, previousIndex: 7, live: 'ready', save: clean }), 'Editable');
  // without a live editor (historical formalization view) the pointer still shows
  assert.equal(editorNote({ following: true, step: agent, previous: user, previousIndex: 2, live: 'none' }), 'Step 3 was your edit');
  assert.equal(editorNote({ following: true, step: agent, previous: agent, previousIndex: 2, live: 'none' }), '');
});

test('samePath and revealTarget implement the reveal-line contract', () => {
  assert.equal(samePath('Lea/Sqrt2/Parity.lean', './Lea/Sqrt2/Parity.lean'), true);
  assert.equal(samePath('/Lea/A.lean', 'Lea/A.lean'), true);
  assert.equal(samePath('Lea/A.lean', 'Lea/B.lean'), false);
  assert.equal(samePath(null, 'Lea/A.lean'), false);
  assert.equal(revealTarget(null, 'a.lean'), null);
  assert.equal(revealTarget({ line: 14, path: 'Lea/A.lean' }, 'Lea/A.lean'), 14);
  assert.equal(revealTarget({ line: 14, path: 'Lea/A.lean' }, 'Lea/B.lean'), null);
  assert.equal(revealTarget({ line: 14 }, 'whatever.lean'), 14);
  assert.equal(revealTarget({ line: 0 }, 'a.lean'), null);
  assert.equal(revealTarget({ line: Number.NaN }, 'a.lean'), null);
});

test('parseLeanProblems reads an absolute daemon path and the info severity', () => {
  const [p, q] = parseLeanProblems('/tmp/ws/proofs/A.lean:3:4: error: unknown identifier \'foo\'\n/tmp/ws/proofs/A.lean:9:1: information: try this');
  assert.equal(p.path, '/tmp/ws/proofs/A.lean');
  assert.equal(p.line, 3);
  assert.equal(p.severity, 'error');
  assert.equal(q.severity, 'info');
  assert.equal(parseLeanProblems('Error: lake build timed out after 30s.')[0].severity, 'error');
});
