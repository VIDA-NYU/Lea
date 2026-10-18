import { workspaceResult, submitWithRecovery } from '../../../packages/lea-workspace-client/index.mjs';
// Lea standalone adapter client for the Overleaf companion.
//
// The adapter (apps/lea-standalone/adapter, :8001) drives the prover in-process.
// A run is started with POST /api/runs, then driven and observed by opening its
// SSE event stream. Per-run config lives in config/lea.local.toml; the companion
// passes the prompt, provenance, and optional session/project metadata.

const TOOL_APPROVAL_DECISION = "always_session";

// done.status values the adapter emits (see bridge._FINISH_STATUS + run_lea):
//   proved | disproved | needs_review | answered | max_turns | cancelled | failed
// For a *formalization* job, "proved" and "disproved" are completed checked work;
// "answered" finished cleanly but proved nothing. The old "success" alias (kept
// for pre-vocabulary test doubles) was removed once the integration harness
// (tests/integration/) began asserting the real wire vocabulary.
const SUCCESS_DONE_STATUS = new Set(["proved", "disproved"]);

function toNonNegativeNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function decodeChunk(chunk) {
  if (typeof chunk === "string") return chunk;
  if (chunk instanceof Uint8Array) return new TextDecoder().decode(chunk);
  return String(chunk);
}

async function* iterateResponseBody(body) {
  if (!body) return;
  if (body[Symbol.asyncIterator]) {
    yield* body;
    return;
  }
  if (!body.getReader) return;
  const reader = body.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      yield value;
    }
  } finally {
    reader.releaseLock?.();
  }
}

// Parse one SSE frame ("event: <type>\ndata: <json>") into { type, data }.
// The adapter encodes the event name on the `event:` line and the payload on
// `data:` lines.
export function parseSseFrame(frame) {
  let type = "message";
  const dataLines = [];
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith("event:")) {
      type = rawLine.slice("event:".length).trim();
    } else if (rawLine.startsWith("data:")) {
      dataLines.push(rawLine.slice("data:".length).replace(/^ /, ""));
    }
  }
  const dataText = dataLines.join("\n").trim();
  if (!dataText) return { type, data: null };
  try {
    return { type, data: JSON.parse(dataText) };
  } catch {
    return { type, data: null };
  }
}

function buildHeaders(apiKey, extra = {}) {
  const headers = { ...extra };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  return headers;
}

const ADAPTER_METADATA_TIMEOUT_MS = 5000;

async function fetchJson(fetchImpl, url, options, { timeoutMs } = {}) {
  if (!(timeoutMs > 0)) return readJson(fetchImpl, url, options);
  const controller = new AbortController();
  let timer;
  try {
    return await Promise.race([
      readJson(fetchImpl, url, { ...options, signal: controller.signal }),
      new Promise((resolve) => {
        timer = setTimeout(() => {
          resolve({ ok: false, timedOut: true, error: `Request to ${url} timed out.` });
          controller.abort();
        }, timeoutMs);
      })
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function readJson(fetchImpl, url, options) {
  let response;
  try {
    response = await fetchImpl(url, options);
  } catch (error) {
    return { ok: false, error: `Request to ${url} failed: ${error instanceof Error ? error.message : String(error)}` };
  }
  let body = null;
  const text = await response.text().catch(() => "");
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const rawDetail = body?.detail ?? body?.error ?? text ?? `HTTP ${response.status}`;
    const detail =
      rawDetail && typeof rawDetail === "object"
        ? rawDetail.message || JSON.stringify(rawDetail)
        : rawDetail;
    return { ok: false, status: response.status, error: `HTTP ${response.status}: ${detail}`, body };
  }
  try {
    if (!url.includes('/api/workspace/commands/')) body = await workspaceResult(body, async id => {
      const receiptUrl = `${String(url).split('/api/')[0]}/api/workspace/commands/${encodeURIComponent(id)}`;
      const result = await readJson(fetchImpl, receiptUrl, {method:'GET', headers: options?.headers});
      if (!result.ok) throw Object.assign(new Error(result.error), {status:result.status});
      return result.body;
    });
  } catch (error) {
    return {ok:false, status:error.status || 409, error:error.message, body:{detail:error.detail, command_id:error.commandId}};
  }
  return { ok: true, status: response.status, body };
}

// --- Shared settings delegation -------------------------------------------
// The adapter's `config/lea.local.toml` (served by GET/PUT /api/settings) is the
// single source of truth for the settings shared with the lea-standalone UI:
// `model`, `max_turns`, `max_spend_usd`, and provider `api_keys`. The companion
// reads them here and writes them via `putAdapterSettings`, instead of keeping
// its own divergent copies, so a change in either UI shows up in both.

export function fetchAdapterHealth({ fetchImpl, baseUrl, apiKey, timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/health`, { method: "GET", headers: buildHeaders(apiKey) }, { timeoutMs });
}

export function fetchAdapterSettings({ fetchImpl, baseUrl, timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/settings`, {
    method: "GET",
    headers: buildHeaders(null),
  }, { timeoutMs });
}

export function fetchAdapterModelCatalog({ fetchImpl, baseUrl, refresh = false, timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/models${refresh ? "/refresh" : ""}`, {
    method: refresh ? "POST" : "GET",
    headers: buildHeaders(null),
  }, { timeoutMs });
}

export function fetchAdapterModelRequirements({ fetchImpl, baseUrl, model, timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(
    fetchImpl,
    `${baseUrl}/api/models/requirements?model=${encodeURIComponent(String(model || ""))}`,
    {
      method: "GET",
      headers: buildHeaders(null),
    },
    { timeoutMs }
  );
}

export function putAdapterSettings({ fetchImpl, baseUrl, body }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/settings`, {
    method: "PUT",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify(body || {}),
  });
}

// The adapter's GET /api/stats (store.usage_stats) is the single source of truth
// for usage the lea-standalone Stats page renders: `global` all-time rollups and
// per-`sessions` accounting rows (each carrying `project_slug`). The companion reads it here
// so the Overleaf popover shows the exact same numbers instead of its own
// in-memory job tally.
export function fetchAdapterUsageStats({ fetchImpl, baseUrl, timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/stats?include_proof_status=false`, {
    method: "GET",
    headers: buildHeaders(null),
  }, { timeoutMs });
}

// Mirror the Overleaf project's .tex/.sty/.cls sources into the matching adapter project's
// `.lea/files/overleaf/` (resolved by slug, get-or-create — same slug the run uses).
// The adapter reconciles synchronously and defers the git commit, so this returns
// quickly; `files` is `[{ path, content }]`. Best-effort: a transport
// failure surfaces as `{ ok:false }` and the caller logs/ignores it.
export function mirrorProjectTexFiles({ fetchImpl, baseUrl, slug, files, mode = "reconcile", overleafProjectTitle = null }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/mirror`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    // mode "upsert" (PLAN 3.2) writes only the provided files — the active-
    // buffer tier; "reconcile" treats the payload as the full truth set.
    body: JSON.stringify({ source: "overleaf", mode, files: files || [],
      ...(overleafProjectTitle != null ? { overleaf_project_title: overleafProjectTitle } : {}) }),
  });
}

// --- Export & GitHub sharing (D34, by-slug) ---------------------------------
// The adapter's by-slug routes mirror the Lea UI's by-id export/share surface but
// are keyed by the same document slug the mirror/run paths use. None of them ever
// create a project — an unknown slug is a 404 ("nothing to export yet").

// Structured artifact index (PLAN-system-hardening 4.1/4.2): which declaration
// lives in which file, as recorded by the adapter's run finalizer. The primary
// artifact-identification source; registry-markdown diffing is the fallback.
export function fetchProjectArtifactsBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/artifacts`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

// Blueprint dependency graph with live-derived status (FEATURE-overleaf-blueprint-view):
// the parsed `.lea/blueprint.md` nodes + `uses` edges, each node enriched with its
// status/verified/session attribution. The read-only viewer in the Lean pane reads this.
export function fetchProjectGraphBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/graph`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

// Populate the blueprint from the project's formalized artifacts (the "Generate from
// formalized theorems" button). Additive + idempotent on the adapter side. Returns
// { added, skipped, warnings, graph }.
export function generateProjectBlueprintBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/blueprint/generate`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: "{}",
  });
}

// Ledger-side target evidence (PLAN-system-hardening 4.4): per-declaration
// file existence / sorry scan / newest check verdict, straight from the
// adapter's own records. One of the two sources the ledger status engine
// merges (the other is the companion's job overlay).
export function fetchProjectTargetStatusBySlug({ fetchImpl, baseUrl, slug, declarations, formalizationIds = [], timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  const query = encodeURIComponent((declarations || []).join(","));
  const ids = encodeURIComponent(formalizationIds.join(","));
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/target-status?declarations=${query}&formalization_ids=${ids}`, {
    method: "GET",
    headers: buildHeaders(null),
  }, { timeoutMs });
}

// Single-writer retirement (PLAN-system-hardening 4.5): a retry asks the adapter
// to delete the previous proof and record that change. If the retry does not
// verify, the adapter restores its SQL-owned verified snapshot (falling back to
// project Git history for legacy/manual files).
export function retireProjectArtifactBySlug({ fetchImpl, baseUrl, slug, path }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/artifacts/retire`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ path }),
  });
}

export function restoreProjectArtifactBySlug({ fetchImpl, baseUrl, slug, path, retireCommit }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/artifacts/restore`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ path, retire_commit: retireCommit }),
  });
}

export function fetchProjectShareStatus({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/share`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

export function fetchProjectIdentityBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/identity`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

export function initializeOverleafDefaultName({ fetchImpl, baseUrl, slug, overleafProjectTitle }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/identity/default-name`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ overleaf_project_title: overleafProjectTitle }),
  });
}

export function previewProjectNamespace({ fetchImpl, baseUrl, projectName, namespace = null, excludeProjectId = null }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/namespace-preview`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_name: projectName,
      namespace,
      exclude_project_id: excludeProjectId
    }),
  });
}

export function updateProjectIdentityBySlug({
  fetchImpl,
  baseUrl,
  slug,
  projectName,
  mode,
  namespace = null,
  expectedNamespace = null,
  createIfMissing = false,
  overleafProjectTitle = null,
}) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/identity`, {
    method: "PUT",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_name: projectName,
      mode,
      namespace,
      expected_namespace: expectedNamespace,
      create_if_missing: Boolean(createIfMissing),
      ...(overleafProjectTitle != null ? { overleaf_project_title: overleafProjectTitle } : {})
    }),
  });
}

export function setProjectRemoteBySlug({ fetchImpl, baseUrl, slug, remoteUrl }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/git/remote`, {
    method: "PUT",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ remote_url: remoteUrl }),
  });
}

export function pushProjectBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/git/push`, {
    method: "POST",
    headers: buildHeaders(null),
  });
}

// Additive GitHub project import (inverse of Share): analyze, confirm, then poll.
export function previewGithubImportBySlug({
  fetchImpl,
  baseUrl,
  slug,
  repositoryUrl,
  targets = [],
  projectName = null,
  namespace = null,
}) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/github-imports/preview`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({
      repository_url: repositoryUrl,
      targets,
      project_name: projectName,
      namespace,
    }),
  });
}

export function confirmGithubImportBySlug({ fetchImpl, baseUrl, slug, previewId }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/github-imports`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ preview_id: previewId }),
  });
}

export function getGithubImportBySlug({ fetchImpl, baseUrl, slug, importId }) {
  return fetchJson(
    fetchImpl,
    `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/github-imports/${encodeURIComponent(importId)}`,
    { method: "GET", headers: buildHeaders(null) }
  );
}

export function syncProjectFormalizationTargetsBySlug({ fetchImpl, baseUrl, slug, targets = [], timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/formalizations/sync`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ targets }),
  }, { timeoutMs });
}

export function fetchCurrentAlignmentCheck({ fetchImpl, baseUrl, formalizationId, sourceBundleHash = "", timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  const query = sourceBundleHash ? `?source_bundle_hash=${encodeURIComponent(sourceBundleHash)}` : "";
  return fetchJson(
    fetchImpl,
    `${baseUrl}/api/formalizations/${encodeURIComponent(formalizationId)}/alignment-checks/current${query}`,
    { method: "GET", headers: buildHeaders(null) }, { timeoutMs }
  );
}

export function filenameFromContentDisposition(header) {
  const match = /filename="([^"]+)"/.exec(String(header || ""));
  return match ? match[1] : null;
}

// Binary download — the one client call that can't go through fetchJson. Returns
// `{ ok, status, bytes: Uint8Array, filename, contentType }` on success and the
// familiar `{ ok:false, status?, error }` shape on failure (the error body is the
// adapter's JSON `detail` when present).
export async function exportProjectZipBySlug({ fetchImpl, baseUrl, slug }) {
  const url = `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/export`;
  let response;
  try {
    response = await fetchImpl(url, { method: "GET", headers: buildHeaders(null) });
  } catch (error) {
    return { ok: false, error: `Request to ${url} failed: ${error instanceof Error ? error.message : String(error)}` };
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    const text = await response.text().catch(() => "");
    if (text) {
      try {
        detail = JSON.parse(text)?.detail || detail;
      } catch {
        /* non-JSON error body — keep the status line */
      }
    }
    return { ok: false, status: response.status, error: detail };
  }
  const buf = await response.arrayBuffer();
  return {
    ok: true,
    status: response.status,
    bytes: new Uint8Array(buf),
    filename: filenameFromContentDisposition(response.headers?.get?.("content-disposition")) || `${slug}.zip`,
    contentType: response.headers?.get?.("content-type") || "application/zip",
  };
}

export async function startApiRun({
  fetchImpl,
  baseUrl,
  apiKey,
  message,
  sessionId = null,
  autonomous = true,
  projectSlug = null,
  projectTitle = null,
  overleafProjectTitle = null,
  projectNamespace = null,
  origin = null,
  originUrl = null,
  focusFormalizationId = null,
  focusSourceHash = null,
  newFormalization = null,
  artifactOperation = null,
  purpose = "general",
  sourceBundle = null,
  sourceContextRef = null,
  allowSourcePause = false,
  idempotencyKey = null,
  timeoutSeconds = null,
}) {
  // `autonomous: true` tells the adapter to run with no per-tool approval gate and
  // the non-interactive `default` prompt variant, so the Overleaf job formalizes
  // end-to-end with zero human interaction. (The client also auto-resolves any
  // approval events below as a belt-and-suspenders, but an autonomous run emits
  // none.) Defaults true because this client is the autonomous Overleaf path.
  //
  // `projectSlug` is the Overleaf document namespace (slugProjectId). When present
  // the adapter tags the session+run with a project of that slug, so the popover's
  // "This project" usage can be summed per document. Omitted for the interactive
  // UI path, which stays project-less.
  //
  // `origin` / `originUrl` record session providence: 'overleaf' + the canonical
  // Overleaf document URL, so the Lea UI can show an origin indicator and open/focus
  // the source document. Independent of the project usage-namespace above.
  if (["overleaf_solver", "overleaf_continuation"].includes(purpose)) {
    const health = await fetchAdapterHealth({ fetchImpl, baseUrl, apiKey });
    const capability = health.body?.capabilities?.lea_status;
    if (!health.ok || capability?.version !== 1 || !capability.admission_enabled || capability.source_pause_policy !== 1) {
      return { ok: false, status: 409, error: "Update/start compatible Lea components: live Lea Status v1 and source pause policy support are required." };
    }
    if (!sourceBundle?.bundleHash) return { ok: false, status: 422, error: "Current LaTeX source context is required. Refresh the Overleaf pane before continuing." };
  }
  const body = { message, autonomous, purpose, allow_source_pause: allowSourcePause === true };
  if (artifactOperation) body.artifact_operation = artifactOperation;
  if (idempotencyKey) body.idempotency_key = idempotencyKey;
  if (Number.isFinite(timeoutSeconds) && timeoutSeconds > 0) body.timeout_seconds = Math.ceil(timeoutSeconds);
  if (sourceContextRef) body.source_context_ref = sourceContextRef;
  if (sourceBundle) { body.source_bundle = sourceBundle; body.lea_status_version = 1; }
  if (sessionId) body.session_id = sessionId;
  if (projectSlug) {
    body.project_slug = projectSlug;
    body.project_title = projectTitle || projectSlug;
    if (overleafProjectTitle != null) body.overleaf_project_title = overleafProjectTitle;
    if (projectNamespace) body.project_namespace = projectNamespace;
  }
  if (origin) body.origin = origin;
  if (originUrl) body.origin_url = originUrl;
  if (focusFormalizationId) body.focus_formalization_id = focusFormalizationId;
  if (focusSourceHash) body.focus_source_hash = focusSourceHash;
  if (newFormalization) body.new_formalization = newFormalization;
  return fetchJson(fetchImpl, `${baseUrl}/api/runs`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

export function resolveApiApproval({ fetchImpl, baseUrl, apiKey, runId, approvalId, decision = TOOL_APPROVAL_DECISION }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify({ decision }),
  });
}

export function startApiContinuation({ fetchImpl, baseUrl, apiKey, runId, sourceBundle, sourceContextRef,
  idempotencyKey, timeoutSeconds, bestEffort = false, allowSourcePause = false, dependencyResume = false }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/runs/${encodeURIComponent(runId)}/continue`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify({ idempotency_key: idempotencyKey, source_bundle: sourceContextRef?undefined:sourceBundle, source_context_ref: sourceContextRef,
      lea_status_version: 1, timeout_seconds: Math.ceil(timeoutSeconds),
      best_effort: bestEffort === true, allow_source_pause: allowSourcePause === true,
      dependency_resume: dependencyResume === true }),
  });
}

export function projectDependencyRequest({ fetchImpl, baseUrl, apiKey, projectSlug, dependencyId, action = null, body = {}, timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(projectSlug)}/dependencies${dependencyId ? `/${encodeURIComponent(dependencyId)}` : ""}${action ? `/${action}` : ""}`, {
    method: action ? "POST" : "GET", headers: buildHeaders(apiKey, action ? { "Content-Type": "application/json" } : {}),
    ...(action ? { body: JSON.stringify(body) } : {}),
  }, action ? {} : { timeoutMs });
}

export function interruptApiRun({ fetchImpl, baseUrl, apiKey, runId, reason = "user_stop", source = "user", signal }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/runs/${encodeURIComponent(runId)}/interrupt`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify({ reason, source }),
    signal,
  });
}

export function cancelReformalization({ fetchImpl, baseUrl, apiKey, operationId }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/reformalizations/${encodeURIComponent(operationId)}/cancel`, {
    method: "POST", headers: buildHeaders(apiKey),
  });
}

// A run's own persisted usage is authoritative. Missing/inaccessible rows have
// unknown accounting; a zero is confirmed only with an explicit final status.
export async function fetchApiRunUsage({ fetchImpl, baseUrl, apiKey, sessionId, runId }) {
  let run = await fetchApiRunRow({ fetchImpl, baseUrl, apiKey, sessionId, runId });
  if ((!run || !Object.hasOwn(run, "cost_usd")) && sessionId) {
    const detail = await fetchApiSessionDetail({ fetchImpl, baseUrl, apiKey, sessionId });
    run = detail.ok && Array.isArray(detail.body?.runs)
      ? detail.body.runs.find((candidate) => candidate?.id === runId) || run : run;
  }
  if (!run || !Object.hasOwn(run, "cost_usd")) {
    return { usage: null, costUsd: null, usageStatus: "unknown", usageRevision: 0 };
  }
  const inputTokens = toNonNegativeNumber(run.input_tokens ?? run.inputTokens);
  const outputTokens = toNonNegativeNumber(run.output_tokens ?? run.outputTokens);
  const costUsd = toNonNegativeNumber(run.cost_usd ?? run.costUsd);
  return {
    usage: { inputTokens, outputTokens, totalTokens: inputTokens + outputTokens },
    costUsd,
    usageDetails: run.usage_details || { cost_basis: "legacy_estimate" },
    usageStatus: run.usage_status || "unknown",
    usageRevision: Number(run.usage_revision || 0),
    usageUpdatedAt: run.usage_updated_at || null,
  };
}

export function fetchApiSessionDetail({ fetchImpl, baseUrl, apiKey, sessionId }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
    headers: buildHeaders(apiKey),
  });
}

// D9: write a manual edit to a session's working file as a first-class,
// run-less step (POST /api/sessions/{id}/file). The adapter commits it
// `author=user`, records a code_step, and returns `{unchanged:true}` for a
// no-op save. Used by the Overleaf lean pane's manual-edit surface
// (docs/FEATURE-overleaf-lean-pane-manual-edit.md) -- the same primitive the
// standalone canvas already uses, just called from a second client.
export async function writeApiSessionFile({
  fetchImpl, baseUrl, apiKey, sessionId, path, content, note,
  formalizationId = null, fileRevision = null,
}) {
  const body = { path, content, check_and_cascade: true };
  if (fileRevision) body.file_revision = fileRevision;
  body.idempotency_key = globalThis.crypto?.randomUUID?.() || `edit-${Date.now()}-${Math.random()}`;
  if (note) body.note = note;
  if (formalizationId) body.formalization_id = formalizationId;
  const send = () => fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  try {
    return await submitWithRecovery(async()=>{
      const value=await send();
      if (!value.ok && !value.status) throw new Error(value.error || 'Submission response was lost.');
      return value;
    },async()=>{
      const value=await fetchJson(fetchImpl, `${baseUrl}/api/workspace/receipts?idempotency_key=${encodeURIComponent(body.idempotency_key)}`,
        {method:'GET',headers:buildHeaders(apiKey)});
      if (!value.ok) throw Object.assign(new Error(value.error),{status:value.status});
      return value;
    });
  } catch(error) {
    return {ok:false,error:error.message,idempotencyKey:body.idempotency_key};
  }

}


// Standalone LSP-backed `lean_check` on a session's working file (D2), no
// run required. Without `author`, back-fills the verdict onto the file's
// existing latest code_step (the original behavior, used for the edited
// file's own check). With `author` (e.g. `"cascade"`), the adapter instead
// records a *new* code_step attributed to that author -- used for
// re-verifying a project dependent that the edit itself didn't touch. See
// docs/PLAN-overleaf-lean-pane-manual-edit.md Phase 1/2.
export function fetchApiLeanCheckRuntime({ fetchImpl, baseUrl, apiKey, sessionId, timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/lean-check-runtime`, {
    method: "GET", headers: buildHeaders(apiKey)
  }, { timeoutMs });
}

export function runApiSessionLeanCheck({
  fetchImpl, baseUrl, apiKey, sessionId, path, author, summary,
  formalizationId = null,
}) {
  const body = {};
  if (path) body.path = path;
  if (author) body.author = author;
  if (summary) body.summary = summary;
  if (formalizationId) body.formalization_id = formalizationId;
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/lean-check`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

// Force a real `lake build` of a session's working file's module (D2-adjacent,
// no run). `runApiSessionLeanCheck`'s LSP fast path never updates the compiled
// `.olean` another file's `import` resolves against, so before trusting a
// cascade re-check of a *dependent* file, the edited module needs this to have
// run once first. See docs/FEATURE-overleaf-lean-pane-manual-edit.md ("Cascade
// verification") and the matching adapter route's docstring
// (routes/sessions.py `rebuild_session_module`).
export function rebuildApiSessionModule({ fetchImpl, baseUrl, apiKey, sessionId, path }) {
  const body = {};
  if (path) body.path = path;
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/rebuild`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

function deriveTurnProgress(payload, defaultMaxTurns) {
  if (!payload || typeof payload !== "object") return null;
  const turn = Number(payload.turn);
  if (!Number.isFinite(turn) || turn <= 0) return null;
  return { currentTurn: turn, maxTurns: defaultMaxTurns ?? null };
}

// Observe one already-admitted run to terminal `done`; observation starts no work.
// Auto-approval follows the existing policy. Returns the terminal
// outcome (NOT usage — caller reads that back via fetchApiRunUsage).
export async function streamApiRun({
  fetchImpl,
  baseUrl,
  apiKey,
  runId,
  maxTurns = null,
  autoApprove = true,
  onEvent = null,
  onProgressUpdated = null,
  signal = null,
}) {
  let response;
  try {
    response = await fetchImpl(`${baseUrl}/api/runs/${encodeURIComponent(runId)}/events`, {
      method: "GET",
      headers: buildHeaders(apiKey),
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") return { ok: false, aborted: true, error: "Run stream aborted." };
    return { ok: false, error: `Could not open run event stream: ${error instanceof Error ? error.message : String(error)}` };
  }
  if (!response?.ok || !response.body) {
    // httpStatus lets the caller tell an HTTP rejection (real failure — the
    // Phase 2 adapter has no busy/finished 409s) apart from a dropped
    // transport (httpStatus null), which runApiProofJob retries.
    return { ok: false, httpStatus: response?.status ?? null, error: `Run event stream returned HTTP ${response?.status ?? "?"}.` };
  }

  let doneStatus = null;
  let resultKind = null;
  let resultDetail = null;
  let stopReason = null;
  let recoverable = false;
  let runError = null;
  let recoveryState = null;
  let recoveryReason = null;
  let waitingDependencies = [];
  let buffer = "";

  const handleFrame = async (frame) => {
    const { type, data } = parseSseFrame(frame);
    if (!type || (type==='done'&&['pending','running'].includes(String(data?.status||'').toLowerCase()))) return;
    if (onEvent) await onEvent(type, data);
    if (onProgressUpdated) {
      const progress = deriveTurnProgress(data, maxTurns);
      if (progress) await onProgressUpdated(progress);
    }
    if (type === "approval_requested" && autoApprove && data?.approval_id) {
      await resolveApiApproval({ fetchImpl, baseUrl, apiKey, runId, approvalId: data.approval_id });
    } else if (type === "run_error") {
      runError = data?.message || "Lea run error.";
    } else if (type === "done") {
      doneStatus = String(data?.status || "").toLowerCase();
      resultKind = String(data?.result_kind || doneStatus || "").toLowerCase() || null;
      resultDetail = typeof data?.result_detail === "string" ? data.result_detail : null;
      stopReason = typeof data?.stop_reason === "string" ? data.stop_reason : null;
      recoverable = data?.recoverable === true;
      recoveryState = data?.recovery_state || null;
      recoveryReason = data?.recovery_reason || null;
      waitingDependencies = data?.waiting_dependencies || [];
    }
  };

  try {
    for await (const chunk of iterateResponseBody(response.body)) {
      buffer += decodeChunk(chunk);
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      for (const frame of frames) {
        if (frame.trim()) await handleFrame(frame);
      }
      if (doneStatus !== null) break;
    }
    if (doneStatus === null && buffer.trim()) await handleFrame(buffer);
  } catch (error) {
    if (error?.name === "AbortError") return { ok: false, aborted: true, error: runError || "Run stream aborted." };
    return { ok: false, error: `Run event stream failed: ${error instanceof Error ? error.message : String(error)}`, doneStatus, runError };
  }

  const ok = doneStatus !== null && SUCCESS_DONE_STATUS.has(doneStatus);
  return {
    ok,
    doneStatus,
    resultKind,
    resultDetail,
    stopReason,
    recoverable,
    recoveryState,
    recoveryReason,
    waitingDependencies,
    error: ok ? null : (runError || (doneStatus ? `Lea run ended with status: ${doneStatus}` : "Lea run ended without a terminal status.")),
  };
}

// Consecutive failures to read the run row while the stream is also failing —
// past this, the adapter is genuinely unreachable and the client gives up
// (best-effort interrupting the run it may be orphaning) instead of spinning
// until the job timeout.

// De-synchronize concurrent re-attachers after a transport drop (e.g. the
// adapter restarting mid-run) so they don't hammer it in lockstep.
function retryDelayWithJitter(baseMs) {
  return baseMs + Math.floor(Math.random() * baseMs * 0.5);
}

// Resolve to true after ms, or false immediately if/when the signal aborts —
// so a queued run reacts to its timeout mid-wait instead of after the delay.
function waitBeforeRetry(ms, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve(false);
    // Deliberately NOT unref'd: this delay gates forward progress (the next
    // attach attempt), so it must keep the event loop alive to fire.
    const timer = setTimeout(() => {
      signal?.removeEventListener?.("abort", onAbort);
      resolve(true);
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    signal?.addEventListener?.("abort", onAbort, { once: true });
  });
}

// Read one run's { id, status, result_kind, result_detail } — the cheap poll the
// busy-wait / stream-drop retry loop makes every ~3s. Hits the dedicated
// GET /api/runs/{run_id} (v2.3 item 16), which reads a single run row instead of
// the full session detail (messages + code_steps + status_events + usage) the old
// path paid on every tick — a self-inflicted DB-contention source under concurrency.
// A 404 for that route on an older adapter falls back to the session-detail scan so
// a companion pointed at a pre-item-16 backend still works. Best-effort: null when
// the adapter or the row can't be reached.
export async function fetchApiRunRow({ fetchImpl, baseUrl, apiKey, sessionId, runId, signal }) {
  if (!runId) return null;
  const res = await fetchJson(fetchImpl, `${baseUrl}/api/runs/${encodeURIComponent(runId)}`, {
    method: "GET",
    headers: buildHeaders(apiKey),
    signal,
  });
  if (res.ok && res.body) return res.body;
  // 404 here is ambiguous: an unknown run id (nothing to fall back to), OR an old
  // adapter that lacks the route. Only try the legacy scan when we have a session
  // to scan; a bare unknown-run 404 stays null.
  if (res.status === 404 && sessionId) {
    const detail = await fetchApiSessionDetail({ fetchImpl, baseUrl, apiKey, sessionId });
    if (!detail.ok || !detail.body) return null;
    const runs = Array.isArray(detail.body.runs) ? detail.body.runs : [];
    return runs.find((r) => r && r.id === runId) || null;
  }
  return null;
}

// High-level: start a run and drive it to completion, returning the shape the
// companion orchestration consumes:
// { ok, timedOut, apiRunId, error, usage, costUsd }.
export async function runApiProofJob({
  fetchImpl,
  baseUrl,
  apiKey,
  message,
  sessionId = null,
  maxTurns = null,
  timeoutMs = 900000,
  deadlineAt = null,
  existingRunId = null,
  startResponse = null,
  idempotencyKey = null,
  settleGraceMs = 10000,
  busyRetryDelayMs = 3000,
  autoApprove = true,
  autonomous = true,
  projectSlug = null,
  projectTitle = null,
  overleafProjectTitle = null,
  projectNamespace = null,
  origin = null,
  originUrl = null,
  focusFormalizationId = null,
  focusSourceHash = null,
  newFormalization = null,
  artifactOperation = null,
  existingReformalizationId = null,
  onOperationQueued = null,
  purpose = "general",
  sourceBundle = null,
  sourceContextRef = null,
  allowSourcePause = false,
  appendLog = null,
  logPath = null,
  onEvent = null,
  onRunStarted = null,
  onProgressUpdated = null,
  observerSignal = null,
}) {
  const log = async (line) => {
    if (appendLog && logPath) await appendLog(logPath, line);
  };

  let start = startResponse || (existingReformalizationId && !existingRunId
    ? await fetchJson(fetchImpl, `${baseUrl}/api/reformalizations/${encodeURIComponent(existingReformalizationId)}`, { headers: buildHeaders(apiKey) })
    : existingRunId ? { ok: true, body: { run_id: existingRunId, session_id: sessionId } } : await startApiRun({
    fetchImpl,
    baseUrl,
    apiKey,
    message,
    sessionId,
    autonomous,
    projectSlug,
    projectTitle,
    overleafProjectTitle,
    projectNamespace,
    origin,
    originUrl,
    focusFormalizationId,
    focusSourceHash,
    newFormalization,
    artifactOperation,
    purpose,
    sourceBundle,
    sourceContextRef,
    allowSourcePause,
    idempotencyKey,
    timeoutSeconds: timeoutMs / 1000,
  }));
  if (!start.ok) return { ok: false, timedOut: false, error: start.error };

  if (start.body?.reformalization_id && onOperationQueued) await onOperationQueued(start.body);
  while (start.body?.reformalization_id && !start.body.run_id) {
    if (observerSignal?.aborted) return { ok: false, detached: true, error: 'Observation detached; the queued retry is retained.' };
    if (!['queued', 'active'].includes(start.body.state)) {
      return { ok: false, timedOut: false, error: start.body.result?.detail || 'The retry was retained without starting a solver.' };
    }
    if (onEvent) await onEvent('reformalization_queued', start.body);
    await new Promise(resolve => setTimeout(resolve, busyRetryDelayMs));
    const polled = await fetchJson(fetchImpl, `${baseUrl}/api/reformalizations/${encodeURIComponent(start.body.reformalization_id)}`, { headers: buildHeaders(apiKey) });
    if (polled.ok) start = polled;
    else if (polled.status != null && polled.status < 500) return { ok: false, timedOut: false, error: polled.error };
  }

  const runId = start.body?.run_id;
  const newSessionId = start.body?.session_id || sessionId;
  if (!runId) return { ok: false, timedOut: false, error: "Lea adapter did not return a run_id." };
  await log(`[backend] Lea adapter run started: ${runId} (session ${newSessionId})\n`);
  if (onRunStarted && !existingRunId) await onRunStarted(runId, newSessionId, start.body || {});

  const abort = new AbortController();
  const detach = () => abort.abort();
  observerSignal?.addEventListener("abort", detach, { once: true });
  if (observerSignal?.aborted) detach();
  let timedOut = false;
  let interruptPromise = null;
  const adapterDeadline = start.body?.deadline_at || deadlineAt;
  const remainingMs = adapterDeadline ? Math.max(1, Date.parse(adapterDeadline) - Date.now()) : timeoutMs;
  const timer = setTimeout(() => {
    timedOut = true;
    abort.abort();
    interruptPromise = interruptApiRun({ fetchImpl, baseUrl, apiKey, runId,
                                       reason: "timeout", source: "companion" });
  }, Math.max(1, remainingMs));
  if (typeof timer.unref === "function") timer.unref();

  // Phase 2 contract (PLAN-system-hardening): the adapter queues runs
  // server-side and the events endpoint is a pure observer — attach is
  // idempotent, a queued run streams `queued` frames, and a finished run
  // replays to a terminal `done`. The old 409 busy/finished disambiguation is
  // gone; any HTTP rejection of the attach is a real failure.
  //
  // What remains client-side is transport robustness: a DROPPED stream (open
  // error, mid-stream disconnect, or close without a `done` frame) does not
  // mean the run failed — it may still be executing adapter-side, so failing
  // the job here would abandon a live, billing run and mislabel its outcome.
  // Consult the exact run row and keep reconnecting while the adapter is
  // unavailable. The original run deadline, not a connection counter, decides
  // when an interruption is requested.
  let loggedQueued = false;
  const observeEvent = async (type, data) => {
    if (type === "queued" && !loggedQueued) {
      loggedQueued = true;
      await log(`[backend] Lea adapter queued this run (position ${Number.isFinite(data?.position) ? data.position : "?"}).\n`);
    }
    if (onEvent) await onEvent(type, data);
  };

  let outcome;
  let loggedStreamDrop = false;
  try {
    for (;;) {
      outcome = await streamApiRun({
        fetchImpl, baseUrl, apiKey, runId, maxTurns, autoApprove,
        signal: abort.signal, onEvent: observeEvent, onProgressUpdated,
      });
      if (outcome.ok || outcome.aborted) break;
      const streamDropped = outcome.httpStatus == null && !outcome.doneStatus;
      if (!streamDropped) break;

      const row = await fetchApiRunRow({ fetchImpl, baseUrl, apiKey, sessionId: newSessionId, runId });
      const rowStatus = String(row?.status || "").toLowerCase();
      if (rowStatus && rowStatus !== "pending" && rowStatus !== "running") {
        // Terminal without us attached: adopt the run row's outcome as if it
        // had arrived on a `done` event.
        const ok = SUCCESS_DONE_STATUS.has(rowStatus);
        outcome = {
          ok,
          doneStatus: rowStatus,
          resultKind: String(row.result_kind || rowStatus).toLowerCase() || null,
          resultDetail: typeof row.result_detail === "string" ? row.result_detail : null,
          stopReason: typeof row.stop_reason === "string" ? row.stop_reason : null,
          recoverable: row.recoverable === true || row.recoverable === 1,
          recoveryState: row.recovery_state || null,
          recoveryReason: row.recovery_reason || null,
          waitingDependencies: JSON.parse(row.waiting_dependencies_json || "[]"),
          error: ok ? null : `Lea run ended with status: ${rowStatus}`,
        };
        break;
      }
      if (!loggedStreamDrop) {
        loggedStreamDrop = true;
        await log("[backend] Adapter connection dropped; waiting to reattach to the same run.\n");
      }
      if (!(await waitBeforeRetry(retryDelayWithJitter(busyRetryDelayMs), abort.signal))) {
        outcome = { ok: false, aborted: true, error: "Run stream aborted." };
        break;
      }
    }
  } finally {
    clearTimeout(timer);
    observerSignal?.removeEventListener("abort", detach);
  }

  let settledRow = null;
  if (timedOut) {
    const until = Date.now() + settleGraceMs;
    if (interruptPromise) {
      await Promise.race([interruptPromise, new Promise((resolve) => setTimeout(resolve, Math.min(1000, settleGraceMs)))]);
    }
    do {
      settledRow = await fetchApiRunRow({ fetchImpl, baseUrl, apiKey, sessionId: newSessionId, runId,
                                         signal: AbortSignal.timeout(Math.max(1, Math.min(3000, until - Date.now()))) });
      if (settledRow && !["pending", "running"].includes(String(settledRow.status || "").toLowerCase())) break;
      if (Date.now() >= until) break;
      await new Promise((resolve) => setTimeout(resolve, Math.min(250, until - Date.now())));
    } while (Date.now() < until);
  }
  const usage = await fetchApiRunUsage({ fetchImpl, baseUrl, apiKey, sessionId: newSessionId, runId });

  if (timedOut) {
    const terminal = settledRow && !["pending", "running"].includes(String(settledRow.status || "").toLowerCase());
    const completed = terminal && SUCCESS_DONE_STATUS.has(String(settledRow.status).toLowerCase());
    return {
      ok: Boolean(completed),
      timedOut: !completed,
      settling: !terminal,
      apiRunId: runId,
      sessionId: newSessionId,
      doneStatus: settledRow?.status || outcome?.doneStatus || null,
      resultKind: settledRow?.result_kind || outcome?.resultKind || null,
      resultDetail: settledRow?.result_detail || outcome?.resultDetail || null,
      stopReason: settledRow?.stop_requested_reason
        || (settledRow?.usage_status != null ? settledRow.stop_reason : null)
        || outcome?.stopReason || (completed ? null : "timeout"),
      recoverable: completed ? false : (settledRow?.recoverable ?? true),
      recoveryState: settledRow?.recovery_state || outcome?.recoveryState || null,
      recoveryReason: settledRow?.recovery_reason || outcome?.recoveryReason || null,
      formalizationId: start.body?.focus_formalization_id || start.body?.formalization?.id || null,
      error: completed ? null : "Time limit reached.",
      ...usage,
    };
  }
  return {
    ok: outcome.ok,
    timedOut: false,
    apiRunId: runId,
    sessionId: newSessionId,
    doneStatus: outcome.doneStatus,
    resultKind: outcome.resultKind || null,
    resultDetail: outcome.resultDetail || null,
    stopReason: outcome.stopReason || null,
    recoverable: outcome.recoverable === true,
    recoveryState: outcome.recoveryState || null,
    recoveryReason: outcome.recoveryReason || null,
    waitingDependencies: outcome.waitingDependencies || [],
    formalizationId: start.body?.focus_formalization_id || start.body?.formalization?.id || null,
    error: outcome.ok ? undefined : outcome.error,
    ...usage,
  };
}

export function fetchInventoryAssessments({ fetchImpl, baseUrl, apiKey, comparisons }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/lea-status/compare`, {
    method: "POST", headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify({ comparisons })
  }, { timeoutMs: ADAPTER_METADATA_TIMEOUT_MS });
}

export function fetchCurrentLeaStatus({ fetchImpl, baseUrl, apiKey, formalizationId, sourceIdentityHash, sourceBundleHash, sourceBundle, coverage = "partial", timeoutMs = ADAPTER_METADATA_TIMEOUT_MS }) {
  if (sourceBundle) return fetchJson(fetchImpl, `${baseUrl}/api/formalizations/${encodeURIComponent(formalizationId)}/lea-status/compare`, {
    method: "POST", headers: buildHeaders(apiKey, { "Content-Type": "application/json" }), body: JSON.stringify({ source_bundle: sourceBundle, coverage })
  }, { timeoutMs });
  const query = new URLSearchParams();
  if (sourceIdentityHash) query.set("source_identity_hash", sourceIdentityHash);
  if (sourceBundleHash) query.set("source_bundle_hash", sourceBundleHash);
  return fetchJson(fetchImpl, `${baseUrl}/api/formalizations/${encodeURIComponent(formalizationId)}/lea-status?${query}`, {
    method: "GET", headers: buildHeaders(apiKey)
  }, { timeoutMs });
}

export function prepareOverleafSource({ fetchImpl, baseUrl, apiKey, formalizationId }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/formalizations/${encodeURIComponent(formalizationId)}/published-source`, {
    method: "GET", headers: buildHeaders(apiKey), signal: AbortSignal.timeout(5000)
  });
}

export function fetchFormalizationReview({ fetchImpl, baseUrl, apiKey, formalizationId, sourceContextRef }) {
  const query = new URLSearchParams();
  if (sourceContextRef) query.set('source_context_ref', sourceContextRef);
  return fetchJson(fetchImpl, `${baseUrl}/api/formalizations/${encodeURIComponent(formalizationId)}/review?${query}`, {
    method: 'GET', headers: buildHeaders(apiKey)
  }, { timeoutMs: ADAPTER_METADATA_TIMEOUT_MS });
}

export function submitFormalizationReview({ fetchImpl, baseUrl, apiKey, formalizationId, decision }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/formalizations/${encodeURIComponent(formalizationId)}/review-decisions`, {
    method: 'POST', headers: buildHeaders(apiKey, { 'Content-Type': 'application/json' }), body: JSON.stringify(decision)
  }, { timeoutMs: ADAPTER_METADATA_TIMEOUT_MS });
}
export function fetchLeaStatusHistory({ fetchImpl, baseUrl, apiKey, formalizationId, after }) {
  const query = new URLSearchParams({ limit: "50" });
  if (after) query.set("after", after);
  return fetchJson(fetchImpl, `${baseUrl}/api/formalizations/${encodeURIComponent(formalizationId)}/lea-status/updates?${query}`, {
    method: "GET", headers: buildHeaders(apiKey)
  });
}

function batchReportsUrl(baseUrl, slug, batchId = "") {
  const root = `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/formalize-batch-reports`;
  return batchId ? `${root}/${encodeURIComponent(batchId)}` : root;
}

export function createFormalizeBatchReport({ fetchImpl, baseUrl, slug, body }) {
  return fetchJson(fetchImpl, batchReportsUrl(baseUrl, slug), {
    method: "POST", headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify(body)
  });
}

export function listFormalizeBatchReports({ fetchImpl, baseUrl, slug, before }) {
  const query = before ? `?before=${encodeURIComponent(before)}` : "";
  return fetchJson(fetchImpl, `${batchReportsUrl(baseUrl, slug)}${query}`, { method: "GET" });
}

export function getFormalizeBatchReport({ fetchImpl, baseUrl, slug, batchId }) {
  return fetchJson(fetchImpl, batchReportsUrl(baseUrl, slug, batchId), { method: "GET" });
}

export function retryFormalizeBatchReport({ fetchImpl, baseUrl, slug, batchId }) {
  return fetchJson(fetchImpl, `${batchReportsUrl(baseUrl, slug, batchId)}/retry`, { method: "POST" });
}
