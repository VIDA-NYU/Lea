"""Freeze source evidence at admission; preserve target vs evidence hash semantics."""
import hashlib
import json
from .alignment_schemas import SourceBundle


def source_hash(bundle: dict, *, identity=False) -> str:
    proof = bundle.get("proofAssociation") or {}
    value = {
        "version": bundle["version"], "targetKey": bundle["targetKey"],
        "targetKind": bundle.get("targetKind", "theorem"), "statement": bundle["statement"],
        "proof": bundle.get("proof", ""),
        "proofAssociation": {k: proof.get(k) for k in ("status", "method", "sourceFile", "proofHash")},
        "uses": bundle.get("uses") or [], "context": bundle.get("context") or "",
        "relevantSource": [] if identity else bundle.get("relevantSource") or [],
        "mirror": None if identity else bundle.get("mirror"),
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_source(bundle: dict) -> dict:
    value = SourceBundle.model_validate(bundle).model_dump()
    evidence = source_hash(value)
    identity = source_hash(value, identity=True)
    if value["bundleHash"] != evidence:
        raise ValueError("source bundle hash does not match its contents")
    if bundle.get("sourceIdentityHash") and bundle["sourceIdentityHash"] != identity:
        raise ValueError("source identity hash does not match its contents")
    value["sourceIdentityHash"] = identity
    return value


def compare_source(previous: dict, current: dict, coverage: str = "complete") -> tuple[str, str | None]:
    """Compare content, not differences in how an evidence snapshot was captured."""
    if any(previous.get(k) != current.get(k) for k in ("targetKey", "targetKind")):
        raise ValueError("Current source belongs to a different formalization target.")
    if coverage != "complete":
        return "unknown", "The full source inventory is unavailable; source freshness could not be checked."
    if source_hash(previous, identity=True) != source_hash(current, identity=True):
        return "source_changed", "The statement, associated proof, or formalization context changed since this run."
    def fragment_content(fragment):
        content = str(fragment.get("content", "")).replace("\r\n", "\n").replace("\r", "\n")
        # Older browser captures appended this acquisition marker after the
        # bounded excerpt. Preserve their hashes, but compare the source bytes.
        if len(content) == 12000 + len("\n[excerpt truncated]") and content.endswith("\n[excerpt truncated]"):
            content = content[:12000]
        return content
    def fragments(bundle):
        return sorted((str(f.get("kind", "context")), str(f.get("path", "")),
                       fragment_content(f))
                      for f in bundle.get("relevantSource", []))
    before, after = fragments(previous), fragments(current)
    if [(k, p) for k, p, _ in before] != [(k, p) for k, p, _ in after]:
        return "unknown", "The available source context has a different scope; its freshness could not be checked."
    if before != after:
        return "evidence_changed", "Surrounding source context changed since this run."
    old_mirror, new_mirror = previous.get("mirror"), current.get("mirror")
    if bool(old_mirror) != bool(new_mirror):
        return "unknown", "The source mirror is unavailable for a comparable context check."
    if old_mirror and old_mirror.get("revision") != new_mirror.get("revision"):
        return "evidence_changed", "The surrounding project sources changed since this run."
    return "current", None
