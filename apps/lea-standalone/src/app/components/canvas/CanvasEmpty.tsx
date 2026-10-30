import { useState } from 'react';
import { IconChat, IconClipboard, IconFile, IconFolder } from './icons';

// Empty state (mockup frame 1): three ways to begin plus a New file button.
// "Paste Lean" and "New file" open small inline forms (Electron has no
// window.prompt); the parent decides what each action does.
export function CanvasEmpty({
  scoped,
  hint,
  onAskLea,
  onOpenProjectFile,
  onPasteLean,
  onNewFile,
  newFileDefault,
  newFileDisabledReason,
}: {
  scoped: boolean;
  hint?: string | null;
  onAskLea: () => void;
  onOpenProjectFile: () => void;
  onPasteLean: (code: string) => void;
  /** Resolves to an error message, or null on success. */
  onNewFile: (path: string) => Promise<string | null>;
  newFileDefault: string;
  newFileDisabledReason?: string | null;
}) {
  const [mode, setMode] = useState<'cards' | 'paste' | 'new'>('cards');
  const [pasted, setPasted] = useState('');
  const [newPath, setNewPath] = useState('');
  const [creating, setCreating] = useState(false);
  const [newError, setNewError] = useState<string | null>(null);

  const createFile = async () => {
    const path = newPath.trim();
    if (!path || creating) return;
    setCreating(true);
    setNewError(null);
    const error = await onNewFile(path);
    setCreating(false);
    if (error) setNewError(error);
    else setMode('cards');
  };

  return (
    <div className="cv-empty">
      <div className="cv-empty-inner">
        <div className="cv-empty-glyph">
          <IconFile size={22} />
        </div>
        <div className="cv-empty-text">
          <p className="cv-empty-title">The proof appears here</p>
          <p className="cv-empty-sub">
            {scoped
              ? 'This formalization has no attributed Lean file yet. Three ways to begin:'
              : 'Lean code, its goals and every check live in this pane. Three ways to begin:'}
          </p>
        </div>
        {mode === 'paste' ? (
          <div className="cv-form">
            <label className="cv-sr" htmlFor="cv-paste-box">Lean to start from</label>
            <textarea
              id="cv-paste-box"
              className="cv-textarea"
              placeholder={'theorem sqrt_two_irrational : Irrational (Real.sqrt 2) := by\n  sorry'}
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              spellCheck={false}
              autoFocus
            />
            <div className="cv-form-actions">
              <button type="button" className="cv-btn" onClick={() => { setMode('cards'); setPasted(''); }}>
                Cancel
              </button>
              <button
                type="button"
                className="cv-btn primary"
                disabled={!pasted.trim()}
                title="Puts the code in the composer; edit the request and send it"
                onClick={() => {
                  onPasteLean(pasted.trim());
                  setMode('cards');
                  setPasted('');
                }}
              >
                Ask Lea to continue it
              </button>
            </div>
          </div>
        ) : mode === 'new' ? (
          <form
            className="cv-form"
            onSubmit={(e) => {
              e.preventDefault();
              void createFile();
            }}
          >
            <label className="cv-form-label" htmlFor="cv-new-path">New file path (in the project)</label>
            <input
              id="cv-new-path"
              className="cv-input mono"
              value={newPath}
              onChange={(e) => setNewPath(e.target.value)}
              spellCheck={false}
              autoFocus
            />
            {newError ? <p className="cv-form-error" role="alert">{newError}</p> : null}
            <div className="cv-form-actions">
              <button type="button" className="cv-btn" onClick={() => { setMode('cards'); setNewError(null); }} disabled={creating}>
                Cancel
              </button>
              <button type="submit" className="cv-btn primary" disabled={!newPath.trim() || creating}>
                {creating ? 'Creating…' : 'Create'}
              </button>
            </div>
          </form>
        ) : (
          <>
            <div className="cv-cards">
              <button type="button" className="cv-card" onClick={onAskLea}>
                <IconChat className="cv-card-ico" />
                <span className="cv-card-title">Ask Lea</span>
                <span className="cv-card-sub">State a theorem in the chat. Lea drafts the Lean file.</span>
              </button>
              <button type="button" className="cv-card" onClick={onOpenProjectFile}>
                <IconFolder className="cv-card-ico" />
                <span className="cv-card-title">Open a project file</span>
                <span className="cv-card-sub">Continue a proof from one of your projects.</span>
              </button>
              <button type="button" className="cv-card" onClick={() => setMode('paste')}>
                <IconClipboard className="cv-card-ico" />
                <span className="cv-card-title">Paste Lean</span>
                <span className="cv-card-sub">Start from a statement you already have.</span>
              </button>
            </div>
            <button
              type="button"
              className="cv-btn lg"
              onClick={() => {
                setNewPath(newFileDefault);
                setNewError(null);
                setMode('new');
              }}
              disabled={!!newFileDisabledReason}
              title={newFileDisabledReason ?? 'Create a Lean file in this project'}
            >
              New file
            </button>
          </>
        )}
        {hint ? <p className="cv-empty-hint" role="status">{hint}</p> : null}
      </div>
    </div>
  );
}
