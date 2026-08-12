import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Github, Loader2, X } from 'lucide-react';
import {
  confirmProjectGithubImport,
  getProjectGithubImport,
  GithubImportApiError,
  previewProjectGithubImport,
  type GithubImportDisposition,
  type GithubImportPreview,
  type GithubImportProgress,
} from '../lib/api';

const DISPOSITIONS: GithubImportDisposition[] = [
  'add',
  'already_present',
  'path_conflict',
  'declaration_conflict',
  'unsupported_module_layout',
  'excluded',
];

const LABELS: Record<GithubImportDisposition, string> = {
  add: 'Will add',
  already_present: 'Already present',
  path_conflict: 'Path conflicts',
  declaration_conflict: 'Declaration conflicts',
  unsupported_module_layout: 'Unsupported layout',
  excluded: 'Excluded',
};

export function GithubImportDialog({
  projectId,
  onClose,
  onCompleted,
  onSelectPath,
}: {
  projectId: string;
  onClose: () => void;
  onCompleted: () => void;
  onSelectPath: (path: string) => void;
}) {
  const [repositoryUrl, setRepositoryUrl] = useState('');
  const [preview, setPreview] = useState<GithubImportPreview | null>(null);
  const [progress, setProgress] = useState<GithubImportProgress | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const completedRef = useRef(false);

  const terminal = progress && ['complete', 'complete_with_issues', 'failed'].includes(progress.status);

  useEffect(() => {
    if (!progress || terminal) return;
    let cancelled = false;
    const poll = window.setInterval(() => {
      getProjectGithubImport(projectId, progress.id)
        .then((next) => !cancelled && setProgress(next))
        .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)));
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [projectId, progress?.id, terminal]);

  useEffect(() => {
    if (terminal && !completedRef.current && progress?.status !== 'failed') {
      completedRef.current = true;
      onCompleted();
    }
  }, [terminal, progress?.status, onCompleted]);

  const groups = useMemo(() => {
    const result = new Map<GithubImportDisposition, GithubImportPreview['plan']['files']>();
    for (const disposition of DISPOSITIONS) result.set(disposition, []);
    for (const file of preview?.plan.files ?? []) result.get(file.disposition)?.push(file);
    return result;
  }, [preview]);

  const analyze = async () => {
    if (!repositoryUrl.trim() || analyzing) return;
    setAnalyzing(true);
    setError(null);
    try {
      setPreview(await previewProjectGithubImport(projectId, repositoryUrl.trim()));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAnalyzing(false);
    }
  };

  const confirm = async () => {
    if (!preview || confirming) return;
    setConfirming(true);
    setError(null);
    try {
      const result = await confirmProjectGithubImport(projectId, preview.preview_id);
      setProgress(result);
    } catch (err) {
      if (err instanceof GithubImportApiError && err.code === 'import_preview_expired') {
        setPreview(null);
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setConfirming(false);
    }
  };

  const added = preview?.plan.counts.add ?? 0;
  const checkCounts = progress?.counts.checks ?? {};
  const eligible = progress?.files.filter((file) =>
    ['add', 'already_present'].includes(file.disposition),
  ) ?? [];

  return (
    <div className="ghi-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="ghi-dialog" role="dialog" aria-modal="true" aria-labelledby="ghi-title">
        <header className="ghi-head">
          <span className="ghi-icon"><Github size={17} /></span>
          <div>
            <h2 id="ghi-title">Add Lean files from GitHub</h2>
            <p>Existing project files are never overwritten.</p>
          </div>
          <button className="ghi-close" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </header>

        {!preview && !progress && (
          <div className="ghi-content">
            <label className="ghi-label" htmlFor="ghi-url">GitHub repository</label>
            <div className="ghi-url-row">
              <input
                id="ghi-url"
                value={repositoryUrl}
                onChange={(event) => setRepositoryUrl(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && analyze()}
                placeholder="https://github.com/owner/repository"
                spellCheck={false}
                autoFocus
              />
              <button onClick={analyze} disabled={!repositoryUrl.trim() || analyzing}>
                {analyzing ? <><Loader2 size={14} className="spin" /> Analyzing…</> : 'Analyze'}
              </button>
            </div>
            <p className="ghi-note">Only tracked <code>.lean</code> files are considered. Generated files, dependencies, symlinks, and Git LFS pointers are excluded.</p>
          </div>
        )}

        {preview && !progress && (
          <div className="ghi-content">
            <div className="ghi-source">
              <strong>{preview.source.owner}/{preview.source.repository}</strong>
              <code>{preview.source.commit_sha.slice(0, 10)}</code>
              <span>→ {preview.plan.destination_namespace}</span>
            </div>

            <div className="ghi-summary-grid">
              <span><strong>{preview.plan.counts.add ?? 0}</strong> to add</span>
              <span><strong>{preview.plan.counts.already_present ?? 0}</strong> already present</span>
              <span><strong>{(preview.plan.counts.path_conflict ?? 0) + (preview.plan.counts.declaration_conflict ?? 0)}</strong> conflicts</span>
              <span><strong>{preview.plan.reusable_declarations}</strong> reusable declarations</span>
            </div>

            <div className="ghi-files">
              {DISPOSITIONS.map((disposition) => {
                const files = groups.get(disposition) ?? [];
                if (!files.length) return null;
                return (
                  <details key={disposition} open={disposition === 'add' || disposition.includes('conflict')}>
                    <summary>{LABELS[disposition]} <span>{files.length}</span></summary>
                    <ul>
                      {files.map((file) => (
                        <li key={file.source_path}>
                          <code>{file.destination_path || file.source_path}</code>
                          <small>{file.reason}</small>
                        </li>
                      ))}
                    </ul>
                  </details>
                );
              })}
            </div>

            {(preview.plan.matched_declarations > 0 || preview.plan.reusable_declarations > 0) && (
              <div className="ghi-matches">
                {preview.plan.matched_declarations > 0 && (
                  <div>
                    <strong>Will populate formalizations</strong>
                    {preview.plan.files.flatMap((file) => file.declarations ?? [])
                      .filter((declaration) => declaration.match)
                      .map((declaration) => <code key={declaration.full_name}>{declaration.full_name}</code>)}
                  </div>
                )}
                {preview.plan.reusable_declarations > 0 && (
                  <div>
                    <strong>Reusable modules only</strong>
                    <span>No formalization or Blueprint node will be created for unmatched declarations.</span>
                  </div>
                )}
              </div>
            )}

            <footer className="ghi-actions">
              <button className="secondary" onClick={() => setPreview(null)}>Use another link</button>
              <button className="primary" onClick={confirm} disabled={confirming || Boolean(preview.plan.blocking_error)}>
                {confirming ? 'Starting…' : added ? `Add ${added} Lean file${added === 1 ? '' : 's'}` : 'Reconcile existing files'}
              </button>
            </footer>
            {preview.plan.blocking_error && <div className="ghi-error">{preview.plan.blocking_error.message}</div>}
          </div>
        )}

        {progress && (
          <div className="ghi-content">
            {!terminal ? (
              <div className="ghi-progress">
                <Loader2 size={24} className="spin" />
                <strong>{progress.status === 'applying' ? 'Adding Lean files…' : `Checking ${eligible.length} files…`}</strong>
                <span>{checkCounts.ok ?? 0} passed · {checkCounts.error ?? 0} failed · {checkCounts.pending ?? 0} pending</span>
              </div>
            ) : (
              <div className="ghi-complete">
                {progress.status === 'complete' ? <CheckCircle2 size={26} /> : <AlertTriangle size={26} />}
                <h3>{progress.reused ? 'This commit is already imported' : progress.status === 'complete' ? 'GitHub files added' : progress.status === 'failed' ? 'Import failed' : 'Import finished with issues'}</h3>
                <p>
                  {progress.counts.dispositions.add ?? 0} added · {progress.counts.dispositions.already_present ?? 0} already present · {(progress.counts.dispositions.path_conflict ?? 0) + (progress.counts.dispositions.declaration_conflict ?? 0)} conflicts skipped
                </p>
                <p>{progress.counts.matched_declarations} formalizations populated · {progress.counts.reusable_declarations} reusable declarations</p>
                {progress.error_detail && <pre>{progress.error_detail}</pre>}
                {progress.files.some((file) => file.check_status === 'error' && file.destination_path) && (
                  <div className="ghi-failures">
                    {progress.files.filter((file) => file.check_status === 'error' && file.destination_path).map((file) => (
                      <button key={file.source_path} onClick={() => { onSelectPath(file.destination_path!); onClose(); }}>
                        Open {file.destination_path}
                      </button>
                    ))}
                  </div>
                )}
                <footer className="ghi-actions"><button className="primary" onClick={onClose}>Done</button></footer>
              </div>
            )}
          </div>
        )}

        {error && <div className="ghi-error">{error}</div>}
      </section>
    </div>
  );
}
