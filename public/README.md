# `public/` — the Lea website

Everything public-facing lives here: the marketing site, the install guide and the
blog. It is a static site with **no dependencies** — `node build.mjs` is the whole
toolchain, so there is nothing to `npm install` before editing it.

```sh
npm run site:dev      # build + serve at http://localhost:4321 (rebuilds on save)
npm run site:build    # write public/dist for deployment
npm run site:test     # unit tests for the Markdown renderer
```

Or, from inside `public/`: `node serve.mjs --watch`, `node build.mjs`,
`node --test "tests/*.test.mjs"`.

## Layout

```
public/
  site.config.mjs      names, links, nav, hero facts   ← edit this first
  build.mjs            the generator (source → dist/)
  serve.mjs            preview server
  content/
    install.md         the install guide
    blog/*.md          one file per blog post
  lib/
    markdown.mjs       Markdown → HTML (no dependencies)
    highlight.mjs      build-time syntax highlighting
    templates.mjs      page shell: head, nav, footer
    pages/home.mjs     the home page (hand-written HTML)
  assets/              css and js — copied verbatim into dist/
  tests/               node --test suites
  dist/                build output (gitignored — never edit by hand)
```

## Writing a blog post

Add one file to `content/blog/`. The filename supplies the date and the URL:
`2026-08-11-my-post.md` publishes at `/blog/my-post/`.

```markdown
---
title: The title, in sentence case
date: 2026-08-11
author: The Lea team
tags: [engineering, design]
description: One or two sentences. Shown on the blog index and in link previews.
---

Your first paragraph.

## A section
```

- `description` is optional; without it, the first real paragraph is used.
- `draft: true` keeps a post out of the build entirely.
- `slug: something-else` overrides the URL.

Posts are sorted by `date`, newest first. Nothing else needs updating — the index,
the Atom feed, `sitemap.xml`, `api/posts.json` and `llms.txt` are all generated.

## Editing the install guide

`content/install.md` is one long Markdown document. Every `##` and `###` heading
automatically becomes an entry in the sidebar table of contents, and its anchor is
the slugified heading text — so renaming a heading changes its link.

## The Markdown subset

The renderer is deliberately small. Supported:

| Syntax | Notes |
| --- | --- |
| `# … ######` | Anchors and TOC entries generated from the text |
| Paragraphs | A single newline is a **space** — wrap your source freely. Two trailing spaces force a line break |
| `**bold**`, `_italic_`, `` `code` ``, `~~strike~~` | |
| `- item`, `1. item` | Nesting by two-space indent; a list resuming after a code block keeps its numbering |
| ` ```lean ` | Highlighting for `lean`, `bash`, `tex`, `json`, `yaml`, `python`; anything else renders plain |
| `[text](/path/)` | Site-root paths are rewritten onto the deploy base path automatically |
| `![alt](/assets/img/x.png)` | |
| Tables | GitHub style, with `:---:` alignment |
| `> quote` | `> **Note.** …` or `> **Warning.** …` renders as a coloured callout |
| Raw HTML blocks | Passed through when a line starts with a block tag |

Not supported, on purpose: reference links, footnotes, setext headings, and `$math$`
(a `$` is a literal `$`). If you need something here, add it to `lib/markdown.mjs`
**and** to `tests/markdown.test.mjs` — the tests are the specification.

## Changing what the site says about itself

`site.config.mjs` holds the name, tagline, description, all outbound links, the nav
bar, and the four facts in the hero strip. Two things there still need filling in:

```js
lab: {
  name: null,         // e.g. "The Formal Methods Group"
  institution: null,  // e.g. "University of ..."
  contact: null,      // e.g. "lea@example.edu"
}
```

They render in the footer once set, and are skipped while `null`.

The home page itself is `lib/pages/home.mjs` — hand-written HTML rather than
Markdown, because its layout carries the argument. The architecture diagram is an
inline SVG in that same file and follows the colour theme automatically.

## Deploying

`.github/workflows/site.yml` builds and publishes to GitHub Pages on every push to
`main` that touches `public/`.

**One-time setup:** a repository admin must turn Pages on under **Settings → Pages →
Source → GitHub Actions**. Until then the workflow fails at `configure-pages` with
*"Get Pages site failed … Not Found"*. The workflow cannot do this for you — creating
a Pages site over the API is outside what `GITHUB_TOKEN` is permitted to do.

The site is built for a project page at `/LeaUIOverleafEcosystem/`. If you move it to a
custom domain, set `origin` in `site.config.mjs` and build with `--base=/`:

```sh
node build.mjs --base=/ --origin=https://lea.example.org
```

`dist/` is self-contained static files — any host will serve it.
