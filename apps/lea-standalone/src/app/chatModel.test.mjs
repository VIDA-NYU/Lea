import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  approvalAsk,
  callResult,
  composerStatus,
  decisionLabel,
  deriveToolCalls,
  errorCount,
  formatDelta,
  formatElapsed,
  lineDelta,
  previousStepByPath,
  runClock,
  toolKind,
  toolNameOf,
  toolRowModel,
  toolTarget,
  userBubbleModel,
} from './lib/chatModel.js';

const RUN = 'run-1';
const at = (s) => new Date(Date.UTC(2026, 9, 30, 12, 0, 0) + s * 1000).toISOString();
const call = (id, name, s, turn) => ({ id, run_id: RUN, status: 'tool_call', message: `Running ${name}`, turn, created_at: at(s) });

// ── formatElapsed ────────────────────────────────────────────────────────────

test('formatElapsed: milliseconds under a second', () => {
  assert.equal(formatElapsed(0), '0 ms');
  assert.equal(formatElapsed(420), '420 ms');
  assert.equal(formatElapsed(999.9), '999 ms');
});

test('formatElapsed: whole seconds under a minute, one decimal when fine', () => {
  assert.equal(formatElapsed(1000), '1 s');
  assert.equal(formatElapsed(12_400), '12 s');
  assert.equal(formatElapsed(59_999), '59 s');
  assert.equal(formatElapsed(4200, { fine: true }), '4.2 s');
  assert.equal(formatElapsed(9999, { fine: true }), '9.9 s');
  assert.equal(formatElapsed(10_000, { fine: true }), '10 s');
  assert.equal(formatElapsed(650, { fine: true }), '650 ms');
});

test('formatElapsed: minutes with padded seconds, then hours with padded minutes', () => {
  assert.equal(formatElapsed(60_000), '1m 00s');
  assert.equal(formatElapsed(65_000), '1m 05s');
  assert.equal(formatElapsed(83_000), '1m 23s');
  assert.equal(formatElapsed(3_599_999), '59m 59s');
  assert.equal(formatElapsed(3_600_000), '1h 00m');
  assert.equal(formatElapsed(3_720_000), '1h 02m');
  assert.equal(formatElapsed(26 * 3_600_000), '26h 00m');
});

test('formatElapsed: negative, NaN and missing clamp to zero', () => {
  assert.equal(formatElapsed(-5000), '0 ms');
  assert.equal(formatElapsed(Number.NaN), '0 ms');
  assert.equal(formatElapsed(undefined), '0 ms');
});

// ── line deltas ──────────────────────────────────────────────────────────────

test('lineDelta counts added and removed lines', () => {
  assert.deepEqual(lineDelta('', 'a\nb\nc\n'), { added: 3, removed: 0 });
  assert.deepEqual(lineDelta('a\nb\nc', 'a\nB\nc\nd'), { added: 2, removed: 1 });
  assert.deepEqual(lineDelta('a\nb', 'a\nb\n'), { added: 0, removed: 0 });
  assert.deepEqual(lineDelta('x\ny\nz', ''), { added: 0, removed: 3 });
  assert.equal(formatDelta({ added: 11, removed: 0 }), '+11');
  assert.equal(formatDelta({ added: 3, removed: 2 }), '+3 −2');
  assert.equal(formatDelta({ added: 0, removed: 4 }), '−4');
  assert.equal(formatDelta({ added: 0, removed: 0 }), 'no change');
});

test('lineDelta falls back to a multiset count for very large inputs', () => {
  const before = Array.from({ length: 2000 }, (_, i) => `line ${i}`).join('\n');
  const after = Array.from({ length: 2000 }, (_, i) => `line ${i === 0 ? 'x' : i}`).join('\n') + '\nend';
  // head differs, tail differs: the trimmed middle is 2000×2001 cells → multiset path
  assert.deepEqual(lineDelta(`${before}\nA`, `${after}`), { added: 2, removed: 2 });
});

// ── tool names and targets ───────────────────────────────────────────────────

test('toolNameOf reads the status message or an explicit tool field', () => {
  assert.equal(toolNameOf({ message: 'Running search_mathlib' }), 'search_mathlib');
  assert.equal(toolNameOf({ tool: 'bash', message: 'Running something' }), 'bash');
  assert.equal(toolNameOf({ message: '' }), 'tool');
});

test('toolKind covers built-ins, MCP and custom tools', () => {
  assert.equal(toolKind('write_file'), 'write');
  assert.equal(toolKind('edit_file'), 'edit');
  assert.equal(toolKind('read_file'), 'read');
  assert.equal(toolKind('lean_check'), 'check');
  assert.equal(toolKind('safe_verify'), 'verify');
  assert.equal(toolKind('bash'), 'bash');
  assert.equal(toolKind('search_mathlib'), 'search');
  assert.equal(toolKind('spawn_subagent'), 'session');
  assert.equal(toolKind('github__search_code'), 'mcp');
  assert.equal(toolKind('wolfram_alpha'), 'custom');
});

test('toolTarget picks the file, command or query', () => {
  assert.equal(toolTarget('write_file', { path: '/w/proofs/abc/Lea/Sqrt2.lean', content: 'x' }), 'Sqrt2.lean');
  assert.equal(toolTarget('bash', { command: 'lake build\necho done' }), 'lake build');
  assert.equal(toolTarget('search_mathlib', { query: 'Nat.Prime.two_le' }), 'Nat.Prime.two_le');
  assert.equal(toolTarget('github__search_code', null), 'github');
  assert.equal(toolTarget('read_file', null), '');
});

// ── toolRowModel: one case per built-in tool, plus an unknown one ────────────

test('toolRowModel: a call with no result is live', () => {
  const v = toolRowModel({ name: 'edit_file', args: { path: 'Sqrt2.lean' } }, null);
  assert.deepEqual([v.icon, v.verb, v.target, v.result, v.tone, v.live], ['edit', 'edit', 'Sqrt2.lean', '', 'running', true]);
});

test('toolRowModel: write shows lines added, green', () => {
  const v = toolRowModel({ name: 'write_file', args: { path: 'a/Sqrt2.lean' } }, { code: 'a\nb\nc\n', previousCode: '' });
  assert.deepEqual([v.icon, v.verb, v.target, v.result, v.tone], ['file', 'write', 'Sqrt2.lean', '+3', 'added']);
});

test('toolRowModel: edit shows +a −b, muted', () => {
  const v = toolRowModel({ name: 'edit_file', target: 'Sqrt2.lean' }, { code: 'a\nX\nc\nd', previousCode: 'a\nb\nc' });
  assert.deepEqual([v.icon, v.verb, v.target, v.result, v.tone], ['edit', 'edit', 'Sqrt2.lean', '+2 −1', 'muted']);
});

test('toolRowModel: read shows the file and line count when known', () => {
  const v = toolRowModel({ name: 'read_file', args: { path: 'x/Parity.lean' } }, { lines: 40 });
  assert.deepEqual([v.icon, v.verb, v.target, v.result], ['file', 'read', 'Parity.lean', '40 lines']);
  assert.equal(toolRowModel({ name: 'read_file' }, {}).result, '');
});

test('toolRowModel: lean_check ok is 0 errors, green', () => {
  const v = toolRowModel({ name: 'lean_check', target: 'Parity.lean' }, { checkStatus: 'ok', code: 'theorem t : True := trivial' });
  assert.deepEqual([v.icon, v.verb, v.result, v.tone], ['play', 'lean_check', '0 errors', 'proved']);
});

test('toolRowModel: lean_check ok with sorry is amber with the count', () => {
  const code = 'theorem a : True := by\n  sorry\n-- sorry in a comment\ntheorem b : True := by sorry\n';
  const v = toolRowModel({ name: 'lean_check' }, { checkStatus: 'ok', code });
  assert.deepEqual([v.result, v.tone], ['2 sorry', 'sorry']);
});

test('toolRowModel: lean_check error is red with the first error line inline', () => {
  const one = toolRowModel({ name: 'lean_check' }, { checkStatus: 'error', checkDetail: 'Sqrt2.lean:12:4: error: type mismatch' });
  assert.deepEqual([one.result, one.tone, one.firstError], ['failed', 'error', 'Sqrt2.lean:12:4: error: type mismatch']);
  const detail = 'A.lean:1:0: error: unknown identifier\nctx\nA.lean:4:2: error: unsolved goals\n⊢ False';
  const many = toolRowModel({ name: 'lean_check' }, { checkStatus: 'error', checkDetail: detail });
  assert.deepEqual([many.result, many.tone], ['2 errors', 'error']);
  assert.equal(errorCount('A.lean:1:0: error: x\n  goal'), 1);
  assert.equal(errorCount('A.lean:1:0: error: x'), null);
});

test('toolRowModel: verify passes green and rejects red', () => {
  assert.deepEqual(
    (({ icon, verb, result, tone }) => [icon, verb, result, tone])(toolRowModel({ name: 'safe_verify' }, { verifyStatus: 'ok' })),
    ['shield', 'verify', 'passed', 'proved'],
  );
  assert.equal(toolRowModel({ name: 'safe_verify' }, { verifyStatus: 'rejected' }).tone, 'error');
});

test('toolRowModel: bash shows the command and exit code', () => {
  const fail = toolRowModel({ name: 'bash', args: { command: 'lake build' } }, { exitCode: 1, preview: 'error: build failed' });
  assert.deepEqual([fail.icon, fail.verb, fail.target, fail.result, fail.tone, fail.firstError],
    ['terminal', 'bash', 'lake build', 'exit 1', 'error', 'error: build failed']);
  assert.equal(toolRowModel({ name: 'bash' }, { exitCode: 0 }).tone, 'muted');
  assert.equal(toolRowModel({ name: 'bash' }, {}).result, '');
});

test('toolRowModel: search shows the query and hit count', () => {
  const v = toolRowModel({ name: 'search_mathlib', args: { query: 'even_of_sq' } }, { hits: 12 });
  assert.deepEqual([v.icon, v.verb, v.target, v.result], ['search', 'search', 'even_of_sq', '12 hits']);
  assert.equal(toolRowModel({ name: 'search_mathlib' }, { hits: 1 }).result, '1 hit');
});

test('toolRowModel: sessions use the blueprint icon and their turn', () => {
  const v = toolRowModel({ name: 'session', target: 'parity_descent' }, { turn: 5 });
  assert.deepEqual([v.icon, v.verb, v.target, v.result, v.tone], ['blueprint', 'session', 'parity_descent', 'turn 5', 'running']);
  const done = toolRowModel({ name: 'session', target: 'x' }, { label: 'compiles', tone: 'proved' });
  assert.deepEqual([done.result, done.tone], ['compiles', 'proved']);
});

test('toolRowModel: an unknown or MCP tool gets the generic icon and its own name', () => {
  const custom = toolRowModel({ name: 'wolfram_alpha', args: { input: 'integrate x^2' } }, {});
  assert.deepEqual([custom.icon, custom.verb, custom.target, custom.result], ['tool', 'wolfram_alpha', 'integrate x^2', '']);
  const mcp = toolRowModel({ name: 'github__search_code' }, { ok: false, preview: 'Error: rate limited' });
  assert.deepEqual([mcp.icon, mcp.verb, mcp.target, mcp.result, mcp.tone], ['tool', 'search_code', 'github', 'error', 'error']);
});

test('toolRowModel: denied, stopped, queued and waiting states', () => {
  assert.deepEqual((({ result, tone }) => [result, tone])(toolRowModel({ name: 'bash' }, { denied: true })), ['denied', 'error']);
  assert.equal(toolRowModel({ name: 'bash' }, { stopped: true }).result, 'stopped');
  assert.equal(toolRowModel({ name: 'bash' }, { queued: true }).result, 'queued');
  assert.deepEqual((({ result, tone }) => [result, tone])(toolRowModel({ name: 'bash' }, { waiting: true })), ['needs approval', 'waiting']);
});

// ── pairing calls with results ───────────────────────────────────────────────

test('deriveToolCalls pairs a write with its code step and a check with its status', () => {
  const statusEvents = [
    call('s1', 'write_file', 0, 1),
    call('s2', 'lean_check', 10, 2),
    { id: 's3', run_id: RUN, status: 'lean_check', message: 'lean_check: error', turn: 2, check_status: 'error',
      check_detail: 'Sqrt2.lean:3:2: error: nope', created_at: at(14.5) },
  ];
  const codeSteps = [{ id: 'c1', run_id: RUN, turn: 1, author: 'agent', path: 'Lea/Sqrt2.lean', code: 'a\nb', created_at: at(2) }];
  const { calls, pairedStepIds } = deriveToolCalls({ statusEvents, codeSteps, runStatusById: { [RUN]: 'failed' } });
  assert.equal(calls.length, 2);
  const [write, check] = calls;
  assert.equal(write.step?.id, 'c1');
  assert.equal(write.endedAt - write.startedAt, 2000);
  assert.equal(write.exact, true);
  assert.equal(check.check?.id, 's3');
  assert.equal(check.checkedStep?.id, 'c1');
  assert.equal(check.endedAt - check.startedAt, 4500);
  assert.ok(pairedStepIds.has('c1'));
  const view = toolRowModel({ name: check.name, target: 'Sqrt2.lean' }, callResult(check));
  assert.deepEqual([view.result, view.tone], ['failed', 'error']);
});

test('deriveToolCalls: a live call with nothing after it is live; earlier ones end at the next event', () => {
  const statusEvents = [call('s1', 'bash', 0, 1), call('s2', 'read_file', 30, 2)];
  const messages = [{ id: 'm1', run_id: RUN, role: 'assistant', created_at: at(20) }];
  const { calls } = deriveToolCalls({ statusEvents, messages, liveRunId: RUN });
  assert.equal(calls[0].state, 'done');
  assert.equal(calls[0].exact, false);
  assert.equal(calls[0].endedAt - calls[0].startedAt, 20_000);
  assert.equal(calls[1].state, 'live');
  assert.equal(callResult(calls[1]), null);
});

test('deriveToolCalls: calls in one turn run in order; the second is queued while the first runs', () => {
  const statusEvents = [call('s1', 'bash', 0, 1), call('s2', 'bash', 0.5, 1)];
  const { calls } = deriveToolCalls({ statusEvents, liveRunId: RUN });
  assert.deepEqual(calls.map((c) => c.state), ['live', 'queued']);
});

test('deriveToolCalls: an approval gives the call its arguments and its waiting state', () => {
  const statusEvents = [call('s1', 'bash', 0, 1)];
  const approvals = [{ approval_id: 'a1', run_id: RUN, tool_name: 'bash', args: { command: 'ls -la' }, decision: null, actionable: true, created_at: at(0.2) }];
  const { calls, pairedApprovalIds } = deriveToolCalls({ statusEvents, approvals, liveRunId: RUN });
  assert.equal(calls[0].state, 'waiting');
  assert.deepEqual(calls[0].args, { command: 'ls -la' });
  assert.ok(pairedApprovalIds.has('a1'));
  assert.equal(toolRowModel({ name: 'bash', args: calls[0].args }, callResult(calls[0])).target, 'ls -la');
});

test('deriveToolCalls: an approved call starts when approved; a denied one ends denied', () => {
  const statusEvents = [call('s1', 'write_file', 0, 1), call('s2', 'bash', 40, 2)];
  const approvals = [
    { approval_id: 'a1', run_id: RUN, tool_name: 'write_file', args: { path: 'B.lean', content: 'x\ny' }, decision: 'allow', created_at: at(0.1), resolved_at: at(30) },
    { approval_id: 'a2', run_id: RUN, tool_name: 'bash', args: { command: 'rm -rf x' }, decision: 'deny', created_at: at(40.1), resolved_at: at(45) },
  ];
  const codeSteps = [{ id: 'c1', run_id: RUN, turn: 1, author: 'agent', path: 'B.lean', code: 'x\ny', created_at: at(31) }];
  const { calls } = deriveToolCalls({ statusEvents, approvals, codeSteps, runStatusById: { [RUN]: 'answered' } });
  assert.equal(calls[0].endedAt - calls[0].startedAt, 1000);
  assert.equal(calls[1].state, 'denied');
  assert.equal(toolRowModel({ name: 'bash' }, callResult(calls[1])).result, 'denied');
});

test('deriveToolCalls: a write to a non-Lean file takes its delta from the approval arguments', () => {
  const statusEvents = [call('s1', 'edit_file', 0, 1)];
  const approvals = [{ approval_id: 'a1', run_id: RUN, tool_name: 'edit_file', decision: 'allow', created_at: at(0.1), resolved_at: at(1),
    args: { path: '/p/.lea/memory.md', old_string: 'a', new_string: 'a\nb\nc' } }];
  const messages = [{ id: 'm', run_id: RUN, role: 'assistant', created_at: at(3) }];
  const { calls } = deriveToolCalls({ statusEvents, approvals, messages, runStatusById: { [RUN]: 'answered' } });
  const view = toolRowModel({ name: 'edit_file', args: calls[0].args }, callResult(calls[0]));
  assert.deepEqual([view.target, view.result], ['memory.md', '+2']);
});

test('deriveToolCalls: a run that was stopped leaves its last call stopped', () => {
  const { calls } = deriveToolCalls({ statusEvents: [call('s1', 'bash', 0, 1)], runStatusById: { [RUN]: 'cancelled' } });
  assert.equal(calls[0].state, 'stopped');
  assert.equal(toolRowModel({ name: 'bash' }, callResult(calls[0])).result, 'stopped');
});

test('deriveToolCalls: a tool_result status event ends a call exactly', () => {
  const statusEvents = [
    call('s1', 'search_mathlib', 0, 1),
    { id: 's2', run_id: RUN, status: 'tool_result', tool: 'search_mathlib', turn: 1, hits: 7, created_at: at(1.2) },
  ];
  const { calls } = deriveToolCalls({ statusEvents, liveRunId: RUN });
  assert.equal(calls[0].exact, true);
  assert.equal(calls[0].endedAt - calls[0].startedAt, 1200);
  assert.equal(toolRowModel({ name: 'search_mathlib' }, callResult(calls[0])).result, '7 hits');
});

test('previousStepByPath links each step to the prior one for its file', () => {
  const steps = [{ id: '1', path: 'A' }, { id: '2', path: 'B' }, { id: '3', path: 'A' }];
  const prev = previousStepByPath(steps);
  assert.equal(prev.get('1'), null);
  assert.equal(prev.get('3')?.id, '1');
});

// ── approvals ────────────────────────────────────────────────────────────────

test('approvalAsk phrases the pending action', () => {
  assert.equal(approvalAsk({ tool_name: 'bash', args: { command: 'ls' } }), 'Lea wants to run a shell command');
  assert.equal(approvalAsk({ tool_name: 'write_file', args: { path: '/a/Sqrt2.lean', content: 'a\nb\n' } }), 'Lea wants to write Sqrt2.lean (2 lines)');
  assert.equal(approvalAsk({ tool_name: 'edit_file', args: { path: 'M.md', old_string: 'a', new_string: 'b' } }), 'Lea wants to edit M.md (+1 −1)');
  assert.equal(approvalAsk({ tool_name: 'github__create_issue', args: {} }), 'Lea wants to use github__create_issue');
  assert.equal(decisionLabel('always_session'), 'approved for this session');
  assert.equal(decisionLabel('deny'), 'denied');
});

// ── the live run ─────────────────────────────────────────────────────────────

test('runClock: start from the run’s user message, turn from the calls, streaming from live text', () => {
  const statusEvents = [call('s1', 'lean_check', 10, 3),
    { id: 's2', run_id: RUN, status: 'lean_check', check_status: 'ok', turn: 3, created_at: at(12) }];
  const { calls } = deriveToolCalls({ statusEvents, liveRunId: RUN });
  const messages = [
    { id: 'u', run_id: RUN, role: 'user', content: 'prove it', created_at: at(0) },
    { id: 'm', run_id: RUN, role: 'assistant', content: 'Working', live: true, stream_state: 'streaming', created_at: at(5) },
  ];
  const clock = runClock({ runId: RUN, calls, messages, statusEvents, seenAt: { m: Date.parse(at(9)) } });
  assert.equal(clock.startedAt, Date.parse(at(0)));
  assert.equal(clock.lastActivityAt, Date.parse(at(12)));
  assert.equal(clock.turn, 4); // turn 3's only call finished, so the model is on turn 4
  assert.equal(clock.streaming, true);
  assert.equal(clock.streamingSince, Date.parse(at(9)));
  assert.equal(clock.busy, false);
});

test('runClock: an open call keeps the turn and marks the run busy', () => {
  const { calls } = deriveToolCalls({ statusEvents: [call('s1', 'bash', 3, 2)], liveRunId: RUN });
  const clock = runClock({ runId: RUN, calls, statusEvents: [call('s1', 'bash', 3, 2)], fallbackStart: Date.parse(at(1)) });
  assert.equal(clock.turn, 2);
  assert.equal(clock.busy, true);
  assert.equal(clock.startedAt, Date.parse(at(3)));
  assert.equal(runClock({ runId: 'other', fallbackStart: 5 }).startedAt, 5);
});

test('composerStatus: live states', () => {
  assert.deepEqual(composerStatus({ live: true, turn: 4, startedAt: 7 }),
    { tone: 'running', label: 'Lea is working · turn 4', since: 7, stop: true });
  assert.equal(composerStatus({ live: true, turn: 2, subagentsRunning: 2 })?.label, 'Lea is working · turn 2 · 2 sub-agents');
  assert.equal(composerStatus({ live: true, waitingApproval: true })?.label, 'Waiting for your approval');
  assert.equal(composerStatus({ live: true, stopRequested: true, waitingApproval: true })?.label, 'Stopping… waiting for the current step');
  assert.equal(composerStatus({ live: true, pending: true })?.label, 'Waiting for a free run slot');
});

test('composerStatus: settled states and the blueprint proposal', () => {
  const done = (outcome) => composerStatus({ live: false, outcome })?.label;
  assert.equal(composerStatus({ live: false, proposalInReview: true })?.label, 'Proposal waiting for your review');
  assert.equal(done({ status: 'proved', proof: 'proved' }), 'Done · proved');
  assert.equal(done({ status: 'proved', proof: 'proved', resultKind: 'defined' }), 'Done · defined');
  assert.equal(done({ status: 'proved', proof: 'stubbed', sorries: 2 }), 'Done · 2 sorry');
  assert.equal(done({ status: 'failed', proof: 'failed', errors: 1 }), 'Done · 1 error');
  assert.equal(done({ status: 'failed', proof: 'failed' }), 'Done · check failed');
  assert.equal(done({ status: 'failed' }), 'Run failed');
  assert.equal(done({ status: 'cancelled' }), 'Stopped');
  assert.equal(done({ status: 'cancelled', stopReason: 'timeout' }), 'Stopped · time limit');
  assert.equal(done({ status: 'max_turns' }), 'Stopped · turn limit');
  assert.equal(done({ status: 'disproved' }), 'Done · disproved');
  assert.equal(done({ status: 'answered' }), 'Done');
  assert.equal(composerStatus({ live: false }), null);
});

// ── user bubbles ─────────────────────────────────────────────────────────────

test('userBubbleModel: a leading slash command becomes a chip', () => {
  assert.deepEqual(userBubbleModel('/blueprint prove parity_descent'),
    { command: '/blueprint prove parity_descent', prompt: null, body: '', context: null });
  assert.deepEqual(userBubbleModel('/compact\nplease keep the lemmas'),
    { command: '/compact', prompt: null, body: 'please keep the lemmas', context: null });
  // A path is not a command.
  assert.equal(userBubbleModel('/Users/me/A.lean fails').command, null);
});

test('userBubbleModel: the prompts /blueprint expands into show as the command', () => {
  const draft = userBubbleModel('Draft the proof blueprint for this project and write it to .lea/blueprint.md. Do not prove anything yet.\nProject: X');
  assert.equal(draft.command, '/blueprint');
  assert.ok(draft.prompt?.startsWith('Draft the proof blueprint'));
  const node = userBubbleModel('Prove the blueprint node `parity_descent` (lemma) from .lea/blueprint.md.\n\nStatement: …');
  assert.equal(node.command, '/blueprint prove parity_descent');
});

test('userBubbleModel: the file-context header becomes a chip under the bubble', () => {
  const m = userBubbleModel('[File in context: Lea/Sqrt2/Parity.lean:14 · ask about this file]\n\nwhy does this fail?');
  assert.deepEqual(m.context, { path: 'Lea/Sqrt2/Parity.lean', line: 14, label: null, intent: 'ask', chip: 'Parity.lean:14', note: 'in context' });
  assert.equal(m.body, 'why does this fail?');
  const p = userBubbleModel('[File in context: A/B.lean (B) · prove the sorries here]\n\nProve the remaining sorries in A/B.lean.');
  assert.equal(p.context?.intent, 'prove-sorries');
  assert.equal(p.context?.chip, 'B');
  assert.equal(p.body, 'Prove the remaining sorries in A/B.lean.');
  const both = userBubbleModel('[File in context: X.lean · ask about this file]\n\n/blueprint next');
  assert.equal(both.command, '/blueprint next');
  assert.equal(both.context?.chip, 'X.lean');
});

test('userBubbleModel: plain text passes through', () => {
  assert.deepEqual(userBubbleModel('Prove that √2 is irrational.'),
    { command: null, prompt: null, body: 'Prove that √2 is irrational.', context: null });
});
