import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  countErrors,
  countSorries,
  deriveFileStatuses,
  editingPath,
  latestStepsByPath,
  lineDelta,
  sessionFiles,
  statusLabel,
  stripComments,
} from './lib/fileStatus.js';

const step = (path, extra = {}) => ({ id: `${path}:${Math.random()}`, path, code: 'theorem t : True := trivial', check_status: 'ok', check_current: true, ...extra });

test('stripComments and countSorries ignore sorry inside comments', () => {
  const src = ['theorem a : True := by', '  sorry -- sorry later', '/- sorry in a block', '   sorry -/', 'theorem b : True := sorry', 'def sorrya := 1'].join('\n');
  assert.equal(countSorries(src), 2);
  assert.equal(countSorries(''), 0);
  assert.equal(countSorries('-- sorry'), 0);
  assert.match(stripComments('a /- x /- y -/ z -/ b'), /^a\s+b$/);
});

test('countErrors counts lean diagnostics', () => {
  assert.equal(countErrors('Foo.lean:8:2: error: unsolved goals\nFoo.lean:14:0: error: type mismatch'), 2);
  assert.equal(countErrors('Foo.lean:8:2: warning: declaration uses sorry'), 0);
  assert.equal(countErrors(null), 0);
});

test('lineDelta reports added and removed lines by line LCS', () => {
  assert.deepEqual(lineDelta('', 'a\nb\nc'), { added: 3, removed: 0 });
  assert.deepEqual(lineDelta('a\nb\nc', 'a\nx\nc\nd'), { added: 2, removed: 1 });
  assert.deepEqual(lineDelta('a\nb', 'a\nb'), { added: 0, removed: 0 });
  assert.deepEqual(lineDelta('a\nb', ''), { added: 0, removed: 2 });
});

test('latestStepsByPath keeps the last step per path', () => {
  const steps = [step('A.lean', { id: 1 }), step('B.lean', { id: 2 }), step('A.lean', { id: 3 })];
  const latest = latestStepsByPath(steps);
  assert.equal(latest.get('A.lean').id, 3);
  assert.equal(latest.get('B.lean').id, 2);
});

test('editingPath is the latest non-scratch step of the live run', () => {
  const steps = [step('Main.lean', { run_id: 'r1' }), step('scratch/Probe.lean', { run_id: 'r2' }), step('Lemmas.lean', { run_id: 'r2' })];
  assert.equal(editingPath({ codeSteps: steps, isRunning: false, currentRunId: 'r2' }), null);
  assert.equal(editingPath({ codeSteps: steps, isRunning: true, currentRunId: 'r2' }), 'Lemmas.lean');
  assert.equal(editingPath({ codeSteps: steps, isRunning: true, currentRunId: 'r1' }), 'Main.lean');
  assert.equal(editingPath({ codeSteps: steps, isRunning: true, currentRunId: 'r3' }), null, 'a run that has not written yet');
  assert.equal(editingPath({ codeSteps: steps, isRunning: true }), 'Lemmas.lean', 'unknown run id falls back to the latest step');
});

test('deriveFileStatuses applies running > error > sorry > proved > unchecked', () => {
  const steps = [
    step('Descent.lean', { check_status: 'error', check_detail: 'Descent.lean:8:2: error: unsolved goals' }),
    step('Lemmas.lean', { code: 'theorem a : True := sorry\ntheorem b : True := sorry' }),
    step('Parity.lean'),
    step('Sqrt2.lean'),
    step('Stale.lean', { check_current: false }),
    step('Lost.lean', { content_lost: true, check_status: null }),
  ];
  const contents = new Map([['Lost.lean', 'theorem c : True := sorry'], ['Fetched.lean', 'def x := 1']]);
  const paths = ['Descent.lean', 'Lemmas.lean', 'Parity.lean', 'Sqrt2.lean', 'Stale.lean', 'Lost.lean', 'Fetched.lean', 'Unknown.lean', 'README.md'];
  const statuses = deriveFileStatuses({ paths, codeSteps: steps, contents, editingPath: 'Sqrt2.lean' });
  assert.equal(statuses.get('Descent.lean').status, 'error');
  assert.equal(statuses.get('Descent.lean').errors, 1);
  assert.equal(statuses.get('Lemmas.lean').status, 'sorry');
  assert.equal(statuses.get('Lemmas.lean').sorries, 2);
  assert.equal(statuses.get('Parity.lean').status, 'proved');
  assert.equal(statuses.get('Sqrt2.lean').status, 'running', 'the live edit wins over an ok check');
  assert.equal(statuses.get('Stale.lean').status, 'unchecked', 'a stale ok check is not proved');
  assert.equal(statuses.get('Stale.lean').stale, true);
  assert.equal(statuses.get('Lost.lean').status, 'sorry', 'lost step content falls back to the fetched file');
  assert.equal(statuses.get('Fetched.lean').status, 'unchecked', 'a fetched file without a check');
  assert.equal(statuses.get('Unknown.lean').status, 'unchecked');
  assert.equal(statuses.get('Unknown.lean').known, false);
  assert.equal(statuses.get('README.md').status, null, 'non-Lean files carry no status');
});

test('an error with sorries is still an error; a sorry beats an ok check', () => {
  const steps = [
    step('E.lean', { check_status: 'error', code: 'theorem a : True := sorry' }),
    step('S.lean', { check_status: 'ok', code: 'theorem a : True := sorry' }),
  ];
  const statuses = deriveFileStatuses({ paths: ['E.lean', 'S.lean'], codeSteps: steps });
  assert.equal(statuses.get('E.lean').status, 'error');
  assert.equal(statuses.get('S.lean').status, 'sorry');
});

test('sessionFiles lists touched files in first-seen order with summed deltas, skipping scratch', () => {
  const steps = [
    step('Parity.lean', { code: 'a\nb\nc' }),
    step('scratch/Probe.lean', { code: 'x' }),
    step('Lemmas.lean', { code: 'l1\nl2' }),
    step('Parity.lean', { code: 'a\nc\nd\ne' }),
    step('Lemmas.lean', { content_lost: true, code: '' }),
  ];
  const files = sessionFiles(steps);
  assert.deepEqual(files.map((f) => f.path), ['Parity.lean', 'Lemmas.lean']);
  assert.deepEqual(files[0], { path: 'Parity.lean', name: 'Parity.lean', added: 5, removed: 1, steps: 2 });
  assert.deepEqual(files[1], { path: 'Lemmas.lean', name: 'Lemmas.lean', added: 2, removed: 0, steps: 2 }, 'a lost step does not count');
  assert.deepEqual(sessionFiles([]), []);
});

test('statusLabel pairs every hue with words', () => {
  assert.equal(statusLabel({ status: 'running' }), 'Lea');
  assert.equal(statusLabel({ status: 'running' }, 'list'), 'Lea · editing');
  assert.equal(statusLabel({ status: 'error', errors: 1 }), '1 error');
  assert.equal(statusLabel({ status: 'error', errors: 3 }), '3 errors');
  assert.equal(statusLabel({ status: 'sorry', sorries: 2 }), '2 sorry');
  assert.equal(statusLabel({ status: 'proved' }), '');
  assert.equal(statusLabel({ status: 'proved' }, 'list'), 'proved');
  assert.equal(statusLabel({ status: 'unchecked' }), 'unchecked');
  assert.equal(statusLabel({ status: null }), '');
  assert.equal(statusLabel(null), '');
});
