// One submission identity survives a lost response. Source acquisition happens
// before admission; a failed acquisition leaves the user's draft untouched.
export function createChatSubmission({ prepare, create, makeId = () => crypto.randomUUID() }) {
  let cached;
  return async function submit({ message, sessionId, model, scope, formalizations, general = false,
    onPreparing = (item) => {}, isCurrent = () => true }) {
    const selected = (formalizations || []).find((item) => item.id === scope);
    const identity = JSON.stringify([message, sessionId, model, scope, general]);
    if (cached?.identity !== identity) cached = { identity, id: makeId() };
    const attempt = cached;
    const request = scope === 'new'
      ? { new_formalization: { display_title: message.slice(0, 120) } }
      : scope === 'project' ? {} : { focus_formalization_id: scope };
    try {
      if (selected?.origin === 'overleaf' && !general) {
        if (!attempt.reference) {
          onPreparing(selected);
          attempt.reference = (await prepare(selected.id)).source_context_ref;
        }
        request.source_context_ref = attempt.reference;
      }
      if (!isCurrent()) throw new Error('The selected chat or target changed before submission.');
      const result = await create(message, sessionId, model, { ...request, idempotency_key: attempt.id });
      if (cached === attempt) cached = undefined;
      return result;
    } catch (error) {
      // A definite rejection permits reacquiring source. An unknown network
      // outcome retries the exact same request, which may already be running.
      if (error.status >= 400 && error.status < 500 && cached === attempt) cached = undefined;
      throw error;
    }
  };
}

// Checkpoint continuation has the same unknown-outcome rule as a chat send.
export function createCheckpointSubmission({ prepare, create, makeId = () => crypto.randomUUID() }) {
  let cached;
  return async ({ runId, targetId, isCurrent = () => true }) => {
    const identity = JSON.stringify([runId, targetId]);
    if (cached?.identity !== identity) cached = { identity, id: makeId() };
    const attempt = cached;
    try {
      attempt.reference ||= (await prepare(targetId)).source_context_ref;
      if (!isCurrent()) throw new Error('The selected chat or target changed before resuming.');
      const result = await create(runId, attempt.reference, attempt.id);
      if (cached === attempt) cached = undefined;
      return result;
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && cached === attempt) cached = undefined;
      throw error;
    }
  };
}
