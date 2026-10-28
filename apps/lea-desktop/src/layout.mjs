// Where the backend lives, in the two ways the app can run.
//
// dev      — straight from the repository checkout, using its own adapter venv
//            and Lean workspace (what `npm start` does unpackaged).
// managed  — from the app-support folder. The bundle's backend tree is synced
//            into <userData>/app (Docker layout); Python, Lean and Mathlib are
//            provisioned next to it on first run. Persistent paths (workspace,
//            proofs, Mathlib cache, SafeVerify binary, venv, data, config) are
//            never touched by an app update.
import path from 'node:path';

export function devLayout(repoRoot) {
  const standalone = path.join(repoRoot, 'apps', 'lea-standalone');
  const proverRoot = path.join(standalone, 'prover');
  return {
    mode: 'dev',
    adapterDir: path.join(standalone, 'adapter'),
    python: path.join(standalone, 'adapter', '.venv', 'bin', 'python'),
    webDist: path.join(standalone, 'dist'),
    companionDir: path.join(repoRoot, 'apps', 'overleaf-extension'),
    proverRoot,
    workspace: path.join(proverRoot, 'workspace'),
    configFile: path.join(standalone, 'config', 'lea.local.toml'),
    configExample: path.join(standalone, 'config', 'lea.local.example.toml'),
    dataDir: path.join(standalone, 'data'),
    env: {},
  };
}

export function managedLayout(userData, backendSource) {
  const appDir = path.join(userData, 'app');
  const proverRoot = path.join(appDir, 'prover');
  const venvDir = path.join(userData, 'venv', 'adapter');
  const dataDir = path.join(userData, 'data');
  const configFile = path.join(userData, 'config', 'lea.local.toml');
  return {
    mode: 'managed',
    userData,
    backendSource,
    appDir,
    adapterDir: path.join(appDir, 'adapter'),
    python: path.join(venvDir, 'bin', 'python'),
    venvDir,
    webDist: path.join(appDir, 'dist'),
    companionDir: path.join(appDir, 'apps', 'overleaf-extension'),
    proverRoot,
    workspace: path.join(proverRoot, 'workspace'),
    safeVerifyDir: path.join(proverRoot, 'third_party', 'SafeVerify'),
    configFile,
    configExample: path.join(appDir, 'config', 'lea.local.example.toml'),
    dataDir,
    uvCacheDir: path.join(userData, 'uv-cache'),
    pythonDir: path.join(userData, 'python'),
    elanHome: path.join(userData, 'elan'),
    mathlibCacheDir: path.join(userData, 'mathlib-cache'),
    stateFile: path.join(userData, 'state.json'),
    env: {
      LEA_ROOT: proverRoot,
      LEA_SHARED_DATA_DIR: dataDir,
      LEA_CONFIG_FILE: configFile,
    },
  };
}
