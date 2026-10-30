import { useState } from 'react';
import { getProjectDocSnapshot, putProjectDoc, type ProjectGraph } from '../../lib/api';
import { appendNodeMarkdown, isValidNodeKey } from '../../lib/blueprintModel.js';
import { useWorkbench } from '../../stores/workbench';

// "Add node": appends a `## key` section (kind, uses, statement) to
// .lea/blueprint.md through putProjectDoc. Lives in the aside while open.

export function AddNodeForm({ projectId, graph, onClose }: { projectId: string; graph: ProjectGraph | null; onClose: () => void }) {
  const [key, setKey] = useState('');
  const [kind, setKind] = useState<'theorem' | 'lemma' | 'definition'>('lemma');
  const [uses, setUses] = useState<string[]>([]);
  const [statement, setStatement] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bump = useWorkbench((s) => s.bumpBlueprintRefresh);
  const selectNode = useWorkbench((s) => s.selectNode);
  const existing = new Set((graph?.nodes ?? []).map((n) => n.key));
  const trimmed = key.trim();
  const keyProblem = !trimmed
    ? null
    : !isValidNodeKey(trimmed)
      ? 'Use letters, digits and underscores, starting with a letter.'
      : existing.has(trimmed)
        ? 'A node with this key already exists.'
        : null;

  const submit = async () => {
    if (!trimmed || keyProblem) return;
    setBusy(true);
    setError(null);
    try {
      const snapshot = await getProjectDocSnapshot(projectId, 'blueprint');
      const next = appendNodeMarkdown(snapshot.content, { key: trimmed, kind, lean: null, uses, file: null, statement: statement.trim() });
      await putProjectDoc(projectId, 'blueprint', next, snapshot.file_revision);
      bump();
      selectNode(trimmed);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="bpv-aside" aria-label="Add node">
      <div className="bpv-aside-head">
        <span className="bpv-aside-title">Add node</span>
        <span className="bpv-aside-sub">appends a section to .lea/blueprint.md</span>
      </div>
      <form
        className="bpv-aside-body bpv-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="bpv-field">
          <span>Key</span>
          <input className="bpv-input bpv-mono" value={key} onChange={(e) => setKey(e.target.value)} placeholder="parity_descent" autoFocus />
          {keyProblem && <span className="bpv-field-err">{keyProblem}</span>}
        </label>
        <label className="bpv-field">
          <span>Kind</span>
          <select className="bpv-input" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            <option value="theorem">theorem</option>
            <option value="lemma">lemma</option>
            <option value="definition">definition</option>
          </select>
        </label>
        <label className="bpv-field">
          <span>Statement</span>
          <textarea
            className="bpv-input"
            rows={3}
            value={statement}
            onChange={(e) => setStatement(e.target.value)}
            placeholder="One or two sentences in plain language."
          />
        </label>
        {existing.size > 0 && (
          <div className="bpv-field">
            <span>Uses</span>
            <div className="bpv-checks">
              {[...existing].map((k) => (
                <label key={k} className="bpv-check">
                  <input
                    type="checkbox"
                    checked={uses.includes(k)}
                    onChange={(e) => setUses((cur) => (e.target.checked ? [...cur, k] : cur.filter((x) => x !== k)))}
                  />
                  <span className="bpv-mono">{k}</span>
                </label>
              ))}
            </div>
          </div>
        )}
        {error && <p className="bpv-note is-error">{error}</p>}
      </form>
      <div className="bpv-aside-foot">
        <button className="bpv-btn is-primary" onClick={() => void submit()} disabled={busy || !trimmed || !!keyProblem}>
          {busy ? 'Adding…' : 'Add node'}
        </button>
        <span className="bpv-spacer" />
        <button className="bpv-btn is-ghost" onClick={onClose} disabled={busy}>
          Cancel
        </button>
      </div>
    </aside>
  );
}
