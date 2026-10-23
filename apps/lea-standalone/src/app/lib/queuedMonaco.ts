// Monaco is browser-local. Language providers submit typed SQL-backed requests.
import * as monaco from 'monaco-editor';
import { EditorClient } from './editorClient.mjs';
let registered = false;
export function createQueuedEditor(element: HTMLElement, path: string, content: string, client: EditorClient) {
  if (!window.MonacoEnvironment) window.MonacoEnvironment = {
    getWorker: () => new Worker(new URL('monaco-editor/esm/vs/editor/editor.worker.js', import.meta.url), { type: 'module' }),
  };
  if (!registered) {
    monaco.languages.register({ id: 'lean4' });
    monaco.languages.setMonarchTokensProvider('lean4', { tokenizer: { root: [
      [/--.*$/, 'comment'], [/\b(theorem|lemma|def|by|import|namespace|end|example|inductive|structure|where|let|have|show|from|fun|match|with)\b/, 'keyword'],
      [/"([^"\\]|\\.)*"/, 'string'], [/\b\d+\b/, 'number'],
    ] } });
    registered = true;
  }
  const model = monaco.editor.createModel(content, 'lean4', monaco.Uri.parse(`inmemory://lea/${encodeURIComponent(client.sessionId)}/${encodeURIComponent(path)}/${crypto.randomUUID()}`));
  const editor = monaco.editor.create(element, { model, automaticLayout: true, minimap: { enabled: false }, fontSize: 14, scrollBeyondLastLine: false });
  const position = (p: monaco.Position) => ({ line: p.lineNumber - 1, character: p.column - 1 });
  const range = (r: { start: { line: number; character: number }; end: { line: number; character: number } }) => new monaco.Range(r.start.line + 1, r.start.character + 1, r.end.line + 1, r.end.character + 1);
  const hover = monaco.languages.registerHoverProvider('lean4', { async provideHover(target, p, token) {
    if (target !== model) return null;
    try {
      const value = await client.request('editor.hover', position(p));
      if (token.isCancellationRequested || !value) return null;
      const parts = Array.isArray(value.contents) ? value.contents : [value.contents];
      return { contents: parts.filter(Boolean).map((v: string | { value: string }) => ({ value: typeof v === 'string' ? v : v.value, isTrusted: false })), ...(value.range ? { range: range(value.range) } : {}) };
    } catch { return null; }
  } });
  const completion = monaco.languages.registerCompletionItemProvider('lean4', { triggerCharacters: ['.'], async provideCompletionItems(target, p, _context, token) {
    if (target !== model) return { suggestions: [] };
    try {
      const value = await client.request('editor.complete', position(p));
      if (token.isCancellationRequested) return { suggestions: [] };
      const word = model.getWordUntilPosition(p), fallback = new monaco.Range(p.lineNumber, word.startColumn, p.lineNumber, word.endColumn);
      return { incomplete: value?.isIncomplete, suggestions: (value?.items || []).map((item: { label: string; detail?: string; insertText?: string; insertTextFormat?: number; textEdit?: { range?: Parameters<typeof range>[0]; newText: string } }) => ({
        label: item.label, kind: monaco.languages.CompletionItemKind.Text, detail: item.detail,
        insertText: item.textEdit?.newText || item.insertText || item.label,
        insertTextRules: item.insertTextFormat === 2 ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
        range: item.textEdit?.range ? range(item.textEdit.range) : fallback,
      })) };
    } catch { return { suggestions: [] }; }
  } });
  return { editor, diagnostics(value: { diagnostics?: Array<{ range: Parameters<typeof range>[0]; severity: number; message: string }> }) {
    monaco.editor.setModelMarkers(model, 'lea-editor', (value?.diagnostics || []).filter(d => d.range).map(d => ({
      ...range(d.range), severity: ({ 1: monaco.MarkerSeverity.Error, 2: monaco.MarkerSeverity.Warning, 3: monaco.MarkerSeverity.Info } as Record<number, number>)[d.severity] || monaco.MarkerSeverity.Hint, message: String(d.message),
    })));
  }, dispose() { hover.dispose(); completion.dispose(); editor.dispose(); model.dispose(); } };
}
