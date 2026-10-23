// Conservative lexical binding for the checked stub. Lean owns compilation;
// this prevents a helper's sorry or a same-named definition satisfying the target.
export function scrubLeanSource(source) {
  let out = '', depth = 0, string = false;
  for (let i = 0; i < source.length; i++) {
    const ch = source[i], pair = source.slice(i, i + 2);
    if (depth) {
      if (pair === '/-') { depth++; out += '  '; i++; }
      else if (pair === '-/') { depth--; out += '  '; i++; }
      else out += ch === '\n' ? '\n' : ' ';
    } else if (string) {
      if (ch === '\\' && i + 1 < source.length) { out += '  '; i++; }
      else { if (ch === '"') string = false; out += ch === '\n' ? '\n' : ' '; }
    } else if (pair === '/-') { depth = 1; out += '  '; i++; }
    else if (pair === '--') {
      while (i < source.length && source[i] !== '\n') { out += ' '; i++; }
      if (i < source.length) out += '\n';
    } else if (ch === '"') { string = true; out += ' '; }
    else out += ch;
  }
  return out;
}

export function stubDeclaration(source, wantedName) {
  const code = String(source || ''), clean = scrubLeanSource(code);
  const lines = clean.split('\n'), declarations = [], scopes = [];
  let offset = 0;
  for (const line of lines) {
    const command = line.match(/^\s*(namespace|section|end)\b\s*([\w.']*)/);
    const declaration = line.match(/^\s*(?:@\[[^\n]*\]\s*)*(?:(?:private|protected|noncomputable|unsafe|partial|local|scoped)\s+)*(theorem|lemma|def|abbrev|structure|class|inductive|coinductive|instance|opaque|axiom|example)\b(?:\s+([A-Za-z_][A-Za-z0-9_'.]*))?/);
    const topLevelCommand = /^\s*(?:#|(?:open|variable|universe|attribute|set_option)\b)/.test(line)
      && declarations.length && line.search(/\S/) <= declarations.at(-1).indent;
    if (command || declaration || topLevelCommand) {
      if (declarations.length) declarations.at(-1).end = Math.min(declarations.at(-1).end, offset);
    }
    if (command) {
      if (command[1] === 'end') scopes.pop();
      else scopes.push(command[1] === 'namespace' ? command[2] : '');
    }
    if (declaration) {
      const name = declaration[2] || '';
      declarations.push({ keyword: declaration[1], name,
        fullName: [...scopes.filter(Boolean), name].join('.'), indent: line.search(/\S/),
        start: offset, end: clean.length });
    }
    offset += line.length + 1;
  }
  const matches = declarations.filter(item => wantedName.includes('.')
    ? item.fullName === wantedName || item.name === wantedName : item.name.split('.').at(-1) === wantedName);
  if (matches.length !== 1 || !['theorem', 'lemma'].includes(matches[0].keyword)) return null;
  const target = matches[0], span = clean.slice(target.start, target.end);
  let depth = 0, delimiter = -1;
  for (let i = 0; i < span.length - 1; i++) {
    if ('([{'.includes(span[i])) depth++;
    else if (')]}'.includes(span[i])) depth--;
    else if (depth === 0 && span.slice(i, i + 2) === ':=') { delimiter = i; break; }
  }
  if (delimiter < 0 || !/\b(?:sorry|admit)\b/.test(span.slice(delimiter + 2))) return null;
  return { name: target.name, fullName: target.fullName,
    statement: code.slice(target.start, target.start + delimiter).trim() };
}
