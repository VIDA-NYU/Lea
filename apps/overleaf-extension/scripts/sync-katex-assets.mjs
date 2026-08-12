import { constants as fsConstants } from "node:fs";
import {
  access,
  copyFile,
  mkdir,
  readFile,
  readdir
} from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const katexPackagePath = require.resolve("katex/package.json");
const katexRoot = path.dirname(katexPackagePath);
const sourceDist = path.join(katexRoot, "dist");
const destinationRoot = path.join(appRoot, "extension", "vendor", "katex");
const destinationFonts = path.join(destinationRoot, "fonts");
const checkOnly = process.argv.includes("--check");

const rootAssets = [
  ["dist/katex.min.js", "katex.min.js"],
  ["dist/katex.min.css", "katex.min.css"],
  ["LICENSE", "LICENSE"]
];

const fontNames = (await readdir(path.join(sourceDist, "fonts")))
  .filter((name) => /\.(?:ttf|woff2?)$/i.test(name))
  .sort();

const assets = [
  ...rootAssets.map(([source, destination]) => ({
    source: path.join(katexRoot, source),
    destination: path.join(destinationRoot, destination)
  })),
  ...fontNames.map((name) => ({
    source: path.join(sourceDist, "fonts", name),
    destination: path.join(destinationFonts, name)
  }))
];

if (checkOnly) {
  const mismatches = [];
  for (const asset of assets) {
    try {
      await access(asset.destination, fsConstants.R_OK);
      const [sourceBytes, destinationBytes] = await Promise.all([
        readFile(asset.source),
        readFile(asset.destination)
      ]);
      if (!sourceBytes.equals(destinationBytes)) {
        mismatches.push(`${path.relative(appRoot, asset.destination)} differs from installed KaTeX`);
      }
    } catch {
      mismatches.push(`${path.relative(appRoot, asset.destination)} is missing`);
    }
  }
  if (mismatches.length > 0) {
    console.error([
      "Vendored KaTeX assets are out of date:",
      ...mismatches.map((message) => `- ${message}`),
      "Run: npm run sync:katex-assets -w apps/overleaf-extension"
    ].join("\n"));
    process.exitCode = 1;
  } else {
    console.log(`Verified ${assets.length} vendored KaTeX assets.`);
  }
} else {
  await mkdir(destinationFonts, { recursive: true });
  await Promise.all(assets.map(({ source, destination }) => copyFile(source, destination)));
  console.log(`Synchronized ${assets.length} KaTeX assets into ${path.relative(appRoot, destinationRoot)}.`);
}
