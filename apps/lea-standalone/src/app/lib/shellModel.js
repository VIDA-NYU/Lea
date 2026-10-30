// Shell model — the pure derivations behind the launch screen, the project home
// and the project-first sidebar (docs/PLAN-ui-redesign.md, package "shell").
// No store or API coupling: everything here is a function of the payloads the
// API already returns (projects, sessions, the blueprint graph, the tree), so
// it unit-tests with `node --test` next to the other `.js` logic modules.

/** Blueprint statuses in the order the five-colour bar draws them. */
export const BLUEPRINT_STATUSES = ['proved', 'ready', 'stated', 'failed', 'planned'];

/** Human labels for the bar segments and the legend (never hue alone). */
export const BLUEPRINT_LABELS = {
  proved: 'proved',
  ready: 'ready',
  stated: 'sorry',
  failed: 'failed',
  planned: 'planned',
};

const SMALL_WORDS = new Set([
  'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'if', 'in', 'into',
  'nor', 'of', 'on', 'or', 'over', 'per', 'the', 'to', 'vs', 'via', 'with',
  'than', 'then', 'that',
]);

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * Count a graph's nodes per blueprint status. `verified` counts proved nodes
 * that SafeVerify audited (the "a of b audited" figure).
 * @param {Array<{status?: string, verified?: boolean}>} nodes
 * @returns {{total:number, proved:number, ready:number, stated:number, failed:number, planned:number, verified:number}}
 */
export function blueprintTally(nodes) {
  const tally = { total: 0, proved: 0, ready: 0, stated: 0, failed: 0, planned: 0, verified: 0 };
  for (const node of nodes || []) {
    tally.total += 1;
    const status = BLUEPRINT_STATUSES.includes(node?.status) ? node.status : 'planned';
    tally[status] += 1;
    if (status === 'proved' && node.verified) tally.verified += 1;
  }
  return tally;
}

/**
 * The five-colour bar: one segment per non-empty status, in drawing order, with
 * a percentage width (one decimal). Empty for a project without a blueprint.
 * @param {ReturnType<typeof blueprintTally>} tally
 * @returns {Array<{status:string, count:number, pct:number, label:string}>}
 */
export function blueprintBar(tally) {
  if (!tally || !tally.total) return [];
  return BLUEPRINT_STATUSES
    .filter((status) => tally[status] > 0)
    .map((status) => ({
      status,
      count: tally[status],
      pct: Math.round((tally[status] / tally.total) * 1000) / 10,
      label: BLUEPRINT_LABELS[status],
    }));
}

function plural(n, singular, pluralForm = `${singular}s`) {
  return `${n} ${n === 1 ? singular : pluralForm}`;
}

/**
 * The card's one-line summary: "6 nodes · 3 proved · 4 sessions",
 * "1 node · proved · verified · 1 session", "2 nodes · 2 ready · no sessions yet".
 * @param {ReturnType<typeof blueprintTally>} tally
 * @param {number} sessionCount
 * @param {number} [fileCount]
 */
export function projectSummary(tally, sessionCount, fileCount) {
  const parts = [];
  if (!tally || !tally.total) {
    parts.push('no blueprint yet');
  } else {
    parts.push(plural(tally.total, 'node'));
    if (tally.proved === tally.total) {
      parts.push('proved');
      if (tally.verified === tally.total) parts.push('verified');
    } else if (tally.proved > 0) parts.push(`${tally.proved} proved`);
    else if (tally.ready > 0) parts.push(`${tally.ready} ready`);
    else if (tally.stated > 0) parts.push(`${tally.stated} stated`);
    else if (tally.failed > 0) parts.push(`${tally.failed} failed`);
    else parts.push(`${tally.planned} planned`);
  }
  parts.push(sessionCount > 0 ? plural(sessionCount, 'session') : 'no sessions yet');
  if (typeof fileCount === 'number' && fileCount > 0) parts.push(plural(fileCount, 'file'));
  return parts.join(' · ');
}

/** The sidebar's "3/6" tally; empty when there is no blueprint. */
export function sidebarTally(tally) {
  return tally && tally.total ? `${tally.proved}/${tally.total}` : '';
}

/**
 * A compact relative time: "just now", "12 min ago", "3 h ago", a weekday
 * within the week ("Mon"), then "Oct 21", then "Oct 21, 2025". Deterministic
 * (English, local time) so the launch screen reads the same everywhere.
 * @param {string} iso
 * @param {number} [now]
 */
export function relativeTime(iso, now = Date.now()) {
  const then = Date.parse(iso || '');
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, (now - then) / 1000);
  if (secs < 45) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${Math.max(1, mins)} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  const date = new Date(then);
  if (days < 7) return WEEKDAYS[date.getDay()];
  const label = `${MONTHS[date.getMonth()]} ${date.getDate()}`;
  return date.getFullYear() === new Date(now).getFullYear() ? label : `${label}, ${date.getFullYear()}`;
}

/**
 * The view App renders for the view it holds. Project first: there are no
 * loose sessions, so the workbench with nothing selected is the launch screen
 * (except while a restore runs or after it failed: the workbench shows that
 * notice), and so is a project home with no project to show.
 * @param {string} view
 * @param {{selectedSessionId?: string|null, restoring?: boolean, restorationFailed?: boolean,
 *   projectHomeId?: string|null, projectDeleted?: boolean}} state
 * @returns {string}
 */
export function shownView(view, state) {
  if (view === 'main' && !state.selectedSessionId && !state.restoring && !state.restorationFailed) return 'home';
  if (view === 'project-home' && !state.projectHomeId && !state.projectDeleted) return 'home';
  return view;
}

/**
 * Where the startup restore lands: only the two start views are settled (a
 * view the user navigated to meanwhile stands); a restored session opens the
 * workbench, a project deep link keeps its project home, nothing → home.
 * @param {string} current
 * @param {{restored: boolean, projectOpened: boolean}} outcome
 * @returns {string}
 */
export function settleRestoredView(current, { restored, projectOpened }) {
  if (current !== 'home' && current !== 'main') return current;
  if (restored) return 'main';
  return projectOpened ? current : 'home';
}

/**
 * Name a project from the statement typed into "What do you want to prove?":
 * drop a leading "Prove that" / "Show that" / "Theorem:", trailing punctuation,
 * cap the length at a word boundary and title-case the plain words (symbols,
 * identifiers and words with their own capitals are left alone).
 * @param {string} statement
 * @param {number} [maxLength]
 */
export function projectTitleFromStatement(statement, maxLength = 60) {
  let text = String(statement || '').replace(/\s+/g, ' ').trim();
  text = text
    .replace(/^(please\s+)?(prove|show|demonstrate|establish|verify|check|formali[sz]e)\s+(that\s+)?/i, '')
    .replace(/^(theorem|lemma|claim|proposition|corollary)\s*[:.\-–—]?\s*/i, '')
    .replace(/^(prove|show)\s*:\s*/i, '')
    .replace(/[\s.。!?;:,]+$/g, '')
    .trim();
  if (!text) return 'Untitled project';
  if (text.length > maxLength) {
    const cut = text.slice(0, maxLength + 1);
    const atSpace = cut.lastIndexOf(' ');
    text = (atSpace > maxLength * 0.5 ? cut.slice(0, atSpace) : text.slice(0, maxLength)).replace(/[\s,;:\-–—]+$/g, '');
  }
  return text
    .split(' ')
    .map((word, index) => {
      if (!/^[a-z]+$/.test(word)) return word; // symbols, digits, identifiers, own capitals
      if (index > 0 && (word.length === 1 || SMALL_WORDS.has(word))) return word; // variables, small words
      return word[0].toUpperCase() + word.slice(1);
    })
    .join(' ');
}

/**
 * sessionId → blueprint node key. The graph attributes a node to the sessions
 * that committed its file (newest first); UI-side bindings win over that.
 * @param {{nodes?: Array<{key:string, sessions?: Array<{session_id:string}>}>} | null | undefined} graph
 * @param {Record<string,string>} [bindings]
 * @returns {Record<string,string>}
 */
export function sessionNodeMap(graph, bindings) {
  const map = {};
  for (const node of graph?.nodes || []) {
    for (const session of node.sessions || []) {
      if (session?.session_id && !map[session.session_id]) map[session.session_id] = node.key;
    }
  }
  for (const [sessionId, key] of Object.entries(bindings || {})) {
    if (key) map[sessionId] = key;
  }
  return map;
}

/**
 * A session's state as the shell shows it.
 * @param {{id:string, status?:string, active_run_count?:number, latest_recovery_state?:string|null}} session
 * @param {string} [runningSessionId]
 * @returns {'running'|'proved'|'failed'|'review'|'disproved'|'idle'}
 */
export function sessionState(session, runningSessionId) {
  if (!session) return 'idle';
  if (session.id === runningSessionId) return 'running';
  if ((session.active_run_count ?? 0) > 0) return 'running';
  if (session.latest_recovery_state === 'review' || session.status === 'needs_review') return 'review';
  if (session.status === 'ok' || session.status === 'proved' || session.status === 'defined') return 'proved';
  if (session.status === 'error') return 'failed';
  if (session.status === 'disproved') return 'disproved';
  return 'idle';
}

/** The label paired with a session state's colour. */
export function sessionStateLabel(state) {
  switch (state) {
    case 'running': return 'running';
    case 'proved': return 'proved';
    case 'failed': return 'error';
    case 'review': return 'needs review';
    case 'disproved': return 'disproved';
    default: return 'idle';
  }
}

export function isSessionRunning(session, runningSessionId) {
  return sessionState(session, runningSessionId) === 'running';
}

/** Root sessions of a project (sub-agent children stay inside their coordinator). */
export function projectSessions(sessions, projectId) {
  return (sessions || []).filter((s) => s && s.project_id === projectId && !s.parent_id);
}

/** Root sessions with no project — the legacy rows the "Unfiled" group keeps. */
export function unfiledSessions(sessions) {
  return (sessions || []).filter((s) => s && !s.project_id && !s.parent_id);
}

export function projectRunning(projectId, sessions, runningSessionId) {
  return (sessions || []).some((s) => s && s.project_id === projectId && isSessionRunning(s, runningSessionId));
}

/** Last activity of a project: the newest of its own timestamp and its sessions'. */
export function projectActivity(project, sessions) {
  let latest = Date.parse(project?.updated_at || '') || 0;
  for (const session of sessions || []) {
    if (session && session.project_id === project?.id) {
      latest = Math.max(latest, Date.parse(session.updated_at || '') || 0);
    }
  }
  return latest;
}

export function sortProjectsByActivity(projects, sessions) {
  return [...(projects || [])]
    .map((project) => ({ project, at: projectActivity(project, sessions) }))
    .sort((a, b) => b.at - a.at)
    .map((entry) => entry.project);
}

/** Newest root sessions first (for "Recent sessions"). */
export function recentSessions(sessions, limit = 6) {
  return (sessions || [])
    .filter((s) => s && !s.parent_id)
    .sort((a, b) => (Date.parse(b.updated_at || '') || 0) - (Date.parse(a.updated_at || '') || 0))
    .slice(0, limit);
}

const UP_NEXT_PRIORITY = { open: 0, prove: 1, 'prove-sorry': 2, retry: 3, blocked: 4 };

/**
 * "Up next" on the project home: what the user can act on, most actionable
 * first. A node whose bound session is running → "Open session"; ready → Prove;
 * stated (sorry) → Prove the sorry; failed → Retry; planned → blocked with the
 * reason (unproved dependencies, or no statement yet). Proved nodes are done.
 * @param {Array<any>} nodes graph nodes
 * @param {Record<string, Array<{id:string, title:string, state:string}>>} sessionsByNode
 * @param {number} [limit]
 */
export function upNext(nodes, sessionsByNode, limit = 6) {
  const byKey = new Map((nodes || []).map((node) => [node.key, node]));
  const items = [];
  for (const node of nodes || []) {
    if (!node || node.status === 'proved') continue;
    const bound = (sessionsByNode && sessionsByNode[node.key]) || [];
    const running = bound.find((s) => s.state === 'running');
    const where = node.file ? ` in ${node.file}` : '';
    if (running) {
      items.push({ node, action: 'open', sessionId: running.id, reason: `${node.status} · session ${running.title} is on it` });
    } else if (node.status === 'ready') {
      items.push({ node, action: 'prove', reason: node.uses?.length ? `ready · uses ${node.uses.join(', ')}` : 'ready · no dependencies' });
    } else if (node.status === 'stated') {
      items.push({ node, action: 'prove-sorry', reason: `stated · sorry left${where}` });
    } else if (node.status === 'failed') {
      items.push({ node, action: 'retry', reason: `failed · last check errored${where}` });
    } else {
      const waiting = (node.uses || []).filter((key) => !byKey.has(key) || byKey.get(key).status !== 'proved');
      items.push({
        node,
        action: 'blocked',
        reason: waiting.length ? `planned · waits on ${waiting.join(', ')}` : 'planned · no statement yet',
      });
    }
  }
  return items
    .sort((a, b) => UP_NEXT_PRIORITY[a.action] - UP_NEXT_PRIORITY[b.action])
    .slice(0, limit);
}

const FILE_STATUS_RANK = { error: 0, sorry: 1, ready: 2, planned: 3, proved: 4 };

function nodeFileStatus(status) {
  if (status === 'failed') return 'error';
  if (status === 'stated') return 'sorry';
  if (status === 'ready') return 'ready';
  if (status === 'proved') return 'proved';
  return 'planned';
}

/**
 * path → file status, derived from the nodes resolved into each file: the
 * weakest node wins (error > sorry > ready > planned > proved).
 * @param {Array<{file?: string|null, status?: string}>} nodes
 * @returns {Record<string, 'error'|'sorry'|'ready'|'planned'|'proved'>}
 */
export function fileStatuses(nodes) {
  const result = {};
  for (const node of nodes || []) {
    if (!node?.file) continue;
    const status = nodeFileStatus(node.status);
    const current = result[node.file];
    if (!current || FILE_STATUS_RANK[status] < FILE_STATUS_RANK[current]) result[node.file] = status;
  }
  return result;
}

/** Flatten a project tree into file paths (dirs recursed, hidden dirs skipped). */
export function flattenTree(entries, { leanOnly = false } = {}) {
  const out = [];
  const walk = (list) => {
    for (const entry of list || []) {
      if (!entry) continue;
      if (entry.type === 'dir') {
        if (entry.name && entry.name.startsWith('.')) continue;
        walk(entry.children);
      } else if (!leanOnly || /\.lean$/i.test(entry.path || entry.name || '')) {
        out.push(entry.path);
      }
    }
  };
  walk(entries);
  return out;
}

/**
 * The first message of a session that proves a blueprint node: it names the
 * node, its declaration, the file, and the statement, so the run knows exactly
 * where to work. `mode`: 'prove' (ready node), 'prove-sorry' (stated node),
 * 'retry' (failed node).
 * @param {{key:string, lean?:string|null, file?:string|null, statement?:string, kind?:string|null, uses?:string[]}} node
 * @param {'prove'|'prove-sorry'|'retry'} [mode]
 */
export function proveNodeMessage(node, mode = 'prove') {
  const decl = node.lean ? ` (declaration \`${node.lean}\`)` : '';
  const file = node.file ? ` in \`${node.file}\`` : '';
  const kind = node.kind ? `${node.kind} ` : '';
  const head = mode === 'prove-sorry'
    ? `Prove the remaining sorry in blueprint node \`${node.key}\`${decl}${file}. Keep the statement unchanged.`
    : mode === 'retry'
      ? `Retry blueprint node \`${node.key}\`${decl}${file}: the last check failed. Fix the proof without changing the statement.`
      : `Prove blueprint node \`${node.key}\`${decl}${file}.`;
  const lines = [head];
  if (node.statement) lines.push('', `${kind}statement: ${node.statement}`);
  if (node.uses?.length) lines.push('', `It may use: ${node.uses.join(', ')}.`);
  return lines.join('\n');
}

/**
 * How many notes a memory/instructions doc holds: list items when there are
 * any, otherwise non-heading paragraphs.
 * @param {string} markdown
 */
export function countNotes(markdown) {
  const text = String(markdown || '');
  const bullets = text.split('\n').filter((line) => /^\s*([-*+]|\d+[.)])\s+\S/.test(line)).length;
  if (bullets > 0) return bullets;
  return text
    .split(/\n\s*\n/)
    .map((block) => block.trim())
    .filter((block) => block && !/^#{1,6}\s/.test(block)).length;
}

/** A plain-text excerpt of a markdown doc for a card: headings and markers stripped. */
export function docExcerpt(markdown, maxLength = 220) {
  const text = String(markdown || '')
    .split('\n')
    .filter((line) => !/^\s*#{1,6}\s/.test(line))
    .map((line) => line
      .replace(/^\s*>\s?/, '')
      .replace(/^\s*([-*+]|\d+[.)])\s+/, '')
      .replace(/\*\*|__|`/g, '')
      .trim())
    .filter(Boolean)
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (text.length <= maxLength) return text;
  const cut = text.slice(0, maxLength + 1);
  const atSpace = cut.lastIndexOf(' ');
  return `${(atSpace > maxLength * 0.6 ? cut.slice(0, atSpace) : text.slice(0, maxLength)).replace(/[\s,;:]+$/, '')}…`;
}
