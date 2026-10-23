import { useEffect, useState } from 'react';
import { cancelReformalization, getReformalization, type CodeStep, type Formalization } from '../lib/api';

export function ReformalizationDraft({ formalization, refresh }: {
  formalization?: Formalization; refresh: number;
}) {
  const operation = formalization?.reformalization;
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<CodeStep[]>([]);
  const [path, setPath] = useState('');
  const [error, setError] = useState('');
  const [stopping, setStopping] = useState(false);
  const [stopped, setStopped] = useState(false);
  useEffect(() => { setStopped(false); setStopping(false); }, [operation?.id]);
  useEffect(() => {
    if (!open || !operation) return;
    let live = true;
    getReformalization(operation.id).then(result => {
      if (live) { setFiles(result.drafts); setError(''); }
    }).catch(reason => { if (live) setError(String(reason.message || reason)); });
    return () => { live = false; };
  }, [open, operation?.id, operation?.state, refresh]);
  if (!operation || operation.state === 'settled') return null;
  if (['queued', 'active'].includes(operation.state) && !operation.has_drafts) {
    return <div className="retry-draft" role="status">
      <p>{stopped ? 'Stop requested. Current work is preserved.' : operation.state === 'queued'
        ? 'Reformalization is waiting to start.' : 'Reformalization is in progress.'}</p>
      {!stopped && <button type="button" disabled={stopping} onClick={async () => {
        setStopping(true);
        try { await cancelReformalization(operation.id); setStopped(true); }
        catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
        finally { setStopping(false); }
      }}>{stopping ? 'Stopping…' : 'Stop reformalization'}</button>}
      {error && <p role="alert">{error}</p>}
    </div>;
  }
  if (!operation.has_drafts) return null;
  const selected = files.find(file => file.path === path) || files[0];
  return <details className="retry-draft" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{operation.state === 'review' ? 'Draft awaiting review' : 'Retained reformalization draft'}</summary>
    <p>This draft is separate from the published proof shown below.</p>
    {operation.detail && <p>{operation.detail}</p>}
    {error && <p role="alert">{error}</p>}
    {!selected && !error && <p role="status">Loading draft…</p>}
    {selected && <>
      <label>Draft file <select value={selected.path} onChange={event => setPath(event.target.value)}>
        {files.map(file => <option key={file.path} value={file.path}>{file.path}</option>)}
      </select></label>
      {selected.baseline_blob_id && selected.blob_id && <p><a target="_blank" rel="noreferrer"
        href={`/api/reformalizations/${encodeURIComponent(operation.id)}/versions/${encodeURIComponent(selected.baseline_blob_id)}?compare_to=${encodeURIComponent(selected.blob_id)}`}>
        Compare with retained baseline
      </a></p>}
      <pre tabIndex={0} aria-label={`Draft source for ${selected.path}`}><code>{selected.code}</code></pre>
    </>}
  </details>;
}
