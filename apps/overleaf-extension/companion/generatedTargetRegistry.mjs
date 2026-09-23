function normalizedPath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/{2,}/g, "/").trim();
}

function words(value) {
  return new Set(String(value || "").toLowerCase().match(/[\p{L}\p{N}]+/gu) || []);
}

function similarity(left, right) {
  const a = words(left);
  const b = words(right);
  if (!a.size || !b.size) return 0;
  let shared = 0;
  for (const word of a) if (b.has(word)) shared += 1;
  return shared / Math.max(a.size, b.size);
}

function kindOf(item) {
  return item.targetKind === "definition" || item.leanKind === "def" ? "definition" : "theorem";
}

/** Assign immutable source-side IDs to targets without an explicit label. */
export function reconcileGeneratedItems(items, registry, projectId, { reservedLabels = [] } = {}) {
  const store = registry && typeof registry === "object" ? registry : {};
  store.version = 1;
  store.projects ||= {};
  const project = store.projects[projectId] ||= { entries: [] };
  project.entries ||= [];
  project.explicitByFile ||= {};
  project.activeByFile ||= {};
  const previous = project.entries;
  const priorActiveByFile = { ...project.activeByFile };
  const used = new Set();
  const reserved = new Set([
    ...reservedLabels,
    ...(items || []).filter((item) => item.labelSource !== "generated")
      .map((item) => String(item.label || "")),
    ...Object.values(project.explicitByFile).flat().map((item) => item.id)
  ]);
  const generated = (items || []).filter((item) => item.labelSource === "generated");
  const resolved = new Map();
  const eligible = (item, entry) => entry.kind === kindOf(item)
    && !used.has(entry.id) && !reserved.has(entry.id);
  const fileOf = (item) => normalizedPath(item.sourceFile);
  const sameFile = (item, entry) => entry.sourceFile === fileOf(item);
  const assignUnambiguous = (predicate) => {
    const pending = generated.filter((item) => !resolved.has(item));
    const matchesByItem = new Map(pending.map((item) => [item, previous.filter((entry) =>
      eligible(item, entry) && predicate(item, entry))]));
    for (const item of pending) {
      const matches = matchesByItem.get(item);
      if (matches.length !== 1) continue;
      const entry = matches[0];
      if (used.has(entry.id)) continue;
      if (pending.filter((other) => matchesByItem.get(other).includes(entry)).length !== 1) continue;
      resolved.set(item, entry);
      used.add(entry.id);
    }
  };

  // Match durable source anchors first. A duplicate LaTeX label is never a
  // sufficient anchor; matching it would silently attach the wrong proof.
  assignUnambiguous((item, entry) => sameFile(item, entry)
    && entry.id === item.label && entry.sourceHash === item.sourceHash);
  assignUnambiguous((item, entry) => Boolean(item.latexLabel)
    && sameFile(item, entry) && entry.latexLabel === item.latexLabel);
  assignUnambiguous((item, entry) => sameFile(item, entry)
    && entry.sourceHash === item.sourceHash);
  assignUnambiguous((item, entry) => sameFile(item, entry)
    && Math.abs(entry.sourceStartLine - item.sourceStartLine) <= 4
    && similarity(entry.statement, String(item.naturalLanguageLatex || "").slice(0, 500)) >= 0.5);
  // A wholesale rewrite at the same marker still belongs to that source
  // position when the file's target count has not changed. Its old proof will
  // be marked stale by the source hash; an insertion/removal disables this
  // positional fallback so it cannot shift another theorem onto the old ID.
  assignUnambiguous((item, entry) => sameFile(item, entry)
    && entry.id === item.label
    && Math.abs(entry.sourceStartLine - item.sourceStartLine) <= 1
    && (priorActiveByFile[fileOf(item)] || []).length
      === generated.filter((other) => fileOf(other) === fileOf(item)).length);

  let changed = false;
  const filesInSnapshot = new Set((items || []).map((item) => fileOf(item)).filter(Boolean));
  for (const sourceFile of filesInSnapshot) {
    const explicit = (items || []).filter((item) => fileOf(item) === sourceFile
      && item.labelSource !== "generated" && item.latexLabel)
      .map((item) => ({ id: item.label, latexLabel: String(item.latexLabel) }));
    if (JSON.stringify(project.explicitByFile[sourceFile] || []) !== JSON.stringify(explicit)) {
      project.explicitByFile[sourceFile] = explicit;
      changed = true;
    }
  }
  const output = (items || []).map((item) => {
    if (item.labelSource !== "generated") return item;
    let entry = resolved.get(item);
    if (!entry) {
      const occupied = new Set([...reserved, ...previous.map((old) => old.id), ...used]);
      let id = item.label;
      for (let suffix = 2; occupied.has(id); suffix += 1) id = `${item.label}_${suffix}`;
      entry = { id };
      previous.push(entry);
      used.add(id);
      changed = true;
    }
    const next = {
      id: entry.id,
      kind: kindOf(item),
      sourceFile: fileOf(item),
      latexLabel: String(item.latexLabel || ""),
      sourceHash: String(item.sourceHash || ""),
      sourceStartLine: Number(item.sourceStartLine || 0),
      statement: String(item.naturalLanguageLatex || "").slice(0, 500)
    };
    if (Object.keys(next).some((key) => entry[key] !== next[key])) {
      Object.assign(entry, next);
      changed = true;
    }
    return {
      ...item,
      label: entry.id,
      leanDeclarationName: item.leanDeclarationName === item.label
        ? entry.id : item.leanDeclarationName
    };
  });
  for (const sourceFile of filesInSnapshot) {
    const active = output.filter((item) => item.labelSource === "generated"
      && fileOf(item) === sourceFile).map((item) => item.label);
    if (JSON.stringify(project.activeByFile[sourceFile] || []) !== JSON.stringify(active)) {
      project.activeByFile[sourceFile] = active;
      changed = true;
    }
  }
  const aliases = new Map();
  for (const entry of previous) {
    if (!entry.latexLabel) continue;
    const matches = aliases.get(entry.latexLabel) || [];
    matches.push(entry.id);
    aliases.set(entry.latexLabel, matches);
  }
  for (const entry of Object.values(project.explicitByFile).flat()) {
    const matches = aliases.get(entry.latexLabel) || [];
    matches.push(entry.id);
    aliases.set(entry.latexLabel, matches);
  }
  return {
    items: output.map((item) => ({
      ...item,
      targetUses: Array.isArray(item.targetUses)
        ? item.targetUses.map((use) => {
          const matches = aliases.get(use) || [];
          return matches.length === 1 ? matches[0] : use;
        })
        : item.targetUses
    })),
    registry: store,
    changed
  };
}
