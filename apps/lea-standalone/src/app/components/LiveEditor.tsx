import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import type { EditorDraftBase, FileWriteResult } from '../lib/api';
import { LeanQuery } from '../lib/leanQuery';
import { EditorClient } from '../lib/editorClient.mjs';
import { EditorDraft } from '../lib/editorDraft.mjs';
import { useProofSession } from '../stores/proofSession';
import type { LineMarks } from './canvas/StepView';

export type SaveResult = {
  status: string;
  detail?: string | null;
  current?: boolean;
  check_current?: boolean;
  check_stale_reason?: string | null;
};
export type SaveState = { state: 'clean' | 'dirty' | 'saving' | 'saved'; detail: string };
export type EditorStatus = 'loading' | 'ready' | 'error';
export interface LiveDiagnostic {
  line: number;
  column: number;
  endLine: number;
  endColumn: number;
  severity: 'error' | 'warning' | 'info';
  message: string;
}
export interface LiveEditorHandle {
  /** Save the buffer if it is dirty, then run lean_check on the published file. */
  check(): Promise<void>;
  /** Replace the buffer with the latest published file. */
  reload(): Promise<void>;
  jumpToLine(line: number): void;
  focus(): void;
  /** Capture the goal at the cursor into the composer surface; resolves to a note when nothing could be captured. */
  askAboutGoal(): Promise<string | null>;
  getValue(): string | null;
}

// How long after the last keystroke we auto-save. Long enough not to commit on
// every character (git churn / redundant checks), short enough to feel automatic.
const AUTOSAVE_MS = 1200;

type MonacoApi = typeof import('monaco-editor');
/* eslint-disable @typescript-eslint/no-explicit-any */
type AnyEditor = any;
/* eslint-enable @typescript-eslint/no-explicit-any */

const SEVERITY: Record<number, LiveDiagnostic['severity']> = { 1: 'error', 2: 'warning', 3: 'info', 4: 'info' };
function mapDiagnostics(value: AnyEditor): LiveDiagnostic[] {
  return (value?.diagnostics || [])
    .filter((d: AnyEditor) => d?.range)
    .map((d: AnyEditor) => ({
      line: (d.range.start?.line ?? 0) + 1,
      column: (d.range.start?.character ?? 0) + 1,
      endLine: (d.range.end?.line ?? d.range.start?.line ?? 0) + 1,
      endColumn: (d.range.end?.character ?? 0) + 1,
      severity: SEVERITY[d.severity] ?? 'info',
      message: String(d.message ?? ''),
    }));
}

export interface LiveEditorProps {
  sessionId: string;
  path?: string;
  formalizationId?: string;
  captureSelection: () => () => boolean;
  locked?: boolean;
  onSave?: (content: string, path: string, base: EditorDraftBase, draftId: string) => Promise<FileWriteResult>;
  onCheck?: (path: string, base: EditorDraftBase) => Promise<SaveResult>;
  /**
   * Changes when a newer published snapshot of this file exists (the latest code
   * step's id). A clean buffer reloads silently so the editor follows Lea while
   * a run is live; a dirty buffer is kept and the note says so.
   */
  followKey?: string;
  /** Line marks drawn as Monaco decorations (changed / error / sorry / caret). */
  marks?: LineMarks;
  onStatus?: (status: EditorStatus, error?: string | null) => void;
  onSaveState?: (save: SaveState) => void;
  onAnalysis?: (state: string) => void;
  onGoals?: (text: string) => void;
  onDiagnostics?: (diagnostics: LiveDiagnostic[]) => void;
  onCursorLine?: (line: number) => void;
  onContentChange?: (value: string) => void;
}

// The Monaco editor over the session's published file. Monaco retains the local
// draft, cursor and undo. Language results are bounded SQL observations; only
// explicit save/check actions can update proof evidence. The surrounding chrome
// (status, goals, problems, verify) lives in the canvas drawer; this component
// reports through the callbacks above and is driven through its handle.
export const LiveEditor = forwardRef<LiveEditorHandle, LiveEditorProps>(function LiveEditor(props, ref) {
  const { sessionId, path, formalizationId, locked, captureSelection, followKey, marks } = props;
  const editorRef = useRef<HTMLDivElement>(null);
  const clientRef = useRef<EditorClient | null>(null);
  const editorApiRef = useRef<AnyEditor>(null);
  const monacoRef = useRef<MonacoApi | null>(null);
  const decorationsRef = useRef<AnyEditor>(null);
  const decorationIdsRef = useRef<string[]>([]);
  const leanQueryRef = useRef<LeanQuery | null>(null);
  const pathRef = useRef<string>('');
  const draftRef = useRef<EditorDraft | null>(null);
  const lockedRef = useRef(locked);
  const saveTimerRef = useRef<number | null>(null);
  const savingRef = useRef(false);
  const pendingRef = useRef(false);
  const suppressChangeRef = useRef(false);
  const runSaveRef = useRef<() => Promise<void>>(async () => {});
  // Re-run analysis + goal inspection (set once the editor is up). Paused while
  // a run holds the lock: the canvas then shows the persisted lean_check verdict
  // and the editor worker does not compete with Lea's own checks.
  const reanalyzeRef = useRef<() => void>(() => {});
  const saveStateRef = useRef<SaveState>({ state: 'clean', detail: '' });
  // Latest props in a ref so the once-registered listeners never read stale values.
  const propsRef = useRef(props);
  propsRef.current = props;
  lockedRef.current = locked;

  const [status, setStatus] = useState<EditorStatus>('loading');
  const setGoalSurface = useProofSession((s) => s.setGoalSurface);

  const emitSave = (next: SaveState | ((s: SaveState) => SaveState)) => {
    const value = typeof next === 'function' ? next(saveStateRef.current) : next;
    saveStateRef.current = value;
    propsRef.current.onSaveState?.(value);
  };
  const emitStatus = (next: EditorStatus, error?: string | null) => {
    setStatus(next);
    propsRef.current.onStatus?.(next, error ?? null);
  };

  const applyMarks = () => {
    const api = editorApiRef.current;
    const monaco = monacoRef.current;
    const model = api?.editor?.getModel?.();
    if (!api || !monaco || !model) return;
    const m = propsRef.current.marks ?? {};
    const count = model.getLineCount();
    const decos: AnyEditor[] = [];
    const push = (line: number | null | undefined, options: AnyEditor) => {
      if (line == null || line < 1 || line > count) return;
      decos.push({ range: new monaco.Range(line, 1, line, model.getLineMaxColumn(line)), options });
    };
    for (const line of m.changed ?? []) push(line, { isWholeLine: true, linesDecorationsClassName: 'cv-mark cv-mark-changed' });
    for (const line of m.sorries ?? []) push(line, { isWholeLine: true, className: 'cv-line-sorry', linesDecorationsClassName: 'cv-mark cv-mark-sorry' });
    for (const line of m.errors ?? []) {
      push(line, {
        isWholeLine: true,
        className: 'cv-line-error',
        linesDecorationsClassName: 'cv-mark cv-mark-error',
        lineNumberClassName: 'cv-ln-error',
      });
    }
    // The squiggle runs from the error's column to the end of its line (Monaco's
    // own markers already underline live diagnostics precisely).
    for (const at of m.errorAt ?? []) {
      if (at.line < 1 || at.line > count) continue;
      const end = model.getLineMaxColumn(at.line);
      const start = at.column && at.column < end ? at.column : model.getLineFirstNonWhitespaceColumn(at.line) || 1;
      if (start >= end) continue;
      decos.push({ range: new monaco.Range(at.line, start, at.line, end), options: { inlineClassName: 'cv-squiggle' } });
    }
    if (m.caret) {
      push(m.caret, {
        isWholeLine: true,
        className: 'cv-line-caret',
        linesDecorationsClassName: 'cv-mark cv-mark-caret',
        after: { content: m.caretLabel ?? 'Lea · editing', inlineClassName: 'cv-caret-chip' },
      });
    }
    if (decorationsRef.current) decorationsRef.current.set(decos);
    else decorationIdsRef.current = api.editor.deltaDecorations(decorationIdsRef.current, decos);
  };

  // Pull the latest published file into the buffer (after the agent edited it, or
  // on the Reload action). Suppress the resulting change event so it isn't treated
  // as a user edit. `silent` is the follow path: it never overwrites a draft that
  // changed underneath it and never reports success as a note.
  const reloadFromPublished = async ({ silent = false }: { silent?: boolean } = {}) => {
    const owned = captureSelection();
    const draft = draftRef.current;
    const api = editorApiRef.current;
    const client = clientRef.current;
    if (!draft || !api?.editor || !client) return;
    const before: string = api.editor.getValue();
    try {
      const info = await client.info();
      if (!owned() || draft !== draftRef.current) return;
      if (api.editor.getValue() !== before) {
        if (silent) return;
        throw new Error('The draft changed during reload. Reload again to replace it.');
      }
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
      pendingRef.current = false;
      draft.load(info);
      const next: string = info.content ?? '';
      if (next !== before) {
        suppressChangeRef.current = true;
        const editor = api.editor;
        const model = editor.getModel();
        if (model) {
          editor.pushUndoStop();
          editor.executeEdits('reload', [{ range: model.getFullModelRange(), text: next }]);
          editor.pushUndoStop();
        }
        suppressChangeRef.current = false;
        // A full-range replacement collapses the line decorations; redraw them
        // against the new content.
        applyMarks();
      }
      emitSave({ state: 'clean', detail: silent ? '' : 'reloaded published file' });
    } catch (err) {
      if (!owned()) return;
      emitSave((s) => ({ ...s, detail: err instanceof Error ? err.message : String(err) }));
    }
  };

  useEffect(() => {
    let disposed = false;
    let leanMonacoEditor: AnyEditor;
    let client: EditorClient;
    let analysisTimer: number;
    let goalTimer: number;
    let goalGeneration = 0;
    let analysisGeneration = 0;
    let environmentTimer: number;
    let refreshing = false;
    const draft = new EditorDraft(sessionId, formalizationId);
    draftRef.current = draft;
    savingRef.current = false;
    pendingRef.current = false;
    emitStatus('loading');
    emitSave({ state: 'clean', detail: '' });

    // Serialize saves: never let two writes overlap; if the buffer changed while a
    // save was in flight, run one more pass after it lands.
    const runSave = async () => {
      const api = editorApiRef.current;
      const onSaveFn = propsRef.current.onSave;
      if (!api || !onSaveFn || lockedRef.current) return;
      if (savingRef.current) {
        pendingRef.current = true;
        return;
      }
      const value: string | null = api.editor?.getValue?.() ?? null;
      if (value == null) return;
      savingRef.current = true;
      emitSave({ state: 'saving', detail: '' });
      try {
        const submitted = draft.prepare(value);
        const receipt = await onSaveFn(value, submitted.base.path, submitted.base, submitted.draft_id);
        if (disposed) return;
        if (!draft.acknowledge(submitted, receipt)) return;
        const publishedBase = draft.base;
        // Check is a separate queued operation. Publication remains saved even
        // when checking fails or a newer draft is typed while checking.
        const onCheckFn = propsRef.current.onCheck;
        if (onCheckFn) {
          void onCheckFn(publishedBase.path, publishedBase)
            .then((result) => {
              if (!disposed && draft.base === publishedBase && api.editor?.getValue?.() === value) {
                emitSave({
                  state: 'saved',
                  detail:
                    result.status === 'ok'
                      ? result.check_current === true
                        ? 'lean_check: 0 errors'
                        : 'recheck required'
                      : result.detail || `lean_check: ${result.status}`,
                });
              }
            })
            .catch((err) => {
              if (!disposed && draft.base === publishedBase && api.editor?.getValue?.() === value) {
                emitSave({ state: 'saved', detail: err instanceof Error ? err.message : 'Check unavailable' });
              }
            });
        }
        if (api.editor?.getValue?.() !== value) {
          pendingRef.current = true;
          emitSave({ state: 'dirty', detail: '' });
          return;
        }
        // The write always persists (a snapshot); the verdict just annotates it.
        emitSave({ state: 'saved', detail: onCheckFn ? 'checking published revision…' : 'published' });
      } catch (err) {
        if (disposed) return;
        emitSave({ state: 'dirty', detail: err instanceof Error ? err.message : String(err) });
      } finally {
        if (!disposed) savingRef.current = false;
        if (!disposed && pendingRef.current) {
          pendingRef.current = false;
          scheduleSave();
        }
      }
    };
    runSaveRef.current = runSave;

    const scheduleSave = () => {
      if (disposed || lockedRef.current) return;
      emitSave((s) => ({ state: 'dirty', detail: s.detail }));
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = window.setTimeout(() => void runSave(), AUTOSAVE_MS);
    };

    const analyze = () => {
      if (lockedRef.current) return;
      const generation = ++analysisGeneration;
      window.clearTimeout(analysisTimer);
      analysisTimer = window.setTimeout(async () => {
        try {
          const value = await client.request('editor.analyze');
          if (!disposed && generation === analysisGeneration) {
            leanMonacoEditor.diagnostics(value);
            propsRef.current.onDiagnostics?.(mapDiagnostics(value));
          }
        } catch {
          if (!disposed && generation === analysisGeneration) {
            leanMonacoEditor?.diagnostics({ diagnostics: [] });
            propsRef.current.onDiagnostics?.([]);
          }
        }
      }, 300);
    };
    const inspectGoal = () => {
      if (lockedRef.current) return;
      const generation = ++goalGeneration;
      window.clearTimeout(goalTimer);
      propsRef.current.onGoals?.('Waiting for goal analysis…');
      goalTimer = window.setTimeout(async () => {
        const pos = leanQueryRef.current?.cursor();
        if (!pos) return;
        propsRef.current.onCursorLine?.(pos.line + 1);
        try {
          const result = await client.request('editor.goals', pos);
          if (!disposed && generation === goalGeneration) {
            propsRef.current.onGoals?.(result?.rendered || result?.goals?.join('\n\n') || 'No goals at this position.');
          }
        } catch (err) {
          if (!disposed && generation === goalGeneration) {
            propsRef.current.onGoals?.(err instanceof Error ? err.message : 'Goal analysis unavailable.');
          }
        }
      }, 250);
    };
    (async () => {
      try {
        client = new EditorClient(sessionId, path, {
          formalizationId,
          onState: (state: string) => {
            if (!disposed) propsRef.current.onAnalysis?.(state);
          },
        });
        clientRef.current = client;
        const info = await client.open();
        if (disposed) {
          client.dispose();
          return;
        }
        draft.load(info);
        pathRef.current = info.path;
        const [{ createQueuedEditor }, monaco] = await Promise.all([import('../lib/queuedMonaco'), import('monaco-editor')]);
        if (disposed) return;
        monacoRef.current = monaco;
        leanMonacoEditor = createQueuedEditor(editorRef.current!, info.path, info.content ?? '', client);
        editorApiRef.current = leanMonacoEditor;
        leanQueryRef.current = new LeanQuery(client, leanMonacoEditor.editor);
        // Mockup metrics: 13px code on a 22px line; a 14px lane left of the
        // numbers carries the gutter marks (changed / error / sorry / caret).
        leanMonacoEditor.editor.updateOptions({
          readOnly: !!lockedRef.current,
          fontSize: 13,
          lineHeight: 22,
          lineDecorationsWidth: 14,
          glyphMargin: false,
          renderLineHighlight: 'none',
          padding: { top: 14, bottom: 14 },
        });
        reanalyzeRef.current = () => {
          analyze();
          inspectGoal();
        };
        decorationsRef.current = leanMonacoEditor.editor.createDecorationsCollection?.() ?? null;
        propsRef.current.onContentChange?.(info.content ?? '');
        leanMonacoEditor.editor.onDidChangeModelContent(() => {
          const value = leanMonacoEditor.editor.getValue();
          client.setContent(value);
          leanMonacoEditor.diagnostics({ diagnostics: [] });
          propsRef.current.onContentChange?.(value);
          if (!suppressChangeRef.current) scheduleSave();
          analyze();
          inspectGoal();
        });
        leanMonacoEditor.editor.onDidChangeCursorPosition(inspectGoal);
        emitStatus('ready');
        applyMarks();
        analyze();
        inspectGoal();
        environmentTimer = window.setInterval(async () => {
          if (disposed || refreshing) return;
          refreshing = true;
          try {
            if (await client.refresh()) {
              leanMonacoEditor.diagnostics({ diagnostics: [] });
              analyze();
              inspectGoal();
            }
          } catch {
            if (!disposed) {
              client.invalidate();
              leanMonacoEditor.diagnostics({ diagnostics: [] });
              propsRef.current.onGoals?.('Environment unavailable.');
            }
          } finally {
            refreshing = false;
          }
        }, 2000);
      } catch (err) {
        if (!disposed) emitStatus('error', err instanceof Error ? err.message : String(err));
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
      reanalyzeRef.current = () => {};
      decorationsRef.current = null;
      decorationIdsRef.current = [];
      leanQueryRef.current = null;
      try {
        leanMonacoEditor?.dispose();
      } catch {
        /* ignore */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, path, formalizationId]);

  // Reflect the modal lock onto the live editor when a run starts/stops, and
  // resume analysis once the run releases it.
  useEffect(() => {
    editorApiRef.current?.editor?.updateOptions({ readOnly: !!locked });
    if (!locked) reanalyzeRef.current();
  }, [locked]);

  // Redraw the line marks whenever they change (or once the editor is up).
  useEffect(() => {
    if (status === 'ready') applyMarks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [marks, status]);

  // Follow a newer published snapshot: a clean buffer reloads silently.
  const lastFollowRef = useRef(followKey);
  useEffect(() => {
    if (status !== 'ready') return;
    if (followKey === lastFollowRef.current) return;
    lastFollowRef.current = followKey;
    const state = saveStateRef.current.state;
    if (state === 'clean' || state === 'saved') void reloadFromPublished({ silent: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followKey, status]);

  useImperativeHandle(
    ref,
    () => ({
      async check() {
        const draft = draftRef.current;
        const api = editorApiRef.current;
        if (!draft || !api?.editor || lockedRef.current) return;
        if (saveTimerRef.current) {
          window.clearTimeout(saveTimerRef.current);
          saveTimerRef.current = null;
        }
        if (saveStateRef.current.state === 'dirty' || pendingRef.current) {
          await runSaveRef.current();
          return;
        }
        const onCheckFn = propsRef.current.onCheck;
        const base = draft.base;
        if (!onCheckFn || !base) return;
        const value: string = api.editor.getValue();
        emitSave({ state: 'saved', detail: 'checking published revision…' });
        try {
          const result = await onCheckFn(base.path, base);
          if (draftRef.current !== draft || api.editor.getValue() !== value) return;
          emitSave({
            state: 'saved',
            detail:
              result.status === 'ok'
                ? result.check_current === true
                  ? 'lean_check: 0 errors'
                  : 'recheck required'
                : result.detail || `lean_check: ${result.status}`,
          });
        } catch (err) {
          if (draftRef.current !== draft) return;
          emitSave({ state: 'saved', detail: err instanceof Error ? err.message : 'Check unavailable' });
        }
      },
      reload: () => reloadFromPublished(),
      jumpToLine(line: number) {
        const editor = editorApiRef.current?.editor;
        if (!editor) return;
        editor.revealLineInCenter(line);
        editor.setPosition({ lineNumber: line, column: 1 });
        editor.focus();
      },
      focus() {
        editorApiRef.current?.editor?.focus();
      },
      // Shared InfoView: ask the live LSP for the goal state at the cursor — the exact
      // goals the drawer shows — and surface it above the composer so the user can
      // ask Lea about it. Queries the human's own session (current buffer, even
      // mid-edit), so no save is required.
      async askAboutGoal() {
        const owned = captureSelection();
        const q = leanQueryRef.current;
        const pos = q?.cursor();
        if (!q || !pos) return 'Editor not ready.';
        if (!q.ready()) return 'Lean server still starting — try again in a moment.';
        try {
          const result = await q.plainGoal();
          if (!owned()) return null;
          const goals = result?.goals ?? [];
          const rendered = (result?.rendered ?? goals.join('\n\n')).trim();
          if (!result || !rendered || goals.length === 0) {
            // pos is the LSP 0-based position — the same numbering the InfoView shows.
            return `No goal at ${pos.line}:${pos.character} — click inside a tactic proof.`;
          }
          setGoalSurface({ rendered, line: pos.line });
          return null;
        } catch (err) {
          if (!owned()) return null;
          return err instanceof Error ? err.message : String(err);
        }
      },
      getValue: () => editorApiRef.current?.editor?.getValue?.() ?? null,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return (
    <div className="live-editor cv-live">
      <div ref={editorRef} className="cv-monaco" />
    </div>
  );
});
