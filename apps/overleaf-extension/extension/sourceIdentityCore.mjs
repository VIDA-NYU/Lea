// The version-2 source hash contract is shared by the browser and companion.
// Keep property order stable: the adapter hashes the same JSON shape in Python.
export function canonicalSourceTargetKind(value) {
  return value === "definition" || value === "def" ? "definition" : "theorem";
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
