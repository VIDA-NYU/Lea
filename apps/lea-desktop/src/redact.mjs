// Redaction of lea.local.toml for the diagnostics bundle (Help → Report a
// Problem…). Pure — no Electron — so `npm test` covers it.
//
// The structure of the config survives (which tables and keys are set, every
// non-secret value such as the model name or a server's base URL); what goes:
//   - every value under [keys] and [environment] (and their sub-tables): users
//     put arbitrary variables there, so none of it is safe to keep;
//   - any key with a secret-shaped word in its name (key, token, secret,
//     password, credential, auth, bearer — as a word: `api_key` and `apiKey`
//     yes, `PORTKEY_BASE_URL` no) in any table, including dotted and quoted
//     keys and keys that are commented out (people comment a real key out to
//     switch providers, which keeps it in the file);
//   - the user:password part of a URL in any value that is kept.
// A redacted multi-line string ("""…""") is dropped whole.
const SECRET_WORD = /^(key|apikey|token|secret|passw|credential|auth|bearer)/;
const SECRET_TABLES = ['keys', 'environment'];
const URL_USERINFO = /(\b[a-z][a-z0-9+.-]*:\/\/)[^\s/@"']+@/gi;

export const REDACTED = '"<redacted>"';

export function redactToml(text) {
  let table = ''; // the table real assignments belong to
  let commentedTable = ''; // the table a commented-out assignment belongs to
  let closing = null; // delimiter that ends a multi-line string being dropped
  const out = [];
  for (const line of String(text).split('\n')) {
    if (closing) {
      if (line.includes(closing)) closing = null;
      continue;
    }
    const header = line.match(/^\s*(#\s*)?\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$/);
    if (header) {
      const name = header[2].trim().replace(/^"|"$/g, '').toLowerCase();
      if (header[1]) commentedTable = name;
      else { table = name; commentedTable = name; }
      out.push(line);
      continue;
    }
    const assignment = line.match(/^(\s*)(#\s*)?("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_.-]+)(\s*=\s*)(.*)$/);
    if (!assignment) {
      out.push(line);
      continue;
    }
    const [, indent, comment = '', rawKey, eq, value] = assignment;
    const key = rawKey.replace(/^["']|["']$/g, '');
    const trimmed = value.trim();
    if (trimmed === '' || trimmed.startsWith('#')) {
      out.push(line);
      continue;
    }
    const inSecretTable = isSecretTable(comment ? commentedTable : table);
    if (inSecretTable || isSecretKey(key)) {
      out.push(`${indent}${comment}${rawKey}${eq}${REDACTED}`);
      const open = trimmed.match(/^("""|''')/);
      if (open && !trimmed.slice(3).includes(open[1])) closing = open[1];
      continue;
    }
    out.push(`${indent}${comment}${rawKey}${eq}${value.replace(URL_USERINFO, '$1<redacted>@')}`);
  }
  return out.join('\n');
}

function isSecretTable(name) {
  return SECRET_TABLES.some((t) => name === t || name.startsWith(`${t}.`));
}

// Words of a key: split on separators and camelCase, then match each word.
function isSecretKey(key) {
  return key
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[^A-Za-z0-9]+/)
    .some((word) => SECRET_WORD.test(word.toLowerCase()));
}
