// Blueprint actions — the impure side of the blueprint package, shared by the
// `/blueprint` slash-command handlers (slashCommandRunner.ts), the view's
// buttons (Prove, the proposal flow) and the run stream (useProofStream).
// Everything that talks to stores or the API for the blueprint lives here; the
// pure derivations are in blueprintModel.js. Contract:
// docs/PLAN-blueprint-tools.md.

import {
  createRun,
  createSessionInProject,
  getProjectBlueprint,
  getProjectDocSnapshot,
  getProjectGraph,
  interruptRun,
  putProjectDoc,
  type GraphNode,
  type ProjectGraph,
} from './api';
import {
  acceptBlueprintDraft,
  discardBlueprintDraft,
  generateProjectBlueprint,
} from './blueprintApi';
import {
  autoNotice,
  autoRecord,
  autoStep,
  bindingPrompt,
  blueprintEventNotification,
  createAutoState,
  draftingPrompt,
  nodeOriginKey,
  nodeSessionTitle,
  parseBlueprintMarkdown,
  readyNodes,
  runningNodeKeys,
  serializeBlueprint,
  stopAuto,
  type AutoState,
  type BlueprintBlock,
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

function newIdempotencyKey(prefix: string): string {
  return globalThis.crypto?.randomUUID?.() ?? `${prefix}-${Date.now()}-${Math.random()}`;
}

/**
 * Create a session bound to `node` (`blueprint_node = node.key`) and start its
 * first run. The message only names the node; the server supplies the node's
 * statement, assigned name, file and dependency status (PLAN §2.7) and binds
 * the names at run start. The UI binding is still recorded as the fallback for a
 * server that does not store `blueprint_node`. The formalization carries
 * `origin_key = blueprint:<key>` so re-proving the node reuses it. The sessions
 * list is refreshed afterwards so the new session shows up in the sidebar.
 */
export async function startNodeSession(projectId: string, node: GraphNode, _graph?: ProjectGraph): Promise<StartedNodeSession> {
  const session = await createSessionInProject(projectId, nodeSessionTitle(node.key), node.key);
  useWorkbench.getState().bindSessionToNode(session.id, node.key);
  try {
    const run = await createRun(bindingPrompt(node), session.id, useModel.getState().model, {
      new_formalization: {
        display_title: node.key,
        kind: node.kind ?? 'theorem',
        declaration_name: node.lean ?? undefined,
        statement: node.statement || undefined,
        origin: 'ui',
        origin_key: nodeOriginKey(node.key),
      },
      idempotency_key: newIdempotencyKey('blueprint'),
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

/** The notice `/blueprint next` and the "Prove next" button show for a result. */
export function readySessionsNotice(r: ReadySessionsResult): string {
  const n = r.started.length;
  const parts: string[] = [];
  if (n > 0) parts.push(`Started ${n} ${n === 1 ? 'session' : 'sessions'}: ${r.started.map((s) => s.nodeKey).join(', ')}`);
  if (r.alreadyRunning.length) parts.push(`already running: ${r.alreadyRunning.join(', ')}`);
  if (r.failed.length) parts.push(`could not start ${r.failed.map((f) => `${f.key} (${f.error})`).join('; ')}`);
  if (r.nothingReady) parts.push('No node is ready: every unproved node waits on a dependency, or the blueprint is empty.');
  return parts.join(' · ');
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

/** Session title of a drafting run started without an open session. */
export const DRAFTING_SESSION_TITLE = 'Draft the blueprint';

/**
 * `/blueprint`: show the blueprint in the canvas; when it has no nodes, draft one.
 *
 * 1. A pending draft (`GET /blueprint` → `draft`) opens for review.
 * 2. A blueprint with nodes just opens.
 * 3. Otherwise the adapter first seeds nodes from the project's formalized
 *    declarations (`/blueprint/generate`, written into the live file); those
 *    are reviewed as an `artifacts` proposal.
 * 4. When that added nothing, a drafting run starts in `sessionId` (else in a
 *    new project session): `purpose: 'blueprint_draft'`, project scope (no
 *    formalization), message `draftingPrompt()`. Its blueprint_upsert calls
 *    land in `.lea/blueprint.draft.md`; the proposal stays `drafting` until the
 *    run settles, then BlueprintView opens the review. `openSession` shows the
 *    run in the chat (App's loadSession), which also attaches its stream.
 */
export async function openOrDraftBlueprint(
  projectId: string,
  options: { sessionId?: string; openSession?: (sessionId: string) => void } = {},
): Promise<OpenBlueprintMode> {
  const wb = useWorkbench.getState();
  wb.openFile(BLUEPRINT_PATH);
  wb.setBlueprintNotice(null);
  const current = await getProjectBlueprint(projectId);
  if (current.draft) {
    const existing = useWorkbench.getState().blueprintProposal;
    const stillDrafting =
      existing?.projectId === projectId && existing.source === 'agent' && existing.phase === 'drafting' && !existing.error;
    if (!stillDrafting) useWorkbench.getState().setBlueprintProposal({ projectId, phase: 'review', source: 'agent' });
    return 'proposal';
  }
  const liveNodes = current.nodes.length || parseBlueprintMarkdown(current.content).nodes.length;
  if (liveNodes > 0) {
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
  const notes = seeded.warnings;
  // Draft in the given session only when it belongs to this project (a session not
  // listed yet is trusted); otherwise in a new project session.
  const listed = options.sessionId ? useSessions.getState().sessions.find((x) => x.id === options.sessionId) : undefined;
  const target = options.sessionId && (!listed || listed.project_id === projectId) ? options.sessionId : undefined;
  useWorkbench.getState().setBlueprintProposal({ projectId, phase: 'drafting', source: 'agent', draftingSessionId: target, notes });
  try {
    const sessionId = target ?? (await createSessionInProject(projectId, DRAFTING_SESSION_TITLE)).id;
    // Project scope: no focus_formalization_id / new_formalization — the run
    // drafts the plan, it is not a formalization.
    const run = await createRun(draftingPrompt(), sessionId, useModel.getState().model, {
      purpose: 'blueprint_draft',
      idempotency_key: newIdempotencyKey('blueprint-draft'),
    });
    useWorkbench.getState().setBlueprintProposal({
      projectId,
      phase: 'drafting',
      source: 'agent',
      draftingSessionId: run.session_id,
      draftingRunId: run.run_id,
      notes,
    });
    void useSessions.getState().refreshSessions().catch(() => {});
    options.openSession?.(run.session_id);
  } catch (err) {
    useWorkbench.getState().setBlueprintProposal({
      projectId, phase: 'drafting', source: 'agent', draftingSessionId: target, notes, error: errorText(err),
    });
    throw err;
  }
  return 'drafting-run';
}

/** Close the proposal after a write and refetch every blueprint reader. */
function closeProposal(notice: string | null) {
  const wb = useWorkbench.getState();
  wb.setBlueprintProposal(null);
  wb.bumpBlueprintRefresh();
  if (notice !== null) wb.setBlueprintNotice(notice);
}

export interface AcceptDraftOutcome {
  applied: string[];
  rejected: { key: string; code: string; message: string }[];
}

/**
 * Accept the agent's draft: merge the checked nodes (`keys`) into the live
 * blueprint and delete the draft (POST /blueprint/draft/accept). Nodes the store
 * rejects are reported in the notice.
 */
export async function acceptDraft(projectId: string, keys: string[]): Promise<AcceptDraftOutcome> {
  const result = await acceptBlueprintDraft(projectId, keys);
  const applied = result.applied.map((a) => a.key);
  const parts = [`Accepted ${applied.length} ${applied.length === 1 ? 'node' : 'nodes'} into the blueprint.`];
  if (result.rejected.length) parts.push(`Rejected: ${result.rejected.map((r) => `${r.key} (${r.message || r.code})`).join('; ')}`);
  closeProposal(parts.join(' '));
  return { applied, rejected: result.rejected };
}

/** Discard the agent's draft: delete `.lea/blueprint.draft.md` (POST /blueprint/draft/discard). */
export async function discardDraft(projectId: string): Promise<void> {
  await discardBlueprintDraft(projectId);
  useWorkbench.getState().setPendingSubmission(null);
  closeProposal('Discarded the draft.');
}

/**
 * Cancel a drafting proposal: stop the drafting run when this page started it,
 * delete whatever draft it wrote, close the proposal.
 */
export async function cancelDrafting(projectId: string, runId?: string): Promise<void> {
  if (runId) await interruptRun(runId).catch(() => {});
  const current = await getProjectBlueprint(projectId).catch(() => null);
  if (current?.draft) await discardBlueprintDraft(projectId);
  closeProposal(null);
}

/**
 * Accept an `artifacts` proposal (the seeder wrote the live file): write the
 * checked nodes back as the blueprint, preamble and prose sections kept.
 */
export async function acceptProposal(
  projectId: string,
  nodes: BlueprintMdNode[],
  preamble: string,
  prose?: BlueprintBlock[],
): Promise<void> {
  const snapshot = await getProjectDocSnapshot(projectId, 'blueprint');
  await putProjectDoc(projectId, 'blueprint', serializeBlueprint(nodes, { preamble, prose }), snapshot.file_revision);
  closeProposal(null);
}

/** Discard an `artifacts` proposal: empty the blueprint again (the seeder wrote it directly). */
export async function discardProposal(projectId: string): Promise<void> {
  const snapshot = await getProjectDocSnapshot(projectId, 'blueprint');
  if (snapshot.content.trim()) await putProjectDoc(projectId, 'blueprint', '', snapshot.file_revision);
  useWorkbench.getState().setPendingSubmission(null);
  closeProposal(null);
}

/** Resolve the project a command applies to: explicit, the session's, or the open project. */
export function resolveProjectId(sessionId?: string, explicit?: string): string | undefined {
  if (explicit) return explicit;
  const fromSession = sessionId ? useSessions.getState().sessions.find((s) => s.id === sessionId)?.project_id : undefined;
  if (fromSession) return fromSession;
  const projects = useProjects.getState();
  return projects.selectedProjectId ?? projects.currentProject?.id;
}

// ── Run-stream events ──────────────────────────────────────────────────────

/**
 * One blueprint event from a run stream (`blueprint_updated`,
 * `blueprint_node_changed`, `blueprint_unlocked`; PLAN §2.4): reduce it into
 * the workbench store, and raise a desktop notification for an unlock while the
 * window is hidden or unfocused (window.leaDesktop is absent in a browser).
 */
export function handleBlueprintStreamEvent(type: string, payload: Record<string, unknown>): void {
  const event = { ...payload, type };
  useWorkbench.getState().applyBlueprintEvent(event);
  const notification = blueprintEventNotification(event);
  if (!notification || typeof window === 'undefined' || typeof document === 'undefined') return;
  const bridge = window.leaDesktop;
  if (!bridge?.notify) return;
  if (!(document.hidden || !document.hasFocus())) return;
  bridge.notify(notification).catch(() => {});
}

// ── /blueprint auto ────────────────────────────────────────────────────────
//
// One loop per page. It steps (blueprintModel.autoStep) at start, on every
// `blueprint_unlocked` the stream delivers for its project, and on a poll — the
// poll is what notices unlocks from background sessions, whose streams this
// page does not follow. Each step starts the nodes autoStep picks and reports
// through the blueprint notice; the state is mirrored in workbench.blueprintAuto.

const AUTO_POLL_MS = 8000;

interface AutoController {
  state: AutoState;
  timer: ReturnType<typeof setInterval> | null;
  unsubscribe: () => void;
  busy: boolean;
  again: boolean;
}

let autoLoop: AutoController | null = null;

function publishAuto(c: AutoController) {
  useWorkbench.getState().setBlueprintAuto(c.state);
}

function teardownAuto(c: AutoController) {
  if (c.timer) clearInterval(c.timer);
  c.timer = null;
  c.unsubscribe();
  if (autoLoop === c) autoLoop = null;
}

async function stepAuto(c: AutoController, first = false): Promise<void> {
  if (autoLoop !== c || !c.state.active) return;
  if (c.busy) {
    c.again = true;
    return;
  }
  c.busy = true;
  try {
    do {
      c.again = false;
      const graph = await getProjectGraph(c.state.projectId);
      if (autoLoop !== c || !c.state.active) return;
      const running = runningNodeKeys(graph.nodes, useSessions.getState().sessions, useWorkbench.getState().nodeBindings);
      const decided = autoStep(c.state, { nodes: graph.nodes, running, now: Date.now() });
      c.state = decided.state;
      const started: string[] = [];
      const failed: { key: string; error: string }[] = [];
      for (const key of decided.start) {
        if (autoLoop !== c) break;
        const node = graph.nodes.find((n) => n.key === key);
        if (!node) continue;
        try {
          await startNodeSession(c.state.projectId, node, graph);
          started.push(key);
        } catch (err) {
          failed.push({ key, error: errorText(err) });
        }
      }
      c.state = autoRecord(c.state, started, failed.map((f) => f.key));
      const notice = autoNotice(c.state, { started, failed, waiting: first ? running.size : 0 });
      if (notice) useWorkbench.getState().setBlueprintNotice(notice);
      publishAuto(c);
      if (!c.state.active) teardownAuto(c);
      first = false;
    } while (c.again && c.state.active && autoLoop === c);
  } catch (err) {
    if (autoLoop === c) useWorkbench.getState().setBlueprintNotice(`Auto: could not read the blueprint (${errorText(err)}); trying again.`);
  } finally {
    c.busy = false;
  }
}

/**
 * `/blueprint auto`: start a bound session for every ready node now, and again
 * for each later unlock, up to 20 sessions; ends on its own when every node is
 * proved, or nothing is ready and nothing is running. A loop already running is
 * replaced.
 */
export async function startBlueprintAuto(projectId: string): Promise<AutoState> {
  if (autoLoop) stopBlueprintAuto();
  const c: AutoController = { state: createAutoState(projectId), timer: null, unsubscribe: () => {}, busy: false, again: false };
  autoLoop = c;
  c.unsubscribe = useWorkbench.subscribe((s, prev) => {
    const unlock = s.blueprintUnlock;
    if (!unlock || unlock === prev.blueprintUnlock) return;
    if (unlock.projectId && unlock.projectId !== projectId) return;
    void stepAuto(c);
  });
  c.timer = setInterval(() => void stepAuto(c), AUTO_POLL_MS);
  publishAuto(c);
  await stepAuto(c, true);
  return c.state;
}

/** `/blueprint stop`: end the auto loop. Returns its final state, or null when none ran. */
export function stopBlueprintAuto(): AutoState | null {
  const c = autoLoop;
  if (!c) return null;
  c.state = stopAuto(c.state, 'user');
  teardownAuto(c);
  publishAuto(c);
  const notice = autoNotice(c.state);
  if (notice) useWorkbench.getState().setBlueprintNotice(notice);
  return c.state;
}

/** Whether `/blueprint auto` is running (for `/blueprint stop` feedback). */
export function blueprintAutoActive(): boolean {
  return !!autoLoop && autoLoop.state.active;
}
