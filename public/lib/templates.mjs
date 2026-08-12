/**
 * Page shell and shared chrome. Every page on the site goes through `layout`.
 *
 * Templates take an explicit `ctx` (see `makeCtx`) rather than reaching for the
 * config directly, so the base path can be overridden at build time and the
 * templates stay testable.
 */

import site from "../site.config.mjs";

/**
 * @param {{base?: string, origin?: string}} [overrides]
 */
export function makeCtx(overrides = {}) {
  const base = (overrides.base ?? site.base).replace(/\/*$/, "/");
  const origin = (overrides.origin ?? site.origin).replace(/\/$/, "");
  return {
    site,
    base,
    origin,
    /** Resolve a site-root path ("/blog/") against the deploy base path. */
    url(href = "/") {
      if (/^(https?:|mailto:|#)/.test(href)) return href;
      return base + String(href).replace(/^\//, "");
    },
    /** Absolute URL, for feeds, sitemaps and social tags. */
    absolute(href = "/") {
      if (/^https?:/.test(href)) return href;
      return origin + base + String(href).replace(/^\//, "");
    },
  };
}

export const LOGO = `<svg class="mark" viewBox="0 0 40 40" aria-hidden="true" focusable="false">
  <circle cx="20" cy="20" r="20"/>
  <text x="20" y="20" text-anchor="middle" dominant-baseline="central">Lea</text>
</svg>`;

function head(ctx, opts) {
  const title = opts.title
    ? `${opts.title} — ${ctx.site.name}`
    : `${ctx.site.name} — ${ctx.site.tagline}`;
  const description = opts.description || ctx.site.description;
  const canonical = ctx.absolute(opts.path || "/");

  // No og:image: the site ships no raster art, and pointing at a file that
  // isn't there renders worse in a link preview than having no card image.
  return `<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeAttr(title)}</title>
<meta name="description" content="${escapeAttr(description)}">
<link rel="canonical" href="${canonical}">
<meta property="og:type" content="${opts.ogType || "website"}">
<meta property="og:title" content="${escapeAttr(title)}">
<meta property="og:description" content="${escapeAttr(description)}">
<meta property="og:url" content="${canonical}">
<meta name="twitter:card" content="summary">
<link rel="icon" href="${ctx.url("/assets/img/favicon.svg")}" type="image/svg+xml">
<link rel="alternate" type="application/atom+xml" title="${escapeAttr(ctx.site.name)} blog" href="${ctx.url("/feed.xml")}">
<link rel="stylesheet" href="${ctx.url("/assets/css/site.css")}">
<script>
  // Applied before first paint so the page never flashes the wrong theme.
  try {
    var saved = localStorage.getItem("lea-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (e) {}
</script>${opts.extraHead || ""}`;
}

function nav(ctx, currentPath) {
  const items = ctx.site.nav
    .map((item) => {
      const active =
        !item.external && currentPath && currentPath.startsWith(item.href) ? ' class="active"' : "";
      const rel = item.external ? ' target="_blank" rel="noopener"' : "";
      return `<a href="${ctx.url(item.href)}"${active}${rel}>${item.label}</a>`;
    })
    .join("");

  return `<nav class="nav" aria-label="Primary">
  ${items}
  <button class="theme-toggle" type="button" data-theme-toggle aria-label="Switch colour theme">
    <span class="theme-icon" aria-hidden="true"></span>
  </button>
</nav>`;
}

function header(ctx, currentPath) {
  return `<header class="site-header">
  <div class="wrap header-inner">
    <a class="brand" href="${ctx.url("/")}">
      ${LOGO}
      <span class="brand-text">${ctx.site.name}</span>
    </a>
    ${nav(ctx, currentPath)}
  </div>
</header>`;
}

function footer(ctx) {
  const { links, lab } = ctx.site;
  const labLine = lab.name
    ? `<p class="lab">${escapeHtml(lab.name)}${lab.institution ? ` · ${escapeHtml(lab.institution)}` : ""}</p>`
    : "";
  const contact = lab.contact
    ? `<li><a href="mailto:${escapeAttr(lab.contact)}">${escapeHtml(lab.contact)}</a></li>`
    : "";

  return `<footer class="site-footer">
  <div class="wrap footer-inner">
    <div class="footer-brand">
      <a class="brand" href="${ctx.url("/")}">${LOGO}<span class="brand-text">${ctx.site.name}</span></a>
      <p class="footer-tagline">${escapeHtml(ctx.site.tagline)}</p>
      ${labLine}
    </div>
    <div class="footer-cols">
      <div>
        <h3>Project</h3>
        <ul>
          <li><a href="${ctx.url("/install/")}">Install guide</a></li>
          <li><a href="${ctx.url("/blog/")}">Blog</a></li>
          <li><a href="${links.github}" target="_blank" rel="noopener">Ecosystem repo</a></li>
          <li><a href="${links.githubProver}" target="_blank" rel="noopener">Prover repo</a></li>
        </ul>
      </div>
      <div>
        <h3>Community</h3>
        <ul>
          <li><a href="${links.discord}" target="_blank" rel="noopener">Discord</a></li>
          <li><a href="${links.issues}" target="_blank" rel="noopener">Issues</a></li>
          ${contact}
        </ul>
      </div>
      <div>
        <h3>Machine-readable</h3>
        <ul>
          <li><a href="${ctx.url("/feed.xml")}">Atom feed</a></li>
          <li><a href="${ctx.url("/api/posts.json")}">posts.json</a></li>
          <li><a href="${ctx.url("/llms.txt")}">llms.txt</a></li>
        </ul>
      </div>
    </div>
  </div>
  <div class="wrap footer-legal">
    <span>Built for mathematicians. Your proofs, sessions and history stay on your machine.</span>
  </div>
</footer>`;
}

/**
 * @param {ReturnType<makeCtx>} ctx
 * @param {{title?: string, description?: string, path?: string, bodyClass?: string,
 *          main: string, extraHead?: string, ogType?: string}} opts
 */
export function layout(ctx, opts) {
  return `<!doctype html>
<html lang="en">
<head>
${head(ctx, opts)}
</head>
<body${opts.bodyClass ? ` class="${opts.bodyClass}"` : ""}>
<a class="skip-link" href="#main">Skip to content</a>
${header(ctx, opts.path)}
<main id="main">
${opts.main}
</main>
${footer(ctx)}
<script src="${ctx.url("/assets/js/site.js")}" defer></script>
</body>
</html>
`;
}

export function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function escapeAttr(text) {
  return escapeHtml(text).replace(/"/g, "&quot;");
}

/** "2026-08-11" → "August 11, 2026" (UTC, so the date never shifts). */
export function formatDate(value) {
  const date = new Date(`${value}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}
