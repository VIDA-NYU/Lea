import assert from "node:assert/strict";
import test from "node:test";
import {
  EDITOR_HOOK_MAX_PROBES,
  createEditorHookWatchdog
} from "../extension/editorHookWatchdog.mjs";

// Manual fake timers: `set` records the callback, `fire` runs the most recent
// pending one — enough to drive the watchdog deterministically.
function fakeTimers() {
  const pending = new Map();
  let nextId = 1;
  return {
    set(fn) {
      const id = nextId++;
      pending.set(id, fn);
      return id;
    },
    clear(id) {
      pending.delete(id);
    },
    fireNext() {
      const [id, fn] = [...pending.entries()][0] || [];
      if (!fn) throw new Error("no pending timer");
      pending.delete(id);
      fn();
    },
    get pendingCount() {
      return pending.size;
    }
  };
}

function makeWatchdog(overrides = {}) {
  const timers = fakeTimers();
  const events = [];
  const watchdog = createEditorHookWatchdog({
    isEditorPresent: overrides.isEditorPresent || (() => true),
    onWarn: () => events.push("warn"),
    onRecover: () => events.push("recover"),
    setTimeoutImpl: (fn, ms) => timers.set(fn, ms),
    clearTimeoutImpl: (id) => timers.clear(id),
    ...(overrides.options || {})
  });
  return { watchdog, timers, events };
}

test("warns when the editor is present but the hook never fired", () => {
  const { watchdog, timers, events } = makeWatchdog();
  watchdog.arm();
  timers.fireNext();
  assert.deepEqual(events, ["warn"]);
  assert.equal(watchdog.hasWarned(), true);
});

test("does not warn when the hook fires before the timeout", () => {
  const { watchdog, timers, events } = makeWatchdog();
  watchdog.arm();
  watchdog.editorHooked();
  assert.equal(timers.pendingCount, 0, "the timer is cancelled");
  assert.deepEqual(events, []);
  assert.equal(watchdog.hasWarned(), false);
});

test("a hook signal that precedes arm() wins", () => {
  const { watchdog, timers, events } = makeWatchdog();
  watchdog.editorHooked();
  watchdog.arm();
  assert.equal(timers.pendingCount, 0);
  assert.deepEqual(events, []);
});

test("re-probes instead of warning while the editor DOM is absent", () => {
  let present = false;
  const { watchdog, timers, events } = makeWatchdog({ isEditorPresent: () => present });
  watchdog.arm();
  timers.fireNext();
  assert.deepEqual(events, [], "no warning on a non-editor page");
  assert.equal(timers.pendingCount, 1, "re-armed for another probe");
  present = true; // the editor finished loading, hook still never fired
  timers.fireNext();
  assert.deepEqual(events, ["warn"]);
});

test("stops probing after the probe cap on non-editor pages", () => {
  const { watchdog, timers, events } = makeWatchdog({ isEditorPresent: () => false });
  watchdog.arm();
  for (let i = 0; i < EDITOR_HOOK_MAX_PROBES; i += 1) {
    timers.fireNext();
  }
  assert.equal(timers.pendingCount, 0, "no re-arm past the cap");
  assert.deepEqual(events, []);
});

test("a late hook after warning triggers recovery", () => {
  const { watchdog, timers, events } = makeWatchdog();
  watchdog.arm();
  timers.fireNext();
  watchdog.editorHooked();
  assert.deepEqual(events, ["warn", "recover"]);
  assert.equal(watchdog.hasWarned(), false);
});

test("arm is idempotent", () => {
  const { watchdog, timers } = makeWatchdog();
  watchdog.arm();
  watchdog.arm();
  assert.equal(timers.pendingCount, 1);
});
