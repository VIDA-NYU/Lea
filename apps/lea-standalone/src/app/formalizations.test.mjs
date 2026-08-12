import assert from 'node:assert/strict';
import test from 'node:test';

import {
  filesForFormalization,
  formalizationCanvasSteps,
  inferComposerFormalizationScope,
  restoreFormalizationSelection,
  sessionFormalizationSummary,
  stepsForFormalization,
} from './lib/formalizations.mjs';

const steps = [
  { id: '1', path: 'a.lean', formalization_id: 'a' },
  { id: '2', path: 'b.lean', formalization_id: 'b' },
  { id: '3', path: 'a.lean', formalization_id: 'a' },
  { id: '4', path: 'scratch.lean', formalization_id: null },
];

test('steps and files remain scoped to a formalization', () => {
  assert.deepEqual(stepsForFormalization(steps, 'a').map((step) => step.id), ['1', '3']);
  assert.deepEqual(filesForFormalization(steps, 'a'), ['a.lean']);
  assert.equal(stepsForFormalization(steps, null).length, 4);
});

test('selection restoration follows explicit, current, latest, active, all-work order', () => {
  const formalizations = [
    { id: 'a', activity: { status: 'idle' } },
    { id: 'b', activity: { status: 'running' } },
  ];
  assert.equal(restoreFormalizationSelection({ explicitId: 'a', currentId: 'b', formalizations }), 'a');
  assert.equal(restoreFormalizationSelection({ currentId: 'b', formalizations }), 'b');
  assert.equal(restoreFormalizationSelection({ latestFocusId: 'a', formalizations }), 'a');
  assert.equal(restoreFormalizationSelection({ formalizations }), 'b');
  assert.equal(restoreFormalizationSelection({ formalizations: [] }), 'project');
});

test('session summary keeps independent validity and activity counts', () => {
  assert.deepEqual(
    sessionFormalizationSummary([
      { validity_status: 'proved', activity: { status: 'idle' } },
      { validity_status: 'failing', activity: { status: 'running' } },
    ]),
    { total: 2, active: 1, proved: 1, failing: 1 },
  );
});

test('composer inference follows an explicitly named formalization instead of the viewed item', () => {
  const formalizations = [
    { id: 'a', declaration_name: 'DefinitionA', display_title: 'Definition A' },
    { id: 'b', declaration_name: 'TheoremB', display_title: 'Theorem B' },
  ];
  assert.equal(
    inferComposerFormalizationScope({
      message: 'Please edit TheoremB and simplify its proof.',
      formalizations,
      viewedScope: 'a',
    }),
    'b',
  );
});

test('composer inference treats multiple named items as project-wide discussion', () => {
  const formalizations = [
    { id: 'a', declaration_name: 'DefinitionA' },
    { id: 'b', declaration_name: 'TheoremB' },
  ];
  assert.equal(
    inferComposerFormalizationScope({
      message: 'Compare DefinitionA with TheoremB.',
      formalizations,
      viewedScope: 'a',
    }),
    'project',
  );
});

test('composer inference recognizes new work and otherwise uses the viewed item as a hint', () => {
  const formalizations = [{ id: 'a', declaration_name: 'DefinitionA' }];
  assert.equal(
    inferComposerFormalizationScope({
      message: 'Now prove a new theorem about compact images.',
      formalizations,
      viewedScope: 'a',
    }),
    'new',
  );
  assert.equal(
    inferComposerFormalizationScope({
      message: 'Can you simplify this proof?',
      formalizations,
      viewedScope: 'a',
    }),
    'a',
  );
  assert.equal(
    inferComposerFormalizationScope({
      message: 'What remains to do in this project?',
      formalizations,
      viewedScope: 'project',
    }),
    'project',
  );
});

test('composer inference matches qualified declarations by their short name', () => {
  assert.equal(
    inferComposerFormalizationScope({
      message: 'Repair continuous_mul.',
      formalizations: [{ id: 'a', declaration_name: 'Topology.continuous_mul' }],
    }),
    'a',
  );
});

test('composer inference does not confuse one-letter declarations with prose articles', () => {
  const formalizations = [
    { id: 'a', declaration_name: 'A', display_title: 'Definition A' },
    { id: 'b', declaration_name: 'B', display_title: 'Theorem B' },
  ];
  assert.equal(
    inferComposerFormalizationScope({
      message: 'Please edit theorem B and use a shorter proof.',
      formalizations,
      viewedScope: 'a',
    }),
    'b',
  );
});

test('canonical and conversation snapshots remain separate canvas sources', () => {
  const historical = [{ id: 's1-v1', session_id: 's1', code: 'old' }];
  const snapshot = {
    formalization_id: 'a',
    files: [{ id: 's2-v2', session_id: 's2', code: 'current' }],
  };
  assert.deepEqual(
    formalizationCanvasSteps({
      mode: 'current',
      formalizationId: 'a',
      snapshot,
      historicalSteps: historical,
    }).map((step) => step.id),
    ['s2-v2'],
  );
  assert.deepEqual(
    formalizationCanvasSteps({
      mode: 'historical',
      formalizationId: 'a',
      snapshot,
      historicalSteps: historical,
    }).map((step) => step.id),
    ['s1-v1'],
  );
});
