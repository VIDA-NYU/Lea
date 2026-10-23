import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, readdir, rm, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createServiceDiagnostics } from './service-diagnostics.mjs';

test('supervisor retains a native/signal exit and redacted stderr with bounded rotation', async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'lea-service-diagnostics-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const secret = 'sk-test-12345678901234567890';
  const diagnostics = createServiceDiagnostics(directory, { maxBytes: 1024, secrets: [secret] });
  const child = spawn(process.execPath, ['-e', `process.stderr.write('failure ${secret}\\n');setTimeout(()=>process.kill(process.pid,'SIGKILL'),30)`], { stdio: ['ignore', 'ignore', 'pipe'] });
  diagnostics.attach('worker', child);
  await once(child, 'close');
  const events = (await readFile(path.join(directory, 'supervisor-events.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.at(-1).event, 'service.exited');
  assert.equal(events.at(-1).signal, 'SIGKILL');
  const stderr = await readFile(path.join(directory, 'worker.stderr.log'), 'utf8');
  assert.match(stderr, /\[redacted\]/);
  assert.ok(!stderr.includes(secret));
  for (let i = 0; i < 100; i++) diagnostics.event('test.rotation', { detail: 'x'.repeat(200) });
  const files = (await readdir(directory)).filter(name => name.startsWith('supervisor-events'));
  assert.ok(files.length <= 3);
  for (const file of files) assert.ok((await stat(path.join(directory, file))).size <= 1024);
});
