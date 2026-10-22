import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

const companion = 'apps/overleaf-extension/.overleaf-lean-stub';
const data = 'apps/lea-standalone/data';
const prover = 'apps/lea-standalone/prover/workspace';
const metadata = ['jobs.json', 'jobs.json.batches.json', 'chatSessions.json', 'generated-targets.json', 'cache.json'];

function fixture(t, customImport) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lea-reset-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (relative, value) => {
    const file = path.join(root, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, value);
  };
  for (const name of ['reset-local-state.mjs', 'env.mjs']) {
    write(`scripts/${name}`, fs.readFileSync(new URL(name, import.meta.url)));
  }
  write('.env', customImport ? 'LEA_COMPANION_IMPORT_DIR=custom-companion\n' : '# retained configuration\n');
  const dirs = customImport ? [companion, 'custom-companion'] : [companion];
  for (const dir of dirs) {
    for (const name of metadata) write(`${dir}/${name}`, JSON.stringify({ old: { sessionId: 'deleted-session' } }));
    write(`${dir}/.lea-db-migration.json`, '{"state":"retired"}');
    write(`${dir}/settings.json`, '{"leaUiBaseUrl":"http://localhost:5173"}');
    write(`${dir}/jobs/old.log`, 'old job');
    write(`${dir}/backups/old.tex`, 'old companion backup');
  }
  write(`${data}/lea-interface.sqlite3`, 'database');
  write(`${data}/lea-interface.sqlite3-wal`, 'journal');
  write(`${data}/backups/recovery.sqlite3`, 'retained database backup');
  write(`${data}/legacy-companion/old/jobs.json.batches.json`, '{"old":{}}');
  write(`${prover}/proofs/Old.lean`, 'old proof');
  write(`${prover}/projects/old.md`, 'old project');
  write(`${prover}/proofs/.gitkeep`, '');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('LEA_')));
  return { root, dirs, run: (...args) => execFileSync(process.execPath, ['scripts/reset-local-state.mjs', ...args], { cwd: root, env, encoding: 'utf8' }) };
}

function snapshot(root) {
  const files = {};
  const visit = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name);
      files[path.relative(root, file)] = entry.isDirectory() ? null : fs.readFileSync(file, 'utf8');
      if (entry.isDirectory()) visit(file);
    }
  };
  visit(root);
  return files;
}

for (const customImport of [false, true]) {
  test(`fresh reset clears companion history and migration metadata${customImport ? ' including the configured import directory' : ''}`, (t) => {
    const x = fixture(t, customImport);
    const before = snapshot(x.root);
    x.run();
    for (const dir of x.dirs) {
      for (const name of metadata) {
        const value = JSON.parse(fs.readFileSync(path.join(x.root, dir, name), 'utf8'));
        assert.deepEqual(value, name === 'generated-targets.json' ? { version: 1, projects: {} } : {});
      }
      assert.equal(fs.existsSync(path.join(x.root, dir, '.lea-db-migration.json')), false);
      assert.equal(fs.existsSync(path.join(x.root, dir, 'backups')), false);
      assert.deepEqual(fs.readdirSync(path.join(x.root, dir, 'jobs')), []);
      assert.equal(fs.readFileSync(path.join(x.root, dir, 'settings.json'), 'utf8'), before[`${dir}/settings.json`]);
    }
    for (const relative of [`${data}/lea-interface.sqlite3`, `${data}/lea-interface.sqlite3-wal`, `${data}/legacy-companion`, `${prover}/proofs/Old.lean`, `${prover}/projects/old.md`]) {
      assert.equal(fs.existsSync(path.join(x.root, relative)), false, relative);
    }
    for (const relative of ['.env', `${data}/backups/recovery.sqlite3`, `${prover}/proofs/.gitkeep`]) {
      assert.equal(fs.readFileSync(path.join(x.root, relative), 'utf8'), before[relative]);
    }
  });
}

test('a fresh reset dry run preserves every file and directory', (t) => {
  const x = fixture(t, true);
  const before = snapshot(x.root);
  const output = x.run('--dry-run');
  assert.match(output, /companion batch index/);
  assert.match(output, /companion migration snapshots/);
  assert.deepEqual(snapshot(x.root), before);
});
