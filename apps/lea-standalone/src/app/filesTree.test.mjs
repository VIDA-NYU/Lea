import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  ancestorDirs,
  baseName,
  childrenOf,
  dirPaths,
  findEntry,
  flattenFiles,
  isLeanPath,
  parentDir,
  scanDeclarations,
} from './lib/filesTree.js';

const tree = [
  {
    name: 'Lea', path: 'Lea', type: 'dir',
    children: [
      {
        name: 'Sqrt2', path: 'Lea/Sqrt2', type: 'dir',
        children: [
          { name: 'Descent.lean', path: 'Lea/Sqrt2/Descent.lean', type: 'file', size: 10 },
          { name: 'Parity.lean', path: 'Lea/Sqrt2/Parity.lean', type: 'file', size: 20 },
        ],
      },
      { name: 'Sqrt2.lean', path: 'Lea/Sqrt2.lean', type: 'file', size: 5 },
    ],
  },
  { name: 'lakefile.lean', path: 'lakefile.lean', type: 'file', size: 1 },
  { name: 'blueprint', path: 'blueprint', type: 'dir', children: [] },
];

test('path helpers', () => {
  assert.equal(isLeanPath('a/b.lean'), true);
  assert.equal(isLeanPath('a/b.md'), false);
  assert.equal(baseName('Lea/Sqrt2/Parity.lean'), 'Parity.lean');
  assert.equal(baseName('Parity.lean'), 'Parity.lean');
  assert.equal(parentDir('Lea/Sqrt2/Parity.lean'), 'Lea/Sqrt2');
  assert.equal(parentDir('Parity.lean'), '');
  assert.deepEqual(ancestorDirs('Lea/Sqrt2/Parity.lean'), ['Lea', 'Lea/Sqrt2']);
  assert.deepEqual(ancestorDirs('Parity.lean'), []);
});

test('flattenFiles lists every file depth-first with its parent dir', () => {
  assert.deepEqual(flattenFiles(tree).map((f) => [f.path, f.dir, f.size]), [
    ['Lea/Sqrt2/Descent.lean', 'Lea/Sqrt2', 10],
    ['Lea/Sqrt2/Parity.lean', 'Lea/Sqrt2', 20],
    ['Lea/Sqrt2.lean', 'Lea', 5],
    ['lakefile.lean', '', 1],
  ]);
  assert.deepEqual(flattenFiles([]), []);
});

test('dirPaths, findEntry and childrenOf walk the tree', () => {
  assert.deepEqual(dirPaths(tree), ['Lea', 'Lea/Sqrt2', 'blueprint']);
  assert.equal(findEntry(tree, 'Lea/Sqrt2/Parity.lean')?.name, 'Parity.lean');
  assert.equal(findEntry(tree, 'Lea/Sqrt2')?.type, 'dir');
  assert.equal(findEntry(tree, 'Lea/Nope.lean'), undefined);
  assert.deepEqual(childrenOf(tree, '').map((e) => e.name), ['Lea', 'blueprint', 'lakefile.lean'], 'dirs first at the root');
  assert.deepEqual(childrenOf(tree, 'Lea').map((e) => e.name), ['Sqrt2', 'Sqrt2.lean']);
  assert.deepEqual(childrenOf(tree, 'Lea/Sqrt2').map((e) => e.name), ['Descent.lean', 'Parity.lean']);
  assert.deepEqual(childrenOf(tree, 'missing'), []);
});

test('scanDeclarations finds named declarations with kinds and 1-based lines', () => {
  const source = [
    'import Mathlib',
    '',
    'namespace Lea.Sqrt2',
    '',
    '/-- If a² is even then a is even. -/',
    'theorem even_of_sq_even {a : ℤ} (h : Even (a ^ 2)) : Even a := by',
    '  sorry',
    '',
    '@[simp] lemma sq_parity (n : ℕ) : n ^ 2 % 2 = n % 2 := by omega',
    'private noncomputable def helper : ℕ → ℕ := fun n => n',
    'abbrev Pair := ℕ × ℕ',
    'structure Point where',
    '  x : ℕ',
    'instance : Inhabited Point := ⟨⟨0⟩⟩',
    'instance pointRepr : Repr Point := ⟨fun _ _ => "p"⟩',
    '-- theorem commented_out : True := trivial',
    '/- theorem in_block : True := trivial',
    '   still a comment -/',
    'end Lea.Sqrt2',
    '',
    'theorem top_level : True := trivial',
  ].join('\n');
  const decls = scanDeclarations(source);
  assert.deepEqual(decls.map((d) => [d.kind, d.name, d.line]), [
    ['theorem', 'even_of_sq_even', 6],
    ['lemma', 'sq_parity', 9],
    ['def', 'helper', 10],
    ['abbrev', 'Pair', 11],
    ['structure', 'Point', 12],
    ['instance', 'pointRepr', 15],
    ['theorem', 'top_level', 21],
  ]);
  assert.equal(decls[0].qualified, 'Lea.Sqrt2.even_of_sq_even', 'namespace is tracked');
  assert.equal(decls[6].qualified, 'top_level', '`end` pops the namespace');
  assert.deepEqual(scanDeclarations(''), []);
});

test('scanDeclarations ignores sections for qualification and nested block comments', () => {
  const source = ['section Foo', 'def inside : ℕ := 1', 'end Foo', '/- outer /- inner -/ still -/', 'def after : ℕ := 2'].join('\n');
  const decls = scanDeclarations(source);
  assert.deepEqual(decls.map((d) => d.qualified), ['inside', 'after']);
});
