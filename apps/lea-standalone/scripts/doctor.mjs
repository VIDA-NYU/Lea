import { existsSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { spawnSync } from "node:child_process";

const root = process.cwd();

function check(label, ok, detail = "") {
  const marker = ok ? "OK" : "FAIL";
  console.log(`[${marker}] ${label}${detail ? ` - ${detail}` : ""}`);
  return ok;
}

function command(args, options = {}) {
  return spawnSync(args[0], args.slice(1), {
    cwd: root,
    encoding: "utf8",
    ...options,
  });
}

function commandDetail(result) {
  return result.error?.message || result.stderr?.trim() || result.stdout?.trim() || "";
}

function portOpen(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port });
    socket.setTimeout(750);
    socket.on("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.on("timeout", () => {
      socket.destroy();
      resolve(false);
    });
    socket.on("error", () => resolve(false));
  });
}

const checks = [];

const nodeMajor = Number(process.versions.node.split(".")[0]);
checks.push(check("Node version", nodeMajor === 22 || nodeMajor === 20, `v${process.versions.node}; Node 22 LTS is recommended`));

// In the monorepo, workspace deps hoist to the repo-root node_modules, so a local
// one may not exist — accept either.
checks.push(check(
  "node_modules",
  existsSync(path.join(root, "node_modules")) || existsSync(path.join(root, "..", "..", "node_modules")),
  "run npm install from the monorepo root if missing",
));
checks.push(check("local config", existsSync(path.join(root, "config", "lea.local.toml")), "copy config/lea.local.example.toml if missing"));
checks.push(check("adapter virtualenv", existsSync(path.join(root, "adapter", ".venv", "bin", "python")), "run npm run setup:api if missing"));
checks.push(check(
  "Lea prover",
  existsSync(path.join(root, "prover", "pyproject.toml")),
  "prover/ is missing — this is a broken checkout",
));
checks.push(check(
  "Lean workspace Mathlib cache",
  existsSync(path.join(
    root,
    "prover",
    "workspace",
    ".lake",
    "packages",
    "mathlib",
    ".lake",
    "build",
    "lib",
    "lean",
    "Mathlib.olean",
  )),
  "run npm run setup:api; otherwise the first lean_check may compile Mathlib for several minutes",
));

// The prover runs in-process inside the adapter venv (lea-prover is installed
// there as an editable path dep, D1). This is the key invariant: if `import lea`
// fails from the adapter venv, no run can start.
const leaImport = command(["./.venv/bin/python", "-c", "import lea; from lea.interface import run_events"], {
  cwd: path.join(root, "adapter"),
});
checks.push(check("prover importable in adapter venv", leaImport.status === 0, commandDetail(leaImport)));

const apiImport = command(["./.venv/bin/python", "-c", "from app.main import app; print(app.title)"], {
  cwd: path.join(root, "adapter"),
});
checks.push(check("API imports", apiImport.status === 0, commandDetail(apiImport)));

// Keep in step with LEA_ADAPTER_PORT (adapter/run_api.py, scripts/dev.mjs).
const adapterPortNumber = Number(process.env.LEA_ADAPTER_PORT) || 8001;
const frontendPort = await portOpen(5173);
const adapterPort = await portOpen(adapterPortNumber);
console.log(`[INFO] frontend port 5173 - ${frontendPort ? "already running" : "not running"}`);
console.log(`[INFO] UI adapter API port ${adapterPortNumber} - ${adapterPort ? "already running" : "not running"}`);

if (!checks.every(Boolean)) {
  process.exitCode = 1;
}
