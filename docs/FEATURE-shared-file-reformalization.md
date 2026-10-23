# Feature: Reformalization That Preserves Existing Work

> Status: implemented — 2026-10-15. The supported initial runtime is macOS with a successful Seatbelt probe. See the delivery and verification record in the implementation plan.
> Implementation: [PLAN-shared-file-reformalization.md](./PLAN-shared-file-reformalization.md).

## Summary

Re-formalizing a theorem or definition must work when its Lean file contains other
declarations. Lea uses current source and previous progress, completes the selected
target and its necessary helpers, and handles safe file organization automatically.
The user never has to split a file merely to make a retry possible.

For example, `Results.lean` may contain helper H, completed theorem A, and unfinished
B. **Re-formalize B** should improve B, complete an eligible unfinished H if needed,
and preserve A. Sharing a file is ordinary project context.

The first release serializes foreground retries within each project. The next retry
captures its baseline after the previous operation settles. It does not require
merging competing solver edits. Independent projects can run concurrently.

## Problem and current behavior

The companion returns `409 shared_artifact` before retry/resume handling when the
adapter reports an existing shared file. This protected an older process that
retired the previous file before another attempt; it could otherwise erase sibling
proofs. Current retries keep the working file, but the blanket refusal remains.
See [companion/server.mjs](../apps/overleaf-extension/companion/server.mjs).

The shared-file detector combines recorded owners, file associations, and a lexical
scan. A theorem's own helpers can trigger it. In addition, some adapter validity and
review paths use whole-file `sorry` checks. The prover's `ProofVerificationState`
also rejects such checks, and final classification extracts a theorem signature
without explicit target selection. Fixing only admission or adapter labels cannot
deliver shared-file completion. See [artifacts.py](../apps/lea-standalone/adapter/app/artifacts.py),
[formalization_reviews.py](../apps/lea-standalone/adapter/app/formalization_reviews.py),
and [prover/lea/agent.py](../apps/lea-standalone/prover/lea/agent.py).

## What preserving existing work guarantees

These guarantees apply to operations performed through the adapter and its managed
execution environment. The baseline includes current relevant bytes, including
uncommitted/untracked work; Git HEAD alone is insufficient.

| Work or event | Guarantee |
| --- | --- |
| Content before an operation | Retain durable before-images and provenance for every replaced, moved, or deleted file, including manual edits. Never reset the project repository to recover a retry. |
| Unrelated declarations and context | Preserve their source, names, mathematical contracts, definition bodies, and visibility, except for validated mechanical relocation/import edits. |
| Selected target and eligible helpers | Their proofs may change to finish the request. Reuse useful progress and retain the old version and a diff; byte-for-byte preservation of editable proofs is not promised. |
| Ordinary proof update | Preserve the target's existing interface and prevent new failures in previously passing affected consumers. |
| Source-driven contract update | Adapt the selected target to an explicitly changed source statement/definition. Preserve dependent source/history, but report any dependent that now needs repair; continued validity of those proofs is not promised. |
| Failed, paused, cancelled, or incomplete attempt | Retain its useful drafts and diagnostics without applying that unfinished target change over canonical work. Independently verified helper publications remain published. |
| Checks and approvals | Keep the completion lineage of unaffected, semantically unchanged protected declarations after actual canonical rechecking, including across a source update to another target. Derive current validity from actual current inputs. Code preservation does not fabricate a new check, source approval, SafeVerify result, or human audit. Existing revision-bound approvals may become stale. |
| Conflicting versions | Keep the baseline, candidate, and observed current version. Never use last-writer-wins or silently restart a paid solver. |

**External editors have a different boundary.** Arbitrary writes outside the
adapter do not participate in its queue or locks. Lea detects observed changes,
checks again before application, and retains the displaced file version before
installing a replacement. An occupied destination or detected divergence becomes a
conflict. A hash comparison followed by replacement is not an atomic
compare-and-swap, and this feature does not guarantee preservation of every
arbitrary external write or intermediate version. Coordinated adapter edits receive
the stronger serialization guarantee. Do not add a routine confirmation dialog for
this limitation or present it as a request to reorganize files.

## Scope and product decisions

Cover individual **Re-formalize**, batch retries, theorem/definition targets,
imported and manually renamed targets, multiple associated files, paused staged
attempts, existing correspondence review, and verified disproof outcomes. Keep the
same formalization ID, source origin key, and conversation across retries.

Stage these explicit retry operations, including existing one-target files, under
one preservation policy. This does not migrate every ordinary chat or first
formalization to a new runtime. Other project writers must cooperate with retry
ownership and the brief application reservation.

Default to editing the current file. Extract a target/helper only when needed;
update imports, compatibility modules, and current mappings automatically. Do not
force every shared file into a new layout. Project identity, remotes, source LaTeX,
toolchain/package pins, instructions, and memory are outside automatic refactoring.

The first release does not provide semantic three-way merging, concurrent
foreground retries in one project, general refactoring, or automatic paid repair
of unrelated targets. It does not change source-faithfulness or pause policy.

## Identity, edit scope, and helper completion

Resolve the stable target identity to its exact current qualified declaration and
associated files. Accept a short name only if unambiguous. A file association or a
common namespace does not establish exclusive ownership. The host derives the
edit scope from current records, source, compiler references, and dependency
reservations; no additional user approval is required for routine scope discovery.

| Content | Permitted work |
| --- | --- |
| Selected target | Revise its proof, fill gaps, and adapt it to current authoritative source; preserve a manual name unless source requires another. |
| Existing target-only unfinished helper | Include it in the edit scope when ownership/use is established. Complete its existing contract without weakening hypotheses or adding assumptions. A completed helper's mathematical contract remains protected. |
| Existing unfinished shared helper | Complete it through the shared-dependency ownership service, preserving its full contract and checking affected consumers. This is a scoped dependency suboperation, not blanket permission to rewrite other results. |
| Existing completed shared result | Reuse it unchanged or add a specialized adapter lemma. Do not rewrite its claim or implementation to make the target pass. |
| Unrelated or uncertain content | Protect it. Investigate uncertain ownership automatically; do not infer permission from a filename or silently widen the target's mathematical scope. |

Manual edits within the selected target are part of its starting work, not immutable
text. Prefer retaining useful parts; if replacement is necessary, preserve the
before-image and explain meaningful changes. Manual comments/context outside the
edit scope remain protected. Filling a helper's `sorry` under its fixed statement
is permitted; weakening that statement is not.

A registered inline helper written in an attempt needs its own checked publication
path. Publish only that helper and its independent prerequisites, not the target's
unfinished draft. If it shares a file with the target, construct the helper-only
update against canonical bytes and leave the target unchanged. Refresh the parent
attempt after publication. If that helper-only module cannot yet elaborate, keep
the helper's checked progress local to the owner until safe extraction or a joint
checked target/helper application is possible. It is not globally ready, and the
parent must not deadlock waiting for itself to publish it. Reuse existing helper
IDs/reservations; do not create extra formalizations for ordinary local glue or
certify a private helper as public.

## Execution and retained drafts

Prepare the retry in an adapter-owned attempt workspace with the same effective
Lake module layout, source context, and package/toolchain identity. Give it its own
writable sources and build outputs. Reuse immutable dependency material where safe;
never link writable canonical sources or build caches into the attempt.

Every model-controlled filesystem mutation and every Lean/Lake execution must use
a filesystem-restricted executor. This includes compiler metaprograms, `#eval`,
module initialization, build hooks, shell commands, and child candidates. Canonical
sources are unavailable for writing inside that executor. Changing `cwd`, filtering
file-tool paths, or disabling shell alone does not enforce this boundary.

Keep normal proof workflows available through the confined executor. Shell access
may remain when it has the same enforced restrictions. Tools outside that boundary
must be read-only or use explicit host-managed dependency/publication operations.
A runtime without the required execution protection reports a technical capability
failure before paid solver work; it must not advertise safe staged retries.

Use existing immutable content/history storage with explicit attempt applicability.
Drafts are inspectable in the same conversation, but cannot become current artifacts
or reusable library entries merely because they are newer. Draft-specific review
is permitted under the explicit review contract below. Filesystem cleanup must not
remove the only copy of a draft or a publication recovery record. Snapshots include
the current project's Lean sources, compiler-resolved transitive source imports,
pinned configuration, and supported context documents/attachments. They exclude
other projects' unrelated sources and all displaced archives, prior stages,
checkpoints, and retained-draft caches. Prior progress is materialized separately
from immutable blobs with a bounded lookback, so retries do not recursively copy
recovery history.

## Verification and result selection

Use one explicit selected result identity across tool checks, the prover's final
gate, final classification, adapter completion, review, and SafeVerify. It includes
the target formalization, actual qualified result declaration, result kind, path,
content, source revision, and environment. The last written file or first theorem
in a file is insufficient.

For `proved` or `defined`, the requested target must exist unambiguously with the
expected kind. For `disproved`, explicitly associate a checked negation or
counterexample declaration with that target; it may have another Lean name. Do not
rename the requested theorem to that name, require a proof of the false original
claim, or display a disproof as proof success. Retain earlier artifacts as history
or clearly identified prior-source work.

Separate module elaboration from target trust. An unrelated sibling's `sorry` may
remain without blocking an independent target. A target using that sibling, or
another transitive `sorryAx`/unsupported-axiom dependency, is incomplete. Apply this
rule in `ProofVerificationState`, final gate messages, classification, adapter
validity, review eligibility, and target-selected SafeVerify, not just the ledger.

Preserve protected declarations and shared command context. Lexical discovery is
navigation metadata, not a safe editing parser. Use bounded text/command edits and
compiler evidence for names, complete types, definition bodies, visibility, and
axiom closure when context or placement changes. Rebuild changed imports before
checking consumers and invalidate stale LSP/build evidence.

### Ordinary proof updates and source-driven changes

For an ordinary proof update, preserve the target interface and previously passing
affected consumers. Unexpected deletion, weakening, changed protected mathematics,
or new consumer errors reject application.

When current source explicitly requires a changed target statement or definition,
record the old/new contract and the source evidence for that change. A source hash
change alone does not authorize arbitrary interface changes. Verify the selected
new target, preserve dependent proof source/history, check affected consumers, and
publish with an explicit impact report if some now require repair. Do not rewrite
their claims, assume those repairs are authorized, or keep showing their old checks
as current. This source-driven outcome is not an incomplete retry merely because
old consumers need adaptation.

The actual result module must elaborate successfully. If a broken dependent shares
that module, Lea must separate the result safely before reporting it checked. A
legacy/compatibility consumer module may remain failing only for the recorded
source-driven impact; it is visibly in need of repair, not certified. Unexpected
failures or unverified attribution retain the candidate for review. Unrelated
pre-existing failures elsewhere in the project do not require project-wide repair.

## Reviewable candidates and disproofs

A technically checked candidate with outcome `needs_review` remains an explicit
review candidate for its own target, separate from current canonical work. The
existing correspondence UI can inspect it and choose an allowed existing result
kind. Review binds the attempt ID, exact result declaration/kind, source, blobs,
and environment. It cannot waive preservation or technical trust requirements.

Approval queues application of that exact candidate; it does not start another
solver. Reacquire project ownership, validate the reviewed inputs, perform actual
canonical checks, and publish with the approval lineage. If source, candidate,
relevant canonical baseline, or dependencies changed, retain the work and mark the
review stale. Do not silently merge, transfer approval, or charge for a replacement
attempt. Withdrawal before application prevents it; withdrawal afterwards uses the
existing review policy and does not reset files.

A verified disproof can follow the ordinary existing outcome policy or the same
review path if correspondence is uncertain. Both preserve previous work and keep
the target's stable source identity distinct from its result declaration.

## Serialization, application, and recovery

The first release uses one durable FIFO foreground retry queue per project. This
also serializes retries in different files; it is an intentional initial tradeoff
that avoids dynamic lock expansion and solver merging. Repeated clicks/idempotent
submissions attach to existing work. A queued retry starts from the latest source
and project bytes when it receives ownership, rather than an obsolete snapshot
captured when clicked. Waiting in the queue does not consume model turns or the
active solver allowance.

Other adapter operations that could mutate existing project work wait or return
their existing busy result while the retry owns that project. Read-only views can
remain available. Managed dependency workers use the same project execution slot:
a waiting parent releases its execution reservation while keeping logical ownership,
then helpers execute serially and capture their baselines when admitted. Helper
publication refreshes the parent's baseline inside the publication gate before it
can resume. Workers cannot alter the parent's target or arbitrary existing files.
Independent projects remain parallel.

Keep the logical foreground owner while waiting on a required dependency, but hold
no filesystem/publication/DB lock across model calls or waiting. A dependency worker
must not wait for the parent's queue slot. After a settled pause or review wait,
release foreground ownership. Resume/approval requeues and validates its baseline;
stale checkpoints/candidates stay retained rather than being automatically merged.
Reviewed application joins FIFO at approval time. One durable successor request is
allowed per paused segment; matching submissions reattach and different concurrent
requests cannot replace it. Stop blocks subsequent automatic continuation. Stop on
an already settled operation leaves its receipt and completion unchanged. Explicit
acknowledged crash recovery starts a new turn from the last safe conversation and
the retained stage, using the remaining original allowance; it does not replay an
uncertain checkpoint or action.

Before application, compare exact bytes/existence, source, associations, namespace,
and dependency/environment identities. Unexpected divergence stops application
with a retained conflict. Known independent helper publications are refreshed and
rechecked under their recorded identities; they are not competing solver merges.

Acquire the exclusive publication reservation before preparing a journal, and
keep it while canonical files are applied and checked. Retain before/after blobs and an operation journal covering exact path/mapping
changes, displaced versions, checks, and any Git commit. Append canonical history
only after actual canonical checks and accepted outcome/impact validation. Draft
checks cannot be relabeled as canonical checks. Keep unrelated Git index entries
out of the commit.

Readers must not observe a half-applied move as accepted work. On failure/restart,
finish or compensate the journal without model calls, duplicate history, or a
repository reset. Restoration recognizes paths already restored to their recorded
before-image and can restart after rollback displacement. Record rollback archive
intent before moving a candidate, so an observed editor version remains discoverable
after a crash. Reconcile accepted helper receipts into parent stages idempotently
before admissions restart. Do not knowingly overwrite a divergent version while restoring;
retain the operation as a conflict if safe restoration cannot be established.
External-editor races remain subject to the boundary stated above.

## User experience and compatibility

Keep the existing **Re-formalize** action without a file wizard or routine approval
of organization. Show queued, working, checking, applying, review, and retained-draft
states through existing activity/Lea Status presentation. Show meaningful moves,
helper completion, or downstream repair needs in the result.

Examples:

> Re-formalized B. Existing declarations were preserved.

> Updated B to match your changed statement. A's proof is retained and needs repair.

> The candidate passed Lean and is ready for correspondence review. Your current
> published work is preserved.

On failure, show the obstruction and access to both retained progress and canonical
work. Reuse compatible partial progress in a later retry after comparison with its
new baseline. If current LaTeX changed, a retained old proof remains visibly stale.
Never ask the user to move declarations as the standard recovery action.

Individual/batch retries and compatible staged continuations use the same contract.
Keep model, spend, waiting, source-obstruction opt-in, and batch pause policies.
Legacy canonical checkpoints either resume under verified compatible assumptions
or remain retained with **Re-formalize** available; do not silently reinterpret
an old checkpoint as a fresh staged attempt. Older clients cannot bypass safe
admission; older adapters must not receive an unsafe retirement fallback.

## Acceptance criteria

| ID | Required observable result |
| --- | --- |
| AC1 | B completes beside helper H and completed A without a file-move request; protected A and H are preserved. |
| AC2 | A theorem with its own helper lemmas is admitted and resolves to the intended target. |
| AC3 | Definitions with namespace helpers and multi-file targets use the same preservation contract. |
| AC4 | Imported targets without old jobs, manual names, uncommitted edits, and untagged content are retained; editable manual target changes remain recoverable. |
| AC5 | Necessary extraction updates imports, compatibility paths, and target mappings without duplicate declarations/formalizations. |
| AC6 | An independent target completes beside unrelated `sorry`; a target transitively using it cannot be certified, including at the prover's final gate. |
| AC7 | Unexpected deletion, weakening, protected context/contract changes, or proof-update regressions reject application. |
| AC8 | Failure/cancel/pause/time/spend outcomes retain drafts without applying unfinished target work; independent verified helpers remain valid. |
| AC9 | Two retries in one shared file execute serially; the second captures the first's settled result and both updates survive without solver merging. |
| AC10 | Stale source, dependency, package, namespace, path, or association inputs stop application; retained candidates are not silently rebased. |
| AC11 | Explicit target/result selection and actual canonical checks support completion; wrong-first-declaration, draft, and stale evidence cannot certify it. |
| AC12 | Restart at application boundaries finishes or compensates coherently, preserving captured versions without duplicate history or repository resets. |
| AC13 | Individual/batch/dependency/resume paths share the contract, with no queue deadlock, paid waiting, or extra organization approvals. |
| AC14 | One-target/no-change retries and existing source, audit, and review policies remain correct; preservation does not fabricate approvals. |
| AC15 | Eligible existing local helpers can be completed; an inline shared helper is independently published from staged content without leaking the target draft. |
| AC16 | A checked draft can be reviewed for its own target; approval queues revalidated application without another solver, and stale/withdrawn approvals cannot apply. |
| AC17 | An explicitly associated, differently named verified disproof remains a successful disproof of the same source target. |
| AC18 | A source-required contract change may publish a checked target with retained dependent proofs marked for repair; ordinary proof updates cannot use this exception. |
| AC19 | Adapter edits serialize; observed external changes/occupied paths retain displaced versions and conflict safely. Tests/documentation do not claim universal external-editor exclusion. |
| AC20 | The execution boundary prevents canonical writes through file tools, shell, Lean metaprograms/build hooks, and children while required proof workflows remain usable. |

## Related contracts

Reuse [multi-formalization identity/history](./FEATURE-multi-formalization-sessions.md),
[shared mathematics and dependencies](./FEATURE-overleaf-shared-mathematics.md),
[source-faithful formalization](./FEATURE-overleaf-faithful-formalization-and-lea-check.md),
[live Lea Status](./FEATURE-overleaf-live-lea-status.md),
[counterexample outcomes](./FEATURE-counterexample-workflows.md), and
[self-repair impact reporting](./FEATURE-overleaf-self-repair.md).
[GitHub import](./FEATURE-github-project-import.md) stays additive; updating an
imported target follows this retry contract. General extraction of historical
private library results remains outside scope.
