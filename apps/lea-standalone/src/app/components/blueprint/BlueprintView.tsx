import '../../../styles/blueprint.css';

// Work package "blueprint" (mockup frames 8–9). The blueprint as a document in
// the canvas: Graph (by level / by file lanes) and Markdown views, a proposal
// state for a freshly drafted blueprint, and the node detail panel (statement,
// lean name, file:line, uses / unblocks with live status, sessions, actions).
// Canvas mounts it instead of the editor when useWorkbench().openPath is
// BLUEPRINT_PATH.
export function BlueprintView({
  projectId,
  onOpenSession,
}: {
  projectId: string;
  onOpenSession: (sessionId: string) => void;
}) {
  void onOpenSession;
  return (
    <section className="blueprint-view" aria-label="Blueprint">
      <div className="blueprint-stub">Blueprint view (project {projectId}) — not built yet</div>
    </section>
  );
}
