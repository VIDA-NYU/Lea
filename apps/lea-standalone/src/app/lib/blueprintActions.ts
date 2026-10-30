// Blueprint actions — the impure side of the blueprint package, shared by the
// `/blueprint` slash-command handlers (slashCommandRunner.ts) and the view's
// buttons (Prove, the proposal flow). Everything that talks to stores or the API
// for the blueprint lives here; the pure derivations are in blueprintModel.js.

import {
  createRun,
  createSessionInProject,
  getProject,
  getProjectBlueprint,
  getProjectDocSnapshot,
  getProjectGraph,
  putProjectDoc,
  type GraphNode,
  type ProjectGraph,
} from './api';
import { generateProjectBlueprint } from './blueprintApi';
import {
  bindingPrompt,
  draftingPrompt,
  nodeOriginKey,
  nodeSessionTitle,
  parseBlueprintMarkdown,
  readyNodes,
  runningNodeKeys,
  serializeBlueprint,
  type BlueprintMdNode,
} from './blueprintModel.js';
import { useModel } from '../stores/model';
import { useProjects } from '../stores/projects';
import { useSessions } from '../stores/sessions';
import { BLUEPRINT_PATH, useWorkbench } from '../stores/workbench';

export interface StartedNodeSession {
  sessionId: string;
  runId: string;
  nodeKey: string;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * Create a session bound to `node` and start its first run. The run's first
 * message states the node key, its statement, the target file and every
 * dependency's status (blueprintModel.bindingPrompt). The formalization carries
 * `origin_key = blueprint:<key>` so re-proving the node reuses it. The sessions
 * list is refreshed afterwards so the new session shows up in the sidebar.
 */
export async function startNodeSession(projectId: string, node: GraphNode, graph: ProjectGraph): Promise<StartedNodeSession> {
  const session = await createSessionInProject(projectId, nodeSessionTitle(node.key));
  const wb = useWorkbench.getState();
  wb.bindSessionToNode(session.id, node.key);
  try {
    const run = await createRun(bindingPrompt(node, graph.nodes), session.id, useModel.getState().model, {
      new_formalization: {
        display_title: node.key,
        kind: node.kind ?? 'theorem',
        declaration_name: node.lean ?? undefined,
        statement: node.statement || undefined,
        origin: 'ui',
        origin_key: nodeOriginKey(node.key),
      },
      idempotency_key: globalThis.crypto?.randomUUID?.() ?? `blueprint-${Date.now()}-${Math.random()}`,
    });
    return { sessionId: run.session_id, runId: run.run_id, nodeKey: node.key };
  } finally {
    void useSessions.getState().refreshSessions().catch(() => {});
    useWorkbench.getState().bumpBlueprintRefresh();
  }
}

export interface ReadySessionsResult {
  started: StartedNodeSession[];
  /** Ready nodes skipped because a bound session is already running. */
  alreadyRunning: string[];
  /** Nodes whose session could not be started, with the reason. */
  failed: { key: string; error: string }[];
  /** Nothing was ready to begin with. */
  nothingReady: boolean;
}

/** `/blueprint next`: one session per ready node without a live session, sequentially. */
export async function startReadySessions(projectId: string): Promise<ReadySessionsResult> {
  const graph = await getProjectGraph(projectId);
  const running = runningNodeKeys(graph.nodes, useSessions.getState().sessions, useWorkbench.getState().nodeBindings);
  const ready = readyNodes(graph.nodes);
  const todo = ready.filter((n) => !running.has(n.key));
  const result: ReadySessionsResult = {
    started: [],
    alreadyRunning: ready.filter((n) => running.has(n.key)).map((n) => n.key),
    failed: [],
    nothingReady: ready.length === 0,
  };
  for (const node of todo) {
    try {
      result.started.push(await startNodeSession(projectId, node, graph));
    } catch (err) {
      result.failed.push({ key: node.key, error: errorText(err) });
    }
  }
  return result;
}

/** `/blueprint prove <key>`: a session bound to that node. Keys match case-insensitively as a fallback. */
export async function proveNode(projectId: string, key: string): Promise<StartedNodeSession> {
  const graph = await getProjectGraph(projectId);
  const node =
    graph.nodes.find((n) => n.key === key) ?? graph.nodes.find((n) => n.key.toLowerCase() === key.toLowerCase());
  if (!node) throw new Error(`No blueprint node named "${key}". Open the blueprint to see the node keys.`);
  if (node.status === 'proved') throw new Error(`${node.key} is already proved — pick another node.`);
  const running = runningNodeKeys(graph.nodes, useSessions.getState().sessions, useWorkbench.getState().nodeBindings);
  if (running.has(node.key)) throw new Error(`A session is already proving ${node.key}. Open it from the blueprint.`);
  return startNodeSession(projectId, node, graph);
}

export type OpenBlueprintMode = 'opened' | 'proposal' | 'drafting-run';

/**
 * `/blueprint`: show the blueprint in the canvas; when it has no nodes, draft one.
 * Drafting first asks the adapter to seed nodes from the project's formalized
 * declarations (`/blueprint/generate`). If that added nothing (a project with no
 * proofs yet), it queues a drafting prompt for Lea through `pendingSubmission` —
 * App submits it in the open session — and marks the proposal `drafting` so the
 * view waits for the run to write the file, then presents it for review.
 */
export async function openOrDraftBlueprint(projectId: string, options: { sessionId?: string } = {}): Promise<OpenBlueprintMode> {
  const wb = useWorkbench.getState();
  wb.openFile(BLUEPRINT_PATH);
  wb.setBlueprintNotice(null);
  const current = await getProjectBlueprint(projectId);
  if (parseBlueprintMarkdown(current.content).nodes.length > 0) {
    if (wb.blueprintProposal?.projectId !== projectId) wb.setBlueprintProposal(null);
    return 'opened';
  }
  wb.setBlueprintProposal({ projectId, phase: 'drafting', source: 'artifacts' });
  let seeded;
  try {
    seeded = await generateProjectBlueprint(projectId);
  } catch (err) {
    useWorkbench.getState().setBlueprintProposal({ projectId, phase: 'drafting', source: 'artifacts', error: errorText(err) });
    throw err;
  }
  if (seeded.added > 0) {
    useWorkbench.getState().setBlueprintProposal({ projectId, phase: 'review', source: 'artifacts', notes: seeded.warnings });
    useWorkbench.getState().bumpBlueprintRefresh();
    return 'proposal';
  }
  const cached = useProjects.getState().currentProject;
  const project = cached?.id === projectId ? cached : await getProject(projectId).catch(() => ({ title: '', description: null }));
  useWorkbench.getState().setBlueprintProposal({
    projectId,
    phase: 'drafting',
    source: 'agent',
    draftingSessionId: options.sessionId,
    notes: seeded.warnings,
  });
  // Scope 'project': the drafting run writes .lea/blueprint.md, it is not a
  // formalization (scope inference would read "prove" in the prompt as one).
  useWorkbench.getState().setPendingSubmission({ text: draftingPrompt(project), sessionId: options.sessionId, scope: 'project' });
  return 'drafting-run';
}

/** Accept the proposal: write the checked nodes back as the blueprint (preamble kept). */
export async function acceptProposal(projectId: string, nodes: BlueprintMdNode[], preamble: string): Promise<void> {
  const snapshot = await getProjectDocSnapshot(projectId, 'blueprint');
  await putProjectDoc(projectId, 'blueprint', serializeBlueprint(nodes, { preamble }), snapshot.file_revision);
  const wb = useWorkbench.getState();
  wb.setBlueprintProposal(null);
  wb.bumpBlueprintRefresh();
}

/** Discard the proposal: empty the blueprint again (the seeder wrote it directly). */
export async function discardProposal(projectId: string): Promise<void> {
  const snapshot = await getProjectDocSnapshot(projectId, 'blueprint');
  if (snapshot.content.trim()) await putProjectDoc(projectId, 'blueprint', '', snapshot.file_revision);
  const wb = useWorkbench.getState();
  wb.setBlueprintProposal(null);
  wb.setPendingSubmission(null);
  wb.bumpBlueprintRefresh();
}

/** Resolve the project a command applies to: explicit, the session's, or the open project. */
export function resolveProjectId(sessionId?: string, explicit?: string): string | undefined {
  if (explicit) return explicit;
  const fromSession = sessionId ? useSessions.getState().sessions.find((s) => s.id === sessionId)?.project_id : undefined;
  if (fromSession) return fromSession;
  const projects = useProjects.getState();
  return projects.selectedProjectId ?? projects.currentProject?.id;
}
