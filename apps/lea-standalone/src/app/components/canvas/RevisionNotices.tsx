import type { FormalizationCurrentSnapshot } from '../../lib/types';

function formatRevisionDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: parsed.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
  });
}

// The formalization-scope notices above the editor (unchanged behaviour from the
// pre-redesign canvas): a historical snapshot banner, "updated in another
// conversation", a missing file in the current revision, and a load error.
export function RevisionNotices({
  scoped,
  revisionMode,
  setRevisionMode,
  snapshot,
  sessionId,
  hasHistory,
  missingFile,
  error,
  onOpenSession,
}: {
  scoped: boolean;
  revisionMode: 'current' | 'historical';
  setRevisionMode: (mode: 'current' | 'historical') => void;
  snapshot: FormalizationCurrentSnapshot | null;
  sessionId?: string;
  hasHistory: boolean;
  missingFile: boolean;
  error: string | null;
  onOpenSession?: (sessionId: string) => void;
}) {
  const showingCurrent = scoped && revisionMode === 'current';
  const conversationBehind = Boolean(
    showingCurrent &&
      snapshot?.conversation &&
      snapshot.conversation.files.length > 0 &&
      !snapshot.conversation.is_current,
  );
  const updater = snapshot?.last_updated_session;
  return (
    <>
      {scoped && revisionMode === 'historical' && (
        <div className="revision-notice historical">
          <span>Historical snapshot from this conversation — not necessarily the current project version.</span>
          <button type="button" onClick={() => setRevisionMode('current')}>
            View current
          </button>
        </div>
      )}
      {conversationBehind && (
        <div className="revision-notice">
          <span>
            Updated in <strong>{updater?.title || 'another conversation'}</strong>
            {snapshot?.last_updated_at ? ` on ${formatRevisionDate(snapshot.last_updated_at)}` : ''}. This conversation
            last worked on an earlier revision.
          </span>
          <div className="revision-actions">
            <button type="button" onClick={() => setRevisionMode('historical')}>
              View this conversation&apos;s version
            </button>
            {updater?.id && updater.id !== sessionId && onOpenSession && (
              <button type="button" onClick={() => onOpenSession(updater.id)}>
                Open updating conversation
              </button>
            )}
          </div>
        </div>
      )}
      {showingCurrent && missingFile && (
        <div className="revision-notice">The selected file is unavailable in the current revision.</div>
      )}
      {showingCurrent && error && (
        <div className="revision-notice error">
          <span>{error}</span>
          {hasHistory && (
            <button type="button" onClick={() => setRevisionMode('historical')}>
              View this conversation&apos;s version
            </button>
          )}
        </div>
      )}
    </>
  );
}
