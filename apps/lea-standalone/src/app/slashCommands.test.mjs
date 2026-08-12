import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  parseSlashCommand,
  findSlashCommand,
  matchSlashCommands,
  SLASH_COMMANDS,
} from './lib/slashCommands.js';

test('parseSlashCommand splits name and args', () => {
  assert.deepEqual(parseSlashCommand('/compact'), { name: 'compact', args: '' });
  assert.deepEqual(parseSlashCommand('  /compact  '), { name: 'compact', args: '' });
  assert.deepEqual(parseSlashCommand('/prove x + y = z'), { name: 'prove', args: 'x + y = z' });
  assert.deepEqual(parseSlashCommand('/COMPACT'), { name: 'compact', args: '' }, 'name lowercased');
});

test('parseSlashCommand rejects non-commands', () => {
  assert.equal(parseSlashCommand('prove that 2+2=4'), null);
  assert.equal(parseSlashCommand(''), null);
  assert.equal(parseSlashCommand('/'), null, 'a bare slash is not a command');
  assert.equal(parseSlashCommand('/123'), null, 'must start with a letter');
  assert.equal(parseSlashCommand('a/b'), null, 'slash must lead');
});

test('/compact is registered as an action command', () => {
  const cmd = findSlashCommand('compact');
  assert.ok(cmd, 'compact command exists');
  assert.equal(cmd.kind, 'action');
  assert.ok(cmd.description);
});

test('findSlashCommand is case-insensitive and misses unknowns', () => {
  assert.ok(findSlashCommand('COMPACT'));
  assert.equal(findSlashCommand('nope'), undefined);
});

test('matchSlashCommands drives the autocomplete', () => {
  assert.deepEqual(matchSlashCommands('/comp').map((c) => c.name), ['compact']);
  assert.deepEqual(matchSlashCommands('/c').map((c) => c.name), ['compact']);
  assert.deepEqual(matchSlashCommands('/zzz'), []);
  // empty / bare slash lists everything in the registry
  assert.equal(matchSlashCommands('/').length, SLASH_COMMANDS.length);
  assert.equal(matchSlashCommands('').length, SLASH_COMMANDS.length);
});
