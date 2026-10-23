import fs from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { MONOREPO_ROOT, ROOT_ENV_PATH, patchEnvFile, readDotEnv } from "./env.mjs";
import { preflight } from "./preflight.mjs";

// Unified monorepo setup.
//
// The backend is now the standalone app (apps/lea-standalone): a FastAPI adapter
// that drives the modern prover in-process. There is no separate vendored-prover
// submodule and no separate lea-ui server — the Lea Python environment + Lean cache
// are provisioned by the standalone app's own `setup:api`, which we delegate to.

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const OVERLEAF_ROOT = path.join(MONOREPO_ROOT, "apps", "overleaf-extension");
const STANDALONE_ROOT = path.join(MONOREPO_ROOT, "apps", "lea-standalone");
const ENV_EXAMPLE_PATH = path.join(MONOREPO_ROOT, ".env.example");
const ROOT_ENV = readDotEnv(ROOT_ENV_PATH);
const LEA_ROOT = resolveMonorepoPath(process.env.LEA_ROOT || ROOT_ENV.LEA_ROOT || "apps/lea-standalone/prover");
const LEA_WORKSPACE = path.join(LEA_ROOT, "workspace");
const VALID_TARGETS = new Set(["all", "ui", "overleaf"]);
const DEFAULT_ENV = {
  LEA_ROOT: "apps/lea-standalone/prover",
  // The standalone adapter is the single backend; both apps talk to it on :8001.
  LEA_API_BASE_URL: "http://127.0.0.1:8001",
  OVERLEAF_COMPANION_URL: "http://127.0.0.1:31245",
  LEA_PROVIDER: "openai",
  LEA_MODEL: "o4-mini",
  LEA_MAX_TURNS: "20",
  LEA_JOB_TIMEOUT_SECONDS: "900",
  LEA_NARRATE_TOOL_STEPS: "true",
};
const DEFAULT_OVERLEAF_LATEX_CONTEXT_MODE = "off";

const options = parseArgs(process.argv.slice(2));

await main();

async function main() {
  console.log(`Lea ecosystem setup (${options.target})\n`);

  // Fail fast with actionable install hints if a toolchain is missing, before
  // any long-running step shells out to it. Skippable for CI/containers where
  // the environment is known-good.
  if (!options.skipPreflight) {
    preflight({ exitOnFailure: true });
  }

  await installNodeDependencies();
  await writeRootEnv();
  await setupStandaloneBackend();

  if (options.target === "all" || options.target === "overleaf") {
    await writeOverleafSettings();
  }

  console.log("\nSetup complete.");
  if (options.skipVerify) {
    console.log("(SafeVerify was skipped — the `/verify` kernel audit will report 'unavailable'.");
    console.log(" Re-run `npm run setup` without --skip-verify to enable it.)");
  }
  console.log("Next steps:");
  console.log("1. Put your provider API key in the monorepo root .env (or the app's Settings).");
  console.log("2. Run `npm run doctor` from the monorepo root.");
  console.log("3. Start the shared backend with `npm run start:adapter`, then `npm run dev:ui` or `npm run dev:overleaf`.");
}

async function installNodeDependencies() {
  console.log("Installing monorepo Node dependencies...");
  await run("npm", ["install"], { cwd: MONOREPO_ROOT });
}

// Provision the standalone app's Python environment + Lean cache by delegating to
// its own setup (builds the adapter venv with the in-process prover, the prover
// venv for tests, and downloads the Mathlib build cache). Replaces the old
// submodule init + bundled `uv sync --extra api` + lea-ui server sync.
async function setupStandaloneBackend() {
  if (options.refreshLeanDeps) {
    console.log("Refreshing the standalone prover's Lean workspace cache...");
    await run("lake", ["update"], { cwd: LEA_WORKSPACE });
  }
  console.log("Setting up the standalone Lea backend (adapter + prover + Lean cache)...");
  // `--` forwards flags through npm to setup-api.mjs. --skip-verify omits the
  // SafeVerify build + its Mathlib download (the `/verify` audit degrades to
  // "unavailable"), roughly halving setup time/disk for quick test installs.
  const apiArgs = ["run", "setup:api", "-w", "apps/lea-standalone"];
  if (options.skipVerify) apiArgs.push("--", "--skip-verify");
  await run("npm", apiArgs, { cwd: MONOREPO_ROOT });
}

async function writeRootEnv() {
  if (!existsSync(ROOT_ENV_PATH) && existsSync(ENV_EXAMPLE_PATH)) {
    await fs.copyFile(ENV_EXAMPLE_PATH, ROOT_ENV_PATH);
    console.log("Created root .env from .env.example.");
  }

  const existing = await readEnvFile(ROOT_ENV_PATH);
  const patch = {};

  for (const [key, value] of Object.entries(DEFAULT_ENV)) {
    patch[key] = existing[key] || value;
  }

  await patchEnvFile(ROOT_ENV_PATH, patch);
  console.log(`Wrote root .env defaults at ${path.relative(MONOREPO_ROOT, ROOT_ENV_PATH)}.`);
}

async function writeOverleafSettings() {
  // Connection bootstrap is infrastructure. Application policy and operational
  // state are owned by the adapter database after its one-time import.
  console.log("Overleaf uses the adapter database; no companion ledger is written.");
}

function parseArgs(args) {
  const parsed = {
    target: "all",
    refreshLeanDeps: false,
    skipVerify: false,
    skipPreflight: false,
  };

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--refresh-lean-deps") {
      parsed.refreshLeanDeps = true;
      continue;
    }
    if (arg === "--skip-verify") {
      parsed.skipVerify = true;
      continue;
    }
    if (arg === "--skip-preflight") {
      parsed.skipPreflight = true;
      continue;
    }
    if (arg === "--target") {
      const value = args[index + 1];
      if (!value) usage("Missing value for --target.");
      parsed.target = value;
      index += 1;
      continue;
    }
    if (arg.startsWith("--target=")) {
      parsed.target = arg.slice("--target=".length);
      continue;
    }
    if (arg === "--help" || arg === "-h") {
      usage();
    }
    usage(`Unknown argument: ${arg}`);
  }

  if (!VALID_TARGETS.has(parsed.target)) {
    usage(`Invalid --target "${parsed.target}".`);
  }

  return parsed;
}

function usage(error) {
  if (error) {
    console.error(`[setup] ${error}\n`);
  }
  console.error(`Usage: node ${path.relative(MONOREPO_ROOT, path.join(SCRIPT_DIR, "setup.mjs"))} [--target all|ui|overleaf] [--refresh-lean-deps] [--skip-verify] [--skip-preflight]`);
  process.exit(error ? 1 : 0);
}

function run(command, args, { cwd }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      stdio: "inherit",
      env: process.env,
    });

    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(new Error(`${command} ${args.join(" ")} failed with exit code ${code}`));
    });
  });
}

async function readEnvFile(filePath) {
  try {
    return readDotEnv(filePath);
  } catch (error) {
    if (error?.code === "ENOENT") return {};
    throw error;
  }
}

async function readJson(filePath, fallback) {
  try {
    return JSON.parse(await fs.readFile(filePath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return fallback;
    throw error;
  }
}

function resolveMonorepoPath(value) {
  return path.isAbsolute(value) ? value : path.resolve(MONOREPO_ROOT, value);
}
