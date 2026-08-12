// Lean 4 syntax highlighting for the canvas, via Shiki (#11).
//
// Shiki ships the real Lean 4 TextMate grammar (the same family VS Code uses), so
// this replaces the old hand-rolled regex tokenizer — block comments, tactics,
// attributes, unicode, etc. all highlight correctly. We use the fine-grained
// bundle (Shiki core + the pure-JS regex engine, no WASM) with a single grammar
// and theme, lazily initialized once per session so it stays off the initial load.
//
// The canvas renders per-line diff rows, so we expose *tokens per line* (not HTML):
// `codeToTokens` gives `ThemedToken[][]` aligned to the source lines, which slots
// straight into the existing gutter/added-line rendering. Colors are inline from
// the theme, so no CSS-var palette to maintain.
//
// Everything Shiki (core + engine + grammar + theme) is imported dynamically inside
// `ensureHighlighter`, so it code-splits into its own chunk and stays entirely off
// the app's initial load — it's fetched only when the canvas first shows Lean code.

// A restrained light theme that reads well on the neutral code surface.
export const LEAN_THEME = 'vitesse-light';
const LEAN_LANG = 'lean';

let _highlighter = null; // the resolved highlighter — set once ready (sync access)
let _initPromise = null; // the in-flight init, so we create exactly one highlighter

/**
 * Kick off (or reuse) the one-time Shiki highlighter init. Resolves to the
 * highlighter; also caches it in `_highlighter` for synchronous use afterward.
 * `forgiving` keeps a stray unsupported grammar regex from throwing — it just
 * skips that pattern rather than blanking the whole file.
 */
export function ensureHighlighter() {
  if (_initPromise) return _initPromise;
  _initPromise = (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([
      import('shiki/core'),
      import('shiki/engine/javascript'),
    ]);
    const hl = await createHighlighterCore({
      themes: [import('@shikijs/themes/vitesse-light')],
      langs: [import('@shikijs/langs/lean')],
      engine: createJavaScriptRegexEngine({ forgiving: true }),
    });
    _highlighter = hl;
    return hl;
  })().catch((err) => {
    _initPromise = null; // allow a retry on the next mount
    throw err;
  });
  return _initPromise;
}

/** True once the highlighter is loaded (so the canvas can render plain until then). */
export function isHighlighterReady() {
  return _highlighter != null;
}

/**
 * Tokenize `code` into per-line token arrays (`ThemedToken[][]`), or null if the
 * highlighter isn't loaded yet (caller renders plain text meanwhile). Each token is
 * `{ content, color, fontStyle }`; concatenating a line's token contents
 * reproduces that source line exactly, so it aligns with the canvas's line rows.
 */
export function highlightToLines(code) {
  if (!_highlighter || !code) return null;
  try {
    return _highlighter.codeToTokens(code, {
      lang: LEAN_LANG,
      theme: LEAN_THEME,
      includeExplanation: false,
    }).tokens;
  } catch {
    return null; // never let a highlighting hiccup break the canvas
  }
}
