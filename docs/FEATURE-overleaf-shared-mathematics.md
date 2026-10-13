# Project mathematics and shared dependencies

The Overleaf companion and standalone interface still use the in-process adapter on
port 8001. Shared results are ordinary formalizations and ordinary adapter runs.
The prover has no adapter imports. Migration `0029_project_library` adds disposable
declaration metadata, dependency lifecycle records, attempts, graph edges, and tool
acknowledgments. Proof content uses the existing timeline/artifact store and artifact
APIs; session validity remains derived.

## Finding mathematics

Startup, project context, searches, and dependency admission reconcile the
filesystem. Existing files stay in place. Lexical discovery supplies names and
locations; `lea/LibraryInspect.lean` supplies compiler names, complete types,
private visibility, definition bodies, and transitive axiom dependencies. Temporary
`.lea` probes, build directories, `.lake`, `.git`, and candidates are excluded.
Search is paginated and has no alphabetical 100-file cutoff.

Returned modules are built with the real Lake module builder. SafeVerify supplies
audit evidence. A definitions-only module receives a temporary trivial theorem
entry point for the same kernel replay. A failed or unavailable audit remains
visible and unverified. File contents, imported project module contents,
package/toolchain revisions, and inspector/auditor revisions key the check cache.
Changed or missing files lose current verification immediately. Unchanged modules
are not rebuilt or audited again.

Private historical helpers are evidence, not import destinations. A proposition
definition is not a proof. Complete theorem types retain ordinary hypotheses and
unresolved claim parameters. A wrapper assuming `BPSDegreeReductionClaim` cannot
discharge a request for that claim.

## Ownership and placement

Agents search the project library and Mathlib, inspect hypotheses and definitions,
then import compatible results or prove adapter lemmas. Missing mathematics is
reserved before writing. The backend supplies complete placement:

| Result | Destination under the authoritative `Lea.<Project>` root |
| --- | --- |
| Document target | Its current target file |
| New shared helper or definition | `Shared/<ReadableName>_<stable-id>.lean` |
| Specific cited result | `External/<CitationKey>/<ResultName>_<stable-id>.lean` |
| Target-specific tactical glue | Target file; private declarations allowed |
| Existing checked public mathematics | Its current module |

One module per reservation avoids concurrent appends. There is no mandatory
umbrella import. Shared modules cannot import unfinished consumers. New definitions
needed by shared lemmas must be independently reserved first.

Canonical identity combines project, declaration kind, and elaborated type, with
binder/universe names normalized. Definitions also include their defining body.
Different hypotheses, constants, or bodies stay distinct. Names, citations, and
similarity never establish equivalence. Atomic SQLite admission elects one writer;
repeated requests return the same result or blocker. Publication requires matching
ownership, the pinned exported statement/body, a current real build, an importable
name, allowed axioms, and SafeVerify.

## Host tools and routes

Opt-in tools `search_project_library` and `request_project_dependency` yield typed
`ProjectLibraryRequested(tool, arguments, invocation_key)` events. The adapter
answers `ProjectLibraryAck(result, error)`. Both sides enter the normal tool journal;
a durable acknowledgment cache makes replays idempotent. The vendored prover knows
only this request/acknowledgment seam.

Dependency requests contain `mode` (`inline` or `deferred`) and a specification:
name, kind, natural-language statement, Lean signature (binders/type without a name
or proof), imports, and relevant source references. Definitions pin a body. Citation
metadata contains a key and optional result locator. Responses are `existing`,
`inline`, `deferred`, or `blocked`, with canonical path/module/namespace/name and a
skeleton where a statement is available.

Inline reservations let the current run prove short helpers. Successful checks
publish independently of the parent theorem's outcome. Deferred reservations hand
drafts and notes to normal runs with `purpose="project_dependency"`, a dependency
ID, and a pinned specification, rather than an Overleaf source block. Standalone
project chats can search and reserve inline results; scheduling is Overleaf-only.

Routes under `/api/projects/by-slug/{slug}`:

- `GET /library?q=...&limit=...&offset=...`, `GET /library/declarations/{name}`.
- `GET /dependencies` (optional `requester_id`), `GET /dependencies/{id}`.
- `POST /dependencies/ensure`, `/dependencies/{id}/run`, `/retry`, `/resume`.

## Waiting, continuation, and batches

Deferred requests return `stop_reason="dependency_wait"`. The prover journals the
acknowledgment, cancels later preplanned tools, and saves a clean turn-boundary
checkpoint. The adapter settles the execution segment as recoverably cancelled,
retaining its waiting dependency IDs, transcript, files, and allowance.

The companion prioritizes requested dependencies, recursively resolves supporting
obligations, awaits other owners, and continues the saved requesting checkpoint via
`POST /api/runs/{id}/continue` with `dependency_resume=true`. Original model,
statement, saved turn, remaining cost, and active execution time survive. Waiting
does not consume the target's timeout. Workers use normal configured limits and the
normal spend cap. Source/runtime/checkpoint incompatibility requires review.

Formalize and Formalize all use the same driver. Document ordering starts from
`lea:uses`; cycles are rejected before admission. Shared nodes live in the backend
registry; batch targets, waiting jobs, and dependency progress survive in companion
snapshots. The graph includes durable source annotations and project import closure
checks. Failures return a shared blocker while independent targets continue.
Restart reattaches to recorded runs and reconciles ownership without replacement
paid work. Cancellation detaches observers where another consumer needs the worker.
Clean interrupted worker checkpoints can resume without another mathematical
attempt; uncertain state requires review. Explicit retry creates another attempt
under the same identity.

Failed attempts are reused until explicit retry or relevant excerpt, mathematics,
import, or package changes. Unrelated LaTeX edits and later batches do not retry
them. Cited tasks are created only for precise needed results. Mirrored sources,
bibliography metadata, uploads, Lean, and Mathlib are available; papers are not
fetched automatically. Missing proof text alone does not prevent proving a precise
statement independently.

The existing panel separates document totals from shared results and exposes
waiting/reuse/proving states, canonical signatures/imports, sources, attempt links,
affected targets, and retry/resume actions. Report facts distinguish new shared
results, reused results, and blockers. Old proofs do not become new document proofs.
Generated context supplements project instructions and memory without overwriting
them.

## Validation

Deterministic model/tool and companion tests cover A waiting for L, one attempt for
L, A continuing, and B reusing L in single and batch workflows. Backend tests cover
concurrent ownership, cached failure/retry, cycles, independent helper publication,
statement pinning, and cached checks. Real Lake projects exercise public/private
Unicode declarations, implicit parameters, alpha normalization, defining bodies,
and imports. Polynomial helpers are inspected in place. Cross-project libraries
and automatic extraction of historical private helpers are deferred.
