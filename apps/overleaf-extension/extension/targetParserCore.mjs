const THEOREM_ENVIRONMENTS = new Set(["theorem", "lemma", "proposition", "corollary"]);
const DEFINITION_ENVIRONMENTS = new Set(["definition"]);
const SUPPORTED_ENVIRONMENTS = new Set([...THEOREM_ENVIRONMENTS, ...DEFINITION_ENVIRONMENTS]);

// Inline tag commands from lea-tags.sty (docs/FEATURE-overleaf-inline-lea-tags.md).
// Each maps to the targetKind it implies; `lea` is the generic form, whose kind
// comes from a kind=... argument instead (mirroring `% lea: formalize kind=...`).
const TAG_COMMAND_KINDS = {
  leatheorem: "theorem",
  lealemma: "theorem",
  leaproposition: "theorem",
  leacorollary: "theorem",
  leadefinition: "definition",
  lea: null
};

// Longest-name-first so the alternation's backtracking (forced by \b below) has
// the least work to do; correctness doesn't depend on this ordering.
const TAG_COMMAND_NAMES = Object.keys(TAG_COMMAND_KINDS).sort((a, b) => b.length - a.length);
const TAG_COMMAND_PATTERN_SOURCE = `\\\\(${TAG_COMMAND_NAMES.join("|")})\\b`;

// Environments where a Lea tag is almost certainly a mistake (a stray tag left
// in a figure/list/math environment rather than a theorem-like statement).
// Non-blocking: the target is still produced, just flagged. Tags are
// environment-name-agnostic by design, so this is the one safety net against
// the obvious accidental cases, not an exhaustive list.
const SUSPICIOUS_TAG_ENVIRONMENTS = new Set([
  "figure", "figure*", "table", "table*", "itemize", "enumerate",
  "equation", "equation*", "align", "align*", "tabular", "tabular*"
]);

export function parseTargetDocument(source, { sourcePath = "" } = {}) {
  if (/\.(?:sty|cls)$/i.test(sourcePath)) return { targets: [], diagnostics: [] };
  const text = String(source || "");
  const markerResult = parseMarkedTargets(text, sourcePath);
  return {
    targets: markerResult.targets.sort((a, b) => a.from - b.from),
    diagnostics: markerResult.diagnostics.sort((a, b) => a.from - b.from)
  };
}

export function parseTargets(source, options) {
  return parseTargetDocument(source, options).targets;
}

export function isValidLeanIdentifier(value) {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(value);
}

function generatedTargetLabel({ sourcePath, targetKind, latexLabel, ordinal }) {
  // The companion pins this candidate to a persistent identity. This pure,
  // browser-safe name also lets the editor draw a badge before that round trip.
  const seed = `${sourcePath}\0${targetKind}\0${latexLabel || `#${ordinal}`}`;
  let hash = 2166136261;
  for (let i = 0; i < seed.length; i += 1) {
    hash = Math.imul(hash ^ seed.charCodeAt(i), 16777619);
  }
  const hint = String(latexLabel || targetKind).replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "").slice(0, 32) || targetKind;
  return `lea_auto_${hint}_${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

function parseMarkedTargets(source, sourcePath) {
  const diagnostics = [];
  const scan = scanLatexSource(source);
  const commentEnvironments = findSupportedEnvironments(source, scan.activeSource);
  let genericEnvironments = null; // computed lazily; only needed if a tag is present
  const getGenericEnvironments = () => {
    if (!genericEnvironments) {
      genericEnvironments = findEnvironmentsInSource(source, null, scan.activeSource);
    }
    return genericEnvironments;
  };

  // Intentional "% lea:" markers have their own view. Ordinary comments,
  // declarations and verbatim contents are inactive for tag/structure scans.
  const commentGroups = findLeaCommentGroups(scan.markerSource)
    .filter((group) => group.hasTargetMarker)
    .map((group) => ({ ...group, syntax: "comment" }));
  const tagDetection = findLeaTagGroups(source, scan.activeSource);
  diagnostics.push(...tagDetection.diagnostics);
  // leacode bodies remain raw; the scanner records only executable openers.
  const codeDetection = findLeaCodeEnvironments(source, scan);
  diagnostics.push(...codeDetection.diagnostics);

  const packageDependentGroups = [...tagDetection.groups, ...codeDetection.groups]
    .sort((a, b) => a.from - b.from);
  if (packageDependentGroups.length > 0 && !hasLeaTagPackageLoaded(scan)) {
    const firstTag = packageDependentGroups[0];
    diagnostics.push(buildDiagnostic({
      code: "tag_package_not_loaded",
      message: "A Lea tag command (e.g. \\leatheorem{...}) is used, but nothing in the preamble defines it. "
        + "Add \\usepackage{lea-tags} (or the inline fallback definitions) before \\begin{document}, "
        + "or this document will fail to compile.",
      from: firstTag.from,
      to: firstTag.to
    }));
  }

  const groupsByEnvironment = new Map();
  for (const group of [...commentGroups, ...tagDetection.groups, ...codeDetection.groups]) {
    // A tag with a body argument (the standalone form) already carries its
    // own ready-made environment-shaped object and needs no lookup at all.
    let environment = group.standaloneEnvironment;
    if (!environment) {
      const lookupEnvironments = group.syntax === "tag" ? getGenericEnvironments() : commentEnvironments;
      environment = findSmallestEnclosingEnvironment(lookupEnvironments, group.from, group.to);
    }
    if (!environment) {
      diagnostics.push(buildDiagnostic({
        code: "missing_environment",
        message: group.syntax === "tag"
          ? "A Lea tag must either be inside a LaTeX environment (e.g. \\begin{theorem}...\\end{theorem}) "
            + "or supply the statement as a second argument, e.g. \\leatheorem{label=foo}{Statement text.}."
          : "Lea marker must be inside a supported theorem or definition environment.",
        from: group.from,
        to: group.to
      }));
      continue;
    }
    const key = environment.from;
    const existing = groupsByEnvironment.get(key) || { groups: [] };
    existing.groups.push({ ...group, environment });
    groupsByEnvironment.set(key, existing);
  }

  const targets = [];
  let ordinal = 0;
  for (const { groups: environmentGroups } of [...groupsByEnvironment.values()]
    .sort((a, b) => a.groups[0].environment.from - b.groups[0].environment.from)) {
    ordinal += 1;
    const environment = environmentGroups[0].environment;
    if (environmentGroups.length > 1) {
      diagnostics.push(buildDiagnostic({
        code: "duplicate_marker",
        message: `Only one Lea marker is supported inside this ${environment.name} environment.`,
        from: environmentGroups[1].from,
        to: environmentGroups[1].to
      }));
      continue;
    }

    const group = environmentGroups[0];
    const markerKind = inferMarkerTargetKind(group);
    if (!markerKind.ok) {
      diagnostics.push(buildDiagnostic({
        code: markerKind.code,
        message: markerKind.message,
        from: group.from,
        to: group.to
      }));
      continue;
    }
    // The environment-name/kind allowlist check only applies to comment
    // markers, which depend on the environment name to infer their kind. A
    // tag states its own kind explicitly, so a custom (non-allowlisted)
    // environment is exactly the case tags are meant to support.
    if (group.syntax === "comment" && markerKind.targetKind !== environment.targetKind) {
      diagnostics.push(buildDiagnostic({
        code: "environment_mismatch",
        message: mismatchMessage(markerKind.targetKind, environment),
        from: group.from,
        to: group.to
      }));
      continue;
    }

    const metadata = group.metadata;
    if (metadata.label && !isValidLeanIdentifier(metadata.label)) {
      diagnostics.push(buildDiagnostic({
        code: "invalid_label",
        message: "Lea marker label must be a valid Lean identifier.",
        from: group.from,
        to: group.to
      }));
      continue;
    }
    const invalidUse = metadata.uses.find((value) =>
      !isValidLeanIdentifier(value) && !/^[A-Za-z][A-Za-z0-9_.:-]*$/.test(value));
    if (invalidUse) {
      diagnostics.push(buildDiagnostic({
        code: "invalid_uses",
        message: `Lea dependency reference is invalid: ${invalidUse}.`,
        from: group.from,
        to: group.to
      }));
      continue;
    }

    const { targetText, latexLabel } = extractEnvironmentText(source, environment);
    if (!targetText.trim()) {
      diagnostics.push(buildDiagnostic({
        code: "missing_target_text",
        message: `Marked ${markerKind.targetKind} block has no text after Lea comments are removed.`,
        from: group.from,
        to: group.to
      }));
      continue;
    }

    if (group.syntax === "tag" && SUSPICIOUS_TAG_ENVIRONMENTS.has(environment.name)) {
      diagnostics.push(buildDiagnostic({
        code: "suspicious_environment",
        message: `Lea tag is inside a ${environment.name} environment, which is unusual for a theorem or `
          + "definition statement. Double-check this is the intended block.",
        from: group.from,
        to: group.to
      }));
    }

    const labelSource = metadata.label ? "explicit" : "generated";
    const targetLabel = metadata.label || generatedTargetLabel({
      sourcePath, targetKind: markerKind.targetKind, latexLabel, ordinal
    });
    targets.push({
      targetKind: markerKind.targetKind,
      targetLabel,
      labelSource,
      ...(labelSource === "generated" ? {
        displayTitle: latexLabel || targetText.replace(/\s+/g, " ").slice(0, 80)
      } : {}),
      targetText,
      targetUses: metadata.uses,
      targetContext: metadata.context,
      latexEnvironment: environment.name,
      latexLabel,
      sourceHash: "",
      syntax: group.syntax,
      from: environment.from,
      to: environment.to,
      badgeFrom: environment.badgeFrom,
      bodyFrom: environment.bodyFrom,
      bodyTo: environment.bodyTo
    });
  }

  return { targets, diagnostics };
}

// A chapter fragment needs no document wrapper. When there is a wrapper,
// only its executable opener (not a commented example) ends the preamble.
function documentBodyOffset(source) {
  const match = /\\begin\s*\{\s*document\s*\}/.exec(source);
  return match ? match.index + match[0].length : 0;
}

function findLeaTagGroups(source, activeSource) {
  const groups = [];
  const diagnostics = [];
  const offset = documentBodyOffset(activeSource);
  const body = source.slice(offset);
  const activeBody = activeSource.slice(offset);
  const pattern = new RegExp(TAG_COMMAND_PATTERN_SOURCE, "g");
  let match;

  while ((match = pattern.exec(activeBody))) {
    const commandStart = match.index;
    const commandName = match[1];
    const afterCommand = pattern.lastIndex;
    const argumentStart = skipLatexTrivia(body, afterCommand);
    const argument = readLatexGroup(body, argumentStart);
    if (!argument.ok) {
      diagnostics.push(buildDiagnostic({
        code: "malformed_tag",
        message: argument.reason === "unterminated"
          ? `\\${commandName}{...} metadata argument is missing its closing brace.`
          : `\\${commandName} requires a metadata argument in braces; use {} when no metadata is needed.`,
        from: offset + commandStart,
        to: offset + afterCommand
      }));
      continue;
    }

    const argumentInner = body.slice(argumentStart + 1, argument.end - 1);
    const metadata = parseMetadata(stripLatexComments(argumentInner));
    const impliedKind = TAG_COMMAND_KINDS[commandName];
    if (impliedKind) {
      // The command name is authoritative over any kind=... the argument
      // also happens to contain: \leatheorem{...} is always a theorem.
      metadata.kind = impliedKind;
    }

    // xparse's optional `g` argument skips whitespace and TeX comments.
    // An adjacent unrelated brace group is therefore still absorbed.
    const bodyArgumentStart = skipLatexTrivia(body, argument.end);
    let bodyArgument = null;
    if (body[bodyArgumentStart] === "{") {
      const parsedBody = readLatexGroup(body, bodyArgumentStart);
      if (!parsedBody.ok) {
        diagnostics.push(buildDiagnostic({
          code: "malformed_tag",
          message: `\\${commandName}{...}{...} body argument is missing its closing brace.`,
          from: offset + commandStart,
          to: offset + bodyArgumentStart
        }));
        continue;
      }
      bodyArgument = parsedBody;
    }

    const group = {
      from: offset + commandStart,
      to: offset + (bodyArgument ? bodyArgument.end : argument.end),
      syntax: "tag",
      command: commandName,
      hasFormalize: impliedKind !== "definition",
      hasDefine: impliedKind === "definition",
      hasTargetMarker: true,
      metadata
    };

    if (bodyArgument) {
      // A ready-to-use environment-shaped object: the rest of the pipeline
      // (extractEnvironmentText, badge placement, the Lean pane) only ever
      // reads name/from/to/badgeFrom/bodyFrom/bodyTo off an "environment",
      // and doesn't care whether those came from \begin{X}...\end{X} or a
      // tag's own body argument.
      group.standaloneEnvironment = {
        name: commandName,
        from: offset + commandStart,
        to: offset + bodyArgument.end,
        badgeFrom: offset + commandStart,
        bodyFrom: offset + bodyArgumentStart + 1,
        bodyTo: offset + bodyArgument.end - 1
      };
    }

    groups.push(group);
    pattern.lastIndex = bodyArgument ? bodyArgument.end : argument.end;
  }

  return { groups, diagnostics };
}

// The lexical scan records real leacode openers without interpreting their
// verbatim bodies. Metadata uses the same argument reader as inline tags.
function findLeaCodeEnvironments(source, scan) {
  const groups = [];
  const diagnostics = [];
  const offset = documentBodyOffset(scan.activeSource);
  for (const environment of scan.codeEnvironments) {
    const { from, openerEnd, metaStart, metadataArgument, bodyFrom, bodyTo, to } = environment;
    if (from < offset) continue;
    if (!metadataArgument.ok || bodyTo === null) {
      const message = !metadataArgument.ok
        ? metadataArgument.reason === "unterminated"
          ? "\\begin{leacode}{...} metadata argument is missing its closing brace."
          : "\\begin{leacode} requires a metadata argument; use {} when no metadata is needed."
        : "\\begin{leacode} is missing its \\end{leacode}.";
      diagnostics.push(buildDiagnostic({ code: "malformed_tag", message, from, to: openerEnd }));
      continue;
    }
    groups.push({
      from, to,
      syntax: "leacode",
      command: LEA_CODE_ENVIRONMENT,
      hasFormalize: true,
      hasDefine: false,
      hasTargetMarker: true,
      metadata: parseMetadata(stripLatexComments(source.slice(metaStart + 1, metadataArgument.end - 1))),
      standaloneEnvironment: {
        name: LEA_CODE_ENVIRONMENT, from, to, badgeFrom: from, bodyFrom, bodyTo
      }
    });
  }
  return { groups, diagnostics };
}

// Read a TeX control word or control symbol as one token. In particular, \\ is
// one token: a following brace or percent sign is NOT escaped by that token.
function readLatexCommand(source, cursor) {
  let end = cursor + 1;
  if (/[A-Za-z@]/.test(source[end] || "")) {
    while (/[A-Za-z@]/.test(source[end] || "")) end += 1;
  } else if (end < source.length) {
    end += 1;
  }
  return { name: source.slice(cursor + 1, end), end };
}

function commentEnd(source, cursor, includeNewline = false) {
  while (cursor < source.length && !isLineBreak(source[cursor])) cursor += 1;
  if (includeNewline) {
    if (source[cursor] === "\r") cursor += 1;
    if (source[cursor] === "\n") cursor += 1;
  }
  return cursor;
}

function skipLatexTrivia(source, cursor) {
  while (cursor < source.length) {
    if (/\s/.test(source[cursor])) cursor += 1;
    else if (source[cursor] === "%") cursor = commentEnd(source, cursor, true);
    else break;
  }
  return cursor;
}

// Lea arguments may span lines. Comments cannot close a group, and control
// symbols such as \{ and \} do not affect its depth. The exported same-line
// parseBalancedSuffix remains unchanged for environment titles/labels.
function readLatexGroup(source, cursor, opener = "{") {
  if (source[cursor] !== opener) return { ok: false, reason: "missing" };
  const closer = opener === "{" ? "}" : "]";
  let depth = 1;
  let braceDepth = 0;
  for (let index = cursor + 1; index < source.length;) {
    const char = source[index];
    if (char === "\\") {
      index = readLatexCommand(source, index).end;
      continue;
    }
    if (char === "%") {
      index = commentEnd(source, index, true);
      continue;
    }
    if (opener === "[" && char === "{") braceDepth += 1;
    else if (opener === "[" && char === "}") braceDepth = Math.max(0, braceDepth - 1);
    else if (braceDepth === 0 && char === opener) depth += 1;
    else if (braceDepth === 0 && char === closer) {
      depth -= 1;
      if (depth === 0) return { ok: true, end: index + 1 };
    }
    index += 1;
  }
  return { ok: false, reason: "unterminated" };
}

// TeX removes both a comment and its line ending, rather than inserting a
// space. This preserves e.g. label=ex% comment\nample as label=example.
function stripLatexComments(source) {
  const parts = [];
  let from = 0;
  for (let cursor = 0; cursor < source.length;) {
    if (source[cursor] === "\\") {
      cursor = readLatexCommand(source, cursor).end;
    } else if (source[cursor] === "%") {
      parts.push(source.slice(from, cursor));
      cursor = commentEnd(source, cursor, true);
      from = cursor;
    } else cursor += 1;
  }
  parts.push(source.slice(from));
  return parts.join("");
}

function hasLeaTagPackageLoaded(scan) {
  const bodyOffset = documentBodyOffset(scan.activeSource);
  const preambleEnd = bodyOffset || scan.activeSource.length;
  const preamble = stripLatexComments(scan.markerSource.slice(0, preambleEnd));
  return (
    /\\usepackage\s*(?:\[[^\]]*\]\s*)?\{[^}]*\blea-tags\b[^}]*\}/.test(preamble)
    || /\\input\s*\{\s*lea-tags\s*\}/.test(preamble)
    || scan.definitions.some(({ from, name, kind }) => from < preambleEnd && (
      kind === "command" ? Object.hasOwn(TAG_COMMAND_KINDS, name) : name === LEA_CODE_ENVIRONMENT
    ))
  );
}

function inferMarkerTargetKind(group) {
  if (group.hasDefine && group.hasFormalize) {
    return {
      ok: false,
      code: "mixed_marker",
      message: "Use either lea: define or lea: formalize for one target, not both."
    };
  }
  if (group.hasDefine) {
    return { ok: true, targetKind: "definition" };
  }
  if (!group.hasFormalize) {
    return {
      ok: false,
      code: "missing_marker",
      message: "Lea metadata comments must start with define or formalize."
    };
  }

  const kind = String(group.metadata.kind || "").trim();
  if (!kind) {
    return { ok: true, targetKind: "theorem" };
  }
  if (kind === "definition" || kind === "theorem") {
    return { ok: true, targetKind: kind };
  }
  return {
    ok: false,
    code: "unsupported_kind",
    message: `Unsupported Lea target kind: ${kind}.`
  };
}

function mismatchMessage(markerKind, environment) {
  if (markerKind === "definition" && environment.targetKind === "theorem") {
    return "lea: define markers must be inside a definition environment.";
  }
  if (markerKind === "theorem" && environment.targetKind === "definition") {
    return "Definition environments must use lea: define or lea: formalize kind=definition.";
  }
  return `Lea ${markerKind} marker does not match the ${environment.name} environment.`;
}

function findSupportedEnvironments(source, activeSource) {
  return findEnvironmentsInSource(source, SUPPORTED_ENVIRONMENTS, activeSource).map((environment) => ({
    ...environment,
    targetKind: DEFINITION_ENVIRONMENTS.has(environment.name) ? "definition" : "theorem"
  }));
}

// Environments whose *contents* LaTeX itself never interprets the way this
// parser otherwise would. Inside \begin{verbatim}/\begin{lstlisting}/
// \begin{minted}, LaTeX's tokenizer is disabled entirely -- a "%" is literal
// displayed text, not a comment character, so a "% lea: ..." line in there
// is not a marker under any real compilation of the document, and a
// \leatheorem{...} tag would print literally rather than execute. `comment`
// (the comment package) goes further and strips its body from the document
// outright. None of these nest in real LaTeX (the first matching \end{name}
// always closes them), so unlike theorem-like environments they need no
// stack -- just "find the next \end{name} after each \begin{name}".
//
// This matters at two levels, both handled below:
//   1. The environment's own \begin{X}...\end{X} pair should never itself
//      become a target -- that's NON_TARGET_ENVIRONMENTS, same mechanism as
//      excluding \begin{document}.
//   2. Text *inside* the pair must be masked out of all environment/marker
//      matching, not just excluded from becoming its own target. Found via a
//      real false positive: a documentation file showing "% lea: ..." syntax
//      examples inside a \begin{verbatim} block, itself containing an
//      illustrative "\begin{theorem}...\end{theorem}\n% lea: ..." snippet.
//      Excluding only the outer verbatim pair (level 1 alone) still let that
//      *inner*, purely-illustrative "\begin{theorem}" text be matched as its
//      own real environment, colliding on label with the actual target the
//      example was describing. Masking the interior is what stops that.
const OPAQUE_ENVIRONMENT_NAMES = [
  "verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "comment"
];

// The `leacode` environment (lea-tags.sty) is a `listings`-backed code block:
// LaTeX reads its interior verbatim (like the names above), so for every
// LaTeX-structure scanner it is just another opaque span and its interior must
// be masked out. It differs in exactly one way -- unlike the others, its
// interior IS a Lea target (the code the author wrote, sent to Lea verbatim as
// the statement). That single exception is handled by findLeaCodeEnvironments,
// which reads the RAW (pre-mask) source; every other scanner sees it masked.
export const LEA_CODE_ENVIRONMENT = "leacode";

const VERBATIM_LIKE_ENVIRONMENTS = new Set([...OPAQUE_ENVIRONMENT_NAMES, LEA_CODE_ENVIRONMENT]);

// Structural environments that are never themselves a theorem/definition
// target, even though they're syntactically just another \begin{X}...\end{X}
// pair. Excluded only from *generic* matching (names: null) -- the
// allowlisted comment-marker path never matches these anyway. Without this,
// a tag with no real enclosing statement environment would resolve to
// \begin{document}...\end{document} itself (present in essentially every
// real Overleaf project) instead of producing a missing_environment
// diagnostic, silently treating the entire paper as the target's body.
// verbatim-like names are included too (level 1 above).
const NON_TARGET_ENVIRONMENTS = new Set(["document", ...VERBATIM_LIKE_ENVIRONMENTS]);

const DOCUMENT_COMMAND_DEFINITIONS = /^(?:New|Renew|Provide|Declare)DocumentCommand$/;
const DOCUMENT_ENVIRONMENT_DEFINITIONS = /^(?:New|Renew|Provide|Declare)DocumentEnvironment$/;
const COMMAND_DEFINITIONS = /^(?:new|renew|provide)command$/;
const ENVIRONMENT_DEFINITIONS = /^(?:(?:new|renew|provide)environment|lstnewenvironment)$/;

function readDefinition(source, command) {
  const documentCommand = DOCUMENT_COMMAND_DEFINITIONS.test(command.name);
  const documentEnvironment = DOCUMENT_ENVIRONMENT_DEFINITIONS.test(command.name);
  const classicCommand = COMMAND_DEFINITIONS.test(command.name);
  const classicEnvironment = ENVIRONMENT_DEFINITIONS.test(command.name);
  if (!documentCommand && !documentEnvironment && !classicCommand && !classicEnvironment) return null;
  const kind = documentCommand || classicCommand ? "command" : "environment";
  let cursor = skipLatexTrivia(source, command.end);
  if (source[cursor] === "*") cursor = skipLatexTrivia(source, cursor + 1);
  const nameGroup = readLatexGroup(source, cursor);
  let name;
  if (nameGroup.ok) {
    name = stripLatexComments(source.slice(cursor + 1, nameGroup.end - 1)).trim();
    cursor = nameGroup.end;
  } else if (kind === "command" && source[cursor] === "\\") {
    const token = readLatexCommand(source, cursor);
    name = source.slice(cursor, token.end);
    cursor = token.end;
  } else return null;
  if (kind === "command") {
    if (!/^\\[A-Za-z@]+$/.test(name)) return null;
    name = name.slice(1);
  }
  // Classic declarations allow an argument count and an optional default.
  // xparse declarations have a required argument specification instead.
  if (classicCommand || classicEnvironment) {
    for (let index = 0; index < 2; index += 1) {
      cursor = skipLatexTrivia(source, cursor);
      if (source[cursor] !== "[") break;
      const option = readLatexGroup(source, cursor, "[");
      if (!option.ok) return { name, kind, to: cursor };
      cursor = option.end;
    }
  }
  const requiredGroups = (documentCommand || documentEnvironment ? 1 : 0) + (kind === "environment" ? 2 : 1);
  for (let index = 0; index < requiredGroups; index += 1) {
    cursor = skipLatexTrivia(source, cursor);
    const group = readLatexGroup(source, cursor);
    // Hide the declaration name even while an author is editing its body,
    // without consuming the next real tag after an incomplete declaration.
    if (!group.ok) return { name, kind, to: cursor };
    cursor = group.end;
  }
  return { name, kind, to: cursor };
}

function maskRange(chars, from, to) {
  for (let index = from; index < to; index += 1) {
    if (!isLineBreak(chars[index])) chars[index] = " ";
  }
}

// Both views retain every original offset and line ending. markerSource keeps
// ordinary comments for the opt-in "% lea:" grammar; activeSource hides them
// for executable tags, environment matching and the pane's loose fallback.
// No view is used as the stored/displayed statement or verbatim code body.
export function scanLatexSource(source) {
  const text = String(source || "");
  const active = text.split("");
  const markers = text.split("");
  const codeEnvironments = [];
  const definitions = [];
  for (let cursor = 0; cursor < text.length;) {
    if (text[cursor] === "%") {
      const end = commentEnd(text, cursor);
      maskRange(active, cursor, end);
      cursor = end;
      continue;
    }
    if (text[cursor] !== "\\") {
      cursor += 1;
      continue;
    }
    const command = readLatexCommand(text, cursor);
    const definition = readDefinition(text, command);
    if (definition) {
      definitions.push({ from: cursor, ...definition });
      maskRange(active, cursor, definition.to);
      maskRange(markers, cursor, definition.to);
      cursor = definition.to;
      continue;
    }
    if (command.name === "\\") {
      // Otherwise a regex could rediscover the second slash of \\lea.
      maskRange(active, cursor, command.end);
      maskRange(markers, cursor, command.end);
    }
    if (Object.hasOwn(TAG_COMMAND_KINDS, command.name)) {
      const metaStart = skipLatexTrivia(text, command.end);
      const metadataArgument = readLatexGroup(text, metaStart);
      if (metadataArgument.ok) {
        maskRange(markers, metaStart, metadataArgument.end);
        // Metadata is not executed, but retain its plain label text for the
        // pane's fallback inventory of invalid (e.g. unsupported-kind) tags.
        for (let index = command.end; index < metadataArgument.end;) {
          if (text[index] === "%") {
            const end = commentEnd(text, index);
            maskRange(active, index, end);
            index = end;
          } else if (text[index] === "\\") {
            const end = readLatexCommand(text, index).end;
            maskRange(active, index, end);
            index = end;
          } else index += 1;
        }
        cursor = metadataArgument.end;
        continue;
      }
    }
    if (command.name === "begin") {
      const nameStart = skipLatexTrivia(text, command.end);
      const nameArgument = readLatexGroup(text, nameStart);
      const name = nameArgument.ok ? stripLatexComments(text.slice(nameStart + 1, nameArgument.end - 1)).trim() : "";
      if (VERBATIM_LIKE_ENVIRONMENTS.has(name)) {
        const openerEnd = nameArgument.end;
        const metaStart = skipLatexTrivia(text, openerEnd);
        const metadataArgument = name === LEA_CODE_ENVIRONMENT ? readLatexGroup(text, metaStart) : null;
        const bodyFrom = metadataArgument?.ok ? metadataArgument.end : openerEnd;
        const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const endPattern = new RegExp(`\\\\end\\s*\\{\\s*${escapedName}\\s*\\}`, "g");
        endPattern.lastIndex = bodyFrom;
        const endMatch = endPattern.exec(text);
        const bodyTo = endMatch ? endMatch.index : null;
        const to = endMatch ? endPattern.lastIndex : text.length;
        if (name === LEA_CODE_ENVIRONMENT) {
          codeEnvironments.push({ from: cursor, openerEnd, metaStart, metadataArgument, bodyFrom, bodyTo, to });
        }
        if (!endMatch) {
          // An incomplete block is not runnable. Continue discovery after its
          // opener so it cannot hide a later valid tag while the source is edited.
          cursor = bodyFrom;
          continue;
        }
        maskRange(active, openerEnd, bodyTo);
        maskRange(markers, openerEnd, bodyTo);
        cursor = to;
        continue;
      }
    }
    cursor = command.end;
  }
  return { activeSource: active.join(""), markerSource: markers.join(""), codeEnvironments, definitions };
}

// Compatibility export for callers that need the comment-marker view.
export function maskOpaqueSpans(source) {
  return scanLatexSource(source).markerSource;
}

// Generic environment finder, shared by the comment-marker path above (via
// findSupportedEnvironments, restricted to the theorem/definition allowlist)
// and by tag-based detection and the Lean pane (shared/leanPaneManifest.mjs),
// which both need to locate *any* \begin{X}...\end{X} pair, not just an
// allowlisted name. Returns environment objects with no targetKind — callers
// that need a theorem/definition classification derive it themselves (from
// the environment name, for the allowlisted case, or from marker metadata,
// for the tag/generic case).
export function findEnvironments(source, { names } = {}) {
  return findEnvironmentsInSource(source, names, scanLatexSource(source).activeSource);
}

function findEnvironmentsInSource(source, names, activeSource) {
  const environments = [];
  const stack = [];
  const pattern = buildEnvironmentPattern(names);
  // Match executable structure while retaining offsets into the raw source.
  let match;

  while ((match = pattern.exec(activeSource))) {
    const [, command, name] = match;
    if (command === "begin") {
      const openerEnd = pattern.lastIndex;
      const bodyFrom = findEnvironmentBadgeAnchor(source, openerEnd);
      stack.push({
        name,
        from: match.index,
        openerEnd,
        badgeFrom: bodyFrom,
        bodyFrom
      });
      continue;
    }

    for (let index = stack.length - 1; index >= 0; index -= 1) {
      if (stack[index].name !== name) continue;
      const [open] = stack.splice(index, 1);
      environments.push({
        ...open,
        bodyTo: match.index,
        to: pattern.lastIndex
      });
      break;
    }
  }

  return names ? environments : environments.filter((environment) => !NON_TARGET_ENVIRONMENTS.has(environment.name));
}

function buildEnvironmentPattern(names) {
  const namePattern = names ? [...names].join("|") : "[A-Za-z]+\\*?";
  return new RegExp(`\\\\(begin|end)\\s*\\{\\s*(${namePattern})\\s*\\}`, "g");
}

function findSmallestEnclosingEnvironment(environments, from, to) {
  return environments
    .filter((environment) => environment.from <= from && to <= environment.to)
    .sort((a, b) => (a.to - a.from) - (b.to - b.from))[0] || null;
}

function findLeaCommentGroups(source) {
  const lines = splitLinesWithRanges(source);
  const groups = [];
  let index = 0;

  while (index < lines.length) {
    const first = parseLeaCommentLine(lines[index]);
    if (!first) {
      index += 1;
      continue;
    }

    const groupLines = [{ ...lines[index], content: first.content }];
    index += 1;
    while (index < lines.length) {
      const parsed = parseLeaCommentLine(lines[index]);
      if (!parsed) break;
      groupLines.push({ ...lines[index], content: parsed.content });
      index += 1;
    }

    const metadataSource = groupLines
      .map((line) => line.content.replace(/^\s*(?:formalize|define)\b\s*/i, ""))
      .join("\n");
    groups.push({
      from: groupLines[0].from,
      to: groupLines[groupLines.length - 1].to,
      hasFormalize: groupLines.some((line) => /^\s*formalize\b/i.test(line.content)),
      hasDefine: groupLines.some((line) => /^\s*define\b/i.test(line.content)),
      hasTargetMarker: groupLines.some((line) => /^\s*(?:formalize|define)\b/i.test(line.content)),
      metadata: parseMetadata(metadataSource)
    });
  }

  return groups;
}

function splitLinesWithRanges(source) {
  const lines = [];
  const pattern = /.*(?:\r?\n|$)/g;
  let match;
  while ((match = pattern.exec(source))) {
    if (match[0] === "" && match.index === source.length) break;
    lines.push({
      text: match[0],
      from: match.index,
      to: match.index + match[0].length
    });
  }
  return lines;
}

function parseLeaCommentLine(line) {
  const match = line.text.match(/^[ \t]*%\s*lea:\s*(.*?)(?:\r?\n)?$/i);
  return match ? { content: match[1] || "" } : null;
}

function extractEnvironmentText(source, environment) {
  const body = source.slice(environment.bodyFrom, environment.bodyTo);
  const labelMatch = source.slice(environment.from, environment.bodyTo).match(/\\label\s*\{([^}]*)\}/);
  return {
    targetText: environment.name === LEA_CODE_ENVIRONMENT ? body.trim() : stripLeaTargetText(body),
    latexLabel: environment.name === LEA_CODE_ENVIRONMENT ? "" : labelMatch ? labelMatch[1].trim() : ""
  };
}

// Canonical natural-language text of a marked environment body: the same text the
// staleness hash is computed over. Shared with the Lean-pane manifest so both
// surfaces hash byte-identical input and can never drift (see
// shared/leanPaneManifest.mjs and PLAN-overleaf-lean-pane-improvements.md item 1).
export function stripLeaTargetText(body) {
  const text = stripLeaTagCalls(String(body || ""));
  const scan = scanLatexSource(text);
  const spans = [
    ...scan.markerSource.matchAll(/^[ \t]*%\s*lea:.*(?:\r?\n|$)/gmi),
    ...scan.activeSource.matchAll(/\\label\s*\{[^}]*\}/g)
  ].map(match => ({ from: match.index, to: match.index + match[0].length }))
    .sort((a, b) => a.from - b.from);
  let result = "";
  let cursor = 0;
  for (const { from, to } of spans) {
    if (from < cursor) continue;
    result += text.slice(cursor, from);
    cursor = to;
  }
  return (result + text.slice(cursor)).trim();
}

// Removes \lea{...}/\leatheorem{...}/etc calls from a body of text. Brace-aware
// (not a fixed-width regex) because context={...} can itself contain braces.
function stripLeaTagCalls(body) {
  const activeSource = scanLatexSource(body).activeSource;
  const pattern = new RegExp(TAG_COMMAND_PATTERN_SOURCE, "g");
  let result = "";
  let cursor = 0;
  let match;

  while ((match = pattern.exec(activeSource))) {
    if (match.index < cursor) continue;
    const afterCommand = pattern.lastIndex;
    const argumentStart = skipLatexTrivia(body, afterCommand);
    const argument = readLatexGroup(body, argumentStart);
    const spanEnd = argument.ok ? argument.end : afterCommand;
    result += body.slice(cursor, match.index);
    cursor = spanEnd;
    pattern.lastIndex = spanEnd;
  }
  result += body.slice(cursor);
  return result;
}

function buildDiagnostic({ code, message, from, to }) {
  return {
    code,
    message,
    targetKind: "diagnostic",
    targetLabel: code,
    targetText: "",
    targetUses: [],
    targetContext: "",
    latexEnvironment: "",
    latexLabel: "",
    sourceHash: "",
    syntax: "diagnostic",
    from,
    to,
    badgeFrom: from,
    bodyFrom: from,
    bodyTo: to
  };
}

function findEnvironmentBadgeAnchor(source, bodyFrom) {
  let cursor = bodyFrom;
  let anchor = bodyFrom;

  while (cursor < source.length) {
    cursor = skipInlineWhitespace(source, cursor);
    if (isLineBreak(source[cursor])) break;

    const group = parseBalancedSuffix(source, cursor);
    if (group.ok) {
      cursor = group.end;
      anchor = cursor;
      continue;
    }

    const label = parseSameLineLabel(source, cursor);
    if (label.ok) {
      cursor = label.end;
      anchor = cursor;
      continue;
    }

    break;
  }

  return anchor;
}

function parseSameLineLabel(source, cursor) {
  if (!source.startsWith("\\label", cursor)) {
    return { ok: false };
  }
  const afterCommand = cursor + "\\label".length;
  if (/[A-Za-z]/.test(source[afterCommand] || "")) {
    return { ok: false };
  }
  const argumentStart = skipInlineWhitespace(source, afterCommand);
  const argument = parseBalancedSuffix(source, argumentStart);
  return argument.ok && source[argumentStart] === "{" ? argument : { ok: false };
}

export function parseBalancedSuffix(source, cursor) {
  const opener = source[cursor];
  const closer = opener === "{" ? "}" : opener === "[" ? "]" : "";
  if (!closer) {
    return { ok: false };
  }

  let depth = 1;
  for (let index = cursor + 1; index < source.length; index += 1) {
    const char = source[index];
    const previous = source[index - 1];
    if (isLineBreak(char)) {
      return { ok: false };
    }
    if (char === opener && previous !== "\\") {
      depth += 1;
    } else if (char === closer && previous !== "\\") {
      depth -= 1;
      if (depth === 0) {
        return { ok: true, end: index + 1 };
      }
    }
  }

  return { ok: false };
}

export function skipInlineWhitespace(source, cursor) {
  while (cursor < source.length && /[ \t]/.test(source[cursor])) {
    cursor += 1;
  }
  return cursor;
}

export function isLineBreak(char) {
  return char === "\n" || char === "\r";
}

function parseMetadata(source) {
  const metadata = { label: "", uses: [], context: "", kind: "" };
  for (const entry of splitMetadataEntries(source)) {
    const separator = entry.indexOf("=");
    if (separator === -1) {
      continue;
    }
    const key = entry.slice(0, separator).trim();
    const value = unbrace(entry.slice(separator + 1).trim());
    if (key === "label") {
      metadata.label = value.trim();
    } else if (key === "uses") {
      metadata.uses = splitTopLevel(value, ",")
        .map((item) => unbrace(item.trim()).trim())
        .filter(Boolean);
    } else if (key === "context") {
      metadata.context = value.trim();
    } else if (key === "kind") {
      metadata.kind = value.trim();
    }
  }
  return metadata;
}

function splitMetadataEntries(source) {
  const parts = [];
  let depth = 0;
  let bracketDepth = 0;
  let partStart = 0;

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === "\\") {
      index = readLatexCommand(source, index).end - 1;
      continue;
    }
    if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth = Math.max(0, depth - 1);
    } else if (char === "[" && depth === 0) {
      bracketDepth += 1;
    } else if (char === "]" && depth === 0 && bracketDepth > 0) {
      bracketDepth -= 1;
    } else if (isMetadataSeparator(source, index, depth, bracketDepth)) {
      parts.push(source.slice(partStart, index));
      partStart = index + 1;
    }
  }

  parts.push(source.slice(partStart));
  return parts;
}

function isMetadataSeparator(source, index, depth, bracketDepth) {
  if (depth !== 0 || bracketDepth !== 0) {
    return false;
  }
  if (source[index] === ",") {
    return true;
  }
  if (/\s/.test(source[index]) && /^(?:\s+)(?:label|uses|context|kind)\s*=/.test(source.slice(index))) {
    return true;
  }
  if (source[index] !== "\n") {
    return false;
  }
  return /^(?:\s*)(?:label|uses|context|kind)\s*=/.test(source.slice(index + 1));
}

function splitTopLevel(source, separator) {
  const parts = [];
  let depth = 0;
  let bracketDepth = 0;
  let partStart = 0;

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === "\\") {
      index = readLatexCommand(source, index).end - 1;
      continue;
    }
    if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth = Math.max(0, depth - 1);
    } else if (char === "[" && depth === 0) {
      bracketDepth += 1;
    } else if (char === "]" && depth === 0 && bracketDepth > 0) {
      bracketDepth -= 1;
    } else if (char === separator && depth === 0 && bracketDepth === 0) {
      parts.push(source.slice(partStart, index));
      partStart = index + 1;
    }
  }

  parts.push(source.slice(partStart));
  return parts;
}

function unbrace(value) {
  if (value.startsWith("{") && value.endsWith("}")) {
    return value.slice(1, -1);
  }
  return value;
}
