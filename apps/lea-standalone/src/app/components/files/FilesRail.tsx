import '../../../styles/files.css';

// Work package "files" (docs/PLAN-ui-redesign.md, mockup frame 5). The rail at
// the canvas's left edge: project switcher, "Go to file", the files Lea touched
// this session, the project tree with per-file status dots, a row context menu.
// Canvas mounts it when useWorkbench().filesRailOpen is true.
export function FilesRail({ projectId }: { projectId: string }) {
  return (
    <aside className="files-rail" aria-label="Files">
      <div className="files-rail-stub">Files rail (project {projectId}) — not built yet</div>
    </aside>
  );
}
