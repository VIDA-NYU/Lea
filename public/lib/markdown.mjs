/**
 * A small, dependency-free Markdown renderer.
 *
 * It covers the subset this site actually writes in — headings, paragraphs,
 * lists, fenced code, tables, blockquotes, rules, images, links, emphasis and
 * raw HTML blocks. It is deliberately not CommonMark: keeping it small means
 * the site builds with `node build.mjs` and no install step. `public/README.md`
 * documents the supported syntax; `public/tests/markdown.test.mjs` pins it.
 */

import { highlight } from "./highlight.mjs";

const SENTINEL = "\u0000";

export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function slugify(text) {
  return String(text)
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/[`*_~]/g, "")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

/* ------------------------------------------------------------------ inline */

/**
 * Render inline markup. The text is HTML-escaped first, so every transform
 * below runs against already-safe text and can only add markup we emit.
 */
function inline(raw, ctx) {
  let text = escapeHtml(raw);

  // Code spans are extracted before anything else so their contents never see
  // the emphasis or link passes. They come back at the end.
  const codes = [];
  text = text.replace(/(`+)([\s\S]+?)\1/g, (_m, _ticks, code) => {
    codes.push(code.replace(/^ (.*) $/, "$1"));
    return `${SENTINEL}C${codes.length - 1}${SENTINEL}`;
  });

  // Images before links — the syntax differs only by the leading "!".
  text = text.replace(
    /!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"([^"]*)")?\s*\)/g,
    (_m, alt, src, title) => {
      const t = title ? ` title="${title}"` : "";
      return `<img src="${resolve(src, ctx)}" alt="${alt}"${t} loading="lazy" decoding="async">`;
    },
  );

  text = text.replace(
    /\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"([^"]*)")?\s*\)/g,
    (_m, label, href, title) => {
      const t = title ? ` title="${title}"` : "";
      const url = resolve(href, ctx);
      const ext = /^https?:/.test(url) ? ' target="_blank" rel="noopener"' : "";
      return `<a href="${url}"${t}${ext}>${label}</a>`;
    },
  );

  // Autolinks: <https://example.com>, escaped to &lt;…&gt; by now.
  text = text.replace(
    /&lt;(https?:\/\/[^\s&]+)&gt;/g,
    '<a href="$1" target="_blank" rel="noopener">$1</a>',
  );

  text = text.replace(/\*\*([^\s*][\s\S]*?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^\w*])\*([^\s*][^*]*?)\*(?![\w*])/g, "$1<em>$2</em>");
  text = text.replace(/(^|[^\w_])_([^\s_][^_]*?)_(?![\w_])/g, "$1<em>$2</em>");
  text = text.replace(/~~([\s\S]+?)~~/g, "<del>$1</del>");

  // No smart-punctuation pass: it would also rewrite the insides of the href
  // attributes generated just above. Type — and – directly instead.

  return text.replace(
    new RegExp(`${SENTINEL}C(\\d+)${SENTINEL}`, "g"),
    (_m, i) => `<code>${codes[Number(i)]}</code>`,
  );
}

/** Rewrite site-root links ("/blog/") onto the configured base path. */
function resolve(href, ctx) {
  if (ctx?.base && href.startsWith("/") && !href.startsWith("//")) {
    return ctx.base.replace(/\/$/, "") + href;
  }
  return href;
}

/* ------------------------------------------------------------------- blocks */

const BLOCK_TAGS =
  /^<(\/?)(?:div|section|figure|figcaption|table|thead|tbody|tr|td|th|ul|ol|li|p|h[1-6]|pre|blockquote|details|summary|aside|nav|iframe|img|hr|video|picture|source)\b/i;

/**
 * @param {string} markdown
 * @param {{base?: string, headingOffset?: number}} [options]
 * @returns {{html: string, headings: Array<{level: number, text: string, id: string}>}}
 */
export function render(markdown, options = {}) {
  const ctx = { base: options.base || "", headingOffset: options.headingOffset || 0 };
  const lines = String(markdown).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  const headings = [];
  const usedIds = new Map();

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    // Fenced code -------------------------------------------------------
    const fence = line.match(/^(\s*)(`{3,}|~{3,})\s*([\w+-]*)\s*$/);
    if (fence) {
      const [, , marker, lang] = fence;
      const close = new RegExp(`^\\s*${marker[0]}{${marker.length},}\\s*$`);
      const body = [];
      i += 1;
      while (i < lines.length && !close.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // consume the closing fence
      const code = body.join("\n");
      const cls = lang ? ` class="language-${lang}"` : "";
      out.push(
        `<figure class="code"><pre${cls}><code>${highlight(code, lang)}</code></pre>` +
          `<button class="copy" type="button" data-copy aria-label="Copy code">copy</button></figure>`,
      );
      continue;
    }

    // ATX heading -------------------------------------------------------
    const heading = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (heading) {
      const level = Math.min(6, heading[1].length + ctx.headingOffset);
      const text = heading[2];
      let id = slugify(text);
      if (usedIds.has(id)) {
        const n = usedIds.get(id) + 1;
        usedIds.set(id, n);
        id = `${id}-${n}`;
      } else {
        usedIds.set(id, 0);
      }
      headings.push({ level, text: text.replace(/[`*_]/g, ""), id });
      out.push(
        `<h${level} id="${id}">${inline(text, ctx)}` +
          `<a class="anchor" href="#${id}" aria-label="Link to this section">#</a></h${level}>`,
      );
      i += 1;
      continue;
    }

    // Thematic break ----------------------------------------------------
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push("<hr>");
      i += 1;
      continue;
    }

    // Table -------------------------------------------------------------
    if (line.includes("|") && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1] || "")) {
      const header = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map((cell) => {
        const left = cell.startsWith(":");
        const right = cell.endsWith(":");
        if (left && right) return "center";
        if (right) return "right";
        return left ? "left" : "";
      });
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const th = header
        .map((cell, n) => `<th${attrAlign(aligns[n])}>${inline(cell, ctx)}</th>`)
        .join("");
      const tb = rows
        .map(
          (row) =>
            "<tr>" +
            row
              .map((cell, n) => `<td${attrAlign(aligns[n])}>${inline(cell, ctx)}</td>`)
              .join("") +
            "</tr>",
        )
        .join("");
      out.push(
        `<div class="table-scroll"><table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table></div>`,
      );
      continue;
    }

    // Blockquote / callout ----------------------------------------------
    if (/^\s*>/.test(line)) {
      const body = [];
      while (i < lines.length && (/^\s*>/.test(lines[i]) || (body.length && lines[i].trim()))) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const inner = render(body.join("\n"), options).html;
      // `> **Note.** …` renders as a labelled callout.
      const kind = body[0]?.match(/^\*\*(Note|Warning|Tip|Caution)\b/i);
      const cls = kind ? ` class="callout callout-${kind[1].toLowerCase()}"` : "";
      out.push(`<blockquote${cls}>${inner}</blockquote>`);
      continue;
    }

    // Lists ---------------------------------------------------------------
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const block = [];
      while (i < lines.length) {
        const l = lines[i];
        if (!l.trim()) {
          // A blank line ends the list unless the next line continues it.
          const next = lines[i + 1] || "";
          if (!/^\s*([-*+]|\d+[.)])\s+/.test(next) && !/^\s{2,}\S/.test(next)) break;
          block.push("");
          i += 1;
          continue;
        }
        if (!/^\s*([-*+]|\d+[.)])\s+/.test(l) && !/^\s{2,}\S/.test(l)) break;
        block.push(l);
        i += 1;
      }
      out.push(renderList(block, ctx, options));
      continue;
    }

    // Raw HTML block ------------------------------------------------------
    if (BLOCK_TAGS.test(line.trim())) {
      const body = [];
      while (i < lines.length && lines[i].trim()) {
        body.push(lines[i]);
        i += 1;
      }
      out.push(body.join("\n"));
      continue;
    }

    // Paragraph -----------------------------------------------------------
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6})\s/.test(lines[i]) &&
      !/^\s*>/.test(lines[i]) &&
      !/^(\s*)(`{3,}|~{3,})/.test(lines[i]) &&
      !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) &&
      !BLOCK_TAGS.test(lines[i].trim())
    ) {
      para.push(lines[i]);
      i += 1;
    }
    if (para.length) {
      // A single newline is just a space — source files wrap for readability.
      // Two or more trailing spaces before it force a hard break, as in Markdown.
      const text = inline(para.join("\n").trim(), ctx)
        .replace(/ {2,}\n/g, "<br>\n")
        .replace(/\n/g, " ");
      out.push(`<p>${text}</p>`);
    } else {
      i += 1;
    }
  }

  return { html: out.join("\n"), headings };
}

function attrAlign(align) {
  return align ? ` style="text-align:${align}"` : "";
}

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/** Build a (possibly nested) list from a run of list lines. */
function renderList(block, ctx, options) {
  const items = [];
  let current = null;
  let baseIndent = null;

  for (const line of block) {
    const match = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (match) {
      const indent = match[1].length;
      if (baseIndent === null) baseIndent = indent;
      if (indent > baseIndent && current) {
        current.children.push(line.slice(baseIndent + 2));
        continue;
      }
      current = { marker: match[2], text: match[3], children: [] };
      items.push(current);
      continue;
    }
    if (!current) continue;
    // Continuation line: either nested content or a wrapped paragraph.
    current.children.push(line.replace(new RegExp(`^\\s{0,${(baseIndent ?? 0) + 2}}`), ""));
  }

  const ordered = /\d/.test(items[0]?.marker || "-");
  // A fenced block between two numbered items splits them into separate lists,
  // so carry the author's own numbering rather than restarting at 1.
  const firstNumber = ordered ? (items[0].marker.match(/^(\d+)/) || [])[1] : null;
  const startAttr = firstNumber && firstNumber !== "1" ? ` start="${firstNumber}"` : "";

  const html = items
    .map((item) => {
      const nested = item.children.join("\n").trim();
      const task = item.text.match(/^\[([ xX])\]\s+(.*)$/);
      const body = task
        ? `<span class="task ${task[1] === " " ? "todo" : "done"}">${inline(task[2], ctx)}</span>`
        : inline(item.text, ctx);
      const sub = nested ? "\n" + render(nested, options).html : "";
      return `<li${task ? ' class="task-item"' : ""}>${body}${sub}</li>`;
    })
    .join("\n");

  return ordered ? `<ol${startAttr}>${html}</ol>` : `<ul>${html}</ul>`;
}

/* -------------------------------------------------------------- frontmatter */

/**
 * Parse a leading `---` frontmatter block. Values are plain strings; a value
 * written as `[a, b]` becomes an array. No YAML dependency, no surprises.
 */
export function parseFrontmatter(source) {
  const text = String(source).replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
  const match = text.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!match) return { data: {}, body: text };

  const data = {};
  for (const line of match[1].split("\n")) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    const at = line.indexOf(":");
    if (at === -1) continue;
    const key = line.slice(0, at).trim();
    let value = line.slice(at + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    } else if (value.startsWith("[") && value.endsWith("]")) {
      data[key] = value
        .slice(1, -1)
        .split(",")
        .map((v) => v.trim().replace(/^["']|["']$/g, ""))
        .filter(Boolean);
      continue;
    }
    data[key] = value;
  }
  return { data, body: text.slice(match[0].length) };
}

/** First paragraph of a post body, as plain text — used for previews. */
export function excerpt(body, limit = 220) {
  const plain = body
    .replace(/^---[\s\S]*?---/, "")
    .replace(/```[\s\S]*?```/g, "")
    .replace(/^#{1,6}\s+.*$/gm, "")
    .replace(/^\s*>.*$/gm, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*_#]/g, "")
    .trim();
  const first = plain.split(/\n\s*\n/).find((p) => p.trim().length > 40) || plain;
  const text = first.replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit).replace(/\s+\S*$/, "") + "…" : text;
}
