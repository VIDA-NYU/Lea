// Startup session selection.
//
// The Overleaf extension's "View in Lea UI" action opens the UI at
// `<ui>/?session=<id>`. That deep-link takes precedence over the last-opened
// session (persisted in localStorage). These helpers are pure so they can be
// unit-tested without a DOM; App.tsx wires them to window.location/history.

export function readDeepLinkSessionId(search) {
  const params = new URLSearchParams(search || '');
  const id = (params.get('session') || '').trim();
  return id || null;
}

export function readDeepLinkFormalizationId(search) {
  const params = new URLSearchParams(search || '');
  const id = (params.get('formalization') || '').trim();
  return id || null;
}

export function stripNavigationParams(search) {
  const params = new URLSearchParams(search || '');
  params.delete('session');
  params.delete('formalization');
  const query = params.toString();
  return query ? `?${query}` : '';
}

// Returns the search string (e.g. "?view=stats" or "") with the `session` param
// removed, so a later reload falls back to the normal saved-session restore.
export function stripSessionParam(search) {
  const params = new URLSearchParams(search || '');
  params.delete('session');
  const query = params.toString();
  return query ? `?${query}` : '';
}

// Decide which session to open on load.
// - 'deep-link': from ?session=, loaded by id directly (need not be in `sessions`).
// - 'saved': last-opened id, only if it still exists in the fetched list.
// - 'none': nothing to restore.
export function pickInitialSession({ search, savedId, sessions } = {}) {
  const deepLinkId = readDeepLinkSessionId(search);
  if (deepLinkId) {
    return { sessionId: deepLinkId, source: 'deep-link' };
  }
  const trimmedSaved = (savedId || '').trim();
  const exists = trimmedSaved && (sessions || []).some((s) => s && s.id === trimmedSaved);
  return exists
    ? { sessionId: trimmedSaved, source: 'saved' }
    : { sessionId: null, source: 'none' };
}
