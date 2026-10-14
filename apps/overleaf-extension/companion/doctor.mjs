import fs from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { applyEnvDefaults, loadDotEnv } from "./config.mjs";
import { fetchAdapterHealth } from "./leaApiClient.mjs";

const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SETTINGS_PATH = path.join(PROJECT_ROOT, ".overleaf-lean-stub", "settings.json");

const dotenv = loadDotEnv();
const settings = applyEnvDefaults(readJson(SETTINGS_PATH, {}));
const checks = [];

checkCommand("node", ["--version"]);
checkCommand("git", ["--version"]);
checkCommand("uv", ["--version"]);
checkCommand("lean", ["--version"]);
checkCommand("lake", ["--version"]);

checkEnv("OPENAI_API_KEY");
// The backend is now the standalone app: the companion reads proof artifacts
// from the standalone prover's workspace and talks to the adapter (:8001), not a
// vendored Lea API. leaRepoPath points at apps/lea-standalone/prover.
checkPath("Lea prover repo", settings.leaRepoPath, (dir) => (
  fs.existsSync(path.join(dir, "pyproject.toml")) &&
  fs.existsSync(path.join(dir, "lea"))
), "must point at the standalone prover (apps/lea-standalone/prover)");
const leaWorkspacePath = settings.leaRepoPath ? path.join(settings.leaRepoPath, "workspace") : "";
checkPath("Lea workspace", leaWorkspacePath, (dir) => (
  fs.existsSync(path.join(dir, "lean-toolchain")) &&
  (fs.existsSync(path.join(dir, "lakefile.lean")) || fs.existsSync(path.join(dir, "lakefile.toml")))
), "must contain lean-toolchain and lakefile.lean or lakefile.toml");
checkMathlib(leaWorkspacePath);
// The API runs in the adapter venv (apps/lea-standalone/adapter/.venv), a sibling
// of the prover dir.
const adapterVenv = settings.leaRepoPath
  ? path.resolve(settings.leaRepoPath, "..", "adapter", ".venv", "bin", "python")
  : "";
checkPath("Lea adapter virtualenv", adapterVenv, () => true, "run `npm run setup` from the monorepo root");
checkUrl("Lea adapter URL", settings.leaApiBaseUrl || "http://127.0.0.1:8001");

const health = await fetchAdapterHealth({ baseUrl: settings.leaApiBaseUrl || "http://127.0.0.1:8001",
  fetchImpl: (url, options) => fetch(url, { ...options, signal: AbortSignal.timeout(1500) }) });
if (health.ok) {
  const capability = health.body?.capabilities?.lea_status;
  checks.push({ ok: capability?.version === 1 && capability?.independent_checks === false,
    label: "Live Lea Status v1", detail: capability?.version === 1
      ? (capability.admission_enabled ? "enabled; independent evaluator retired" : "new admissions disabled")
      : "restart the updated adapter and reload the extension" });
} else {
  console.log("• Live Lea Status capability will be checked when the adapter starts.");
}
console.log("Overleaf Lea Formalizer doctor\n");
console.log(`${dotenv.loaded ? "✓" : "•"} root .env ${dotenv.loaded ? `loaded from ${dotenv.path}` : "not found; using shell/settings only"}`);
for (const check of checks) {
  console.log(`${check.ok ? "✓" : "✗"} ${check.label}${check.detail ? `: ${check.detail}` : ""}`);
}

const failed = checks.filter((check) => !check.ok);
if (failed.length > 0) {
  console.log("\nFix the failed checks above, then run `npm run doctor` again.");
  process.exitCode = 1;
} else {
  console.log("\nAll checks passed. Start with `npm start`.");
}

function checkCommand(command, args) {
  const result = spawnSync(command, args, { encoding: "utf8" });
  checks.push({
    ok: result.status === 0,
    label: `${command} available`,
    detail: result.status === 0
      ? firstLine(result.stdout || result.stderr)
      : "not found on PATH"
  });
}

function checkEnv(name) {
  checks.push({
    ok: Boolean(process.env[name]),
    label: `${name} set`,
    detail: process.env[name] ? "present" : "missing"
  });
}

function checkPath(label, value, predicate, requirement) {
  const ok = Boolean(value) && path.isAbsolute(value) && fs.existsSync(value) && predicate(value);
  checks.push({
    ok,
    label,
    detail: ok ? value : `${value || "unset"} (${requirement})`
  });
}

function checkUrl(label, value) {
  let ok = false;
  try {
    const parsed = new URL(value);
    ok = ["http:", "https:"].includes(parsed.protocol);
  } catch {
    ok = false;
  }
  checks.push({
    ok,
    label,
    detail: ok ? value : `${value || "unset"} (must be absolute http(s) URL)`
  });
}

function checkMathlib(leaWorkspacePath) {
  const lakefilePath = path.join(leaWorkspacePath, "lakefile.lean");
  const manifestPath = path.join(leaWorkspacePath, "lake-manifest.json");
  const mathlibPackagePath = path.join(leaWorkspacePath, ".lake", "packages", "mathlib");
  const mathlibOleanPath = path.join(mathlibPackagePath, ".lake", "build", "lib", "lean", "Mathlib.olean");
  const lakefile = readText(lakefilePath);
  const manifest = readText(manifestPath);
  const configured = /require\s+mathlib\b/.test(lakefile) || /"name"\s*:\s*"mathlib"/.test(manifest);
  const fetched = fs.existsSync(mathlibPackagePath);
  const compiled = fs.existsSync(mathlibOleanPath);

  checks.push({
    ok: configured,
    label: "Mathlib dependency configured",
    detail: configured ? "require mathlib found" : "add `require mathlib` to lakefile.lean"
  });
  checks.push({
    ok: fetched,
    label: "Mathlib package fetched",
    detail: fetched ? mathlibPackagePath : "run `lake update` and `lake exe cache get`"
  });
  checks.push({
    ok: compiled,
    label: "Mathlib compiled cache",
    detail: compiled ? mathlibOleanPath : "run `npm run update-lean-deps` from the monorepo root and wait for cache decompression to finish"
  });
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function readText(filePath) {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return "";
  }
}

function firstLine(text) {
  return String(text).trim().split(/\r?\n/)[0] || "ok";
}
