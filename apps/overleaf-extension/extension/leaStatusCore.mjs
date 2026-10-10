const CONFIDENCE = new Set(["unassessed", "low", "medium", "high"]);
const ATTENTION = new Set(["none", "source_issue", "needs_author_input"]);

export function normalizeLeaStatus(value) {
  const status = value && typeof value === "object" ? value : {};
  const assessment = status.assessment && CONFIDENCE.has(status.assessment.confidence) ? status.assessment : null;
  return { ...status, assessment, confidence: assessment?.confidence || "unassessed",
    attention: ATTENTION.has(status.attention) ? status.attention : "none",
    freshness: status.freshness || "unknown", activity: status.activity || { status: "unknown" } };
}

export function leaStatusLabel(value) {
  const status = normalizeLeaStatus(value);
  const label = status.confidence === "unassessed" ? "Not assessed"
    : `${status.confidence[0].toUpperCase()}${status.confidence.slice(1)} confidence`;
  const attention = status.attention === "needs_author_input" ? " · Needs author input"
    : status.attention === "source_issue" ? " · Source issue" : "";
  const reason = { source_changed: "Source changed", evidence_changed: "Surrounding source context changed",
    newer_artifact: "Updated work not yet assessed", dependency_changed: "Supporting results changed" }[status.freshness];
  const freshness = reason ? ` · ${reason}${status.assessment ? " · Last assessed version" : ""}`
    : status.assessment && status.freshness === "unknown" ? " · Freshness unavailable" : "";
  return label + assessmentScopeLabel(status.assessment?.scope) + attention + freshness;
}

export function assessmentScopeLabel(scope) {
  const label = { source_only: "Source assessment", partial_artifact: "Partial artifact",
    statement_and_proof: "Statement and proof" }[scope];
  return label ? ` · ${label}` : "";
}

export function applyLeaStatusEvent(previous, event) {
  if (!event?.run_id || !event?.formalization_id || !Number.isInteger(event.sequence) || event.sequence < 1 || !Number.isInteger(event.run_generation) || event.run_generation < 1 || !event.assessment) return previous;
  if (previous?.formalization_id && previous.formalization_id !== event.formalization_id) return previous;
  const generation = Number(previous?.run_generation || 0);
  if (Number(event.run_generation) < generation) return previous;
  if (Number(event.run_generation) === generation && event.sequence <= Number(previous?.sequence || 0)) return previous;
  // The event orders assessments only. Source freshness and run lifecycle require a fresh read.
  return normalizeLeaStatus({ ...previous, ...event, freshness: "unknown" });
}
