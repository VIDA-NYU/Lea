import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ChevronRight,
  Download,
  File as FileIcon,
  FileDown,
  Folder,
  FolderOpen,
  Github,
  Pencil,
  UploadCloud,
} from 'lucide-react';
import {
  getProject,
  getProjectFile,
  getProjectTree,
  getSettings,
  projectExportUrl,
  pushProject,
  putProjectFile,
  setProjectRemote,
  type TreeEntry,
} from '../lib/api';
import { GithubImportDialog } from './GithubImportDialog';

// The Filesystem tab (F8 / D34) — "see everything, edit, download, share." The project
// is already a git repo, so this exposes it: a tree of the repo (left), a viewer/editor
// for the selected file (right; write+commit, with a `.lean` check verdict), an Export
// button (zip), and a Share bar (6b) — set a GitHub remote + explicit Push using the
// token from Settings (the token is injected into the push URL only, never persisted).

type CheckVerdict = { status: 'ok' | 'error'; detail?: string | null } | null;

function collectDirPaths(entries: TreeEntry[], into: string[] = []): string[] {
  for (const e of entries) {
    if (e.type === 'dir') {
      into.push(e.path);
      collectDirPaths(e.children ?? [], into);
    }
  }
  return into;
}

export function FilesystemTab({
  projectId,
  refreshSignal = 0,
  onProjectChanged,
}: {
  projectId: string;
  refreshSignal?: number;
  onProjectChanged?: () => void;
}) {
  const [tree, setTree] = useState<TreeEntry[] | null>(null);
  const [treeError, setTreeError] = useState<string | null>(null);
  const [treeLoading, setTreeLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const seededRef = useRef<string | null>(null);

  const [selected, setSelected] = useState<string | null>(null);

  // Share-to-GitHub (6b/D34): the project's saved remote + whether a token is set.
  const [showShare, setShowShare] = useState(false);
  const [remoteUrl, setRemoteUrl] = useState('');
  const [savedRemote, setSavedRemote] = useState('');
  const [tokenConfigured, setTokenConfigured] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [shareMsg, setShareMsg] = useState<string | null>(null);
  const [shareErr, setShareErr] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);

  // Load the project's saved remote + whether a GitHub token is configured (drives
  // the Push button's enabled state + the "add a token" hint).
  useEffect(() => {
    let cancelled = false;
    Promise.all([getProject(projectId), getSettings().catch(() => null)])
      .then(([project, settings]) => {
        if (cancelled) return;
        setSavedRemote(project.remote_url ?? '');
        setRemoteUrl(project.remote_url ?? '');
        setTokenConfigured(Boolean(settings?.github_token?.configured));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const saveRemote = async () => {
    setShareErr(null);
    setShareMsg(null);
    try {
      const res = await setProjectRemote(projectId, remoteUrl.trim());
      setSavedRemote(res.remote_url);
      setRemoteUrl(res.remote_url);
      setShareMsg('Remote saved.');
    } catch (err) {
      setShareErr(err instanceof Error ? err.message : String(err));
    }
  };

  const push = async () => {
    if (!savedRemote) return;
    if (!window.confirm(`Push this project to ${savedRemote}?\n\nThis pushes your local commits to the repo's main branch.`)) return;
    setSharing(true);
    setShareErr(null);
    setShareMsg(null);
    try {
      const res = await pushProject(projectId);
      setShareMsg(`Pushed to ${res.remote_url}.`);
    } catch (err) {
      setShareErr(err instanceof Error ? err.message : String(err));
    } finally {
      setSharing(false);
    }
  };

  // Load + live-refresh the tree. Auto-expand every dir once per project (the repo is
  // small), then preserve the user's toggles across later refreshes.
  useEffect(() => {
    let cancelled = false;
    setTreeLoading(true);
    getProjectTree(projectId)
      .then((rows) => {
        if (cancelled) return;
        setTree(rows);
        setTreeError(null);
        if (seededRef.current !== projectId) {
          setExpanded(new Set(collectDirPaths(rows)));
          seededRef.current = projectId;
        }
      })
      .catch((err) => !cancelled && setTreeError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setTreeLoading(false));
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshSignal]);

  const toggle = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  // After a save, refetch the tree so a newly-created file/dir appears.
  const [treeBump, setTreeBump] = useState(0);
  useEffect(() => {
    if (treeBump === 0) return;
    let cancelled = false;
    getProjectTree(projectId)
      .then((rows) => !cancelled && setTree(rows))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [treeBump, projectId]);

  return (
    <div className="fs">
      <div className="fs-bar">
        <span className="fs-bar-hint">The project repo — browse, edit any file, download, share.</span>
        <button
          className="fs-import-toggle"
          onClick={() => setShowImport(true)}
          title="Add non-conflicting Lean files from a GitHub repository"
        >
          <FileDown size={13} /> Import
        </button>
        <button
          className={`fs-share-toggle${showShare ? ' is-open' : ''}`}
          onClick={() => setShowShare((s) => !s)}
          title="Share this project to GitHub"
        >
          <Github size={13} /> Share
        </button>
        <a className="fs-export" href={projectExportUrl(projectId)} download title="Download the whole project as a zip">
          <Download size={13} /> Export
        </a>
      </div>

      {showShare && (
        <div className="fs-share">
          <div className="fs-share-row">
            <input
              className="fs-share-input"
              value={remoteUrl}
              placeholder="https://github.com/you/repo"
              onChange={(e) => setRemoteUrl(e.target.value)}
              spellCheck={false}
            />
            <button
              className="fs-share-save"
              onClick={saveRemote}
              disabled={!remoteUrl.trim() || remoteUrl.trim() === savedRemote}
            >
              Save remote
            </button>
            <button
              className="fs-share-push"
              onClick={push}
              disabled={!savedRemote || !tokenConfigured || sharing}
              title={
                !tokenConfigured
                  ? 'Add a GitHub token in Settings to push'
                  : !savedRemote
                  ? 'Save a remote first'
                  : 'Push to GitHub'
              }
            >
              <UploadCloud size={13} /> {sharing ? 'Pushing…' : 'Push to GitHub'}
            </button>
          </div>
          {!tokenConfigured && (
            <div className="fs-share-hint">Add a GitHub token in Settings to enable Push.</div>
          )}
          {shareMsg && <div className="fs-share-ok">{shareMsg}</div>}
          {shareErr && <div className="fs-share-err">{shareErr}</div>}
        </div>
      )}

      {showImport && (
        <GithubImportDialog
          projectId={projectId}
          onClose={() => setShowImport(false)}
          onCompleted={() => {
            setTreeBump((value) => value + 1);
            onProjectChanged?.();
          }}
          onSelectPath={(path) => setSelected(path)}
        />
      )}

      <div className="fs-body">
        <div className="fs-tree">
          {treeLoading && !tree ? (
            <div className="fs-muted">Loading…</div>
          ) : treeError ? (
            <div className="fs-muted fs-err">{treeError}</div>
          ) : !tree || tree.length === 0 ? (
            <div className="fs-muted">Empty repo.</div>
          ) : (
            <TreeNodes
              entries={tree}
              depth={0}
              expanded={expanded}
              onToggle={toggle}
              selected={selected}
              onSelect={setSelected}
            />
          )}
        </div>

        <div className="fs-viewer">
          {selected ? (
            <FileViewer
              key={selected}
              projectId={projectId}
              path={selected}
              onSaved={() => setTreeBump((b) => b + 1)}
            />
          ) : (
            <div className="fs-empty">Select a file to view or edit it.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function TreeNodes({
  entries,
  depth,
  expanded,
  onToggle,
  selected,
  onSelect,
}: {
  entries: TreeEntry[];
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  return (
    <>
      {entries.map((e) => {
        const pad = 8 + depth * 14;
        if (e.type === 'dir') {
          const open = expanded.has(e.path);
          return (
            <div key={e.path}>
              <button className="fs-row fs-dir" style={{ paddingLeft: pad }} onClick={() => onToggle(e.path)}>
                <ChevronRight size={13} className={`fs-chevron ${open ? 'open' : ''}`} />
                {open ? <FolderOpen size={14} /> : <Folder size={14} />}
                <span className="fs-name">{e.name}</span>
              </button>
              {open && e.children && e.children.length > 0 && (
                <TreeNodes
                  entries={e.children}
                  depth={depth + 1}
                  expanded={expanded}
                  onToggle={onToggle}
                  selected={selected}
                  onSelect={onSelect}
                />
              )}
            </div>
          );
        }
        return (
          <button
            key={e.path}
            className={`fs-row fs-file ${selected === e.path ? 'active' : ''}`}
            style={{ paddingLeft: pad + 17 }}
            onClick={() => onSelect(e.path)}
          >
            <FileIcon size={13} />
            <span className="fs-name">{e.name}</span>
          </button>
        );
      })}
    </>
  );
}

function FileViewer({
  projectId,
  path,
  onSaved,
}: {
  projectId: string;
  path: string;
  onSaved: () => void;
}) {
  const [content, setContent] = useState('');
  const [binary, setBinary] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [check, setCheck] = useState<CheckVerdict>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setEditing(false);
    setCheck(null);
    getProjectFile(projectId, path)
      .then((f) => {
        if (cancelled) return;
        setContent(f.content);
        setBinary(f.binary);
        setError(null);
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [projectId, path]);

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const result = await putProjectFile(projectId, path, draft);
      setContent(draft);
      setCheck(result.check ?? null);
      setEditing(false);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fs-file-pane">
      <div className="fs-view-head">
        <span className="fs-view-path" title={path}>{path}</span>
        {!binary && !editing && (
          <button
            className="fs-edit"
            onClick={() => {
              setDraft(content);
              setCheck(null);
              setEditing(true);
            }}
          >
            <Pencil size={12} /> Edit
          </button>
        )}
      </div>

      {check && (
        <div className={`fs-check ${check.status === 'ok' ? 'ok' : 'err'}`}>
          {check.status === 'ok' ? '✓ lean_check passed' : '✗ lean_check failed'}
          {check.status === 'error' && check.detail ? <pre className="fs-check-detail">{check.detail}</pre> : null}
        </div>
      )}

      {loading ? (
        <div className="fs-muted">Loading…</div>
      ) : error && !editing ? (
        <div className="fs-muted fs-err">{error}</div>
      ) : binary ? (
        <div className="fs-empty">
          This file isn’t text. <a href={projectExportUrl(projectId)} download>Export the project</a> to get it.
        </div>
      ) : editing ? (
        <div className="fs-editor">
          <textarea
            className="fs-textarea"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            autoFocus
          />
          {error && <div className="fs-muted fs-err">{error}</div>}
          <div className="fs-editor-foot">
            <button className="fs-cancel" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
            <button className="fs-save" onClick={save} disabled={saving}>
              {saving ? 'Saving…' : 'Save & commit'}
            </button>
          </div>
        </div>
      ) : (
        <pre className="fs-code">{content || '(empty file)'}</pre>
      )}
    </div>
  );
}
