// Sandboxed preload: the only bridge between the native shell and the page.
// Exposed as window.leaDesktop on both the splash page and the Lea UI.
const { contextBridge, ipcRenderer } = require('electron');

function subscribe(channel, callback) {
  const handler = (_event, payload) => callback(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

contextBridge.exposeInMainWorld('leaDesktop', {
  platform: process.platform,
  // Startup progress while the backend is provisioned and comes up (splash page):
  // { step, state: 'running' | 'done' | 'failed' | 'skipped', message }.
  onStatus: (callback) => subscribe('lea:status', callback),
  // Raw output lines from provisioning commands (splash page log tail).
  onLog: (callback) => subscribe('lea:log', callback),
  // Native menu → UI navigation, e.g. { view: 'settings' } (App.tsx).
  onNavigate: (callback) => subscribe('lea:navigate', callback),
  // Open an http(s) URL in the system browser.
  openExternal: (url) => ipcRenderer.invoke('lea:open-external', String(url)),
  // Splash "Open logs" / "Quit" buttons.
  showLogs: () => ipcRenderer.invoke('lea:show-logs'),
  quit: () => ipcRenderer.invoke('lea:quit'),
  // Help → Report a problem…, also reachable from the splash page when startup fails.
  reportProblem: () => ipcRenderer.invoke('lea:report-problem'),

  // Updates: { state: 'idle'|'checking'|'available'|'downloading'|'ready'|'uptodate'|'error',
  //            version, percent, downloadOnly, releaseUrl, message }.
  onUpdate: (callback) => subscribe('lea:update', callback),
  getUpdateState: () => ipcRenderer.invoke('lea:update-state'),
  // Download, restart to install, or open the download page, depending on the state.
  installUpdate: () => ipcRenderer.invoke('lea:update-install'),
  checkForUpdates: () => ipcRenderer.invoke('lea:update-check'),

  // Activity for the dock badge: { running: <number of sessions with a live run> }.
  setActivity: (activity) => ipcRenderer.invoke('lea:set-activity', activity),
  // A system notification, shown only while the window is not in front:
  // { title, body, sessionId? }. Clicking it opens the session.
  notify: (notification) => ipcRenderer.invoke('lea:notify', notification),
});
