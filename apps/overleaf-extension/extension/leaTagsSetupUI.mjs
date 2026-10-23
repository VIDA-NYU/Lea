import { createOverleafSetupClient, installLeaTags, PACKAGE_LINE, EDITOR_TIMEOUT_MS, setupError } from "./leaTagsSetup.mjs";

export function mountLeaTagsSetup({ slot, window: win, document: doc, chrome: extension, getProjectId, onChanged = () => {}, makeClient = createOverleafSetupClient, runSetup = installLeaTags }) {
  let operation = null;
  let disposed = false;
  let styleText = "";
  const disclosure = doc.createElement("details");
  disclosure.className = "ol-lean-tags-setup-disclosure";
  disclosure.open = true;
  const summary = doc.createElement("summary");
  summary.className = "ol-lean-tags-setup-summary";
  summary.textContent = "Lea tags setup";
  const button = doc.createElement("button");
  button.type = "button";
  button.className = "ol-lean-pane-action";
  button.textContent = "Add Lea tags";
  const hint = doc.createElement("p");
  hint.className = "ol-lean-tags-setup-hint";
  hint.textContent = "Create the style file and load it in your main document.";
  const status = doc.createElement("p");
  status.className = "ol-lean-tags-setup-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.setAttribute("aria-atomic", "true");
  const fallback = doc.createElement("div");
  fallback.className = "ol-lean-tags-setup-fallback";
  fallback.hidden = true;
  const download = doc.createElement("button");
  download.type = "button";
  download.className = "ol-lean-pane-action";
  download.textContent = "Download style";
  const copy = doc.createElement("button");
  copy.type = "button";
  copy.className = "ol-lean-pane-action";
  copy.textContent = "Copy package line";
  const code = doc.createElement("code");
  code.textContent = PACKAGE_LINE;
  fallback.append(download, copy, code);
  slot.className = "ol-lean-tags-setup";
  disclosure.append(summary, button, hint, status, fallback);
  slot.append(disclosure);

  const setStatus = (message, error = false) => {
    if (disposed) return;
    status.textContent = message;
    status.classList.toggle("is-error", error);
  };
  function loadStyle() {
    if (styleText) return Promise.resolve(styleText);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(setupError("The extension did not return the bundled style. Reload the extension and retry.", "asset")), EDITOR_TIMEOUT_MS);
      try {
        extension.runtime.sendMessage({ type: "READ_LEA_TAGS_STYLE" }, response => {
          clearTimeout(timer);
          if (extension.runtime.lastError || !response?.ok || typeof response.text !== "string") {
            reject(setupError(response?.message || "The bundled style could not be loaded. Reload the extension and try again.", "asset"));
          } else { styleText = response.text; resolve(styleText); }
        });
      } catch (error) { clearTimeout(timer); reject(error); }
    });
  }
  download.addEventListener("click", async () => {
    download.disabled = true;
    try {
      const text = await loadStyle();
      if (disposed) return;
      const url = win.URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
      const link = doc.createElement("a");
      link.href = url;
      link.download = "lea-tags.sty";
      doc.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => win.URL.revokeObjectURL(url), 1000);
    } catch (error) { setStatus(error.message, true); }
    finally { download.disabled = false; }
  });
  copy.addEventListener("click", async () => {
    try {
      await win.navigator.clipboard.writeText(PACKAGE_LINE);
      setStatus("Package line copied. Paste it into your main document's preamble.");
    } catch { setStatus("Copy the displayed package line into your main document's preamble.", true); }
  });

  function bridgeRequest(current, action, payload = {}) {
    if (current.controller.signal.aborted || getProjectId() !== current.projectId) return Promise.reject(setupError("Setup cancelled because the project or active file changed.", "cancelled"));
    const requestId = current.id + ":" + (++current.sequence);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        current.pending.delete(requestId);
        reject(setupError("The Overleaf editor did not respond. Reload in Code mode or finish setup manually.", "editor_timeout"));
      }, EDITOR_TIMEOUT_MS + 500);
      current.pending.set(requestId, { resolve, reject, timer });
      win.postMessage({ type: "OL_LEAN_TAGS_SETUP_REQUEST", operationId: current.id, requestId, projectId: current.projectId, action, ...payload }, "*");
    });
  }
  const onMessage = event => {
    const data = event.data;
    const current = operation;
    if (event.source !== win || !current || data?.operationId !== current.id || data.projectId !== current.projectId) return;
    if (data.type === "OL_LEAN_TAGS_SETUP_CANCELLED") { current.controller.abort(); return; }
    if (data.type !== "OL_LEAN_TAGS_SETUP_RESULT") return;
    const request = current.pending.get(data.requestId);
    if (!request) return;
    clearTimeout(request.timer);
    current.pending.delete(data.requestId);
    if (getProjectId() !== current.projectId || current.controller.signal.aborted) {
      request.reject(setupError("The Overleaf project or active file changed.", "cancelled"));
    } else if (data.ok) request.resolve(data.result);
    else request.reject(setupError(data.error?.message || "The editor operation failed.", data.error?.code));
  };
  win.addEventListener("message", onMessage);
  button.addEventListener("click", async () => {
    if (operation || disposed) return;
    const current = {
      id: win.crypto.randomUUID(), projectId: getProjectId(), sequence: 0,
      controller: new AbortController(), pending: new Map()
    };
    operation = current;
    button.disabled = true;
    fallback.hidden = true;
    setStatus("Checking project");
    current.controller.signal.addEventListener("abort", () => {
      for (const pending of current.pending.values()) {
        clearTimeout(pending.timer);
        pending.reject(setupError("Setup stopped because you navigated away or changed files.", "cancelled"));
      }
      current.pending.clear();
      win.postMessage({ type: "OL_LEAN_TAGS_SETUP_REQUEST", operationId: current.id, requestId: current.id + ":cancel", projectId: current.projectId, action: "cancel" }, "*");
    }, { once: true });
    const bridge = (action, payload) => bridgeRequest(current, action, payload);
    let changed = false;
    let began = false;
    try {
      if (!current.projectId || current.projectId === "unknown") throw setupError("Open a writable Overleaf project to add Lea tags.", "project_metadata");
      const text = await loadStyle();
      await bridge("begin");
      began = true;
      const client = makeClient({
        projectId: current.projectId, signal: current.controller.signal,
        fetchImpl: win.fetch.bind(win), documentImpl: doc, bridge,
        isCurrent: () => !disposed && operation === current && getProjectId() === current.projectId
      });
      const result = await runSetup({ client, bridge, styleText: text, signal: current.controller.signal, onProgress: message => setStatus(message) });
      changed = result.changed;
      setStatus(result.message);
      fallback.hidden = !result.manual;
    } catch (error) {
      changed = Boolean(error.setupResult?.changed);
      setStatus(error.message, true);
      fallback.hidden = false;
    } finally {
      if (began && !current.controller.signal.aborted && getProjectId() === current.projectId) {
        try { await bridge("restore"); } catch { /* Never force the user back after independent navigation. */ }
        try { await bridge("finish"); } catch { /* The page may have gone away. */ }
      }
      for (const pending of current.pending.values()) { clearTimeout(pending.timer); pending.reject(setupError("Setup finished.", "cancelled")); }
      current.pending.clear();
      if (operation === current) operation = null;
      button.disabled = false;
      if (changed && !disposed && getProjectId() === current.projectId) onChanged();
    }
  });
  return {
    dispose() {
      disposed = true;
      operation?.controller.abort();
      win.removeEventListener("message", onMessage);
    }
  };
}
