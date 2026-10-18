// One supervisor; independently owned API, workspace worker and UI transports.
// Run: node scripts/supervisor.mjs adapter|ui|stack
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadRootEnv } from "./env.mjs";
import { assertServicesAvailable } from "./dev-stack-preflight.mjs";
import { createProcessSupervisor } from "./process-supervisor.mjs";
import { serviceEnvironment } from "./service-env.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
loadRootEnv();
const mode = process.argv[2] || "adapter";
if (!["adapter", "ui", "stack"].includes(mode)) {
  console.error("Usage: supervisor.mjs adapter|ui|stack");
  process.exit(2);
}

const adapterDir = path.join(root, "apps/lea-standalone/adapter");
const python = path.join(adapterDir, ".venv/bin/python");
if (!existsSync(python)) {
  console.error("Adapter virtualenv is missing. Run npm run setup.");
  process.exit(1);
}

// Reject duplicate launches before spawning children or installing shutdown
// handlers that could drain an adapter owned by a different supervisor.
try {
  await assertServicesAvailable(mode);
} catch (error) {
  console.error(`[supervisor] ${error.message}`);
  process.exit(1);
}

const owned = new Map();
const owner = createProcessSupervisor({ prefix: "[supervisor]", onUnexpectedExit: () => true });
const crashes = new Map();
const delays = [1000, 2000, 5000];
let stopping = false;

function start(label, command, args, cwd, { restart = false } = {}) {
  if (stopping || owner.stopping) return null;
  const child = owner.spawn(label, command, args, {
    cwd, env: serviceEnvironment(label), stdio: "inherit", detached: true,
  });
  owned.set(label, child);
  child.on("exit", async (code, signal) => {
    if (owned.get(label) !== child) return;
    owned.delete(label);
    if (label === "adapter" || label === "worker") {
      await reapOwnedGroup(child.pid);
      if (stopping || owner.stopping) return;
      if (code === 0 && !signal) {
        console.log(`[supervisor] ${label} stopped cleanly; other services remain available.`);
        return;
      }
      const now = Date.now();
      const history = crashes.get(label) || [];
      crashes.set(label, history);
      history.push(now);
      while (history.length && history[0] < now - 300000) history.shift();
      if (history.length > 3) {
        console.error(`[supervisor] ${label} crash loop: automatic restart stopped; other services remain available.`);
        process.exitCode = 1;
        return;
      }
      const delay = delays[history.length - 1];
      console.error(`[supervisor] ${label} exited (${signal || code}); restarting in ${delay / 1000}s.`);
      setTimeout(() => startService(label), delay);
    } else if (!stopping && !owner.stopping && restart) {
      console.error(`[supervisor] ${label} exited (${signal || code}); restarting it.`);
      setTimeout(() => startService(label), 1000);
    }
  });
  return child;
}

async function reapOwnedGroup(pid) {
  if (!pid) return;
  try { process.kill(-pid, "SIGTERM"); }
  catch (error) { if (error.code === "ESRCH") return; throw error; }
  await new Promise((resolve) => setTimeout(resolve, 5000));
  try { process.kill(-pid, "SIGKILL"); }
  catch (error) { if (error.code !== "ESRCH") throw error; }
}

function startAdapter() {
  start("adapter", python, ["run_api.py"], adapterDir);
}

function startService(label) {
  if (label === "adapter") return startAdapter();
  if (label === "worker") return start("worker", python, ["run_worker.py"], adapterDir);
  if (label === "web") {
    const vite = path.join(root, "node_modules/.bin/vite");
    start("web", vite, ["--host", "0.0.0.0", "--strictPort"],
          path.join(root, "apps/lea-standalone"), { restart: true });
  } else if (label === "companion") {
    start("companion", "npm", ["start", "-w", "apps/overleaf-extension"], root, { restart: true });
  }
}

function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  owner.stop(code);
}

try {
  execFileSync(python, ["bootstrap.py"], { cwd: adapterDir, env: process.env, stdio: "inherit" });
} catch (error) {
  console.error("[supervisor] Database bootstrap failed; no serving process was started.");
  process.exit(1);
}
startService("worker");
startAdapter();
if (mode !== "adapter") startService("web");
if (mode === "stack") startService("companion");
