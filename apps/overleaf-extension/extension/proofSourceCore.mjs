import { findEnvironments, parseTargets } from "./targetParserCore.mjs";

const PROOF_ENVIRONMENTS = new Set(["proof", "proof*"]);
const EXPLICIT_ASSOCIATION = /^[ \t]*%\s*lea:\s*proof-for\s*=\s*(?:\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))[ \t]*(?:\r?\n|$)/im;

function pathOf(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/{2,}/g, "/").trim();
}

function lineForOffset(source, offset) {
  return String(source || "").slice(0, Math.max(0, offset)).split(/\r?\n/).length;
}

function targetIdentity(target) {
  return {
    label: String(target?.targetLabel || target?.label || "").trim(),
    sourceFile: pathOf(target?.sourceFile || target?.path || ""),
    sourceEndOffset: Number(target?.sourceEndOffset ?? target?.to ?? -1)
  };
}

function asAssociation(status, method, candidate = null, candidates = []) {
  return {
    status,
    method,
    ...(candidate || {}),
    candidateCount: candidates.length,
    candidates: candidates.map(({ sourceFile, sourceStartLine, sourceEndLine }) => ({
      sourceFile, sourceStartLine, sourceEndLine
    }))
  };
}

/** Browser-safe proof association. Hashing is deliberately left to the caller. */
export function associateProofSourcesCore({ targets = [], files = [] } = {}) {
  const normalizedFiles = (Array.isArray(files) ? files : [])
    .map((file) => ({ path: pathOf(file?.path), content: String(file?.content ?? "") }))
    .filter((file) => file.path && file.path.toLowerCase().endsWith(".tex"));
  const filesByPath = new Map(normalizedFiles.map((file) => [file.path, file]));
  const candidates = normalizedFiles.flatMap((file) =>
    findEnvironments(file.content, { names: PROOF_ENVIRONMENTS })
      .sort((a, b) => a.from - b.from)
      .map((environment) => {
        const raw = file.content.slice(environment.bodyFrom, environment.bodyTo);
        const explicit = raw.match(EXPLICIT_ASSOCIATION);
        return {
          sourceFile: file.path,
          sourceStartOffset: environment.from,
          sourceEndOffset: environment.to,
          sourceStartLine: lineForOffset(file.content, environment.from),
          sourceEndLine: lineForOffset(file.content, environment.to),
          proof: raw.replace(EXPLICIT_ASSOCIATION, "").trim(),
          explicitLabel: String(explicit?.[1] || explicit?.[2] || "").trim()
        };
      })
  );
  const explicitByLabel = new Map();
  for (const candidate of candidates) {
    if (!candidate.explicitLabel) continue;
    const matches = explicitByLabel.get(candidate.explicitLabel) || [];
    matches.push(candidate);
    explicitByLabel.set(candidate.explicitLabel, matches);
  }
  const latexLabelCounts = new Map();
  for (const file of normalizedFiles) {
    for (const target of parseTargets(file.content)) {
      const label = String(target.latexLabel || "").trim();
      if (label) latexLabelCounts.set(label, (latexLabelCounts.get(label) || 0) + 1);
    }
  }
  return (Array.isArray(targets) ? targets : []).map((target) => {
    const identity = targetIdentity(target);
    const targetKind = String(
      target?.targetKind
      || target?.kind
      || (target?.leanKind === "def" ? "definition" : "theorem")
    ).toLowerCase();
    const latexAlias = String(target?.latexLabel || "").trim();
    const explicit = explicitByLabel.get(identity.label)
      || (latexLabelCounts.get(latexAlias) === 1 ? explicitByLabel.get(latexAlias) : null)
      || [];
    let proofAssociation;
    if (targetKind === "definition" || targetKind === "def") {
      proofAssociation = asAssociation("not_applicable", "none", null, []);
    } else if (explicit.length === 1) {
      proofAssociation = asAssociation("associated", "explicit", explicit[0], explicit);
    } else if (explicit.length > 1) {
      proofAssociation = asAssociation("ambiguous", "explicit", null, explicit);
    } else {
      const file = filesByPath.get(identity.sourceFile);
      const adjacent = file ? candidates.filter((candidate) => {
        if (candidate.sourceFile !== identity.sourceFile || candidate.sourceStartOffset < identity.sourceEndOffset) return false;
        const between = file.content.slice(Math.max(0, identity.sourceEndOffset), candidate.sourceStartOffset);
        return between.replace(/%[^\r\n]*/g, "").trim().length === 0;
      }) : [];
      proofAssociation = adjacent.length
        ? asAssociation("associated", "adjacent", adjacent[0], [adjacent[0]])
        : asAssociation("missing", "none", null, []);
    }
    return {
      ...target,
      proofAssociation,
      sourceProof: proofAssociation.status === "associated" ? proofAssociation.proof : ""
    };
  });
}
