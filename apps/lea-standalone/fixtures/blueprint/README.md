# Blueprint parser fixtures

This folder is the shared oracle for the two blueprint parsers:

- `adapter/app/blueprint.py`: Python, the system of record (`parse`, `render`,
  `validate`). Every write of `.lea/blueprint.md` goes through it.
- `src/app/lib/blueprintModel.js`: the UI copy (`parseBlueprintMarkdown`,
  `serializeBlueprint`). It must read a file the same way the adapter does.

The format rules live in `docs/PLAN-blueprint-tools.md` §2.1 (monorepo root).
They are not restated here. If a case and §2.1 disagree, the case is wrong.

## Case layout

Each case is a pair:

- `NN-name.md`: the input text.
- `NN-name.expected.json`: what parsing it must produce.

```text
{"canonical": false,
 "nodes": [{"key", "kind", "lean", "file", "uses": [...], "statement_starts_with": "..."}],
 "prose_sections": ["Goal"],
 "warnings": [{"code": "...", "node": "..."}]}
```

- `nodes`: the parsed nodes, in file order. Every field is compared:
  `key`, `kind`, `lean` and `file` (`null` when absent), and `uses` (`[]` when
  empty). `statement_starts_with` is a prefix check on the trimmed statement.
- `prose_sections`: the headings of the `## ` sections that are not nodes, in
  order. Python compares them with the prose blocks of `parse(text)["blocks"]`.
  JS only has to keep them out of `nodes`.
- `warnings`: compared as a set of `(code, node)` pairs. `message` is not
  compared. Python only (the JS parser does not validate).
- `canonical: true` means `render(parse(text)) == text`, byte for byte.

Every case, canonical or not, must satisfy
`render(parse(render(parse(t)))) == render(parse(t))`. The rendered text ends
with exactly one `\n`, and parsing it gives the same nodes as parsing `t`.

## Cases

| Case | What it pins down |
|---|---|
| `01-clean` | Canonical rendering: title, preamble, header order `kind, lean, file, uses`, backticks around `lean` and `file`, one blank line between blocks, one trailing newline. |
| `02-realanalysis-blend` | A real agent-written file, copied verbatim from `prover/workspace/proofs/Lea/RealAnalysis/.lea/blueprint.md` at the keystone commit. Do not refresh it from that file. `## Goal` is prose, `uses: none` is empty, only the first backtick span of `lean:` counts, `- status:` is dropped with `unknown_header`. |
| `03-lean-variants` | `lean:` and `file:` extraction: backtick span first, else the first token with trailing `,;:.)` stripped, empty means `null`. |
| `04-uses-variants` | `uses:` splitting on commas and whitespace, stripped backticks, and the empty sentinels (`none`, `—`, `-`, `n/a`, `Nothing`). |
| `05-fenced` | Fences are inert: a `## ` heading and a header line inside a fence (in the preamble and in a statement) are plain text. The statement starts with a fence. |
| `06-validation` | Per-node warnings: `duplicate_key`, `missing_kind`, `unknown_kind`, `invalid_key`. All five nodes are still parsed. |
| `07-edges` | Document-level warnings: `cycle`, `dangling_dependency`, `self_dependency`. Edges are kept as declared. |
| `08-prose-sections` | Prose sections between nodes keep their place in canonical output. |

## Conventions these cases settle

§2.1 does not spell these out. The cases decide them:

- A node that uses itself gets `self_dependency` only, not `cycle`. `cycle` is
  for cycles of two or more nodes (07).
- Duplicate keys: every node is kept, in file order (06).
- An invalid key is kept verbatim as the node's key (06, `Bad Key!`).
- `kind` is lower-cased even when it is not a known kind (06,
  `Proposition` becomes `proposition`).
- The canonical form of a prose section is `## <heading>`, a blank line, then
  the body (08).
- A fence inside a node's statement does not split the node (05).

## Consumers

- `adapter/tests/test_blueprint_fixtures.py`:
  `cd apps/lea-standalone/adapter && uv run pytest tests/test_blueprint_fixtures.py -q`
- `src/app/blueprintFixtures.test.mjs`:
  `cd apps/lea-standalone && node --test src/app/blueprintFixtures.test.mjs`

Both collect every `*.md` here except this README, in sorted order. To add a
case, take the next `NN`, write both files, and run both consumers.
