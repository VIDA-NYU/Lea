import { test } from 'node:test';
import assert from 'node:assert/strict';
import { countRunning, finishedRuns, indexById, outcomeLine } from './lib/desktopActivity.js';

test('countRunning counts root sessions with a live run only', () => {
  const sessions = [
    { id: 'a', active_run_count: 1 },
    { id: 'b', active_run_count: 0 },
    { id: 'c', active_run_count: 2, parent_id: 'a' },
    { id: 'd' },
  ];
  assert.equal(countRunning(sessions), 1);
});

test('finishedRuns reports the >0 → 0 transition and nothing on the first snapshot', () => {
  const before = indexById([
    { id: 'a', title: 'Sqrt2', active_run_count: 1, status: 'running' },
    { id: 'b', title: 'Other', active_run_count: 0, status: 'proved' },
    { id: 's', title: 'sub', active_run_count: 1, parent_id: 'a' },
  ]);
  const after = [
    { id: 'a', title: 'Sqrt2', active_run_count: 0, status: 'proved' },
    { id: 'b', title: 'Other', active_run_count: 0, status: 'proved' },
    { id: 's', title: 'sub', active_run_count: 0, parent_id: 'a', status: 'proved' },
    { id: 'new', title: 'New', active_run_count: 0, status: 'proved' },
  ];
  assert.deepEqual(finishedRuns(null, after), []);
  assert.deepEqual(finishedRuns(before, after), [{ sessionId: 'a', title: 'Sqrt2', body: 'Proof complete.' }]);
});

test('outcomeLine maps statuses to one line', () => {
  assert.equal(outcomeLine({ status: 'proved' }), 'Proof complete.');
  assert.equal(outcomeLine({ status: 'error', latest_check_status: 'error' }), 'Stopped with a Lean error.');
  assert.equal(outcomeLine({ status: 'error' }), 'Stopped with an error.');
  assert.equal(outcomeLine({ status: 'needs_review' }), 'Needs your review.');
  assert.equal(outcomeLine({ status: 'disproved' }), 'Found a counterexample.');
  assert.equal(outcomeLine({ status: 'whatever' }), 'Run finished.');
});
