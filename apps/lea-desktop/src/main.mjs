// Lea Desktop — main process.
//
// A native window around the Lea UI, with the backend provisioned, started and
// stopped by the app. See README.md for the runtime layout and build steps.
import { app, BrowserWindow, Menu, Notification, dialog, ipcMain, nativeImage, nativeTheme, shell } from 'electron';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createBackend } from './backend.mjs';
import { issueUrl, suggestedZipName, writeDiagnostics } from './diagnostics.mjs';
import { devLayout, managedLayout } from './layout.mjs';
import { buildMenu } from './menu.mjs';
import { createPrefs } from './prefs.mjs';
import { createProvisioner } from './provision.mjs';
import { setupUpdater } from './updater.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const APP_ROOT = path.resolve(here, '..');
const MONOREPO_ROOT = path.resolve(APP_ROOT, '..', '..');
const ICON_PNG = path.join(APP_ROOT, 'assets', 'icon.png');
const PROTOCOL = 'lea';
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);
// Smoke-test mode (LEA_DESKTOP_SCREENSHOT set): never block on a dialog.
const SMOKE = Boolean(process.env.LEA_DESKTOP_SCREENSHOT);

// Main-process log: stdout (visible under `npm start`) plus
// ~/Library/Logs/Lea/lea-desktop.log, since a Finder launch has no stdout.
let logFile = null;
const log = (...args) => {
  const line = `[lea-desktop] ${args.join(' ')}`;
  console.log(line);
  logFile?.write(`${new Date().toISOString()} ${line}\n`);
};

let win = null;
let backend = null;
let provisioner = null;
let updater = null;
let prefs = null;
let runtime = null; // { mode, apiUrl, uiUrl, companionUrl }
let layout = null;
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

  // In a packaged build the scheme comes from Info.plist (electron-builder's
  // `protocols`); under `electron .` macOS ignores this registration.
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

// ---- mode -------------------------------------------------------------------

function resolveLayout() {
  const managed = app.isPackaged || process.env.LEA_DESKTOP_MODE === 'managed';
  if (!managed) return { layout: devLayout(MONOREPO_ROOT), uvBinary: null };
  const resources = app.isPackaged ? process.resourcesPath : path.join(APP_ROOT, 'build');
  const backendSource = path.join(resources, 'backend');
  const bundledUv = app.isPackaged
    ? path.join(resources, 'bin', 'uv')
    : path.join(resources, 'bin', process.arch, 'uv');
  const uvBinary = fs.existsSync(bundledUv) ? bundledUv : 'uv';
  return { layout: managedLayout(app.getPath('userData'), backendSource), uvBinary };
}

// ---- lifecycle ---------------------------------------------------------------

async function main() {
  await app.whenReady();

  logDir = app.getPath('logs');
  fs.mkdirSync(logDir, { recursive: true });
  logFile = fs.createWriteStream(path.join(logDir, 'lea-desktop.log'), { flags: 'a' });
  log(`Lea ${app.getVersion()} starting (${app.isPackaged ? 'packaged' : 'dev'}, pid ${process.pid})`);
  app.on('will-quit', () => log('quitting'));
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
  ipcMain.handle('lea:show-logs', () => shell.openPath(logDir));
  ipcMain.handle('lea:quit', () => app.quit());
  ipcMain.handle('lea:report-problem', () => reportProblem());
  ipcMain.handle('lea:update-state', () => updater?.getState() ?? null);
  ipcMain.handle('lea:update-install', () => updater?.install());
  ipcMain.handle('lea:update-check', () => updater?.check({ interactive: false }));
  ipcMain.handle('lea:set-activity', (_event, activity) => setActivity(activity));
  ipcMain.handle('lea:notify', (_event, notification) => showNotification(notification));

  const resolved = resolveLayout();
  layout = resolved.layout;
  log(`mode ${layout.mode}${layout.mode === 'managed' ? ` (${layout.userData})` : ''}`);
  prefs = createPrefs(app.getPath('userData'), { log });

  createWindow();
  updater = setupUpdater({ log, broadcast: (state) => win?.webContents.send('lea:update', state) });
  Menu.setApplicationMenu(buildMenu({
    navigate,
    getRuntime: () => runtime,
    getBackend: () => backend,
    getLogDir: () => logDir,
    getDataDir: () => (layout.mode === 'managed' ? layout.userData : layout.dataDir),
    checkForUpdates: () => updater.check({ interactive: true }),
    reportProblem,
    prefs,
  }));

  backend = createBackend({ layout, logDir, log, onStatus: sendStatus, onCrash });

  try {
    let provisioned = { env: {} };
    if (layout.mode === 'managed') {
      const provisionLog = fs.createWriteStream(path.join(logDir, 'provision.log'), { flags: 'a' });
      provisionLog.write(`\n--- ${new Date().toISOString()} ---\n`);
      provisioner = createProvisioner(layout, {
        uvBinary: resolved.uvBinary,
        log: (message) => { log(message); provisionLog.write(`${message}\n`); },
        onStatus: sendStatus,
        onLog: (line) => {
          provisionLog.write(`${line}\n`);
          win?.webContents.send('lea:log', { line });
        },
      });
      try {
        provisioned = await provisioner.run();
      } finally {
        provisioner = null;
        provisionLog.end();
      }
    }
    // "Open in Lea" links from Overleaf: the lea:// scheme only resolves for a
    // packaged app, so dev builds keep pointing at the adapter-served UI.
    const uiBaseUrl = app.isPackaged ? `${PROTOCOL}://open` : undefined;
    runtime = await backend.start({ env: provisioned.env, uiBaseUrl });
  } catch (error) {
    if (quitting) return;
    log(error.stack || error.message);
    sendStatus({ step: 'services', state: 'error', message: error.message });
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
  // The UI origin is the same across app versions (127.0.0.1:8001); drop the
  // renderer's HTTP cache so an updated backend never serves against stale UI
  // code the previous version left behind.
  await win?.webContents.session.clearCache();
  if (win) await win.loadURL(runtime.uiUrl);
  for (const link of pendingDeepLinks.splice(0)) handleDeepLink(link);
  maybeSmokeTest();
}

app.on('before-quit', (event) => {
  if (quitting) return;
  quitting = true;
  provisioner?.cancel();
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
    // macOS: no title bar; the UI reserves a 28px drag strip for the traffic
    // lights when it detects the desktop shell (html.lea-desktop-mac).
    ...(process.platform === 'darwin'
      ? { titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 12, y: 8 } }
      : {}),
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
  log(`navigate → ${view}`);
  win?.webContents.send('lea:navigate', { view });
  focusWindow();
}

// ---- activity, notifications, diagnostics -------------------------------------

// Dock badge = number of sessions with a live run (renderer-driven; the UI
// already follows the sessions feed).
function setActivity(activity) {
  const running = Math.max(0, Number(activity?.running) || 0);
  if (process.platform === 'darwin') app.dock?.setBadge(running > 0 ? String(running) : '');
}

// A run finished (or failed) while the user was elsewhere. The renderer decides
// *what* happened; this decides *whether* to show it: never when the window is
// in front, never when the user turned notifications off.
function showNotification({ title, body, sessionId } = {}) {
  if (!title || !prefs?.get('notifications') || !Notification.isSupported()) return false;
  if (win && win.isVisible() && win.isFocused()) return false;
  const notification = new Notification({ title: String(title), body: body ? String(body) : undefined });
  notification.on('click', () => {
    if (sessionId) handleDeepLink(`${PROTOCOL}://session/${encodeURIComponent(String(sessionId))}`);
    else focusWindow();
  });
  notification.show();
  log(`notification: ${title}${body ? ` — ${body}` : ''}`);
  return true;
}

// Help → Report a problem…: logs, provisioning output, a redacted config and
// the environment summary, zipped where the user chooses, then a pointer to a
// prefilled GitHub issue.
async function reportProblem() {
  let zipPath;
  if (SMOKE) {
    zipPath = path.join(app.getPath('temp'), suggestedZipName());
  } else {
    const chosen = await dialog.showSaveDialog(win ?? undefined, {
      title: 'Save diagnostics',
      defaultPath: path.join(app.getPath('desktop'), suggestedZipName()),
      filters: [{ name: 'Zip archive', extensions: ['zip'] }],
    });
    if (chosen.canceled || !chosen.filePath) return null;
    zipPath = chosen.filePath;
  }
  try {
    await writeDiagnostics({ layout, logDir, runtime, backend, zipPath });
  } catch (error) {
    log(`diagnostics failed: ${error.message}`);
    if (!SMOKE) {
      await dialog.showMessageBox(win ?? undefined, { type: 'error', title: 'Report a problem', message: 'Could not write the diagnostics bundle.', detail: error.message });
    }
    return null;
  }
  log(`diagnostics written to ${zipPath}`);
  if (SMOKE) return zipPath;
  const { response } = await dialog.showMessageBox(win ?? undefined, {
    type: 'info',
    title: 'Report a problem',
    message: 'Diagnostics saved.',
    detail: `${zipPath}\n\nThe bundle holds the app, backend and provisioning logs, a redacted copy of the config and the environment summary. Attach it to the issue.`,
    buttons: ['Open a GitHub issue', 'Reveal in Finder', 'Done'],
    defaultId: 0,
    cancelId: 2,
  });
  if (response === 0) {
    shell.showItemInFolder(zipPath);
    await shell.openExternal(issueUrl({ runtime }));
  } else if (response === 1) {
    shell.showItemInFolder(zipPath);
  }
  return zipPath;
}

// lea://session/<id> · lea://formalization/<id> · lea://settings · lea://stats ·
// lea://open[?session=..&formalization=..]
function handleDeepLink(raw) {
  let url;
  try { url = new URL(raw); } catch { return; }
  if (url.protocol !== `${PROTOCOL}:`) return;
  if (!runtime) { pendingDeepLinks.push(raw); return; }
  const kind = url.hostname;
  const id = decodeURIComponent(url.pathname.replace(/^\/+/, ''));
  log(`deep link ${raw}`);
  focusWindow();
  const target = new URL(runtime.uiUrl);
  if (kind === 'session' && id) target.searchParams.set('session', id);
  else if (kind === 'formalization' && id) target.searchParams.set('formalization', id);
  else if (kind === 'settings' || kind === 'stats') { navigate(kind); return; }
  else if (kind === 'open') {
    for (const key of ['session', 'formalization']) {
      const value = url.searchParams.get(key);
      if (value) target.searchParams.set(key, value);
    }
  } else return;
  if ([...target.searchParams.keys()].length) win?.loadURL(target.toString());
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
  // LEA_DESKTOP_SCREENSHOT_HEIGHT=<px> makes the window tall enough to capture a
  // long page in one image.
  const height = Number(process.env.LEA_DESKTOP_SCREENSHOT_HEIGHT || 0);
  if (height > 0) win.setSize(win.getBounds().width, height);
  // LEA_DESKTOP_SCREENSHOT_SCROLL=<px> scrolls the page before the capture.
  const scroll = Number(process.env.LEA_DESKTOP_SCREENSHOT_SCROLL || 0);
  if (scroll > 0) {
    setTimeout(() => {
      win?.webContents.executeJavaScript(
        `(() => { const el = document.scrollingElement || document.documentElement; el.scrollTo(0, ${scroll});
          for (const node of document.querySelectorAll('*')) { if (node.scrollHeight > node.clientHeight + 50) node.scrollTo(0, ${scroll}); } })()`,
      ).catch(() => {});
    }, Math.max(500, delay - 1000));
  }
  win.webContents.executeJavaScript('typeof window.leaDesktop + "/" + typeof window.leaDesktop?.onNavigate')
    .then((result) => log(`bridge in page: ${result}`))
    .catch((error) => log(`bridge check failed: ${error.message}`));
  // LEA_DESKTOP_SMOKE_REPORT=1 writes a diagnostics bundle to the temp folder;
  // LEA_DESKTOP_SMOKE_NOTIFY=1 sends a notification through the bridge (the
  // shell declines it while the window is in front, which the log records).
  if (process.env.LEA_DESKTOP_SMOKE_REPORT) {
    setTimeout(() => reportProblem().then((file) => log(`smoke diagnostics: ${file}`)).catch((error) => log(`smoke diagnostics failed: ${error.message}`)), 1500);
  }
  if (process.env.LEA_DESKTOP_SMOKE_NOTIFY) {
    setTimeout(() => {
      win?.webContents.executeJavaScript('window.leaDesktop.notify({ title: "Lea smoke", body: "notification path" })')
        .then((shown) => log(`smoke notification shown: ${shown}`))
        .catch((error) => log(`smoke notification failed: ${error.message}`));
    }, 2000);
  }
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
