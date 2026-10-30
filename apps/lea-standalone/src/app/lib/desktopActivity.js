// Desktop shell activity derived from the session list (pure, unit-tested in
// desktopActivity.test.mjs): how many root sessions have a live run, and which
// sessions just finished one, with a notification title/body for each.
//
// A session "finishes" when its active_run_count drops from >0 to 0 between two
// snapshots. Sub-agents (parent_id set) are excluded: their coordinator's run
// is the one the user is waiting on.

/**
 * @typedef {{ id: string, title?: string, parent_id?: string | null, active_run_count?: number,
 *             status?: string, latest_check_status?: string | null }} SessionLike
 */

/** @param {SessionLike[]} sessions */
export function countRunning(sessions) {
  let running = 0;
  for (const s of sessions) if (!s.parent_id && (s.active_run_count ?? 0) > 0) running += 1;
  return running;
}

/** One line describing a settled session for a notification. */
export function outcomeLine(session) {
  switch (session.status) {
    case 'proved':
    case 'ok':
    case 'defined':
      return 'Proof complete.';
    case 'error':
      return session.latest_check_status === 'error' ? 'Stopped with a Lean error.' : 'Stopped with an error.';
    case 'needs_review':
      return 'Needs your review.';
    case 'disproved':
      return 'Found a counterexample.';
    default:
      return 'Run finished.';
  }
}

/**
 * Sessions whose run finished between `previous` and `next`.
 * @param {Map<string, SessionLike> | null} previous  by id; null on the first snapshot
 * @param {SessionLike[]} next
 * @returns {{ sessionId: string, title: string, body: string }[]}
 */
export function finishedRuns(previous, next) {
  if (!previous) return [];
  const out = [];
  for (const s of next) {
    if (s.parent_id) continue;
    const before = previous.get(s.id);
    if (!before) continue;
    if ((before.active_run_count ?? 0) > 0 && (s.active_run_count ?? 0) === 0) {
      out.push({ sessionId: s.id, title: s.title || 'Lea', body: outcomeLine(s) });
    }
  }
  return out;
}

/** @param {SessionLike[]} sessions */
export function indexById(sessions) {
  return new Map(sessions.map((s) => [s.id, s]));
}
