const LEAN_COMMENT_BLOCK_RE = /\/-[\s\S]*?-\//g;
const LEAN_COMMENT_LINE_RE = /--.*$/gm;
const LEAN_STUB_RE = /\b(?:sorry|admit)\b/;
const CHECK_DETAIL_STUB_RE = /(?:uses\s+['"`]?(?:sorry|admit)['"`]?|sorryAx|\b(?:sorry|admit)\b)/i;

export function hasSorryLikeCode(code) {
  if (typeof code !== 'string' || !code.trim()) return false;
  const withoutComments = code
    .replace(LEAN_COMMENT_BLOCK_RE, ' ')
    .replace(LEAN_COMMENT_LINE_RE, ' ');
  return LEAN_STUB_RE.test(withoutComments);
}

export function hasSorryLikeCheckDetail(detail) {
  return typeof detail === 'string' && CHECK_DETAIL_STUB_RE.test(detail);
}

export function codeStepContainsSorry(step) {
  if (!step) return false;
  return hasSorryLikeCode(step.code) || hasSorryLikeCheckDetail(step.check_detail);
}

export function deriveCodeStepProofStatus(step) {
  if (!step || !step.check_status || step.check_status === 'unchecked') return 'unchecked';
  if (step.check_status === 'error') return 'failed';
  if (step.check_status !== 'ok') return 'unchecked';
  if (codeStepContainsSorry(step)) return 'stubbed';
  if (step.artifact_kind === 'definition') return 'defined';
  if (step.artifact_kind === 'unknown') return 'checked';
  return 'proved';
}

export function latestCodeStep(steps = []) {
  return steps.length ? steps[steps.length - 1] : null;
}

/**
 * @param {string | null | undefined} runStatus
 * @param {Array<any>} steps
 * @param {string | null | undefined} resultKind
 */
export function deriveRunCompletionStatus(runStatus, steps = [], resultKind = null) {
  if (runStatus === 'disproved') return runStatus;
  if (runStatus === 'needs_review' || resultKind === 'needs_review') return 'needs_review';
  if (runStatus !== 'proved' && runStatus !== 'success') return runStatus || 'pending';
  const latest = latestCodeStep(steps);
  const proofStatus = deriveCodeStepProofStatus(latest);
  if (proofStatus === 'defined' || ((resultKind === 'defined' || latest?.artifact_kind === 'definition') && proofStatus === 'proved')) return 'defined';
  return proofStatus === 'proved' || proofStatus === 'stubbed' ? proofStatus : 'answered';
}
