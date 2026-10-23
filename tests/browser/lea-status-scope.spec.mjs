import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const extension = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../apps/overleaf-extension/extension');

test('Lea Status shows the assessed portion, omitted identity and reconciled document context', async ({ page }) => {
  await page.route('https://lea-status.test/**', async route => {
    const filename = new URL(route.request().url()).pathname.slice(1);
    if (/^[\w.-]+\.mjs$/.test(filename)) {
      return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(extension, filename), 'utf8') });
    }
    return route.fulfill({ contentType: 'text/html', body: '<!doctype html><main></main>' });
  });
  await page.goto('https://lea-status.test/');
  await page.evaluate(async () => {
    const { renderLeaStatus } = await import('/leaStatusView.mjs');
    const item = { id: 'ua', formalizationId: 'ua', leaStatus: {
      assessment: { confidence: 'medium', scope: 'partial_artifact', summary: 'The RHS is encoded.',
        confidence_reason: 'The source identity and its intended domain still need justification.',
        correspondence: {
          scope_summary: 'Only the complex right-hand sum has been formalized.',
          omitted_source_parts: ['The coefficient identity involving M_a(n).'],
          context_status: 'reconciled',
          context_summary: 'The later upper-half-plane convention is relevant to the analytic interpretation.',
          context_evidence: [{ side: 'latex', path: 'main.tex', start_line: 326,
            excerpt: 'q = exp(2*pi*i*tau), tau in the upper half-plane' }],
        }, findings: [{ title: 'Domain convention', detail: 'The semantic choice is still conditional.',
          severity: 'warning', source_resolution: 'open', lean_resolution: 'applied' }],
      }, attention: 'source_issue', freshness: 'current', activity: { status: 'proved' },
      latest_update: { id: 'final' },
    } };
    document.querySelector('main').append(renderLeaStatus(item, { document,
      loadHistory: async () => ({ updates: [], has_more: false }), pause: async () => {} }));
  });
  await expect(page.locator('.ol-lean-live-status-meta')).toContainText('Medium confidence · Partial artifact · Source issue');
  await page.getByText('Lea Status details', { exact: true }).click();
  await expect(page.getByText('Only the complex right-hand sum has been formalized.')).toBeVisible();
  await expect(page.getByText('The coefficient identity involving M_a(n).')).toBeVisible();
  await expect(page.getByText('Document context: reconciled')).toBeVisible();
  await expect(page.getByText('main.tex · line 326', { exact: false })).toBeVisible();
  await expect(page.getByText(/Needs review|awaits approval/)).toHaveCount(0);
  await expect(page.getByText('Statement and proof', { exact: false })).toHaveCount(0);
});
