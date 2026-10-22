"""Host-mediated Overleaf reporting. No provider calls or adapter dependencies."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

TOOL_NAME = "update_lea_status"
VERSION = 1


def _described(schema, description):
    return {**schema, "description": description} if description else schema


def _text(limit=4000, *, nonempty=False, description=None):
    return _described({"type": "string", "maxLength": limit, **({"minLength": 1} if nonempty else {})}, description)


def _enum(*values, description=None):
    return _described({"type": "string", "enum": list(values)}, description)


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _list(item, maximum=100, *, description=None):
    return _described({"type": "array", "items": item, "maxItems": maximum}, description)


EVIDENCE = _object({
    "side": _enum("latex", "lean", "system"), "path": _text(1000),
    "start_line": {"type": "integer", "minimum": 1},
    "end_line": {"type": "integer", "minimum": 1}, "excerpt": _text(2000),
}, ["side"])
MATCH = _object({"summary": _text(), "latex_reference": EVIDENCE, "lean_reference": EVIDENCE}, ["summary"])
FINDING = _object({
    "key": _text(160, nonempty=True, description=
        "Stable identifier for this finding, e.g. 'depth_cutoff_offbyone'. Reuse the same key to update "
        "a finding; findings omitted from an update are kept unchanged."),
    "severity": _enum("info", "caveat", "warning", "blocking", description=
        "info: neutral note. caveat: a disclosed formalization choice the author may want to know about. "
        "warning: a problem in the LaTeX source the author should look at (flags a source issue in "
        "Overleaf while open). blocking: a required semantic change or materially ambiguous claim; "
        "requests author input and may pause the run. Lowering a finding's severity requires "
        "resolution_explanation; a warning or blocking finding cannot drop below warning unless "
        "source_resolution is 'resolved' or 'retracted'."),
    "category": _enum("statement", "proof_method", "assumption", "case_split", "witness", "dependency", "source_gap", "formalization_choice", "other",
                      description="What the finding is about."),
    "title": _text(300, nonempty=True, description="One-line summary shown as the finding's heading."),
    "detail": _text(nonempty=True, description=
        "What you observed and why it matters. Describe the issue here; describe any Lean change in "
        "`correction`, not here."),
    "evidence": _list(EVIDENCE, 10, description="Pointers to the LaTeX or Lean lines involved."),
    "lean_resolution": _enum("not_applicable", "open", "planned", "applied", description=
        "Status of the Lean side of this finding. not_applicable: no Lean change is involved. open: not "
        "yet addressed in Lean. planned: you will change the Lean to address it. applied: you have changed "
        "the Lean to address it. 'planned' and 'applied' REQUIRE `correction`. Do not report 'open' for a "
        "change you have already made."),
    "source_resolution": _enum("not_applicable", "open", "resolved", "retracted", description=
        "Status of the LaTeX side. not_applicable: the source is fine. open: the source itself has a "
        "problem only the author can fix. resolved: the author edited the source and the problem is gone "
        "(only valid for a finding first reported on an earlier source revision). retracted: the finding "
        "was mistaken. 'resolved' and 'retracted' REQUIRE `resolution_explanation`. A Lean change never "
        "resolves a source issue."),
    "correction": _text(description=
        "Required when lean_resolution is 'planned' or 'applied'. What the Lean change is, e.g. 'Shifted "
        "the depth cutoff to d < clog 2 n + 1 to match the source's inclusive bound.' Shown in Overleaf as "
        "'Lean correction (Applied): ...'."),
    "resolution_explanation": _text(description=
        "Required when source_resolution is 'resolved' or 'retracted', or when lowering severity from an "
        "earlier update. Why the source issue is closed or the severity dropped. Not used for Lean "
        "changes; put those in `correction`."),
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
        key = finding["key"]
        if finding["severity"] in {"warning", "blocking"} and finding["source_resolution"] == "not_applicable" and finding["lean_resolution"] == "not_applicable":
            raise ValueError(f"finding '{key}': a {finding['severity']} finding cannot have both "
                             "lean_resolution and source_resolution 'not_applicable'")
        if finding["source_resolution"] in {"resolved", "retracted"} and not finding.get("resolution_explanation", "").strip():
            raise ValueError(f"finding '{key}': source_resolution '{finding['source_resolution']}' requires "
                             "a non-empty 'resolution_explanation'")
        if finding["lean_resolution"] in {"planned", "applied"} and not finding.get("correction", "").strip():
            raise ValueError(f"finding '{key}': lean_resolution '{finding['lean_resolution']}' requires a "
                             "non-empty 'correction' field describing the Lean change "
                             "('resolution_explanation' is only for source resolutions)")
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
            raise ValueError(f"finding '{item['key']}': lowering severity from '{prior['severity']}' to "
                             f"'{item['severity']}' requires 'resolution_explanation'")
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
