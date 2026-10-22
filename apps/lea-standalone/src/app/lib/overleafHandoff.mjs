// One submission identity survives a lost response. Source acquisition happens
// before admission; a failed acquisition leaves the user's draft untouched.
import { createIntentRegistry } from './submissionIdentity.mjs';

export function createChatSubmission({ prepare, create, makeId = () => crypto.randomUUID(), storage = undefined }) {
  const registry = createIntentRegistry('chat', { makeId, storage });
  return async function submit({ message, sessionId, model, scope, formalizations, general = false,
    onPreparing = (item) => {}, isCurrent = () => true }) {
    const selected = (formalizations || []).find((item) => item.id === scope);
    const identity = {message, sessionId, model, scope, general};
    return registry.run(identity, async () => {
      const attempt = registry.prepare(identity);
      if (!attempt.request) {
        const request = scope === 'new'
          ? { new_formalization: { display_title: message.slice(0, 120) } }
          : scope === 'project' ? {} : { focus_formalization_id: scope };
        if (selected?.origin === 'overleaf' && !general) {
          if (!attempt.reference) {
            onPreparing(selected);
            attempt.reference = (await prepare(selected.id)).source_context_ref;
          }
          request.source_context_ref = attempt.reference;
        }
        attempt.request = request;
        registry.save();
      }
      if (!isCurrent()) throw new Error('The selected chat or target changed before submission.');
      const result = await create(attempt.payload.message, attempt.payload.sessionId, attempt.payload.model,
        { ...attempt.request, idempotency_key: attempt.id });
      return result;
    });
  };
}

// Checkpoint continuation has the same unknown-outcome rule as a chat send.
export function createCheckpointSubmission({ prepare, create, makeId = () => crypto.randomUUID(), storage = undefined }) {
  const registry = createIntentRegistry('checkpoint', { makeId, storage });
  return async ({ runId, targetId, isCurrent = () => true }) => {
    const identity = {runId, targetId};
    return registry.run(identity, async () => {
      const attempt = registry.prepare(identity);
      attempt.reference ||= (await prepare(targetId)).source_context_ref;
      registry.save();
      if (!isCurrent()) throw new Error('The selected chat or target changed before resuming.');
      const result = await create(runId, attempt.reference, attempt.id);
      return result;
    });
  };
}
