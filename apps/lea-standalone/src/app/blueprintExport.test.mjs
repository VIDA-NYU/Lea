import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  EXPORT_MIME,
  fileSlug,
  exportFileName,
  blueprintJson,
  blueprintJsonText,
  imageFrame,
  solidBackground,
} from './lib/blueprintExport.js';

const graph = {
  nodes: [
    {
      key: 'sqrt_two_irrational',
      kind: 'theorem',
      lean: 'Lea.Sqrt2.sqrt_two_irrational',
      uses: ['parity_descent'],
      statement: 'The square root of two is irrational.',
      file: 'Lea/Sqrt2.lean',
      status: 'stated',
      verified: false,
      sessions: [{ session_id: 's1', title: 'Prove sqrt_two_irrational', last_at: '2026-10-30T10:00:00Z' }],
      last_modified_by: 's1',
    },
    { key: 'parity_descent', kind: null, lean: null, uses: [], statement: '', file: null, status: 'planned', verified: false, sessions: [] },
  ],
  edges: [{ from: 'sqrt_two_irrational', to: 'parity_descent' }],
};

test('fileSlug keeps letters, digits, . _ - and folds the rest', () => {
  assert.equal(fileSlug('Sqrt2'), 'Sqrt2');
  assert.equal(fileSlug('real-analysis'), 'real-analysis');
  assert.equal(fileSlug('Real Analysis'), 'Real-Analysis');
  assert.equal(fileSlug('  Café / √2 proof!  '), 'Cafe-2-proof');
  assert.equal(fileSlug('../etc/passwd'), 'etc-passwd');
  assert.equal(fileSlug(''), '');
  assert.equal(fileSlug(null), '');
  assert.equal(fileSlug('√'), '');
  assert.ok(fileSlug('x'.repeat(200)).length <= 60);
  assert.ok(!fileSlug(`${'a'.repeat(59)}-b`).endsWith('-'));
});

test('exportFileName prefixes the slug, else the title, else nothing', () => {
  assert.equal(exportFileName({ slug: 'Sqrt2', title: 'Whatever' }, 'md'), 'Sqrt2-blueprint.md');
  assert.equal(exportFileName({ slug: null, title: 'Real Analysis' }, 'json'), 'Real-Analysis-blueprint.json');
  assert.equal(exportFileName({ slug: '', title: '√' }, 'png'), 'blueprint.png');
  assert.equal(exportFileName(null, 'md'), 'blueprint.md');
  assert.equal(exportFileName(undefined, 'png'), 'blueprint.png');
});

test('EXPORT_MIME covers every format', () => {
  assert.match(EXPORT_MIME.md, /^text\/markdown/);
  assert.match(EXPORT_MIME.json, /^application\/json/);
  assert.equal(EXPORT_MIME.png, 'image/png');
});

test('blueprintJson: project, timestamp, the node fields and the edges', () => {
  const doc = blueprintJson(graph, {
    project: { id: 'p1', slug: 'Sqrt2', title: 'Sqrt2', namespace: 'Lea.Sqrt2' },
    generatedAt: new Date('2026-10-30T12:00:00Z'),
  });
  assert.deepEqual(Object.keys(doc), ['project', 'generated_at', 'nodes', 'edges']);
  assert.deepEqual(doc.project, { id: 'p1', slug: 'Sqrt2', title: 'Sqrt2', namespace: 'Lea.Sqrt2' });
  assert.equal(doc.generated_at, '2026-10-30T12:00:00.000Z');
  assert.deepEqual(doc.nodes[0], {
    key: 'sqrt_two_irrational',
    kind: 'theorem',
    lean: 'Lea.Sqrt2.sqrt_two_irrational',
    file: 'Lea/Sqrt2.lean',
    status: 'stated',
    verified: false,
    uses: ['parity_descent'],
    statement: 'The square root of two is irrational.',
    sessions: [{ session_id: 's1', title: 'Prove sqrt_two_irrational', last_at: '2026-10-30T10:00:00Z' }],
  });
  assert.ok(!('last_modified_by' in doc.nodes[0]), 'only the documented fields');
  assert.deepEqual(doc.edges, [{ from: 'sqrt_two_irrational', to: 'parity_descent' }]);
});

test('blueprintJson normalises missing fields and derives edges from uses when absent', () => {
  const doc = blueprintJson({ nodes: [{ key: 'a', uses: ['b'] }, { key: 'b' }] }, { generatedAt: 0 });
  assert.equal(doc.project, null);
  assert.equal(doc.generated_at, '1970-01-01T00:00:00.000Z');
  assert.deepEqual(doc.nodes[1], {
    key: 'b',
    kind: null,
    lean: null,
    file: null,
    status: 'planned',
    verified: false,
    uses: [],
    statement: '',
    sessions: [],
  });
  assert.deepEqual(doc.edges, [{ from: 'a', to: 'b' }]);
  assert.deepEqual(blueprintJson({ nodes: [] }).nodes, []);
});

test('blueprintJsonText is parseable, indented and ends with a newline', () => {
  const text = blueprintJsonText(graph, { project: { id: 'p1' }, generatedAt: '2026-10-30T12:00:00Z' });
  assert.ok(text.endsWith('}\n'));
  assert.ok(text.includes('\n  "nodes": ['));
  const back = JSON.parse(text);
  assert.equal(back.nodes.length, 2);
  assert.deepEqual(back.project, { id: 'p1', slug: null, title: null, namespace: null });
});

test('imageFrame adds the margin on each side and rounds up', () => {
  assert.deepEqual(imageFrame({ x: 10, y: 20, width: 400.2, height: 300 }, 32), { width: 465, height: 364 });
  assert.deepEqual(imageFrame({ x: 0, y: 0, width: 0, height: 0 }, 0), { width: 1, height: 1 });
});

test('solidBackground keeps opaque colours and replaces see-through ones', () => {
  assert.equal(solidBackground('rgb(255, 255, 255)'), 'rgb(255, 255, 255)');
  assert.equal(solidBackground('rgb(20, 20, 22)'), 'rgb(20, 20, 22)');
  assert.equal(solidBackground('rgba(0, 0, 0, 0)'), '#ffffff');
  assert.equal(solidBackground('rgba(10, 10, 10, 0.5)', '#111'), '#111');
  assert.equal(solidBackground('rgba(10, 10, 10, 1)'), 'rgba(10, 10, 10, 1)');
  assert.equal(solidBackground('rgb(10 10 10 / 0.4)'), '#ffffff');
  assert.equal(solidBackground('rgb(10 10 10 / 40%)'), '#ffffff');
  assert.equal(solidBackground('transparent'), '#ffffff');
  assert.equal(solidBackground(''), '#ffffff');
  assert.equal(solidBackground(null, '#000'), '#000');
});
