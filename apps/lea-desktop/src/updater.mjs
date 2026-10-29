// Auto-update via electron-updater. The feed is the rolling `desktop-latest`
// GitHub release (see electron-builder.yml `publish` and the desktop-release
// workflow), kept separate from the repository's other releases.
//
// The state is broadcast to the renderer (preload `onUpdate`), which shows an
// "Update to x.y.z" button in the sidebar; the native menu's Check for Updates…
// uses the same machinery. Two modes:
//
//   in-app        the update is downloaded here and installed on restart.
//   download-only macOS only applies an update to an app whose signature
//                 matches the running one, so an unsigned or ad-hoc-signed
//                 build cannot install in place: the button opens the DMG for
//                 this architecture in the browser instead.
import { app, dialog, shell } from 'electron';
import electronUpdater from 'electron-updater';
import { execFile } from 'node:child_process';
import path from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const { autoUpdater } = electronUpdater;

const FEED_URL = 'https://github.com/VIDA-NYU/Lea/releases/download/desktop-latest';
const RELEASE_PAGE = 'https://github.com/VIDA-NYU/Lea/releases/tag/desktop-latest';
const RECHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

export function setupUpdater({ log, broadcast }) {
  let state = { state: 'idle', version: null, percent: 0, downloadOnly: false, releaseUrl: RELEASE_PAGE, message: '' };
  const setState = (patch) => {
    state = { ...state, ...patch };
    broadcast?.(state);
  };
  const terse = (value) => String(value?.message ?? value).split('\n')[0].slice(0, 200);

  // Dev builds have no feed. LEA_DESKTOP_FAKE_UPDATE=<version> pretends one is
  // available (download-only) so the renderer side can be exercised.
  if (!app.isPackaged) {
    const fake = process.env.LEA_DESKTOP_FAKE_UPDATE;
    if (fake) {
      setTimeout(() => setState({ state: 'available', version: fake, downloadOnly: true, message: `Lea ${fake} is available.` }), 1500);
    }
    return {
      enabled: Boolean(fake),
      getState: () => state,
      check: async ({ interactive } = {}) => {
        if (interactive && !fake) {
          await dialog.showMessageBox({ type: 'info', title: 'Updates', message: 'Updates are only available in a packaged build.' });
        }
      },
      install: async () => { if (fake) await shell.openExternal(RELEASE_PAGE); },
    };
  }

  autoUpdater.logger = {
    info: (m) => log(`updater: ${terse(m)}`),
    warn: (m) => log(`updater: ${terse(m)}`),
    error: () => {}, // reported through the state + check() below
    debug: () => {},
  };
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;

  let inApp = null; // resolved lazily: can this build install updates in place?
  const canInstallInApp = async () => {
    if (inApp === null) inApp = await isTrustworthySignature();
    return inApp;
  };

  autoUpdater.on('checking-for-update', () => setState({ state: 'checking', message: 'Checking for updates…' }));
  autoUpdater.on('update-not-available', (info) => setState({ state: 'uptodate', version: info?.version ?? app.getVersion(), message: `Lea ${app.getVersion()} is the latest version.` }));
  autoUpdater.on('update-available', async (info) => {
    const downloadOnly = !(await canInstallInApp());
    setState({
      state: 'available',
      version: info.version,
      percent: 0,
      downloadOnly,
      releaseUrl: directDownloadUrl(info) ?? RELEASE_PAGE,
      message: downloadOnly
        ? `Lea ${info.version} is available. This build cannot update in place: download the new version and drag it to Applications.`
        : `Lea ${info.version} is available.`,
    });
    log(`updater: ${info.version} available (${downloadOnly ? 'download only' : 'in-app'})`);
    if (!downloadOnly) autoUpdater.downloadUpdate().catch((error) => setState({ state: 'error', message: terse(error) }));
  });
  autoUpdater.on('download-progress', (progress) => setState({ state: 'downloading', percent: Math.round(progress.percent) }));
  autoUpdater.on('update-downloaded', (info) => {
    setState({ state: 'ready', version: info.version, percent: 100, message: `Lea ${info.version} is downloaded. Restart to install it.` });
    log(`updater: ${info.version} downloaded`);
  });
  autoUpdater.on('error', (error) => {
    const message = terse(error);
    log(`updater: ${message}`);
    setState({ state: 'error', message });
  });

  async function check({ interactive = false } = {}) {
    try {
      const result = await autoUpdater.checkForUpdates();
      const latest = result?.updateInfo?.version;
      if (!interactive) return;
      if (latest && latest === app.getVersion()) {
        await dialog.showMessageBox({ type: 'info', title: 'Updates', message: `Lea ${latest} is the latest version.` });
      } else if (state.state === 'available' || state.state === 'downloading' || state.state === 'ready') {
        const downloadOnly = state.downloadOnly;
        const { response } = await dialog.showMessageBox({
          type: 'info',
          title: 'Update available',
          message: `Lea ${state.version} is available.`,
          detail: downloadOnly
            ? 'Download the new version and drag it to Applications to replace this one.'
            : state.state === 'ready' ? 'Restart to install it.' : 'It is downloading in the background; you will be asked to restart when it is ready.',
          buttons: [downloadOnly ? 'Download' : state.state === 'ready' ? 'Restart now' : 'OK', 'Later'],
          defaultId: 0,
          cancelId: 1,
        });
        if (response === 0 && (downloadOnly || state.state === 'ready')) await install();
      }
    } catch (error) {
      log(`updater: check failed: ${terse(error)}`);
      if (interactive) {
        await dialog.showMessageBox({ type: 'warning', title: 'Updates', message: 'Could not check for updates.', detail: terse(error) });
      }
    }
  }

  // What the renderer's button does, for whatever state it is in.
  async function install() {
    if (state.state === 'ready') {
      log('updater: restarting to install');
      autoUpdater.quitAndInstall();
      return;
    }
    if (state.state === 'available' || state.state === 'error') {
      if (state.downloadOnly) {
        await shell.openExternal(state.releaseUrl);
        return;
      }
      await autoUpdater.downloadUpdate().catch((error) => setState({ state: 'error', message: terse(error) }));
    }
  }

  setTimeout(() => check(), 15_000);
  setInterval(() => check(), RECHECK_INTERVAL_MS).unref();
  return { enabled: true, getState: () => state, check, install };
}

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
