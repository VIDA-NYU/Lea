import { useState } from 'react';

export type FileMeta = { path: string; isScratch: boolean; latestIndex: number; count: number };

function baseName(path: string): string {
  return path.split('/').pop() || path;
}

// The session's files, next to the crumbs: a session can touch several files (a
// main proof plus throwaway `scratch` probes). Picking one shows its latest
// snapshot and makes it the file lean_check / SafeVerify act on. Main
// (non-scratch) files first, then a dimmed "scratch" group.
export function FileSelect({
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
      type="button"
      role="menuitemradio"
      aria-checked={f.path === current}
      className={`cv-file-row ${f.path === current ? 'active' : ''} ${f.isScratch ? 'scratch' : ''}`}
      onClick={() => pick(f.path)}
      title={f.path}
    >
      <span className="cv-file-row-name mono">{baseName(f.path)}</span>
      {f.path === mainPath && <span className="cv-file-tag">main</span>}
      {f.isScratch && <span className="cv-file-tag">scratch</span>}
    </button>
  );
  return (
    <div className="cv-file-select">
      <button
        type="button"
        className="cv-file-btn"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Files this session touched"
      >
        {files.length} files <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <>
          <div className="cv-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="cv-file-menu" role="menu" aria-label="Session files">
            {mains.map(row)}
            {scratch.length > 0 && <div className="cv-menu-group">scratch</div>}
            {scratch.map(row)}
          </div>
        </>
      )}
    </div>
  );
}
