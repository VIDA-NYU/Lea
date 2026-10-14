// Match the adapter's loopback Host policy without changing Overleaf's Origin policy.
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0']);

function authority(value) {
  if (typeof value !== 'string' || !value || /[\s/@\\?#,]/.test(value)) return null;
  const match = value.startsWith('[')
    ? /^\[([^\]]+)\](?::([0-9]+))?$/.exec(value)
    : /^([^:]+)(?::([0-9]+))?$/.exec(value);
  if (!match || (match[2] && Number(match[2]) > 65535)) return null;
  try {
    const url = new URL(`http://${value}`);
    if (!url.hostname) return null;
    return {host: match[1].toLowerCase(), port: match[2] || null};
  } catch { return null; }
}

export function isAllowedHost(value, env = {}) {
  // HTTP/1.1 browser requests always supply Host; missing headers are local tooling.
  if (value === undefined || value === null || value === '') return true;
  const parsed = authority(value);
  if (!parsed) return false;
  if (LOCAL_HOSTS.has(parsed.host)) return true;
  return String(env.LEA_ALLOWED_HOSTS || '').split(',').some(item => {
    const allowed = authority(item.trim());
    return allowed && allowed.host === parsed.host && (!allowed.port || allowed.port === parsed.port);
  });
}
