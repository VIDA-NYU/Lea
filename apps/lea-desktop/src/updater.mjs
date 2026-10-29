// Auto-update via electron-updater. The feed is the rolling `desktop-latest`
// GitHub release (see electron-builder.yml `publish` and the desktop-release
// workflow), kept separate from the repository's other releases.
//
// The state is broadcast to the renderer (preload `onUpdate`), which shows one
// button in the sidebar footer (DesktopUpdateButton); the native menu's Check
// for Updates… uses the same machinery. Two modes:
//
//   in-app        "Update to x.y.z" downloads here, "Restart to update" installs.
//   download-only macOS only applies an update to an app whose signature
//                 matches the running one, so an unsigned or ad-hoc-signed
//                 build cannot install in place: the button opens the DMG for
//                 this architecture in the browser instead.
//
// State: { state, version, percent, downloadOnly, releaseUrl, notesUrl, message }
//   state        idle | checking | available | downloading | ready | uptodate | error
//   version      the version the state is about (null until a check answered)
//   percent      download progress, 0–100
//   downloadOnly true when this build cannot install in place
//   releaseUrl   what "download" opens: the DMG for this architecture, else the release page
//   notesUrl     the release page ("What's new")
//   message      one line for a tooltip or a dialog
// A failed background check stays quiet (idle); a failed download is `error`
// and the button offers a retry.
import { app, dialog, shell } from 'electron';
import electronUpdater from 'electron-updater';
import { execFile } from 'node:child_process';
import path from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const { autoUpdater } = electronUpdater;

const FEED_URL = 'https://github.com/VIDA-NYU/Lea/releases/download/desktop-latest';
const RELEASE_PAGE = 'https://github.com/VIDA-NYU/Lea/releases/tag/desktop-latest';
const FIRST_CHECK_DELAY_MS = 15_000;
const RECHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

export function setupUpdater({ log, broadcast }) {
  let state = { state: 'idle', version: null, percent: 0, downloadOnly: false, releaseUrl: RELEASE_PAGE, notesUrl: RELEASE_PAGE, message: '' };
  const getState = () => state;
  const setState = (patch) => {
    state = { ...state, ...patch };
    broadcast?.(state);
  };

  if (!app.isPackaged) return fakeUpdater({ log, getState, setState });

  autoUpdater.logger = {
    info: (m) => log(`updater: ${terse(m)}`),
    warn: (m) => log(`updater: ${terse(m)}`),
    error: () => {}, // reported through the state + check() below
    debug: () => {},
  };
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  // Can this build install updates in place? Decided once, before the first
  // check, so the update-available handler can stay synchronous.
  let inApp = null;
  const signature = isTrustworthySignature().then((ok) => { inApp = ok; return ok; });
  let downloading = false;

  autoUpdater.on('checking-for-update', () => {
    // Keep an already-known update on the button while a re-check runs.
    if (['idle', 'uptodate'].includes(state.state)) setState({ state: 'checking', message: 'Checking for updates…' });
  });
  autoUpdater.on('update-not-available', (info) => {
    setState({ state: 'uptodate', version: info?.version ?? app.getVersion(), percent: 0, message: `Lea ${app.getVersion()} is the latest version.` });
  });
  autoUpdater.on('update-available', (info) => {
    const downloadOnly = inApp !== true;
    setState({
      state: 'available',
      version: info.version,
      percent: 0,
      downloadOnly,
      releaseUrl: directDownloadUrl(info) ?? RELEASE_PAGE,
      message: availableMessage(info.version, downloadOnly),
    });
    log(`updater: ${info.version} available (${downloadOnly ? 'download only' : 'in-app'})`);
  });
  autoUpdater.on('download-progress', (progress) => {
    const percent = Math.round(progress.percent);
    setState({ state: 'downloading', percent, message: `Downloading Lea ${state.version}… ${percent}%` });
  });
  autoUpdater.on('update-downloaded', (info) => {
    setState({ state: 'ready', version: info.version, percent: 100, message: readyMessage(info.version) });
    log(`updater: ${info.version} downloaded`);
  });
  autoUpdater.on('error', (error) => {
    const message = terse(error);
    log(`updater: ${message}`);
    if (downloading) setState({ state: 'error', message });
    else if (state.state === 'checking') setState({ state: 'idle', message });
  });

  async function check({ interactive = false } = {}) {
    try {
      await signature;
      await autoUpdater.checkForUpdates();
      if (!interactive) return;
      if (['available', 'downloading', 'ready'].includes(state.state)) await offer({ state, install });
      else await dialog.showMessageBox({ type: 'info', title: 'Updates', message: `Lea ${app.getVersion()} is the latest version.` });
    } catch (error) {
      log(`updater: check failed: ${terse(error)}`);
      if (interactive) {
        await dialog.showMessageBox({ type: 'warning', title: 'Updates', message: 'Could not check for updates.', detail: terse(error) });
      }
    }
  }

  async function download() {
    downloading = true;
    setState({ state: 'downloading', percent: 0, message: `Downloading Lea ${state.version}…` });
    try {
      await autoUpdater.downloadUpdate();
    } catch (error) {
      setState({ state: 'error', message: terse(error) });
    } finally {
      downloading = false;
    }
  }

  // What the renderer's button does, for whatever state it is in.
  async function install() {
    switch (state.state) {
      case 'ready':
        log('updater: restarting to install');
        autoUpdater.quitAndInstall();
        return;
      case 'available':
        if (state.downloadOnly) await shell.openExternal(state.releaseUrl);
        else await download();
        return;
      case 'error':
        if (state.downloadOnly) { await shell.openExternal(state.releaseUrl); return; }
        await check();
        if (state.state === 'available') await download();
        return;
      default:
        return;
    }
  }

  setTimeout(() => check(), FIRST_CHECK_DELAY_MS);
  setInterval(() => check(), RECHECK_INTERVAL_MS).unref();
  return { enabled: true, getState, check, install };
}

// Dev builds have no feed. LEA_DESKTOP_FAKE_UPDATE=<version> pretends one is
// available, download-only (what an unsigned build sees); <version>:inapp
// pretends the in-app flow and simulates the download when the button is
// clicked, so every renderer state can be exercised without a release.
function fakeUpdater({ log, getState, setState }) {
  const [version, mode] = String(process.env.LEA_DESKTOP_FAKE_UPDATE || '').split(':');
  const downloadOnly = mode !== 'inapp';
  if (version) {
    setTimeout(() => {
      setState({ state: 'available', version, downloadOnly, message: availableMessage(version, downloadOnly) });
      log(`updater: fake ${version} available (${downloadOnly ? 'download only' : 'in-app'})`);
    }, 1500);
  }

  async function install() {
    const state = getState();
    if (state.state === 'available' && state.downloadOnly) {
      await shell.openExternal(state.releaseUrl);
    } else if (state.state === 'available' || state.state === 'error') {
      for (let percent = 0; percent <= 100; percent += 20) {
        setState({ state: 'downloading', percent, message: `Downloading Lea ${version}… ${percent}%` });
        await new Promise((resolve) => setTimeout(resolve, 400));
      }
      setState({ state: 'ready', percent: 100, message: readyMessage(version) });
      log('updater: fake download complete');
    } else if (state.state === 'ready') {
      log('updater: fake restart to install');
      app.relaunch();
      app.quit();
    }
  }

  async function check({ interactive = false } = {}) {
    if (!interactive) return;
    if (!version) {
      await dialog.showMessageBox({ type: 'info', title: 'Updates', message: 'Updates are only available in a packaged build.' });
      return;
    }
    await offer({ state: getState(), install });
  }

  return { enabled: Boolean(version), getState, check, install };
}

// Check for Updates… when an update is known: the same choice as the button.
async function offer({ state, install }) {
  const ready = state.state === 'ready';
  const busy = state.state === 'downloading';
  const { response } = await dialog.showMessageBox({
    type: 'info',
    title: 'Update available',
    message: `Lea ${state.version} is available.`,
    detail: state.downloadOnly
      ? 'This build cannot update in place: download the new version and drag it to Applications to replace this one.'
      : ready ? 'It is downloaded. Restart to install it.'
      : busy ? `Downloading… ${state.percent}%. You will be asked to restart when it is ready.`
      : 'It downloads in the background; you will be asked to restart when it is ready.',
    buttons: [state.downloadOnly ? 'Download' : ready ? 'Restart now' : busy ? 'OK' : 'Download', 'Later'],
    defaultId: 0,
    cancelId: 1,
  });
  if (response === 0 && !busy) await install();
}

const terse = (value) => String(value?.message ?? value).split('\n')[0].slice(0, 200);

const availableMessage = (version, downloadOnly) => (downloadOnly
  ? `Lea ${version} is available. This build cannot update in place: download the new version and drag it to Applications.`
  : `Lea ${version} is available.`);

const readyMessage = (version) => `Lea ${version} is downloaded. Restart to install it.`;

// The DMG for this architecture from the feed's file list, when it lists one.
function directDownloadUrl(info) {
  const wanted = process.arch === 'arm64' ? 'arm64' : 'x64';
  const files = Array.isArray(info?.files) ? info.files : [];
  const dmg = files.find((f) => /\.dmg$/i.test(f.url) && f.url.includes(wanted)) ?? files.find((f) => /\.dmg$/i.test(f.url));
  if (!dmg) return null;
  return /^https?:/i.test(dmg.url) ? dmg.url : `${FEED_URL}/${dmg.url}`;
}

// macOS: Squirrel only installs an update whose signature matches the running
// app's designated requirement. An unsigned or ad-hoc-signed bundle fails that
// check, so the shell falls back to a plain download.
async function isTrustworthySignature() {
  if (process.platform !== 'darwin') return true;
  const bundle = path.resolve(process.execPath, '..', '..', '..');
  try {
    const { stderr } = await execFileAsync('codesign', ['-dv', '--verbose=2', bundle]);
    const details = String(stderr);
    if (/Signature=adhoc/.test(details)) return false;
    if (/TeamIdentifier=not set/.test(details)) return false;
    return /TeamIdentifier=\S+/.test(details);
  } catch {
    return false; // "code object is not signed at all", or no codesign
  }
}
