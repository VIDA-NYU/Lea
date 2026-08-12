import { useEffect, useMemo, useState } from 'react';
import {
  getCurrentFormalization,
  type CodeStep,
  type SafeVerifyStatus,
} from '../lib/api';
import { LiveEditor } from './LiveEditor';
import { diffForStep } from '../lib/codeDiff';
import { ensureHighlighter, highlightToLines, isHighlighterReady } from '../lib/leanHighlight.mjs';
import { deriveCodeStepProofStatus } from '../lib/proofDisplay.mjs';
import { sortCodeSteps } from '../lib/timeline.mjs';
import { distinctFiles, latestIndexForPath, mainFilePath } from '../lib/canvasFiles.mjs';
import { useProofSession } from '../stores/proofSession';
import {
  formalizationCanvasSteps,
  stepsForFormalization,
} from '../lib/formalizations.mjs';

export interface CheckOutcome {
  status: string;
  detail?: string | null;
}

// The right-hand canvas: navigable, syntax-highlighted Lean snapshots with a
// per-step verdict, plus direct editing → lean_check and SafeVerify on the
// latest snapshot. Network calls are delegated to the parent (which owns the
// session id + state) via onSaveAndCheck / onVerify.
export function Canvas({
  sessionId,
  onClose,
  onSaveAndCheck,
  onVerify,
  onOpenSession,
}: {
  // Present once the session is persisted; the Live editor needs it for the
  // per-session LSP WebSocket. Absent on a brand-new, unsaved session.
  sessionId?: string;
  onClose: () => void;
  onSaveAndCheck: (
    content: string,
    path?: string,
    baseRevision?: string,
  ) => Promise<CheckOutcome>;
  onVerify: (path?: string) => Promise<CheckOutcome>;
  onOpenSession?: (sessionId: string) => void;
}) {
  // v2.2: History = the Shiki snapshot stepper (unchanged); Live = the lean4monaco
  // editor with real goals/hover/diagnostics (D66). Live needs a persisted session
  // and at least one file to open.
  const [mode, setMode] = useState<'history' | 'live'>('history');
  // R1b/R1c: canvas state (verdict, snapshots, stepper position, run-active flag)
  // comes straight from the store now — no props from App.
  const sessionPersistedVerify = useProofSession((s) => s.safeVerify);
  const isRunning = useProofSession((s) => s.isRunning);
  const rawSteps = useProofSession((s) => s.codeSteps);
  const allCodeSteps = useMemo(() => sortCodeSteps(rawSteps), [rawSteps]);
  const scope = useProofSession((s) => s.formalizationScope);
  const revisionMode = useProofSession((s) => s.canvasRevisionMode);
  const setRevisionMode = useProofSession((s) => s.setCanvasRevisionMode);
  const currentSnapshot = useProofSession((s) => s.currentFormalizationSnapshot);
  const setCurrentSnapshot = useProofSession((s) => s.setCurrentFormalizationSnapshot);
  const refreshToken = useProofSession((s) => s.formalizationRefreshToken);
  const [currentLoading, setCurrentLoading] = useState(false);
  const [currentError, setCurrentError] = useState<string | null>(null);
  const scoped = scope !== 'project' && scope !== 'new';
  useEffect(() => {
    if (!scoped || !sessionId) {
      setCurrentSnapshot(null);
      setCurrentError(null);
      return;
    }
    let cancelled = false;
    setCurrentSnapshot(null);
    setCurrentLoading(true);
    setCurrentError(null);
    getCurrentFormalization(scope, sessionId)
      .then((snapshot) => {
        if (!cancelled) setCurrentSnapshot(snapshot);
      })
      .catch((err) => {
        if (!cancelled) {
          setCurrentError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setCurrentLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scope, scoped, sessionId, refreshToken, setCurrentSnapshot]);

  const persistedVerify = scoped
    ? (
        currentSnapshot?.formalization_id === scope
        && currentSnapshot.safe_verify?.current
          ? {
              status: currentSnapshot.safe_verify.status,
              detail: currentSnapshot.safe_verify.detail,
            }
          : null
      )
    : sessionPersistedVerify;
  const historicalSteps = useMemo(
    () => scoped ? stepsForFormalization(allCodeSteps, scope) : allCodeSteps,
    [allCodeSteps, scope, scoped],
  );
  const showingCurrent = Boolean(scoped && revisionMode === 'current');
  const codeSteps = useMemo(
    () => scoped
      ? formalizationCanvasSteps({
          mode: revisionMode,
          formalizationId: scope,
          snapshot: currentSnapshot,
          historicalSteps,
        })
      : historicalSteps,
    [currentSnapshot, historicalSteps, revisionMode, scope, scoped],
  );
  const globalIndex = useProofSession((s) => s.codeIndex);
  const setGlobalIndex = useProofSession((s) => s.setCodeIndex);
  const [currentIndex, setCurrentIndex] = useState(0);
  const total = codeSteps.length;
  const selectedId = allCodeSteps[globalIndex]?.id;
  const selectedScopedIndex = historicalSteps.findIndex((item) => item.id === selectedId);
  const safeIndex = showingCurrent
    ? Math.min(currentIndex, Math.max(0, total - 1))
    : selectedScopedIndex >= 0
      ? selectedScopedIndex
      : Math.max(0, total - 1);
  useEffect(() => {
    if (showingCurrent) setCurrentIndex(0);
  }, [scope, currentSnapshot?.revision_token, showingCurrent]);
  const onIndexChange = (nextIndex: number) => {
    const bounded = Math.min(Math.max(nextIndex, 0), Math.max(total - 1, 0));
    if (showingCurrent) {
      setCurrentIndex(bounded);
      return;
    }
    const id = codeSteps[bounded]?.id;
    const nextGlobal = allCodeSteps.findIndex((item) => item.id === id);
    if (nextGlobal >= 0) setGlobalIndex(nextGlobal);
  };
  const step = codeSteps[safeIndex];

  // Live mode needs a saved session + a file to open. If those go away (e.g. the
  // session resets), fall back to History so we never mount the editor with nothing.
  const canLive = !!sessionId && total > 0 && (!scoped || showingCurrent);
  useEffect(() => {
    if (!canLive && mode === 'live') setMode('history');
  }, [canLive, mode]);

  // File model (#10): a session can touch several files (a main proof + throwaway
  // `scratch` probes). `shownPath` is the file currently in view; `isFileCurrent` is
  // true when the shown step is the newest snapshot OF THAT FILE — so Edit / Run
  // SafeVerify / the verdict stay available on the main proof even when a scratch
  // write is the newest step overall (the old `isLatest === total-1` hid them).
  const shownPath = step?.path;
  const files = useMemo(() => distinctFiles(codeSteps), [codeSteps]);
  const mainPath = useMemo(
    () => showingCurrent
      ? (
          currentSnapshot?.files.find((file) => file.role === 'primary')?.path
          || mainFilePath(codeSteps)
        )
      : mainFilePath(codeSteps),
    [codeSteps, currentSnapshot, showingCurrent],
  );
  const isFileCurrent = !!step && (
    showingCurrent || safeIndex === latestIndexForPath(codeSteps, shownPath)
  );
  const pickFile = (path: string) => onIndexChange(latestIndexForPath(codeSteps, path));

  const [busy, setBusy] = useState(false);
  const [verify, setVerify] = useState<{ status: SafeVerifyStatus; detail?: string | null } | null>(null);

  // Reset the transient SafeVerify result when the shown step changes.
  useEffect(() => {
    setVerify(null);
  }, [safeIndex, step?.id]);

  const rows = useMemo(() => {
    if (!step) return [];
    if (showingCurrent) {
      return String(step.code).split('\n').map((line: string, index: number) => ({
        kind: 'unchanged',
        line,
        newLineNumber: index + 1,
      }));
    }
    // diffForStep returns unchanged/added/removed; the current file is the
    // non-removed rows, with 'added' lines tinted green like the mockup.
    return diffForStep(codeSteps, safeIndex).filter((r: any) => r.kind !== 'removed');
  }, [codeSteps, safeIndex, showingCurrent, step]);

  // Lean highlighting via Shiki (#11). The highlighter loads async once per session;
  // `hlReady` flips true when it's in, triggering a re-highlight. `tokenLines` is the
  // whole snapshot tokenized (so multi-line /- -/ block comments highlight correctly),
  // indexed by source line; a row picks its tokens by its new-file line number. Null
  // (still loading / no code) → the rows fall back to plain text, never blank.
  const [hlReady, setHlReady] = useState(isHighlighterReady());
  useEffect(() => {
    if (hlReady) return;
    let cancelled = false;
    ensureHighlighter()
      .then(() => !cancelled && setHlReady(true))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [hlReady]);
  const tokenLines = useMemo(
    () => (hlReady && step?.code ? highlightToLines(step.code) : null),
    [hlReady, step?.code],
  );

  const runVerify = async () => {
    setBusy(true);
    setVerify({ status: 'running' });
    try {
      const result = await onVerify(shownPath);
      setVerify({ status: result.status as SafeVerifyStatus, detail: result.detail });
    } catch (err) {
      setVerify({ status: 'error', detail: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const proofStatus = deriveCodeStepProofStatus(step);
  const verdict = proofStatus === 'proved'
    ? { cls: 'ok', text: '✓ compiles' }
    : proofStatus === 'defined'
    ? { cls: 'ok', text: '✓ definition' }
    : proofStatus === 'checked'
    ? { cls: 'ok', text: '✓ checked' }
    : proofStatus === 'stubbed'
    ? { cls: 'stub', text: '○ checked stub' }
    : proofStatus === 'failed'
    ? { cls: 'err', text: '✗ errors' }
    : { cls: 'idle', text: '○ not checked' };

  // Live result (this session) wins; otherwise fall back to the persisted verdict
  // when viewing the shown file's current snapshot, so a reload still shows
  // SafeVerify ✓ (M24) — and it stays on the main proof, not a later scratch.
  const shownVerify = verify ?? (isFileCurrent ? persistedVerify ?? null : null);
  const conversationBehind = Boolean(
    showingCurrent
    && currentSnapshot?.conversation
    && currentSnapshot.conversation.files.length > 0
    && !currentSnapshot.conversation.is_current,
  );
  const updater = currentSnapshot?.last_updated_session;

  return (
    <section className="canvas">
      <div className="canvas-head">
        {files.length > 1 ? (
          <FileSelect files={files} current={shownPath} mainPath={mainPath} onPick={pickFile} />
        ) : (
          <span className="file">{step ? step.path : 'no file yet'}</span>
        )}
        <span className="head-spacer" />
        {canLive && (
          <div className="cv-mode" role="tablist" aria-label="Canvas mode">
            <button
              className={`cv-mode-btn ${mode === 'history' ? 'active' : ''}`}
              onClick={() => setMode('history')}
              title="Review the agent's proof snapshots step by step"
            >
              History
            </button>
            <button
              className={`cv-mode-btn ${mode === 'live' ? 'active' : ''}`}
              onClick={() => setMode('live')}
              title="Edit the proof with live Lean feedback (goals, hover, errors on every keystroke)"
            >
              Edit
            </button>
          </div>
        )}
        <button className="x" onClick={onClose} title="Hide canvas">
          ✕
        </button>
      </div>

      {scoped && revisionMode === 'historical' && (
        <div className="revision-notice historical">
          <span>
            Historical snapshot from this conversation — not necessarily the current project version.
          </span>
          <button type="button" onClick={() => setRevisionMode('current')}>
            View current
          </button>
        </div>
      )}
      {conversationBehind && (
        <div className="revision-notice">
          <span>
            Updated in <strong>{updater?.title || 'another conversation'}</strong>
            {currentSnapshot?.last_updated_at
              ? ` on ${formatRevisionDate(currentSnapshot.last_updated_at)}`
              : ''}. This conversation last worked on an earlier revision.
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
      {scoped && showingCurrent && currentLoading && !step && (
        <div className="revision-loading">Loading current project version…</div>
      )}
      {scoped && showingCurrent && currentError && (
        <div className="revision-notice error">
          <span>{currentError}</span>
          {historicalSteps.length > 0 && (
            <button type="button" onClick={() => setRevisionMode('historical')}>
              View this conversation&apos;s version
            </button>
          )}
        </div>
      )}

      {mode === 'live' && sessionId ? (
        <LiveEditor
          sessionId={sessionId}
          locked={isRunning}
          onSave={(content, path) => onSaveAndCheck(
            content,
            path,
            scoped && showingCurrent
              ? currentSnapshot?.revision_token || undefined
              : undefined,
          )}
          onVerify={onVerify}
        />
      ) : (
        <>
      {total > 0 && (
        <div className="stepper">
          <button className="nav" onClick={() => onIndexChange(safeIndex - 1)} disabled={safeIndex === 0}>
            ‹
          </button>
          <button
            className="nav"
            onClick={() => onIndexChange(safeIndex + 1)}
            disabled={safeIndex === total - 1}
          >
            ›
          </button>
          <span className="label">
            {showingCurrent
              ? 'Current project version'
              : `Step ${safeIndex + 1} of ${total}`}
            {step?.turn ? <span className="stepname"> · turn {step.turn}</span> : null}
            {step?.author === 'user' ? <span className="stepname"> · your edit</span> : null}
          </span>
          <span className="spacer" />
          <span className={`verdict ${verdict.cls}`}>{verdict.text}</span>
        </div>
      )}

      {!step ? (
        <div className="canvas-empty">
          {scoped
            ? 'This formalization has no attributed Lean file yet.'
            : 'Lean code will appear here as Lea edits files.'}
        </div>
      ) : (
        <div className="code-wrap">
          <pre className="code">
            {rows.map((row: any, i: number) => (
              <div key={i} className={`ln ${row.kind === 'added' ? 'add' : ''}`}>
                <span className="gut">{row.newLineNumber ?? ''}</span>
                <span className="src">
                  {renderLineTokens(tokenLines, row)}
                  {row.line === '' ? ' ' : ''}
                </span>
              </div>
            ))}
          </pre>
        </div>
      )}

      <div className="canvas-foot">
        {proofStatus === 'proved' || proofStatus === 'defined' || proofStatus === 'checked' ? (
          <span className="badge compile">✓ lean_check: 0 errors</span>
        ) : proofStatus === 'stubbed' ? (
          <span className="badge stub">✓ lean_check: 0 errors · contains sorry</span>
        ) : step?.check_status === 'error' ? (
          <span className="err-detail">{step.check_detail || 'lean_check: errors'}</span>
        ) : (
          <span className="badge idle">○ not checked yet</span>
        )}

        {shownVerify ? (
          shownVerify.status === 'running' ? (
            <span className="badge idle">🛡 SafeVerify…</span>
          ) : shownVerify.status === 'ok' ? (
            <span className="badge sv">🛡 SafeVerify ✓</span>
          ) : (
            // rejected / error / unavailable: show the badge AND the detail inline.
            // The detail is *why* it failed (kernel-audit output, axiom violation,
            // build issue) — it must be visible, not hidden in a hover tooltip.
            <span className="sv-fail">
              <span className="badge bad">🛡 SafeVerify {shownVerify.status}</span>
              {shownVerify.detail && (
                <span className="err-detail">{shownVerify.detail}</span>
              )}
            </span>
          )
        ) : (
          step &&
          isFileCurrent &&
          !isRunning &&
          proofStatus === 'proved' && (
            <button className="cv-btn" onClick={runVerify} disabled={busy}>
              🛡 Run SafeVerify
            </button>
          )
        )}
      </div>
        </>
      )}
    </section>
  );
}

type FileMeta = { path: string; isScratch: boolean; latestIndex: number; count: number };

function formatRevisionDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: parsed.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
  });
}

function baseName(path: string): string {
  return path.split('/').pop() || path;
}

// The canvas file selector (#10): pick which file the canvas shows + which file
// SafeVerify/lean_check act on. Main (non-scratch) files first, then a dimmed
// "scratch" group. Picking a file jumps the stepper to that file's latest snapshot.
function FileSelect({
  files,
  current,
  mainPath,
  onPick,
}: {
  files: FileMeta[];
  current?: string;
  mainPath: string | null;
  onPick: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Non-scratch first, each group most-recently-touched first.
  const sorted = [...files].sort(
    (a, b) => Number(a.isScratch) - Number(b.isScratch) || b.latestIndex - a.latestIndex,
  );
  const mains = sorted.filter((f) => !f.isScratch);
  const scratch = sorted.filter((f) => f.isScratch);
  const pick = (path: string) => {
    onPick(path);
    setOpen(false);
  };
  const row = (f: FileMeta) => (
    <button
      key={f.path}
      className={`cv-file-row ${f.path === current ? 'active' : ''} ${f.isScratch ? 'scratch' : ''}`}
      onClick={() => pick(f.path)}
      title={f.path}
    >
      <span className="cv-file-dot" />
      <span className="cv-file-row-name">{baseName(f.path)}</span>
      {f.path === mainPath && <span className="cv-file-tag">main</span>}
    </button>
  );
  return (
    <div className="cv-file-select">
      <button className="cv-file-btn" onClick={() => setOpen((o) => !o)} title={current}>
        📄 <span className="cv-file-name">{current ? baseName(current) : 'no file'}</span>
        <span className="cv-file-caret">▾</span>
      </button>
      {open && (
        <>
          <div className="cv-file-backdrop" onClick={() => setOpen(false)} />
          <div className="cv-file-menu">
            {mains.map(row)}
            {scratch.length > 0 && <div className="cv-file-group">scratch</div>}
            {scratch.map(row)}
          </div>
        </>
      )}
    </div>
  );
}

// Render one diff row's source using its Shiki tokens (colors inline from the
// theme). Tokens are keyed by the row's new-file line number, so this stays aligned
// with the gutter. Falls back to the raw line text while the highlighter loads or if
// a line has no tokens. Shiki fontStyle is a bitmask (Italic=1, Bold=2, Underline=4).
function renderLineTokens(tokenLines: any[] | null, row: any) {
  const toks =
    tokenLines && row.newLineNumber != null ? tokenLines[row.newLineNumber - 1] : null;
  if (!toks) return row.line;
  return toks.map((t: any, j: number) => (
    <span
      key={j}
      style={{
        color: t.color,
        fontStyle: t.fontStyle & 1 ? 'italic' : undefined,
        fontWeight: t.fontStyle & 2 ? 600 : undefined,
        textDecoration: t.fontStyle & 4 ? 'underline' : undefined,
      }}
    >
      {t.content}
    </span>
  ));
}
