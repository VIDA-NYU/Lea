import {
  existsSync,
  mkdirSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { MONOREPO_ROOT, loadRootEnv, readDotEnv, ROOT_ENV_PATH } from "./env.mjs";

// Unified local-state reset for the whole monorepo. Replaces the former
// per-app reset scripts; both apps share the Lea workspace under the standalone
// prover (apps/lea-standalone/prover), so their cleanup is consolidated here.

const dryRun = process.argv.includes("--dry-run");

loadRootEnv();
const rootEnv = readDotEnv(ROOT_ENV_PATH);
const leaRoot = resolveMonorepoPath(process.env.LEA_ROOT || rootEnv.LEA_ROOT || "apps/lea-standalone/prover");
const workspaceDir = path.join(leaRoot, "workspace");
const projectsDir = path.join(workspaceDir, "projects");
const proofsDir = path.join(workspaceDir, "proofs");
const overleafContextDir = path.join(workspaceDir, "context", "overleaf");

// Overleaf companion local state.
const companionDirs = new Set([
  path.join(MONOREPO_ROOT, "apps", "overleaf-extension", ".overleaf-lean-stub"),
]);
const companionImportDir = process.env.LEA_COMPANION_IMPORT_DIR || rootEnv.LEA_COMPANION_IMPORT_DIR;
if (companionImportDir) companionDirs.add(resolveMonorepoPath(companionImportDir));

// Lea UI / adapter local state. The standalone adapter keeps its SQLite DB
// (sessions, runs, code steps) under apps/lea-standalone/data; LEA_DB_PATH /
// LEA_SHARED_DATA_DIR can relocate it. (The retired apps/lea-ui/data path is kept
// in the scan list only so older checkouts still get cleaned up.)
const uiDataDir = resolveMonorepoPath(
  process.env.LEA_SHARED_DATA_DIR || rootEnv.LEA_SHARED_DATA_DIR || "apps/lea-standalone/data",
);
const legacyUiDataDir = path.join(MONOREPO_ROOT, "apps", "lea-ui", "data");
const githubImportsDir = path.join(uiDataDir, "github-imports");

function resolveMonorepoPath(value) {
  return path.isAbsolute(value) ? value : path.resolve(MONOREPO_ROOT, value);
}

function rel(filePath) {
  const relativePath = path.relative(MONOREPO_ROOT, filePath);
  return relativePath && !relativePath.startsWith("..") ? relativePath : filePath;
}

function collectEntries(dir, predicate = () => true, files = [], recurse = false) {
  if (!existsSync(dir)) return files;
  for (const entry of readdirSync(dir)) {
    if (entry === ".gitkeep") continue;
    const entryPath = path.join(dir, entry);
    const isDir = statSync(entryPath).isDirectory();
    if (isDir && recurse) {
      collectEntries(entryPath, predicate, files, recurse);
    } else if (predicate(entryPath, isDir)) {
      files.push(entryPath);
    }
  }
  return files;
}

function removeEntries(label, entries) {
  console.log(`[reset] ${label}: ${entries.length} entr${entries.length === 1 ? "y" : "ies"}`);
  for (const entryPath of entries) {
    if (!dryRun) rmSync(entryPath, { recursive: true, force: true });
    console.log(`${dryRun ? "Would remove" : "Removed"} ${rel(entryPath)}`);
  }
}

function removeDir(label, dir) {
  const exists = existsSync(dir);
  console.log(`[reset] ${label}: ${exists ? rel(dir) : "not present"}`);
  if (exists && !dryRun) rmSync(dir, { recursive: true, force: true });
  if (exists) console.log(`${dryRun ? "Would remove" : "Removed"} ${rel(dir)}`);
}

function writeJsonFile(label, filePath, value) {
  console.log(`[reset] ${label}: ${rel(filePath)}`);
  if (!dryRun) {
    mkdirSync(path.dirname(filePath), { recursive: true });
    writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  }
  console.log(`${dryRun ? "Would write" : "Wrote"} ${rel(filePath)}`);
}

// `data/backups/` holds pre-migration snapshots (adapter/app/backup.py) and is the
// recovery path now that SQL — not git — owns proof content. It must survive a
// reset: a safety net that vanishes exactly when someone reaches for it is worse
// than none, since "just reset it" is precisely what you try when things look
// broken. Everything else under data/ is still fair game.
const BACKUPS_DIR_NAME = "backups";
const isInBackups = (filePath) =>
  path.relative(uiDataDir, filePath).split(path.sep).includes(BACKUPS_DIR_NAME);

const isSqlite = (filePath, isDir) =>
  !isDir &&
  !isInBackups(filePath) &&
  /\.(db|sqlite|sqlite3)(-(journal|shm|wal))?$/.test(path.basename(filePath));
const sqliteFiles = [
  ...collectEntries(uiDataDir, isSqlite, [], true),
  ...collectEntries(legacyUiDataDir, isSqlite, [], true),
];

// Shared Lea workspace artifacts (used by both apps).
removeEntries("Lea project entries", collectEntries(projectsDir));
removeEntries("Lea proof entries", collectEntries(proofsDir));
removeDir("Lea Overleaf LaTeX context", overleafContextDir);

// Overleaf companion artifacts.
// The worker imports these legacy ledgers into a new database on startup. Clear
// every runtime ledger together so a fresh database cannot resurrect old work.
// Connection/provider settings stay in settings.json and the root .env.
for (const companionDir of companionDirs) {
  removeDir("companion job logs", path.join(companionDir, "jobs"));
  removeDir("companion backups", path.join(companionDir, "backups"));
  writeJsonFile("companion job index", path.join(companionDir, "jobs.json"), {});
  writeJsonFile("companion batch index", path.join(companionDir, "jobs.json.batches.json"), {});
  writeJsonFile("companion chat associations", path.join(companionDir, "chatSessions.json"), {});
  writeJsonFile("companion generated targets", path.join(companionDir, "generated-targets.json"), { version: 1, projects: {} });
  writeJsonFile("companion cache", path.join(companionDir, "cache.json"), {});
  removeDir("companion migration checkpoint", path.join(companionDir, ".lea-db-migration.json"));
}

// Lea UI artifacts.
removeEntries("Lea UI SQLite databases", sqliteFiles);
removeDir("GitHub import staging", githubImportsDir);
removeDir("companion migration snapshots", path.join(uiDataDir, "legacy-companion"));

if (!dryRun) {
  mkdirSync(projectsDir, { recursive: true });
  mkdirSync(proofsDir, { recursive: true });
  for (const companionDir of companionDirs) mkdirSync(path.join(companionDir, "jobs"), { recursive: true });
  mkdirSync(uiDataDir, { recursive: true });
}

console.log(`[reset] Ecosystem local state ${dryRun ? "dry run" : "reset"} complete.`);
