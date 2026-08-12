import { hashTargetText, normalizeTargetText } from "./theoremParser.mjs";
import {
  findEnvironments,
  isLineBreak,
  maskOpaqueSpans,
  parseBalancedSuffix,
  parseTargets,
  skipInlineWhitespace,
  stripLeaTargetText
} from "../extension/targetParserCore.mjs";

// Used only as a display-kind fallback (leanKindFor) when an environment has
// no validated target -- e.g. a malformed or mismatched marker. Custom
// (non-allowlisted) environments aren't classifiable this way and default to
// "theorem"; see leanKindFor.
const DEFINITION_KINDS = new Set(["definition"]);

// Target syntaxes whose target already carries its own body span (no enclosing
// environment to find in `environments`), so they are pulled straight from the
// strict target list rather than the environment loop.
const STANDALONE_TARGET_SYNTAXES = new Set(["tag", "leacode"]);

// The pane hashes the exact same canonical body text the formalize path hashes
// (`hashTargetText` over `stripLeaTargetText`). The server combines that body
// hash with each item's uses/context metadata for full freshness checks. These
// aliases keep the original export names while delegating to the single source
// of truth in theoremParser/targetParserCore.
export const normalizeLeanPaneText = normalizeTargetText;
export const hashLeanPaneSource = hashTargetText;

export function buildLeanPaneManifest({
  overleafProjectId = "unknown",
  files = []
} = {}) {
  const normalizedFiles = normalizeFiles(files);
  const diagnostics = [];
  const orderedPaths = normalizedFiles.map((file) => file.path).sort((a, b) => a.localeCompare(b));

  const items = [];
  const labels = new Map();
  for (const sourceFile of orderedPaths) {
    const file = normalizedFiles.find((candidate) => candidate.path === sourceFile);
    if (!file) continue;
    // Reuse the formalize-path parser to recover full marker metadata (uses/context)
    // and confirm the marker is a valid formalize target. Keyed by environment
    // offset, which both parsers compute identically for the same source.
    const targetByOffset = new Map(parseTargets(file.content).map((target) => [target.from, target]));
    for (const item of parseLeanPaneItemsFromFile(file, items.length)) {
      const documentOrder = items.length;
      const matchedTarget = targetByOffset.get(item.sourceStartOffset);
      items.push({
        // documentOrder keeps the id unique even when two environments share the
        // same kind+label (a duplicate_label case), so expansion state and DOM
        // dataset ids never collide in the pane.
        id: `${item.kind}:${item.label}:${documentOrder}`,
        overleafProjectId,
        ...item,
        // formalize-from-pane (item 12): only a valid marker is a runnable target.
        formalizable: Boolean(matchedTarget),
        targetUses: matchedTarget?.targetUses || [],
        targetContext: matchedTarget?.targetContext || "",
        documentOrder
      });
      const seen = labels.get(item.label) || [];
      seen.push(item.sourceFile);
      labels.set(item.label, seen);
    }
  }

  for (const [label, sourceFiles] of labels.entries()) {
    if (sourceFiles.length <= 1) continue;
    diagnostics.push({
      code: "duplicate_label",
      label,
      message: `Label ${label} appears in multiple Lean-pane items.`,
      sourceFiles
    });
  }

  return {
    ok: true,
    generatedAt: new Date().toISOString(),
    rootFile: "",
    items,
    diagnostics
  };
}

export function parseLeanPaneItemsFromFile(file, initialOrder = 0) {
  const content = String(file?.content ?? "");
  const sourceFile = normalizePath(file?.path || "");
  // Generic (no name allowlist): a custom environment tagged with
  // \leatheorem{...}/\leadefinition{...}/etc. is inventoried exactly like
  // theorem/definition. The allowlist was only ever a proxy for "this might
  // be a Lea target" -- a tag states that directly, so custom environments
  // (e.g. \begin{claim}) are exactly the case tags exist to support.
  //
  // Sort by start offset: findEnvironments pushes an environment when its
  // \end{} is matched, so a nested pair (rare for theorem-like environments,
  // but not impossible) would otherwise appear before its enclosing one.
  const environments = findEnvironments(content, { names: null }).sort((a, b) => a.from - b.from);
  // The strict, validated parser (same one the formalize path and
  // buildLeanPaneManifest's own formalizable-matching use), keyed by
  // environment start offset. A malformed/mismatched marker won't appear
  // here, but should still surface as a (non-formalizable) pane item -- see
  // extractLeaMarkerLabel below -- so this is consulted only for the
  // validated kind, not for whether to list the item at all.
  const targets = parseTargets(content);
  const targetsByOffset = new Map(targets.map((target) => [target.from, target]));
  const coveredOffsets = new Set();
  // Masked once, reused for every environment's marker check below. A real,
  // legitimately-matched environment (e.g. \begin{enumerate} in a
  // documentation file) can still *contain* a nested verbatim-like block --
  // findEnvironments already keeps that nested block from becoming its own
  // target, but extracted.rawLatex below is a plain substring of the
  // ORIGINAL content, and would still literally contain whatever marker text
  // sits inside that nested block. Searching the masked slice instead is
  // what stops a real false positive: a recipe list containing a "% lea:
  // ..." example inside a \begin{verbatim} block had the whole enclosing
  // \begin{enumerate} promoted to its own phantom pane item, using the
  // example's label. rawLatex itself (used for display) stays on the
  // original content -- only the marker *search* uses the masked version.
  const maskedContent = maskOpaqueSpans(content);
  // Collect candidates from both sources before sorting -- pushing straight
  // from two separate loops would order all environment-based items before
  // all standalone-tag items regardless of where they actually sit in the
  // file, which is wrong: documentOrder/id below are assigned by push order,
  // and the pane should read top-to-bottom in source order.
  const candidates = [];

  for (const environment of environments) {
    const extracted = extractEnvironmentContent(content, environment);
    const maskedRawLatex = maskedContent.slice(environment.bodyFrom, environment.bodyTo);
    const leanName = extractLeaMarkerLabel(maskedRawLatex);
    if (!leanName) continue;
    coveredOffsets.add(environment.from);
    candidates.push({
      kind: environment.name,
      target: targetsByOffset.get(environment.from),
      leanName,
      from: environment.from,
      to: environment.to,
      rawLatex: extracted.rawLatex,
      title: extracted.title,
      latexLabel: extracted.latexLabel
    });
  }

  // Standalone tags (\leatheorem{label=foo}{Statement...}, see
  // docs/FEATURE-overleaf-inline-lea-tags.md) need no enclosing environment,
  // so they never appear in `environments` above -- the target itself
  // already carries the body span (from a synthetic environment built in
  // targetParserCore.mjs), so pull any not already covered straight from the
  // strict target list. Unlike the loop above, there's no loose/malformed
  // case to handle here: a target only exists in `targets` if it already
  // passed full validation, so every standalone candidate here is
  // formalizable by construction. A malformed standalone tag (e.g. an
  // invalid label=) currently does not surface in the pane at all -- see
  // docs/PLAN-overleaf-inline-lea-tags.md for why this is an accepted, known
  // gap rather than an oversight.
  for (const target of targets) {
    // Both standalone forms carry their own body span from a synthetic
    // environment (targetParserCore.mjs): "tag" is \leatheorem{...}{...}, and
    // "leacode" is a \begin{leacode}...\end{leacode} code block.
    if (!STANDALONE_TARGET_SYNTAXES.has(target.syntax) || coveredOffsets.has(target.from)) continue;
    candidates.push({
      kind: target.latexEnvironment,
      target,
      leanName: target.targetLabel,
      from: target.from,
      to: target.to,
      rawLatex: content.slice(target.bodyFrom, target.bodyTo).trim(),
      title: undefined,
      latexLabel: target.latexLabel
    });
  }

  const items = [];
  for (const { kind, target, leanName, from, to, rawLatex, title, latexLabel } of candidates.sort((a, b) => a.from - b.from)) {
    const naturalLanguageLatex = stripLeaTargetText(rawLatex);
    const sourceStart = offsetToLineColumn(content, from);
    const sourceEnd = offsetToLineColumn(content, to);
    items.push({
      label: leanName,
      kind,
      title: title || undefined,
      latexLabel: latexLabel || undefined,
      documentOrder: initialOrder + items.length,
      sourceFile,
      sourceStartLine: sourceStart.line,
      sourceEndLine: sourceEnd.line,
      sourceStartOffset: from,
      sourceEndOffset: to,
      sourceHash: hashTargetText(naturalLanguageLatex),
      syntax: target?.syntax || "comment",
      naturalLanguageLatex,
      naturalLanguageRendered: renderLightLatex(naturalLanguageLatex),
      leanKind: leanKindFor(target, kind),
      leanDeclarationName: leanName || undefined,
      status: "missing-stub"
    });
  }
  return items;
}

function leanKindFor(target, kind) {
  if (target) return target.targetKind === "definition" ? "def" : "theorem";
  // No validated target (missing/invalid/mismatched marker): fall back to a
  // best-effort guess from the environment/command name, same as the
  // original comment-only behavior, for the allowlisted names it can
  // classify. Anything else defaults to "theorem" -- display only, since the
  // item is already marked not formalizable whenever target is absent.
  return DEFINITION_KINDS.has(kind) ? "def" : "theorem";
}

function normalizeFiles(files) {
  const byPath = new Map();
  for (const file of Array.isArray(files) ? files : []) {
    const filePath = normalizePath(file?.path || "");
    if (!filePath || !filePath.toLowerCase().endsWith(".tex")) continue;
    byPath.set(filePath, { path: filePath, content: String(file?.content ?? "") });
  }
  return [...byPath.values()].sort((a, b) => a.path.localeCompare(b.path));
}

// Title-only helper: an environment may open with an optional bracketed or
// braced title immediately after \begin{name} (e.g.
// \begin{definition}[Locally finite family]), before any \label{...}. Body
// extraction itself (bodyFrom/bodyTo) comes from the shared findEnvironments
// above; this only recovers the display title, which is pane-specific and
// not needed by the comment/tag marker parsers.
function extractEnvironmentTitle(source, openerEnd) {
  const index = skipInlineWhitespace(source, openerEnd);
  if (isLineBreak(source[index])) return "";
  const group = parseBalancedSuffix(source, index);
  if (!group.ok) return "";
  return renderLightLatex(source.slice(index + 1, group.end - 1).trim());
}

function extractEnvironmentContent(source, environment) {
  const rawLatex = source.slice(environment.bodyFrom, environment.bodyTo).trim();
  const heading = source.slice(environment.from, environment.bodyTo);
  const labelMatch = heading.match(/\\label\s*\{([^}]*)\}/);
  return {
    rawLatex,
    latexLabel: labelMatch ? labelMatch[1].trim() : "",
    title: extractEnvironmentTitle(source, environment.openerEnd)
  };
}

function renderLightLatex(source) {
  return String(source || "")
    .replace(/^[ \t]*%.*(?:\r?\n|$)/gm, "")
    .replace(/\\label\s*\{[^}]*\}/g, "")
    .replace(/\\(?:emph|textbf|textit|mathrm|operatorname)\s*\{([^{}]*)\}/g, "$1")
    .replace(/\\(?:ref|eqref|cite|autoref)\s*\{([^{}]*)\}/g, "$1")
    .replace(/\\(?:left|right)\b/g, "")
    .replace(/\\[,;:!]/g, " ")
    .replace(/~+/g, " ")
    .replace(/[ \t]*\r?\n[ \t]*/g, "\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

// Loose, best-effort label extraction -- not full validation. Deliberately
// permissive (unlike targetParserCore's strict parser) so that a malformed or
// mismatched marker still surfaces as a pane item (just not formalizable),
// rather than silently disappearing from the inventory. Tries the comment
// marker syntax first, then the tag syntax.
function extractLeaMarkerLabel(source) {
  const text = String(source || "");
  const commentMatch = text.match(/^[ \t]*%\s*lea:\s*(?:formalize|define)?[\s\S]*?\blabel\s*=\s*(?:\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))/im);
  const commentValue = (commentMatch?.[1] || commentMatch?.[2] || "").trim();
  if (isValidLeanIdentifier(commentValue)) return commentValue;

  const tagMatch = text.match(/\\(?:lea|leatheorem|lealemma|leaproposition|leacorollary|leadefinition)\b[\s\S]*?\blabel\s*=\s*(?:\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))/);
  const tagValue = (tagMatch?.[1] || tagMatch?.[2] || "").trim();
  return isValidLeanIdentifier(tagValue) ? tagValue : "";
}

function isValidLeanIdentifier(value) {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(String(value || ""));
}

function offsetToLineColumn(source, offset) {
  const prefix = String(source || "").slice(0, Math.max(0, offset));
  const lines = prefix.split(/\r?\n/);
  return {
    line: lines.length,
    column: lines[lines.length - 1].length + 1
  };
}

function normalizePath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/{2,}/g, "/").trim();
}
