# Live Lea Status implementation and validation

Implemented on 2026-09-19 against `ed2d21d`. Runtime changes are in the working tree. This record accompanies the [feature specification](FEATURE-overleaf-live-lea-status.md) and [implementation plan](PLAN-overleaf-live-lea-status.md).

The independent evaluator has been removed. Supported Overleaf formalization and continuation runs receive an opt-in `update_lea_status` tool. A tool call persists an assessment and publishes its event before later tools execute. The extension presents scoped confidence, source concerns, current work, corrections, freshness, history, and Pause separately from Lean Check.

## Implementation map

| Area | Delivered behavior |
| --- | --- |
| Prover contract | `lea/status_reporting.py` owns strict bounded validation, finding transitions, attention derivation, tool schema, and typed request/acknowledgment events. It has no adapter or provider dependency. |
| Run admission | Source bundles and their hashes are validated before `create_run_bundle` commits the run, formalization association, immutable source context, and generation together. Reporting requires contract v1. |
| Persistence | Migration `0022_live_lea_status` adds run contexts and append-only materialized updates. Invocation keys deduplicate uncertain delivery; target sequences and run generations are allocated under the existing SQLite write lock. |
| Proof revisions | Status stores canonical blob/code-step references. Snapshot capture uses this run's writes and its starting revision, excluding later writes from other sessions. Registered shell changes are reconciled through the existing code-step path; transitive project dependency hashes are captured separately. |
| Prover loop | Writes/checks/status execute and emit effects in order. A blocking acknowledgment cancels the rest of the tool batch. Reporting runs do not delegate mutations to child agents. Ordinary standalone tool execution and approval behavior remain unchanged. |
| Prompt and context | The Overleaf prompt requires an initial assessment, useful milestones, disclosure before corrections, and a final tool update. A reminder follows about 60 seconds or five tools without an accepted update. The latest accepted findings and frozen source context are reinserted after compaction. An author-authorized best-effort continuation permits disclosed conventional inferred context for one resumed run without weakening ordinary faithful runs. |
| Stop behavior | Required semantic changes—added assumptions, changed conclusions/domains/quantifiers, materially ambiguous meanings, or abandonment of an explicitly supplied proof's essential approach—produce blocking findings. Under the 2026-09-23 revision, only an opted-in individual run converts such a finding to `source_obstruction`. Missing proofs and ordinary meaning-preserving proof/Lean choices remain non-blocking. A material update that cannot persist after one host retry yields `status_reporting_failed`. Stop reasons are recoverable. Turn/cost/user stops retain the last update without an extra farewell/model request. |
| Adapter API | Health advertises capability/version/admission state. `/api/formalizations/{id}/lea-status` and `/lea-status/updates` reconstruct current state and paginated history without the live broker. Activity, attention, freshness, and compiler results are separate. |
| Companion | Formalization, resume, repair, and source-bound continuation carry source evidence and consume existing run events. Existing project refresh notifications deliver status changes. Settings expose adapter capability. History and Pause use the existing companion connection. |
| Extension | Lea Status replaces the active Lea Check tag. Details show corrections and retained source issues, with paginated update history and separately labeled old reports. Pause remains available for active work against stale source. A proofless `source_obstruction` pause offers **Continue best effort** as its primary action and keeps **Resume faithfully** as an alternative; the companion revalidates eligibility. Details nodes are reused; refresh preserves focus/scroll and defers while status text is selected. Material attention changes receive polite announcements. |
| Retirement | The evaluator runner and all completion/edit/repair/import scheduling hooks are removed. Legacy start/retry endpoints return `410 lea_check_retired`; historical reports and their original accounting remain readable. No fallback evaluator exists. |

Names were consolidated where appropriate: the shared prover contract owns finding validation/merge semantics rather than adding a second adapter schema implementation. The existing repository stores proof content in canonical content-addressed blobs; status references those APIs and does not introduce another proof-content store.

Stub generation retains its existing general-run behavior; it does not claim faithful proof completion or schedule a semantic evaluator. When older target payloads include statement text but no separate proof bundle, the companion freezes that supplied statement with an explicit missing proof association. Lea reports the missing method as non-blocking and, when the statement is precise, attempts a standard meaning-preserving proof without pretending that it came from the source. It reports a blocking finding if the statement itself requires a semantic change or is materially ambiguous; the run's pause policy controls stopping. A bound continuation without usable source is rejected with a refresh instruction. Repairs with an existing source bundle assess that frozen revision; current source/evidence hashes determine whether that assessment can be presented as current.

## 2026-09-23 pause-policy revision

The Overleaf settings pane and extension options now offer **Pause formalization when Lea needs author input**, initially off. The companion saves this preference and snapshots it onto each individual formalization, repair, or source-bound chat continuation. Migration `0023_source_pause_policy` stores the immutable per-run decision in the adapter; Lea Status findings still publish when stopping is disabled. **Formalize all** forces the decision off for each batch item, skips dependents of failed items, and continues independent items. Manual Pause remains available to the author.

Automated revision checks passed: 764 adapter tests excluding the five URL-validation tests that require public DNS, 535 Overleaf tests excluding the SSE listener test that requires a local socket, 60 standalone frontend tests, the prompt-policy module, and the prover condenser and subagent regression modules. Focused migration and pause-policy tests passed. Frontend typecheck, JavaScript syntax checks, and `git diff --check` passed. The excluded tests were blocked by the current sandbox's network restrictions.

On resume, retained findings are reassessed under this boundary. A legacy finding that was blocking only because the source proof was absent is updated under the same stable key to a warning/caveat with an explanation when the statement is precise; it is not merely omitted, because omitted findings intentionally remain active.

## Automated validation

Providers and compiler outcomes are mocked for the new integration tests. No paid proof run or separate evaluator was used.

| Check | Result |
| --- | --- |
| Full adapter suite | 765 passed at the full-suite checkpoint. Subsequent additions and revision-capture changes passed the focused suites below. |
| Final adapter status tests | 14 passed: durable replay, terminal replay, invalid payloads, concurrent sequence allocation, pagination, new-run selection, source/artifact/dependency freshness, atomic rollback, retained findings, continuation activation, commit-before-next-effect, persistence/delivery failures, competing sessions, and intentional cleanup. |
| Adapter bridge/formalization/status regression | 81 passed after adding run-specific revision capture; the final bridge/status run passed 75 tests after the last failure-path changes. |
| Full JavaScript suite | 507 Overleaf tests and 60 standalone frontend tests passed. |
| Prover module regression | Agent run-events, approval gates, meaning events, registry, registry scope, condenser, and interface facade suites passed. |
| Prover status tests | 5 passed: publication before later tools/intermediate bytes; cancellation after a blocking acknowledgment; absent-host errors; rejection defers a queued correction; cadence and retained context through compaction. |
| Frontend typecheck | Passed. |
| Repository doctor | Passed; local adapter/UI processes were not running. |
| Patch whitespace/syntax checks | Passed. |

The full suites needed ordinary test-environment permissions: the JavaScript SSE test listens on a local port, and existing adapter URL validation tests resolve public DNS names. They passed with those permissions. These permissions did not enable paid model calls.

The UI action regression checks live confidence, stale-source labeling, retained Lean corrections, history loading, and a Pause request targeting the active run. It runs the actual extension modules in the existing DOM harness.

## Activation and rollout

Restart the updated adapter so its normal startup applies the additive migration. Restart the companion and reload the unpacked Chrome extension so the new web-accessible modules are available together. A new companion refuses reporting runs against an older/incompatible adapter.

`LEA_LIVE_STATUS_ENABLED=1` enables new admissions (the implementation default). Set it to `0` to stop new reporting admissions during a staged rollout or incident. Runs already admitted retain their reporting contract and can continue to publish. Disabling admission does not restart the old evaluator. Do not downgrade away accepted status tables.

The read-only historical routes remain available. Existing pending evaluator records are recovered as interrupted historical work and are never resubmitted. No user's proof data was reset or deleted during validation.

## Remaining release validation

The implementation and deterministic checks are complete; the plan's manual browser and live-proof pilot is still a release step. No real Overleaf browser session or paid long-running proof was exercised here. In particular, mathematical fidelity, usefulness of updates on a long proof, visual/keyboard behavior in actual Chrome, and end-to-end publication latency need the pilot cases in P8. Test counts cannot establish those model-behavior and browser-quality properties.

The durable rows and logs provide source/artifact identities, update times, sequence/generation, first-update gaps, publication rejections, and incomplete reporting. Use those records to evaluate disclosure timing and delivery latency during the pilot. Automatic cadence is a reminder at a model boundary, not a timer that can interrupt a single blocking Lean/tool call.
