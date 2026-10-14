// The version-2 source hash contract is shared by the browser and companion.
// Keep property order stable: the adapter hashes the same JSON shape in Python.
export function canonicalSourceTargetKind(value) {
  return value === "definition" || value === "def" ? "definition" : "theorem";
}

const normalizePath = value => String(value || "").replaceAll("\\", "/").replace(/^\/+/, "").trim();
const normalizeText = value => String(value || "").replace(/\r\n?/g, "\n");

// The browser and the inventory must capture the same bounded evidence. These
// constants describe acquisition, and do not change the frozen v2 hash contract.
export function surroundingSource(target, files = []) {
  const path = normalizePath(target.sourceFile);
  const file = files.find(candidate => normalizePath(candidate.path) === path);
  if (!file || !file.content) return [];
  const lines = normalizeText(file.content).split("\n");
  const first = Math.max(1, Number(target.sourceStartLine) || 1);
  const last = Math.max(first, Number(target.sourceEndLine) || first);
  const startLine = Math.max(1, first - 24);
  const endLine = Math.min(lines.length, last + 24);
  const excerpt = lines.slice(startLine - 1, endLine).join("\n");
  return [{ kind: "surrounding-context", path, startLine, endLine,
    content: excerpt.slice(0, 12000), truncated: excerpt.length > 12000 }];
}

export function buildSourceBundleCore(target, {
  targetContext = target.targetContext || "", targetUses = target.targetUses || [],
  relevantSource, files = [], mirror = null, proofHash = target.proofAssociation?.proofHash || ""
} = {}) {
  const association = target.proofAssociation || {};
  return {
    version: 2,
    targetKey: String(target.targetLabel || target.label || ""),
    targetKind: canonicalSourceTargetKind(target.targetKind || target.leanKind || target.kind),
    statement: normalizeText(target.targetText || target.naturalLanguageLatex),
    proof: normalizeText(target.sourceProof),
    proofAssociation: {
      status: String(association.status || "missing"), method: String(association.method || "none"),
      sourceFile: normalizePath(association.sourceFile), sourceStartLine: Number(association.sourceStartLine || 0),
      sourceEndLine: Number(association.sourceEndLine || 0), proofHash: String(proofHash)
    },
    statementLocation: { sourceFile: normalizePath(target.sourceFile),
      sourceStartLine: Number(target.sourceStartLine || 0), sourceEndLine: Number(target.sourceEndLine || 0) },
    uses: (Array.isArray(targetUses) ? targetUses : []).map(String), context: normalizeText(targetContext),
    relevantSource: (relevantSource || surroundingSource(target, files)).map(fragment => ({
      ...fragment, content: normalizeText(fragment.content)
    })),
    mirror: mirror ? { ...mirror } : null
  };
}

export function sourceHashInputs(bundle) {
  const evidence = {
    version: bundle.version,
    targetKey: bundle.targetKey,
    targetKind: bundle.targetKind,
    statement: bundle.statement,
    proof: bundle.proof,
    proofAssociation: {
      status: bundle.proofAssociation.status,
      method: bundle.proofAssociation.method,
      sourceFile: bundle.proofAssociation.sourceFile,
      proofHash: bundle.proofAssociation.proofHash
    },
    uses: bundle.uses,
    context: bundle.context,
    relevantSource: bundle.relevantSource,
    mirror: bundle.mirror
  };
  return {
    evidence,
    identity: { ...evidence, relevantSource: [], mirror: null }
  };
}
