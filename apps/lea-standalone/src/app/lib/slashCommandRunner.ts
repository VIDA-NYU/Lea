// Slash-command framework — the impure half: the handler map for 'action' commands (the
// ones that DO something in the UI/adapter rather than start a proof run). The pure parser
// + metadata registry live in slashCommands.js; this file wires each command name to what
// it does, touching stores + api. Adding an action command = one entry in SLASH_COMMANDS
// (metadata) + one handler here.

import { useProofSession, type CompactionPayload } from '../stores/proofSession';
import { useFactories } from '../stores/factories';
import { compactSession, type ChatMessage } from './api';
import { findSlashCommand } from './slashCommands.js';

// A local, client-only compaction marker (kind='compaction'). Used for the in-flight
// "Compacting…" card and the no-op notice — neither is persisted server-side, so both
// correctly vanish on reload. A real compaction returns a durable `message` instead.
function localCompaction(sessionId: string, id: string, payload: Partial<CompactionPayload>): ChatMessage {
  return {
    id,
    session_id: sessionId,
    role: 'assistant',
    kind: 'compaction',
    content: JSON.stringify({ manual: true, ...payload }),
    created_at: new Date().toISOString(),
  };
}

export interface SlashRunContext {
  sessionId?: string;
  args: string;
}

type Handler = (ctx: SlashRunContext) => Promise<void>;

// name → handler. Only 'action' commands appear here; 'prompt' commands are expanded into
// a normal run by the caller (they have no handler).
const HANDLERS: Record<string, Handler> = {
  // E0e: both open the per-session picker. They need a session because an override is
  // stored against one — before the first message there is nothing to attach it to.
  skills: async ({ sessionId }) => openPicker('skills', sessionId),
  mcp: async ({ sessionId }) => openPicker('mcp', sessionId),

  compact: async ({ sessionId }) => {
    const { setError, setMessages } = useProofSession.getState();
    if (!sessionId) {
      setError('Nothing to compact yet — start a proof first.');
      return;
    }
    // Show an in-flight "Compacting…" card immediately — the request can take seconds (it
    // may make an LLM summary call), and silence reads as "nothing happened". We swap this
    // pending marker for the real result (or remove it on failure) when the call returns.
    const pendingId = `compact-pending-${Date.now()}`;
    setMessages((cur) => [...cur, localCompaction(sessionId, pendingId, { pending: true })]);
    try {
      const r = await compactSession(sessionId);
      // The marker is a durable timeline message (kind='compaction'), so it survives a
      // reload like any message. A no-op returns no persisted message → a transient one.
      const marker = r.message ?? localCompaction(sessionId, `compact-local-${Date.now()}`, r);
      setMessages((cur) => {
        const swapped = cur.map((m) => (m.id === pendingId ? marker : m));
        // Guard against a rare duplicate id (the swap already placed the marker in position).
        return swapped.filter((m, i) => m.id !== marker.id || swapped.findIndex((x) => x.id === marker.id) === i);
      });
    } catch (err) {
      // Drop the pending card and surface the failure — never leave a spinner stuck.
      setMessages((cur) => cur.filter((m) => m.id !== pendingId));
      setError(err instanceof Error ? err.message : 'Failed to compact.');
    }
  },
};

function openPicker(kind: 'skills' | 'mcp', sessionId?: string) {
  if (!sessionId) {
    useProofSession.getState().setError(
      `Send a message first — ${kind === 'skills' ? 'skills' : 'MCP servers'} are chosen per conversation. ` +
      `Until then, set them on the project or in the Library.`,
    );
    return;
  }
  useFactories.getState().setSkillsMcpPicker(kind);
}

export interface SlashDispatch {
  handled: boolean; // false → not a known command (caller should surface an error)
  kind?: 'action' | 'prompt';
  template?: string; // for 'prompt' commands: the body to expand into a run
}

/**
 * Dispatch a parsed slash command. For an 'action' command, runs its handler and returns
 * { handled: true, kind: 'action' }. For a 'prompt' command, returns its template so the
 * caller can expand `$ARGUMENTS` and start a normal run. Unknown → { handled: false }.
 */
export async function runSlashCommand(name: string, ctx: SlashRunContext): Promise<SlashDispatch> {
  const cmd = findSlashCommand(name);
  if (!cmd) return { handled: false };
  if (cmd.kind === 'prompt') {
    return { handled: true, kind: 'prompt', template: cmd.template };
  }
  const handler = HANDLERS[cmd.name];
  if (!handler) return { handled: false };
  await handler(ctx);
  return { handled: true, kind: 'action' };
}
