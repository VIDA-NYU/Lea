import '../../../styles/blueprint.css';
import { GitBranch } from 'lucide-react';
import { byKey, dependents } from '../../lib/blueprintModel.js';
import { useSessions } from '../../stores/sessions';
import { BLUEPRINT_PATH, useWorkbench } from '../../stores/workbench';
import { StatusDot, toneOf } from './StatusDot';
import { useProjectGraph } from './useProjectGraph';

// Work package "blueprint" (mockup frame 10). The strip above the editor for a
// session bound to a node: Blueprint › node · uses … · unblocks …, each dep a
// status pill that selects the node and opens the blueprint. Renders nothing for
// an unbound session. Canvas mounts it above the editor. The binding is the
// session's `blueprint_node`, else the UI binding (older sessions).
export function BlueprintStrip({ projectId, sessionId }: { projectId: string | null; sessionId: string | undefined }) {
  const served = useSessions((s) => (sessionId ? s.sessions.find((x) => x.id === sessionId)?.blueprint_node : undefined));
  const local = useWorkbench((s) => (sessionId ? s.nodeBindings[sessionId] : undefined));
  const nodeKey = served || local || undefined;
  const selectNode = useWorkbench((s) => s.selectNode);
  const openFile = useWorkbench((s) => s.openFile);
  const { graph } = useProjectGraph(projectId && nodeKey ? projectId : null);
  if (!projectId || !nodeKey) return null;

  const nodes = graph?.nodes ?? [];
  const index = byKey(nodes);
  const node = index.get(nodeKey);
  const uses = node ? node.uses.map((k) => index.get(k) ?? { key: k, status: 'planned' as const }) : [];
  const unblocks = node ? dependents(nodes, nodeKey) : [];

  const open = (key: string) => {
    selectNode(key);
    openFile(BLUEPRINT_PATH);
  };

  return (
    <div className="blueprint-strip" aria-label="Blueprint context">
      <button className="bpv-strip-root" onClick={() => open(nodeKey)} title="Open the blueprint">
        <GitBranch size={13} /> Blueprint
      </button>
      <span className="bpv-strip-sep">›</span>
      <button className="bpv-strip-node bpv-mono" onClick={() => open(nodeKey)} title="Show this node in the blueprint">
        {node && <StatusDot tone={toneOf(node)} />}
        {nodeKey}
      </button>
      {graph && !node && <span className="bpv-strip-muted">not in the blueprint any more</span>}
      {node && (
        <>
          <span className="bpv-strip-rule" />
          <span className="bpv-strip-muted">uses</span>
          {uses.length === 0 && <span className="bpv-strip-muted">nothing</span>}
          {uses.map((u) => (
            <button key={u.key} className={`bpv-pill is-${toneOf(u)} is-button`} onClick={() => open(u.key)} title={`${u.key}: ${u.status}`}>
              <StatusDot tone={toneOf(u)} />
              {u.key}
            </button>
          ))}
          <span className="bpv-strip-muted bpv-strip-gap">unblocks</span>
          {unblocks.length === 0 && <span className="bpv-strip-muted">nothing</span>}
          {unblocks.map((u) => (
            <button key={u.key} className={`bpv-pill is-${toneOf(u)} is-button`} onClick={() => open(u.key)} title={`${u.key}: ${u.status}`}>
              <StatusDot tone={toneOf(u)} />
              {u.key}
            </button>
          ))}
        </>
      )}
    </div>
  );
}
