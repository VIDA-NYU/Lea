import { test, expect } from '@playwright/test';
import { build } from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const bundle = build({ stdin: { resolveDir: root, loader: 'tsx', contents: `
  import React from 'react';
  import { createRoot } from 'react-dom/client';
  import { LeanCheckRuntimeStatus } from './apps/lea-standalone/src/app/components/LeanCheckRuntimeStatus';
  createRoot(document.getElementById('status')).render(<LeanCheckRuntimeStatus sessionId="session" active={false} refreshKey={0}/>);
` }, bundle: true, write: false, platform: 'browser', format: 'iife', jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"test"' } });

for (const [initial, message] of [
  ['pending', 'Waiting for Lean runtime status'],
  ['disconnected', 'Lean runtime status unavailable · retrying'],
  ['unavailable', 'Lean check runtime unavailable'],
]) {
  test(`runtime status recovers from ${initial} while the session is idle`, async ({ page }, info) => {
    await page.setContent('<main style="max-width:720px;margin:24px auto"><textarea aria-label="Draft">Retain this draft and selection.</textarea><div id="status"></div></main>');
    await page.addStyleTag({ path: path.join(root, 'apps/lea-standalone/src/styles/lea-v2.css') });
    await page.evaluate(initial => {
      window.statusFixture = { phase: initial, requests: [] };
      window.fetch = async (url, options) => {
        const f = window.statusFixture;
        f.requests.push({ url: String(url), method: options?.method || 'GET' });
        if (url !== '/api/sessions/session/lean-check-runtime') throw new Error('Unexpected runtime request: ' + url);
        if (f.phase === 'disconnected') throw new TypeError('Connection unavailable');
        if (f.phase === 'pending') return Response.json({ detail: { error: 'projection_unavailable', message: 'Worker has not published this view yet.' } }, { status: 503 });
        return Response.json({ state: f.phase, active_cold_checks: 0 });
      };
    }, initial);
    await page.addScriptTag({ content: (await bundle).outputFiles[0].text });
    const status = page.getByRole('status');
    await expect(status).toHaveText(message);
    if (initial === 'pending') {
      for (const width of [1100, 390]) {
        await page.setViewportSize({ width, height: 700 });
        expect(await status.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
        await status.screenshot({ path: info.outputPath(`pending-${width}.png`) });
      }
    }
    const draft = page.getByRole('textbox', { name: 'Draft' });
    await draft.focus();
    await draft.evaluate(el => el.setSelectionRange(2, 12));
    await page.evaluate(() => { statusFixture.phase = 'idle'; });
    await expect(status).toHaveCount(0, { timeout: 10000 });
    await expect(draft).toBeFocused();
    await expect(draft).toHaveValue('Retain this draft and selection.');
    expect(await draft.evaluate(el => [el.selectionStart, el.selectionEnd])).toEqual([2, 12]);
    const requests = await page.evaluate(() => statusFixture.requests);
    expect(requests.length).toBeGreaterThanOrEqual(2);
    expect(requests.every(req => req.method === 'GET')).toBe(true);
  });
}
