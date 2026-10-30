import { useEffect, useState } from 'react';
import { RevisionConflictError, type BlueprintDraft, type BlueprintWarning } from '../../lib/api';
import { putBlueprintDraft } from '../../lib/blueprintApi';

// "Edit markdown" for a draft under review: the text of .lea/blueprint.draft.md in a
// textarea, saved through PUT /blueprint/draft (the live PUT's revision contract;
// the server normalizes to the canonical rendering and returns it with warnings).
// The live blueprint keeps its own editor (MarkdownDoc) in the Markdown view.

export function DraftEditor({
  projectId,
  draft,
  onSaved,
  onClose,
}: {
  projectId: string;
  draft: BlueprintDraft;
  onSaved: () => void;
  onClose: () => void;
}) {
  const [text, setText] = useState(draft.content);
  const [base, setBase] = useState(draft.content);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<BlueprintWarning[]>(draft.warnings);
  const dirty = text !== base;

  // A refetched draft (the drafting run still writing) replaces an unedited text.
  useEffect(() => {
    if (!dirty) {
      setText(draft.content);
      setBase(draft.content);
      setWarnings(draft.warnings);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.content]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const saved = await putBlueprintDraft(projectId, text, draft.file_revision);
      setText(saved.content);
      setBase(saved.content);
      setWarnings(saved.warnings);
      onSaved();
    } catch (err) {
      setError(
        err instanceof RevisionConflictError
          ? 'The draft changed since you opened it. Copy your edits, reload the draft and apply them again.'
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bpv-draft-editor">
      <div className="bpv-draft-head">
        <span className="bpv-aside-title">Draft markdown</span>
        <span className="bpv-aside-sub bpv-mono">.lea/blueprint.draft.md</span>
        <span className="bpv-spacer" />
        <button className="bpv-btn is-small is-primary" onClick={() => void save()} disabled={busy || !dirty}>
          {busy ? 'Saving…' : 'Save draft'}
        </button>
        <button className="bpv-btn is-small" onClick={() => setText(base)} disabled={busy || !dirty}>
          Revert
        </button>
        <button className="bpv-btn is-small is-ghost" onClick={onClose} disabled={busy}>
          Back to the graph
        </button>
      </div>
      {error && <p className="bpv-note is-error">{error}</p>}
      {warnings.length > 0 && (
        <div className="bpv-warnings">
          {warnings.length} draft {warnings.length === 1 ? 'warning' : 'warnings'}: {warnings.map((w) => w.message).join(' · ')}
        </div>
      )}
      <textarea
        className="bpv-input bpv-mono bpv-draft-text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        aria-label="Draft blueprint markdown"
      />
      <p className="bpv-note">
        Saving normalizes the text to the canonical format. Nodes are <code>## key</code> sections with <code>- kind:</code>,{' '}
        <code>- uses:</code> and a statement; the system keeps the assigned <code>lean</code> and <code>file</code> lines.
      </p>
    </div>
  );
}
