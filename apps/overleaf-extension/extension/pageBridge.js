import { parseTargetDocument } from "./targetParserCore.mjs";

(function () {
  let activeView = null;
  const NAVIGATE_POLL_INTERVAL_MS = 120;
  const NAVIGATE_POLL_ATTEMPTS = 25; // ~3s for a freshly-opened doc to load

  window.addEventListener("UNSTABLE_editor:extensions", (event) => {
    const detail = event.detail || {};
    const CodeMirror = detail.CodeMirror || window.CodeMirror;
    const extensions = detail.extensions;

    if (!CodeMirror || !Array.isArray(extensions)) {
      return;
    }

    const ViewPlugin = CodeMirror.ViewPlugin;
    const Decoration = CodeMirror.Decoration;
    if (!ViewPlugin || !Decoration) {
      return;
    }

    const targetMark = Decoration.mark({ class: "ol-lean-theorem" });

    function buildDecorations(view) {
      const builder = [];
      const source = view.state.doc.toString();
      const documentResult = parseTargetDocument(source, { sourcePath: getActiveDocPath() });
      for (const target of [...documentResult.targets, ...documentResult.diagnostics]) {
        builder.push(targetMark.range(target.from, target.to));
      }
      return Decoration.set(builder, true);
    }

    const targetPlugin = ViewPlugin.fromClass(
      class {
        constructor(view) {
          activeView = view;
          this.decorations = buildDecorations(view);
          publishTargets(view);
        }

        update(update) {
          activeView = update.view;
          if (update.docChanged || update.viewportChanged) {
            this.decorations = buildDecorations(update.view);
            publishTargets(update.view);
          }
        }
      },
      {
        decorations: (plugin) => plugin.decorations,
        eventHandlers: {
          click(event, view) {
            const coords = { x: event.clientX, y: event.clientY };
            const pos = view.posAtCoords(coords);
            if (typeof pos !== "number") return false;

            const target = findTargetAtPosition(view.state.doc.toString(), pos);
            if (!target) return false;

            event.preventDefault();
            event.stopPropagation();
            window.postMessage({
              type: target.syntax === "diagnostic" ? "OL_LEAN_DIAGNOSTIC_CLICK" : "OL_LEAN_TARGET_CLICK",
              clientX: event.clientX,
              clientY: event.clientY,
              target,
              diagnostic: target
            }, "*");
            return true;
          }
        }
      }
    );

    extensions.push(targetPlugin);
    // Tell the content script the integration is alive (editor-hook watchdog,
    // PLAN-system-hardening 0.4): Overleaf's UNSTABLE_ event fired AND the
    // CodeMirror plugin is installed. Posted only after the push above so a
    // partial hook (event fired, plugin rejected) still trips the watchdog.
    window.postMessage({ type: "OL_LEAN_EDITOR_HOOKED" }, "*");
  });

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.data?.type === "OL_LEAN_REQUEST_TARGETS") {
      if (activeView) publishTargets(activeView);
      return;
    }
    if (event.data?.type === "OL_LEAN_NAVIGATE") {
      navigateToSource(event.data);
      return;
    }
  });

  // Item 11: jump the editor to a source block. Same-file navigation may fall back
  // to byte offsets, but cross-file navigation only trusts marker/label anchors
  // until Overleaf confirms the target file is active.
  function navigateToSource(message) {
    const activePath = getActiveDocPath();
    const sourceFile = normalizeDocPath(message?.sourceFile);
    const inActiveFile = sourceFile ? sameDocPath(activePath, sourceFile) : true;

    if (inActiveFile) {
      const ok = selectTargetInActiveView(message, { allowOffsets: true });
      postNavigateResult(ok, ok ? "" : "not_found", message?.sourceFile);
      return;
    }

    if (!activePath && selectTargetInActiveView(message, { allowOffsets: false })) {
      postNavigateResult(true, "", message?.sourceFile);
      return;
    }

    // A different, known file: open it through Overleaf's IDE API when available,
    // or through the current editor-tab/file-tree UI. Modern Overleaf no longer
    // exposes `window._ide`, so the DOM path is the normal route there. Then wait
    // for that doc to become active and select the block precisely.
    if (openDocByPath(message?.sourceFile, message)) {
      waitForActiveDoc(message?.sourceFile, message, NAVIGATE_POLL_ATTEMPTS);
      return;
    }

    // Couldn't switch files (private API changed/unavailable): only navigate in the
    // current view if the item's anchor text is actually present, so we never select
    // an unrelated range from a different file, and tell the UI it failed.
    const ok = selectTargetInActiveView(message, { allowOffsets: false });
    postNavigateResult(ok, ok ? "" : "open_failed", message?.sourceFile);
  }

  // After opening a different file, the new CodeMirror document loads asynchronously.
  // Poll (bounded) until the target doc is active (or its anchor text is visible),
  // then select; report failure if it never arrives.
  function waitForActiveDoc(targetPath, message, attempts) {
    const activePath = getActiveDocPath();
    const source = activeView ? activeView.state.doc.toString() : "";
    const anchorIndex = findAnchorIndex(source, message);
    const hasAnchor = Boolean(
      (message?.labelSource !== "generated" && String(message?.leanLabel || "").trim())
      || String(message?.latexLabel || "").trim()
    );
    // A selected editor tab can update just before CodeMirror hands the bridge its
    // new view. When an anchor is available, require it as proof that `activeView`
    // belongs to the selected target file before applying that file's offsets.
    const ready = activePath && sameDocPath(activePath, targetPath)
      ? (!hasAnchor || anchorIndex >= 0)
      : anchorIndex >= 0;

    if (ready) {
      const ok = selectTargetInActiveView(message, { allowOffsets: true });
      postNavigateResult(ok, ok ? "" : "not_found", targetPath);
      return;
    }
    if (attempts <= 0) {
      postNavigateResult(false, "open_timeout", targetPath);
      return;
    }
    // Opening a nested path through the file tree may require expanding one folder
    // per render. Retrying also handles the short interval before a newly selected
    // editor tab is mounted.
    openDocByPath(targetPath, message);
    window.setTimeout(() => waitForActiveDoc(targetPath, message, attempts - 1), NAVIGATE_POLL_INTERVAL_MS);
  }

  function postNavigateResult(ok, reason, sourceFile) {
    window.postMessage({
      type: "OL_LEAN_NAVIGATE_RESULT",
      ok: Boolean(ok),
      reason: reason || "",
      sourceFile: sourceFile || ""
    }, "*");
  }

  function selectTargetInActiveView(message, { allowOffsets }) {
    const view = activeView;
    if (!view) return false;
    const source = view.state.doc.toString();

    let from = findAnchorIndex(source, message);
    let to = from;
    if (from < 0) {
      if (!allowOffsets) return false;
      const offsetFrom = Number(message?.from);
      if (!Number.isFinite(offsetFrom)) return false;
      from = offsetFrom;
      const offsetTo = Number(message?.to);
      to = Number.isFinite(offsetTo) ? offsetTo : offsetFrom;
    }

    const docLength = view.state.doc.length;
    const anchor = Math.max(0, Math.min(from, docLength));
    const head = Math.max(anchor, Math.min(to, docLength));
    view.dispatch({ selection: { anchor, head }, scrollIntoView: true });
    if (typeof view.focus === "function") view.focus();
    return true;
  }

  // Locate the item's block by its marker text. Prefers the Lea marker
  // (`% lea: … label=<name>`), then the LaTeX `\label{…}`. Returns -1 if neither is
  // found (e.g. the item lives in a different file than the one open).
  function findAnchorIndex(source, message) {
    const text = String(source || "");
    const leanLabel = String(message?.leanLabel || "").trim();
    if (leanLabel && message?.labelSource !== "generated") {
      const markerRe = new RegExp(
        `%[ \\t]*lea:[^\\n]*\\blabel[ \\t]*=[ \\t]*\\{?${escapeRegExp(leanLabel)}\\b`,
        "i"
      );
      const match = markerRe.exec(text);
      if (match) return match.index;
    }
    const latexLabel = String(message?.latexLabel || "").trim();
    if (latexLabel) {
      const index = text.indexOf(`\\label{${latexLabel}}`);
      if (index >= 0) return index;
    }
    return -1;
  }

  function escapeRegExp(value) {
    return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function sameDocPath(a, b) {
    const left = normalizeDocPath(a);
    return left !== "" && left === normalizeDocPath(b);
  }

  function normalizeDocPath(value) {
    return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").trim();
  }

  // Open a project file by path. Older Overleaf builds expose a private IDE API;
  // current builds do not, so fall back to the rendered editor tabs/file tree.
  // Returns true if an open/expand action was made.
  function openDocByPath(targetPath, message) {
    const wanted = normalizeDocPath(targetPath);
    if (!wanted) return false;
    if (openDocWithIde(wanted, message)) return true;
    return openDocFromUi(wanted);
  }

  function openDocWithIde(wanted, message) {
    try {
      const ide = window._ide;
      const ft = ide && ide.fileTreeManager;
      const em = ide && ide.editorManager;
      if (!ft || !em) return false;

      const entity = resolveEntityByPath(ft, wanted);
      if (!entity) return false;

      const line = Number(message?.line);
      const options = Number.isFinite(line) && line > 0 ? { gotoLine: line } : {};
      const id = entity._id || entity.id;
      if (id && typeof em.openDocId === "function" && tryOpenDoc(() => em.openDocId(id, options))) return true;
      if (typeof em.openDoc === "function" && tryOpenDoc(() => em.openDoc(entity, options))) return true;
      if (id && typeof em.openDoc === "function" && tryOpenDoc(() => em.openDoc(id, options))) return true;
      return typeof em.openEntity === "function" && tryOpenDoc(() => em.openEntity(entity, options));
    } catch {
      return false;
    }
  }

  function openDocFromUi(wanted) {
    const doc = window.document;
    if (!doc || typeof doc.querySelectorAll !== "function") return false;

    // Prefer an already-open editor tab. Its visible path is complete even for
    // nested files, and selecting it does not depend on the file-tree panel being
    // open or on ancestor folders being expanded.
    const tabs = queryAll(doc, '.editor-file-tab[role="tab"], [role="tab"][data-tab-id]');
    let matchingTab = tabs.find((tab) => sameDocPath(editorTabPath(tab), wanted));
    if (!matchingTab) {
      const basename = wanted.split("/").pop() || "";
      const basenameTabs = tabs.filter((tab) => editorTabPath(tab).split("/").pop() === basename);
      if (basenameTabs.length === 1) matchingTab = basenameTabs[0];
    }
    if (matchingTab && clickElement(matchingTab)) return true;

    const treeItems = queryAll(doc, '[data-testid="file-tree-list-root"] [role="treeitem"], .file-tree [role="treeitem"]');
    const docItems = treeItems.filter(isDocumentTreeItem);
    const exactItem = docItems.find((item) => sameDocPath(fileTreeItemPath(item), wanted));
    if (exactItem && clickElement(treeItemClickTarget(exactItem))) return true;

    // Some Overleaf builds expose only the basename on root-level tree items.
    // Use that only when it is unique, so duplicate filenames in different
    // directories can never open the wrong source.
    const basename = wanted.split("/").pop() || "";
    const basenameMatches = docItems.filter((item) => normalizeUiPath(treeItemName(item)) === basename);
    if (basenameMatches.length === 1 && clickElement(treeItemClickTarget(basenameMatches[0]))) return true;

    // A nested target may not be rendered until each ancestor folder is expanded.
    // Expand the shallowest matching collapsed ancestor; waitForActiveDoc retries
    // after React renders its children.
    const folderItems = treeItems
      .filter((item) => !isDocumentTreeItem(item))
      .map((item) => ({ item, path: fileTreeItemPath(item) }))
      .filter(({ path }) => path && wanted.startsWith(`${path}/`))
      .sort((a, b) => a.path.split("/").length - b.path.split("/").length);
    const collapsedFolder = folderItems.find(({ item }) => item.getAttribute?.("aria-expanded") !== "true");
    if (collapsedFolder && clickElement(treeItemClickTarget(collapsedFolder.item))) return true;

    // If another sidebar pane is selected, expose the file tree and retry on the
    // next poll. This is intentionally last so an open editor tab remains enough.
    const fileTreeTabs = queryAll(doc, '#ide-rail-tabs-tab-file-tree');
    const fileTreeTab = fileTreeTabs[0];
    if (fileTreeTab && fileTreeTab.getAttribute?.("aria-selected") !== "true") {
      return clickElement(fileTreeTab);
    }
    return false;
  }

  function queryAll(root, selector) {
    try {
      return Array.from(root.querySelectorAll(selector) || []);
    } catch {
      return [];
    }
  }

  function clickElement(element) {
    try {
      if (!element || typeof element.click !== "function") return false;
      element.click();
      return true;
    } catch {
      return false;
    }
  }

  function editorTabPath(tab) {
    const pathNode = typeof tab?.querySelector === "function" ? tab.querySelector(".editor-file-tab-path") : null;
    return normalizeUiPath(pathNode?.textContent || tab?.getAttribute?.("aria-label") || "");
  }

  function treeItemClickTarget(item) {
    if (typeof item?.querySelector !== "function") return item;
    return item.querySelector(".entity-name") || item.querySelector(".entity") || item;
  }

  function isDocumentTreeItem(item) {
    if (typeof item?.querySelector === "function") {
      const entity = item.querySelector('[data-file-type="doc"]');
      if (entity) return true;
      const folder = item.querySelector('[data-file-type="folder"]');
      if (folder) return false;
    }
    return /\.tex$/i.test(treeItemName(item));
  }

  function treeItemName(item) {
    return String(item?.getAttribute?.("aria-label") || "").trim();
  }

  function fileTreeItemPath(item) {
    const parts = [];
    let current = item;
    while (current) {
      if (current.getAttribute?.("role") === "treeitem") {
        const name = normalizeUiPath(treeItemName(current));
        if (name) parts.unshift(name);
      }
      current = current.parentElement || null;
    }
    return normalizeDocPath(parts.join("/"));
  }

  function normalizeUiPath(value) {
    // Overleaf prefixes editor-tab paths with a left-to-right mark and may add
    // other invisible directionality controls around filenames.
    return normalizeDocPath(String(value || "").replace(/[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/gi, ""));
  }

  function tryOpenDoc(open) {
    try {
      open();
      return true;
    } catch {
      return false;
    }
  }

  // Resolve a file-tree entity from a path, tolerating the different return shapes
  // Overleaf's findEntityByPath has used (entity directly, or { entity, ... }) and
  // an optional leading slash.
  function resolveEntityByPath(ft, wanted) {
    if (typeof ft.findEntityByPath === "function") {
      for (const candidate of [wanted, `/${wanted}`]) {
        try {
          const found = ft.findEntityByPath(candidate);
          const entity = found && (found.entity || found);
          if (isDocEntity(entity)) return entity;
        } catch {
          // try the next candidate form
        }
      }
    }
    return findEntityInTree(ft, wanted);
  }

  function isDocEntity(entity) {
    return Boolean(entity && (entity._id || entity.id || entity.name || entity.path || entity.pathname || entity.fileRef));
  }

  function findEntityInTree(ft, wanted) {
    const roots = [
      ft.root,
      ft.rootFolder,
      ft.fileTree,
      ft.tree,
      ft.docs,
      ft.entities,
      typeof ft.getRootFolder === "function" ? safeCall(() => ft.getRootFolder()) : null,
      typeof ft.getFileTree === "function" ? safeCall(() => ft.getFileTree()) : null,
      typeof ft.getAllEntities === "function" ? safeCall(() => ft.getAllEntities()) : null
    ].filter(Boolean);
    const seen = new Set();
    for (const root of roots) {
      const found = walkEntityTree(root, wanted, "", seen);
      if (found) return found;
    }
    return null;
  }

  function walkEntityTree(node, wanted, parentPath, seen) {
    if (!node || typeof node !== "object") return null;
    if (seen.has(node)) return null;
    seen.add(node);

    if (Array.isArray(node)) {
      for (const child of node) {
        const found = walkEntityTree(child, wanted, parentPath, seen);
        if (found) return found;
      }
      return null;
    }

    const entity = node.entity || node;
    const entityPath = normalizeEntityPath(entity, parentPath);
    if (entityPath && sameDocPath(entityPath, wanted) && isDocEntity(entity)) {
      return entity;
    }

    const childParentPath = entityPath || parentPath;
    const children = [
      entity.children,
      entity.folders,
      entity.docs,
      entity.fileRefs,
      entity.entries,
      entity.entities
    ].filter(Boolean);
    for (const childSet of children) {
      const found = walkEntityTree(childSet, wanted, childParentPath, seen);
      if (found) return found;
    }
    if (!isDocEntity(entity)) {
      for (const value of Object.values(entity)) {
        const found = walkEntityTree(value, wanted, parentPath, seen);
        if (found) return found;
      }
    }
    return null;
  }

  function normalizeEntityPath(entity, parentPath) {
    const direct = entity.path || entity.pathname || entity.filePath || entity.fullPath;
    if (direct) return normalizeDocPath(direct);
    const name = entity.name || entity._name || entity.filename;
    if (!name) return "";
    const joined = parentPath ? `${normalizeDocPath(parentPath)}/${name}` : name;
    return normalizeDocPath(joined);
  }

  function safeCall(fn) {
    try {
      return fn();
    } catch {
      return null;
    }
  }

  window.setInterval(() => {
    if (activeView) publishTargets(activeView);
  }, 1500);

  function getActiveDocPath() {
    try {
      const ide = window._ide;
      const ft = ide && ide.fileTreeManager;
      const em = ide && ide.editorManager;
      const docId = typeof em?.getCurrentDocId === "function" ? em.getCurrentDocId() : null;
      if (docId && ft) {
        const entity = ft.findEntityById ? ft.findEntityById(docId) : null;
        if (entity) {
          const path = ft.getEntityPath ? ft.getEntityPath(entity) : entity.path;
          if (path) return path;
        }
      }
    } catch {
      // Fall through to the current Overleaf UI below.
    }
    const doc = window.document;
    if (!doc || typeof doc.querySelectorAll !== "function") return "";
    const selectedTabs = queryAll(doc, '.editor-file-tab[role="tab"][aria-selected="true"], [role="tab"][data-tab-id][aria-selected="true"]');
    return selectedTabs.length > 0 ? editorTabPath(selectedTabs[0]) : "";
  }

  function publishTargets(view) {
    const source = view.state.doc.toString();
    const documentResult = parseTargetDocument(source, { sourcePath: getActiveDocPath() });
    window.postMessage({
      type: "OL_LEAN_TARGETS_VISIBLE",
      activeTex: source,
      activePath: getActiveDocPath(),
      targets: documentResult.targets.map((target) => withCoords(view, target)).filter(hasCoords),
      diagnostics: documentResult.diagnostics.map((diagnostic) => withCoords(view, diagnostic)).filter(hasCoords)
    }, "*");
  }

  function findTargetAtPosition(source, position) {
    const documentResult = parseTargetDocument(source, { sourcePath: getActiveDocPath() });
    return [...documentResult.targets, ...documentResult.diagnostics]
      .find((target) => target.from <= position && position <= target.to);
  }

  function withCoords(view, target) {
    const sourceFile = getActiveDocPath();
    const docLength = Number.isFinite(view.state.doc.length)
      ? view.state.doc.length
      : view.state.doc.toString().length;
    const sourceStartLine = documentLineAt(view.state.doc, Math.max(0, target.from || 0));
    const sourceEndLine = documentLineAt(
      view.state.doc,
      Math.max(0, Math.min(target.to || target.from || 0, docLength))
    );
    return {
      targetKind: target.targetKind,
      targetLabel: target.targetLabel,
      labelSource: target.labelSource,
      displayTitle: target.displayTitle,
      targetText: target.targetText,
      targetUses: target.targetUses,
      targetContext: target.targetContext,
      latexEnvironment: target.latexEnvironment,
      latexLabel: target.latexLabel,
      sourceHash: target.sourceHash,
      sourceFile,
      sourceStartLine,
      sourceEndLine,
      syntax: target.syntax,
      code: target.code,
      message: target.message,
      from: target.from,
      to: target.to,
      badgeFrom: target.badgeFrom,
      bodyFrom: target.bodyFrom,
      bodyTo: target.bodyTo,
      coords: getTargetCoords(view, target)
    };
  }

  function documentLineAt(doc, offset) {
    if (typeof doc?.lineAt === "function") return doc.lineAt(offset).number;
    return doc?.toString().slice(0, offset).split("\n").length || 1;
  }

  function hasCoords(target) {
    return Boolean(target.coords);
  }

  function getTargetCoords(view, target) {
    const positions = [
      target.badgeFrom,
      target.bodyFrom,
      target.from,
      Math.max(target.from, target.to - 1)
    ].filter((position) => typeof position === "number");
    let coords = null;
    for (const position of positions) {
      coords = view.coordsAtPos(position);
      if (coords) break;
    }
    if (!coords) {
      return null;
    }
    if (!isUsableCoords(view, coords)) {
      return null;
    }
    return {
      left: Math.max(coords.right, coords.left),
      top: coords.top,
      bottom: coords.bottom
    };
  }

  function isUsableCoords(view, coords) {
    if (![coords.left, coords.right, coords.top, coords.bottom].every(Number.isFinite)) {
      return false;
    }

    const viewportHeight = Number.isFinite(window.innerHeight) ? window.innerHeight : null;
    if (viewportHeight !== null && (coords.bottom < 0 || coords.top > viewportHeight)) {
      return false;
    }

    const editorRect = view.scrollDOM?.getBoundingClientRect?.() || view.dom?.getBoundingClientRect?.();
    if (editorRect && (coords.bottom < editorRect.top || coords.top > editorRect.bottom)) {
      return false;
    }

    return true;
  }
})();
