import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { build } from 'esbuild';
import { buildModelRows, catalogStatus } from './lib/modelPickerRows.mjs';
await import('../../../overleaf-extension/extension/modelPicker.js');

for (const [name, rows] of [['standalone', buildModelRows], ['Overleaf', globalThis.LeaModelPicker.buildModelRows]]) {
  test(`${name}: a suggestion absent from metadata remains searchable and selectable`, () => {
    const result = rows({ catalog: [], featured: [{ value: 'gpt-future', label: 'Future model' }], query: 'gpt-future' });
    assert.deepEqual(result.map(m => m.value), ['gpt-future']);
    assert.equal(Boolean(result[0].custom), false);
  });
  test(`${name}: current custom model survives refresh and exact matches precede partials`, () => {
    const catalog = [{ value: 'vendor/model-larger' }, { value: 'vendor/model' }];
    assert.equal(rows({ catalog, query: 'vendor/model' })[0].value, 'vendor/model');
    assert.equal(rows({ catalog, value: 'custom/model', query: 'custom/model' })[0].value, 'custom/model');
    assert.equal(rows({ catalog, value: 'custom/model' })[0].value, 'custom/model');
    assert.equal(rows({ catalog, query: 'other/model' })[0].custom, true);
  });
  test(`${name}: unions deduplicate and preserve compatibility metadata`, () => {
    const model = { value: 'new/model', provider: 'new', compatibility: 'unverified' };
    const result = rows({ catalog: [model], featured: [{ ...model, label: 'New model' }], query: 'New model' });
    assert.equal(result.filter(m => !m.custom).length, 1);
    assert.equal(result.find(m => !m.custom).compatibility, 'unverified');
  });
}

test('freshness and fallback messages agree between applications', () => {
  for (const payload of [{ degraded: true, warnings: [{ message: 'Provider offline' }] }, { refreshing: true, updated_at: '2026-10-16T10:00:00Z' }]) {
    assert.equal(catalogStatus(payload), globalThis.LeaModelPicker.catalogStatus(payload));
  }
});

const bundled = await build({ entryPoints: [new URL('./lib/api.ts', import.meta.url).pathname], bundle: true, write: false, platform: 'node', format: 'cjs' });
test('the client retains source status, revision, suggestions and warnings on GET and refresh', async () => {
  const payload = { models: [{ value: 'new/model' }], featured: [], revision: 'revision-2', degraded: true,
    sources: { openai: { stale: true } }, warnings: [{ message: 'Offline' }] };
  const calls = [];
  const context = { module: { exports: {} }, exports: {}, fetch: async (url, options) => {
    calls.push([url, options.method]);
    return new Response(JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } });
  } };
  context.exports = context.module.exports;
  vm.runInNewContext(bundled.outputFiles[0].text, context);
  assert.deepEqual(JSON.parse(JSON.stringify(await context.module.exports.fetchModelCatalog())), payload);
  await context.module.exports.fetchModelCatalog(true);
  assert.deepEqual(calls, [['/api/models', 'GET'], ['/api/models/refresh', 'POST']]);
});
