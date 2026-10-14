# Implementation Plan: Live Lea Status During Overleaf Formalization

**Status:** Runtime implemented and deterministic checks passed. See the [implementation and validation record](IMPLEMENTED-overleaf-live-lea-status.md) for delivered behavior, test evidence, activation instructions, and the remaining manual/live pilot. The detailed checklists below are the original acceptance plan; they are not a claim that the release pilot has run.

**Date:** 2026-09-19

**Code inspected:** `ed2d21d`, plus the working-tree feature specification.

**Specification:** [Live Lea Status During Overleaf Formalization](FEATURE-overleaf-live-lea-status.md)

**Replaces for future work:** [Faithful Formalization and Dual Checks implementation plan](PLAN-overleaf-faithful-formalization-and-lea-check.md). That document records the currently implemented evaluator architecture.

Implement this as one integrated solver/reporting flow. Lea calls `update_lea_status` while formalizing; the adapter persists and streams the assessment immediately; the Overleaf pane updates its confidence tag and explanation. Retire every separate Lea Check execution path while retaining historical reports and usage.

The confirmed correction policy is part of the implementation contract: disclose and continue when the theorem and any explicitly supplied proof approach are preserved. A missing proof, ordinary proof gap, Lean encoding choice, or equivalent library lemma is non-blocking for a precise statement. Publish a blocking finding when continuing requires a semantic change or abandoning an explicitly supplied proof's essential approach. Automatic source-obstruction pausing requires a settings opt-in and is always disabled for **Formalize all** runs.

Post-implementation policy addendum: a proofless target that still reaches a structured `source_obstruction` pause exposes a scoped **Continue best effort** action in the Lean pane. The companion validates the prior pause and absent proof, records the override on the resumed job, and supplies an author-authorized prompt section. The normal faithful policy remains unchanged, and **Resume faithfully** remains available.

The pause-policy revision adds a default-off companion setting, a per-run `allow_source_pause` snapshot in the adapter, and a host acknowledgment that requests `source_obstruction` only when the snapshot is true. The prover prompt reports findings under either policy. **Formalize all** passes false for every item and settles failures by skipping dependents while continuing independent items. This amendment supersedes the original pause assumptions in the historical work-package checklist below.

**1. Delivery order and boundaries**

Use these work packages as independently reviewable changes. “Complete” means the implementation and its package-specific checks pass; it does not mean the feature is released. All packages are required for the final supported workflow.

| Package | Deliverable | Dependencies | Completion evidence |
| --- | --- | --- | --- |
| P0 | Shared reporting contract and reusable source/revision helpers | None | Contract, hash, finding-transition, and snapshot fixtures |
| P1 | Durable run source context and append-only updates | P0 | Admission, migration, transaction, history, and deletion tests |
| P2 | Immediate prover publication and typed acknowledgment | P0 | Fake-host tests proving ordering, isolation, and cancellation |
| P3 | Adapter publication, current/history APIs, and stop semantics | P1, P2 | Bridge/API integration with a blocked downstream tool |
| P4 | Overleaf prompt, reporting cadence, and retained context | P2, P3 | Prompt/loop/compaction tests without provider calls |
| P5 | Companion delivery and all supported run entry points | P1, P3, P4 | Formalize, repair, continuation, refresh, and batch tests |
| P6 | Lea Status UI, history, freshness, Pause/Resume | P3, P5 | DOM/action tests and browser validation |
| P7 | Evaluator retirement and coordinated cutover | P3–P6 | No evaluator launch on any former entry point; legacy reads work |
| P8 | Integrated regression suite and pilot release | P0–P7 | All acceptance criteria mapped to passing evidence |

Keep implementation inside the existing extension → companion → adapter → in-process prover architecture. Do not create another backend, introduce browser connections per target, store proof files in status rows, or persist another session proof-status field.

Introduce the core capability behind an adapter setting while building it. The setting controls admission to the new flow, not whether an already admitted run can publish. Each admitted run retains a fixed reporting-contract version through completion. P7 removes the old execution path before the new workflow is released; disabling new admissions must not reactivate the evaluator.

**2. Engineering decisions to use**

| Decision | Implementation choice |
| --- | --- |
| Tool | One opt-in `update_lea_status` tool; no independent model request inside its handler |
| Host communication | Typed `LeaStatusUpdateRequested` event and `LeaStatusUpdateAck` reply through the existing `gen.send(...)` driver |
| Storage | Immutable run source context plus append-only `lea_status_updates`; derive the current display from these and live revision/lifecycle data |
| Tool schema ownership | A prover-owned, dependency-light contract module; adapter validation calls the same validator and adds host-specific checks |
| Confidence | `unassessed`, `low`, `medium`, `high`, with required scope and rationale |
| Attention | Deterministically derived from retained findings, independent of confidence and compiler state |
| Run selection | Explicit per-formalization run generation; do not choose current status by latest arrival time |
| Ordering | Status publication and preceding file/check effects are delivered before later serial tools execute |
| Compatibility | Additive capability discovery and versioned run admission; no fallback to terminal evaluation |
| History | Retain both solver updates and separately labeled legacy reports; no verdict-to-confidence conversion |

New names below are proposed files or fields, not claims that they already exist. Change a name if repository conventions require it, while retaining the responsibilities and contracts.

**3. P0 — Shared contract and revision foundations**

**Existing anchors:** `apps/lea-standalone/prover/lea/{events,registry,tools}.py`, adapter `alignment_schemas.py` and `alignment_checks.py`, `formalizations.current_snapshot`, Overleaf `shared/proofSource.mjs` and `shared/leanPaneManifest.mjs`.

- [ ] **P0.1: Add the canonical reporting contract.** Create `apps/lea-standalone/prover/lea/status_reporting.py` for the tool name, JSON input schema, field enums, payload validation, typed publication/acknowledgment values, and defaults. Keep it independent of adapter imports. Use standard-library validation/data structures rather than relying on a transitive Pydantic dependency in the prover. Adapter wire-response models may use its existing Pydantic dependency.
- [ ] **P0.2: Implement merge and finding rules.** Add adapter `lea_status_schemas.py` for host projections and `lea_status.py` for assessment/finding transitions. Required fields and optional-list semantics follow feature-spec section 5. Unknown tool fields, attempted host-identity overrides, invalid enums, malformed references, and oversized payloads produce specific validation errors. An omitted finding is retained; an explicit empty list only clears the appropriate replaceable list. A final update must refresh its closing lists and next action.
- [ ] **P0.3: Preserve finding dispositions.** Validate stable keys, source/Lean resolution states, and resolution/retraction explanations. Merge within a run or an explicitly inherited finding lineage, never with whichever run happened to publish last. Resolving a source issue requires a newly inspected source context; a Lean correction cannot resolve it. Source-resolution edits and severity reductions require an explanation and remain in history. `blocking` findings with unresolved disposition derive `needs_author_input`; material source concerns derive `source_issue`.
- [ ] **P0.4: Extract source/evidence contracts.** Introduce adapter `source_context.py`, moving reusable source-bundle parsing and hashing out of evaluator execution code. Preserve the legacy schema import surface for historical reads until P7. Capture and validate both `sourceIdentityHash` and `bundleHash`; the existing adapter `SourceBundle` model currently has only `bundleHash` and must not silently discard the identity hash for new runs.
- [ ] **P0.5: Extract artifact/dependency references.** Introduce adapter `artifact_snapshots.py` around `formalizations.current_snapshot`, the existing `_snapshot` and transitive-dependency helpers. Support an empty manifest for a source-only assessment. Return immutable references to canonical code steps/git-backed blobs, not duplicate proof contents. Include dependency content hashes and relevant file roles. Refactor the old evaluator to import these helpers temporarily without altering its behavior before cutover.

Use these contract defaults:

| Property | Rule |
| --- | --- |
| `schema_version` | Version 1, bound by the host to each run; reject unsupported versions |
| `summary` | Nonempty, maximum 500 characters |
| `finding_updates` | Maximum 10 changes per tool call; keys unique within the call |
| Total tool payload | Maximum 32 KiB of UTF-8 JSON; do not silently truncate findings |
| Evidence | `latex`, `lean`, or `system` side; bounded excerpt; one-based positive line references with coherent ranges |
| Optional field omission | Preserve previous value; use provided empty lists to clear only replaceable lists |
| Invalid input | No persisted update, sequence allocation, confidence change, or finding deletion |
| Mathematical claims | Model assessments; validation checks structure and consistency, not whether a claimed argument is true |

Source hashes need shared fixtures across Python and JavaScript. The current `proofSource.mjs` deliberately excludes surrounding excerpts and mirror data from `sourceIdentityHash`, but includes them in `bundleHash`. Neither hash is interchangeable with a whole-file LaTeX hash or an arbitrary existing `focus_source_hash`. Do not change this policy incidentally during extraction.

Freshness must separately consider target semantics and assessment evidence. Inserting unrelated prose should preserve target identity; changing a cited context fragment may invalidate assessment evidence. If only a broad mirror revision changed and the relevant evidence cannot be compared, expose unknown/outdated evidence rather than assert that the theorem changed. Retain original line references in history; remap navigation only when the current location is known.

**P0 completion gate:** fixtures cover all confidence values, valid/invalid findings, planned/applied corrections, omitted lists, terminal payloads, both source hashes, empty artifacts, and transitive dependency changes. Importing the new contract does not load the adapter or register a universally available tool.

**4. P1 — Durable source contexts, ordering, and update history**

**Existing anchors:** adapter `store.create_run_bundle`, `db.write`, `routes/runs.py`, `store.delete_project_cascade`, and migrations through `0021_github_import_alignment`.

- [ ] **P1.1: Add an additive migration.** Use the next migration after the actual head when implementation starts; `0022_live_lea_status` is the expected name at the inspected revision. Follow the existing reconciliatory pattern for preexisting physical columns/tables. Do not edit a historical migration or drop `alignment_checks`.
- [ ] **P1.2: Extend run admission atomically.** Add validated `source_bundle` and a reporting-contract request/version field to `RunRequest`, then pass them through `store.create_run_bundle`. Validate source/target/project association before queuing. The run, target association, source context, generation, and user message must commit together, or none may be admitted. Do not first start the prover and attach its source context afterward.
- [ ] **P1.3: Add storage helpers.** Create `lea_status_store.py` for append, get-current-for-run, paginated history, and context retrieval. Reuse `db.write()` for every read-modify-write sequence. Its `BEGIN IMMEDIATE` behavior is required for generation allocation, sequence allocation, deduplication, and finding-state merges.
- [ ] **P1.4: Add explicit cleanup.** Include new rows in project/session/formalization deletion and reset paths that currently own these records. SQLite foreign keys are currently disabled; a schema `REFERENCES` clause or `ON DELETE CASCADE` alone will not remove them. Test both retention during restart and removal during intentional deletion.

Proposed durable schema:

| Record | Fields and constraints |
| --- | --- |
| `lea_status_run_contexts` | `run_id` primary key; formalization/project/session IDs; `run_generation`; `contract_version`; operation; full immutable `source_bundle_json`; recomputed identity/evidence hashes; optional inherited assessment reference; creation time; unique `(formalization_id, run_generation)` |
| `lea_status_updates` | Update ID; bound run/formalization IDs; per-formalization `sequence`; stable `invocation_key`; `kind`; `schema_version`; submitted payload; fully materialized assessment/finding JSON; source-context reference; immutable artifact/dependency manifest JSON and hashes; provenance `solver_tool`; publication time |
| Indexes | Unique `(formalization_id, sequence)`; unique `(run_id, invocation_key)`; indexes for `(run_id, sequence)` and formalization history |

A context is stored once per run so source bundles are not copied for every progress update. Each update stores the materialized resulting assessment as well as the submitted change; reading history must not require replaying unrelated newer runs. Existing run rows own lifecycle, stop reasons, and usage. System notices use existing diagnostics/lifecycle records and never become solver-authored confidence updates.

Assign `run_generation` monotonically within a formalization at admission. Formalize and repair runs own the current assessment from admission; until their first update the prior assessment appears as previous work. For a bound chat continuation, retain the prior assessment during a pure discussion and activate the continuation's status ownership only when it publishes a status or starts proof mutation. Derive that activation from its first durable status update or attributable code step; do not mutate the frozen context or increment generations in response to incoming events. A late update from generation 3 cannot replace a current generation 4 assessment even if its update sequence is larger.

Do not mutate old context rows on resume. Resume is a new run with a new generation, a frozen source bundle, and an explicit inherited assessment/finding reference. On an unchanged source, carry unresolved findings forward. On a changed source, carry prior findings as issues to reassess and require explicit linkage/resolution against that new revision.

**P1 completion gate:** fresh and upgraded databases work; admission rollback leaves no orphan run/context; concurrent appends allocate unique sequences; duplicate invocation acknowledgments return the original update; same invocation with conflicting payload is rejected; cleanup is explicit; legacy report rows and costs are unchanged.

**5. P2 — Immediate tool execution and typed publication**

**Existing anchors:** prover `agent.run_events`, its tool-execution phases and `_meaning_events`, `registry.Tool.opt_in`, `LeaConfig.extra_tools`, `events.py`, `interface.py`, `render.py`, and run-context support.

- [ ] **P2.1: Register an opt-in tool.** Register the schema as a first-class built-in with `opt_in=True`. An admitted supported run adds the name through the existing `extra_tools` mechanism. Add a runtime guard as well: a configured tool name alone cannot publish without host reporting context. Do not mutate the global registry per run or let a user-defined tool shadow this name.
- [ ] **P2.2: Add a typed handshake.** Export `LeaStatusUpdateRequested` and `LeaStatusUpdateAck` through the prover interface. The request contains the validated model payload and actual invocation identity; the adapter attaches target identity. The reply contains accepted/rejected status, durable update ID/sequence, a compact retained-state context, validation notices, and a host control disposition such as continue or pause. This is an internal host exchange, not a per-tool human approval request.
- [ ] **P2.3: Execute publication in serial order.** Special-case the host exchange at the point this tool executes. Yield the request, receive the acknowledgment through the existing generator, form the provider tool result, and only then execute later calls. A missing/malformed acknowledgment is a tool error, not success. Keep status out of `_PARALLEL_SAFE_TOOLS`.
- [ ] **P2.4: Deliver preceding effects immediately for reporting runs.** Refactor result/meaning-event delivery so each serial file write or check in a reporting-enabled run reaches the adapter before the next serial tool. Track emitted results to avoid a second `FileChanged`, `CheckResult`, or `ToolResulted` in the final assembly phase. Keep provider tool-result messages matched to their original invocation order. Preserve existing behavior for consumers without reporting capability unless shared refactoring is necessary and covered by regression tests.
- [ ] **P2.5: Stop remaining execution after a blocking acknowledgment.** Persist the tool result in the transcript, synthesize explicit canceled results for subsequent unexecuted calls so provider history remains valid, and emit a structured recoverable `Finished` outcome. Do not execute a later edit, shell command, check, or delegated operation. Check the existing user/cost stop signal before each subsequent serial tool in reporting-enabled runs as well, so those stops do not wait through the rest of an already generated batch. Do not enter the no-artifact/final-check retry path. Already completed file writes retain their actual events and artifacts.
- [ ] **P2.6: Preserve identity and unsupported-consumer behavior.** Compute invocation identity from the real provider ID plus turn/index, using stable turn/index identity when a provider omits IDs. Never accept it from tool arguments. Ordinary CLI, evaluation, and standalone runs retain their default toolset and event behavior. Defensive unsupported-consumer handling must not turn an absent host acknowledgment into successful publication.

Required execution trace:

```text
write_file(A)
  execute write -> emit FileChanged -> adapter captures code step for A
update_lea_status(U)
  validate -> yield publication request
  adapter persists U against A -> emits SSE -> returns acknowledgment
  prover records successful tool result
lean_check(A)
  starts only after U was accepted and published
```

The existing adapter reads file content when it handles `FileChanged`. If those events remain deferred until after later writes, an update can be bound to the wrong bytes. Moving only the new status event is insufficient: preceding file effects must be captured first.

`ToolCalled` currently announces a requested tool while the provider response is streaming. That announcement is not execution or accepted publication. Do not update confidence from it. Associate execution effects with their actual invocation/path rather than assuming the last announced call is the one now executing.

For shell/custom-tool writes that do not emit `FileChanged`, reconcile changed **registered target files and relevant dependencies** through the existing canonical code-step storage path before accepting an artifact-scoped status. Do not scan and attribute every workspace scratch file to the target. If capture fails or an artifact cannot be associated reliably, return a capture error or accept only explicitly source-scoped reporting; never claim a current artifact snapshot. This is a reuse of proof storage, not another proof store.

**P2 completion gate:** fake streams prove `write A -> status U1 -> write B -> status U2` records distinct correct snapshots; `status -> blocked check` publishes before the check completes; invalid/refused calls produce no publication; a blocking status cancels later tools; no extra meaning events occur; ordinary gate/subagent tests retain their existing behavior.

**6. P3 — Adapter acknowledgment, read APIs, and lifecycle**

**Existing anchors:** `bridge.run_lea`, the manual `gen.send(to_send)` driver, `RunnerContext`, `routes/runs.py`, `runbroker.py`, `formalizations.py`, and `main.py`.

- [ ] **P3.1: Load context into the runner.** Load the immutable run context before configuring tools or prompts. Bind reporting by supported purpose, target, source context, and contract version. Keep `autonomous=True` independent of reporting capability. Never infer this context from assistant text, a label alone, or client-supplied report IDs.
- [ ] **P3.2: Implement publication service.** On `LeaStatusUpdateRequested`, validate host association and invocation identity, capture the current artifact/dependencies, merge with that run's retained state, insert the update, commit, emit `lea_status_updated`, and send the acknowledgment. Catch validation/persistence errors at this seam and return structured tool errors. Do not let a publication failure silently erase the previous status.
- [ ] **P3.3: Bound snapshot races.** Capture a coherent manifest of immutable blob/code-step references and bind it to the run's working revision. If another session has already changed the canonical target, preserve this run's snapshot with noncurrent freshness rather than attribute that other session's bytes to it. Recheck revision tokens around capture when necessary. Keep filesystem/hash work outside long SQLite write transactions; use short serialized transactions for sequence/deduplication/merge.
- [ ] **P3.4: Add current/history routes.** Implement the APIs below in `routes/lea_status.py` and register the router. The service computes confidence, attention, current run selection, lifecycle, freshness, and history pointers independently. Historical records are immutable and retain their provenance.
- [ ] **P3.5: Add structured pause outcomes.** Add `source_obstruction` and `status_reporting_failed` to prover/bridge outcome mapping and the companion's recoverable-stop projection. Map them to existing resumable run behavior; do not require a new stored session proof status. Preserve the actual reason through final bookkeeping rather than overwriting every interrupted outcome as `user_stop`.
- [ ] **P3.6: Handle uncertain publication and repeated failures.** A commit followed by an SSE/ack failure remains an accepted update; retrying the same host invocation returns it. Delivery failure alone should not stop a run when durable publication succeeded. For durable publication failures, allow one bounded host retry for transient storage errors and report the error to Lea. If two consecutive attempts to publish a material finding fail, stop at a safe boundary with a reporting-failure diagnostic; no extra provider request is created by the host.
- [ ] **P3.7: Reconcile restart and terminal paths.** Reuse run recovery and diagnostic infrastructure. Keep accepted updates available even if the in-memory broker was retired. A run that ends without a final tool call has reporting incompleteness, not an invented final summary. A later compiler result changes Lean Check, not the model assessment.

Proposed APIs and event fields:

| Surface | Contract |
| --- | --- |
| `GET /api/formalizations/{id}/lea-status` | `formalization_id`, current `run_id`/generation, `latest_update`, materialized `assessment`, derived `attention`, `activity`, `freshness`, history cursor/count, previous-assessment reference |
| Optional current-source query | `source_identity_hash` and `source_bundle_hash` from the companion's current manifest; comparison inputs only, never mutations of a stored run context |
| `GET /api/formalizations/{id}/lea-status/updates?after=...&limit=...` | Ascending accepted updates after a scoped opaque cursor, with `next_cursor`/`has_more`; default 50, maximum 100; reject malformed or cross-target cursors |
| Existing run SSE: `lea_status_updated` | Formalization ID, run ID/generation, update ID, assessment sequence, contract version, accepted assessment and its revision basis |
| Existing run SSE: `done` | Existing outcome plus structured stop reason/recoverability; no synthetic assessment |

The SSE broker sequence and durable assessment sequence are different. The first orders all events in one live broker; the second orders stored assessments across runs of a target. Do not substitute one cursor for the other.

The adapter cannot know that the author edited unsynchronized Overleaf text. Freshness against the visible source requires current manifest comparison inputs from the companion/extension. Without those, report source currency as unknown/last known rather than assert that a frozen run bundle is today's source. Immutable historical evidence still remains available.

**P3 completion gate:** the first persisted source-only update is readable and streamed during an active run; history pagination is stable; old generations cannot overwrite current status; interrupted runs recover history; errors acknowledge accurately; source obstruction works with no artifact or with a noncompiling partial artifact; final completion/stop races preserve the actual outcome.

**7. P4 — Prompt obligations, cadence, resume, and compaction**

**Existing anchors:** `prompt.OVERLEAF_FAITHFUL_PROMPT`, `agent.run_events`, `_summarize_on_max_turns`, `_forced_tool_narration`, `_classify_final_result`, and `condenser.py`.

- [ ] **P4.1: Add the exact reporting imperative.** Implement feature-spec section 6 in the Overleaf system prompt and tool description. Describe this tool as the only channel that updates Lea Status. Require initial, milestone, confidence-change, finding, correction, stalled-progress, and final updates. Distinguish planned from applied corrections and high scoped confidence from proof completion.
- [ ] **P4.2: Align correction instructions.** Replace conflicting blanket “stop on any incomplete argument” text with the approved repair boundary. Require disclosure before a semantics-preserving repair and a checked follow-up. Missing proofs, ordinary proof gaps, Lean encoding choices, and equivalent library lemmas remain non-blocking for a precise statement. Added assumptions, changed conclusions/domains/quantifiers, materially ambiguous meanings, or abandonment of an explicitly supplied proof's essential approach yield blocking findings. Preserve hard theorem-statement and final-proof guardrails.
- [ ] **P4.3: Add an in-loop cadence tracker.** Track monotonic time since the last accepted publication and substantive non-status tool executions. At the next ordinary turn/tool boundary after approximately 60 seconds or five such tools, append one concise reminder. Suppress repeated reminders until the model gets an opportunity to respond. Invalid calls do not reset the accepted-publication clock. Do not put changing timing text into the cached system-prompt prefix.
- [ ] **P4.4: Retain an explicit assessment context.** Before each model request and after compaction, rebuild a designated context message from the current accepted summary, scope, unresolved findings, planned/applied corrections, and source binding. Replace this message instead of appending copies. Preserve all material open findings; use compact structured text and existing context-budget handling rather than silently truncating warnings. Do not misidentify this host context as the user's latest mathematical request.
- [ ] **P4.5: Seed resume accurately.** Use the admission context's inherited assessment for a new run. Same-source resumes retain finding keys. Changed-source runs receive old findings labeled as prior evidence and must reassess them. A source-obstruction resume must not silently permit the previously rejected change merely because the author clicked Resume.
- [ ] **P4.6: Remove status-only extra requests in this flow.** Set `narrate_tool_steps=False` for reporting-enabled Overleaf runs; their purposeful status calls carry the updates. Skip `_summarize_on_max_turns` for these runs. Check stop/cost/turn conditions before any paid compaction or reporting-related continuation. Keep other consumers' established behavior unchanged.
- [ ] **P4.7: Handle final reporting honestly.** Prompt Lea to perform its final check before its final status update. Do not grant extra budget or start another run to recover a missing final call. If the automatic final check exposes further problems, continue only within the ordinary remaining budget and expect a superseding status. Record missing initial/final reporting separately from mathematical success.

The existing `_classify_final_result` serves `proved`/`disproved`/`needs_review` result routing and is distinct from `alignment_runner`. Preserve conservative proof/counterexample routing; do not use this helper as a Lea Status fallback or add an assessment phase to it. Ensure blocked, interrupted, or exhausted runs cannot enter that paid terminal helper. Any change to successful-run result classification must retain AC16 and be reviewed separately from deleting the alignment evaluator.

**P4 completion gate:** fake-clock/fake-provider tests verify reminder cadence, no repeated reminder spam, preserved context after compaction, correct resume lineage, no Overleaf farewell after a turn cap, no forced narration, and unchanged ordinary interactive prompt/approval behavior. Prompt contracts are necessary checks, not proof of live-model compliance.

**8. P5 — Companion integration and entry-point coverage**

**Existing anchors:** `companion/leaApiClient.mjs`, `server.mjs`, `eventBus.mjs`, `jobStore.mjs`, `shared/checkState.mjs`, `shared/proofSource.mjs`, and `extension/eventsClient.mjs`.

- [ ] **P5.1: Extend the API client.** Thread complete source bundles and reporting version through `startApiRun`/`runApiProofJob` and their callers. Add helpers for current status and history. Consume `lea_status_updated` through the existing `onEvent` path without creating a second run driver. Keep terminal and usage parsing intact.
- [ ] **P5.2: Add a focused companion status module.** Create `companion/leaStatus.mjs` for event validation, current-source comparison inputs, projection reconciliation, and history relay. Avoid spreading status state machines through additional sections of the already large `server.mjs`. Keep target-response `leaStatus` separate from historical `leaCheck` data.
- [ ] **P5.3: Preserve event composition.** `runLeaProofJobForJob` accepts caller event callbacks; compose status handling with existing code/check/cascade consumers rather than replacing them. In `startChatRun`, compose it with the current throttled `chat-updated` notification handler. Persist any companion cache only as a recoverable mirror of adapter data.
- [ ] **P5.4: Publish existing refresh notifications.** After an accepted update changes the projection, publish `jobs-changed` with project, target, formalization, run, and sequence identity. Use `chat-updated` as appropriate for transcript changes. Keep the existing one-connection-per-extension-surface event client; no new EventSource per proof. Coalesce routine refreshes, but do not delay a new blocking/material finding behind a long throttle.
- [ ] **P5.5: Reconcile safely after reconnect.** Fetch current durable state on attachment/reconnect and after a sequence gap. Apply assessment updates only for the selected generation and newer assessment sequence. Fetch-response tokens/current manifest revisions prevent late requests from overwriting source freshness. Do not use assessment sequence alone to suppress a terminal activity update or a LaTeX edit with no new assessment.
- [ ] **P5.6: Update all launchers.** Apply the entry-point table below. Reconstruct source context through existing parser/mirror facilities where possible; do not treat an old linked job's source bundle as the user's current text by default. New supported runs with missing required context return a useful admission error, or remain explicitly general/unassessed as allowed by the specification.
- [ ] **P5.7: Separate activity from freshness.** Remove the `!stale` condition as a blanket reason to stop lifecycle refresh or hide pause controls. A run may still be active against older source. Keep source-stale display, active run identity, and polling decisions as separate properties.
- [ ] **P5.8: Relay history and targeted pause.** Add scoped companion handlers for status history and target interruption. Reuse `interruptApiRun` and existing session/target resolution; the UI should not choose a different target's active run. Use the existing resume/formalize API with a freshly captured source bundle, retained artifact, and explicit assessment lineage.

| Entry point | Existing anchor | Implementation |
| --- | --- | --- |
| Formalize/re-formalize/resume | `handleFormalize`, `runLeaJob`, `runLeaProofJobForJob` | Send current bundle/version; receive status from the active run; capture a new context on resume |
| Definition | `buildLeaDefinitionPrompt` and shared proof-job launcher | Same tool/transport with definition-appropriate scope; remove conflicting interpretation/disclosure instructions |
| Formalize all | `runTargetBatch`, `runFormalizeBatchItem` | Each target gets independent context and status; publish progress without waiting for the entire batch |
| Repair and self-repair | `createRepairJob`, `resolveRepairContext`, `runLeaRepairJob`, `startRepairRun` | Resolve current bundle and preserve the same correction/pause policy; feed disclosures into the same history |
| Overleaf chat continuation | `handleChatMessage`, `startChatRun`, `finishChatRunCascade` | Add supported bound-continuation purpose/context; the present launcher omits `purpose`; reporting applies when doing formalization work |
| Manual Lean save/check | Existing edit handlers and `runLeanCheck` | Preserve compiler checks and invalidate affected assessment freshness; no model assessment |
| Compiler-only cascade | Existing dependency/cascade rebuild path | Invalidate relevant dependency assessments and retain compiler outcomes |
| Stub creation | `runLeaStubJob`/stub batches | Keep stub lifecycle; never label a stub as completed proof; any solver assessment must use partial scope |
| GitHub import | Adapter `_start_import_alignment_checks` | Remove evaluation scheduling in P7; keep source evidence/imported files and show no solver assessment yet |

For bound chat, use a distinct supported purpose such as `overleaf_continuation` and a shared reporting/fidelity prompt block. Keep general discussion behavior: do not force a pure question into a new formalization or clear its previous status. Activate reporting ownership when formalization work actually begins, using a status call or proof mutation rather than a heuristic that scans user prose. For new proof work the reporting imperative still requires a status before substantive changes. Unbound/general LeaChat stays outside this contract.

For **Formalize all**, disable automatic source-obstruction pauses for every item even if the companion setting is on. Retain findings on failed items, skip dependent items, continue independent ones, and settle the batch with a terminal summary. Keep the batch Cancel operation separate.

**P5 completion gate:** fake adapter streams exercise formalization, definitions, repair, chat modification, batch pause, manual edit, and reconnect; a target update reaches `leaStatus` while the run is active; concurrent targets and late old-run events are isolated; a changed LaTeX source does not hide a still-running old-source job.

**9. P6 — Lea Status presentation, history, and author controls**

**Existing anchors:** `extension/content.js` (`renderProjectCheckChip`, `renderLeaCheckReport`, pane actions and batch controls), `leanPaneView.mjs`, `content.css`, `lea-theme.css`, and `shared/checkState.mjs`.

- [ ] **P6.1: Add a pure presentation projection.** Keep `projectLeanCheck` intact. Add `normalizeLeaStatus` and presentation helpers, preferably in focused `shared/leaStatus.mjs` / `extension/leaStatusView.mjs` modules. Map confidence, attention, activity, and freshness independently. Do not reuse old terminal Lea Check verdict enums for live status.
- [ ] **P6.2: Replace the active card/tag.** Show Lea Status, scoped confidence, optional Source issue/Needs author input indicator, latest summary, and update age. Before an update, show a host-generated waiting/no-assessment state. Do not display green “approved” simply because a run finished or Lean compiled.
- [ ] **P6.3: Adapt the detail report.** Render retained matches, findings, planned/applied corrections, obligations, caveats, limitations, next action, and revision details. Keep source issues visible after Lean fixes. Use existing safe text-node rendering; do not inject model-provided HTML. Historical evidence opens its evaluated revision, or clearly identifies when only a quoted historical excerpt is available.
- [ ] **P6.4: Add accessible paginated history.** Show meaningful updates in chronological order, including confidence changes and resolutions. Fetch a bounded first page on expansion and offer load-more navigation. Key entries/findings by stable IDs so updates preserve expanded state, scroll position, focus, and selections. Legacy reports appear in a separate Previous Lea Check report area with original verdict/provenance.
- [ ] **P6.5: Wire Pause/Resume.** Show Pause for the actual active target run, including a stale-source run; show Pausing until the backend settles it. Reuse the canonical resume action after capturing current source. A completed-before-pause race stays completed. Show the original obstruction/cost/time/user-stop reason with its next action. Never initiate status-only paid work from these controls.
- [ ] **P6.6: Separate semantic and operational errors.** Reporting unavailable, disconnected, interrupted, and compiler errors have operational copy. They do not lower mathematical confidence automatically. Outdated assessments stay visible with explicit scope/freshness instead of being erased or displayed as current.
- [ ] **P6.7: Remove active retry controls at cutover.** Remove Retry Lea Check, evaluator-in-progress text, and new-flow `Lea Check` labels. Keep those words only where they identify historical reports or retired compatibility responses.
- [ ] **P6.8: Validate interaction quality.** Use text/icons in addition to color, polite announcements for material changes, keyboard-accessible expansion and history, and unchanged editor focus. A routine update must not reopen a closed panel or collapse an inspected finding.

**P6 completion gate:** DOM tests cover all four confidence values, both attention states, empty/source-only/partial/final/outdated cases, paused/error/offline runs, history updates, and real action requests. Browser scenarios confirm update visibility before completion, stable expansion/scroll/focus, Pause while source is stale, and multiple tabs without multiplying per-target connections.

**10. P7 — Retire evaluator execution and cut over safely**

This package is required even if live reporting already works. Keeping the old terminal hook would leave two competing assessments and continue the extra cost/delay the feature is intended to remove.

- [ ] **P7.1: Advertise capability.** Extend adapter `/api/health` (currently only `{ok: true}`) with a backward-compatible capability object, for example `lea_status: {version: 1, admission_enabled: true, independent_checks: false}`. Reflect compatible capability through companion health/settings state. Verify it before admitting new-flow jobs.
- [ ] **P7.2: Remove all companion scheduling hooks.** Delete `scheduleAlignmentCheckForJob` and calls in manual edit/check handlers, recoverable repair stops, repair completion/no-op paths, `finishChatRunCascade`, and `runLeaJob`. Replace necessary refresh/freshness bookkeeping explicitly; do not delete surrounding compiler/cascade work.
- [ ] **P7.3: Remove adapter import scheduling.** Delete `_start_import_alignment_checks` execution and related imports/calls in `github_import_service.py`. Preserve import target/source evidence, compiler checks, progress, and error reporting unrelated to evaluator execution.
- [ ] **P7.4: Retire mutation routes.** Return HTTP 410 with a stable `lea_check_retired` code from legacy adapter start/retry endpoints and companion `/lean-check/retry`. Retire the unused `overleaf_alignment` run-purpose path. Keep read-only historical routes and helpers free of imports that instantiate an evaluator executor or trigger provider work.
- [ ] **P7.5: Remove evaluator runtime.** Remove or isolate unused execution code in `alignment_runner.py`, the executor/scheduler from `alignment_checks.py`, evaluator retries, and evaluator-specific configuration/UI settings where present. Keep historical parsing/presentation in explicitly read-only modules. Do not remove the provider configuration used by the solver.
- [ ] **P7.6: Preserve accounting and historical recovery.** Keep existing historical `alignment_checks` token/cost rows included once in totals. New status updates add no separate usage rows. On startup, old pending/running checks become interrupted historical records and are never resubmitted; preserve completed reports.
- [ ] **P7.7: Finish documentation and compatibility tests.** Update setup/doctor messages and feature documentation to describe Lea Status. Preserve old docs as implementation history with replacement links. Test new companion/old adapter, old companion/new adapter, missing source context, and unsupported contract versions; none may silently launch a terminal evaluator.

Cutover sequence:

1. Finish the new schema, readers, tool seam, and fake-provider integration while admission is disabled.
2. Stop admitting new evaluator jobs. Let already running evaluations finish under the old version or cancel them explicitly as historical work before replacing processes. Do not migrate an in-flight solver to a different status contract.
3. Upgrade the adapter and migrate its database additively. Historical reads remain available throughout.
4. Upgrade companion and extension, verify capability agreement, and run the deterministic smoke suite.
5. Enable live-status admission for pilot runs. Historical rows cannot populate the new current-assessment field.
6. Enable by default after P8. If a problem occurs, disable new admissions and retain current runs/history; repair forward. Do not roll back by re-enabling independent checks or downgrading away accepted status rows.

**P7 completion gate:** every former trigger is exercised with evaluator/provider-spy failure hooks; zero independent evaluator invocations occur. Legacy start/retry routes produce 410, history still renders, and usage is unchanged for historical data. A status update alone incurs no provider call beyond the solver turn that generated it.

**11. P8 — Validation matrix and release evidence**

Add focused tests next to the existing suites. Suggested new test files are identified as new below; extend existing suites where they already own the behavior rather than duplicating the same scenario in many places.

| Layer | Existing coverage to extend | Suggested additions |
| --- | --- | --- |
| Prover tool/event loop | `tests/agent/test_run_events.py`, `test_gate.py`, `tests/events/test_meaning_emit.py`, registry/scope tests | New `tests/agent/test_lea_status.py`: acknowledgment, immediate effects, no-artifact block, canceled remaining calls, cadence, tool visibility |
| Prover retained context | `tests/condenser/test_condenser.py`, prompt tests, interface facade tests | Reporting-context replacement, same/changed-source resume, final/cap behavior, event exports |
| Adapter persistence | `test_migrations.py`, `test_formalizations.py`, run/concurrency tests | New `test_lea_status_store.py`: atomic admission, deduplication, lineage, sequence races, cleanup, immutable snapshots |
| Adapter bridge/API | `test_bridge.py`, `test_routes_runs.py`, `test_runbroker.py`, `test_concurrent_runs_e2e.py` | New `test_lea_status.py`: blocking downstream tool, durable-before-SSE, current/history, generation selection, source comparison, restart |
| Evaluator retirement | `test_alignment_checks.py`, import tests, usage assertions | Replace start/retry expectations with 410/no-execution tests; retain historical/report/usage coverage |
| Companion | `leaApiClient.test.mjs`, `companion.test.mjs`, `jobStore.test.mjs`, event tests | New `leaStatus.test.mjs`: reordering, generation isolation, capability, reconnect, live projection |
| Overleaf workflows | `leanPaneRepair.test.mjs`, `leanPaneChat.test.mjs`, `leanPaneEdit.test.mjs`, `leanPaneBatch.test.mjs`, `cascadeVerify.test.mjs` | Full source admission, mutation-only status activation, batch source obstruction, no evaluator hooks |
| Extension | `checkState.test.mjs`, `contentActions.test.mjs`, `leanPaneView.test.mjs`, `eventsClient.test.mjs` | Confidence/attention/freshness rendering, history, Pause/Resume, accessible refresh behavior |

Required adversarial ordering tests use controllable fake tools, not timing-sensitive sleeps:

- Hold `lean_check` behind a test latch; assert the preceding update is already in SQLite and the SSE/companion projection before releasing it.
- Make two writes in one provider response with a status between them; assert the earlier manifest references the earlier bytes and exactly one code step is emitted per write.
- Accept a blocking finding followed by a write in the same response; assert the write handler is never called and the canceled result is retained in the transcript.
- Commit an update, fail delivery/ack, then retry its host invocation; assert one update and the same acknowledgment identity.
- Publish a late old-run update after a newer generation starts; assert history gains the old update but the current tag does not change.
- Change source, artifact, and a transitive dependency independently without a new assessment; assert correct freshness and continuing lifecycle visibility.
- Reconnect after the broker buffer has been retired; reconstruct state from durable readers.

Acceptance-criterion traceability:

| Spec criterion | Implementation packages | Required evidence |
| --- | --- | --- |
| AC1: meaningful intermediate reporting | P4–P6, P8 | Fake cadence test plus reviewed long live proof |
| AC2: tag and details change while active | P3, P5, P6 | Active-run API/DOM integration |
| AC3: no delay behind slow tool | P2, P3 | Latch-based ordering test |
| AC4: assessment before artifact | P0–P3 | Source-only publication and no-artifact obstruction |
| AC5: disclosed correction with retained issue | P0, P4, P6 | Finding-transition test plus reviewed correction case |
| AC6: material change reports; opted-in individual run pauses before later mutation | P2–P5 | Blocking acknowledgment with policy on/off and tool-ordering test |
| AC19: default-off setting and Formalize all override | Pause-policy revision | Settings persistence, adapter run snapshot, and batch continuation tests |
| AC7: confidence does not imply checked/approved | P0, P3, P6 | Projection/compiler independence tests |
| AC8: final tool update, no evaluator | P4, P7 | Normal-completion integration with evaluator spies |
| AC9: stops retain state without extra reporting spend | P2–P5, P7 | Stop/cap/crash and provider-call-count tests |
| AC10: revision and old-run correctness | P0, P1, P3, P5 | Hash fixtures and stale-generation race tests |
| AC11: replay/restart without duplicates | P1, P3, P5, P6 | Persistence/reconnect/multi-tab scenarios |
| AC12: validation and failure semantics | P0–P3 | Invalid payload, capture/storage failure, corrected call |
| AC13: resume/compaction preserves findings | P1, P4, P5 | Same-source and changed-source lineage tests |
| AC14: all evaluator entry points retired | P5, P7 | Trigger matrix with zero-execution assertions |
| AC15: historical provenance and usage retained | P1, P6, P7 | Migration fixture plus historical UI/accounting tests |
| AC16: existing proof/LeaChat contracts preserved | P2–P7 | Existing gate, result, counterexample, approval, and frontend regressions |
| AC17: accessible refresh and stale-source Pause | P5, P6 | DOM/action tests plus manual browser review |
| AC18: concurrent targets isolated | P1–P3, P5 | Parallel fake runs and generation/identity checks |

Run commands from the indicated directory after implementation, using the provisioned local environments:

```bash
# Monorepo root: workspace JavaScript tests, frontend typecheck, shared health check.
npm test
npm run typecheck -w apps/lea-standalone
npm run doctor

# apps/lea-standalone/adapter: full backend suite; root npm test does not include it.
./.venv/bin/python -m pytest

# apps/lea-standalone/prover: module-style regression suites and the new status suite.
uv run python -m tests.agent.test_run_events
uv run python -m tests.agent.test_gate
uv run python -m tests.agent.test_lea_status
uv run python -m tests.events.test_meaning_emit
uv run python -m tests.registry.test_registry
uv run python -m tests.registry.test_registry_scope
uv run python -m tests.condenser.test_condenser
uv run python -m tests.interface.test_facade
```

`tests.agent.test_lea_status` is a planned new module and must provide the repository's module-run entry point. Run affected existing prompt and subagent suites as well if their shared contracts change. Use mocked providers for deterministic tests; the test suite must not consume configured provider credits. Do not run resets or clear a user's current proof data for validation.

Manual/live pilot checklist:

- [ ] A long faithful theorem produces a useful first assessment and multiple intermediate updates before completion.
- [ ] A recoverable omitted justification is disclosed before repair and remains visible as a LaTeX issue afterward.
- [ ] A repairable false intermediate step is corrected without changing the theorem or source method, with accurate evidence and limitations.
- [ ] An essential missing hypothesis or changed method pauses without writing a silently modified theorem.
- [ ] Missing/ambiguous proof association is explained before an artifact is required; definitions use the right scope.
- [ ] The author pauses, edits LaTeX, and resumes with a new source context and prior findings to reassess.
- [ ] A forced budget/time stop preserves the last real assessment without an evaluator or farewell request.
- [ ] A transitive dependency change and manual Lean edit invalidate the right assessment while retaining compiler checks.
- [ ] Refresh, temporary disconnect, process restart, and two Overleaf tabs recover the same history and current status.
- [ ] New-flow completion, manual edit, repair, continuation, import, and legacy retry all produce zero separate Lea Check executions.

Record live-case source/artifact revisions, tool/update times, meaningful findings, stop reasons, and provider usage in a small validation record linked from this plan when executed. A mathematically informed reviewer must inspect disclosure timing and fidelity; test counts or successful compilation alone cannot establish them.

Instrument time to first accepted assessment, gap between updates, publish-to-render latency, missing initial/final calls, rejected payloads, persistence failures, stale-event drops, source-obstruction pauses, and attempts to use retired endpoints. Target visibility within two seconds on the healthy local notification path, or one configured active polling interval plus rendering when using fallback. Distinguish model silence from delivery latency. Keep historical evaluator cost and ordinary solver cost distinct without double-counting reporting.

**12. Completion checklist**

- [ ] P0–P8 are implemented and their completion evidence is recorded.
- [ ] All 18 specification acceptance criteria have evidence, including live-model and browser behavior where automated tests are insufficient.
- [ ] No active workflow, manual retry, restart recovery, or import path can launch a separate Lea Check evaluator.
- [ ] The user can receive an assessment before the first artifact, inspect live findings, pause, revise source, and resume without losing provenance.
- [ ] Existing proof verification, human approval, standalone LeaChat, and git/SQLite ownership contracts remain intact.
- [ ] Documentation and capability/version handling identify the new integrated flow, with old reports retained as history.

The original plan is retained for acceptance traceability. Current implementation and test results are recorded in [IMPLEMENTED-overleaf-live-lea-status.md](IMPLEMENTED-overleaf-live-lea-status.md); manual/live release checks remain explicitly pending there.
