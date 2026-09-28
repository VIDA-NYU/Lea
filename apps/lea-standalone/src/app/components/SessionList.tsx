import { Ban, CheckCircle, XCircle, Circle, CircleDashed, Loader2 } from 'lucide-react';
import type { SessionSummary } from '../lib/api';

// v2 session status is derived from the latest code step's verdict plus its run
// outcome when the latest step is agent-authored.
function StatusIcon({ status }: { status: SessionSummary['status'] }) {
  switch (status) {
    case 'running':
      return <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />;
    case 'proved':
    case 'defined':
    case 'ok':
      return <CheckCircle className="h-4 w-4 text-green-600" />;
    case 'disproved':
      return <Ban className="h-4 w-4 text-amber-600" />;
    case 'needs_review':
      return <CircleDashed className="h-4 w-4 text-amber-600" aria-label="Needs review" />;
    case 'error':
      return <XCircle className="h-4 w-4 text-destructive" />;
    case 'unchecked':
      return <Circle className="h-4 w-4 text-muted-foreground" />;
    default:
      return <CircleDashed className="h-4 w-4 text-muted-foreground/60" />;
  }
}

export function SessionList({
  sessions,
  selectedSessionId,
  onSelectSession,
  onNewSession,
}: {
  sessions: SessionSummary[];
  selectedSessionId?: string;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
}) {
  return (
    <div className="flex h-full flex-col border-r border-sidebar-border bg-sidebar">
      <div className="border-b border-sidebar-border p-4">
        <h2 className="text-sidebar-foreground">Sessions</h2>
      </div>

      <div className="flex-1 overflow-y-auto">
        {sessions.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground">No saved sessions yet.</div>
        ) : (
          sessions.map((session) => (
            <button
              key={session.id}
              onClick={() => onSelectSession(session.id)}
              className={`w-full border-b border-sidebar-border p-4 text-left transition-colors hover:bg-sidebar-accent ${
                selectedSessionId === session.id ? 'bg-sidebar-accent' : ''
              }`}
            >
              <div className="flex items-start gap-3">
                <div className="mt-1 flex-shrink-0">
                  <StatusIcon status={session.status} />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-sidebar-foreground">{session.title}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {new Date(session.updated_at).toLocaleString()}
                    {session.primary_model ? ` · ${session.primary_model}` : ''}
                    {session.status === 'needs_review' ? ' · Needs review' : ''}
                  </p>
                </div>
              </div>
            </button>
          ))
        )}
      </div>

      <div className="border-t border-sidebar-border p-4">
        <button
          onClick={onNewSession}
          className="w-full rounded-md bg-primary px-4 py-2 text-primary-foreground transition-opacity hover:opacity-90"
        >
          New Session
        </button>
      </div>
    </div>
  );
}
