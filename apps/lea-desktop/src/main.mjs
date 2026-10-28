// Lea Desktop — proof of concept.
//
// A native window around the existing Lea UI, with the backend started and
// stopped by the app. See README.md for what is and is not in this POC.
import { app, BrowserWindow, Menu, dialog, ipcMain, nativeImage, nativeTheme, shell } from 'electron';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createBackend } from './backend.mjs';
import { buildMenu } from './menu.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(here, '..');
const MONOREPO_ROOT = path.resolve(APP_ROOT, '..', '..');
const ICON_PNG = path.join(APP_ROOT, 'assets', 'icon.png');
const PROTOCOL = 'lea';
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);
// Smoke-test mode (LEA_DESKTOP_SCREENSHOT set): never block on a dialog.
const SMOKE = Boolean(process.env.LEA_DESKTOP_SCREENSHOT);

const log = (...args) => console.log('[lea-desktop]', ...args);

let win = null;
let backend = null;
let runtime = null; // { mode, apiUrl, uiUrl, companionUrl }
let logDir = null;
let quitting = false;
const pendingDeepLinks = [];

app.setName('Lea');

// ---- single instance + lea:// protocol --------------------------------------

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => {
    focusWindow();
    for (const arg of argv) if (arg.startsWith(`${PROTOCOL}://`)) handleDeepLink(arg);
  });

  // In a packaged build the scheme comes from Info.plist; in `electron .` dev
  // mode macOS ignores this, so lea:// links only work once packaged.
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient(PROTOCOL, process.execPath, [path.resolve(process.argv[1])]);
  } else {
    app.setAsDefaultProtocolClient(PROTOCOL);
  }
  app.on('open-url', (event, url) => {
    event.preventDefault();
    handleDeepLink(url);
  });

  main().catch((error) => {
    log(error.stack || error.message);
    app.exit(1);
  });
}

// ---- lifecycle ---------------------------------------------------------------

async function main() {
  await app.whenReady();

  logDir = app.getPath('logs');
  fs.mkdirSync(logDir, { recursive: true });
  if (process.platform === 'darwin' && fs.existsSync(ICON_PNG)) {
    app.dock.setIcon(nativeImage.createFromPath(ICON_PNG));
  }
  app.setAboutPanelOptions({
    applicationName: 'Lea',
    applicationVersion: app.getVersion(),
    copyright: 'VIDA, New York University',
    iconPath: ICON_PNG,
  });

  ipcMain.handle('lea:open-external', (_event, url) => {
    if (/^https?:\/\//i.test(String(url))) return shell.openExternal(String(url));
    return undefined;
  });

  createWindow();
  Menu.setApplicationMenu(buildMenu({
    navigate,
    getRuntime: () => runtime,
    getBackend: () => backend,
    getLogDir: () => logDir,
  }));

  backend = createBackend({ root: MONOREPO_ROOT, logDir, log, onStatus: sendStatus, onCrash });

  try {
    runtime = await backend.start();
  } catch (error) {
    log(error.stack || error.message);
    sendStatus({ step: 'error', message: error.message });
    if (!SMOKE) {
      await dialog.showMessageBox(win ?? undefined, {
        type: 'error',
        title: 'Lea could not start',
        message: 'Lea could not start its backend.',
        detail: `${error.message}\n\nLogs: ${logDir}`,
        buttons: ['Quit'],
      });
    }
    quitting = true;
    if (!backend.stop(1)) app.exit(1);
    return;
  }

  log(`backend ${runtime.mode}; ui ${runtime.uiUrl}; api ${runtime.apiUrl}`);
  sendStatus({ step: 'loading', message: 'Opening Lea…' });
  if (win) await win.loadURL(runtime.uiUrl);
  for (const link of pendingDeepLinks.splice(0)) handleDeepLink(link);
  maybeSmokeTest();
}

app.on('before-quit', (event) => {
  if (quitting) return;
  quitting = true;
  // The supervisor ends the process itself once every child is gone.
  if (backend?.stop()) event.preventDefault();
});

app.on('window-all-closed', () => {
  // macOS convention: the app (and its backend) keeps running without a window,
  // which also keeps the Overleaf companion available.
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (!win) createWindow(runtime?.uiUrl);
});

// ---- window -------------------------------------------------------------------

function createWindow(initialUrl = null) {
  const state = loadWindowState();
  win = new BrowserWindow({
    width: state.width ?? 1360,
    height: state.height ?? 880,
    x: state.x,
    y: state.y,
    minWidth: 960,
    minHeight: 640,
    title: 'Lea',
    show: false,
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#111318' : '#f7f7f5',
    webPreferences: {
      preload: path.join(here, 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: true,
    },
  });

  win.once('ready-to-show', () => win?.show());
  win.on('page-title-updated', (event) => event.preventDefault()); // keep "Lea"
  win.on('close', () => { if (win) saveWindowState(win.getBounds()); });
  win.on('closed', () => { win = null; });

  // Anything that is not our own local UI opens in the system browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isLocalUrl(url)) win?.loadURL(url);
    else if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (isLocalUrl(url) || url.startsWith('file:')) return;
    event.preventDefault();
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
  });

  if (initialUrl) win.loadURL(initialUrl);
  else win.loadFile(path.join(here, 'splash.html'));
}

function focusWindow() {
  if (!win) {
    createWindow(runtime?.uiUrl);
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function sendStatus(status) {
  win?.webContents.send('lea:status', status);
}

function navigate(view) {
  win?.webContents.send('lea:navigate', { view });
  focusWindow();
}

// lea://session/<id> · lea://formalization/<id> · lea://settings · lea://open
function handleDeepLink(raw) {
  let url;
  try { url = new URL(raw); } catch { return; }
  if (url.protocol !== `${PROTOCOL}:`) return;
  if (!runtime) { pendingDeepLinks.push(raw); return; }
  const kind = url.hostname;
  const id = decodeURIComponent(url.pathname.replace(/^\/+/, ''));
  focusWindow();
  if (kind === 'session' && id) win?.loadURL(`${runtime.uiUrl}/?session=${encodeURIComponent(id)}`);
  else if (kind === 'formalization' && id) win?.loadURL(`${runtime.uiUrl}/?formalization=${encodeURIComponent(id)}`);
  else if (kind === 'settings') navigate('settings');
}

async function onCrash(label, { code, signal }) {
  if (quitting) return;
  if (SMOKE) {
    log(`${label} stopped unexpectedly (${signal || `exit code ${code}`}); quitting`);
    app.quit();
    return;
  }
  const { response } = await dialog.showMessageBox(win ?? undefined, {
    type: 'warning',
    title: 'Lea backend stopped',
    message: `The Lea ${label} process stopped unexpectedly (${signal || `exit code ${code}`}).`,
    detail: `Logs: ${logDir}`,
    buttons: ['Quit Lea', 'Keep the window open'],
    defaultId: 0,
    cancelId: 1,
  });
  if (response === 0) app.quit();
}

function isLocalUrl(value) {
  try {
    const url = new URL(value);
    return (url.protocol === 'http:' || url.protocol === 'https:') && LOOPBACK.has(url.hostname);
  } catch {
    return false;
  }
}

// ---- remembered window bounds -----------------------------------------------

function windowStatePath() {
  return path.join(app.getPath('userData'), 'window-state.json');
}

function loadWindowState() {
  try {
    return JSON.parse(fs.readFileSync(windowStatePath(), 'utf8'));
  } catch {
    return {};
  }
}

function saveWindowState(bounds) {
  try {
    fs.mkdirSync(path.dirname(windowStatePath()), { recursive: true });
    fs.writeFileSync(windowStatePath(), JSON.stringify(bounds));
  } catch (error) {
    log(`could not save window state: ${error.message}`);
  }
}

// ---- smoke test hook -----------------------------------------------------------
// LEA_DESKTOP_SCREENSHOT=<file.png> captures the window once the UI has settled,
// then quits. Used to verify the shell without a human at the screen.

function maybeSmokeTest() {
  const target = process.env.LEA_DESKTOP_SCREENSHOT;
  if (!target || !win) return;
  const delay = Number(process.env.LEA_DESKTOP_SCREENSHOT_DELAY_MS || 6000);
  // LEA_DESKTOP_SCREENSHOT_VIEW=settings exercises the native-menu → UI bridge
  // before the capture.
  const view = process.env.LEA_DESKTOP_SCREENSHOT_VIEW;
  if (view) setTimeout(() => navigate(view), Math.max(500, delay - 2000));
  setTimeout(async () => {
    try {
      const image = await win.webContents.capturePage();
      fs.writeFileSync(path.resolve(target), image.toPNG());
      log(`screenshot written to ${path.resolve(target)}`);
    } catch (error) {
      log(`screenshot failed: ${error.message}`);
    }
    app.quit();
  }, delay);
}
