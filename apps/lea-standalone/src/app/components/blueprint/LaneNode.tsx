import { memo } from 'react';
import type { Node, NodeProps } from '@xyflow/react';

// A By-file lane (mockup frame 9): a background rectangle around the nodes of one
// file on one row, with the file name and node count in a header strip. The
// dashed variant holds nodes with no file yet. Lanes are inert — not selectable,
// not focusable, pointer events off (set on the node in lib/blueprintFlow.js) —
// and sit below edges and cards (zIndex -1).

export interface LaneNodeData extends Record<string, unknown> {
  label: string;
  file: string | null;
  dashed: boolean;
  count: number;
  level: number;
}

export type LaneFlowNode = Node<LaneNodeData, 'laneNode'>;

function LaneBox({ data }: NodeProps<LaneFlowNode>) {
  // A one-node lane is only a card wide, so the count is a bare number (as in the
  // mockup), worded for screen readers. The label takes pointer events back so a
  // truncated path shows in full on hover.
  const nodes = data.count === 1 ? 'node' : 'nodes';
  return (
    <div className={`bpv-flane${data.dashed ? ' is-dashed' : ''}`}>
      <div className="bpv-flane-head">
        <span className="bpv-flane-label" title={`${data.label} · ${data.count} ${nodes}`}>
          {data.label}
        </span>
        <span className="bpv-flane-count">
          {data.count}
          <span className="bpv-sr-only"> {nodes}</span>
        </span>
      </div>
    </div>
  );
}

export const LaneNode = memo(LaneBox);
