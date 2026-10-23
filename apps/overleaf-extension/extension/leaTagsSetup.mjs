import { extractLatexSourcesFromZip } from "./zipTex.mjs";

export const STYLE_PATH = "lea-tags.sty";
export const PACKAGE_LINE = "\\usepackage{lea-tags}";
export const NETWORK_TIMEOUT_MS = 30000;
export const EDITOR_TIMEOUT_MS = 10000;

export function setupError(message, code = "setup_failed") {
  return Object.assign(new Error(message), { code });
}

export function normalizeStyle(text) {
  return String(text).replace(/\r\n?|\n/g, "\n");
}

export function normalizeSetupPath(path) {
  const value = String(path || "").replace(/[\u200e\u200f]/g, "").replace(/^\/+/, "");
  if (!value || value.split("/").some(part => !part || part === "." || part === "..") || value.includes("\\")) return "";
  return value;
}

// Preserve offsets while removing comments and opaque spans. This is deliberately
// a conservative preamble scanner, not a TeX interpreter.
function maskPreamble(source) {
  const chars = source.split("");
  const hide = (from, to) => {
    for (let i = from; i < to; i++) if (chars[i] !== "\n" && chars[i] !== "\r") chars[i] = " ";
  };
  for (let i = 0; i < source.length; i++) {
    if (source[i] === "\\") { i++; continue; }
    if (source[i] === "%") {
      const end = source.indexOf("\n", i);
      hide(i, end < 0 ? source.length : end);
      i = end < 0 ? source.length : end;
    }
  }
  let text = chars.join("");
  const opaque = /\\begin\s*\{\s*(verbatim\*?|Verbatim|lstlisting|minted|comment)\s*\}[\s\S]*?\\end\s*\{\s*\1\s*\}/g;
  for (const match of text.matchAll(opaque)) hide(match.index, match.index + match[0].length);
  text = chars.join("");
  for (const match of text.matchAll(/\\verb\*?([^\w\s])([^\r\n]*?)\1/g)) hide(match.index, match.index + match[0].length);
  return chars.join("");
}

export function inspectTagPreamble(source) {
  source = String(source || "");
  const text = maskPreamble(source);
  const commands = [];
  let depth = 0;
  let begin = -1;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\\") {
      const match = /^\\([A-Za-z@]+|.)/.exec(text.slice(i));
      if (!match) continue;
      if (depth === 0) {
        if (match[1] === "verb" || (match[1] === "begin" && /^\\begin\s*\{\s*(?:comment|verbatim\*?|Verbatim|lstlisting|minted)\s*\}/.test(text.slice(i)))) {
          return { kind: "manual", reason: "The preamble contains an unfinished verbatim or comment block." };
        }
        if (match[1] === "begin" && /^\\begin\s*\{\s*document\s*\}/.test(text.slice(i))) {
          begin = i;
          break;
        }
        commands.push({ name: match[1], offset: i });
      }
      i += match[0].length - 1;
    } else if (text[i] === "{") depth++;
    else if (text[i] === "}") {
      if (depth === 0) return { kind: "manual", reason: "The main document has an unbalanced preamble." };
      depth--;
    }
  }
  if (begin < 0 || depth !== 0) return { kind: "manual", reason: "A complete main-document preamble could not be identified." };
  const preamble = text.slice(0, begin);
  const loaded = /\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\]\s*)?\{([^{}]*)\}/g;
  let packageLoaded = false;
  for (const match of preamble.matchAll(loaded)) {
    if (match[1].split(",").some(name => name.trim() === "lea-tags")) packageLoaded = true;
    if (match[1].split(",").some(name => /(?:^|\/)lea-tags(?:\.sty)?$/.test(name.trim()) && name.trim() !== "lea-tags")) {
      return { kind: "manual", reason: "The preamble already loads a Lea style from another path. Review that load manually." };
    }
  }
  const directInput = /^\\input\s*(?:\{\s*lea-tags(?:\.sty)?\s*\}|lea-tags(?:\.sty)?(?=\s|$))/;
  packageLoaded ||= commands.some(command => command.name === "input" && directInput.test(preamble.slice(command.offset)));
  const inline = commands.some(command => (
    /^\\(?:(?:New|Renew|Provide)DocumentCommand|(?:new|renew|provide)command)\*?\s*\{?\s*\\lea(?:theorem|lemma|proposition|corollary|definition)?\b/.test(preamble.slice(command.offset))
    || /^\\(?:lst)?newenvironment\s*\{\s*leacode\s*\}/.test(preamble.slice(command.offset))
  ));
  const uncertain = commands.some(command => {
    if (command.name === "input" && directInput.test(preamble.slice(command.offset))) return false;
    return /^(?:if.*|else|fi|If.*|csname|catcode|def|gdef|edef|xdef|input|include|subfile|InputIfFileExists|AtBeginDocument|AtEndPreamble|AddToHook)$/.test(command.name);
  }) || /\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand|NewDocumentCommand|NewDocumentEnvironment|newenvironment|renewenvironment)[\s\S]*\\(?:usepackage|RequirePackage)/.test(preamble);
  // Do not mistake a load hidden inside a macro/conditional for an executed one.
  if (uncertain) {
    return { kind: "manual", reason: "The preamble delegates or conditionally defines package loading. Add the package line manually." };
  }
  if (inline) return { kind: "inline" };
  if (packageLoaded) return { kind: "loaded" };
  if (!commands.some(command => command.name === "documentclass")) {
    return { kind: "manual", reason: "The configured main document does not have a standard document preamble." };
  }
  const newline = source.includes("\r\n") ? "\r\n" : source.includes("\r") ? "\r" : "\n";
  const lineStart = Math.max(source.lastIndexOf("\n", begin - 1), source.lastIndexOf("\r", begin - 1)) + 1;
  const offset = /^\s*$/.test(source.slice(lineStart, begin)) ? lineStart : begin;
  return { kind: "insert", offset, insert: (offset === lineStart ? "" : newline) + PACKAGE_LINE + newline };
}

export function projectFromMetadata(model, projectId) {
  if (String(model?._id || "") !== projectId || !Array.isArray(model.rootFolder) || !model.rootFolder[0]) {
    throw setupError("Overleaf returned incomplete or mismatched project metadata.", "project_metadata");
  }
  const entities = [];
  function walk(folder, parent) {
    for (const doc of folder.docs || []) entities.push({ id: String(doc._id), path: parent + doc.name, type: "doc" });
    for (const file of folder.fileRefs || []) entities.push({ id: String(file._id), path: parent + file.name, type: "file" });
    for (const child of folder.folders || []) {
      entities.push({ id: String(child._id), path: parent + child.name, type: "folder" });
      walk(child, parent + child.name + "/");
    }
  }
  walk(model.rootFolder[0], "");
  return {
    projectId, rootId: String(model.rootFolder[0]._id), entities,
    main: entities.find(entity => entity.id === String(model.rootDoc_id) && entity.type === "doc") || null
  };
}

export function createOverleafSetupClient({ projectId, signal, fetchImpl = fetch, documentImpl = document, bridge, isCurrent, parseHtml = html => new DOMParser().parseFromString(html, "text/html") }) {
  const check = () => {
    if (signal?.aborted || !isCurrent()) throw setupError("Setup stopped because you navigated away or changed files.", "cancelled");
  };
  async function request(path, options = {}) {
    check();
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, NETWORK_TIMEOUT_MS);
    try {
      const response = await fetchImpl(path, { ...options, credentials: "same-origin", cache: "no-store", redirect: "error", signal: controller.signal });
      check();
      if (!response.ok) {
        const code = response.status === 401 || response.status === 403 ? "permission" : "http";
        const detail = (await response.text()).slice(0, 300);
        throw setupError(response.status === 401 || response.status === 403
          ? "Overleaf refused the request. Check that you are signed in and have permission to edit this project."
          : "Overleaf returned HTTP " + response.status + (detail.includes("file already exists") ? " (file already exists)." : "."), code);
      }
      // Consume the body inside the timeout; fetch can resolve before it arrives.
      const body = options.binary ? await response.arrayBuffer() : await response.text();
      check();
      return body;
    } catch (error) {
      check();
      if (controller.signal.aborted) throw setupError("Overleaf did not finish the request within 30 seconds.", "timeout");
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  return {
    async inspect() {
      const html = await request("/project/" + encodeURIComponent(projectId));
      const parsed = parseHtml(html);
      const id = parsed.querySelector('meta[name="ol-project_id"]')?.content;
      const meta = parsed.querySelector('meta[name="ol-project"]');
      if (id && id !== projectId) throw setupError("Overleaf returned a different project.", "project_metadata");
      if (meta) return projectFromMetadata(JSON.parse(meta.content), projectId);
      if (id !== projectId) throw setupError("Your Overleaf session could not be confirmed. Reload the project or sign in again.", "project_metadata");
      // Current SaaS builds load the project over their collaborative connection
      // rather than embedding ol-project. Read its configured root via the UI.
      const inventory = JSON.parse(await request("/project/" + encodeURIComponent(projectId) + "/entities"));
      if (!Array.isArray(inventory) && String(inventory.project_id || "") !== projectId) {
        throw setupError("Overleaf returned a different file inventory.", "project_metadata");
      }
      const entities = Array.isArray(inventory) ? inventory : inventory.entities;
      if (!Array.isArray(entities)) throw setupError("Overleaf's file inventory is unavailable.", "project_metadata");
      const context = await bridge("context");
      return { projectId, rootId: null, main: context.main || null, entities: entities.map(entity => ({ ...entity, path: normalizeSetupPath(entity.path) })) };
    },
    async archive() {
      return extractLatexSourcesFromZip(await request("/project/" + encodeURIComponent(projectId) + "/download/zip", { binary: true }));
    },
    async createStyle(rootId) {
      const token = documentImpl.querySelector('meta[name="ol-csrfToken"]')?.content;
      if (!token) throw setupError("Overleaf's session token is missing. Reload the project and try again.", "csrf");
      try {
        const result = JSON.parse(await request("/project/" + encodeURIComponent(projectId) + "/doc", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Csrf-Token": token, Accept: "application/json" },
          body: JSON.stringify({ name: STYLE_PATH, parent_folder_id: rootId || null })
        }));
        if (!result?._id || result.name !== STYLE_PATH) throw setupError("Overleaf did not confirm the new style document.", "creation_unknown");
        return { id: String(result._id), path: STYLE_PATH, type: "doc" };
      } catch (error) {
        // A request may have reached the server even if its reply was lost.
        // Inspect again, but never treat an existing empty document as ours.
        try {
          const project = await this.inspect();
          if (project.entities.some(entity => entity.path === STYLE_PATH)) {
            error.code = "creation_unknown";
            error.message = "lea-tags.sty now exists, but its creation could not be confirmed. It was left unchanged; inspect it or finish setup manually.";
          }
        } catch { /* Retain the original error. */ }
        throw error;
      }
    },
    check
  };
}

// Dependency injection keeps the operation testable without an Overleaf account.
export async function installLeaTags({ client, bridge, styleText, onProgress = () => {}, wait = abortableWait, signal }) {
  const result = { style: "missing", package: "manual", mainPath: "", changed: false, manual: true };
  const progress = text => { client.check(); onProgress(text); };
  const readMain = async project => project.main ? bridge("read", { document: project.main }) : null;
  try {
    progress("Checking project");
    const project = await client.inspect();
    result.mainPath = project.main?.path || "";
    const initialEditor = await bridge("peek");
    const main = await readMain(project);
    const preamble = main ? inspectTagPreamble(main.source) : { kind: "manual", reason: "The configured main document could not be identified." };
    if (preamble.kind === "inline") {
      return { ...result, style: "not-needed", package: "inline", manual: false, message: "Already enabled by existing Lea definitions. Your preamble was left unchanged." };
    }
    let style = project.entities.find(entity => entity.path === STYLE_PATH);
    let files = await client.archive();
    // Respect unsaved modifications when the style file is currently being edited.
    if (style?.type === "doc") {
      const live = initialEditor;
      if (live?.document?.path === STYLE_PATH && (!style.id || live.document.id === style.id)) {
        files = files.map(file => file.path === STYLE_PATH ? { ...file, content: live.source } : file);
      }
    }
    if (style) {
      const existing = files.find(file => file.path === STYLE_PATH);
      if (style.type !== "doc" || !existing || normalizeStyle(existing.content) !== normalizeStyle(styleText)) {
        result.style = "custom";
        result.package = preamble.kind === "loaded" ? "loaded" : "manual";
        return { ...result, manual: result.package !== "loaded", message: result.package === "loaded"
          ? "The existing lea-tags.sty is already loaded. Its contents were left unchanged."
          : "An existing lea-tags.sty differs from the bundled style or is not editable. It was left unchanged; review it before adding the package line." };
      }
      result.style = "existing";
    } else {
      progress("Creating style file");
      style = await client.createStyle(project.rootId);
      result.style = "created";
      result.changed = true;
      await bridge("populate", { document: style, styleText });
    }
    // Confirm the saved style before inserting a package line.
    progress("Verifying");
    files = await verifySaved(client, files => files.some(file => file.path === STYLE_PATH && normalizeStyle(file.content) === normalizeStyle(styleText)), wait, signal);
    if (preamble.kind === "manual") {
      return { ...result, message: "lea-tags.sty is installed. " + preamble.reason + " Copy the package line to finish setup." };
    }
    progress("Enabling tags");
    const enabled = await bridge("enable", { document: project.main });
    if (enabled.kind === "manual" || enabled.kind === "inline") {
      return { ...result, package: enabled.kind, manual: enabled.kind !== "inline", message: enabled.kind === "inline"
        ? "lea-tags.sty is installed. Existing Lea definitions were preserved."
        : "lea-tags.sty is installed. " + enabled.reason + " Copy the package line to finish setup." };
    }
    result.changed ||= enabled.changed;
    result.package = "loaded";
    progress("Verifying");
    await verifySaved(client, files => {
      const savedStyle = files.find(file => file.path === STYLE_PATH);
      const savedMain = files.find(file => file.path === project.main.path);
      return savedStyle && normalizeStyle(savedStyle.content) === normalizeStyle(styleText) && savedMain && inspectTagPreamble(savedMain.content).kind === "loaded";
    }, wait, signal);
    return { ...result, manual: false, message: result.changed
      ? "Lea tags are installed and loaded in " + project.main.path + ". Recompile your project to use them."
      : "Already enabled in " + project.main.path + ". No files were changed." };
  } catch (error) {
    error.setupResult = result;
    if (result.style === "created" || result.style === "existing") {
      error.message = "lea-tags.sty was " + (result.style === "created" ? "created" : "preserved") + ". " + error.message;
    }
    throw error;
  }
}

async function verifySaved(client, predicate, wait, signal) {
  for (let attempt = 0; attempt < 2; attempt++) {
    client.check();
    const files = await client.archive();
    if (predicate(files)) return files;
    if (attempt === 0) await wait(2000, signal);
  }
  throw setupError("Saving could not be confirmed. Check the project files before retrying.", "verification");
}

export function abortableWait(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(setupError("Setup cancelled.", "cancelled")); return; }
    const abort = () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); reject(setupError("Setup cancelled.", "cancelled")); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, ms);
    signal?.addEventListener("abort", abort, { once: true });
  });
}
