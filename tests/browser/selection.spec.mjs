import { test, expect } from '@playwright/test';
import { build } from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
// Render the real hook, Zustand stores, and Canvas. Only the API, syntax colorer,
// draft panel, and Monaco transport are substituted; no backend/provider is run.
const bundle = build({
  stdin: { resolveDir: root, loader: 'tsx', contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { useProofStream } from './apps/lea-standalone/src/app/hooks/useProofStream';
    import { Canvas } from './apps/lea-standalone/src/app/components/Canvas';
    import { useProofSession } from './apps/lea-standalone/src/app/stores/proofSession';
    import { useSessions } from './apps/lea-standalone/src/app/stores/sessions';
    function App() {
      const hook=useProofStream(); window.hook=hook; window.proof=useProofSession;
      const scope=useProofSession(s=>s.formalizationScope);
      const mode=useProofSession(s=>s.canvasRevisionMode);
      const selected=useProofSession(s=>s.codeIndex);
      const count=useProofSession(s=>s.codeSteps.length);
      return <><output data-testid="selection">{scope}:{mode}:{selected}:{count}</output>
        <button onClick={()=>hook.applyDetail(window.fixture.detail)}>Open session</button>
        <button onClick={()=>{useProofSession.getState().setCodeIndex(0);useProofSession.getState().setCanvasRevisionMode('historical')}}>Choose history</button>
        <button onClick={()=>void hook.reconcile('session')}>Refresh</button>
        <button onClick={()=>useProofSession.getState().setFormalizationScope('target-b')}>Choose B</button>
        <Canvas sessionId="session" captureSelection={hook.captureSelection}
          onClose={()=>{}} onSaveAndCheck={async()=>({status:'ok'})} onVerify={async()=>({status:'ok'})}/></>;
    }
    useSessions.getState().setSelectedSessionId('session');
    createRoot(document.getElementById('app')).render(<App/>);
  ` },
  bundle: true, write: false, platform: 'browser', format: 'iife', jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"test"' },
  plugins: [{ name: 'selection-fixtures', setup(b) {
    b.onResolve({ filter: /\/lib\/api$/ }, () => ({ path: 'api', namespace: 'selection' }));
    b.onResolve({ filter: /\/LiveEditor$/ }, () => ({ path: 'editor', namespace: 'selection' }));
    b.onResolve({ filter: /\/ReformalizationDraft$/ }, () => ({ path: 'draft', namespace: 'selection' }));
    b.onResolve({ filter: /\/lib\/leanHighlight\.mjs$/ }, () => ({ path: 'color', namespace: 'selection' }));
    b.onLoad({ filter: /.*/, namespace: 'selection' }, ({ path: name }) => ({
      loader: 'jsx', resolveDir: root, contents: name === 'api'
        ? `export const getSession=async()=>structuredClone(window.fixture.detail);
           export const getCurrentFormalization=async id=>structuredClone(window.fixture.snapshots[id]);
           export const listSessions=async()=>[];
           export const sessionListSnapshot=async()=>({sessions:[],cursor:0});
           export const awaitReformalization=async()=>({});`
        : name === 'editor'
          ? `import React from 'react'; export function LiveEditor(){return <textarea aria-label="Editor draft" defaultValue="local draft"/>}`
          : name === 'draft'
            ? 'export function ReformalizationDraft(){return null}'
            : 'export const ensureHighlighter=async()=>{}; export const isHighlighterReady=()=>false; export const highlightToLines=()=>null;',
    }));
  } }],
});

const step = (id, seq, target = 'target-a', file = 'A.lean') => ({
  id, seq, path: file, formalization_id: target, code: `-- ${id}`, session_id: 'session',
  author: 'agent', created_at: '2026-10-16T12:00:00Z', check_status: 'ok', check_current: true,
});
async function setup(page) {
  await page.setContent('<div id="app"></div>');
  await page.evaluate(({ steps }) => {
    window.fixture = {
      detail: { id: 'session', messages: [], status_events: [], code_steps: steps, runs: [],
        formalizations: [{ id: 'target-a', activity: { status: 'idle' } }, { id: 'target-b', activity: { status: 'running' } }],
        latest_focus_formalization_id: 'target-a', active_run: { id: 'run', status: 'running' } },
      snapshots: { 'target-a': { formalization_id: 'target-a', revision_token: 'v1', files: steps.filter(s=>s.formalization_id==='target-a') },
        'target-b': { formalization_id: 'target-b', revision_token: 'b1', files: steps.filter(s=>s.formalization_id==='target-b') } },
    };
    window.sources = [];
    class Source {
      static OPEN = 1;
      readyState = 1; listeners = new Map();
      constructor() { window.sources.push(this); }
      close() {}
      addEventListener(name, handler) { this.listeners.set(name, handler); }
      emit(name, payload) { this.listeners.get(name)?.({ data: JSON.stringify(payload) }); }
    }
    window.EventSource = Source;
    window.fetch = async () => ({ ok: true, json: async () => ({ lifecycle_state: 'ready' }) });
  }, { steps: [step('a-old', 2), step('a-helper', 4, 'target-a', 'Helper.lean'), step('b-old', 6, 'target-b', 'B.lean')] });
  await page.addScriptTag({ content: (await bundle).outputFiles[0].text });
  await page.getByRole('button', { name: 'Open session', exact: true }).click();
  await expect(page.getByTestId('selection')).toContainText('target-a:current');
}

test('live, replayed and completed work preserves a historical revision and selected target', async ({ page }) => {
  await setup(page);
  await page.getByRole('button', { name: 'Choose history', exact: true }).click();
  await expect(page.locator('.canvas')).toContainText('-- a-old');
  await page.evaluate((incoming) => {
    window.sources.at(-1).emit('code_step', incoming);
    window.sources.at(-1).emit('code_step', { ...incoming, id: 'b-backfill', seq: 1 });
  }, step('b-new', 8, 'target-b', 'B.lean'));
  await expect(page.getByTestId('selection')).toHaveText('target-a:historical:1:5');
  await expect(page.locator('.canvas')).toContainText('-- a-old');
  await page.evaluate(() => {
    window.fixture.detail.code_steps = [...window.proof.getState().codeSteps,
      {id:'a-after',seq:10,path:'A.lean',formalization_id:'target-a',code:'-- a-after'}];
    window.fixture.detail.latest_focus_formalization_id = 'target-b';
    window.fixture.detail.active_run = null;
    window.sources.at(-1).emit('done', { status: 'proved' });
  });
  await expect(page.getByTestId('selection')).toHaveText('target-a:historical:1:6');
  await expect(page.locator('.canvas')).toContainText('-- a-old');
  // Explicit navigation still works.
  await page.getByRole('button', { name: 'Choose B', exact: true }).click();
  await expect(page.getByTestId('selection')).toContainText('target-b:historical');
});

test('snapshot refresh retains the selected file across reordering and leaves the editor mounted', async ({ page }) => {
  await setup(page);
  await page.locator('.cv-file-btn').click();
  await page.getByTitle('Helper.lean', { exact: true }).click();
  await expect(page.locator('.cv-file-name')).toHaveText('Helper.lean');
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  const editor = page.getByRole('textbox', { name: 'Editor draft' });
  await editor.fill('unsaved author text');
  await editor.evaluate(el => { el.focus(); el.setSelectionRange(3, 8); el.dataset.identity = 'original-editor'; });
  await page.evaluate(() => {
    const snap = window.fixture.snapshots['target-a'];
    snap.revision_token = 'v2'; snap.files.reverse();
    window.proof.getState().bumpFormalizationRefresh();
  });
  await expect.poll(() => page.evaluate(() => window.proof.getState().currentFormalizationSnapshot?.revision_token)).toBe('v2');
  await expect(page.locator('.cv-file-name')).toHaveText('Helper.lean');
  await expect(editor).toHaveValue('unsaved author text');
  expect(await editor.evaluate(el => [el.dataset.identity, el.selectionStart, el.selectionEnd, document.activeElement === el]))
    .toEqual(['original-editor', 3, 8, true]);
  await page.evaluate(() => { window.fixture.detail.messages = [{id:'refreshed',role:'assistant',content:'Fresh history'}]; });
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.proof.getState().messages[0]?.id)).toBe('refreshed');
  await expect(editor).toHaveValue('unsaved author text');
  await expect(page.locator('.cv-file-name')).toHaveText('Helper.lean');
  await page.evaluate(() => {
    window.fixture.snapshots['target-a'] = {formalization_id:'target-a',revision_token:'v3',files:[]};
    window.proof.getState().bumpFormalizationRefresh();
  });
  await expect(page.locator('.canvas')).toContainText('The selected file is unavailable');
  await expect(page.locator('.canvas-head')).toContainText('Helper.lean');
  await expect(editor).toHaveValue('unsaved author text');
});
