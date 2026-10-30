import { create } from 'zustand';

/**
 * State the desktop shell pushes into the UI (apps/lea-desktop). Empty in a
 * browser. App.tsx feeds it from the preload bridge; the Sidebar footer renders
 * the update button from it.
 */
interface DesktopState {
  update: LeaDesktopUpdateState | null;
  setUpdate: (update: LeaDesktopUpdateState | null) => void;
}

export const useDesktop = create<DesktopState>((set) => ({
  update: null,
  setUpdate: (update) => set({ update }),
}));
