// Driven by pytest with a real migrated SQLite DB and workspace command API.
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { openWorkerDatabase } from '../companion/databaseState.mjs';
import { setWorkerDatabase, applyProofOutcomeToJob, reconcileRegistryUpdates,
  reconcileRecoveredBatches } from '../companion/workerService.mjs';

const input = JSON.parse(readFileSync(0, 'utf8'));
const database = openWorkerDatabase(input);
setWorkerDatabase(database);
try {
  const state = { settings: { leaRepoPath: input.leaRoot, leaApiBaseUrl: input.baseUrl },
    jobsPath: '/__sql__/jobs.json', repairBatchesPath: '/__sql__/jobs.json.batches.json',
    jobs: database.get('jobs.json', {}), fetchImpl: fetch, env: {} };
  if (input.phase === 'finalize') {
    const job = { jobId: 'registry-job', jobKey: `observed:${input.kind}:result`, targetKind: input.kind,
      targetLabel: 'result', declarationName: 'result', projectSlug: 'observed',
      projectNamespace: 'Lea.Observed', overleafProjectId: 'observed', apiRunId: input.runId,
      formalizationId: input.formalizationId, leaSessionId: input.sessionId,
      acceptedCommandId: input.parent, status: 'in_progress', startedAt: new Date().toISOString(),
      logPath: '/__sql__/registry.log', leaApiBaseUrl: input.baseUrl };
    state.jobs[job.jobId] = job;
    const target = { targetKind: input.kind, targetLabel: 'result', projectSlug: 'observed',
      projectNamespace: 'Lea.Observed', overleafProjectId: 'observed',
      projectMarkdownPath: path.join(input.leaRoot, 'workspace/projects/observed.md') };
    await database.runAccepted(input.parent, () => applyProofOutcomeToJob({ state, job, target,
      exit: { ok: true, resultKind: input.kind === 'definition' ? 'defined' : 'proved', apiRunId: input.runId } }));
    state.repairBatches = { 'formalize-batch-registry': { batchId: 'formalize-batch-registry', operation: 'formalize',
      overleafProjectId: 'observed', acceptedCommandId: input.parent, createdAt: job.startedAt,
      done: false, running: false, usesByLabel: new Map(), items: [{ targetKind: input.kind,
        targetLabel: 'result', state: 'running', runJobId: job.jobId }] } };
    await database.runAccepted(input.parent, () => reconcileRecoveredBatches(state));
    await state.repairBatches['formalize-batch-registry'].reportSubmission;
  } else {
    if (['missing', 'legacy-busy', 'different-run'].includes(input.phase)) {
      const job = state.jobs['registry-job'];
      delete job.registryUpdate;
      delete job.registryRecoveryCheckedAt;
      job.recordedProofPath = null;
      if (input.phase !== 'missing') {
        job.status = 'failed';
        job.error = 'Wait for active project work before editing files.';
      }
      if (input.phase === 'different-run') job.apiRunId = 'a-different-run';
      database.put('jobs.json', state.jobs);
    }
    await reconcileRegistryUpdates(state, { now: Date.now() + 60000 });
  }
  process.stdout.write(JSON.stringify(state.jobs['registry-job']));
} finally {
  setWorkerDatabase(null);
  database.close();
}
