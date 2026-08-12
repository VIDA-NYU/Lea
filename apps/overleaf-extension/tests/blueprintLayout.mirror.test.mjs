// Guard: the extension's blueprintLayout.mjs is a hand-maintained MIRROR of the
// canonical packages/lea-blueprint/blueprintLayout.mjs (a Chrome content-script
// module can't import outside the extension package, and this repo has no bundler/
// copy step — see the mirror file's header). This test fails if the two drift, so
// "one source of truth" is enforced even though there are two physical files.
//
// It compares the exported functions' source (`.toString()`) and the exported
// constants, which ignores header comments/formatting outside function bodies but
// catches any logic or value divergence.

import test from "node:test";
import assert from "node:assert/strict";

import * as canonical from "../../../packages/lea-blueprint/blueprintLayout.mjs";
import * as mirror from "../extension/blueprintLayout.mjs";

test("extension blueprintLayout mirrors the canonical package module", () => {
  const fnNames = ["statusLabel", "statusClass", "computeLayout", "truncate"];
  for (const name of fnNames) {
    assert.equal(typeof canonical[name], "function", `canonical.${name} is a function`);
    assert.equal(typeof mirror[name], "function", `mirror.${name} is a function`);
    assert.equal(
      mirror[name].toString(),
      canonical[name].toString(),
      `${name} drifted between the canonical module and the extension mirror`,
    );
  }

  for (const name of ["NODE_W", "NODE_H", "H_GAP", "V_GAP", "PAD"]) {
    assert.equal(mirror[name], canonical[name], `${name} constant drifted`);
  }
  assert.deepEqual(mirror.STATUS_LABEL, canonical.STATUS_LABEL, "STATUS_LABEL drifted");
});
