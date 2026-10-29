// Bridge exposed by the desktop shell's sandboxed preload
// (apps/lea-desktop/src/preload.cjs). Absent when the UI runs in a browser.

/** Auto-update progress as the shell reports it (apps/lea-desktop/src/updater.mjs). */
interface LeaDesktopUpdateState {
  state: 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'uptodate' | 'error';
  version: string | null;
  percent: number;
  /** True when this build cannot install in place; the button opens the download instead. */
  downloadOnly: boolean;
  /** What "download" opens: the DMG for this architecture, else the release page. */
  releaseUrl: string;
  /** The release page ("What's new"). */
  notesUrl?: string;
  message: string;
}

interface LeaDesktopBridge {
  platform: string;
  /** Startup progress while the backend comes up; returns an unsubscribe. */
  onStatus(callback: (status: { step: string; message: string }) => void): () => void;
  /** Native menu → UI navigation; returns an unsubscribe. */
  onNavigate(callback: (target: { view: string }) => void): () => void;
  /** Open an http(s) URL in the system browser. */
  openExternal(url: string): Promise<void>;
  /** Show an absolute path in the Finder; false when it is not absolute or does not exist. */
  revealPath?(path: string): Promise<boolean>;
  /** Help → Report a problem…: bundle logs and a redacted config into a zip. */
  reportProblem?(): Promise<string | null>;
  /** Update state changes; returns an unsubscribe. */
  onUpdate?(callback: (state: LeaDesktopUpdateState) => void): () => void;
  getUpdateState?(): Promise<LeaDesktopUpdateState | null>;
  /** Download, restart to install, or open the download page, depending on the state. */
  installUpdate?(): Promise<void>;
  checkForUpdates?(): Promise<void>;
  /** Dock badge: how many sessions have a live run. */
  setActivity?(activity: { running: number }): Promise<void>;
  /** System notification, shown only while the window is not in front. */
  notify?(notification: { title: string; body?: string; sessionId?: string }): Promise<boolean>;
}

interface Window {
  leaDesktop?: LeaDesktopBridge;
}
