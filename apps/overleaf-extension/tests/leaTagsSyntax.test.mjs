import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { parseTargetDocument, parseTargets, hashTargetText } from '../shared/theoremParser.mjs';
import { buildLeanPaneManifest } from '../shared/leanPaneManifest.mjs';
import { associateProofSourcesCore } from '../extension/proofSourceCore.mjs';
import { buildFormalizationSourceBundle } from '../shared/proofSource.mjs';
import { stripLeaTargetText, parseBalancedSuffix } from '../extension/targetParserCore.mjs';

const document = body => '\\usepackage{lea-tags}\n\\begin{document}\n' + body + '\n\\end{document}';
const commands = ['lea', 'leatheorem', 'lealemma', 'leaproposition', 'leacorollary', 'leadefinition'];

for (const command of commands) {
  for (const newline of ['\n', '\r\n']) {
    test(command + ' accepts multiline, commented metadata and body arguments with ' + JSON.stringify(newline), () => {
      const body = '\\' + command + String.raw`% argument follows
{
  label=ex% join the identifier; these } braces are ignored
ample,
  uses={first, second},
  context={Use {nested} groups, \{literal\} braces, \%, and \\}
}% ignore { unmatched comment braces
{A statement with $\{x\}$.}`;
      const source = document(body).replaceAll('\n', newline);
      const result = parseTargetDocument(source);
      assert.deepEqual(result.diagnostics, []);
      assert.equal(result.targets.length, 1);
      const [target] = result.targets;
      assert.equal(target.targetLabel, 'example');
      assert.equal(target.targetKind, command === 'leadefinition' ? 'definition' : 'theorem');
      assert.deepEqual(target.targetUses, ['first', 'second']);
      assert.equal(target.targetContext, String.raw`Use {nested} groups, \{literal\} braces, \%, and \\`);
      assert.equal(target.targetText, String.raw`A statement with $\{x\}$.`);
      assert.equal(source.slice(target.from, target.from + command.length + 1), '\\' + command);
      assert.equal(source.slice(target.bodyFrom, target.bodyTo), target.targetText);
      assert.equal(source.slice(target.to - 1, target.to), '}');
    });
  }
}

test('environment tags strip multiline metadata without changing statement text or hashes', () => {
  const single = document(String.raw`\begin{claim}\label{clm:sample}
\leatheorem{label=sample, uses={first}, context={Use {nested} groups.}}
A statement.
\end{claim}`);
  const multi = single.replace(
    String.raw`\leatheorem{label=sample, uses={first}, context={Use {nested} groups.}}`,
    String.raw`\leatheorem% metadata follows
{
label=sample, % ignore }
uses={first},
context={Use {nested} groups.}
}`
  );
  const [original] = parseTargets(single);
  const [changed] = parseTargets(multi);
  for (const key of ['targetLabel', 'targetKind', 'targetText', 'sourceHash', 'targetUses', 'targetContext']) {
    assert.deepEqual(changed[key], original[key], key);
  }
  assert.equal(changed.targetText, 'A statement.');
  assert.equal(parseBalancedSuffix('{first\nsecond}', 0).ok, false);
});

test('commented valid and malformed tags do not create diagnostics or pane items', () => {
  const source = document(String.raw`% \leatheorem{label=old}{Old statement.}
% \lealemma{label=broken
\begin{claim}
% \lea{kind=bogus, label=phantom}
Unmarked text.
\end{claim}
\leatheorem{label=current}{Current statement.}`);
  const parsed = parseTargetDocument(source);
  assert.deepEqual(parsed.diagnostics, []);
  assert.deepEqual(parsed.targets.map(t => t.targetLabel), ['current']);
  const pane = buildLeanPaneManifest({ files: [{ path: 'main.tex', content: source }] });
  assert.deepEqual(pane.items.map(i => i.label), ['current']);
});

test('commented structural and opaque openers cannot hide real targets', () => {
  const source = document(String.raw`% \begin{verbatim}
% \begin{claim}
\begin{claim}
\leatheorem{label=current}
Current statement.
% \end{claim}
\end{claim}
% \end{verbatim}`);
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.equal(result.targets[0].targetText, 'Current statement.\n% \\end{claim}');
  assert.equal(result.targets[0].to, source.indexOf('\n\\end{claim}') + '\n\\end{claim}'.length);
});

test('command definitions are inactive in fragments, including their bodies', () => {
  const declarations = [
    ...['New', 'Renew', 'Provide', 'Declare'].map(prefix => '\\' + prefix + 'DocumentCommand{\\wrapper}{m}{\\leatheorem{label=template}{#1}}'),
    ...['new', 'renew', 'provide'].map(prefix => '\\' + prefix + 'command*\\wrapper[1][{a]b}]{\\leatheorem{label=template}{#1}}'),
    String.raw`\NewDocumentCommand% comment between declaration arguments
{\lea}{m g}{\IfValueT{#2}{#2}}`,
    String.raw`\newenvironment{custom}[1]{\leatheorem{label=template}{#1}}{}`
  ];
  const source = declarations.join('\n') + '\n' + String.raw`\leatheorem{label=real}{Real fragment statement.}`;
  const result = parseTargetDocument(source, { sourcePath: 'chapters/results.tex' });
  assert.deepEqual(result.diagnostics, []);
  assert.deepEqual(result.targets.map(t => t.targetLabel), ['real']);
  assert.equal(buildLeanPaneManifest({ files: [{ path: 'chapters/results.tex', content: source }] }).items.length, 1);
});

test('the shipped style and copied preamble produce no phantom tags even without a source path', async () => {
  const sty = await fs.readFile(new URL('../extension/assets/lea-tags.sty', import.meta.url), 'utf8');
  const options = await fs.readFile(new URL('../extension/options.js', import.meta.url), 'utf8');
  const snippet = JSON.parse(options.match(/const LEA_TAGS_PREAMBLE_SNIPPET = (\[[\s\S]*?\])\.join/)[1]).join('\n');
  for (const source of [sty, snippet]) {
    assert.deepEqual(parseTargetDocument(source), { targets: [], diagnostics: [] });
    const result = parseTargetDocument(source + '\n' + String.raw`\leatheorem{}{A real statement.}`);
    assert.deepEqual(result.diagnostics, []);
    assert.equal(result.targets.length, 1);
  }
  for (const sourcePath of ['lea-tags.sty', 'custom.CLS']) {
    assert.deepEqual(parseTargetDocument(String.raw`\lea{broken`, { sourcePath }), { targets: [], diagnostics: [] });
  }
});

test('a commented document opener does not hide a real chapter-fragment tag', () => {
  const source = String.raw`\usepackage{lea-tags}
\leatheorem{label=real}{Fragment statement.}
% \begin{document}`;
  assert.deepEqual(parseTargets(source).map(t => t.targetLabel), ['real']);
});

test('commented package imports and declarations do not satisfy package detection', () => {
  for (const preamble of [
    String.raw`% \usepackage{lea-tags}`,
    String.raw`% \input{lea-tags}`,
    String.raw`% \NewDocumentCommand{\lea}{m}{}`
  ]) {
    const result = parseTargetDocument(preamble + '\n\\begin{document}\n\\leatheorem{}{Statement.}\n\\end{document}');
    assert.deepEqual(result.diagnostics.map(d => d.code), ['tag_package_not_loaded']);
    assert.equal(result.targets.length, 1);
  }
});

for (const newline of ['\n', '\r\n']) {
  test('leacode has multiline metadata and an untouched verbatim body with ' + JSON.stringify(newline), () => {
    const code = String.raw`theorem sample {n : Nat} : n % 2 = n % 2 := by rfl
-- literal \lea{label=not_a_tag} and \label{not_a_label}
% lea: formalize label=not_a_marker
-- literal { } \\`;
    const source = document(String.raw`\begin{leacode}% metadata follows
{
label=sample,
context={Use {nested} groups.}
}` + '\n' + code + '\n\\end{leacode}').replaceAll('\n', newline);
    const result = parseTargetDocument(source);
    assert.deepEqual(result.diagnostics, []);
    assert.equal(result.targets[0].targetText, code.replaceAll('\n', newline));
    assert.equal(result.targets[0].latexLabel, '');
    const pane = buildLeanPaneManifest({ files: [{ path: 'main.tex', content: source }] });
    assert.equal(pane.items[0].naturalLanguageLatex, result.targets[0].targetText);
    assert.equal(pane.items[0].sourceHash, result.targets[0].sourceHash);
  });
}

test('commented leacode blocks are inactive and code examples inside other verbatim blocks are ignored', () => {
  const source = document(String.raw`% \begin{leacode}{label=commented}
% \end{leacode}
\begin{verbatim}
\begin{leacode}{label=example}
% lea: formalize label=not_a_marker
\end{leacode}
\end{verbatim}
\leatheorem{label=real}{Real statement.}`);
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.deepEqual(result.targets.map(t => t.targetLabel), ['real']);
});

test('escaped command backslashes cannot become tags or structural openers', () => {
  const source = document(String.raw`\\leatheorem{label=not_a_tag}{Example text.}
\\begin{claim}
\leatheorem{label=real}{Real statement.}`);
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.deepEqual(result.targets.map(t => t.targetLabel), ['real']);
});

test('genuinely malformed arguments retain distinct messages and recover later valid tags', () => {
  const cases = [
    [String.raw`\leatheorem text`, /requires a metadata argument/],
    [String.raw`\leatheorem{label=bad`, /metadata argument is missing its closing brace/],
    [String.raw`\leatheorem{label=bad}{Unclosed statement`, /body argument is missing its closing brace/]
  ];
  for (const [bad, message] of cases) {
    const source = document(bad + '\n\\lealemma{label=good}{A valid statement.}');
    const result = parseTargetDocument(source);
    assert.deepEqual(result.targets.map(t => t.targetLabel), ['good']);
    assert.equal(result.diagnostics.length, 1);
    assert.equal(result.diagnostics[0].code, 'malformed_tag');
    assert.match(result.diagnostics[0].message, message);
  }
  for (const [body, message] of [
    [String.raw`\begin{leacode} text\end{leacode}`, /requires a metadata argument/],
    [String.raw`\begin{leacode}{label=bad\end{leacode}`, /metadata argument is missing its closing brace/],
    [String.raw`\begin{leacode}{} theorem sample : True := trivial`, /missing its \\end\{leacode\}/]
  ]) {
    const result = parseTargetDocument(document(body));
    assert.equal(result.targets.length, 0);
    assert.match(result.diagnostics[0].message, message);
  }
});

test('comment markers remain opt-in and compatible beside multiline tags', () => {
  const source = document(String.raw`\begin{theorem}
% lea: formalize label=commented uses={dependency} context={A hint.}
A comment-marked statement.
\end{theorem}
\leatheorem{
label=tagged
}{A tagged statement.}`);
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.deepEqual(result.targets.map(t => t.targetLabel), ['commented', 'tagged']);
  assert.deepEqual(result.targets[0].targetUses, ['dependency']);
  assert.equal(result.targets[0].targetText, 'A comment-marked statement.');
});

test('browser and pane extraction retain the same metadata, source locations and formalization identity', () => {
  const source = document(String.raw`\begin{claim}\label{clm:sample}
\leatheorem% metadata follows
{
label=sample, uses={dependency},
context={A hint.}
}
A statement.
\end{claim}
\begin{proof}
A source proof.
\end{proof}`);
  const files = [{ path: 'main.tex', content: source }];
  const [target] = parseTargets(source, { sourcePath: 'main.tex' });
  const browserTarget = {
    ...target, sourceFile: 'main.tex',
    sourceStartLine: source.slice(0, target.from).split('\n').length,
    sourceEndLine: source.slice(0, target.to).split('\n').length
  };
  const [associated] = associateProofSourcesCore({ targets: [browserTarget], files });
  // The browser core deliberately leaves hashing to its caller.
  associated.proofAssociation.proofHash = createHash('sha256')
    .update(associated.sourceProof.replace(/\r\n?/g, '\n')).digest('hex');
  const bundle = buildFormalizationSourceBundle(associated, { files, targetUses: target.targetUses, targetContext: target.targetContext });
  const [item] = buildLeanPaneManifest({ files }).items;
  assert.equal(item.naturalLanguageLatex, target.targetText);
  assert.equal(item.sourceHash, hashTargetText(target.targetText));
  assert.deepEqual(item.targetUses, target.targetUses);
  assert.equal(item.targetContext, target.targetContext);
  assert.equal(item.sourceStartOffset, target.from);
  assert.equal(item.sourceEndOffset, target.to);
  assert.equal(item.sourceStartLine, browserTarget.sourceStartLine);
  assert.equal(item.sourceEndLine, browserTarget.sourceEndLine);
  assert.equal(item.proofAssociation.status, 'associated');
  assert.equal(item.formalizationInputHash, bundle.sourceIdentityHash);
});

test('tag metadata is stripped while commented-out calls remain ordinary source text', () => {
  const body = String.raw`\leatheorem% metadata
{label=sample,
context={Hint with \} and \{.}}
Statement.
% \leatheorem{label=old}{Old statement.}`;
  assert.equal(stripLeaTargetText(body), String.raw`Statement.
% \leatheorem{label=old}{Old statement.}`);
});


test('a double backslash before a closing metadata brace does not swallow later fields', () => {
  const source = document(String.raw`\leatheorem{context={Hint ending in \\}, label=sample, uses={dependency}}{Statement.}`);
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.equal(result.targets[0].targetLabel, 'sample');
  assert.deepEqual(result.targets[0].targetUses, ['dependency']);
  assert.equal(result.targets[0].targetContext, String.raw`Hint ending in \\`);
});

test('statement extraction preserves inactive marker and label examples inside verbatim', () => {
  const source = document(String.raw`\begin{claim}
\leatheorem{label=sample}
Statement with a literal example:
\begin{verbatim}
% lea: formalize label=not_a_marker
\lea{label=not_a_tag}
\label{not_a_label}
\end{verbatim}
\end{claim}`);
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.equal(result.targets.length, 1);
  assert.match(result.targets[0].targetText, /% lea: formalize label=not_a_marker/);
  assert.match(result.targets[0].targetText, /\\lea\{label=not_a_tag\}/);
  assert.match(result.targets[0].targetText, /\\label\{not_a_label\}/);
});

test('the LaTeX compilation fixture detects exactly its eight intended targets', async () => {
  const source = await fs.readFile(new URL('./fixtures/lea-tags-syntax.tex', import.meta.url), 'utf8');
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.deepEqual(result.targets.map(t => t.targetLabel), [
    'syntax_generic', 'syntax_theorem', 'syntax_lemma', 'syntax_proposition',
    'syntax_corollary', 'syntax_definition', 'syntax_environment', 'syntax_code'
  ]);
  const pane = buildLeanPaneManifest({ files: [{ path: 'main.tex', content: source }] });
  assert.equal(pane.items.length, 8);
  assert.ok(pane.items.every(i => i.formalizable));
});

test('the editor bridge publishes multiline targets and clears stale diagnostics after edits', async () => {
  let sourcePath = 'main.tex';
  let source = document(String.raw`\leatheorem{label=sample`);
  const listeners = new Map();
  const messages = [];
  const extensions = [];
  globalThis.window = {
    _ide: {
      editorManager: { getCurrentDocId: () => 'doc' },
      fileTreeManager: { findEntityById: () => ({ path: sourcePath }), getEntityPath: entity => entity.path }
    },
    addEventListener: (type, listener) => listeners.set(type, listener),
    postMessage: message => messages.push(message),
    setInterval() {}
  };
  try {
    await import('../extension/pageBridge.js?leaTagsSyntax');
    listeners.get('UNSTABLE_editor:extensions')({ detail: {
      extensions,
      CodeMirror: {
        ViewPlugin: { fromClass: (PluginClass, spec) => ({ PluginClass, spec }) },
        Decoration: {
          mark: () => ({ range: (from, to) => ({ from, to }) }),
          set: ranges => ranges
        }
      }
    } });
    const view = {
      state: { doc: { toString: () => source, get length() { return source.length; } } },
      coordsAtPos: position => ({ left: position, right: position + 1, top: 10, bottom: 28 })
    };
    const plugin = new extensions[0].PluginClass(view);
    const latest = () => messages.filter(m => m.type === 'OL_LEAN_TARGETS_VISIBLE').at(-1);
    assert.equal(latest().diagnostics[0].code, 'malformed_tag');
    source = document(String.raw`% \leatheorem{label=old
\leatheorem% Metadata follows.
{label=sample,
context={A hint.}}
% The statement follows.
{A statement.}`);
    plugin.update({ view, docChanged: true });
    assert.deepEqual(latest().diagnostics, []);
    assert.equal(latest().targets.length, 1);
    assert.equal(latest().targets[0].targetText, 'A statement.');
    assert.equal(latest().targets[0].targetContext, 'A hint.');
    assert.equal(latest().targets[0].sourceFile, 'main.tex');
    sourcePath = 'lea-tags.sty';
    source = await fs.readFile(new URL('../extension/assets/lea-tags.sty', import.meta.url), 'utf8');
    plugin.update({ view, docChanged: true });
    assert.deepEqual(latest().targets, []);
    assert.deepEqual(latest().diagnostics, []);
  } finally {
    delete globalThis.window;
  }
});

test('package-load detection removes comments inside package names with TeX semantics', () => {
  const source = String.raw`\usepackage% Package follows.
{lea-% Ignore this line ending.
tags}
\begin{document}
\leatheorem{}{Statement.}
\end{document}`;
  const result = parseTargetDocument(source);
  assert.deepEqual(result.diagnostics, []);
  assert.equal(result.targets.length, 1);
});

test('an incomplete leacode block cannot hide a later valid tag', () => {
  const source = document(String.raw`\begin{leacode}{label=unfinished}
theorem unfinished : True := trivial
\leatheorem{label=real}{A real statement.}`);
  const result = parseTargetDocument(source);
  assert.deepEqual(result.targets.map(t => t.targetLabel), ['real']);
  assert.equal(result.diagnostics.length, 1);
  assert.match(result.diagnostics[0].message, /missing its \\end\{leacode\}/);
});
