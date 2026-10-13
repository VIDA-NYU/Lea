# Plan — Inline Lea Tags (LaTeX Package)

Implementation plan for `docs/FEATURE-overleaf-inline-lea-tags.md`: a second,
opt-in marker syntax (`\leatheorem{...}` / `\lea{kind=...}` from a small
`lea-tags.sty`) alongside the existing `% lea:` comment marker, so a target no
longer needs to sit inside an allowlisted environment name.

## Status (updated 2026-06-30)

**Done: Phases 0-6, plus the standalone-form follow-up below.** All sections
in this plan are implemented and tested (202 tests passing in
`apps/overleaf-extension`, up from the pre-feature baseline of 176). The
`leatheorem` shorthand environment mentioned in the feature spec was
deferred (not built) -- everything else shipped as planned.

Two correctness issues surfaced during implementation that weren't anticipated
in this plan and are worth recording, since both would have made tag
detection silently wrong (not loudly broken) in realistic documents:

1. **Generic environment matching needed a "document" exclusion.** Section 1
   of this plan didn't anticipate that `findEnvironments(source, {names:
   null})` would happily match `\begin{document}...\end{document}` itself as
   a candidate enclosing environment -- present in essentially every real
   `.tex` file. Without excluding it, a tag with no real enclosing statement
   environment resolved to the *whole document* as its target body instead of
   producing the intended `missing_environment` diagnostic. Fixed with a
   `NON_TARGET_ENVIRONMENTS` denylist (currently just `"document"`) applied
   only to generic (non-allowlisted) matching. Caught by a test, not by
   review -- worth noting as a reminder that "any `\begin{X}...\end{X}`" is a
   wider net than it first sounds.
2. **Tag detection needed to skip the preamble.** The first implementation
   scanned the whole source for tag commands, which made `lea-tags.sty`'s own
   `\NewDocumentCommand{\lea}{m}{}` *definition* (or a user's pasted inline
   preamble snippet) match as a malformed *invocation* of `\lea`, since
   `{\lea}` contains the literal substring `\lea}`. Fixed by restricting tag
   *usage* detection (not environment-finding, not the
   `tag_package_not_loaded` preamble check, which both still need to see the
   preamble) to the text after `\begin{document}`, falling back to the whole
   source when that marker is absent. Also caught by a test
   (`leaTags.test.mjs`'s "does not false-positive on the package's own
   `\NewDocumentCommand{\lea}{m}{}` definition").

Both are exactly the kind of thing Section 4 (Edge cases) and Section 6 (Open
risks) below were trying to anticipate in the abstract ("math-mode boundary",
"diagnostic noise") -- the concrete instances turned out to be different from
what was guessed, which is the usual shape of this kind of risk.

**Follow-up (same day): the standalone (body-argument) form.** A direct
question after the above shipped -- "does a tag still need an enclosing
block, and could it not?" -- led to a same-day addition: `lea-tags.sty`'s six
commands moved from an `m`-only xparse signature to `m g` (`g` = xparse's
"optional group" type), with the optional second argument both typeset
(`\IfValueT{#2}{#2}`) and sent to Lea as the statement, e.g.
`\leatheorem{label=foo}{Statement text.}`. With a body argument, nothing
needs to enclose the tag.

This addition was verified against real `pdflatex` compilation before any JS
was written (the sandbox has `pdflatex`/`lualatex`/`xelatex` installed),
which caught the actual TeX whitespace-skipping semantics directly rather
than guessing: argument scanning skips *all* whitespace (including blank
lines) between the metadata and body arguments, so the parser's own
whitespace-skip for detecting the body argument was written to match that
exactly (a new `skipWhitespace`, distinct from the existing
`skipInlineWhitespace` used everywhere else in this parser, which
deliberately stays same-line). One real, documented consequence: a
single-argument tag immediately followed by an unrelated standalone `{...}`
group, with nothing but whitespace between them, has that group absorbed as
its body -- in real Overleaf compilation and in this parser identically.
Confirmed with a compiled fixture, then encoded as a test
(`leaTags.test.mjs`).

Implementation notes:
- The body argument itself uses a new newline-tolerant brace matcher
  (`parseBalancedSuffixMultiline`); the metadata argument's matcher stays
  single-line, unchanged.
- A tag with a body argument builds its own synthetic environment-shaped
  object (`name` = the tag command, `bodyFrom`/`bodyTo` = inside the body's
  braces) and skips the environment-lookup step in `parseMarkedTargets`
  entirely -- everything downstream (`extractEnvironmentText`, badge
  placement, the Lean pane) already only reads that shape, not where it came
  from, so no other code needed to change.
- `leanPaneManifest.mjs`'s `parseLeanPaneItemsFromFile` needed a second pass
  (standalone targets never appear in the environment walk) -- and, caught
  by a test before merge, needed candidates from *both* passes sorted by
  source offset before assigning `documentOrder`/pushing, since pushing from
  two separate loops put every standalone item after every environment item
  regardless of actual position in the file. A genuinely separate bug from
  the two above, same category: an ordering assumption that was true within
  one pass and silently false across two.
- A malformed standalone tag does not currently surface in the Lean pane at
  all (no loose-detection fallback was built for the no-environment case,
  to avoid a third near-duplicate of the metadata-extraction logic) --
  recorded as a known, accepted gap, not a target for this pass.

**Decisions locked (from the feature spec).**
- Additive, not a replacement. `% lea:` stays the zero-setup default.
- v1 distribution: manual `.sty` upload **or** a copy-paste inline preamble
  snippet. No Overleaf project-file write integration in v1.
- Tag-based targets carry `syntax: "tag"`; comment-based targets keep
  `syntax: "comment"` (the field already exists and is already populated).
- A new `tag_package_not_loaded` diagnostic fires when a tag command is found
  but nothing in the preamble defines it — this is the one new
  compile-breaking failure mode this feature introduces, so it must be
  surfaced before the author hits a broken Overleaf compile.

---

## 1. Grounding — what's there today, and one thing that isn't obvious

Three places currently know how to find theorem/definition environments and
markers, not two:

1. **`extension/targetParserCore.mjs`** — canonical parser. Exports
   `parseTargetDocument` / `parseTargets`. `findSupportedEnvironments` walks
   only `theorem`/`lemma`/`proposition`/`corollary`/`definition`;
   `findLeaCommentGroups` finds `% lea: ...` lines; `parseCommentMarkedTargets`
   joins them, validates, and extracts body text via
   `extractEnvironmentText` / `stripLeaTargetText`.
2. **`extension/pageBridge.js`** — imports `parseTargetDocument` from (1)
   directly. No independent parsing logic. This is the state the
   `FEATURE-overleaf-definition-tags.md` "Implementation Notes" section asked
   for ("should not be implemented twice with diverging logic") — already
   achieved for this surface.
3. **`shared/leanPaneManifest.mjs`** (the project-wide Lean pane,
   `FEATURE-overleaf-lean-pane.md`) — imports `parseTargets` /
   `stripLeaTargetText` from (1) for *metadata enrichment* (matches an item to
   its `uses`/`context` by environment start offset), but has its **own**
   copy of environment-finding (`findSupportedEnvironments`, same name,
   same allowlist, second implementation) and its **own** comment-only label
   extractor (`extractLeaMarkerLabel`, a regex that only understands
   `% lea: ... label=...`). This second implementation is what decides which
   items the pane shows at all.

Consequence: if tag detection is added only to `targetParserCore.mjs`, a
`\leatheorem{...}`-tagged theorem will show up in the popover/badge flow (via
`pageBridge.js`) but **silently not appear in the Lean pane**, because the
pane's own `findSupportedEnvironments`/`extractLeaMarkerLabel` never learns
about tags. This duplication is real today (confirmed by reading both files)
and isn't called out in the feature spec, so it needs to be a phase of its
own rather than an afterthought.

Other grounding facts that shape the plan:

- `targetKey`/`jobKey` in `companion/server.mjs` are built from
  `targetKind`/`targetLabel` only (`${targetKind}:${targetLabel}`,
  `buildLeaTarget`) — `syntax` is not part of identity anywhere downstream.
  Good: it means `syntax` is purely informational and the companion/adapter
  genuinely need no behavioral branching on it, confirming the feature
  spec's "no prompt changes" claim.
- `companion/server.mjs` never parses raw `.tex` itself for the formalize
  path — the extension parses client-side and POSTs already-extracted
  `targetKind`/`targetLabel`/`targetText`/`targetUses`/`targetContext`/
  `sourceHash` (see `validateTargetPayload`). It *does* parse raw `.tex` for
  the Lean pane (`handleLeanPaneManifest` → `buildLeanPaneManifest`). So
  companion changes for this feature are minimal; pane changes are not.
- `extension/content.js` renders diagnostics generically off
  `diagnostic.message` / `diagnostic.code` (three call sites, no
  per-code branching). A new diagnostic code needs no UI code change beyond
  giving it a clear `message` — confirmed by reading the render call sites.
- `findEnvironmentBadgeAnchor` (badge placement) and `parseBalancedSuffix`
  (generic brace matching) in `targetParserCore.mjs` are already
  environment-name-agnostic — they operate on whatever environment object
  they're given. Generic environment matching can reuse them unchanged.
- The extension bundles `targetParserCore.mjs` directly as a
  `web_accessible_resource` (`manifest.json`) so `pageBridge.js` can import it
  in the page's main world. Any new export needed by `pageBridge.js` must
  stay inside that file (or a file added to the same
  `web_accessible_resources` list).

---

## 2. Work breakdown

### Phase 0 — De-duplicate environment/label discovery (prerequisite)

Do this before adding tag support, not after, so tags don't get implemented a
third time.

**`shared/leanPaneManifest.mjs`**
- Delete its private `findSupportedEnvironments`, `extractLeaMarkerLabel`,
  `findEnvironmentOpenerEnd`, `parseBalancedSuffix`, `skipInlineWhitespace`,
  `isLineBreak` (all duplicates of, or near-duplicates of, code already in
  `targetParserCore.mjs`).
- Export from `targetParserCore.mjs` whatever the pane needs that isn't
  already exported: an environment-discovery function and a body/label
  extraction function that work given a generic name allowlist *parameter*
  rather than a hardcoded one. Concretely: generalize
  `findSupportedEnvironments(source)` into
  `findEnvironments(source, { names } = {})` where omitting `names` (or
  passing `null`) matches any `\begin{X}...\end{X}`, and the existing
  exported `parseTargetDocument`/`parseTargets` keep calling it with the
  fixed allowlist so today's behavior is byte-for-byte unchanged.
- Rebuild `parseLeanPaneItemsFromFile` on top of the shared
  `findEnvironments` (still called with the theorem/definition allowlist —
  the pane's allowlist question is Phase 3, see below) and a shared
  marker-label extractor that understands **both** `% lea:` and tag syntax
  once Phase 2 lands. Until Phase 2 lands, this phase is purely a refactor
  with no behavior change — land it and get the existing
  `leanPaneManifest.test.mjs` suite green before touching tag logic, so any
  regression is isolated to the refactor.

This phase has a clean, independently testable success condition: the full
`leanPaneManifest.test.mjs` and `theoremParser.test.mjs`/parser test suites
pass unmodified, with zero behavior change, before Phase 2 starts.

### Phase 1 — The package file

**New file: `apps/overleaf-extension/extension/assets/lea-tags.sty`**

```tex
\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{lea-tags}[2026/07/01 v0.1 Lea inline formalization tags]
\RequirePackage{xparse}

\NewDocumentCommand{\lea}{m}{}
\NewDocumentCommand{\leatheorem}{m}{}
\NewDocumentCommand{\lealemma}{m}{}
\NewDocumentCommand{\leaproposition}{m}{}
\NewDocumentCommand{\leacorollary}{m}{}
\NewDocumentCommand{\leadefinition}{m}{}
```

- Add it to `manifest.json`'s `web_accessible_resources` resource list (it
  needs to be fetchable from the options page, not from the Overleaf page,
  so it can also just be linked as a static options-page asset — confirm
  during implementation whether `web_accessible_resources` is actually
  required or whether `chrome.runtime.getURL` from `options.js` is enough;
  the existing entries are all `matches: ["https://www.overleaf.com/*"]`
  because they're injected into Overleaf, which this file is not, so it
  likely does **not** need to go in that list).
- `options.html`/`options.js`: add a small "Inline Lea tags" section with a
  download link for `lea-tags.sty` and a "copy preamble snippet" button that
  copies the same five `\NewDocumentCommand` lines plus the `\RequirePackage{xparse}`
  line (the inline-fallback path from the feature spec's Distribution
  section). This is the only new UI surface this feature adds to the options
  page.
- Mirror the package source into `docs/FEATURE-overleaf-inline-lea-tags.md`
  or a new `docs/lea-tags.sty` doc copy if the docs should be
  self-contained — pick one canonical location
  (`extension/assets/lea-tags.sty`) and have the other reference it instead
  of duplicating the literal source, so they can't drift.
- README: add a "Inline tag syntax" subsection mirroring the existing
  "Theorem Syntax" section, with the install step and one worked example.

### Phase 2 — `targetParserCore.mjs`: tag detection

This is the core of the feature. Plan of functions to add/change:

- `findLeaTagCommands(source)`, parallel in shape to `findLeaCommentGroups`:
  regex `/\\(lea|leatheorem|lealemma|leaproposition|leacorollary|leadefinition)\b/g`
  guarded so it doesn't match a longer user macro name sharing the prefix
  (check the character immediately after the matched name isn't
  `[A-Za-z]`, the same guard pattern `parseSameLineLabel` already uses for
  `\label`). For each match, call `parseBalancedSuffix` on the next
  non-whitespace character to extract the single `{...}` argument; skip
  inline whitespace between the command and its argument the same way
  `findEnvironmentBadgeAnchor` does. A command with no balanced `{...}`
  immediately following it is a `malformed_tag` diagnostic, not a silent
  skip.
- Map command name → implied `kind`:
  `{lea: null, leatheorem: "theorem", lealemma: "theorem", leaproposition: "theorem", leacorollary: "theorem", leadefinition: "definition"}`.
  For `\lea{...}`, `kind` must come from the argument's own `kind=` field
  (reuse `parseMetadata`, which already parses `kind`); missing `kind` on
  `\lea{...}` is the same `missing_marker`-shaped diagnostic the comment path
  already raises for a marker with neither `define` nor `formalize`.
- `findGenericEnvironments(source)`: same nesting-stack algorithm as
  `findSupportedEnvironments`, but using the `findEnvironments(source, {names: null})`
  generalization from Phase 0 (any `\begin{X}...\end{X}`, no allowlist). Used
  **only** to locate the smallest enclosing environment for a tag-based
  marker. The comment-marker path keeps calling the allowlisted version
  unchanged — no existing document's behavior changes.
- Generalize `parseCommentMarkedTargets` (rename to `parseMarkedTargets`
  internally, keep the same exported `parseTargetDocument`/`parseTargets`
  surface) so it gathers groups from **both** `findLeaCommentGroups` and
  `findLeaTagCommands` into the same per-environment grouping map
  (`groupsByEnvironment`, keyed by `environment.from`, as today), tagging
  each group with which syntax produced it. The existing
  "more than one marker group in an environment → `duplicate_marker`"
  check then naturally covers cross-syntax duplicates (acceptance criterion
  6 in the feature spec) with no new branching — it was already
  syntax-agnostic, it just only ever saw one syntax before.
- `stripLeaTargetText`: add stripping of matched tag commands (same
  balanced-brace match used for detection) alongside the existing
  `% lea: ...` and `\label{...}` stripping, so the body sent to Lea never
  contains a literal `\leatheorem{...}` call.
- Targets built from a tag group get `syntax: "tag"` instead of
  `"comment"`.
- New diagnostic, `tag_package_not_loaded`: once any tag command is found in
  the document, check the text before `\begin{document}` (or the whole
  document if that marker is absent) for
  `/\\usepackage(?:\[[^\]]*\])?\{[^}]*\blea-tags\b[^}]*\}/`,
  `/\\input\{lea-tags\}/`, or a local redefinition
  (`/\\(?:New|Renew|Provide)DocumentCommand\{\\lea\}/` or
  `/\\(?:new|renew|provide)command\{\\\\lea\b/` for the inline-snippet path).
  If none match, emit the diagnostic once per document (anchored at the
  first tag command's position, not once per tag) rather than once per
  occurrence — a forgotten `\usepackage` shouldn't flood the popover with
  one warning per tagged theorem.
- New diagnostic (optional, recommended), `suspicious_environment`: when a
  tag's smallest enclosing environment is a known non-statement environment
  (`figure`, `table`, `itemize`, `enumerate`, `equation`, `align`,
  `tabular`), warn rather than silently extracting probably-wrong body text.
  This exists because dropping the environment-name allowlist for tags is
  exactly what makes this mistake newly possible — the allowlist used to be
  an accidental guardrail against it. Non-blocking (the target is still
  produced), so it doesn't need a "kind" map beyond a small denylist
  constant.

### Phase 3 — `leanPaneManifest.mjs`: make the pane tag-aware

Decision needed (record the answer in the feature spec or here once made,
don't leave it implicit): **should the pane's own environment allowlist stay
restricted to the five named environments even for tag-based items, or
should it widen to match whatever `targetParserCore.mjs` now accepts?**

Recommendation: widen it. The whole point of tags is custom environment
names; a pane that's supposed to be "the marked-target inventory" for the
project (per the resolved decision in
`PLAN-overleaf-lean-pane-improvements.md` item 10) should show a
tag-marked `claim` block exactly as it shows a `theorem` block today.

Once Phase 0's `findEnvironments(source, {names})` and Phase 2's
tag-detection helpers exist in `targetParserCore.mjs`, `leanPaneManifest.mjs`
becomes mostly deletions:

- `parseLeanPaneItemsFromFile` calls `findEnvironments(content, {names: null})`
  instead of its own copy, and a new shared `extractMarkerLabel`-equivalent
  (exported from `targetParserCore.mjs`, built on the same
  `findLeaCommentGroups`/`findLeaTagCommands` machinery, returning just the
  resolved `label` + `kind`) instead of its private regex-only
  `extractLeaMarkerLabel`.
- `leanKind` derivation (`"def"` vs `"theorem"`) switches from "is the
  environment name `definition`" to "is the resolved marker `kind`
  `definition`" — this is a behavior-preserving change for comment-marked
  items (kind was always derived consistently with environment name there)
  and the only way tag-marked items in custom environments get the right
  `leanKind` at all.
- `naturalLanguageLatex` / `renderLightLatex` need the same tag-stripping
  `stripLeaTargetText` already gets in Phase 2 (the pane currently strips
  `%...` comments and `\label{}` itself in `renderLightLatex`, separately
  from `stripLeaTargetText` — confirm during implementation whether to unify
  these two text-cleaning paths too, since they're a fourth near-duplicate;
  not required for this feature but worth a one-line note if left alone).

### Phase 4 — Companion (`companion/server.mjs`)

Minimal, by design (Section 1 confirmed `syntax` isn't part of any identity
key):

- `validateTargetPayload`: accept and pass through an optional `syntax`
  field on the incoming payload (default `"comment"` if absent, for
  backward compatibility with any client that predates this change —
  though in practice the only client is this same extension, shipped
  together).
- Store `syntax` on the job record alongside the existing
  `targetKind`/`targetLabel` fields, purely for debugging/telemetry
  (visible in job logs / `/jobs` listing if there is one) — not used in
  `buildLeaPrompt`, `jobKey`, or any status/dependency logic.
- No new endpoint, no new prompt branch. Confirm this with a regression
  test: a tag-sourced target and a comment-sourced target with identical
  `targetKind`/`targetLabel`/`targetText`/`targetUses`/`targetContext`
  produce byte-identical `buildLeaPrompt` output.

### Phase 5 — Extension UI (`content.js`, `pageBridge.js`)

Expected to need close to nothing, given Section 1's findings:

- `pageBridge.js` needs no changes — it already imports
  `parseTargetDocument` and forwards `targets`/`diagnostics` generically; new
  `syntax: "tag"` targets and the new diagnostic codes flow through
  unchanged.
- `content.js` needs no changes for the common case — diagnostics already
  render generically off `message`/`code`. Verify (test, not new code) that
  the popover's existing "this Lea marker is malformed" fallback copy reads
  sensibly for `tag_package_not_loaded` and `malformed_tag`, or give those
  two diagnostics a more specific `message` string so the generic fallback
  copy is never shown for them (preferred — write the message strings to be
  good enough that no diagnostic ever needs the generic fallback).
- Optional, low-priority: a `(tag)` vs `(comment)` indicator in the
  diagnostics/debug view only, per the feature spec's UI section. Build only
  if useful while testing; not required for acceptance.

### Phase 6 — Docs

- `README.md`: new "Inline tag syntax" subsection (Phase 1).
- `docs/FEATURE-overleaf-inline-lea-tags.md`: update once implementation
  details are final if anything here changes the spec (e.g. final diagnostic
  code names, final package filename) — keep the spec and the shipped
  behavior in sync.
- This plan file: update the Status section (added below, once work starts)
  the way `PLAN-overleaf-lean-pane-improvements.md` tracks per-item status.

---

## 3. Suggested sequencing

1. **Phase 0 (de-duplication refactor).** Zero behavior change, fully
   covered by existing tests. Land and confirm green before anything else —
   this is the cheapest possible point to catch a refactor mistake, before
   any new behavior is layered on top.
2. **Phase 2 core (`targetParserCore.mjs` tag detection)**, with Phase 1's
   package file written alongside it (the parser tests need real `.tex`
   fixtures containing `\usepackage{lea-tags}` and tag calls, so the two are
   easiest to write together).
3. **Phase 3 (Lean pane)**, now that the shared helpers it needs exist.
4. **Phase 4 (companion)** — small, can land in parallel with Phase 3.
5. **Phase 5 (UI verification)** — mostly testing existing code paths
   against new inputs, plus the two diagnostic message strings.
6. **Phase 6 (docs)** last, once diagnostic codes and the package filename
   are final.

## 4. Edge cases to handle

- **Tag with no enclosing environment at all** → `missing_environment`
  diagnostic, same code the comment path already uses for the analogous
  case (no new code needed).
- **Tag and comment marker in the same environment** → `duplicate_marker`,
  generalized to be cross-syntax (Phase 2).
- **`\lea{...}` with no `kind=`** → reuse the existing "marker must start
  with define or formalize"-shaped diagnostic, reworded for the tag form.
- **`kind=` value other than `theorem`/`definition`** → existing
  `unsupported_kind` diagnostic, unchanged.
- **Tag command name collides with a user's own macro** (e.g. a document
  already defines `\lea` for something unrelated) → out of scope to detect
  reliably; document it as a known limitation (advise renaming on either
  side) rather than building macro-collision detection.
- **Document loads `lea-tags` but also pastes the inline fallback snippet**
  → both define the same commands; LaTeX's last definition wins, harmless,
  no diagnostic needed (the `tag_package_not_loaded` check only needs to
  confirm *some* valid source defined the commands, not exactly one).
- **Tag's enclosing environment is `figure`/`table`/etc.** →
  `suspicious_environment` warning (Phase 2), non-blocking.
- **Generic environment matching accidentally pairs mismatched
  `\begin{X}`/`\end{Y}` across a math-mode boundary** (e.g. inside
  `$...$` or `\[...\]`, which can contain literal `{`/`}` not part of any
  environment) → not a new risk introduced by this feature; the existing
  allowlisted matcher has the same theoretical exposure today and there's no
  reported issue, so no special handling planned unless it surfaces in
  testing with real documents.
- **Leftover `% lea:` comment style guidance in old docs** → no migration
  needed; both syntaxes are permanently supported per the spec's
  Compatibility section.

## 5. Testing & verification

- **`targetParserCore.mjs` (node --test, extend `theoremParser.test.mjs` or
  add a sibling `targetParserCore.test.mjs`):** tag detection for each named
  wrapper, the generic `\lea{kind=...}` form, custom (non-allowlisted)
  environment names, the `leatheorem` shorthand environment if implemented,
  `tag_package_not_loaded` (present/absent `\usepackage`, present/absent
  inline fallback definition), cross-syntax `duplicate_marker`,
  `suspicious_environment`, malformed/unbalanced tag argument, and that
  `stripLeaTargetText` removes tag calls from extracted body text.
- **`leanPaneManifest.test.mjs`:** Phase 0's refactor first (must stay
  green with zero new behavior), then new cases for a tag-marked item in a
  custom environment appearing in the manifest with the correct `leanKind`.
- **`companion.test.mjs` / `leaApiClient.test.mjs`:** `syntax: "tag"`
  payload accepted, stored on the job, and produces an identical
  `buildLeaPrompt` output to an equivalent `syntax: "comment"` payload.
- **`pageBridge.test.mjs` / `contentActions.test.mjs`:** existing generic
  target/diagnostic flow tests should pass unmodified against tag-sourced
  fixtures with no new code (this is itself the regression test that Phase 5
  needed nothing new).
- **Manual acceptance:** real Overleaf project, `\usepackage{lea-tags}` in
  the preamble, a `\begin{claim}` environment tagged with `\leatheorem{...}`
  — confirm it compiles (tag invisible in the PDF), appears as a badge/popover
  target, appears in the Lean pane, and formalizes through the existing
  companion flow with no prompt difference from an equivalent comment-marked
  theorem. Then remove the `\usepackage` line and confirm the
  `tag_package_not_loaded` diagnostic appears in the extension *before*
  attempting to compile in Overleaf.

## 6. Open risks

- **(Medium) Phase 3 scope creep.** Widening the Lean pane's allowlist to
  "any environment" (recommended above) is a small spec decision riding
  along with an implementation plan — flag it for explicit confirmation
  before building, the same way `PLAN-overleaf-lean-pane-improvements.md`
  item 10 was called out as a decision rather than assumed.
- **(Low) `xparse` dependency.** Assumed available on Overleaf's TeXLive by
  default; confirm during Phase 1 rather than assuming — if not guaranteed,
  fall back to a dependency-free `\newcommand` definition (loses nothing,
  since all commands are no-ops and don't need `xparse`'s argument-parsing
  power; `xparse` was a convenience choice, not a requirement, and could be
  dropped to shrink the preamble snippet further).
- **(Low) Web-accessible-resource question in Phase 1.** Needs a quick check
  of whether `options.html` can `fetch`/link a packaged extension asset
  without adding it to `web_accessible_resources` (it almost certainly can,
  via `chrome.runtime.getURL`, since that list is for page-world injection
  into matched sites, not for the extension's own pages) — flagged so it
  isn't assumed incorrectly during implementation.
- **(Low) Diagnostic noise from `suspicious_environment`.** If real users
  legitimately tag inside environments not on the small denylist this plan
  proposes, the warning will be silent (no false positive) but also useless
  for those cases; treat the denylist as a starting point to expand from
  observed false negatives, not a complete list to get right up front.
