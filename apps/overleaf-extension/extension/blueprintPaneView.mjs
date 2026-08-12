// Blueprint graph renderer for the Overleaf Lean pane (FEATURE-overleaf-blueprint-view).
//
// The read-only counterpart of the standalone's `BlueprintGraph.tsx`: it draws the
// project's derived dependency graph — shape by `kind` (definition = box,
// lemma/theorem = ellipse), color by live-derived `status`, edges are `uses`
// dependencies — plus a legend and a click-to-inspect node-detail panel. Loaded in
// the content script via `import(chrome.runtime.getURL("blueprintPaneView.mjs"))`.
//
// Layout geometry + status label/class strings come from the shared (mirrored)
// `blueprintLayout.mjs`, so the graph lays out identically to the standalone. This
// module owns only the DOM assembly (via the page's global `document`); it holds no
// state — the caller (content.js) owns the current selection and re-renders on change.
//
// Deliberately NO "worked on by" session links (unlike the standalone): the Overleaf
// extension has no session surface to deep-link into. Node detail is key / kind /
// status / lean decl / statement only.

import {
  NODE_W,
  NODE_H,
  STATUS_LABEL,
  computeLayout,
  statusClass,
  statusLabel,
  truncate,
} from "./blueprintLayout.mjs";
// Reuse the Lean pane's tokenizer so decl names + signatures in the node detail read
// the same (same colours/classes) as Lean code elsewhere in the pane.
import { highlightLeanLine } from "./leanPaneView.mjs";

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value != null) node.setAttribute(key, String(value));
  }
  return node;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// The SVG canvas: edges first (so nodes paint on top), then one <g> per node.
// `shapeByKey` is filled with each node's shape element so selection can be toggled
// in place (no full rebuild); a node click calls `onNodeClick(key)`.
function buildSvg(graph, layout, selectedKey, onNodeClick, shapeByKey) {
  const svg = svgEl("svg", {
    width: layout.width,
    height: layout.height,
    role: "img",
    "aria-label": "Blueprint dependency graph",
  });

  for (const edge of graph.edges) {
    const from = layout.placed.get(edge.from);
    const to = layout.placed.get(edge.to);
    if (!from || !to) continue;
    const x1 = from.x + NODE_W / 2;
    const y1 = from.y + NODE_H;
    const x2 = to.x + NODE_W / 2;
    const y2 = to.y;
    const my = (y1 + y2) / 2;
    svg.appendChild(
      svgEl("path", {
        class: "bp-edge",
        d: `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`,
        "marker-end": "url(#bp-arrow)",
      }),
    );
  }

  const defs = svgEl("defs");
  const marker = svgEl("marker", {
    id: "bp-arrow",
    markerWidth: 9,
    markerHeight: 9,
    refX: 7,
    refY: 3,
    orient: "auto",
    markerUnits: "strokeWidth",
  });
  marker.appendChild(svgEl("path", { d: "M0,0 L7,3 L0,6 Z", class: "bp-arrowhead" }));
  defs.appendChild(marker);
  svg.appendChild(defs);

  for (const key of layout.order) {
    const placed = layout.placed.get(key);
    if (!placed) continue;
    const n = placed.node;
    const isDef = n.kind === "definition";
    const cls = `bp-node ${statusClass(n)}${selectedKey === key ? " is-selected" : ""}`;

    const g = svgEl("g", { class: "bp-node-g", transform: `translate(${placed.x},${placed.y})` });
    g.addEventListener("click", () => onNodeClick(key));

    const shape = isDef
      ? svgEl("rect", { class: cls, width: NODE_W, height: NODE_H, rx: 7 })
      : svgEl("ellipse", { class: cls, cx: NODE_W / 2, cy: NODE_H / 2, rx: NODE_W / 2, ry: NODE_H / 2 });
    shapeByKey.set(key, shape);
    g.appendChild(shape);

    const keyText = svgEl("text", { class: "bp-node-key", x: NODE_W / 2, y: NODE_H / 2 - 3 });
    keyText.textContent = truncate(n.key);
    g.appendChild(keyText);

    const kindText = svgEl("text", { class: "bp-node-kind", x: NODE_W / 2, y: NODE_H / 2 + 13 });
    kindText.textContent = `${n.kind ?? "—"} · ${statusLabel(n)}`;
    g.appendChild(kindText);

    svg.appendChild(g);
  }

  return svg;
}

// Legend: the two proved tiers (audited vs audit-pending), the other statuses, and
// the two node shapes. Mirrors the standalone's legend.
function buildLegend() {
  const ul = el("ul", "bp-legend");

  const audited = el("li");
  audited.appendChild(el("span", "bp-swatch bp-proved bp-audited"));
  audited.append(" Proved ✓ ");
  audited.appendChild(el("span", "bp-legend-note", "(SafeVerify-audited)"));
  ul.appendChild(audited);

  const pending = el("li");
  pending.appendChild(el("span", "bp-swatch bp-proved bp-unaudited"));
  pending.append(" check ✓ ");
  pending.appendChild(el("span", "bp-legend-note", "(audit pending)"));
  ul.appendChild(pending);

  for (const status of ["ready", "stated", "planned", "failed"]) {
    const li = el("li");
    li.appendChild(el("span", `bp-swatch bp-${status}`));
    li.append(` ${STATUS_LABEL[status]}`);
    ul.appendChild(li);
  }

  const ellipse = el("li", "bp-legend-shape");
  ellipse.appendChild(el("span", "bp-shape-ellipse"));
  ellipse.append(" lemma / theorem");
  ul.appendChild(ellipse);

  const box = el("li", "bp-legend-shape");
  box.appendChild(el("span", "bp-shape-box"));
  box.append(" definition");
  ul.appendChild(box);

  return ul;
}

// A Lean-highlighted `<code>` element built from the same tokenizer + token classes
// the Lean pane uses, so decl names / signatures match its formatting. Multi-line
// text is highlighted line-by-line with explicit newlines between (like the pane's
// renderLeanPaneCode), so source line breaks survive under the block's pre-wrap.
function leanCode(text, className) {
  const code = el("code", className || null);
  const lines = String(text ?? "").split("\n");
  lines.forEach((line, i) => {
    for (const token of highlightLeanLine(line)) {
      const span = document.createElement("span");
      if (token.cls) span.className = `ol-lean-project-lean-${token.cls}`;
      span.textContent = token.text;
      code.appendChild(span);
    }
    if (i < lines.length - 1) code.appendChild(document.createTextNode("\n"));
  });
  return code;
}

// A generated node's statement is a Lean signature (highlight it as Lean); a
// hand-authored or fallback statement is prose (leave it plain). The synthesizer's
// "Formalized as `X`." fallback is prose too.
function isLeanStatement(node) {
  return Boolean(node.lean) && Boolean(node.statement) && !node.statement.startsWith("Formalized as ");
}

// Detail for the selected node. No "worked on by" sessions — see module header.
function buildDetail(node) {
  const detail = el("div", "bp-detail");

  const head = el("div", "bp-detail-head");
  head.appendChild(el("span", "bp-detail-key", node.key));
  head.appendChild(el("span", `bp-detail-status ${statusClass(node)}`, statusLabel(node)));
  detail.appendChild(head);

  const meta = el("div", "bp-detail-meta", node.kind ?? "node");
  if (node.lean) {
    meta.append(" · ");
    meta.appendChild(leanCode(node.lean));
  }
  detail.appendChild(meta);

  if (node.status === "proved") {
    const audit = el(
      "p",
      `bp-detail-audit ${node.verified ? "is-audited" : "is-pending"}`,
      node.verified
        ? "SafeVerify-audited — kernel replay + axiom whitelist confirm this proof."
        : "Lean check passed (compiles, no sorry). SafeVerify audit pending — run Verify on the session to seal it.",
    );
    detail.appendChild(audit);
  }

  if (node.statement) {
    detail.appendChild(
      isLeanStatement(node)
        ? leanCode(node.statement, "bp-detail-lean")
        : el("p", "bp-detail-stmt", node.statement),
    );
  }

  return detail;
}

// Render the populated graph into a fresh `.bp-graph` element (caller appends it).
// Selection is handled *in place*: the SVG is built once, and a node click only
// toggles the `is-selected` class on the two affected shapes and swaps the detail
// panel — no relayout/rebuild. `selectedKey` seeds the initial selection;
// `onSelectNode(keyOrNull)` fires on each change so the caller can persist it across
// full refreshes. Empty / loading / error states are the caller's job.
export function renderBlueprintView(graph, { selectedKey = null, onSelectNode = () => {} } = {}) {
  const layout = computeLayout(graph);
  const nodeByKey = new Map(graph.nodes.map((n) => [n.key, n]));

  const root = el("div", "bp-graph");

  let selected = nodeByKey.has(selectedKey) ? selectedKey : null;
  const shapeByKey = new Map();

  const canvas = el("div", "bp-graph-canvas");
  canvas.appendChild(buildSvg(graph, layout, selected, onNodeClick, shapeByKey));
  root.appendChild(canvas);

  const side = el("div", "bp-graph-side");
  side.appendChild(buildLegend());
  const detailHost = el("div", "bp-detail-host");
  side.appendChild(detailHost);
  root.appendChild(side);

  function renderDetail() {
    const node = selected ? nodeByKey.get(selected) : null;
    detailHost.replaceChildren(
      node ? buildDetail(node) : el("div", "bp-detail-empty", "Click a node to see its statement and status."),
    );
  }

  function onNodeClick(key) {
    const next = selected === key ? null : key;
    shapeByKey.get(selected)?.classList.remove("is-selected");
    shapeByKey.get(next)?.classList.add("is-selected");
    selected = next;
    renderDetail();
    onSelectNode(selected);
  }

  renderDetail();
  return root;
}
