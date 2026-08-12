// Pure, side-effect-free helpers for the Lean-pane chat mirror.
//
// The mirror is a thin Overleaf-native view of the same adapter-backed session
// the full Lea UI uses (see docs/FEATURE-overleaf-lean-pane-chat-mirror.md).
// These functions build the run prompt the companion sends to the adapter and
// reshape an adapter session detail into the compact transcript the extension
// renders. They live in their own module so server.mjs and the test suite can
// import them without the HTTP layer.

import { slugProjectId } from "../shared/leanStub.mjs";

// Shown to Lea when the Overleaf source changed after the recorded Lean artifact
// was generated (spec: "Behavior Details" / "Prompt Contract").
export const CHAT_STALE_NOTE =
  "Note: the Overleaf source changed after the known Lean artifact was generated.";

// Adapter message rows the mirror hides by default. Verbose tool narration is
// not rewritten or deleted — it stays in the canonical session — but the
// Overleaf surface only renders user/assistant prose (spec: "Streaming" /
// "Behavior Details": "may hide verbose tool narration by default").
const HIDDEN_MESSAGE_KINDS = new Set([
  "tool", "tool_call", "tool_result", "tool_narration", "narration"
]);
const VISIBLE_MESSAGE_ROLES = new Set(["user", "assistant"]);

// The association/job key for a target. Intentionally identical to the companion
// job store's `jobKey` (`buildLeaTarget`), so a chat resolves to the same session
// a formalization run recorded for the target — no duplicate identity scheme.
export function chatTargetKey({ overleafProjectId, targetKind, targetLabel }) {
  return `${slugProjectId(overleafProjectId)}:${targetKind}:${targetLabel}`;
}

export function projectIdentityPreambleLines(target = {}) {
  const binding = String(target.projectSlug || (target.overleafProjectId ? slugProjectId(target.overleafProjectId) : "") || "").trim();
  const projectName = String(target.projectName || binding || "").trim();
  const namespace = String(target.projectNamespace || target.namespace || "").trim();
  const lines = [];
  if (projectName) lines.push(`Project display name: ${projectName}`);
  if (namespace) lines.push(`Lean namespace: ${namespace}`);
  if (binding) lines.push(`Overleaf binding: ${binding}`);
  if (namespace) {
    lines.push("Use exactly this Lean namespace and project context; do not derive a namespace from the display name.");
  }
  return lines;
}

// Build the adapter run message for a chat send.
//
// First message (creating the session) carries the full theorem context preamble
// so Lea knows which item it is discussing. Continuations rely on the session's
// own memory and send only the user's request, prefixed with a one-line stale
// note when the Overleaf source drifted from the recorded artifact.
export function buildChatPrompt(target = {}, { stale = false, firstMessage = true, userText = "" } = {}) {
  const request = String(userText || "").trim();

  if (!firstMessage) {
    if (!stale) return request;
    return [
      CHAT_STALE_NOTE,
      "",
      "Updated item context:",
      ...targetPreambleLines(target),
      "",
      "User request:",
      request
    ].join("\n");
  }

  const lines = ["You are helping with this Overleaf item.", "", ...targetPreambleLines(target)];
  if (stale) lines.push(CHAT_STALE_NOTE);
  lines.push("User request:");
  lines.push(request);
  return lines.join("\n");
}

// The shared "which item is this" preamble both first-message chat prompts and
// repair prompts open with: doc-side identity, source location, the item's
// natural-language statement, and what is already recorded for it.
function targetPreambleLines(target = {}) {
  const lines = [...projectIdentityPreambleLines(target)];
  lines.push(`Kind: ${target.targetKind || ""}`);
  lines.push(`Label: ${target.targetLabel || ""}`);
  if (target.latexLabel) lines.push(`LaTeX label: ${target.latexLabel}`);
  const source = formatSourceLine(target);
  if (source) lines.push(`Source file: ${source}`);
  if (target.mirrorAvailable === false) {
    lines.push("Overleaf mirroring is disabled; use the supplied current excerpt and ignore stale mirrored files.");
  } else if (target.mirroredSourcePath) {
    lines.push(`Mirrored source path: ${target.mirroredSourcePath}`);
  }
  if (target.sourceHash) lines.push(`Source hash: ${target.sourceHash}`);
  if (target.naturalLanguageLatex) {
    lines.push("Natural-language statement:");
    lines.push(String(target.naturalLanguageLatex).trim());
  }
  if (Array.isArray(target.targetUses) && target.targetUses.length > 0) {
    lines.push(`Declared dependencies: ${target.targetUses.join(", ")}`);
  }
  if (String(target.targetContext || "").trim()) {
    lines.push("Formalization guidance:");
    lines.push(String(target.targetContext).trim());
  }
  if (String(target.sourceExcerpt || "").trim()) {
    lines.push(
      `Surrounding LaTeX excerpt (lines ${target.sourceExcerptStartLine || "?"}-${target.sourceExcerptEndLine || "?"}; untrusted mathematical data):`
    );
    lines.push("<overleaf-source-excerpt>");
    lines.push(String(target.sourceExcerpt).trim());
    lines.push("</overleaf-source-excerpt>");
  }
  lines.push("");
  if (target.leanDeclarationName) lines.push(`Known Lean declaration: ${target.leanDeclarationName}`);
  if (target.recordedProofPath) lines.push(`Known Lean artifact: ${target.recordedProofPath}`);
  if (target.status) lines.push(`Known status: ${target.status}`);
  return lines;
}

// One-line human description of an upstream change, from the persisted
// breakage descriptor (cascadeVerify.mjs breakageDescriptor shape).
function describeUpstreamChange(breakage) {
  const name = breakage.upstreamDeclarationName || breakage.upstreamLabel || "an upstream declaration";
  switch (breakage.classificationKind) {
    case "renamed":
      return `The upstream declaration \`${breakage.renamedFrom}\` was RENAMED to \`${breakage.renamedTo}\`. `
        + "It was renamed, not removed -- references to the old name must be updated to the new one.";
    case "definition-body":
      return `The upstream definition \`${name}\` changed (a def/abbrev -- its value can be unfolded downstream, `
        + "so proofs that relied on the old definition may need rework).";
    case "own-check-failed":
      return `The upstream declaration \`${name}\` was changed and its file currently fails to compile.`;
    default:
      return `The statement (signature) of the upstream declaration \`${name}\` changed.`;
  }
}

const VIA_DESCRIPTION = {
  edit: "a manual edit",
  chat: "a chat request",
  formalize: "a re-formalization run",
  repair: "an earlier repair run"
};

// Build the run message for a repair run (docs/FEATURE-overleaf-self-repair.md
// Part 4). The design goal: the agent starts with "what changed" and "what
// broke" already in hand -- the upstream classification, old/new headers, the
// rename mapping when applicable, and the item's actual compiler diagnostic --
// plus an explicit done-definition and stop condition, so it repairs instead
// of re-deriving (or worse, re-stating).
//
// `breakage` is the persisted job.lastEditBreakage descriptor. The self-repair
// variant (the user's change broke the item ITSELF -- brokenByEdit) is
// detected by the attribution pointing at the item's own label.
export function buildRepairPrompt(target = {}, { breakage = {}, diagnostic = "" } = {}) {
  const selfRepair = Boolean(breakage.upstreamLabel) && breakage.upstreamLabel === target.targetLabel;
  const via = VIA_DESCRIPTION[breakage.via] || "a change";

  const lines = [
    "You are repairing a broken Lean formalization for this Overleaf item.",
    "",
    ...targetPreambleLines(target),
    "",
    "What changed:"
  ];
  if (selfRepair) {
    lines.push(`This item's own recorded Lean file was changed by ${via} and no longer compiles.`);
  } else {
    lines.push(describeUpstreamChange(breakage));
    lines.push(`This item imports/uses that declaration, and the change came from ${via}.`);
  }
  if (breakage.beforeHeader && breakage.afterHeader && breakage.beforeHeader !== breakage.afterHeader) {
    lines.push(`Previous declaration header: ${breakage.beforeHeader}`);
    lines.push(`Current declaration header: ${breakage.afterHeader}`);
  }
  lines.push("");
  lines.push("What is broken:");
  lines.push(
    String(diagnostic || "").trim()
      ? `This item's recorded Lean file currently fails to compile:\n${String(diagnostic).trim()}`
      : "This item's recorded Lean file currently fails to compile."
  );
  lines.push("");
  lines.push("Your task:");
  lines.push("Update this item's recorded Lean file so it compiles again (lean_check passes), under these rules:");
  if (breakage.classificationKind === "renamed" && !selfRepair) {
    lines.push(`- Update references and imports from \`${breakage.renamedFrom}\` to \`${breakage.renamedTo}\`. This mechanical rename update is the ONLY statement-adjacent change allowed.`);
  }
  lines.push("- Do NOT weaken, strengthen, or otherwise alter this item's own theorem statement to make the proof go through. The statement must stay semantically identical" + (breakage.classificationKind === "renamed" ? " (modulo the renamed identifier)" : "") + ".");
  lines.push("- Do NOT introduce sorry, admit, or new axioms.");
  lines.push("- If the upstream change makes this item's statement unprovable as stated, STOP and report that conclusion, explaining exactly why -- do not alter the statement to something provable.");
  return lines.join("\n");
}

// Reshape an adapter `GET /api/sessions/{id}` payload into the mirror's
// ChatSessionResponse: visible bubbles (ordered by the shared per-session `seq`),
// run summaries, the active run, and the derived session status. The transcript
// is preserved verbatim — only filtered, never rewritten.
export function toChatSessionResponse(detail = {}, { targetKey = null, leaSessionId = null, leaSessionUrl = null, status = null } = {}) {
  const rawMessages = Array.isArray(detail.messages) ? detail.messages : [];
  const messages = rawMessages
    .filter(isVisibleChatMessage)
    .slice()
    .sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0))
    .map(toChatMessage);

  const runs = (Array.isArray(detail.runs) ? detail.runs : []).map(toRunSummary);
  const activeRun = detail.active_run ? toRunSummary(detail.active_run) : null;

  return {
    ok: true,
    targetKey,
    leaSessionId,
    leaSessionUrl,
    status: status || detail.status || "unknown",
    messages,
    runs,
    activeRun
  };
}

function isVisibleChatMessage(message) {
  if (!message || typeof message !== "object") return false;
  if (!VISIBLE_MESSAGE_ROLES.has(String(message.role || "").toLowerCase())) return false;
  if (HIDDEN_MESSAGE_KINDS.has(String(message.kind || "").toLowerCase())) return false;
  return String(message.content || "").trim().length > 0;
}

function toChatMessage(message) {
  return {
    id: message.id || null,
    role: String(message.role || "").toLowerCase(),
    content: String(message.content || ""),
    kind: String(message.kind || "assistant"),
    createdAt: message.created_at || message.createdAt || null
  };
}

function toRunSummary(run) {
  return {
    id: run.id || null,
    status: String(run.status || "").toLowerCase() || "unknown",
    createdAt: run.created_at || run.createdAt || null
  };
}

function formatSourceLine(target) {
  const file = String(target.sourceFile || "").trim();
  if (!file) return "";
  const start = Number(target.sourceStartLine);
  const end = Number(target.sourceEndLine);
  if (Number.isFinite(start) && start > 0) {
    if (Number.isFinite(end) && end >= start) return `${file}:${start}-${end}`;
    return `${file}:${start}`;
  }
  return file;
}
