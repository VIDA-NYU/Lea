// One process owner for the adapter, frontend, and optional companion.
// Run: node scripts/supervisor.mjs adapter|ui|stack
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadRootEnv } from "./env.mjs";

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

const owned = new Map();
const crashes = [];
const delays = [1000, 2000, 5000];
let stopping = false;
let adapterChild = null;
let stopResolve = null;

function start(label, command, args, cwd, { restart = false } = {}) {
  if (stopping) return null;
  const child = spawn(command, args, {
    cwd, env: process.env, stdio: "inherit", detached: true,
  });
  owned.set(label, child);
  if (label === "adapter") adapterChild = child;
  child.on("exit", async (code, signal) => {
    if (owned.get(label) !== child) return;
    owned.delete(label);
    if (label === "adapter") {
      await reapOwnedGroup(child.pid);
      if (stopping) {
        stopResolve?.();
        return;
      }
      const now = Date.now();
      crashes.push(now);
      while (crashes.length && crashes[0] < now - 300000) crashes.shift();
      if (crashes.length > 3) {
        console.error("[supervisor] Adapter crash loop: more than three exits in five minutes.");
        process.exitCode = 1;
        await stop({ force: true });
        return;
      }
      const delay = delays[crashes.length - 1];
      console.error(`[supervisor] Adapter exited (${signal || code}); restarting in ${delay / 1000}s.`);
      setTimeout(startAdapter, delay);
    } else if (!stopping && restart) {
      console.error(`[supervisor] ${label} exited (${signal || code}); restarting it.`);
      setTimeout(() => startService(label), 1000);
    }
  });
  return child;
}

function signalGroup(child, signal) {
  if (!child?.pid) return;
  try { process.kill(-child.pid, signal); }
  catch (error) { if (error.code !== "ESRCH") throw error; }
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
  if (label === "web") {
    const vite = path.join(root, "node_modules/.bin/vite");
    start("web", vite, ["--host", "0.0.0.0", "--strictPort"],
          path.join(root, "apps/lea-standalone"), { restart: true });
  } else if (label === "companion") {
    start("companion", "npm", ["start", "-w", "apps/overleaf-extension"], root, { restart: true });
  }
}

async function requestDrain(force = false) {
  try {
    const response = await fetch("http://127.0.0.1:8001/api/runtime/restart", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ shutdown: true, force }),
      signal: AbortSignal.timeout(3000),
    });
    return response.ok;
  } catch { return false; }
}

async function stop({ force = false } = {}) {
  if (stopping) return;
  stopping = true;
  const child = adapterChild;
  if (child && owned.get("adapter") === child) {
    const requested = await requestDrain(force);
    if (!requested) signalGroup(child, "SIGTERM");
    await new Promise((resolve) => {
      stopResolve = resolve;
      if (!owned.has("adapter")) resolve();
    });
  }
  for (const [label, service] of owned) {
    if (label !== "adapter") signalGroup(service, "SIGTERM");
  }
  await Promise.all([...owned.values()].map((service) => new Promise((resolve) => {
    if (service.exitCode !== null || service.signalCode !== null) return resolve();
    service.once("exit", resolve);
  })));
}

process.on("SIGINT", () => { void stop(); });
process.on("SIGTERM", () => { void stop(); });
startAdapter();
if (mode !== "adapter") startService("web");
if (mode === "stack") startService("companion");
