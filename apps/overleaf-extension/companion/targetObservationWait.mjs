// A successful solver result can precede publication of its checked inputs.
// Elapsed observation time is never evidence that mathematics needs review.
import { requestFailure, isSqliteBusy } from './workflowPolicy.mjs';
export async function awaitTargetObservation({ read, requireProjection = false,
  onWait = async () => {}, shouldCancel = () => false,
  wait = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
  let expectsProjection = requireProjection;
  for (;;) {
    let evidence;
    try {
      evidence = await read();
      if (evidence?.projection) expectsProjection = true;
      const pending = evidence?.projection
        ? evidence.projection.state !== 'observed'
        : expectsProjection;
      // Preserve an already current result if Stop races its publication.
      if (!pending) return { evidence, cancelled: false };
      if (await shouldCancel()) return { evidence: null, cancelled: true };
    }
    catch (error) {
      if(error.code==='ERR_SQLITE_ERROR'&&!isSqliteBusy(error))throw error;
      if ((!error.status && !error.code) || !requestFailure(error).retry) throw error;
      evidence = { projection: { state: 'unavailable' } };
    }
    await onWait(evidence);
    await wait(500);
  }
}
