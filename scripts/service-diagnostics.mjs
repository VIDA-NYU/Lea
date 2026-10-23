// Supervisor-owned diagnostics also survive a worker SIGKILL/native crash.
import { appendFileSync, chmodSync, existsSync, mkdirSync, renameSync, rmSync, statSync } from 'node:fs';
import path from 'node:path';

export function createServiceDiagnostics(directory, { maxBytes = 2 * 1024 * 1024, backups = 2, secrets = [] } = {}) {
  function redact(text) {
    for (const secret of secrets.filter(value => typeof value === 'string' && value.length >= 8)) text = text.replaceAll(secret, '[redacted]');
    return text.replace(/\b(?:sk-(?:ant-)?[A-Za-z0-9_./+=-]{8,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,})/g, '[redacted]');
  }
  function append(name, text) {
    try {
      mkdirSync(directory, { recursive: true, mode: 0o700 });
      const file = path.join(directory, name);
      text = redact(text).slice(-32768);
      if (existsSync(file) && statSync(file).size + Buffer.byteLength(text) > maxBytes) {
        rmSync(file + '.' + backups, { force: true });
        for (let i = backups - 1; i >= 1; i--) if (existsSync(file + '.' + i)) renameSync(file + '.' + i, file + '.' + (i + 1));
        renameSync(file, file + '.1');
      }
      appendFileSync(file, text, { mode: 0o600 });
      chmodSync(file, 0o600);
    } catch (error) { process.stderr.write('[supervisor] Could not persist diagnostics: ' + error.code + '\n'); }
  }
  function event(event, details = {}) {
    const failed = event === 'service.spawn_failed' || event === 'service.restart_limit' ||
      (event === 'service.exited' && (details.code !== 0 || details.signal));
    append('supervisor-events.jsonl', JSON.stringify({ at: new Date().toISOString(), event,
      severity: failed ? 'error' : 'info', ...details }) + '\n');
  }
  function attach(label, child) {
    event('service.started', { service: label, pid: child.pid });
    let pending = '';
    child.stderr?.on('data', chunk => {
      process.stderr.write(chunk);
      pending += chunk.toString();
      let end;
      while ((end = pending.indexOf('\n')) >= 0) {
        append(label + '.stderr.log', pending.slice(0, end + 1));
        pending = pending.slice(end + 1);
      }
      if (pending.length > 32768) { append(label + '.stderr.log', pending); pending = ''; }
    });
    child.on('close', () => { if (pending) append(label + '.stderr.log', pending); });
    child.on('error', error => event('service.spawn_failed', { service: label, pid: child.pid, message: error.message }));
    child.on('exit', (code, signal) => event('service.exited', { service: label, pid: child.pid, code, signal }));
  }
  return { event, attach };
}
