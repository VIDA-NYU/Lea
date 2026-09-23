"""Host-mediated Overleaf reporting. No provider calls or adapter dependencies."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

TOOL_NAME = "update_lea_status"
VERSION = 1


def _text(limit=4000, *, nonempty=False):
    return {"type": "string", "maxLength": limit, **({"minLength": 1} if nonempty else {})}


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _list(item, maximum=100):
    return {"type": "array", "items": item, "maxItems": maximum}


EVIDENCE = _object({
    "side": _enum("latex", "lean", "system"), "path": _text(1000),
    "start_line": {"type": "integer", "minimum": 1},
    "end_line": {"type": "integer", "minimum": 1}, "excerpt": _text(2000),
}, ["side"])
MATCH = _object({"summary": _text(), "latex_reference": EVIDENCE, "lean_reference": EVIDENCE}, ["summary"])
FINDING = _object({
    "key": _text(160, nonempty=True),
    "severity": _enum("info", "caveat", "warning", "blocking"),
    "category": _enum("statement", "proof_method", "assumption", "case_split", "witness", "dependency", "source_gap", "formalization_choice", "other"),
    "title": _text(300, nonempty=True), "detail": _text(nonempty=True),
    "evidence": _list(EVIDENCE, 10),
    "lean_resolution": _enum("not_applicable", "open", "planned", "applied"),
    "source_resolution": _enum("not_applicable", "open", "resolved", "retracted"),
    "correction": _text(), "resolution_explanation": _text(),
}, ["key", "severity", "category", "title", "detail", "lean_resolution", "source_resolution"])
INPUT_SCHEMA = _object({
    "kind": _enum("initial", "progress", "finding", "final"),
    "confidence": _enum("unassessed", "low", "medium", "high"),
    "summary": _text(500, nonempty=True),
    "scope": _enum("source_only", "statement_only", "partial_artifact", "statement_and_proof"),
    "confidence_reason": _text(nonempty=True), "current_work": _text(),
    "matches": _list(MATCH), "finding_updates": _list(FINDING, 10),
    "caveats": _list(_text()), "remaining_obligations": _list(_text()), "limitations": _list(_text()),
    "recommended_next_action": _object({
        "kind": _enum("continue", "review_latex", "pause_and_edit_latex", "inspect_result"),
        "detail": _text(),
    }, ["kind", "detail"]),
}, ["kind", "confidence", "summary", "scope", "confidence_reason"])
TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Publish the author's live Lea Status confidence, progress, findings and corrections. This is the only way to update Lea Status in Overleaf. Publish before repairing source issues and at milestones; a blocking finding requests author input and pauses only when this run's pause policy allows it. A missing source proof, ordinary proof gap, Lean encoding choice, or equivalent library-lemma substitution is not blocking when the claim and any explicit source method remain intact. Reserve blocking findings for a required semantic change, a materially ambiguous claim, or abandoning an explicitly supplied proof method. When the run is marked Author-authorized best-effort continuation, conventional inferred context and assumptions are authorized formalization choices: disclose them and retain the open source gap as a non-blocking warning instead of blocking again. This reports your assessment, not independent verification. Check final edits before a final update.",
    "input_schema": INPUT_SCHEMA,
}


def _validate(value, schema, path="status"):
    kind = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int}[kind]
    if not valid:
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: must be one of {schema['enum']}")
    if kind == "object":
        extra = set(value) - schema["properties"].keys()
        missing = set(schema["required"]) - value.keys()
        if extra or missing:
            raise ValueError(f"{path}: unknown fields {sorted(extra)}, missing fields {sorted(missing)}")
        for key, item in value.items():
            _validate(item, schema["properties"][key], f"{path}.{key}")
        if "end_line" in value and value["end_line"] < value.get("start_line", 1):
            raise ValueError(f"{path}: end_line precedes start_line")
    elif kind == "array":
        if len(value) > schema["maxItems"]:
            raise ValueError(f"{path}: too many entries")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
    elif kind == "string":
        if len(value) > schema.get("maxLength", 4000) or (schema.get("minLength") and not value.strip()):
            raise ValueError(f"{path}: empty or too long")
    elif value < schema.get("minimum", 0):
        raise ValueError(f"{path}: must be positive")


def validate_payload(payload: dict) -> dict:
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 32768:
        raise ValueError("Lea Status exceeds 32 KiB")
    _validate(payload, INPUT_SCHEMA)
    keys = [f["key"] for f in payload.get("finding_updates", [])]
    if len(set(keys)) != len(keys):
        raise ValueError("finding keys must be unique within an update")
    for finding in payload.get("finding_updates", []):
        if finding["severity"] in {"warning", "blocking"} and finding["source_resolution"] == "not_applicable" and finding["lean_resolution"] == "not_applicable":
            raise ValueError("Material findings require an explicit source or Lean disposition")
        if finding["source_resolution"] in {"resolved", "retracted"} and not finding.get("resolution_explanation", "").strip():
            raise ValueError("Resolving or retracting a finding requires an explanation")
        if finding["lean_resolution"] in {"planned", "applied"} and not finding.get("correction", "").strip():
            raise ValueError("A planned/applied correction requires a description")
    if payload["kind"] == "final":
        required = {"matches", "caveats", "remaining_obligations", "limitations", "recommended_next_action"}
        if not required <= payload.keys():
            raise ValueError(f"Final update must refresh {sorted(required)}")
    return copy.deepcopy(payload)


def merge_assessment(previous: dict | None, payload: dict, source_hash: str) -> dict:
    """Omitted findings survive; a Lean fix never silently resolves the source."""
    result = copy.deepcopy(previous or {})
    findings = {f["key"]: f for f in result.get("findings", [])}
    for incoming in payload.get("finding_updates", []):
        item = copy.deepcopy(incoming)
        prior = findings.get(item["key"])
        if item["source_resolution"] == "resolved":
            if not prior or prior.get("source_hash") == source_hash:
                raise ValueError("Source resolution requires a finding from an earlier source revision")
        if prior and (prior["severity"] in {"warning", "blocking"} and item["severity"] not in {"warning", "blocking"} and item["source_resolution"] not in {"resolved", "retracted"}
                      or prior["source_resolution"] == "open" and item["source_resolution"] == "not_applicable"):
            raise ValueError("Resolve or retract a material source finding explicitly; do not downgrade it")
        severity_rank = {"info": 0, "caveat": 1, "warning": 2, "blocking": 3}
        if prior and severity_rank[item["severity"]] < severity_rank[prior["severity"]] and not item.get("resolution_explanation", "").strip():
            raise ValueError("Reducing a finding's severity requires an explanation")
        item["source_hash"] = prior.get("source_hash", source_hash) if prior and item["source_resolution"] == "open" else source_hash
        findings[item["key"]] = item
    result.update({k: copy.deepcopy(v) for k, v in payload.items() if k != "finding_updates"})
    result["findings"] = list(findings.values())
    if payload["kind"] == "final":
        result["current_work"] = payload.get("current_work", "")
    return result


def attention(assessment: dict | None) -> str:
    findings = (assessment or {}).get("findings", [])
    active = [f for f in findings if f.get("source_resolution") not in {"resolved", "retracted"}
              and (f.get("source_resolution") == "open" or f.get("lean_resolution") in {"open", "planned"})]
    if any(f["severity"] == "blocking" for f in active):
        return "needs_author_input"
    return "source_issue" if any(f["severity"] == "warning" for f in active) else "none"


@dataclass(frozen=True)
class LeaStatusUpdateRequested:
    payload: dict
    invocation_key: str


@dataclass(frozen=True)
class LeaStatusUpdateAck:
    accepted: bool
    update_id: str | None = None
    sequence: int | None = None
    assessment: dict = field(default_factory=dict)
    error: str | None = None
    stop_reason: str | None = None


def unsupported_handler(args):
    return "Error: Lea Status requires a supported Overleaf run and a reporting host."
