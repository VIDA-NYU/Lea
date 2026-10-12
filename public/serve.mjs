#!/usr/bin/env node
/**
 * Preview server: rebuilds the site, then serves it at http://localhost:4321.
 *
 *   node serve.mjs            build once and serve
 *   node serve.mjs --watch    rebuild whenever a source file changes
 *
 * The preview always builds with base "/" so links work off a bare localhost.
 */

import { createServer } from "node:http";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.join(ROOT, "dist");
const PORT = Number(process.env.PORT || 4321);
const watch = process.argv.includes("--watch");

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".xml": "application/xml; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".woff2": "font/woff2",
};

function build() {
  const result = spawnSync(process.execPath, [path.join(ROOT, "build.mjs"), "--base=/"], {
    stdio: "inherit",
  });
  if (result.status !== 0) console.error("build failed");
}

build();

if (watch) {
  let timer = null;
  for (const dir of ["lib", "content", "assets"]) {
    fs.watch(path.join(ROOT, dir), { recursive: true }, () => {
      clearTimeout(timer);
      timer = setTimeout(build, 120);
    });
  }
  fs.watch(ROOT, (_event, file) => {
    if (file === "build.mjs" || file === "site.config.mjs") {
      clearTimeout(timer);
      timer = setTimeout(build, 120);
    }
  });
}

createServer((req, res) => {
  const url = decodeURIComponent((req.url || "/").split("?")[0]);
  let file = path.join(DIST, url);

  // Directory URLs resolve to their index.html.
  if (!path.extname(file)) file = path.join(file, "index.html");

  // Never serve outside dist.
  if (!file.startsWith(DIST)) {
    res.writeHead(403).end("forbidden");
    return;
  }

  fs.readFile(file, (error, body) => {
    if (error) {
      fs.readFile(path.join(DIST, "404.html"), (_e, notFound) => {
        res.writeHead(404, { "content-type": TYPES[".html"] });
        res.end(notFound || "not found");
      });
      return;
    }
    res.writeHead(200, {
      "content-type": TYPES[path.extname(file)] || "application/octet-stream",
      "cache-control": "no-cache",
    });
    res.end(body);
  });
}).listen(PORT, () => {
  console.log(`\n  Lea site → http://localhost:${PORT}${watch ? "  (watching)" : ""}\n`);
});
