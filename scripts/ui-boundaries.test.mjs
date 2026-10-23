import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

test('every adapter route and companion dispatch branch has a reviewed migration category', () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const output = execFileSync('python3', ['scripts/audit-ui-boundaries.py'], { cwd: root, encoding: 'utf8' });
  assert.match(output, /Boundary register covers \d+ adapter routes and \d+ companion dispatch branches/);
});

test('an unclassified route fails the boundary gate instead of inheriting a default category', () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  assert.throws(() => execFileSync('python3', ['-c', `
import runpy
audit = runpy.run_path('scripts/audit-ui-boundaries.py')
found = audit['discover']()
found['adapter POST /api/unclassified-work'] = {}
audit['check'](found)
`], { cwd: root, encoding: 'utf8', stdio: 'pipe' }), error => {
    assert.match(error.stderr, /Unclassified route: adapter POST \/api\/unclassified-work/);
    return true;
  });
});
