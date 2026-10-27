import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

const root = process.cwd();

// SafeVerify is a second Lean build + Mathlib download purely for the optional
// `/verify` kernel audit. Skip it for quick test installs (--skip-verify or
// LEA_SKIP_SAFEVERIFY=1); the prover's verify() returns "unavailable" when the
// binary is absent, so the app still runs — the audit just can't be invoked.
const skipVerify =
  process.argv.slice(2).includes("--skip-verify") ||
  ["1", "true", "yes"].includes(String(process.env.LEA_SKIP_SAFEVERIFY || "").toLowerCase());

function fail(message) {
  console.error(`\n[setup] ${message}`);
  process.exit(1);
}

function ensure(pathname, message) {
  if (!existsSync(path.join(root, pathname))) {
    fail(message);
  }
}

function run(label, args, cwd) {
  console.log(`[setup] ${label}`);
  const result = spawnSync(args[0], args.slice(1), {
    cwd,
    stdio: "inherit",
    env: process.env,
  });
  if (result.status !== 0) {
    fail(`${label} failed with exit code ${result.status ?? "unknown"}.`);
  }
}

ensure(
  "prover/pyproject.toml",
  "prover/ is missing — this is a broken checkout.",
);

// The adapter venv runs the app: `uv sync` here builds the vendored prover
// (lea-prover) as an editable path dep and installs its deps too (D1).
run("Installing UI adapter dependencies (incl. the in-process prover)", ["uv", "sync", "--locked"], path.join(root, "adapter"));
// The prover's own venv is only needed to run the prover's standalone test suite.
run("Installing prover dependencies (for prover tests)", ["uv", "sync", "--locked"], path.join(root, "prover"));
run("Downloading Lean workspace cache", ["lake", "exe", "cache", "get"], path.join(root, "prover", "workspace"));
ensure(
  "prover/workspace/.lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean",
  "Lean Mathlib cache is missing after cache download.",
);

// SafeVerify is a separate Lean project (third_party/SafeVerify) the prover invokes
// for the kernel-level proof audit (`/verify`). It ships as source only and has its
// OWN Mathlib dependency, so it needs two things or verify reports failure:
//   1. its exe built (`safe_verify`), and
//   2. its own Mathlib oleans, because it replays proofs that `import Mathlib` in its
//      own Lake project — the workspace cache above does NOT cover it.
// Toolchain is pinned to the workspace's (v4.29.0); the cache is a prebuilt download.
if (skipVerify) {
  console.log("[setup] Skipping SafeVerify build (--skip-verify). The `/verify` audit will report 'unavailable'.");
} else {
  const safeVerifyDir = path.join(root, "prover", "third_party", "SafeVerify");
  ensure(
    "prover/third_party/SafeVerify/lakefile.lean",
    "prover/third_party/SafeVerify is missing — this is a broken checkout.",
  );
  run("Building SafeVerify audit binary", ["lake", "build", "safe_verify"], safeVerifyDir);
  run("Downloading SafeVerify's Mathlib cache (for proof replay)", ["lake", "exe", "cache", "get"], safeVerifyDir);
  ensure(
    "prover/third_party/SafeVerify/.lake/build/bin/safe_verify",
    "SafeVerify binary is missing after build.",
  );
  ensure(
    "prover/third_party/SafeVerify/.lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean",
    "SafeVerify's Mathlib cache is missing after cache download.",
  );
}

console.log("[setup] Done.");
