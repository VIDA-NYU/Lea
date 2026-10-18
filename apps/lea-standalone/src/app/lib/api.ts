// LeaUI v2 frontend API client — the HTTP + SSE calls against /api.
//
// All shared types live in ./types and are re-exported here, so existing
// `import { Foo } from "./api"` sites keep working; new code can import types
// straight from ./types.

import type {
  ApprovalMode,
  ApprovalDecision,
  LeanCheckExecution,
  LeanCheckRuntime,
  Diagnostic,
  SessionSummary,
  SessionStatus,
  ChatMessage,
  CodeStep,
  SafeVerifyStatus,
  SessionDetail,
  Project,
  ProjectDetail,
  ProjectFile,
  ProjectGraph,
  Skill,
  AuthoringFieldValues,
  McpServer,
  McpTransport,
  McpTestResult,
  CustomTool,
  SessionSkillsMcp,
  SubagentProfile,
  SubagentSettings,
  BlueprintWarning,
  TreeEntry,
  SearchResult,
  Formalization,
  FormalizationCurrentSnapshot,
  GithubImportPreview,
  GithubImportProgress,
} from './types';

export * from './types';
import { workspaceResult, submitWithRecovery } from '../../../../../packages/lea-workspace-client/index.mjs';

// ────────────────────────────────────────────────────────────────────────────
// HTTP
// ────────────────────────────────────────────────────────────────────────────

async function detailMessage(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => ({} as any));
  if (typeof body.detail === 'string') return body.detail;
  if (body.detail?.message) return body.detail.message;
  if (typeof body.message === 'string') return body.message;
  return fallback;
}

async function readWorkspaceResponse(response: Response): Promise<any> {
  return readWorkspaceValue(await response.json());
}

async function readWorkspaceValue(initial: any): Promise<any> {
  try {
    return await workspaceResult(initial, async (id: string) => {
      const receipt = await fetch(`/api/workspace/commands/${encodeURIComponent(id)}`);
      if (!receipt.ok) throw Object.assign(new Error(await detailMessage(receipt, 'Unable to read workspace receipt.')), {status: receipt.status});
      return receipt.json();
    });
  } catch (error: any) {
    if (error.detail?.code === 'revision_conflict') throw new RevisionConflictError(error.message, error.detail);
    throw error;
  }
}

async function workspaceMutation(url:string,method:string,body:Record<string,unknown>):Promise<Response> {
  const key=String(body.idempotency_key || globalThis.crypto?.randomUUID?.() || `edit-${Date.now()}-${Math.random()}`);
  const serialized=JSON.stringify({...body,idempotency_key:key,draft_id:body.draft_id || key});
  return submitWithRecovery(
    ()=>fetch(url,{method,headers:{'Content-Type':'application/json'},body:serialized}),
    async()=>{
      const response=await fetch(`/api/workspace/receipts?idempotency_key=${encodeURIComponent(key)}`);
      if(!response.ok)throw Object.assign(new Error('Unable to recover the edit receipt.'),{status:response.status});
      return response;
    });
}

export async function listSessions(): Promise<SessionSummary[]> {
  return (await sessionListSnapshot()).sessions;
}

export async function sessionListSnapshot(): Promise<{ sessions: SessionSummary[]; cursor?: number }> {
  const response = await fetch('/api/sessions');
  if (!response.ok) throw new Error(`Failed to load sessions: ${response.statusText}`);
  const data = await readWorkspaceResponse(response);
  return { sessions: Array.isArray(data.sessions) ? data.sessions : [],
    cursor: Number.isSafeInteger(data.cursor) ? data.cursor : undefined };
}

export async function getSession(sessionId: string, signal?: AbortSignal): Promise<SessionDetail> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { signal });
  if (!response.ok) throw new Error(`Failed to load session: ${response.statusText}`);
  return readWorkspaceResponse(response);
}

export async function createRun(
  message: string,
  sessionId?: string,
  model?: string,
  scope?: {
    focus_formalization_id?: string;
    focus_source_hash?: string;
    project_slug?: string;
    project_title?: string;
    project_namespace?: string;
    new_formalization?: {
      display_title: string;
      kind?: string;
      declaration_name?: string;
      statement?: string;
      origin?: string;
      origin_key?: string;
      source_hash?: string;
    };
    source_context_ref?: string;
    idempotency_key?: string;
    artifact_operation?: 'reformalize';
  },
  onQueued?: (operation: { session_id: string; reformalization_id: string }) => void,
  stillSelected?: () => boolean,
): Promise<{
  session_id: string;
  run_id: string;
  model: string;
  autonomous?: boolean;
  approval_mode?: ApprovalMode;
  message: ChatMessage;
  focus_formalization_id?: string | null;
  formalization?: Formalization | null;
}> {
  const response = await fetch('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId, model, ...scope }),
  });
  if (!response.ok) {
    throw Object.assign(new Error(await detailMessage(response, `Failed to start run: ${response.statusText}`)), { status: response.status });
  }
  return awaitReformalization(await response.json(), onQueued, stillSelected);
}


export async function cancelReformalization(id: string): Promise<void> {
  const response = await fetch(`/api/reformalizations/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  if (!response.ok) throw new Error(await detailMessage(response, 'Could not stop the reformalization.'));
}


export async function getReformalization(id: string): Promise<{
  reformalization_id: string; state: string; session_id: string; run_id?: string;
  result?: { detail?: string }; drafts: CodeStep[];
}> {
  const response = await fetch(`/api/reformalizations/${encodeURIComponent(id)}`);
  if (!response.ok) throw new Error(await detailMessage(response, 'Could not load the retained retry.'));
  return readWorkspaceResponse(response);
}

export async function awaitReformalization(initial: any,
  onQueued?: (operation: { session_id: string; reformalization_id: string }) => void,
  stillSelected: () => boolean = () => true): Promise<any> {
  let operation = initial;
  if (operation.reformalization_id) onQueued?.(operation);
  while (operation.reformalization_id && !operation.run_id) {
    if (!stillSelected()) return operation;
    if (operation.state && !['queued', 'active'].includes(operation.state)) {
      throw new Error(operation.result?.detail || 'This retry is retained without an active solver.');
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
    operation = await getReformalization(operation.reformalization_id);
  }
  return operation;
}

export async function resumeInterruptedRun(runId: string): Promise<{ run_id: string; session_id: string }> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/resume`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ acknowledge_uncertainty: true, idempotency_key: `resume:${runId}` }),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Could not resume the interrupted run.'));
  return awaitReformalization(await response.json());
}

export async function prepareOverleafSource(formalizationId: string): Promise<{ source_context_ref: string; captured_at: string }> {
  const response = await fetch(`/api/formalizations/${encodeURIComponent(formalizationId)}/published-source`);
  if (!response.ok) throw Object.assign(new Error(await detailMessage(response, 'Could not connect to the Overleaf document.')), { status: response.status });
  return readWorkspaceResponse(response);
}

export async function continueOverleafRun(runId: string, reference: string, idempotencyKey: string): Promise<{ run_id: string; session_id: string }> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/continue`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_context_ref: reference, idempotency_key: idempotencyKey })
  });
  if (!response.ok) throw Object.assign(new Error(await detailMessage(response, 'Could not continue the saved Overleaf formalization.')), { status: response.status });
  return awaitReformalization(await response.json());
}

export async function listProjectFormalizations(
  projectId: string,
): Promise<{ formalizations: Formalization[]; summary: Record<string, number> }> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/formalizations`,
  );
  if (!response.ok) {
    throw new Error(await detailMessage(response, 'Failed to load formalizations.'));
  }
  return readWorkspaceResponse(response);
}

export async function getFormalization(formalizationId: string): Promise<Formalization> {
  const response = await fetch(
    `/api/formalizations/${encodeURIComponent(formalizationId)}`,
  );
  if (!response.ok) {
    throw new Error(await detailMessage(response, 'Failed to load formalization.'));
  }
  return readWorkspaceResponse(response);
}

export async function getCurrentFormalization(
  formalizationId: string,
  sessionId?: string,
): Promise<FormalizationCurrentSnapshot> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  const response = await fetch(
    `/api/formalizations/${encodeURIComponent(formalizationId)}/current${query}`,
  );
  if (!response.ok) {
    throw new Error(await detailMessage(response, 'Failed to load the current formalization.'));
  }
  return readWorkspaceResponse(response);
}

export async function updateSessionTitle(
  sessionId: string,
  title: string,
): Promise<SessionSummary> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, 'Failed to rename conversation.'));
  }
  return readWorkspaceResponse(response);
}

export async function submitApproval(
  runId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<void> {
  const response = await fetch(
    `/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision }),
    },
  );
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to submit decision: ${response.statusText}`));
  }
}

export async function interruptRun(runId: string): Promise<void> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/interrupt`, { method: 'POST' });
  if (!response.ok && response.status !== 409) {
    throw new Error(await detailMessage(response, `Failed to interrupt run: ${response.statusText}`));
  }
}

// Stop a single running child sub-agent (D2), addressed by its child SESSION id —
// without cancelling the coordinator run. A 404 means it already finished (nothing to
// stop), which we treat as success.
export async function interruptSubagent(sessionId: string): Promise<void> {
  const response = await fetch(`/api/sub-agents/${encodeURIComponent(sessionId)}/interrupt`, { method: 'POST' });
  if (!response.ok && response.status !== 404) {
    throw new Error(await detailMessage(response, `Failed to stop sub-agent: ${response.statusText}`));
  }
}

// Manual context compaction (G3): the `/compact` slash command fires the same condenser
// G1 runs automatically. Returns the token delta so the composer can note "freed ~N".
export interface CompactionResult {
  changed: boolean;
  pruned: number;
  summarized: boolean;
  before_tokens: number;
  after_tokens: number;
  freed_tokens: number;
  referenced_files: string[]; // files still in the model's view after compaction
  // The durable timeline marker (kind='compaction'), present only when something changed;
  // null on a no-op. Its `content` is the JSON payload the thread renders as the card.
  message: ChatMessage | null;
}

export async function compactSession(sessionId: string): Promise<CompactionResult> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/compact`, {
    method: 'POST',
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to compact: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

// ── Projects (v2.1) ────────────────────────────────────────────────────────────
export async function listProjects(): Promise<Project[]> {
  const response = await fetch('/api/projects');
  if (!response.ok) throw new Error(`Failed to load projects: ${response.statusText}`);
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.projects) ? data.projects : [];
}

export async function getProject(projectId: string): Promise<ProjectDetail> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}`);
  if (!response.ok) throw new Error(`Failed to load project: ${response.statusText}`);
  return readWorkspaceResponse(response);
}

export async function createProject(title: string, description?: string): Promise<Project> {
  const response = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, description }),
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to create project: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

export async function updateProject(
  projectId: string,
  update: { title?: string; description?: string },
): Promise<Project> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to update project: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

export async function deleteProject(projectId: string): Promise<void> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, { method: 'DELETE' });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to delete project: ${response.statusText}`));
  }
  await readWorkspaceResponse(response);
}

export interface ProjectSession {
  id: string;
  title: string;
  project_id: string | null;
  created_at: string;
  updated_at: string;
}

// Create a session that lives inside the project (D23). The run started for it
// resolves the shared project repo + namespace server-side.
export async function createSessionInProject(projectId: string, title?: string): Promise<ProjectSession> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to create session: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

// ── Project files: upload / list / download / delete (.lea/files/, S1/S2) ──────
export async function listProjectFiles(projectId: string): Promise<ProjectFile[]> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/files`);
  if (!response.ok) throw new Error(await detailMessage(response, `Failed to load files: ${response.statusText}`));
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.files) ? data.files : [];
}

export async function uploadProjectFile(projectId: string, file: File): Promise<ProjectFile> {
  const form = new FormData();
  form.append('file', file);
  const key=globalThis.crypto?.randomUUID?.() || `upload-${Date.now()}-${Math.random()}`;
  const response = await submitWithRecovery(()=>fetch(`/api/projects/${encodeURIComponent(projectId)}/files`, {
    method: 'POST',headers:{'Idempotency-Key':key},
    body: form, // the browser sets the multipart boundary
  }),async()=>{
    const value=await fetch(`/api/workspace/receipts?idempotency_key=${encodeURIComponent(key)}`);
    if(!value.ok)throw Object.assign(new Error('Unable to recover the upload receipt.'),{status:value.status});
    return value;
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to upload ${file.name}: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

export async function deleteProjectFile(projectId: string, fileId: string): Promise<void> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(fileId)}`,
    { method: 'DELETE' },
  );
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to delete file: ${response.statusText}`));
  }
  await readWorkspaceResponse(response);
}

// The browser navigates here to download; the route streams the stored bytes.
export function projectFileDownloadUrl(projectId: string, fileId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(fileId)}`;
}

// ── Project docs: Instructions / Memory / Blueprint (.lea/*.md) ────────────────
// One pair of calls backs the markdown editors (D39). `doc` is the route segment;
// content is raw markdown in/out. Blueprint shares the same content round-trip
// (its responses also carry `warnings`, fetched separately via getProjectBlueprint).
export type ProjectDocName = 'instructions' | 'memory' | 'blueprint';

export async function getProjectDoc(projectId: string, doc: ProjectDocName): Promise<string> {
  return (await getProjectDocSnapshot(projectId,doc)).content;
}

export async function getProjectDocSnapshot(projectId: string, doc: ProjectDocName): Promise<{content:string; file_revision?:string}> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/${doc}`,
  );
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to load ${doc}: ${response.statusText}`));
  }
  const data = await readWorkspaceResponse(response);
  return {content:typeof data.content === 'string' ? data.content : '',file_revision:data.file_revision};
}

export async function putProjectDoc(
  projectId: string,
  doc: ProjectDocName,
  content: string,
  fileRevision?: string,
): Promise<{ content: string; commit_sha?: string; file_revision?:string }> {
  const response = await workspaceMutation(`/api/projects/${encodeURIComponent(projectId)}/${doc}`,'PUT',
    {content,file_revision:fileRevision});
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to save ${doc}: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

// ── Blueprint authoring + derived graph (Slice 5, D28/D29) ─────────────────────
// The blueprint's content round-trips through getProjectDoc/putProjectDoc('blueprint');
// these two add the blueprint-specific extras: structural `warnings` (advisory) and
// the parsed-and-derived dependency `graph` (node status + session attribution).

export async function getProjectBlueprint(
  projectId: string,
): Promise<{ content: string; warnings: BlueprintWarning[] }> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/blueprint`);
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to load blueprint: ${response.statusText}`));
  }
  const data = await readWorkspaceResponse(response);
  return { content: typeof data.content === 'string' ? data.content : '', warnings: data.warnings ?? [] };
}

export async function getProjectGraph(projectId: string): Promise<ProjectGraph> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/graph`);
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Failed to load graph: ${response.statusText}`));
  }
  const data = await readWorkspaceResponse(response);
  return { nodes: Array.isArray(data.nodes) ? data.nodes : [], edges: Array.isArray(data.edges) ? data.edges : [] };
}

// ── Filesystem tab: tree / read / edit / export the project repo (Slice 6, D34) ─
// The project is already a git repo, so this is mostly exposure: browse the tree,
// read/edit any file (write+commit, path-guarded), download the whole thing.

export async function getProjectTree(projectId: string): Promise<TreeEntry[]> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/tree`);
  if (!response.ok) throw new Error(await detailMessage(response, `Failed to load files: ${response.statusText}`));
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.tree) ? data.tree : [];
}

// A binary/undecodable file comes back as 415; we surface that as `binary: true`
// (the viewer offers a download instead of garbled text) rather than throwing.
export async function getProjectFile(
  projectId: string,
  path: string,
): Promise<{ path: string; content: string; lean: boolean; binary: boolean; file_revision?: string }> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}`,
  );
  if (response.status === 415) return { path, content: '', lean: false, binary: true };
  if (!response.ok) throw new Error(await detailMessage(response, `Failed to load ${path}: ${response.statusText}`));
  const data = await readWorkspaceResponse(response);
  return { path: data.path ?? path, content: data.content ?? '', lean: !!data.lean, binary: false, file_revision: data.file_revision };
}

export async function putProjectFile(
  projectId: string,
  path: string,
  content: string,
  fileRevision?: string,
): Promise<{ path: string; commit_sha: string; file_revision?: string; check: { status: 'ok' | 'error'; detail?: string | null } | null }> {
  const response = await workspaceMutation(`/api/projects/${encodeURIComponent(projectId)}/file`,'PUT',
    {path,content,file_revision:fileRevision});
  if (!response.ok) throw new Error(await detailMessage(response, `Failed to save ${path}: ${response.statusText}`));
  return readWorkspaceResponse(response);
}

export class GithubImportApiError extends Error {
  code?: string;
  status: number;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'GithubImportApiError';
    this.status = status;
    this.code = code;
  }
}

async function throwGithubImportError(response: Response, fallback: string): Promise<never> {
  const body = await response.json().catch(() => ({} as any));
  const detail = body.detail ?? body;
  const message =
    (typeof detail === 'string' ? detail : detail?.message) || fallback;
  throw new GithubImportApiError(message, response.status, detail?.error || detail?.code);
}

export async function previewProjectGithubImport(
  projectId: string,
  repositoryUrl: string,
): Promise<GithubImportPreview> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/github-imports/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repository_url: repositoryUrl }),
    },
  );
  if (!response.ok) {
    return throwGithubImportError(response, 'Failed to analyze the GitHub repository.');
  }
  return readWorkspaceResponse(response);
}

export async function confirmProjectGithubImport(
  projectId: string,
  previewId: string,
): Promise<GithubImportProgress> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/github-imports`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preview_id: previewId }),
    },
  );
  if (!response.ok) {
    return throwGithubImportError(response, 'Failed to add the Lean files.');
  }
  return readWorkspaceResponse(response);
}

export async function getProjectGithubImport(
  projectId: string,
  importId: string,
): Promise<GithubImportProgress> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/github-imports/${encodeURIComponent(importId)}`,
  );
  if (!response.ok) {
    return throwGithubImportError(response, 'Failed to load GitHub import progress.');
  }
  return readWorkspaceResponse(response);
}

// The browser navigates here to download the project as a zip.
export function projectExportUrl(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/export`;
}

// A direct-download URL for a single session's files as a zip (#14). Loose sessions
// have no other download path; used by the session header's Download button.
export function sessionExportUrl(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/export`;
}

export async function downloadWorkspaceExport(url: string): Promise<void> {
  const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(!response.ok)throw new Error(await detailMessage(response,'Could not prepare this export.'));
  const artifact=await readWorkspaceResponse(response) as {download_url:string};
  const link=document.createElement('a');link.href=artifact.download_url;link.download='';link.click();
}

// ── Git sharing: set remote + push to GitHub (6b/U3, D34) ─────────────────────
// The remote URL is stored per-project; the token is global (Settings, redacted).
export async function setProjectRemote(
  projectId: string,
  remoteUrl: string,
): Promise<{ id: string; remote_url: string }> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/git/remote`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ remote_url: remoteUrl }),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to set the GitHub remote.'));
  return readWorkspaceResponse(response);
}

export async function pushProject(
  projectId: string,
): Promise<{ pushed: boolean; remote_url: string; detail: string }> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/git/push`, {
    method: 'POST',
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Push to GitHub failed.'));
  return readWorkspaceResponse(response);
}

// ── Sub-agents (D6) ───────────────────────────────────────────────────────────
// View/edit each built-in role's settings over /api/sub-agents. Edits persist as
// per-role overrides (not by mutating the vendored YAML) and are merged at spawn.
export async function createSubagentRole(input: {
  name: string;
  authoring?: AuthoringFieldValues;
  system_prompt?: string;
  model?: string | null;
  tools?: string[] | null;
  max_turns?: number | null;
}): Promise<SubagentProfile> {
  const response = await fetch('/api/sub-agents/roles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to add the sub-agent.'));
  return readWorkspaceResponse(response);
}

export async function updateSubagentRole(
  roleId: string,
  update: { name?: string; authoring?: AuthoringFieldValues; max_turns?: number | null },
): Promise<SubagentProfile> {
  const response = await fetch(`/api/sub-agents/roles/${encodeURIComponent(roleId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to update the sub-agent.'));
  return readWorkspaceResponse(response);
}

export async function deleteSubagentRole(roleId: string): Promise<void> {
  const response = await fetch(`/api/sub-agents/roles/${encodeURIComponent(roleId)}`, { method: 'DELETE' });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to delete the sub-agent.'));
}

export async function listSubagentProfiles(): Promise<SubagentProfile[]> {
  const response = await fetch('/api/sub-agents/profiles');
  if (!response.ok)
    throw new Error(await detailMessage(response, `Failed to load sub-agents: ${response.statusText}`));
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.profiles) ? data.profiles : [];
}

// PUT the effective settings the user edited; the backend stores only the diff-from-
// default (so an untouched default keeps flowing through; sending defaults resets it).
export async function updateSubagentProfile(
  name: string,
  settings: Partial<SubagentSettings>,
): Promise<SubagentProfile> {
  const response = await fetch(`/api/sub-agents/profiles/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  });
  if (!response.ok)
    throw new Error(await detailMessage(response, `Failed to save sub-agent: ${response.statusText}`));
  return readWorkspaceResponse(response);
}

// ── Skills (Skill Factory, v2.1.1) ────────────────────────────────────────────
// CRUD + scope assignment + GitHub import over /api/skills. A skill's `body` is
// markdown injected into the prover's system prompt for the project runs it
// resolves for (global ∪ assigned, D47).
export async function listSkills(): Promise<Skill[]> {
  const response = await fetch('/api/skills');
  if (!response.ok) throw new Error(`Failed to load skills: ${response.statusText}`);
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.skills) ? data.skills : [];
}

export async function createSkill(input: {
  name: string;
  body?: string;
  authoring?: AuthoringFieldValues;
  is_global?: boolean;
  project_ids?: string[];
}): Promise<Skill> {
  const response = await fetch('/api/skills', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to create the skill.'));
  return readWorkspaceResponse(response);
}

// Import a skill from a GitHub link (D56) — the headline "paste a link → Add".
export interface ImportedExtras {
  imported_roles?: { name: string; status: string; reason?: string; unmapped_tools?: string[] }[];
  imported_servers?: { name: string; status: string; reason?: string }[];
}

export async function importSkill(input: {
  url: string;
  is_global?: boolean;
  project_ids?: string[];
}): Promise<Skill & ImportedExtras> {
  const response = await fetch('/api/skills/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to import the skill.'));
  return readWorkspaceResponse(response);
}

export async function updateSkill(
  skillId: string,
  update: { name?: string; body?: string; authoring?: AuthoringFieldValues },
): Promise<Skill> {
  const response = await fetch(`/api/skills/${encodeURIComponent(skillId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to update the skill.'));
  return readWorkspaceResponse(response);
}

export async function setSkillAssignment(
  skillId: string,
  assignment: { is_global: boolean; project_ids: string[] },
): Promise<Skill> {
  const response = await fetch(`/api/skills/${encodeURIComponent(skillId)}/assignment`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(assignment),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to update the skill scope.'));
  return readWorkspaceResponse(response);
}

export async function deleteSkill(skillId: string): Promise<void> {
  const response = await fetch(`/api/skills/${encodeURIComponent(skillId)}`, { method: 'DELETE' });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to delete the skill.'));
}

// ── MCP servers (v2.5 E0) ─────────────────────────────────────────────────────
// Deliberately mirrors the skills client above: same CRUD + assignment shape,
// because an MCP server is the same kind of library item. `testMcpServer` has no
// skills counterpart — it dry-runs an UNSAVED draft so the form can answer "did I
// type this right?" before a row exists (E0b).
export interface McpServerInput {
  name: string;
  transport?: McpTransport;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  env_from?: string[];
  url?: string | null;
  api_key_name?: string | null;
  enabled?: boolean;
  is_global?: boolean;
  project_ids?: string[];
}

export async function listMcpServers(): Promise<McpServer[]> {
  const response = await fetch('/api/mcp-servers');
  if (!response.ok) throw new Error(`Failed to load MCP servers: ${response.statusText}`);
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.servers) ? data.servers : [];
}

export async function createMcpServer(input: McpServerInput): Promise<McpServer> {
  const response = await fetch('/api/mcp-servers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to add the MCP server.'));
  return readWorkspaceResponse(response);
}

export async function updateMcpServer(
  serverId: string,
  update: Partial<McpServerInput>,
): Promise<McpServer> {
  const response = await fetch(`/api/mcp-servers/${encodeURIComponent(serverId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to update the MCP server.'));
  return readWorkspaceResponse(response);
}

export async function setMcpServerAssignment(
  serverId: string,
  assignment: { is_global: boolean; project_ids: string[] },
): Promise<McpServer> {
  const response = await fetch(`/api/mcp-servers/${encodeURIComponent(serverId)}/assignment`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(assignment),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to update the server scope.'));
  return readWorkspaceResponse(response);
}

export async function deleteMcpServer(serverId: string): Promise<void> {
  const response = await fetch(`/api/mcp-servers/${encodeURIComponent(serverId)}`, { method: 'DELETE' });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to delete the MCP server.'));
}

// ── Per-session skills / MCP (v2.5 E0e) ───────────────────────────────────────
export async function getSessionSkillsMcp(sessionId: string): Promise<SessionSkillsMcp> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/skills-mcp`);
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to load skills and servers.'));
  return readWorkspaceResponse(response);
}

export async function setSessionSkillMcp(
  sessionId: string,
  toggle: { kind: 'skill' | 'mcp_server'; item_id: string; action: 'add' | 'remove' | null },
): Promise<SessionSkillsMcp> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/skills-mcp`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(toggle),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to update skills and servers.'));
  return readWorkspaceResponse(response);
}

export async function getMcpServerDefaults(): Promise<{ lean_project_path: string | null }> {
  const response = await fetch('/api/mcp-servers/defaults');
  if (!response.ok) return { lean_project_path: null };
  return readWorkspaceResponse(response);
}

export async function listCustomTools(): Promise<CustomTool[]> {
  const response = await fetch('/api/custom-tools');
  if (!response.ok) return [];
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.tools) ? data.tools : [];
}

export async function createCustomTool(input: {
  name: string;
  url: string;
  description?: string;
  authoring?: AuthoringFieldValues;
  method?: string;
  params?: Record<string, unknown>;
  auth_key_name?: string | null;
  is_global?: boolean;
  project_ids?: string[];
}): Promise<CustomTool> {
  const response = await fetch('/api/custom-tools', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to add the tool.'));
  return readWorkspaceResponse(response);
}

export async function setCustomToolAssignment(
  toolId: string,
  assignment: { is_global: boolean; project_ids: string[] },
): Promise<CustomTool> {
  const response = await fetch(`/api/custom-tools/${encodeURIComponent(toolId)}/assignment`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(assignment),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to scope the tool.'));
  return readWorkspaceResponse(response);
}

export async function deleteCustomTool(toolId: string): Promise<void> {
  const response = await fetch(`/api/custom-tools/${encodeURIComponent(toolId)}`, { method: 'DELETE' });
  if (!response.ok) throw new Error(await detailMessage(response, 'Failed to delete the tool.'));
}

export interface CatalogEntry {
  id: string;
  title: string;
  summary: string;
  requires?: string;
  installed: boolean;
  skill_url?: string;
  skill_note?: string;
  recommended_tools?: string[];
}

export async function listMcpCatalog(): Promise<CatalogEntry[]> {
  const response = await fetch('/api/mcp-servers/catalog');
  if (!response.ok) return [];
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.entries) ? data.entries : [];
}

export async function installMcpCatalogEntry(entryId: string): Promise<McpServer> {
  const response = await fetch(`/api/mcp-servers/catalog/${encodeURIComponent(entryId)}`, {
    method: 'POST',
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Could not install that.'));
  return readWorkspaceResponse(response);
}

export interface ToolCatalogEntry {
  id: string;
  title: string;
  summary: string;
  requires?: string;
  installed: boolean;
}

export async function listToolCatalog(): Promise<ToolCatalogEntry[]> {
  const response = await fetch('/api/custom-tools/catalog');
  if (!response.ok) return [];
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.entries) ? data.entries : [];
}

export async function installToolCatalogEntry(entryId: string): Promise<CustomTool> {
  const response = await fetch(`/api/custom-tools/catalog/${encodeURIComponent(entryId)}`, {
    method: 'POST',
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Could not install that.'));
  return readWorkspaceResponse(response);
}

export interface McpKeyRequirement {
  env: string;
  servers: string[];
  configured: boolean;
}

export async function getMcpKeyRequirements(): Promise<McpKeyRequirement[]> {
  const response = await fetch('/api/mcp-servers/key-requirements');
  if (!response.ok) return [];
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.requirements) ? data.requirements : [];
}

export async function testMcpServer(spec: {
  transport?: McpTransport;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  env_from?: string[];
  url?: string | null;
  api_key_name?: string | null;
}): Promise<McpTestResult> {
  const response = await fetch('/api/mcp-servers/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(spec),
  });
  if (!response.ok) throw new Error(await detailMessage(response, 'Could not run the test.'));
  return readWorkspaceResponse(response);
}

// ── Global search (Slice 7, D41) ──────────────────────────────────────────────
// Sessions matching the query by their own title or their project's title, each
// tagged with its project. The only way to reach a project session (sidebar-hidden).
export async function searchSessions(query: string): Promise<SearchResult[]> {
  const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
  if (!response.ok) throw new Error(await detailMessage(response, `Search failed: ${response.statusText}`));
  const data = await readWorkspaceResponse(response);
  return Array.isArray(data.results) ? data.results : [];
}

// ── Writeable canvas + manual checks (F5 wires the UI to these) ────────────────
export interface FileWriteResult {
  unchanged: boolean;
  code_step?: CodeStep | null;
  note?: ChatMessage | null;
  revision_token?: string | null;
}

export class RevisionConflictError extends Error {
  currentRevision?: string | null;
  lastUpdatedSession?: { id: string; title: string } | null;

  constructor(
    message: string,
    detail?: {
      current_revision?: string | null;
      last_updated_session?: { id: string; title: string } | null;
    },
  ) {
    super(message);
    this.name = 'RevisionConflictError';
    this.currentRevision = detail?.current_revision;
    this.lastUpdatedSession = detail?.last_updated_session;
  }
}

export async function writeSessionFile(
  sessionId: string,
  path: string,
  content: string,
  note?: string,
  formalizationId?: string,
  baseRevision?: string,
  fileRevision?: string,
): Promise<FileWriteResult> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() || `save-${Date.now()}-${Math.random()}`;
  const draftId = idempotencyKey;
  const metadata = await fetch(`/api/workspace/file?session_id=${encodeURIComponent(sessionId)}&path=${encodeURIComponent(path)}`);
  if (!metadata.ok) {
    const body = await metadata.clone().json().catch(() => ({}));
    if (body.detail?.code === 'revision_conflict') throw new RevisionConflictError(body.detail.message, body.detail);
    throw new Error(await detailMessage(metadata, 'Unable to read the file revision.'));
  }
  const base = await metadata.json();
  const payload = {version: 1, action: 'session.save', idempotency_key: idempotencyKey, draft_id: draftId,
    expected_revision: fileRevision || base.file_revision,
    arguments: {session_id: sessionId, request: {path, content, note, formalization_id: formalizationId, base_revision: baseRevision}}};
  const send = async () => {
    const response = await fetch('/api/workspace/commands', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    if (!response.ok) throw Object.assign(new Error(await detailMessage(response, 'Unable to save file.')), {status: response.status});
    return response.json();
  };
  const lookup = async () => {
    const response = await fetch(`/api/workspace/receipts?idempotency_key=${encodeURIComponent(idempotencyKey)}`);
    if (!response.ok) throw Object.assign(new Error('Unable to recover the save receipt.'), {status:response.status});
    return response.json();
  };
  const admitted = await submitWithRecovery(send, lookup);
  return readWorkspaceValue(admitted);
}

export async function fetchLeanCheckRuntime(sessionId: string, signal?: AbortSignal): Promise<LeanCheckRuntime> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/lean-check-runtime`, { signal });
  if (!response.ok) return { state: 'unavailable' };
  return readWorkspaceResponse(response);
}

export async function leanCheckSession(
  sessionId: string,
  path?: string,
  formalizationId?: string,
): Promise<{ path: string; status: 'ok' | 'error'; check_current: boolean; check_stale_reason?: string | null; detail?: string | null; execution?: LeanCheckExecution; diagnostics?: Diagnostic[]; lean_check_runtime?: LeanCheckRuntime }> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/lean-check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, formalization_id: formalizationId }),
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `lean_check failed: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

export async function verifySession(
  sessionId: string,
  path?: string,
  formalizationId?: string,
): Promise<{ path: string; status: SafeVerifyStatus; current: boolean; detail?: string | null }> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, formalization_id: formalizationId }),
  });
  if (!response.ok) {
    throw new Error(await detailMessage(response, `Verify failed: ${response.statusText}`));
  }
  return readWorkspaceResponse(response);
}

// ────────────────────────────────────────────────────────────────────────────
// Settings / Stats / Models  (F6 rewires the pages; the endpoints already exist)
// ────────────────────────────────────────────────────────────────────────────

// The two approval modes the live system supports: "stepwise" gates the mutating
// tools (interactive default), "none" runs fully autonomous (no gate). Mirrors the
// backend config.PERMISSION_TIERS.
export type PermissionTier = 'stepwise' | 'none';

export interface PermissionTierOption {
  value: PermissionTier;
  label: string;
  description: string;
}

// A masked provider-key status (presence-only; the raw key never reaches the client).
export interface ApiKeyStatus {
  configured: boolean;
  last4?: string | null;
  label: string;
}

export interface AppSettings {
  model?: string;
  permission_tier?: PermissionTier;
  permission_tiers?: PermissionTierOption[];
  max_turns?: number | null;
  max_spend_usd?: number | null;
  current_spend_usd?: number;
  api_keys?: Record<string, ApiKeyStatus>;
  // Provider endpoint overrides (e.g. PORTKEY_BASE_URL): the effective value and
  // whether it was saved in Settings, exported in the shell, or is the default.
  provider_endpoints?: Record<string, ProviderEndpoint>;
  github_token?: { configured: boolean; last4?: string | null };
  model_options?: { value: string; label: string; family?: string }[];
  [key: string]: unknown;
}

export interface ProviderEndpoint {
  value: string;
  source: 'config' | 'env' | 'default';
  default: string;
}

export interface SettingsUpdate {
  model?: string;
  permission_tier?: PermissionTier;
  max_turns?: number | null;
  max_spend_usd?: number | null;
  api_keys?: Record<string, { value?: string; clear?: boolean }>;
  provider_endpoints?: Record<string, { value?: string; clear?: boolean }>;
  github_token?: { value?: string; clear?: boolean };
}

export interface ModelCatalogEntry {
  value: string; label: string; provider: string;
  compatibility?: string; availability?: string; preview?: boolean;
}
export interface ModelOption { value: string; label: string; family?: string }
export interface ModelCatalogPayload {
  models: ModelCatalogEntry[];
  featured?: ModelOption[];
  revision?: string;
  updated_at?: string | null;
  refreshing?: boolean;
  updates_disabled?: boolean;
  degraded?: boolean;
  warnings?: { message?: string; code?: string }[];
  sources?: Record<string, { updated_at: string | null; stale: boolean; error?: string | null }>;
}
export interface ModelRequiredKey { env: string; label: string; configured: boolean }
export interface ModelRequirements {
  model: string;
  provider?: string | null;
  required_keys: ModelRequiredKey[];
  satisfied: boolean;
}

// A session row as returned by GET /api/stats. Same shape as SessionSummary, but
// a live session can report status 'running' (used to drive the stats live-refresh).
export interface UsageSessionSummary extends Omit<SessionSummary, 'status'> {
  status: SessionStatus | 'running';
}

// All-time rollups (store.usage_stats → "global"). Internal to UsageStats.
interface UsageGlobals {
  session_count: number;
  message_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usage_details?: import("./types").UsageDetails | null;
  cost_usd: number;
  incomplete_usage_runs?: number;
  unconfirmed_usage_runs?: number;
  average_tokens_per_session: number;
  average_cost_per_session: number;
  average_messages_per_session: number;
}

// One calendar day of run usage (store.usage_stats → "daily"). Internal to UsageStats.
interface UsageDay {
  day: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usage_details?: import("./types").UsageDetails | null;
  cost_usd: number;
  run_count: number;
  session_count: number;
}

// Per-model rollup across runs (store.usage_stats → "models"). Internal to UsageStats.
interface UsageModelRow {
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usage_details?: import("./types").UsageDetails | null;
  cost_usd: number;
  run_count: number;
  session_count: number;
}

// Per-origin rollup across sessions (store.usage_stats → "origins"): Direct (UI) vs
// Overleaf extension. Internal to UsageStats.
interface UsageOriginRow {
  origin: 'ui' | 'overleaf' | string;
  session_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usage_details?: import("./types").UsageDetails | null;
  cost_usd: number;
}

export interface UsageStats {
  sessions: UsageSessionSummary[];
  global: UsageGlobals;
  daily: UsageDay[];
  models: UsageModelRow[];
  origins: UsageOriginRow[];
}

export async function getSettings(): Promise<AppSettings> {
  const response = await fetch('/api/settings');
  if (!response.ok) throw new Error(`Failed to load settings: ${response.statusText}`);
  return readWorkspaceResponse(response);
}

export async function saveSettings(update: SettingsUpdate): Promise<AppSettings> {
  const response = await fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!response.ok) {
    const error = new Error(await detailMessage(response, `Failed to save settings: ${response.statusText}`));
    throw error;
  }
  return readWorkspaceResponse(response);
}

export async function fetchModelCatalog(refresh = false): Promise<ModelCatalogPayload> {
  const response = await fetch(refresh ? '/api/models/refresh' : '/api/models', { method: refresh ? 'POST' : 'GET' });
  if (!response.ok) throw new Error(`Failed to load models: ${response.statusText}`);
  const data = await readWorkspaceResponse(response);
  return { ...data, models: Array.isArray(data.models) ? data.models : [] };
}

export async function fetchModelRequirements(model: string): Promise<ModelRequirements> {
  const response = await fetch(`/api/models/requirements?model=${encodeURIComponent(model)}`);
  if (!response.ok) throw new Error(`Failed to load model requirements: ${response.statusText}`);
  return readWorkspaceResponse(response);
}

export async function getUsageStats(): Promise<UsageStats> {
  const response = await fetch('/api/stats');
  if (!response.ok) throw new Error(`Failed to load statistics: ${response.statusText}`);
  return readWorkspaceResponse(response);
}
