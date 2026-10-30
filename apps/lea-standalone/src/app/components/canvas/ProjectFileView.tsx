import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { putProjectFile } from '../../lib/api';
import { getProjectDocument } from '../../lib/canvasApi';
import { errorLines, parseLeanProblems, sorryLines, theoremName } from '../../lib/canvasModel.js';
import { StepView, plainRows, type StepViewHandle } from './StepView';

export type FileCheck = { status: 'ok' | 'error'; detail?: string | null } | null;

export interface ProjectFileState {
  /** The file this report is about (the open path). */
  path: string;
  loading: boolean;
  error: string | null;
  binary: boolean;
  lean: boolean;
  editable: boolean;
  editing: boolean;
  saving: boolean;
  /** The lean_check verdict of the last save (the API checks .lean files on write). */
  check: FileCheck;
  content: string;
  name: string | null;
  sorries: number[];
}

// A project file opened from the rail / QuickOpen (useWorkbench().openPath) that
// this session never touched: the canvas keeps its header and drawer; this view
// is the strip (a 36px file bar) and the body. A read view (Shiki, line marks),
// and when the API says the file is editable a plain editor that saves through
// the existing putProjectFile path (which commits and checks .lean files).
// Not a session file, so no Monaco / LSP here; files the session touched open
// in the session editor instead.
export const ProjectFileView = forwardRef<
  StepViewHandle,
  { projectId: string; path: string; onState: (state: ProjectFileState) => void; onBack: () => void }
>(function ProjectFileView({ projectId, path, onState, onBack }, ref) {
  const [content, setContent] = useState('');
  const [binary, setBinary] = useState(false);
  const [lean, setLean] = useState(false);
  const [editable, setEditable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [check, setCheck] = useState<FileCheck>(null);
  const revision = useRef<string | undefined>(undefined);
  const generation = useRef(0);
  const viewRef = useRef<StepViewHandle>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(
    ref,
    () => ({
      jumpToLine(line: number) {
        const ta = textRef.current;
        if (!ta) {
          viewRef.current?.jumpToLine(line);
          return;
        }
        // Put the caret at the start of the line and scroll it into view.
        const lines = ta.value.split('\n');
        const offset = lines.slice(0, Math.max(0, line - 1)).reduce((n, l) => n + l.length + 1, 0);
        ta.focus();
        ta.setSelectionRange(offset, offset);
        const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
        ta.scrollTop = Math.max(0, (line - 1) * lineHeight - ta.clientHeight / 2);
      },
    }),
    [],
  );

  useEffect(() => {
    let cancelled = false;
    generation.current += 1;
    setLoading(true);
    setEditing(false);
    setCheck(null);
    setError(null);
    getProjectDocument(projectId, path)
      .then((f) => {
        if (cancelled) return;
        setContent(f.content);
        revision.current = f.file_revision;
        setBinary(f.binary);
        setLean(f.lean);
        setEditable(f.editable && !f.binary);
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [projectId, path]);

  const shown = editing ? draft : content;
  const name = useMemo(() => theoremName(lean ? shown : '', path), [lean, shown, path]);
  const sorries = useMemo(() => (lean ? sorryLines(shown) : []), [lean, shown]);
  useEffect(() => {
    onState({ path, loading, error, binary, lean, editable, editing, saving, check, content: shown, name, sorries });
    // onState is a stable callback from Canvas (useCallback); the state tuple is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, loading, error, binary, lean, editable, editing, saving, check, shown, name, sorries]);

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    const gen = generation.current;
    const submitted = draft;
    try {
      const result = await putProjectFile(projectId, path, submitted, revision.current);
      if (gen !== generation.current) return;
      revision.current = result.file_revision;
      setContent(submitted);
      setCheck(result.check ?? null);
      setEditing(false);
    } catch (err) {
      if (gen !== generation.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (gen === generation.current) setSaving(false);
    }
  };

  const rows = useMemo(() => plainRows(content), [content]);
  const marks = useMemo(
    () => ({ errors: errorLines(parseLeanProblems(check?.detail)), sorries }),
    [check, sorries],
  );

  const note = loading
    ? 'Loading…'
    : binary
      ? 'Binary · read-only'
      : !editable
        ? 'Read-only'
        : editing
          ? saving
            ? 'Saving and checking…'
            : 'Editing · saving commits the file'
          : check
            ? `Saved · ${check.status === 'ok' ? 'lean_check: 0 errors' : 'lean_check: errors'}`
            : 'Editable';

  return (
    <>
      <div className="cv-filebar">
        <span className="cv-filebar-label">Project file</span>
        <span className="cv-filebar-note">{note}</span>
        <span className="cv-spacer" />
        {!loading && editable && !editing && !error && (
          <button
            type="button"
            className="cv-btn"
            onClick={() => {
              setDraft(content);
              setCheck(null);
              setEditing(true);
            }}
          >
            Edit
          </button>
        )}
        {editing && (
          <>
            <button type="button" className="cv-btn" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
            <button type="button" className="cv-btn primary" onClick={save} disabled={saving || draft === content}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </>
        )}
        <button type="button" className="cv-btn" onClick={onBack}>
          Back to the proof
        </button>
      </div>
      <div className="cv-editor">
        {loading ? (
          <div className="cv-note">Loading…</div>
        ) : error && !editing ? (
          <div className="cv-note err" role="alert">{error}</div>
        ) : binary ? (
          <div className="cv-note">This file isn’t text. Export the project to get it.</div>
        ) : editing ? (
          <div className="cv-plain-editor">
            <textarea
              ref={textRef}
              className="cv-textarea fill mono"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                  e.preventDefault();
                  if (draft !== content) void save();
                }
              }}
              spellCheck={false}
              autoFocus
              aria-label={`Edit ${path}`}
            />
            {error ? <div className="cv-note err" role="alert">{error}</div> : null}
          </div>
        ) : (
          <StepView ref={viewRef} code={content} rows={rows} marks={marks} lean={lean} />
        )}
      </div>
    </>
  );
});
