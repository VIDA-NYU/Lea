// Safe adapter around the vendored KaTeX browser build. Keeping configuration
// here prevents individual pane surfaces from drifting on trust, macro, or
// failure behavior.

export const LEAN_PANE_KATEX_MACROS = Object.freeze({
  "\\RR": "\\mathbb{R}",
  "\\CC": "\\mathbb{C}",
  "\\NN": "\\mathbb{N}",
  "\\QQ": "\\mathbb{Q}",
  "\\ZZ": "\\mathbb{Z}"
});

export function renderPaneMath(renderer, element, source, displayMode = false) {
  if (!renderer?.render || !element) {
    return { ok: false, error: new Error("KaTeX is unavailable.") };
  }
  try {
    renderer.render(String(source || ""), element, {
      displayMode: Boolean(displayMode),
      output: "htmlAndMathml",
      strict: "ignore",
      throwOnError: true,
      trust: false,
      maxExpand: 1000,
      macros: { ...LEAN_PANE_KATEX_MACROS }
    });
    return { ok: true, error: null };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error : new Error(String(error))
    };
  }
}
