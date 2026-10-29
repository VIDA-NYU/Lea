// "Report a problem": bundle what a maintainer needs to reproduce an issue into
// one zip the user can attach to a GitHub issue. Everything secret-shaped in the
// config is redacted before it is copied; the database and the workspace are
// never included.
import { app } from 'electron';
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

// Log files are appended across launches; keep only the tail of each.
const LOG_TAIL_BYTES = 2 * 1024 * 1024;
const SECRET_KEY = /key|token|secret|password|credential/i;

export const ISSUES_URL = 'https://github.com/VIDA-NYU/Lea/issues/new';

export async function writeDiagnostics({ layout, logDir, runtime, backend, zipPath }) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const stage = path.join(os.tmpdir(), `lea-diagnostics-${stamp}`);
  fs.mkdirSync(stage, { recursive: true });
  try {
    fs.writeFileSync(path.join(stage, 'about.txt'), await aboutText({ layout, runtime, backend }));

    if (logDir && fs.existsSync(logDir)) {
      const logs = path.join(stage, 'logs');
      fs.mkdirSync(logs);
      for (const name of fs.readdirSync(logDir)) {
        if (!name.endsWith('.log')) continue;
        copyTail(path.join(logDir, name), path.join(logs, name));
      }
    }

    if (layout.stateFile && fs.existsSync(layout.stateFile)) {
      fs.copyFileSync(layout.stateFile, path.join(stage, 'state.json'));
    }
    if (fs.existsSync(layout.configFile)) {
      fs.writeFileSync(path.join(stage, 'lea.local.redacted.toml'), redactToml(fs.readFileSync(layout.configFile, 'utf8')));
    }
    for (const name of ['lean-toolchain', 'lake-manifest.json']) {
      const source = path.join(layout.workspace, name);
      if (fs.existsSync(source)) fs.copyFileSync(source, path.join(stage, name));
    }

    await zipDirectory(stage, zipPath);
    return zipPath;
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }
}

export function suggestedZipName() {
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  return `Lea-diagnostics-${stamp}.zip`;
}

export function issueUrl({ runtime }) {
  const body = [
    '**What happened**',
    '',
    '',
    '**Steps to reproduce**',
    '',
    '',
    '**Environment**',
    `- Lea ${app.getVersion()} (${app.isPackaged ? 'packaged' : 'dev'}), backend ${runtime?.mode ?? 'not started'}`,
    `- ${os.type()} ${os.release()} ${process.arch}`,
    '',
    'Diagnostics zip from Help → Report a problem… attached below.',
  ].join('\n');
  const params = new URLSearchParams({ title: 'Desktop: ', body });
  return `${ISSUES_URL}?${params.toString()}`;
}

async function aboutText({ layout, runtime, backend }) {
  const health = runtime ? await fetchJson(`${runtime.apiUrl}/api/health`) : null;
  const lines = [
    `Lea ${app.getVersion()} (${app.isPackaged ? 'packaged' : 'dev'})`,
    `Generated ${new Date().toISOString()}`,
    `Electron ${process.versions.electron} · Chrome ${process.versions.chrome} · Node ${process.versions.node}`,
    `${os.type()} ${os.release()} ${process.arch} · ${os.cpus().length} cores · ${Math.round(os.totalmem() / 2 ** 30)} GB RAM`,
    `Locale ${app.getLocale()} · uptime ${Math.round(process.uptime())} s`,
    '',
    `Layout: ${layout.mode}`,
    `  userData: ${layout.userData ?? '(dev checkout)'}`,
    `  workspace: ${layout.workspace}`,
    `  config: ${layout.configFile}`,
    '',
    `Backend: ${runtime ? `${runtime.mode} · api ${runtime.apiUrl} · ui ${runtime.uiUrl} · companion ${runtime.companionUrl}` : 'not started'}`,
    `  processes owned by the app: ${backend?.processes?.length ? backend.processes.join(', ') : 'none'}`,
    `  health: ${health ? JSON.stringify(health) : 'no answer'}`,
  ];
  return `${lines.join('\n')}\n`;
}

function copyTail(source, target) {
  const size = fs.statSync(source).size;
  if (size <= LOG_TAIL_BYTES) {
    fs.copyFileSync(source, target);
    return;
  }
  const fd = fs.openSync(source, 'r');
  try {
    const buffer = Buffer.alloc(LOG_TAIL_BYTES);
    fs.readSync(fd, buffer, 0, LOG_TAIL_BYTES, size - LOG_TAIL_BYTES);
    fs.writeFileSync(target, `[... ${size - LOG_TAIL_BYTES} earlier bytes omitted ...]\n${buffer.toString('utf8')}`);
  } finally {
    fs.closeSync(fd);
  }
}

// Keep the structure of the config (which tables and keys are set) but never a
// value that could be a credential. Everything under [environment] is treated as
// secret since users put arbitrary variables there.
export function redactToml(text) {
  let table = '';
  return text
    .split('\n')
    .map((line) => {
      const header = line.match(/^\s*\[([^\]]+)\]/);
      if (header) {
        table = header[1].trim().toLowerCase();
        return line;
      }
      const assignment = line.match(/^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.*)$/);
      if (!assignment) return line;
      const [, indent, key, eq, value] = assignment;
      if (value.trim().startsWith('#') || value.trim() === '') return line;
      if (table === 'environment' || table === 'keys' || SECRET_KEY.test(key)) {
        return `${indent}${key}${eq}"<redacted>"`;
      }
      return line;
    })
    .join('\n');
}

async function zipDirectory(dir, zipPath) {
  fs.rmSync(zipPath, { force: true });
  if (process.platform === 'darwin') {
    await execFileAsync('ditto', ['-c', '-k', '--sequesterRsrc', dir, zipPath]);
  } else if (process.platform === 'win32') {
    await execFileAsync('powershell.exe', ['-NoProfile', '-Command',
      `Compress-Archive -Path '${dir.replace(/'/g, "''")}\\*' -DestinationPath '${zipPath.replace(/'/g, "''")}'`]);
  } else {
    await execFileAsync('zip', ['-r', '-q', zipPath, '.'], { cwd: dir });
  }
}

async function fetchJson(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(2000) });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}
