// Editor-hook watchdog (PLAN-system-hardening 0.4 / review B2).
//
// The whole Overleaf integration hangs on Overleaf's `UNSTABLE_editor:extensions`
// event — the name is an explicit instability warning. If Overleaf renames or
// removes it (or the CodeMirror surface the page bridge needs), the extension
// used to degrade to *nothing*: no badges, no error, just an extension that
// silently stopped working. This watchdog makes that failure visible: if the
// editor DOM is present but the page bridge never reported hooking it, warn.
//
// Pure timer/state logic, DOM-free: content.js supplies `isEditorPresent` (a
// DOM probe) and `onWarn`/`onRecover` (banner rendering), tests supply fakes.
// Loaded by content.js via chrome.runtime.getURL, like the other .mjs modules.

export const EDITOR_HOOK_TIMEOUT_MS = 20000;
// Re-probe cap: a slow-loading project may not have editor DOM at the first
// fire, so the watchdog re-arms — but not forever (15 probes ≈ 5 minutes).
export const EDITOR_HOOK_MAX_PROBES = 15;

export function createEditorHookWatchdog({
  isEditorPresent,
  onWarn,
  onRecover = null,
  timeoutMs = EDITOR_HOOK_TIMEOUT_MS,
  maxProbes = EDITOR_HOOK_MAX_PROBES,
  setTimeoutImpl = (fn, ms) => setTimeout(fn, ms),
  clearTimeoutImpl = (id) => clearTimeout(id)
}) {
  let hooked = false;
  let warned = false;
  let probes = 0;
  let timer = null;

  function fire() {
    timer = null;
    if (hooked || warned) return;
    if (!isEditorPresent()) {
      // Not an editor page (dashboard, history view) — or one still loading.
      // Re-probe a bounded number of times instead of warning spuriously.
      probes += 1;
      if (probes < maxProbes) {
        timer = setTimeoutImpl(fire, timeoutMs);
      }
      return;
    }
    warned = true;
    onWarn();
  }

  return {
    // Arm after injecting the page bridge. Idempotent; a hook signal that
    // already arrived wins over arming.
    arm() {
      if (timer !== null || hooked || warned) return;
      timer = setTimeoutImpl(fire, timeoutMs);
    },
    // The page bridge reported that Overleaf's editor event fired and the
    // CodeMirror plugin is installed — the integration is alive.
    editorHooked() {
      hooked = true;
      if (timer !== null) {
        clearTimeoutImpl(timer);
        timer = null;
      }
      if (warned && onRecover) {
        warned = false;
        onRecover();
      }
    },
    hasWarned() {
      return warned;
    }
  };
}
