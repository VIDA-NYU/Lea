import { createHash } from 'node:crypto';
import {sourceHashInputs} from '../../apps/overleaf-extension/extension/sourceIdentityCore.mjs';
// Cross-layer integration harness (PLAN-system-hardening Phase 1 / review B6).
//
// Boots the REAL adapter (uvicorn, stub prover via adapter_entry.py) and the
// REAL companion (in-process createServer on an ephemeral port), then drives
// the extension's exact HTTP shapes end-to-end. This is the contract net the
// per-layer suites with fakes cannot provide: SSE event names, done.status
// vocabulary, and session-detail field names are asserted against the real
// wire, so drift between the layers fails here instead of in production.
//
// Run with: npm run test:integration  (needs the adapter venv provisioned)

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";

import { createServer } from "../../apps/overleaf-extension/companion/server.mjs";
import {
  fetchApiSessionDetail,
  startApiRun,
  streamApiRun
} from "../../apps/overleaf-extension/companion/leaApiClient.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.LEA_TEST_PYTHON || path.join(REPO_ROOT, "apps", "lea-standalone", "adapter", ".venv", "bin", "python");
const ADAPTER_ENTRY = path.join(REPO_ROOT, "tests", "integration", "adapter_entry.py");

// The adapter's current event vocabulary (bridge.emit call sites). A new event
// type is fine — add it here deliberately — but an unexpected one usually
// means a layer renamed something the other layers still expect.
const KNOWN_EVENT_TYPES = new Set([
  "status", "assistant_delta", "message", "code_step", "project_updated",
  "run_error", "approval_requested", "approval_resolved", "queued", "resync", "done"
]);
const KNOWN_DONE_STATUSES = new Set([
  "proved", "disproved", "needs_review", "answered", "max_turns", "cancelled", "failed"
]);

let home;
let adapter;
let adapterBaseUrl;
let companion;
let companionBaseUrl;
let companionJobsPath;
let completedRunId;

async function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

async function waitFor(predicate, { timeoutMs = 30000, intervalMs = 100, label = "condition" } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${label}.`);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

async function makeFixtureHome() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-itest-"));
  const prover = path.join(dir, "prover");
  const workspace = path.join(prover, "workspace");
  await fs.mkdir(path.join(workspace, "proofs"), { recursive: true });
  await fs.mkdir(path.join(workspace, "projects"), { recursive: true });
  await fs.mkdir(path.join(prover, "lea"), { recursive: true });
  await fs.writeFile(path.join(prover, "pyproject.toml"), "[project]\nname = \"itest-stub\"\n");
  await fs.writeFile(path.join(workspace, "lean-toolchain"), "leanprover/lean4:v4.29.0\n");
  await fs.writeFile(path.join(workspace, "lakefile.lean"), 'import Lake\nopen Lake DSL\npackage itest\nlean_lib Lea where\n  srcDir := "proofs"\n');
  await fs.mkdir(path.join(dir, "config"), { recursive: true });
  await fs.writeFile(
    path.join(dir, "config", "lea.local.toml"),
    `model = "o4-mini"\nmax_turns = 5\nlea_root = ${JSON.stringify(prover)}\n`
  );
  await fs.mkdir(path.join(dir, "data"), { recursive: true });
  await fs.mkdir(path.join(dir, "companion"), { recursive: true });
  // Initialize Lake's manifest/config cache before the staged repair's read-only
  // canonical baseline is confined. No dependencies or model requests are used.
  if(!process.env.LEA_ITEST_SKIP_LEAN) {
    const lake=spawn('lake',['env','lean','--version'],{cwd:workspace,stdio:['ignore','pipe','pipe']});
    let output='';lake.stdout.on('data',chunk=>output+=chunk);lake.stderr.on('data',chunk=>output+=chunk);
    assert.equal(await new Promise((resolve,reject)=>{lake.once('exit',resolve);lake.once('error',reject);}),0,output);
  }


  return { dir, prover };
}

before(async () => {
  const fixture = await makeFixtureHome();
  home = fixture.dir;
  if(process.env.LEA_ITEST_DEBUG)console.log("Fixture home",home);

  const port = await freePort();
  adapterBaseUrl = `http://127.0.0.1:${port}`;
  adapter = spawn(PYTHON, [ADAPTER_ENTRY], {
    env: { ...process.env, LEA_ITEST_HOME: home, LEA_ITEST_PORT: String(port) },
    stdio: ["ignore", "pipe", "pipe"]
  });
  let adapterLog = "";
  adapter.stdout.on("data", (chunk) => { adapterLog += chunk; });
  adapter.stderr.on("data", (chunk) => { adapterLog += chunk; if(process.env.LEA_ITEST_DEBUG)process.stderr.write(chunk); });
  const exited = new Promise((_, reject) => {
    adapter.once("exit", (code) => reject(new Error(`adapter exited early (code ${code}):\n${adapterLog}`)));
  });
  await Promise.race([
    exited,
    waitFor(async () => {
      try {
        const res = await fetch(`${adapterBaseUrl}/api/ready`);
        return res.ok;
      } catch {
        return false;
      }
    }, { timeoutMs: 60000, intervalMs: 250, label: "adapter boot" })
  ]);
  adapter.removeAllListeners("exit");

  companionJobsPath = path.join(home, "companion", "jobs.json");
  companion = await createServer({
    settingsPath: path.join(home, "companion", "settings.json"),
    envPath: path.join(home, ".env"),
    jobsPath: companionJobsPath,
    chatSessionsPath: path.join(home, "companion", "chatSessions.json"),
    env: {
      OPENAI_API_KEY: "itest-key",
      LEA_ROOT: fixture.prover,
      LEA_API_BASE_URL: adapterBaseUrl,
      LEA_UI_BASE_URL: "http://localhost:5173",
      LEA_MODEL: "o4-mini",
      LEA_MAX_TURNS: "5",
      LEA_JOB_TIMEOUT_SECONDS: "60"
    }
  });
  await new Promise((resolve) => companion.listen(0, "127.0.0.1", resolve));
  companionBaseUrl = `http://127.0.0.1:${companion.address().port}`;
});

after(async () => {
  if (companion) await new Promise((resolve) => companion.close(resolve));
  if (adapter && adapter.exitCode === null) {
    adapter.kill("SIGTERM");
    await new Promise((resolve) => {
      adapter.once("exit", resolve);
      setTimeout(() => {
        adapter.kill("SIGKILL");
        resolve();
      }, 3000).unref();
    });
  }
  if (home && !process.env.LEA_ITEST_KEEP_HOME) await fs.rm(home, { recursive: true, force: true });
  if(process.env.LEA_ITEST_KEEP_HOME)console.log('Retained fixture',home);
});

const publicationSequences=new Map(),publicationGenerations=new Map();
async function publishedTarget(body,{complete=false}={}) {
  const document=body.overleafProjectId;
  const response=await fetch(adapterBaseUrl+'/api/overleaf/publishers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({document_id:document,client_id:'integration-tab',expected_generation:publicationGenerations.get(document)||0})});
  const authority=await response.json();assert.equal(response.status,200,JSON.stringify(authority));
  publicationGenerations.set(document,authority.generation);
  const proof='Direct proof.',content=body.targetText+'\n'+proof,hash=v=>createHash('sha256').update(v).digest('hex');
  const source={version:2,targetKey:body.targetLabel,targetKind:body.targetKind||'theorem',statement:body.targetText,proof,
    proofAssociation:{status:'associated',method:'adjacent',sourceFile:complete?'main.tex':'',sourceStartLine:0,sourceEndLine:0,proofHash:hash(proof)},
    statementLocation:{sourceFile:complete?'main.tex':'',sourceStartLine:0,sourceEndLine:0},uses:body.targetUses||[],context:body.targetContext||'',
    relevantSource:complete?[{kind:'surrounding-context',path:'main.tex',startLine:1,endLine:2,content,truncated:false}]:[],
    mirror:complete?{revision:hash('main.tex\0'+hash(content)),mirroredFileCount:1,verified:true}:null};
  const hashes=sourceHashInputs(source);source.bundleHash=hash(JSON.stringify(hashes.evidence));source.sourceIdentityHash=hash(JSON.stringify(hashes.identity));
  const sequence=(publicationSequences.get(document)||0)+1;publicationSequences.set(document,sequence);
  const published=await fetch(adapterBaseUrl+'/api/overleaf/publications',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:1,document_id:document,client_id:'integration-tab',generation:authority.generation,sequence,source_bundle:source,files:complete?{'main.tex':content}:{},coverage:complete?'complete':'partial'})});
  const receipt=await published.json();assert.equal(published.status,201,JSON.stringify(receipt));
  return {...body,sourceBundle:source,source_context_ref:receipt.source_context_ref,mirrorAvailable:complete};
}
async function postJson(url, body, idempotencyKey=null) {
  if(['/formalize','/stub'].includes(new URL(url).pathname)&&!body.source_context_ref)body=await publishedTarget(body);
  const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json',...(idempotencyKey?{'Idempotency-Key':idempotencyKey}:{})},body:JSON.stringify(body)});
  return {status:response.status,body:await response.json()};
}

async function readCompanionJobs() {
  const {jobs}=await fetch(adapterBaseUrl+'/api/companion/jobs').then(r=>r.json());
  return Object.fromEntries(await Promise.all(jobs.map(async ({id})=>[id,await fetch(adapterBaseUrl+'/api/companion/jobs/'+encodeURIComponent(id)).then(r=>r.json())])));
}

async function pollStatus(overleafProjectId, target, wanted, { timeoutMs = 90000 } = {}) {
  let last;
  try {
    return await waitFor(async () => {
      const res=await postJson(`${companionBaseUrl}/statuses`,{overleafProjectId,targets:[target]});
      last=res.body?.statuses?.[`${target.targetKind}:${target.targetLabel}`];
      return wanted.includes(last?.status)?last:null;
    },{timeoutMs,label:`status in {${wanted}}`});
  } catch(error) {
    const health=await fetch(adapterBaseUrl+'/api/health').then(r=>r.json()).catch(()=>null);
    const jobs=Object.values(await readCompanionJobs()).filter(job=>job.overleafProjectId===overleafProjectId);
    error.message+=' '+JSON.stringify({last,health,jobs});throw error;
  }
}

test("adapter wire contract: events, done vocabulary, persistence, completed-run reattach", async () => {
  const events = [];
  const start = await startApiRun({
    fetchImpl: fetch,
    baseUrl: adapterBaseUrl,
    apiKey: null,
    message: "Formalize the Overleaf theorem labeled wire_contract_check.\n\nTrue is true.",
    projectSlug: "itest-wire",
    projectTitle: "Wire contract",
    origin: "overleaf",
    originUrl: "https://www.overleaf.com/project/itest-wire"
  });
  assert.equal(start.ok, true, start.error);
  const runId = start.body.run_id;
  completedRunId = runId;
  const sessionId = start.body.session_id;
  assert.ok(runId && sessionId);
  assert.equal(start.body.project_slug, "itest-wire");
  // Phase 2: creation enqueues; the response reports the FIFO position (it may
  // already be null if the worker started the run before we read it).
  assert.ok("queue_position" in start.body, "create response carries queue_position");

  const outcome = await streamApiRun({
    fetchImpl: fetch,
    baseUrl: adapterBaseUrl,
    apiKey: null,
    runId,
    onEvent: (type, data) => events.push({ type, data })
  });
  assert.equal(outcome.ok, true, outcome.error);
  assert.equal(outcome.doneStatus, "proved");
  assert.equal(outcome.resultKind, "proved");

  // Every event type on the wire is one the clients know.
  for (const { type } of events) {
    assert.ok(KNOWN_EVENT_TYPES.has(type), `unknown SSE event type on the wire: ${type}`);
  }
  const done = events.at(-1);
  assert.equal(done.type, "done");
  assert.ok(KNOWN_DONE_STATUSES.has(done.data.status), `done.status outside vocabulary: ${done.data.status}`);

  // code_step events carry the SQL-owned file snapshot directly.
  const codeSteps = events.filter((e) => e.type === "code_step");
  assert.ok(codeSteps.length >= 1);
  const step = codeSteps.at(-1).data;
  assert.equal(step.content_lost, false);
  assert.match(step.code, /theorem wire_contract_check : True/);
  assert.equal(step.check_status, "ok");

  // Persisted session detail: the fields the companion reads by name.
  const detail = await fetchApiSessionDetail({ fetchImpl: fetch, baseUrl: adapterBaseUrl, apiKey: null, sessionId });
  assert.equal(detail.ok, true);
  assert.equal(detail.body.origin, "overleaf");
  const partial = events.find((e) => e.type === 'assistant_delta');
  assert.ok(partial?.data.message?.id, 'streamed partial has a persisted message identity');
  const finalMessage = detail.body.messages.find(m => m.id === partial.data.message.id);
  assert.ok(finalMessage);
  assert.equal(finalMessage.stream_state, 'final');
  assert.equal(finalMessage.live, false);
  const run = detail.body.runs.find((r) => r.id === runId);
  assert.equal(run.status, "proved");
  assert.equal(run.result_kind, "proved");
  assert.equal(run.input_tokens, 11);
  assert.equal(run.output_tokens, 7);

  // Phase 2 contract: re-attaching to a completed run replays its buffered
  // history to a terminal `done` (HTTP 200) — the 409 disambiguation dance
  // is gone, so any number of late observers are safe.
  const reattach = await fetch(`${adapterBaseUrl}/api/runs/${runId}/events`);
  assert.equal(reattach.status, 200);
  const replayText = await reattach.text();
  assert.match(replayText, /event: done/);
  assert.match(replayText, /"status": "proved"/);
  assert.match(replayText, /event: assistant_delta/);
  assert.match(replayText, /event: code_step/);
});

test("companion end-to-end: /formalize drives a run to formalized with a session link", async () => {
  const target = { targetKind: "theorem", targetLabel: "int_e2e_true", targetText: "True is true." };
  const input=await publishedTarget({overleafProjectId:'itest-doc',...target});
  const result=await postJson(`${companionBaseUrl}/formalize`,input,'first-formalize-ack');
  assert.equal(result.status, 200);
  assert.equal(result.body.status, "in_progress");
  const jobId = result.body.jobId;
  assert.ok(jobId);

  const info = await pollStatus("itest-doc", target, ["formalized", "failed"]);
  assert.equal(info.status, "formalized",JSON.stringify({info,jobs:await readCompanionJobs()}));
  assert.equal(info.resultKind, "proved");

  const job=await waitFor(async()=>{const jobs=await readCompanionJobs();return jobs[jobId]?.finishedAt?jobs[jobId]:null;},{timeoutMs:90000,label:'business result settlement'});
  assert.equal(job.status, "formalized",JSON.stringify(job));
  assert.ok(job.leaSessionId, "job records the adapter session id");
  assert.equal(job.costUsd, 0.002);

  // The session the extension deep-links to really exists adapter-side, is
  // Overleaf-tagged, and carries the document's project slug.
  const detail = await fetchApiSessionDetail({
    fetchImpl: fetch, baseUrl: adapterBaseUrl, apiKey: null, sessionId: job.leaSessionId
  });
  assert.equal(detail.ok, true);
  assert.equal(detail.body.origin, "overleaf");

  // 4.1 + 4.2 together, over the real wire: the adapter's run finalizer
  // recorded a structured artifact row, and the companion identified the
  // artifact from that index (not from registry-markdown diffing — the stub
  // prover never writes markdown).
  const artifacts = await fetch(`${adapterBaseUrl}/api/projects/by-slug/itest-doc/artifacts`)
    .then((res) => res.json());
  const row = (artifacts.artifacts || []).find((a) => a.declaration_name === "int_e2e_true");
  assert.ok(row, "the adapter recorded an artifact row for the run's declaration");
  assert.equal(row.kind, "proof");
  assert.ok(job.recordedProofPath?.endsWith(`/${row.path}`),
    "companion's recorded path resolves the adapter's repo-relative path");
  assert.equal(job.formalizationId,row.formalization_id,'The compatibility job is bound to the canonical formalization identity');
  assert.equal(job.domain_run_status,'proved','The operational result comes from the SQL run');
  const replay=await postJson(companionBaseUrl+'/formalize',input,'first-formalize-ack');
  assert.equal(replay.status,200,JSON.stringify(replay.body));assert.equal(replay.body.jobId,jobId);
  assert.equal(Object.values(await readCompanionJobs()).filter(job=>job.jobKey==='itest-doc:theorem:int_e2e_true').length,1);

  assert.match(job.logTail,/Formalize the Overleaf theorem/);
});

test("companion push channel: /events streams jobs-changed during a formalize (PLAN 3.1)", async () => {
  const response = await fetch(`${companionBaseUrl}/events?projectId=itest-doc`);
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") || "", /text\/event-stream/);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const readUntil = async (needle, timeoutMs = 30000) => {
    const deadline = Date.now() + timeoutMs;
    while (!buffer.includes(needle)) {
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${needle}`);
      const { value, done } = await reader.read();
      if (done) throw new Error(`stream ended before ${needle} arrived`);
      buffer += decoder.decode(value);
    }
  };

  try {
    await readUntil("event: hello");
    const target = { targetKind: "theorem", targetLabel: "int_push_true", targetText: "True is true." };
    const result = await postJson(`${companionBaseUrl}/formalize`, {
      overleafProjectId: "itest-doc",
      ...target
    });
    assert.equal(result.status, 200);
    await readUntil("event: jobs-changed");
    // And the run still completes normally underneath.
    const info = await pollStatus("itest-doc", target, ["formalized", "failed"]);
    assert.equal(info.status, "formalized");
  } finally {
    await reader.cancel().catch(() => {});
  }
});

test("retry lifecycle: a failed changed-source attempt retains the previous proof as stale history", async () => {
  const target = { targetKind: "theorem", targetLabel: "int_retry_true", targetText: "True is true." };
  const first = await postJson(`${companionBaseUrl}/formalize`, { overleafProjectId: "itest-doc", ...target });
  assert.equal(first.status, 200);
  const firstInfo = await pollStatus("itest-doc", target, ["formalized", "failed"]);
  assert.equal(firstInfo.status, "formalized");

  const jobsAfterFirst = await readCompanionJobs();
  const firstJob = jobsAfterFirst[first.body.jobId];
  assert.ok(firstJob.recordedProofPath, "first run records its proof file");
  const absoluteProof = path.join(home, "prover", firstJob.recordedProofPath);
  const original = await fs.readFile(absoluteProof, "utf8");
  assert.match(original, /theorem int_retry_true/);

  // New attempts keep working files. Failure preserves earlier proof history,
  // while changed source makes that proof stale for the current target.
  const retry = await postJson(`${companionBaseUrl}/formalize`, {
    overleafProjectId: "itest-doc",
    ...target,
    targetText: "True is true. [stub:fail]"
  });
  assert.equal(retry.status, 200);
  await waitFor(async () => {
    const jobs = await readCompanionJobs();
    return jobs[retry.body.jobId]?.status === "failed";
  }, { timeoutMs: 45000, label: "retry to reach failed" });

  const restoredContent = await fs.readFile(absoluteProof, "utf8");
  assert.equal(restoredContent, original, "the previous proof is retained on disk");
  const detail = await fetchApiSessionDetail({ fetchImpl: fetch, baseUrl: adapterBaseUrl, sessionId: firstJob.leaSessionId });
  assert.equal(detail.ok, true);
  assert.ok(detail.body.code_steps.some((step) => step.code === original), "SQL retains proof history");

  const retryJob = (await readCompanionJobs())[retry.body.jobId];
  assert.equal((retryJob.retryCleanup?.backups?.retiredFiles || []).length, 0,
    "new attempts do not retire the prior proof");
  assert.equal((retryJob.retryCleanup?.backups?.proofFiles || []).length, 0,
    "no proof bytes stashed on the job record");
});

test("companion end-to-end: a failing run surfaces as failed, not as a hang", async () => {
  const target = {
    targetKind: "theorem",
    targetLabel: "int_e2e_fails",
    targetText: "True is true. [stub:fail]"
  };
  const result = await postJson(`${companionBaseUrl}/formalize`, {
    overleafProjectId: "itest-doc",
    ...target
  });
  assert.equal(result.status, 200);

  const info = await pollStatus("itest-doc", target, ["failed"]);
  assert.equal(info.status, "failed");
});

test("the ledger engine keeps a changed-source failed retry stale", async () => {
  const targets = [
    { targetKind: "theorem", targetLabel: "int_e2e_true", targetText: "True is true." },
    { targetKind: "theorem", targetLabel: "int_push_true", targetText: "True is true." },
    { targetKind: "theorem", targetLabel: "int_e2e_fails", targetText: "True is true. [stub:fail]" },
    { targetKind: "theorem", targetLabel: "int_retry_true", targetText: "True is true." }
  ];
  await pollStatus('itest-doc',targets[0],['formalized']);
  await pollStatus('itest-doc',targets[1],['formalized']);
  const res = await postJson(`${companionBaseUrl}/statuses`, { overleafProjectId: "itest-doc", targets });
  assert.equal(res.status, 200);
  const statuses = Object.fromEntries(Object.entries(res.body.statuses).map(([key, info]) => [key, info.status]));
  assert.equal(statuses["theorem:int_e2e_true"], "formalized");
  assert.equal(statuses["theorem:int_push_true"], "formalized");
  assert.equal(statuses["theorem:int_e2e_fails"], "failed");
  assert.equal(statuses["theorem:int_retry_true"], "stale");
});

test("a checked target with an explicit review result stays needs_review", async () => {
  const target = { targetKind: "theorem", targetLabel: "int_review", targetText: "True is true. [stub:review]" };
  const response = await postJson(`${companionBaseUrl}/formalize`, { overleafProjectId: "itest-doc", ...target });
  assert.equal(response.status, 200);
  const info = await pollStatus("itest-doc", target, ["needs_review", "failed"]);
  assert.equal(info.status, "needs_review");
});


test("Overleaf manual edits preserve the adapter's pinned proof history", async () => {
  assert.ok(!process.env.LEA_ITEST_SKIP_LEAN,'The staged repair qualification requires the installed Lean toolchain.');
  const target = {targetKind: "theorem", targetLabel: "int_manual_edit", targetText: "True is true."};
  const identity = {overleafProjectId: "itest-manual-edit", ...target};
  const started = await postJson(`${companionBaseUrl}/formalize`, identity);
  assert.equal(started.status, 200);
  assert.equal((await pollStatus(identity.overleafProjectId, target, ["formalized", "failed"])).status, "formalized");
  const opened = await postJson(`${companionBaseUrl}/lean-pane/edit/start`, identity);
  assert.equal(opened.status, 200);
  const original = opened.body.content;
  const broken = original.replace("trivial", "bad_tactic");
  assert.notEqual(broken, original);
  const saved = await postJson(`${companionBaseUrl}/lean-pane/edit/save`, {...identity, content: broken});
  assert.equal(saved.status, 200, JSON.stringify(saved.body));
  const detail = await fetchApiSessionDetail({fetchImpl: fetch, baseUrl: adapterBaseUrl, sessionId: opened.body.leaSessionId});
  assert.equal(detail.body.code_steps.at(-1).code, broken);
  assert.equal(detail.body.code_steps.at(-1).check_status, "error");
  const repair=await postJson(companionBaseUrl+'/lean-pane/repair/start',await publishedTarget(identity));
  assert.equal(repair.status,200,JSON.stringify(repair.body));assert.ok(repair.body.jobId);
  const repaired=await waitFor(async()=>{const jobs=await readCompanionJobs();const job=jobs[repair.body.jobId];return job?.finishedAt?job:null;},{timeoutMs:180000,label:'staged repair'});
  assert.equal(repaired.lastRepair?.state,'repaired',JSON.stringify(repaired));
  const operation=await fetch(adapterBaseUrl+'/api/reformalizations/'+repaired.reformalizationId).then(r=>r.json());
  assert.equal(operation.state,'settled',JSON.stringify(operation));
  const afterRepair=(await fetchApiSessionDetail({fetchImpl:fetch,baseUrl:adapterBaseUrl,sessionId:opened.body.leaSessionId})).body;
  assert.equal(afterRepair.code_steps.at(-1).code,original,'Preservation-checked body repair restores the original claim');
  assert.ok(afterRepair.code_steps.some(step=>step.code===broken),'The manually broken revision remains in canonical history');
  const restored = await postJson(`${companionBaseUrl}/lean-pane/edit/save`, {...identity, content: original});
  assert.equal(restored.status, 200, JSON.stringify(restored.body));
  const history = await fetchApiSessionDetail({fetchImpl: fetch, baseUrl: adapterBaseUrl, sessionId: opened.body.leaSessionId});
  assert.equal(history.body.code_steps.at(-1).code, original);
  assert.equal(history.body.code_steps.at(-1).check_status, "ok");
  assert.ok(history.body.code_steps.some(step => step.code === broken && step.check_status === "error"));
});

test('completed run replay survives an actual adapter process restart', async () => {
  assert.ok(completedRunId);
  const before=await waitFor(async()=>{
    const text=await (await fetch(`${adapterBaseUrl}/api/runs/${completedRunId}/events`)).text();
    return /id: [^\n]+\nevent: done\n/.test(text)?text:null;
  },{label:'committed terminal replay before restart'});
  const stopped = new Promise(resolve => adapter.once('exit', resolve));
  adapter.kill('SIGTERM');
  await stopped;
  adapter = spawn(PYTHON, [ADAPTER_ENTRY], {
    env: { ...process.env, LEA_ITEST_HOME: home, LEA_ITEST_PORT: new URL(adapterBaseUrl).port },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  // Drain fixture logs so a full pipe cannot stall the replacement process.
  let output = '';
  adapter.stdout.on('data', chunk => { output += chunk; });
  adapter.stderr.on('data', chunk => { output += chunk; if(process.env.LEA_ITEST_DEBUG)process.stderr.write(chunk); });
  await waitFor(async () => {
    assert.equal(adapter.exitCode, null, output);
    try { return (await fetch(`${adapterBaseUrl}/api/ready`)).ok; } catch { return false; }
  }, {timeoutMs: 60000, label: 'adapter restart'});
  const after = await (await fetch(`${adapterBaseUrl}/api/runs/${completedRunId}/events`)).text();
  assert.equal(after, before, 'restart preserves exact committed event IDs and payloads');
});

test('expired replay recovers canonical state and the real companion settles without executing again',async()=>{
  assert.ok(completedRunId);
  const run = await (await fetch(`${adapterBaseUrl}/api/runs/${completedRunId}`)).json();
  const snapshotUrl=`${adapterBaseUrl}/api/snapshots/sessions/${run.session_id}`;
  const before=await (await fetch(snapshotUrl)).json();
  const script=`import sys\nfrom pathlib import Path\nfrom datetime import datetime,timedelta,timezone\nfrom app import db,observation\ndb.DB_PATH=Path(sys.argv[1])\nold=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()\nwith db.write() as c:\n c.execute('update run_events set created_at=? where run_id=?',(old,sys.argv[2]))\n c.execute('update runs set updated_at=? where id=?',(old,sys.argv[2]))\nprint(observation.compact_run_events())\n`;
  const process=spawn(PYTHON,['-c',script,path.join(home,'data','lea-interface.sqlite3'),completedRunId],{
    cwd:path.join(REPO_ROOT,'apps','lea-standalone','adapter'),stdio:['ignore','pipe','pipe']});
  let output='';process.stdout.on('data',x=>output+=x);process.stderr.on('data',x=>output+=x);
  assert.equal(await new Promise(resolve=>process.once('exit',resolve)),0,output);
  const after=await (await fetch(snapshotUrl)).json();
  assert.deepEqual(after.timeline,before.timeline,'canonical text/proof history survives compaction');
  assert.equal(after.runs.length,before.runs.length);
  const seen=[];
  const outcome=await streamApiRun({fetchImpl:fetch,baseUrl:adapterBaseUrl,runId:completedRunId,
    onEvent:(type,data)=>seen.push({type,data})});
  assert.ok(outcome.ok,JSON.stringify(outcome));
  assert.ok(seen.some(x=>x.type==='resync' && x.data.reason==='events_expired'));
  assert.equal(seen.at(-1).type,'done');
  assert.equal((await (await fetch(snapshotUrl)).json()).runs.length,before.runs.length);
});

test('session-list snapshot cursor closes the HTTP subscription race',async()=>{
  const snapshot=await (await fetch(`${adapterBaseUrl}/api/sessions`)).json();
  assert.ok(Number.isSafeInteger(snapshot.cursor));
  const session=snapshot.sessions[0];assert.ok(session);
  const changed=await fetch(`${adapterBaseUrl}/api/sessions/${session.id}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'After snapshot'})});
  assert.ok(changed.ok);
  const response=await fetch(`${adapterBaseUrl}/api/sessions/events?after=${snapshot.cursor}`,{
    signal:AbortSignal.timeout(5000)});
  const reader=response.body.getReader();
  try {
    const chunk=new TextDecoder().decode((await reader.read()).value);
    assert.match(chunk,/event: sessions_changed/);
    assert.ok(Number(chunk.match(/id: (\d+)/)[1])>snapshot.cursor);
  } finally {await reader.cancel();}
  const current=await (await fetch(`${adapterBaseUrl}/api/sessions`)).json();
  assert.equal(current.sessions.find(x=>x.id===session.id).title,'After snapshot');
});

test('enrolled core HTTP queries and authenticated synthetic commands survive the real process boundary',async()=>{
  const run=await (await fetch(`${adapterBaseUrl}/api/runs/${completedRunId}`)).json();
  const project=await (await fetch(`${adapterBaseUrl}/api/projects/${run.project_id}`)).json();
  const script=`import sys,json\nfrom pathlib import Path\nfrom app import db,config\nhome=Path(sys.argv[1])\ndb.DB_PATH=home/'data/lea-interface.sqlite3'\ndb.ROOT=home\nconfig.ROOT=home\nfrom app import core_observer,commands\nscope='p:'+sys.argv[2]\ncore_observer.enroll(scope)\nfor _ in range(100):\n result=core_observer.capture(scope)\n if result['state']=='observed':break\nelse:raise RuntimeError('backfill did not settle')\nprint(json.dumps({'credential':commands.register_producer('http-fixture',project_id=sys.argv[2])}))\n`;
  const child=spawn(PYTHON,['-c',script,home,run.project_id],{cwd:path.join(REPO_ROOT,'apps/lea-standalone/adapter'),stdio:['ignore','pipe','pipe']});
  let output='',errors='';child.stdout.on('data',x=>output+=x);child.stderr.on('data',x=>errors+=x);
  assert.equal(await new Promise(resolve=>child.once('exit',resolve)),0,errors);
  const {credential}=JSON.parse(output.trim());
  const detail=await (await fetch(`${adapterBaseUrl}/api/sessions/${run.session_id}`)).json();
  assert.equal(detail.projection.state,'observed');assert.ok(detail.code_steps.length);
  assert.ok(Number.isSafeInteger(detail.cursor));
  const headers={'Authorization':`Bearer ${credential}`,'Content-Type':'application/json'};
  const body={kind:'synthetic.echo',idempotency_key:'http-once',session_id:run.session_id,payload:{text:'database receipt'}};
  const submit=await fetch(`${adapterBaseUrl}/api/commands`,{method:'POST',headers,body:JSON.stringify(body)});
  assert.equal(submit.status,202);const receipt=await submit.json();
  const repeated=await (await fetch(`${adapterBaseUrl}/api/commands`,{method:'POST',headers,body:JSON.stringify(body)})).json();
  assert.equal(repeated.id,receipt.id);
  const settled=await waitFor(async()=>{
    const row=await (await fetch(`${adapterBaseUrl}/api/commands/${receipt.id}`,{headers})).json();
    return row.state==='succeeded'?row:false;
  },{label:'synthetic command receipt'});
  assert.equal(settled.result.text,'database receipt');
  const final=await (await fetch(`${adapterBaseUrl}/api/sessions/${run.session_id}`)).json();
  assert.equal(final.runs.length,detail.runs.length,'synthetic work creates no solver run');
});

test('durable solver commands progress without observers and settle duplicate approval or stop intent',async()=>{
  async function parked(key){
    const start=await postJson(`${adapterBaseUrl}/api/runs`,{message:'[stub:approvals] Prove a fixture',idempotency_key:key});
    assert.equal(start.status,200);assert.equal(start.body.command.kind,'run.execute');
    const replay=await postJson(`${adapterBaseUrl}/api/runs`,{message:'[stub:approvals] Prove a fixture',idempotency_key:key});
    assert.equal(replay.body.run_id,start.body.run_id);assert.equal(replay.body.command.id,start.body.command.id);
    // No SSE observer is attached, in either client.
    const gate=await waitFor(async()=>{
      const detail=await (await fetch(`${adapterBaseUrl}/api/sessions/${start.body.session_id}`)).json();
      return detail.active_run?.pending_approval;
    },{label:'persisted approval with every observer detached'});
    return {...start.body,gate};
  }
  const first=await parked('stage4-approve');
  const url=`${adapterBaseUrl}/api/runs/${first.run_id}/approvals/${first.gate.approval_id}`;
  const a=await postJson(url,{decision:'allow'}),b=await postJson(url,{decision:'allow'});
  assert.equal(a.status,200);assert.equal(a.body.status,'accepted');assert.equal(a.body.command.id,b.body.command.id);
  assert.equal((await postJson(url,{decision:'deny'})).status,409);
  await waitFor(async()=>{
    const row=await (await fetch(`${adapterBaseUrl}/api/runs/${first.run_id}`)).json();
    return row.command.state==='succeeded' && row.status!=='running';
  },{label:'unobserved approved solver settlement'});
  const second=await parked('stage4-cancel');
  const stopped=await postJson(`${adapterBaseUrl}/api/runs/${second.run_id}/interrupt`,{});
  assert.equal(stopped.body.status,'interrupt_requested');assert.equal(stopped.body.worker_observed,false);
  assert.equal((await postJson(`${adapterBaseUrl}/api/runs/${second.run_id}/approvals/${second.gate.approval_id}`,{decision:'allow'})).status,409);
  const final=await waitFor(async()=>{
    const row=await (await fetch(`${adapterBaseUrl}/api/runs/${second.run_id}`)).json();
    return row.status==='cancelled' && row.command.state==='succeeded'?row:false;
  },{label:'durable stop observed by worker'});
  assert.equal(final.stop_requested_reason,'user_stop');
  assert.ok(final.control_commands.some(c=>c.kind==='run.stop' && c.state==='succeeded'));
});


test('Stage 6 source publication, chat, batch, namespace rename and SQL export through stateless transport',async()=>{
  const target=await publishedTarget({overleafProjectId:'itest-stage6',targetKind:'theorem',targetLabel:'int_stage6',targetText:'True is true.'},{complete:true});
  const secondTab=await postJson(adapterBaseUrl+'/api/overleaf/publishers',{document_id:target.overleafProjectId,client_id:'competing-tab'});
  assert.equal(secondTab.status,409,'A second tab cannot replace the current publication authority');
  const first=await postJson(companionBaseUrl+'/formalize',target);assert.equal(first.status,200,JSON.stringify(first.body));
  assert.equal((await pollStatus(target.overleafProjectId,target,['formalized','failed'])).status,'formalized');
  const initial=(await readCompanionJobs())[first.body.jobId];
  const chatTarget={...target,naturalLanguageLatex:target.targetText};
  const chat=await postJson(companionBaseUrl+'/lean-pane/chat/message',{target:chatTarget,message:'Explain the proof labeled int_stage6.'});
  assert.equal(chat.status,200,JSON.stringify(chat.body));assert.equal(chat.body.leaSessionId,initial.leaSessionId);
  await waitFor(async()=>{const run=await fetch(adapterBaseUrl+'/api/runs/'+chat.body.runId).then(r=>r.json());return ['proved','needs_review','failed'].includes(run.status)?run:null;},{label:'chat result'});
  const transcript=await fetch(companionBaseUrl+'/lean-pane/chat/session/'+initial.leaSessionId).then(r=>r.json());
  assert.ok(transcript.messages.some(m=>m.content.includes('Explain the proof labeled')));
  const batchItem=await publishedTarget({overleafProjectId:'itest-stage6-batch',targetKind:'theorem',targetLabel:'int_batch6',targetText:'True is true.'});
  const batch=await postJson(companionBaseUrl+'/formalize/all',{overleafProjectId:batchItem.overleafProjectId,items:[batchItem]});
  assert.equal(batch.status,200,JSON.stringify(batch.body));assert.ok(batch.body.batchId);
  await waitFor(async()=>{const result=await postJson(companionBaseUrl+'/lean-pane/repair/status',{batchId:batch.body.batchId});return result.body.done?result.body:null;},{timeoutMs:60000,label:'database batch settlement'});
  assert.equal((await pollStatus(batchItem.overleafProjectId,batchItem,['formalized','failed'])).status,'formalized');
  const identity=await fetch(companionBaseUrl+'/project/identity?overleafProjectId='+target.overleafProjectId).then(r=>r.json());
  const renamedResponse=await fetch(companionBaseUrl+'/project/identity',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({overleafProjectId:target.overleafProjectId,projectName:'Stage Six Renamed',mode:'rename-namespace',namespace:'Lea.StageSixRenamed',expectedNamespace:identity.namespace})});
  const renamed=await renamedResponse.json();assert.equal(renamedResponse.status,200,JSON.stringify(renamed));
  const current=await fetch(companionBaseUrl+'/project/identity?overleafProjectId='+target.overleafProjectId).then(r=>r.json());
  assert.equal(current.namespace,'Lea.StageSixRenamed');assert.ok(current.namespaceRevision>identity.namespaceRevision);
  const download=await fetch(companionBaseUrl+'/project-export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({overleafProjectId:target.overleafProjectId})});
  assert.equal(download.status,200,await download.clone().text());assert.match(download.headers.get('content-type'),/zip/);
  assert.ok((await download.arrayBuffer()).byteLength>100);
  assert.equal(await fs.access(companionJobsPath).then(()=>true,()=>false),false,'The optional companion never creates a JSON job ledger');
});
