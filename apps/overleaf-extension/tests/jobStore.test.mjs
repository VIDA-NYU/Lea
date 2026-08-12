import assert from "node:assert/strict";
import test from "node:test";
import {
  findActiveJob,
  findLatestFinishedJob,
  findLatestJob,
  jobRecency,
  pruneJobs
} from "../companion/jobStore.mjs";

function makeJob(id, overrides = {}) {
  const minute = String(overrides.seq ?? 0).padStart(4, "0");
  return {
    jobId: id,
    jobKey: "project-1:theorem:sample",
    status: "formalized",
    mode: "formalization",
    startedAt: `2026-01-01T00:${minute.slice(0, 2)}:${minute.slice(2)}.000Z`,
    finishedAt: `2026-01-01T01:${minute.slice(0, 2)}:${minute.slice(2)}.000Z`,
    leaSessionId: null,
    declarationName: "sample",
    ...overrides
  };
}

function makeStore(count, overrides = () => ({})) {
  const jobs = {};
  for (let i = 0; i < count; i += 1) {
    const id = `job-${String(i).padStart(4, "0")}`;
    jobs[id] = makeJob(id, { seq: i, ...overrides(i) });
  }
  return jobs;
}

test("jobRecency prefers finishedAt and falls back to startedAt", () => {
  assert.equal(jobRecency({ finishedAt: "b", startedAt: "a" }), "b");
  assert.equal(jobRecency({ startedAt: "a" }), "a");
  assert.equal(jobRecency({}), "");
});

test("prune keeps the newest N per key and removes older finished jobs", () => {
  const jobs = makeStore(50);
  const { jobs: kept, removed, changed } = pruneJobs(jobs, { keepPerKey: 20 });
  assert.equal(changed, true);
  // newest 20 by recency are the highest sequence numbers
  assert.ok(kept["job-0049"]);
  assert.ok(kept["job-0030"]);
  assert.ok(!kept["job-0010"]);
  // ...except the single oldest-of-status-mode representative rule below:
  // all jobs share one (status, mode), so exactly newest-20 survive.
  assert.equal(Object.keys(kept).length, 20);
  assert.equal(removed.length, 30);
});

test("prune never removes in_progress jobs regardless of age", () => {
  const jobs = makeStore(50, (i) => (i === 0 ? { status: "in_progress", finishedAt: null } : {}));
  const { jobs: kept } = pruneJobs(jobs, { keepPerKey: 5 });
  assert.ok(kept["job-0000"], "the oldest job is live and must survive");
});

test("prune keeps the newest job of each (status, mode) combination beyond the window", () => {
  const jobs = makeStore(60, (i) => {
    if (i === 3) return { status: "disproved" };
    if (i === 5) return { status: "failed", mode: "repair" };
    if (i === 7) return { status: "needs_review" };
    return {};
  });
  const { jobs: kept } = pruneJobs(jobs, { keepPerKey: 10 });
  assert.ok(kept["job-0003"], "only disproved job must survive");
  assert.ok(kept["job-0005"], "only failed repair job must survive");
  assert.ok(kept["job-0007"], "only needs_review job must survive");
  assert.ok(!kept["job-0004"], "an old job with a duplicated status/mode is pruned");
});

test("prune keeps the newest session-linked job per declarationName", () => {
  const jobs = makeStore(60, (i) => {
    if (i === 2) return { leaSessionId: "sess-old", declarationName: "renamed_decl" };
    if (i === 4) return { leaSessionId: "sess-old-2", declarationName: "renamed_decl" };
    return {};
  });
  const { jobs: kept } = pruneJobs(jobs, { keepPerKey: 10 });
  // the newest session-linked job for "renamed_decl" is job-0004; job-0002 is
  // an older duplicate for the same declaration and may be pruned.
  assert.ok(kept["job-0004"]);
  assert.ok(!kept["job-0002"]);
});

test("prune reports changed=false when everything fits the window", () => {
  const jobs = makeStore(10);
  const { jobs: kept, removed, changed } = pruneJobs(jobs, { keepPerKey: 20 });
  assert.equal(changed, false);
  assert.equal(removed.length, 0);
  assert.deepEqual(Object.keys(kept).sort(), Object.keys(jobs).sort());
});

test("latest-job selections answer identically before and after pruning", () => {
  const jobs = makeStore(120, (i) => {
    if (i % 17 === 0) return { status: "failed" };
    if (i % 23 === 0) return { status: "disproved" };
    if (i === 119) return { status: "in_progress", finishedAt: null };
    if (i % 11 === 0) return { leaSessionId: `sess-${i}` };
    return {};
  });
  const key = "project-1:theorem:sample";
  const before = {
    active: findActiveJob(jobs, key)?.jobId,
    failed: findLatestJob(jobs, key, "failed")?.jobId,
    disproved: findLatestJob(jobs, key, "disproved")?.jobId,
    formalized: findLatestJob(jobs, key, "formalized")?.jobId,
    finished: findLatestFinishedJob(jobs, key)?.jobId
  };
  const { jobs: kept } = pruneJobs(jobs, { keepPerKey: 20 });
  const after = {
    active: findActiveJob(kept, key)?.jobId,
    failed: findLatestJob(kept, key, "failed")?.jobId,
    disproved: findLatestJob(kept, key, "disproved")?.jobId,
    formalized: findLatestJob(kept, key, "formalized")?.jobId,
    finished: findLatestFinishedJob(kept, key)?.jobId
  };
  assert.deepEqual(after, before);
});

test("prune keeps kept-job object identity so in-flight mutation still lands", () => {
  const jobs = makeStore(30);
  const live = jobs["job-0029"];
  const { jobs: kept } = pruneJobs(jobs, { keepPerKey: 5 });
  assert.equal(kept["job-0029"], live);
  live.status = "failed";
  assert.equal(kept["job-0029"].status, "failed");
});
