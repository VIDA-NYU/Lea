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
  // Startup progress while the backend comes up (splash page).
  onStatus: (callback) => subscribe('lea:status', callback),
  // Native menu → UI navigation, e.g. { view: 'settings' } (App.tsx).
  onNavigate: (callback) => subscribe('lea:navigate', callback),
  // Open an http(s) URL in the system browser.
  openExternal: (url) => ipcRenderer.invoke('lea:open-external', String(url)),
});
