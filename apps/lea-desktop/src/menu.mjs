// Native application menu. Items that act on the UI send a navigation message
// to the renderer (see preload.cjs and App.tsx's onNavigate hook).
import { Menu, app, clipboard, dialog, shell } from 'electron';

const GITHUB_URL = 'https://github.com/VIDA-NYU/Lea';
const DISCORD_URL = 'https://discord.gg/58ZWUXU8FM';
const EXTENSION_GUIDE_URL = `${GITHUB_URL}/blob/main/apps/overleaf-extension/README.md`;
const OVERLEAF_URL = 'https://www.overleaf.com/project';

export function buildMenu({ navigate, getRuntime, getBackend, getLogDir, getDataDir, checkForUpdates }) {
  const isMac = process.platform === 'darwin';
  const settingsItem = { label: 'Settings…', accelerator: 'CmdOrCtrl+,', click: () => navigate('settings') };
  const updatesItem = { label: 'Check for Updates…', click: () => checkForUpdates?.() };

  const template = [
    ...(isMac ? [{
      label: app.name,
      submenu: [
        { role: 'about' },
        updatesItem,
        { type: 'separator' },
        settingsItem,
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    }] : []),
    {
      label: 'File',
      submenu: [
        { label: 'Home', accelerator: 'CmdOrCtrl+Shift+H', click: () => navigate('main') },
        { type: 'separator' },
        ...(isMac ? [{ role: 'close' }] : [settingsItem, { type: 'separator' }, { role: 'quit' }]),
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        ...(isMac
          ? [{ role: 'pasteAndMatchStyle' }, { role: 'delete' }, { role: 'selectAll' },
             { type: 'separator' },
             { label: 'Speech', submenu: [{ role: 'startSpeaking' }, { role: 'stopSpeaking' }] }]
          : [{ role: 'delete' }, { type: 'separator' }, { role: 'selectAll' }]),
      ],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Statistics', accelerator: 'CmdOrCtrl+Shift+S', click: () => navigate('stats') },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Overleaf',
      submenu: [
        { label: 'Open Overleaf', click: () => shell.openExternal(OVERLEAF_URL) },
        { label: 'Install the Chrome extension…', click: () => shell.openExternal(EXTENSION_GUIDE_URL) },
        { type: 'separator' },
        { label: 'Companion status…', click: () => showCompanionStatus(getRuntime()) },
        { label: 'Copy companion URL', click: () => clipboard.writeText(getRuntime()?.companionUrl || 'http://127.0.0.1:31245') },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        { role: 'zoom' },
        ...(isMac ? [{ type: 'separator' }, { role: 'front' }, { type: 'separator' }, { role: 'window' }] : [{ role: 'close' }]),
      ],
    },
    {
      role: 'help',
      submenu: [
        { label: 'Lea on GitHub', click: () => shell.openExternal(GITHUB_URL) },
        { label: 'Join the Discord', click: () => shell.openExternal(DISCORD_URL) },
        { type: 'separator' },
        ...(isMac ? [] : [updatesItem]),
        { label: 'Backend status…', click: () => showBackendStatus(getRuntime(), getBackend()) },
        { label: 'Show logs folder', click: () => { const dir = getLogDir(); if (dir) shell.openPath(dir); } },
        { label: 'Show data folder', click: () => { const dir = getDataDir?.(); if (dir) shell.openPath(dir); } },
      ],
    },
  ];

  return Menu.buildFromTemplate(template);
}

async function showBackendStatus(runtime, backend) {
  if (!runtime) {
    await dialog.showMessageBox({ type: 'info', title: 'Backend status', message: 'The backend is still starting.' });
    return;
  }
  const health = await fetchJson(`${runtime.apiUrl}/api/health`);
  const lines = [
    `Mode: ${runtime.mode === 'attached' ? 'attached to a stack started outside the app' : 'started by the app'}`,
    `API: ${runtime.apiUrl}`,
    `UI: ${runtime.uiUrl}`,
    health
      ? `Health: ${health.ok ? 'ok' : 'not ok'} · worker ${health.lifecycle_state || 'unknown'} · ` +
        `${health.active_runs ?? 0} active, ${health.queued_runs ?? 0} queued`
      : 'Health: no answer',
  ];
  if (backend?.processes?.length) lines.push(`Processes owned by the app: ${backend.processes.join(', ')}`);
  await dialog.showMessageBox({ type: 'info', title: 'Backend status', message: 'Lea backend', detail: lines.join('\n') });
}

async function showCompanionStatus(runtime) {
  const url = runtime?.companionUrl || 'http://127.0.0.1:31245';
  const health = await fetchJson(`${url}/health`);
  await dialog.showMessageBox({
    type: health ? 'info' : 'warning',
    title: 'Overleaf companion',
    message: health ? 'The Overleaf companion is running.' : 'The Overleaf companion is not reachable.',
    detail: health
      ? `${url}\n\nThe Chrome extension connects here by default; no configuration is needed.`
      : `${url}\n\nStart the companion (the app starts it when it owns the backend), then reload the Overleaf tab.`,
  });
}

async function fetchJson(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(2000) });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}
