import { useState } from 'react';
import { ExternalLink, FileText } from 'lucide-react';
import { openOverleafDocument } from '../lib/overleafLink';

// Unobtrusive session-origin indicator. For an Overleaf-spawned session it renders a
// clickable "Overleaf" pill that opens/focuses the source document (via the extension
// bridge, with a new-tab fallback). For a UI session it optionally renders a muted
// "Direct (UI)" tag (Stats pane) so every session states its origin; in the chat
// header `showDirect` is false so direct sessions show nothing.
//
// Styled with the shared `.origin-badge` classes in lea-v2.css, which live under
// `.lea-app` so the same markup reads correctly in both the chat header and the
// (also `.lea-app`-scoped) Stats page.
export function OriginBadge({
  origin,
  originUrl,
  showDirect = false,
}: {
  origin?: string | null;
  originUrl?: string | null;
  showDirect?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  if (origin !== 'overleaf') {
    if (!showDirect) return null;
    return (
      <span className="origin-badge origin-ui" title="Started in the Lea UI">
        Direct (UI)
      </span>
    );
  }

  const hasLink = Boolean(originUrl);
  const handleOpen = async () => {
    if (!hasLink || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      await openOverleafDocument(originUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not open the Overleaf document.');
    } finally {
      setBusy(false);
    }
  };

  const title = error
    ? error
    : hasLink
    ? 'Formalized from Overleaf — open the source document'
    : 'Formalized from Overleaf';

  return (
    <button
      type="button"
      className="origin-badge origin-overleaf"
      onClick={handleOpen}
      disabled={!hasLink || busy}
      title={title}
      aria-label={title}
    >
      <FileText className="origin-badge-glyph" size={11} aria-hidden="true" />
      <span>Overleaf</span>
      {hasLink && <ExternalLink className="origin-badge-out" size={10} aria-hidden="true" />}
    </button>
  );
}
