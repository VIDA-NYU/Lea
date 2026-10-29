import '../../../styles/shell.css';

// Work package "shell" (mockup frame 11). The launch screen: "What do you want
// to prove?" (a statement becomes a project), project cards with the blueprint
// bar, recent sessions. App renders it for view === 'home'.
export function Home({
  onOpenProject,
  onStartProject,
  onNewProject,
}: {
  onOpenProject: (projectId: string) => void;
  /** Create a project named from the statement, its first session, and start the run. */
  onStartProject: (statement: string) => void;
  onNewProject: () => void;
}) {
  void onOpenProject;
  void onStartProject;
  void onNewProject;
  return (
    <section className="home" aria-label="Projects">
      <div className="home-stub">Home — not built yet</div>
    </section>
  );
}
