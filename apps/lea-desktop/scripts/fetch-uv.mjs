#!/usr/bin/env node
// Download the pinned uv release for macOS into build/bin/<arch>/uv, verifying
// the published SHA-256. uv is the only binary the app bundles: on first run it
// installs Python and the adapter's locked dependencies into app support.
//
//   node scripts/fetch-uv.mjs [--arch arm64|x64|all]     (default: this machine)
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const UV_VERSION = '0.13.6';
const TARGETS = { arm64: 'aarch64-apple-darwin', x64: 'x86_64-apple-darwin' };
const APP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BIN = path.join(APP, 'build', 'bin');

async function fetchBuffer(url) {
  const response = await fetch(url, { redirect: 'follow' });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return Buffer.from(await response.arrayBuffer());
}

function findFile(dir, name) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      const found = findFile(abs, name);
      if (found) return found;
    } else if (entry.name === name) {
      return abs;
    }
  }
  return null;
}

async function fetchOne(arch) {
  const triple = TARGETS[arch];
  if (!triple) throw new Error(`unknown arch ${arch}; use arm64 or x64`);
  const dir = path.join(BIN, arch);
  const out = path.join(dir, 'uv');
  const stamp = path.join(dir, 'VERSION');
  if (fs.existsSync(out) && fs.existsSync(stamp) && fs.readFileSync(stamp, 'utf8').trim() === UV_VERSION) {
    console.log(`[uv] ${arch}: ${UV_VERSION} already present`);
    return;
  }
  const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${triple}.tar.gz`;
  console.log(`[uv] downloading ${url}`);
  const [tarball, shaText] = await Promise.all([fetchBuffer(url), fetchBuffer(`${url}.sha256`)]);
  const expected = shaText.toString('utf8').trim().split(/\s+/)[0].toLowerCase();
  const actual = createHash('sha256').update(tarball).digest('hex');
  if (expected !== actual) throw new Error(`[uv] checksum mismatch for ${arch}: expected ${expected}, got ${actual}`);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lea-uv-'));
  try {
    const file = path.join(tmp, 'uv.tar.gz');
    fs.writeFileSync(file, tarball);
    execFileSync('tar', ['-xzf', file, '-C', tmp]);
    const found = findFile(tmp, 'uv');
    if (!found) throw new Error('[uv] binary not found in the archive');
    fs.mkdirSync(dir, { recursive: true });
    fs.copyFileSync(found, out);
    fs.chmodSync(out, 0o755);
    fs.writeFileSync(stamp, `${UV_VERSION}\n`);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  let version = UV_VERSION;
  if (arch === process.arch) version = execFileSync(out, ['--version']).toString().trim();
  console.log(`[uv] ${arch}: ${version} → ${path.relative(APP, out)}`);
}

const argIndex = process.argv.indexOf('--arch');
const requested = argIndex === -1 ? process.arch : process.argv[argIndex + 1];
const arches = requested === 'all' ? Object.keys(TARGETS) : [requested];
for (const arch of arches) await fetchOne(arch);
