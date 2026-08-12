// Downstream cascade verification, factored out of handleLeanPaneEditSave so
// every path that changes a recorded declaration (manual edit, chat-mirror run,
// re-formalize, repair run) can re-verify dependents through one pipeline.
// docs/FEATURE-overleaf-self-repair.md / docs/PLAN-overleaf-self-repair.md
// (Phase 0).
//
// The pipeline preserves three behaviors that postdate the original manual-edit
// spec and were bug-driven (see the inline comments, kept from the original):
//   1. the changed module is force-rebuilt before any dependent is checked, and
//      a rebuild failure fails CLOSED (dependents marked unknown, not valid);
//   2. each dependent's verdict comes from a real `lake build` (adapter
//      /rebuild), not the warm LSP check, which can pass against stale .oleans;
//   3. breakage propagates transitively down the recorded import graph to a
//      fixpoint, so second-hop dependents can't spuriously read "still valid".
//
// Whether a cascade is REQUIRED at all (cascadeRequired(classification) ||
// recovery-from-failure) is the caller's decision -- it depends on caller-local
// state such as the pre-change verdict. This module only executes the pass.
//
// Every collaborator is injected via `deps` rather than imported: the session/
// job helpers live in server.mjs (importing them here would be circular), and
// injection lets the test suite drive the pipeline with the same fake-fetchImpl
// pattern leanPaneEdit.test.mjs already uses.

/**
 * The attribution descriptor for the upstream change that triggered this
 * cascade. Threaded onto every brokenByUpstream entry so the pane / chat
 * mirror can say what broke a dependent, how, and when -- and so a later
 * repair dispatch can reconstruct the context (feature spec Part 4).
 *
 * {
 *   overleafProjectId: string,
 *   targetLabel: string,        // pane label of the changed item (doc anchor)
 *   effectiveName: string,      // Lean declaration name currently in the file
 *   classification: EditClassification,   // from classifyEdit
 *   via: "edit" | "chat" | "formalize" | "repair",
 *   editedAt: string,           // ISO timestamp of the change
 *   sessionId: string,          // upstream session (for the rebuild step)
 *   path: string,               // upstream working-file path (for the rebuild)
 *   namespace: string,
 *   moduleName: string
 * }
 */

// Build the `brokenByUpstream` attribution for one broken dependent.
function brokenByUpstreamEntry(upstream, extra = {}) {
  const { classification } = upstream;
  return {
    targetLabel: upstream.targetLabel,
    renamed: classification.kind === "renamed",
    via: upstream.via,
    editedAt: upstream.editedAt,
    ...extra
  };
}

// The persisted-attribution mirror of brokenByUpstreamEntry: what Phase 2
// stores on a broken dependent's linked job (job.lastEditBreakage) so the
// repair offer survives manifest refreshes and companion restarts. The
// upstream item's own broken state uses the same shape with
// upstreamLabel === its own label.
export function breakageDescriptor(upstream) {
  const { classification } = upstream;
  return {
    upstreamLabel: upstream.targetLabel,
    upstreamDeclarationName: classification.kind === "renamed" ? classification.to : upstream.effectiveName,
    classificationKind: classification.kind,
    renamedFrom: classification.kind === "renamed" ? classification.from : undefined,
    renamedTo: classification.kind === "renamed" ? classification.to : undefined,
    via: upstream.via,
    editedAt: upstream.editedAt,
    // The upstream declaration's pre/post-change headers (normalized strings,
    // parseDeclarationHeader shape) -- persisted so a repair prompt built long
    // after the change (or after a companion restart) can still show the agent
    // exactly what changed instead of re-deriving or omitting it.
    beforeHeader: upstream.beforeHeader?.header || null,
    afterHeader: upstream.afterHeader?.header || null
  };
}

/**
 * Re-verify every transitive dependent of a changed module. Returns
 * `{ dependentsImpact, jobsChanged }`; the caller persists state.jobs once,
 * after all mutations, exactly as handleLeanPaneEditSave always has.
 */
export async function runCascadeVerification({ state, deps, upstream }) {
  const {
    fetchImpl,
    baseUrl,
    apiKey,
    dependentsOf,
    rebuildApiSessionModule,
    runApiSessionLeanCheck,
    resolveDependentSession,
    recordEditCheckVerdict,
    summarizeDependentFile,
    parseLeanImports
  } = deps;
  const { overleafProjectId, targetLabel, effectiveName, classification } = upstream;
  const breakage = breakageDescriptor(upstream);

  let jobsChanged = false;
  const dependentsImpact = [];

  let dependents = [];
  try {
    dependents = await dependentsOf({
      leaRepoPath: state.settings.leaRepoPath,
      namespace: upstream.namespace,
      moduleName: upstream.moduleName
    });
  } catch {
    dependents = [];
  }

  // The dependents loop below re-checks each dependent via the fast LSP
  // `lean-check` path -- which never touches the *edited* module's compiled
  // `.olean`, so without this, every dependent would resolve `import
  // <editedModule>` against whatever was built before this save, no matter
  // how many times it's rechecked (the bug this cascade exists to catch).
  // Force one real rebuild of the edited module first, so the checks below
  // are against its current source. Skipped when there's nothing to verify
  // against (dependents.length === 0) -- no point paying for a rebuild that
  // nothing downstream would observe.
  if (dependents.length > 0) {
    const rebuild = await rebuildApiSessionModule({
      fetchImpl, baseUrl, apiKey, sessionId: upstream.sessionId, path: upstream.path
    });
    const rebuildOk = rebuild.ok && String(rebuild.body?.status || "").toLowerCase() === "ok";
    if (!rebuildOk) {
      // The edited module doesn't produce a real, current .olean right now
      // (a genuine compile failure the fast own-check may have missed via
      // sorry-recovery, an adapter/transport failure, or a timeout) -- every
      // dependent's check below would be checking against nothing
      // trustworthy. Report "can't verify" rather than guessing either way.
      for (const file of dependents) {
        const summary = summarizeDependentFile(file);
        const dependentSession = resolveDependentSession({ state, overleafProjectId, targetLabel: summary.targetLabel });

        if (dependentSession.activeJob) {
          // Don't race a live run on the dependent -- same rule as the
          // per-dependent loop below.
          dependentsImpact.push({ ...summary, status: "busy", attributed: true, busy: true, brokenByUpstream: null });
          continue;
        }

        // Fail closed: this dependent was never actually re-checked (the
        // upstream rebuild itself failed), so its status CHIP must not keep
        // reading whatever it last read -- typically "valid", from before
        // this edit -- or the pane is right back to the exact bug this
        // cascade exists to catch, just one layer removed (a wrong message
        // is fixed by formatDependentOutcome's "unknown" branch above it in
        // leanPaneView.mjs; the item's own chip is a separate render path,
        // getTheoremStatus's lastEditCheckStatus override, and needs its
        // own write). There is no "unconfirmed" chip state today -- treating
        // it the same as "broken" is the safe default until one exists.
        const detail = `Not re-verified: rebuilding ${effectiveName} failed, so this dependent's status is unconfirmed.`;
        if (dependentSession.linkedJob) {
          jobsChanged = recordEditCheckVerdict(dependentSession.linkedJob, { status: "error", detail }, breakage) || jobsChanged;
        }
        dependentsImpact.push({
          ...summary,
          status: "unknown",
          attributed: Boolean(dependentSession.leaSessionId),
          busy: false,
          checkDetail: rebuild.body?.detail || rebuild.error || detail,
          brokenByUpstream: null
        });
      }
      dependents = [];
    }
  }

  // Kept for the post-loop transitive propagation below: which project
  // modules each dependent imports, and which session/job each resolved to.
  const importsByModule = new Map(dependents.map((file) => [file.moduleName, parseLeanImports(file.content)]));
  const sessionByModule = new Map();

  for (const file of dependents) {
    const summary = summarizeDependentFile(file);
    const dependentSession = resolveDependentSession({ state, overleafProjectId, targetLabel: summary.targetLabel });
    sessionByModule.set(file.moduleName, dependentSession);

    if (!dependentSession.leaSessionId) {
      // No recorded session for this file's declaration (e.g. jobs.json was
      // reset since it was generated) -- can't attribute a cascade
      // code_step, but still tell the caller this file exists and is at
      // risk so the pane can prompt a manual re-check.
      dependentsImpact.push({ ...summary, status: "unknown", attributed: false, busy: false, brokenByUpstream: null });
      continue;
    }
    if (dependentSession.activeJob) {
      // Don't race a live run on the dependent (PLAN Phase 2 edge case).
      dependentsImpact.push({ ...summary, status: "busy", attributed: true, busy: true, brokenByUpstream: null });
      continue;
    }

    // The dependent's VERDICT comes from a real `lake build` of its module
    // (adapter /rebuild), NOT from the warm LSP lean-check: live testing
    // showed the warm check can still resolve the edited import against a
    // stale compiled build even after the edited module's own rebuild +
    // daemon mark_stale (VS Code, which compiles from source, disagreed --
    // and was right: the file genuinely no longer compiled). `lake build`
    // compiles the dependent AND everything on its import path from
    // current source, exactly like VS Code, so its verdict cannot be a
    // caching artifact. It also makes the transitive case sound end-to-end:
    // building a second-hop dependent rebuilds the broken middle module
    // from source and fails with the real cause.
    const cascadeBuild = await rebuildApiSessionModule({
      fetchImpl, baseUrl, apiKey,
      sessionId: dependentSession.leaSessionId,
      path: file.stepPath
    });
    if (!cascadeBuild.ok) {
      dependentsImpact.push({ ...summary, status: "unknown", attributed: true, busy: false, brokenByUpstream: null });
      continue;
    }
    const broken = String(cascadeBuild.body?.status || "").toLowerCase() !== "ok";

    // Timeline entry (best-effort): the author:"cascade" lean-check records
    // the re-verification as its own code_step in the adapter DB (who/why/
    // when). Its own verdict is deliberately NOT trusted for the chip --
    // see the rebuild comment above; if it disagrees with the build, the
    // build is right. A failure here loses only the timeline entry.
    await runApiSessionLeanCheck({
      fetchImpl, baseUrl, apiKey,
      sessionId: dependentSession.leaSessionId,
      path: file.stepPath,
      author: "cascade",
      formalizationId: dependentSession.linkedJob?.formalizationId || null,
      // The post-edit name: for a rename, the NEW identifier is what the
      // dependent's import now fails (or succeeds) against.
      summary: `Re-checked after edit to ${classification.kind === "renamed" ? classification.to : effectiveName}`
    });

    // Same override as the edited item itself: the cascade verdict is just
    // as authoritative as an own check, so the dependent's chip must
    // reflect it too, not only the impact-list note.
    jobsChanged = recordEditCheckVerdict(dependentSession.linkedJob, cascadeBuild.body, broken ? breakage : null) || jobsChanged;
    dependentsImpact.push({
      ...summary,
      status: broken ? "invalid" : "reverified",
      attributed: true,
      busy: false,
      checkDetail: cascadeBuild.body?.detail || null,
      brokenByUpstream: broken ? brokenByUpstreamEntry(upstream) : null
    });
  }

  // Compilation is transitive: if a dependent's own SOURCE no longer
  // compiles, everything that imports it cannot compile from source either
  // -- but the warm cascade checks above may still have spuriously passed
  // for those second-hop dependents, because LSP checks resolve imports
  // against compiled .oleans and only the EDITED module was rebuilt: the
  // broken dependent's stale .olean still exports its old, working self.
  // (Recipe 4's chain hit exactly this: compactness_corollary broke, but
  // heine_borel_application -- whose import chain reaches the edit only
  // THROUGH the corollary -- read "re-checked, still valid".) Propagate
  // breakage down the recorded import graph to a fixpoint instead of
  // trusting those checks: a "still valid" verdict is only kept when no
  // module on the dependent's import path is known-broken.
  const brokenModules = new Set(
    dependentsImpact
      .filter((entry) => entry.status === "invalid")
      .map((entry) => entry.moduleName)
      .filter(Boolean)
  );
  let propagated = brokenModules.size > 0;
  while (propagated) {
    propagated = false;
    for (const entry of dependentsImpact) {
      if (entry.status !== "reverified") continue;
      const imports = importsByModule.get(entry.moduleName);
      if (!imports) continue;
      const brokenImport = [...imports].find((moduleName) => brokenModules.has(moduleName));
      if (!brokenImport) continue;
      entry.status = "invalid";
      entry.brokenByUpstream = brokenByUpstreamEntry(upstream, { viaModule: brokenImport });
      entry.checkDetail = `Import ${brokenImport} no longer compiles after the edit to ${effectiveName}; `
        + "this item cannot compile from source (its passing re-check was against a stale build of that import).";
      const dependentSession = sessionByModule.get(entry.moduleName);
      if (dependentSession?.linkedJob) {
        jobsChanged = recordEditCheckVerdict(dependentSession.linkedJob, { status: "error", detail: entry.checkDetail }, breakage) || jobsChanged;
      }
      brokenModules.add(entry.moduleName);
      propagated = true;
    }
  }

  return { dependentsImpact, jobsChanged };
}
