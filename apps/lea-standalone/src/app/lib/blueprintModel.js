// Blueprint view model — the pure, testable half of the "blueprint" work package
// (docs/PLAN-ui-redesign.md §3). No DOM, no stores, no fetch: status counts, the
// footer summary, ready/unblocks derivations, session merging, the markdown
// parser/serialiser used by the proposal state, and the prompts a bound session
// starts with. The impure side (components, the slash-command handlers) imports
// from here; the tests next to it (`src/app/blueprintModel.test.mjs`) run it alone.
//
// Status vocabulary mirrors adapter/app/graph.py: planned · stated · ready ·
// proved · failed, plus the audit-grade `verified` flag on a proved node.

/** @typedef {'planned'|'stated'|'ready'|'proved'|'failed'} BlueprintStatus */

/**
 * @typedef {Object} BlueprintNodeLike
 * @property {string} key
 * @property {string | null} [kind]
 * @property {string | null} [lean]
 * @property {string[]} uses
 * @property {string} [statement]
 * @property {string | null} [file]
 * @property {BlueprintStatus} [status]
 * @property {boolean} [verified]
 * @property {{ session_id: string, title: string, last_at: string }[]} [sessions]
 */

/** Display order of statuses in summaries and legends. */
export const STATUS_ORDER = /** @type {const} */ (['proved', 'ready', 'stated', 'failed', 'planned']);

/** Legend / detail labels, one per status, always paired with the colour. */
export const STATUS_TEXT = {
  proved: 'proved',
  ready: 'ready: all deps proved',
  stated: 'stated · sorry',
  failed: 'failed',
  planned: 'planned: no Lean yet',
  running: 'session running',
};

/**
 * @template {{ key: string }} T
 * @param {T[]} nodes
 * @returns {Map<string, T>}
 */
export function byKey(nodes) {
  return new Map((nodes || []).map((n) => [n.key, n]));
}

/**
 * Counts per status plus `verified` (audited proofs) and `noFile` (nodes whose
 * decl resolved to no project file).
 * @param {BlueprintNodeLike[]} nodes
 */
export function statusCounts(nodes) {
  const counts = { total: 0, proved: 0, ready: 0, stated: 0, failed: 0, planned: 0, verified: 0, noFile: 0 };
  for (const n of nodes || []) {
    counts.total += 1;
    const s = n.status ?? 'planned';
    if (s in counts) counts[s] += 1;
    if (n.verified) counts.verified += 1;
    if (!n.file) counts.noFile += 1;
  }
  return counts;
}

/**
 * Whether a node can be proved now. The adapter is the only place that computes
 * it (docs/PLAN-blueprint-tools.md §2.8): a not-proved node whose every
 * dependency is proved is `ready`, leaves included. The UI reads the status.
 * The second argument is accepted for older callers and ignored.
 * @param {BlueprintNodeLike} node
 * @param {Map<string, BlueprintNodeLike>} [_index]
 */
export function isReady(node, _index) {
  return node?.status === 'ready';
}

/**
 * The nodes `/blueprint next` would start, in graph order.
 * @template {BlueprintNodeLike} T
 * @param {T[]} nodes
 * @returns {T[]}
 */
export function readyNodes(nodes) {
  return (nodes || []).filter((n) => isReady(n));
}

/**
 * Nodes that list `key` among their dependencies — what proving `key` unblocks.
 * @template {BlueprintNodeLike} T
 * @param {T[]} nodes
 * @param {string} key
 * @returns {T[]}
 */
export function dependents(nodes, key) {
  return (nodes || []).filter((n) => (n.uses || []).includes(key));
}

/**
 * For an "Unblocks" row: the other unproved dependencies the dependent still waits
 * on, so the panel can say "planned · also waits on rational_sq_form".
 * @param {BlueprintNodeLike} dependent
 * @param {string} key
 * @param {Map<string, BlueprintNodeLike>} index
 */
export function otherBlockers(dependent, key, index) {
  return (dependent.uses || []).filter((d) => d !== key && index.get(d)?.status !== 'proved');
}

/**
 * Short status text for a node card: "proved ✓", "stated · sorry", "proposed", …
 * @param {BlueprintNodeLike} node
 * @param {{ proposal?: boolean }} [options]
 */
export function nodeStatusText(node, options = {}) {
  if (options.proposal) return 'proposed';
  switch (node.status ?? 'planned') {
    case 'proved':
      return node.verified ? 'proved ✓ · verified' : 'proved ✓';
    case 'ready':
      return 'ready';
    case 'stated':
      return 'stated · sorry';
    case 'failed':
      return 'failed';
    default:
      return 'planned';
  }
}

/**
 * The chip next to the title: "2 of 6 proved", "All 6 proved", "No nodes yet",
 * "Proposal · unsaved". `tone` is the status colour it pairs with.
 * @param {BlueprintNodeLike[]} nodes
 * @param {{ proposal?: boolean }} [options]
 * @returns {{ text: string, tone: BlueprintStatus }}
 */
export function headerChip(nodes, options = {}) {
  const c = statusCounts(nodes);
  if (options.proposal) {
    return { text: c.total ? `Proposal · ${c.total} ${c.total === 1 ? 'node' : 'nodes'} · unsaved` : 'Proposal · drafting', tone: 'stated' };
  }
  if (c.total === 0) return { text: 'No nodes yet', tone: 'planned' };
  if (c.proved === c.total) return { text: `All ${c.total} proved`, tone: 'proved' };
  const tone = c.failed > 0 ? 'failed' : c.proved > 0 ? 'proved' : c.ready > 0 ? 'ready' : 'planned';
  return { text: `${c.proved} of ${c.total} proved`, tone };
}

/**
 * Footer line: "6 nodes · 2 proved · 1 ready · 1 stated · 1 failed · 1 planned ·
 * 2 sessions running · 1 node has no file yet". Zero buckets are omitted.
 * @param {BlueprintNodeLike[]} nodes
 * @param {{ running?: number }} [options]
 */
export function summaryLine(nodes, options = {}) {
  const c = statusCounts(nodes);
  if (c.total === 0) return 'No nodes yet';
  const parts = [`${c.total} ${c.total === 1 ? 'node' : 'nodes'}`];
  for (const s of STATUS_ORDER) if (c[s] > 0) parts.push(`${c[s]} ${s}`);
  const running = options.running ?? 0;
  if (running > 0) parts.push(`${running} ${running === 1 ? 'session' : 'sessions'} running`);
  if (c.noFile > 0) parts.push(`${c.noFile} ${c.noFile === 1 ? 'node has' : 'nodes have'} no file yet`);
  return parts.join(' · ');
}

/**
 * @typedef {Object} NodeSessionRow
 * @property {string} session_id
 * @property {string} title
 * @property {boolean} running
 * @property {boolean} bound     bound through the UI (nodeBindings), not only via a file commit
 * @property {string} last_at
 */

/**
 * sessionId → node key for every bound session: `session.blueprint_node` (the
 * server's binding, §2.10) wins; the localStorage `nodeBindings` fill in for
 * sessions the server has no binding for (sessions created before the column).
 * @param {{ id: string, blueprint_node?: string | null }[]} sessions
 * @param {Record<string, string>} nodeBindings
 * @returns {Record<string, string>}
 */
export function effectiveNodeBindings(sessions, nodeBindings) {
  /** @type {Record<string, string>} */
  const map = {};
  for (const [sid, key] of Object.entries(nodeBindings || {})) if (key) map[sid] = key;
  for (const s of sessions || []) {
    if (s && typeof s.blueprint_node === 'string' && s.blueprint_node) map[s.id] = s.blueprint_node;
  }
  return map;
}

/**
 * The node a session is bound to: `session.blueprint_node` first, the
 * localStorage binding second, else null.
 * @param {string | null | undefined} sessionId
 * @param {{ id: string, blueprint_node?: string | null }[]} sessions
 * @param {Record<string, string>} nodeBindings
 * @returns {string | null}
 */
export function sessionNodeKey(sessionId, sessions, nodeBindings) {
  if (!sessionId) return null;
  const s = (sessions || []).find((x) => x && x.id === sessionId);
  if (s && typeof s.blueprint_node === 'string' && s.blueprint_node) return s.blueprint_node;
  return (nodeBindings || {})[sessionId] || null;
}

/**
 * @typedef {Object} NodeSessionRow
 * @property {string} session_id
 * @property {string} title
 * @property {boolean} running
 * @property {boolean} bound     bound to the node (blueprint_node or a UI binding), not only via a file commit
 * @property {string} last_at
 */

/**
 * The sessions shown for a node: the ones bound to it (`session.blueprint_node`,
 * else the UI's nodeBindings: sessionId → key) merged with the ones the adapter
 * attributes through file commits (`node.sessions`). Running sessions first,
 * then newest first.
 * @param {BlueprintNodeLike} node
 * @param {{ id: string, title: string, updated_at?: string, active_run_count?: number, blueprint_node?: string | null }[]} sessions  the sessions store list
 * @param {Record<string, string>} nodeBindings
 * @returns {NodeSessionRow[]}
 */
export function nodeSessions(node, sessions, nodeBindings) {
  const list = sessions || [];
  const summary = new Map(list.map((s) => [s.id, s]));
  /** @type {Map<string, NodeSessionRow>} */
  const rows = new Map();
  for (const [sid, key] of Object.entries(effectiveNodeBindings(list, nodeBindings))) {
    if (key !== node.key) continue;
    const s = summary.get(sid);
    rows.set(sid, {
      session_id: sid,
      title: s?.title || 'session',
      running: (s?.active_run_count ?? 0) > 0,
      bound: true,
      last_at: s?.updated_at || '',
    });
  }
  for (const gs of node.sessions || []) {
    const existing = rows.get(gs.session_id);
    if (existing) {
      if (!existing.last_at || gs.last_at > existing.last_at) existing.last_at = gs.last_at;
      continue;
    }
    const s = summary.get(gs.session_id);
    rows.set(gs.session_id, {
      session_id: gs.session_id,
      title: s?.title || gs.title || 'session',
      running: (s?.active_run_count ?? 0) > 0,
      bound: false,
      last_at: gs.last_at || s?.updated_at || '',
    });
  }
  return [...rows.values()].sort((a, b) => {
    if (a.running !== b.running) return a.running ? -1 : 1;
    return (b.last_at || '').localeCompare(a.last_at || '');
  });
}

/**
 * Keys of nodes with at least one live session (a bound session with
 * `active_run_count > 0`, or a committing session that is still running).
 * @param {BlueprintNodeLike[]} nodes
 * @param {{ id: string, title: string, active_run_count?: number, blueprint_node?: string | null }[]} sessions
 * @param {Record<string, string>} nodeBindings
 * @returns {Set<string>}
 */
export function runningNodeKeys(nodes, sessions, nodeBindings) {
  const live = new Set((sessions || []).filter((s) => (s.active_run_count ?? 0) > 0).map((s) => s.id));
  const keys = new Set();
  for (const [sid, key] of Object.entries(effectiveNodeBindings(sessions, nodeBindings))) if (live.has(sid)) keys.add(key);
  for (const n of nodes || []) {
    if ((n.sessions || []).some((s) => live.has(s.session_id))) keys.add(n.key);
  }
  return keys;
}

// ── Markdown round-trip (mirrors adapter/app/blueprint.py) ─────────────────
//
// The format is docs/PLAN-blueprint-tools.md §2.1; the shared oracle is
// fixtures/blueprint (README there). A `## ` section is a node iff its header
// block (the consecutive `- word: value` lines right after the heading, blank
// lines before the first one skipped) has at least one of kind/lean/file/uses;
// otherwise it is a prose section, kept verbatim and in place. Unknown header
// keys inside a node's header block are dropped. Text before the first `## ` is
// the preamble. Lines inside a ``` fence are inert. The JS parser does not
// validate (warnings are the adapter's job).

const HEADING_RE = /^##\s+(.+?)\s*$/;
const HEADER_LINE_RE = /^\s*-\s*([A-Za-z][\w-]*)\s*:\s*(.*?)\s*$/;
const FENCE_RE = /^\s*```/;
const RECOGNISED_HEADERS = new Set(['kind', 'lean', 'file', 'uses']);
const USES_SENTINELS = new Set(['none', '-', '—', '–', 'n/a', 'nothing']);

/**
 * @typedef {Object} BlueprintMdNode
 * @property {'node'} [type]
 * @property {string} key
 * @property {string | null} kind
 * @property {string | null} lean
 * @property {string[]} uses
 * @property {string | null} file
 * @property {string} statement
 */

/**
 * @typedef {Object} BlueprintProse
 * @property {'prose'} type
 * @property {string} heading
 * @property {string} body
 */

/** @typedef {BlueprintMdNode | BlueprintProse} BlueprintBlock */

/** Lines with the leading and trailing blank lines removed, joined. */
function trimBlankLines(lines) {
  let start = 0;
  let end = lines.length;
  while (start < end && !lines[start].trim()) start += 1;
  while (end > start && !lines[end - 1].trim()) end -= 1;
  return lines.slice(start, end).join('\n');
}

/**
 * `lean:` / `file:` value: the first backtick span if any, else the first
 * whitespace token with trailing `,;:.)` stripped. Empty → null.
 * @param {string} value
 * @returns {string | null}
 */
export function extractNameValue(value) {
  const text = (value || '').trim();
  if (!text) return null;
  const span = text.match(/`([^`]*)`/);
  if (span) return span[1].trim() || null;
  const token = text.split(/\s+/)[0].replace(/[,;:.)]+$/, '');
  return token || null;
}

/**
 * `uses:` tokens: split on commas and whitespace, backticks stripped. The empty
 * sentinels are resolved later, once every node key is known.
 * @param {string} value
 * @returns {string[]}
 */
function usesTokens(value) {
  return (value || '')
    .split(/[\s,]+/)
    .map((t) => t.replace(/`/g, '').trim())
    .filter(Boolean);
}

/**
 * Parse `.lea/blueprint.md` (or the draft) into nodes, prose sections and the
 * preamble, the way the adapter does.
 * @param {string} text
 * @returns {{ nodes: BlueprintMdNode[], preamble: string, blocks: BlueprintBlock[] }}
 */
export function parseBlueprintMarkdown(text) {
  const lines = (text || '').split(/\r?\n/);
  /** @type {string[]} */
  const preamble = [];
  /** @type {{ heading: string, lines: string[] }[]} */
  const sections = [];
  let inFence = false;
  for (const line of lines) {
    if (FENCE_RE.test(line)) {
      inFence = !inFence;
    } else if (!inFence) {
      const heading = line.match(HEADING_RE);
      if (heading) {
        sections.push({ heading: heading[1].trim(), lines: [] });
        continue;
      }
    }
    if (sections.length) sections[sections.length - 1].lines.push(line);
    else preamble.push(line);
  }

  /** @type {BlueprintBlock[]} */
  const blocks = [];
  /** @type {BlueprintMdNode[]} */
  const nodes = [];
  /** @type {Map<BlueprintMdNode, string[]>} */
  const rawUses = new Map();
  for (const section of sections) {
    let i = 0;
    while (i < section.lines.length && !section.lines[i].trim()) i += 1;
    /** @type {[string, string][]} */
    const headers = [];
    let j = i;
    for (; j < section.lines.length; j += 1) {
      const line = section.lines[j];
      if (FENCE_RE.test(line)) break;
      const m = line.match(HEADER_LINE_RE);
      if (!m) break;
      headers.push([m[1].toLowerCase(), m[2]]);
    }
    if (!headers.some(([name]) => RECOGNISED_HEADERS.has(name))) {
      blocks.push({ type: 'prose', heading: section.heading, body: trimBlankLines(section.lines) });
      continue;
    }
    /** @type {BlueprintMdNode} */
    const node = { type: 'node', key: section.heading, kind: null, lean: null, file: null, uses: [], statement: '' };
    for (const [name, value] of headers) {
      if (name === 'kind') node.kind = value.trim().toLowerCase() || null;
      else if (name === 'lean') node.lean = extractNameValue(value);
      else if (name === 'file') node.file = extractNameValue(value);
      else if (name === 'uses') rawUses.set(node, usesTokens(value));
    }
    node.statement = trimBlankLines(section.lines.slice(j));
    blocks.push(node);
    nodes.push(node);
  }

  const keys = new Set(nodes.map((n) => n.key));
  for (const [node, tokens] of rawUses) {
    const sentinel = tokens.length === 1 && USES_SENTINELS.has(tokens[0].toLowerCase()) && !keys.has(tokens[0]);
    node.uses = sentinel ? [] : tokens;
  }
  return { nodes, preamble: trimBlankLines(preamble), blocks };
}

/**
 * One node in canonical form: heading, then `kind`, `lean`, `file`, `uses` (each
 * only when known), a blank line and the statement. A node with no header at
 * all keeps an empty `- kind:` line so it still reads back as a node.
 * @param {BlueprintMdNode} n
 */
function renderNode(n) {
  const lines = [`## ${n.key}`];
  if (n.kind) lines.push(`- kind: ${n.kind}`);
  if (n.lean) lines.push(`- lean: \`${n.lean}\``);
  if (n.file) lines.push(`- file: \`${n.file}\``);
  if (n.uses && n.uses.length) lines.push(`- uses: ${n.uses.join(', ')}`);
  if (lines.length === 1) lines.push('- kind:');
  const statement = trimBlankLines((n.statement || '').split(/\r?\n/));
  if (statement) lines.push('', statement);
  return lines.join('\n');
}

/** @param {{ heading: string, body?: string }} p */
function renderProse(p) {
  const body = trimBlankLines((p.body || '').split(/\r?\n/));
  return body ? `## ${p.heading}\n\n${body}` : `## ${p.heading}`;
}

/**
 * Canonical rendering (§2.1): the preamble, then one block per node or prose
 * section, one blank line between blocks, exactly one trailing newline. `file`
 * is always written when present.
 *
 * `prose` keeps the prose sections. Pass the parsed `blocks` to keep every
 * prose section in its place between the nodes (nodes are matched by key; a
 * node of `nodes` missing from the blocks is appended, a node block missing
 * from `nodes` is dropped). A list of prose sections only is rendered before
 * the nodes.
 * @param {BlueprintMdNode[]} nodes
 * @param {{ preamble?: string, prose?: Array<BlueprintBlock | { heading: string, body: string }> }} [options]
 * @returns {string}
 */
export function serializeBlueprint(nodes, options = {}) {
  const list = nodes || [];
  const layout = options.prose || [];
  const isNodeBlock = (b) => /** @type {any} */ (b).type === 'node';
  /** @type {string[]} */
  const out = [];
  const head = trimBlankLines((options.preamble || '').split(/\r?\n/));
  if (head) out.push(head);
  if (layout.some(isNodeBlock)) {
    const pending = [...list];
    for (const block of layout) {
      if (!isNodeBlock(block)) {
        out.push(renderProse(/** @type {BlueprintProse} */ (block)));
        continue;
      }
      const at = pending.findIndex((n) => n.key === /** @type {BlueprintMdNode} */ (block).key);
      if (at < 0) continue;
      out.push(renderNode(pending[at]));
      pending.splice(at, 1);
    }
    for (const n of pending) out.push(renderNode(n));
  } else {
    for (const p of layout) out.push(renderProse(/** @type {BlueprintProse} */ (p)));
    for (const n of list) out.push(renderNode(n));
  }
  const text = out.join('\n\n');
  return text ? `${text}\n` : '';
}

/**
 * Markdown with one more node appended (the Add node form), after a blank line so
 * it lands below whatever is in the file.
 * @param {string} existing
 * @param {BlueprintMdNode} node
 */
export function appendNodeMarkdown(existing, node) {
  const block = serializeBlueprint([node]);
  const head = (existing || '').replace(/\s+$/, '');
  return head ? `${head}\n\n${block}` : block;
}

/** A node key as §2.1 accepts it: `^[A-Za-z_][A-Za-z0-9_']*$`, at most 64 characters. */
export function isValidNodeKey(key) {
  const k = (key || '').trim();
  return k.length <= 64 && /^[A-Za-z_][A-Za-z0-9_']*$/.test(k);
}

/**
 * First line of a statement, capped, for list rows.
 * @param {string} statement
 * @param {number} [max]
 */
export function firstLine(statement, max = 140) {
  const line = (statement || '').split('\n').map((l) => l.trim()).find(Boolean) || '';
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

/**
 * A plausible file for a node from its Lean name: `Lea.Sqrt2.parity_descent` →
 * `Lea/Sqrt2/ParityDescent.lean`. Only a suggestion for planned nodes; the adapter
 * resolves the real file once the decl exists.
 * @param {string | null | undefined} lean
 * @returns {string | null}
 */
export function suggestFileFromLean(lean) {
  if (!lean) return null;
  const parts = lean.split('.').filter(Boolean);
  if (parts.length === 0) return null;
  const short = parts.pop() || '';
  const pascal = short
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((p) => p[0].toUpperCase() + p.slice(1))
    .join('');
  if (!pascal) return null;
  return [...parts, `${pascal}.lean`].join('/');
}

/**
 * The file a proposal row shows: the resolved one, else a suggestion, else null.
 * @param {{ file?: string | null, lean?: string | null }} node
 */
export function displayFile(node) {
  return node.file || suggestFileFromLean(node.lean) || null;
}

// ── Small display helpers ──────────────────────────────────────────────────

/**
 * "just now" · "12 s ago" · "5 min ago" · "3 h ago" · "2 d ago" · else the date.
 * @param {string | null | undefined} iso
 * @param {number} [now]   Date.now() by default
 */
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 5) return 'just now';
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  if (d < 14) return `${d} d ago`;
  return new Date(t).toLocaleDateString();
}

const DECL_KEYWORDS = 'theorem|lemma|def|abbrev|instance|structure|inductive|class|opaque|axiom';

/**
 * 1-based line of the declaration named `lean` (last dotted component) in a Lean
 * source, or null. Attributes and modifiers before the keyword are allowed.
 * @param {string} content
 * @param {string | null | undefined} lean
 * @returns {number | null}
 */
export function findDeclLine(content, lean) {
  if (!content || !lean) return null;
  const short = lean.split('.').filter(Boolean).pop();
  if (!short) return null;
  const escaped = short.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(
    `^\\s*(?:@\\[[^\\]]*\\]\\s*)?(?:(?:private|protected|noncomputable|unsafe|partial|scoped|local)\\s+)*(?:${DECL_KEYWORDS})\\s+(?:[\\w'.]+\\.)?${escaped}(?![\\w'])`,
  );
  const lines = content.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) if (re.test(lines[i])) return i + 1;
  return null;
}

// ── Sessions bound to a node ───────────────────────────────────────────────

/** Session title for a node session — names the node, as the sidebar shows it. */
export function nodeSessionTitle(key) {
  return `Prove ${key}`;
}

/** The formalization origin key a bound run uses, so re-proving a node reuses it. */
export function nodeOriginKey(key) {
  return `blueprint:${key}`;
}

/**
 * The first message of a session bound to a node. The server supplies the node
 * context (statement, assigned name and file, dependency status) through the
 * session's `blueprint_node` (docs/PLAN-blueprint-tools.md §2.7), so the
 * message only names the node. The second argument is ignored (older callers).
 * @param {{ key: string }} node
 * @param {unknown} [_nodes]
 */
export function bindingPrompt(node, _nodes) {
  return `Prove the blueprint node \`${node.key}\`.`;
}

/** The drafting run's message (§3 P4 item 5); the run's purpose is `blueprint_draft`. */
export const DRAFTING_PROMPT =
  'Draft the proof blueprint for this project. Decompose the goal into nodes and call blueprint_upsert once with every node: key, kind, a one or two sentence statement, and uses. Put the main theorem first and its lemmas below it. Do not prove anything and do not write files. Then stop and summarise the nodes.';

/**
 * The message `/blueprint` sends when nothing could be seeded from existing
 * proofs. The project's goal reaches the run through the server's context
 * message, so the prompt is the same for every project.
 * @param {unknown} [_project]
 */
export function draftingPrompt(_project) {
  return DRAFTING_PROMPT;
}

// ── Draft graph ────────────────────────────────────────────────────────────

/**
 * A graph-shaped view of the draft's nodes (`GET /blueprint` → `draft.nodes`),
 * so the canvas can show the proposal before it is accepted: every node planned,
 * edges only to keys inside the draft.
 * @param {{ key: string, kind?: string | null, lean?: string | null, file?: string | null, uses?: string[], statement?: string }[]} draftNodes
 */
export function draftGraph(draftNodes) {
  const list = draftNodes || [];
  const keys = new Set(list.map((n) => n.key));
  const nodes = list.map((n) => ({
    key: n.key,
    kind: n.kind ?? null,
    lean: n.lean ?? null,
    uses: [...(n.uses || [])],
    statement: n.statement || '',
    file: n.file ?? null,
    status: /** @type {BlueprintStatus} */ ('planned'),
    verified: false,
    sessions: [],
    last_modified_by: null,
    declared: false,
    planned_file: n.file ?? null,
  }));
  const edges = [];
  for (const n of list) for (const dep of n.uses || []) if (keys.has(dep)) edges.push({ from: n.key, to: dep });
  return { nodes, edges };
}

/**
 * Where an agent drafting proposal stands, from what the page can observe:
 * the drafting session's live run (sessions list), whether it was ever seen
 * running, the run's status when this page streamed it, the number of draft
 * nodes, and whether the draft was read after the run ended.
 * - `drafting`: the run is live, or not seen ending yet.
 * - `review`: the run ended and the draft has nodes.
 * - `empty`: the run ended, a draft read after that has no nodes.
 * @param {{ running: boolean, seenRunning: boolean, runStatus?: string | null, draftNodes: number, readAfterEnd: boolean }} s
 * @returns {'drafting' | 'review' | 'empty'}
 */
export function draftingOutcome({ running, seenRunning, runStatus, draftNodes, readAfterEnd }) {
  if (running || runStatus === 'running' || runStatus === 'pending') return 'drafting';
  const ended = seenRunning || (!!runStatus && runStatus !== 'running' && runStatus !== 'pending');
  if (!ended) return 'drafting';
  if (draftNodes > 0) return 'review';
  return readAfterEnd ? 'empty' : 'drafting';
}

// ── Stream events (docs/PLAN-blueprint-tools.md §2.4) ──────────────────────
//
//   blueprint_updated       { project_id, draft, applied, removed, run_id, session_id }
//   blueprint_node_changed  { project_id, key, from, to, lean, file, run_id, session_id }
//   blueprint_unlocked      { project_id, keys, cause, run_id, session_id }
//
// useProofStream hands each one (payload plus `type`) to applyBlueprintEvent,
// which returns the next blueprint slice of the workbench store.

export const BLUEPRINT_EVENTS = /** @type {const} */ (['blueprint_updated', 'blueprint_node_changed', 'blueprint_unlocked']);

/**
 * @typedef {Object} BlueprintUnlock
 * @property {string | null} projectId
 * @property {string[]} keys        the nodes that became ready
 * @property {string | null} cause  the node whose proof unlocked them
 * @property {string | null} runId
 * @property {string | null} sessionId
 * @property {number} seq           the refresh counter at the unlock; changes on every unlock
 */

/**
 * @typedef {Object} BlueprintEventState
 * @property {number} refresh            workbench.blueprintRefresh
 * @property {string | null} notice      workbench.blueprintNotice
 * @property {string[]} nextKeys         the keys of the latest unlock
 * @property {BlueprintUnlock | null} unlock
 * @property {{ projectId: string, phase: 'drafting' | 'review' } | null} proposal
 */

/** Strings only, deduplicated, order kept. */
function keyList(value) {
  const out = [];
  for (const k of Array.isArray(value) ? value : []) if (typeof k === 'string' && k && !out.includes(k)) out.push(k);
  return out;
}

/**
 * "Proved helper · unlocked main, other" (or "Unlocked main" without a cause).
 * @param {string | null | undefined} cause
 * @param {string[]} keys
 */
export function unlockNoticeText(cause, keys) {
  const list = keys.join(', ');
  return cause ? `Proved ${cause} · unlocked ${list}` : `Unlocked ${list}`;
}

/**
 * The pure reducer behind the three blueprint stream events.
 * - `blueprint_unlocked`: notice "Proved <cause> · unlocked <keys>", `nextKeys`
 *   and `unlock` set to the event's keys, refresh bumps.
 * - `blueprint_node_changed`: refresh bumps; a node that became proved sets the
 *   notice "Proved <key>" (an unlock that follows replaces it).
 * - `blueprint_updated`: refresh bumps; the proposal is left as it is, so a
 *   drafting proposal stays `drafting` while the run writes the draft.
 * Anything else returns the state unchanged.
 * @template {BlueprintEventState} S
 * @param {S} state
 * @param {{ type: string, [field: string]: any }} event
 * @returns {S}
 */
export function applyBlueprintEvent(state, event) {
  if (!event || !BLUEPRINT_EVENTS.includes(/** @type {any} */ (event.type))) return state;
  const refresh = (state.refresh ?? 0) + 1;
  if (event.type === 'blueprint_unlocked') {
    const keys = keyList(event.keys);
    if (keys.length === 0) return { ...state, refresh };
    const cause = typeof event.cause === 'string' && event.cause ? event.cause : null;
    return {
      ...state,
      refresh,
      notice: unlockNoticeText(cause, keys),
      nextKeys: keys,
      unlock: {
        projectId: event.project_id ?? null,
        keys,
        cause,
        runId: event.run_id ?? null,
        sessionId: event.session_id ?? null,
        seq: refresh,
      },
    };
  }
  if (event.type === 'blueprint_node_changed') {
    if (event.to === 'proved' && typeof event.key === 'string' && event.key) {
      return { ...state, refresh, notice: `Proved ${event.key}` };
    }
    return { ...state, refresh };
  }
  // blueprint_updated: the proposal (drafting or review) is untouched.
  return { ...state, refresh };
}

/**
 * The desktop notification for an event, or null. Only unlocks notify (a run
 * that finishes already notifies through the session list); the caller shows it
 * only while the window is hidden or unfocused.
 * @param {{ type: string, [field: string]: any }} event
 * @returns {{ title: string, body: string, sessionId?: string } | null}
 */
export function blueprintEventNotification(event) {
  if (!event || event.type !== 'blueprint_unlocked') return null;
  const keys = keyList(event.keys);
  if (keys.length === 0) return null;
  const cause = typeof event.cause === 'string' && event.cause ? event.cause : null;
  const notification = {
    title: cause ? `Proved ${cause}` : 'Blueprint nodes unlocked',
    body: `Ready to prove: ${keys.join(', ')}`,
  };
  return typeof event.session_id === 'string' && event.session_id ? { ...notification, sessionId: event.session_id } : notification;
}

// ── /blueprint auto ────────────────────────────────────────────────────────
//
// Auto mode starts a bound session for every ready node now, then again each
// time nodes unlock, until it is stopped. The state machine is here (pure); the
// timer, the stream subscription and the API calls live in blueprintActions.ts.

/** Sessions one `/blueprint auto` may start. */
export const AUTO_SESSION_CAP = 20;
/** Nothing ready and nothing running for this long ends auto mode. */
export const AUTO_IDLE_MS = 20000;

/**
 * @typedef {'done' | 'cap' | 'stalled' | 'empty' | 'user'} AutoStopReason
 * @typedef {Object} AutoState
 * @property {string} projectId
 * @property {boolean} active
 * @property {number} cap
 * @property {string[]} started        keys started by this invocation
 * @property {string[]} failed         keys whose session could not be started (not retried)
 * @property {number | null} idleSince when the loop first saw nothing to start and nothing running
 * @property {AutoStopReason | null} stopReason
 */

/**
 * @param {string} projectId
 * @param {number} [cap]
 * @returns {AutoState}
 */
export function createAutoState(projectId, cap = AUTO_SESSION_CAP) {
  return { projectId, active: true, cap, started: [], failed: [], idleSince: null, stopReason: null };
}

/**
 * @param {AutoState} state
 * @param {AutoStopReason} reason
 * @returns {AutoState}
 */
export function stopAuto(state, reason) {
  return state.active ? { ...state, active: false, stopReason: reason } : state;
}

/**
 * Decide one step from the live graph and the keys that have a running
 * session: which ready nodes to start now (never one this invocation already
 * started or failed to start, never one with a running session, never past the
 * cap), or stop. Stop conditions: every node proved (`done`), the cap reached
 * (`cap`), an empty blueprint (`empty`), nothing to start and nothing running —
 * at once on the first step, else for `idleMs` (`stalled`).
 * @param {AutoState} state
 * @param {{ nodes: BlueprintNodeLike[], running: Set<string>, now: number, idleMs?: number }} input
 * @returns {{ state: AutoState, start: string[] }}
 */
export function autoStep(state, { nodes, running, now, idleMs = AUTO_IDLE_MS }) {
  if (!state.active) return { state, start: [] };
  const list = nodes || [];
  if (list.length === 0) return { state: stopAuto(state, 'empty'), start: [] };
  if (list.every((n) => n.status === 'proved')) return { state: stopAuto(state, 'done'), start: [] };
  const room = state.cap - state.started.length;
  if (room <= 0) return { state: stopAuto(state, 'cap'), start: [] };
  const tried = new Set([...state.started, ...state.failed]);
  const start = readyNodes(list)
    .map((n) => n.key)
    .filter((k) => !running.has(k) && !tried.has(k))
    .slice(0, room);
  if (start.length > 0 || running.size > 0) return { state: { ...state, idleSince: null }, start };
  const idleSince = state.idleSince ?? now;
  const firstStep = state.started.length === 0 && state.failed.length === 0;
  if (firstStep || now - idleSince >= idleMs) return { state: stopAuto({ ...state, idleSince }, 'stalled'), start: [] };
  return { state: { ...state, idleSince }, start: [] };
}

/**
 * Record what a step actually started; reaching the cap stops auto mode.
 * @param {AutoState} state
 * @param {string[]} started
 * @param {string[]} [failed]
 * @returns {AutoState}
 */
export function autoRecord(state, started, failed = []) {
  const next = { ...state, started: [...state.started, ...started], failed: [...state.failed, ...failed] };
  return next.active && next.started.length >= next.cap ? stopAuto(next, 'cap') : next;
}

/**
 * The notice for one step: what started, what failed, and why auto mode ended.
 * Null when there is nothing to say.
 * @param {AutoState} state   after the step
 * @param {{ started?: string[], failed?: { key: string, error: string }[], waiting?: number }} [step]
 * @returns {string | null}
 */
export function autoNotice(state, step = {}) {
  const started = step.started || [];
  const failed = step.failed || [];
  const parts = [];
  if (started.length) parts.push(`Auto: started ${started.join(', ')} (${state.started.length} of ${state.cap})`);
  if (failed.length) parts.push(`could not start ${failed.map((f) => `${f.key} (${f.error})`).join('; ')}`);
  if (!state.active) {
    const n = state.started.length;
    const sessions = `${n} ${n === 1 ? 'session' : 'sessions'}`;
    switch (state.stopReason) {
      case 'done':
        parts.push(`Auto finished: every node is proved (started ${sessions}).`);
        break;
      case 'cap':
        parts.push(`Auto stopped at the cap of ${state.cap} sessions.`);
        break;
      case 'empty':
        parts.push('Auto stopped: the blueprint has no nodes.');
        break;
      case 'user':
        parts.push(`Auto stopped (started ${sessions}).`);
        break;
      default:
        parts.push(n === 0 && failed.length === 0 ? 'Auto stopped: no node is ready and no session is running.' : `Auto finished: nothing else is ready (started ${sessions}).`);
    }
  } else if (!started.length && !failed.length && step.waiting) {
    parts.push(`Auto is on: waiting for ${step.waiting} running ${step.waiting === 1 ? 'session' : 'sessions'} to unlock more.`);
  }
  return parts.length ? parts.join(' · ') : null;
}
