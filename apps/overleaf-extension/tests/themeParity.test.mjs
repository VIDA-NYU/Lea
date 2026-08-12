import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION = path.resolve(HERE, "../extension");
const UI_THEME = path.resolve(HERE, "../../lea-standalone/src/styles/lea-v2.css");

function firstRootBlock(css) {
  const start = css.indexOf(":root");
  assert.notEqual(start, -1, "expected a :root token block");
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  assert.ok(open > start && close > open, "expected a complete :root token block");
  return css.slice(open + 1, close);
}

function variables(css) {
  const result = new Map();
  for (const match of css.matchAll(/--([a-z0-9-]+)\s*:\s*([^;]+);/gi)) {
    result.set(match[1], match[2].trim().toLowerCase());
  }
  return result;
}

function numericProperty(css, selector, property) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const rule = css.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
  assert.ok(rule, `expected a ${selector} rule`);
  const declaration = rule[1].match(new RegExp(`${property}\\s*:\\s*(\\d+)`));
  assert.ok(declaration, `expected ${property} in ${selector}`);
  return Number.parseInt(declaration[1], 10);
}

test("extension core theme stays in parity with the standalone Lea palette", async () => {
  const [uiCss, extensionCss] = await Promise.all([
    readFile(UI_THEME, "utf8"),
    readFile(path.join(EXTENSION, "lea-theme.css"), "utf8"),
  ]);
  const ui = variables(firstRootBlock(uiCss));
  const extension = variables(firstRootBlock(extensionCss));
  const mapping = {
    bg: "ol-bg",
    panel: "ol-panel",
    "panel-2": "ol-panel-2",
    ink: "ol-ink",
    "ink-2": "ol-ink-2",
    muted: "ol-muted",
    line: "ol-line",
    "line-2": "ol-line-2",
    accent: "ol-accent",
    "accent-soft": "ol-accent-soft",
    green: "ol-green",
    "green-soft": "ol-green-soft",
    red: "ol-red",
    "red-soft": "ol-red-soft",
    amber: "ol-amber",
    "amber-soft": "ol-amber-soft",
    "code-bg": "ol-code-bg",
    kw: "ol-code-kw",
    fn: "ol-code-fn",
    str: "ol-code-str",
    num: "ol-code-num",
    com: "ol-code-com",
    type: "ol-code-type",
    radius: "ol-radius",
  };

  for (const [uiName, extensionName] of Object.entries(mapping)) {
    assert.equal(
      extension.get(extensionName),
      ui.get(uiName),
      `${extensionName} must match standalone --${uiName}`,
    );
  }
});

test("settings controls and popover stack above the Lean project pane", async () => {
  const css = await readFile(path.join(EXTENSION, "content.css"), "utf8");
  const paneLayer = numericProperty(css, ".ol-lean-project-pane", "z-index");
  const settingsButtonLayer = numericProperty(css, ".ol-lean-settings-trigger", "z-index");
  const popoverLayer = numericProperty(css, ".ol-lean-popover", "z-index");

  assert.ok(settingsButtonLayer > paneLayer, "settings button must remain clickable above the Lean pane");
  assert.ok(popoverLayer > paneLayer, "settings popover must render above the Lean pane");
});

test("extension surfaces load the shared theme before component CSS", async () => {
  const [manifestText, optionsHtml] = await Promise.all([
    readFile(path.join(EXTENSION, "manifest.json"), "utf8"),
    readFile(path.join(EXTENSION, "options.html"), "utf8"),
  ]);
  const manifest = JSON.parse(manifestText);
  const overleafScript = manifest.content_scripts.find((entry) =>
    entry.matches?.includes("https://www.overleaf.com/project/*"),
  );

  assert.deepEqual(overleafScript?.css, [
    "vendor/katex/katex.min.css",
    "lea-theme.css",
    "modelPicker.css",
    "content.css"
  ]);
  assert.deepEqual(overleafScript?.js, [
    "vendor/katex/katex.min.js",
    "modelPicker.js",
    "content.js"
  ]);
  assert.ok(
    optionsHtml.indexOf('href="lea-theme.css"') < optionsHtml.indexOf('href="options.css"'),
    "options page must load the shared theme before its component styles",
  );
  assert.ok(
    optionsHtml.indexOf('src="modelPicker.js"') < optionsHtml.indexOf('src="options.js"'),
    "options page must load the shared model picker before its controller",
  );
});

test("retired warm-paper chrome colors do not return to extension surfaces", async () => {
  const files = ["lea-theme.css", "modelPicker.css", "content.css", "options.css"];
  const css = (
    await Promise.all(files.map((file) => readFile(path.join(EXTENSION, file), "utf8")))
  ).join("\n").toLowerCase();
  const retired = [
    "#f5f4ef",
    "#faf9f6",
    "#1f1e1d",
    "#3d3c39",
    "#8a8983",
    "#e9e7e0",
    "#efede7",
    "#c96442",
    "#f3e3db",
    "#a44f33",
    "#4f8a5b",
    "#e6f0e7",
    "#c0564a",
    "#f6e3e0",
    "#9a3f35",
    "#b8842a",
    "#f6ecd8",
    "#fbfaf7",
  ];

  for (const color of retired) {
    assert.equal(css.includes(color), false, `retired color ${color} is present`);
  }
});
