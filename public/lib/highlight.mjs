/**
 * Build-time syntax highlighting, no dependencies and no client-side JS.
 *
 * Each language is one ordered list of token rules. The tokenizer walks the
 * source once, taking the first rule that matches at the cursor, so earlier
 * rules win — comments and strings are listed first for that reason.
 */

const escapeHtml = (text) =>
  String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

const LEAN_KEYWORDS =
  /^(?:theorem|lemma|def|abbrev|example|instance|structure|inductive|class|namespace|section|end|open|import|variable|universe|noncomputable|private|protected|partial|mutual|where|deriving|attribute|macro|notation|infixl|infixr|prefix|postfix|set_option|by|do|fun|let|have|show|from|suffices|calc|match|with|if|then|else|at|using|in|forall|exists)\b/;

const LEAN_TACTICS =
  /^(?:intro|intros|rintro|rcases|obtain|exact|apply|refine|rfl|simp|simpa|norm_num|ring|ring_nf|linarith|nlinarith|positivity|omega|decide|constructor|cases|rcases|induction|rw|rwa|subst|unfold|field_simp|push_cast|push_neg|gcongr|bound|aesop|tauto|trivial|assumption|contrapose|specialize|use|exists|ext|funext|congr|convert|norm_cast|interval_cases|nlinarith)\b/;

const RULES = {
  lean: [
    [/^\/-[\s\S]*?-\//, "comment"],
    [/^--[^\n]*/, "comment"],
    [/^"(?:[^"\\]|\\.)*"/, "string"],
    [/^\b(?:sorry|native_decide|axiom)\b/, "danger"],
    [LEAN_KEYWORDS, "keyword"],
    [LEAN_TACTICS, "tactic"],
    [/^[ℝℚℤℕ𝔽ℂ∀∃¬∧∨→↔≤≥≠∈∉⊆∑∏√∫±·∘⁻¹]/u, "operator"],
    [/^:=|^<;>|^=>|^->|^<-|^\|-|^↦/, "operator"],
    [/^\b\d+(?:\.\d+)?\b/, "number"],
    [/^[A-Z][A-Za-z0-9_']*(?:\.[A-Za-z0-9_']+)*/, "type"],
  ],
  bash: [
    [/^#[^\n]*/, "comment"],
    [/^"(?:[^"\\]|\\.)*"|^'(?:[^'\\]|\\.)*'/, "string"],
    [/^\B--?[A-Za-z][\w-]*/, "flag"],
    [
      /^\b(?:git|npm|npx|node|cd|cp|mv|rm|mkdir|curl|docker|uv|lake|elan|python|pytest|sh|bash|export|echo|open|chmod|source)\b/,
      "keyword",
    ],
    [/^\$\{?[A-Za-z_][\w]*\}?/, "variable"],
    [/^[|&;><]+/, "operator"],
  ],
  tex: [
    [/^%[^\n]*/, "comment"],
    [/^\\(?:begin|end)\b/, "keyword"],
    [/^\\[A-Za-z@]+\*?/, "tactic"],
    [/^\{[^{}\n]*\}/, "string"],
    [/^[$&^_~]/, "operator"],
  ],
  json: [
    [/^"(?:[^"\\]|\\.)*"(?=\s*:)/, "type"],
    [/^"(?:[^"\\]|\\.)*"/, "string"],
    [/^\b(?:true|false|null)\b/, "keyword"],
    [/^-?\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b/, "number"],
    [/^[{}[\],:]/, "operator"],
  ],
  yaml: [
    [/^#[^\n]*/, "comment"],
    [/^[A-Za-z_][\w-]*(?=\s*:)/, "type"],
    [/^"(?:[^"\\]|\\.)*"|^'(?:[^'\\]|\\.)*'/, "string"],
    [/^\b(?:true|false|null)\b/, "keyword"],
    [/^-\s/, "operator"],
  ],
  python: [
    [/^#[^\n]*/, "comment"],
    [/^(?:"""[\s\S]*?"""|'''[\s\S]*?''')/, "string"],
    [/^"(?:[^"\\]|\\.)*"|^'(?:[^'\\]|\\.)*'/, "string"],
    [
      /^\b(?:def|class|return|import|from|as|if|elif|else|for|while|with|try|except|finally|raise|yield|async|await|lambda|pass|not|and|or|in|is|None|True|False)\b/,
      "keyword",
    ],
    [/^\b\d+(?:\.\d+)?\b/, "number"],
  ],
};

RULES.sh = RULES.bash;
RULES.shell = RULES.bash;
RULES.console = RULES.bash;
RULES.latex = RULES.tex;
RULES.yml = RULES.yaml;
RULES.py = RULES.python;
RULES.js = RULES.python; // close enough for the few JS snippets on the site
RULES.mjs = RULES.python;

/**
 * @param {string} code raw source text
 * @param {string} lang fence info string, e.g. "lean"
 * @returns {string} HTML with <span class="tok-*"> wrappers, safely escaped
 */
export function highlight(code, lang) {
  const rules = RULES[String(lang || "").toLowerCase()];
  if (!rules) return escapeHtml(code);

  let out = "";
  let rest = code;
  let plain = "";

  const flushPlain = () => {
    if (plain) {
      out += escapeHtml(plain);
      plain = "";
    }
  };

  while (rest.length) {
    let matched = false;
    for (const [pattern, cls] of rules) {
      const m = rest.match(pattern);
      if (m && m[0]) {
        flushPlain();
        out += `<span class="tok-${cls}">${escapeHtml(m[0])}</span>`;
        rest = rest.slice(m[0].length);
        matched = true;
        break;
      }
    }
    if (!matched) {
      // Consume a whole identifier at a time. Advancing one character would let
      // a later rule match mid-word — "ungit" would highlight "git".
      const word = rest.match(/^[\w'.]+/);
      const take = word ? word[0] : rest[0];
      plain += take;
      rest = rest.slice(take.length);
    }
  }
  flushPlain();
  return out;
}

export default highlight;
