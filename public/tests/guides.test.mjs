import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const build = fileURLToPath(new URL("../build.mjs", import.meta.url));

for (const base of ["/Lea/", "/"]) {
  test(`publishes the Overleaf guide and its install link with base ${base}`, (t) => {
    const out = fs.mkdtempSync(path.join(os.tmpdir(), "lea-site-guides-"));
    t.after(() => fs.rmSync(out, { recursive: true, force: true }));
    execFileSync(process.execPath, [build, `--out=${out}`, `--base=${base}`]);
    const read = (file) => fs.readFileSync(path.join(out, file), "utf8");
    const guide = read("overleaf/index.html");
    const install = read("install/index.html");
    const section = install.split('<h2 id="overleaf-extension">')[1]
      .split('<h2 id="your-first-proof">')[0];
    const guideLink = `href="${base}overleaf/"`;

    assert.ok(section.includes(guideLink), "install section links to the published guide");
    assert.ok(section.indexOf(guideLink) > section.indexOf("</ol>"),
      "guide link follows the extension installation steps");
    assert.match(guide, /<h1>Overleaf extension usage guide<\/h1>/);
    assert.ok(guide.includes(`href="${base}install/#overleaf-extension"`));
    assert.ok(guide.includes('href="#formalize-an-item"'));
    assert.ok(guide.includes('id="formalize-an-item"'));
    assert.ok(guide.includes(`href="https://vida-nyu.github.io${base}overleaf/"`),
      "the guide has its own canonical URL");
    assert.ok(read("sitemap.xml").includes(`https://vida-nyu.github.io${base}overleaf/`));
    assert.ok(read("llms.txt").includes(`https://vida-nyu.github.io${base}overleaf/`));
  });
}
