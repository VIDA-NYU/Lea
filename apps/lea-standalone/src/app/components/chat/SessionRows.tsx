import { useState } from 'react';
import { interruptSubagent } from '../../lib/api';
import type { Diagnostic, SessionSummary } from '../../lib/api';
import { firstLine, parseTime, toolRowModel, type Tone } from '../../lib/chatModel.js';
import { useProofSession } from '../../stores/proofSession';
import { MarkdownMessage } from '../MarkdownMessage';
import { DiagnosticCard } from './DiagnosticCard';
import { ToolRow } from './ToolRow';

/**
 * The sub-agents a coordinator spawned at one point in the thread, one session
 * row each: title, the turn a running child is on (with its elapsed time), and
 * its verdict once finished. Clicking a row opens the child session; the
 * chevron expands it in place to the delegated task and its output.
 */
export function SessionRows({
  children,
  onSelectSession,
  diagnosticsByChild,
}: {
  children: SessionSummary[];
  onSelectSession?: (id: string) => void;
  diagnosticsByChild?: Map<string, Diagnostic[]>;
}) {
  return (
    <div className="chat-sessions">
      {children.map((child) => (
        <SessionRow
          key={child.id}
          child={child}
          onSelectSession={onSelectSession}
          diagnostics={diagnosticsByChild?.get(child.id) ?? []}
        />
      ))}
    </div>
  );
}

function SessionRow({
  child,
  onSelectSession,
  diagnostics,
}: {
  child: SessionSummary;
  onSelectSession?: (id: string) => void;
  diagnostics: Diagnostic[];
}) {
  const [expanded, setExpanded] = useState(false);
  const [stopping, setStopping] = useState(false);
  // E1: ephemeral live state per running child, fed by `subagent_progress` SSE.
  const live = useProofSession((s) => s.subagentProgress[child.id]);
  // A child that could not run (API/config error, crash) → the real error message.
  const error = useProofSession((s) => s.subagentErrors[child.id]);
  // D4: spawned but still waiting for a concurrency slot — not yet exploring.
  const isQueued = useProofSession((s) => Boolean(s.subagentQueued[child.id]));
  // D3: ran, but stopped short (budget / stopped / no candidate).
  const stopNotice = useProofSession((s) => s.subagentStopNotices[child.id]);

  const badge = error
    ? { text: 'failed', tone: 'error' as Tone, running: false }
    : isQueued
      ? { text: 'queued', tone: 'muted' as Tone, running: false }
      : subagentBadge(child);
  const running = !error && !isQueued && badge.running;
  const label = running ? (live?.turn ? `turn ${live.turn}` : 'exploring…') : stopNotice && !error ? 'stopped' : badge.text;
  const view = {
    ...toolRowModel({ name: 'session', target: child.title }, { label, tone: badge.tone }),
    verb: 'sub-agent',
  };
  // While running, the live feed IS the preview: the current tool, else the
  // streaming narration. A failed child shows its error; else the final summary.
  const preview = isQueued
    ? 'waiting for a free slot…'
    : running
      ? live?.tool ? `running ${live.tool}…` : firstLine(live?.text, 140)
      : error
        ? firstLine(error, 140)
        // D3: a child that stopped short leads with WHY, not with a summary that
        // makes a truncated run look like a considered conclusion.
        : stopNotice
          ? `Stopped — ${stopNotice}`
          : firstLine(child.final_summary, 140);
  const created = parseTime(child.created_at);
  const ended = parseTime(child.ended_at) ?? (child.duration_seconds && created != null ? created + child.duration_seconds * 1000 : null);
  const stop = () => {
    setStopping(true);
    interruptSubagent(child.id).catch(() => {}); // 404 (already done) is fine; the row settles on finish
  };
  return (
    <ToolRow
      view={view}
      state={running ? 'live' : 'done'}
      startedAt={created}
      endedAt={running ? null : ended}
      meta={child.role ? <span className="chat-tool-meta">{child.role.split('-')[0]}</span> : null}
      note={preview || null}
      noteTone={error ? 'error' : running ? 'running' : 'muted'}
      onOpen={onSelectSession ? () => onSelectSession(child.id) : undefined}
      expanded={expanded}
      onToggle={() => setExpanded((v) => !v)}
      title={onSelectSession ? 'Open this session' : undefined}
      extra={
        running ? (
          <button
            type="button"
            className="chat-btn small"
            disabled={stopping}
            onClick={stop}
            title="Stop this sub-agent (the coordinator keeps running)"
          >
            {stopping ? 'Stopping…' : 'Stop'}
          </button>
        ) : null
      }
    >
      {/* What the coordinator asked for, first — and for a RUNNING child too: one
          with no output yet is only judgeable by its task. */}
      {child.task && (
        <div className="chat-detail-block">
          <div className="chat-detail-label">Delegated task</div>
          <pre className="chat-detail-pre prose">{child.task}</pre>
        </div>
      )}
      {error ? (
        <div className="chat-detail-block">
          <div className="chat-detail-label error">This sub-agent could not run</div>
          <pre className="chat-detail-pre">{error}</pre>
        </div>
      ) : (
        <>
          {stopNotice && <div className="chat-detail-stopped">This sub-agent {stopNotice}.</div>}
          {child.final_summary ? (
            <div className="chat-prose">
              <MarkdownMessage content={child.final_summary} />
            </div>
          ) : (
            <p className="chat-detail-facts">
              {isQueued
                ? 'Waiting for a free slot — not started yet.'
                : running
                  ? 'Still working — no output yet.'
                  : 'This sub-agent produced no final output.'}
            </p>
          )}
        </>
      )}
      {/* Failures the child itself reported, persisted against its own session. No
          settings jump here: the child is read-only, its failures are the
          coordinator's to act on. */}
      {diagnostics.map((d) => (
        <DiagnosticCard key={`kd:${d.id ?? d.code}`} diagnostic={d} />
      ))}
      {onSelectSession && (
        <button type="button" className="chat-link" onClick={() => onSelectSession(child.id)}>
          Open full session →
        </button>
      )}
    </ToolRow>
  );
}

// The compiler's verdict on a sub-agent's candidate (item 24). The ranking that
// picks a winner is the adapter's job (item 25); this is each child's own check.
export function subagentBadge(child: SessionSummary): { text: string; tone: Tone; running: boolean } {
  const ok =
    child.latest_check_status === 'ok' ||
    child.status === 'ok' ||
    child.status === 'proved' ||
    child.status === 'defined';
  if (ok) return { text: 'compiles', tone: 'proved', running: false };
  if (child.status === 'error' || child.latest_check_status === 'error') {
    return { text: 'errors', tone: 'error', running: false };
  }
  if ((child.active_run_count ?? 0) > 0 || child.status === 'running') {
    return { text: 'exploring…', tone: 'running', running: true };
  }
  return { text: 'no candidate', tone: 'muted', running: false };
}
