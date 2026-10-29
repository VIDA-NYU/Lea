import '../../../styles/blueprint.css';
import { GitBranch } from 'lucide-react';
import { useSessions } from '../../stores/sessions';
import { BLUEPRINT_PATH, useWorkbench } from '../../stores/workbench';
import { StatusDot, toneOf } from './StatusDot';
import { useProjectGraph } from './useProjectGraph';

// Work package "blueprint" (mockup frame 10). The node chip in the chat header of a
// node-bound session: blueprint icon + mono key, coloured by the node's live status
// when the graph is known. Clicking opens the blueprint on that node. ChatThread
// mounts it next to the title.
export function NodeChip({ sessionId }: { sessionId: string | undefined }) {
  const nodeKey = useWorkbench((s) => (sessionId ? s.nodeBindings[sessionId] : undefined));
  const projectId = useSessions((s) => (sessionId ? s.sessions.find((x) => x.id === sessionId)?.project_id : undefined));
  const selectNode = useWorkbench((s) => s.selectNode);
  const openFile = useWorkbench((s) => s.openFile);
  const { graph } = useProjectGraph(nodeKey && projectId ? projectId : null);
  if (!nodeKey) return null;
  const node = graph?.nodes.find((n) => n.key === nodeKey);
  const tone = node ? toneOf(node) : 'planned';
  return (
    <button
      className={`chip node-chip is-${tone}`}
      title={`Bound to blueprint node ${nodeKey}${node ? ` · ${node.status}` : ''} — open the blueprint`}
      onClick={() => {
        selectNode(nodeKey);
        openFile(BLUEPRINT_PATH);
      }}
    >
      <GitBranch size={12} />
      <span className="bpv-mono">{nodeKey}</span>
      {node && <StatusDot tone={tone} />}
    </button>
  );
}
