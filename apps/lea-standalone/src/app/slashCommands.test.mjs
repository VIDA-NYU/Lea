import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  parseSlashCommand,
  findSlashCommand,
  matchSlashCommands,
  parseBlueprintArgs,
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

// ── /blueprint (UI redesign, blueprint package) ────────────────────────────────

test('/blueprint is registered as an action command with an argument hint', () => {
  const cmd = findSlashCommand('blueprint');
  assert.ok(cmd, 'blueprint command exists');
  assert.equal(cmd.kind, 'action');
  assert.ok(cmd.argumentHint.includes('next'));
  assert.ok(cmd.argumentHint.includes('prove'));
  assert.deepEqual(matchSlashCommands('/blue').map((c) => c.name), ['blueprint']);
});

test('parseSlashCommand hands the sub-command to parseBlueprintArgs', () => {
  assert.deepEqual(parseSlashCommand('/blueprint'), { name: 'blueprint', args: '' });
  assert.deepEqual(parseSlashCommand('/blueprint next'), { name: 'blueprint', args: 'next' });
  assert.deepEqual(parseSlashCommand('/blueprint prove parity_descent'), { name: 'blueprint', args: 'prove parity_descent' });
});

test('parseBlueprintArgs: open / next / prove <node>', () => {
  assert.deepEqual(parseBlueprintArgs(''), { sub: 'open' });
  assert.deepEqual(parseBlueprintArgs('   '), { sub: 'open' });
  assert.deepEqual(parseBlueprintArgs('next'), { sub: 'next' });
  assert.deepEqual(parseBlueprintArgs('NEXT'), { sub: 'next' }, 'sub-command is case-insensitive');
  assert.deepEqual(parseBlueprintArgs('prove parity_descent'), { sub: 'prove', node: 'parity_descent' });
  assert.deepEqual(parseBlueprintArgs('Prove `Sq_Parity`'), { sub: 'prove', node: 'Sq_Parity' }, 'node keeps its case, backticks dropped');
});

test('parseBlueprintArgs reports a missing or malformed node and unknown sub-commands', () => {
  const missing = parseBlueprintArgs('prove');
  assert.equal(missing.sub, 'prove');
  assert.match(missing.error, /Name the node/);
  const spaced = parseBlueprintArgs('prove two words');
  assert.equal(spaced.sub, 'prove');
  assert.match(spaced.error, /prove two/);
  const unknown = parseBlueprintArgs('zap');
  assert.equal(unknown.sub, 'unknown');
  assert.match(unknown.error, /Unknown blueprint command "zap"/);
});
