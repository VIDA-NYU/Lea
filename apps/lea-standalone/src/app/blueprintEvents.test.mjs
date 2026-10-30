// The blueprint stream events (docs/PLAN-blueprint-tools.md §2.4) as the UI
// reduces them, the desktop notification they raise, the /blueprint auto state
// machine, the draft graph, and the Up next "unlocked" badge. All pure.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  AUTO_IDLE_MS,
  AUTO_SESSION_CAP,
  applyBlueprintEvent,
  autoNotice,
  autoRecord,
  autoStep,
  blueprintEventNotification,
  createAutoState,
  draftGraph,
  draftingOutcome,
  stopAuto,
  unlockNoticeText,
} from './lib/blueprintModel.js';
import { parseBlueprintArgs } from './lib/slashCommands.js';
import { upNext } from './lib/shellModel.js';

const initial = () => ({ refresh: 0, notice: null, nextKeys: [], unlock: null, proposal: null });

const unlocked = {
  type: 'blueprint_unlocked',
  project_id: 'p1',
  keys: ['main', 'other'],
  cause: 'helper',
  run_id: 'r1',
  session_id: 's1',
};

test('blueprint_unlocked: notice "Proved helper · unlocked main, other", nextKeys, refresh bumps', () => {
  const next = applyBlueprintEvent(initial(), unlocked);
  assert.equal(next.notice, 'Proved helper · unlocked main, other');
  assert.deepEqual(next.nextKeys, ['main', 'other']);
  assert.equal(next.refresh, 1);
  assert.deepEqual(next.unlock, { projectId: 'p1', keys: ['main', 'other'], cause: 'helper', runId: 'r1', sessionId: 's1', seq: 1 });
  // A second unlock replaces the first and gets a new seq.
  const again = applyBlueprintEvent(next, { ...unlocked, keys: ['top'], cause: 'main' });
  assert.equal(again.notice, 'Proved main · unlocked top');
  assert.deepEqual(again.nextKeys, ['top']);
  assert.equal(again.unlock.seq, 2);
});

test('blueprint_unlocked without a cause, with junk keys, or with none', () => {
  const noCause = applyBlueprintEvent(initial(), { ...unlocked, cause: null });
  assert.equal(noCause.notice, 'Unlocked main, other');
  assert.equal(noCause.unlock.cause, null);
  const junk = applyBlueprintEvent(initial(), { ...unlocked, keys: ['a', '', 3, 'a', 'b'] });
  assert.deepEqual(junk.nextKeys, ['a', 'b']);
  const none = applyBlueprintEvent({ ...initial(), notice: 'kept' }, { ...unlocked, keys: [] });
  assert.equal(none.refresh, 1);
  assert.equal(none.notice, 'kept');
  assert.equal(none.unlock, null);
  assert.equal(unlockNoticeText('a', ['b']), 'Proved a · unlocked b');
});

test('blueprint_node_changed: refresh bumps; a proof sets "Proved <key>"', () => {
  const changed = applyBlueprintEvent(initial(), {
    type: 'blueprint_node_changed', project_id: 'p1', key: 'helper', from: 'ready', to: 'stated', lean: 'Lea.Demo.helper', file: 'Helper.lean',
  });
  assert.equal(changed.refresh, 1);
  assert.equal(changed.notice, null);
  const proved = applyBlueprintEvent(changed, { type: 'blueprint_node_changed', project_id: 'p1', key: 'helper', from: 'stated', to: 'proved' });
  assert.equal(proved.refresh, 2);
  assert.equal(proved.notice, 'Proved helper');
  // The unlock that follows a proof replaces the notice.
  assert.equal(applyBlueprintEvent(proved, unlocked).notice, 'Proved helper · unlocked main, other');
});

test('blueprint_updated with draft: true while drafting: proposal stays drafting, refresh bumps', () => {
  const proposal = { projectId: 'p1', phase: 'drafting', source: 'agent', draftingSessionId: 's1' };
  const state = { ...initial(), proposal };
  const next = applyBlueprintEvent(state, {
    type: 'blueprint_updated', project_id: 'p1', draft: true, applied: ['main', 'helper'], removed: [], run_id: 'r1', session_id: 's1',
  });
  assert.equal(next.refresh, 1);
  assert.equal(next.proposal.phase, 'drafting');
  assert.equal(next.proposal, proposal, 'the proposal object is untouched');
  const live = applyBlueprintEvent(next, { type: 'blueprint_updated', project_id: 'p1', draft: false, applied: ['x'], removed: [] });
  assert.equal(live.refresh, 2);
  assert.equal(live.proposal, proposal);
});

test('unknown events leave the state alone', () => {
  const state = initial();
  assert.equal(applyBlueprintEvent(state, { type: 'message' }), state);
  assert.equal(applyBlueprintEvent(state, null), state);
});

test('only unlocks raise a desktop notification', () => {
  assert.deepEqual(blueprintEventNotification(unlocked), {
    title: 'Proved helper',
    body: 'Ready to prove: main, other',
    sessionId: 's1',
  });
  assert.deepEqual(blueprintEventNotification({ ...unlocked, cause: null, session_id: null }), {
    title: 'Blueprint nodes unlocked',
    body: 'Ready to prove: main, other',
  });
  assert.equal(blueprintEventNotification({ ...unlocked, keys: [] }), null);
  assert.equal(blueprintEventNotification({ type: 'blueprint_node_changed', key: 'a', to: 'proved' }), null);
  assert.equal(blueprintEventNotification({ type: 'blueprint_updated', draft: true }), null);
});

// ── /blueprint auto ─────────────────────────────────────────────────────────

const node = (key, status, uses = []) => ({ key, status, uses, kind: 'lemma', statement: '', file: null, lean: null });

test('auto: the first step starts every ready node without a running session', () => {
  const nodes = [node('main', 'planned', ['a', 'b']), node('a', 'ready'), node('b', 'ready'), node('c', 'ready'), node('d', 'proved')];
  const s0 = createAutoState('p1');
  assert.equal(s0.cap, AUTO_SESSION_CAP);
  const { state, start } = autoStep(s0, { nodes, running: new Set(['c']), now: 0 });
  assert.deepEqual(start, ['a', 'b'], 'c already has a running session');
  assert.equal(state.active, true);
  const recorded = autoRecord(state, ['a'], ['b']);
  assert.deepEqual(recorded.started, ['a']);
  assert.deepEqual(recorded.failed, ['b']);
  assert.equal(autoNotice(recorded, { started: ['a'], failed: [{ key: 'b', error: 'boom' }] }), 'Auto: started a (1 of 20) · could not start b (boom)');
  // Neither a started nor a failed key is tried again, even when still ready.
  const second = autoStep(recorded, { nodes, running: new Set(), now: 1 });
  assert.deepEqual(second.start, ['c']);
});

test('auto: each unlock starts the newly ready nodes', () => {
  let state = autoRecord(createAutoState('p1'), ['helper']);
  const before = [node('main', 'planned', ['helper']), node('other', 'planned', ['helper']), node('helper', 'ready')];
  let step = autoStep(state, { nodes: before, running: new Set(['helper']), now: 0 });
  assert.deepEqual(step.start, []);
  assert.equal(step.state.active, true, 'waiting on a running session');
  assert.equal(autoNotice(step.state, { waiting: 1 }), 'Auto is on: waiting for 1 running session to unlock more.');
  const after = [node('main', 'ready', ['helper']), node('other', 'ready', ['helper']), node('helper', 'proved')];
  step = autoStep(step.state, { nodes: after, running: new Set(), now: 1000 });
  assert.deepEqual(step.start, ['main', 'other']);
  state = autoRecord(step.state, step.start);
  assert.equal(autoNotice(state, { started: step.start }), 'Auto: started main, other (3 of 20)');
});

test('auto: stops at the cap of 20 sessions per invocation', () => {
  const nodes = Array.from({ length: 25 }, (_, i) => node(`n${i}`, 'ready'));
  const { state, start } = autoStep(createAutoState('p1'), { nodes, running: new Set(), now: 0 });
  assert.equal(start.length, 20);
  const capped = autoRecord(state, start);
  assert.equal(capped.active, false);
  assert.equal(capped.stopReason, 'cap');
  assert.equal(autoNotice(capped, { started: ['n19'] }), 'Auto: started n19 (20 of 20) · Auto stopped at the cap of 20 sessions.');
  assert.deepEqual(autoStep(capped, { nodes, running: new Set(), now: 1 }).start, [], 'a stopped loop starts nothing');
  // A smaller cap leaves room for only part of the ready set.
  const small = autoRecord(createAutoState('p1', 3), ['a', 'b']);
  assert.deepEqual(autoStep(small, { nodes, running: new Set(), now: 0 }).start, ['n0']);
});

test('auto: stop conditions — every node proved, an empty blueprint, nothing to do, the user', () => {
  const done = autoStep(autoRecord(createAutoState('p1'), ['a']), { nodes: [node('a', 'proved')], running: new Set(), now: 0 });
  assert.equal(done.state.stopReason, 'done');
  assert.equal(autoNotice(done.state), 'Auto finished: every node is proved (started 1 session).');

  const empty = autoStep(createAutoState('p1'), { nodes: [], running: new Set(), now: 0 });
  assert.equal(empty.state.stopReason, 'empty');

  // First step with nothing ready and nothing running: stop at once.
  const blocked = [node('main', 'planned', ['a']), node('a', 'failed')];
  const first = autoStep(createAutoState('p1'), { nodes: blocked, running: new Set(), now: 0 });
  assert.equal(first.state.stopReason, 'stalled');
  assert.equal(autoNotice(first.state), 'Auto stopped: no node is ready and no session is running.');

  // Later: idle must last AUTO_IDLE_MS (a session list that lags a start must not end the loop).
  let state = autoRecord(createAutoState('p1'), ['a']);
  let step = autoStep(state, { nodes: blocked, running: new Set(), now: 1000 });
  assert.equal(step.state.active, true);
  assert.equal(step.state.idleSince, 1000);
  step = autoStep(step.state, { nodes: blocked, running: new Set(['a']), now: 2000 });
  assert.equal(step.state.idleSince, null, 'a running session resets the idle clock');
  step = autoStep(step.state, { nodes: blocked, running: new Set(), now: 3000 });
  step = autoStep(step.state, { nodes: blocked, running: new Set(), now: 3000 + AUTO_IDLE_MS - 1 });
  assert.equal(step.state.active, true);
  step = autoStep(step.state, { nodes: blocked, running: new Set(), now: 3000 + AUTO_IDLE_MS });
  assert.equal(step.state.stopReason, 'stalled');
  assert.equal(autoNotice(step.state), 'Auto finished: nothing else is ready (started 1 session).');

  state = stopAuto(autoRecord(createAutoState('p1'), ['a', 'b']), 'user');
  assert.equal(state.active, false);
  assert.equal(autoNotice(state), 'Auto stopped (started 2 sessions).');
  assert.equal(stopAuto(state, 'cap').stopReason, 'user', 'the first reason sticks');
});

test('/blueprint auto and /blueprint stop parse; next is unchanged', () => {
  assert.deepEqual(parseBlueprintArgs('auto'), { sub: 'auto' });
  assert.deepEqual(parseBlueprintArgs('AUTO'), { sub: 'auto' });
  assert.deepEqual(parseBlueprintArgs('stop'), { sub: 'stop' });
  assert.deepEqual(parseBlueprintArgs('next'), { sub: 'next' });
  assert.match(parseBlueprintArgs('zap').error, /\/blueprint auto/);
});

// ── Draft graph and Up next ─────────────────────────────────────────────────

test('draftGraph shows draft nodes as planned, with edges inside the draft only', () => {
  const g = draftGraph([
    { key: 'main', kind: 'theorem', lean: 'Lea.Demo.main', file: 'Main.lean', uses: ['helper', 'ghost'], statement: 'Main.' },
    { key: 'helper', kind: 'lemma', lean: 'Lea.Demo.helper', file: 'Helper.lean', uses: [], statement: 'Helper.' },
  ]);
  assert.deepEqual(g.edges, [{ from: 'main', to: 'helper' }]);
  assert.deepEqual(g.nodes.map((n) => [n.key, n.status, n.lean, n.file, n.planned_file, n.declared]), [
    ['main', 'planned', 'Lea.Demo.main', 'Main.lean', 'Main.lean', false],
    ['helper', 'planned', 'Lea.Demo.helper', 'Helper.lean', 'Helper.lean', false],
  ]);
  assert.deepEqual(g.nodes[0].sessions, []);
  assert.deepEqual(draftGraph(null), { nodes: [], edges: [] });
});

test('upNext marks the keys of the latest unlock, order unchanged', () => {
  const nodes = [
    { key: 'helper', status: 'proved', uses: [] },
    { key: 'main', status: 'ready', uses: ['helper'] },
    { key: 'other', status: 'ready', uses: ['helper'] },
    { key: 'side', status: 'stated', uses: [] },
  ];
  const plain = upNext(nodes, {});
  const marked = upNext(nodes, {}, 6, ['other']);
  assert.deepEqual(marked.map((i) => [i.node.key, i.action]), plain.map((i) => [i.node.key, i.action]));
  assert.deepEqual(marked.map((i) => [i.node.key, i.unlocked]), [
    ['main', false],
    ['other', true],
    ['side', false],
  ]);
  assert.deepEqual(upNext(nodes, {}, 6, new Set(['main'])).map((i) => i.unlocked), [true, false, false]);
  assert.ok(plain.every((i) => i.unlocked === false));
});

test('draftingOutcome: review once the drafting run ends with draft nodes', () => {
  const base = { running: false, seenRunning: false, runStatus: null, draftNodes: 0, readAfterEnd: false };
  assert.equal(draftingOutcome({ ...base, running: true, seenRunning: true, draftNodes: 3 }), 'drafting', 'still writing');
  assert.equal(draftingOutcome({ ...base, draftNodes: 3 }), 'drafting', 'a stale session list: the run was never seen');
  assert.equal(draftingOutcome({ ...base, seenRunning: true, draftNodes: 3 }), 'review');
  assert.equal(draftingOutcome({ ...base, runStatus: 'proved', draftNodes: 2 }), 'review', 'the streamed run status ends it too');
  assert.equal(draftingOutcome({ ...base, runStatus: 'running', seenRunning: true, draftNodes: 2 }), 'drafting');
  assert.equal(draftingOutcome({ ...base, seenRunning: true }), 'drafting', 'wait for a draft read after the end');
  assert.equal(draftingOutcome({ ...base, seenRunning: true, readAfterEnd: true }), 'empty');
});
