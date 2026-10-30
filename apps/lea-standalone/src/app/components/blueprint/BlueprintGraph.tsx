import '@xyflow/react/dist/style.css';
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
  type MutableRefObject,
} from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  ViewportPortal,
  getViewportForBounds,
  useReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
  type NodeTypes,
} from '@xyflow/react';
import type { ProjectGraph } from '../../lib/api';
import { NODE_W, NODE_H } from '../../../../../../packages/lea-blueprint/blueprintLayout.mjs';
import {
  BLUEPRINT_NODE,
  LANE_NODE,
  firstKey,
  flowEdges,
  flowNodes,
  graphLayout,
  isLaneId,
  neighbourKey,
  nodeCenter,
} from '../../lib/blueprintFlow.js';
import { imageFrame, solidBackground } from '../../lib/blueprintExport.js';
import { BlueprintNode, type BlueprintNodeData } from './BlueprintNode';
import { LaneNode } from './LaneNode';

// The dependency graph (mockup frames 8–9) as a React Flow canvas. Positions come
// from the shared layout module (packages/lea-blueprint/blueprintLayout.mjs) through
// lib/blueprintFlow.js — `computeLayout` for By level, `computeFileLaneLayout` for
// By file — so this component never lays anything out itself. Cards are custom
// `blueprintNode`s, By-file lanes are inert `laneNode`s behind them, and edges run
// from a dependent's bottom to its dependency's top with an arrowhead.
//
// Selection lives in the workbench store (`selectedKey` / `onSelect`); this view
// only reflects it. Click a card to select it (again to clear), click the pane or
// press Escape to clear. Arrow keys walk the graph: ↑ to a dependent, ↓ to a
// dependency, ←/→ along the row; Enter/Space select the focused card. A selection
// that arrives from outside (the strip pill, the sidebar, a Uses/Unblocks link)
// pans the view to that node.
//
// Lazy-loaded by BlueprintView (the default export), so React Flow and
// html-to-image stay out of the main bundle.

export type Grouping = 'level' | 'file';

export interface BlueprintGraphHandle {
  /** The whole graph as a PNG (2× pixel ratio, fitted to every node, on the canvas background). */
  toPng: () => Promise<Blob>;
}

export interface BlueprintGraphProps {
  graph: ProjectGraph;
  grouping: Grouping;
  selectedKey: string | null;
  runningKeys: Set<string>;
  proposal?: boolean;
  onSelect: (key: string | null) => void;
  /** Set while the graph is mounted, so the toolbar's Download menu can export it. */
  handleRef?: MutableRefObject<BlueprintGraphHandle | null>;
}

const nodeTypes: NodeTypes = { [BLUEPRINT_NODE]: BlueprintNode, [LANE_NODE]: LaneNode };

// Fit leaves room along the bottom for the controls and the minimap, which float
// over the canvas, so no node starts hidden under them.
const FIT = {
  padding: { top: '32px', left: '32px', right: '32px', bottom: '128px' },
  maxZoom: 1.1,
} as const;
const MIN_ZOOM = 0.1;
const MAX_ZOOM = 2;
const IMAGE_MARGIN = 32;
const MINIMAP_SIZE = { width: 150, height: 96 };

const ARROWS = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' } as const;

function minimapColor(node: Node): string {
  if (node.type === LANE_NODE) return 'var(--bpv-mm-lane)';
  const tone = (node.data as BlueprintNodeData).tone;
  return `var(--bpv-${tone})`;
}

const SVG_PAINT = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linecap', 'stroke-linejoin', 'opacity', 'fill-opacity', 'stroke-opacity'];

/**
 * html-to-image copies computed styles onto every cloned HTML element but deep-clones
 * an <svg> as is, so its children lose the stylesheet (edges would come out without
 * a stroke, arrowheads black). Inline their paint for the capture; returns the undo.
 */
function inlineSvgPaint(root: Element): () => void {
  const saved: [SVGElement, string | null][] = [];
  root.querySelectorAll('svg *').forEach((el) => {
    if (!(el instanceof SVGElement)) return;
    const computed = getComputedStyle(el);
    saved.push([el, el.getAttribute('style')]);
    for (const prop of SVG_PAINT) el.style.setProperty(prop, computed.getPropertyValue(prop));
  });
  return () => {
    for (const [el, style] of saved) {
      if (style === null) el.removeAttribute('style');
      else el.setAttribute('style', style);
    }
  };
}

export function BlueprintGraph(props: BlueprintGraphProps) {
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  );
}

export default BlueprintGraph;

function Flow({ graph, grouping, selectedKey, runningKeys, proposal = false, onSelect, handleRef }: BlueprintGraphProps) {
  const rf = useReactFlow();
  const wrapRef = useRef<HTMLDivElement>(null);
  const uid = useId().replace(/[^A-Za-z0-9_-]/g, '');
  const marker = `bpv-arrow-${uid}`;
  const activeMarker = `bpv-arrow-on-${uid}`;
  const helpId = `bpv-flow-help-${uid}`;

  const layout = useMemo(() => graphLayout(graph, grouping), [graph, grouping]);
  const nodes = useMemo(
    () => flowNodes(layout, { selectedKey, runningKeys, proposal }) as unknown as Node[],
    [layout, selectedKey, runningKeys, proposal],
  );
  const edges = useMemo(
    () => flowEdges(graph, { selectedKey, marker, activeMarker }) as unknown as Edge[],
    [graph, selectedKey, marker, activeMarker],
  );

  // Refs for handlers that must not re-subscribe on every refetch.
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const selectedRef = useRef(selectedKey);
  selectedRef.current = selectedKey;
  /** A selection made here (click, keys) — it must not re-centre the view. */
  const ownSelection = useRef<string | null | undefined>(undefined);
  const readyRef = useRef(false);
  const [ready, setReady] = useState(false);

  const select = useCallback(
    (key: string | null) => {
      ownSelection.current = key;
      onSelect(key);
    },
    [onSelect],
  );

  const centerOn = useCallback(
    (key: string, options: { duration?: number; keepZoom?: boolean } = {}) => {
      const c = nodeCenter(layoutRef.current, key);
      if (!c) return Promise.resolve(false);
      const current = rf.getZoom();
      const zoom = options.keepZoom ? current : Math.min(Math.max(current, 0.75), 1.25);
      return rf.setCenter(c.x, c.y, { zoom, duration: options.duration ?? 300 });
    },
    [rf],
  );

  /** Pan (keeping the zoom) when a node is not fully on screen. */
  const ensureVisible = useCallback(
    (key: string) => {
      const p = layoutRef.current.placed.get(key);
      const el = wrapRef.current;
      if (!p || !el) return;
      const { x, y, zoom } = rf.getViewport();
      const left = p.x * zoom + x;
      const top = p.y * zoom + y;
      const margin = 16;
      if (left < margin || top < margin || left + NODE_W * zoom > el.clientWidth - margin || top + NODE_H * zoom > el.clientHeight - margin) {
        void centerOn(key, { duration: 200, keepZoom: true });
      }
    },
    [rf, centerOn],
  );

  const markReady = useCallback(() => {
    readyRef.current = true;
    setReady(true);
  }, []);

  // First paint: fit every node, or centre on a node that was selected before the
  // graph mounted (the strip pill opens the blueprint with its node selected).
  const onInit = useCallback(() => {
    const key = selectedRef.current;
    const settle = key && layoutRef.current.placed.has(key) ? centerOn(key, { duration: 0 }) : rf.fitView(FIT);
    void settle.finally(markReady);
  }, [rf, centerOn, markReady]);
  useEffect(() => {
    // Never leave the canvas invisible if a fit is still queued for some reason.
    const id = window.setTimeout(markReady, 600);
    return () => window.clearTimeout(id);
  }, [markReady]);

  // Switching By level / By file re-fits the view.
  const lastGrouping = useRef(grouping);
  useEffect(() => {
    if (lastGrouping.current === grouping) return;
    lastGrouping.current = grouping;
    void rf.fitView({ ...FIT, duration: 250 });
  }, [grouping, rf]);

  // A selection from outside the graph pans to that node. Keyed on the selection
  // alone: `useReactFlow()` hands out a new object once the viewport initialises.
  const centerOnRef = useRef(centerOn);
  centerOnRef.current = centerOn;
  useEffect(() => {
    const own = ownSelection.current;
    ownSelection.current = undefined;
    if (!selectedKey || own === selectedKey || !readyRef.current) return;
    void centerOnRef.current(selectedKey);
  }, [selectedKey]);

  const focusNode = useCallback((key: string) => {
    const el = wrapRef.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(key)}"]`);
    el?.focus({ preventScroll: true });
  }, []);

  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      // A lane's file label takes pointer events (for its tooltip); a click there
      // counts as a click on the pane.
      if (node.type !== BLUEPRINT_NODE) select(null);
      else select(node.id === selectedRef.current ? null : node.id);
    },
    [select],
  );

  const onPaneClick = useCallback(() => select(null), [select]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target as HTMLElement;
    if (target.closest('input, textarea, select, [contenteditable="true"], .react-flow__controls, .react-flow__minimap')) return;
    const focusedId = target.closest<HTMLElement>('.react-flow__node')?.dataset.id ?? null;
    const focused = focusedId && !isLaneId(focusedId) ? focusedId : null;

    const direction = ARROWS[event.key as keyof typeof ARROWS];
    if (direction) {
      event.preventDefault();
      const from = selectedRef.current ?? focused;
      const next = from && layoutRef.current.placed.has(from) ? neighbourKey(layoutRef.current, from, direction) : firstKey(layoutRef.current);
      if (next) {
        select(next);
        focusNode(next);
      }
      return;
    }
    if ((event.key === 'Enter' || event.key === ' ') && focused) {
      event.preventDefault();
      if (focused !== selectedRef.current) select(focused);
      return;
    }
    if (event.key === 'Escape' && selectedRef.current) {
      event.preventDefault();
      select(null);
      // React Flow blurs the node on Escape; keep keyboard focus in the graph.
      window.requestAnimationFrame(() => wrapRef.current?.focus({ preventScroll: true }));
    }
  };

  const onFocus = (event: FocusEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    const id = target.classList.contains('react-flow__node') ? target.dataset.id : undefined;
    if (id && !isLaneId(id) && target.matches(':focus-visible')) ensureVisible(id);
  };

  // PNG export for the Download menu: render the viewport with a transform that
  // fits every node into an image of exactly their bounds plus a margin. Only the
  // viewport is captured, so the minimap, controls and background dots stay out.
  useEffect(() => {
    if (!handleRef) return;
    const handle: BlueprintGraphHandle = {
      toPng: async () => {
        const root = wrapRef.current;
        const viewport = root?.querySelector<HTMLElement>('.react-flow__viewport');
        if (!root || !viewport) throw new Error('The graph is not on screen.');
        const bounds = rf.getNodesBounds(rf.getNodes());
        const { width, height } = imageFrame(bounds, IMAGE_MARGIN);
        const fit = getViewportForBounds(bounds, width, height, 1, 1, 0);
        const flowEl = root.querySelector<HTMLElement>('.react-flow') ?? root;
        const backgroundColor = solidBackground(getComputedStyle(flowEl).backgroundColor);
        const { toBlob } = await import('html-to-image');
        // The image shows the blueprint, not the current selection or focus.
        root.classList.add('is-exporting');
        const restore = inlineSvgPaint(viewport);
        try {
          const blob = await toBlob(viewport, {
            backgroundColor,
            width,
            height,
            pixelRatio: 2,
            skipFonts: true,
            style: {
              width: `${width}px`,
              height: `${height}px`,
              transform: `translate(${fit.x}px, ${fit.y}px) scale(${fit.zoom})`,
              opacity: '1',
            },
          });
          if (!blob) throw new Error('Could not render the graph as an image.');
          return blob;
        } finally {
          restore();
          root.classList.remove('is-exporting');
        }
      },
    };
    handleRef.current = handle;
    return () => {
      if (handleRef.current === handle) handleRef.current = null;
    };
  }, [handleRef, rf]);

  return (
    <div
      ref={wrapRef}
      className={`bpv-flow${ready ? ' is-ready' : ''}`}
      tabIndex={0}
      role="region"
      aria-label={`Blueprint dependency graph, ${grouping === 'file' ? 'grouped by file' : 'by level'}`}
      aria-describedby={helpId}
      onKeyDown={onKeyDown}
      onFocus={onFocus}
    >
      <p id={helpId} className="bpv-sr-only">
        Arrow keys move the selection: up to a node that uses this one, down to a dependency, left and right along the row.
        Enter selects the focused node; Escape clears the selection.
      </p>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onInit={onInit}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable
        edgesFocusable={false}
        edgesReconnectable={false}
        elementsSelectable
        selectNodesOnDrag={false}
        elevateNodesOnSelect={false}
        autoPanOnNodeFocus={false}
        deleteKeyCode={null}
        selectionKeyCode={null}
        multiSelectionKeyCode={null}
        minZoom={MIN_ZOOM}
        maxZoom={MAX_ZOOM}
        attributionPosition="top-right"
      >
        <ViewportPortal>
          <svg className="bpv-flow-defs" width="0" height="0" aria-hidden="true" focusable="false">
            <defs>
              <marker id={marker} viewBox="0 0 10 10" refX="10" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto">
                <path d="M0,0 L10,5 L0,10 Z" className="bpv-arrowhead" />
              </marker>
              <marker id={activeMarker} viewBox="0 0 10 10" refX="10" refY="5" markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" orient="auto">
                <path d="M0,0 L10,5 L0,10 Z" className="bpv-arrowhead is-active" />
              </marker>
            </defs>
          </svg>
        </ViewportPortal>
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--bpv-flow-dots)" />
        <Controls
          position="bottom-left"
          showInteractive={false}
          fitViewOptions={{ ...FIT, duration: 200 }}
          aria-label="Zoom controls"
        />
        <MiniMap
          position="bottom-right"
          pannable
          zoomable
          ariaLabel="Blueprint overview"
          nodeColor={minimapColor}
          nodeStrokeColor={minimapColor}
          nodeStrokeWidth={1}
          nodeBorderRadius={6}
          style={MINIMAP_SIZE}
        />
      </ReactFlow>
    </div>
  );
}
