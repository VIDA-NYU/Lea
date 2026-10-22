import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from '../../apps/overleaf-extension/companion/server.mjs';

const extension = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../apps/overleaf-extension/extension');

// Run the shipped content script/styles and lazy browser modules. Only the
// Overleaf host and SQL transport are fixtures; no worker/provider is started.
async function setup(page, { targetCount = 1, leaseMs = 0, runtimePhase = null, completionPhase = null, batchProgress = null, identityTransportBase = null, badgePhase = null, reviewPhase = null, blueprint = null } = {}) {
  await page.route('https://lea-fixture.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/sql-identity' && identityTransportBase) {
      const response = await fetch(identityTransportBase + '/project/identity' + url.search);
      return route.fulfill({ status: response.status, contentType: 'application/json', body: await response.text() });
    }
    if (url.pathname.startsWith('/extension/')) {
      const file = url.pathname.slice('/extension/'.length);
      if (!/^[\w.-]+\.mjs$/.test(file)) return route.fulfill({ contentType: 'text/javascript', body: '' });
      return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(extension, file), 'utf8') });
    }
    await route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Batch Notes - Overleaf</title><main style="padding:24px;font:16px system-ui">Overleaf fixture</main>' });
  });
  await page.goto('https://lea-fixture.test/project/project-1');
  await page.evaluate(({ targetCount, leaseMs, runtimePhase, completionPhase, batchProgress, identityTransportBase, badgePhase, reviewPhase, blueprint }) => {
    window.fixture = { offline: false, runtimePhase, completionPhase, batchProgress, badgePhase, reviewPhase, blueprint, reviewVersion: 1, decisions: [], batchAccepted: false,
      calls: [], owner: leaseMs ? 'previous-tab' : null, generation: leaseMs ? 1 : 0,
      leaseUntil: Date.now() + leaseMs, sequence: 0, publications: [],
      targets: Array.from({ length: targetCount }, (_, i) => ({ targetKind: reviewPhase ? 'definition' : 'theorem',
        targetLabel: targetCount === 1 ? 'demo_theorem' : 'target_' + i, targetText: 'A theorem.', targetUses: [] })) };
    const area = { get: async defaults => ({ ...defaults }), set: async () => {}, remove: async () => {} };
    window.chrome = { runtime: { id: 'fixture', getURL: file => 'https://lea-fixture.test/extension/' + file,
      sendMessage: (_message, callback) => callback?.({ ok: true }) },
      storage: { sync: area, local: area, onChanged: { addListener() {} } } };
    window.EventSource = class {
      static OPEN = 1; listeners = new Map();
      constructor() { window.fixture.events = this; }
      addEventListener(name, fn) { this.listeners.set(name, fn); }
      close() {}
    };
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (url, options = {}) => {
      const pathname = new URL(url, location.href).pathname;
      const input = options.body ? JSON.parse(options.body) : {};
      const f = window.fixture;
      f.calls.push({ pathname, method: options.method || 'GET', input });
      const json = (value, status = 200) => Response.json(value, { status });
      if (pathname.startsWith('/lean-pane/review/')) return json({ message: 'Result review is retired.' }, 410);
      if (pathname === '/settings') return window.fixture.offline ? json({ message: 'API starting' }, 503)
        : json({ leaTexMirrorEnabled: false });
      if (pathname === '/project/graph' && f.blueprint) return json(f.blueprint);
      if (pathname === '/source-context/publisher') {
        const live = f.leaseUntil > Date.now();
        if (f.owner && !(f.owner === input.client_id && live) && (live || input.expected_generation !== f.generation)) {
          return json({ detail: { code: 'publisher_conflict', generation: f.generation,
            retry_after_ms: Math.max(0, f.leaseUntil - Date.now()), message: 'Another tab holds source publication' } }, 409);
        }
        if (f.owner !== input.client_id || !live) {
          f.owner = input.client_id; f.generation++; f.sequence = 0; f.leaseUntil = Date.now() + 90000;
        }
        return json({ generation: f.generation, sequence: f.sequence });
      }
      if (pathname === '/source-context/publish') {
        if (input.client_id !== f.owner || input.generation !== f.generation || input.sequence <= f.sequence) {
          return json({ message: 'Publication lost its owning lease or sequence' }, 409);
        }
        f.sequence = input.sequence; f.leaseUntil = Date.now() + 90000;
        const ref = 'frozen-source-' + f.sequence;
        f.publications.push({ client: input.client_id, sequence: input.sequence, ref });
        return json({ source_context_ref: ref });
      }
      const inventory = { ok: true, rootFile: 'main.tex', diagnostics: [], items: f.targets.map((target, i) => ({
        id: 'theorem:' + target.targetLabel + ':0', kind: f.reviewPhase ? 'definition' : 'theorem', label: target.targetLabel, status: 'missing-stub',
        sourceFile: 'main.tex', sourceStartLine: 1, sourceEndLine: 4, naturalLanguageLatex: target.targetText,
        leanKind: f.reviewPhase ? 'def' : 'theorem', leanDeclarationName: f.reviewPhase ? 'Ua' : target.targetLabel, formalizable: true,
        ...(f.reviewPhase ? { status: f.reviewPhase === 'approved' ? 'defined' : 'needs-review',
          leanArtifactContent: 'def Ua : Nat := 0',
          leanCheck: { status: 'checked' }, review: { eligible: false, requested: false },
          leaStatus: { confidence: 'medium', attention: 'warning', assessment: { confidence: 'medium', scope: 'statement_and_proof', summary: 'The definition checks; the source omits a boundary case.' } } } : {}),
        ...(f.completionPhase ? { inProgress: false, status: f.completionPhase === 'pending' ? 'synchronizing' : 'valid',
          projection: { state: f.completionPhase === 'pending' ? 'inputs_changed' : 'observed' },
          leanCheck: { status: f.completionPhase === 'pending' ? 'synchronizing' : 'checked' } } : {}),
        ...(f.runtimePhase ? { leaSessionId: 'runtime-session' } : {})
      })), ...(f.runtimePhase ? { leanCheckRuntime: { state: 'unavailable' }, leanCheckRuntimeMessage: 'Lean check runtime unavailable', leanCheckRuntimePolling: true } : {}) };
      const batch = () => ({ ok: true, batchId: 'progress-batch', operation: 'formalize',
        done: f.batchProgress === targetCount, running: f.batchProgress < targetCount, pausedOn: null,
        items: f.targets.map((target, i) => ({ targetKind: target.targetKind, targetLabel: target.targetLabel,
          state: i < f.batchProgress ? 'formalized' : i === f.batchProgress ? 'running' : 'pending' })) });
      if (f.batchAccepted) inventory.items = inventory.items.map((item, i) => ({ ...item,
        status: i < f.batchProgress ? 'valid' : i === f.batchProgress ? 'in-progress' : 'synchronizing',
        inProgress: i === f.batchProgress, projection: i === f.batchProgress ? undefined
          : { state: i < f.batchProgress ? 'observed' : 'inputs_changed' }
      }));
      if (pathname === '/lean-pane/repair/status' && f.batchProgress !== null) return json(batch());
      if (pathname === '/lean-pane/check-runtime/runtime-session') {
        if (f.runtimePhase === 'offline') throw new TypeError('Status connection lost');
        const runtime = f.runtimePhase === 'pending'
          ? { state: 'unavailable', observation: { state: 'pending', message: 'The worker has not published this view yet.' } }
          : { state: f.runtimePhase };
        return json({ runtime, message: f.runtimePhase === 'pending' ? 'Waiting for Lean runtime status'
          : f.runtimePhase === 'unavailable' ? 'Lean check runtime unavailable' : '', polling: f.runtimePhase !== 'ready' });
      }
      if (pathname === '/lean-pane/manifest' && f.queueNextInventory) {
        f.queueNextInventory = false; f.inventoryQueuedAt = Date.now();
        return json({ workspace_command: true, command: { id: 'inventory-refresh', state: 'queued' } }, 202);
      }
      if (pathname === '/commands/inventory-refresh') return json({ workspace_command: true, command: {
        id: 'inventory-refresh', state: Date.now() - f.inventoryQueuedAt < 11500 ? 'queued' : 'succeeded',
        result: { statusCode: 200, body: inventory }
      } });
      if (pathname === '/lean-pane/manifest') return json(inventory);
      if (pathname === '/project/identity' && identityTransportBase) return nativeFetch('/sql-identity' + new URL(url, location.href).search);
      if (pathname.startsWith('/project/identity')) return json({ ok: true, identity: { exists: true, projectId: 'adapter-project', projectName: 'Batch Notes', namespace: 'Lea.BatchNotes' } });
      if (pathname === '/statuses' && f.stableTagPhase) {
        const p=f.stableTagPhase;
        if(p==='read_error')return json({message:'Status snapshot temporarily unavailable'},503);
        const status=p==='running'?{status:'in_progress'}:p==='finished'?{status:'formalized',projection:{state:'observed'}}
          :p==='known'?{status:'unformalized',projection:{state:'observed'}}
          :{status:'unknown',projection:{state:p==='pending'?'inputs_changed':'observation_expired'},
            lastObservedResult:{validity_status:p==='finished_expired'?'proved':'planned'}};
        return json({statuses:Object.fromEntries(f.targets.map(t=>[`${t.targetKind}:${t.targetLabel}`,status]))});
      }
      if (pathname === '/statuses' && f.reviewPhase) return json({ statuses: Object.fromEntries(f.targets.map(t => [
        `${t.targetKind}:${t.targetLabel}`, { status: f.reviewPhase === 'approved' ? 'formalized' : 'needs_review', review: { eligible: true }, leanCheckPassed: true }
      ])) });
      if (pathname === '/statuses') return json({ statuses: f.badgePhase
        ? Object.fromEntries(f.targets.map((t,i)=>[`${t.targetKind}:${t.targetLabel}`, i===1
          ? {status:'in_progress',turnProgress:{current:3,max:100}}
          : f.badgePhase==='observed' ? {status:i===0?'formalized':'unformalized',projection:{state:'observed'}}
            : {status:'unknown',projection:{state:'inputs_changed'},lastObservedResult:i===0
              ? {validity_status:'proved',observed_at:'2026-10-18T15:12:00Z'}:null}]))
        : { demo_theorem: { status: 'unformalized' } } });
      if (pathname === '/formalize/all' && !input.items?.every(item => f.publications.some(p => p.ref === item.source_context_ref))) return json({ message: 'Source was not published' }, 422);
      if (pathname === '/formalize/all' && f.batchProgress !== null) { f.batchAccepted = true; return json(batch()); }
      if (pathname === '/formalize/all' || pathname === '/commands/startup-click') return json({ workspace_command: true,
        command: { id: 'startup-click', state: 'queued' } }, pathname === '/formalize/all' ? 202 : 200);
      if (pathname.endsWith('/download/zip')) {
        if (f.offline) return new Response('', { status: 503 });
        // A complete one-file archive; the content script overlays the live
        // main.tex buffer. Batch admission requires complete source coverage.
        const zip = Uint8Array.from(atob('UEsDBBQAAAAAAPW5TV0AAAAAAAAAAAAAAAAIAAAAbWFpbi50ZXhQSwECFAMUAAAAAAD1uU1dAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAgAEAAAAAbWFpbi50ZXhQSwUGAAAAAAEAAQA2AAAAJgAAAAAA'), c => c.charCodeAt(0));
        return new Response(zip, { headers: { 'Content-Type': 'application/zip' } });
      }
      return json({ ok: true, reports: [], jobs: [], updates: [] });
    };
  }, { targetCount, leaseMs, runtimePhase, completionPhase, batchProgress, identityTransportBase, badgePhase, reviewPhase, blueprint });
  for (const file of ['lea-theme.css', 'content.css']) await page.addStyleTag({ content: await fs.readFile(path.join(extension, file), 'utf8') });
  await page.addScriptTag({ content: await fs.readFile(path.join(extension, 'content.js'), 'utf8') });
  await page.evaluate(() => window.postMessage({ type: 'OL_LEAN_TARGETS_VISIBLE', activePath: 'main.tex',
    activeTex: fixture.reviewPhase ? '\\begin{definition}\n% lea: define label=demo_theorem\nA theorem.\n\\end{definition}'
      : '\\begin{theorem}\n% lea: formalize label=demo_theorem\nA theorem.\n\\end{theorem}',
    targets: window.fixture.targets, diagnostics: [] }, '*'));
}

for (const width of [1280, 760]) test(`Lea Status describes a checked definition without review at ${width}px`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 900 });
  await setup(page, { reviewPhase: 'approved' });
  await page.getByRole('button', { name: /Lean project pane/ }).click();
  if (width === 760) await page.getByRole('separator', { name: 'Resize Lean pane' }).press('Home');
  await page.locator('.ol-lean-project-tree-row').filter({ hasText: 'main.tex' }).click();
  await page.locator('.ol-lean-project-item-header').click();
  const pane = page.locator('.ol-lean-project-pane');
  const openChat = pane.getByRole('button', { name: 'Open in LeaChat', exact: true });
  await expect(openChat).toBeDisabled();
  await expect(openChat).toHaveAttribute('title', 'No LeaChat session is available yet.');
  await expect(pane).toContainText(/Lean Check:\s*checked/);
  await expect(pane).toContainText('Medium confidence');
  await expect(pane).toContainText('The definition checks; the source omits a boundary case.');
  await expect(pane).not.toContainText(/Needs review|Review result|Approve definition|awaits approval/);
  await expect(page.locator('.ol-lean-review-panel')).toHaveCount(0);
  await expect.poll(() => pane.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('lea-status-without-review.png') });
  expect(await page.evaluate(() => fixture.calls.filter(c => /review\/(context|decision)|^\/formalize(?:\/all)?$|^\/api\/runs$/.test(c.pathname)))).toHaveLength(0);
});

for (const width of [1280, 760]) test(`Blueprint refresh and keyboard selection at ${width}px`, async ({page}, info) => {
  await page.setViewportSize({width, height:900});
  const node={key:'demo_double_add',kind:'theorem',lean:'Lea.Demo.demo_double_add',uses:[],
    status:'proved',verified:false,statement:'(m n : Nat) : D (m + n) = D m + D n'};
  await setup(page,{blueprint:{nodes:[node],edges:[],projection:{state:'inputs_changed'}}});
  await page.getByRole('button',{name:/Lean project pane/}).click();
  if(width===760)await page.getByRole('separator',{name:'Resize Lean pane'}).press('Home');
  await page.getByRole('tab',{name:'Blueprint',exact:true}).click();
  const pane=page.locator('.ol-lean-project-pane');
  await expect(pane).toContainText('Waiting for the current graph');
  const graphNode=pane.getByRole('button',{name:/^demo_double_add, theorem/});
  await graphNode.press('Enter');
  await expect(graphNode).toHaveAttribute('aria-pressed','true');
  await expect(pane.getByRole('region',{name:'Selected blueprint node'})).toContainText('(m n : Nat)');
  await page.evaluate(()=>{fixture.blueprint={...fixture.blueprint,projection:{state:'observed'}};});
  await expect(pane).not.toContainText('Waiting for the current graph');
  await expect(graphNode).toHaveAttribute('aria-pressed','true');
  await graphNode.press('Space');
  await expect(graphNode).toHaveAttribute('aria-pressed','false');
  await graphNode.press('Enter');
  await expect.poll(()=>pane.evaluate(el=>el.scrollWidth<=el.clientWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('blueprint-ready-keyboard.png')});
});

test('the intentionally disabled Chat and Edit controls remain disabled', async ({page}) => {
  await setup(page,{reviewPhase:'approved'});
  await page.getByRole('button',{name:/Lean project pane/}).click();
  await page.locator('.ol-lean-project-tree-row').filter({hasText:'main.tex'}).click();
  await page.locator('.ol-lean-project-item-header').click();
  const pane=page.locator('.ol-lean-project-pane');
  await expect(pane.getByRole('button',{name:'Chat',exact:true})).toBeDisabled();
  await pane.getByRole('button',{name:'More actions',exact:true}).click();
  await expect(pane.getByRole('menuitem',{name:'Edit',exact:true})).toBeDisabled();
  expect(await page.evaluate(()=>fixture.calls.filter(call=>/chat\/send|\/edit\/save|\/review\//.test(call.pathname)))).toHaveLength(0);
});

test('a saved review result has no approval action or new review request', async ({ page }) => {
  await setup(page, { reviewPhase: 'ready' });
  await page.getByRole('button', { name: /Lean project pane/ }).click();
  await page.locator('.ol-lean-project-tree-row').filter({ hasText: 'main.tex' }).click();
  await expect(page.getByRole('button', { name: 'Review result', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Re-formalize', exact: true }).first()).toBeVisible();
  await expect(page.locator('.ol-lean-project-pane')).not.toContainText(/Needs review|awaits approval/);
  expect(await page.evaluate(() => fixture.calls.filter(c => c.pathname.startsWith('/lean-pane/review/')))).toHaveLength(0);
});

test('known LaTeX tag states do not change when SQL snapshots refresh or expire',async({page},info)=>{
  await setup(page);
  const observe=phase=>page.evaluate(phase=>{fixture.stableTagPhase=phase;window.postMessage({type:'OL_LEAN_TARGETS_VISIBLE',
    activePath:'main.tex',activeTex:'\\begin{theorem}\nTrue.\n\\end{theorem}',
    targets:fixture.targets.map(t=>({...t,coords:{left:window.innerWidth-80,top:70}})),diagnostics:[]},'*');},phase);
  const badge=page.locator('.ol-lean-status[data-target-key="theorem:demo_theorem"]');
  await page.setViewportSize({width:1280,height:900});
  await observe('known');await expect(badge).toHaveText('unformalized');
  await page.evaluate(()=>{const range=document.createRange();range.selectNodeContents(document.querySelector('main'));
    getSelection().removeAllRanges();getSelection().addRange(range);});
  for(const phase of ['pending','expired','known','read_error','known','expired']){
    const before=await page.evaluate(()=>fixture.calls.filter(c=>c.pathname==='/statuses').length);
    await observe(phase);
    await expect.poll(()=>page.evaluate(()=>fixture.calls.filter(c=>c.pathname==='/statuses').length)).toBeGreaterThan(before);
    await expect(badge).toHaveText('unformalized');
    await expect(page.locator('.ol-lean-status-unknown')).toHaveCount(0);
  }
  await page.screenshot({path:info.outputPath('stable-tag-desktop.png')});
  await page.setViewportSize({width:480,height:740});await observe('expired');
  await expect.poll(()=>badge.evaluate(el=>el.getBoundingClientRect().right<=window.innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('stable-tag-narrow.png')});
  await observe('running');await expect(badge).toContainText('in progress');
  await observe('finished');await expect(badge).toHaveText('formalized');
  await observe('finished_expired');await expect(badge).toHaveText('formalized');
  expect(await page.evaluate(()=>getSelection().toString())).toBe('Overleaf fixture');
  expect(await page.evaluate(()=>fixture.calls.filter(c=>/formalize|cancel|restart|interrupt|source-context/.test(c.pathname)).length)).toBe(0);
});

test('LaTeX badges distinguish recorded results, active work and awaiting SQL status', async ({page},info) => {
  await setup(page,{targetCount:3,badgePhase:'pending',leaseMs:90000});
  const publishTargets=()=>page.evaluate(()=>window.postMessage({type:'OL_LEAN_TARGETS_VISIBLE',activePath:'main.tex',
    activeTex:'\\begin{theorem}\nTrue.\n\\end{theorem}',targets:fixture.targets.map((t,i)=>({...t,coords:{left:window.innerWidth-80,top:60+i*50}})),diagnostics:[]},'*'));
  await page.setViewportSize({width:1280,height:900});
  await publishTargets();
  const recorded=page.locator('.ol-lean-status[data-target-key="theorem:target_0"]');
  await expect(recorded).toHaveText('formalized');
  await expect(page.locator('.ol-lean-status[data-target-key="theorem:target_1"]')).toContainText('in progress');
  await expect(page.locator('.ol-lean-status[data-target-key="theorem:target_2"]')).toHaveText('awaiting status');
  await expect(page.locator('.ol-lean-status-formalized')).toHaveCount(1);
  await page.screenshot({path:info.outputPath('recorded-badges-desktop.png')});
  await page.setViewportSize({width:480,height:740});
  await publishTargets();
  await expect.poll(()=>recorded.evaluate(el=>el.getBoundingClientRect().right<=window.innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('recorded-badges-narrow.png')});
  await page.evaluate(()=>{const selection=getSelection();const range=document.createRange();range.selectNodeContents(document.querySelector('main'));
    selection.removeAllRanges();selection.addRange(range);fixture.selected=selection.toString();fixture.badgePhase='observed';});
  await expect(recorded).toHaveText('formalized',{timeout:12000});
  await expect(page.locator('.ol-lean-status[data-target-key="theorem:target_2"]')).toHaveText('unformalized');
  expect(await page.evaluate(()=>getSelection().toString())).toBe('Overleaf fixture');
  expect(await page.evaluate(()=>fixture.calls.filter(c=>/\/source-context\/(publisher|publish)$/.test(c.pathname)).length)).toBe(0);
  expect(await page.evaluate(()=>fixture.owner)).toBe('previous-tab');
  expect(await page.evaluate(()=>fixture.calls.filter(c=>/formalize|cancel|restart|interrupt/.test(c.pathname)).length)).toBe(0);
});

test('completed Overleaf proofs await SQL statuses without false errors or missing results', async ({ page }, info) => {
  await setup(page, { targetCount: 9, completionPhase: 'pending' });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('button', { name: 'Open Lean project pane', exact: true }).click();
  const pending = page.locator('.ol-lean-project-progress-segment-unconfirmed');
  await expect(pending).toHaveAttribute('data-count', '9');
  await expect(pending).toHaveAttribute('title', /Awaiting status: 9 of 9/);
  await expect(page.locator('.ol-lean-project-progress-segment-unformalized')).toHaveCount(0);
  await expect(page.locator('.ol-lean-project-progress-segment-failed')).toHaveCount(0);
  await page.locator('.ol-lean-project-tree-row').filter({ hasText: 'main.tex' }).click();
  await expect(page.locator('.ol-lean-project-check-chip-synchronizing')).toHaveCount(9);
  await expect(page.locator('.ol-lean-project-check-chip-error')).toHaveCount(0);
  await expect(page.getByText('Updating proof statuses.', { exact: false })).toBeVisible();
  await page.locator('.ol-lean-project-pane').screenshot({ path: info.outputPath('synchronizing-desktop.png') });
  await page.setViewportSize({ width: 480, height: 740 });
  const pane = page.locator('.ol-lean-project-pane');
  expect(await pane.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await pane.screenshot({ path: info.outputPath('synchronizing-narrow.png') });
  await page.evaluate(() => { fixture.completionPhase = 'observed'; });
  await expect(page.locator('.ol-lean-project-check-chip-checked')).toHaveCount(9, { timeout: 12000 });
  await expect(pending).toHaveCount(0);
  await expect(page.locator('.ol-lean-project-pane-synchronizing')).toHaveCount(0);
  expect(await page.evaluate(() => fixture.calls.filter(call => call.pathname === '/formalize/all').length)).toBe(0);
});

test('active batch and file status reconcile together with pending SQL explicitly unfilled', async ({ page }, info) => {
  await setup(page, { targetCount: 9, batchProgress: 0 });
  await page.setViewportSize({ width: 1280, height: 1700 });
  await page.evaluate(() => fixture.events.onopen?.());
  await page.getByRole('button', { name: 'Open Lean project pane', exact: true }).click();
  await page.getByRole('button', { name: 'Formalize all (9)', exact: true }).click();
  const batch = page.locator('.ol-lean-batch-queue');
  await expect(batch.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0');
  await expect(page.locator('.ol-lean-project-progress-summary')).toHaveText('0 verified · 1 running · 8 awaiting SQL status');
  await expect(page.locator('.ol-lean-project-progress-in-progress')).toHaveCount(0);
  await page.evaluate(() => { fixture.batchProgress = 1; });
  await expect(batch.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '1', { timeout: 12000 });
  await expect(batch).toContainText('Current · 2 of 9');
  await expect(page.locator('.ol-lean-project-progress-summary')).toHaveText('1 verified · 1 running · 7 awaiting SQL status');
  const waiting = page.locator('.ol-lean-project-progress-segment-unconfirmed');
  const appearance = await waiting.evaluate(el => ({ background: getComputedStyle(el).backgroundColor,
    borderStyle: getComputedStyle(el).borderTopStyle, image: getComputedStyle(el).backgroundImage }));
  expect(appearance.borderStyle).toBe('dashed');
  expect(appearance.image).toBe('none');
  await page.locator('.ol-lean-project-pane').screenshot({ path: info.outputPath('paired-progress-desktop.png') });
  await page.setViewportSize({ width: 480, height: 740 });
  await page.locator('.ol-lean-project-progress-summary').scrollIntoViewIfNeeded();
  const pane = page.locator('.ol-lean-project-pane');
  expect(await pane.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await pane.screenshot({ path: info.outputPath('paired-progress-narrow.png') });
  expect(await page.evaluate(() => fixture.calls.filter(c => /cancel|restart|interrupt/.test(c.pathname)).length)).toBe(0);
  expect(await page.evaluate(() => fixture.calls.filter(c => c.pathname === '/formalize/all').length)).toBe(1);
});

test('Overleaf retains a startup click, confirms SQL acceptance, and preserves selection', async ({ page }, info) => {
  await setup(page);
  await page.getByRole('button', { name: 'Open Lean project pane', exact: true }).click();
  const start = page.getByRole('button', { name: 'Formalize all (1)', exact: true });
  await expect(start).toBeVisible();
  await page.evaluate(() => {
    window.fixture.offline = true;
    const tree = document.querySelector('.ol-lean-project-tree');
    const range = document.createRange(); range.selectNodeContents(tree);
    window.getSelection().removeAllRanges(); window.getSelection().addRange(range);
    window.fixture.selected = window.getSelection().toString();
  });
  await start.evaluate(button => { button.click(); button.click(); });
  const admission = page.locator('.ol-lean-batch-admission');
  await expect(admission).toContainText('Waiting for a connection.');
  await expect(admission.getByRole('button', { name: 'Cancel request', exact: true })).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 800 });
  await admission.screenshot({ path: info.outputPath('waiting-desktop.png') });
  await page.evaluate(() => { window.fixture.offline = false; });
  await admission.getByRole('button', { name: 'Retry connection now', exact: true }).click();
  await expect(admission).toContainText('Queued in the database.');
  // Pointer clicks intentionally change browser selection. Select the source
  // again and qualify automatic receipt updates without another user action.
  await page.evaluate(() => {
    const range = document.createRange(); range.selectNodeContents(document.querySelector('.ol-lean-project-tree'));
    getSelection().removeAllRanges(); getSelection().addRange(range);
    window.fixture.selected = getSelection().toString();
  });
  await page.waitForFunction(() => window.fixture.calls.filter(call => call.pathname === '/commands/startup-click').length >= 1);
  expect(await page.evaluate(() => getSelection().toString())).toBe(await page.evaluate(() => window.fixture.selected));
  expect(await page.evaluate(() => window.fixture.calls.filter(call => call.pathname === '/formalize/all').length)).toBe(1);
  await page.setViewportSize({ width: 480, height: 740 });
  await expect(admission).toBeVisible();
  expect(await admission.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await admission.screenshot({ path: info.outputPath('queued-narrow.png') });
  const contrast = await admission.evaluate(el => {
    const style = getComputedStyle(el);
    return { foreground: style.color, background: style.backgroundColor };
  });
  await info.attach('admission-colors', { body: JSON.stringify(contrast), contentType: 'application/json' });
});


test('Overleaf retains a nine-target batch through an active publisher lease without self-conflicts', async ({ page }, info) => {
  await setup(page, { targetCount: 9, leaseMs: 2500 });
  await page.getByRole('button', { name: 'Open Lean project pane', exact: true }).click();
  await page.getByRole('button', { name: 'Formalize all (9)', exact: true }).click();
  const admission = page.locator('.ol-lean-batch-admission');
  await expect(admission).toContainText('Waiting for the source publication lease.');
  const claimCount = () => page.evaluate(() => fixture.calls.filter(call => call.pathname === '/source-context/publisher').length);
  expect(await claimCount()).toBe(1);
  await admission.getByRole('button', { name: 'Retry connection now', exact: true }).evaluate(button => { button.click(); button.click(); });
  await expect(admission).toContainText('Waiting for the source publication lease.');
  expect(await claimCount()).toBe(1);
  await page.setViewportSize({ width: 480, height: 740 });
  expect(await admission.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await admission.screenshot({ path: info.outputPath('publisher-lease-waiting.png') });
  await expect(admission).toContainText('Queued in the database.');
  const evidence = await page.evaluate(() => ({
    clients: [...new Set(fixture.calls.filter(call => call.pathname === '/source-context/publisher').map(call => call.input.client_id))],
    publications: fixture.publications,
    submissions: fixture.calls.filter(call => call.pathname === '/formalize/all')
  }));
  expect(evidence.clients).toHaveLength(1);
  expect(evidence.publications).toHaveLength(9);
  expect(evidence.publications.map(p => p.sequence)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
  expect(evidence.submissions).toHaveLength(1);
  expect(evidence.submissions[0].input.items).toHaveLength(9);
});


test('a slow queued inventory refresh shares its receipt and preserves selected source', async ({ page }, info) => {
  await setup(page);
  await page.getByRole('button', { name: 'Open Lean project pane', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Formalize all (1)', exact: true })).toBeVisible();
  const before = await page.evaluate(() => fixture.calls.filter(call => call.pathname === '/lean-pane/manifest').length);
  await page.evaluate(() => {
    fixture.queueNextInventory = true;
    const tree = document.querySelector('.ol-lean-project-tree');
    const range = document.createRange(); range.selectNodeContents(tree);
    getSelection().removeAllRanges(); getSelection().addRange(range);
    fixture.selectedSource = getSelection().toString();
    fixture.selectedTree = tree;
  });
  const refresh = page.getByRole('button', { name: 'Refresh Lean pane', exact: true });
  await refresh.evaluate(button => button.click());
  const warning = page.locator('.ol-lean-project-pane-refresh-warning');
  await expect(warning).toHaveCount(0);
  await refresh.evaluate(button => { for (let i = 0; i < 10; i++) button.click(); });
  await page.evaluate(() => {
    const listener = fixture.events.listeners.get('jobs-changed');
    for (let i = 0; i < 10; i++) listener({ data: '{}' });
  });
  await page.setViewportSize({ width: 480, height: 740 });
  await page.locator('.ol-lean-project-pane').screenshot({ path: info.outputPath('quiet-inventory-refresh.png') });
  await page.waitForFunction(() => fixture.calls.filter(call => call.pathname === '/commands/inventory-refresh').length >= 10);
  await expect(warning).toHaveCount(0);
  expect(await page.evaluate(() => fixture.calls.filter(call => call.pathname === '/lean-pane/manifest').length)).toBe(before + 1);
  await expect(warning).toHaveCount(0, { timeout: 10000 });
  expect(await page.evaluate(() => getSelection().toString())).toBe(await page.evaluate(() => fixture.selectedSource));
  expect(await page.evaluate(() => document.querySelector('.ol-lean-project-tree') === fixture.selectedTree)).toBe(true);
  expect(await page.evaluate(() => fixture.calls.filter(call => call.pathname === '/formalize/all').length)).toBe(0);
});

test('current SQL runtime status replaces an old inventory alarm and recovers without moving selection', async ({ page }, info) => {
  await setup(page, { runtimePhase: 'pending' });
  await page.getByRole('button', { name: 'Open Lean project pane', exact: true }).click();
  const runtime = page.locator('.ol-lean-project-pane-runtime');
  await expect(runtime).toHaveText('Waiting for Lean runtime status');
  await expect(page.locator('.ol-lean-project-pane-diagnostics')).toHaveCount(0);
  await runtime.screenshot({ path: info.outputPath('pending-runtime-desktop.png') });
  await page.setViewportSize({ width: 480, height: 740 });
  expect(await runtime.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await runtime.screenshot({ path: info.outputPath('pending-runtime-narrow.png') });
  await page.evaluate(() => {
    const tree = document.querySelector('.ol-lean-project-tree');
    const range = document.createRange(); range.selectNodeContents(tree);
    getSelection().removeAllRanges(); getSelection().addRange(range);
    fixture.selectedTree = tree; fixture.selectedSource = getSelection().toString();
    fixture.runtimePhase = 'ready';
  });
  await page.waitForFunction(() => fixture.calls.filter(call => call.pathname === '/lean-pane/check-runtime/runtime-session').length >= 2);
  expect(await page.evaluate(() => document.querySelector('.ol-lean-project-tree') === fixture.selectedTree)).toBe(true);
  expect(await page.evaluate(() => getSelection().toString())).toBe(await page.evaluate(() => fixture.selectedSource));
  // Clearing selection permits the existing deferred snapshot repaint.
  await page.evaluate(() => { getSelection().removeAllRanges(); document.dispatchEvent(new Event('selectionchange')); });
  await expect(runtime).toHaveCount(0);
  await expect(page.locator('.ol-lean-project-pane-diagnostics')).toHaveCount(0);
  await page.evaluate(() => { fixture.runtimePhase = 'unavailable'; });
  await page.getByRole('button', { name: 'Refresh Lean pane', exact: true }).evaluate(button => button.click());
  await expect(page.locator('.ol-lean-project-pane-diagnostics')).toHaveText('Lean check runtime unavailable');
  expect(await page.evaluate(() => fixture.calls.filter(call => call.pathname.includes('check-runtime')).every(call => call.method === 'GET'))).toBe(true);
  expect(await page.evaluate(() => fixture.calls.filter(call => call.pathname === '/formalize/all').length)).toBe(0);
});

test('the header discovers a newly created SQL namespace through the real public transport', async ({ page }, info) => {
  let created = false;
  const calls = [];
  const namespace = 'Lea.FailInduce1OnlineLaTeXEditorOverleaf';
  const server = await createServer({ fetchImpl: async (url, options) => {
    calls.push({ url, method: options.method });
    if (url.endsWith('/api/health')) return Response.json({ capabilities: { database_boundary: { version: 1,
      ui_access: 'database', command_direction: 'ui_to_worker', processes: 'separate',
      queries: 1, changes: 1, commands: 1, source_publications: 1, editor: 1 } } });
    expect(url).toContain('/api/projects/by-slug/project-1/identity');
    if (!created) return Response.json({ detail: 'No Lea project exists for this document yet.' }, { status: 404 });
    return Response.json({ exists: true, projectId: 'sql-project', overleafProjectId: 'project-1', slug: 'project-1',
      projectName: 'FailInduce1', nameSource: 'overleaf', namespace, namespaceRevision: 1 });
  } });
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    await setup(page, { identityTransportBase: 'http://127.0.0.1:' + server.address().port });
    await page.getByRole('button', { name: 'Open Lean project pane', exact: true }).click();
    await expect(page.getByText('Namespace not created yet', { exact: true })).toBeVisible();
    await page.evaluate(() => {
      const tree = document.querySelector('.ol-lean-project-tree');
      const range = document.createRange(); range.selectNodeContents(tree);
      getSelection().removeAllRanges(); getSelection().addRange(range);
      fixture.selectedTree = tree; fixture.selectedSource = getSelection().toString();
    });
    created = true;
    await page.getByRole('button', { name: 'Refresh Lean pane', exact: true }).evaluate(button => button.click());
    await expect(page.getByText(namespace, { exact: true })).toBeVisible();
    await expect(page.getByText('Namespace not created yet', { exact: true })).toHaveCount(0);
    await expect(page.locator('.ol-lean-project-pane-header h2')).toContainText('FailInduce1');
    expect(await page.evaluate(() => document.querySelector('.ol-lean-project-tree') === fixture.selectedTree)).toBe(true);
    expect(await page.evaluate(() => getSelection().toString())).toBe(await page.evaluate(() => fixture.selectedSource));
    await page.setViewportSize({ width: 480, height: 740 });
    await page.locator('.ol-lean-project-pane-header').screenshot({ path: info.outputPath('database-namespace-header.png') });
    expect(calls.every(call => call.method === 'GET')).toBe(true);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});
