import { test, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const origin = 'http://127.0.0.1:5197';
let vite;
test.beforeAll(async () => {
  vite = spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--config', 'apps/lea-standalone/vite.config.ts', 'apps/lea-standalone', '--host', '127.0.0.1', '--port', '5197', '--strictPort'], { cwd: root, stdio: 'ignore' });
  for (let i = 0; i < 100; i++) { try { if ((await fetch(origin)).ok) return; } catch {} await new Promise(r => setTimeout(r, 100)); }
  throw new Error('Editor Vite fixture unavailable');
});
test.afterAll(() => { vite?.kill('SIGTERM'); });
async function open(page, saving = false) {
  const state = { environment: 'env', workerEpoch: 'epoch-1', base: 'base', content: 'example : True := by\n  trivial\n', saves: [], publications: [], requests: [], resultState: 'available', pending: false, rejectRequests: null, buffers: new Map(), receipts: new Map(), holdPublication: null };
  await page.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url());
    let result;
    if (url.pathname === '/api/editor/clients/current') result = { closed: true };
    else if (url.pathname.endsWith('/editor/clients')) result = { owner_id: 'owner', credential: 'token' };
    else if (url.pathname.endsWith('/editor')) result = { path: 'Main.lean', content: state.content, environment_revision: state.environment, file_revision: state.base, worker: { state: 'available', epoch: state.workerEpoch } };
    else if (url.pathname === '/api/workspace/commands') {
      const value = req.postDataJSON(); state.saves.push(value);
      if (value.expected_revision !== state.base) return route.fulfill({status:409,json:{detail:{code:'revision_conflict',message:'Changed in another tab.',current_revision:state.base}}});
      state.base = `published-${state.saves.length}`; state.content = value.arguments.request.content;
      result = {draft_id:value.draft_id,file_revision:state.base};
      if (state.holdPublication) await state.holdPublication;
    }
    else if (url.pathname === '/api/editor/buffers') {
      const value = req.postDataJSON(); state.publications.push(value);
      result = { id: `buffer-${value.version}`, ...value, expires_at: new Date(Date.now() + 60000).toISOString() }; state.buffers.set(result.id, result);
    } else if (url.pathname === '/api/editor/requests') {
      if (state.rejectRequests) return route.fulfill({ status: 429, json: { detail: { error: state.rejectRequests } } });
      const value = req.postDataJSON(); state.requests.push(value);
      const buffer = state.buffers.get(value.buffer_id);
      result = { id: value.idempotency_key, state: state.pending ? 'claimed' : 'succeeded', current: !state.pending && state.resultState === 'available', buffer,
        result_state: state.resultState, result: { value: value.kind === 'editor.analyze' ? { diagnostics: [{ range: { start: { line: 0, character: 0 }, end: { line: 0, character: 7 } }, severity: 2, message: 'Draft diagnostic' }] } : value.kind === 'editor.hover' ? { contents: { value: 'Hover from SQL' } } : value.kind === 'editor.complete' ? { items: [{ label: 'queued_completion', insertText: 'queued_completion' }] } : { rendered: '⊢ True', goals: ['⊢ True'] } } };
      state.receipts.set(result.id, result);
    } else if (url.pathname.startsWith('/api/editor/requests/')) {
      result = { ...state.receipts.get(url.pathname.split('/').at(-1)), state: state.pending ? 'claimed' : 'succeeded', current: !state.pending && state.resultState === 'available', result_state: state.resultState };
    } else throw new Error(`Unexpected editor request ${req.method()} ${url.pathname}`);
    await route.fulfill({ json: result });
  });
  await page.route('**/src/main.tsx', route => route.fulfill({ contentType: 'application/javascript', body: '' }));
  await page.addInitScript(({saving}) => { window.addEventListener('DOMContentLoaded', async () => {
    const React = await import('/node_modules/.vite/deps/react.js');
    const dom = await import('/node_modules/.vite/deps/react-dom_client.js');
    const { createRoot } = dom.default || dom;
    const { LiveEditor } = await import('/src/app/components/LiveEditor.tsx');
    const { writeSessionFile } = await import('/src/app/lib/api.ts');
    document.body.innerHTML = '<main id="editor-fixture" style="height:800px;display:flex"></main>';
    window.editorRoot = createRoot(document.querySelector('main'));
    window.renderEditor = (sessionId = 'session') => window.editorRoot.render((React.default || React).createElement(LiveEditor, { sessionId, path: 'Main.lean', captureSelection: () => () => true,
      ...(saving ? {onSave:(content,path,base,draftId)=>writeSessionFile(base.session_id,path,content,undefined,base.formalization_id,base.formalization_revision,base.file_revision,draftId),
        onCheck:async()=>{throw new Error('Check service unavailable');}} : {}) }));
    window.renderEditor();
  }); }, {saving});
  await page.goto(origin);
  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 45000 });
  return state;
}
test('real Monaco uses queued diagnostics and goals while preserving draft, cursor and undo', async ({ page }) => {
  const sockets = []; page.on('websocket', socket => sockets.push(socket.url()));
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const state = await open(page);
  await expect(page.getByLabel('Goal analysis')).toContainText('⊢ True', { timeout: 20000 });
  const input = page.locator('.monaco-editor textarea'); await input.focus();
  await page.keyboard.press('ControlOrMeta+End'); await page.keyboard.type('-- local draft');
  await expect.poll(() => state.publications.at(-1)?.content).toContain('-- local draft');
  await page.keyboard.press('ArrowLeft');
  const before = await input.getAttribute('aria-label');
  state.environment = 'env2';
  await page.keyboard.press('ArrowLeft');
  await expect.poll(() => state.publications.at(-1)?.environment_revision).toBe('env2');
  await expect(page.locator('.view-lines')).toContainText('-- local draft');
  await expect(input).toBeFocused();
  expect(await input.getAttribute('aria-label')).toBe(before);
  await page.keyboard.press('ControlOrMeta+z');
  await expect(page.locator('.view-lines')).not.toContainText('-- local draft');
  expect(sockets.filter(url => url.includes('/lsp'))).toEqual([]);
  expect(errors).toEqual([]);
});
test('superseded and unavailable results never replace the active draft or navigate the editor', async ({ page }) => {
  const state = await open(page);
  const input = page.locator('.monaco-editor textarea'); await input.focus();
  state.pending = true; await page.keyboard.press('ControlOrMeta+End'); await page.keyboard.type('-- first');
  await expect.poll(() => state.requests.length).toBeGreaterThan(2);
  await page.keyboard.type(' newest');
  state.resultState = 'environment_changed'; state.pending = false;
  await expect(page.locator('.live-editor-bar')).toContainText('environment changed', { timeout: 15000 });
  await expect(page.locator('.view-lines')).toContainText('-- first newest');
  state.resultState = 'available'; state.workerEpoch = 'restarted';
  await page.keyboard.press('ArrowLeft');
  await expect(page.getByLabel('Goal analysis')).toContainText('⊢ True', { timeout: 15000 });
  await page.evaluate(() => window.renderEditor('other-session'));
  await expect(page.locator('.view-lines')).not.toContainText('-- first newest');
});

test('Monaco hover and completion providers consume typed results', async ({ page }) => {
  const state = await open(page);
  await page.evaluate(async () => {
    const monaco = await import('/node_modules/.vite/deps/monaco-editor.js');
    const editor = monaco.editor.getEditors()[0];
    editor.setPosition({ lineNumber: 1, column: 12 }); editor.focus();
    await editor.getAction('editor.action.showHover').run();
  });
  await expect(page.locator('.monaco-hover')).toContainText('Hover from SQL', { timeout: 10000 });
  await page.keyboard.press('Escape');
  await page.evaluate(async () => {
    const monaco = await import('/node_modules/.vite/deps/monaco-editor.js');
    const editor = monaco.editor.getEditors()[0];
    editor.setPosition({ lineNumber: 2, column: 3 });
    await editor.getAction('editor.action.triggerSuggest').run();
  });
  await expect.poll(() => state.requests.some(v => v.kind === 'editor.complete')).toBe(true);
  await expect(page.locator('.suggest-widget')).toContainText('queued_completion');
});

test('idle environment refresh and queue saturation preserve the local draft', async ({ page }) => {
  const state = await open(page);
  await expect(page.getByLabel('Goal analysis')).toContainText('⊢ True');
  const input = page.locator('.monaco-editor textarea'); await input.focus();
  state.rejectRequests = 'queue_full';
  await page.keyboard.press('ControlOrMeta+End'); await page.keyboard.type('-- keep this draft');
  await expect(page.locator('.live-editor-bar')).toContainText('queue full');
  await expect(page.locator('.view-lines')).toContainText('-- keep this draft');
  state.rejectRequests = null; state.environment = 'idle-environment-change';
  await expect.poll(() => state.publications.at(-1)?.environment_revision, { timeout: 10000 }).toBe(state.environment);
  await expect(page.getByLabel('Goal analysis')).toContainText('⊢ True');
  await expect(input).toBeFocused();
  await expect(page.locator('.view-lines')).toContainText('-- keep this draft');
});

test('a dirty Monaco draft keeps its loaded base after another tab publishes',async({page})=>{
  const state=await open(page,true);
  state.base='another-tab';state.content='-- remote publication';
  const input=page.locator('.monaco-editor textarea');await input.focus();
  await page.keyboard.press('ControlOrMeta+End');await page.keyboard.type('-- my unsaved draft');
  await expect(page.locator('.live-editor-bar')).toContainText('Changed in another tab.');
  expect(state.saves[0].expected_revision).toBe('base');
  expect(state.content).toBe('-- remote publication');
  await expect(page.locator('.view-lines')).toContainText('-- my unsaved draft');
  await expect(input).toBeFocused();
});

test('typing during publication survives its receipt and a later check failure',async({page})=>{
  const state=await open(page,true);let release;
  state.holdPublication=new Promise(resolve=>{release=resolve;});
  const input=page.locator('.monaco-editor textarea');await input.focus();
  await page.keyboard.press('ControlOrMeta+End');await page.keyboard.type('-- first');
  await expect.poll(()=>state.saves.length).toBe(1);
  await page.keyboard.type(' newest');state.holdPublication=null;release();
  await expect.poll(()=>state.saves.length).toBe(2);
  expect(state.saves[1].expected_revision).toBe('published-1');
  expect(state.saves[1].arguments.request.content).toContain('-- first newest');
  await expect(page.locator('.live-editor-bar')).toContainText('Check service unavailable');
  await expect(page.locator('.view-lines')).toContainText('-- first newest');
  await expect(input).toBeFocused();
});
