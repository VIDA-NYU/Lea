import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";

// Dev orchestration. The prover is no longer a separate process — the adapter
// imports it as a library and drives it in-process (architecture D1/D20), and it
// exports its own provider keys from config (D1·cfg). So dev runs just two
// processes: the FastAPI adapter (:8001) and the Vite web dev server (:5173).
const root = process.cwd();
// Set LEA_ADAPTER_PORT to move the adapter off :8001 (e.g. when that port is
// already taken by something else on the machine). adapter/run_api.py binds it,
// vite.config.ts proxies /api to it, and scripts/doctor.mjs probes it — all from
// this one variable, which children inherit through `env: process.env` below.
const ADAPTER_PORT = Number(process.env.LEA_ADAPTER_PORT) || 8001;
// In the monorepo, npm hoists workspace deps to the repo-root node_modules, so
// this app may have no local node_modules. Resolve hoisted tools from either
// place (local checkout first, then the monorepo root).
const MONOREPO_ROOT = path.resolve(root, "..", "..");
const children = [];

function resolveHoisted(...segments) {
  const local = path.join(root, "node_modules", ...segments);
  if (existsSync(local)) return local;
  return path.join(MONOREPO_ROOT, "node_modules", ...segments);
}

function fail(message) {
  console.error(`\n[dev] ${message}`);
  process.exit(1);
}

function ensure(pathname, message) {
  if (!existsSync(path.join(root, pathname))) {
    fail(message);
  }
}

function waitFor(url, label, timeoutMs = 20000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const request = http.get(url, (response) => {
        response.resume();
        resolve();
      });
      request.setTimeout(1000, () => {
        request.destroy();
      });
      request.on("error", () => {
        if (Date.now() - started > timeoutMs) {
          reject(new Error(`${label} did not become ready at ${url}`));
          return;
        }
        setTimeout(tick, 400);
      });
    };
    tick();
  });
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

async function ensurePortAvailable(port, label, envVar) {
  if (await portOpen(port)) {
    const escape = envVar ? ` (or set ${envVar} to a free port)` : "";
    fail(`${label} port ${port} is already in use. Stop the existing process${escape} and run npm run dev again.`);
  }
}

function start(label, command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
    env: process.env,
    ...options,
  });
  children.push(child);

  child.stdout.on("data", (chunk) => process.stdout.write(`[${label}] ${chunk}`));
  child.stderr.on("data", (chunk) => process.stderr.write(`[${label}] ${chunk}`));
  child.on("exit", (code) => {
    if (code !== 0 && !shuttingDown) {
      console.error(`[dev] ${label} exited with ${code}`);
      shutdown(1);
    }
  });
  return child;
}

let shuttingDown = false;
function shutdown(code = 0) {
  shuttingDown = true;
  for (const child of children) {
    child.kill("SIGINT");
  }
  setTimeout(() => process.exit(code), 200);
}

process.on("SIGINT", () => shutdown());
process.on("SIGTERM", () => shutdown());

if (!existsSync(path.join(root, "node_modules")) && !existsSync(path.join(MONOREPO_ROOT, "node_modules"))) {
  fail("node_modules is missing. Run npm install from the monorepo root.");
}
ensure("config/lea.local.toml", "config/lea.local.toml is missing. Copy config/lea.local.example.toml.");
ensure("adapter/.venv/bin/python", "adapter virtualenv is missing. Run npm run setup:api.");
ensure("prover/pyproject.toml", "prover/ is missing — this is a broken checkout.");

const nodeMajor = Number(process.versions.node.split(".")[0]);
if (nodeMajor !== 22 && nodeMajor !== 20) {
  console.warn(`[dev] Node ${process.versions.node} detected. Node 22 LTS is recommended.`);
}

// The adapter drives the prover in-process; it reads config/lea.local.toml and
// exports provider keys to its own environment (D1·cfg), so there is nothing to
// inject here.
await ensurePortAvailable(ADAPTER_PORT, "UI adapter API", "LEA_ADAPTER_PORT");
start("adapter", "./.venv/bin/python", ["run_api.py"], { cwd: path.join(root, "adapter") });

try {
  await waitFor(`http://127.0.0.1:${ADAPTER_PORT}/api/health`, "UI adapter API");
  console.log(`[dev] UI adapter API ready at http://127.0.0.1:${ADAPTER_PORT}`);
} catch (error) {
  console.error(`[dev] ${error.message}`);
  shutdown(1);
}

await ensurePortAvailable(5173, "frontend");
start("web", resolveHoisted(".bin", "vite"), ["--host", "0.0.0.0", "--strictPort"]);

try {
  await waitFor("http://127.0.0.1:5173", "frontend");
  console.log("[dev] Frontend ready at http://localhost:5173");
  console.log("[dev] Press Ctrl+C to stop both servers.");
} catch (error) {
  console.error(`[dev] ${error.message}`);
  shutdown(1);
}
