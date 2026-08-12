// Job-store helpers extracted from server.mjs (PLAN-system-hardening 0.2, plus
// the D1 strangler rule: seams a change touches move into their own module).
// This owns the one recency definition every "latest job" selection uses, and
// the retention prune that keeps jobs.json bounded over months of use.

// One recency definition for every "latest job" selection (AUDIT L6/L7): a
// job's recency is when it FINISHED, falling back to when it started for a job
// still lacking a finishedAt. Plain string comparison rather than
// localeCompare: ISO-8601 timestamps sort correctly bytewise, and it's
// deterministic (no locale sensitivity, no odd "null" coercion).
export function jobRecency(job) {
  return String(job?.finishedAt || job?.startedAt || "");
}

export function jobsByRecencyDesc(jobs, predicate) {
  return Object.values(jobs || {})
    .filter(predicate)
    .sort((a, b) => (jobRecency(a) < jobRecency(b) ? 1 : jobRecency(a) > jobRecency(b) ? -1 : 0));
}

export function findActiveJob(jobs, jobKey) {
  return Object.values(jobs || {}).find((job) => job.jobKey === jobKey && job.status === "in_progress");
}

export function findLatestJob(jobs, jobKey, status) {
  return jobsByRecencyDesc(jobs, (job) => job.jobKey === jobKey && job.status === status)[0] || null;
}

export function findLatestFinishedJob(jobs, jobKey) {
  return jobsByRecencyDesc(jobs, (job) => job.jobKey === jobKey && job.status !== "in_progress")[0] || null;
}

// The newest job whose terminal result can be the provenance of an artifact
// that still exists. Failed attempts are deliberately excluded: a retry retires
// the previous proof before running and restores it on failure, so attributing
// the restored proof to the failed retry's source hash would incorrectly make
// it look current.
const ARTIFACT_JOB_STATUSES = new Set([
  "formalized",
  "repaired",
  "needs_review",
  "disproved",
  "sorry_stub"
]);

function hasArtifactResult(job) {
  if (ARTIFACT_JOB_STATUSES.has(job?.status)) return true;
  // A proof run can fail while still leaving a checked sorry stub as its
  // explicit effective result. That stub has real source provenance.
  return job?.status === "failed"
    && job?.finalStatus === "sorry_stub"
    && Boolean(job?.declarationName)
    && Boolean(job?.recordedProofPath);
}

export function findLatestArtifactJob(jobs, jobKey, {
  declarationName = "",
  recordedProofPath = ""
} = {}) {
  const candidates = jobsByRecencyDesc(
    jobs,
    (job) => job.jobKey === jobKey
      && hasArtifactResult(job)
      && Boolean(job.targetTextHash)
  );
  if (candidates.length === 0) return null;

  const wantedDeclaration = String(declarationName || "").trim();
  const wantedPath = String(recordedProofPath || "").trim();
  const matching = candidates.find((job) => {
    if (wantedPath && job.recordedProofPath === wantedPath) return true;
    return wantedDeclaration && job.declarationName === wantedDeclaration;
  });
  return matching || candidates[0];
}

// Retention prune (PLAN-system-hardening 0.2 / review B4): nothing ever removed
// jobs, so jobs.json grew without bound and every /statuses hit paid for it.
// Per jobKey we keep a superset of everything the status/selection queries can
// reach, so pruning is invisible to behavior:
//   - the newest `keepPerKey` jobs (recency order);
//   - every job still in_progress (a live run must never lose its record);
//   - the newest job of each distinct (status, mode) combination — preserving
//     every findLatestJob(key, status) / latest-stub / latest-repair answer;
//   - the newest session-linked job per declarationName — preserving
//     findLatestJobWithLeaSession* resolution (chat / edit / rename paths).
// Returns { jobs, removed, changed }; kept job objects retain identity, so
// in-flight closures mutating a kept job keep working on the same object.
export function pruneJobs(jobs, { keepPerKey = 20 } = {}) {
  const byKey = new Map();
  for (const [id, job] of Object.entries(jobs || {})) {
    const key = String(job?.jobKey || "");
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push([id, job]);
  }

  const keepIds = new Set();
  for (const entries of byKey.values()) {
    entries.sort((a, b) => (jobRecency(a[1]) < jobRecency(b[1]) ? 1 : jobRecency(a[1]) > jobRecency(b[1]) ? -1 : 0));
    const seenStatusMode = new Set();
    const seenSessionDecl = new Set();
    entries.forEach(([id, job], index) => {
      if (index < keepPerKey || job?.status === "in_progress") {
        keepIds.add(id);
      }
      const statusMode = `${job?.status || ""}\u0000${job?.mode || ""}`;
      if (!seenStatusMode.has(statusMode)) {
        seenStatusMode.add(statusMode);
        keepIds.add(id);
      }
      if (job?.leaSessionId) {
        const decl = String(job.declarationName || "");
        if (!seenSessionDecl.has(decl)) {
          seenSessionDecl.add(decl);
          keepIds.add(id);
        }
      }
    });
  }

  const kept = {};
  const removed = [];
  for (const [id, job] of Object.entries(jobs || {})) {
    if (keepIds.has(id)) {
      kept[id] = job;
    } else {
      removed.push(job);
    }
  }
  return { jobs: kept, removed, changed: removed.length > 0 };
}
