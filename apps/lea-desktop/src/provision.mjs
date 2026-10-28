// First-run (and after-update) provisioning of the managed backend.
//
// Mirrors what the Dockerfile and docker/prepare-mathlib.sh do at image build
// and container start, but into the user's app-support folder:
//
//   runtime     sync the bundle's backend tree into <userData>/app
//   python      uv sync --frozen  → <userData>/venv/adapter (managed Python 3.13)
//   lean        reuse ~/.elan if it has the pinned toolchain, else install elan
//               into <userData>/elan and the toolchain from lean-toolchain
//   mathlib     lake exe cache get + lake build Mathlib + the --no-build gate
//   safeverify  lake build safe_verify against the shared Mathlib (non-fatal)
//
// Every step is idempotent and cheap when already done, so this runs on every
// launch; only the first launch (or a Mathlib bump) does real work.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const ELAN_INIT_URL = 'https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh';

export const STEPS = [
  ['runtime', 'Lea runtime'],
  ['python', 'Python environment'],
  ['lean', 'Lean toolchain'],
  ['mathlib', 'Mathlib library'],
  ['safeverify', 'Proof verifier'],
  ['services', 'Starting Lea'],
];

export function createProvisioner(layout, { uvBinary, onStatus = () => {}, onLog = () => {}, log = console.log }) {
  let current = null;      // the running child, for cancel()
  let cancelled = false;
  const state = readJson(layout.stateFile) || {};

  const status = (step, stateName, message) => onStatus({ step, state: stateName, message });

  async function run() {
    const manifest = readJson(path.join(layout.backendSource, 'manifest.json'));
    if (!manifest) throw new Error(`No backend manifest at ${layout.backendSource}. Run \`npm run build:backend -w apps/lea-desktop\` (dev) or reinstall the app.`);

    status('runtime', 'running', 'Preparing the Lea runtime…');
    const installed = readJson(path.join(layout.appDir, 'manifest.json'));
    if (installed?.id !== manifest.id) {
      log(`syncing backend ${installed?.id || 'none'} → ${manifest.id}`);
      syncTree(manifest, installed);
    }
    ensurePersistentDirs(manifest);
    status('runtime', 'done', `Lea ${manifest.appVersion} (${manifest.gitSha})`);

    status('python', 'running', 'Setting up the Python environment… the first run downloads about 300 MB.');
    await exec(uvBinary, ['sync', '--frozen', '--no-dev', '--project', layout.adapterDir], { env: uvEnv() });
    if (!fs.existsSync(layout.python)) throw new Error(`uv sync finished but ${layout.python} is missing`);
    status('python', 'done', 'Python environment ready');

    status('lean', 'running', 'Checking the Lean toolchain…');
    const lean = await ensureLean(manifest);
    status('lean', 'done', `${manifest.leanToolchain} (${lean.elanHome ? 'managed' : 'from ~/.elan'})`);

    status('mathlib', 'running', 'Checking the Mathlib library…');
    await ensureMathlib(lean, manifest);
    status('mathlib', 'done', 'Mathlib ready');

    status('safeverify', 'running', 'Checking the proof verifier…');
    try {
      await ensureSafeVerify(lean, manifest);
      status('safeverify', 'done', 'SafeVerify ready');
    } catch (error) {
      if (cancelled) throw error;
      log(`SafeVerify unavailable: ${error.message}`);
      status('safeverify', 'failed', 'SafeVerify could not be built; kernel audits are off (see logs)');
    }

    return { env: lean.env, manifest };
  }

  function cancel() {
    cancelled = true;
    if (current?.pid) {
      try { process.kill(-current.pid, 'SIGTERM'); } catch { /* already gone */ }
    }
  }

  // ---- runtime --------------------------------------------------------------

  function syncTree(manifest, installed) {
    const stale = new Set(installed?.files || []);
    for (const rel of manifest.files) {
      const src = path.join(layout.backendSource, rel);
      const dst = path.join(layout.appDir, rel);
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(src, dst);
      fs.chmodSync(dst, fs.statSync(src).mode & 0o777);
      stale.delete(rel);
    }
    for (const rel of stale) {
      const victim = path.join(layout.appDir, rel);
      fs.rmSync(victim, { force: true });
      pruneEmptyDirs(path.dirname(victim));
    }
    const release = path.join(layout.adapterDir, 'app', '_release.json');
    fs.writeFileSync(release, JSON.stringify({ source: `desktop-${manifest.appVersion}-${manifest.gitSha}` }));
    fs.writeFileSync(path.join(layout.appDir, 'manifest.json'), JSON.stringify(manifest, null, 2));
  }

  function pruneEmptyDirs(dir) {
    while (dir.startsWith(layout.appDir) && dir !== layout.appDir) {
      try {
        if (fs.readdirSync(dir).length) return;
        fs.rmdirSync(dir);
      } catch {
        return;
      }
      dir = path.dirname(dir);
    }
  }

  function ensurePersistentDirs() {
    for (const dir of [
      path.join(layout.workspace, 'proofs'),
      path.join(layout.workspace, 'projects'),
      path.join(layout.companionDir, '.overleaf-lean-stub'),
      layout.dataDir,
      path.dirname(layout.configFile),
      layout.uvCacheDir,
      layout.pythonDir,
    ]) fs.mkdirSync(dir, { recursive: true });
    if (!fs.existsSync(layout.configFile) && fs.existsSync(layout.configExample)) {
      fs.copyFileSync(layout.configExample, layout.configFile);
      fs.chmodSync(layout.configFile, 0o600);
    }
  }

  // ---- python ---------------------------------------------------------------

  function uvEnv() {
    return {
      ...baseEnv(),
      UV_PROJECT_ENVIRONMENT: layout.venvDir,
      UV_CACHE_DIR: layout.uvCacheDir,
      UV_PYTHON_INSTALL_DIR: layout.pythonDir,
      UV_PYTHON_PREFERENCE: 'only-managed', // never pick up a stray system Python
      UV_NO_MODIFY_PATH: '1',
    };
  }

  // ---- lean -----------------------------------------------------------------

  async function ensureLean(manifest) {
    const toolchain = manifest.leanToolchain;
    const managedBin = path.join(layout.elanHome, 'bin');
    const homeBin = path.join(os.homedir(), '.elan', 'bin');
    let binDir = [managedBin, homeBin].find((d) => fs.existsSync(path.join(d, 'lake')) && fs.existsSync(path.join(d, 'elan')));
    if (!binDir) {
      status('lean', 'running', 'Installing the Lean toolchain manager (elan)…');
      await installElan();
      binDir = managedBin;
    }
    const elanHome = binDir === managedBin ? layout.elanHome : null;
    const env = leanEnv(binDir, elanHome);
    const installed = await capture(path.join(binDir, 'elan'), ['toolchain', 'list'], { env });
    if (!installed.split('\n').some((line) => line.trim().startsWith(toolchain))) {
      status('lean', 'running', `Downloading Lean ${toolchain.split(':').pop()}… (about 300 MB)`);
      await exec(path.join(binDir, 'elan'), ['toolchain', 'install', toolchain], { env });
    }
    return { binDir, elanHome, env, lake: path.join(binDir, 'lake') };
  }

  async function installElan() {
    const response = await fetch(ELAN_INIT_URL);
    if (!response.ok) throw new Error(`could not download elan-init.sh (HTTP ${response.status})`);
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'lea-elan-'));
    const script = path.join(tmp, 'elan-init.sh');
    fs.writeFileSync(script, await response.text());
    fs.mkdirSync(layout.elanHome, { recursive: true });
    try {
      await exec('sh', [script, '-y', '--default-toolchain', 'none', '--no-modify-path'],
        { env: { ...baseEnv(), ELAN_HOME: layout.elanHome } });
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
    if (!fs.existsSync(path.join(layout.elanHome, 'bin', 'elan'))) throw new Error('elan did not install');
  }

  function leanEnv(binDir, elanHome) {
    const env = { ...baseEnv() };
    env.PATH = `${binDir}${path.delimiter}${env.PATH}`;
    if (elanHome) env.ELAN_HOME = elanHome;
    else delete env.ELAN_HOME;
    return env;
  }

  // ---- mathlib --------------------------------------------------------------

  async function ensureMathlib(lean, manifest) {
    const key = manifest.mathlibKey;
    const cwd = layout.workspace;
    const env = { ...lean.env, MATHLIB_CACHE_DIR: layout.mathlibCacheDir };
    if (state.mathlibKey === key) {
      status('mathlib', 'running', 'Verifying the Mathlib cache…');
      if (await succeeds(lean.lake, ['build', '--no-build', 'Mathlib'], { cwd, env })) return;
      log('Mathlib cache incomplete or stale; repairing');
    }
    status('mathlib', 'running', 'Downloading the Mathlib library… the first run fetches several GB and takes a while.');
    try {
      await exec(lean.lake, ['exe', 'cache', 'get'], { cwd, env });
    } catch (error) {
      if (cancelled) throw error;
      log(`cache get failed once (${error.message}); retrying`);
      await exec(lean.lake, ['exe', 'cache', 'get'], { cwd, env });
    }
    status('mathlib', 'running', 'Compiling anything the cache did not cover…');
    await exec(lean.lake, ['build', 'Mathlib'], { cwd, env });
    await exec(lean.lake, ['build', '--no-build', 'Mathlib'], { cwd, env });
    state.mathlibKey = key;
    writeState();
    fs.rmSync(layout.mathlibCacheDir, { recursive: true, force: true });
  }

  // ---- safeverify -----------------------------------------------------------

  async function ensureSafeVerify(lean, manifest) {
    const dir = layout.safeVerifyDir;
    const binary = path.join(dir, '.lake', 'build', 'bin', 'safe_verify');
    // Share the workspace's Mathlib instead of downloading a second copy (as the Dockerfile does).
    const packages = path.join(dir, '.lake', 'packages');
    const link = path.join('..', '..', '..', 'workspace', '.lake', 'packages');
    fs.mkdirSync(path.join(dir, '.lake'), { recursive: true });
    let current = null;
    try { current = fs.readlinkSync(packages); } catch { /* not a symlink */ }
    if (current !== link) {
      fs.rmSync(packages, { recursive: true, force: true });
      fs.symlinkSync(link, packages);
    }
    if (state.safeverifyKey === manifest.mathlibKey && fs.existsSync(binary)) return;
    status('safeverify', 'running', 'Building the proof verifier… a few minutes, once.');
    await exec(lean.lake, ['build', 'safe_verify'], { cwd: dir, env: lean.env });
    if (!fs.existsSync(binary)) throw new Error('safe_verify binary missing after build');
    state.safeverifyKey = manifest.mathlibKey;
    writeState();
  }

  // ---- helpers --------------------------------------------------------------

  function baseEnv() {
    const env = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    const home = os.homedir();
    const extra = [path.join(home, '.local', 'bin'), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'];
    env.PATH = [...new Set([...(env.PATH || '').split(path.delimiter).filter(Boolean), ...extra])].join(path.delimiter);
    return env;
  }

  function writeState() {
    fs.mkdirSync(path.dirname(layout.stateFile), { recursive: true });
    fs.writeFileSync(layout.stateFile, JSON.stringify(state, null, 2));
  }

  function exec(command, args, { cwd, env } = {}) {
    return new Promise((resolve, reject) => {
      if (cancelled) return reject(new Error('cancelled'));
      log(`$ ${path.basename(command)} ${args.join(' ')}${cwd ? `  (in ${cwd})` : ''}`);
      const child = spawn(command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
      current = child;
      const tail = [];
      const onLine = (line) => {
        if (!line.trim()) return;
        tail.push(line);
        if (tail.length > 8) tail.shift();
        onLog(line);
      };
      for (const stream of [child.stdout, child.stderr]) {
        let buffer = '';
        stream.on('data', (chunk) => {
          buffer += chunk.toString();
          const lines = buffer.split(/\r?\n|\r/);
          buffer = lines.pop();
          lines.forEach(onLine);
        });
        stream.on('end', () => { if (buffer) onLine(buffer); });
      }
      child.on('error', (error) => { current = null; reject(error); });
      child.on('exit', (code, signal) => {
        current = null;
        if (code === 0) resolve();
        else reject(new Error(`${path.basename(command)} ${args.join(' ')} failed (${signal || `exit ${code}`})\n${tail.join('\n')}`));
      });
    });
  }

  async function succeeds(command, args, options) {
    try { await exec(command, args, options); return true; } catch (error) { if (cancelled) throw error; return false; }
  }

  function capture(command, args, { env } = {}) {
    return new Promise((resolve, reject) => {
      const child = spawn(command, args, { env, stdio: ['ignore', 'pipe', 'pipe'] });
      let out = '';
      child.stdout.on('data', (c) => { out += c; });
      child.stderr.on('data', (c) => { out += c; });
      child.on('error', reject);
      child.on('exit', (code) => (code === 0 ? resolve(out) : reject(new Error(`${path.basename(command)} ${args.join(' ')} exited ${code}: ${out.slice(-400)}`))));
    });
  }

  return { run, cancel };
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}
