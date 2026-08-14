#!/usr/bin/env node
/**
 * Static site generator for the Lea site. No dependencies — `node build.mjs`.
 *
 *   node build.mjs                 build to public/dist with the configured base
 *   node build.mjs --base=/        build for a custom domain or local preview
 *   node build.mjs --out=../site   build somewhere else
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { render, parseFrontmatter, excerpt } from "./lib/markdown.mjs";
import { layout, makeCtx, escapeHtml, escapeAttr, formatDate } from "./lib/templates.mjs";
import { homePage } from "./lib/pages/home.mjs";
import { WORDMARK_PATH, WORDMARK_VIEWBOX } from "./lib/wordmark.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));

const args = Object.fromEntries(
  process.argv.slice(2).map((arg) => {
    const [key, value = "true"] = arg.replace(/^--/, "").split("=");
    return [key, value];
  }),
);

const OUT = path.resolve(ROOT, args.out || "dist");
const ctx = makeCtx({ base: args.base, origin: args.origin });

/* ----------------------------------------------------------------- helpers */

function write(relativePath, contents) {
  const target = path.join(OUT, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, contents);
  return relativePath;
}

function copyDir(from, to) {
  if (!fs.existsSync(from)) return;
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const src = path.join(from, entry.name);
    const dest = path.join(to, entry.name);
    if (entry.isDirectory()) copyDir(src, dest);
    else fs.copyFileSync(src, dest);
  }
}

function readMarkdown(file) {
  const { data, body } = parseFrontmatter(fs.readFileSync(file, "utf8"));
  return { data, body };
}

/** Build a nested table of contents from h2/h3 headings. */
function tocHtml(headings) {
  const items = headings
    .filter((h) => h.level === 2 || h.level === 3)
    .map(
      (h) =>
        `<li class="lvl-${h.level}"><a href="#${h.id}">${escapeHtml(h.text)}</a></li>`,
    )
    .join("\n");
  if (!items) return "";
  return `<nav class="toc" aria-label="On this page"><h2>On this page</h2><ul>${items}</ul></nav>`;
}

/* -------------------------------------------------------------------- clean */

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

/* ------------------------------------------------------------------ assets */

copyDir(path.join(ROOT, "assets"), path.join(OUT, "assets"));

// Favicon: the wordmark, drawn to survive being 16px tall. It carries the
// outlined glyphs rather than live text — a favicon cannot load the webfont,
// and the ramp is skipped here because it turns to mud at this size.
write(
  "assets/img/favicon.svg",
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">
  <rect width="40" height="40" rx="9" fill="#191814"/>
  <svg x="6" y="13.6" width="28" height="12.4" viewBox="${WORDMARK_VIEWBOX}"
       preserveAspectRatio="xMidYMid meet">
    <path fill="#fbfaf7" d="${WORDMARK_PATH}"/>
  </svg>
</svg>
`,
);

// GitHub Pages otherwise runs the output through Jekyll.
write(".nojekyll", "");

/* -------------------------------------------------------------------- home */

write("index.html", layout(ctx, { path: "/", main: homePage(ctx) }));

/* ----------------------------------------------------------- install guide */

{
  const { data, body } = readMarkdown(path.join(ROOT, "content", "install.md"));
  const { html, headings } = render(body, { base: ctx.base });
  const main = `<div class="wrap guide">
  ${tocHtml(headings)}
  <article class="guide-body prose">
    <h1>${escapeHtml(data.title || "Install")}</h1>
    <p class="lead">${escapeHtml(data.description || "")}</p>
    <hr>
    ${html}
  </article>
</div>`;
  write(
    "install/index.html",
    layout(ctx, {
      path: "/install/",
      title: data.title,
      description: data.description,
      main,
    }),
  );
}

/* -------------------------------------------------------------------- blog */

const postsDir = path.join(ROOT, "content", "blog");
const posts = fs
  .readdirSync(postsDir)
  .filter((name) => name.endsWith(".md"))
  .map((name) => {
    const { data, body } = readMarkdown(path.join(postsDir, name));
    const slug = data.slug || name.replace(/^\d{4}-\d{2}-\d{2}-/, "").replace(/\.md$/, "");
    return {
      slug,
      href: `/blog/${slug}/`,
      title: data.title || slug,
      date: data.date || name.slice(0, 10),
      author: data.author || "The Lea team",
      tags: Array.isArray(data.tags) ? data.tags : data.tags ? [data.tags] : [],
      description: data.description || excerpt(body),
      body,
      draft: data.draft === "true",
    };
  })
  .filter((post) => !post.draft)
  .sort((a, b) => (a.date < b.date ? 1 : -1));

for (const [index, post] of posts.entries()) {
  const { html } = render(post.body, { base: ctx.base });
  const newer = posts[index - 1];
  const older = posts[index + 1];

  const tags = post.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
  const nextPrev =
    newer || older
      ? `<hr>
<nav class="post-nav" aria-label="More posts">
  ${older ? `<p><span class="post-meta">Earlier</span><br><a href="${ctx.url(older.href)}">${escapeHtml(older.title)}</a></p>` : ""}
  ${newer ? `<p><span class="post-meta">Later</span><br><a href="${ctx.url(newer.href)}">${escapeHtml(newer.title)}</a></p>` : ""}
</nav>`
      : "";

  const main = `<div class="wrap page-narrow">
  <header class="post-header">
    <p class="post-meta">${formatDate(post.date)} · ${escapeHtml(post.author)}</p>
    <h1>${escapeHtml(post.title)}</h1>
    <p class="lead">${escapeHtml(post.description)}</p>
    <div class="tags" style="margin-top:1rem">${tags}</div>
  </header>
  <article class="prose">
    ${html}
    ${nextPrev}
  </article>
</div>`;

  write(
    `blog/${post.slug}/index.html`,
    layout(ctx, {
      path: post.href,
      title: post.title,
      description: post.description,
      ogType: "article",
      main,
    }),
  );
}

{
  const items = posts
    .map(
      (post) => `<li class="post-item">
    <div class="post-meta">${formatDate(post.date)}</div>
    <div>
      <h2><a href="${ctx.url(post.href)}">${escapeHtml(post.title)}</a></h2>
      <p>${escapeHtml(post.description)}</p>
      <div class="tags">${post.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
    </div>
  </li>`,
    )
    .join("\n");

  const main = `<div class="wrap page-narrow">
  <header class="section-head">
    <p class="kicker">Blog</p>
  </header>
  <ul class="post-list">
${items}
  </ul>
</div>`;

  write(
    "blog/index.html",
    layout(ctx, {
      path: "/blog/",
      title: "Blog",
      description: `Design notes and engineering write-ups from the ${ctx.site.name} team.`,
      main,
    }),
  );
}

/* ---------------------------------------------------------- machine-readable */

const updated = posts[0] ? `${posts[0].date}T12:00:00Z` : "2026-01-01T00:00:00Z";

write(
  "feed.xml",
  `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>${escapeHtml(ctx.site.fullName)}</title>
  <subtitle>${escapeHtml(ctx.site.tagline)}</subtitle>
  <link href="${ctx.absolute("/feed.xml")}" rel="self"/>
  <link href="${ctx.absolute("/")}"/>
  <updated>${updated}</updated>
  <id>${ctx.absolute("/")}</id>
${posts
  .map(
    (post) => `  <entry>
    <title>${escapeHtml(post.title)}</title>
    <link href="${ctx.absolute(post.href)}"/>
    <id>${ctx.absolute(post.href)}</id>
    <updated>${post.date}T12:00:00Z</updated>
    <author><name>${escapeHtml(post.author)}</name></author>
    <summary>${escapeHtml(post.description)}</summary>
  </entry>`,
  )
  .join("\n")}
</feed>
`,
);

write(
  "sitemap.xml",
  `<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${["/", "/install/", "/blog/"]
  .concat(posts.map((post) => post.href))
  .map((href) => `  <url><loc>${ctx.absolute(href)}</loc></url>`)
  .join("\n")}
</urlset>
`,
);

write("robots.txt", `User-agent: *\nAllow: /\n\nSitemap: ${ctx.absolute("/sitemap.xml")}\n`);

write(
  "api/posts.json",
  JSON.stringify(
    {
      site: ctx.site.name,
      url: ctx.absolute("/"),
      posts: posts.map(({ title, date, author, tags, description, href }) => ({
        title,
        date,
        author,
        tags,
        description,
        url: ctx.absolute(href),
      })),
    },
    null,
    2,
  ) + "\n",
);

// A plain-text brief for language models and anyone who wants the summary fast.
write(
  "llms.txt",
  `# ${ctx.site.fullName}

> ${ctx.site.tagline}

${ctx.site.description}

Lea is a Lean 4 agent backbone. The prover runs in-process behind one application-neutral
API exposing runs, sessions and a stream of typed events. Two applications ship on it:
LeaChat (standalone web client) and LeaOverleaf (Chrome extension + local companion that
formalizes theorems marked in an Overleaf document).

Design commitments:
- The mathematician steers decomposition, intervenes mid-proof, and reviews each claim.
- "Proved" (the file elaborates) is never collapsed into "verified" (SafeVerify kernel
  replay, per-declaration type/body match, axiom whitelist).
- Status is derived from the latest Lean verdict, never stored.
- Extension points are plain files: skills (markdown), sub-agent roles (YAML), tools and
  MCP servers (one registry).

## Pages
- Home: ${ctx.absolute("/")}
- Install guide: ${ctx.absolute("/install/")}
- Blog: ${ctx.absolute("/blog/")}
- Source: ${ctx.site.links.github}
- Community: ${ctx.site.links.discord}

## Posts
${posts.map((post) => `- [${post.date}] ${post.title} — ${ctx.absolute(post.href)}\n  ${post.description}`).join("\n")}
`,
);

/* --------------------------------------------------------------------- 404 */

write(
  "404.html",
  layout(ctx, {
    path: "/404.html",
    title: "Page not found",
    description: "That page does not exist.",
    main: `<div class="wrap page-narrow prose" style="text-align:center">
  <p class="kicker">404</p>
  <h1>No such page</h1>
  <p class="lead">The proof of this page's existence does not compile.</p>
  <p><a class="btn btn-primary" href="${ctx.url("/")}">Back to the home page</a></p>
</div>`,
  }),
);

/* ------------------------------------------------------------------ summary */

const files = [];
(function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else files.push(path.relative(OUT, full));
  }
})(OUT);

const pages = files.filter((f) => f.endsWith(".html")).length;
console.log(`Built ${pages} pages, ${files.length} files → ${path.relative(process.cwd(), OUT)}`);
console.log(`Base path: ${ctx.base}   Origin: ${ctx.origin}`);
console.log(`Blog posts: ${posts.length}`);
