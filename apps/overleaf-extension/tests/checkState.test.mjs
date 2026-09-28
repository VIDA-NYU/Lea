import assert from "node:assert/strict";
import test from "node:test";
import { isDualCheckStatus, normalizeLeaCheck, projectLeanCheck } from "../shared/checkState.mjs";

test("projects compiler and attempt facts into the six Lean Check states", () => {
  assert.equal(projectLeanCheck({ paneStatus: "missing-stub" }).status, "unformalized");
  assert.equal(projectLeanCheck({ paneStatus: "stub-generated" }).status, "stubbed");
  assert.equal(projectLeanCheck({ paneStatus: "in-progress" }).status, "in-progress");
  assert.equal(projectLeanCheck({ paneStatus: "valid" }).status, "checked");
  assert.equal(projectLeanCheck({ paneStatus: "paused" }).status, "paused");
  assert.equal(projectLeanCheck({ paneStatus: "invalid" }).status, "error");
  assert.equal(projectLeanCheck({ paneStatus: "needs-review", statusInfo: { leanCheckPassed: true } }).status, "checked");
  assert.equal(projectLeanCheck({ paneStatus: "needs-review", statusInfo: { leanCheckPassed: false } }).status, "unformalized");
});

test("normalizes missing semantic evidence to an explained N/A", () => {
  assert.deepEqual(normalizeLeaCheck(null), { status: "N/A", reason: "not_evaluated", current: true });
  assert.equal(isDualCheckStatus({
    leanCheck: projectLeanCheck({ paneStatus: "valid" }),
    leaCheck: normalizeLeaCheck({ status: "warning" })
  }), true);
});
