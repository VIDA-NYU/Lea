// Keystone conformance test: the UI blueprint parser (`./lib/blueprintModel.js`)
// against the shared parser fixtures in `apps/lea-standalone/fixtures/blueprint`.
// The same cases drive adapter/tests/test_blueprint_fixtures.py, so the UI reads a
// blueprint exactly as the adapter (the system of record) does. Format rules:
// docs/PLAN-blueprint-tools.md §2.1; case format: fixtures/blueprint/README.md.
// JS checks nodes only: prose sections stay out of `nodes`; warnings are Python only.
//
// Red by design until P4 lands the tolerant parser. Do not edit the fixtures to
// make this pass.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { parseBlueprintMarkdown, serializeBlueprint } from './lib/blueprintModel.js';

const FIXTURES = new URL('../../fixtures/blueprint/', import.meta.url);
const CASES = readdirSync(FIXTURES)
  .filter((name) => name.endsWith('.md') && name !== 'README.md')
  .sort();

const orNull = (value) => (value === undefined ? null : value);

/** The compared node fields; `undefined` counts as `null`. */
function view(node) {
  return { key: node.key, kind: orNull(node.kind), lean: orNull(node.lean), file: orNull(node.file), uses: node.uses };
}

function fullView(node) {
  return { ...view(node), statement: (node.statement ?? '').trim() };
}

function load(name) {
  const text = readFileSync(new URL(name, FIXTURES), 'utf8');
  const expected = JSON.parse(readFileSync(new URL(name.replace(/\.md$/, '.expected.json'), FIXTURES), 'utf8'));
  return { text, expected };
}

test('the keystone fixture cases are present', () => {
  assert.ok(CASES.length >= 8, `expected the keystone cases in ${FIXTURES.pathname}`);
});

for (const name of CASES) {
  const { text, expected } = load(name);

  test(`${name}: parseBlueprintMarkdown matches the fixture`, () => {
    const { nodes } = parseBlueprintMarkdown(text);
    const keys = nodes.map((n) => n.key);
    assert.deepEqual(keys, expected.nodes.map((n) => n.key));
    expected.nodes.forEach((want, i) => {
      const { statement_starts_with: prefix, ...fields } = want;
      assert.deepEqual(view(nodes[i]), fields, `node ${want.key}`);
      const statement = (nodes[i].statement ?? '').trim();
      assert.ok(statement.startsWith(prefix), `node ${want.key}: statement starts ${JSON.stringify(statement.slice(0, 80))}`);
    });
    for (const heading of expected.prose_sections) {
      assert.ok(!keys.includes(heading), `prose section "${heading}" parsed as a node`);
    }
  });

  test(`${name}: serializeBlueprint round-trips the nodes`, () => {
    const parsed = parseBlueprintMarkdown(text);
    const again = parseBlueprintMarkdown(serializeBlueprint(parsed.nodes, { preamble: parsed.preamble })).nodes;
    assert.deepEqual(again.map(fullView), parsed.nodes.map(fullView));
  });
}
