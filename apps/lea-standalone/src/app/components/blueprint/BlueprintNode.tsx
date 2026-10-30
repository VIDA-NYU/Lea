import { memo } from 'react';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import type { GraphNode } from '../../lib/api';
import { StatusDot, type Tone } from './StatusDot';

// The card React Flow draws for a blueprint node (mockup frames 8–9): the key in
// mono, a status dot with "kind · status" (never hue alone), the file name in small
// text when the node has one, a running badge when a bound session is live, a
// dashed border for planned nodes and for every node in a proposal, and a ring
// when selected. Size and handle geometry come from lib/blueprintFlow.js; the two
// Handles here (target on top, source at the bottom) only exist so React Flow
// knows the node's connection points — they are invisible and not connectable.

export interface BlueprintNodeData extends Record<string, unknown> {
  node: GraphNode;
  tone: Tone;
  statusText: string;
  label: string;
  running: boolean;
  proposal: boolean;
  fileName: string | null;
}

export type BlueprintFlowNode = Node<BlueprintNodeData, 'blueprintNode'>;

function BlueprintNodeCard({ data, selected }: NodeProps<BlueprintFlowNode>) {
  const { node, tone, label, running, proposal, fileName } = data;
  const classes = [
    'bpv-fnode',
    `is-${tone}`,
    proposal ? 'is-proposal' : '',
    selected ? 'is-selected' : '',
    running ? 'is-running' : '',
    fileName ? 'has-file' : '',
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <div className={classes} title={node.statement ? `${node.key}\n${node.statement}` : node.key}>
      <Handle type="target" position={Position.Top} isConnectable={false} className="bpv-fhandle" />
      <span className="bpv-fnode-key">{node.key}</span>
      <span className="bpv-fnode-sub">
        <StatusDot tone={tone} />
        <span className="bpv-fnode-label">{label}</span>
      </span>
      {fileName && (
        <span className="bpv-fnode-file" title={node.file ?? undefined}>
          {fileName}
        </span>
      )}
      {running && <span className="bpv-fnode-badge" aria-hidden="true" />}
      <Handle type="source" position={Position.Bottom} isConnectable={false} className="bpv-fhandle" />
    </div>
  );
}

export const BlueprintNode = memo(BlueprintNodeCard);
