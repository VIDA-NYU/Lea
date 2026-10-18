import { useEffect, useRef, useState } from 'react';
import { Maximize2, Minimize2, Pencil } from 'lucide-react';
import { getProjectDocSnapshot, putProjectDoc, type ProjectDocName } from '../lib/api';
import { MarkdownMessage } from './MarkdownMessage';

// One reusable editor for a project's `.lea/*.md` docs (D39) — backs the Instructions
// and Memory rail cards and the Blueprint authoring view. Rendered view via
// MarkdownMessage; an edit mode swaps in a textarea that PUTs on Save. An **expand**
// affordance pops that same editor into a roomy modal for long docs — inline and modal
// share one `draft`/`save()`, so either entry point works. `agentWritten` docs (Memory)
// re-fetch when `refreshSignal` changes, so a run that edits memory.md shows up live.
export function MarkdownDoc({
  projectId,
  doc,
  title,
  icon,
  agentWritten = false,
  refreshSignal = 0,
  emptyHint,
  onSaved,
}: {
  projectId: string;
  doc: ProjectDocName;
  title: string;
  icon?: string;
  agentWritten?: boolean;
  refreshSignal?: number;
  emptyHint?: string;
  // Fired after a successful save — lets a parent re-derive dependent views (e.g.
  // the Blueprint tab refetches its warnings + graph once blueprint.md changes).
  onSaved?: () => void;
}) {
  const [content, setContent] = useState('');
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  // The modal editor — a roomy pop-out of the same edit session (shares `draft`).
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadedRevision=useRef<string | undefined>(undefined);
  const draftRevision=useRef<string | undefined>(undefined);
  const latestDraft=useRef(draft);latestDraft.current=draft;
  const scope=useRef('');scope.current=projectId+':'+doc;

  // Load on mount / project change, and re-load on refreshSignal for agent-written
  // docs — but never clobber an in-progress edit.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getProjectDocSnapshot(projectId, doc)
      .then((snapshot) => {
        if (cancelled) return;
        setContent(snapshot.content);
        loadedRevision.current=snapshot.file_revision;
        if (!editing) setError(null);
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, doc, refreshSignal]);

  const startEdit = () => {
    setDraft(content);
    draftRevision.current=loadedRevision.current;
    setError(null);
    setEditing(true);
  };

  // Expand opens the modal; from view mode it also enters the (shared) edit session.
  const openModal = () => {
    if (!editing) {setDraft(content);draftRevision.current=loadedRevision.current;}
    setError(null);
    setEditing(true);
    setExpanded(true);
  };

  // Collapse pops back to the inline editor, keeping the draft.
  const collapse = () => setExpanded(false);

  const cancel = () => {
    setEditing(false);
    setExpanded(false);
    setError(null);
  };

  const save = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    const owned=scope.current;
    const submitted=draft;
    try {
      const result = await putProjectDoc(projectId, doc, submitted,draftRevision.current);
      if (owned!==scope.current)return;
      setContent(result.content);
      loadedRevision.current=result.file_revision;
      draftRevision.current=result.file_revision;
      if (latestDraft.current===submitted) {setEditing(false);setExpanded(false);}
      onSaved?.();
    } catch (err) {
      if (owned!==scope.current)return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (owned===scope.current)setBusy(false);
    }
  };

  const hasContent = content.trim().length > 0;

  return (
    <section className="rail-card">
      <header className="rail-card-head">
        <span className="rail-card-title">
          {icon && <span className="rail-card-icon">{icon}</span>}
          {title}
          {agentWritten && (
            <span className="rail-card-badge" title="Co-authored: Lea updates this as it works, and so can you">
              Lea + you
            </span>
          )}
        </span>
        <div className="rail-card-actions">
          {!editing && (
            <button className="rail-card-edit" onClick={startEdit} title={`Edit ${title}`}>
              <Pencil size={13} /> Edit
            </button>
          )}
          <button className="rail-card-expand" onClick={openModal} title={`Open ${title} in a larger editor`}>
            <Maximize2 size={14} />
          </button>
        </div>
      </header>

      {editing && !expanded ? (
        <div className="rail-card-editor">
          <textarea
            className="rail-card-textarea"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={`# ${title}\n\nMarkdown…`}
            autoFocus
          />
          {error && <div className="rail-card-error">{error}</div>}
          <div className="rail-card-foot">
            <button className="rail-card-cancel" onClick={cancel} disabled={busy}>
              Cancel
            </button>
            <button className="rail-card-save" onClick={save} disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      ) : (
        <div className="rail-card-body">
          {loading ? (
            <div className="rail-card-muted">Loading…</div>
          ) : error && !expanded ? (
            <div className="rail-card-error">{error}</div>
          ) : hasContent ? (
            <MarkdownMessage content={content} />
          ) : (
            <div className="rail-card-muted">
              {emptyHint ?? `Nothing here yet. Click Edit to add ${title.toLowerCase()}.`}
            </div>
          )}
        </div>
      )}

      {expanded && (
        <div className="modal-overlay" onMouseDown={collapse}>
          <div className="modal doc-modal" onMouseDown={(e) => e.stopPropagation()}>
            <div className="doc-modal-head">
              <span className="doc-modal-title">
                {icon && <span className="rail-card-icon">{icon}</span>}
                {title}
              </span>
              <button className="doc-modal-collapse" onClick={collapse} title="Collapse to the card">
                <Minimize2 size={15} />
              </button>
            </div>
            <textarea
              className="doc-modal-textarea"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={`# ${title}\n\nMarkdown…`}
              autoFocus
            />
            {error && <div className="modal-error">{error}</div>}
            <div className="modal-foot">
              <button className="modal-btn" onClick={cancel} disabled={busy}>
                Cancel
              </button>
              <button className="modal-btn primary" onClick={save} disabled={busy}>
                {busy ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
