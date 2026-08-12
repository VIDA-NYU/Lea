(function () {
  const DEFAULT_COMPANION_URL = "http://127.0.0.1:31245";
  const DEFAULT_LEA_UI_BASE_URL = "http://localhost:5173";
  // Placeholder only, used before the first successful companion fetch. The
  // adapter's LiteLLM catalog is authoritative; the shared package supplies the
  // offline featured fallback. Keep the default in sync with options.js (AUDIT L9).
  const DEFAULT_LEA_MODEL = "o4-mini";
  const DEFAULT_LEA_MAX_TURNS = 20;
  const DEFAULT_LEA_TEX_MIRROR_ENABLED = true;
  const LEA_UI_VIEW_STATUSES = new Set(["formalized", "defined", "disproved", "in_progress", "sorry_stub", "stale"]);
  const TEX_MIRROR_SYNC_DELAY_MS = 1500;
  const TEX_MIRROR_FULL_SYNC_INTERVAL_MS = 10 * 60 * 1000;
  const TARGET_CONTEXT_RADIUS_LINES = 24;
  const TARGET_CONTEXT_MAX_CHARS = 12000;
  const MAX_SPEND_ERROR_CODE = "max_spend_reached";
  const MAX_SPEND_PANE_MESSAGE = "Lea could not complete this formalization because the configured maximum spend has been reached. Increase or clear the cap in Lea settings, then try again.";
  const LEAN_PANE_REFRESH_DELAY_MS = 1500;
  const LEAN_PANE_POLL_DELAY_MS = 4000;
  const LEAN_PANE_WIDTH_STORAGE_KEY = "leanPaneWidthPx";
  const DEFAULT_LEAN_PANE_WIDTH_PX = 520;
  const MIN_LEAN_PANE_WIDTH_PX = 360;
  const LEAN_PANE_VIEWPORT_GUTTER_PX = 24;
  const LEAN_PANE_KEYBOARD_STEP_PX = 24;
  const LEAN_PANE_KEYBOARD_LARGE_STEP_PX = 80;
  const SETTINGS_POPOVER_WIDTH_STORAGE_KEY = "settingsPopoverWidthPx";
  const DEFAULT_SETTINGS_POPOVER_WIDTH_PX = 360;
  const MIN_SETTINGS_POPOVER_WIDTH_PX = 360;
  const MAX_SETTINGS_POPOVER_WIDTH_PX = 720;
  const SETTINGS_POPOVER_VIEWPORT_GUTTER_PX = 24;
  const SETTINGS_POPOVER_KEYBOARD_STEP_PX = 24;
  const SETTINGS_POPOVER_KEYBOARD_LARGE_STEP_PX = 80;
  // Short debounce for an edit-triggered status refresh; a much longer cadence
  // for the in-progress self-poll so an active run doesn't hammer /statuses
  // (each hit does per-target FS scans + adapter fetches) four times a second
  // (AUDIT M4).
  const STATUS_REFRESH_DEBOUNCE_MS = 250;
  const STATUS_REFRESH_IN_PROGRESS_MS = 3000;
  // Push channel (PLAN-system-hardening 3.1): while the companion's /events
  // stream is connected, the fast polls stretch to these slow reconciliation
  // cadences — pushes drive updates, polls only catch missed events. When the
  // stream drops, the schedulers fall back to the fast cadences above.
  const STATUS_REFRESH_RECONCILE_MS = 30000;
  const LEAN_PANE_POLL_RECONCILE_MS = 60000;
  const LEAN_PANE_CHAT_POLL_RECONCILE_MS = 30000;
  const REPAIR_BATCH_POLL_MS = 2000;
  const REPAIR_BATCH_POLL_RECONCILE_MS = 30000;
  const GITHUB_IMPORT_POLL_MS = 1000;
  const LEAN_PANE_ARCHIVE_TIMEOUT_MS = 15000;
  const LEAN_PANE_COMPANION_TIMEOUT_MS = 10000;
  const LEAN_PANE_STARTUP_WATCHDOG_MS = 12000;
  const GITHUB_IMPORT_ACTIVE_STATUSES = new Set(["applying", "checking"]);
  const HUMAN_APPROVAL_STORAGE_KEY = "leaHumanApprovalsV1";
  const MODEL_FAMILY_LABELS = {
    openai: "OpenAI",
    google: "Google AI",
    anthropic: "Anthropic"
  };
  const DEFAULT_MODEL_OPTIONS = [
    { value: DEFAULT_LEA_MODEL, label: DEFAULT_LEA_MODEL, family: "openai" }
  ];
  let activePopover = null;
  let settingsPopoverWidthPx = DEFAULT_SETTINGS_POPOVER_WIDTH_PX;
  let settingsPopoverResizeState = null;
  let statusRefreshTimer = null;
  let usageRefreshTimer = null;
  let latestTargets = [];
  let latestDiagnostics = [];
  let latestActiveTex = "";
  let latestActiveTexPath = "";
  let latestActiveTexProjectId = "";
  let lastMirrorFiles = null;
  let lastMirrorProjectId = "";
  let texMirrorActivatedProjectId = "";
  let texMirrorDirty = false;
  let texMirrorSyncedOnce = false;
  let texMirrorSyncTimer = null;
  let texMirrorSyncPromise = null;
  // When the last zip-download full sync ran (PLAN 3.2): ordinary edits ship
  // only the active buffer; the zip refresh happens on this cadence.
  let lastTexMirrorFullSyncAt = 0;
  let latestStatuses = {};
  // Server-provided target truth is kept separately from the short-lived UI
  // overlay used while imported proofs are being checked. This prevents an
  // ordinary /statuses refresh from re-enabling a matched target mid-import.
  let latestBaseStatuses = {};
  let activeGithubImport = null;
  let githubImportNotice = null;
  let githubImportNoticeTimer = null;
  let githubImportNoticeExpanded = false;
  let humanApprovals = {};
  let humanApprovalsLoadPromise = null;
  let humanApprovalBusyKeys = new Set();
  let badgeLayer = null;
  let settingsButton = null;
  let leanPaneButton = null;
  let leanPane = null;
  let leanPaneBody = null;
  let leanPaneStatus = null;
  let leanPaneProjectTitle = null;
  let leanPaneProjectNamespace = null;
  let leanPaneWidthPx = DEFAULT_LEAN_PANE_WIDTH_PX;
  let leanPaneResizeState = null;
  let leanPaneRefreshTimer = null;
  let leanPanePollTimer = null;
  let leanPaneStartupWatchdogTimer = null;
  let leanPaneView = null;
  let leanPaneExpandedTreeNodeIds = new Set();
  let leanPaneTreeDefaultsKey = "";
  let leanPaneExpandedItemIds = new Set();
  let leanPaneHighlightTimer = null;
  let lastLeanPaneManifest = null;
  let lastLeanPaneManifestProjectId = "";
  let lastProjectIdentity = null;
  let lastLeanPaneFiles = null;
  let lastLeanPaneProjectId = "";
  let leanPaneInventoryWarning = "";
  let leanPaneArchiveLoad = null;
  // Share panel (D34): remote + push against the adapter's project repo, via the
  // companion's /share/github passthroughs. One panel, toggled from the header.
  let leanPaneSharePanel = null;
  let leanPaneShareState = null;
  let leanPaneShareBusy = false;
  // GitHub pushes use a Lea-owned confirmation surface. Browser-native confirm
  // dialogs cannot inherit the extension's typography, spacing, or theme.
  let githubPushDialogState = null;
  // Project identity editing stays inside Lea's visual language instead of
  // falling through to the browser's unstyleable prompt/confirm pair. The
  // dialog owns its async namespace preview so stale responses cannot repaint
  // a newer draft.
  let projectIdentityDialog = null;
  let projectIdentityEditorState = null;
  let projectIdentityPreviewTimer = null;
  // Lean-pane chat mirror: a compact view of the same adapter session the full
  // Lea UI uses. One panel at a time; `leanPaneChatToken` invalidates stale
  // fetch/poll callbacks when the user switches items or closes the panel.
  let leanPaneChatPanel = null;
  let leanPaneChatItem = null;
  let leanPaneChatTarget = null;
  let leanPaneChatResponse = null;
  let leanPaneChatSessionId = "";
  let leanPaneChatRunId = "";
  let leanPaneChatLoading = false;
  let leanPaneChatSending = false;
  let leanPaneChatError = null;
  let leanPaneChatOptimistic = [];
  let leanPaneChatPollTimer = null;
  let leanPaneChatToken = 0;
  // Blueprint view (FEATURE-overleaf-blueprint-view): the Lean pane has two top-level
  // views over the same project — the document-driven "Items" tree (default) and the
  // read-only "Blueprint" dependency graph. `leanPaneBlueprintView` is the lazily
  // imported renderer; the graph + selection are cached so a node click re-renders
  // without a refetch. All reset on pane open/close.
  let leanPaneMainView = "items"; // "items" | "blueprint"
  let leanPaneBlueprintView = null;
  let leanPaneBlueprintToggle = null; // { items, blueprint } header buttons
  let leanPaneBlueprintGraph = null; // last fetched { nodes, edges, exists }
  let leanPaneBlueprintSelectedKey = null;
  let leanPaneBlueprintGenerateBtn = null; // the "Generate…" button, for the in-flight disable
  // Consecutive transient poll failures (AUDIT M2): a thrown fetch used to stop
  // polling entirely, freezing the panel on "Lea is working…". We now retry
  // with backoff up to this cap before giving up and surfacing the error.
  let leanPaneChatPollFailures = 0;
  const LEAN_PANE_CHAT_POLL_MAX_FAILURES = 5;
  // Manual edit (docs/FEATURE-overleaf-lean-pane-manual-edit.md): at most one
  // item's edit view is open at a time, tracked by item id the same way
  // leanPaneExpandedItemIds tracks expansion -- module state survives the
  // pane's full replaceChildren re-render.
  let leanPaneEditingItemId = "";
  let leanPaneEditDraft = "";
  let leanPaneEditPreSaveDependents = [];
  let leanPaneEditError = "";
  let leanPaneEditLastResult = null;
  // Self-repair (docs/FEATURE-overleaf-self-repair.md): the live batch (if
  // any) + the last repair dispatch error. Batch state is companion-side;
  // this holds only the latest /lean-pane/repair/status snapshot.
  let leanPaneRepairBatch = null;
  let leanPaneRepairBatchTimer = 0;
  // Batch queue disclosure survives the pane's replaceChildren re-render, but
  // is scoped to one batch id so a new run always starts compact.
  let leanPaneExpandedBatchQueueId = "";
  let leanPaneExpandedBatchCompletedId = "";
  // A repair DISPATCH failure, scoped to what was being dispatched:
  // { itemKey, message } with itemKey = the single item's target label, or
  // "batch" (PLAN-self-repair-stale-offers Fix 4 -- a global string rendered
  // under every broken item was itself a member of the stale-copy class).
  let leanPaneRepairError = null;
  // Formalize/stub dispatch errors belong to the item that launched them. The
  // pane body is replaced on every manifest refresh, so DOM-only feedback (or
  // the shared inventory status line) disappears almost immediately. Keep the
  // latest error per item in module state and render it with the item detail.
  let leanPaneActionErrors = new Map();
  // Error messages reported by the manifest can be re-created on every
  // background refresh. Keep exact dismissed-message fingerprints for the
  // lifetime of the open pane so a dismissal remains respected until the
  // error changes or the user explicitly retries the action.
  let dismissedLeanPaneErrorKeys = new Set();
  // At most one item-card overflow ("More actions") menu is open at a time;
  // the same global click/Escape listeners that dismiss popovers close it.
  let activeOverflowMenu = null;
  // Editor-hook watchdog (PLAN-system-hardening 0.4): warns when the editor is
  // visible but the page bridge never hooked Overleaf's UNSTABLE_ editor event
  // — i.e. Overleaf changed and the integration is silently dead.
  let editorHookWatchdog = null;
  let editorHookSignalSeen = false;
  let editorHookWarningBanner = null;
  // Push channel (PLAN 3.1): one EventSource on the companion's /events.
  // pushConnected is consulted by every poll scheduler when picking a delay.
  let eventsClient = null;
  let pushConnected = false;

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.data?.type === "OL_LEAN_EDITOR_HOOKED") {
      editorHookSignalSeen = true;
      editorHookWatchdog?.editorHooked();
      return;
    }
    if (event.data?.type === "OL_LEAN_TARGET_CLICK") {
      rememberTarget(event.data.target);
      showTargetPopover(event.data.clientX, event.data.clientY, event.data.target);
      return;
    }
    if (event.data?.type === "OL_LEAN_DIAGNOSTIC_CLICK") {
      showDiagnosticPopover(event.data.clientX, event.data.clientY, event.data.diagnostic || event.data.target);
      return;
    }
    if (event.data?.type === "OL_LEAN_NAVIGATE_RESULT") {
      if (!event.data.ok && leanPaneStatus) {
        const file = event.data.sourceFile || "the source file";
        leanPaneStatus.textContent = `Couldn't open ${file} automatically. Open it in Overleaf, then click "Go to source" again.`;
      }
      return;
    }
    if (event.data?.type === "OL_LEAN_TARGETS_VISIBLE") {
      const nextActiveTex = typeof event.data.activeTex === "string" ? event.data.activeTex : "";
      const nextProjectId = extractOverleafProjectId();
      const activeTexChanged = nextActiveTex !== latestActiveTex || nextProjectId !== latestActiveTexProjectId;
      latestTargets = event.data.targets || [];
      latestDiagnostics = event.data.diagnostics || [];
      latestActiveTex = nextActiveTex;
      latestActiveTexPath = typeof event.data.activePath === "string" ? event.data.activePath : latestActiveTexPath;
      latestActiveTexProjectId = nextProjectId;
      renderStatusBadges();
      if (activeTexChanged) {
        texMirrorDirty = true;
        scheduleTexMirrorSync();
        if (leanPane) scheduleLeanPaneRefresh();
      }
      scheduleStatusRefresh();
    }
  });

  injectPageBridge();
  startEditorHookWatchdog();
  startEventsClient();
  requestTargetsSoon();
  renderSettingsButton();
  renderLeanPaneButton();
  hydrateLeanPaneWidthFromStorage();
  hydrateSettingsPopoverWidthFromStorage();
  loadHumanApprovals().then(() => {
    renderStatusBadges();
    if (lastLeanPaneManifest) renderLeanPaneManifest(lastLeanPaneManifest);
  }).catch(() => {});
  chrome.storage?.onChanged?.addListener(handleHumanApprovalStorageChanged);

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (githubImportNoticeExpanded) {
      setGithubImportNoticeExpanded(false);
      return;
    }
    if (githubPushDialogState) {
      closeGithubPushConfirmation();
      return;
    }
    if (projectIdentityDialog) {
      closeProjectIdentityEditor();
      return;
    }
    if (activeOverflowMenu) {
      closeActiveOverflowMenu();
      return;
    }
    if (activePopover) {
      closePopover();
      return;
    }
    if (leanPane) closeLeanPane();
  });

  document.addEventListener("click", (event) => {
    if (
      githubImportNoticeExpanded
      && githubImportNotice?.isConnected
      && !githubImportNotice.contains(event.target)
    ) {
      setGithubImportNoticeExpanded(false);
    }
    if (activeOverflowMenu && !activeOverflowMenu.wrap.contains(event.target)) {
      closeActiveOverflowMenu();
    }
    if (activePopover && !activePopover.contains(event.target)) {
      closePopover();
    }
  });

  window.addEventListener("resize", () => {
    renderStatusBadges();
    clampOpenLeanPaneToViewport();
    clampOpenSettingsPopoverToViewport();
  });
  // Capture-phase scroll fires very frequently; coalesce to one update per
  // animation frame (AUDIT M4) instead of re-parsing the whole document and
  // re-laying out every badge on every scroll tick.
  let scrollRafPending = false;
  window.addEventListener("scroll", () => {
    if (scrollRafPending) return;
    scrollRafPending = true;
    requestAnimationFrame(() => {
      scrollRafPending = false;
      requestTargets();
      renderStatusBadges();
    });
  }, true);

  function renderSettingsButton() {
    if (settingsButton) return;
    settingsButton = document.createElement("button");
    settingsButton.type = "button";
    settingsButton.className = "ol-lean-settings-trigger";
    settingsButton.setAttribute("aria-label", "Open Lea settings and usage");
    settingsButton.title = "Lea settings and usage";
    settingsButton.innerHTML = `
      <span class="ol-lean-trigger-mark">L</span>
    `;
    settingsButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      showSettingsPopover();
    });
    (document.body || document.documentElement).appendChild(settingsButton);
  }

  function renderLeanPaneButton() {
    if (leanPaneButton) return;
    leanPaneButton = document.createElement("button");
    leanPaneButton.type = "button";
    leanPaneButton.className = "ol-lean-pane-trigger";
    leanPaneButton.setAttribute("aria-label", "Open Lean project pane");
    leanPaneButton.title = "Lean project pane";
    const mark = document.createElement("span");
    mark.className = "ol-lean-trigger-mark";
    mark.textContent = "Π";
    leanPaneButton.appendChild(mark);
    leanPaneButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (leanPane) {
        closeLeanPane();
      } else {
        showLeanPane();
      }
    });
    (document.body || document.documentElement).appendChild(leanPaneButton);
  }

  function showLeanPane({ deferRefresh = false, preservePopover = false } = {}) {
    if (!preservePopover) closePopover();
    if (leanPane) return;
    // Fresh pane always opens on the Items view with no blueprint selection.
    leanPaneMainView = "items";
    leanPaneBlueprintGraph = null;
    leanPaneBlueprintSelectedKey = null;
    leanPane = document.createElement("aside");
    leanPane.className = "ol-lean-project-pane";
    leanPane.setAttribute("role", "complementary");
    leanPane.setAttribute("aria-label", "Lean project pane");
    leanPane.tabIndex = -1;
    applyLeanPaneWidth();

    const resizer = document.createElement("button");
    resizer.type = "button";
    resizer.className = "ol-lean-project-pane-resizer";
    resizer.setAttribute("role", "separator");
    resizer.setAttribute("aria-orientation", "vertical");
    resizer.setAttribute("aria-label", "Resize Lean pane");
    resizer.title = "Resize Lean pane";
    resizer.tabIndex = 0;
    resizer.addEventListener("pointerdown", startLeanPaneResize);
    resizer.addEventListener("mousedown", startLeanPaneResize);
    resizer.addEventListener("keydown", handleLeanPaneResizeKeydown);

    const header = document.createElement("div");
    header.className = "ol-lean-project-pane-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "ol-lean-project-pane-titlewrap";
    const paneLabel = document.createElement("span");
    paneLabel.className = "ol-lean-sr-only";
    paneLabel.textContent = "Lean pane";
    const kicker = document.createElement("p");
    kicker.className = "ol-lean-project-pane-kicker";
    kicker.textContent = "Lea project";
    const title = document.createElement("h2");
    const sigma = document.createElement("span");
    sigma.className = "ol-lean-project-pane-sigma";
    sigma.setAttribute("aria-hidden", "true");
    sigma.textContent = "∑";
    leanPaneProjectTitle = document.createElement("span");
    leanPaneProjectTitle.textContent = "Lean pane";
    title.appendChild(sigma);
    title.appendChild(leanPaneProjectTitle);
    const namespace = document.createElement("p");
    namespace.className = "ol-lean-project-pane-namespace";
    namespace.title = "Lean namespace";
    const namespaceLabel = document.createElement("span");
    namespaceLabel.className = "ol-lean-sr-only";
    namespaceLabel.textContent = "Lean namespace: ";
    leanPaneProjectNamespace = document.createElement("span");
    leanPaneProjectNamespace.textContent = "Connecting…";
    namespace.appendChild(namespaceLabel);
    namespace.appendChild(leanPaneProjectNamespace);
    titleWrap.appendChild(paneLabel);
    titleWrap.appendChild(kicker);
    titleWrap.appendChild(title);
    titleWrap.appendChild(namespace);

    const controls = document.createElement("div");
    controls.className = "ol-lean-project-pane-controls";
    // Export lives inside the Share panel (one header entry for everything
    // that moves the project off this page: zip download + GitHub push).
    const shareButton = document.createElement("button");
    shareButton.type = "button";
    shareButton.className = "ol-lean-pane-action";
    shareButton.title = "Share or export the Lean project";
    shareButton.textContent = "Share";
    shareButton.addEventListener("click", () => {
      toggleSharePanel().catch(renderLeanPaneError);
    });
    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.className = "ol-lean-pane-action";
    renameButton.title = "Edit project name";
    renameButton.textContent = "Rename";
    renameButton.addEventListener("click", () => {
      openProjectIdentityEditor({ source: "lean-pane", trigger: renameButton }).catch(renderLeanPaneError);
    });
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "ol-lean-icon-button";
    refresh.title = "Refresh Lean pane";
    refresh.setAttribute("aria-label", "Refresh Lean pane");
    refresh.textContent = "↻";
    refresh.addEventListener("click", () => {
      refreshLeanPaneNow({ forceFetch: true }).catch(renderLeanPaneError);
    });
    const close = document.createElement("button");
    close.type = "button";
    close.className = "ol-lean-icon-button";
    close.title = "Close Lean pane";
    close.setAttribute("aria-label", "Close Lean pane");
    close.textContent = "x";
    close.addEventListener("click", closeLeanPane);
    controls.appendChild(shareButton);
    controls.appendChild(renameButton);
    controls.appendChild(refresh);
    controls.appendChild(close);
    header.appendChild(titleWrap);
    header.appendChild(controls);

    const viewTabs = buildLeanPaneViewTabs();

    leanPaneStatus = document.createElement("p");
    leanPaneStatus.className = "ol-lean-project-pane-status";
    leanPaneBody = document.createElement("div");
    leanPaneBody.className = "ol-lean-project-pane-body";

    leanPane.appendChild(resizer);
    leanPane.appendChild(header);
    leanPane.appendChild(viewTabs);
    leanPane.appendChild(leanPaneStatus);
    leanPane.appendChild(leanPaneBody);
    document.body.appendChild(leanPane);
    leanPane.focus({ preventScroll: true });
    if (!deferRefresh) {
      clearTimeout(leanPaneStartupWatchdogTimer);
      leanPaneStartupWatchdogTimer = setTimeout(() => {
        if (leanPane && leanPaneStatus?.textContent === "Loading project inventory...") {
          renderLeanPaneError(new Error(
            "Pane startup stalled before the project manifest rendered. The extension resource or browser storage request did not settle."
          ));
        }
      }, LEAN_PANE_STARTUP_WATCHDOG_MS);
      refreshLeanPaneNow({ forceFetch: true })
        .catch(renderLeanPaneError)
        .finally(() => {
          clearTimeout(leanPaneStartupWatchdogTimer);
          leanPaneStartupWatchdogTimer = null;
        });
    }
  }

  function closeLeanPane() {
    closeGithubPushConfirmation({ restoreFocus: false });
    if (projectIdentityEditorState?.source === "lean-pane") {
      closeProjectIdentityEditor({ restoreFocus: false });
    }
    clearTimeout(leanPaneRefreshTimer);
    leanPaneRefreshTimer = null;
    clearTimeout(leanPanePollTimer);
    leanPanePollTimer = null;
    clearTimeout(leanPaneStartupWatchdogTimer);
    leanPaneStartupWatchdogTimer = null;
    clearTimeout(leanPaneHighlightTimer);
    leanPaneHighlightTimer = null;
    stopLeanPaneResize({ persist: false });
    closeLeanPaneChat();
    closeActiveOverflowMenu();
    leanPaneSharePanel = null;
    leanPaneShareState = null;
    leanPaneShareBusy = false;
    leanPaneActionErrors = new Map();
    dismissedLeanPaneErrorKeys = new Set();
    if (!leanPane) return;
    leanPane.remove();
    leanPane = null;
    leanPaneBody = null;
    leanPaneStatus = null;
    leanPaneProjectTitle = null;
    leanPaneProjectNamespace = null;
    leanPaneExpandedTreeNodeIds = new Set();
    leanPaneTreeDefaultsKey = "";
    leanPaneMainView = "items";
    leanPaneBlueprintToggle = null;
    leanPaneBlueprintGraph = null;
    leanPaneBlueprintSelectedKey = null;
    leanPaneBlueprintGenerateBtn = null;
  }

  function hydrateLeanPaneWidthFromStorage() {
    if (isExtensionContextInvalidated()) return;
    chrome.storage.sync.get({ [LEAN_PANE_WIDTH_STORAGE_KEY]: DEFAULT_LEAN_PANE_WIDTH_PX })
      .then((settings) => {
        leanPaneWidthPx = clampLeanPaneWidth(settings?.[LEAN_PANE_WIDTH_STORAGE_KEY]);
        applyLeanPaneWidth();
      })
      .catch(() => {
        leanPaneWidthPx = clampLeanPaneWidth(DEFAULT_LEAN_PANE_WIDTH_PX);
        applyLeanPaneWidth();
      });
  }

  function maxLeanPaneWidthPx() {
    const viewportWidth = Number(window.innerWidth) || DEFAULT_LEAN_PANE_WIDTH_PX + LEAN_PANE_VIEWPORT_GUTTER_PX;
    return Math.max(MIN_LEAN_PANE_WIDTH_PX, viewportWidth - LEAN_PANE_VIEWPORT_GUTTER_PX);
  }

  function clampLeanPaneWidth(width) {
    const numeric = Number.parseInt(String(width), 10);
    const fallback = Number.isFinite(numeric) ? numeric : DEFAULT_LEAN_PANE_WIDTH_PX;
    return Math.min(Math.max(fallback, MIN_LEAN_PANE_WIDTH_PX), maxLeanPaneWidthPx());
  }

  function applyLeanPaneWidth(width = leanPaneWidthPx) {
    leanPaneWidthPx = clampLeanPaneWidth(width);
    if (leanPane) {
      leanPane.style.setProperty("--ol-lean-pane-width", `${leanPaneWidthPx}px`);
    }
    return leanPaneWidthPx;
  }

  function persistLeanPaneWidth() {
    if (isExtensionContextInvalidated()) return;
    chrome.storage.sync.set({ [LEAN_PANE_WIDTH_STORAGE_KEY]: leanPaneWidthPx }).catch(() => {});
  }

  function clampOpenLeanPaneToViewport() {
    const nextWidth = clampLeanPaneWidth(leanPaneWidthPx);
    if (nextWidth === leanPaneWidthPx) return;
    applyLeanPaneWidth(nextWidth);
    persistLeanPaneWidth();
  }

  function hydrateSettingsPopoverWidthFromStorage() {
    if (isExtensionContextInvalidated()) return;
    chrome.storage.sync.get({ [SETTINGS_POPOVER_WIDTH_STORAGE_KEY]: DEFAULT_SETTINGS_POPOVER_WIDTH_PX })
      .then((settings) => {
        settingsPopoverWidthPx = clampSettingsPopoverWidth(settings?.[SETTINGS_POPOVER_WIDTH_STORAGE_KEY]);
        applySettingsPopoverWidth();
      })
      .catch(() => {
        settingsPopoverWidthPx = clampSettingsPopoverWidth(DEFAULT_SETTINGS_POPOVER_WIDTH_PX);
        applySettingsPopoverWidth();
      });
  }

  function maxSettingsPopoverWidthPx() {
    const viewportWidth = Number(window.innerWidth)
      || DEFAULT_SETTINGS_POPOVER_WIDTH_PX + SETTINGS_POPOVER_VIEWPORT_GUTTER_PX;
    return Math.max(
      0,
      Math.min(MAX_SETTINGS_POPOVER_WIDTH_PX, viewportWidth - SETTINGS_POPOVER_VIEWPORT_GUTTER_PX)
    );
  }

  function minSettingsPopoverWidthPx() {
    return Math.min(MIN_SETTINGS_POPOVER_WIDTH_PX, maxSettingsPopoverWidthPx());
  }

  function clampSettingsPopoverWidth(width) {
    const numeric = Number.parseInt(String(width), 10);
    const fallback = Number.isFinite(numeric) ? numeric : DEFAULT_SETTINGS_POPOVER_WIDTH_PX;
    const maxWidth = maxSettingsPopoverWidthPx();
    return Math.min(Math.max(fallback, minSettingsPopoverWidthPx()), maxWidth);
  }

  function isSettingsPopover(popover) {
    return Boolean(popover?.classList?.contains("ol-lean-settings-popover"));
  }

  function applySettingsPopoverWidth(width = settingsPopoverWidthPx, popover = activePopover) {
    settingsPopoverWidthPx = clampSettingsPopoverWidth(width);
    if (isSettingsPopover(popover)) {
      popover.style.setProperty("--ol-lean-settings-width", `${settingsPopoverWidthPx}px`);
      const resizer = popover.querySelector(".ol-lean-settings-popover-resizer");
      resizer?.setAttribute("aria-valuemin", String(minSettingsPopoverWidthPx()));
      resizer?.setAttribute("aria-valuemax", String(maxSettingsPopoverWidthPx()));
      resizer?.setAttribute("aria-valuenow", String(settingsPopoverWidthPx));
    }
    return settingsPopoverWidthPx;
  }

  function persistSettingsPopoverWidth() {
    if (isExtensionContextInvalidated()) return;
    chrome.storage.sync.set({ [SETTINGS_POPOVER_WIDTH_STORAGE_KEY]: settingsPopoverWidthPx }).catch(() => {});
  }

  function clampOpenSettingsPopoverToViewport() {
    const nextWidth = clampSettingsPopoverWidth(settingsPopoverWidthPx);
    const widthChanged = nextWidth !== settingsPopoverWidthPx;
    if (isSettingsPopover(activePopover)) {
      applySettingsPopoverWidth(nextWidth, activePopover);
      positionSettingsPopover(activePopover);
    } else {
      settingsPopoverWidthPx = nextWidth;
    }
    if (widthChanged) persistSettingsPopoverWidth();
  }

  function startSettingsPopoverResize(event) {
    const popover = activePopover;
    if (!isSettingsPopover(popover) || settingsPopoverResizeState) return;
    if (event.type === "mousedown" && event.button !== undefined && event.button !== 0) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    settingsPopoverResizeState = {
      popover,
      startClientX: Number(event.clientX) || 0,
      startWidth: settingsPopoverWidthPx,
      pointerId: event.pointerId,
      usingPointer: event.type === "pointerdown"
    };
    popover.classList.add("ol-lean-settings-popover-resizing");
    document.body?.classList?.add("ol-lean-settings-resizing");
    if (settingsPopoverResizeState.usingPointer) {
      document.addEventListener("pointermove", handleSettingsPopoverResizeMove, true);
      document.addEventListener("pointerup", finishSettingsPopoverResize, true);
      document.addEventListener("pointercancel", cancelSettingsPopoverResize, true);
    } else {
      document.addEventListener("mousemove", handleSettingsPopoverResizeMove, true);
      document.addEventListener("mouseup", finishSettingsPopoverResize, true);
    }
  }

  function handleSettingsPopoverResizeMove(event) {
    if (!settingsPopoverResizeState) return;
    if (
      settingsPopoverResizeState.pointerId !== undefined
      && event.pointerId !== undefined
      && event.pointerId !== settingsPopoverResizeState.pointerId
    ) return;
    event.preventDefault?.();
    const currentClientX = Number(event.clientX) || 0;
    const delta = settingsPopoverResizeState.startClientX - currentClientX;
    applySettingsPopoverWidth(
      settingsPopoverResizeState.startWidth + delta,
      settingsPopoverResizeState.popover
    );
  }

  function finishSettingsPopoverResize(event) {
    event?.preventDefault?.();
    stopSettingsPopoverResize({ persist: true });
  }

  function cancelSettingsPopoverResize(event) {
    event?.preventDefault?.();
    stopSettingsPopoverResize({ persist: false });
  }

  function stopSettingsPopoverResize({ persist }) {
    if (!settingsPopoverResizeState) return;
    const { popover, usingPointer } = settingsPopoverResizeState;
    settingsPopoverResizeState = null;
    if (usingPointer) {
      document.removeEventListener?.("pointermove", handleSettingsPopoverResizeMove, true);
      document.removeEventListener?.("pointerup", finishSettingsPopoverResize, true);
      document.removeEventListener?.("pointercancel", cancelSettingsPopoverResize, true);
    } else {
      document.removeEventListener?.("mousemove", handleSettingsPopoverResizeMove, true);
      document.removeEventListener?.("mouseup", finishSettingsPopoverResize, true);
    }
    popover?.classList?.remove("ol-lean-settings-popover-resizing");
    document.body?.classList?.remove("ol-lean-settings-resizing");
    if (persist) persistSettingsPopoverWidth();
  }

  function handleSettingsPopoverResizeKeydown(event) {
    let nextWidth = null;
    const step = event.shiftKey
      ? SETTINGS_POPOVER_KEYBOARD_LARGE_STEP_PX
      : SETTINGS_POPOVER_KEYBOARD_STEP_PX;
    if (event.key === "ArrowLeft") nextWidth = settingsPopoverWidthPx + step;
    else if (event.key === "ArrowRight") nextWidth = settingsPopoverWidthPx - step;
    else if (event.key === "Home") nextWidth = minSettingsPopoverWidthPx();
    else if (event.key === "End") nextWidth = maxSettingsPopoverWidthPx();
    if (nextWidth === null) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    applySettingsPopoverWidth(nextWidth);
    persistSettingsPopoverWidth();
  }

  function startLeanPaneResize(event) {
    if (!leanPane || leanPaneResizeState) return;
    if (event.type === "mousedown" && event.button !== undefined && event.button !== 0) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    leanPaneResizeState = {
      startClientX: Number(event.clientX) || 0,
      startWidth: leanPaneWidthPx,
      pointerId: event.pointerId,
      usingPointer: event.type === "pointerdown"
    };
    leanPane.classList.add("ol-lean-project-pane-resizing");
    document.body?.classList?.add("ol-lean-pane-resizing");
    if (leanPaneResizeState.usingPointer) {
      document.addEventListener("pointermove", handleLeanPaneResizeMove, true);
      document.addEventListener("pointerup", finishLeanPaneResize, true);
      document.addEventListener("pointercancel", cancelLeanPaneResize, true);
    } else {
      document.addEventListener("mousemove", handleLeanPaneResizeMove, true);
      document.addEventListener("mouseup", finishLeanPaneResize, true);
    }
  }

  function handleLeanPaneResizeMove(event) {
    if (!leanPaneResizeState) return;
    if (leanPaneResizeState.pointerId !== undefined && event.pointerId !== undefined && event.pointerId !== leanPaneResizeState.pointerId) return;
    event.preventDefault?.();
    const currentClientX = Number(event.clientX) || 0;
    const delta = leanPaneResizeState.startClientX - currentClientX;
    applyLeanPaneWidth(leanPaneResizeState.startWidth + delta);
  }

  function finishLeanPaneResize(event) {
    event?.preventDefault?.();
    stopLeanPaneResize({ persist: true });
  }

  function cancelLeanPaneResize(event) {
    event?.preventDefault?.();
    stopLeanPaneResize({ persist: false });
  }

  function stopLeanPaneResize({ persist }) {
    if (!leanPaneResizeState) return;
    const usingPointer = leanPaneResizeState.usingPointer;
    leanPaneResizeState = null;
    if (usingPointer) {
      document.removeEventListener?.("pointermove", handleLeanPaneResizeMove, true);
      document.removeEventListener?.("pointerup", finishLeanPaneResize, true);
      document.removeEventListener?.("pointercancel", cancelLeanPaneResize, true);
    } else {
      document.removeEventListener?.("mousemove", handleLeanPaneResizeMove, true);
      document.removeEventListener?.("mouseup", finishLeanPaneResize, true);
    }
    leanPane?.classList.remove("ol-lean-project-pane-resizing");
    document.body?.classList?.remove("ol-lean-pane-resizing");
    if (persist) persistLeanPaneWidth();
  }

  function handleLeanPaneResizeKeydown(event) {
    let nextWidth = null;
    const step = event.shiftKey ? LEAN_PANE_KEYBOARD_LARGE_STEP_PX : LEAN_PANE_KEYBOARD_STEP_PX;
    if (event.key === "ArrowLeft") nextWidth = leanPaneWidthPx + step;
    else if (event.key === "ArrowRight") nextWidth = leanPaneWidthPx - step;
    else if (event.key === "Home") nextWidth = MIN_LEAN_PANE_WIDTH_PX;
    else if (event.key === "End") nextWidth = maxLeanPaneWidthPx();
    if (nextWidth === null) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    applyLeanPaneWidth(nextWidth);
    persistLeanPaneWidth();
  }

  // ── Export & GitHub sharing (D34) ──────────────────────────────────────────
  // Both actions go through the companion (never :8001 directly): the zip is
  // streamed from GET /project-export, and the share panel drives the
  // /share/github status/remote/push passthroughs. All git/token mechanics stay
  // in the adapter.

  async function exportLeanProject(button) {
    const projectId = extractOverleafProjectId();
    const baseUrl = await chatCompanionBaseUrl();
    const view = await ensureLeanPaneView();
    if (button) button.disabled = true;
    if (leanPaneStatus) leanPaneStatus.textContent = "Preparing the project zip...";
    try {
      const response = await fetch(
        `${baseUrl}/project-export?overleafProjectId=${encodeURIComponent(projectId)}`
      );
      if (!response.ok) {
        let message = `Export failed (HTTP ${response.status}).`;
        try {
          message = (await response.json())?.message || message;
        } catch { /* keep the fallback */ }
        if (leanPaneStatus) leanPaneStatus.textContent = message;
        return;
      }
      const blob = await response.blob();
      const filename = view.filenameFromContentDisposition(
        response.headers.get("content-disposition"),
        "lean-project.zip"
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
      if (leanPaneStatus) leanPaneStatus.textContent = `Downloaded ${filename}.`;
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function toggleSharePanel() {
    if (leanPaneSharePanel) {
      leanPaneSharePanel.remove();
      leanPaneSharePanel = null;
      return;
    }
    if (!leanPane) return;
    await ensureLeanPaneView();

    const panel = document.createElement("div");
    panel.className = "ol-lean-share-panel";
    panel.innerHTML = `
      <label class="ol-lean-share-remote">
        <span>GitHub remote</span>
        <input type="url" autocomplete="off" spellcheck="false" placeholder="https://github.com/you/repo" data-role="share-remote">
      </label>
      <div class="ol-lean-share-actions">
        <button type="button" class="ol-lean-provider-key-button" data-role="share-save">Save remote</button>
        <button type="button" class="ol-lean-save-button" data-role="share-push">Push to GitHub</button>
      </div>
      <div class="ol-lean-share-actions">
        <button type="button" class="ol-lean-provider-key-button" data-role="share-export" title="Download the Lean project as a zip">Download .zip</button>
        <button type="button" class="ol-lean-provider-key-button" data-role="github-import" title="Add non-conflicting Lean files from GitHub">Add Lean files from GitHub</button>
      </div>
      <p class="ol-lean-share-hint" data-role="share-hint" hidden></p>
      <p class="ol-lean-share-status" role="status" data-role="share-status">Loading share status...</p>
    `;
    leanPane.insertBefore(panel, leanPaneStatus);
    leanPaneSharePanel = panel;

    const input = panel.querySelector("[data-role='share-remote']");
    input.addEventListener("input", () => renderShareControls());
    panel.querySelector("[data-role='share-save']").addEventListener("click", () => {
      saveShareRemote().catch((error) => setShareStatus(errorText(error)));
    });
    panel.querySelector("[data-role='share-push']").addEventListener("click", (event) => {
      pushShareRemote(event.currentTarget).catch((error) => setShareStatus(errorText(error)));
    });
    const exportButton = panel.querySelector("[data-role='share-export']");
    exportButton?.addEventListener("click", () => {
      exportLeanProject(exportButton).catch((error) => setShareStatus(errorText(error)));
    });
    panel.querySelector("[data-role='github-import']")?.addEventListener("click", () => {
      openGithubImportDialog().catch((error) => setShareStatus(errorText(error)));
    });

    try {
      await loadShareStatus();
    } catch (error) {
      setShareStatus(errorText(error));
    }
  }

  async function loadShareStatus() {
    const projectId = extractOverleafProjectId();
    const baseUrl = await chatCompanionBaseUrl();
    const response = await fetch(
      `${baseUrl}/share/github?overleafProjectId=${encodeURIComponent(projectId)}`
    );
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body?.message || `Could not load share status (HTTP ${response.status}).`);
    }
    leanPaneShareState = {
      exists: Boolean(body.exists),
      remoteUrl: body.remoteUrl || null,
      tokenConfigured: Boolean(body.tokenConfigured)
    };
    const input = leanPaneSharePanel?.querySelector("[data-role='share-remote']");
    if (input) input.value = leanPaneShareState.remoteUrl || "";
    setShareStatus("");
    renderShareControls();
  }

  // GitHub-import preview ensures the adapter project exists, even when the
  // Overleaf document did not have one before. Refresh the already-open Share
  // panel after that transition (and again after apply) so it does not retain
  // the pre-import `exists: false` snapshot. A status-refresh failure should
  // not turn a successful preview/import into a failed import operation.
  async function refreshShareStatusAfterProjectEnsure() {
    if (!leanPaneSharePanel) return;
    try {
      await loadShareStatus();
    } catch (error) {
      setShareStatus(`Could not refresh share status: ${errorText(error)}`);
    }
  }

  function renderShareControls() {
    if (!leanPaneSharePanel) return;
    const input = leanPaneSharePanel.querySelector("[data-role='share-remote']");
    const save = leanPaneSharePanel.querySelector("[data-role='share-save']");
    const push = leanPaneSharePanel.querySelector("[data-role='share-push']");
    const hint = leanPaneSharePanel.querySelector("[data-role='share-hint']");
    const controls = leanPaneView.deriveShareControls({
      exists: Boolean(leanPaneShareState?.exists),
      remoteUrl: leanPaneShareState?.remoteUrl || null,
      draftRemote: input?.value,
      tokenConfigured: Boolean(leanPaneShareState?.tokenConfigured),
      busy: leanPaneShareBusy
    });
    if (input) input.disabled = leanPaneShareBusy || !leanPaneShareState?.exists;
    if (save) save.disabled = !controls.canSave;
    if (push) {
      push.disabled = !controls.canPush;
      push.textContent = leanPaneShareBusy ? "Working..." : "Push to GitHub";
    }
    if (hint) {
      hint.textContent = controls.hint;
      hint.hidden = !controls.hint;
    }
  }

  function setShareStatus(text) {
    const status = leanPaneSharePanel?.querySelector("[data-role='share-status']");
    if (status) status.textContent = text || "";
  }

  async function saveShareRemote() {
    const input = leanPaneSharePanel?.querySelector("[data-role='share-remote']");
    const remoteUrl = String(input?.value || "").trim();
    if (!remoteUrl) return;
    const projectId = extractOverleafProjectId();
    const baseUrl = await chatCompanionBaseUrl();
    leanPaneShareBusy = true;
    renderShareControls();
    setShareStatus("Saving remote...");
    try {
      const response = await fetch(`${baseUrl}/share/github/remote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overleafProjectId: projectId, remoteUrl })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        setShareStatus(body?.message || `Could not save the remote (HTTP ${response.status}).`);
        return;
      }
      leanPaneShareState = { ...leanPaneShareState, remoteUrl: body.remoteUrl || remoteUrl };
      if (input) input.value = leanPaneShareState.remoteUrl;
      setShareStatus("Remote saved.");
    } finally {
      leanPaneShareBusy = false;
      renderShareControls();
    }
  }

  function closeGithubPushConfirmation({ confirmed = false, restoreFocus = true } = {}) {
    const state = githubPushDialogState;
    if (!state) return;
    githubPushDialogState = null;
    state.shell.remove();
    if (restoreFocus && state.trigger?.isConnected) {
      state.trigger.focus({ preventScroll: true });
    }
    state.resolve(Boolean(confirmed));
  }

  function requestGithubPushConfirmation(remote, trigger = null) {
    closeGithubPushConfirmation({ restoreFocus: false });
    return new Promise((resolve) => {
      const shell = createProjectIdentityElement(
        "div",
        "ol-lean-project-identity-backdrop ol-lean-github-push-backdrop"
      );
      const dialog = createProjectIdentityElement(
        "section",
        "ol-lean-project-identity-dialog ol-lean-github-push-dialog"
      );
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.setAttribute("aria-labelledby", "ol-lean-github-push-title");
      dialog.setAttribute("aria-describedby", "ol-lean-github-push-description");

      const header = createProjectIdentityElement(
        "header",
        "ol-lean-project-identity-header ol-lean-github-push-header"
      );
      const mark = createProjectIdentityElement(
        "span",
        "ol-lean-project-identity-mark ol-lean-github-push-mark",
        "↗"
      );
      mark.setAttribute("aria-hidden", "true");
      const heading = createProjectIdentityElement("div", "ol-lean-project-identity-heading");
      heading.appendChild(createProjectIdentityElement(
        "p",
        "ol-lean-project-identity-kicker",
        "GitHub repository"
      ));
      const title = createProjectIdentityElement("h2", "", "Push project?");
      title.id = "ol-lean-github-push-title";
      const description = createProjectIdentityElement(
        "p",
        "ol-lean-project-identity-description",
        "Review the destination before sending this Lea project's commits."
      );
      description.id = "ol-lean-github-push-description";
      heading.appendChild(title);
      heading.appendChild(description);
      const close = createProjectIdentityElement(
        "button",
        "ol-lean-icon-button ol-lean-project-identity-close",
        "×"
      );
      close.type = "button";
      close.setAttribute("aria-label", "Close GitHub push confirmation");
      header.appendChild(mark);
      header.appendChild(heading);
      header.appendChild(close);

      const content = createProjectIdentityElement("div", "ol-lean-github-push-content");
      const review = createProjectIdentityElement("section", "ol-lean-github-push-review");
      review.setAttribute("aria-label", "GitHub push destination");
      review.appendChild(createProjectIdentityElement(
        "p",
        "ol-lean-project-identity-preview-title",
        "Destination"
      ));

      const remoteRow = createProjectIdentityElement("div", "ol-lean-github-push-review-row");
      remoteRow.appendChild(createProjectIdentityElement("span", "", "Repository"));
      remoteRow.appendChild(createProjectIdentityElement("code", "", remote));
      review.appendChild(remoteRow);

      const branchRow = createProjectIdentityElement("div", "ol-lean-github-push-review-row");
      branchRow.appendChild(createProjectIdentityElement("span", "", "Branch"));
      branchRow.appendChild(createProjectIdentityElement("code", "", "main"));
      review.appendChild(branchRow);
      content.appendChild(review);

      const note = createProjectIdentityElement(
        "p",
        "ol-lean-github-push-note",
        "Lea will update the remote main branch with its committed proof files. If the repository has newer commits, the push will stop so you can reconcile them first."
      );
      content.appendChild(note);

      const actions = createProjectIdentityElement(
        "footer",
        "ol-lean-project-identity-actions ol-lean-github-push-actions"
      );
      const cancel = createProjectIdentityElement("button", "ol-lean-secondary-button", "Cancel");
      cancel.type = "button";
      const confirm = createProjectIdentityElement(
        "button",
        "ol-lean-primary-button ol-lean-github-push-confirm",
        "Push to GitHub"
      );
      confirm.type = "button";
      confirm.dataset.role = "confirm-push";
      actions.appendChild(cancel);
      actions.appendChild(confirm);
      content.appendChild(actions);

      dialog.appendChild(header);
      dialog.appendChild(content);
      shell.appendChild(dialog);
      document.body.appendChild(shell);
      githubPushDialogState = { shell, dialog, trigger, resolve };

      const cancelPush = () => closeGithubPushConfirmation();
      close.addEventListener("click", cancelPush);
      cancel.addEventListener("click", cancelPush);
      confirm.addEventListener("click", () => {
        closeGithubPushConfirmation({ confirmed: true });
      });
      shell.addEventListener("click", (event) => {
        event.stopPropagation();
        if (event.target === shell) cancelPush();
      });
      dialog.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          cancelPush();
          return;
        }
        if (event.key !== "Tab") return;
        const focusable = [close, cancel, confirm].filter((element) => !element.disabled && !element.hidden);
        const index = focusable.indexOf(document.activeElement);
        if (event.shiftKey && index <= 0) {
          event.preventDefault();
          focusable[focusable.length - 1].focus();
        } else if (!event.shiftKey && index === focusable.length - 1) {
          event.preventDefault();
          focusable[0].focus();
        }
      });
      confirm.focus({ preventScroll: true });
    });
  }

  async function pushShareRemote(trigger = null) {
    const remote = leanPaneShareState?.remoteUrl;
    if (!remote) return;
    const confirmed = await requestGithubPushConfirmation(remote, trigger);
    if (!confirmed) return;
    const projectId = extractOverleafProjectId();
    const baseUrl = await chatCompanionBaseUrl();
    leanPaneShareBusy = true;
    renderShareControls();
    setShareStatus("Pushing to GitHub...");
    try {
      const response = await fetch(`${baseUrl}/share/github/push`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overleafProjectId: projectId })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        setShareStatus(body?.message || `Push failed (HTTP ${response.status}).`);
        return;
      }
      setShareStatus(`Pushed to ${body.remoteUrl || remote}.`);
    } finally {
      leanPaneShareBusy = false;
      renderShareControls();
    }
  }

  function errorText(error) {
    return error instanceof Error ? error.message : String(error);
  }

  function githubImportProgressText(progress) {
    const checks = progress?.counts?.checks || {};
    const passed = Number(checks.ok || 0);
    const failed = Number(checks.error || 0);
    const pending = Number(checks.pending || 0);
    if (progress?.status === "applying") return "Adding Lean files from GitHub…";
    if (passed === 0 && failed === 0) {
      return pending > 0
        ? `${pending} imported Lean file${pending === 1 ? " is" : "s are"} queued for checking.`
        : "Imported Lean files are queued for local checks.";
    }
    return `Checking imported Lean files: ${passed} passed · ${failed} failed · ${pending} pending`;
  }

  function githubImportCompletionText(progress) {
    const dispositions = progress?.counts?.dispositions || {};
    const conflicts = Number(dispositions.path_conflict || 0)
      + Number(dispositions.declaration_conflict || 0);
    return `${progress?.reused ? "Already imported · " : ""}${Number(dispositions.add || 0)} added · ${Number(dispositions.already_present || 0)} already present · ${conflicts} conflicts skipped · ${Number(progress?.counts?.matched_declarations || 0)} formalizations populated · ${Number(progress?.counts?.reusable_declarations || 0)} reusable declarations`;
  }

  function githubImportFormalizationQueue(tracker) {
    if (!tracker) return [];
    const progress = tracker.progress || {};
    const files = Array.isArray(progress.files) ? progress.files : [];
    const declarations = Array.isArray(progress.declarations) ? progress.declarations : [];
    const pendingFile = files.find((file) => file?.check_status === "pending");
    const items = [];
    for (const target of tracker.targetDetails || []) {
      const declaration = declarations.find((row) => (
        row?.declaration_name === target.declarationName
        || row?.full_name === target.declarationName
      ));
      const destinationPath = String(declaration?.destination_path || target.destinationPath || "");
      const file = files.find((row) => row?.destination_path === destinationPath);
      if (["ok", "error"].includes(String(file?.check_status || ""))) continue;
      const checking = progress.status === "checking" && (
        pendingFile
          ? pendingFile.destination_path === destinationPath
          : items.length === 0
      );
      items.push({
        key: target.key,
        label: target.displayTitle || target.declarationName || target.targetLabel,
        state: checking ? "checking" : "queued",
      });
    }
    return items.sort((left, right) => Number(right.state === "checking") - Number(left.state === "checking"));
  }

  function githubImportNoticeText(tracker, queue = githubImportFormalizationQueue(tracker)) {
    if (queue.length === 0) return githubImportProgressText(tracker?.progress);
    const noun = queue.length === 1 ? "formalization" : "formalizations";
    const current = queue.find((item) => item.state === "checking");
    return current
      ? `${queue.length} ${noun} remaining · Checking ${current.label}`
      : `${queue.length} ${noun} queued for import checks`;
  }

  function setGithubImportNoticeExpanded(expanded) {
    githubImportNoticeExpanded = Boolean(expanded);
    if (!githubImportNotice) return;
    const toggle = githubImportNotice.querySelector("[data-role='toggle']");
    const details = githubImportNotice.querySelector("[data-role='details']");
    const arrow = githubImportNotice.querySelector("[data-role='arrow']");
    const dismiss = githubImportNotice.querySelector("[data-role='dismiss']");
    const active = githubImportNotice.dataset.active === "true";
    toggle?.setAttribute("aria-expanded", String(githubImportNoticeExpanded));
    if (details) details.hidden = !githubImportNoticeExpanded;
    if (arrow) arrow.textContent = githubImportNoticeExpanded ? "⌄" : "⌃";
    if (dismiss) {
      dismiss.hidden = active && !githubImportNoticeExpanded;
      dismiss.textContent = active ? "−" : "×";
      dismiss.setAttribute(
        "aria-label",
        active ? "Minimize GitHub import status" : "Dismiss GitHub import status"
      );
    }
    githubImportNotice.classList.toggle("is-expanded", githubImportNoticeExpanded);
  }

  function showGithubImportNotice(message, { error = false, settled = false, queue = [] } = {}) {
    const importId = String(activeGithubImport?.id || "");
    clearTimeout(githubImportNoticeTimer);
    githubImportNoticeTimer = null;
    if (!githubImportNotice?.isConnected) {
      githubImportNotice = document.createElement("div");
      githubImportNotice.className = "ol-lean-github-import-notice";
      githubImportNotice.setAttribute("role", "region");
      githubImportNotice.setAttribute("aria-label", "GitHub import progress");

      const header = document.createElement("div");
      header.className = "ol-lean-github-import-notice-header";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "ol-lean-github-import-notice-toggle";
      toggle.dataset.role = "toggle";
      const arrow = document.createElement("span");
      arrow.dataset.role = "arrow";
      arrow.setAttribute("aria-hidden", "true");
      const copy = document.createElement("span");
      copy.dataset.role = "copy";
      copy.setAttribute("role", "status");
      copy.setAttribute("aria-live", "polite");
      toggle.append(arrow, copy);
      toggle.addEventListener("click", () => {
        setGithubImportNoticeExpanded(!githubImportNoticeExpanded);
      });
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "ol-lean-icon-button";
      dismiss.dataset.role = "dismiss";
      dismiss.addEventListener("click", () => {
        if (githubImportNotice?.dataset.active === "true") {
          setGithubImportNoticeExpanded(false);
          return;
        }
        githubImportNotice?.remove();
      });
      header.append(toggle, dismiss);

      const details = document.createElement("div");
      details.className = "ol-lean-github-import-notice-details";
      details.dataset.role = "details";
      details.hidden = true;
      githubImportNotice.append(header, details);
      (document.body || document.documentElement).appendChild(githubImportNotice);
    }
    githubImportNotice.dataset.importId = importId;
    githubImportNotice.dataset.active = String(!settled && Boolean(importId));
    githubImportNotice.querySelector("[data-role='copy']").textContent = message;
    githubImportNotice.classList.toggle("is-error", error);
    githubImportNotice.classList.toggle("is-settled", settled);
    const toggle = githubImportNotice.querySelector("[data-role='toggle']");
    if (toggle) {
      toggle.disabled = queue.length === 0;
      toggle.title = queue.length > 0 ? "Show remaining formalizations" : "";
    }
    const arrow = githubImportNotice.querySelector("[data-role='arrow']");
    if (arrow) arrow.hidden = queue.length === 0;
    const details = githubImportNotice.querySelector("[data-role='details']");
    details?.replaceChildren();
    if (details && queue.length > 0) {
      const list = document.createElement("ol");
      list.className = "ol-lean-github-import-queue";
      for (const item of queue) {
        const row = document.createElement("li");
        row.className = `is-${item.state}`;
        const marker = document.createElement("span");
        marker.className = "ol-lean-github-import-queue-marker";
        marker.textContent = item.state === "checking" ? "●" : "○";
        marker.setAttribute("aria-hidden", "true");
        const label = document.createElement("strong");
        label.textContent = item.label;
        const state = document.createElement("span");
        state.textContent = item.state === "checking" ? "Checking now" : "Queued";
        row.append(marker, label, state);
        list.appendChild(row);
      }
      details.appendChild(list);
    }
    if (settled || queue.length === 0) githubImportNoticeExpanded = false;
    setGithubImportNoticeExpanded(githubImportNoticeExpanded);
    if (settled) {
      githubImportNoticeTimer = setTimeout(() => githubImportNotice?.remove(), 8000);
    }
  }

  function githubImportStatusOverlay(statuses) {
    const overlaid = { ...(statuses || {}) };
    if (!activeGithubImport) return overlaid;
    const message = githubImportProgressText(activeGithubImport.progress);
    for (const key of activeGithubImport.targetKeys) {
      const current = overlaid[key] || {};
      overlaid[key] = {
        ...current,
        status: "in_progress",
        effectiveStatus: current.effectiveStatus || current.status || "unformalized",
        message,
        githubImportPending: true,
        githubImportId: activeGithubImport.id,
      };
    }
    return overlaid;
  }

  function restoreGithubImportPaneItem(item) {
    if (!item?.githubImportPending) return item;
    const restored = {
      ...item,
      status: item.githubImportPreviousStatus,
      inProgress: item.githubImportPreviousInProgress,
      message: item.githubImportPreviousMessage,
    };
    delete restored.githubImportPending;
    delete restored.githubImportId;
    delete restored.githubImportPreviousStatus;
    delete restored.githubImportPreviousInProgress;
    delete restored.githubImportPreviousMessage;
    return restored;
  }

  function githubImportPaneOverlay(manifest) {
    if (!manifest || !Array.isArray(manifest.items)) return manifest;
    const items = manifest.items.map((rawItem) => {
      const item = restoreGithubImportPaneItem(rawItem);
      const kind = item?.leanKind === "def" ? "definition" : "theorem";
      const label = String(item?.label || item?.leanDeclarationName || "").trim();
      if (!activeGithubImport?.targetKeys.has(`${kind}:${label}`)) return item;
      return {
        ...item,
        status: "in-progress",
        inProgress: true,
        message: githubImportProgressText(activeGithubImport.progress),
        githubImportPending: true,
        githubImportId: activeGithubImport.id,
        githubImportPreviousStatus: item.status,
        githubImportPreviousInProgress: item.inProgress,
        githubImportPreviousMessage: item.message,
      };
    });
    return { ...manifest, items };
  }

  function renderGithubImportSurfaceState() {
    latestStatuses = githubImportStatusOverlay(latestBaseStatuses);
    renderStatusBadges();
    if (activePopover?.dataset.targetKey) {
      const target = latestTargets.find((item) => targetKey(item) === activePopover.dataset.targetKey);
      if (target) updatePopoverStatus(activePopover, target);
    }
    if (lastLeanPaneManifest) renderLeanPaneManifest(lastLeanPaneManifest);
  }

  function settleGithubImportTracking(tracker, progress) {
    if (activeGithubImport !== tracker) return;
    activeGithubImport = null;
    renderGithubImportSurfaceState();
    const complete = progress?.status === "complete";
    const detail = progress?.error_detail
      ? ` ${String(progress.error_detail)}`
      : "";
    showGithubImportNotice(
      complete
        ? `GitHub import complete. ${githubImportCompletionText(progress)}`
        : `GitHub import finished with issues.${detail}`,
      { error: !complete, settled: true }
    );
    refreshLeanPaneNow({ forceFetch: true, background: true }).catch(() => {});
    refreshStatusesNow().catch(() => {});
    refreshShareStatusAfterProjectEnsure().catch(() => {});
  }

  async function pollGithubImport(tracker) {
    if (activeGithubImport !== tracker) return;
    try {
      const response = await fetch(
        `${tracker.baseUrl}/project/github-import/status?overleafProjectId=${encodeURIComponent(tracker.projectId)}&importId=${encodeURIComponent(tracker.id)}`
      );
      const progress = await response.json().catch(() => ({}));
      if (!response.ok) throw companionRequestError(response, progress);
      if (activeGithubImport !== tracker) return;
      tracker.progress = progress;
      renderGithubImportSurfaceState();
      const queue = githubImportFormalizationQueue(tracker);
      showGithubImportNotice(githubImportNoticeText(tracker, queue), { queue });
      if (!GITHUB_IMPORT_ACTIVE_STATUSES.has(progress.status)) {
        settleGithubImportTracking(tracker, progress);
        return;
      }
    } catch (error) {
      if (activeGithubImport !== tracker) return;
      showGithubImportNotice(`GitHub import is still running, but its status could not be refreshed. Retrying… ${errorText(error)}`, { error: true });
    }
    tracker.timer = setTimeout(() => pollGithubImport(tracker), GITHUB_IMPORT_POLL_MS);
  }

  function startGithubImportTracking({ progress, preview, targets, projectId, baseUrl }) {
    const tracker = {
      id: String(progress?.id || ""),
      projectId,
      baseUrl,
      progress,
      targetDetails: leanPaneView.githubImportMatchedTargets(preview, targets),
      timer: null,
    };
    tracker.targetKeys = new Set(tracker.targetDetails.map((target) => target.key));
    if (!tracker.id || !GITHUB_IMPORT_ACTIVE_STATUSES.has(progress?.status)) {
      showGithubImportNotice(
        progress?.status === "complete"
          ? `GitHub import complete. ${githubImportCompletionText(progress)}`
          : `GitHub import finished with issues. ${githubImportCompletionText(progress)}`,
        { error: progress?.status !== "complete", settled: true }
      );
      refreshLeanPaneNow({ forceFetch: true, background: true }).catch(() => {});
      refreshStatusesNow().catch(() => {});
      refreshShareStatusAfterProjectEnsure().catch(() => {});
      return;
    }
    if (activeGithubImport?.timer) clearTimeout(activeGithubImport.timer);
    githubImportNoticeExpanded = false;
    activeGithubImport = tracker;
    renderGithubImportSurfaceState();
    const queue = githubImportFormalizationQueue(tracker);
    showGithubImportNotice(githubImportNoticeText(tracker, queue), { queue });
    tracker.timer = setTimeout(() => pollGithubImport(tracker), GITHUB_IMPORT_POLL_MS);
  }

  async function openGithubImportDialog() {
    await ensureLeanPaneView();
    await refreshLeanPaneNow({ forceFetch: true, background: true });
    const projectId = extractOverleafProjectId();
    const baseUrl = await chatCompanionBaseUrl();
    const targets = (lastLeanPaneManifest?.items || []).map((item) =>
      leanPaneView.paneItemToGithubImportTarget(item)
    );
    let preview = null;

    const overlay = document.createElement("div");
    overlay.className = "ol-lean-github-import-overlay";
    overlay.innerHTML = `
      <section class="ol-lean-github-import-dialog" role="dialog" aria-modal="true" aria-labelledby="ol-lean-github-import-title">
        <header>
          <div><h2 id="ol-lean-github-import-title">Add Lean files from GitHub</h2><p>Existing project files are never overwritten.</p></div>
          <button type="button" class="ol-lean-icon-button" data-role="close" aria-label="Close">x</button>
        </header>
        <div class="ol-lean-github-import-content">
          <label>GitHub repository<input type="url" autocomplete="off" spellcheck="false" placeholder="https://github.com/owner/repository" data-role="url"></label>
          <p class="ol-lean-github-import-note">Only tracked .lean files are considered. Conflicting files are skipped independently.</p>
          <div class="ol-lean-github-import-result" data-role="result"></div>
          <p class="ol-lean-github-import-status" data-role="status" role="status"></p>
          <footer>
            <button type="button" class="ol-lean-provider-key-button" data-role="cancel">Cancel</button>
            <button type="button" class="ol-lean-save-button" data-role="analyze">Analyze</button>
            <button type="button" class="ol-lean-save-button" data-role="confirm" hidden>Add Lean files</button>
          </footer>
        </div>
      </section>
    `;
    document.body.appendChild(overlay);
    const urlInput = overlay.querySelector("[data-role='url']");
    const resultNode = overlay.querySelector("[data-role='result']");
    const statusNode = overlay.querySelector("[data-role='status']");
    const analyzeButton = overlay.querySelector("[data-role='analyze']");
    const confirmButton = overlay.querySelector("[data-role='confirm']");

    const close = () => {
      overlay.remove();
    };
    overlay.querySelector("[data-role='close']")?.addEventListener("click", close);
    overlay.querySelector("[data-role='cancel']")?.addEventListener("click", close);
    overlay.addEventListener("mousedown", (event) => {
      if (event.target === overlay) close();
    });

    const setStatus = (text, error = false) => {
      statusNode.textContent = text || "";
      statusNode.classList.toggle("is-error", error);
    };

    const renderPlan = (payload) => {
      const plan = payload?.plan || {};
      const counts = plan.counts || {};
      resultNode.replaceChildren();
      const summary = document.createElement("div");
      summary.className = "ol-lean-github-import-summary";
      summary.textContent = `${counts.add || 0} to add · ${counts.already_present || 0} already present · ${(counts.path_conflict || 0) + (counts.declaration_conflict || 0)} conflicts · ${plan.reusable_declarations || 0} reusable`;
      resultNode.appendChild(summary);
      const list = document.createElement("ul");
      for (const file of plan.files || []) {
        const row = document.createElement("li");
        const path = document.createElement("code");
        path.textContent = file.destination_path || file.source_path;
        const disposition = document.createElement("span");
        disposition.textContent = file.disposition.replaceAll("_", " ");
        disposition.className = `is-${file.disposition}`;
        const reason = document.createElement("small");
        reason.textContent = file.reason || "";
        row.append(path, disposition, reason);
        list.appendChild(row);
      }
      resultNode.appendChild(list);
      const addCount = Number(counts.add || 0);
      confirmButton.textContent = addCount
        ? `Add ${addCount} Lean file${addCount === 1 ? "" : "s"}`
        : "Reconcile existing files";
      confirmButton.hidden = false;
      confirmButton.disabled = Boolean(plan.blocking_error);
      analyzeButton.hidden = true;
      urlInput.disabled = true;
      if (plan.blocking_error) setStatus(plan.blocking_error.message || "This repository cannot be imported.", true);
    };

    analyzeButton.addEventListener("click", async () => {
      const repositoryUrl = String(urlInput.value || "").trim();
      if (!repositoryUrl) return;
      analyzeButton.disabled = true;
      setStatus("Analyzing repository...");
      try {
        const response = await fetch(`${baseUrl}/project/github-import/preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ overleafProjectId: projectId, repositoryUrl, targets })
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw companionRequestError(response, body);
        preview = body;
        await refreshShareStatusAfterProjectEnsure();
        renderPlan(body);
        setStatus("Review the additive file plan before confirming.");
      } catch (error) {
        setStatus(errorText(error), true);
      } finally {
        analyzeButton.disabled = false;
      }
    });

    confirmButton.addEventListener("click", async () => {
      if (!preview?.preview_id) return;
      confirmButton.disabled = true;
      setStatus("Adding files and handing checks to the background...");
      try {
        const response = await fetch(`${baseUrl}/project/github-import/confirm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ overleafProjectId: projectId, previewId: preview.preview_id })
        });
        const progress = await response.json().catch(() => ({}));
        if (!response.ok) throw companionRequestError(response, progress);
        startGithubImportTracking({ progress, preview, targets, projectId, baseUrl });
        close();
      } catch (error) {
        setStatus(errorText(error), true);
        confirmButton.disabled = false;
      }
    });

    urlInput.focus();
  }

  function companionRequestError(response, payload = {}) {
    const error = new Error(payload?.message || `Companion returned HTTP ${response?.status}.`);
    error.name = "CompanionRequestError";
    error.code = String(payload?.error || "");
    error.status = Number(response?.status) || 0;
    return error;
  }

  // Load the pure pane helpers once. The pane is only built on user click (well
  // after startup), so a lazy import here always resolves before any render runs.
  async function ensureLeanPaneView() {
    if (leanPaneView) return leanPaneView;
    leanPaneView = await import(chrome.runtime.getURL("leanPaneView.mjs"));
    return leanPaneView;
  }

  // Lazily load the blueprint graph renderer (imports the shared, mirrored
  // blueprintLayout.mjs). Only pulled in the first time the Blueprint tab is opened.
  async function ensureBlueprintPaneView() {
    if (leanPaneBlueprintView) return leanPaneBlueprintView;
    leanPaneBlueprintView = await import(chrome.runtime.getURL("blueprintPaneView.mjs"));
    return leanPaneBlueprintView;
  }

  // The Items | Blueprint segmented control in the pane header.
  function buildLeanPaneViewTabs() {
    const tabs = document.createElement("div");
    tabs.className = "ol-lean-pane-viewtabs";
    tabs.setAttribute("role", "tablist");
    const make = (view, label) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ol-lean-pane-viewtab";
      button.textContent = label;
      button.setAttribute("role", "tab");
      button.addEventListener("click", () => {
        setLeanPaneMainView(view).catch(renderLeanPaneError);
      });
      return button;
    };
    const items = make("items", "Items");
    const blueprint = make("blueprint", "Blueprint");
    tabs.appendChild(items);
    tabs.appendChild(blueprint);
    leanPaneBlueprintToggle = { items, blueprint };
    updateLeanPaneViewTabs();
    return tabs;
  }

  function updateLeanPaneViewTabs() {
    if (!leanPaneBlueprintToggle) return;
    for (const [view, button] of Object.entries(leanPaneBlueprintToggle)) {
      const active = leanPaneMainView === view;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    }
  }

  // Switch the pane's top-level view. Items re-renders from the cached manifest when
  // one exists (no refetch); Blueprint fetches + renders its graph.
  async function setLeanPaneMainView(view) {
    if (view === leanPaneMainView || !leanPane) return;
    leanPaneMainView = view;
    updateLeanPaneViewTabs();
    if (view === "blueprint") {
      closeLeanPaneChat();
      closeActiveOverflowMenu();
      await renderLeanPaneBlueprint({});
      return;
    }
    // Back to Items: cheap re-render from cache, else a fresh refresh.
    if (lastLeanPaneManifest) {
      renderLeanPaneManifest(lastLeanPaneManifest);
      scheduleLeanPanePollIfNeeded(lastLeanPaneManifest);
    } else {
      await refreshLeanPaneNow({ forceFetch: false });
    }
  }

  // Fetch the project's blueprint graph from the companion and render it (or the
  // appropriate empty/error state). Guards against a view switch mid-fetch.
  async function renderLeanPaneBlueprint({ background = false } = {}) {
    if (!leanPane || !leanPaneBody || !leanPaneStatus) return;
    if (leanPaneMainView !== "blueprint") return;
    await ensureBlueprintPaneView();
    if (!background) {
      leanPaneStatus.textContent = "Loading blueprint…";
      leanPaneBody.replaceChildren();
    }

    const projectId = extractOverleafProjectId();
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    try {
      const identity = await loadProjectIdentity({ baseUrl, projectId });
      lastProjectIdentity = identity;
      renderLeanPaneProjectIdentity(identity);
    } catch {}

    let payload;
    try {
      const response = await fetch(
        `${baseUrl}/project/graph?overleafProjectId=${encodeURIComponent(projectId)}`,
      );
      payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
      }
    } catch (error) {
      if (leanPaneMainView !== "blueprint" || !leanPaneBody) return;
      leanPaneStatus.textContent = "Blueprint unavailable.";
      leanPaneBody.replaceChildren(buildBlueprintToolbar(), buildBlueprintMessage(errorText(error), "error"));
      return;
    }

    if (leanPaneMainView !== "blueprint") return; // toggled away mid-fetch
    leanPaneBlueprintGraph = payload;
    renderBlueprintBody(payload);
  }

  // Render the cached graph payload into the body: no-project / empty / populated.
  // Always leads with the Refresh + Generate toolbar. Called on fetch and on every
  // node-selection change (cheap, self-contained).
  function renderBlueprintBody(payload) {
    if (!leanPaneBody || !leanPaneStatus || leanPaneMainView !== "blueprint") return;
    const nodes = Array.isArray(payload?.nodes) ? payload.nodes : [];
    const edges = Array.isArray(payload?.edges) ? payload.edges : [];
    const prevScrollTop = leanPaneBody.scrollTop;
    leanPaneBody.replaceChildren(buildBlueprintToolbar());

    if (payload && payload.exists === false) {
      leanPaneStatus.textContent = "No Lea project yet.";
      leanPaneBody.appendChild(
        buildBlueprintMessage(
          "No Lea project for this document yet — formalize a theorem to start one.",
          "empty",
        ),
      );
      return;
    }
    if (nodes.length === 0) {
      leanPaneStatus.textContent = "Blueprint is empty.";
      leanPaneBody.appendChild(
        buildBlueprintMessage(
          "No blueprint nodes yet. Click “Generate from formalized theorems” above to build a starter graph from what you've formalized — or add nodes in the Lea UI.",
          "empty",
        ),
      );
      return;
    }

    leanPaneStatus.textContent = `${nodes.length} blueprint node${nodes.length === 1 ? "" : "s"}.`;
    const element = leanPaneBlueprintView.renderBlueprintView(
      { nodes, edges },
      {
        selectedKey: leanPaneBlueprintSelectedKey,
        // The renderer updates selection in place; we only persist it so the choice
        // survives a full re-render (refresh / generate).
        onSelectNode: (key) => {
          leanPaneBlueprintSelectedKey = key;
        },
      },
    );
    leanPaneBody.appendChild(element);
    leanPaneBody.scrollTop = prevScrollTop;
  }

  // The blueprint body's action row: Refresh (re-fetch the graph) + Generate
  // (populate .lea/blueprint.md from formalized artifacts). Present in every state.
  function buildBlueprintToolbar() {
    const bar = document.createElement("div");
    bar.className = "ol-lean-blueprint-toolbar";

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "ol-lean-pane-action";
    refresh.textContent = "Refresh";
    refresh.title = "Re-fetch the blueprint graph";
    refresh.addEventListener("click", () => {
      renderLeanPaneBlueprint({}).catch(renderLeanPaneError);
    });

    const generate = document.createElement("button");
    generate.type = "button";
    generate.className = "ol-lean-pane-action is-primary";
    generate.textContent = "Generate from formalized theorems";
    generate.title = "Add a blueprint node for each formalized theorem (safe to re-run)";
    generate.addEventListener("click", () => {
      generateBlueprint().catch(renderLeanPaneError);
    });
    leanPaneBlueprintGenerateBtn = generate;

    bar.appendChild(refresh);
    bar.appendChild(generate);
    return bar;
  }

  // POST the generate request, then render the returned graph and report what changed.
  async function generateBlueprint() {
    if (!leanPane || !leanPaneBody || !leanPaneStatus || leanPaneMainView !== "blueprint") return;
    if (leanPaneBlueprintGenerateBtn) {
      leanPaneBlueprintGenerateBtn.disabled = true;
      leanPaneBlueprintGenerateBtn.textContent = "Generating…";
    }
    leanPaneStatus.textContent = "Generating blueprint from formalized theorems…";

    const projectId = extractOverleafProjectId();
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    let payload;
    try {
      const response = await fetch(`${baseUrl}/project/blueprint/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overleafProjectId: projectId }),
      });
      payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
      }
    } catch (error) {
      if (leanPaneMainView !== "blueprint" || !leanPaneBody) return;
      leanPaneStatus.textContent = "Blueprint unavailable.";
      leanPaneBody.replaceChildren(buildBlueprintToolbar(), buildBlueprintMessage(errorText(error), "error"));
      return;
    }

    if (leanPaneMainView !== "blueprint") return;
    leanPaneBlueprintGraph = payload;
    leanPaneBlueprintSelectedKey = null;
    renderBlueprintBody(payload); // rebuilds the toolbar (button re-enabled) + graph

    // Overlay a result message over the node count renderBlueprintBody just set.
    if (payload.exists === false) {
      leanPaneStatus.textContent = "No Lea project for this document yet.";
    } else if (payload.added > 0) {
      leanPaneStatus.textContent = `Added ${payload.added} node${payload.added === 1 ? "" : "s"} from formalized theorems.`;
    } else if (Array.isArray(payload.nodes) && payload.nodes.length > 0) {
      leanPaneStatus.textContent = "Blueprint already covers your formalized theorems.";
    } else {
      leanPaneStatus.textContent = "No formalized theorems to generate from yet.";
    }
  }

  // A centered message block for the blueprint's empty / error states.
  function buildBlueprintMessage(text, kind) {
    const wrap = document.createElement("div");
    wrap.className = `ol-lean-blueprint-message${kind ? ` is-${kind}` : ""}`;
    const line = document.createElement("p");
    line.textContent = text;
    wrap.appendChild(line);
    return kind === "error"
      ? makeLeanPaneErrorDismissible(wrap, {
          onDismiss: () => {
            if (leanPaneStatus) leanPaneStatus.textContent = "Blueprint error dismissed. Refresh to try again.";
          }
        })
      : wrap;
  }

  // Edits to the open document re-render the pane from the cached file set with the
  // live buffer overlaid — a cheap, no-blink background refresh (no project download).
  function scheduleLeanPaneRefresh() {
    clearTimeout(leanPaneRefreshTimer);
    leanPaneRefreshTimer = setTimeout(() => {
      refreshLeanPaneNow({ background: true }).catch(renderLeanPaneError);
    }, LEAN_PANE_REFRESH_DELAY_MS);
  }

  // `forceFetch` re-downloads the project archive; `background` skips the blanking
  // "Loading…" state and preserves scroll, for edit-driven and poll refreshes.
  async function refreshLeanPaneNow({ forceFetch = false, background = false } = {}) {
    if (!leanPane || !leanPaneBody || !leanPaneStatus) return;
    clearTimeout(leanPaneRefreshTimer);
    leanPaneRefreshTimer = null;
    clearTimeout(leanPanePollTimer);
    leanPanePollTimer = null;
    // Blueprint view has its own (Items-independent) fetch + render — no file
    // archive, manifest, or in-progress poll. The blueprint is derived from
    // .lea/blueprint.md + Lean state, NOT the .tex buffer, so edit-/poll-driven
    // background refreshes can't change it — skip them. Explicit refreshes (the
    // header ↻ and initial open, both non-background) still re-fetch.
    if (leanPaneMainView === "blueprint") {
      if (!background) await renderLeanPaneBlueprint({});
      return;
    }
    await ensureLeanPaneView();
    if (!background) {
      leanPaneStatus.textContent = "Loading project inventory...";
      leanPaneBody.replaceChildren();
    }

    const projectId = extractOverleafProjectId();
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");

    // Identity and the Overleaf archive are independent. Fetching them in
    // parallel prevents a slow project download from leaving the header at its
    // placeholder namespace, and both requests are bounded so pane startup can
    // always settle into either content or a useful error state.
    loadProjectIdentity({ baseUrl, projectId })
      .then((identity) => {
        lastProjectIdentity = identity;
        renderLeanPaneProjectIdentity(identity);
      })
      .catch(() => {});
    const files = await getLeanPaneProjectFiles({
      projectId,
      forceFetch,
      // Never make the user wait for Overleaf to prepare a ZIP. The live or
      // cached sources are enough to render useful pane content immediately;
      // the complete inventory hydrates and re-renders in the background.
      deferArchiveFetch: !background
    });
    const { response, payload } = await fetchJsonWithTimeout(`${baseUrl}/lean-pane/manifest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        overleafProjectId: projectId,
        activePath: latestActiveTexPath || "",
        files
      })
    }, {
      timeoutMs: LEAN_PANE_COMPANION_TIMEOUT_MS,
      timeoutMessage: "The Lea companion timed out while loading the project inventory."
    });
    if (!response.ok) {
      throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    }
    // Personal-approval storage is presentation metadata, not project
    // inventory. Chrome storage can occasionally be slow to wake after an
    // extension reload; never hold the entire pane behind it. Reconcile and
    // repaint when it settles.
    reconcileHumanApprovals(
      (payload.items || []).map((item) => ({
        target: paneItemApprovalTarget(item),
        statusInfo: item
      }))
    ).then((changed) => {
      if (
        changed
        &&
        leanPane
        && leanPaneMainView === "items"
        && lastLeanPaneManifest
        && lastLeanPaneManifestProjectId === projectId
      ) {
        renderLeanPaneManifest(lastLeanPaneManifest);
      }
    }).catch(() => {});
    renderLeanPaneManifest(payload);
    scheduleLeanPanePollIfNeeded(payload);
  }

  // Keep refreshing while any item is still being formalized; stop once it settles.
  function scheduleLeanPanePollIfNeeded(manifest) {
    if (!leanPane || !leanPaneView?.hasInProgressItems(manifest?.items)) return;
    clearTimeout(leanPanePollTimer);
    leanPanePollTimer = setTimeout(() => {
      refreshLeanPaneNow({ background: true }).catch(renderLeanPaneError);
    }, pushConnected ? LEAN_PANE_POLL_RECONCILE_MS : LEAN_PANE_POLL_DELAY_MS);
  }

  async function getLeanPaneProjectFiles({ projectId, forceFetch, deferArchiveFetch = false }) {
    if (!projectId || projectId === "unknown") {
      leanPaneInventoryWarning = "";
      return latestActiveTexPath && typeof latestActiveTex === "string"
        ? [{ path: latestActiveTexPath, content: latestActiveTex }]
        : [];
    }
    const needFetch = leanPaneView.shouldRefetchLeanPaneFiles({
      forceFetch,
      lastFiles: lastLeanPaneFiles,
      lastProjectId: lastLeanPaneProjectId,
      projectId
    });
    let files;
    if (needFetch && deferArchiveFetch) {
      files = fallbackLeanPaneProjectFiles(projectId);
      leanPaneInventoryWarning = "Loading the full Overleaf project inventory in the background.";
      queueLeanPaneArchiveRefresh(projectId);
    } else if (needFetch) {
      try {
        files = await loadLeanPaneArchive(projectId);
        leanPaneInventoryWarning = "";
      } catch {
        files = fallbackLeanPaneProjectFiles(projectId);
        leanPaneInventoryWarning = leanPaneArchiveUnavailableMessage(files);
      }
    } else {
      files = lastLeanPaneFiles.map((file) => ({ ...file }));
      leanPaneView.overlayActiveTex(files, latestActiveTexPath, latestActiveTex);
    }
    lastLeanPaneFiles = files.map((file) => ({ ...file }));
    lastLeanPaneProjectId = projectId;
    return files;
  }

  function leanPaneArchiveUnavailableMessage(files) {
    return files.length > 1
      ? "The Overleaf archive was unavailable; showing cached and live TeX files."
      : files.length === 1
        ? "The Overleaf archive was unavailable; showing the open TeX file."
        : "The Overleaf archive was unavailable. Use Refresh to try again.";
  }

  function loadLeanPaneArchive(projectId) {
    if (leanPaneArchiveLoad?.projectId === projectId) return leanPaneArchiveLoad.promise;
    const entry = { projectId, promise: null };
    entry.promise = collectProjectTexFiles(projectId, { timeoutMs: LEAN_PANE_ARCHIVE_TIMEOUT_MS })
      .finally(() => {
        if (leanPaneArchiveLoad === entry) leanPaneArchiveLoad = null;
      });
    leanPaneArchiveLoad = entry;
    return entry.promise;
  }

  function queueLeanPaneArchiveRefresh(projectId) {
    loadLeanPaneArchive(projectId)
      .then((files) => {
        if (extractOverleafProjectId() !== projectId) return;
        lastLeanPaneFiles = files.map((file) => ({ ...file }));
        lastLeanPaneProjectId = projectId;
        leanPaneInventoryWarning = "";
        if (leanPane && leanPaneMainView === "items") {
          refreshLeanPaneNow({ background: true }).catch(renderLeanPaneError);
        }
      })
      .catch(() => {
        if (extractOverleafProjectId() !== projectId || lastLeanPaneProjectId !== projectId) return;
        leanPaneInventoryWarning = leanPaneArchiveUnavailableMessage(lastLeanPaneFiles || []);
        if (
          leanPane
          && leanPaneMainView === "items"
          && lastLeanPaneManifest
          && lastLeanPaneManifestProjectId === projectId
        ) {
          renderLeanPaneManifest(lastLeanPaneManifest);
        }
      });
  }

  function fallbackLeanPaneProjectFiles(projectId) {
    const files = lastLeanPaneProjectId === projectId && Array.isArray(lastLeanPaneFiles)
      ? lastLeanPaneFiles.map((file) => ({ ...file }))
      : [];
    const activeBelongsToProject = !latestActiveTexProjectId || latestActiveTexProjectId === projectId;
    const activePath = String(latestActiveTexPath || "").replace(/^\/+/, "");
    if (!activeBelongsToProject || !activePath || typeof latestActiveTex !== "string") return files;
    const active = files.find((file) => file.path === activePath);
    if (active) active.content = latestActiveTex;
    else files.push({ path: activePath, content: latestActiveTex });
    return files;
  }

  function renderLeanPaneManifest(manifest) {
    if (!leanPaneBody || !leanPaneStatus) return;
    manifest = githubImportPaneOverlay(manifest);
    const prevScrollTop = leanPaneBody.scrollTop;
    const items = Array.isArray(manifest?.items) ? manifest.items : [];
    const tree = leanPaneView.buildLeanPaneTree(items);
    const useRelationships = leanPaneView.buildPaneUseRelationships(items);
    const fileCount = tree.files.length;
    lastLeanPaneManifest = manifest || null;
    lastLeanPaneManifestProjectId = extractOverleafProjectId();
    prepareLeanPaneTreeExpansion(manifest, tree);
    leanPaneBody.replaceChildren();
    const inventorySummary = items.length
      ? `${items.length} labeled item${items.length === 1 ? "" : "s"} across ${fileCount} .tex file${fileCount === 1 ? "" : "s"}.`
      : "No labeled theorem, lemma, proposition, corollary, or definition environments found.";
    leanPaneStatus.textContent = leanPaneInventoryWarning
      ? `${inventorySummary} ${leanPaneInventoryWarning}`
      : inventorySummary;

    if (Array.isArray(manifest?.diagnostics) && manifest.diagnostics.length > 0) {
      const visibleDiagnostics = manifest.diagnostics.slice(0, 4);
      const dismissKey = leanPaneErrorKey(
        "diagnostics",
        ...visibleDiagnostics.map((diagnostic) => diagnostic.message || diagnostic.code || "Lean pane diagnostic")
      );
      const diagnostics = document.createElement("div");
      diagnostics.className = "ol-lean-project-pane-diagnostics";
      for (const diagnostic of visibleDiagnostics) {
        const line = document.createElement("p");
        line.textContent = diagnostic.message || diagnostic.code || "Lean pane diagnostic";
        diagnostics.appendChild(line);
      }
      if (!dismissedLeanPaneErrorKeys.has(dismissKey)) {
        leanPaneBody.appendChild(makeLeanPaneErrorDismissible(diagnostics, {
          errorKey: dismissKey,
          label: "Dismiss Lean pane diagnostics"
        }));
      }
    }

    const repairBatchPanel = renderLeanPaneRepairBatchPanel();
    if (repairBatchPanel) leanPaneBody.appendChild(repairBatchPanel);

    const batchActions = renderLeanPaneBatchActions(items);
    if (batchActions) leanPaneBody.appendChild(batchActions);

    if (items.length > 0) {
      const treeElement = document.createElement("div");
      treeElement.className = "ol-lean-project-tree";
      for (const node of tree.children) {
        treeElement.appendChild(renderLeanPaneTreeNode(node, 0, manifest, useRelationships));
      }
      leanPaneBody.appendChild(treeElement);
    }

    leanPaneBody.scrollTop = prevScrollTop;
  }

  function renderLeanPaneProjectIdentity(identity) {
    if (!leanPaneProjectTitle || !leanPaneProjectNamespace) return;
    const fallback = guessProjectName(lastLeanPaneFiles || []);
    leanPaneProjectTitle.textContent = identity?.projectName || fallback;
    leanPaneProjectNamespace.textContent = identity?.namespace || "Namespace not created yet";
  }

  async function loadProjectIdentity({ baseUrl, projectId }) {
    const { response, payload } = await fetchJsonWithTimeout(
      `${baseUrl}/project/identity?overleafProjectId=${encodeURIComponent(projectId)}`,
      {},
      {
        timeoutMs: LEAN_PANE_COMPANION_TIMEOUT_MS,
        timeoutMessage: "The Lea companion timed out while loading the project identity."
      }
    );
    if (!response.ok) throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    return payload.identity || null;
  }

  async function fetchJsonWithTimeout(url, options = {}, { timeoutMs, timeoutMessage }) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeoutId = controller
      ? setTimeout(() => controller.abort(), timeoutMs)
      : null;
    try {
      const response = await fetch(url, controller ? { ...options, signal: controller.signal } : options);
      const payload = await response.json().catch(() => ({}));
      return { response, payload };
    } catch (error) {
      if (controller?.signal.aborted) throw new Error(timeoutMessage);
      throw error;
    } finally {
      if (timeoutId != null) clearTimeout(timeoutId);
    }
  }

  async function previewProjectIdentity({ baseUrl, projectId, projectName, namespace = "", excludeProjectId = "" }) {
    const response = await fetch(`${baseUrl}/project/identity/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overleafProjectId: projectId, projectName, namespace, excludeProjectId })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    return payload;
  }

  async function saveProjectIdentity({ baseUrl, projectId, projectName, mode, namespace = "", expectedNamespace = "", createIfMissing = false }) {
    const response = await fetch(`${baseUrl}/project/identity`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ overleafProjectId: projectId, projectName, mode, namespace, expectedNamespace, createIfMissing })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    return payload;
  }

  function guessProjectName(files = []) {
    for (const file of files || []) {
      const match = String(file?.content || "").match(/\\title\s*\{([^}]*)\}/);
      if (match?.[1]?.trim()) return match[1].trim();
    }
    const title = String(document.title || "").replace(/\s*-\s*Overleaf\s*$/i, "").trim();
    return title || "Overleaf Project";
  }

  function renderProjectIdentityFeedback({ source = "lean-pane", popover = null, message = "", kind = "info" } = {}) {
    const text = String(message || "");
    if (source === "lean-pane" && leanPaneStatus) {
      leanPaneStatus.textContent = text;
    }
    const projectMessage = popover?.querySelector("[data-role='project-message']");
    if (projectMessage) {
      projectMessage.textContent = text;
      projectMessage.dataset.kind = text ? kind : "";
    }
  }

  function createProjectIdentityElement(tagName, className = "", text = "") {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }

  function closeProjectIdentityEditor({ restoreFocus = true } = {}) {
    clearTimeout(projectIdentityPreviewTimer);
    projectIdentityPreviewTimer = null;
    const trigger = projectIdentityEditorState?.trigger;
    projectIdentityDialog?.remove();
    projectIdentityDialog = null;
    projectIdentityEditorState = null;
    if (restoreFocus && trigger?.isConnected) trigger.focus({ preventScroll: true });
  }

  function buildProjectIdentityEditor({ source, popover, trigger, baseUrl, projectId, identity }) {
    closeProjectIdentityEditor({ restoreFocus: false });

    const currentName = String(identity?.projectName || guessProjectName(lastLeanPaneFiles || [])).trim();
    const currentNamespace = String(identity?.namespace || "").trim();
    const shell = createProjectIdentityElement("div", "ol-lean-project-identity-backdrop");
    const dialog = createProjectIdentityElement("section", "ol-lean-project-identity-dialog");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "ol-lean-project-identity-title");
    dialog.setAttribute("aria-describedby", "ol-lean-project-identity-description");

    const header = createProjectIdentityElement("header", "ol-lean-project-identity-header");
    const mark = createProjectIdentityElement("span", "ol-lean-project-identity-mark", "∑");
    mark.setAttribute("aria-hidden", "true");
    const heading = createProjectIdentityElement("div", "ol-lean-project-identity-heading");
    heading.appendChild(createProjectIdentityElement("p", "ol-lean-project-identity-kicker", "Project identity"));
    const title = createProjectIdentityElement("h2", "", "Rename project");
    title.id = "ol-lean-project-identity-title";
    const description = createProjectIdentityElement(
      "p",
      "ol-lean-project-identity-description",
      "Choose the name shown in Lea and preview how it maps to your Lean namespace."
    );
    description.id = "ol-lean-project-identity-description";
    heading.appendChild(title);
    heading.appendChild(description);
    const close = createProjectIdentityElement("button", "ol-lean-icon-button ol-lean-project-identity-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close project rename dialog");
    header.appendChild(mark);
    header.appendChild(heading);
    header.appendChild(close);

    const form = createProjectIdentityElement("form", "ol-lean-project-identity-form");
    const field = createProjectIdentityElement("label", "ol-lean-project-identity-field");
    const fieldLabel = createProjectIdentityElement("span", "ol-lean-project-identity-label", "Project name");
    const input = createProjectIdentityElement("input", "ol-lean-project-identity-input");
    input.type = "text";
    input.value = currentName;
    input.autocomplete = "off";
    input.spellcheck = true;
    input.maxLength = 160;
    input.setAttribute("aria-describedby", "ol-lean-project-identity-status");
    field.appendChild(fieldLabel);
    field.appendChild(input);

    const previewCard = createProjectIdentityElement("section", "ol-lean-project-identity-preview");
    previewCard.setAttribute("aria-label", "Project identity preview");
    previewCard.appendChild(createProjectIdentityElement("p", "ol-lean-project-identity-preview-title", "Preview"));

    const nameRow = createProjectIdentityElement("div", "ol-lean-project-identity-preview-row");
    nameRow.appendChild(createProjectIdentityElement("span", "", "Display name"));
    const nameValue = createProjectIdentityElement("strong", "ol-lean-project-identity-name-value", currentName);
    nameRow.appendChild(nameValue);
    previewCard.appendChild(nameRow);

    const namespaceRow = createProjectIdentityElement("div", "ol-lean-project-identity-preview-row");
    namespaceRow.appendChild(createProjectIdentityElement("span", "", "Lean namespace"));
    const namespaceValue = createProjectIdentityElement("code", "ol-lean-project-identity-namespace-value", currentNamespace || "—");
    namespaceRow.appendChild(namespaceValue);
    previewCard.appendChild(namespaceRow);

    const sync = createProjectIdentityElement("label", "ol-lean-project-identity-sync");
    const syncCheckbox = createProjectIdentityElement("input", "ol-lean-project-identity-sync-input");
    syncCheckbox.type = "checkbox";
    syncCheckbox.checked = true;
    const syncTrack = createProjectIdentityElement("span", "ol-lean-project-identity-sync-track");
    syncTrack.setAttribute("aria-hidden", "true");
    const syncCopy = createProjectIdentityElement("span", "ol-lean-project-identity-sync-copy");
    syncCopy.appendChild(createProjectIdentityElement("strong", "", "Keep Lean namespace in sync"));
    const syncDetail = createProjectIdentityElement("small", "", "Lea will update the namespace to match the new name.");
    syncCopy.appendChild(syncDetail);
    sync.appendChild(syncCheckbox);
    sync.appendChild(syncTrack);
    sync.appendChild(syncCopy);
    previewCard.appendChild(sync);

    const impact = createProjectIdentityElement("p", "ol-lean-project-identity-impact");
    previewCard.appendChild(impact);
    const suggestions = createProjectIdentityElement("div", "ol-lean-project-identity-suggestions");
    suggestions.hidden = true;
    previewCard.appendChild(suggestions);

    const status = createProjectIdentityElement("p", "ol-lean-project-identity-status");
    status.id = "ol-lean-project-identity-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    const actions = createProjectIdentityElement("footer", "ol-lean-project-identity-actions");
    const cancel = createProjectIdentityElement("button", "ol-lean-secondary-button", "Cancel");
    cancel.type = "button";
    const save = createProjectIdentityElement("button", "ol-lean-primary-button ol-lean-project-identity-save", "Save changes");
    save.type = "submit";
    save.disabled = true;
    actions.appendChild(cancel);
    actions.appendChild(save);

    form.appendChild(field);
    form.appendChild(previewCard);
    form.appendChild(status);
    form.appendChild(actions);
    dialog.appendChild(header);
    dialog.appendChild(form);
    shell.appendChild(dialog);
    document.body.appendChild(shell);

    const state = {
      source,
      popover,
      trigger,
      baseUrl,
      projectId,
      identity,
      currentName,
      currentNamespace,
      shell,
      dialog,
      input,
      nameValue,
      namespaceValue,
      sync,
      syncCheckbox,
      syncDetail,
      impact,
      suggestions,
      status,
      cancel,
      save,
      preview: null,
      previewError: "",
      requestedNamespace: "",
      previewRequest: 0,
      saving: false
    };
    projectIdentityDialog = shell;
    projectIdentityEditorState = state;

    const closeEditor = () => closeProjectIdentityEditor();
    close.addEventListener("click", closeEditor);
    cancel.addEventListener("click", closeEditor);
    shell.addEventListener("click", (event) => {
      event.stopPropagation();
      if (event.target === shell) closeEditor();
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeEditor();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [input, syncCheckbox, cancel, save, close].filter((element) => !element.disabled && !element.hidden);
      if (focusable.length === 0) return;
      const index = focusable.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) {
        event.preventDefault();
        focusable[focusable.length - 1].focus();
      } else if (!event.shiftKey && index === focusable.length - 1) {
        event.preventDefault();
        focusable[0].focus();
      }
    });
    input.addEventListener("input", () => scheduleProjectIdentityPreview(state));
    syncCheckbox.addEventListener("change", () => renderProjectIdentityEditor(state));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveProjectIdentityEditor(state).catch(() => {});
    });
    // The direct listener keeps the lightweight DOM test harness faithful; in
    // Chrome the form submit listener above is the normal path.
    save.addEventListener("click", (event) => {
      event.preventDefault();
      saveProjectIdentityEditor(state).catch(() => {});
    });

    renderProjectIdentityEditor(state);
    input.focus({ preventScroll: true });
    input.select?.();
  }

  function scheduleProjectIdentityPreview(state) {
    if (state !== projectIdentityEditorState) return;
    clearTimeout(projectIdentityPreviewTimer);
    projectIdentityPreviewTimer = null;
    state.preview = null;
    state.previewError = "";
    state.requestedNamespace = "";
    state.previewRequest += 1;
    renderProjectIdentityEditor(state);
    const name = String(state.input.value || "").trim();
    if (!name || name === state.currentName) return;
    projectIdentityPreviewTimer = setTimeout(() => {
      projectIdentityPreviewTimer = null;
      refreshProjectIdentityPreview(state).catch(() => {});
    }, 220);
  }

  async function refreshProjectIdentityPreview(state) {
    if (state !== projectIdentityEditorState) return;
    const name = String(state.input.value || "").trim();
    if (!name || name === state.currentName) return;
    const request = ++state.previewRequest;
    state.preview = null;
    state.previewError = "";
    renderProjectIdentityEditor(state);
    try {
      const preview = await previewProjectIdentity({
        baseUrl: state.baseUrl,
        projectId: state.projectId,
        projectName: name,
        namespace: state.requestedNamespace,
        excludeProjectId: state.identity?.projectId || ""
      });
      if (state !== projectIdentityEditorState || request !== state.previewRequest) return;
      state.preview = preview;
    } catch (error) {
      if (state !== projectIdentityEditorState || request !== state.previewRequest) return;
      state.previewError = normalizeErrorMessage(error);
    }
    renderProjectIdentityEditor(state);
  }

  function chooseProjectIdentityNamespace(state, namespace) {
    if (state !== projectIdentityEditorState) return;
    state.requestedNamespace = String(namespace || "");
    state.preview = null;
    state.previewError = "";
    refreshProjectIdentityPreview(state).catch(() => {});
  }

  function renderProjectIdentityEditor(state) {
    if (state !== projectIdentityEditorState) return;
    const name = String(state.input.value || "").trim();
    const changed = Boolean(name && name !== state.currentName);
    const preview = state.preview;
    const previewNamespace = String(preview?.namespace || "");
    const namespace = previewNamespace || (changed ? "" : state.currentNamespace);
    const namespaceChanges = Boolean(namespace && namespace !== state.currentNamespace);
    const projectExists = Boolean(state.identity?.exists);
    const checking = changed && !preview && !state.previewError;

    state.nameValue.textContent = name || "Untitled project";
    state.namespaceValue.textContent = namespace || (checking ? "Checking…" : state.currentNamespace || "—");
    state.namespaceValue.dataset.loading = checking ? "true" : "false";
    state.sync.hidden = !changed;
    state.syncCheckbox.disabled = !projectExists;
    if (!projectExists) state.syncCheckbox.checked = true;

    if (!changed) {
      state.syncDetail.textContent = "Lea will update the namespace to match the new name.";
      state.impact.textContent = "Enter a new project name to see its Lean namespace.";
    } else if (!state.syncCheckbox.checked) {
      state.syncDetail.textContent = `Lean files will stay in ${state.currentNamespace || "their current namespace"}.`;
      state.impact.textContent = "Only the display name will change; proof paths and imports are untouched.";
    } else if (checking) {
      state.syncDetail.textContent = "Lea is checking the matching namespace.";
      state.impact.textContent = "Previewing the project identity…";
    } else if (namespaceChanges && state.identity?.hasRecordedProofs) {
      state.syncDetail.textContent = `${state.currentNamespace || "Current namespace"} → ${namespace}`;
      state.impact.textContent = "Lea will migrate recorded proof files and keep their history attached to this project.";
    } else if (namespaceChanges) {
      state.syncDetail.textContent = `${state.currentNamespace || "Current namespace"} → ${namespace}`;
      state.impact.textContent = "New Lean artifacts will use the previewed namespace.";
    } else {
      state.syncDetail.textContent = namespace ? `Lean files will remain in ${namespace}.` : "Lea will keep the current namespace.";
      state.impact.textContent = "The display name changes without moving Lean files.";
    }

    state.suggestions.replaceChildren();
    const suggestionValues = preview?.available === false ? (preview.suggestions || []).slice(0, 3) : [];
    state.suggestions.hidden = suggestionValues.length === 0 || !state.syncCheckbox.checked;
    if (!state.suggestions.hidden) {
      state.suggestions.appendChild(createProjectIdentityElement("span", "", "Available alternatives"));
      for (const suggestion of suggestionValues) {
        const button = createProjectIdentityElement("button", "ol-lean-project-identity-suggestion", suggestion);
        button.type = "button";
        button.addEventListener("click", () => chooseProjectIdentityNamespace(state, suggestion));
        state.suggestions.appendChild(button);
      }
    }

    let message = "";
    let kind = "";
    if (!name) {
      message = "Project name is required.";
      kind = "error";
    } else if (state.previewError && state.syncCheckbox.checked) {
      message = state.previewError;
      kind = "error";
    } else if (preview?.available === false && state.syncCheckbox.checked) {
      message = `${previewNamespace || "That namespace"} is already in use. Choose an alternative or turn off namespace sync.`;
      kind = "error";
    } else if (state.previewError && !state.syncCheckbox.checked) {
      message = "Namespace preview is unavailable, but you can still save the display name only.";
      kind = "info";
    } else if (!changed) {
      message = "Enter a different name to save changes.";
      kind = "info";
    } else if (checking) {
      message = "Checking namespace availability…";
      kind = "info";
    }
    state.status.textContent = message;
    state.status.dataset.kind = kind;

    const namespaceReady = !state.syncCheckbox.checked || Boolean(preview?.available);
    state.save.disabled = state.saving || !changed || !name || !namespaceReady;
    state.input.disabled = state.saving;
    state.syncCheckbox.disabled = state.saving || !projectExists;
    state.cancel.disabled = state.saving;
    state.save.textContent = state.saving ? "Saving…" : "Save changes";
  }

  async function saveProjectIdentityEditor(state) {
    if (state !== projectIdentityEditorState || state.saving || state.save.disabled) return false;
    const projectName = String(state.input.value || "").trim();
    const preview = state.preview;
    const migrate = Boolean(
      state.syncCheckbox.checked &&
      preview?.available &&
      preview.namespace &&
      preview.namespace !== state.currentNamespace
    );
    state.saving = true;
    state.previewError = "";
    renderProjectIdentityEditor(state);
    let result;
    try {
      result = await saveProjectIdentity({
        baseUrl: state.baseUrl,
        projectId: state.projectId,
        projectName,
        mode: migrate ? "rename-namespace" : "display-only",
        namespace: migrate ? preview.namespace : "",
        expectedNamespace: state.currentNamespace,
        createIfMissing: true
      });
    } catch (error) {
      if (state !== projectIdentityEditorState) return false;
      state.saving = false;
      state.previewError = normalizeErrorMessage(error);
      renderProjectIdentityEditor(state);
      return false;
    }
    if (state !== projectIdentityEditorState) return false;
    lastProjectIdentity = result.identity || null;
    renderLeanPaneProjectIdentity(lastProjectIdentity);
    if (state.popover) renderProjectSettingsSection(state.popover, lastProjectIdentity);
    const savedNamespace = result.identity?.namespace || state.currentNamespace || preview?.namespace || "";
    let message = !migrate && savedNamespace
      ? `Project name saved. Lean files still use namespace ${savedNamespace}.`
      : "Project name and Lean namespace saved.";
    let feedbackKind = "success";
    if (migrate && leanPane) {
      try {
        // The rename endpoint migrates the files synchronously. Re-fetch the
        // manifest before dismissing the dialog so already-rendered code uses
        // those rewritten working files immediately.
        if (leanPaneMainView === "blueprint") {
          // No Lean source is visible in Blueprint, but its cached Items view
          // would otherwise resurrect the pre-rename manifest when selected.
          lastLeanPaneManifest = null;
        } else {
          await refreshLeanPaneNow({ background: true });
        }
      } catch (error) {
        message += ` The Lean pane could not refresh: ${normalizeErrorMessage(error)}`;
        feedbackKind = "error";
      }
    }
    const { source, popover } = state;
    closeProjectIdentityEditor();
    renderProjectIdentityFeedback({ source, popover, message, kind: feedbackKind });
    return true;
  }

  async function openProjectIdentityEditor({ source = "lean-pane", popover = null, trigger = null } = {}) {
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    const projectId = extractOverleafProjectId();
    const identity = lastProjectIdentity || await loadProjectIdentity({ baseUrl, projectId });
    buildProjectIdentityEditor({ source, popover, trigger, baseUrl, projectId, identity });
    return true;
  }

  function prepareLeanPaneTreeExpansion(manifest, tree) {
    const key = [
      itemsProjectId(manifest?.items || [])
    ].join(":");
    if (key !== leanPaneTreeDefaultsKey) {
      leanPaneExpandedTreeNodeIds = new Set();
      leanPaneTreeDefaultsKey = key;
    }

    const liveIds = new Set();
    collectLeanPaneTreeNodeIds(tree.children, liveIds);
    for (const id of [...leanPaneExpandedTreeNodeIds]) {
      if (!liveIds.has(id)) leanPaneExpandedTreeNodeIds.delete(id);
    }
  }

  function itemsProjectId(items) {
    const item = Array.isArray(items) ? items.find((candidate) => candidate?.overleafProjectId) : null;
    return item?.overleafProjectId || extractOverleafProjectId() || "unknown";
  }

  function collectLeanPaneTreeNodeIds(nodes, ids) {
    for (const node of nodes || []) {
      ids.add(node.id);
      if (node.type === "folder") collectLeanPaneTreeNodeIds(node.children, ids);
    }
  }

  function renderLeanPaneTreeNode(node, depth, manifest, useRelationships) {
    const expanded = leanPaneExpandedTreeNodeIds.has(node.id);
    const section = document.createElement("section");
    section.className = `ol-lean-project-tree-node ol-lean-project-tree-node-${node.type}`;
    section.dataset.treeNodeId = node.id;

    const row = document.createElement("button");
    row.type = "button";
    row.className = `ol-lean-project-tree-row ol-lean-project-tree-row-${node.type}`;
    row.style.setProperty("--ol-tree-depth", String(depth));
    row.setAttribute("aria-expanded", String(expanded));
    row.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${node.type === "folder" ? "folder" : "file"} ${node.path || node.name}`);
    row.addEventListener("click", () => {
      toggleLeanPaneTreeNode(node.id);
      section.replaceWith(renderLeanPaneTreeNode(node, depth, manifest, useRelationships));
    });

    const disclosure = document.createElement("span");
    disclosure.className = "ol-lean-project-tree-disclosure";
    disclosure.textContent = expanded ? "▾" : "▸";
    row.appendChild(disclosure);

    const name = document.createElement("span");
    name.className = "ol-lean-project-tree-name";
    name.textContent = node.type === "folder" ? `${node.name}/` : node.name;
    row.appendChild(name);

    const count = document.createElement("span");
    count.className = "ol-lean-project-tree-count";
    count.textContent = `${node.itemCount} item${node.itemCount === 1 ? "" : "s"}`;
    row.appendChild(count);

    if (node.type === "file") {
      row.appendChild(renderLeanPaneFileProgress(node));
    } else {
      const chip = document.createElement("span");
      chip.className = `ol-lean-project-status ol-lean-project-tree-status ol-lean-project-status-${node.status || "unknown"}`;
      chip.textContent = leanPaneView.formatPaneStatus(node.status || "unknown");
      row.appendChild(chip);
    }
    section.appendChild(row);

    if (expanded) {
      const children = document.createElement("div");
      children.className = "ol-lean-project-tree-children";
      if (node.type === "folder") {
        for (const child of node.children) {
          children.appendChild(renderLeanPaneTreeNode(child, depth + 1, manifest, useRelationships));
        }
      } else {
        children.className = "ol-lean-project-tree-items";
        for (const item of node.items) {
          children.appendChild(renderLeanPaneItem(item, useRelationships));
        }
      }
      section.appendChild(children);
    }
    return section;
  }

  function renderLeanPaneFileProgress(node) {
    const summary = node.progress || leanPaneView.summarizePaneProgress(node.items || []);
    const progress = document.createElement("span");
    progress.className = `ol-lean-project-progress${summary.inProgress > 0 ? " ol-lean-project-progress-in-progress" : ""}`;
    progress.setAttribute("role", "img");
    progress.setAttribute("aria-label", leanPaneView.formatPaneProgressLabel(node.path || node.name, summary));

    for (const segment of leanPaneView.paneProgressSegments(summary)) {
      const element = document.createElement("span");
      element.className = `ol-lean-project-progress-segment ol-lean-project-progress-segment-${segment.id}`;
      element.style.setProperty("width", `${segment.percent}%`);
      element.dataset.bucket = segment.id;
      element.dataset.count = String(segment.count);
      element.dataset.percent = String(segment.percent);
      element.title = segment.title;
      progress.appendChild(element);
    }
    return progress;
  }

  function toggleLeanPaneTreeNode(id) {
    if (leanPaneExpandedTreeNodeIds.has(id)) {
      leanPaneExpandedTreeNodeIds.delete(id);
    } else {
      leanPaneExpandedTreeNodeIds.add(id);
    }
  }

  function renderLeanPaneItem(item, useRelationships) {
    const expanded = leanPaneExpandedItemIds.has(item.id);
    const card = document.createElement("section");
    card.className = `ol-lean-project-item ol-lean-project-item-${item.status || "unknown"}`;
    card.dataset.itemId = item.id || "";

    const headerRow = document.createElement("div");
    headerRow.className = "ol-lean-project-item-header-row";
    const header = document.createElement("button");
    header.type = "button";
    header.className = "ol-lean-project-item-header";
    header.setAttribute("aria-expanded", String(expanded));
    header.addEventListener("click", () => {
      if (leanPaneExpandedItemIds.has(item.id)) {
        leanPaneExpandedItemIds.delete(item.id);
      } else {
        leanPaneExpandedItemIds.add(item.id);
      }
      card.replaceWith(renderLeanPaneItem(item, useRelationships));
    });

    const text = document.createElement("span");
    text.className = "ol-lean-project-item-title";
    renderLeanPaneTitle(text, item);
    const meta = document.createElement("span");
    meta.className = "ol-lean-project-item-meta";
    meta.textContent = item.label;
    const chip = document.createElement("span");
    chip.className = `ol-lean-project-status ol-lean-project-status-${item.status || "unknown"}`;
    chip.textContent = leanPaneView.formatPaneStatus(item.status || "unknown");
    header.appendChild(text);
    header.appendChild(meta);
    header.appendChild(chip);
    // Same amber "!" the document overlay's badge shows for a proof whose
    // imports are currently sorry-stubbed -- the pane item and the doc badge
    // describe the same status object and must agree.
    if (getStubbedTheoremUses(item).length > 0) {
      header.appendChild(createStubbedTheoremUsesMark());
    }
    headerRow.appendChild(header);
    if (
      Object.prototype.hasOwnProperty.call(item, "approvalEligible")
      || Boolean(item.approvalRevision)
    ) {
      headerRow.appendChild(createHumanApprovalButton(paneItemApprovalTarget(item), item, { pane: true }));
    }
    card.appendChild(headerRow);

    const natural = document.createElement("p");
    natural.className = "ol-lean-project-natural";
    renderLeanPaneLatex(natural, item.naturalLanguageLatex || item.naturalLanguageRendered || "");
    card.appendChild(natural);

    if (item.githubImportPending) {
      const importState = document.createElement("p");
      importState.className = "ol-lean-project-import-state";
      importState.setAttribute("role", "status");
      importState.textContent = item.message || "Imported Lean proof is queued for checking.";
      card.appendChild(importState);
    }

    const relationships = renderLeanPaneUseRelationships(item, useRelationships);
    if (relationships) card.appendChild(relationships);

    if (item.status === "stale") {
      const staleNote = document.createElement("p");
      staleNote.className = "ol-lean-project-stale-note";
      staleNote.setAttribute("role", "status");
      staleNote.textContent = item.message
        || "Out of date — the LaTeX changed after this Lean artifact was generated. Re-formalize to synchronize it.";
      card.appendChild(staleNote);
    }

    if (getStubbedTheoremUses(item).length > 0) {
      const stubbedWarning = document.createElement("p");
      stubbedWarning.className = "ol-lean-project-impact-note";
      renderStubbedTheoremUsesWarning(stubbedWarning, item);
      card.appendChild(stubbedWarning);
    }

    if (item.leanStub) {
      card.appendChild(renderLeanCodeBlock("ol-lean-project-code", item.leanStub, "Copy stub"));
    } else {
      const missing = document.createElement("p");
      missing.className = "ol-lean-project-missing";
      missing.textContent = "No Lean stub has been generated yet.";
      card.appendChild(missing);
    }

    if (expanded) {
      card.appendChild(renderLeanPaneItemDetail(item));
    }
    return card;
  }

  function renderLeanPaneUseRelationships(item, useRelationships) {
    const uses = useRelationships?.usesByItem?.get(item) || [];
    const usedBy = useRelationships?.usedByItem?.get(item) || [];
    if (uses.length === 0 && usedBy.length === 0) return null;

    const container = document.createElement("div");
    container.className = "ol-lean-project-relationships";
    container.setAttribute("role", "group");
    container.setAttribute("aria-label", `Relationships for ${item.label || item.leanDeclarationName || "this item"}`);
    if (uses.length > 0) {
      container.appendChild(renderLeanPaneUseRelationshipRow("Uses", "→", "uses", uses));
    }
    if (usedBy.length > 0) {
      container.appendChild(renderLeanPaneUseRelationshipRow("Used by", "←", "used-by", usedBy));
    }
    return container;
  }

  function renderLeanPaneUseRelationshipRow(label, arrow, direction, relationships) {
    const row = document.createElement("div");
    row.className = "ol-lean-project-relationship-row";

    const heading = document.createElement("span");
    heading.className = "ol-lean-project-relationship-label";
    heading.textContent = label;
    row.appendChild(heading);

    const arrowElement = document.createElement("span");
    arrowElement.className = "ol-lean-project-relationship-arrow";
    arrowElement.setAttribute("aria-hidden", "true");
    arrowElement.textContent = arrow;
    row.appendChild(arrowElement);

    const list = document.createElement("div");
    list.className = "ol-lean-project-relationship-list";
    list.setAttribute("role", "group");
    list.setAttribute("aria-label", label);
    for (const relationship of relationships) {
      list.appendChild(renderLeanPaneUseRelationshipChip(relationship, direction));
    }
    row.appendChild(list);
    return row;
  }

  function renderLeanPaneUseRelationshipChip(relationship, direction) {
    const navigable = Boolean(relationship?.item);
    const element = document.createElement(navigable ? "button" : "span");
    if (navigable) element.type = "button";
    const status = String(relationship?.status || "unknown").toLowerCase().replace(/[^a-z0-9-]+/g, "-");
    element.className = [
      "ol-lean-project-relationship-chip",
      `ol-lean-project-relationship-chip-${status}`,
      navigable ? "is-navigable" : "is-unavailable",
      relationship?.resolution === "ambiguous" ? "is-ambiguous" : ""
    ].filter(Boolean).join(" ");
    element.dataset.relationshipDirection = direction;
    element.dataset.targetLabel = relationship?.label || "";
    element.textContent = relationship?.label || "";

    if (navigable) {
      const role = direction === "uses" ? "dependency" : "dependent";
      const state = leanPaneView.formatPaneStatus(relationship.status || "unknown");
      const accessibleLabel = `Open ${role} ${relationship.label}, currently ${state}`;
      element.setAttribute("aria-label", accessibleLabel);
      element.title = accessibleLabel;
      element.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        revealLeanPaneItem(relationship.item);
      });
    } else {
      const ambiguous = relationship?.resolution === "ambiguous";
      const message = ambiguous
        ? `${relationship.label} matches more than one item in the Lean-pane inventory.`
        : `${relationship.label} is not present in the current Lean-pane inventory.`;
      element.setAttribute("aria-label", message);
      element.setAttribute("aria-disabled", "true");
      element.title = message;
    }
    return element;
  }

  function renderLeanPaneItemDetail(item) {
    const detail = document.createElement("div");
    detail.className = "ol-lean-project-detail";
    const meta = document.createElement("p");
    meta.textContent = [
      item.sourceFile,
      item.sourceStartLine ? `lines ${item.sourceStartLine}-${item.sourceEndLine || item.sourceStartLine}` : "",
      item.leanDeclarationName ? `Lean: ${item.leanDeclarationName}` : "",
      item.leanArtifactPath ? `Artifact: ${item.leanArtifactPath}` : ""
    ].filter(Boolean).join(" · ");
    detail.appendChild(meta);

    const editing = leanPaneEditingItemId === item.id;

    // One row, three visual weights (leanPaneView.paneItemActions): a single
    // status-derived primary, an icon rail for navigation, and an overflow
    // menu for the rare alternatives. Copy lives on the code blocks instead.
    const { primary, rail, overflow } = leanPaneView.paneItemActions(item, { editing });
    const actions = document.createElement("div");
    actions.className = "ol-lean-project-detail-actions";
    if (primary) actions.appendChild(renderPaneItemPrimaryAction(item, primary));
    const railElement = document.createElement("div");
    railElement.className = "ol-lean-icon-rail";
    for (const action of rail) {
      railElement.appendChild(renderPaneItemIconAction(item, action));
    }
    if (overflow.length > 0) {
      railElement.appendChild(renderPaneOverflowMenu(item, overflow));
    }
    actions.appendChild(railElement);
    detail.appendChild(actions);

    const actionError = renderLeanPaneActionError(item);
    if (actionError) detail.appendChild(actionError);

    if (item.breakage) {
      detail.appendChild(renderLeanPaneBreakage(item));
    }
    if (item.repairNeedsReview) {
      const review = document.createElement("p");
      review.className = "ol-lean-project-repair-review";
      review.textContent = "A repair for this item compiles, but its declaration header changed -- review that the statement still matches the source.";
      detail.appendChild(review);
    }

    if (editing) {
      detail.appendChild(renderLeanPaneEditControls(item));
    } else if (item.leanArtifactContent) {
      detail.appendChild(
        renderLeanCodeBlock("ol-lean-project-artifact", item.leanArtifactContent, "Copy artifact")
      );
    } else {
      const empty = document.createElement("p");
      empty.className = "ol-lean-project-missing";
      empty.textContent = "No generated Lean artifact is available for this item.";
      detail.appendChild(empty);
    }

    if (!editing && leanPaneEditLastResult && leanPaneEditLastResult.itemId === item.id) {
      const summary = renderLeanPaneEditImpactSummary(leanPaneEditLastResult, item);
      if (summary) detail.appendChild(summary);
    }
    return detail;
  }

  function leanPaneErrorKey(...parts) {
    return parts.map((part) => String(part ?? "")).join("\u001f");
  }

  function clearDismissedLeanPaneErrors(prefix) {
    const prefixWithSeparator = `${prefix}\u001f`;
    for (const key of dismissedLeanPaneErrorKeys) {
      if (key === prefix || key.startsWith(prefixWithSeparator)) {
        dismissedLeanPaneErrorKeys.delete(key);
      }
    }
  }

  function makeLeanPaneErrorDismissible(element, {
    errorKey = "",
    label = "Dismiss error message",
    onDismiss = null,
    removeOnDismiss = true
  } = {}) {
    if (!element) return element;
    element.classList.add("ol-lean-dismissible-error");
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "ol-lean-error-dismiss";
    dismiss.setAttribute("aria-label", label);
    dismiss.title = "Dismiss";
    dismiss.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (errorKey) dismissedLeanPaneErrorKeys.add(errorKey);
      onDismiss?.();
      if (removeOnDismiss) element.remove();
    });
    element.appendChild(dismiss);
    return element;
  }

  function leanPaneActionErrorKey(item) {
    return String(item?.id || `${item?.leanKind || "theorem"}:${item?.label || item?.leanDeclarationName || ""}`);
  }

  function clearLeanPaneActionError(item) {
    const itemKey = leanPaneActionErrorKey(item);
    leanPaneActionErrors.delete(itemKey);
    clearDismissedLeanPaneErrors(leanPaneErrorKey("action", itemKey));
  }

  function rememberLeanPaneActionError(item, error, operation = "formalize") {
    const maxSpend = isMaxSpendError(error);
    leanPaneActionErrors.set(leanPaneActionErrorKey(item), {
      code: maxSpend ? MAX_SPEND_ERROR_CODE : String(error?.code || ""),
      message: normalizeErrorMessage(error),
      operation
    });
  }

  function isUnresolvedUsesError(error) {
    return String(error?.code || "") === "unresolved_uses";
  }

  function actionFailureMessage(error) {
    const message = typeof error?.message === "string"
      ? error.message
      : normalizeErrorMessage(error);
    return isUnresolvedUsesError(error)
      ? `${message} No Lea run was started.`
      : message;
  }

  function leanPaneActionErrorForItem(item) {
    const local = leanPaneActionErrors.get(leanPaneActionErrorKey(item));
    if (local) return local;
    if (item?.failureCode === MAX_SPEND_ERROR_CODE || item?.finalStatus === "max_spend") {
      return {
        code: MAX_SPEND_ERROR_CODE,
        message: item?.failureMessage || item?.message || "Max spend limit has been reached.",
        operation: "formalize"
      };
    }
    return null;
  }

  function renderLeanPaneActionError(item) {
    const error = leanPaneActionErrorForItem(item);
    if (!error) return null;
    const itemKey = leanPaneActionErrorKey(item);
    const dismissKey = leanPaneErrorKey(
      "action",
      itemKey,
      error.code,
      error.operation,
      error.message
    );
    if (dismissedLeanPaneErrorKeys.has(dismissKey)) return null;
    const maxSpend = error.code === MAX_SPEND_ERROR_CODE;
    const dependencyBlocked = isUnresolvedUsesError(error);
    const alert = document.createElement("div");
    alert.className = "ol-lean-project-action-error";
    alert.setAttribute("role", "alert");
    alert.setAttribute("aria-live", "assertive");

    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = maxSpend
      ? "Cost cap reached"
      : dependencyBlocked
        ? "Dependency must be formalized first"
        : error.operation === "stub"
          ? "Could not create Lean stub"
          : "Could not start formalization";
    const message = document.createElement("p");
    message.textContent = maxSpend ? MAX_SPEND_PANE_MESSAGE : actionFailureMessage(error);
    copy.appendChild(title);
    copy.appendChild(message);
    alert.appendChild(copy);

    if (maxSpend) {
      const settings = document.createElement("button");
      settings.type = "button";
      settings.className = "ol-lean-secondary-button ol-lean-project-action-error-settings";
      settings.textContent = "Open settings";
      settings.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        showSettingsPopover();
        activePopover?.querySelector("[data-role='max-spend']")?.focus({ preventScroll: true });
      });
      alert.appendChild(settings);
    }
    return makeLeanPaneErrorDismissible(alert, {
      errorKey: dismissKey,
      onDismiss: () => leanPaneActionErrors.delete(itemKey)
    });
  }

  // --- Self-repair actions (docs/FEATURE-overleaf-self-repair.md, Phase 5) ---

  function renderRepairButton(item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ol-lean-secondary-button ol-lean-item-primary-action ol-lean-repair-button";
    button.textContent = "Repair with Lea";
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const projectId = itemsProjectId(lastLeanPaneManifest?.items || []);
      const target = leanPaneView.paneItemToEditTarget(item, projectId);
      requestRepair({
        overleafProjectId: projectId,
        items: [{ targetKind: target.targetKind, targetLabel: target.targetLabel }]
      });
    });
    return button;
  }

  // The chip-adjacent breakage explanation + repair lifecycle line.
  function renderLeanPaneBreakage(item) {
    const container = document.createElement("div");
    container.className = "ol-lean-project-breakage";
    const attribution = document.createElement("p");
    attribution.textContent = leanPaneView.formatBreakageAttribution(item.breakage);
    container.appendChild(attribution);
    const itemKey = item.leanDeclarationName || item.label || "";
    const repair = item.breakage.repair;
    if (repair?.state === "running") {
      const line = document.createElement("p");
      line.className = "ol-lean-project-breakage-running";
      line.textContent = "A repair run is in progress for this item...";
      container.appendChild(line);
    } else if (repair?.state === "failed") {
      const reason = repair.failureReason || "the repaired file still does not compile.";
      const dismissKey = leanPaneErrorKey("repair-result", itemKey, reason);
      const line = document.createElement("p");
      line.className = "ol-lean-project-breakage-failed";
      line.textContent = `Repair failed: ${reason}`;
      if (!dismissedLeanPaneErrorKeys.has(dismissKey)) {
        container.appendChild(makeLeanPaneErrorDismissible(line, { errorKey: dismissKey }));
      }
    }
    if (leanPaneRepairError && leanPaneRepairError.itemKey === itemKey) {
      const dismissKey = leanPaneErrorKey("repair-dispatch", itemKey, leanPaneRepairError.message);
      const line = document.createElement("p");
      line.className = "ol-lean-project-breakage-failed";
      line.textContent = leanPaneRepairError.message;
      if (!dismissedLeanPaneErrorKeys.has(dismissKey)) {
        container.appendChild(makeLeanPaneErrorDismissible(line, {
          errorKey: dismissKey,
          onDismiss: () => { leanPaneRepairError = null; }
        }));
      }
    }
    return container;
  }

  // Dispatch: a single item goes through /lean-pane/repair/start; several go
  // through the topologically ordered batch (/lean-pane/repair/all).
  async function requestRepair({ overleafProjectId, items }) {
    leanPaneRepairError = null;
    const errorKey = items.length === 1 ? items[0].targetLabel : "batch";
    clearDismissedLeanPaneErrors(leanPaneErrorKey("repair-dispatch", errorKey));
    for (const item of items) {
      clearDismissedLeanPaneErrors(leanPaneErrorKey("repair-result", item.targetLabel));
    }
    try {
      const baseUrl = await chatCompanionBaseUrl();
      if (items.length === 1) {
        const response = await fetch(`${baseUrl}/lean-pane/repair/start`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ overleafProjectId, ...items[0] })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload?.message || `Companion returned HTTP ${response.status}.`);
      } else {
        const response = await fetch(`${baseUrl}/lean-pane/repair/all`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ overleafProjectId, items })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload?.message || `Companion returned HTTP ${response.status}.`);
        leanPaneRepairBatch = payload;
        leanPaneExpandedBatchQueueId = "";
        leanPaneExpandedBatchCompletedId = "";
        startRepairBatchPolling({ immediate: true });
      }
    } catch (error) {
      leanPaneRepairError = { itemKey: errorKey, message: normalizeErrorMessage(error) };
    }
    renderLeanPaneManifest(lastLeanPaneManifest);
    scheduleLeanPaneRefresh();
  }

  function startRepairBatchPolling({ immediate = false } = {}) {
    if (leanPaneRepairBatchTimer) clearTimeout(leanPaneRepairBatchTimer);
    const delayMs = immediate
      ? 0
      : (pushConnected ? REPAIR_BATCH_POLL_RECONCILE_MS : REPAIR_BATCH_POLL_MS);
    leanPaneRepairBatchTimer = setTimeout(async () => {
      leanPaneRepairBatchTimer = 0;
      const batchId = leanPaneRepairBatch?.batchId;
      if (!batchId) return;
      // (delay above: instant when a push event announced a change, slow
      // reconciliation while the stream is up, fast poll when it's down)
      try {
        const baseUrl = await chatCompanionBaseUrl();
        const response = await fetch(`${baseUrl}/lean-pane/repair/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ batchId })
        });
        const payload = await response.json().catch(() => ({}));
        if (response.ok) leanPaneRepairBatch = payload;
      } catch {
        // transient; keep the last snapshot and try again
      }
      renderLeanPaneManifest(lastLeanPaneManifest);
      scheduleLeanPaneRefresh();
      if (leanPaneRepairBatch && !leanPaneRepairBatch.done && !leanPaneRepairBatch.pausedOn) {
        startRepairBatchPolling();
      }
    }, delayMs);
  }

  async function continueRepairBatch() {
    const batchId = leanPaneRepairBatch?.batchId;
    if (!batchId) return;
    try {
      const baseUrl = await chatCompanionBaseUrl();
      const response = await fetch(`${baseUrl}/lean-pane/repair/all/continue`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batchId })
      });
      const payload = await response.json().catch(() => ({}));
      if (response.ok) leanPaneRepairBatch = payload;
      startRepairBatchPolling({ immediate: true });
    } catch (error) {
      leanPaneRepairError = { itemKey: "batch", message: normalizeErrorMessage(error) };
    }
    renderLeanPaneManifest(lastLeanPaneManifest);
  }

  // Stop a running batch: the companion halts further items and interrupts the
  // one mid-run. The snapshot comes back `stopping` (then `canceled` once it
  // settles); keep polling so the panel reflects the final stopped state.
  async function cancelRepairBatch() {
    const batchId = leanPaneRepairBatch?.batchId;
    if (!batchId) return;
    try {
      const baseUrl = await chatCompanionBaseUrl();
      const response = await fetch(`${baseUrl}/lean-pane/repair/all/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batchId })
      });
      const payload = await response.json().catch(() => ({}));
      if (response.ok) leanPaneRepairBatch = payload;
      startRepairBatchPolling({ immediate: true });
    } catch (error) {
      leanPaneRepairError = { itemKey: "batch", message: normalizeErrorMessage(error) };
    }
    renderLeanPaneManifest(lastLeanPaneManifest);
  }

  // Project-level "Stub all" / "Formalize all" launchers, above the item tree.
  // Only one batch surface exists at a time: while a batch panel is showing
  // (running, paused, or awaiting dismiss) the launchers stay hidden so a
  // second batch can't clobber the first. Each button is present only when it
  // has eligible work (un-stubbed theorems / not-yet-proven items).
  function renderLeanPaneBatchActions(items) {
    if (leanPaneRepairBatch) return null;
    const stubbable = leanPaneView.stubbableItems(items);
    const formalizable = leanPaneView.formalizableItems(items);
    if (stubbable.length === 0 && formalizable.length === 0) return null;
    const row = document.createElement("div");
    row.className = "ol-lean-project-batch-actions";
    if (stubbable.length > 0) {
      const stubAll = document.createElement("button");
      stubAll.type = "button";
      stubAll.className = "ol-lean-secondary-button ol-lean-stub-all-button";
      stubAll.textContent = `Stub all (${stubbable.length})`;
      stubAll.title = "Generate a Lean sorry-stub for every un-stubbed theorem in the project.";
      stubAll.addEventListener("click", () => { stubAllTheorems(); });
      row.appendChild(stubAll);
    }
    if (formalizable.length > 0) {
      const formalizeAll = document.createElement("button");
      formalizeAll.type = "button";
      formalizeAll.className = "ol-lean-primary-button ol-lean-formalize-all-button";
      formalizeAll.textContent = `Formalize all (${formalizable.length})`;
      formalizeAll.title = "Run Lea to formalize every theorem and definition that has no verified proof yet.";
      formalizeAll.addEventListener("click", () => { formalizeAllItems(); });
      row.appendChild(formalizeAll);
    }
    return row;
  }

  // Live batch progress at the top of the pane. The companion already returns
  // dependency-ordered entries, so the client can render a real queue (active
  // ordinal, next items, exact outcomes) without duplicating orchestration.
  function renderLeanPaneRepairBatchPanel() {
    const batch = leanPaneRepairBatch;
    if (!batch || !Array.isArray(batch.items) || batch.items.length === 0) return null;
    const operation = batch.operation || "repair";
    const noun = operation === "stub" ? "Stub" : operation === "formalize" ? "Formalize" : "Repair";
    const runningVerb = operation === "stub" ? "Stubbing" : operation === "formalize" ? "Formalizing" : "Repairing";
    const completedStates = new Set(operation === "stub"
      ? ["stubbed"]
      : operation === "formalize"
        ? ["formalized"]
        : ["repaired"]);
    const successfulStates = new Set(operation === "stub"
      ? ["stubbed"]
      : operation === "formalize"
        ? ["formalized", "disproved"]
        : ["repaired", "needs_review"]);
    const attentionStates = new Set(["failed", "skipped", "canceled", "needs_review", "disproved"]);
    const completedEntries = batch.items.filter((entry) => completedStates.has(entry.state));
    const attentionEntries = batch.items.filter((entry) => attentionStates.has(entry.state));
    const reportedActiveEntry = batch.items.find((entry) => entry.state === "running") || null;
    // The launch response can land after the batch loop is marked running but
    // just before its first entry flips pending → running. Show that first
    // dispatch as current instead of briefly rendering an idle-looking queue.
    const activeEntry = reportedActiveEntry || (
      batch.running && !batch.done && !batch.pausedOn && !batch.stopping
        ? batch.items.find((entry) => entry.state === "pending") || null
        : null
    );
    const queuedEntries = batch.items.filter((entry) => entry.state === "pending" && entry !== activeEntry);
    const activeIndex = activeEntry ? batch.items.indexOf(activeEntry) : -1;
    const total = batch.items.length;
    const completedCount = batch.items.filter((entry) => successfulStates.has(entry.state)).length;
    const failedCount = batch.items.filter((entry) => entry.state === "failed").length;
    const skippedCount = batch.items.filter((entry) => entry.state === "skipped").length;
    const canceledCount = batch.items.filter((entry) => entry.state === "canceled").length;

    const panel = document.createElement("div");
    panel.className = `ol-lean-project-repair-batch ol-lean-batch-queue${
      batch.pausedOn
        ? " ol-lean-batch-queue-paused"
        : batch.canceled
          ? " ol-lean-batch-queue-stopped"
          : batch.done
            ? " ol-lean-batch-queue-done"
            : ""
    }`;

    const header = document.createElement("div");
    header.className = "ol-lean-batch-queue-header";
    const heading = document.createElement("div");
    heading.className = "ol-lean-batch-queue-heading";
    const title = document.createElement("strong");
    title.textContent = `${noun} all`;
    heading.appendChild(title);
    const state = document.createElement("span");
    state.className = "ol-lean-batch-queue-state";
    state.setAttribute("aria-live", "polite");
    state.textContent = batch.canceled
      ? "Stopped"
      : batch.stopping
        ? "Stopping…"
        : batch.done
          ? "Complete"
          : batch.pausedOn
            ? "Paused"
            : `${runningVerb}…`;
    heading.appendChild(state);
    header.appendChild(heading);
    const count = document.createElement("span");
    count.className = "ol-lean-batch-queue-count";
    count.textContent = formatBatchQueueCount({
      batch,
      completedCount,
      failedCount,
      skippedCount,
      canceledCount,
      total
    });
    header.appendChild(count);
    panel.appendChild(header);

    const progress = document.createElement("div");
    progress.className = "ol-lean-batch-queue-progress";
    progress.setAttribute("role", "progressbar");
    progress.setAttribute("aria-label", formatBatchQueueProgressLabel({
      noun,
      completedCount,
      failedCount,
      skippedCount,
      canceledCount,
      total
    }));
    progress.setAttribute("aria-valuemin", "0");
    progress.setAttribute("aria-valuemax", String(total));
    progress.setAttribute("aria-valuenow", String(completedCount));
    for (const entry of batch.items) {
      const segment = document.createElement("span");
      const stateClass = entry === activeEntry
        ? "active"
        : entry.state === "failed"
          ? "failed"
          : entry.state === "skipped"
            ? "skipped"
            : entry.state === "canceled"
              ? "canceled"
              : entry.state === "disproved" || entry.state === "needs_review"
                ? "attention"
                : successfulStates.has(entry.state)
                  ? "success"
                  : "pending";
      segment.className = `ol-lean-batch-queue-progress-segment ol-lean-batch-queue-progress-${stateClass}`;
      segment.style.width = `${100 / total}%`;
      segment.setAttribute("aria-hidden", "true");
      progress.appendChild(segment);
    }
    panel.appendChild(progress);

    if (batch.pausedOn) {
      const callout = document.createElement("div");
      callout.className = "ol-lean-batch-queue-callout";
      const calloutTitle = document.createElement("strong");
      calloutTitle.textContent = batch.pausedOn.reason === "max_spend"
        ? "Maximum spend reached"
        : `${batch.pausedOn.targetLabel || "An item"} failed`;
      callout.appendChild(calloutTitle);
      const calloutDetail = document.createElement("span");
      calloutDetail.textContent = batch.pausedOn.reason === "max_spend"
        ? "Increase or clear the spend limit before continuing."
        : queuedEntries.length > 0
          ? `${queuedEntries.length} independent item${queuedEntries.length === 1 ? "" : "s"} can still run.`
          : "No independent items remain in the queue.";
      callout.appendChild(calloutDetail);
      panel.appendChild(callout);
    }

    if (activeEntry) {
      const current = document.createElement("section");
      current.className = "ol-lean-batch-queue-current";
      const meta = document.createElement("span");
      meta.className = "ol-lean-batch-queue-eyebrow";
      meta.textContent = `Current · ${activeIndex + 1} of ${total}`;
      current.appendChild(meta);
      const currentRow = renderBatchQueueEntry(activeEntry, activeIndex, {
        marker: "●",
        stateClass: "running",
        detail: formatBatchQueueActiveDetail(activeEntry, runningVerb)
      });
      current.appendChild(currentRow);
      panel.appendChild(current);
    }

    if (queuedEntries.length > 0) {
      const queued = document.createElement("section");
      queued.className = "ol-lean-batch-queue-section";
      const queuedHeading = document.createElement("strong");
      queuedHeading.className = "ol-lean-batch-queue-section-title";
      queuedHeading.textContent = "Next";
      queued.appendChild(queuedHeading);
      const list = document.createElement("ol");
      list.className = "ol-lean-batch-queue-list";
      const queueExpanded = leanPaneExpandedBatchQueueId === batch.batchId;
      const visibleQueued = queueExpanded ? queuedEntries : queuedEntries.slice(0, 3);
      for (const entry of visibleQueued) {
        const entryIndex = batch.items.indexOf(entry);
        list.appendChild(renderBatchQueueEntry(entry, entryIndex, {
          marker: "○",
          stateClass: "pending",
          detail: `Queued · position ${entryIndex + 1} of ${total}`
        }));
      }
      queued.appendChild(list);
      if (queuedEntries.length > 3) {
        const remaining = queuedEntries.length - 3;
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "ol-lean-batch-queue-disclosure";
        toggle.setAttribute("aria-expanded", String(queueExpanded));
        toggle.textContent = queueExpanded ? "Show fewer queued" : `+${remaining} more queued`;
        toggle.addEventListener("click", () => {
          leanPaneExpandedBatchQueueId = queueExpanded ? "" : batch.batchId;
          renderLeanPaneManifest(lastLeanPaneManifest);
        });
        queued.appendChild(toggle);
      }
      panel.appendChild(queued);
    }

    if (attentionEntries.length > 0) {
      const attention = document.createElement("section");
      attention.className = "ol-lean-batch-queue-section ol-lean-batch-queue-attention";
      const attentionHeading = document.createElement("strong");
      attentionHeading.className = "ol-lean-batch-queue-section-title";
      attentionHeading.textContent = "Needs attention";
      attention.appendChild(attentionHeading);
      const list = document.createElement("ul");
      list.className = "ol-lean-batch-queue-list";
      for (const entry of attentionEntries) {
        list.appendChild(renderBatchQueueEntry(entry, batch.items.indexOf(entry), {
          marker: entry.state === "disproved" ? "◇" : "!",
          stateClass: entry.state,
          detail: formatBatchQueueOutcomeDetail(entry, operation)
        }));
      }
      attention.appendChild(list);
      panel.appendChild(attention);
    }

    if (completedEntries.length > 0) {
      const completed = document.createElement("section");
      completed.className = "ol-lean-batch-queue-section ol-lean-batch-queue-completed";
      const completedExpanded = leanPaneExpandedBatchCompletedId === batch.batchId;
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "ol-lean-batch-queue-completed-toggle";
      toggle.setAttribute("aria-expanded", String(completedExpanded));
      toggle.textContent = `${completedExpanded ? "Hide" : "Show"} ${completedEntries.length} completed`;
      toggle.addEventListener("click", () => {
        leanPaneExpandedBatchCompletedId = completedExpanded ? "" : batch.batchId;
        renderLeanPaneManifest(lastLeanPaneManifest);
      });
      completed.appendChild(toggle);
      if (completedExpanded) {
        const list = document.createElement("ul");
        list.className = "ol-lean-batch-queue-list";
        for (const entry of completedEntries) {
          list.appendChild(renderBatchQueueEntry(entry, batch.items.indexOf(entry), {
            marker: "✓",
            stateClass: "completed",
            detail: formatBatchQueueOutcomeDetail(entry, operation)
          }));
        }
        completed.appendChild(list);
      }
      panel.appendChild(completed);
    }

    if (leanPaneRepairError && leanPaneRepairError.itemKey === "batch") {
      const dismissKey = leanPaneErrorKey("repair-dispatch", "batch", leanPaneRepairError.message);
      const line = document.createElement("p");
      line.className = "ol-lean-project-breakage-failed";
      line.textContent = leanPaneRepairError.message;
      if (!dismissedLeanPaneErrorKeys.has(dismissKey)) {
        panel.appendChild(makeLeanPaneErrorDismissible(line, {
          errorKey: dismissKey,
          onDismiss: () => { leanPaneRepairError = null; }
        }));
      }
    }
    const controls = document.createElement("div");
    controls.className = "ol-lean-project-detail-actions";
    // Stop is available while the batch is actively working (not paused, not
    // finished, not already stopping): it halts further items and interrupts
    // the one mid-run.
    if (!batch.done && !batch.pausedOn && !batch.stopping) {
      const stop = document.createElement("button");
      stop.type = "button";
      stop.className = "ol-lean-secondary-button ol-lean-stop-batch-button";
      stop.textContent = "Stop";
      stop.addEventListener("click", () => { cancelRepairBatch(); });
      controls.appendChild(stop);
    }
    if (batch.pausedOn) {
      const cont = document.createElement("button");
      cont.type = "button";
      cont.className = "ol-lean-primary-button";
      cont.textContent = "Continue remaining";
      cont.addEventListener("click", () => { continueRepairBatch(); });
      controls.appendChild(cont);
    }
    if (batch.done || batch.pausedOn) {
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "ol-lean-secondary-button";
      dismiss.textContent = "Dismiss";
      dismiss.addEventListener("click", () => {
        leanPaneRepairBatch = null;
        leanPaneExpandedBatchQueueId = "";
        leanPaneExpandedBatchCompletedId = "";
        renderLeanPaneManifest(lastLeanPaneManifest);
      });
      controls.appendChild(dismiss);
    }
    if (controls.children.length > 0) panel.appendChild(controls);
    return panel;
  }

  function renderBatchQueueEntry(entry, index, { marker, stateClass, detail }) {
    const row = document.createElement("li");
    row.className = `ol-lean-batch-queue-item ol-lean-batch-queue-item-${stateClass}`;
    row.dataset.position = String(index + 1);
    const icon = document.createElement("span");
    icon.className = "ol-lean-batch-queue-marker";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = marker;
    row.appendChild(icon);
    const copy = document.createElement("span");
    copy.className = "ol-lean-batch-queue-item-copy";
    const label = document.createElement("strong");
    label.textContent = entry.targetLabel || `Item ${index + 1}`;
    copy.appendChild(label);
    const description = document.createElement("span");
    description.className = "ol-lean-batch-queue-item-detail";
    description.textContent = detail;
    copy.appendChild(description);
    row.appendChild(copy);
    return row;
  }

  function formatBatchQueueActiveDetail(entry, runningVerb) {
    const statusInfo = latestStatuses[targetKey(entry)] || {};
    const paneItem = (lastLeanPaneManifest?.items || []).find((item) => (
      item.label === entry.targetLabel
      && (entry.targetKind !== "definition" || item.leanKind === "def")
    ));
    const progress = statusInfo.turnProgress || paneItem?.turnProgress;
    const current = Number.parseInt(String(progress?.current || ""), 10);
    const max = Number.parseInt(String(progress?.max || ""), 10);
    const turn = Number.isFinite(current) && current > 0 && Number.isFinite(max) && max > 0
      ? ` · Lea turn ${current} of ${max}`
      : "";
    return `${runningVerb}…${turn}`;
  }

  function formatBatchQueueOutcomeDetail(entry, operation) {
    const outcome = leanPaneView.formatRepairOutcome(entry, operation);
    const prefix = `${entry?.targetLabel || ""}: `;
    return outcome.startsWith(prefix) ? outcome.slice(prefix.length) : outcome;
  }

  function formatBatchQueueCount({ batch, completedCount, failedCount, skippedCount, canceledCount, total }) {
    if (!batch.canceled && !batch.pausedOn && !batch.done) {
      return `${completedCount} / ${total} complete`;
    }
    const parts = [];
    if (completedCount > 0) parts.push(`${completedCount} complete`);
    if (failedCount > 0) parts.push(`${failedCount} failed`);
    if (skippedCount > 0) parts.push(`${skippedCount} skipped`);
    if (canceledCount > 0) parts.push(`${canceledCount} stopped`);
    return parts.length > 0 ? parts.join(" · ") : `0 / ${total} complete`;
  }

  function formatBatchQueueProgressLabel({ noun, completedCount, failedCount, skippedCount, canceledCount, total }) {
    const parts = [`${noun} all: ${completedCount} of ${total} completed`];
    if (failedCount > 0) parts.push(`${failedCount} failed`);
    if (skippedCount > 0) parts.push(`${skippedCount} skipped`);
    if (canceledCount > 0) parts.push(`${canceledCount} stopped`);
    return `${parts.join(", ")}.`;
  }

  // Open the inline edit view for an item: shows the current artifact
  // immediately (no network round trip needed to start typing), then
  // best-effort refreshes the draft + pre-save dependents preview from
  // /lean-pane/edit/start. A network failure here still leaves editing usable
  // -- the preview is a nicety, not a precondition (feature spec: v1 does not
  // block editing on the impact preview being available).
  async function openLeanPaneEdit(item) {
    leanPaneEditingItemId = item.id;
    leanPaneEditDraft = item.leanArtifactContent || "";
    leanPaneEditPreSaveDependents = [];
    leanPaneEditError = "";
    leanPaneEditLastResult = null;
    renderLeanPaneManifest(lastLeanPaneManifest);
    try {
      const projectId = itemsProjectId(lastLeanPaneManifest?.items || []);
      const target = leanPaneView.paneItemToEditTarget(item, projectId);
      const baseUrl = await chatCompanionBaseUrl();
      const response = await fetch(`${baseUrl}/lean-pane/edit/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(target)
      });
      const payload = await response.json().catch(() => ({}));
      if (response.ok && payload?.ok && leanPaneEditingItemId === item.id) {
        leanPaneEditDraft = typeof payload.content === "string" ? payload.content : leanPaneEditDraft;
        leanPaneEditPreSaveDependents = Array.isArray(payload.dependents) ? payload.dependents : [];
        renderLeanPaneManifest(lastLeanPaneManifest);
      }
    } catch {
      // Best-effort only -- see doc comment above.
    }
  }

  function closeLeanPaneEdit() {
    leanPaneEditingItemId = "";
    leanPaneEditDraft = "";
    leanPaneEditPreSaveDependents = [];
    leanPaneEditError = "";
    renderLeanPaneManifest(lastLeanPaneManifest);
  }

  function renderLeanPaneEditErrorLine(errorLine) {
    errorLine.replaceChildren();
    errorLine.classList.remove("ol-lean-dismissible-error");
    errorLine.hidden = !leanPaneEditError;
    if (!leanPaneEditError) return;
    const message = document.createElement("span");
    message.textContent = leanPaneEditError;
    errorLine.appendChild(message);
    makeLeanPaneErrorDismissible(errorLine, {
      removeOnDismiss: false,
      onDismiss: () => {
        leanPaneEditError = "";
        errorLine.hidden = true;
        errorLine.replaceChildren();
        errorLine.classList.remove("ol-lean-dismissible-error");
      }
    });
  }

  function renderLeanPaneEditControls(item) {
    const container = document.createElement("div");
    container.className = "ol-lean-project-edit";

    if (leanPaneEditPreSaveDependents.length > 0) {
      const note = document.createElement("p");
      note.className = "ol-lean-project-impact-note";
      note.textContent = leanPaneView.formatDependentsImpact(leanPaneEditPreSaveDependents);
      container.appendChild(note);
    }

    // Overlay editor: the textarea stays the real input, but its text is
    // transparent (caret excepted); the visible text is the same regex-highlighted
    // rendering the read view uses (renderLeanPaneCode), in a backdrop layer kept
    // in sync on input/scroll. Both layers share identical font/padding/wrapping
    // so the glyphs line up exactly.
    const editor = document.createElement("div");
    editor.className = "ol-lean-project-edit-editor";

    const highlightLayer = document.createElement("pre");
    highlightLayer.className = "ol-lean-project-edit-highlight";
    highlightLayer.setAttribute("aria-hidden", "true");
    editor.appendChild(highlightLayer);

    const textarea = document.createElement("textarea");
    textarea.className = "ol-lean-project-edit-textarea";
    textarea.value = leanPaneEditDraft;
    textarea.spellcheck = false;
    textarea.setAttribute("aria-label", `Edit Lean source for ${item.leanDeclarationName || item.label || "this item"}`);
    textarea.addEventListener("input", () => {
      leanPaneEditDraft = textarea.value;
      renderLeanPaneCode(highlightLayer, textarea.value);
      highlightLayer.scrollTop = textarea.scrollTop;
    });
    textarea.addEventListener("scroll", () => {
      highlightLayer.scrollTop = textarea.scrollTop;
      highlightLayer.scrollLeft = textarea.scrollLeft;
    });
    editor.appendChild(textarea);
    renderLeanPaneCode(highlightLayer, leanPaneEditDraft);
    container.appendChild(editor);

    const errorLine = document.createElement("p");
    errorLine.className = "ol-lean-project-edit-error";
    renderLeanPaneEditErrorLine(errorLine);
    container.appendChild(errorLine);

    const actions = document.createElement("div");
    actions.className = "ol-lean-project-detail-actions";

    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "ol-lean-secondary-button";
    cancelButton.textContent = "Cancel";
    cancelButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      closeLeanPaneEdit();
    });

    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.className = "ol-lean-secondary-button ol-lean-edit-save-button";
    saveButton.textContent = "Save";
    saveButton.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const content = textarea.value;
      saveButton.disabled = true;
      cancelButton.disabled = true;
      textarea.disabled = true;
      saveButton.textContent = "Saving…";
      errorLine.hidden = true;
      try {
        const projectId = itemsProjectId(lastLeanPaneManifest?.items || []);
        const target = leanPaneView.paneItemToEditTarget(item, projectId);
        const baseUrl = await chatCompanionBaseUrl();
        const response = await fetch(`${baseUrl}/lean-pane/edit/save`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...target, content })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload?.ok) {
          throw new Error(payload?.message || `Save failed (HTTP ${response.status}).`);
        }
        leanPaneEditingItemId = "";
        leanPaneEditError = "";
        leanPaneEditLastResult = { itemId: item.id, ...payload };
        // No agent run is started by a save (feature spec acceptance criterion
        // 9); this refresh just picks up the new check verdicts -- the edited
        // item's own status plus any cascade-checked dependents -- through the
        // pane's normal manifest path rather than a bespoke cross-item update.
        await refreshLeanPaneNow({ background: true });
      } catch (error) {
        leanPaneEditError = normalizeErrorMessage(error);
        saveButton.disabled = false;
        cancelButton.disabled = false;
        textarea.disabled = false;
        saveButton.textContent = "Save";
        renderLeanPaneEditErrorLine(errorLine);
      }
    });

    actions.appendChild(saveButton);
    actions.appendChild(cancelButton);
    container.appendChild(actions);
    return container;
  }

  // Post-save summary shown under an item right after a successful save (until
  // the item is re-expanded/re-edited or the pane state key changes).
  // The edited item's OWN outcome is deliberately not repeated here: it now
  // drives the item's status chip and `message` directly (getTheoremStatus's
  // lastEditCheckStatus override, docs/FEATURE-overleaf-lean-pane-manual-edit.md),
  // the same place any other failed item's reason already shows. Repeating it
  // here in small print was the original, confusing version of this note --
  // this summary is now scoped to what the chip *can't* show on its own: a
  // one-time confirmation of what a save actually did across OTHER items.
  // Returns null when there's nothing worth a separate note for.
  function renderLeanPaneEditImpactSummary(result, item = null) {
    if (result.unchanged) {
      const container = document.createElement("div");
      container.className = "ol-lean-project-impact-note";
      container.textContent = "No changes to save.";
      return container;
    }
    // The save response is a HISTORICAL snapshot; the pane re-renders this
    // summary on every manifest refresh, so reconcile it against the live
    // per-item truth each time -- a dependent fixed through any other path
    // (per-item repair, manual edit, chat) drops out of the counts and the
    // offer on the next refresh (PLAN-self-repair-stale-offers Fix 1).
    const dependents = leanPaneView.reconcileDependentsImpact(
      result.dependentsImpact || [],
      lastLeanPaneManifest?.items || []
    );
    if (dependents.length === 0) return null;

    const container = document.createElement("div");
    container.className = "ol-lean-project-impact-note";
    const heading = document.createElement("p");
    const stillBroken = leanPaneView.stillBrokenDependents(dependents);
    const stillBrokenSet = new Set(stillBroken);
    const repairingCount = dependents.filter((d) => d.nowRepairing && (d.brokenByUpstream || d.busy || d.status === "unknown")).length;
    const fixedCount = dependents.filter(
      (d) => d.sinceFixed && (d.brokenByUpstream || d.busy || d.status === "unknown")
    ).length;
    const busyCount = dependents.filter(
      (d) => d.busy && !d.sinceFixed && !d.nowRepairing && !stillBrokenSet.has(d)
    ).length;
    const okCount = dependents.length - stillBroken.length - repairingCount - fixedCount - busyCount;
    if (stillBroken.length + repairingCount + busyCount === 0 && fixedCount > 0) {
      heading.textContent = `${dependents.length} downstream item${dependents.length === 1 ? " was" : "s were"} affected by this edit -- all since fixed or re-verified.`;
    } else {
      const parts = [];
      if (stillBroken.length > 0) parts.push(`${stillBroken.length} broken`);
      if (repairingCount > 0) parts.push(`${repairingCount} repairing`);
      if (fixedCount > 0) parts.push(`${fixedCount} since fixed`);
      if (busyCount > 0) parts.push(`${busyCount} not yet re-checked`);
      if (okCount > 0) parts.push(`${okCount} still valid`);
      heading.textContent = `${dependents.length} downstream item${dependents.length === 1 ? "" : "s"} affected: ${parts.join(", ")}.`;
    }
    container.appendChild(heading);
    for (const dependent of dependents) {
      const p = document.createElement("p");
      p.textContent = leanPaneView.formatDependentOutcome(dependent);
      container.appendChild(p);
    }
    // Self-repair: offer one action for the whole CURRENTLY-broken set
    // (feature spec Part 2) -- never for snapshot-broken items that live
    // truth says are fixed or already being repaired. Suppressed when the
    // edit broke the edited item ITSELF -- its own repair offer (on the
    // item) is the right entry point until it compiles again.
    const broken = stillBroken;
    const ownBroken = String(result.ownResult?.checkStatus || "").toLowerCase() === "error";
    if (broken.length > 0 && !ownBroken) {
      const actions = document.createElement("div");
      actions.className = "ol-lean-project-detail-actions";
      const repairAll = document.createElement("button");
      repairAll.type = "button";
      repairAll.className = "ol-lean-primary-button ol-lean-repair-all-button";
      repairAll.textContent = `Repair all (${broken.length})`;
      repairAll.addEventListener("click", () => {
        const projectId = itemsProjectId(lastLeanPaneManifest?.items || []);
        requestRepair({
          overleafProjectId: projectId,
          items: broken.map((d) => ({ targetKind: "theorem", targetLabel: d.targetLabel }))
        });
      });
      actions.appendChild(repairAll);
      container.appendChild(actions);
    }
    return container;
  }

  function renderLeanPaneTitle(element, item) {
    element.replaceChildren();
    element.appendChild(document.createTextNode(`${leanPaneView.capitalize(item.kind)}: `));
    const title = item.title || item.leanDeclarationName || item.label || "";
    if (item.title) {
      renderLeanPaneLatex(element, title, { append: true });
    } else {
      element.appendChild(document.createTextNode(title));
    }
  }

  function renderLeanPaneLatex(element, source, { append = false } = {}) {
    if (!append) element.replaceChildren();
    const segments = leanPaneView.parsePaneLatex(source || "");
    if (segments.length === 0) {
      element.appendChild(document.createTextNode(source || ""));
      return;
    }

    for (const segment of segments) {
      if (segment.type !== "math") {
        renderLeanPaneLatexText(element, segment.text);
        continue;
      }
      const math = document.createElement("span");
      math.className = segment.display
        ? "ol-lean-project-math ol-lean-project-math-display"
        : "ol-lean-project-math";
      if (renderLeanPaneKatex(math, segment.text, segment.display)) {
        element.appendChild(math);
        continue;
      }

      math.classList.add("ol-lean-project-math-fallback");
      math.dataset.mathRenderer = "fallback";
      const parts = leanPaneView.formatLiteMath(segment.text);
      if (parts.length === 0) {
        math.textContent = segment.text;
      } else {
        for (const part of parts) {
          if (part.type === "sup" || part.type === "sub") {
            const script = document.createElement("span");
            script.className = `ol-lean-project-math-script ol-lean-project-math-${part.type}`;
            script.textContent = part.text;
            math.appendChild(script);
          } else {
            math.appendChild(document.createTextNode(part.text));
          }
        }
      }
      element.appendChild(math);
    }
  }

  function renderLeanPaneKatex(element, source, displayMode) {
    const renderer = typeof katex !== "undefined" ? katex : globalThis.katex;
    const result = leanPaneView.renderPaneMath(renderer, element, source, displayMode);
    if (result.ok) {
      element.dataset.mathRenderer = "katex";
      return true;
    }
    if (renderer?.render) {
      element.title = `Could not fully render this expression: ${errorText(result.error)}`;
    }
    return false;
  }

  function renderLeanPaneLatexText(element, source) {
    const parts = leanPaneView.formatLiteLatexText(source);
    if (parts.length === 0) {
      element.appendChild(document.createTextNode(source || ""));
      return;
    }
    for (const part of parts) {
      if (!Array.isArray(part.marks) || part.marks.length === 0) {
        element.appendChild(document.createTextNode(part.text));
        continue;
      }
      const span = document.createElement("span");
      span.className = part.marks
        .map((mark) => `ol-lean-project-latex-${mark}`)
        .join(" ");
      span.textContent = part.text;
      element.appendChild(span);
    }
  }

  function renderLeanPaneCode(element, code) {
    element.replaceChildren();
    const lines = String(code || "").split("\n");
    lines.forEach((line, lineIndex) => {
      const row = document.createElement("span");
      row.className = "ol-lean-project-code-line";
      for (const token of leanPaneView.highlightLeanLine(line)) {
        const span = document.createElement("span");
        if (token.cls) span.className = `ol-lean-project-lean-${token.cls}`;
        span.textContent = token.text;
        row.appendChild(span);
      }
      if (line === "") row.appendChild(document.createTextNode(" "));
      element.appendChild(row);
      if (lineIndex < lines.length - 1) {
        element.appendChild(document.createTextNode("\n"));
      }
    });
  }

  // Item 11: jump the Overleaf editor to this item's source block. The actual
  // scroll/select happens in pageBridge (page world) which owns the CodeMirror view.
  function goToPaneItemSource(item) {
    if (leanPaneStatus) {
      leanPaneStatus.textContent = item.sourceFile
        ? `Opening ${item.sourceFile}...`
        : "Opening source...";
    }
    window.postMessage({
      type: "OL_LEAN_NAVIGATE",
      sourceFile: item.sourceFile || "",
      from: item.sourceStartOffset,
      to: item.sourceEndOffset,
      line: item.sourceStartLine,
      // Text anchors let pageBridge locate the block even when byte offsets have
      // drifted (edits) or the file path can't be matched exactly.
      leanLabel: item.label || item.leanDeclarationName || "",
      latexLabel: item.latexLabel || ""
    }, "*");
  }

  // Item 12: start a formalization run for this item, reusing the same /formalize
  // path the in-document badge uses, then refresh so the pane reflects in-progress
  // (polling, from item 4, takes over until it settles).
  function renderFormalizeButton(item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ol-lean-secondary-button ol-lean-item-primary-action ol-lean-formalize-button";
    const idleLabel = item.status === "missing-stub" ? "Formalize" : "Re-formalize";
    button.textContent = idleLabel;
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      clearLeanPaneActionError(item);
      button.disabled = true;
      button.textContent = "Starting…";
      try {
        await formalize(leanPaneView.paneItemToFormalizeTarget(item));
        clearLeanPaneActionError(item);
        await refreshLeanPaneNow({ background: true });
      } catch (error) {
        // Startup can be rejected before Lea creates a run (for example when a
        // declared upstream theorem has not been formalized yet). Keep the
        // action consistent with the manifest state rather than implying an
        // initial formalization effort occurred.
        rememberLeanPaneActionError(item, error, "formalize");
        renderLeanPaneManifest(lastLeanPaneManifest);
      }
    });
    return button;
  }

  // --- Item action row (leanPaneView.paneItemActions) ------------------------
  // One primary text button, an icon rail, and an overflow menu per item card.

  const PANE_ICON_SVG_ATTRS = 'viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
  const PANE_ICON_SVG = {
    "go-to-source": `<svg ${PANE_ICON_SVG_ATTRS}><circle cx="12" cy="12" r="7"></circle><line x1="12" y1="2" x2="12" y2="5"></line><line x1="12" y1="19" x2="12" y2="22"></line><line x1="2" y1="12" x2="5" y2="12"></line><line x1="19" y1="12" x2="22" y2="12"></line></svg>`,
    chat: `<svg ${PANE_ICON_SVG_ATTRS}><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>`,
    "view-in-lea": `<svg ${PANE_ICON_SVG_ATTRS}><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>`,
    copy: `<svg ${PANE_ICON_SVG_ATTRS}><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>`,
    check: `<svg ${PANE_ICON_SVG_ATTRS}><polyline points="20 6 9 17 4 12"></polyline></svg>`,
    more: `<svg ${PANE_ICON_SVG_ATTRS}><circle cx="5" cy="12" r="1"></circle><circle cx="12" cy="12" r="1"></circle><circle cx="19" cy="12" r="1"></circle></svg>`
  };

  function renderPaneItemPrimaryAction(item, action) {
    return action.id === "repair" ? renderRepairButton(item) : renderFormalizeButton(item);
  }

  function renderPaneItemIconAction(item, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ol-lean-icon-action";
    button.title = action.label;
    button.setAttribute("aria-label", action.label);
    button.innerHTML = PANE_ICON_SVG[action.id] || "";
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (action.id === "go-to-source") {
        goToPaneItemSource(item);
        return;
      }
      if (action.id === "chat") {
        openLeanPaneChat(item);
        return;
      }
      if (action.id === "view-in-lea") {
        button.disabled = true;
        if (leanPaneStatus) leanPaneStatus.textContent = "Opening Lea session...";
        try {
          const { sessionOpened } = await openLeaUiForPaneItem(item);
          if (leanPaneStatus) {
            leanPaneStatus.textContent = sessionOpened ? "Opened Lea session." : "Opened Lea UI.";
          }
        } catch (error) {
          if (leanPaneStatus) leanPaneStatus.textContent = normalizeErrorMessage(error);
        } finally {
          button.disabled = false;
        }
      }
    });
    return button;
  }

  function renderPaneOverflowMenu(item, overflowActions) {
    const wrap = document.createElement("div");
    wrap.className = "ol-lean-overflow";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "ol-lean-icon-action";
    trigger.title = "More actions";
    trigger.setAttribute("aria-label", "More actions");
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-expanded", "false");
    trigger.innerHTML = PANE_ICON_SVG.more;
    const menu = document.createElement("div");
    menu.className = "ol-lean-overflow-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    for (const action of overflowActions) {
      const entry = document.createElement("button");
      entry.type = "button";
      entry.className = "ol-lean-overflow-item";
      entry.setAttribute("role", "menuitem");
      entry.textContent = action.label;
      entry.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        closeActiveOverflowMenu();
        runPaneOverflowAction(item, action).catch(renderLeanPaneError);
      });
      menu.appendChild(entry);
    }
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const wasOpen = !menu.hidden;
      closeActiveOverflowMenu();
      if (!wasOpen) {
        menu.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        activeOverflowMenu = { wrap, menu, trigger };
      }
    });
    wrap.appendChild(trigger);
    wrap.appendChild(menu);
    return wrap;
  }

  function closeActiveOverflowMenu() {
    if (!activeOverflowMenu) return;
    activeOverflowMenu.menu.hidden = true;
    activeOverflowMenu.trigger.setAttribute("aria-expanded", "false");
    activeOverflowMenu = null;
  }

  // Overflow actions report through the pane status line (the menu entry is
  // gone once the menu closes, so per-button pending text has nowhere to live).
  // Formalize/stub reuse the same companion paths as the primary button and
  // the in-document popover.
  async function runPaneOverflowAction(item, action) {
    if (action.id === "edit") {
      openLeanPaneEdit(item);
      return;
    }
    if (action.id !== "formalize" && action.id !== "stub") return;
    clearLeanPaneActionError(item);
    if (leanPaneStatus) {
      leanPaneStatus.textContent = action.id === "stub"
        ? "Creating Lean stub..."
        : "Starting formalization...";
    }
    try {
      const target = leanPaneView.paneItemToFormalizeTarget(item);
      await (action.id === "stub" ? stubTheorem(target) : formalize(target));
      clearLeanPaneActionError(item);
      await refreshLeanPaneNow({ background: true });
    } catch (error) {
      rememberLeanPaneActionError(item, error, action.id);
      renderLeanPaneManifest(lastLeanPaneManifest);
    }
  }

  // A highlighted Lean code block with a hover-revealed copy control in its
  // corner -- copy belongs to the content it copies, not to the action row.
  function renderLeanCodeBlock(className, code, copyLabel) {
    const wrap = document.createElement("div");
    wrap.className = "ol-lean-code-block";
    const pre = document.createElement("pre");
    pre.className = className;
    renderLeanPaneCode(pre, code);
    wrap.appendChild(pre);
    wrap.appendChild(renderCopyIconButton(copyLabel, code));
    return wrap;
  }

  function renderCopyIconButton(label, text) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ol-lean-icon-action ol-lean-code-copy";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.innerHTML = PANE_ICON_SVG.copy;
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      try {
        await navigator.clipboard.writeText(text);
        button.innerHTML = PANE_ICON_SVG.check;
        button.title = "Copied";
      } catch {
        button.title = "Copy failed";
      }
      setTimeout(() => {
        button.innerHTML = PANE_ICON_SVG.copy;
        button.title = label;
      }, 1500);
    });
    return button;
  }

  // Open this item's Lea session in the standalone UI, mirroring the popover's
  // "View in Lea UI" action. The session is resolved through the companion's
  // chat-session lookup (read-only: it never creates a session); when no
  // session has been recorded yet, fall back to the Lea UI itself, matching
  // the popover's base-link fallback.
  async function openLeaUiForPaneItem(item) {
    const baseUrl = await chatCompanionBaseUrl();
    const target = leanPaneView.paneItemToChatTarget(item, itemsProjectId(lastLeanPaneManifest?.items || []));
    const response = await fetch(`${baseUrl}/lean-pane/chat/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target })
    });
    const payload = await response.json().catch(() => ({}));
    const sessionUrl = String(payload?.leaSessionUrl || "").trim();
    if (sessionUrl) {
      await openLeaSession({ url: sessionUrl, baseUrl: sessionUrl });
      return { sessionOpened: true };
    }
    const settings = await getSettings();
    const uiBaseUrl = String(settings.leaUiBaseUrl || DEFAULT_LEA_UI_BASE_URL).replace(/\/+$/, "");
    await openLeaSession({ url: uiBaseUrl, baseUrl: uiBaseUrl });
    return { sessionOpened: false };
  }

  function isChatResponseActive(payload) {
    return Boolean(payload && payload.ok !== false && payload.activeRun);
  }

  function ensureChatPanel() {
    if (leanPaneChatPanel && leanPaneChatPanel.isConnected) return leanPaneChatPanel;
    const panel = document.createElement("div");
    panel.className = "ol-lean-chat-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Lea chat");
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closeLeanPaneChat();
      }
    });
    (leanPane || document.body).appendChild(panel);
    leanPaneChatPanel = panel;
    return panel;
  }

  function closeLeanPaneChat() {
    leanPaneChatToken += 1;
    clearTimeout(leanPaneChatPollTimer);
    leanPaneChatPollTimer = null;
    if (leanPaneChatPanel) leanPaneChatPanel.remove();
    leanPaneChatPanel = null;
    leanPaneChatItem = null;
    leanPaneChatTarget = null;
    leanPaneChatResponse = null;
    leanPaneChatSessionId = "";
    leanPaneChatRunId = "";
    leanPaneChatLoading = false;
    leanPaneChatSending = false;
    leanPaneChatError = null;
    leanPaneChatOptimistic = [];
    leanPaneChatPollFailures = 0;
  }

  async function chatCompanionBaseUrl() {
    const settings = await getSettings();
    return String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
  }

  async function openLeanPaneChat(item) {
    const token = ++leanPaneChatToken;
    clearTimeout(leanPaneChatPollTimer);
    leanPaneChatItem = item;
    leanPaneChatTarget = leanPaneView.paneItemToChatTarget(item, itemsProjectId(lastLeanPaneManifest?.items || []));
    leanPaneChatResponse = null;
    leanPaneChatSessionId = "";
    leanPaneChatRunId = "";
    leanPaneChatError = null;
    leanPaneChatOptimistic = [];
    leanPaneChatPollFailures = 0;
    leanPaneChatLoading = true;
    leanPaneChatSending = false;
    ensureChatPanel();
    renderChatPanel();
    const input = leanPaneChatPanel?.querySelector(".ol-lean-chat-input");
    if (input) input.focus();
    try {
      const baseUrl = await chatCompanionBaseUrl();
      const response = await fetch(`${baseUrl}/lean-pane/chat/session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: leanPaneChatTarget })
      });
      const payload = await response.json().catch(() => ({}));
      if (token !== leanPaneChatToken) return;
      leanPaneChatLoading = false;
      leanPaneChatResponse = payload;
      leanPaneChatSessionId = payload.leaSessionId || "";
      if (isChatResponseActive(payload)) {
        leanPaneChatSending = true;
        startChatPolling();
      }
      renderChatPanel();
    } catch (error) {
      if (token !== leanPaneChatToken) return;
      leanPaneChatLoading = false;
      leanPaneChatError = error;
      renderChatPanel();
    }
  }

  async function sendChatMessage() {
    if (!leanPaneChatTarget) return;
    const input = leanPaneChatPanel?.querySelector(".ol-lean-chat-input");
    const text = String(input?.value || "").trim();
    if (!text) return;
    const token = leanPaneChatToken;
    leanPaneChatSending = true;
    leanPaneChatError = null;
    leanPaneChatPollFailures = 0;
    leanPaneChatOptimistic.push({ role: "user", content: text, kind: "user" });
    if (input) input.value = "";
    renderChatPanel();
    try {
      // Flush the latest .tex mirror so Lea sees current source before answering.
      const mirrorResult = await syncTexMirrorNow({ force: true });
      leanPaneChatTarget = {
        ...leanPaneChatTarget,
        ...(await buildFormalizationSourceContext(leanPaneChatTarget, {
          verifyMirror: mirrorResult?.disabled !== true
        }))
      };
      const baseUrl = await chatCompanionBaseUrl();
      const response = await fetch(`${baseUrl}/lean-pane/chat/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: leanPaneChatTarget, message: text })
      });
      const payload = await response.json().catch(() => ({}));
      if (token !== leanPaneChatToken) return;
      if (!response.ok || !payload.ok) {
        leanPaneChatSending = false;
        leanPaneChatError = new Error(payload.message || `Companion returned HTTP ${response.status}.`);
        renderChatPanel();
        return;
      }
      leanPaneChatSessionId = payload.leaSessionId || leanPaneChatSessionId;
      leanPaneChatRunId = payload.runId || "";
      startChatPolling();
      renderChatPanel();
    } catch (error) {
      if (token !== leanPaneChatToken) return;
      leanPaneChatSending = false;
      leanPaneChatError = error;
      renderChatPanel();
    }
  }

  function startChatPolling(delayMs = (pushConnected ? LEAN_PANE_CHAT_POLL_RECONCILE_MS : LEAN_PANE_POLL_DELAY_MS)) {
    clearTimeout(leanPaneChatPollTimer);
    leanPaneChatPollTimer = setTimeout(() => {
      // pollChatSession handles its own transient errors; the catch is a
      // last-resort guard against an unexpected synchronous throw.
      pollChatSession().catch(() => {});
    }, delayMs);
  }

  async function pollChatSession() {
    if (!leanPaneChatSessionId) return;
    const token = leanPaneChatToken;
    let payload = null;
    try {
      const baseUrl = await chatCompanionBaseUrl();
      const response = await fetch(`${baseUrl}/lean-pane/chat/session/${encodeURIComponent(leanPaneChatSessionId)}`);
      payload = await response.json().catch(() => ({}));
    } catch {
      // Transient failure (companion restarting, network blip). AUDIT M2: do
      // NOT stop polling and strand the panel on "Lea is working…" — retry
      // with backoff up to a cap, only surfacing an error once we've truly
      // given up.
      if (token !== leanPaneChatToken) return;
      leanPaneChatPollFailures += 1;
      if (leanPaneChatPollFailures >= LEAN_PANE_CHAT_POLL_MAX_FAILURES) {
        leanPaneChatSending = false;
        leanPaneChatError = new Error("Lost contact with the companion while waiting for Lea. Check that it's running, then try again.");
        renderChatPanel();
        return;
      }
      startChatPolling(LEAN_PANE_POLL_DELAY_MS * (leanPaneChatPollFailures + 1));
      return;
    }
    if (token !== leanPaneChatToken) return;
    leanPaneChatPollFailures = 0;
    if (payload && payload.ok) {
      leanPaneChatResponse = payload;
      leanPaneChatOptimistic = [];
      if (isChatResponseActive(payload)) {
        leanPaneChatSending = true;
        startChatPolling();
      } else {
        const wasRunning = leanPaneChatSending;
        leanPaneChatSending = false;
        // A finished chat run may have changed the item's artifact/status.
        if (wasRunning) refreshLeanPaneNow({ background: true }).catch(() => {});
      }
    } else {
      // Adapter reachable but reporting unavailable mid-run: surface it and
      // stop polling.
      leanPaneChatSending = false;
      leanPaneChatResponse = payload;
    }
    renderChatPanel();
  }

  async function stopChatRun() {
    const token = leanPaneChatToken;
    try {
      const baseUrl = await chatCompanionBaseUrl();
      await fetch(`${baseUrl}/lean-pane/chat/interrupt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runId: leanPaneChatRunId, sessionId: leanPaneChatSessionId })
      });
    } catch {
      // best-effort; the next poll reflects the actual run state
    }
    if (token === leanPaneChatToken) pollChatSession().catch(() => {});
  }

  function chatTranscriptMessages() {
    const persisted = Array.isArray(leanPaneChatResponse?.messages) ? leanPaneChatResponse.messages : [];
    return [...persisted, ...leanPaneChatOptimistic];
  }

  function renderChatPanel() {
    if (!leanPaneChatPanel) return;
    const panel = leanPaneChatPanel;
    const item = leanPaneChatItem || {};
    const state = leanPaneView.nextChatState({
      loading: leanPaneChatLoading,
      sending: leanPaneChatSending,
      response: leanPaneChatResponse,
      error: leanPaneChatError
    });
    panel.dataset.chatState = state;
    // Autoscroll only when the user is already pinned to the bottom, so live
    // answers follow along but reading earlier messages isn't yanked away.
    const previousTranscript = panel.querySelector(".ol-lean-chat-transcript");
    const pinToBottom = previousTranscript
      ? (previousTranscript.scrollHeight - previousTranscript.scrollTop - previousTranscript.clientHeight) < 48
      : true;
    panel.replaceChildren();

    // Header: declaration name + pane status chip + close
    const header = document.createElement("div");
    header.className = "ol-lean-chat-header";
    const title = document.createElement("span");
    title.className = "ol-lean-chat-title";
    title.textContent = item.leanDeclarationName || item.label || "Lea chat";
    header.appendChild(title);
    const chip = document.createElement("span");
    chip.className = `ol-lean-project-status ol-lean-project-status-${item.status || "unknown"}`;
    chip.textContent = leanPaneView.formatPaneStatus(item.status || "unknown");
    header.appendChild(chip);
    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "ol-lean-chat-close";
    closeButton.setAttribute("aria-label", "Close chat");
    closeButton.textContent = "✕";
    closeButton.addEventListener("click", closeLeanPaneChat);
    header.appendChild(closeButton);
    panel.appendChild(header);

    if (item.sourceFile) {
      const source = document.createElement("p");
      source.className = "ol-lean-chat-source";
      source.textContent = item.sourceStartLine
        ? `${item.sourceFile}:${item.sourceStartLine}-${item.sourceEndLine || item.sourceStartLine}`
        : item.sourceFile;
      panel.appendChild(source);
    }

    // Transcript
    const transcript = document.createElement("div");
    transcript.className = "ol-lean-chat-transcript";
    if (state === "loading-session") {
      transcript.appendChild(chatNotice("Loading conversation…"));
    } else if (state === "adapter-unavailable") {
      transcript.appendChild(chatNotice(leanPaneChatResponse?.message || "The Lea adapter is unavailable."));
    } else {
      const messages = chatTranscriptMessages();
      if (messages.length === 0 && state === "no-session") {
        transcript.appendChild(chatNotice("No conversation yet. Ask Lea about this item to start one."));
      } else if (messages.length === 0) {
        transcript.appendChild(chatNotice("No messages yet."));
      } else {
        for (const message of messages) {
          transcript.appendChild(renderChatBubble(message));
        }
      }
      if (leanPaneChatSending) transcript.appendChild(chatNotice("Lea is working…"));
      // Self-repair: the last completed run's downstream impact (companion
      // Phase 1 post-run cascade) -- broken dependents + one repair action.
      const impactNotice = renderChatRunImpactNotice(leanPaneChatResponse?.lastRunImpact);
      if (impactNotice) transcript.appendChild(impactNotice);
    }
    panel.appendChild(transcript);

    if (leanPaneChatError) {
      const error = document.createElement("p");
      error.className = "ol-lean-chat-error";
      error.textContent = normalizeErrorMessage(leanPaneChatError);
      panel.appendChild(makeLeanPaneErrorDismissible(error, {
        onDismiss: () => {
          leanPaneChatError = null;
          renderChatPanel();
        }
      }));
    }

    // Composer
    const composer = document.createElement("div");
    composer.className = "ol-lean-chat-composer";
    const input = document.createElement("textarea");
    input.className = "ol-lean-chat-input";
    input.rows = 2;
    input.placeholder = "Ask Lea about this item...";
    input.disabled = !leanPaneView.chatComposerEnabled(state);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (leanPaneView.chatComposerEnabled(state)) sendChatMessage();
      }
    });
    composer.appendChild(input);

    const controls = document.createElement("div");
    controls.className = "ol-lean-chat-controls";
    if (leanPaneView.chatRunActive(state)) {
      const stop = document.createElement("button");
      stop.type = "button";
      stop.className = "ol-lean-secondary-button ol-lean-chat-stop";
      stop.textContent = "Stop";
      stop.addEventListener("click", stopChatRun);
      controls.appendChild(stop);
    } else {
      const send = document.createElement("button");
      send.type = "button";
      send.className = "ol-lean-primary-button ol-lean-chat-send";
      send.textContent = "Send";
      send.disabled = !leanPaneView.chatComposerEnabled(state);
      send.addEventListener("click", () => sendChatMessage());
      controls.appendChild(send);
    }
    const sessionUrl = leanPaneChatResponse?.leaSessionUrl || "";
    if (sessionUrl) {
      const open = document.createElement("a");
      open.className = "ol-lean-chat-open";
      // Keep href + target as a safe fallback (and for context-menu / modifier
      // clicks), but route a plain click through the background worker so an
      // already-open Lea tab is focused and navigated instead of duplicated.
      open.href = sessionUrl;
      open.target = "_blank";
      open.rel = "noopener noreferrer";
      open.textContent = "Open in Lea";
      open.addEventListener("click", (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;
        event.preventDefault();
        // baseUrl defaults to the session URL's origin, which is what the worker
        // matches existing Lea tabs against.
        openLeaSession({ url: sessionUrl, baseUrl: sessionUrl }).catch((error) => {
          leanPaneChatError = error;
          renderChatPanel();
        });
      });
      controls.appendChild(open);
    }
    composer.appendChild(controls);
    panel.appendChild(composer);

    // Pin to the bottom once the whole panel (composer included) is laid out, so
    // the transcript's final flex height is settled before we scroll.
    if (pinToBottom) transcript.scrollTop = transcript.scrollHeight;
  }

  function chatNotice(text) {
    const notice = document.createElement("p");
    notice.className = "ol-lean-chat-notice";
    notice.textContent = text;
    return notice;
  }

  // "This change broke N downstream items" after a chat run whose post-run
  // cascade found breakage, with the same repair affordance the pane offers.
  // The stored impact is the HISTORICAL record of what the run broke; the
  // companion annotates each entry with its live state (stillBroken /
  // nowRepairing, PLAN-self-repair-stale-offers Fix 2), and the counts and
  // the repair offer here derive from that live state only.
  function renderChatRunImpactNotice(lastRunImpact) {
    const raw = Array.isArray(lastRunImpact?.dependentsImpact) ? lastRunImpact.dependentsImpact : [];
    if (raw.length === 0) return null;
    // Map the server annotation onto the reconciliation fields
    // (stillBroken is LIVE truth, so `matched` when present); an unannotated
    // entry (older record) keeps its snapshot state via the unmatched
    // fallback in stillBrokenDependents.
    const dependents = raw.map((d) => ({
      ...d,
      matched: d.stillBroken !== undefined,
      nowBroken: d.stillBroken === true,
      sinceFixed: Boolean(d.brokenByUpstream) && d.stillBroken === false && !d.nowRepairing,
      nowRepairing: Boolean(d.nowRepairing)
    }));
    const broken = leanPaneView.stillBrokenDependents(dependents);
    const everBroken = dependents.filter((d) => d.brokenByUpstream);

    const container = document.createElement("div");
    container.className = "ol-lean-chat-impact";
    const heading = document.createElement("p");
    heading.textContent = broken.length > 0
      ? `This change broke ${broken.length} downstream item${broken.length === 1 ? "" : "s"}:`
      : everBroken.length > 0
        ? `This change broke ${everBroken.length} downstream item${everBroken.length === 1 ? "" : "s"} -- all since fixed or being repaired:`
        : `This change touched ${dependents.length} downstream item${dependents.length === 1 ? "" : "s"} (re-verified):`;
    container.appendChild(heading);
    for (const dependent of dependents) {
      const line = document.createElement("p");
      line.className = "ol-lean-chat-impact-item";
      line.textContent = leanPaneView.formatDependentOutcome(dependent);
      container.appendChild(line);
    }
    if (broken.length > 0) {
      const repairAll = document.createElement("button");
      repairAll.type = "button";
      repairAll.className = "ol-lean-primary-button ol-lean-repair-all-button";
      repairAll.textContent = `Repair all (${broken.length})`;
      repairAll.addEventListener("click", () => {
        requestRepair({
          overleafProjectId: leanPaneChatTarget?.overleafProjectId || itemsProjectId(lastLeanPaneManifest?.items || []),
          items: broken.map((d) => ({ targetKind: "theorem", targetLabel: d.targetLabel }))
        });
      });
      container.appendChild(repairAll);
    }
    return container;
  }

  function renderChatBubble(message) {
    const bubble = document.createElement("div");
    bubble.className = `ol-lean-chat-bubble ${leanPaneView.chatBubbleClass(message.role)}`;
    renderChatMarkdown(bubble, message.content || "");
    return bubble;
  }

  // Minimal inline rendering: paragraphs, `inline code`, and **bold**. The mirror
  // shows the persisted transcript verbatim; it never rewrites stored messages.
  function renderChatMarkdown(container, text) {
    const lines = String(text || "").split(/\r?\n/);
    lines.forEach((line, index) => {
      renderChatInline(container, line);
      if (index < lines.length - 1) container.appendChild(document.createElement("br"));
    });
  }

  function renderChatInline(container, line) {
    const tokenRe = /(`[^`]+`)|(\*\*[^*]+\*\*)/g;
    let lastIndex = 0;
    let match;
    while ((match = tokenRe.exec(line)) !== null) {
      if (match.index > lastIndex) {
        container.appendChild(document.createTextNode(line.slice(lastIndex, match.index)));
      }
      if (match[1]) {
        const code = document.createElement("code");
        code.textContent = match[1].slice(1, -1);
        container.appendChild(code);
      } else if (match[2]) {
        const strong = document.createElement("strong");
        strong.textContent = match[2].slice(2, -2);
        container.appendChild(strong);
      }
      lastIndex = tokenRe.lastIndex;
    }
    if (lastIndex < line.length) {
      container.appendChild(document.createTextNode(line.slice(lastIndex)));
    }
  }

  function renderLeanPaneError(error) {
    if (!leanPaneBody || !leanPaneStatus) return;
    leanPaneStatus.textContent = "Could not load Lean pane.";
    leanPaneBody.replaceChildren();
    const message = document.createElement("p");
    message.className = "ol-lean-project-pane-error";
    message.textContent = normalizeErrorMessage(error);
    leanPaneBody.appendChild(makeLeanPaneErrorDismissible(message, {
      onDismiss: () => {
        if (leanPaneMainView === "blueprint" && leanPaneBlueprintGraph) {
          renderBlueprintBody(leanPaneBlueprintGraph);
        } else if (leanPaneMainView === "items" && lastLeanPaneManifest) {
          renderLeanPaneManifest(lastLeanPaneManifest);
        } else if (leanPaneStatus) {
          leanPaneStatus.textContent = "Error dismissed. Refresh to try again.";
        }
      }
    }));
  }

  function showTargetPopover(clientX, clientY, target) {
    if (target?.syntax === "diagnostic") {
      showDiagnosticPopover(clientX, clientY, target);
      return;
    }
    closePopover();

    const popover = document.createElement("div");
    popover.className = "ol-lean-theorem-popover";
    popover.innerHTML = `
      <p class="ol-lean-popover-title">Lean formalization</p>
      <p class="ol-lean-popover-meta">Label: <strong></strong></p>
      <div class="ol-lean-popover-actions" data-role="theorem-actions"></div>
      <pre class="ol-lean-popover-lean" hidden></pre>
      <p class="ol-lean-popover-warning" hidden></p>
      <p class="ol-lean-popover-status"></p>
    `;

    const key = targetKey(target);
    popover.dataset.targetKey = key;
    popover.querySelector("strong").textContent = target.targetLabel;
    const actions = popover.querySelector("[data-role='theorem-actions']");
    const status = popover.querySelector(".ol-lean-popover-status");
    const leanStatement = popover.querySelector(".ol-lean-popover-lean");
    const stubbedWarning = popover.querySelector(".ol-lean-popover-warning");
    const statusInfo = latestStatuses[key] || {};
    const currentStatus = getDisplayStatus(statusInfo);
    const actionStatus = getActionStatus(statusInfo);
    renderLeanStatement(leanStatement, statusInfo.leanStatement || "");
    renderTargetWarning(stubbedWarning, target, statusInfo);
    renderTargetActions(actions, target, currentStatus, status, leanStatement, actionStatus, statusInfo);
    if (currentStatus === "in_progress") {
      status.textContent = inProgressMessage(latestStatuses[key], target);
    } else if (isExtensionContextInvalidated()) {
      status.textContent = "Extension was reloaded. Refresh this Overleaf tab.";
    }

    document.body.appendChild(popover);
    positionPopover(popover, clientX, clientY);
    activePopover = popover;
  }

  function showDiagnosticPopover(clientX, clientY, diagnostic) {
    if (!diagnostic) return;
    closePopover();

    const popover = document.createElement("div");
    popover.className = "ol-lean-theorem-popover";
    popover.innerHTML = `
      <p class="ol-lean-popover-title">Lea marker problem</p>
      <p class="ol-lean-popover-meta">Issue: <strong></strong></p>
      <div class="ol-lean-popover-actions" data-role="theorem-actions"></div>
      <pre class="ol-lean-popover-lean" hidden></pre>
      <p class="ol-lean-popover-warning" hidden></p>
      <p class="ol-lean-popover-status"></p>
    `;

    popover.dataset.diagnosticCode = diagnostic.code || "marker_error";
    popover.querySelector("strong").textContent = diagnostic.code || "marker_error";
    const actions = popover.querySelector("[data-role='theorem-actions']");
    const status = popover.querySelector(".ol-lean-popover-status");
    status.textContent = diagnostic.message || "This Lea marker is malformed.";

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.textContent = "Close";
    closeButton.addEventListener("click", closePopover);
    actions.appendChild(closeButton);

    document.body.appendChild(popover);
    positionPopover(popover, clientX, clientY);
    activePopover = popover;
  }

  function renderTargetActions(actions, target, currentStatus, status, leanStatement, actionStatus = currentStatus, statusInfo = {}) {
    actions.replaceChildren();
    if (statusInfo.githubImportPending) {
      const checking = document.createElement("button");
      checking.type = "button";
      checking.textContent = "Checking import…";
      checking.disabled = true;
      checking.title = "This item cannot be formalized again until its imported Lean proof has been checked.";
      actions.appendChild(checking);
      return;
    }
    const disabled = currentStatus === "in_progress" || isExtensionContextInvalidated();
    const actionSpecs = actionSpecsForStatus(actionStatus, target);
    for (const spec of actionSpecs) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = spec.label;
      button.dataset.role = spec.role;
      if (spec.primary) {
        button.dataset.primary = "true";
      }
      button.disabled = disabled;
      button.addEventListener("click", async () => {
        for (const actionButton of actions.querySelectorAll("button")) {
          actionButton.disabled = true;
        }
        renderPopoverActionStatus(status, spec.pendingText);
        try {
          const result = await spec.run(target);
          renderPopoverActionStatus(
            status,
            `${formatStatus(result.status, result)}${result.relativePath ? ` at ${result.relativePath}` : ""}`
          );
          renderLeanStatement(leanStatement, result.leanStatement || latestStatuses[targetKey(target)]?.leanStatement || "");
          await refreshStatusesNow();
        } catch (error) {
          renderPopoverActionError(status, error);
          const latestStatus = latestStatuses[targetKey(target)] || { status: currentStatus };
          renderTargetActions(actions, target, latestStatus.status || currentStatus, status, leanStatement, getActionStatus(latestStatus));
        }
      });
      actions.appendChild(button);
    }

    const leaSession = getLeaSessionLink(statusInfo);
    // Only statuses that represent a real Lea run or saved proof artifact should
    // offer a route into the Lea UI. Stale session metadata must not make
    // unformalized/unknown theorems appear viewable.
    const showLeaUiButton = canViewInLeaUi(actionStatus);
    if (showLeaUiButton) {
      const leaUiLink = leaSession || getLeaUiBaseLink(statusInfo);
      const sessionButton = document.createElement("button");
      sessionButton.type = "button";
      sessionButton.textContent = "View in Lea UI";
      sessionButton.dataset.role = "open-lea-session";
      sessionButton.disabled = isExtensionContextInvalidated();
      sessionButton.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        status.textContent = leaUiLink.sessionId ? "Opening Lea session..." : "Opening Lea UI...";
        try {
          await openLeaSession(leaUiLink);
          status.textContent = leaUiLink.sessionId ? "Opened Lea session." : "Opened Lea UI.";
        } catch (error) {
          status.textContent = error instanceof Error ? error.message : String(error);
        }
      });
      actions.appendChild(sessionButton);
    }

    const paneButton = document.createElement("button");
    paneButton.type = "button";
    paneButton.textContent = "Show in Lean pane";
    paneButton.dataset.role = "show-in-lean-pane";
    paneButton.disabled = isExtensionContextInvalidated();
    paneButton.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      status.textContent = "Opening Lean pane...";
      try {
        const item = await showTargetInLeanPane(target);
        status.textContent = `Opened ${item.label || target.targetLabel} in the Lean pane.`;
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
      }
    });
    actions.appendChild(paneButton);

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.textContent = "Close";
    closeButton.addEventListener("click", closePopover);
    actions.appendChild(closeButton);
  }

  function renderPopoverActionStatus(status, message) {
    status.classList.remove("ol-lean-popover-status-error");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.textContent = message;
  }

  function renderPopoverActionError(status, error) {
    const dependencyBlocked = isUnresolvedUsesError(error);
    status.classList.add("ol-lean-popover-status-error");
    status.setAttribute("role", "alert");
    status.setAttribute("aria-live", "assertive");
    status.textContent = "";
    status.replaceChildren();

    const title = document.createElement("strong");
    title.textContent = dependencyBlocked
      ? "Formalization blocked"
      : "Action failed";
    const message = document.createElement("span");
    message.textContent = actionFailureMessage(error);
    status.appendChild(title);
    status.appendChild(message);
  }

  async function showTargetInLeanPane(target) {
    if (!leanPane) {
      showLeanPane({ deferRefresh: true, preservePopover: true });
    }
    await refreshLeanPaneNow({
      forceFetch: !lastLeanPaneManifest,
      background: Boolean(lastLeanPaneManifest)
    });
    const item = findLeanPaneItemForTarget(lastLeanPaneManifest?.items || [], target);
    if (!item) {
      throw new Error(`Could not find ${target.targetLabel || "this item"} in the Lean pane.`);
    }
    revealLeanPaneItem(item);
    return item;
  }

  function revealLeanPaneItem(item) {
    if (!item || !lastLeanPaneManifest) return false;
    for (const id of leanPaneView.treeAncestorIdsForFile(item.sourceFile || "")) {
      leanPaneExpandedTreeNodeIds.add(id);
    }
    leanPaneExpandedItemIds.add(item.id);
    renderLeanPaneManifest(lastLeanPaneManifest);
    highlightLeanPaneItem(item.id);
    return true;
  }

  function findLeanPaneItemForTarget(items, target) {
    const targetLabel = String(target?.targetLabel || "").trim();
    const latexLabel = String(target?.latexLabel || "").trim();
    const targetFrom = Number(target?.from);
    const targetTo = Number(target?.to);
    const activePath = normalizeDocPath(latestActiveTexPath);
    let best = null;

    for (const item of Array.isArray(items) ? items : []) {
      let score = 0;
      if (targetLabel && item?.label === targetLabel) score += 100;
      if (targetLabel && item?.leanDeclarationName === targetLabel) score += 100;
      if (latexLabel && item?.latexLabel === latexLabel) score += 80;
      if (
        Number.isFinite(targetFrom) &&
        Number.isFinite(targetTo) &&
        item?.sourceStartOffset === targetFrom &&
        item?.sourceEndOffset === targetTo
      ) {
        score += 60;
      }
      if (activePath && normalizeDocPath(item?.sourceFile) === activePath) score += 5;
      if (score >= 60 && (!best || score > best.score)) {
        best = { item, score };
      }
    }
    return best?.item || null;
  }

  function highlightLeanPaneItem(itemId) {
    if (!leanPaneBody) return;
    const element = [...leanPaneBody.querySelectorAll(".ol-lean-project-item")]
      .find((candidate) => candidate.dataset.itemId === itemId);
    if (!element) return;
    element.classList.add("ol-lean-project-item-focus");
    element.scrollIntoView?.({ block: "center", behavior: "smooth" });
    clearTimeout(leanPaneHighlightTimer);
    leanPaneHighlightTimer = setTimeout(() => {
      element.classList.remove("ol-lean-project-item-focus");
    }, 1800);
  }

  function actionSpecsForStatus(status, target) {
    const definition = isDefinitionTarget(target);
    if (status === "unformalized") {
      const specs = [{
        role: "target-action",
        label: definition ? "Formalize definition" : "Formalize",
        primary: true,
        pendingText: "Starting Lea...",
        run: formalize
      }];
      if (!definition) {
        specs.push({
          role: "theorem-stub-action",
          label: "Stub",
          primary: false,
          pendingText: "Creating Lean stub...",
          run: stubTheorem
        });
      }
      return specs;
    }
    if (status === "sorry_stub") {
      return [{
        role: "theorem-action",
        label: "Formalize",
        primary: true,
        pendingText: "Starting Lea...",
        run: formalize
      }];
    }
    if (status === "formalized" || status === "defined" || status === "disproved") {
      return [{
        role: "theorem-action",
        label: definition ? "Regenerate definition" : "Re-formalize",
        primary: true,
        pendingText: "Starting Lea...",
        run: formalize
      }];
    }
    if (status === "unknown") {
      return [{
        role: "theorem-action",
        label: "Check status",
        primary: true,
        pendingText: "Checking Lea status...",
        run: refreshSingleStatus
      }];
    }
    return [{
      role: "theorem-action",
      label: buttonTextForStatus(status, target),
      primary: true,
      pendingText: "Starting Lea...",
      run: formalize
    }];
  }

  function showSettingsPopover() {
    closePopover();

    const popover = document.createElement("div");
    popover.className = "ol-lean-popover ol-lean-settings-popover";
    popover.innerHTML = `
      <div class="ol-lean-popover-arrow ol-lean-popover-arrow-bottom" aria-hidden="true"></div>
      <div class="ol-lean-popover-header">
        <div class="ol-lean-popover-kicker">
          <span class="ol-lean-popover-mark" aria-hidden="true">L</span>
          <span>Extension Settings</span>
        </div>
        <button type="button" class="ol-lean-icon-button" data-role="close" aria-label="Close Lea popover">x</button>
      </div>
      <div class="ol-lean-popover-body">
        <section class="ol-lean-project-identity-panel" data-role="project-identity">
          <div class="ol-lean-provider-title">Project</div>
          <div class="ol-lean-provider-row">
            <div class="ol-lean-provider-row-head">
              <span data-role="project-name">Overleaf Project</span>
              <strong data-role="project-exists">Not created</strong>
            </div>
            <p class="ol-lean-provider-note" data-role="project-namespace">Lean namespace: --</p>
            <p class="ol-lean-provider-note" data-role="project-binding">Overleaf binding: --</p>
            <p class="ol-lean-provider-note ol-lean-project-message" data-role="project-message"></p>
            <div class="ol-lean-provider-key-controls">
              <button type="button" class="ol-lean-provider-key-button" data-role="edit-project-name">Edit name</button>
            </div>
          </div>
        </section>
        <section class="ol-lean-usage-panel" aria-live="polite">
          <div class="ol-lean-usage-row" data-usage="project">
            <div class="ol-lean-usage-row-head">
              <span>This project</span>
              <strong data-field="cost">--</strong>
            </div>
            <div class="ol-lean-usage-metrics">
              <span><small>In</small><strong data-field="input">--</strong></span>
              <span><small>Out</small><strong data-field="output">--</strong></span>
            </div>
          </div>
          <div class="ol-lean-usage-separator"></div>
          <div class="ol-lean-usage-row" data-usage="allTime">
            <div class="ol-lean-usage-row-head">
              <span>All-time</span>
              <strong data-field="cost">--</strong>
            </div>
            <div class="ol-lean-usage-metrics">
              <span><small>In</small><strong data-field="input">--</strong></span>
              <span><small>Out</small><strong data-field="output">--</strong></span>
            </div>
          </div>
          <p class="ol-lean-usage-cap" data-role="cost-cap-summary" hidden></p>
        </section>
        <section class="ol-lean-provider-panel" data-role="provider-keys">
          <div class="ol-lean-provider-title">Model families</div>
          <p class="ol-lean-provider-note">Keys are saved to the root .env by the companion, not to Chrome or settings.json.</p>
          ${Object.entries(MODEL_FAMILY_LABELS).map(([family, label]) => `
            <div class="ol-lean-provider-row" data-family="${family}">
              <div class="ol-lean-provider-row-head">
                <span>${label}</span>
                <strong data-role="provider-status">Missing</strong>
              </div>
              <div class="ol-lean-provider-key-controls">
                <button type="button" class="ol-lean-provider-key-button" data-role="provider-key-toggle" data-family="${family}">Add key</button>
                <input type="password" autocomplete="off" spellcheck="false" data-role="provider-key-input" data-family="${family}" placeholder="${label} API key" hidden>
              </div>
            </div>
          `).join("")}
        </section>
        <section class="ol-lean-provider-panel ol-lean-github-token-panel" data-role="github-token-panel">
          <div class="ol-lean-provider-title">GitHub sharing</div>
          <div class="ol-lean-github-token-card">
            <div class="ol-lean-github-token-summary">
              <span class="ol-lean-github-token-mark" aria-hidden="true">GH</span>
              <div class="ol-lean-github-token-copy">
                <strong>Repository access</strong>
                <span data-role="github-token-description">Add a token to push Lean projects to GitHub.</span>
              </div>
              <strong class="ol-lean-github-token-status" data-role="github-token-status" aria-live="polite">Not set</strong>
            </div>
            <p class="ol-lean-github-token-storage-note">
              <svg class="ol-lean-github-token-lock" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <rect x="3" y="7" width="10" height="7" rx="2"></rect>
                <path d="M5.25 7V5.25a2.75 2.75 0 0 1 5.5 0V7"></path>
              </svg>
              <span>Stored locally by Lea, never in Chrome. It is used only when you choose <strong>Push to GitHub</strong>.</span>
            </p>
            <div class="ol-lean-github-token-actions" data-role="github-token-summary-actions">
              <button type="button" class="ol-lean-provider-key-button" data-role="github-token-clear" data-variant="danger" hidden>Remove token</button>
              <button type="button" class="ol-lean-provider-key-button" data-role="github-token-toggle" data-variant="primary">Add GitHub token</button>
            </div>
            <div class="ol-lean-github-token-editor" data-role="github-token-editor" hidden>
              <form data-role="github-token-form">
                <label class="ol-lean-github-token-label" for="ol-lean-github-token-input">Personal access token</label>
                <div class="ol-lean-github-token-field">
                  <input id="ol-lean-github-token-input" type="password" autocomplete="off" spellcheck="false" data-role="github-token-input" placeholder="github_pat_... or ghp_..." aria-describedby="ol-lean-github-token-help" required>
                  <button type="button" data-role="github-token-visibility" aria-label="Show GitHub token" aria-pressed="false">Show</button>
                </div>
                <p id="ol-lean-github-token-help" class="ol-lean-provider-note">Use a personal access token with permission to write to the repository. For security, the saved value cannot be shown again.</p>
                <div class="ol-lean-github-token-form-actions">
                  <button type="button" class="ol-lean-provider-key-button" data-role="github-token-cancel">Cancel</button>
                  <button type="submit" class="ol-lean-provider-key-button" data-role="github-token-save" data-variant="primary">Save token</button>
                </div>
              </form>
            </div>
          </div>
        </section>
        <section class="ol-lean-settings-panel">
          <div class="ol-lean-model-field">
            <span>Model</span>
            <div data-role="model"></div>
          </div>
          <p class="lea-model-requirement-note" data-role="model-catalog-status"></p>
          <div class="lea-model-requirements" data-role="model-requirements" aria-live="polite"></div>
          <label>
            <span>Max turns</span>
            <input type="number" min="1" max="200" data-role="max-turns">
          </label>
          <label>
            <span>Cost cap (USD)</span>
            <input type="number" min="0" step="0.01" data-role="max-spend" placeholder="None">
          </label>
          <label class="ol-lean-checkbox-field">
            <input type="checkbox" data-role="tex-mirror">
            <span>Mirror Overleaf LaTeX sources into the project</span>
          </label>
          <button type="button" class="ol-lean-save-button" data-role="save-settings" disabled>Save changes</button>
        </section>
      </div>
      <p class="ol-lean-popover-status" role="status"></p>
    `;

    const resizer = document.createElement("button");
    resizer.type = "button";
    resizer.className = "ol-lean-settings-popover-resizer";
    resizer.setAttribute("role", "separator");
    resizer.setAttribute("aria-orientation", "vertical");
    resizer.setAttribute("aria-label", "Resize Lea settings");
    resizer.title = "Resize Lea settings";
    resizer.tabIndex = 0;
    resizer.addEventListener("pointerdown", startSettingsPopoverResize);
    resizer.addEventListener("mousedown", startSettingsPopoverResize);
    resizer.addEventListener("keydown", handleSettingsPopoverResizeKeydown);
    popover.insertBefore(resizer, popover.children[0] || null);

    const closeButton = popover.querySelector("[data-role='close']");
    const status = popover.querySelector(".ol-lean-popover-status");
    const modelSelect = popover.querySelector("[data-role='model']");
    const maxTurnsInput = popover.querySelector("[data-role='max-turns']");
    const maxSpendInput = popover.querySelector("[data-role='max-spend']");
    const texMirrorInput = popover.querySelector("[data-role='tex-mirror']");
    const saveButton = popover.querySelector("[data-role='save-settings']");

    closeButton.addEventListener("click", closePopover);
    modelSelect.addEventListener("change", () => {
      markSettingsDirty();
      void loadPopoverModelRequirements(popover, modelSelect.value);
    });
    maxTurnsInput.addEventListener("input", markSettingsDirty);
    maxSpendInput.addEventListener("input", markSettingsDirty);
    texMirrorInput.addEventListener("change", markSettingsDirty);
    for (const button of popover.querySelectorAll("[data-role='provider-key-toggle']")) {
      button.addEventListener("click", () => {
        const input = popover.querySelector(`[data-role='provider-key-input'][data-family='${button.dataset.family}']`);
        if (!input) return;
        input.hidden = false;
        input.focus();
      });
    }
    for (const input of popover.querySelectorAll("[data-role='provider-key-input']")) {
      input.addEventListener("input", () => {
        updatePopoverRequirementSummary(popover);
        markSettingsDirty();
      });
    }

    // GitHub push token (D34): saved immediately via its own companion endpoint
    // (write-through to the adapter's lea.local.toml), independent of the main
    // "Save changes" flow. Presence-only display; the raw token is never read back.
    const githubToggle = popover.querySelector("[data-role='github-token-toggle']");
    const githubClear = popover.querySelector("[data-role='github-token-clear']");
    const githubSummaryActions = popover.querySelector("[data-role='github-token-summary-actions']");
    const githubEditor = popover.querySelector("[data-role='github-token-editor']");
    const githubForm = popover.querySelector("[data-role='github-token-form']");
    const githubInput = popover.querySelector("[data-role='github-token-input']");
    const githubSave = popover.querySelector("[data-role='github-token-save']");
    const githubCancel = popover.querySelector("[data-role='github-token-cancel']");
    const githubVisibility = popover.querySelector("[data-role='github-token-visibility']");

    const closeGithubTokenEditor = () => {
      githubInput.value = "";
      githubInput.type = "password";
      githubVisibility.textContent = "Show";
      githubVisibility.setAttribute("aria-label", "Show GitHub token");
      githubVisibility.setAttribute("aria-pressed", "false");
      githubEditor.hidden = true;
      githubSummaryActions.hidden = false;
    };

    githubToggle.addEventListener("click", () => {
      githubSummaryActions.hidden = true;
      githubEditor.hidden = false;
      status.textContent = "";
      githubInput.focus();
    });
    githubCancel.addEventListener("click", () => {
      closeGithubTokenEditor();
      githubToggle.focus();
    });
    githubVisibility.addEventListener("click", () => {
      const reveal = githubInput.type === "password";
      githubInput.type = reveal ? "text" : "password";
      githubVisibility.textContent = reveal ? "Hide" : "Show";
      githubVisibility.setAttribute("aria-label", `${reveal ? "Hide" : "Show"} GitHub token`);
      githubVisibility.setAttribute("aria-pressed", reveal ? "true" : "false");
      githubInput.focus();
    });
    githubForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = githubInput.value.trim();
      if (!value) {
        status.textContent = "Enter a GitHub personal access token.";
        githubInput.focus();
        return;
      }
      githubSave.disabled = true;
      githubCancel.disabled = true;
      githubVisibility.disabled = true;
      status.textContent = "Saving GitHub token...";
      try {
        await updateGithubToken({ value });
        closeGithubTokenEditor();
        renderGithubTokenStatus(popover, true);
        status.textContent = "GitHub token saved. Push to GitHub is ready.";
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        githubSave.disabled = false;
        githubCancel.disabled = false;
        githubVisibility.disabled = false;
      }
    });
    githubClear.addEventListener("click", async () => {
      githubClear.disabled = true;
      status.textContent = "Removing GitHub token...";
      try {
        await updateGithubToken({ clear: true });
        closeGithubTokenEditor();
        renderGithubTokenStatus(popover, false);
        status.textContent = "GitHub token removed. GitHub pushes are disabled.";
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
      } finally {
        githubClear.disabled = false;
      }
    });
    const editProjectName = popover.querySelector("[data-role='edit-project-name']");
    editProjectName.addEventListener("click", async () => {
      status.textContent = "";
      try {
        await openProjectIdentityEditor({ source: "settings", popover, trigger: editProjectName });
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
      }
    });
    saveButton.addEventListener("click", async () => {
      saveButton.disabled = true;
      status.textContent = "Saving Lea settings...";
      try {
        const settings = await savePopoverSettings(popover);
        popover.dataset.savedModel = settings.leaModel;
        popover.dataset.savedMaxTurns = String(settings.leaMaxTurns);
        popover.dataset.savedMaxSpend = settings.leaMaxSpendUsd == null ? "" : String(settings.leaMaxSpendUsd);
        popover.dataset.savedTexMirror = String(settings.leaTexMirrorEnabled !== false);
        popover.leaApiKeys = settings.leaApiKeys || popover.leaApiKeys || {};
        renderProviderKeys(popover, settings.leaProviderKeys || {});
        clearProviderKeyInputs(popover);
        clearDynamicApiKeyInputs(popover);
        renderPopoverModelRequirements(popover, settings.leaModelRequirements || null);
        markSettingsDirty();
        scheduleTexMirrorSync();
        status.textContent = "Settings saved.";
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
        markSettingsDirty();
      }
    });

    applySettingsPopoverWidth(settingsPopoverWidthPx, popover);
    document.body.appendChild(popover);
    activePopover = popover;
    positionSettingsPopover(popover);
    loadPopoverSettings(popover).catch((error) => {
      status.textContent = error instanceof Error ? error.message : String(error);
    });
    loadUsage(popover).catch((error) => {
      status.textContent = error instanceof Error ? error.message : String(error);
    });
    scheduleUsageRefresh(popover);

    function markSettingsDirty() {
      const dirty = modelSelect.value !== popover.dataset.savedModel ||
        String(Number.parseInt(maxTurnsInput.value, 10) || DEFAULT_LEA_MAX_TURNS) !== popover.dataset.savedMaxTurns ||
        normalizeMaxSpendInput(maxSpendInput.value) !== (popover.dataset.savedMaxSpend || "") ||
        String(texMirrorInput.checked) !== (popover.dataset.savedTexMirror || "true") ||
        hasProviderKeyInput(popover);
      saveButton.disabled = !dirty;
    }
  }

  function closePopover() {
    clearTimeout(usageRefreshTimer);
    usageRefreshTimer = null;
    stopSettingsPopoverResize({ persist: false });
    if (activePopover) {
      activePopover.querySelector("[data-role='model']")?.leaModelPicker?.destroy();
      activePopover.remove();
      activePopover = null;
    }
  }

  function positionPopover(popover, clientX, clientY) {
    const rect = popover.getBoundingClientRect();
    const gap = 12;
    const left = Math.min(clientX + gap, window.innerWidth - rect.width - 12);
    const top = Math.min(clientY + gap, window.innerHeight - rect.height - 12);
    popover.style.left = `${Math.max(12, left)}px`;
    popover.style.top = `${Math.max(12, top)}px`;
  }

  function positionSettingsPopover(popover) {
    const buttonRect = settingsButton?.getBoundingClientRect();
    const right = 20;
    const bottom = buttonRect ? window.innerHeight - buttonRect.top + 12 : 76;
    const anchoredBottom = Math.max(12, bottom);
    const maxHeight = Math.max(0, window.innerHeight - anchoredBottom - 12);
    popover.style.right = `${right}px`;
    popover.style.bottom = `${anchoredBottom}px`;
    popover.style.left = "auto";
    popover.style.top = "auto";
    popover.style.maxHeight = `${maxHeight}px`;
  }

  function updatePopoverStatus(popover, target) {
    const key = targetKey(target);
    if (!popover || popover.dataset.targetKey !== key) return;
    const statusInfo = latestStatuses[key] || { status: "unknown" };
    const currentStatus = getDisplayStatus(statusInfo);
    const actionStatus = getActionStatus(statusInfo);
    const chip = popover.querySelector(".ol-lean-status-chip");
    const detail = popover.querySelector(".ol-lean-popover-detail");
    const actions = popover.querySelector("[data-role='theorem-actions']");
    const leanStatement = popover.querySelector(".ol-lean-popover-lean");
    const stubbedWarning = popover.querySelector(".ol-lean-popover-warning");

    if (chip) {
      chip.className = `ol-lean-status-chip ol-lean-status-chip-${currentStatus}`;
      chip.textContent = formatStatus(currentStatus, statusInfo);
      if (hasStubbedTheoremUses(statusInfo)) {
        chip.appendChild(createStubbedTheoremUsesMark());
      }
    }
    if (actions) {
      renderTargetActions(actions, target, currentStatus, popover.querySelector(".ol-lean-popover-status"), leanStatement, actionStatus, statusInfo);
    }

    if (detail) {
      if (isExtensionContextInvalidated()) {
        detail.textContent = "Extension was reloaded. Refresh this Overleaf tab.";
      } else if (statusInfo.message) {
        detail.textContent = statusInfo.message;
      } else if (statusInfo.relativePath) {
        detail.textContent = statusInfo.relativePath;
      } else if (currentStatus === "in_progress") {
        detail.textContent = inProgressMessage(statusInfo, target);
      } else {
        detail.textContent = `Ready to send this ${targetNoun(target)} to Lea.`;
      }
    }
    renderLeanStatement(leanStatement, statusInfo.leanStatement || "");
    renderTargetWarning(stubbedWarning, target, statusInfo);
  }

  async function buildFormalizationSourceContext(target, { verifyMirror = true } = {}) {
    const sourceFile = normalizeDocPath(target?.sourceFile || latestActiveTexPath);
    const candidates = [
      ...(Array.isArray(lastMirrorFiles) ? lastMirrorFiles : []),
      ...(Array.isArray(lastLeanPaneFiles) ? lastLeanPaneFiles : [])
    ];
    // The editor buffer is authoritative for the active file, including when
    // mirroring has just been disabled and the cached mirror may be older.
    let source = sourceFile && normalizeDocPath(latestActiveTexPath) === sourceFile
      ? { path: sourceFile, content: latestActiveTex }
      : candidates.find((file) => normalizeDocPath(file?.path) === sourceFile);
    const content = typeof source?.content === "string" ? source.content : "";
    const sourceStartLine = Math.max(1, Number(target?.sourceStartLine) || 1);
    const sourceEndLine = Math.max(sourceStartLine, Number(target?.sourceEndLine) || sourceStartLine);
    const lines = content.split(/\r?\n/);
    const excerptStartLine = Math.max(1, sourceStartLine - TARGET_CONTEXT_RADIUS_LINES);
    const excerptEndLine = Math.min(lines.length, sourceEndLine + TARGET_CONTEXT_RADIUS_LINES);
    let sourceExcerpt = content
      ? lines.slice(excerptStartLine - 1, excerptEndLine).join("\n")
      : "";
    if (sourceExcerpt.length > TARGET_CONTEXT_MAX_CHARS) {
      sourceExcerpt = `${sourceExcerpt.slice(0, TARGET_CONTEXT_MAX_CHARS)}\n[excerpt truncated]`;
    }
    const uniqueFiles = new Map();
    for (const file of candidates) {
      const normalized = normalizeDocPath(file?.path);
      if (normalized && !uniqueFiles.has(normalized)) uniqueFiles.set(normalized, String(file?.content ?? ""));
    }
    return {
      sourceFile,
      sourceStartLine,
      sourceEndLine,
      mirroredSourcePath: sourceFile ? `.lea/files/overleaf/${sourceFile}` : "",
      sourceFileHash: content && verifyMirror ? await sha256(content) : "",
      mirrorAvailable: verifyMirror,
      sourceExcerpt,
      sourceExcerptStartLine: sourceExcerpt ? excerptStartLine : null,
      sourceExcerptEndLine: sourceExcerpt ? excerptEndLine : null,
      sourceCorpusFileCount: uniqueFiles.size,
      sourceCorpusChars: [...uniqueFiles.values()].reduce((total, text) => total + text.length, 0)
    };
  }

  async function formalize(target) {
    // A run must never begin against a mirror that failed to accept the live buffer.
    const mirrorResult = await syncTexMirrorNow({ force: true });
    const sourceContext = await buildFormalizationSourceContext(target, {
      verifyMirror: mirrorResult?.disabled !== true
    });
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    const response = await fetch(`${baseUrl}/formalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        overleafProjectId: extractOverleafProjectId(),
        targetKind: target.targetKind,
        targetLabel: target.targetLabel,
        targetText: target.targetText,
        targetUses: target.targetUses || [],
        targetContext: target.targetContext || "",
        syntax: target.syntax || "comment",
        projectName: lastProjectIdentity?.projectName || guessProjectName(lastLeanPaneFiles || []),
        projectNamespace: lastProjectIdentity?.namespace || "",
        sourceHash: await sha256(normalizeTargetText(target.targetText)),
        ...sourceContext
      })
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw companionRequestError(response, payload);
    }
    return payload;
  }

  async function stubTheorem(target) {
    // Stubbing also needs the current .tex mirror because statement translation may
    // depend on local notation/definitions in the surrounding document.
    const mirrorResult = await syncTexMirrorNow({ force: true });
    const sourceContext = await buildFormalizationSourceContext(target, {
      verifyMirror: mirrorResult?.disabled !== true
    });
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    const response = await fetch(`${baseUrl}/stub`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        overleafProjectId: extractOverleafProjectId(),
        targetKind: target.targetKind,
        targetLabel: target.targetLabel,
        targetText: target.targetText,
        targetUses: target.targetUses || [],
        targetContext: target.targetContext || "",
        syntax: target.syntax || "comment",
        projectName: lastProjectIdentity?.projectName || guessProjectName(lastLeanPaneFiles || []),
        projectNamespace: lastProjectIdentity?.namespace || "",
        sourceHash: await sha256(normalizeTargetText(target.targetText)),
        ...sourceContext
      })
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw companionRequestError(response, payload);
    }
    return payload;
  }

  // Build the full per-item payload /stub and /formalize expect (the same shape
  // the single-item formalize() sends), for every item a batch will run over.
  async function buildBatchTargetPayloads(items, { verifyMirror = true } = {}) {
    const overleafProjectId = extractOverleafProjectId();
    const projectName = lastProjectIdentity?.projectName || guessProjectName(lastLeanPaneFiles || []);
    const projectNamespace = lastProjectIdentity?.namespace || "";
    return Promise.all(items.map(async (item) => {
      const target = leanPaneView.paneItemToFormalizeTarget(item);
      const sourceContext = await buildFormalizationSourceContext(target, { verifyMirror });
      return {
        overleafProjectId,
        targetKind: target.targetKind,
        targetLabel: target.targetLabel,
        targetText: target.targetText,
        targetUses: target.targetUses || [],
        targetContext: target.targetContext || "",
        syntax: target.syntax || "comment",
        projectName,
        projectNamespace,
        sourceHash: await sha256(normalizeTargetText(target.targetText)),
        ...sourceContext
      };
    }));
  }

  // "Stub all" / "Formalize all": the batch versions of the per-item buttons.
  // They gather the eligible items from the live manifest, flush the .tex
  // mirror (statement translation reads local notation), POST the full target
  // set to the companion, then drive the SAME batch panel + polling the repair
  // batch uses -- one shared progress surface, distinguished by `operation`.
  async function runTargetBatch({ endpoint, items, errorKey }) {
    leanPaneRepairError = null;
    if (items.length === 0) return;
    try {
      const mirrorResult = await syncTexMirrorNow({ force: true });
      const baseUrl = await chatCompanionBaseUrl();
      const payloads = await buildBatchTargetPayloads(items, {
        verifyMirror: mirrorResult?.disabled !== true
      });
      const response = await fetch(`${baseUrl}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overleafProjectId: extractOverleafProjectId(), items: payloads })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.message || `Companion returned HTTP ${response.status}.`);
      leanPaneRepairBatch = payload;
      leanPaneExpandedBatchQueueId = "";
      leanPaneExpandedBatchCompletedId = "";
      startRepairBatchPolling({ immediate: true });
    } catch (error) {
      leanPaneRepairError = { itemKey: errorKey, message: normalizeErrorMessage(error) };
    }
    renderLeanPaneManifest(lastLeanPaneManifest);
    scheduleLeanPaneRefresh();
  }

  function stubAllTheorems() {
    const items = leanPaneView.stubbableItems(lastLeanPaneManifest?.items || []);
    return runTargetBatch({ endpoint: "/stub/all", items, errorKey: "batch" });
  }

  function formalizeAllItems() {
    const items = leanPaneView.formalizableItems(lastLeanPaneManifest?.items || []);
    return runTargetBatch({ endpoint: "/formalize/all", items, errorKey: "batch" });
  }

  async function refreshSingleStatus(target) {
    await refreshStatusesNow();
    return latestStatuses[targetKey(target)] || {
      status: "unavailable",
      relativePath: ""
    };
  }

  function scheduleStatusRefresh(delayMs = STATUS_REFRESH_DEBOUNCE_MS) {
    clearTimeout(statusRefreshTimer);
    statusRefreshTimer = setTimeout(() => {
      refreshStatusesNow().catch((error) => {
        postStatusError(error);
      });
    }, delayMs);
  }

  async function refreshStatusesNow() {
    if (latestTargets.length === 0) {
      postStatuses({});
      return;
    }

    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    const response = await fetch(`${baseUrl}/statuses`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        overleafProjectId: extractOverleafProjectId(),
        targets: latestTargets.map((target) => ({
          targetKind: target.targetKind,
          targetLabel: target.targetLabel,
          targetText: target.targetText,
          targetUses: target.targetUses || [],
          targetContext: target.targetContext || ""
        }))
      })
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    }
    const statuses = withFallbackStatuses(payload.statuses || {});
    await reconcileHumanApprovals(latestTargets.map((target) => ({
      target,
      statusInfo: statuses[targetKey(target)]
    })));
    postStatuses(statuses);
    if (activePopover?.dataset.targetKey) {
      const target = latestTargets.find((item) => targetKey(item) === activePopover.dataset.targetKey);
      if (target) updatePopoverStatus(activePopover, target);
    }
    if (Object.values(latestStatuses).some((status) => (
      status.status === "in_progress" && !status.githubImportPending
    ))) {
      scheduleStatusRefresh(pushConnected ? STATUS_REFRESH_RECONCILE_MS : STATUS_REFRESH_IN_PROGRESS_MS);
    }
  }

  function scheduleTexMirrorSync() {
    clearTimeout(texMirrorSyncTimer);
    texMirrorSyncTimer = setTimeout(() => {
      syncTexMirrorNow({ force: false }).catch(() => {});
    }, TEX_MIRROR_SYNC_DELAY_MS);
  }

  // Mirror the project's .tex sources into the matching Lea project (via the
  // companion's /mirror-tex → adapter). Driven in the background as the document
  // changes; `force` flushes a pending sync before a formalize. Skips all work when
  // nothing has changed since the last successful mirror (the formalize fast path).
  //
  // Lazy project creation: a Lea project must only come into being once the user
  // actually formalizes — never from merely opening or editing an Overleaf tab. So a
  // `force` sync (the formalize flush) ACTIVATES mirroring for this project; background
  // syncs stay completely inert (no zip fetch, no /mirror-tex, no project) until then.
  async function syncTexMirrorNow({ force }) {
    clearTimeout(texMirrorSyncTimer);
    texMirrorSyncTimer = null;

    if (texMirrorSyncPromise) {
      // Coalesce with an in-flight sync; its result may already be current.
      if (force) {
        await texMirrorSyncPromise;
      } else {
        await texMirrorSyncPromise.catch(() => {});
      }
    }

    const projectId = latestActiveTexProjectId || extractOverleafProjectId();
    if (!projectId || projectId === "unknown") return null;

    if (force) {
      texMirrorActivatedProjectId = projectId;  // formalizing activates this project
    } else if (texMirrorActivatedProjectId !== projectId) {
      return null;  // background activity never creates/mirrors before the first formalize
    }

    // Fast path: nothing changed since the last mirror for this project.
    if (!force && !texMirrorDirty && texMirrorSyncedOnce && lastMirrorProjectId === projectId) {
      return null;
    }

    const settings = await loadCompanionSettings();
    if (settings.leaTexMirrorEnabled === false) return { disabled: true };
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");

    texMirrorSyncPromise = (async () => {
      // Two sync tiers (PLAN-system-hardening 3.2): the whole-project zip
      // download used to run on every edit-pause once a project was activated
      // — heavy for large projects and unkind to Overleaf's servers. Now an
      // ordinary edit ships just the active editor buffer (mode "upsert" —
      // the adapter writes it without treating absent files as deleted); the
      // zip + full reconcile runs only on activation, when the active file
      // isn't in the cached set (new/renamed doc), on a periodic refresh to
      // pick up collaborator edits, and stays the base of the forced
      // pre-formalize sync.
      const activeRel = String(latestActiveTexPath || "").replace(/^\/+/, "");
      const cacheUsable = Boolean(lastMirrorFiles) && lastMirrorProjectId === projectId;
      const activeKnown = !activeRel || (cacheUsable && lastMirrorFiles.some((file) => file.path === activeRel));
      const fullSyncDue =
        !cacheUsable ||
        !activeKnown ||
        Date.now() - lastTexMirrorFullSyncAt > TEX_MIRROR_FULL_SYNC_INTERVAL_MS;

      if (!force && !fullSyncDue && activeRel && typeof latestActiveTex === "string") {
        const response = await fetch(`${baseUrl}/mirror-tex`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            overleafProjectId: projectId,
            mode: "upsert",
            files: [{ path: activeRel, content: latestActiveTex }]
          })
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
        }
        lastMirrorFiles = lastMirrorFiles.map((file) =>
          file.path === activeRel ? { ...file, content: latestActiveTex } : file
        );
        texMirrorDirty = false;
        return payload;
      }

      // Full tier. Re-download + unzip only when the cached set can't serve
      // (new project / unknown active file / periodic refresh); a forced
      // pre-formalize sync with a healthy cache POSTs the cached set — the
      // adapter is authoritative and no-ops cheaply on identical content, so
      // divergence self-heals without a zip per formalize.
      const needFetch = !cacheUsable || !activeKnown ||
        Date.now() - lastTexMirrorFullSyncAt > TEX_MIRROR_FULL_SYNC_INTERVAL_MS;
      const files = needFetch
        ? await collectProjectTexFiles(projectId)
        : lastMirrorFiles.map((file) => ({ ...file }));
      // A forced formalize flush can arrive before the edit debounce. Even with a
      // healthy full-project cache, the live editor buffer is authoritative.
      if (activeRel && typeof latestActiveTex === "string") {
        const active = files.find((file) => file.path === activeRel);
        if (active) active.content = latestActiveTex;
      }
      const response = await fetch(`${baseUrl}/mirror-tex`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overleafProjectId: projectId, files })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
      }
      lastMirrorFiles = files;
      lastMirrorProjectId = projectId;
      texMirrorSyncedOnce = true;
      texMirrorDirty = false;
      if (needFetch) lastTexMirrorFullSyncAt = Date.now();
      return payload;
    })().finally(() => {
      texMirrorSyncPromise = null;
    });

    return texMirrorSyncPromise;
  }

  // Download the project's source archive (authenticated, same-origin) and return
  // its .tex entries as [{ path, content }]. Overlays the live active-editor buffer
  // when its path is known, so the file being edited is current even if Overleaf's
  // saved copy lags. Unzipping uses the dependency-free reader in zipTex.mjs.
  async function collectProjectTexFiles(projectId, { timeoutMs = LEAN_PANE_ARCHIVE_TIMEOUT_MS } = {}) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeoutId = controller
      ? setTimeout(() => controller.abort(), timeoutMs)
      : null;
    let buffer;
    try {
      const response = await fetch(`/project/${encodeURIComponent(projectId)}/download/zip`, {
        credentials: "same-origin",
        ...(controller ? { signal: controller.signal } : {})
      });
      if (!response.ok) {
        throw new Error(`Overleaf returned HTTP ${response.status} for the project download.`);
      }
      buffer = await response.arrayBuffer();
    } catch (error) {
      if (controller?.signal.aborted) {
        throw new Error("Overleaf timed out while preparing the project download.");
      }
      throw error;
    } finally {
      if (timeoutId != null) clearTimeout(timeoutId);
    }
    const { extractLatexSourcesFromZip } = await import(chrome.runtime.getURL("zipTex.mjs"));
    const files = await extractLatexSourcesFromZip(buffer);

    if (latestActiveTexPath && typeof latestActiveTex === "string") {
      // Override only an entry that already exists in the archive — never invent a
      // path, so a misread active-file path can't inject a spurious mirror file.
      const wanted = String(latestActiveTexPath).replace(/^\/+/, "");
      const existing = files.find((file) => file.path === wanted);
      if (existing) existing.content = latestActiveTex;
    }
    return files;
  }

  function postStatusError(error) {
    const message = normalizeErrorMessage(error);
    const statuses = {};
    for (const target of latestTargets) {
      statuses[targetKey(target)] = {
        status: "offline",
        message
      };
    }
    postStatuses(statuses);
  }

  function paneItemApprovalTarget(item) {
    return {
      targetKind: item?.leanKind === "def" ? "definition" : "theorem",
      targetLabel: item?.label || item?.leanDeclarationName || ""
    };
  }

  function humanApprovalKey(target) {
    return `${extractOverleafProjectId()}:${targetKey(target)}`;
  }

  function humanApprovalRecord(target) {
    return humanApprovals[humanApprovalKey(target)] || null;
  }

  function isHumanApproved(target, statusInfo) {
    const record = humanApprovalRecord(target);
    return Boolean(
      record
      && statusInfo?.approvalEligible
      && statusInfo?.approvalRevision
      && record.revision === statusInfo.approvalRevision
    );
  }

  async function loadHumanApprovals() {
    if (humanApprovalsLoadPromise) return humanApprovalsLoadPromise;
    humanApprovalsLoadPromise = (async () => {
      if (isExtensionContextInvalidated() || !chrome.storage?.local) {
        humanApprovals = {};
        return humanApprovals;
      }
      const stored = await chrome.storage.local.get({ [HUMAN_APPROVAL_STORAGE_KEY]: {} });
      const value = stored?.[HUMAN_APPROVAL_STORAGE_KEY];
      humanApprovals = value && typeof value === "object" && !Array.isArray(value) ? { ...value } : {};
      return humanApprovals;
    })().catch(() => {
      humanApprovals = {};
      return humanApprovals;
    });
    return humanApprovalsLoadPromise;
  }

  async function persistHumanApprovals() {
    if (isExtensionContextInvalidated() || !chrome.storage?.local) return;
    await chrome.storage.local.set({ [HUMAN_APPROVAL_STORAGE_KEY]: humanApprovals });
  }

  async function reconcileHumanApprovals(entries) {
    await loadHumanApprovals();
    let changed = false;
    for (const { target, statusInfo } of entries || []) {
      if (!target?.targetLabel) continue;
      const key = humanApprovalKey(target);
      const record = humanApprovals[key];
      if (!record) continue;
      if (
        !statusInfo?.approvalEligible
        || !statusInfo?.approvalRevision
        || record.revision !== statusInfo.approvalRevision
      ) {
        delete humanApprovals[key];
        changed = true;
      }
    }
    if (changed) await persistHumanApprovals();
    return changed;
  }

  async function toggleHumanApproval(target, statusInfo) {
    await loadHumanApprovals();
    const key = humanApprovalKey(target);
    if (humanApprovalBusyKeys.has(key)) return;
    humanApprovalBusyKeys.add(key);
    renderApprovalSurfaces();
    try {
      if (isHumanApproved(target, statusInfo)) {
        delete humanApprovals[key];
      } else {
        if (!statusInfo?.approvalEligible || !statusInfo?.approvalRevision) return;
        humanApprovals[key] = {
          revision: statusInfo.approvalRevision,
          approvedAt: new Date().toISOString()
        };
      }
      await persistHumanApprovals();
    } finally {
      humanApprovalBusyKeys.delete(key);
      renderApprovalSurfaces();
    }
  }

  function renderApprovalSurfaces() {
    renderStatusBadges();
    if (lastLeanPaneManifest) renderLeanPaneManifest(lastLeanPaneManifest);
    if (activePopover?.dataset.targetKey) {
      const target = latestTargets.find((item) => targetKey(item) === activePopover.dataset.targetKey);
      if (target) updatePopoverStatus(activePopover, target);
    }
  }

  function handleHumanApprovalStorageChanged(changes, areaName) {
    if (areaName !== "local" || !changes?.[HUMAN_APPROVAL_STORAGE_KEY]) return;
    const next = changes[HUMAN_APPROVAL_STORAGE_KEY].newValue;
    humanApprovals = next && typeof next === "object" && !Array.isArray(next) ? { ...next } : {};
    humanApprovalsLoadPromise = Promise.resolve(humanApprovals);
    renderApprovalSurfaces();
  }

  function createHumanApprovalButton(target, statusInfo, { pane = false } = {}) {
    const approved = isHumanApproved(target, statusInfo);
    const key = humanApprovalKey(target);
    const busy = humanApprovalBusyKeys.has(key);
    const button = document.createElement("button");
    button.type = "button";
    button.className = [
      "ol-lean-human-approval",
      pane ? "ol-lean-human-approval-pane" : "ol-lean-human-approval-source",
      approved ? "ol-lean-human-approval-approved" : ""
    ].filter(Boolean).join(" ");
    button.textContent = "✓";
    button.disabled = busy || !approved && !statusInfo?.approvalEligible;
    button.setAttribute("aria-pressed", String(approved));
    button.setAttribute(
      "aria-label",
      approved
        ? `Remove personal approval for ${target.targetLabel}`
        : `Mark ${target.targetLabel} as personally audited and approved`
    );
    button.title = busy
      ? "Saving personal approval…"
      : approved
        ? "Personally audited and approved. Click to remove."
        : statusInfo?.approvalEligible
          ? "Mark this exact proof and its current dependencies as personally audited."
          : statusInfo?.approvalIneligibleReason || "Personal approval is unavailable for this item.";
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      toggleHumanApproval(target, statusInfo).catch(() => {});
    });
    return button;
  }

  function postStatuses(statuses) {
    latestBaseStatuses = statuses || {};
    latestStatuses = githubImportStatusOverlay(latestBaseStatuses);
    renderStatusBadges();
  }

  function withFallbackStatuses(statuses) {
    const completeStatuses = { ...statuses };
    for (const target of latestTargets) {
      const key = targetKey(target);
      if (!completeStatuses[key]) {
        completeStatuses[key] = {
          status: "unavailable",
          message: `The companion did not return a status for this ${targetNoun(target)}.`
        };
      }
    }
    return completeStatuses;
  }

  function renderStatusBadges() {
    if (!badgeLayer) {
      badgeLayer = document.createElement("div");
      badgeLayer.className = "ol-lean-status-layer";
      (document.body || document.documentElement).appendChild(badgeLayer);
    }

    badgeLayer.replaceChildren();
    for (const diagnostic of latestDiagnostics) {
      const coords = diagnostic.coords;
      if (!coords) continue;
      const badge = document.createElement("button");
      badge.className = "ol-lean-status ol-lean-status-failed";
      badge.type = "button";
      badge.appendChild(document.createTextNode("fix marker"));
      badge.title = diagnostic.message || "This Lea marker is malformed.";
      badge.setAttribute("aria-label", badge.title);
      badge.style.left = `${Math.min(coords.left + 8, window.innerWidth - 140)}px`;
      badge.style.top = `${coords.top}px`;
      badge.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        showDiagnosticPopover(event.clientX, event.clientY, diagnostic);
      });
      badgeLayer.appendChild(badge);
    }
    for (const target of latestTargets) {
      const coords = target.coords;
      if (!coords) continue;
      const statusInfo = latestStatuses[targetKey(target)] || { status: "unknown" };
      const status = getDisplayStatus(statusInfo);
      const badge = document.createElement("button");
      badge.className = `ol-lean-status ol-lean-status-${status}`;
      badge.type = "button";
      badge.appendChild(document.createTextNode(formatStatus(status, statusInfo)));
      if (hasStubbedTheoremUses(statusInfo)) {
        badge.appendChild(createStubbedTheoremUsesMark());
      }
      const turnProgress = getTurnProgressDisplay(statusInfo);
      if (turnProgress.text) {
        const progress = document.createElement("span");
        progress.className = `ol-lean-status-progress${turnProgress.pending ? " ol-lean-status-progress-pending" : ""}`;
        progress.textContent = turnProgress.text;
        if (turnProgress.pending) {
          progress.setAttribute("aria-hidden", "true");
        }
        badge.appendChild(progress);
      }
      const stubbedUsesLabel = hasStubbedTheoremUses(statusInfo) ? " warning: proof uses sorry-stubbed support" : "";
      const statusLabel = `${formatStatus(status, statusInfo)}${turnProgress.label ? ` ${turnProgress.label}` : ""}${stubbedUsesLabel}`;
      badge.title = statusInfo.sourceFreshness === "stale"
        ? statusInfo.sourceFreshnessMessage
          || "The LaTeX source changed after this Lean artifact was generated. Re-formalize to synchronize it."
        : statusInfo.message || `Lean status for ${target.targetLabel}: ${statusLabel}`;
      badge.setAttribute("aria-label", `Open Lea popover for ${target.targetLabel}. Status: ${statusLabel}.`);
      badge.style.left = `${Math.min(coords.left + 8, window.innerWidth - 140)}px`;
      badge.style.top = `${coords.top}px`;
      badge.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        showTargetPopover(event.clientX, event.clientY, target);
      });
      badgeLayer.appendChild(badge);
      if (
        Object.prototype.hasOwnProperty.call(statusInfo, "approvalEligible")
        || Boolean(statusInfo.approvalRevision)
      ) {
        const approval = createHumanApprovalButton(target, statusInfo);
        const badgeRect = badge.getBoundingClientRect();
        approval.style.left = `${Math.min(badgeRect.right + 4, window.innerWidth - 24)}px`;
        approval.style.top = `${coords.top}px`;
        badgeLayer.appendChild(approval);
      }
    }
  }

  function formatStatus(status, statusInfo = null) {
    if ((status === "formalized" || status === "defined") && statusInfo?.resultKind === "defined") {
      return "defined";
    }
    switch (status) {
      case "unformalized":
        return "unformalized";
      case "in_progress":
        return "in progress";
      case "formalized":
        return "formalized";
      case "stale":
        return "out of date";
      case "defined":
        return "defined";
      case "disproved":
        return "Counterexample found";
      case "sorry_stub":
        return "sorry stub";
      case "failed":
        return "failed";
      case "offline":
        return "offline";
      case "unavailable":
        return "unavailable";
      default:
        return "checking";
    }
  }

  function inProgressMessage(statusInfo, target = null) {
    if (statusInfo?.githubImportPending) {
      return statusInfo.message || `An imported Lean ${targetNoun(target)} is queued for checking.`;
    }
    const turnProgressText = formatTurnProgress(statusInfo);
    const noun = targetNoun(target);
    return turnProgressText
      ? `Lea ${noun} formalization is in progress: ${turnProgressText}.`
      : `Lea ${noun} formalization is in progress. Waiting for the first turn update.`;
  }

  function getTurnProgressDisplay(statusInfo) {
    if (statusInfo?.status !== "in_progress") return { text: "", label: "", pending: false };
    const turnProgressText = formatTurnProgress(statusInfo);
    if (turnProgressText) return { text: turnProgressText, label: turnProgressText, pending: false };
    return { text: "...", label: "progress pending", pending: true };
  }

  function formatTurnProgress(statusInfo) {
    if (statusInfo?.status !== "in_progress") return "";
    const current = Number.parseInt(String(statusInfo.turnProgress?.current || ""), 10);
    const max = Number.parseInt(String(statusInfo.turnProgress?.max || ""), 10);
    if (!Number.isFinite(current) || current < 1 || !Number.isFinite(max) || max < 1) return "";
    return `${current}/${max}`;
  }

  function renderLeanStatement(element, statement) {
    if (!element) return;
    if (!statement) {
      element.hidden = true;
      element.textContent = "";
      return;
    }
    element.hidden = false;
    element.textContent = statement;
  }

  function renderStubbedTheoremUsesWarning(element, statusInfo) {
    if (!element) return;
    const uses = getStubbedTheoremUses(statusInfo);
    if (uses.length === 0) {
      element.hidden = true;
      element.textContent = "";
      return;
    }
    const names = uses.map((use) => use.declarationName || use.targetLabel).filter(Boolean).join(", ");
    const plural = uses.length !== 1;
    element.hidden = false;
    element.textContent = plural
      ? `Proof uses supporting theorems ${names}, which have been sorry stubbed but not fully formalized.`
      : `Proof uses supporting theorem ${names}, which has been sorry stubbed but not fully formalized.`;
  }

  function renderTargetWarning(element, target, statusInfo) {
    if (!element) return;
    const warnings = [];
    if (statusInfo?.sourceFreshness === "stale") {
      warnings.push(
        statusInfo.sourceFreshnessMessage
        || "The LaTeX source changed after this Lean artifact was generated. Re-formalize to synchronize it."
      );
    }
    const uses = getStubbedTheoremUses(statusInfo);
    if (uses.length > 0) {
      const names = uses.map((use) => use.declarationName || use.targetLabel).filter(Boolean).join(", ");
      const plural = uses.length !== 1;
      warnings.push(plural
        ? `Proof uses supporting theorems ${names}, which have been sorry stubbed but not fully formalized.`
        : `Proof uses supporting theorem ${names}, which has been sorry stubbed but not fully formalized.`);
    }
    element.hidden = warnings.length === 0;
    element.textContent = warnings.join(" ");
  }

  function getStubbedTheoremUses(statusInfo) {
    return Array.isArray(statusInfo?.stubbedTheoremUses) ? statusInfo.stubbedTheoremUses : [];
  }

  function hasStubbedTheoremUses(statusInfo) {
    return statusInfo?.status === "formalized" && getStubbedTheoremUses(statusInfo).length > 0;
  }

  function createStubbedTheoremUsesMark() {
    const mark = document.createElement("span");
    mark.className = "ol-lean-stubbed-use-mark";
    mark.textContent = "!";
    mark.title = "Proof uses sorry-stubbed support";
    mark.setAttribute("aria-hidden", "true");
    return mark;
  }

  function buttonTextForStatus(status, target = null) {
    const definition = isDefinitionTarget(target);
    switch (status) {
      case "in_progress":
        return "Formalizing...";
      case "formalized":
      case "defined":
      case "disproved":
      case "stale":
        return definition ? "Regenerate definition" : "Re-formalize";
      case "unknown":
        return "Check status";
      case "sorry_stub":
      case "unformalized":
      default:
        return definition ? "Regenerate definition" : "Run Lea";
    }
  }

  function getActionStatus(statusInfo) {
    if (statusInfo?.sourceFreshness === "stale") {
      return "stale";
    }
    if (statusInfo?.status === "failed") {
      return statusInfo.effectiveStatus || "unformalized";
    }
    return statusInfo?.status || "unknown";
  }

  function getDisplayStatus(statusInfo) {
    return statusInfo?.sourceFreshness === "stale"
      ? "stale"
      : statusInfo?.status || "unknown";
  }

  function canViewInLeaUi(status) {
    return LEA_UI_VIEW_STATUSES.has(status);
  }

  function getLeaSessionLink(statusInfo) {
    const sessionUrl = String(statusInfo?.leaSessionUrl || "").trim();
    const sessionId = String(statusInfo?.leaSessionId || "").trim();
    if (!sessionUrl && !sessionId) return null;
    const baseUrl = String(statusInfo?.leaUiBaseUrl || DEFAULT_LEA_UI_BASE_URL).replace(/\/+$/, "");
    return {
      sessionId,
      baseUrl,
      url: sessionUrl || buildLeaSessionUrl(baseUrl, sessionId)
    };
  }

  function getLeaUiBaseLink(statusInfo) {
    const baseUrl = String(statusInfo?.leaUiBaseUrl || DEFAULT_LEA_UI_BASE_URL).replace(/\/+$/, "");
    return { sessionId: "", baseUrl, url: baseUrl };
  }

  function buildLeaSessionUrl(baseUrl, sessionId) {
    // A malformed stored leaUiBaseUrl (a user typo in options) must not throw
    // out of popover render (AUDIT L4) — fall back to the default origin.
    let url;
    try {
      url = new URL(baseUrl || DEFAULT_LEA_UI_BASE_URL);
    } catch {
      url = new URL(DEFAULT_LEA_UI_BASE_URL);
    }
    url.searchParams.set("session", sessionId);
    return url.toString();
  }

  function openLeaSession(sessionLink) {
    if (!sessionLink?.url) {
      return Promise.reject(new Error("Lea session link is not available yet."));
    }
    return new Promise((resolve, reject) => {
      const fallback = () => {
        const opened = window.open(sessionLink.url, "_blank", "noopener");
        if (opened) resolve();
        else reject(new Error("Browser blocked the Lea session tab."));
      };
      if (!globalThis.chrome?.runtime?.sendMessage) {
        fallback();
        return;
      }
      chrome.runtime.sendMessage({
        type: "OPEN_LEA_SESSION",
        url: sessionLink.url,
        baseUrl: sessionLink.baseUrl
      }, (response) => {
        if (chrome.runtime.lastError) {
          fallback();
          return;
        }
        if (response?.ok) {
          resolve();
          return;
        }
        reject(new Error(response?.message || "Could not open Lea session."));
      });
    });
  }

  function getSettings() {
    if (isExtensionContextInvalidated()) {
      return Promise.reject(new Error("Extension was reloaded. Refresh this Overleaf tab."));
    }
    return chrome.storage.sync.get({
      companionUrl: DEFAULT_COMPANION_URL,
      leaRepoPath: "",
      leaApiBaseUrl: "http://127.0.0.1:8001",
      leaUiBaseUrl: DEFAULT_LEA_UI_BASE_URL,
      leaModel: DEFAULT_LEA_MODEL,
      leaMaxTurns: DEFAULT_LEA_MAX_TURNS,
      leaMaxSpendUsd: null,
      leaTexMirrorEnabled: DEFAULT_LEA_TEX_MIRROR_ENABLED
    });
  }

  async function loadCompanionSettings() {
    const stored = await getSettings();
    const baseUrl = String(stored.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    try {
      const response = await fetch(`${baseUrl}/settings`);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
      }
      let catalogPayload = {};
      try {
        const catalogResponse = await fetch(`${baseUrl}/settings/models`);
        catalogPayload = await catalogResponse.json().catch(() => ({}));
        if (!catalogResponse.ok) catalogPayload = {};
      } catch {
        catalogPayload = {};
      }
      const settings = {
        companionUrl: baseUrl,
        leaRepoPath: payload.leaRepoPath || stored.leaRepoPath || "",
        leaApiBaseUrl: payload.leaApiBaseUrl || stored.leaApiBaseUrl || "http://127.0.0.1:8001",
        leaUiBaseUrl: payload.leaUiBaseUrl || stored.leaUiBaseUrl || DEFAULT_LEA_UI_BASE_URL,
        leaModel: payload.leaModel || stored.leaModel || DEFAULT_LEA_MODEL,
        leaMaxTurns: payload.leaMaxTurns || stored.leaMaxTurns || DEFAULT_LEA_MAX_TURNS,
        leaMaxSpendUsd: payload.leaMaxSpendUsd ?? stored.leaMaxSpendUsd ?? null,
        leaCurrentSpendUsd: payload.leaCurrentSpendUsd ?? 0,
        leaTexMirrorEnabled: payload.leaTexMirrorEnabled ?? stored.leaTexMirrorEnabled ?? DEFAULT_LEA_TEX_MIRROR_ENABLED,
        leaModelOptions: payload.leaModelOptions || DEFAULT_MODEL_OPTIONS,
        leaModelCatalog: Array.isArray(catalogPayload.models) && catalogPayload.models.length > 0
          ? catalogPayload.models
          : payload.leaModelOptions || DEFAULT_MODEL_OPTIONS,
        leaModelCatalogDegraded: catalogPayload.degraded !== false,
        leaProviderKeys: payload.leaProviderKeys || {},
        leaApiKeys: payload.leaApiKeys || {},
        leaModelRequirements: payload.leaModelRequirements || null,
        githubTokenConfigured: Boolean(payload.githubTokenConfigured)
      };
      await chrome.storage.sync.set({
        companionUrl: settings.companionUrl,
        leaRepoPath: settings.leaRepoPath,
        leaApiBaseUrl: settings.leaApiBaseUrl,
        leaUiBaseUrl: settings.leaUiBaseUrl,
        leaModel: settings.leaModel,
        leaMaxTurns: settings.leaMaxTurns,
        leaMaxSpendUsd: settings.leaMaxSpendUsd,
        leaTexMirrorEnabled: settings.leaTexMirrorEnabled
      });
      return settings;
    } catch {
      return {
        ...stored,
        companionUrl: baseUrl,
        leaModelOptions: DEFAULT_MODEL_OPTIONS,
        leaModelCatalog: DEFAULT_MODEL_OPTIONS,
        leaModelCatalogDegraded: true,
        leaProviderKeys: {},
        leaApiKeys: {},
        leaModelRequirements: null
      };
    }
  }

  async function loadPopoverSettings(popover) {
    const settings = await loadCompanionSettings();
    const modelSelect = popover.querySelector("[data-role='model']");
    const maxTurnsInput = popover.querySelector("[data-role='max-turns']");
    const maxSpendInput = popover.querySelector("[data-role='max-spend']");
    const texMirrorInput = popover.querySelector("[data-role='tex-mirror']");
    popover.dataset.modelOptions = JSON.stringify(settings.leaModelOptions || DEFAULT_MODEL_OPTIONS);
    popover.leaModelCatalog = settings.leaModelCatalog || settings.leaModelOptions || DEFAULT_MODEL_OPTIONS;
    popover.leaApiKeys = settings.leaApiKeys || {};
    renderProviderKeys(popover, settings.leaProviderKeys || {});
    renderModelOptions(
      modelSelect,
      popover.leaModelCatalog,
      settings.leaModelOptions || DEFAULT_MODEL_OPTIONS,
      settings.leaModel || DEFAULT_LEA_MODEL
    );
    const catalogStatus = popover.querySelector("[data-role='model-catalog-status']");
    if (catalogStatus) {
      catalogStatus.textContent = settings.leaModelCatalogDegraded
        ? "Adapter catalog unavailable — using the offline fallback."
        : `${popover.leaModelCatalog.length.toLocaleString()} models available; search by ID or provider.`;
    }
    renderPopoverModelRequirements(popover, settings.leaModelRequirements);
    maxTurnsInput.value = String(settings.leaMaxTurns || DEFAULT_LEA_MAX_TURNS);
    maxSpendInput.value = settings.leaMaxSpendUsd == null ? "" : String(settings.leaMaxSpendUsd);
    texMirrorInput.checked = settings.leaTexMirrorEnabled !== false;
    renderGithubTokenStatus(popover, Boolean(settings.githubTokenConfigured));
    popover.dataset.savedModel = modelSelect.value;
    popover.dataset.savedMaxTurns = String(Number.parseInt(maxTurnsInput.value, 10) || DEFAULT_LEA_MAX_TURNS);
    popover.dataset.savedMaxSpend = settings.leaMaxSpendUsd == null ? "" : String(settings.leaMaxSpendUsd);
    popover.dataset.savedTexMirror = String(texMirrorInput.checked);
    popover.querySelector("[data-role='save-settings']").disabled = true;
    try {
      const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
      lastProjectIdentity = await loadProjectIdentity({ baseUrl, projectId: extractOverleafProjectId() });
      renderProjectSettingsSection(popover, lastProjectIdentity);
      renderLeanPaneProjectIdentity(lastProjectIdentity);
    } catch {
      renderProjectSettingsSection(popover, null);
    }
  }

  function renderProjectSettingsSection(popover, identity) {
    const projectName = popover.querySelector("[data-role='project-name']");
    const exists = popover.querySelector("[data-role='project-exists']");
    const namespace = popover.querySelector("[data-role='project-namespace']");
    const binding = popover.querySelector("[data-role='project-binding']");
    const fallback = guessProjectName(lastLeanPaneFiles || []);
    if (projectName) projectName.textContent = identity?.projectName || fallback;
    if (exists) exists.textContent = identity?.exists ? "Created" : "Not created";
    if (namespace) namespace.textContent = `Lean namespace: ${identity?.namespace || "--"}`;
    if (binding) binding.textContent = `Overleaf binding: ${identity?.slug || extractOverleafProjectId() || "--"}`;
  }

  function renderGithubTokenStatus(popover, configured) {
    const panel = popover.querySelector("[data-role='github-token-panel']");
    const chip = popover.querySelector("[data-role='github-token-status']");
    const description = popover.querySelector("[data-role='github-token-description']");
    const toggle = popover.querySelector("[data-role='github-token-toggle']");
    const clear = popover.querySelector("[data-role='github-token-clear']");
    if (!chip) return;
    const card = panel?.querySelector(".ol-lean-github-token-card");
    if (card) card.dataset.configured = configured ? "true" : "false";
    chip.textContent = configured ? "Saved" : "Not set";
    if (description) {
      description.textContent = configured
        ? "A token is saved. GitHub verifies it when you push."
        : "Add a token to push Lean projects to GitHub.";
    }
    if (toggle) toggle.textContent = configured ? "Replace token" : "Add GitHub token";
    if (clear) clear.hidden = !configured;
  }

  // POST /settings/github-token: { value } saves, { clear: true } removes. The
  // companion writes through to the adapter's settings and never persists the
  // token itself.
  async function updateGithubToken(payload) {
    const baseUrl = await chatCompanionBaseUrl();
    const response = await fetch(`${baseUrl}/settings/github-token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body?.message || `Could not update the GitHub token (HTTP ${response.status}).`);
    }
    return body;
  }

  function renderModelOptions(select, catalog, featured, selectedModel) {
    globalThis.LeaModelPicker.createModelPicker({
      root: select,
      value: selectedModel,
      catalog,
      featured
    });
  }

  function renderProviderKeys(popover, providerKeys) {
    popover.dataset.providerKeys = JSON.stringify(providerKeys || {});
    for (const family of Object.keys(MODEL_FAMILY_LABELS)) {
      const row = popover.querySelector(`.ol-lean-provider-row[data-family='${family}']`);
      if (!row) continue;
      const configured = Boolean(providerKeys?.[family]?.configured);
      const status = row.querySelector("[data-role='provider-status']");
      const button = row.querySelector("[data-role='provider-key-toggle']");
      row.dataset.configured = configured ? "true" : "false";
      status.textContent = configured ? "Configured" : "Missing";
      if (button) button.textContent = configured ? "Replace key" : "Add key";
    }
  }

  async function loadPopoverModelRequirements(popover, model) {
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    try {
      const response = await fetch(
        `${baseUrl}/settings/models/requirements?model=${encodeURIComponent(model)}`
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.message || `HTTP ${response.status}`);
      renderPopoverModelRequirements(popover, payload);
    } catch {
      renderPopoverModelRequirements(popover, null);
    }
  }

  function staticProviderInputForEnv(popover, env) {
    const family = env === "OPENAI_API_KEY"
      ? "openai"
      : env === "GOOGLE_API_KEY" || env === "GEMINI_API_KEY"
        ? "google"
        : env === "ANTHROPIC_API_KEY" || env === "ANTHROPIC_AUTH_TOKEN"
          ? "anthropic"
          : "";
    return family ? popover.querySelector(`[data-role='provider-key-input'][data-family='${family}']`) : null;
  }

  function popoverRequirementConfigured(popover, requirement) {
    if (requirement?.configured || popover.leaApiKeys?.[requirement?.env]?.configured) return true;
    const staticInput = staticProviderInputForEnv(popover, requirement?.env);
    if (staticInput?.value.trim()) return true;
    return [...popover.querySelectorAll("[data-role='model-requirements'] input[data-env]")]
      .some((input) => input.dataset.env === requirement?.env && Boolean(input.value.trim()));
  }

  function updatePopoverRequirementSummary(popover) {
    const container = popover.querySelector("[data-role='model-requirements']");
    const requirements = container?.leaRequirements;
    const note = container?.querySelector(".lea-model-requirement-note");
    if (!requirements || !note) return;
    const required = Array.isArray(requirements.required_keys) ? requirements.required_keys : [];
    const satisfied = required.length === 0 || required.some((key) => popoverRequirementConfigured(popover, key));
    note.dataset.satisfied = satisfied ? "true" : "false";
    if (required.length === 0) {
      note.textContent = requirements.degraded
        ? "Provider requirements are unavailable while the adapter is offline."
        : "This model does not require a single API-key credential.";
    } else if (satisfied) {
      note.textContent = `${requirements.provider || "Model"} credentials are configured.`;
    } else {
      note.textContent = `Add one of: ${required.map((key) => key.env).join(" or ")}.`;
    }
  }

  function renderPopoverModelRequirements(popover, requirements) {
    const container = popover.querySelector("[data-role='model-requirements']");
    if (!container) return;
    container.replaceChildren();
    container.leaRequirements = requirements;
    if (!requirements) {
      const note = document.createElement("p");
      note.className = "lea-model-requirement-note";
      note.textContent = "Model credential requirements are currently unavailable.";
      container.appendChild(note);
      return;
    }
    const note = document.createElement("p");
    note.className = "lea-model-requirement-note";
    container.appendChild(note);
    for (const requirement of requirements.required_keys || []) {
      if (staticProviderInputForEnv(popover, requirement.env)) continue;
      const label = document.createElement("label");
      label.className = "lea-model-requirement-field";
      label.textContent = requirement.label || requirement.env;
      const input = document.createElement("input");
      input.type = "password";
      input.autocomplete = "off";
      input.dataset.env = requirement.env;
      input.placeholder = requirement.configured || popover.leaApiKeys?.[requirement.env]?.configured
        ? "Configured — leave blank to keep"
        : requirement.env;
      input.addEventListener("input", () => {
        updatePopoverRequirementSummary(popover);
        popover.querySelector("[data-role='save-settings']").disabled = false;
      });
      label.appendChild(input);
      container.appendChild(label);
    }
    updatePopoverRequirementSummary(popover);
  }

  function collectProviderApiKeyPatch(popover) {
    const patch = {};
    for (const input of popover.querySelectorAll("[data-role='provider-key-input']")) {
      const value = input.value.trim();
      if (value) patch[input.dataset.family] = value;
    }
    return patch;
  }

  function collectDynamicApiKeyPatch(popover) {
    const patch = {};
    for (const input of popover.querySelectorAll("[data-role='model-requirements'] input[data-env]")) {
      const value = input.value.trim();
      if (value) patch[input.dataset.env] = value;
    }
    return patch;
  }

  function hasProviderKeyInput(popover) {
    return [...popover.querySelectorAll("[data-role='provider-key-input'], [data-role='model-requirements'] input[data-env]")]
      .some((input) => Boolean(input.value.trim()));
  }

  function clearProviderKeyInputs(popover) {
    for (const input of popover.querySelectorAll("[data-role='provider-key-input']")) {
      input.value = "";
      input.hidden = true;
    }
  }

  function clearDynamicApiKeyInputs(popover) {
    for (const input of popover.querySelectorAll("[data-role='model-requirements'] input[data-env]")) {
      input.value = "";
    }
  }

  async function savePopoverSettings(popover) {
    const current = await loadCompanionSettings();
    const baseUrl = String(current.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    const leaModel = popover.querySelector("[data-role='model']").value || DEFAULT_LEA_MODEL;
    const leaMaxTurns = Number.parseInt(popover.querySelector("[data-role='max-turns']").value, 10) || DEFAULT_LEA_MAX_TURNS;
    const leaMaxSpendUsd = parseMaxSpendInput(popover.querySelector("[data-role='max-spend']").value);
    const leaTexMirrorEnabled = popover.querySelector("[data-role='tex-mirror']").checked;
    const response = await fetch(`${baseUrl}/settings/lea`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        leaRepoPath: current.leaRepoPath,
        leaApiBaseUrl: current.leaApiBaseUrl,
        leaModel,
        leaMaxTurns,
        leaMaxSpendUsd,
        leaTexMirrorEnabled,
        leaProviderApiKeys: collectProviderApiKeyPatch(popover),
        leaApiKeys: collectDynamicApiKeyPatch(popover)
      })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    }
    await chrome.storage.sync.set({
      companionUrl: baseUrl,
      leaRepoPath: payload.leaRepoPath,
      leaApiBaseUrl: payload.leaApiBaseUrl,
      leaUiBaseUrl: payload.leaUiBaseUrl || current.leaUiBaseUrl || DEFAULT_LEA_UI_BASE_URL,
      leaModel: payload.leaModel,
      leaMaxTurns: payload.leaMaxTurns,
      leaMaxSpendUsd: payload.leaMaxSpendUsd,
      leaTheoremTranslationMaxRetries: payload.leaTheoremTranslationMaxRetries,
      leaTexMirrorEnabled: payload.leaTexMirrorEnabled
    });
    return payload;
  }

  async function loadUsage(popover) {
    if (!popover?.isConnected) return;
    const settings = await getSettings();
    const baseUrl = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
    const response = await fetch(`${baseUrl}/usage?overleafProjectId=${encodeURIComponent(extractOverleafProjectId())}`);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || `Companion returned HTTP ${response.status}.`);
    }
    renderUsage(popover, "project", payload.project);
    renderUsage(popover, "allTime", payload.allTime);
    renderCostCapSummary(popover, payload);
  }

  function scheduleUsageRefresh(popover) {
    clearTimeout(usageRefreshTimer);
    usageRefreshTimer = setTimeout(async () => {
      if (!popover?.isConnected || activePopover !== popover) {
        return;
      }
      try {
        await loadUsage(popover);
      } catch (error) {
        const status = popover.querySelector(".ol-lean-popover-status");
        if (status) status.textContent = error instanceof Error ? error.message : String(error);
      }
      scheduleUsageRefresh(popover);
      // 5s, not 1s (AUDIT M4): each refresh fans out to the adapter's
      // /api/stats; the settings popover doesn't need per-second usage.
    }, 5000);
  }

  function renderUsage(popover, key, usage) {
    const row = popover.querySelector(`[data-usage='${key}']`);
    if (!row) return;
    row.querySelector("[data-field='cost']").textContent = formatCost(usage?.costUsd || 0);
    row.querySelector("[data-field='input']").textContent = formatTokens(usage?.inputTokens || 0);
    row.querySelector("[data-field='output']").textContent = formatTokens(usage?.outputTokens || 0);
  }

  function renderCostCapSummary(popover, payload) {
    const summary = popover.querySelector("[data-role='cost-cap-summary']");
    if (!summary) return;
    const maxSpend = payload?.leaMaxSpendUsd;
    if (maxSpend === null || maxSpend === undefined || maxSpend === "") {
      summary.hidden = true;
      summary.textContent = "";
      return;
    }
    const current = payload?.leaCurrentSpendUsd ?? payload?.allTime?.costUsd ?? 0;
    summary.hidden = false;
    summary.textContent = `Cost cap: ${formatCost(current)} / ${formatCost(maxSpend)}`;
    const reached = Boolean(payload?.leaSpendLimitReached);
    summary.dataset.reached = reached ? "true" : "false";
  }

  function isMaxSpendError(error) {
    return error?.code === MAX_SPEND_ERROR_CODE ||
      String(error instanceof Error ? error.message : error).includes("Max spend limit");
  }

  function normalizeMaxSpendInput(value) {
    const trimmed = String(value || "").trim();
    if (!trimmed) return "";
    const number = Number(trimmed);
    return Number.isFinite(number) && number >= 0 ? String(number) : trimmed;
  }

  function parseMaxSpendInput(value) {
    const trimmed = String(value || "").trim();
    if (!trimmed) return null;
    const number = Number(trimmed);
    if (!Number.isFinite(number) || number < 0) {
      throw new Error("Cost cap must be a non-negative dollar amount.");
    }
    return number;
  }

  function formatTokens(value) {
    const number = Number(value) || 0;
    if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(2)}M`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(1)}k`;
    return String(number);
  }

  function formatCost(value) {
    const number = Number(value) || 0;
    if (number > 0 && number < 0.01) return "<$0.01";
    return `$${number.toFixed(2)}`;
  }

  function isExtensionContextInvalidated() {
    return !globalThis.chrome?.runtime?.id;
  }

  function normalizeErrorMessage(error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/Extension context invalidated/i.test(message)) {
      return "Extension was reloaded. Refresh this Overleaf tab.";
    }
    return message;
  }

  function rememberTarget(target) {
    if (!target?.targetLabel) return;
    const key = targetKey(target);
    const existingIndex = latestTargets.findIndex((item) => targetKey(item) === key);
    if (existingIndex === -1) {
      latestTargets = [...latestTargets, target];
      return;
    }
    latestTargets = latestTargets.map((item, index) => (index === existingIndex ? target : item));
  }

  function targetKey(target) {
    return `${target?.targetKind || "theorem"}:${target?.targetLabel || ""}`;
  }

  function isDefinitionTarget(target) {
    return target?.targetKind === "definition";
  }

  function targetNoun(target) {
    return isDefinitionTarget(target) ? "definition" : "theorem";
  }

  function extractOverleafProjectId() {
    const match = location.pathname.match(/\/project\/([^/]+)/);
    return match ? match[1] : "unknown";
  }

  function normalizeDocPath(value) {
    return String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").trim();
  }

  function normalizeTargetText(text) {
    return String(text).replace(/\s+/g, " ").trim();
  }

  async function sha256(text) {
    const data = new TextEncoder().encode(text);
    const digest = await crypto.subtle.digest("SHA-256", data);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function startEventsClient() {
    try {
      const module = await import(chrome.runtime.getURL("eventsClient.mjs"));
      // The companion URL comes from settings (async); cache it for the
      // client's synchronous url() and refresh the cache on every reconnect
      // attempt so a settings change is picked up without a page reload.
      let companionUrlCache = DEFAULT_COMPANION_URL;
      const refreshUrlCache = () => {
        getSettings()
          .then((settings) => {
            companionUrlCache = String(settings.companionUrl || DEFAULT_COMPANION_URL).replace(/\/+$/, "");
          })
          .catch(() => {});
      };
      refreshUrlCache();
      eventsClient = module.createEventsClient({
        url: () => {
          refreshUrlCache();
          const projectId = extractOverleafProjectId();
          const query = projectId && projectId !== "unknown"
            ? `?projectId=${encodeURIComponent(projectId)}`
            : "";
          return `${companionUrlCache}/events${query}`;
        },
        onEvent: handlePushEvent,
        onConnectionChange: (connected) => {
          pushConnected = connected;
          if (connected) {
            // Reconcile once on (re)connect: anything that changed while the
            // stream was down is picked up now instead of on the slow poll.
            scheduleStatusRefresh();
            if (leanPane) scheduleLeanPaneRefresh();
          }
        },
        // Bind EventSource + timers to the content-script scope: the module's
        // own defaults resolve in the module realm, which under the test
        // harness is Node's — real sockets and a real clock (same trap as
        // editorHookWatchdog). `typeof` guard: no EventSource here means the
        // push channel is unavailable and the polls stay primary.
        EventSourceImpl: typeof EventSource === "undefined" ? null : EventSource,
        setTimeoutImpl: (fn, ms) => setTimeout(fn, ms),
        clearTimeoutImpl: (id) => clearTimeout(id)
      });
      eventsClient.start();
    } catch {
      // Push is an optimization; the poll fallback keeps everything working.
    }
  }

  function handlePushEvent(type, data) {
    if (type === "jobs-changed") {
      scheduleStatusRefresh();
      if (leanPane) scheduleLeanPaneRefresh();
      return;
    }
    if (type === "chat-updated") {
      // Only refetch when the chat panel is open — and if the event names a
      // target, only when it's the one being viewed.
      if (!leanPaneChatPanel || !leanPaneChatItem) return;
      const eventKey = data && typeof data.targetKey === "string" ? data.targetKey : "";
      if (eventKey && leanPaneChatTarget?.targetKey && eventKey !== leanPaneChatTarget.targetKey) return;
      pollChatSession().catch(() => {});
      return;
    }
    if (type === "repair-batch-updated") {
      if (!leanPaneRepairBatch?.batchId) return;
      if (data && data.batchId && data.batchId !== leanPaneRepairBatch.batchId) return;
      startRepairBatchPolling({ immediate: true });
    }
  }

  async function startEditorHookWatchdog() {
    try {
      const module = await import(chrome.runtime.getURL("editorHookWatchdog.mjs"));
      editorHookWatchdog = module.createEditorHookWatchdog({
        // querySelector guard: exotic embedding contexts (and the test
        // harness's minimal document) may lack it — treat as "no editor".
        isEditorPresent: () =>
          typeof document.querySelector === "function" &&
          Boolean(document.querySelector(".cm-editor, .cm-content")),
        onWarn: renderEditorHookWarning,
        onRecover: removeEditorHookWarning,
        // Bind timers to the content-script scope: the module's own defaults
        // resolve in the module realm, which under the test harness is the
        // real Node clock rather than the page's (fake) one.
        setTimeoutImpl: (fn, ms) => setTimeout(fn, ms),
        clearTimeoutImpl: (id) => clearTimeout(id)
      });
      // The hook signal can beat the module import — honor it instead of arming.
      if (editorHookSignalSeen) editorHookWatchdog.editorHooked();
      else editorHookWatchdog.arm();
    } catch {
      // Best-effort: the watchdog must never break the page.
    }
  }

  function renderEditorHookWarning() {
    if (editorHookWarningBanner) return;
    const banner = document.createElement("div");
    banner.className = "ol-lean-editor-hook-warning";
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = "Lea can't attach to the Overleaf editor";
    const detail = document.createElement("span");
    detail.textContent = "Overleaf may have changed its editor internals. Theorem badges and % lea: markers won't work until the extension is updated.";
    body.append(title, detail);
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.textContent = "×";
    dismiss.addEventListener("click", removeEditorHookWarning);
    banner.append(body, dismiss);
    (document.body || document.documentElement).appendChild(banner);
    editorHookWarningBanner = banner;
  }

  function removeEditorHookWarning() {
    editorHookWarningBanner?.remove();
    editorHookWarningBanner = null;
  }

  function injectPageBridge() {
    const script = document.createElement("script");
    script.type = "module";
    script.src = chrome.runtime.getURL("pageBridge.js");
    script.onload = () => script.remove();
    const target = document.documentElement || document.head || document.body;
    if (target) {
      target.appendChild(script);
      return;
    }
    document.addEventListener("DOMContentLoaded", () => {
      (document.documentElement || document.head || document.body).appendChild(script);
    }, { once: true });
  }

  function requestTargetsSoon() {
    requestTargets();
    setTimeout(requestTargets, 300);
    setTimeout(requestTargets, 1000);
  }

  function requestTargets() {
    window.postMessage({ type: "OL_LEAN_REQUEST_TARGETS" }, "*");
  }
})();
