import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { reconcilePendingJobUsage } from "../companion/workerService.mjs";
import { createEventBus } from "../companion/eventBus.mjs";

test("late timeout settlement records exact-run usage without rewriting the original job", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-timeout-accounting-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const job = {
    jobId: "job-1", apiRunId: "run-1", leaSessionId: "session-1", overleafProjectId: "history-project",
    status: "paused", timedOut: true, stopReason: "timeout",
    usageSyncPending: true, usageStatus: "unknown", costUsd: 0,
    usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
  };
  const state = {
    jobs: { "job-1": job }, jobsPath: path.join(dir, "jobs.json"),
    settings: { leaApiBaseUrl: "http://adapter.test" }, env: {},
    fetchImpl: async (url) => {
      assert.match(url, /\/api\/runs\/run-1$/);
      return { ok: true, text: async () => JSON.stringify({
        id: "run-1", status: "cancelled", stop_reason: "timeout",
        input_tokens: 120, output_tokens: 30, cost_usd: 0.42,
        usage_status: "final", usage_revision: 2,
      }) };
    },
  };
  const events = [];
  state.eventBus = createEventBus();
  state.eventBus.subscribe(event => events.push(event));
  assert.ok(await reconcilePendingJobUsage(state) > 0);
  assert.equal(job.costUsd, 0);
  assert.equal(job.reconciledUsage.costUsd, 0.42);
  assert.equal(job.reconciledUsage.totalTokens, 150);
  assert.equal(job.reconciledStopReason, "timeout");
  assert.equal(job.usageSyncPending, false);
  assert.equal(job.settling, false);
  const persisted = JSON.parse(await fs.readFile(state.jobsPath, "utf8"));
  assert.equal(persisted["job-1"].reconciledUsage.costUsd, 0.42);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'jobs-changed');
  assert.equal(events[0].overleafProjectId, 'history-project');
});

test("unavailable adapter usage stays unconfirmed and retries with backoff", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-timeout-accounting-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  let calls = 0;
  const job = { jobId: "job-1", apiRunId: "run-1", leaSessionId: "session-1",
    status: "paused", timedOut: true, usageSyncPending: true };
  const state = { jobs: { "job-1": job }, jobsPath: path.join(dir, "jobs.json"),
    settings: { leaApiBaseUrl: "http://adapter.test" }, env: {},
    fetchImpl: async () => { calls++; return { ok: false, status: 503, text: async () => "{}" }; } };
  const events = [];
  state.eventBus = createEventBus();
  state.eventBus.subscribe(event => events.push(event));
  assert.equal(await reconcilePendingJobUsage(state), 1);
  assert.equal(job.reconciledUsage, undefined);
  assert.equal(job.usageSyncAttempts, 1);
  assert.ok(Date.parse(job.usageNextSyncAt) > Date.now());
  assert.equal(await reconcilePendingJobUsage(state), 0);
  assert.equal(calls, 1);
  const persisted = JSON.parse(await fs.readFile(state.jobsPath, 'utf8'));
  assert.equal(persisted['job-1'].usageSyncAttempts, 1);
  assert.equal(persisted['job-1'].usageSyncPending, true);
  assert.deepEqual(events, []);
});

test('unchanged partial accounting persists backoff without reloading inventories', async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'lea-timeout-accounting-'));
  t.after(() => fs.rm(dir, {recursive: true, force: true}));
  const job = {jobId: 'job-1', apiRunId: 'run-1', leaSessionId: 'session-1',
    overleafProjectId: 'history-project', status: 'paused', usageSyncPending: true,
    adapterOutcomeStatus: 'cancelled', reconciledUsage: {runId: 'run-1', revision: 1, status: 'partial', costUsd: .1}};
  let revision = 1;
  const state = {jobs: {'job-1': job}, jobsPath: path.join(dir, 'jobs.json'),
    settings: {leaApiBaseUrl: 'http://adapter.test'}, env: {}, eventBus: createEventBus(),
    fetchImpl: async () => ({ok: true, text: async () => JSON.stringify({
      id: 'run-1', status: 'cancelled', usage_status: 'partial', usage_revision: revision,
      cost_usd: revision / 10, input_tokens: revision, output_tokens: 1})})};
  const events = [];
  state.eventBus.subscribe(event => events.push(event));
  assert.equal(await reconcilePendingJobUsage(state), 1);
  assert.equal(job.usageSyncAttempts, 1);
  assert.equal(job.reconciledUsage.costUsd, .1);
  assert.deepEqual(events, []);
  job.usageNextSyncAt = null;
  revision = 2;
  assert.ok(await reconcilePendingJobUsage(state) > 1);
  assert.equal(job.reconciledUsage.costUsd, .2);
  assert.equal(job.status, 'paused');
  assert.equal(job.usageSyncPending, true);
  assert.equal(events.length, 1);
  assert.equal(events[0].overleafProjectId, 'history-project');
});

test("a legacy user_stop row cannot rewrite a locally observed timeout", async (t) => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-timeout-accounting-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const job = { jobId: "job-1", apiRunId: "run-1", leaSessionId: "session-1",
    status: "paused", timedOut: true, stopReason: "timeout", usageSyncPending: true };
  const state = { jobs: { "job-1": job }, jobsPath: path.join(dir, "jobs.json"),
    settings: { leaApiBaseUrl: "http://adapter.test" }, env: {},
    fetchImpl: async () => ({ ok: true, text: async () => JSON.stringify({
      id: "run-1", status: "cancelled", stop_reason: "user_stop", cost_usd: 0,
    }) }) };
  await reconcilePendingJobUsage(state);
  assert.equal(job.stopReason, "timeout");
  assert.equal(job.reconciledStopReason, undefined);
  assert.equal(job.reconciledUsage.status, "unknown");
});
