import { existsSync, mkdirSync, readdirSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dryRun = process.argv.includes("--dry-run");

const dataDir = path.join(root, "data");
const githubImportsDir = path.join(dataDir, "github-imports");
// The prover is vendored at prover/ (it was once the external/lea-prover submodule).
const workspaceDir = path.join(root, "prover", "workspace");
const projectsDir = path.join(workspaceDir, "projects");
const proofsDir = path.join(workspaceDir, "proofs");

function collectFiles(dir, predicate, files = []) {
  if (!existsSync(dir)) {
    return files;
  }
  for (const entry of readdirSync(dir)) {
    const filePath = path.join(dir, entry);
    const stats = statSync(filePath);
    if (stats.isDirectory()) {
      collectFiles(filePath, predicate, files);
    } else if (predicate(filePath)) {
      files.push(filePath);
    }
  }
  return files;
}

function removeEmptyDirs(dir, keepDir = dir) {
  if (!existsSync(dir)) {
    return;
  }
  for (const entry of readdirSync(dir)) {
    const entryPath = path.join(dir, entry);
    if (statSync(entryPath).isDirectory()) {
      removeEmptyDirs(entryPath, keepDir);
    }
  }
  if (dir !== keepDir && readdirSync(dir).length === 0) {
    if (!dryRun) {
      rmSync(dir, { recursive: true, force: true });
    }
    console.log(`${dryRun ? "Would remove" : "Removed"} empty dir ${path.relative(root, dir)}`);
  }
}

function removeFiles(label, files) {
  console.log(`[reset] ${label}: ${files.length} file${files.length === 1 ? "" : "s"}`);
  for (const filePath of files) {
    if (!dryRun) {
      rmSync(filePath, { force: true });
    }
    console.log(`${dryRun ? "Would remove" : "Removed"} ${path.relative(root, filePath)}`);
  }
}

const sqliteFiles = collectFiles(dataDir, (filePath) => {
  const name = path.basename(filePath);
  return /\.(db|sqlite|sqlite3)(-(journal|shm|wal))?$/.test(name);
});
const projectMarkdowns = collectFiles(projectsDir, (filePath) => filePath.endsWith(".md"));
const leanFiles = collectFiles(proofsDir, (filePath) => filePath.endsWith(".lean"));

removeFiles("SQLite databases", sqliteFiles);
if (existsSync(githubImportsDir)) {
  if (!dryRun) rmSync(githubImportsDir, { recursive: true, force: true });
  console.log(`${dryRun ? "Would remove" : "Removed"} ${path.relative(root, githubImportsDir)}`);
}
removeFiles("project markdowns", projectMarkdowns);
removeFiles("Lean proof files", leanFiles);
removeEmptyDirs(proofsDir);

if (!dryRun) {
  mkdirSync(dataDir, { recursive: true });
  mkdirSync(projectsDir, { recursive: true });
  mkdirSync(proofsDir, { recursive: true });
}

console.log(`[reset] ${dryRun ? "Dry run complete" : "Local state reset complete"}.`);
