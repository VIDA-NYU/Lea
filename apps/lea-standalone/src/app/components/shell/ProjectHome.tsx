import '../../../styles/shell.css';

// Work package "shell" (mockup frame 12). The project home: description and
// progress bar, "Up next" (ready and sorry nodes with Prove), sessions with the
// node each owns, files, Overleaf, git, instructions, memory. App renders it for
// view === 'project-home'.
export function ProjectHome({
  projectId,
  onOpenSession,
  onNewSession,
  onOpenBlueprint,
  onOpenFiles,
}: {
  projectId: string;
  onOpenSession: (sessionId: string) => void;
  onNewSession: () => void;
  /** Open the blueprint in the canvas (files store: openFile(BLUEPRINT_PATH)). */
  onOpenBlueprint: () => void;
  /** Open a project file in the canvas with the rail shown. */
  onOpenFiles: (path?: string) => void;
}) {
  void onOpenSession;
  void onNewSession;
  void onOpenBlueprint;
  void onOpenFiles;
  return (
    <section className="project-home" aria-label="Project overview">
      <div className="project-home-stub">Project home ({projectId}) — not built yet</div>
    </section>
  );
}
