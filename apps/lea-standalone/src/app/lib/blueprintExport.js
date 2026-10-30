// Blueprint downloads — the pure half of the Download menu in BlueprintView
// (components/blueprint/DownloadMenu.tsx): file names, the JSON document, the PNG
// frame and background. No DOM, no fetch; the menu does the Blob + <a download>
// and the graph component does the html-to-image capture. Tested alone in
// src/app/blueprintExport.test.mjs.

/** @typedef {'md' | 'json' | 'png'} ExportFormat */

/** MIME type per format. */
export const EXPORT_MIME = {
  md: 'text/markdown;charset=utf-8',
  json: 'application/json;charset=utf-8',
  png: 'image/png',
};

/**
 * A file-name-safe fragment: accents folded, anything but letters, digits, `.`,
 * `_` and `-` becomes `-`, runs collapsed, trimmed, capped at 60 characters.
 * "Real Analysis" → "Real-Analysis"; "√2 / irrational" → "2-irrational".
 * @param {unknown} text
 * @returns {string}
 */
export function fileSlug(text) {
  if (text == null) return '';
  return String(text)
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '')
    .slice(0, 60)
    .replace(/[-.]+$/g, '');
}

/**
 * `<slug>-blueprint.<ext>` (slug first, else the title), or `blueprint.<ext>` when
 * the project is unknown. `{ slug: 'Sqrt2' }` + 'md' → `Sqrt2-blueprint.md`.
 * @param {{ slug?: string | null, title?: string | null } | null | undefined} project
 * @param {ExportFormat} ext
 * @returns {string}
 */
export function exportFileName(project, ext) {
  const prefix = fileSlug(project?.slug) || fileSlug(project?.title);
  return `${prefix ? `${prefix}-` : ''}blueprint.${ext}`;
}

/**
 * @typedef {Object} ExportProject
 * @property {string} id
 * @property {string | null} [slug]
 * @property {string | null} [title]
 * @property {string | null} [namespace]
 */

/**
 * The JSON download: `{ project, generated_at, nodes, edges }`. Nodes keep the
 * fields a reader needs to rebuild the graph (key, kind, lean, file, status,
 * verified, uses, statement, sessions); edges are `{ from, to }` (from depends on
 * to). Missing fields are normalised (null, [] or '') so the shape is fixed.
 * @param {{ nodes: any[], edges?: { from: string, to: string }[] }} graph
 * @param {{ project?: ExportProject | null, generatedAt?: Date | string | number }} [options]
 */
export function blueprintJson(graph, options = {}) {
  const p = options.project ?? null;
  const at = options.generatedAt == null ? new Date() : new Date(options.generatedAt);
  const nodes = (graph?.nodes ?? []).map((n) => ({
    key: String(n.key),
    kind: n.kind ?? null,
    lean: n.lean ?? null,
    file: n.file ?? null,
    status: n.status ?? 'planned',
    verified: !!n.verified,
    uses: Array.isArray(n.uses) ? n.uses.map(String) : [],
    statement: typeof n.statement === 'string' ? n.statement : '',
    sessions: Array.isArray(n.sessions)
      ? n.sessions.map((s) => ({ session_id: s.session_id, title: s.title ?? '', last_at: s.last_at ?? null }))
      : [],
  }));
  const edges = Array.isArray(graph?.edges)
    ? graph.edges.map((e) => ({ from: String(e.from), to: String(e.to) }))
    : nodes.flatMap((n) => n.uses.map((to) => ({ from: n.key, to })));
  return {
    project: p
      ? { id: p.id, slug: p.slug ?? null, title: p.title ?? null, namespace: p.namespace ?? null }
      : null,
    generated_at: at.toISOString(),
    nodes,
    edges,
  };
}

/**
 * `blueprintJson` as pretty-printed text with a trailing newline.
 * @param {{ nodes: any[], edges?: { from: string, to: string }[] }} graph
 * @param {{ project?: ExportProject | null, generatedAt?: Date | string | number }} [options]
 */
export function blueprintJsonText(graph, options = {}) {
  return `${JSON.stringify(blueprintJson(graph, options), null, 2)}\n`;
}

/**
 * The PNG frame: the bounds of every node plus a margin on each side, rounded up
 * to whole pixels (CSS pixels; the capture doubles them with pixelRatio 2).
 * @param {{ x: number, y: number, width: number, height: number }} bounds
 * @param {number} [margin]
 * @returns {{ width: number, height: number }}
 */
export function imageFrame(bounds, margin = 32) {
  return {
    width: Math.max(1, Math.ceil(bounds.width + margin * 2)),
    height: Math.max(1, Math.ceil(bounds.height + margin * 2)),
  };
}

/**
 * The PNG background: the graph's own computed background when it is a solid
 * colour, else the fallback (white). A transparent or semi-transparent value would
 * leave the exported image see-through.
 * @param {string | null | undefined} computed   e.g. getComputedStyle(el).backgroundColor
 * @param {string} [fallback]
 * @returns {string}
 */
export function solidBackground(computed, fallback = '#ffffff') {
  const v = (computed ?? '').trim();
  if (!v || v === 'transparent') return fallback;
  // rgba(r, g, b, a) — the form getComputedStyle reports
  const legacy = v.match(/^rgba\(([^)]*)\)$/i);
  if (legacy) {
    const parts = legacy[1].split(',');
    if (parts.length === 4 && Number(parts[3].trim()) < 1) return fallback;
  }
  // rgb(r g b / a), color(srgb r g b / a), …
  const slash = v.match(/\/\s*([\d.]+)(%?)\s*\)$/);
  if (slash && Number(slash[1]) / (slash[2] ? 100 : 1) < 1) return fallback;
  return v;
}
