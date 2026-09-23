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
  const commentEnvironments = findSupportedEnvironments(source);
  let genericEnvironments = null; // computed lazily; only needed if a tag is present
  const getGenericEnvironments = () => {
    if (!genericEnvironments) {
      genericEnvironments = findEnvironments(source, { names: null });
    }
    return genericEnvironments;
  };

  // A "% lea: ..." line (or \leatheorem{...} tag) inside a verbatim-like
  // environment is never a real marker -- LaTeX itself never treats "%" as a
  // comment character there, and a tag command would print literally rather
  // than execute. Both scanners below run against the masked source (see
  // maskOpaqueSpans), so such a line simply can't match at all -- no
  // diagnostic, no target, exactly as if it weren't there. For any *real*
  // marker (outside every opaque span), masked text is byte-identical to the
  // original, so this changes nothing about genuine detection.
  const maskedSource = maskOpaqueSpans(source);
  const commentGroups = findLeaCommentGroups(maskedSource)
    .filter((group) => group.hasTargetMarker)
    .map((group) => ({ ...group, syntax: "comment" }));
  const tagDetection = findLeaTagGroups(maskedSource);
  diagnostics.push(...tagDetection.diagnostics);
  // leacode reads the RAW source: its interior is masked out of maskedSource
  // (it is a verbatim-like environment), so the tag/comment scanners above
  // never see it, and it is recovered here instead.
  const codeDetection = findLeaCodeEnvironments(source);
  diagnostics.push(...codeDetection.diagnostics);

  const packageDependentGroups = [...tagDetection.groups, ...codeDetection.groups]
    .sort((a, b) => a.from - b.from);
  if (packageDependentGroups.length > 0 && !hasLeaTagPackageLoaded(source)) {
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

// Tag usage is only meaningful in the document body. Restricting detection to
// after \begin{document} (when present) avoids false-positive matches against
// the package's own \NewDocumentCommand{\lea}{m}{} *definition* in the
// preamble (or a user's pasted inline-fallback snippet), which contains the
// literal substring "\lea}" but is not a tag invocation.
function documentBodyOffset(source) {
  const marker = "\\begin{document}";
  const index = source.indexOf(marker);
  return index === -1 ? 0 : index + marker.length;
}

// Expects `source` to already be masked (see maskOpaqueSpans) by the caller
// -- a tag command's literal text inside a verbatim-like environment (e.g. a
// documentation example showing \leatheorem{...} syntax) is never really
// invoked by LaTeX, and masked text simply can't match the tag pattern
// there, so no malformed_tag/missing_environment diagnostic is generated for
// it at all.
function findLeaTagGroups(source) {
  const groups = [];
  const diagnostics = [];
  const offset = documentBodyOffset(source);
  const body = source.slice(offset);
  const pattern = new RegExp(TAG_COMMAND_PATTERN_SOURCE, "g");
  let match;

  while ((match = pattern.exec(body))) {
    const commandStart = match.index;
    const commandName = match[1];
    const afterCommand = pattern.lastIndex;
    const argumentStart = skipInlineWhitespace(body, afterCommand);
    const argument = body[argumentStart] === "{" ? parseBalancedSuffix(body, argumentStart) : { ok: false };
    if (!argument.ok) {
      diagnostics.push(buildDiagnostic({
        code: "malformed_tag",
        message: `\\${commandName}{...} must be followed by a single balanced { } argument on the same line.`,
        from: offset + commandStart,
        to: offset + afterCommand
      }));
      continue;
    }

    const argumentInner = body.slice(argumentStart + 1, argument.end - 1);
    const metadata = parseMetadata(argumentInner);
    const impliedKind = TAG_COMMAND_KINDS[commandName];
    if (impliedKind) {
      // The command name is authoritative over any kind=... the argument
      // also happens to contain: \leatheorem{...} is always a theorem.
      metadata.kind = impliedKind;
    }

    // Optional second (body) argument -- the standalone form:
    // \leatheorem{label=foo}{Statement text...}. lea-tags.sty defines this
    // as an xparse `g` argument, which typesets it and needs no enclosing
    // environment. Mirrors real TeX argument-scanning exactly: it skips
    // *all* whitespace (including newlines/blank lines, not just inline
    // whitespace) looking for the next "{", verified against actual
    // pdflatex output. That means a single-argument tag immediately
    // followed by an unrelated standalone {...} group, with nothing but
    // whitespace between them, really does get absorbed -- both in real
    // compilation and here. See docs/FEATURE-overleaf-inline-lea-tags.md
    // ("Standalone form") for the caveat this implies for authors.
    const bodyArgumentStart = skipWhitespace(body, argument.end);
    let bodyArgument = null;
    if (body[bodyArgumentStart] === "{") {
      const parsedBody = parseBalancedSuffixMultiline(body, bodyArgumentStart);
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

// Detects \begin{leacode}{metadata}...\end{leacode} blocks (lea-tags.sty). The
// body between the metadata argument and \end{leacode} is Lean code, read
// verbatim and used as the target's statement text -- the same role the body
// of a standalone \leatheorem{...}{...} tag plays, so this produces the same
// synthetic-environment group shape and flows through the identical downstream
// pipeline (kind inference, label validation, extractEnvironmentText). `kind=`
// is honored exactly as the generic \lea{...} command honors it.
//
// Runs against the RAW source (not the masked source the other scanners use),
// because leacode's own interior is masked out of that for everyone else. A
// leacode block shown *inside* another verbatim-like environment (e.g. a
// documentation example) is not a real block, so matches inside those other
// opaque spans are skipped.
function findLeaCodeEnvironments(source) {
  const groups = [];
  const diagnostics = [];
  const offset = documentBodyOffset(source);
  const otherOpaqueSpans = findOpaqueSpans(source, OPAQUE_ENVIRONMENT_NAMES);
  const beginPattern = new RegExp(`\\\\begin\\s*\\{\\s*${LEA_CODE_ENVIRONMENT}\\s*\\}`, "g");
  let match;

  while ((match = beginPattern.exec(source))) {
    const beginStart = match.index;
    if (beginStart < offset) continue;
    if (otherOpaqueSpans.some((span) => beginStart >= span.contentFrom && beginStart < span.contentTo)) continue;

    const afterBegin = beginPattern.lastIndex;
    // Metadata argument: a single balanced { } on the same line as \begin, the
    // same single-line convention the tag commands' metadata argument uses.
    const metaStart = skipInlineWhitespace(source, afterBegin);
    const metaArg = source[metaStart] === "{" ? parseBalancedSuffix(source, metaStart) : { ok: false };
    if (!metaArg.ok) {
      diagnostics.push(buildDiagnostic({
        code: "malformed_tag",
        message: `\\begin{${LEA_CODE_ENVIRONMENT}} requires a metadata argument; use {} when no metadata is needed.`,
        from: beginStart,
        to: afterBegin
      }));
      continue;
    }

    const endPattern = new RegExp(`\\\\end\\s*\\{\\s*${LEA_CODE_ENVIRONMENT}\\s*\\}`, "g");
    endPattern.lastIndex = metaArg.end;
    const endMatch = endPattern.exec(source);
    if (!endMatch) {
      diagnostics.push(buildDiagnostic({
        code: "malformed_tag",
        message: `\\begin{${LEA_CODE_ENVIRONMENT}} is missing its \\end{${LEA_CODE_ENVIRONMENT}}.`,
        from: beginStart,
        to: afterBegin
      }));
      continue;
    }

    const bodyFrom = metaArg.end;
    const bodyTo = endMatch.index;
    const metadata = parseMetadata(source.slice(metaStart + 1, metaArg.end - 1));

    groups.push({
      from: beginStart,
      to: endPattern.lastIndex,
      syntax: "leacode",
      command: LEA_CODE_ENVIRONMENT,
      hasFormalize: true,
      hasDefine: false,
      hasTargetMarker: true,
      metadata,
      standaloneEnvironment: {
        name: LEA_CODE_ENVIRONMENT,
        from: beginStart,
        to: endPattern.lastIndex,
        badgeFrom: beginStart,
        bodyFrom,
        bodyTo
      }
    });
    beginPattern.lastIndex = endPattern.lastIndex;
  }

  return { groups, diagnostics };
}

// Like skipInlineWhitespace, but crosses newlines/blank lines -- used only to
// check for a tag's optional body argument, matching real TeX argument
// scanning (which skips arbitrary whitespace between macro arguments, not
// just whitespace on the same line). Every *other* same-line convention in
// this parser (the metadata argument itself, \label) deliberately keeps
// using skipInlineWhitespace; this one helper is the sole, deliberate
// exception, because it needs to match what Overleaf will actually compile.
function skipWhitespace(source, cursor) {
  while (cursor < source.length && /\s/.test(source[cursor])) {
    cursor += 1;
  }
  return cursor;
}

// Like parseBalancedSuffix, but tolerates newlines inside the matched span.
// Used only for a tag's body argument, which is routinely a multi-line or
// multi-paragraph statement; the metadata argument stays single-line via
// parseBalancedSuffix, unchanged.
function parseBalancedSuffixMultiline(source, cursor) {
  const opener = source[cursor];
  const closer = opener === "{" ? "}" : opener === "[" ? "]" : "";
  if (!closer) {
    return { ok: false };
  }

  let depth = 1;
  for (let index = cursor + 1; index < source.length; index += 1) {
    const char = source[index];
    const previous = source[index - 1];
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

function hasLeaTagPackageLoaded(source) {
  const documentStart = source.indexOf("\\begin{document}");
  const preamble = documentStart === -1 ? source : source.slice(0, documentStart);
  return (
    /\\usepackage(?:\[[^\]]*\])?\{[^}]*\blea-tags\b[^}]*\}/.test(preamble)
    || /\\input\s*\{\s*lea-tags\s*\}/.test(preamble)
    || /\\(?:New|Renew|Provide)DocumentCommand\s*\{\s*\\lea\s*\}/.test(preamble)
    || /\\(?:new|renew|provide)command\*?\s*\{\s*\\lea\s*\}/.test(preamble)
    // Inline-snippet users who only use the code-block form define just the
    // `leacode` environment, without any \lea command -- recognize that too.
    || new RegExp(`\\\\(?:lst)?newenvironment\\s*\\{\\s*${LEA_CODE_ENVIRONMENT}\\s*\\}`).test(preamble)
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

function findSupportedEnvironments(source) {
  return findEnvironments(source, { names: SUPPORTED_ENVIRONMENTS }).map((environment) => ({
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

// Finds every [contentFrom, contentTo) span whose text lies strictly between
// a \begin{name}...\end{name} pair for a verbatim-like name -- i.e. text
// LaTeX itself would never tokenize as \begin/\end/%.
function findOpaqueSpans(source, names = VERBATIM_LIKE_ENVIRONMENTS) {
  const spans = [];
  for (const name of names) {
    const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const beginPattern = new RegExp(`\\\\begin\\s*\\{\\s*${escapedName}\\s*\\}`, "g");
    let beginMatch;
    while ((beginMatch = beginPattern.exec(source))) {
      const contentFrom = beginPattern.lastIndex;
      const endPattern = new RegExp(`\\\\end\\s*\\{\\s*${escapedName}\\s*\\}`, "g");
      endPattern.lastIndex = contentFrom;
      const endMatch = endPattern.exec(source);
      if (!endMatch) break; // unterminated -- nothing real to mask, stop scanning this name
      spans.push({ contentFrom, contentTo: endMatch.index });
      beginPattern.lastIndex = endMatch.index + endMatch[0].length;
    }
  }
  return spans;
}

// Returns a string the exact same length as `source`, with every opaque
// span's interior replaced by spaces (newlines preserved, so line/column
// math elsewhere stays correct). Once masked, no "\begin", "\end", "%", or
// "\lea..." tag can ever be found inside a verbatim-like environment's
// interior, at *any* nesting depth or by *any* scanner -- not just the
// environment-pair matcher below. This is deliberately a single, blunt
// primitive rather than scattered "is this position inside a span?" checks
// at each call site: an earlier version of this fix only masked the
// environment-pair matcher, which still let a legitimately real *outer*
// environment (e.g. \begin{enumerate}, in a real false positive) have its
// raw captured body text -- taken as a plain substring of the ORIGINAL
// source by leanPaneManifest.mjs's extractLeaMarkerLabel -- independently
// re-discover a marker nested inside a verbatim sub-block within it. Every
// scanner in this module, and leanPaneManifest.mjs's own marker-detection
// slice, now runs against this masked text instead. Offsets are unaffected
// (same length), so callers that need the *real* displayed text for a
// legitimately matched environment still slice the original source.
export function maskOpaqueSpans(source) {
  const text = String(source || "");
  const spans = findOpaqueSpans(text);
  if (spans.length === 0) return text;
  const chars = text.split("");
  for (const { contentFrom, contentTo } of spans) {
    for (let i = contentFrom; i < contentTo; i += 1) {
      if (chars[i] !== "\n") chars[i] = " ";
    }
  }
  return chars.join("");
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
  const environments = [];
  const stack = [];
  const pattern = buildEnvironmentPattern(names);
  // Matched against the masked text (see maskOpaqueSpans) so no \begin/\end
  // pair can ever be found inside a verbatim-like environment's interior.
  // Offsets are identical to the original source (masking preserves length).
  const maskedSource = maskOpaqueSpans(source);
  let match;

  while ((match = pattern.exec(maskedSource))) {
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
  return { targetText: stripLeaTargetText(body), latexLabel: labelMatch ? labelMatch[1].trim() : "" };
}

// Canonical natural-language text of a marked environment body: the same text the
// staleness hash is computed over. Shared with the Lean-pane manifest so both
// surfaces hash byte-identical input and can never drift (see
// shared/leanPaneManifest.mjs and PLAN-overleaf-lean-pane-improvements.md item 1).
export function stripLeaTargetText(body) {
  return stripLeaTagCalls(String(body || ""))
    .replace(/^[ \t]*%\s*lea:.*(?:\r?\n|$)/gmi, "")
    .replace(/\\label\s*\{[^}]*\}/g, "")
    .trim();
}

// Removes \lea{...}/\leatheorem{...}/etc calls from a body of text. Brace-aware
// (not a fixed-width regex) because context={...} can itself contain braces.
function stripLeaTagCalls(body) {
  const pattern = new RegExp(TAG_COMMAND_PATTERN_SOURCE, "g");
  let result = "";
  let cursor = 0;
  let match;

  while ((match = pattern.exec(body))) {
    if (match.index < cursor) continue;
    const afterCommand = pattern.lastIndex;
    const argumentStart = skipInlineWhitespace(body, afterCommand);
    const argument = body[argumentStart] === "{" ? parseBalancedSuffix(body, argumentStart) : { ok: false };
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
    const previous = source[index - 1];
    if (char === "{" && previous !== "\\") {
      depth += 1;
    } else if (char === "}" && previous !== "\\") {
      depth = Math.max(0, depth - 1);
    } else if (char === "[" && previous !== "\\" && depth === 0) {
      bracketDepth += 1;
    } else if (char === "]" && previous !== "\\" && depth === 0 && bracketDepth > 0) {
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
    const previous = source[index - 1];
    if (char === "{" && previous !== "\\") {
      depth += 1;
    } else if (char === "}" && previous !== "\\") {
      depth = Math.max(0, depth - 1);
    } else if (char === "[" && previous !== "\\" && depth === 0) {
      bracketDepth += 1;
    } else if (char === "]" && previous !== "\\" && depth === 0 && bracketDepth > 0) {
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
