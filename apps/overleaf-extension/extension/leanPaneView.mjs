// Pure, DOM-free helpers for the Overleaf Lean project pane.
//
// These live in their own module so they can be unit-tested directly: the content
// script that consumes them (content.js) is a classic IIFE that the test runner
// can't import, so any logic worth testing is pulled out here. content.js loads
// this module lazily via `import(chrome.runtime.getURL("leanPaneView.mjs"))` (the
// same web-accessible-resource pattern as zipTex.mjs / targetParserCore.mjs).

export { renderPaneMath } from "./leanPaneMath.mjs";

const PANE_STATUS_LABELS = {
  "missing-stub": "missing stub",
  "stub-generated": "stub generated",
  valid: "valid",
  defined: "defined",
  // A verified disproof is surfaced as a counterexample: a successful result, but
  // not a proof of the stated theorem (FEATURE-counterexample-workflows.md).
  disproved: "counterexample",
  "in-progress": "in progress",
  invalid: "invalid",
  stale: "out of date",
  error: "error",
  mixed: "mixed",
  unknown: "unknown"
};

const LEAN_KEYWORDS = new Set([
  "theorem", "lemma", "def", "example", "instance", "structure", "class", "inductive",
  "abbrev", "import", "open", "namespace", "end", "section", "variable", "variables",
  "by", "fun", "let", "have", "show", "from", "with", "do", "return", "match",
  "if", "then", "else", "calc", "where", "deriving", "attribute", "set_option",
  "exact", "apply", "intro", "intros", "refine", "rw", "rewrite", "simp", "simpa",
  "ring", "linarith", "nlinarith", "omega", "norm_num", "constructor", "cases",
  "rcases", "rintro", "obtain", "use", "exists", "induction", "sorry", "admit",
  "unfold", "dsimp", "subst", "contradiction", "assumption", "trivial", "decide"
]);

const LEAN_TOKEN_RE = /(--[^\n]*)|("(?:[^"\\]|\\.)*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_']*)|(\s+)|([^\sA-Za-z0-9_]+)/g;

const MATH_COMMANDS = new Map([
  ["alpha", "α"], ["beta", "β"], ["gamma", "γ"], ["delta", "δ"],
  ["epsilon", "ε"], ["varepsilon", "ε"], ["zeta", "ζ"], ["eta", "η"],
  ["theta", "θ"], ["vartheta", "ϑ"], ["iota", "ι"], ["kappa", "κ"],
  ["lambda", "λ"], ["mu", "μ"], ["nu", "ν"], ["xi", "ξ"],
  ["pi", "π"], ["rho", "ρ"], ["sigma", "σ"], ["tau", "τ"],
  ["upsilon", "υ"], ["phi", "φ"], ["varphi", "φ"], ["chi", "χ"],
  ["psi", "ψ"], ["omega", "ω"], ["Gamma", "Γ"], ["Delta", "Δ"],
  ["Theta", "Θ"], ["Lambda", "Λ"], ["Xi", "Ξ"], ["Pi", "Π"],
  ["Sigma", "Σ"], ["Phi", "Φ"], ["Psi", "Ψ"], ["Omega", "Ω"],
  ["forall", "∀"], ["exists", "∃"], ["in", "∈"], ["notin", "∉"],
  ["subset", "⊂"], ["subseteq", "⊆"], ["supset", "⊃"], ["supseteq", "⊇"],
  ["cup", "∪"], ["cap", "∩"], ["emptyset", "∅"], ["varnothing", "∅"],
  ["to", "→"], ["rightarrow", "→"], ["mapsto", "↦"], ["leftarrow", "←"],
  ["Rightarrow", "⇒"], ["implies", "⇒"], ["iff", "⇔"], ["leftrightarrow", "↔"],
  ["le", "≤"], ["leq", "≤"], ["ge", "≥"], ["geq", "≥"], ["neq", "≠"],
  ["ne", "≠"], ["approx", "≈"], ["equiv", "≡"], ["sim", "∼"],
  ["triangleq", "≜"], ["coloneqq", "≔"], ["coloneq", "≔"],
  ["cdot", "·"], ["times", "×"], ["pm", "±"], ["setminus", "∖"],
  ["partial", "∂"], ["nabla", "∇"], ["infty", "∞"], ["infinity", "∞"],
  ["land", "∧"], ["lor", "∨"], ["neg", "¬"], ["bot", "⊥"], ["top", "⊤"],
  ["mid", "∣"], ["vert", "∣"], ["parallel", "∥"], ["ldots", "…"], ["cdots", "⋯"]
]);

const DOUBLE_STRUCK = {
  A: "𝔸", B: "𝔹", C: "ℂ", D: "𝔻", E: "𝔼", F: "𝔽", G: "𝔾", H: "ℍ",
  I: "𝕀", J: "𝕁", K: "𝕂", L: "𝕃", M: "𝕄", N: "ℕ", O: "𝕆", P: "ℙ",
  Q: "ℚ", R: "ℝ", S: "𝕊", T: "𝕋", U: "𝕌", V: "𝕍", W: "𝕎", X: "𝕏",
  Y: "𝕐", Z: "ℤ"
};

const PANE_TEXT_COMMAND_MARKS = new Map([
  ["emph", "em"],
  ["textit", "em"],
  ["textbf", "strong"],
  ["texttt", "code"],
  ["textrm", ""],
  ["textnormal", ""],
  ["mbox", ""]
]);

const PANE_REFERENCE_COMMANDS = new Set(["ref", "eqref", "autoref", "cite"]);
const PANE_TEXT_ESCAPES = new Map([
  ["%", "%"],
  ["&", "&"],
  ["#", "#"],
  ["_", "_"],
  ["{", "{"],
  ["}", "}"],
  ["$", "$"]
]);

const SUCCESS_PANE_STATUSES = new Set(["valid", "defined", "disproved"]);
const PROGRESS_SUCCESS_STATUSES = new Set(["valid", "formalized", "defined", "disproved"]);
const PROGRESS_STUB_STATUSES = new Set(["stub-generated", "sorry_stub"]);
const PROGRESS_FAILED_STATUSES = new Set(["failed", "invalid", "error"]);
const PROGRESS_IN_PROGRESS_STATUSES = new Set(["in-progress", "in_progress"]);
const PROGRESS_SEGMENT_DEFS = [
  { id: "success", label: "Successful", countKey: "success" },
  { id: "sorry-stubbed", label: "Sorry-stubbed", countKey: "sorryStubbed" },
  { id: "failed", label: "Failed", countKey: "failed" },
  { id: "out-of-date", label: "Out of date", countKey: "outOfDate" },
  { id: "unformalized", label: "Unformalized", countKey: "unformalized" }
];

export function formatPaneStatus(status) {
  return PANE_STATUS_LABELS[status] || "unknown";
}

export function capitalize(value) {
  const text = String(value || "");
  return text ? `${text.slice(0, 1).toUpperCase()}${text.slice(1)}` : "";
}

export function parsePaneLatex(source) {
  const text = String(source || "");
  const segments = [];
  let cursor = 0;

  while (cursor < text.length) {
    const next = findNextMathDelimiter(text, cursor);
    if (!next) {
      appendPaneLatexSegment(segments, { type: "text", text: text.slice(cursor) });
      break;
    }
    if (next.index > cursor) {
      appendPaneLatexSegment(segments, { type: "text", text: text.slice(cursor, next.index) });
    }

    const contentStart = next.index + next.open.length;
    const closeIndex = findClosingMathDelimiter(text, contentStart, next);
    if (closeIndex === -1) {
      appendPaneLatexSegment(segments, { type: "text", text: text.slice(next.index) });
      break;
    }

    appendPaneLatexSegment(segments, {
      type: "math",
      text: next.includeDelimiters
        ? text.slice(next.index, closeIndex + next.close.length)
        : text.slice(contentStart, closeIndex),
      display: next.display
    });
    cursor = closeIndex + next.close.length;
  }

  return segments;
}

export function formatLiteMath(source) {
  const text = normalizeLiteMath(String(source || ""));
  const segments = [];
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char !== "^" && char !== "_") {
      appendMathPart(segments, { type: "text", text: cleanMathText(char) });
      continue;
    }

    const parsed = parseMathScriptArgument(text, index + 1);
    if (!parsed) {
      appendMathPart(segments, { type: "text", text: char });
      continue;
    }
    appendMathPart(segments, {
      type: char === "^" ? "sup" : "sub",
      text: cleanMathText(parsed.text)
    });
    index = parsed.end - 1;
  }

  return segments.filter((segment) => segment.text);
}

// Render the non-math portions of theorem text conservatively. This is not a
// TeX interpreter: it handles the small set of prose commands that commonly
// occur in theorem statements and preserves unknown commands verbatim. Keeping
// the result structured lets content.js build DOM nodes without injecting the
// original LaTeX as HTML.
export function formatLiteLatexText(source) {
  const parts = [];
  appendLiteLatexText(parts, String(source || ""), []);
  return parts;
}

export function highlightLeanLine(line) {
  const text = String(line || "");
  if (!text) return [];
  const spans = [];
  let match;
  LEAN_TOKEN_RE.lastIndex = 0;
  while ((match = LEAN_TOKEN_RE.exec(text)) !== null) {
    const [, comment, str, num, word, space, sym] = match;
    if (comment !== undefined) spans.push({ cls: "com", text: comment });
    else if (str !== undefined) spans.push({ cls: "str", text: str });
    else if (num !== undefined) spans.push({ cls: "num", text: num });
    else if (word !== undefined) spans.push({ cls: classifyLeanWord(word), text: word });
    else if (space !== undefined) spans.push({ cls: "", text: space });
    else if (sym !== undefined) spans.push({ cls: "", text: sym });
  }
  return spans;
}

export function buildLeanPaneTree(items) {
  const root = { type: "root", children: [], childMap: new Map(), order: Number.POSITIVE_INFINITY };
  const files = new Map();
  const sortedItems = [...(Array.isArray(items) ? items : [])]
    .filter((item) => normalizePanePath(item?.sourceFile))
    .sort((a, b) => paneItemOrder(a) - paneItemOrder(b));

  for (const item of sortedItems) {
    const sourceFile = normalizePanePath(item.sourceFile);
    const parts = sourceFile.split("/").filter(Boolean);
    if (parts.length === 0) continue;

    let parent = root;
    let currentPath = "";
    for (let index = 0; index < parts.length - 1; index += 1) {
      currentPath = currentPath ? `${currentPath}/${parts[index]}` : parts[index];
      const id = paneTreeFolderId(currentPath);
      let folder = parent.childMap.get(id);
      if (!folder) {
        folder = {
          type: "folder",
          id,
          name: parts[index],
          path: currentPath,
          children: [],
          childMap: new Map(),
          itemCount: 0,
          status: "unknown",
          order: paneItemOrder(item)
        };
        parent.childMap.set(id, folder);
        parent.children.push(folder);
      }
      folder.order = Math.min(folder.order, paneItemOrder(item));
      parent = folder;
    }

    const fileName = parts[parts.length - 1];
    const fileId = paneTreeFileId(sourceFile);
    let file = parent.childMap.get(fileId);
    if (!file) {
      file = {
        type: "file",
        id: fileId,
        name: fileName,
        path: sourceFile,
        items: [],
        itemCount: 0,
        status: "unknown",
        order: paneItemOrder(item)
      };
      parent.childMap.set(fileId, file);
      parent.children.push(file);
      files.set(sourceFile, file);
    }
    file.items.push(item);
    file.itemCount = file.items.length;
    file.order = Math.min(file.order, paneItemOrder(item));
  }

  finalizePaneTreeNodes(root.children);
  return { children: root.children, files: [...files.values()] };
}

// Build the source-declared dependency edges (`uses=`) and their inverse for
// Lean-pane display. The marker label is the stable source-side identity: a
// manual Lean rename may change leanDeclarationName, but it must not retarget
// the relationship recorded in LaTeX. Missing labels are not errors here -- a
// dependency may be a valid recorded artifact that simply is not marked in the
// current project inventory.
export function buildPaneUseRelationships(items) {
  const list = Array.isArray(items) ? items : [];
  const itemsByLabel = new Map();
  const usesByItem = new Map();
  const usedByItem = new Map();

  for (const item of list) {
    usesByItem.set(item, []);
    usedByItem.set(item, []);
    const label = String(item?.label || "").trim();
    if (!label) continue;
    const matches = itemsByLabel.get(label) || [];
    matches.push(item);
    itemsByLabel.set(label, matches);
  }

  for (const sourceItem of list) {
    const uses = (Array.isArray(sourceItem?.targetUses) ? sourceItem.targetUses : [])
      .map((value) => String(value || "").trim())
      .filter(Boolean);
    const relationships = [];

    for (const label of uses) {
      const matches = itemsByLabel.get(label) || [];
      const targetItem = matches.length === 1 ? matches[0] : null;
      const resolution = matches.length === 1
        ? "resolved"
        : matches.length > 1
          ? "ambiguous"
          : "not-in-inventory";
      relationships.push({
        label,
        item: targetItem,
        status: targetItem?.status || "unknown",
        resolution
      });

      if (targetItem) {
        usedByItem.get(targetItem).push({
          label: String(sourceItem?.label || sourceItem?.leanDeclarationName || "").trim(),
          item: sourceItem,
          status: sourceItem?.status || "unknown",
          resolution: "resolved"
        });
      }
    }
    usesByItem.set(sourceItem, relationships);
  }

  return { usesByItem, usedByItem };
}

export function aggregatePaneStatus(items) {
  const statuses = (Array.isArray(items) ? items : [])
    .map((item) => String(item?.status || "unknown"))
    .filter(Boolean);
  if (statuses.length === 0) return "unknown";
  if (statuses.includes("in-progress")) return "in-progress";
  if (statuses.includes("error")) return "error";
  if (statuses.includes("invalid")) return "invalid";
  if (statuses.includes("stale")) return "stale";
  if (statuses.includes("missing-stub")) return "missing-stub";
  if (statuses.includes("stub-generated")) return "stub-generated";
  if (statuses.every((status) => SUCCESS_PANE_STATUSES.has(status))) {
    const unique = new Set(statuses);
    return unique.size === 1 ? statuses[0] : "mixed";
  }
  const unique = new Set(statuses);
  return unique.size === 1 ? statuses[0] : "mixed";
}

export function paneProgressBucketForItem(item) {
  const status = String(item?.status || item?.effectiveStatus || "unknown").toLowerCase();
  if (PROGRESS_SUCCESS_STATUSES.has(status)) return "success";
  if (PROGRESS_STUB_STATUSES.has(status)) return "sorryStubbed";
  if (PROGRESS_FAILED_STATUSES.has(status)) return "failed";
  if (status === "stale") return "outOfDate";
  return "unformalized";
}

export function summarizePaneProgress(items) {
  const summary = {
    total: 0,
    success: 0,
    sorryStubbed: 0,
    failed: 0,
    outOfDate: 0,
    unformalized: 0,
    inProgress: 0
  };
  for (const item of Array.isArray(items) ? items : []) {
    summary.total += 1;
    summary[paneProgressBucketForItem(item)] += 1;
    const status = String(item?.status || "").toLowerCase();
    if (item?.inProgress || PROGRESS_IN_PROGRESS_STATUSES.has(status)) {
      summary.inProgress += 1;
    }
  }
  return summary;
}

export function paneProgressSegments(summary) {
  const total = Number(summary?.total || 0);
  if (!total) return [];
  return PROGRESS_SEGMENT_DEFS
    .map((segment) => {
      const count = Number(summary?.[segment.countKey] || 0);
      return {
        ...segment,
        count,
        percent: (count / total) * 100,
        title: `${segment.label}: ${count} of ${total}, ${formatProgressPercent((count / total) * 100)}`
      };
    })
    .filter((segment) => segment.count > 0);
}

export function formatPaneProgressLabel(path, summary) {
  const total = Number(summary?.total || 0);
  const file = normalizePanePath(path) || "File";
  const parts = [
    `${file}: ${total} Lea item${total === 1 ? "" : "s"}`
  ];
  appendProgressLabelPart(parts, summary?.success, "successful");
  appendProgressLabelPart(parts, summary?.sorryStubbed, "sorry-stubbed");
  appendProgressLabelPart(parts, summary?.failed, "failed");
  appendProgressLabelPart(parts, summary?.outOfDate, "out of date");
  appendProgressLabelPart(parts, summary?.unformalized, "unformalized");
  appendProgressLabelPart(parts, summary?.inProgress, "in progress");
  return `${parts.join(", ")}.`;
}

export function treeAncestorIdsForFile(sourceFile) {
  const parts = normalizePanePath(sourceFile).split("/").filter(Boolean);
  if (parts.length === 0) return [];
  const ids = [];
  let currentPath = "";
  for (let index = 0; index < parts.length - 1; index += 1) {
    currentPath = currentPath ? `${currentPath}/${parts[index]}` : parts[index];
    ids.push(paneTreeFolderId(currentPath));
  }
  ids.push(paneTreeFileId(parts.join("/")));
  return ids;
}

// Decide whether the pane must re-download the whole project archive, or can reuse
// the cached file set and just overlay the live active-editor buffer. A full fetch
// is only needed on an explicit request (manual refresh / first open) or when the
// project changed — ordinary edits to the active file are handled by the overlay, so
// typing no longer triggers a project download on every keystroke.
export function shouldRefetchLeanPaneFiles({ forceFetch, lastFiles, lastProjectId, projectId } = {}) {
  if (forceFetch) return true;
  if (!Array.isArray(lastFiles) || lastFiles.length === 0) return true;
  return lastProjectId !== projectId;
}

// Replace the cached content of the active file with the live editor buffer. Only
// overrides a path that already exists in the set — never invents a file — so a
// misread active path can't inject a spurious entry. Mutates and returns `files`.
export function overlayActiveTex(files, activePath, activeContent) {
  if (!Array.isArray(files)) return files;
  if (!activePath || typeof activeContent !== "string") return files;
  const wanted = String(activePath).replace(/^\/+/, "");
  const existing = files.find((file) => file && file.path === wanted);
  if (existing) existing.content = activeContent;
  return files;
}

// True when any manifest item is still being formalized — the signal the pane uses
// to keep polling and then stop once everything settles.
export function hasInProgressItems(items) {
  return Array.isArray(items) && items.some((item) => item && item.inProgress);
}

// Pane statuses included by the project-level "Formalize all" action. Completed
// work stays out of a batch even though its per-item Re-formalize action remains
// available.
const BATCH_FORMALIZABLE_PANE_STATUSES = new Set([
  "missing-stub", "stub-generated", "stale", "invalid", "unknown", "error"
]);

// Whether the pane should offer a Formalize / Re-formalize action for an item.
// Any valid marker target can be rerun, including a settled valid, defined, or
// disproved item; only an active run suppresses the action.
export function canFormalizePaneItem(item) {
  if (!item || !item.formalizable || item.inProgress) return false;
  return item.status !== "in-progress";
}

// Whether the pane should offer a sorry-stub action for an item. Mirrors the
// in-document popover rule: stubbing only applies to a theorem with no Lean
// artifact yet — definitions always get a full body, and once a stub or proof
// exists, Formalize / Re-formalize is the right action.
export function canStubPaneItem(item) {
  if (!item || !item.formalizable || item.inProgress) return false;
  if (item.leanKind === "def") return false;
  return item.status === "missing-stub";
}

// Shape a manifest item into the target payload the existing /formalize flow
// expects. targetLabel is the LaTeX marker `label` — NOT leanDeclarationName
// (AUDIT M6): the companion keys jobs by the marker label (the doc-side anchor
// that never changes), and the in-document badge always posts that label.
// Sending the current declaration name here instead forked a renamed item's
// job history — the pane and the badge would track two divergent jobKeys, and
// their busy-checks and cleanup wouldn't compose. The declaration-name bridge
// in resolveEditSession/resolveChatSession only exists because those paths
// can't re-key; a formalize CAN and should just use the label.
export function paneItemToFormalizeTarget(item) {
  return {
    targetKind: item?.leanKind === "def" ? "definition" : "theorem",
    targetLabel: item?.label || item?.leanDeclarationName || "",
    targetText: item?.naturalLanguageLatex || "",
    targetUses: Array.isArray(item?.targetUses) ? item.targetUses : [],
    targetContext: item?.targetContext || "",
    sourceFile: item?.sourceFile || "",
    sourceStartLine: item?.sourceStartLine,
    sourceEndLine: item?.sourceEndLine,
    sourceHash: item?.sourceHash || "",
    syntax: item?.syntax || "comment"
  };
}

// Shape a current tagged item for the additive GitHub import flow. The stable
// LaTeX marker label remains the identity anchor; the current Lean declaration
// name is the exact code-match key. The companion, not the page, computes the
// origin key so import/formalize/chat always use identical identity semantics.
export function paneItemToGithubImportTarget(item) {
  const targetLabel = item?.label || item?.leanDeclarationName || "";
  const declarationName = item?.leanDeclarationName || item?.label || "";
  return {
    targetKind: item?.leanKind === "def" ? "definition" : "theorem",
    targetLabel,
    declarationName,
    displayTitle: declarationName || targetLabel,
    statement: item?.naturalLanguageLatex || "",
    sourceHash: item?.sourceHash || ""
  };
}

// Return the document targets whose declarations will be populated by a
// confirmed GitHub import.  The preview is authoritative here: files skipped
// for either a path or declaration conflict must not make an unrelated item
// look busy.  Prefer the stable origin key emitted by the companion, with the
// declaration name as a compatibility fallback for older formalization rows
// that predate origin keys.
export function githubImportMatchedTargets(preview, targets) {
  const candidates = Array.isArray(targets) ? targets : [];
  const matched = new Map();
  for (const file of preview?.plan?.files || []) {
    if (!file || !["add", "already_present"].includes(file.disposition)) continue;
    for (const declaration of file.declarations || []) {
      const match = declaration?.match;
      if (!match) continue;
      for (const target of candidates) {
        const kind = target?.targetKind === "definition" ? "definition" : "theorem";
        const label = String(target?.targetLabel || "").trim();
        const declarationName = String(target?.declarationName || label).trim();
        const originKey = String(match.origin_key || "");
        const originMatches = label
          && originKey.endsWith(`:${kind}:${label}`);
        const declarationMatches = !originKey && declarationName
          && String(match.declaration_name || "") === declarationName;
        if (originMatches || declarationMatches) {
          const key = `${kind}:${label}`;
          if (!matched.has(key)) {
            matched.set(key, {
              key,
              targetKind: kind,
              targetLabel: label,
              declarationName,
              displayTitle: String(target?.displayTitle || match.display_title || declarationName || label),
              destinationPath: String(file.destination_path || ""),
            });
          }
        }
      }
    }
  }
  return [...matched.values()];
}

export function githubImportMatchedTargetKeys(preview, targets) {
  return githubImportMatchedTargets(preview, targets).map((target) => target.key);
}

// Pane statuses that correspond to a real Lea run or saved proof artifact.
// Same rule the in-document popover applies before offering "View in Lea UI"
// (companion statuses formalized / defined / disproved / in_progress /
// sorry_stub), translated to the pane vocabulary and extended with the
// pane-only artifact states (stale / invalid), which also imply
// a recorded run. missing-stub / unknown / error stay excluded so stale
// session metadata can't make unformalized items appear viewable.
const LEA_UI_VIEWABLE_PANE_STATUSES = new Set([
  "valid", "defined", "disproved", "in-progress",
  "stub-generated", "stale", "invalid"
]);

// Whether the pane should offer a "View in Lea UI" action for an item. Needs a
// stable target identity (like chat) to resolve the associated Lea session.
export function canViewPaneItemInLeaUi(item) {
  if (!item || !(item.leanDeclarationName || item.label)) return false;
  return LEA_UI_VIEWABLE_PANE_STATUSES.has(item.status);
}

// --- Lean-pane chat mirror -------------------------------------------------

// Whether the pane should offer a Chat action for an item. The mirror needs a
// stable target identity (a Lean declaration name, falling back to the marker
// label) to resolve or create the associated Lea session.
export function canChatPaneItem(item) {
  return Boolean(item && (item.leanDeclarationName || item.label));
}

// Shape a manifest item into the LeanPaneChatTarget payload the companion's
// /lean-pane/chat/* endpoints expect.
export function paneItemToChatTarget(item, overleafProjectId) {
  return {
    overleafProjectId: String(overleafProjectId || ""),
    targetKind: item?.leanKind === "def" ? "definition" : "theorem",
    targetLabel: item?.leanDeclarationName || item?.label || "",
    latexLabel: item?.latexLabel || "",
    sourceFile: item?.sourceFile || "",
    sourceStartLine: item?.sourceStartLine,
    sourceEndLine: item?.sourceEndLine,
    sourceHash: item?.sourceHash || "",
    naturalLanguageLatex: item?.naturalLanguageLatex || "",
    targetUses: Array.isArray(item?.targetUses) ? item.targetUses : [],
    targetContext: item?.targetContext || "",
    leanDeclarationName: item?.leanDeclarationName || "",
    recordedProofPath: item?.leanArtifactPath || "",
    status: item?.status || ""
  };
}

// Resolve the chat panel's visual state from the current fetch/run flags and the
// latest companion response. Drives which controls are enabled and what the
// panel renders (spec: "UI Specification" / states).
export function nextChatState({ loading = false, sending = false, response = null, error = null } = {}) {
  if (error) return "error";
  if (response && response.ok === false) {
    return response.error === "adapter_unavailable" ? "adapter-unavailable" : "error";
  }
  if (loading) return "loading-session";
  if (sending) return "running";
  if (response && response.activeRun) return "running";
  if (response && response.status === "no-session") return "no-session";
  if (response && response.ok) return "ready";
  return "loading-session";
}

// The composer accepts input only when the session is loaded and idle. A new
// session (no-session) accepts the first message; an in-flight run does not.
export function chatComposerEnabled(state) {
  return state === "ready" || state === "no-session";
}

// True while a run is in flight — the panel shows Stop instead of Send and keeps
// polling the companion until the run settles.
export function chatRunActive(state) {
  return state === "running";
}

// CSS modifier class for a transcript bubble by author role.
export function chatBubbleClass(role) {
  return String(role).toLowerCase() === "user"
    ? "ol-lean-chat-bubble-user"
    : "ol-lean-chat-bubble-assistant";
}

// --- Lean-pane manual edit --------------------------------------------------
// docs/FEATURE-overleaf-lean-pane-manual-edit.md.

// Editing is only offered for an item that already has a recorded artifact --
// a missing-stub item has nothing to edit; "Formalize" is the right action
// there.
export function canEditPaneItem(item) {
  return Boolean(item && item.leanArtifactContent);
}

// --- Item action hierarchy ---------------------------------------------------

// The full action set for an expanded pane item, arranged by visual weight so
// the card stays one row instead of a wall of same-looking buttons:
//   primary  -- at most ONE status-derived accented text button (repair wins
//               over formalize: breakage attribution means the guided path).
//   rail     -- always-relevant navigation, rendered as icon buttons.
//   overflow -- the rare alternatives, behind a "More actions" menu. A
//               formalizable-but-broken item keeps Re-formalize here so no
//               capability is lost when Repair takes the primary slot.
// Copy actions are absent by design: they belong on the code blocks they copy.
export function paneItemActions(item, { editing = false } = {}) {
  const canFormalize = canFormalizePaneItem(item);
  const formalizeAction = {
    id: "formalize",
    label: item?.status === "missing-stub" ? "Formalize" : "Re-formalize"
  };

  let primary = null;
  if (!editing && canRepairPaneItem(item)) {
    primary = { id: "repair", label: "Repair with Lea" };
  } else if (!editing && canFormalize) {
    primary = formalizeAction;
  }

  const rail = [{ id: "go-to-source", label: "Go to source" }];
  if (canChatPaneItem(item)) rail.push({ id: "chat", label: "Chat" });
  if (canViewPaneItemInLeaUi(item)) rail.push({ id: "view-in-lea", label: "Open in Lea UI" });

  const overflow = [];
  if (primary?.id === "repair" && canFormalize) overflow.push(formalizeAction);
  if (canStubPaneItem(item)) overflow.push({ id: "stub", label: "Stub" });
  if (!editing && canEditPaneItem(item)) overflow.push({ id: "edit", label: "Edit" });

  return { primary, rail, overflow };
}

// Shape a manifest item into the LeanPaneEditTarget payload the companion's
// /lean-pane/edit/* endpoints expect. Mirrors paneItemToChatTarget's shape.
export function paneItemToEditTarget(item, overleafProjectId) {
  return {
    overleafProjectId: String(overleafProjectId || ""),
    targetKind: item?.leanKind === "def" ? "definition" : "theorem",
    targetLabel: item?.leanDeclarationName || item?.label || ""
  };
}

// The pre-save impact preview: "editing this may affect N downstream
// item(s)". Accepts either /edit/start's `dependents` or /edit/save's
// `dependentsImpact` -- both carry `targetLabel`.
export function formatDependentsImpact(dependents) {
  const list = Array.isArray(dependents) ? dependents : [];
  if (list.length === 0) return "";
  const names = list.map((d) => d?.targetLabel).filter(Boolean).join(", ");
  const count = list.length;
  return `Editing this may affect ${count} downstream item${count === 1 ? "" : "s"}: ${names}.`;
}

// The post-save outcome line for a single cascade-checked dependent --
// distinguishes a verified break (renamed vs. same-name signature change,
// feature spec acceptance criterion 8) from busy/unattributed/still-fine.
//
// `status: "unknown"` with `attributed: true` is a DIFFERENT outcome from the
// "still valid" default below, and must come before it: it means the cascade
// never actually got a trustworthy verdict for this dependent -- either the
// upstream module's rebuild itself failed (server.mjs's dependents.length>0
// branch, before any dependent is even reached) or the per-dependent
// lean-check call itself errored out (adapter unreachable, etc.). Either way
// this dependent was NOT re-checked against current source, so saying "still
// valid" here would be exactly the stale-verdict bug this whole cascade
// exists to prevent -- falling through to the same text as a genuine,
// successful recheck was a real regression caught live (a rebuild failure
// correctly skipped the per-dependent check, but still rendered as "re-
// checked, still valid").
export function formatDependentOutcome(dependent) {
  if (!dependent) return "";
  const label = dependent.targetLabel || "";
  const base = (() => {
    if (dependent.busy) return `${label}: not re-checked yet (a Lea run is already in progress for it).`;
    if (dependent.brokenByUpstream?.renamed) {
      return `${label}: broken -- the declaration it referred to was renamed.`;
    }
    if (dependent.brokenByUpstream) return `${label}: broken by this edit.`;
    if (dependent.attributed === false) {
      return `${label}: may be affected, but no recorded session was found to re-check it.`;
    }
    if (dependent.status === "unknown") {
      const reason = dependent.checkDetail ? ` (${dependent.checkDetail})` : "";
      return `${label}: could not be verified${reason} -- treat as unconfirmed, not as valid.`;
    }
    return `${label}: re-checked, still valid.`;
  })();
  // Reconciliation suffixes (PLAN-self-repair-stale-offers Fix 1): the base
  // line is the HISTORICAL record of what this change did; when live truth
  // has since moved on, the line says so instead of being rewritten -- and
  // only on lines that claimed a problem, never on "still valid".
  const claimedProblem = Boolean(dependent.brokenByUpstream) || dependent.busy || dependent.status === "unknown";
  if (dependent.sinceFixed && claimedProblem) {
    return `${base.replace(/\.$/, "")} -- since fixed.`;
  }
  if (dependent.nowRepairing && claimedProblem) {
    return `${base.replace(/\.$/, "")} -- repair in progress.`;
  }
  // The upgrade direction: the snapshot claimed no break, but live truth says
  // this dependent is broken NOW (a busy-skipped re-check whose run since
  // ended broken, or a later change). The busy base line is a current-state
  // claim that is no longer true, so it is corrected, not merely suffixed.
  if (dependent.nowBroken && !dependent.brokenByUpstream) {
    if (dependent.busy) {
      return `${label}: was busy during this edit's re-check -- now broken.`;
    }
    return `${base.replace(/\.$/, "")} -- now broken.`;
  }
  return base;
}

// Reconcile a save-time dependentsImpact snapshot against the LIVE manifest
// items (PLAN-self-repair-stale-offers Fix 1). The snapshot is a historical
// record; anything claiming current state or offering an action must derive
// from live truth. Each entry gains:
//   sinceFixed   -- its manifest item positively reads fixed (no breakage,
//                   not invalid/unknown/in-progress)
//   nowRepairing -- a repair run is live on it right now
//   matched      -- a manifest item was found at all; unmatched entries keep
//                   their snapshot state (fail toward offering: a stale offer
//                   is a server-validated no-op, a hidden offer for something
//                   still broken is worse).
export function reconcileDependentsImpact(dependents, manifestItems) {
  const list = Array.isArray(dependents) ? dependents : [];
  const items = Array.isArray(manifestItems) ? manifestItems : [];
  const byName = new Map();
  for (const item of items) {
    for (const key of [item?.leanDeclarationName, item?.label]) {
      if (key && !byName.has(key)) byName.set(key, item);
    }
  }
  return list.map((dependent) => {
    const item = dependent?.targetLabel ? byName.get(dependent.targetLabel) : null;
    if (!item) return { ...dependent, sinceFixed: false, nowRepairing: false, nowBroken: false, matched: false };
    const nowRepairing = item.status === "in-progress" || item.breakage?.repair?.state === "running";
    const sinceFixed = !nowRepairing
      && !item.breakage
      && !["invalid", "unknown", "in-progress"].includes(String(item.status || ""));
    // The upgrade direction of the reconciliation: an entry the snapshot
    // recorded as busy/unknown/ok can be BROKEN by live truth now -- e.g. a
    // dependent skipped as busy during this edit's cascade whose run since
    // ended in failure (the second-rename-while-repairing case). Suppressed
    // breakage stays out of nowBroken: offering it would be a server 409.
    const nowBroken = !nowRepairing
      && Boolean(item.breakage)
      && !item.breakage.repairSuppressed;
    return { ...dependent, sinceFixed, nowRepairing, nowBroken, matched: true };
  });
}

// The subset of a reconciled impact list that warrants a repair offer, per
// the reconciliation principle in BOTH directions: for a dependent matched to
// live state, live truth alone decides (a snapshot-busy entry whose run since
// ended broken joins the offer; a snapshot-broken entry since fixed leaves
// it). Only an UNMATCHED dependent falls back to its snapshot -- fail toward
// offering, since a stale dispatch is a server-validated no-op.
export function stillBrokenDependents(dependents) {
  return (Array.isArray(dependents) ? dependents : []).filter((d) => {
    if (d.nowRepairing) return false;
    if (d.matched) return Boolean(d.nowBroken);
    return Boolean(d.brokenByUpstream) && !d.sinceFixed;
  });
}

// --- Self-repair (docs/FEATURE-overleaf-self-repair.md, Phase 5) -------------

// A repair is offered on an item that carries breakage attribution, is not
// suppressed (upstream itself broken -- the offer belongs there), and is not
// already running something.
export function canRepairPaneItem(item) {
  return Boolean(
    item &&
    item.breakage &&
    !item.breakage.repairSuppressed &&
    item.status !== "in-progress" &&
    item.breakage.repair?.state !== "running"
  );
}

// The chip-adjacent explanation of WHY an item is broken -- distinguishes the
// user's own edit, an upstream rename (acceptance criterion 8 of the
// manual-edit spec), and how the upstream change happened (via).
export function formatBreakageAttribution(breakage) {
  if (!breakage) return "";
  const via = { edit: "a manual edit", chat: "a chat request", formalize: "a re-formalization", repair: "a repair run" }[breakage.via] || "a change";
  if (breakage.selfBroken) {
    return `Broken by ${via} to this item.`;
  }
  if (breakage.classificationKind === "renamed" && breakage.renamedFrom && breakage.renamedTo) {
    return `Broken: \`${breakage.renamedFrom}\` was renamed to \`${breakage.renamedTo}\` (via ${via}). This item still refers to the old name.`;
  }
  if (breakage.repairSuppressed === "upstream_broken") {
    return `Broken by ${via} to ${breakage.upstreamLabel} -- repair ${breakage.upstreamLabel} first (its file currently fails to compile).`;
  }
  return `Broken by ${via} to ${breakage.upstreamLabel}.`;
}

// One line per item for the batch progress / outcome list.
// The eligible sets the project-level "Stub all" / "Formalize all" launchers
// operate over. The batch intentionally skips completed work: users can rerun a
// settled item explicitly without making "Formalize all" restart every proof.
export function stubbableItems(items) {
  return (Array.isArray(items) ? items : []).filter(canStubPaneItem);
}

export function formalizableItems(items) {
  return (Array.isArray(items) ? items : []).filter(
    (item) => canFormalizePaneItem(item) && BATCH_FORMALIZABLE_PANE_STATUSES.has(item.status)
  );
}

// One line per batch item. Wording depends on the batch `operation` only for
// the in-flight ("running") verb and the "already done" skip note; terminal
// states carry their own vocabulary (stubbed / formalized / repaired).
export function formatRepairOutcome(entry, operation = "repair") {
  if (!entry) return "";
  const label = entry.targetLabel || "";
  const runningVerb = operation === "stub" ? "stubbing" : operation === "formalize" ? "formalizing" : "repairing";
  switch (entry.state) {
    case "pending": return `${label}: waiting.`;
    case "running": return `${label}: ${runningVerb}...`;
    case "repaired": return `${label}: repaired and verified.`;
    case "stubbed": return `${label}: stub created.`;
    case "formalized": return `${label}: formalized and verified.`;
    case "disproved": return `${label}: counterexample found.`;
    case "needs_review": return `${label}: repaired, but the statement changed -- review required.`;
    case "canceled": return `${label}: stopped.`;
    case "failed": {
      const verb = operation === "stub" ? "stub failed" : operation === "formalize" ? "formalization failed" : "repair failed";
      return `${label}: ${verb}${entry.reason ? ` -- ${entry.reason}` : "."}`;
    }
    case "skipped":
      if (String(entry.reason || "").startsWith("depends_on_failed:")) {
        return `${label}: skipped -- depends on failed ${operation === "formalize" ? "formalization" : "repair"} of ${String(entry.reason).slice("depends_on_failed:".length)}.`;
      }
      return `${label}: skipped${entry.reason === "already_fixed" ? " -- already compiles." : entry.reason ? ` (${entry.reason})` : "."}`;
    default: return `${label}: ${entry.state || "unknown"}.`;
  }
}

function finalizePaneTreeNodes(nodes) {
  nodes.sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
  for (const node of nodes) {
    if (node.type === "folder") {
      finalizePaneTreeNodes(node.children);
      const descendantItems = node.children.flatMap((child) => child.type === "file" ? child.items : collectPaneTreeItems(child));
      node.itemCount = descendantItems.length;
      node.status = aggregatePaneStatus(descendantItems);
      delete node.childMap;
    } else if (node.type === "file") {
      node.items.sort((a, b) => paneItemOrder(a) - paneItemOrder(b));
      node.itemCount = node.items.length;
      node.status = aggregatePaneStatus(node.items);
      node.progress = summarizePaneProgress(node.items);
    }
  }
}

function collectPaneTreeItems(node) {
  if (node.type === "file") return node.items;
  return node.children.flatMap((child) => collectPaneTreeItems(child));
}

function paneItemOrder(item) {
  const order = Number(item?.documentOrder);
  return Number.isFinite(order) ? order : Number.POSITIVE_INFINITY;
}

function paneTreeFolderId(path) {
  return `folder:${normalizePanePath(path)}`;
}

function paneTreeFileId(path) {
  return `file:${normalizePanePath(path)}`;
}

function normalizePanePath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/{2,}/g, "/").trim();
}

function appendProgressLabelPart(parts, count, label) {
  const value = Number(count || 0);
  if (value > 0) parts.push(`${value} ${label}`);
}

function formatProgressPercent(value) {
  const rounded = Math.round(Number(value || 0) * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}%`;
}

function findNextMathDelimiter(text, cursor) {
  const delimiters = [
    { open: "\\begin{equation*}", close: "\\end{equation*}", display: true, includeDelimiters: true },
    { open: "\\begin{equation}", close: "\\end{equation}", display: true, includeDelimiters: true },
    { open: "\\begin{align*}", close: "\\end{align*}", display: true, includeDelimiters: true },
    { open: "\\begin{align}", close: "\\end{align}", display: true, includeDelimiters: true },
    { open: "\\begin{gather*}", close: "\\end{gather*}", display: true, includeDelimiters: true },
    { open: "\\begin{gather}", close: "\\end{gather}", display: true, includeDelimiters: true },
    { open: "$$", close: "$$", display: true },
    { open: "\\[", close: "\\]", display: true },
    { open: "\\(", close: "\\)", display: false },
    { open: "$", close: "$", display: false }
  ];
  let best = null;
  for (const delimiter of delimiters) {
    let index = cursor;
    while (index < text.length) {
      index = text.indexOf(delimiter.open, index);
      if (index === -1) break;
      if (delimiter.open === "$" && text[index + 1] === "$") {
        index += 2;
        continue;
      }
      if (!isEscaped(text, index)) {
        if (!best || index < best.index || (index === best.index && delimiter.open.length > best.open.length)) {
          best = { ...delimiter, index };
        }
        break;
      }
      index += delimiter.open.length;
    }
  }
  return best;
}

function findClosingMathDelimiter(text, cursor, delimiter) {
  let index = cursor;
  while (index < text.length) {
    index = text.indexOf(delimiter.close, index);
    if (index === -1) return -1;
    if (!isEscaped(text, index)) return index;
    index += delimiter.close.length;
  }
  return -1;
}

function isEscaped(text, index) {
  let slashCount = 0;
  for (let cursor = index - 1; cursor >= 0 && text[cursor] === "\\"; cursor -= 1) {
    slashCount += 1;
  }
  return slashCount % 2 === 1;
}

function appendPaneLatexSegment(segments, segment) {
  if (!segment.text) return;
  const previous = segments[segments.length - 1];
  if (previous && previous.type === "text" && segment.type === "text") {
    previous.text += segment.text;
  } else {
    segments.push(segment);
  }
}

function normalizeLiteMath(source) {
  let text = source
    .replace(/\r?\n/g, " ")
    .replace(/\\(?:left|right)\b/g, "")
    .replace(/\\(?:quad|qquad|,|;|:|!)/g, " ");

  for (let i = 0; i < 3; i += 1) {
    text = text
      .replace(/\\mathbb\s*\{([A-Za-z])\}/g, (_match, letter) => DOUBLE_STRUCK[letter] || letter)
      .replace(/\\(?:mathrm|operatorname|text|textit|textbf)\s*\{([^{}]*)\}/g, "$1")
      .replace(/\\sqrt\s*\{([^{}]*)\}/g, "√($1)")
      .replace(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "($1)/($2)");
  }

  return text
    .replace(/\\([{}])/g, "$1")
    .replace(/\\([A-Za-z]+)\b/g, (_match, command) => (
      MATH_COMMANDS.has(command) ? MATH_COMMANDS.get(command) : `\\${command}`
    ))
    .replace(/\s+/g, " ")
    .trim();
}

function appendLiteLatexText(parts, text, inheritedMarks) {
  for (let index = 0; index < text.length;) {
    const char = text[index];
    if (char === "~") {
      appendLiteLatexPart(parts, "\u00a0", inheritedMarks);
      index += 1;
      continue;
    }
    if (char !== "\\") {
      appendLiteLatexPart(parts, char, inheritedMarks);
      index += 1;
      continue;
    }

    const escaped = text[index + 1];
    if (PANE_TEXT_ESCAPES.has(escaped)) {
      appendLiteLatexPart(parts, PANE_TEXT_ESCAPES.get(escaped), inheritedMarks);
      index += 2;
      continue;
    }
    if (escaped === "\\") {
      appendLiteLatexPart(parts, "\n", inheritedMarks);
      index += 2;
      continue;
    }
    if (escaped === " ") {
      appendLiteLatexPart(parts, " ", inheritedMarks);
      index += 2;
      continue;
    }

    const commandMatch = text.slice(index).match(/^\\([A-Za-z]+)\b/);
    if (!commandMatch) {
      appendLiteLatexPart(parts, "\\", inheritedMarks);
      index += 1;
      continue;
    }

    const command = commandMatch[1];
    const commandEnd = index + commandMatch[0].length;
    const groupStart = skipLiteLatexWhitespace(text, commandEnd);
    const group = parseLiteLatexGroup(text, groupStart);

    if (command === "label" && group) {
      index = group.end;
      continue;
    }

    if (PANE_TEXT_COMMAND_MARKS.has(command) && group) {
      const mark = PANE_TEXT_COMMAND_MARKS.get(command);
      const marks = mark ? [...inheritedMarks, mark] : inheritedMarks;
      appendLiteLatexText(parts, group.text, marks);
      index = group.end;
      continue;
    }

    if (PANE_REFERENCE_COMMANDS.has(command) && group) {
      const referenceText = command === "eqref" ? `(${group.text})` : group.text;
      appendLiteLatexPart(parts, referenceText, [...inheritedMarks, "ref"]);
      index = group.end;
      continue;
    }

    // Unknown commands stay source-visible. In particular, never turn \Spec
    // into "Spec" merely because this lightweight prose path does not know it.
    appendLiteLatexPart(parts, commandMatch[0], inheritedMarks);
    index = commandEnd;
  }
}

function appendLiteLatexPart(parts, text, marks) {
  if (!text) return;
  const normalizedMarks = [...new Set(marks)].sort();
  const previous = parts[parts.length - 1];
  if (previous && previous.marks.join("\u0000") === normalizedMarks.join("\u0000")) {
    previous.text += text;
    return;
  }
  parts.push({ type: "text", text, marks: normalizedMarks });
}

function skipLiteLatexWhitespace(text, cursor) {
  let index = cursor;
  while (index < text.length && /[ \t]/.test(text[index])) index += 1;
  return index;
}

function parseLiteLatexGroup(text, cursor) {
  if (text[cursor] !== "{") return null;
  let depth = 1;
  for (let index = cursor + 1; index < text.length; index += 1) {
    if (text[index] === "{" && !isEscaped(text, index)) {
      depth += 1;
    } else if (text[index] === "}" && !isEscaped(text, index)) {
      depth -= 1;
      if (depth === 0) {
        return { text: text.slice(cursor + 1, index), end: index + 1 };
      }
    }
  }
  return null;
}

function parseMathScriptArgument(text, cursor) {
  if (cursor >= text.length) return null;
  if (text[cursor] === "{") {
    let depth = 1;
    for (let index = cursor + 1; index < text.length; index += 1) {
      if (text[index] === "{" && !isEscaped(text, index)) {
        depth += 1;
      } else if (text[index] === "}" && !isEscaped(text, index)) {
        depth -= 1;
        if (depth === 0) {
          return { text: text.slice(cursor + 1, index), end: index + 1 };
        }
      }
    }
    return null;
  }
  return { text: text[cursor], end: cursor + 1 };
}

function cleanMathText(text) {
  return String(text || "").replace(/[{}]/g, "");
}

function appendMathPart(segments, segment) {
  if (!segment.text) return;
  const previous = segments[segments.length - 1];
  if (previous && previous.type === segment.type) {
    previous.text += segment.text;
  } else {
    segments.push(segment);
  }
}

function classifyLeanWord(word) {
  if (LEAN_KEYWORDS.has(word)) return "kw";
  if (/^[A-Z]/.test(word)) return "ty";
  return "";
}

// --- Export & GitHub sharing (D34) -------------------------------------------
// Pure state derivation for the pane's Share panel, kept here (like the rest of
// this module) so the enable/disable/hint logic is unit-testable without a DOM.

/**
 * Derive the Share panel's control state.
 * @param {object} s
 * @param {boolean} s.exists - a Lea project exists for this document
 * @param {string|null} s.remoteUrl - the saved remote (from the adapter project row)
 * @param {string|undefined} s.draftRemote - the input's current value (defaults to saved)
 * @param {boolean} s.tokenConfigured - a GitHub token is set in shared settings
 * @param {boolean} [s.busy] - a save/push request is in flight
 * @returns {{canSave: boolean, canPush: boolean, hint: string}}
 */
export function deriveShareControls({ exists, remoteUrl, draftRemote, tokenConfigured, busy = false }) {
  const saved = String(remoteUrl || "").trim();
  const draft = String(draftRemote ?? saved).trim();
  if (!exists) {
    return {
      canSave: false,
      canPush: false,
      hint: "This document has no Lea project yet — formalize a theorem first."
    };
  }
  const canSave = !busy && Boolean(draft) && draft !== saved;
  const canPush = !busy && Boolean(saved) && Boolean(tokenConfigured);
  let hint = "";
  if (!saved) {
    hint = "Save a GitHub remote (https://github.com/you/repo) to enable Push.";
  } else if (!tokenConfigured) {
    hint = "Add a GitHub token in Settings to enable Push.";
  }
  return { canSave, canPush, hint };
}

// Pull `filename="…"` out of a Content-Disposition header for the zip download.
export function filenameFromContentDisposition(header, fallback = "project.zip") {
  const match = /filename="([^"]+)"/.exec(String(header || ""));
  return match ? match[1] : fallback;
}
