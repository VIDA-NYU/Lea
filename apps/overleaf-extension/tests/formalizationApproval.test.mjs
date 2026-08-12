import assert from "node:assert/strict";
import test from "node:test";
import {
  buildApprovalRevisionContext,
  computeFormalizationApprovalRevision,
  transitiveProjectDependencies
} from "../companion/formalizationApproval.mjs";

const files = [
  {
    moduleName: "Lea.P.base",
    stepPath: "base.lean",
    content: "theorem base : True := by trivial\n"
  },
  {
    moduleName: "Lea.P.middle",
    stepPath: "middle.lean",
    content: "import Lea.P.base\n\ntheorem middle : True := by exact base\n"
  },
  {
    moduleName: "Lea.P.unrelated",
    stepPath: "unrelated.lean",
    content: "theorem unrelated : True := by trivial\n"
  }
];

function revision({ root, inputHash = "input-v1", context = buildApprovalRevisionContext(files) } = {}) {
  return computeFormalizationApprovalRevision({
    formalizationInputHash: inputHash,
    declarationName: "result",
    artifactPath: "result.lean",
    moduleName: "Lea.P.result",
    artifactContent: root || "import Lea.P.middle\n\ntheorem result : True := by exact middle\n",
    context
  });
}

test("approval revisions include direct and transitive project-local imports", () => {
  const context = buildApprovalRevisionContext(files);
  assert.deepEqual(
    transitiveProjectDependencies({
      rootContent: "import Lea.P.middle\n",
      rootModuleName: "Lea.P.result",
      context
    }).map((file) => file.moduleName),
    ["Lea.P.base", "Lea.P.middle"]
  );

  const original = revision({ context });
  const changedFiles = files.map((file) => file.moduleName === "Lea.P.base"
    ? { ...file, content: "theorem base : True := by\n  exact True.intro\n" }
    : file);
  assert.notEqual(original, revision({ context: buildApprovalRevisionContext(changedFiles) }));
});

test("approval revisions ignore unrelated project files and file enumeration order", () => {
  const original = revision();
  const changedUnrelated = files.map((file) => file.moduleName === "Lea.P.unrelated"
    ? { ...file, content: "theorem unrelated : False := by contradiction\n" }
    : file);
  assert.equal(original, revision({ context: buildApprovalRevisionContext(changedUnrelated) }));
  assert.equal(original, revision({ context: buildApprovalRevisionContext([...files].reverse()) }));
});

test("approval revisions change with the source input or the approved artifact", () => {
  const original = revision();
  assert.notEqual(original, revision({ inputHash: "input-v2" }));
  assert.notEqual(
    original,
    revision({ root: "import Lea.P.middle\n\ntheorem result : True := by\n  exact True.intro\n" })
  );
});

test("approval revisions require both an input hash and artifact content", () => {
  assert.equal(revision({ inputHash: "" }), "");
  assert.equal(computeFormalizationApprovalRevision({
    formalizationInputHash: "input",
    artifactContent: ""
  }), "");
});
