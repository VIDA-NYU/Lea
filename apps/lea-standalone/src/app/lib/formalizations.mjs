export function stepsForFormalization(steps, formalizationId) {
  const rows = Array.isArray(steps) ? steps : [];
  if (!formalizationId) return rows;
  return rows.filter((step) => step?.formalization_id === formalizationId);
}

export function filesForFormalization(steps, formalizationId) {
  const seen = new Set();
  const files = [];
  for (const step of stepsForFormalization(steps, formalizationId)) {
    if (!step?.path || seen.has(step.path)) continue;
    seen.add(step.path);
    files.push(step.path);
  }
  return files;
}

export function sessionFormalizationSummary(items) {
  const summary = { total: 0, active: 0 };
  for (const item of Array.isArray(items) ? items : []) {
    summary.total += 1;
    const status = item?.validity_status || 'unknown';
    summary[status] = (summary[status] || 0) + 1;
    if (item?.activity?.status && item.activity.status !== 'idle') summary.active += 1;
  }
  return summary;
}

export function restoreFormalizationSelection({
  explicitId,
  currentId,
  latestFocusId,
  formalizations,
} = {}) {
  const ids = new Set((formalizations || []).map((item) => item?.id).filter(Boolean));
  if (explicitId && ids.has(explicitId)) return explicitId;
  if (currentId && ids.has(currentId)) return currentId;
  if (latestFocusId && ids.has(latestFocusId)) return latestFocusId;
  const active = (formalizations || []).find((item) => item?.activity?.status !== 'idle');
  return active?.id || 'project';
}

export function formalizationStatusLabel(item) {
  const activity = item?.activity?.status;
  const validity = String(item?.validity_status || 'unknown').replaceAll('_', ' ');
  return activity && activity !== 'idle' ? `${activity} · ${validity}` : validity;
}

export function formalizationStatusClass(item) {
  if (item?.activity?.status && item.activity.status !== 'idle') return 'run';
  if (item?.validity_status === 'proved' || item?.validity_status === 'defined') return 'ok';
  if (item?.validity_status === 'failing') return 'fail';
  if (item?.validity_status === 'stale') return 'warn';
  return 'idle';
}

function normalizeText(value) {
  return String(value || '').normalize('NFKC').toLocaleLowerCase();
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function messageMentionsAlias(message, alias) {
  const normalizedMessage = normalizeText(message);
  const normalizedAlias = normalizeText(alias).trim();
  if (!normalizedAlias) return false;
  if ([...normalizedAlias].length === 1) {
    // A bare one-letter declaration must not match ordinary prose articles
    // such as "a". Require a mathematical kind prefix or code formatting.
    return new RegExp(
      `(?:\\b(?:theorem|lemma|definition|formalization|claim|result)\\s+\`?|\`)${escapeRegExp(normalizedAlias)}(?:\\b|\`)`,
      'u',
    ).test(normalizedMessage);
  }
  // Dots belong inside qualified Lean names, but outside a complete alias they
  // are commonly sentence punctuation and should count as a boundary.
  const identifierEdge = "\\p{L}\\p{N}_'";
  return new RegExp(
    `(^|[^${identifierEdge}])${escapeRegExp(normalizedAlias)}($|[^${identifierEdge}])`,
    'u',
  ).test(normalizedMessage);
}

function aliasesForFormalization(item) {
  const aliases = new Set();
  for (const value of [item?.declaration_name, item?.display_title]) {
    const normalized = normalizeText(value).trim();
    if (!normalized) continue;
    aliases.add(normalized);
    if (value === item?.declaration_name && normalized.includes('.')) {
      aliases.add(normalized.split('.').at(-1));
    }
  }
  return [...aliases].filter(Boolean);
}

function looksLikeNewFormalization(message) {
  const normalized = normalizeText(message);
  return (
    /\b(?:new|another)\s+(?:theorem|lemma|definition|formalization|claim|result)\b/u.test(normalized)
    || /\b(?:prove|formalize|define)\b/u.test(normalized)
  );
}

/**
 * Resolve the next run's scope from the user's words. The selected canvas item
 * is only a fallback hint; an explicit declaration mention always wins.
 *
 * @param {{
 *   message?: string,
 *   formalizations?: Array<{
 *     id: string,
 *     declaration_name?: string | null,
 *     display_title?: string | null
 *   }>,
 *   viewedScope?: string
 * }} options
 */
export function inferComposerFormalizationScope({
  message,
  formalizations,
  viewedScope = 'project',
} = /** @type {any} */ ({})) {
  const referencedIds = new Set();
  for (const item of formalizations || []) {
    if (aliasesForFormalization(item).some((alias) => messageMentionsAlias(message, alias))) {
      referencedIds.add(item.id);
    }
  }
  if (referencedIds.size === 1) return [...referencedIds][0];
  if (referencedIds.size > 1) return 'project';
  if (looksLikeNewFormalization(message)) return 'new';
  if (
    viewedScope
    && viewedScope !== 'project'
    && viewedScope !== 'new'
    && (formalizations || []).some((item) => item?.id === viewedScope)
  ) {
    return viewedScope;
  }
  return 'project';
}

/**
 * Keep canonical project snapshots and immutable conversation history as two
 * distinct canvas sources. Never merge them into one stepper.
 *
 * @param {{
 *   mode: 'current' | 'historical',
 *   formalizationId: string,
 *   snapshot?: { formalization_id?: string, files?: any[] } | null,
 *   historicalSteps?: any[]
 * }} options
 */
export function formalizationCanvasSteps({
  mode,
  formalizationId,
  snapshot,
  historicalSteps,
}) {
  if (
    mode === 'current'
    && snapshot?.formalization_id === formalizationId
  ) {
    return [...(snapshot.files || [])];
  }
  return [...(historicalSteps || [])];
}
