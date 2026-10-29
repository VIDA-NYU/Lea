import '../../../styles/blueprint.css';
import { useWorkbench } from '../../stores/workbench';

// Work package "blueprint" (mockup frame 10). The strip above the editor for a
// session bound to a node: Blueprint › node · uses … · unblocks …, each dep a
// status pill that selects the node. Renders nothing for an unbound session.
// Canvas mounts it above the editor.
export function BlueprintStrip({ projectId, sessionId }: { projectId: string | null; sessionId: string | undefined }) {
  const nodeKey = useWorkbench((s) => (sessionId ? s.nodeBindings[sessionId] : undefined));
  if (!projectId || !nodeKey) return null;
  return (
    <div className="blueprint-strip" aria-label="Blueprint context">
      <span className="blueprint-strip-stub">Blueprint › {nodeKey} — strip not built yet</span>
    </div>
  );
}
