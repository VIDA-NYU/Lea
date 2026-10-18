import { useEffect, useRef, useState } from 'react';
import type { SafeVerifyResult } from '../lib/api';
import { LeanQuery } from '../lib/leanQuery';
import { EditorClient } from '../lib/editorClient.mjs';
import { useProofSession } from '../stores/proofSession';

type SaveResult = { status: string; detail?: string | null; current?: boolean; check_current?: boolean; check_stale_reason?: string | null };

// How long after the last keystroke we auto-save. Long enough not to commit on
// every character (git churn / redundant checks), short enough to feel automatic.
const AUTOSAVE_MS = 1200;

// Monaco retains the local draft, cursor and undo. Language results are bounded
// SQL observations; only explicit save/check actions can update proof evidence.
export function LiveEditor({
  sessionId,
  path,
  locked,
  onSave,
  onVerify,
  captureSelection,
}: {
  sessionId: string;
  path?: string;
  captureSelection: () => () => boolean;
  locked?: boolean;
  onSave?: (content: string, path: string) => Promise<SaveResult>;
  onVerify?: (path: string) => Promise<SaveResult>;
}) {
  const editorRef = useRef<HTMLDivElement>(null);
  const clientRef = useRef<EditorClient | null>(null);
  const [analysis, setAnalysis] = useState('pending');
  const [goals, setGoals] = useState('Click inside a proof to inspect its goals.');
  const splitRef = useRef<HTMLDivElement>(null);
  // The editor/InfoView split is a draggable vertical divider: `editorPct` is the
  // editor's share of the height; the InfoView takes the rest.
  const [editorPct, setEditorPct] = useState(60);
  const [dragging, setDragging] = useState(false);
  // Live handles + latest props kept in refs so the (once-registered) change
  // listener and the Save/Reload handlers read current values, never stale ones.
  /* eslint-disable @typescript-eslint/no-explicit-any */
  const editorApiRef = useRef<any>(null);
  /* eslint-enable @typescript-eslint/no-explicit-any */
  // Typed Lean-LSP query layer, built once the editor is up (Phase 3).
  const leanQueryRef = useRef<LeanQuery | null>(null);
  const pathRef = useRef<string>('');
  const onSaveRef = useRef(onSave);
  const lockedRef = useRef(locked);
  const saveTimerRef = useRef<number | null>(null);
  const savingRef = useRef(false);
  const pendingRef = useRef(false);
  const suppressChangeRef = useRef(false);
  onSaveRef.current = onSave;
  lockedRef.current = locked;

  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState<string | null>(null);
  // Save lifecycle for the status label: clean (in sync) / dirty (pending) /
  // saving / saved. `detail` carries the lean_check verdict once saved.
  const [save, setSave] = useState<{ state: 'clean' | 'dirty' | 'saving' | 'saved'; detail: string }>(
    { state: 'clean', detail: '' },
  );
  const [verify, setVerify] = useState<{ state: 'idle' | 'running' | 'done'; status?: string; detail?: string }>(
    { state: 'idle' },
  );

  useEffect(() => {
    let disposed = false;
    /* eslint-disable @typescript-eslint/no-explicit-any */
    let leanMonacoEditor: any;
    let client: EditorClient;
    let analysisTimer: number;
    let goalTimer: number;
    let goalGeneration = 0;
    let analysisGeneration = 0;
    let environmentTimer: number;
    let refreshing = false;
    /* eslint-enable @typescript-eslint/no-explicit-any */

    // Serialize saves: never let two writes overlap; if the buffer changed while a
    // save was in flight, run one more pass after it lands.
    const runSave = async () => {
      const api = editorApiRef.current;
      const onSaveFn = onSaveRef.current;
      if (!api || !onSaveFn || lockedRef.current) return;
      if (savingRef.current) {
        pendingRef.current = true;
        return;
      }
      const value: string | null = api.editor?.getValue?.() ?? null;
      if (value == null) return;
      savingRef.current = true;
      setSave({ state: 'saving', detail: '' });
      try {
        const result = await onSaveFn(value, pathRef.current);
        if (disposed) return;
        if (api.editor?.getValue?.() !== value) {
          pendingRef.current = true;
          setSave({ state: 'dirty', detail: '' });
          return;
        }
        // The write always persists (a snapshot); the verdict just annotates it.
        setSave({
          state: 'saved',
          detail: result.status === 'ok'
            ? result.check_current === true ? 'lean_check: 0 errors' : 'recheck required'
            : result.detail || `lean_check: ${result.status}`,
        });
      } catch (err) {
        if (disposed) return;
        setSave({ state: 'dirty', detail: err instanceof Error ? err.message : String(err) });
      } finally {
        savingRef.current = false;
        if (!disposed && pendingRef.current) {
          pendingRef.current = false;
          scheduleSave();
        }
      }
    };

    const scheduleSave = () => {
      if (disposed || lockedRef.current) return;
      setSave((s) => ({ state: 'dirty', detail: s.detail }));
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = window.setTimeout(() => void runSave(), AUTOSAVE_MS);
    };

    const analyze = () => {
      const generation = ++analysisGeneration;
      window.clearTimeout(analysisTimer);
      analysisTimer = window.setTimeout(async () => {
        try { const value = await client.request('editor.analyze'); if (!disposed && generation === analysisGeneration) leanMonacoEditor.diagnostics(value); }
        catch { if (!disposed && generation === analysisGeneration) leanMonacoEditor?.diagnostics({ diagnostics: [] }); }
      }, 300);
    };
    const inspectGoal = () => {
      const generation = ++goalGeneration;
      window.clearTimeout(goalTimer);
      setGoals('Waiting for goal analysis…');
      goalTimer = window.setTimeout(async () => {
        const pos = leanQueryRef.current?.cursor();
        if (!pos) return;
        try {
          const result = await client.request('editor.goals', pos);
          if (!disposed && generation === goalGeneration) setGoals(result?.rendered || result?.goals?.join('\n\n') || 'No goals at this position.');
        } catch (err) { if (!disposed && generation === goalGeneration) setGoals(err instanceof Error ? err.message : 'Goal analysis unavailable.'); }
      }, 250);
    };
    (async () => {
      try {
        client = new EditorClient(sessionId, path, { onState: (state: string) => { if (!disposed) setAnalysis(state); } });
        clientRef.current = client;
        const info = await client.open();
        if (disposed) { client.dispose(); return; }
        pathRef.current = info.path;
        const { createQueuedEditor } = await import('../lib/queuedMonaco');
        if (disposed) return;
        leanMonacoEditor = createQueuedEditor(editorRef.current!, info.path, info.content ?? '', client);
        editorApiRef.current = leanMonacoEditor;
        leanQueryRef.current = new LeanQuery(client, leanMonacoEditor.editor);
        leanMonacoEditor.editor.updateOptions({ readOnly: !!lockedRef.current });
        leanMonacoEditor.editor.onDidChangeModelContent(() => {
          client.setContent(leanMonacoEditor.editor.getValue());
          leanMonacoEditor.diagnostics({ diagnostics: [] });
          if (!suppressChangeRef.current) scheduleSave();
          analyze(); inspectGoal();
        });
        leanMonacoEditor.editor.onDidChangeCursorPosition(inspectGoal);
        setStatus('ready'); analyze(); inspectGoal();
        environmentTimer = window.setInterval(async () => {
          if (disposed || refreshing) return;
          refreshing = true;
          try {
            if (await client.refresh()) {
              leanMonacoEditor.diagnostics({ diagnostics: [] }); analyze(); inspectGoal();
            }
          } catch (err) {
            if (!disposed) { client.invalidate(); leanMonacoEditor.diagnostics({ diagnostics: [] }); setGoals('Environment unavailable.'); }
          } finally { refreshing = false; }
        }, 2000);
      } catch (err) {
        if (!disposed) { setError(err instanceof Error ? err.message : String(err)); setStatus('error'); }
      }
    })();

    return () => {
      disposed = true;
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
      window.clearTimeout(analysisTimer);
      window.clearTimeout(goalTimer);
      window.clearInterval(environmentTimer);
      client?.dispose();
      clientRef.current = null;
      editorApiRef.current = null;
      leanQueryRef.current = null;
      try {
        leanMonacoEditor?.dispose();
      } catch {
        /* ignore */
      }
    };
  }, [sessionId, path]);

  // Reflect the modal lock onto the live editor when a run starts/stops.
  useEffect(() => {
    editorApiRef.current?.editor?.updateOptions({ readOnly: !!locked });
  }, [locked]);

  // A fresh edit makes any prior SafeVerify verdict stale — clear it.
  useEffect(() => {
    if (save.state === 'dirty' || save.state === 'saving') setVerify({ state: 'idle' });
  }, [save.state]);

  // Draggable split: translate cursor Y into the editor's height share, clamped so
  // neither pane collapses. Listeners live on window so the drag continues off the
  // 6px handle (and while dragging we disable iframe pointer-events below, so the
  // InfoView doesn't swallow the mousemove).
  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const rect = splitRef.current?.getBoundingClientRect();
      if (!rect || rect.height === 0) return;
      const pct = ((e.clientY - rect.top) / rect.height) * 100;
      setEditorPct(Math.min(85, Math.max(15, pct)));
    };
    const onUp = () => setDragging(false);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [dragging]);

  // Monaco doesn't reflow to a resized container on its own — relayout after the
  // split ratio changes.
  useEffect(() => {
    editorApiRef.current?.editor?.layout();
  }, [editorPct]);

  // SafeVerify runs on the *saved* on-disk file (kernel replay + axiom audit), so
  // it's only offered when the buffer is in sync (not mid-edit).
  const inSync = save.state === 'clean' || save.state === 'saved';
  const setVerifySurface = useProofSession((s) => s.setVerifySurface);
  const setGoalSurface = useProofSession((s) => s.setGoalSurface);
  const [goalNote, setGoalNote] = useState<string | null>(null);

  // Shared InfoView (Phase 3): ask the live LSP for the goal state at the cursor —
  // the exact goals the InfoView shows — and surface it above the composer so the
  // user can ask Lea about it. Queries the human's own session (current buffer,
  // even mid-edit), so no save is required.
  const handleAskGoal = async () => {
    const owned = captureSelection();
    setGoalNote(null);
    const q = leanQueryRef.current;
    const pos = q?.cursor();
    if (!q || !pos) {
      setGoalNote('Editor not ready.');
      return;
    }
    if (!q.ready()) {
      setGoalNote('Lean server still starting — try again in a moment.');
      return;
    }
    try {
      const result = await q.plainGoal();
      if (!owned()) return;
      const goals = result?.goals ?? [];
      const rendered = (result?.rendered ?? goals.join('\n\n')).trim();
      if (!result || !rendered || goals.length === 0) {
        // pos is the LSP 0-based position — the same numbering the InfoView shows.
        setGoalNote(`No goal at ${pos.line}:${pos.character} — click inside a tactic proof.`);
        return;
      }
      setGoalSurface({ rendered, line: pos.line });
    } catch (err) {
      if (!owned()) return;
      setGoalNote(err instanceof Error ? err.message : String(err));
    }
  };
  const handleVerify = async () => {
    if (!onVerify) return;
    const owned = captureSelection();
    setVerify({ state: 'running' });
    setVerifySurface(null); // clear any prior box while this run is in flight
    try {
      const result = await onVerify(pathRef.current);
      if (!owned()) return;
      setVerify({ state: 'done', status: result.status, detail: result.detail ?? undefined });
      // Surface the result as a box above the composer (collapse / send-to-Lea).
      setVerifySurface({ status: result.status, detail: result.detail ?? null, current: result.current } as SafeVerifyResult);
    } catch (err) {
      if (!owned()) return;
      const detail = err instanceof Error ? err.message : String(err);
      setVerify({ state: 'done', status: 'error', detail });
      setVerifySurface({ status: 'error', detail } as SafeVerifyResult);
    }
  };

  // Pull the latest on-disk file (e.g. after the agent edited it) into the buffer.
  // Suppress the resulting change event so it isn't treated as a user edit.
  const handleReload = async () => {
    const owned = captureSelection();
    try {
      const info = await clientRef.current!.info();
      if (!owned()) return;
      suppressChangeRef.current = true;
      const editor = editorApiRef.current?.editor;
      const model = editor?.getModel();
      if (model) { editor.pushUndoStop(); editor.executeEdits('reload', [{ range: model.getFullModelRange(), text: info.content ?? '' }]); editor.pushUndoStop(); }
      suppressChangeRef.current = false;
      setSave({ state: 'clean', detail: 'reloaded published file' });
    } catch (err) {
      if (!owned()) return;
      setSave({ state: 'clean', detail: err instanceof Error ? err.message : String(err) });
    }
  };

  const saveLabel = locked
    ? 'Read-only — agent is running'
    : save.state === 'saving'
    ? 'Saving…'
    : save.state === 'dirty'
    ? 'Editing…'
    : save.state === 'saved'
    ? `Saved · ${save.detail}`
    : save.detail || 'All changes saved';

  return (
    <div className="live-editor">
      <div className="live-editor-bar">
        <span className={`live-editor-status-dot ${locked ? 'locked' : save.state}`} />
        <span className="live-editor-note">{saveLabel}</span>
        <span className="live-editor-bar-spacer" />
        {status === 'loading' && <span className="live-editor-note">Opening editor…</span>}
        {status === 'error' && <span className="live-editor-note err">unavailable: {error}</span>}
        {status === 'ready' && <span className="live-editor-note" role="status">{analysis === 'available' ? 'Analysis ready' : analysis.replace(/_/g, ' ')}</span>}
        {goalNote && <span className="live-editor-note">{goalNote}</span>}
        <button
          className="cv-btn"
          onClick={handleAskGoal}
          disabled={status !== 'ready'}
          title="Grab the goal state at the cursor and ask Lea about it"
        >
          💬 Ask about goal
        </button>
        {onVerify && (
          <button
            className="cv-btn"
            onClick={handleVerify}
            disabled={status !== 'ready' || !!locked || !inSync || verify.state === 'running'}
            title={inSync ? 'Kernel-audit the saved proof (SafeVerify)' : 'Save first — SafeVerify checks the saved file'}
          >
            {verify.state === 'running' ? '🛡 SafeVerify…' : '🛡 SafeVerify'}
          </button>
        )}
        <button
          className="cv-btn"
          onClick={handleReload}
          disabled={status !== 'ready'}
          title="Load the latest published file"
        >
          ↻ Reload
        </button>
      </div>
      <div ref={splitRef} className={`live-editor-split ${dragging ? 'dragging' : ''}`}>
        <div ref={editorRef} className="live-editor-monaco" style={{ flexBasis: `${editorPct}%` }} />
        <div
          className="live-editor-resizer"
          onMouseDown={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          title="Drag to resize the goal panel"
        />
        <div className="live-editor-infoview vscode-light" aria-label="Goal analysis" style={{ overflow: 'auto', padding: 12 }}>
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{goals}</pre>
          <p className="live-editor-note">Draft analysis. Interactive widgets, navigation and code actions are unavailable.</p>
        </div>
      </div>
    </div>
  );
}
