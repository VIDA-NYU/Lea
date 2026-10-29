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

  focusMode: false,
  setFocusMode: (focusMode) => set({ focusMode }),
  drawerTab: 'goals',
  setDrawerTab: (drawerTab) => set({ drawerTab }),
}));
