// Browser-local human approval notes are bound to one opaque revision of a
// formalization. The companion computes the revision because it already owns
// artifact lookup and the project-local Lean import graph; the extension stores
// only { revision, approvedAt } in chrome.storage.local.
//
// This module is deliberately pure. Approval has no effect on Lea runs,
// checking, prompts, or artifact status.

import { createHash } from "node:crypto";
import path from "node:path";
import { parseLeanImports } from "./leanDependencyGraph.mjs";

function sha256(value) {
  return createHash("sha256").update(String(value ?? "")).digest("hex");
}

function normalizePath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "");
}

export function buildApprovalRevisionContext(files = []) {
  const normalized = (Array.isArray(files) ? files : []).map((file) => ({
    ...file,
    moduleName: String(file?.moduleName || ""),
    stepPath: normalizePath(file?.stepPath),
    absolutePath: file?.absolutePath ? path.resolve(String(file.absolutePath)) : "",
    content: String(file?.content || "")
  }));
  return {
    files: normalized,
    byModule: new Map(normalized.filter((file) => file.moduleName).map((file) => [file.moduleName, file]))
  };
}

export function computeFormalizationApprovalRevision({
  formalizationInputHash,
  declarationName = "",
  artifactPath = "",
  moduleName = "",
  artifactContent = "",
  context = buildApprovalRevisionContext()
} = {}) {
  const inputHash = String(formalizationInputHash || "");
  const content = String(artifactContent || "");
  if (!inputHash || !content) return "";

  const dependencies = transitiveProjectDependencies({
    rootContent: content,
    rootModuleName: String(moduleName || ""),
    context
  }).map((file) => ({
    moduleName: file.moduleName,
    contentHash: sha256(file.content)
  }));

  return sha256(JSON.stringify({
    version: 1,
    formalizationInputHash: inputHash,
    declarationName: String(declarationName || ""),
    artifactPath: normalizePath(artifactPath),
    moduleName: String(moduleName || ""),
    artifactHash: sha256(content),
    dependencies
  }));
}

export function transitiveProjectDependencies({
  rootContent = "",
  rootModuleName = "",
  context = buildApprovalRevisionContext()
} = {}) {
  const byModule = context?.byModule instanceof Map ? context.byModule : new Map();
  const visited = new Set(rootModuleName ? [String(rootModuleName)] : []);
  const found = new Map();
  const queue = [...parseLeanImports(String(rootContent || ""))];

  while (queue.length > 0) {
    const imported = queue.shift();
    if (visited.has(imported)) continue;
    visited.add(imported);
    const file = byModule.get(imported);
    if (!file) continue; // Mathlib/external modules are outside this personal note.
    found.set(imported, file);
    for (const dependency of parseLeanImports(file.content)) {
      if (!visited.has(dependency)) queue.push(dependency);
    }
  }

  return [...found.values()].sort((a, b) => a.moduleName.localeCompare(b.moduleName));
}
