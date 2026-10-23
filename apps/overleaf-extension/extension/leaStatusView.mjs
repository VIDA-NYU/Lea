import { normalizeLeaStatus, leaStatusLabel } from "./leaStatusCore.mjs";
import { dependencyWait } from "./dependencyWaitCore.mjs";
export { leaStatusLabel };

// Reuse nodes across pane refreshes so open history, selection, and focus survive.
const views = new Map();
const friendly = (value) => String(value || "").replaceAll("_", " ");

export function renderLeaStatus(item, { document, loadHistory, pause }) {
  const key = item.formalizationId || item.id;
  let view = views.get(key);
  const node = (tag, text, parent) => {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (parent) parent.appendChild(value);
    return value;
  };
  if (!view || view.root.ownerDocument !== document) {
    const root = node("section");
    root.className = "ol-lean-live-status";
    const summary = node("p", "", root);
    const meta = node("p", "", root);
    meta.className = "ol-lean-live-status-meta";
    const pauseButton = node("button", "Pause", root);
    pauseButton.type = "button";
    pauseButton.className = "ol-lean-secondary-button";
    const error = node("p", "", root);
    error.setAttribute("role", "status");
    const announcement = node("p", "", root);
    announcement.className = "ol-lean-status-announcement";
    announcement.setAttribute("aria-live", "polite");
    const details = node("details", undefined, root);
    node("summary", "Lea Status details", details);
    const body = node("div", undefined, details);
    const history = node("details", undefined, details);
    node("summary", "Update history", history);
    const entries = node("div", undefined, history);
    const more = node("button", "Load updates", history);
    more.type = "button";
    more.className = "ol-lean-secondary-button";
    view = { root, summary, meta, details, body, history, entries, more, pauseButton, error, announcement,
      ids: new Set(), cursor: null, version: null, item };
    const currentView = view;
    pauseButton.addEventListener("click", async () => {
      pauseButton.disabled = true;
      pauseButton.textContent = dependencyWait(currentView.item) ? "Stopping wait…" : "Pausing…";
      try { await pause(currentView.item); }
      catch (failure) {
        error.textContent = failure.message;
        pauseButton.disabled = false;
        pauseButton.textContent = dependencyWait(currentView.item) ? "Stop waiting" : "Pause";
      }
    });
    more.addEventListener("click", async () => {
      more.disabled = true;
      try {
        const result = await loadHistory(currentView.item, currentView.cursor);
        for (const update of result.updates || []) {
          if (currentView.ids.has(update.id)) continue;
          currentView.ids.add(update.id);
          const entry = node("details", undefined, entries);
          node("summary", `${new Date(update.created_at).toLocaleString()} · ${friendly(update.assessment?.confidence)} confidence · ${update.assessment?.summary || ""}`, entry);
          renderAssessment(node, entry, update.assessment);
        }
        for (const report of result.legacy_reports || []) {
          if (currentView.ids.has(report.id)) continue;
          currentView.ids.add(report.id);
          const entry = node("details", undefined, entries);
          node("summary", `Previous Lea Check report · ${report.status}`, entry);
          renderAssessment(node, entry, report.report);
        }
        currentView.cursor = result.next_cursor;
        more.hidden = !result.has_more;
        if (!currentView.ids.size) node("p", "No updates have been published yet.", entries);
      } catch (failure) { error.textContent = failure.message; }
      finally { more.disabled = false; more.textContent = "Load more updates"; }
    });
    history.addEventListener("toggle", () => {
      if (history.open && currentView.cursor === null && !more.disabled) more.click();
    });
    views.set(key, view);
  }
  view.item = item;
  if (Number(item.leaStatus?.sequence || 0) > Number(view.sequence || 0)) view.more.hidden = false;
  view.sequence = item.leaStatus?.sequence || 0;
  view.history.hidden = !item.formalizationId;
  const status = normalizeLeaStatus(item.leaStatus);
  const wait = dependencyWait(item);
  const active = ["running", "pending"].includes(status.activity.status);
  if (status.attention !== view.attention && status.attention !== "none") {
    view.announcement.textContent = `${leaStatusLabel(status)}. ${status.assessment?.summary || ""}`;
  }
  view.attention = status.attention;
  view.summary.textContent = wait ? `${wait.message}${!status.assessment ? " Assessment pending while supporting results are resolved." : ""}` : status.assessment?.summary || (active
    ? "Starting formalization; waiting for Lea's first assessment."
    : "No assessment published. Start or resume formalization to receive Lea Status updates.");
  const stamp = status.latest_update?.created_at;
  const age = stamp ? Math.max(0, Math.floor((Date.now() - new Date(stamp).getTime()) / 1000)) : null;
  view.meta.textContent = [leaStatusLabel(status), age !== null ? `Updated ${age < 60 ? `${age} seconds` : `${Math.floor(age / 60)} minutes`} ago` : "",
    status.assessment ? `Scope: ${friendly(status.assessment.scope)}` : "",
    wait ? "" : friendly(status.activity.stop_reason), status.freshness_detail, status.deliveryError].filter(Boolean).join(" · ");
  view.pauseButton.hidden = (!active && !wait?.autoResume) || !status.run_id;
  view.pauseButton.textContent = wait ? "Stop waiting" : "Pause";
  if (!active) { view.pauseButton.disabled = false; view.pauseButton.textContent = wait ? "Stop waiting" : "Pause"; }
  const version = status.latest_update?.id || "empty";
  const selection = document.getSelection?.();
  if (view.version !== version && !(selection?.anchorNode && view.body.contains(selection.anchorNode) && !selection.isCollapsed)) {
    view.body.replaceChildren();
    renderAssessment(node, view.body, status.assessment);
    view.version = version;
  }
  // Lifecycle diagnostics do not imply a model assessment exists, and stay out
  // of the primary operational explanation while a dependency is being resolved.
  if (!view.reporting) view.reporting = node("p", "", view.details);
  view.reporting.textContent = status.reporting_incomplete
    ? status.assessment ? "This execution segment ended without a final assessment; its last published update is retained."
      : "No assessment was published for this execution segment." : "";
  view.reporting.hidden = !status.reporting_incomplete;
  return view.root;
}

function renderAssessment(node, body, assessment) {
  if (!assessment) return;
  node("p", assessment.confidence_reason || "", body);
  const correspondence = assessment.correspondence;
  if (correspondence) {
    node("strong", "Assessed scope", body);
    node("p", correspondence.scope_summary, body);
    if (correspondence.omitted_source_parts?.length) {
      node("strong", "Source parts not formalized", body);
      const list = node("ul", undefined, body);
      for (const part of correspondence.omitted_source_parts) node("li", part, list);
    }
    node("strong", `Document context: ${friendly(correspondence.context_status)}`, body);
    node("p", correspondence.context_summary, body);
    for (const evidence of correspondence.context_evidence || []) {
      node("blockquote", [evidence.path, evidence.start_line ? `line ${evidence.start_line}` : "", evidence.excerpt].filter(Boolean).join(" · "), body);
    }
  }
  if (assessment.current_work) node("p", `Current work: ${assessment.current_work}`, body);
  for (const finding of assessment.findings || []) {
    const section = node("section", undefined, body);
    section.className = `ol-lean-lea-check-finding ol-lean-lea-check-finding-${finding.severity}`;
    node("strong", finding.title, section);
    node("p", finding.detail, section);
    if (finding.correction) node("p", `Lean correction (${friendly(finding.lean_resolution)}): ${finding.correction}`, section);
    if (finding.source_resolution) node("p", `Source issue: ${friendly(finding.source_resolution)}`, section);
    if (finding.resolution_explanation) node("p", finding.resolution_explanation, section);
    for (const evidence of finding.evidence || []) {
      node("blockquote", [evidence.path, evidence.start_line ? `line ${evidence.start_line}` : "", evidence.excerpt].filter(Boolean).join(" · "), section);
    }
  }
  for (const [key, title] of [["matches", "What matched"], ["remaining_obligations", "Remaining obligations"], ["caveats", "Caveats"], ["limitations", "Limitations"]]) {
    if (!assessment[key]?.length) continue;
    node("strong", title, body);
    const list = node("ul", undefined, body);
    for (const value of assessment[key]) node("li", typeof value === "string" ? value : value.summary, list);
  }
  if (assessment.recommended_next_action) node("p", typeof assessment.recommended_next_action === "string"
    ? assessment.recommended_next_action : assessment.recommended_next_action.detail, body);
}
