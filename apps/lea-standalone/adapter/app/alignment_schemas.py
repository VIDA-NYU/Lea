"""Validated contracts for the Overleaf source-to-Lean alignment check."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceLocation(BaseModel):
    sourceFile: str = ""
    sourceStartLine: int = 0
    sourceEndLine: int = 0


class ProofAssociation(SourceLocation):
    status: Literal["associated", "missing", "ambiguous", "not_applicable"] = "missing"
    method: Literal["explicit", "adjacent", "none"] = "none"
    proofHash: str = ""


class SourceBundle(BaseModel):
    version: Literal[2]
    targetKey: str
    targetKind: str = "theorem"
    statement: str
    proof: str = ""
    proofAssociation: ProofAssociation
    statementLocation: SourceLocation
    uses: list[str] = Field(default_factory=list)
    context: str = ""
    relevantSource: list[dict] = Field(default_factory=list)
    mirror: dict | None = None
    bundleHash: str


class AlignmentCheckRequest(BaseModel):
    source_bundle: SourceBundle
    trigger: Literal[
        "solver_terminal", "solver_paused", "github_import", "manual", "retry"
    ] = "manual"
    solver_run_id: str | None = None


class EvidenceReference(BaseModel):
    side: Literal["latex", "lean", "system"]
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    excerpt: str = ""


class AlignmentFinding(BaseModel):
    severity: Literal["warning", "caveat", "info"]
    category: Literal[
        "statement", "proof_method", "assumption", "case_split", "witness",
        "dependency", "source_gap", "formalization_choice", "other",
    ]
    title: str
    detail: str
    evidence: list[EvidenceReference] = Field(default_factory=list)


class AlignmentDimensions(BaseModel):
    statement: Literal["aligned", "discrepancy", "inconclusive", "not_applicable"] = "inconclusive"
    proof_strategy: Literal["aligned", "discrepancy", "inconclusive", "not_applicable"] = "inconclusive"
    coverage: Literal["aligned", "discrepancy", "inconclusive", "not_applicable"] = "inconclusive"
    dependencies: Literal["aligned", "discrepancy", "inconclusive", "not_applicable"] = "inconclusive"


class AlignmentMatch(BaseModel):
    summary: str
    latex_reference: EvidenceReference | None = None
    lean_reference: EvidenceReference | None = None


class EvaluatedRevision(BaseModel):
    source_hash: str = ""
    artifact_commit: str = ""
    artifact_content_hash: str = ""
    dependency_hash: str = ""


class AlignmentReport(BaseModel):
    schema_version: Literal[1] = 1
    verdict: Literal["approved", "warning", "error"]
    scope: Literal["statement_and_proof", "statement_only", "partial_artifact"] = "statement_and_proof"
    summary: str
    confidence: Literal["high", "medium", "low"] = "medium"
    dimensions: AlignmentDimensions = Field(default_factory=AlignmentDimensions)
    statement_match: Literal["matches", "equivalent", "mismatch", "uncertain"]
    approach_match: Literal[
        "faithful", "minor_deviation", "material_deviation", "insufficient_evidence",
    ]
    matches: list[AlignmentMatch] = Field(default_factory=list)
    findings: list[AlignmentFinding] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    remaining_obligations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    recommended_next_action: str = ""
    evaluated_revision: EvaluatedRevision = Field(default_factory=EvaluatedRevision)


def normalize_report(report: AlignmentReport) -> AlignmentReport:
    """Make the verdict a deterministic projection of the structured evidence."""
    warning = (
        report.statement_match in {"mismatch", "uncertain"}
        or report.approach_match in {"material_deviation", "insufficient_evidence"}
        or any(item.severity == "warning" for item in report.findings)
    )
    if report.verdict != "error":
        report.verdict = "warning" if warning else "approved"
    return report
