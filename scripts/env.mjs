import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const MONOREPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const ROOT_ENV_PATH = path.join(MONOREPO_ROOT, ".env");

export function parseDotEnv(content) {
  const values = {};
  for (const line of String(content || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const equals = trimmed.indexOf("=");
    if (equals === -1) continue;
    const key = trimmed.slice(0, equals).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    values[key] = parseEnvValue(trimmed.slice(equals + 1).trim());
  }
  return values;
}

export function readDotEnv(filePath) {
  if (!fs.existsSync(filePath)) return {};
  return parseDotEnv(fs.readFileSync(filePath, "utf8"));
}

export function loadEnvFile(filePath, target = process.env) {
  if (!fs.existsSync(filePath)) return { loaded: false, path: filePath };
  const values = parseDotEnv(fs.readFileSync(filePath, "utf8"));
  for (const [key, value] of Object.entries(values)) {
    if (target[key] === undefined) {
      target[key] = value;
    }
  }
  return { loaded: true, path: filePath };
}

export function loadRootEnv(target = process.env) {
  return loadEnvFile(ROOT_ENV_PATH, target);
}

export async function patchEnvFile(filePath, patch) {
  const nextPatch = Object.fromEntries(
    Object.entries(patch || {}).filter(([key]) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(key))
  );
  if (Object.keys(nextPatch).length === 0) return;

  let content = "";
  try {
    content = await fsp.readFile(filePath, "utf8");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const lines = content ? content.split(/\r?\n/) : [];
  const seen = new Set();
  const nextLines = [];
  for (const line of lines) {
    const match = line.match(/^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$/);
    if (!match || !Object.prototype.hasOwnProperty.call(nextPatch, match[2])) {
      nextLines.push(line);
      continue;
    }
    const key = match[2];
    seen.add(key);
    const value = nextPatch[key];
    if (value === null || value === undefined || value === "") {
      continue;
    }
    nextLines.push(`${match[1]}${key}${match[3]}${formatEnvValue(value)}`);
  }

  if (nextLines.length > 0 && nextLines[nextLines.length - 1] !== "") {
    nextLines.push("");
  }
  for (const [key, value] of Object.entries(nextPatch)) {
    if (!seen.has(key) && value !== null && value !== undefined && value !== "") {
      nextLines.push(`${key}=${formatEnvValue(value)}`);
    }
  }

  await fsp.mkdir(path.dirname(filePath), { recursive: true });
  await writePrivateFile(filePath, `${trimTrailingBlankLines(nextLines).join("\n")}\n`);
}

// `.env` holds every provider key. Written atomically and owner-only, for the same
// reasons as the adapter's `config.write_private_text` (AUDIT-2026-07-24 S6): a plain
// writeFile truncates first, so an interrupted save left the file empty with the keys
// gone, and the default mode is world-readable for a file that is entirely secrets.
// The temp file is created 0600 in the SAME directory, because rename is only atomic
// within a filesystem.
export async function writePrivateFile(filePath, contents) {
  const dir = path.dirname(filePath);
  const tmp = path.join(dir, `.${path.basename(filePath)}.${process.pid}.tmp`);
  try {
    await fsp.writeFile(tmp, contents, { encoding: "utf8", mode: 0o600 });
    await fsp.rename(tmp, filePath);
  } catch (error) {
    await fsp.rm(tmp, { force: true }).catch(() => {});
    throw error;
  }
}

export function formatEnvValue(value) {
  const text = String(value);
  return /[\s#"'\\]/.test(text) ? JSON.stringify(text) : text;
}

function parseEnvValue(value) {
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    if (value.startsWith('"')) {
      try {
        return JSON.parse(value);
      } catch {
        return value.slice(1, -1);
      }
    }
    return value.slice(1, -1);
  }
  return value;
}

function trimTrailingBlankLines(lines) {
  const next = [...lines];
  while (next.length > 0 && next[next.length - 1] === "") {
    next.pop();
  }
  return next;
}
