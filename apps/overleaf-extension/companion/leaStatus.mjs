import { applyLeaStatusEvent } from "../shared/leaStatus.mjs";
import { publishEvent } from "./eventBus.mjs";

export function acceptStatusEvent(state, event, job = null, overleafProjectId = null) {
  if (!event?.formalization_id) return false;
  state.leaStatuses ||= {};
  const previous = state.leaStatuses[event.formalization_id];
  const next = applyLeaStatusEvent(previous, event);
  if (next === previous || !next) return false;
  state.leaStatuses[event.formalization_id] = next;
  if (job && (!job.apiRunId || job.apiRunId === event.run_id)) job.leaStatus = next;
  publishEvent(state, "jobs-changed", { formalizationId: event.formalization_id,
    runId: event.run_id, sequence: event.sequence, overleafProjectId: job?.overleafProjectId || overleafProjectId });
  return true;
}
