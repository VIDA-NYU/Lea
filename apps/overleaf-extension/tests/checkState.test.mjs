import assert from "node:assert/strict";
import test from "node:test";
import { isDualCheckStatus, normalizeLeaCheck, projectLeanCheck } from "../shared/checkState.mjs";

test("projects compiler and attempt facts into Lean Check states", () => {
  assert.equal(projectLeanCheck({ paneStatus: "missing-stub" }).status, "unformalized");
  assert.equal(projectLeanCheck({ paneStatus: "stub-generated" }).status, "stubbed");
  assert.equal(projectLeanCheck({ paneStatus: "in-progress" }).status, "in-progress");
  assert.equal(projectLeanCheck({ paneStatus: "valid" }).status, "checked");
  assert.equal(projectLeanCheck({ paneStatus: "paused" }).status, "paused");
  assert.equal(projectLeanCheck({ paneStatus: "invalid" }).status, "error");
  assert.equal(projectLeanCheck({ paneStatus: "needs-review", statusInfo: { leanCheckPassed: true } }).status, "checked");
  assert.equal(projectLeanCheck({ paneStatus: "needs-review", statusInfo: { leanCheckPassed: false } }).status, "unformalized");
});

test("unknown or synchronizing evidence is never a compiler failure or a current success", () => {
  assert.equal(projectLeanCheck({ paneStatus: "unknown" }).status, "unconfirmed");
  for (const state of ["inputs_changed", "capturing", "observation_expired", "reconciling"]) {
    const projected = projectLeanCheck({ paneStatus: "valid", statusInfo: { projection: { state } } });
    assert.equal(projected.status, "synchronizing");
    assert.equal(isDualCheckStatus({ leanCheck: projected, leaCheck: normalizeLeaCheck(null) }), true);
  }
  assert.equal(projectLeanCheck({ paneStatus: "valid", statusInfo: { projection: { state: "observed" } } }).status, "checked");
  assert.equal(projectLeanCheck({ paneStatus: "invalid" }).status, "error");
});

test("normalizes missing semantic evidence to an explained N/A", () => {
  assert.deepEqual(normalizeLeaCheck(null), { status: "N/A", reason: "not_evaluated", current: true });
  assert.equal(isDualCheckStatus({
    leanCheck: projectLeanCheck({ paneStatus: "valid" }),
    leaCheck: normalizeLeaCheck({ status: "warning" })
  }), true);
});
