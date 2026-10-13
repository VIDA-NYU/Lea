export function selectSourceTarget(items, request) {
  const matches = (items || []).filter((item) => item.label === request.targetLabel
    && (item.leanKind === "def" ? "definition" : "theorem") === request.targetKind);
  if (matches.length !== 1) {
    throw new Error(matches.length
      ? "This target label is ambiguous in the current document. Give each target a unique label, then send again."
      : "This target no longer exists in the current document. Refresh the Overleaf pane and select its current target.");
  }
  return matches[0];
}

export function createSourceRefreshResponder({ documentId, readSource, respond }) {
  const pending = new Map();
  const completed = new Set();
  return async function handle(request) {
    if (!request?.request_id || request.overleafProjectId !== documentId()) return;
    if (completed.has(request.request_id)) return;
    if (pending.has(request.request_id)) return pending.get(request.request_id);
    const work = (async () => {
      let payload;
      try { payload = { sourceContext: await readSource(request) }; }
      catch (error) { payload = { error: error.message || "The current Overleaf source could not be read." }; }
      await respond({ ...request, ...payload });
      completed.add(request.request_id);
      if (completed.size > 100) completed.delete(completed.values().next().value);
    })();
    pending.set(request.request_id, work);
    try { await work; }
    finally { pending.delete(request.request_id); }
  };
}
