import { inspectTagPreamble, normalizeSetupPath, STYLE_PATH, EDITOR_TIMEOUT_MS, setupError } from "./leaTagsSetup.mjs";

// All document writes use Overleaf's own CodeMirror view/collaborative pipeline.
// The unstable store is exposed by Overleaf for external editor integrations.
export function installLeaTagsEditorBridge({ window: win, getView, openDocByPath, getActiveDocPath, timeoutMs = EDITOR_TIMEOUT_MS }) {
  let session = null;
  const viewDocuments = new WeakMap();
  const doc = win.document;
  const projectId = () => win.location?.pathname?.match(/^\/project\/([^/]+)/)?.[1] || "";
  const selectedTab = () => doc?.querySelector?.('.editor-file-tab[aria-selected="true"][data-tab-id], [role="tab"][aria-selected="true"][data-tab-id]');
  function snapshot() {
    const view = getView();
    const store = win.overleaf?.unstable?.store;
    const id = store?.get?.("editor.open_doc_id");
    const exposedView = store?.get?.("editor.view");
    const tab = selectedTab();
    const path = normalizeSetupPath(getActiveDocPath());
    if (!view || exposedView !== view || !id || !path || (tab && tab.getAttribute("data-tab-id") !== String(id))) {
      throw setupError("The active Overleaf editor's document identity could not be confirmed. Reload in Code mode or finish setup manually.", "editor_identity");
    }
    // Overleaf reuses views across files and publishes the ID separately. Bind
    // each immutable document buffer within its view, so changing the selected
    // ID alone cannot authorize writing into the previous file's buffer.
    let buffers = viewDocuments.get(view);
    if (!buffers) { buffers = new WeakMap(); viewDocuments.set(view, buffers); }
    const boundId = buffers.get(view.state.doc);
    if (boundId && boundId !== String(id)) {
      throw setupError("Overleaf is still switching editor documents. Finish setup manually if it does not settle.", "editor_identity");
    }
    buffers.set(view.state.doc, String(id));
    return { view, document: { id: String(id), path, type: "doc" }, source: view.state.doc.toString() };
  }
  function assertSession(message) {
    if (!session || session.operationId !== message.operationId || session.projectId !== message.projectId || projectId() !== message.projectId || session.cancelled) {
      throw setupError("Setup stopped because the project or active file changed.", "cancelled");
    }
    if (!session.opening && session.expectedDocument) {
      const selectedId = selectedTab()?.getAttribute("data-tab-id") || win.overleaf?.unstable?.store?.get?.("editor.open_doc_id");
      if (selectedId && String(selectedId) !== session.expectedDocument.id) {
        cancel();
        throw setupError("Setup stopped because you changed files.", "cancelled");
      }
    }
  }
  function cancel() {
    if (!session || session.cancelled) return;
    session.cancelled = true;
    win.postMessage({ type: "OL_LEAN_TAGS_SETUP_CANCELLED", operationId: session.operationId, projectId: session.projectId }, "*");
  }
  doc?.addEventListener?.("click", event => {
    // Our navigation uses .click(), whose events are untrusted. A real file/tab
    // click always wins over the ongoing setup, including during editor loading.
    if (event.isTrusted && event.target?.closest?.('[data-file-id], .editor-file-tab, [role="tab"][data-tab-id]')) cancel();
  }, true);
  win.addEventListener("pagehide", cancel);
  const poll = ms => new Promise(resolve => win.setTimeout(resolve, ms));
  async function open(message) {
    assertSession(message);
    const target = message.document;
    if (!target?.id || !normalizeSetupPath(target.path)) throw setupError("The target document could not be identified.", "editor_identity");
    session.opening = true;
    try {
      const deadline = Date.now() + timeoutMs;
      do {
        assertSession(message);
        try {
          const current = snapshot();
          if (current.document.id === target.id && current.document.path === target.path) {
            session.expectedDocument = current.document;
            return current;
          }
        } catch (error) { if (error.code !== "editor_identity") throw error; }
        openDocByPath(target.path, {});
        await poll(120);
      } while (Date.now() < deadline);
      throw setupError("Overleaf did not open " + target.path + " within 10 seconds. Open it in Code mode and retry, or finish setup manually.", "editor_timeout");
    } finally { if (session) session.opening = false; }
  }
  function writable(current) {
    if (current.view.state.readOnly || current.view.contentDOM?.getAttribute?.("contenteditable") === "false") {
      throw setupError("This Overleaf document is read-only. You need permission to edit it.", "permission");
    }
  }
  async function context(message) {
    assertSession(message);
    // Modern Overleaf omits the full project meta tag. Its root-document select
    // exposes document IDs and full paths without changing any project setting.
    let select = doc.querySelector("#rootDocId");
    let opened = false;
    let dialog = null;
    try {
      if (!select) {
        const settings = Array.from(doc.querySelectorAll("#ide-root button")).find(button => button.getAttribute("aria-label") === "Settings" || button.textContent.trim() === "Settings");
        if (!settings) return { main: null };
        settings.click();
        opened = true;
        const deadline = Date.now() + timeoutMs;
        do {
          assertSession(message);
          select = doc.querySelector("#rootDocId");
          dialog = doc.querySelector('[role="dialog"]');
          if (select) break;
          const compiler = doc.querySelector("#ide-settings-tabs-tab-compiler");
          compiler?.click();
          await poll(120);
        } while (Date.now() < deadline);
      }
      assertSession(message);
      const option = Array.from(select?.options || []).find(option => option.value === select.value);
      const path = normalizeSetupPath(option?.textContent?.trim());
      return { main: select?.value && path ? { id: select.value, path, type: "doc" } : null };
    } finally {
      if (opened) {
        dialog ||= doc.querySelector('[role="dialog"]');
        const close = dialog && Array.from(dialog.querySelectorAll("button")).find(button => button.getAttribute("aria-label") === "Close dialog" || button.textContent.trim() === "Close");
        close?.click();
      }
    }
  }
  async function handle(message) {
    if (message.action === "begin") {
      if (projectId() !== message.projectId) throw setupError("The Overleaf project changed.", "cancelled");
      if (session && !session.cancelled) throw setupError("A setup operation is already running.", "busy");
      const original = snapshot();
      session = {
        operationId: message.operationId, projectId: message.projectId,
        original: original.document, selection: original.view.state.selection?.main,
        expectedDocument: original.document, opening: false, cancelled: false
      };
      return { document: original.document };
    }
    assertSession(message);
    if (message.action === "cancel") { cancel(); return {}; }
    if (message.action === "finish") { session = null; return {}; }
    if (message.action === "context") return context(message);
    if (message.action === "peek") {
      const current = snapshot();
      return { document: current.document, source: current.source };
    }
    if (message.action === "restore") {
      const current = snapshot();
      if (current.document.id !== session.expectedDocument?.id) { cancel(); return {}; }
      const original = session.original;
      const selection = session.selection;
      const restored = await open({ ...message, document: original });
      if (selection) {
        const length = restored.view.state.doc.length;
        restored.view.dispatch({ selection: { anchor: Math.min(selection.anchor, length), head: Math.min(selection.head, length) }, scrollIntoView: true });
      }
      return {};
    }
    if (!["read", "populate", "enable"].includes(message.action)) throw setupError("Unsupported setup operation.", "invalid_request");
    const current = await open(message);
    assertSession(message);
    // Re-read immediately before a synchronous mutation; navigation and remote
    // edits during the asynchronous open must not invalidate our assumptions.
    const fresh = snapshot();
    if (fresh.view !== current.view || fresh.document.id !== message.document.id || fresh.document.path !== message.document.path) throw setupError("The active document changed.", "cancelled");
    if (message.action === "read") return { document: fresh.document, source: fresh.source };
    writable(fresh);
    if (message.action === "populate") {
      if (fresh.document.path !== STYLE_PATH || fresh.source !== "" || typeof message.styleText !== "string" || message.styleText.length > 100000 || !message.styleText.includes("\\ProvidesPackage{lea-tags}")) {
        throw setupError("The new style document is no longer empty or could not be confirmed. It was left unchanged.", "style_changed");
      }
      // The creation response must have registered this exact ID in the content
      // script; we never populate a file obtained from an inventory lookup.
      fresh.view.dispatch({ changes: { from: 0, insert: message.styleText } });
      return { changed: true };
    }
    const preamble = inspectTagPreamble(fresh.source);
    if (preamble.kind !== "insert") return { ...preamble, changed: false };
    fresh.view.dispatch({ changes: { from: preamble.offset, insert: preamble.insert } });
    if (session.original.id === fresh.document.id && session.selection) {
      const map = position => position < preamble.offset ? position : position + preamble.insert.length;
      session.selection = { anchor: map(session.selection.anchor), head: map(session.selection.head) };
    }
    return { kind: "loaded", changed: true };
  }
  win.addEventListener("message", event => {
    const message = event.data;
    if (event.source !== win || message?.type !== "OL_LEAN_TAGS_SETUP_REQUEST") return;
    if (!message.operationId || !message.requestId || !message.projectId) return;
    Promise.resolve().then(() => handle(message)).then(
      result => win.postMessage({ type: "OL_LEAN_TAGS_SETUP_RESULT", operationId: message.operationId, requestId: message.requestId, projectId: message.projectId, ok: true, result }, "*"),
      error => win.postMessage({ type: "OL_LEAN_TAGS_SETUP_RESULT", operationId: message.operationId, requestId: message.requestId, projectId: message.projectId, ok: false, error: { code: error.code || "editor", message: error.message } }, "*")
    );
  });
  win.setInterval(() => {
    if (!session || session.cancelled || session.opening) return;
    if (projectId() !== session.projectId) { cancel(); return; }
    try {
      const current = snapshot();
      if (current.document.id !== session.expectedDocument?.id) cancel();
    } catch { /* An editor may briefly unmount; every mutation checks identity. */ }
  }, 200);
}
