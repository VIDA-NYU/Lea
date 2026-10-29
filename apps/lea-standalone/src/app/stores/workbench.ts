import { create } from 'zustand';

/**
 * Workbench state shared between the redesign's work packages (see
 * docs/PLAN-ui-redesign.md). Each package reads and writes only its own slice
 * through this store; App.tsx stays thin.
 *
 *   files      → openPath, filesRailOpen, quickOpenOpen
 *   blueprint  → selectedNodeKey, nodeBindings
 *   canvas     → focusMode, drawerTab
 */

/** Repo-relative path of the project blueprint; opening it shows the graph. */
export const BLUEPRINT_PATH = '.lea/blueprint.md';

/** Per-file status as the files rail and breadcrumbs show it. */
export type FileStatus = 'proved' | 'sorry' | 'error' | 'unchecked' | 'running';

export type DrawerTab = 'goals' | 'problems' | 'verify';

/**
 * A freshly drafted blueprint awaiting review — the `/blueprint` proposal state
 * (blueprint package, mockup frame 8). The draft already sits in
 * `.lea/blueprint.md` (the adapter's generate command writes the file directly);
 * `phase: 'review'` tells BlueprintView to present it as a proposal with per-node
 * checkboxes and Accept / Edit markdown / Discard instead of as the saved graph.
 */
export interface BlueprintProposal {
  projectId: string;
  /** drafting: the seeder or a drafting run is in flight · review: awaiting Accept / Discard. */
  phase: 'drafting' | 'review';
  /** Where the draft comes from: the artifact seeder (`/blueprint/generate`) or an agent run. */
  source: 'artifacts' | 'agent';
  /** The session whose run is drafting (agent source); review starts once it settles. */
  draftingSessionId?: string;
  /** Advisory notes from the seeder. */
  notes?: string[];
  error?: string;
}

/**
 * A composer submission a slash command wants App to send through its normal
 * submit path (the runner cannot reach the composer or the run stream). The
 * integrator wires it: when set, App submits `text` in `sessionId` (or the open
 * session) exactly as if typed, then clears it.
 */
export interface PendingSubmission {
  text: string;
  sessionId?: string;
}

interface WorkbenchState {
  // ── files ──────────────────────────────────────────────────────────────
  /** File open in the canvas (repo-relative). null = the session's own proof. */
  openPath: string | null;
  openFile: (path: string | null) => void;
  filesRailOpen: boolean;
  setFilesRailOpen: (open: boolean) => void;
  toggleFilesRail: () => void;
  quickOpenOpen: boolean;
  setQuickOpenOpen: (open: boolean) => void;

  // ── blueprint ──────────────────────────────────────────────────────────
  selectedNodeKey: string | null;
  selectNode: (key: string | null) => void;
  /**
   * sessionId → blueprint node key. UI-side binding until the backend stores
   * `blueprint_node` on sessions; persisted in localStorage by the blueprint
   * package so a reload keeps the chip and the strip.
   */
  nodeBindings: Record<string, string>;
  bindSessionToNode: (sessionId: string, nodeKey: string | null) => void;
  /** The `/blueprint` proposal under review, or null when the blueprint is the saved one. */
  blueprintProposal: BlueprintProposal | null;
  setBlueprintProposal: (proposal: BlueprintProposal | null) => void;
  /** Bumped after anything writes the blueprint or starts a node session; graph readers refetch. */
  blueprintRefresh: number;
  bumpBlueprintRefresh: () => void;
  /** A one-line notice the blueprint view shows (e.g. "Started 2 sessions"); null = none. */
  blueprintNotice: string | null;
  setBlueprintNotice: (notice: string | null) => void;
  /** See PendingSubmission — consumed by App (integrator wiring). */
  pendingSubmission: PendingSubmission | null;
  setPendingSubmission: (submission: PendingSubmission | null) => void;

  // ── canvas ─────────────────────────────────────────────────────────────
  focusMode: boolean;
  setFocusMode: (on: boolean) => void;
  drawerTab: DrawerTab;
  setDrawerTab: (tab: DrawerTab) => void;
}

const BINDINGS_KEY = 'lea:nodeBindings';

function loadBindings(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(BINDINGS_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export const useWorkbench = create<WorkbenchState>((set) => ({
  openPath: null,
  openFile: (openPath) => set({ openPath }),
  filesRailOpen: false,
  setFilesRailOpen: (filesRailOpen) => set({ filesRailOpen }),
  toggleFilesRail: () => set((s) => ({ filesRailOpen: !s.filesRailOpen })),
  quickOpenOpen: false,
  setQuickOpenOpen: (quickOpenOpen) => set({ quickOpenOpen }),

  selectedNodeKey: null,
  selectNode: (selectedNodeKey) => set({ selectedNodeKey }),
  nodeBindings: loadBindings(),
  bindSessionToNode: (sessionId, nodeKey) =>
    set((s) => {
      const nodeBindings = { ...s.nodeBindings };
      if (nodeKey) nodeBindings[sessionId] = nodeKey;
      else delete nodeBindings[sessionId];
      try {
        window.localStorage.setItem(BINDINGS_KEY, JSON.stringify(nodeBindings));
      } catch {
        // storage unavailable: binding lives for this page only
      }
      return { nodeBindings };
    }),
  blueprintProposal: null,
  setBlueprintProposal: (blueprintProposal) => set({ blueprintProposal }),
  blueprintRefresh: 0,
  bumpBlueprintRefresh: () => set((s) => ({ blueprintRefresh: s.blueprintRefresh + 1 })),
  blueprintNotice: null,
  setBlueprintNotice: (blueprintNotice) => set({ blueprintNotice }),
  pendingSubmission: null,
  setPendingSubmission: (pendingSubmission) => set({ pendingSubmission }),

  focusMode: false,
  setFocusMode: (focusMode) => set({ focusMode }),
  drawerTab: 'goals',
  setDrawerTab: (drawerTab) => set({ drawerTab }),
}));
