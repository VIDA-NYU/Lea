// Blueprint-package API calls that lib/api.ts (integrator-owned) does not have yet.
// Same conventions as api.ts: fetch against /api, 202 commands resolved by polling
// the receipt at /api/workspace/commands/{id} through the shared workspace client.
// The integrator may fold these into api.ts at merge time.

import { workspaceResult } from '../../../../../packages/lea-workspace-client/index.mjs';
import type { ProjectGraph } from './types';

async function detailMessage(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => ({} as any));
  if (typeof body.detail === 'string') return body.detail;
  if (body.detail?.message) return body.detail.message;
  if (typeof body.message === 'string') return body.message;
  return fallback;
}

async function readWorkspaceResponse(response: Response): Promise<any> {
  const initial = await response.json();
  return workspaceResult(initial, async (id: string) => {
    const receipt = await fetch(`/api/workspace/commands/${encodeURIComponent(id)}`);
    if (!receipt.ok) {
      throw Object.assign(new Error(await detailMessage(receipt, 'Unable to read workspace receipt.')), { status: receipt.status });
    }
    return receipt.json();
  });
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
