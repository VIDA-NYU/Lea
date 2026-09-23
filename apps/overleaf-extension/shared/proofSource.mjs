import { createHash } from "node:crypto";
import { findEnvironments, parseTargets } from "../extension/targetParserCore.mjs";

const PROOF_ENVIRONMENTS = new Set(["proof", "proof*"]);
const EXPLICIT_ASSOCIATION = /^[ \t]*%\s*lea:\s*proof-for\s*=\s*(?:\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))[ \t]*(?:\r?\n|$)/im;

function normalizePath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/{2,}/g, "/").trim();
}

function normalizeNewlines(value) {
  return String(value || "").replace(/\r\n?/g, "\n");
}

function lineForOffset(source, offset) {
  return normalizeNewlines(source).slice(0, Math.max(0, offset)).split("\n").length;
}

function hashJson(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function proofCandidate(file, environment) {
  const raw = file.content.slice(environment.bodyFrom, environment.bodyTo);
  const explicit = raw.match(EXPLICIT_ASSOCIATION);
  const proof = raw.replace(EXPLICIT_ASSOCIATION, "").trim();
  return {
    sourceFile: file.path,
    sourceStartOffset: environment.from,
    sourceEndOffset: environment.to,
    sourceStartLine: lineForOffset(file.content, environment.from),
    sourceEndLine: lineForOffset(file.content, environment.to),
    proof,
    proofHash: createHash("sha256").update(normalizeNewlines(proof)).digest("hex"),
    explicitLabel: String(explicit?.[1] || explicit?.[2] || "").trim()
  };
}

function targetIdentity(target) {
  return {
    label: String(target?.targetLabel || target?.label || "").trim(),
    sourceFile: normalizePath(target?.sourceFile || target?.path || ""),
    sourceStartOffset: Number(target?.sourceStartOffset ?? target?.from ?? -1),
    sourceEndOffset: Number(target?.sourceEndOffset ?? target?.to ?? -1)
  };
}

function interveningBoundary(file, target, proof) {
  const between = file.content.slice(Math.max(0, target.sourceEndOffset), Math.max(0, proof.sourceStartOffset));
  // Inferred association is intentionally strict: after removing ordinary
  // comments, only whitespace may separate the statement from its proof.
  // This rejects unmarked/custom intervening theorem environments too, which
  // `parseTargets` cannot see and which would otherwise attach the wrong proof.
  return between.replace(/%[^\r\n]*/g, "").trim().length > 0;
}

function association(status, method, candidate = null, candidates = []) {
  return {
    status,
    method,
    ...(candidate ? candidate : {}),
    candidateCount: candidates.length,
    candidates: candidates.map(({ sourceFile, sourceStartLine, sourceEndLine }) => ({
      sourceFile,
      sourceStartLine,
      sourceEndLine
    }))
  };
}

/**
 * Associate complete natural-language proof bodies with Lea targets.
 *
 * Explicit `% lea: proof-for=<target>` markers win and may cross files. Without
 * one, only the first adjacent proof in the same file is accepted, and never
 * across another statement target or section boundary. Ambiguity is surfaced;
 * proof alternatives are not concatenated or guessed.
 */
export function associateProofSources({ targets = [], files = [] } = {}) {
  const normalizedFiles = (Array.isArray(files) ? files : [])
    .map((file) => ({ path: normalizePath(file?.path), content: String(file?.content ?? "") }))
    .filter((file) => file.path && file.path.toLowerCase().endsWith(".tex"));
  const filesByPath = new Map(normalizedFiles.map((file) => [file.path, file]));
  const candidates = normalizedFiles.flatMap((file) =>
    findEnvironments(file.content, { names: PROOF_ENVIRONMENTS })
      .sort((a, b) => a.from - b.from)
      .map((environment) => proofCandidate(file, environment))
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

  const associatedTargets = (Array.isArray(targets) ? targets : []).map((original) => {
    const identity = targetIdentity(original);
    const targetKind = String(
      original?.targetKind
      || original?.kind
      || (original?.leanKind === "def" ? "definition" : "theorem")
    ).toLowerCase();
    const latexAlias = String(original?.latexLabel || "").trim();
    const explicit = explicitByLabel.get(identity.label)
      || (latexLabelCounts.get(latexAlias) === 1 ? explicitByLabel.get(latexAlias) : null)
      || [];
    let proofAssociation;
    if (targetKind === "definition" || targetKind === "def") {
      proofAssociation = association("not_applicable", "none", null, []);
    } else if (explicit.length === 1) {
      proofAssociation = association("associated", "explicit", explicit[0], explicit);
    } else if (explicit.length > 1) {
      proofAssociation = association("ambiguous", "explicit", null, explicit);
    } else {
      const file = filesByPath.get(identity.sourceFile);
      const adjacent = file
        ? candidates.filter((candidate) =>
            candidate.sourceFile === identity.sourceFile &&
            candidate.sourceStartOffset >= identity.sourceEndOffset &&
            !interveningBoundary(file, identity, candidate)
          )
        : [];
      proofAssociation = adjacent.length
        ? association("associated", "adjacent", adjacent[0], [adjacent[0]])
        : association("missing", "none", null, []);
    }
    return {
      ...original,
      proofAssociation,
      sourceProof: proofAssociation.status === "associated" ? proofAssociation.proof : ""
    };
  });

  const targetLabels = new Set(associatedTargets.map((target) => targetIdentity(target).label).filter(Boolean));
  for (const target of associatedTargets) {
    const label = String(target?.latexLabel || "").trim();
    if (latexLabelCounts.get(label) === 1) targetLabels.add(label);
  }
  const unresolvedExplicit = candidates
    .filter((candidate) => candidate.explicitLabel && !targetLabels.has(candidate.explicitLabel))
    .map((candidate) => ({
      code: "unresolved_proof_association",
      label: candidate.explicitLabel,
      sourceFile: candidate.sourceFile,
      sourceStartLine: candidate.sourceStartLine,
      message: `Proof explicitly targets unknown Lea label ${candidate.explicitLabel}.`
    }));
  return {
    targets: associatedTargets,
    diagnostics: [
      ...associatedTargets
      .filter((target) => target.proofAssociation.status !== "associated")
      .filter((target) => target.proofAssociation.status !== "not_applicable")
      .map((target) => ({
        code: target.proofAssociation.status === "ambiguous" ? "ambiguous_source_proof" : "missing_source_proof",
        label: targetIdentity(target).label,
        message: target.proofAssociation.status === "ambiguous"
          ? `Multiple source proofs explicitly target ${targetIdentity(target).label}.`
          : `No unambiguous source proof is associated with ${targetIdentity(target).label}.`
      })),
      ...unresolvedExplicit
    ]
  };
}

export function buildFormalizationSourceBundle(target, {
  targetContext = "",
  targetUses = [],
  relevantSource = [],
  mirror = null
} = {}) {
  const identity = targetIdentity(target);
  const association = target?.proofAssociation || { status: "missing", method: "none" };
  const bundle = {
    version: 2,
    targetKey: identity.label,
    targetKind: String(target?.targetKind || target?.kind || "theorem"),
    statement: normalizeNewlines(target?.targetText || target?.naturalLanguageLatex || ""),
    proof: normalizeNewlines(target?.sourceProof || ""),
    proofAssociation: {
      status: String(association.status || "missing"),
      method: String(association.method || "none"),
      sourceFile: normalizePath(association.sourceFile || ""),
      sourceStartLine: Number(association.sourceStartLine || 0),
      sourceEndLine: Number(association.sourceEndLine || 0),
      proofHash: String(association.proofHash || "")
    },
    statementLocation: {
      sourceFile: identity.sourceFile,
      sourceStartLine: Number(target?.sourceStartLine || 0),
      sourceEndLine: Number(target?.sourceEndLine || 0)
    },
    uses: (Array.isArray(targetUses) ? targetUses : []).map(String),
    context: normalizeNewlines(targetContext),
    relevantSource: (Array.isArray(relevantSource) ? relevantSource : []).map((fragment) => ({
      ...fragment,
      content: normalizeNewlines(fragment?.content || "")
    })),
    mirror: mirror ? { ...mirror } : null
  };
  // Locations are report-navigation metadata, not mathematical identity. A
  // heading inserted above the theorem must not invalidate an unchanged proof.
  const hashIdentity = {
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
    ...bundle,
    bundleHash: hashJson(hashIdentity),
    // Bounded excerpts and the whole-project mirror revision are frozen
    // evaluator evidence. They are not target identity: an unrelated paragraph
    // elsewhere in the paper must not make this formalization stale.
    sourceIdentityHash: hashJson({ ...hashIdentity, relevantSource: [], mirror: null })
  };
}
