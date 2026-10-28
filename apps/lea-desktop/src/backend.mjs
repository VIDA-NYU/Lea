// The desktop shell's view of the Lea backend.
//
// Two modes:
//   attached — a healthy adapter already answers on 127.0.0.1:8001 (for example
//              `./start-dev.sh` in a terminal). The shell only opens a window on it.
//   spawned  — the shell owns the stack itself: bootstrap.py once, then the
//              worker, the API (serving the built UI), and the Overleaf companion,
//              exactly as scripts/supervisor.mjs does for `npm run dev:ui`.
//
// Process ownership, process-group signalling and bounded shutdown are the
// repo's existing createProcessSupervisor; nothing about the backend changes.
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createProcessSupervisor } from '../../../scripts/process-supervisor.mjs';
import { serviceEnvironment } from '../../../scripts/service-env.mjs';
import { loadRootEnv } from '../../../scripts/env.mjs';
import { portAvailable } from '../../../scripts/dev-stack-preflight.mjs';

export const API_PORT = 8001;
export const VITE_PORT = 5173;
export const COMPANION_PORT = 31245;

export function createBackend({ root, logDir, log = console.log, onStatus = () => {}, onCrash = () => {} }) {
  const standaloneDir = path.join(root, 'apps/lea-standalone');
  const adapterDir = path.join(standaloneDir, 'adapter');
  const companionDir = path.join(root, 'apps/overleaf-extension');
  const python = path.join(adapterDir, '.venv/bin/python');
  const webDist = path.join(standaloneDir, 'dist');
  const apiUrl = `http://127.0.0.1:${API_PORT}`;
  const companionUrl = `http://127.0.0.1:${COMPANION_PORT}`;

  let owner = null;
  let mode = null;
  const children = new Map();

  async function start() {
    const policy = String(process.env.LEA_DESKTOP_ATTACH || 'auto').toLowerCase();
    const never = ['0', 'never', 'false', 'no'].includes(policy);
    const always = ['1', 'always', 'true', 'yes'].includes(policy);
    if (!never) {
      const existing = await detectExisting();
      if (existing) {
        mode = 'attached';
        return existing;
      }
      if (always) {
        throw new Error(`LEA_DESKTOP_ATTACH=${policy} but no running Lea adapter answered on ${apiUrl}.`);
      }
    }
    mode = 'spawned';
    return spawnStack();
  }

  // ---- attached -------------------------------------------------------------

  async function detectExisting() {
    const health = await fetchJson(`${apiUrl}/api/health`, 1500);
    if (!health || health.ok !== true) return null;
    log(`a Lea adapter is already running on :${API_PORT}; attaching to it`);
    // Dev stacks serve the UI from Vite; container-style stacks serve dist/ from the API.
    if (await fetchOk(`http://localhost:${VITE_PORT}/`, 1500)) {
      return { mode: 'attached', apiUrl, uiUrl: `http://localhost:${VITE_PORT}`, companionUrl };
    }
    if (await servesHtml(`${apiUrl}/`, 1500)) {
      return { mode: 'attached', apiUrl, uiUrl: apiUrl, companionUrl };
    }
    throw new Error(
      `A Lea adapter is running on :${API_PORT} but nothing serves the UI ` +
      `(no Vite on :${VITE_PORT} and the adapter has no LEA_WEB_DIST). ` +
      `Stop that adapter and let the app start its own stack, or run \`npm run dev:ui\`.`,
    );
  }

  // ---- spawned --------------------------------------------------------------

  async function spawnStack() {
    await preflight();
    const env = baseEnvironment();
    ensureLocalConfig();
    fs.mkdirSync(logDir, { recursive: true });

    onStatus({ step: 'bootstrap', message: 'Preparing the database…' });
    await runOnce(python, ['bootstrap.py'], { cwd: adapterDir, env });

    owner = createProcessSupervisor({ prefix: '[lea-desktop]', onUnexpectedExit: handleUnexpectedExit });

    onStatus({ step: 'worker', message: 'Starting the Lea worker…' });
    launch('worker', python, ['run_worker.py'], adapterDir, {
      ...serviceEnvironment('worker', env),
      // The worker's private companion child is a Node script; run it with the
      // app's own binary so a Finder launch needs no Node on PATH.
      LEA_NODE: process.execPath,
      ELECTRON_RUN_AS_NODE: '1',
    });

    onStatus({ step: 'api', message: 'Starting the API…' });
    launch('adapter', python, ['run_api.py', '--log-level', 'info'], adapterDir, {
      ...serviceEnvironment('adapter', env),
      LEA_WEB_DIST: webDist,
    });

    const companionScript = path.join(companionDir, 'companion/server.mjs');
    if (fs.existsSync(companionScript) && await portAvailable({ host: '127.0.0.1', port: COMPANION_PORT })) {
      onStatus({ step: 'companion', message: 'Starting the Overleaf companion…' });
      launch('companion', process.execPath, [companionScript], companionDir, {
        ...serviceEnvironment('companion', env),
        ELECTRON_RUN_AS_NODE: '1',
        LEA_API_BASE_URL: apiUrl,
        // "Open in Lea" links from Overleaf land on the adapter-served UI until the
        // lea:// protocol is registered by a packaged build.
        LEA_UI_BASE_URL: apiUrl,
      });
    } else {
      log(`companion not started (port ${COMPANION_PORT} busy or script missing)`);
    }

    onStatus({ step: 'health', message: 'Waiting for the API…' });
    await waitFor(async () => (await fetchJson(`${apiUrl}/api/health`, 1500))?.ok === true,
      { timeoutMs: 90_000, label: 'API' });

    onStatus({ step: 'ready', message: 'Waiting for the Lean workspace… the first start after a reboot can take a minute.' });
    await waitFor(async () => (await fetchStatus(`${apiUrl}/api/ready`, 1500)) === 200,
      { timeoutMs: 15 * 60_000, label: 'worker' });

    return { mode: 'spawned', apiUrl, uiUrl: apiUrl, companionUrl };
  }

  async function preflight() {
    if (!fs.existsSync(python)) {
      throw new Error(`The adapter virtualenv is missing:\n${python}\n\nRun \`npm run setup\` in the repository first.`);
    }
    if (!fs.existsSync(path.join(webDist, 'index.html'))) {
      throw new Error(`The UI is not built:\n${webDist}\n\nRun \`npm run build -w apps/lea-standalone\` first.`);
    }
    if (!(await portAvailable({ host: '127.0.0.1', port: API_PORT }))) {
      throw new Error(`Port ${API_PORT} is in use, but whatever holds it is not a healthy Lea adapter.`);
    }
  }

  function baseEnvironment() {
    loadRootEnv(); // <repo>/.env, never overriding what the shell already had
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    env.PYTHONUNBUFFERED = '1';
    // A Finder launch inherits a minimal PATH; make sure lake, lean, uv and git resolve.
    const home = os.homedir();
    const extra = [
      path.join(home, '.elan/bin'), path.join(home, '.local/bin'), path.join(home, '.cargo/bin'),
      '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin',
    ];
    env.PATH = [...new Set([...(env.PATH || '').split(path.delimiter).filter(Boolean), ...extra])].join(path.delimiter);
    return env;
  }

  // Same as start-dev.sh: the Settings pane stores provider keys here, so the
  // file is created owner-only from the example the first time.
  function ensureLocalConfig() {
    const target = path.join(standaloneDir, 'config/lea.local.toml');
    const example = path.join(standaloneDir, 'config/lea.local.example.toml');
    if (fs.existsSync(target) || !fs.existsSync(example)) return;
    fs.copyFileSync(example, target);
    fs.chmodSync(target, 0o600);
    log(`created ${target} from the example config`);
  }

  function launch(label, command, args, cwd, env) {
    const child = owner.spawn(label, command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] });
    const file = fs.createWriteStream(path.join(logDir, `${label}.log`), { flags: 'a' });
    file.write(`\n--- ${new Date().toISOString()} pid ${child.pid} ---\n`);
    for (const stream of [child.stdout, child.stderr]) {
      stream.pipe(file, { end: false });
      stream.on('data', (chunk) => process.stderr.write(`[${label}] ${chunk}`));
    }
    child.on('exit', () => file.end());
    children.set(label, child);
    log(`${label} started (pid ${child.pid})`);
    return child;
  }

  function handleUnexpectedExit(label, code, signal) {
    children.delete(label);
    if (owner?.stopping) return true;
    log(`${label} exited (${signal || code})`);
    onCrash(label, { code, signal });
    return true; // keep the other services; the shell decides what to do
  }

  // Returns true when a supervised stack is shutting down (the supervisor exits
  // the process once every child is gone), false when there is nothing to stop.
  function stop(code = 0) {
    if (mode !== 'spawned' || !owner) return false;
    owner.stop(code);
    return true;
  }

  return {
    start,
    stop,
    get mode() { return mode; },
    get apiUrl() { return apiUrl; },
    get companionUrl() { return companionUrl; },
    get processes() { return [...children.keys()]; },
  };
}

// ---- small helpers ----------------------------------------------------------

function runOnce(command, args, options) {
  return new Promise((resolve, reject) => {
    execFile(command, args, { ...options, maxBuffer: 64 * 1024 * 1024 }, (error, stdout, stderr) => {
      if (stdout) process.stderr.write(`[${path.basename(args[0] || command)}] ${stdout}`);
      if (stderr) process.stderr.write(`[${path.basename(args[0] || command)}] ${stderr}`);
      if (error) reject(new Error(`${args.join(' ')} failed: ${error.message}\n${stderr || ''}`.trim()));
      else resolve();
    });
  });
}

async function waitFor(probe, { timeoutMs, intervalMs = 750, label }) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await probe().catch(() => false)) return;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`Timed out after ${Math.round(timeoutMs / 1000)}s waiting for the ${label}.`);
}

async function fetchJson(url, timeoutMs) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    return await response.json();
  } catch {
    return null;
  }
}

async function fetchStatus(url, timeoutMs) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    return response.status;
  } catch {
    return 0;
  }
}

async function fetchOk(url, timeoutMs) {
  return (await fetchStatus(url, timeoutMs)) === 200;
}

async function servesHtml(url, timeoutMs) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    return response.ok && /text\/html/i.test(response.headers.get('content-type') || '');
  } catch {
    return false;
  }
}
