// Blueprint-package API calls that lib/api.ts (integrator-owned) does not have yet.
// Same conventions as api.ts: fetch against /api, 202 commands resolved by polling
// the receipt at /api/workspace/commands/{id} through the shared workspace client.
// The integrator may fold these into api.ts at merge time.

import { submitWithRecovery, workspaceResult } from '../../../../../packages/lea-workspace-client/index.mjs';
import { RevisionConflictError } from './api';
import type {
  BlueprintAcceptResult,
  BlueprintAppliedNode,
  BlueprintDiscardResult,
  BlueprintRemoveResult,
  BlueprintUpsertNode,
  BlueprintUpsertResult,
  BlueprintWarning,
  ProjectGraph,
} from './types';

async function detailMessage(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => ({} as any));
  if (typeof body.detail === 'string') return body.detail;
  if (body.detail?.message) return body.detail.message;
  if (typeof body.message === 'string') return body.message;
  return fallback;
}

async function readWorkspaceResponse(response: Response): Promise<any> {
  const initial = await response.json();
  try {
    return await workspaceResult(initial, async (id: string) => {
      const receipt = await fetch(`/api/workspace/commands/${encodeURIComponent(id)}`);
      if (!receipt.ok) {
        throw Object.assign(new Error(await detailMessage(receipt, 'Unable to read workspace receipt.')), { status: receipt.status });
      }
      return receipt.json();
    });
  } catch (error: any) {
    if (error?.detail?.code === 'revision_conflict') throw new RevisionConflictError(error.message, error.detail);
    throw error;
  }
}

function newKey(prefix: string): string {
  return globalThis.crypto?.randomUUID?.() ?? `${prefix}-${Date.now()}-${Math.random()}`;
}

/**
 * A blueprint write: one JSON request with an idempotency key, recovered through
 * the receipts endpoint when the response is lost (the same pattern as the
 * project-doc PUT), then the 202 command resolved through its receipt.
 */
async function blueprintWrite(path: string, method: 'POST' | 'PUT', body: Record<string, unknown>, failure: string): Promise<any> {
  const key = String(body.idempotency_key || newKey('blueprint'));
  const serialized = JSON.stringify({ ...body, idempotency_key: key });
  const response = await submitWithRecovery(
    () => fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: serialized }),
    async () => {
      const receipt = await fetch(`/api/workspace/receipts?idempotency_key=${encodeURIComponent(key)}`);
      if (!receipt.ok) throw Object.assign(new Error('Unable to recover the blueprint receipt.'), { status: receipt.status });
      return receipt;
    },
  );
  if (!response.ok) {
    throw Object.assign(new Error(await detailMessage(response, `${failure}: ${response.statusText}`)), { status: response.status });
  }
  return readWorkspaceResponse(response);
}

const base = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/blueprint`;

/** `applied` entries per §2.2 are objects; a bare key is accepted too. */
function appliedList(value: unknown): BlueprintAppliedNode[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((a: any) =>
      typeof a === 'string'
        ? { key: a, kind: null, lean: null, file: null, uses: [], status: 'planned' as const }
        : a && typeof a.key === 'string'
          ? { kind: null, lean: null, file: null, uses: [], status: 'planned' as const, ...a }
          : null,
    )
    .filter((a): a is BlueprintAppliedNode => a !== null);
}

/**
 * POST /api/projects/{id}/blueprint/nodes — create or update nodes through the
 * store (`project.blueprint_upsert`). The system assigns `lean` and `file`;
 * rejected nodes come back in `rejected` with a code and message.
 */
export async function upsertBlueprintNodes(
  projectId: string,
  nodes: BlueprintUpsertNode[],
  options: { draft?: boolean } = {},
): Promise<BlueprintUpsertResult> {
  const data = await blueprintWrite(`${base(projectId)}/nodes`, 'POST', options.draft ? { nodes, draft: true } : { nodes }, 'Failed to save the blueprint nodes');
  return {
    applied: appliedList(data?.applied),
    rejected: Array.isArray(data?.rejected) ? data.rejected : [],
    ready: Array.isArray(data?.ready) ? data.ready : [],
    counts: data?.counts ?? { total: 0, proved: 0, ready: 0, stated: 0, failed: 0, planned: 0 },
    draft: Boolean(data?.draft),
    commit_sha: data?.commit_sha ?? null,
  };
}

/** POST /api/projects/{id}/blueprint/nodes/remove — `project.blueprint_remove`. */
export async function removeBlueprintNodes(
  projectId: string,
  keys: string[],
  options: { cascade?: boolean } = {},
): Promise<BlueprintRemoveResult> {
  const data = await blueprintWrite(`${base(projectId)}/nodes/remove`, 'POST', options.cascade ? { keys, cascade: true } : { keys }, 'Failed to remove the blueprint nodes');
  return {
    removed: Array.isArray(data?.removed) ? data.removed : [],
    rejected: Array.isArray(data?.rejected) ? data.rejected : [],
    commit_sha: data?.commit_sha ?? null,
  };
}

/**
 * POST /api/projects/{id}/blueprint/draft/accept — merge the selected draft nodes
 * (all of them when `keys` is omitted) into the live blueprint and delete the draft
 * (`project.blueprint_accept_draft`).
 */
export async function acceptBlueprintDraft(projectId: string, keys?: string[]): Promise<BlueprintAcceptResult> {
  const data = await blueprintWrite(`${base(projectId)}/draft/accept`, 'POST', keys ? { keys } : {}, 'Failed to accept the draft');
  return {
    applied: appliedList(data?.applied),
    rejected: Array.isArray(data?.rejected) ? data.rejected : [],
    commit_sha: data?.commit_sha ?? null,
  };
}

/** POST /api/projects/{id}/blueprint/draft/discard — delete the draft (`project.blueprint_discard_draft`). */
export async function discardBlueprintDraft(projectId: string): Promise<BlueprintDiscardResult> {
  const data = await blueprintWrite(`${base(projectId)}/draft/discard`, 'POST', {}, 'Failed to discard the draft');
  return { discarded: Boolean(data?.discarded) };
}

/**
 * PUT /api/projects/{id}/blueprint/draft — the human edit of the draft markdown,
 * with the live PUT's revision contract: `file_revision` is the base the edit
 * was made on; a stale base fails with RevisionConflictError. The server
 * normalizes and returns the canonical text.
 */
export async function putBlueprintDraft(
  projectId: string,
  content: string,
  fileRevision?: string,
): Promise<{ content: string; warnings: BlueprintWarning[]; file_revision?: string }> {
  const key = newKey('blueprint-draft');
  const data = await blueprintWrite(
    `${base(projectId)}/draft`,
    'PUT',
    { content, file_revision: fileRevision, idempotency_key: key, draft_id: key },
    'Failed to save the draft',
  );
  return {
    content: typeof data?.content === 'string' ? data.content : content,
    warnings: Array.isArray(data?.warnings) ? data.warnings : [],
    file_revision: typeof data?.file_revision === 'string' ? data.file_revision : undefined,
  };
}

export interface BlueprintGenerateResult {
  /** Nodes appended to .lea/blueprint.md (0 when nothing new could be seeded). */
  added: number;
  /** Artifacts already represented or unresolvable. */
  skipped: number;
  warnings: string[];
  /** The freshly derived graph, so a caller can render in one round-trip. */
  graph: ProjectGraph;
}

/**
 * POST /api/projects/{id}/blueprint/generate — seeds `.lea/blueprint.md` with one
 * node per formalized declaration the project already has (additive, idempotent).
 * A 202 command: the receipt is polled until it settles. Note the adapter writes
 * the file directly; the UI treats the written content as the proposal.
 */
export async function generateProjectBlueprint(projectId: string): Promise<BlueprintGenerateResult> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/blueprint/generate`, { method: 'POST' });
  if (!response.ok) {
    throw Object.assign(
      new Error(await detailMessage(response, `Failed to draft the blueprint: ${response.statusText}`)),
      { status: response.status },
    );
  }
  const data = await readWorkspaceResponse(response);
  return {
    added: Number(data?.added ?? 0),
    skipped: Number(data?.skipped ?? 0),
    warnings: Array.isArray(data?.warnings) ? data.warnings.map(String) : [],
    graph: {
      nodes: Array.isArray(data?.graph?.nodes) ? data.graph.nodes : [],
      edges: Array.isArray(data?.graph?.edges) ? data.graph.edges : [],
    },
  };
}
