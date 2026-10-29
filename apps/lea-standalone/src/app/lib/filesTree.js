// lib/filesTree.js — pure helpers over the project tree (`getProjectTree`) and
// Lean sources, for the files rail, the breadcrumbs and the ⌘P palette.
//
// Paths are repo-relative POSIX, exactly as the tree and the session's code
// steps carry them (a project session's code_step.path is relativised to the
// project repo by the adapter), so the two join on `path` without translation.

/**
 * @typedef {Object} TreeEntry
 * @property {string} name
 * @property {string} path
 * @property {'dir' | 'file'} type
 * @property {number} [size]
 * @property {TreeEntry[]} [children]
 */

/**
 * @typedef {Object} FlatFile
 * @property {string} path
 * @property {string} name
 * @property {string} dir   parent directory ('' at the root)
 * @property {number} [size]
 */

/** True for a Lean source path. */
export function isLeanPath(path) {
  return /\.lean$/i.test(String(path || ''));
}

/** The basename of a repo-relative path. */
export function baseName(path) {
  const p = String(path || '');
  return p.slice(p.lastIndexOf('/') + 1);
}

/** The parent directory of a repo-relative path ('' at the root). */
export function parentDir(path) {
  const p = String(path || '');
  const cut = p.lastIndexOf('/');
  return cut < 0 ? '' : p.slice(0, cut);
}

/** Every file in the tree, depth-first in tree order. */
export function flattenFiles(tree, into = []) {
  for (const entry of tree || []) {
    if (entry.type === 'dir') flattenFiles(entry.children || [], into);
    else into.push({ path: entry.path, name: entry.name, dir: parentDir(entry.path), size: entry.size });
  }
  return into;
}

/** Every directory path in the tree, depth-first. */
export function dirPaths(tree, into = []) {
  for (const entry of tree || []) {
    if (entry.type !== 'dir') continue;
    into.push(entry.path);
    dirPaths(entry.children || [], into);
  }
  return into;
}

/** The entry at `path`, or undefined. */
export function findEntry(tree, path) {
  for (const entry of tree || []) {
    if (entry.path === path) return entry;
    if (entry.type === 'dir' && path.startsWith(entry.path + '/')) {
      const hit = findEntry(entry.children || [], path);
      if (hit) return hit;
    }
  }
  return undefined;
}

/**
 * The entries directly inside `dirPath` ('' for the root): directories first,
 * then files, each group in tree order.
 * @returns {TreeEntry[]}
 */
export function childrenOf(tree, dirPath) {
  const entries = dirPath ? findEntry(tree, dirPath)?.children || [] : tree || [];
  return [...entries.filter((e) => e.type === 'dir'), ...entries.filter((e) => e.type !== 'dir')];
}

/** The directories a path lives under, outermost first: 'a/b/c.lean' → ['a', 'a/b']. */
export function ancestorDirs(path) {
  const parts = String(path || '').split('/');
  const out = [];
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join('/'));
  return out;
}

// ── Declarations ───────────────────────────────────────────────────────────────
// A regex scan is the fallback the palette ships with; the LSP's document
// symbols are the exact source and a later improvement. The scan knows the
// common shapes — attributes, modifiers, a doc comment above — and tracks
// `namespace … end` so a hit can show its qualified name.

const DECL_KINDS = ['theorem', 'lemma', 'def', 'abbrev', 'structure', 'instance', 'inductive', 'class', 'opaque', 'axiom'];
const DECL_RE = new RegExp(
  '^\\s*(?:@\\[[^\\]]*\\]\\s*)*(?:(?:private|protected|noncomputable|unsafe|partial|nonrec|scoped|local)\\s+)*' +
    '(' + DECL_KINDS.join('|') + ')\\s+([^\\s:(){}\\[\\]]+)',
);

/**
 * @typedef {Object} Declaration
 * @property {string} kind       theorem | lemma | def | abbrev | structure | instance | …
 * @property {string} name       as written
 * @property {string} qualified  with the enclosing namespaces
 * @property {number} line       1-based
 */

/**
 * Scan Lean source for named declarations. Anonymous instances are skipped.
 * @param {string} content
 * @returns {Declaration[]}
 */
export function scanDeclarations(content) {
  const lines = String(content || '').split('\n');
  /** @type {{ kind: 'namespace' | 'section', name: string }[]} */
  const scopes = [];
  /** @type {Declaration[]} */
  const out = [];
  let inBlockComment = 0;
  lines.forEach((raw, index) => {
    const line = raw.replace(/\r$/, '');
    if (inBlockComment > 0) {
      inBlockComment += (line.match(/\/-/g) || []).length - (line.match(/-\//g) || []).length;
      return;
    }
    const opens = (line.match(/\/-/g) || []).length;
    const closes = (line.match(/-\//g) || []).length;
    if (opens > closes) {
      inBlockComment = opens - closes;
      return;
    }
    if (/^\s*--/.test(line)) return;
    const ns = line.match(/^\s*namespace\s+([^\s]+)/);
    if (ns) {
      scopes.push({ kind: 'namespace', name: ns[1] });
      return;
    }
    if (/^\s*section\b/.test(line)) {
      scopes.push({ kind: 'section', name: '' });
      return;
    }
    const end = line.match(/^\s*end\b\s*([^\s]*)/);
    if (end) {
      if (scopes.length) scopes.pop();
      return;
    }
    const m = line.match(DECL_RE);
    if (!m) return;
    const name = m[2];
    if (!name || name === ':=' || name.startsWith(':')) return;
    const prefix = scopes.filter((s) => s.kind === 'namespace').map((s) => s.name);
    out.push({ kind: m[1], name, qualified: [...prefix, name].join('.'), line: index + 1 });
  });
  return out;
}
