import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  deriveCodeStepProofStatus,
  deriveRunCompletionStatus,
  hasSorryLikeCheckDetail,
  hasSorryLikeCode,
} from './lib/proofDisplay.mjs';

const step = (extra = {}) => ({
  id: 'c1',
  session_id: 's',
  author: 'agent',
  path: 'Proof.lean',
  commit_sha: 'sha',
  check_status: 'ok',
  check_detail: null,
  code: 'theorem t : True := by\n  trivial\n',
  created_at: '2026-06-16T00:00:00Z',
  ...extra,
});

test('checked code without sorry is displayed as proved', () => {
  assert.equal(deriveCodeStepProofStatus(step()), 'proved');
});

test('checked unknown artifact is displayed as checked code, not proof', () => {
  assert.equal(deriveCodeStepProofStatus(step({ artifact_kind: 'unknown' })), 'checked');
});

test('checked code with sorry is displayed as stubbed', () => {
  assert.equal(
    deriveCodeStepProofStatus(step({ code: 'theorem t : True := by\n  sorry\n' })),
    'stubbed',
  );
});

test('checked code with admit is displayed as stubbed', () => {
  assert.equal(
    deriveCodeStepProofStatus(step({ code: 'theorem t : True := by\n  admit\n' })),
    'stubbed',
  );
});

test('Lean warning detail can mark unavailable code as stubbed', () => {
  const detail = "warning: declaration uses 'sorry'";
  assert.equal(hasSorryLikeCheckDetail(detail), true);
  assert.equal(deriveCodeStepProofStatus(step({ code: '', check_detail: detail })), 'stubbed');
});

test('sorryAx detail is treated as a remaining stub', () => {
  assert.equal(hasSorryLikeCheckDetail('found sorryAx in proof artifact'), true);
});

test('failed checks remain failed even when code contains sorry', () => {
  assert.equal(
    deriveCodeStepProofStatus(step({ check_status: 'error', code: 'theorem t : True := by sorry' })),
    'failed',
  );
});

test('run proved with a sorry-bearing latest step is displayed as stubbed', () => {
  const steps = [
    step({ id: 'c1', code: 'theorem t : True := by\n  trivial\n' }),
    step({ id: 'c2', code: 'theorem t : True := by\n  sorry\n' }),
  ];
  assert.equal(deriveRunCompletionStatus('proved', steps), 'stubbed');
  assert.equal(deriveRunCompletionStatus('success', steps), 'stubbed');
});

test('run proved with defined result kind is displayed as definition creation', () => {
  assert.equal(deriveRunCompletionStatus('proved', [step({ code: 'def d : Nat := 0\n' })], 'defined'), 'defined');
});

test('run disproved is displayed as disproof, not proof', () => {
  assert.equal(deriveRunCompletionStatus('disproved', [step()]), 'disproved');
});

test('reviewed theorem artifacts retain the review verdict', () => {
  assert.equal(deriveRunCompletionStatus('needs_review', [step({ artifact_kind: 'proof' })]), 'needs_review');
});

test('reviewed definition artifacts retain the review verdict', () => {
  assert.equal(
    deriveRunCompletionStatus('needs_review', [step({ code: 'def d : Nat := 0\n', artifact_kind: 'definition' })]),
    'needs_review',
  );
});

test('reviewed unknown artifacts retain the review verdict', () => {
  assert.equal(deriveRunCompletionStatus('needs_review', [step({ artifact_kind: 'unknown' })]), 'needs_review');
});

test('Lean comments mentioning sorry do not make the code a stub', () => {
  assert.equal(hasSorryLikeCode('-- sorry here is only a note\ntheorem t : True := by\n  trivial\n'), false);
  assert.equal(hasSorryLikeCode('/- admit in a comment -/\ntheorem t : True := by\n  trivial\n'), false);
});
