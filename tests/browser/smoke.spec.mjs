import { test, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
let home, child, base, session, log = '';

async function waitFor(fn) {
  for (let i = 0; i < 240; i++) {
    if (child?.exitCode !== null) throw new Error(log);
    try { if (await fn()) return; } catch {}
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error(`Fixture did not become ready: ${log}`);
}

test.beforeAll(async () => {
  home = await fs.mkdtemp(path.join(os.tmpdir(), 'lea-browser-'));
  const prover = path.join(home, 'prover');
  for (const dir of ['workspace/proofs', 'workspace/projects', 'lea']) await fs.mkdir(path.join(prover, dir), {recursive:true});
  await fs.writeFile(path.join(prover, 'pyproject.toml'), '[project]\nname="browser-stub"\n');
  await fs.writeFile(path.join(prover, 'workspace/lean-toolchain'), 'leanprover/lean4:v4.0.0\n');
  await fs.writeFile(path.join(prover, 'workspace/lakefile.lean'), '-- browser fixture\n');
  await fs.mkdir(path.join(home, 'config'));
  await fs.writeFile(path.join(home, 'config/lea.local.toml'), `model="o4-mini"\nlea_root=${JSON.stringify(prover)}\n`);
  const probe = net.createServer();
  await new Promise(r => probe.listen(0, '127.0.0.1', r));
  const port = probe.address().port;
  await new Promise(r => probe.close(r));
  base = `http://127.0.0.1:${port}`;
  child = spawn(path.join(root, 'apps/lea-standalone/adapter/.venv/bin/python'), [path.join(root, 'tests/integration/adapter_entry.py')], {
    env: {...process.env, LEA_ITEST_HOME:home, LEA_ITEST_PORT:String(port), LEA_WEB_DIST:path.join(root, 'apps/lea-standalone/dist')},
    stdio:['ignore','pipe','pipe'], detached:true,
  });
  child.stdout.on('data', d => {log += d;}); child.stderr.on('data', d => {log += d;});
  await waitFor(async () => (await fetch(`${base}/api/ready`)).ok);
  const started = await fetch(`${base}/api/runs`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({message:'Formalize the Overleaf theorem labeled browser_proof.',autonomous:true})});
  const run = await started.json();
  expect(started.ok, JSON.stringify(run)).toBe(true);
  session = run.session_id;
  const stream = await fetch(`${base}/api/runs/${run.run_id}/events`);
  expect(await stream.text()).toContain('event: done');
});

test.afterAll(async () => {
  if (child?.exitCode === null) {
    const closed = new Promise(r => child.once('exit',r));
    process.kill(-child.pid, 'SIGTERM');
    await Promise.race([closed, new Promise(r => setTimeout(r,3000))]);
    try {process.kill(-child.pid,'SIGKILL');} catch {}
  }
  if (home) await fs.rm(home,{recursive:true,force:true});
});

test('current canvas requires recheck after an external edit and reload', async ({page}) => {
  await page.goto(`${base}/?session=${session}`);
  await expect(page.locator('.canvas')).toBeVisible();
  await expect(page.locator('.canvas')).toContainText('compiles');
  const detail = await (await fetch(`${base}/api/sessions/${session}`)).json();
  expect(detail.code_steps.at(-1).check_current).toBe(true);
  const proofRoot = path.join(home,'prover/workspace/proofs');
  const matches = (await fs.readdir(proofRoot,{recursive:true})).filter(name => name.endsWith('/browser_proof.lean'));
  expect(matches).toHaveLength(1);
  const proof = path.join(proofRoot,matches[0]);
  expect(await fs.readFile(proof,'utf8')).toContain('trivial');
  await fs.writeFile(proof, 'theorem browser_proof : False := by bad_tactic\n');
  await page.reload();
  await expect(page.locator('.canvas')).toContainText('recheck required');
  await expect(page.locator('.canvas')).not.toContainText('✓ compiles');
  const stale = await (await fetch(`${base}/api/sessions/${session}`)).json();
  expect(stale.code_steps.at(-1).check_status).toBe('ok');
  expect(stale.code_steps.at(-1).check_current).toBe(false);
});

test('settings expose gateway routing and render the current catalog', async ({page}) => {
  await page.goto(base);
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('combobox').click();
  const search = page.getByPlaceholder(/Search models/);
  await search.fill('portkey/@readiness/gpt-test');
  await search.press('Enter');
  await expect(page.getByLabel('Gateway URL')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Failed to load');
});

test('vendored KaTeX renders math and rejects trusted HTML in a browser', async ({page}) => {
  await page.goto(base);
  // Load the exact extension asset without needing an Overleaf account.
  await page.addScriptTag({path:path.join(root,'apps/overleaf-extension/extension/vendor/katex/katex.min.js')});
  const result = await page.evaluate(() => {
    const target = document.createElement('div'); document.body.append(target);
    window.katex.render('\\frac{n(n+1)}{2} = \\sum_{k=1}^n k',target,{throwOnError:true,trust:false});
    const math = target.querySelector('math') !== null;
    window.katex.render('\\htmlClass{unsafe}{x}',target,{throwOnError:false,trust:false});
    const trusted = target.querySelector('.unsafe') !== null;
    let malformed = false;
    try {window.katex.render('\\frac{',target,{throwOnError:true,trust:false});} catch {malformed=true;}
    return {math,trusted,malformed};
  });
  expect(result).toEqual({math:true,trusted:false,malformed:true});
});
