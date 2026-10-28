// Bridge exposed by the desktop shell's sandboxed preload
// (apps/lea-desktop/src/preload.cjs). Absent when the UI runs in a browser.
interface LeaDesktopBridge {
  platform: string;
  /** Startup progress while the backend comes up; returns an unsubscribe. */
  onStatus(callback: (status: { step: string; message: string }) => void): () => void;
  /** Native menu → UI navigation; returns an unsubscribe. */
  onNavigate(callback: (target: { view: string }) => void): () => void;
  /** Open an http(s) URL in the system browser. */
  openExternal(url: string): Promise<void>;
}

interface Window {
  leaDesktop?: LeaDesktopBridge;
}
