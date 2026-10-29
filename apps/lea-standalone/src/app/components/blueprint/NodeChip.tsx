import '../../../styles/blueprint.css';
import { useWorkbench } from '../../stores/workbench';

// Work package "blueprint" (mockup frame 10). The node chip in the chat header
// of a node-bound session. ChatThread mounts it next to the title.
export function NodeChip({ sessionId }: { sessionId: string | undefined }) {
  const nodeKey = useWorkbench((s) => (sessionId ? s.nodeBindings[sessionId] : undefined));
  if (!nodeKey) return null;
  return <span className="chip node-chip" title="This session is bound to a blueprint node">{nodeKey}</span>;
}
