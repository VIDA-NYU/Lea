#!/usr/bin/env node
// Assemble build/backend: the backend tree the desktop app ships, laid out like
// the Docker image (adapter/, prover/, dist/, apps/, packages/, scripts/,
// config/). Only git-tracked files are taken, so .venv, .lake and caches can
// never leak in. A manifest.json with a content id and the file list lets the
// app sync updates into its app-support copy without touching persistent data.
//
//   node scripts/build-backend.mjs [--ui]     # --ui (re)builds the React UI first
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = path.resolve(APP, '..', '..');
const OUT = path.join(APP, 'build', 'backend');
const STANDALONE = 'apps/lea-standalone';
const DIST = path.join(ROOT, STANDALONE, 'dist');

// Mirrors the Dockerfile's COPY list.
const SOURCES = [
  `${STANDALONE}/adapter`,
  `${STANDALONE}/prover`,
  `${STANDALONE}/config/lea.local.example.toml`,
  `${STANDALONE}/src/app/lib/leanCheckRuntime.mjs`,
  'apps/overleaf-extension/companion',
  'apps/overleaf-extension/shared',
  'apps/overleaf-extension/extension',
  'packages/lea-model-catalog',
  'packages/lea-workspace-client',
  'scripts/env.mjs',
];
const EXCLUDE = [
  /^apps\/lea-standalone\/adapter\/tests\//,
  /^apps\/lea-standalone\/prover\/(tests|docs|examples)\//,
  /^apps\/lea-standalone\/prover\/(lea-roadmap|lea-api-design|DESIGN|USAGE)\.md$/,
  /\/SafeVerifyTest\//,
  /\/tests?\//,
  /\.test\.mjs$/,
  /__pycache__/,
  /\/\.gitignore$/,
  /\.DS_Store$/,
];

// Docker layout: adapter/, prover/ and config/ at the top; everything else keeps
// its monorepo-relative path (the companion imports ../../../packages etc.).
function target(rel) {
  for (const top of ['adapter', 'prover', 'config']) {
    const prefix = `${STANDALONE}/${top}/`;
    if (rel.startsWith(prefix)) return `${top}/${rel.slice(prefix.length)}`;
  }
  return rel;
}

function walk(dir, base = dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(abs, base));
    else out.push(path.relative(base, abs).split(path.sep).join('/'));
  }
  return out;
}

function git(args) {
  return execFileSync('git', args, { cwd: ROOT }).toString().trim();
}

function main() {
  if (process.argv.includes('--ui') || !fs.existsSync(path.join(DIST, 'index.html'))) {
    console.log('[backend] building the UI (vite)…');
    const result = spawnSync('npm', ['run', 'build', '-w', STANDALONE], { cwd: ROOT, stdio: 'inherit' });
    if (result.status !== 0) process.exit(result.status ?? 1);
  }

  const tracked = execFileSync('git', ['ls-files', '-z', '--', ...SOURCES], { cwd: ROOT })
    .toString().split('\0').filter(Boolean);
  const files = new Map(); // target path → absolute source
  for (const rel of tracked) {
    if (EXCLUDE.some((re) => re.test(rel))) continue;
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs)) continue; // deleted in the working tree
    files.set(target(rel), abs);
  }
  for (const rel of walk(DIST)) files.set(`dist/${rel}`, path.join(DIST, rel));

  fs.rmSync(OUT, { recursive: true, force: true });
  const id = createHash('sha256');
  const list = [...files.keys()].sort();
  for (const rel of list) {
    const src = files.get(rel);
    const dst = path.join(OUT, rel);
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(src, dst);
    fs.chmodSync(dst, fs.statSync(src).mode & 0o777);
    id.update(rel).update('\0').update(createHash('sha256').update(fs.readFileSync(src)).digest()).update('\n');
  }

  const workspace = path.join(ROOT, STANDALONE, 'prover/workspace');
  const leanToolchain = fs.readFileSync(path.join(workspace, 'lean-toolchain'), 'utf8').trim();
  const mathlibKey = createHash('sha256')
    .update(fs.readFileSync(path.join(workspace, 'lake-manifest.json')))
    .update(leanToolchain).digest('hex').slice(0, 12);
  const appVersion = JSON.parse(fs.readFileSync(path.join(APP, 'package.json'), 'utf8')).version;
  const gitSha = git(['rev-parse', '--short', 'HEAD']);
  const gitDirty = git(['status', '--porcelain', '--', ...SOURCES]) !== '';
  const manifest = {
    schema: 1,
    id: id.digest('hex').slice(0, 16),
    builtAt: new Date().toISOString(),
    appVersion,
    gitSha,
    gitDirty,
    leanToolchain,
    mathlibKey,
    files: list,
  };
  fs.writeFileSync(path.join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 2));
  console.log(`[backend] ${list.length} files → ${path.relative(APP, OUT)} ` +
    `(id ${manifest.id}, git ${gitSha}${gitDirty ? '+dirty' : ''}, ${leanToolchain}, mathlib key ${mathlibKey})`);
}

main();
