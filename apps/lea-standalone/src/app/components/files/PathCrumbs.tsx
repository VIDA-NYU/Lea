import '../../../styles/files.css';

// Work package "files" (mockup frame 6). The path under the canvas title: one
// button per segment; a folder crumb opens a menu of its siblings with status
// dots. Canvas mounts it in its header. `path` is repo-relative (null while the
// canvas shows the session's own proof and no project file is open).
export function PathCrumbs({ projectId, path }: { projectId: string | null; path: string | null }) {
  return (
    <nav className="path-crumbs" aria-label="File path">
      <span className="path-crumbs-stub">{projectId ? path ?? 'proofs / —' : 'proofs / —'}</span>
    </nav>
  );
}
