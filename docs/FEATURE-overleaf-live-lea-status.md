# Feature: Live Lea Status During Overleaf Formalization

**Status:** Runtime implemented; automated checks passed. Manual browser/live-proof release validation remains. See the [implementation record](IMPLEMENTED-overleaf-live-lea-status.md).

**Date:** 2026-09-19

**Primary surface:** Overleaf extension, including the Lean pane and project item cards

**Affected systems:** Extension, companion, shared FastAPI adapter, vendored prover

**Predecessor:** [Faithful Overleaf Formalization and Dual Lean/Lea Checks](FEATURE-overleaf-faithful-formalization-and-lea-check.md)

**Implementation plan:** [Live Lea Status implementation plan](PLAN-overleaf-live-lea-status.md)

Lea will communicate its assessment of a formalization while it is working, through a dedicated tool that updates the user's **Lea Status**. The tool updates both the confidence tag and the substantive explanation behind it. The user can follow progress, see source-proof issues and disclosed corrections, and decide whether to let Lea continue or pause to edit the LaTeX.

This replaces the separate Lea Check evaluator. The formalizing agent becomes responsible for maintaining the assessment throughout the same run. A completed run leaves a final status summary assembled from that flow; it does not launch another model evaluation.

**Confirmed product decisions**

- Remove the separate evaluator entirely, including an optional manual independent-review action.
- Lea may continue with explicitly disclosed corrections that preserve the theorem and the source proof's mathematical approach.
- A missing source proof, ordinary proof gap, Lean encoding choice, or equivalent library-lemma substitution is non-blocking when the statement is precise and the claim and any explicitly supplied proof method remain intact.
- Lea reports a blocking finding when continuing would require a semantic change—such as adding an assumption, changing the conclusion/domain/quantifiers, choosing between materially different meanings, or abandoning an explicitly supplied proof's essential approach. Automatic source-obstruction pauses are off by default and require the author's settings opt-in. **Formalize all** suppresses those pauses for every run it starts, regardless of the setting.
- When a proofless target nevertheless pauses for `source_obstruction`, the Lean pane offers **Continue best effort**. This is an explicit, one-run author override that permits conventional inferred context and assumptions with full disclosure; **Resume faithfully** remains available as the conservative alternative.

Other concrete choices below—tool naming, confidence labels, update cadence, and API names—are proposed implementation defaults. They do not require further product decisions to begin implementation.

**1. Product contract**

For every supported Overleaf formalization run, the user must be able to answer:

1. What has Lea established or successfully translated so far?
2. How confident is Lea that the current translation faithfully represents the supplied mathematics?
3. What remains uncertain, incomplete, or difficult?
4. Has Lea found an issue in the LaTeX proof, and what has it done or proposed to do about it?
5. Should the author continue waiting, inspect a finding, or pause and revise the source?

The answer must evolve during the run. A spinner, raw tool log, final-only report, or periodically rerun evaluator does not satisfy this feature.

Two distinct signals remain visible:

| Surface | Meaning | Authority |
| --- | --- | --- |
| Lean Check | Existing compiler and formalization-run state | Existing Lean checks, verification, and run lifecycle |
| Lea Status | Lea's current confidence, progress, findings, and guidance about the source-to-Lean translation | The formalizing agent's structured status updates |

Lea Status is the solver's own assessment. It must not be presented as independent verification, a calibrated probability, proof completion percentage, or an additional kernel check. High confidence can describe faithful partial work; it does not mean the proof is complete. Lean completion alone never raises Lea's confidence.

**2. Existing behavior and reusable infrastructure**

The implementation currently has the following relevant seams:

| Area | Current behavior | Change required |
| --- | --- | --- |
| `prover/lea/prompt.py` | Dedicated `overleaf_faithful` system prompt requires fidelity and disclosure | Require live tool-mediated reporting and clarify the correction/pause policy |
| `prover/lea/tools.py`, `registry.py`, `agent.py` | Registered tools and a typed event stream consumed in-process | Add a scoped reporting tool and immediate publication with a persistence acknowledgment |
| `adapter/app/bridge.py` | Selects the Overleaf prompt by run purpose; persists proof events and emits SSE | Validate, persist, and stream status updates for the bound target |
| `adapter/app/alignment_schemas.py` | Structured matches, findings, evidence, caveats, confidence, obligations, limitations, and revision references | Reuse useful structures without the evaluator's terminal-verdict contract |
| `adapter/app/alignment_checks.py` | Captures artifacts/dependencies and schedules a separate evaluator | Extract reusable revision logic; retire evaluation scheduling |
| `companion/server.mjs` | `scheduleAlignmentCheckForJob` runs after formalization and other mutation paths | Replace scheduling with live status consumption and freshness invalidation |
| `companion/leaApiClient.mjs` | Consumes the adapter run SSE stream, with an `onEvent` callback | Consume the new status event on the existing stream |
| `shared/checkState.mjs` | Normalizes separate Lean and Lea check projections | Introduce the Lea Status projection |
| `extension/content.js` | Shows Lea Check tags and structured reports | Rename and adapt these surfaces for live status and update history |

Paths beginning with `prover/` or `adapter/` above are under `apps/lea-standalone/`; companion, shared, and extension paths are under `apps/overleaf-extension/`.

The adapter remains the only backend on port 8001 and imports the prover in-process. The companion remains on port 31245. Use the existing run event stream, companion refresh notifications, and pane refresh/polling infrastructure. No new service or per-target browser SSE connection is needed.

Git continues to own proof content. SQLite owns status metadata, assessments, and history. Existing session proof status remains derived from code-step verdicts; the new feature must not add a competing stored session status.

**3. User experience**

The existing Lea Check surface becomes **Lea Status** everywhere it describes the new flow: pane cards, project summaries, accessible labels, tooltips, and detail views. Existing Lean Check and human-approval controls retain their meanings.

Each target shows:

- A confidence label: **Not assessed**, **Low confidence**, **Medium confidence**, or **High confidence**.
- An attention indicator when applicable: **Source issue** or **Needs author input**.
- A one- or two-sentence latest summary, visible while formalization is running.
- The age of the last update and relevant activity/freshness information.
- An expandable detail view containing findings and update history.

For example:

> **Lean Check: in progress**
>
> **Lea Status: Medium confidence · Source issue**
>
> The main construction follows your proof. The injectivity argument omits a justification; I am filling it in using the existing assumptions. The final case is still unfinished.
>
> Updated 12 seconds ago · View details · Pause

The initial host-generated display may say “Starting formalization; waiting for Lea's first assessment.” This is operational information, not a model assessment. The reporting tool must work before a Lean file exists, allowing Lea to report an ambiguous source proof or an early concern immediately.

The detail view retains the useful content of today's report:

- What has translated successfully, with source/Lean correspondence where available.
- Current work and remaining proof obligations.
- Source issues, ambiguities, mismatches, and dependency concerns.
- Corrections planned or applied in Lean, including why they preserve the claim and approach.
- Caveats, scope, limitations, confidence rationale, and recommended next action.
- Exact source and artifact revisions, available through an unobtrusive details control.
- A chronological history of meaningful updates, including prior confidence changes.

The latest view is concise. History may be paginated; it must remain durable and inspectable after refresh or restart. A routine progress update cannot erase a previously reported mathematical issue.

A new material issue is indicated immediately without opening a modal, stealing keyboard focus, or moving the editor scroll position. Use text and icons as well as color. Screen-reader announcements should summarize material changes without reading every progress update. Expanded findings, selection, and scroll position should survive background refreshes.

**4. Confidence and attention semantics**

The confidence field means **confidence in the faithful correspondence of the assessed work to the supplied source, within the stated scope**. It does not mean the likelihood that more compute will eventually solve the theorem.

| Confidence | Intended use |
| --- | --- |
| Not assessed | No accepted assessment for this run/source context yet, or Lea explicitly lacks enough evidence to assess |
| Low | Significant uncertainty about the translation, interpretation, or support for key proof steps |
| Medium | The translation looks plausible, with material obligations or uncertainties still being investigated |
| High | Strong supporting evidence for faithful correspondence of the assessed portion; disclose any remaining scope limits |

Confidence may rise or fall. The summary must explain a material change. Incomplete work does not automatically imply low confidence; source ambiguity or a material mismatch does not become high confidence merely because the Lean artifact compiles.

Attention is separate from confidence:

| Attention | Intended use |
| --- | --- |
| None | No unresolved material source concern in the current assessment |
| Source issue | A source gap, correction, ambiguity, or other material concern the author should inspect |
| Needs author input | Faithful continuation is blocked, or proceeding would change the claim or approach |

The server derives attention from retained findings and their dispositions. It must not accept a clear attention indicator alongside an unresolved blocking finding. A corrected Lean argument may have high confidence and still show **Source issue** because the LaTeX needs a corresponding correction.

Run activity, confidence, attention, and freshness are separate properties. Pausing does not change confidence to “paused.” An infrastructure failure does not become “low mathematical confidence.” Ending the run does not turn the tag into “approved.”

For definitions or targets without an applicable proof, state the actual scope. A definition can be assessed for meaning and dependencies without inventing a proof-strategy judgment. A theorem with missing or ambiguously associated proof text must disclose that limitation; when its statement is precise, Lea may choose and clearly label a standard meaning-preserving proof strategy. That strategy must not be presented as author-supplied evidence.

**5. Reporting tool**

Add one tool, provisionally named `update_lea_status`, to supported Overleaf solver runs. One tool handles early assessment, ordinary progress, findings, confidence changes, and the final summary. It does not call a second model.

The tool is available only when the adapter has established a supported Overleaf purpose, a formalization target, and a frozen source bundle. It is not exposed indiscriminately to all autonomous runs or to ordinary LeaChat. Tool arguments cannot select another project, run, or formalization.

The tool performs a structured status publication. It does not edit a report file, change a Lean verdict, approve a proof, modify the LaTeX, or authorize a changed theorem statement. A blocking finding can cause the host to pause through the existing run-stop mechanism, as specified below.

Proposed input contract:

| Field | Required | Meaning |
| --- | --- | --- |
| `kind` | Yes | `initial`, `progress`, `finding`, or `final` |
| `confidence` | Yes | `unassessed`, `low`, `medium`, or `high` |
| `summary` | Yes | Concise current assessment suitable for the card |
| `scope` | Yes | `source_only`, `statement_only`, `partial_artifact`, or `statement_and_proof` |
| `confidence_reason` | Yes | Brief evidence-based rationale, particularly when confidence changes |
| `current_work` | No | What Lea is currently attempting |
| `matches` | No | Successful source-to-Lean correspondences; reuse evidence-reference structures |
| `finding_updates` | No | Create or revise findings by stable key; omission never deletes existing findings |
| `caveats` | No | Non-material qualifications |
| `remaining_obligations` | No | Remaining work or unresolved mathematical steps |
| `limitations` | No | Missing evidence or assessment limits |
| `recommended_next_action` | No | Structured action kind and human-readable explanation |

Optional lists other than `finding_updates` replace their current-view list when supplied; omission retains the prior value. An explicit empty list clears only that list. Every accepted update records the submitted change and the resulting full assessment, so historical rendering does not depend on mutable state. A final update must refresh scope, matches, caveats, obligations, limitations, and next action; it cannot leave obsolete progress text as the closing assessment.

`recommended_next_action.kind` is one of `continue`, `review_latex`, `pause_and_edit_latex`, or `inspect_result`. It is guidance for the user. Run completion is still established by the run driver and existing proof checks.

Each finding contains:

| Field | Meaning |
| --- | --- |
| `key` | Stable finding key within the assessment lineage |
| `severity` | `info`, `caveat`, `warning`, or `blocking` |
| `category` | Reuse statement, proof method, assumption, case split, witness, dependency, source gap, formalization choice, and other categories |
| `title`, `detail` | The issue and its mathematical significance |
| `evidence` | Source/Lean locations and bounded excerpts, using the existing evidence-reference shape |
| `lean_resolution` | `not_applicable`, `open`, `planned`, or `applied` |
| `source_resolution` | `not_applicable`, `open`, `resolved`, or `retracted` |
| `correction` | If relevant: proposed/applied change and why the theorem and approach are preserved |
| `resolution_explanation` | Required when resolving or retracting a finding |

Resolve source issues only after inspecting a source revision that addresses them. Retract a false alarm only with an explanation. Applying a Lean correction alone does not resolve a source issue. Stable keys survive resume on the same source; a new source revision can explicitly link a finding to its predecessor while reassessing it.

Example tool call, with illustrative evidence locations:

```json
{
  "kind": "finding",
  "confidence": "medium",
  "summary": "The construction follows your proof. An omitted injectivity justification needs to be filled in before I can complete the final step.",
  "scope": "partial_artifact",
  "confidence_reason": "The construction and intermediate statements correspond to the source; the final implication is still being checked.",
  "current_work": "Deriving the missing injectivity step from the existing assumptions.",
  "finding_updates": [
    {
      "key": "injectivity-justification",
      "severity": "warning",
      "category": "source_gap",
      "title": "Injectivity is asserted without justification",
      "detail": "The source uses injectivity in its final implication but does not justify it. The existing hypotheses appear sufficient to supply that argument.",
      "evidence": [
        {
          "side": "latex",
          "path": "main.tex",
          "start_line": 84,
          "end_line": 85,
          "excerpt": "The map is injective, so the claim follows."
        }
      ],
      "lean_resolution": "planned",
      "source_resolution": "open",
      "correction": "Add the missing intermediate argument using the current hypotheses, preserving the construction and final implication."
    }
  ],
  "remaining_obligations": ["Check the injectivity argument", "Complete the final implication"],
  "recommended_next_action": {
    "kind": "review_latex",
    "detail": "You may let formalization continue; the LaTeX proof should also include this justification."
  }
}
```

The adapter supplies trusted identity and revision metadata: project, formalization, session, run, source hashes, artifact manifest, dependency fingerprint, publication time, sequence, and tool-call identity. The model does not supply authoritative values for those fields.

The tool returns a structured acknowledgment only after successful durable persistence, including the update ID, sequence, freshness, and any validation notices. A rejected call returns a useful error and leaves the prior assessment intact. A transport replay must not create another update. Proposed payload bounds are a 500-character summary, at most 10 finding changes per call, and 32 KiB total; extensive evidence belongs in referenced files.

**6. Required reporting behavior and system prompt**

Add this obligation to `OVERLEAF_FAITHFUL_PROMPT`, with corresponding tool documentation:

> Keep the author informed throughout formalization by calling `update_lea_status`. This tool is the only way to update the author's Lea Status tag and status details in Overleaf. Ordinary assistant text, tool logs, and a final response do not update that surface.
>
> Publish an initial assessment after reading the supplied statement and proof, before substantial formalization work. Update the author at meaningful milestones, when confidence changes, when you find an issue or ambiguity, when you plan or apply a mathematical correction, and when progress stalls. Explain what is going well, what remains uncertain, and what the author can do next. Do not wait until the end to disclose a finding.
>
> Preserve the statement and any explicitly supplied mathematical approach. You may fill gaps or correct intermediate reasoning when the existing assumptions and approach support the correction. Disclose the issue and proposed correction before applying it, then update the finding after checking it. A missing proof alone is non-blocking: when the statement is precise, choose a standard meaning-preserving proof and continue. Ordinary proof gaps, Lean encoding choices, and equivalent library lemmas are also non-blocking. If continuation requires a changed claim, an additional assumption, a changed conclusion/domain/quantifier, a choice between materially different meanings, or abandoning an explicitly supplied proof's essential approach, publish a blocking finding. The host-provided run policy decides whether that publication pauses work; otherwise continue faithful work without silently changing the claim.
>
> Before normal completion, publish a final status summarizing the result, corrections, remaining source issues, caveats, and limitations. High confidence is an assessment of the stated scope; it is not a claim that unfinished work is complete. Never describe a planned correction as applied, or a conjectured counterexample as verified.

The “only way” statement refers specifically to Lea Status. Existing assistant transcripts may remain available through chat/session views. They are not parsed as a fallback status channel.

Update companion-generated theorem, definition, repair, and supported Overleaf continuation prompts wherever they currently instruct Lea to disclose issues only “in your response.” Resolve conflicts with blanket instructions to stop at every incomplete argument: supported, disclosed repairs may continue under the confirmed policy. Instructions to silently choose an interpretation must not override a material-ambiguity finding.

Proposed cadence:

| Trigger | Required behavior |
| --- | --- |
| Source read; work about to begin | Initial assessment, even without an artifact |
| Meaningful milestone | Explain completed correspondence and next obligation |
| Material issue, confidence change, or correction | Publish promptly; disclose a correction before applying it |
| Sustained work without a milestone | Give a useful progress update at the next model/tool boundary after approximately 60 seconds or five substantive non-status tool executions, whichever comes first |
| Normal completion or voluntary stop | Final assessment before ending |
| Forced stop, timeout, crash, or exhausted budget | Host preserves the last assessment and reports the actual stop reason; no invented final assessment |

Use a lightweight reminder in the existing model loop when the cadence is overdue. Do not run a timer-driven evaluator or make a separate provider request just to manufacture a heartbeat. Record missing updates as an observable defect.

For normal completion, instruct Lea to check its final edits before publishing the final assessment. If the existing automatic final-check path subsequently discovers a failure or work changes, preserve the real compiler outcome and the assessment's original scope; do not silently rewrite the final assessment. When work continues within the existing budget, Lea should publish a superseding update. A missing final tool call is visible reporting incompleteness, not permission to start a second assessment run.

The current prover also has a tool-less summary request on turn-budget exhaustion and optional forced-narration requests. For the supported Overleaf flow, skip the post-budget summary and do not use forced narration to satisfy status reporting. Keep the requested updates inside ordinary solver turns; leave other consumers' summary behavior unchanged.

Long provider reasoning or a single blocking compile may prevent the model from producing a new assessment at an exact wall-clock interval. Keep the last assessment timestamp and real run activity visible during that interval. Distinguish model silence from broken delivery; do not fabricate mathematical progress.

**7. Correction policy and author control**

The decision boundary is whether the correction is supported by the existing statement, hypotheses, and mathematical route—not whether Lea can find some other proof.

| Situation | Behavior |
| --- | --- |
| Lean notation, coercion, tactic, or library integration problem | Continue; mention it when useful to explain progress |
| Precise statement with no associated natural-language proof | Disclose the missing method as a warning/caveat; choose a standard meaning-preserving proof and continue |
| Lean representation choice or substitution of an equivalent library lemma | Continue when the alternatives preserve the same mathematical claim and any explicit source method |
| Omitted justification derivable from the stated hypotheses | Disclose the gap and proposed repair, continue, then report what was checked |
| Incorrect intermediate step that can be repaired within the same argument | Disclose before repair; preserve the claim and approach; keep the source issue visible |
| Essential unstated hypothesis, false target claim, materially ambiguous meaning, or abandonment of an explicitly supplied method required | Publish a blocking finding; pause only for an opted-in individual run, otherwise retain the finding and finish as faithfully as possible |
| Proofless `source_obstruction` after the author selects **Continue best effort** | Infer a conventional coherent context and proof strategy, disclose every inference as a formalization choice, downgrade covered blockers to non-blocking warnings, and continue; pause only when no defensible interpretation exists or the interpreted claim is false |
| Uncertainty about whether a change preserves the claim or approach | Explain the uncertainty; investigate without silently making the material change; if unresolved, publish a blocking finding and obey the run pause policy |
| Verified counterexample | Preserve the existing disproof workflow and evidence; report its consequence for the source claim |

A blocking status is persisted and published before the host requests a cooperative pause when the run has opted in. Only then does the acknowledgment stop later proof-mutating tools in the same model response. Otherwise Lea may continue meaning-preserving work while the finding stays visible. Snapshot the policy at run admission so a settings change does not alter an active run. Use a structured stop reason such as `source_obstruction`; do not detect this by scanning prose. A statement that the theorem is false remains a model finding unless supported by the existing verified-counterexample machinery.

The source-obstruction path must also work before a proof file exists or when the partial artifact does not compile. It bypasses the ordinary loop's encouragement to keep producing/checking an artifact, while preserving compiler verification requirements for any claimed completed proof. The status tool cannot turn an incomplete artifact into a successful run.

Expose **Pause** beside an active formalization and **Resume** for recoverable stopped work, using the existing interrupt/resume infrastructure. Pause means stop ongoing work while retaining artifacts and status history; it is not an operating-system suspension that promises to preserve an in-flight model request. Show “Pausing…” until the driver confirms the stop. Do not spend more tokens for a farewell or launch any evaluation after the user's pause.

For a paused item whose structured stop reason is `source_obstruction` and whose current source bundle has no associated proof, make **Continue best effort** the primary Lean-pane action and retain **Resume faithfully** in the overflow menu. The companion must validate the same conditions server-side. The override applies only to that resumed run, remains visible in its prompt/job record, and does not permit choosing between competing explicit proofs or misrepresenting inferred material as source text.

If the proof completed before the pause request won the race, keep the completed outcome. **Formalize all** starts each item with automatic source pauses disabled. A failed item causes its dependents to be skipped, while independent items continue and the batch reaches a terminal summary. Unrelated user-started runs retain their own snapshotted setting.

After editing LaTeX, starting again creates a new run against a newly captured source bundle. Existing Lean work and prior findings can be supplied as context, but Lea must reassess them against the changed source. Never resume an old mathematical contract merely by relabeling its source hash.

**8. Immediate event delivery and storage**

The intended path is:

```text
Lea calls update_lea_status
  -> prover validates the tool payload and yields a typed publication request
  -> adapter binds the target and revision, persists the update, and acknowledges it
  -> adapter publishes lea_status_updated on the existing run SSE stream
  -> companion updates its projection and publishes a refresh notification
  -> extension renders the latest Lea Status and detail view
```

The event name is provisional; the typed contract and timing are requirements. The prover must not import adapter storage modules. Use the existing in-process generator seam, with a host acknowledgment or equivalent scoped callback, to keep the boundary explicit.

**Immediate delivery is essential.** `agent.py` currently executes a batch of tools and emits downstream result/meaning events in a later phase. Simply adding a status meaning event to that deferred phase could hide an update behind a slow `lean_check` or another long-running tool in the same response. Publication and acknowledgment must happen when the status tool executes, before any subsequent expensive tool.

Preserve tool execution order. Prior file changes must be captured through the canonical proof-storage path before a status snapshot claims to describe them. Later writes must not be attributed to an earlier update. Do not assign artifact hashes retrospectively from the end of the batch. Test the sequences `write -> status -> slow check` and `status -> write -> status` explicitly.

Store append-only assessment updates in a dedicated metadata table, provisionally `lea_status_updates`, rather than repeatedly overwriting an evaluator result. Reuse existing revision-reference utilities and evidence structures where appropriate.

Each persisted record needs:

- An update ID and monotonically increasing sequence within the formalization.
- Bound project/formalization/session/run identity and a stable tool-invocation identity.
- The frozen source bundle or its durable reference, including semantic identity and evidence-location metadata.
- A source/artifact/dependency revision reference; artifact references may be empty for source-only assessments.
- The validated payload and materialized assessment/finding state.
- Publication time, update kind, schema version, and provenance (`solver_tool`).

Host lifecycle notices have separate provenance (`system`) and cannot overwrite model confidence or fabricate findings. Reuse existing lifecycle storage where possible. A cached current projection is permitted only if rebuildable from durable updates and current run/revision data.

Enforce idempotency by run plus stable tool-invocation identity, including an invocation index when a provider omits call IDs. A retried persistence acknowledgment returns the original record. Client replay ignores already-applied or older sequences. An older run can add to its own history, but cannot replace a newer active run's current assessment.

Existing proof bytes remain in git-backed storage. Status records store references and bounded evidence excerpts, not duplicate proof files. Extract hashing/snapshot helpers from evaluator orchestration so retiring the evaluator does not remove freshness support.

**9. API and refresh behavior**

Proposed additive read APIs:

| API | Purpose |
| --- | --- |
| `GET /api/formalizations/{id}/lea-status` | Current projection, lifecycle, freshness, last update, and history cursor |
| `GET /api/formalizations/{id}/lea-status/updates?after=...&limit=...` | Paginated update history with unambiguous ordering |
| Existing run SSE, event `lea_status_updated` | Live accepted update with formalization ID, run ID, sequence, and projection |

Publication is an internal prover-to-adapter action, not an unauthenticated browser endpoint allowing callers to overwrite confidence. Read routes use the adapter's existing access and project/formalization scoping.

Capture the complete version-2 source bundle at run admission and retain it for the run. The existing `focus_source_hash` alone is insufficient for pre-artifact assessments, evidence locations, and restart recovery. Add a validated source-bundle reference or request field through the companion/client/run-creation path; recompute and validate hashes at the adapter boundary.

The companion exposes `leaStatus` in target responses and consumes live events through `streamApiRun` and its `onEvent` callback. Its persisted jobs may cache a projection for offline display; SQLite remains authoritative. On attachment, browser refresh, or reconnection, fetch the latest durable projection and reconcile by sequence before applying subsequent live updates. An in-memory SSE replay buffer is not the only history source.

Do not couple polling exclusively to confidence or the old evaluator's `in-progress` status. Refresh while a target's solver is active, while a pause is settling, or when a refresh event arrives. Source edits can make the artifact stale while the old run is still active; that must not suppress lifecycle updates or hide the Pause control.

Proposed delivery target: on a healthy local stack, a persisted update becomes visible within two seconds through the notification path, or within one configured active-pane polling interval plus rendering time when polling is the fallback. Measure this separately from model time-to-first-assessment.

**10. Freshness, edits, and run boundaries**

Every assessment describes a particular source and work snapshot. Displaying an assessment in a live run does not make it a timeless property of the theorem.

| Event | Required projection |
| --- | --- |
| Solver makes another Lean edit after an update | Retain the update as “last assessed version”; indicate newer Lean work exists until Lea assesses it |
| Source statement, proof, context, or relevant declared dependency changes | Mark the previous assessment as belonging to an older source revision |
| Only source line positions shift | Preserve semantic currency under the existing identity-hash policy; refresh navigation metadata without misdirecting evidence links |
| Relevant local Lean dependency changes | Mark the prior artifact assessment out of date even if the target file itself is unchanged |
| User edits Lean manually or through another session | Mark the prior assessment out of date; preserve the compiler recheck; do not run an evaluator |
| New run starts on the same source | Show its initial assessment when available; label earlier updates as previous work and supply their unresolved findings as context |
| Old-run status arrives after a new run/source revision | Retain it under the old run; do not apply it to the new run's current tag |
| Normal completion | Present the final tool assessment if it matches the final artifact/source/dependency snapshot |
| Final code changes or automatic checks occur after the last assessment | Keep the last assessment explicitly partial/outdated as applicable; do not promote it into an assessment of unseen changes |

The current projection should distinguish `current`, `newer_artifact`, `source_changed`, `dependency_changed`, and `unknown` freshness, or equivalent structured reasons. The UI can simplify this to “Updated work not yet assessed,” “Source changed,” or “Last known status.” It must not show an unqualified high-confidence tag for a different revision.

Do not clear useful commentary on every intermediate file write. The last update remains visible, anchored to its assessed version, while Lea works toward the next update. Before any artifact exists, `source_only` scope and a null artifact reference are legitimate rather than an error.

On pause/resume and context compaction, preserve the current status summary, unresolved findings, planned/applied corrections, and source binding in the solver's usable context. Resumed Lea should update these findings rather than rediscovering them as unrelated duplicates. At a changed source revision, carry them as prior findings to reassess, not as automatically current judgments.

**11. Failure and interruption behavior**

| Failure or stop | User-visible result |
| --- | --- |
| Model never calls the tool | “No assessment published” plus the actual run outcome; record missing-reporting telemetry |
| Invalid status payload | Return validation details to Lea; preserve the prior status; allow correction during the existing run |
| Persistence failure | Do not acknowledge publication or show an unpersisted confidence change; return a tool error and surface a reporting failure |
| Continued inability to publish material findings | Pause at a safe boundary with a reporting-failure reason rather than continue indefinitely without the promised visibility |
| SSE/browser disconnect | Keep the run and storage active; recover current status/history from SQLite on reconnect |
| Companion or adapter restart | Recover accepted updates; show the run's actual recovered/interrupted state and any interruption notice |
| Turn, cost, time, or global spend cap | Preserve last status, unresolved findings, and artifact; display the structured recoverable stop reason |
| User Pause | Preserve the last accepted assessment and record the stop; no extra model call for status |
| Compiler error after high confidence | Lean Check reports the error; retain the scoped assessment and mark newer/unassessed work when needed |

Operational errors must remain distinguishable from mathematical concerns. Existing human-approval and kernel verification outcomes cannot be changed by a status tool call.

**12. Removing the separate evaluator**

For the new flow, remove all automatic and manual paths that start an independent Lea Check, not just the main formalization-completion hook. This includes terminal/pause scheduling, manual Lean edits, self-repair and cascade paths, Overleaf chat mutations, and GitHub import scheduling.

Remove the **Retry Lea Check** action from the extension. Do not replace it with a hidden evaluation run or a status-only independent agent. Users can resume or initiate a supported formalization effort; its solver maintains Lea Status while working.

Historical checks remain readable with their original provenance and revisions, labeled **Previous Lea Check report**. Do not migrate old `approved` verdicts into new solver confidence values. Existing imported or manually edited artifacts with no corresponding solver assessment show **Not assessed** until a supported solver run assesses them.

For compatibility, legacy start/retry endpoints should return a clear retired-feature response, such as HTTP 410 with a migration message. Read-only historical endpoints can remain available. Remove evaluator execution, queuing, retries, and evaluator-specific configuration from the active path. Retain historical evaluator usage in totals; new status calls are accounted for within ordinary solver usage, without a second charge category or duplicate accounting.

During a staged release, bind each new run to one version of the status contract. Advertise capability between adapter and companion. A companion using the new flow must not silently fall back to terminal evaluation against an older adapter; explain that compatible components are required. Existing evaluator work should be drained or explicitly canceled during migration, and can only produce historical reports. It must never overwrite a live solver assessment.

This specification supersedes the predecessor's requirements for independent evaluation, automatic post-run checks, final-only reporting, Lea Check verdict labels, and Retry Lea Check. Its fidelity, source association, evidence, git/SQLite split, compiler verification, and human-approval principles continue to apply.

**13. Scope across entry points**

| Entry point | Required behavior |
| --- | --- |
| Theorem formalization, resume, and batch formalization | Full live Lea Status contract |
| Definition formalization | Same reporting mechanism with appropriate statement/definition scope |
| Overleaf self-repair or proof-modifying continuation with a bound source bundle | Same tool, reporting imperative, revision binding, and correction policy |
| Manual Lean edit or compiler-only cascade check | Update Lean Check and invalidate affected assessment freshness; no fabricated Lea assessment |
| Overleaf chat that modifies a bound formalization | Apply the reporting contract to the modification run; retain the chat transcript |
| General discussion or standalone LeaChat | Preserve existing interaction and approval behavior; do not attach a target status implicitly |
| GitHub import without a solver run | Preserve imported artifacts and historical evidence; current solver confidence starts unassessed |

Route these capabilities by trusted run purpose and target/source context. `autonomous: true` alone must not activate Overleaf reporting. Where an existing repair/chat entry point lacks a complete source bundle, add that binding before claiming it supports live assessment; otherwise display the prior status as outdated and the new work as unassessed.

Automatic LaTeX editing, desktop/email notifications, a new whole-project semantic analysis, and independent review are outside this feature. No numerical confidence score or proof-completion percentage is introduced.

**14. Implementation sequence**

1. Define the versioned status/finding schema, confidence/attention projection, revision references, and migration. Extract reusable source/artifact snapshot helpers from evaluator-specific code.
2. Persist the full source context at supported run admission. Add the scoped tool and a typed publication/acknowledgment seam with immediate ordering guarantees.
3. Implement durable status storage, read/history APIs, replay/idempotency handling, and the bridge's live SSE event. Add structured source-obstruction pause handling.
4. Consume status events in the companion and expose durable current projections. Update all supported Overleaf solver/repair/continuation entry points and prompt composition.
5. Adapt the existing tags/report UI into Lea Status, add history and freshness handling, and wire Pause/Resume consistently.
6. Retire evaluator execution, all scheduling hooks, start/retry controls, and obsolete configuration. Preserve historical reports and usage.
7. Validate the integrated flow, including live-model behavior and long-running browser scenarios, before enabling it by default.

The predecessor feature document and associated implementation plan identify this specification and its implementation plan as the planned replacement. These changes are documentation only; the prior documents remain a record of the existing implementation.

**15. Validation and acceptance criteria**

Automated tests should focus on behavioral contracts across the existing prover, adapter, companion, and extension suites. Live-model evaluation is also required: a prompt-string test establishes that the instruction exists, not that Lea reliably follows it.

| ID | Acceptance scenario |
| --- | --- |
| AC1 | A long-running formalization publishes a meaningful initial assessment and multiple intermediate updates before its terminal event. |
| AC2 | `update_lea_status` changes both the visible confidence tag and status details while the solver remains active. |
| AC3 | A status call followed by a blocked/slow tool is persisted and visible before that later tool returns. |
| AC4 | Source-only status can be published before any Lean file exists. |
| AC5 | A source gap is disclosed before its Lean correction, later marked applied only after checking, and remains a source issue until resolved in the source or explicitly retracted. |
| AC6 | A required new assumption or materially changed approach produces a visible blocking finding. An opted-in individual run pauses recoverably before subsequent proof mutation; a default run continues only with faithful work. |
| AC7 | High confidence on partial work never changes Lean Check to checked, hides obligations, or appears as independent approval. |
| AC8 | A final status uses the same tool and retains material findings; normal completion launches zero independent evaluator calls. |
| AC9 | Pause, budget exhaustion, and crashes preserve accepted status and artifacts without spending tokens for a post-stop assessment. |
| AC10 | Source, artifact, and dependency changes produce the correct freshness reason; old-run updates cannot replace the current run's assessment. |
| AC11 | Refresh, multi-tab attachment, disconnect/reconnect, and process restart recover the same latest accepted assessment and history without duplicates. |
| AC12 | Malformed payloads and persistence failures do not erase or falsely acknowledge status; a corrected call can succeed in the existing run. |
| AC13 | Findings and reporting context survive resume and compaction, including reassessment after LaTeX edits. |
| AC14 | All existing evaluator start paths, including import/manual edit/repair/chat, are retired; legacy retries cannot incur evaluator usage. |
| AC15 | Previous Lea Check reports remain available with original provenance and usage; no historical approval is relabeled as solver confidence. |
| AC16 | General LeaChat, human approval, compiler checks, verified counterexamples, stub/dependency warnings, and derived session proof status retain their existing contracts. |
| AC17 | The UI updates accessibly without collapsing inspected findings, stealing focus, or suppressing Pause merely because the source became stale. |
| AC18 | Concurrent formalizations keep their identities, source bundles, sequences, findings, and current projections isolated. |
| AC19 | The pause setting defaults off, persists in the companion, and is snapshotted at run admission. **Formalize all** forces it off for each item, continues independent work after a failed item, and never stops on a source finding. |

Relevant test locations include `apps/lea-standalone/prover/tests/agent/`, adapter bridge/routes/alignment tests, and Overleaf companion/check-state/content/pane tests. Replace tests that require post-run evaluation with tests for the new behavior; retain historical-report read coverage.

The reviewed live-model corpus should include a faithful long proof, an omitted but recoverable justification, a false intermediate claim repairable within the same method, an essential missing hypothesis, a missing/ambiguous source proof, a definition, a dependency obstruction, and a forced stop before the first or final update. Inspect mathematical honesty, usefulness of updates, and correction timing—not only successful compilation.

Collect time to first assessment, gaps between accepted updates, publish-to-render latency, missing initial/final updates, invalid tool payloads, reporting failures, and any attempted retired-evaluator launches. Record ordinary solver token/cost changes to assess reporting overhead. Numerical alert thresholds beyond the proposed cadence and delivery target should be calibrated from pilot runs.

The feature is complete when a user can observe and act on Lea's substantive assessment during formalization, with durable revision-aware history, and there is no separate Lea Check execution phase left in the supported workflow.
