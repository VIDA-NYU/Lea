import { existsSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { createProcessSupervisor } from "../../../scripts/process-supervisor.mjs";

// Dev orchestration. The prover is no longer a separate process — the adapter
// imports it as a library and drives it in-process (architecture D1/D20), and it
// exports its own provider keys from config (D1·cfg). One owner runs the adapter
// (:8001), Vite (:5173), and optionally the Overleaf companion (:31245).
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// In the monorepo, npm hoists workspace deps to the repo-root node_modules, so
// this app may have no local node_modules. Resolve hoisted tools from either
// place (local checkout first, then the monorepo root).
const MONOREPO_ROOT = path.resolve(root, "..", "..");
const supervisor = createProcessSupervisor();
const withOverleaf = process.argv.includes("--overleaf");
const adapterOnly = process.argv.includes("--adapter-only");

function resolveHoisted(...segments) {
  const local = path.join(root, "node_modules", ...segments);
  if (existsSync(local)) return local;
  return path.join(MONOREPO_ROOT, "node_modules", ...segments);
}

function fail(message) {
  throw new Error(message);
}

function ensure(pathname, message) {
  if (!existsSync(path.join(root, pathname))) {
    fail(message);
  }
}

async function waitFor(url, label, timeoutMs = 20000) {
  const started = Date.now();
  while (!supervisor.stopping) {
    try {
      const response = await fetch(url, {
        signal: AbortSignal.any([supervisor.signal, AbortSignal.timeout(1000)]),
      });
      await response.body?.cancel();
      if (response.ok) return;
    } catch (error) {
      if (supervisor.stopping) throw error;
    }
    if (Date.now() - started > timeoutMs) {
      fail(`${label} did not become ready at ${url}`);
    }
    await delay(400, undefined, { signal: supervisor.signal });
  }
  supervisor.signal.throwIfAborted();
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

async function ensurePortAvailable(port, label) {
  if (await portOpen(port)) {
    fail(`${label} port ${port} is already in use. Stop the existing process and run npm run dev again.`);
  }
}

function start(label, command, args, options = {}) {
  const child = supervisor.spawn(label, command, args, {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
    env: process.env,
    ...options,
  });
  child.stdout.on("data", (chunk) => process.stdout.write(`[${label}] ${chunk}`));
  child.stderr.on("data", (chunk) => process.stderr.write(`[${label}] ${chunk}`));
  return child;
}

try {
  if (!adapterOnly && !existsSync(path.join(root, "node_modules")) && !existsSync(path.join(MONOREPO_ROOT, "node_modules"))) {
    fail("node_modules is missing. Run npm install from the monorepo root.");
  }
  if (!adapterOnly) ensure("config/lea.local.toml", "config/lea.local.toml is missing. Copy config/lea.local.example.toml.");
  ensure("adapter/.venv/bin/python", "adapter virtualenv is missing. Run npm run setup:api.");
  ensure("prover/pyproject.toml", "prover/ is missing — this is a broken checkout.");

  const nodeMajor = Number(process.versions.node.split(".")[0]);
  if (nodeMajor !== 22 && nodeMajor !== 20) {
    console.warn(`[dev] Node ${process.versions.node} detected. Node 22 LTS is recommended.`);
  }

  // The adapter reads the shared configuration and drives the prover in-process.
  await ensurePortAvailable(8001, "UI adapter API");
  if (!adapterOnly) await ensurePortAvailable(5173, "frontend");
  if (withOverleaf) await ensurePortAvailable(31245, "Overleaf companion");
  start("adapter", "./.venv/bin/python", ["run_api.py"], { cwd: path.join(root, "adapter") });

  await waitFor("http://127.0.0.1:8001/api/health", "UI adapter API");
  console.log("[dev] UI adapter API ready at http://127.0.0.1:8001");

  if (!adapterOnly) start("web", resolveHoisted(".bin", "vite"), ["--host", "0.0.0.0", "--strictPort"]);
  if (withOverleaf) {
    start("companion", process.execPath, ["companion/server.mjs"], {
      cwd: path.join(MONOREPO_ROOT, "apps/overleaf-extension"),
    });
  }

  if (!adapterOnly) {
    await waitFor("http://127.0.0.1:5173", "frontend");
    console.log("[dev] Frontend ready at http://localhost:5173");
  }
  console.log("[dev] Press Ctrl+C to stop the stack; press it again to force quit.");
} catch (error) {
  if (!supervisor.stopping) {
    console.error(`[dev] ${error.message}`);
    supervisor.stop(1);
  }
}
