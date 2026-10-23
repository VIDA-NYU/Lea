import { test, expect } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

for (const width of [1100, 390]) {
  test(`Overleaf picker refresh preserves selection and finds suggestions at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 850 });
    await page.setContent(`<style>:root{--ol-panel:#fff;--ol-panel-2:#f5f5f7;--ol-ink:#18181b;--ol-muted:#52525b;--ol-line:#ddd;--ol-accent:#1c1c20;--ol-accent-soft:#ededf0;--ol-sans:system-ui;--ol-mono:monospace}body{padding:20px}.host{max-width:360px}</style><div class="host" id="picker"></div>`);
    await page.addStyleTag({ path: path.join(root, 'apps/overleaf-extension/extension/modelPicker.css') });
    await page.addScriptTag({ path: path.join(root, 'apps/overleaf-extension/extension/modelPicker.js') });
    await page.evaluate(() => {
      window.catalogRequests = [];
      window.picker = globalThis.LeaModelPicker.createModelPicker({ root: document.querySelector('#picker'),
        value: 'custom/selected', featured: [{ value: 'gpt-future', family: 'openai' }],
        loadCatalog: async refresh => {
          window.catalogRequests.push(refresh);
          return { models: refresh ? [{ value: 'gpt-new', provider: 'openai', compatibility: 'unverified' }] : [],
            featured: [{ value: 'gpt-future', family: 'openai' }], revision: refresh ? 'new' : 'old',
            warnings: [{ message: 'Provider unavailable; showing saved models.' }] };
        } });
    });
    await page.getByRole('combobox').click();
    await expect(page.getByRole('status')).toContainText('Provider unavailable');
    await page.getByPlaceholder(/Search models/).fill('gpt-future');
    await expect(page.getByRole('option')).toHaveCount(1);
    await expect(page.getByRole('option')).toHaveText('gpt-futureopenai');
    await page.getByRole('button', { name: 'Refresh models' }).click();
    await expect.poll(() => page.evaluate(() => window.catalogRequests.includes(true))).toBe(true);
    await expect.poll(() => page.evaluate(() => window.picker.getValue())).toBe('custom/selected');
    await page.getByPlaceholder(/Search models/).fill('gpt-new');
    await expect(page.getByRole('option')).toHaveCount(1);
    await expect(page.getByRole('option')).toContainText('Compatibility unverified');
    await page.getByPlaceholder(/Search models/).fill('');
    await expect(page.getByRole('option').first()).toContainText('custom/selected');
    if (process.env.LEA_CATALOG_SCREENSHOTS) await page.screenshot({ path: `/tmp/lea-catalog-overleaf-${width}.png` });
    await page.evaluate(() => window.picker.destroy());
  });
}
