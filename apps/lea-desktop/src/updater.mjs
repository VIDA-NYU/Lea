// Auto-update via electron-updater. The feed is the rolling `desktop-latest`
// GitHub release (see electron-builder.yml `publish` and the desktop-release
// workflow), kept separate from the repository's other releases. Only active in
// packaged builds. macOS refuses to apply updates to an unsigned app, so until
// builds are signed this logs the failure once and stays quiet.
import { app, dialog } from 'electron';
import electronUpdater from 'electron-updater';

const { autoUpdater } = electronUpdater;

export function setupUpdater({ log }) {
  if (!app.isPackaged) {
    return {
      enabled: false,
      check: async ({ interactive } = {}) => {
        if (interactive) {
          await dialog.showMessageBox({ type: 'info', title: 'Updates', message: 'Updates are only available in a packaged build.' });
        }
      },
    };
  }

  const terse = (value) => String(value?.message ?? value).split('\n')[0].slice(0, 200);
  autoUpdater.logger = {
    info: (m) => log(`updater: ${terse(m)}`),
    warn: (m) => log(`updater: ${terse(m)}`),
    error: () => {}, // reported once by check() below
    debug: () => {},
  };
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-downloaded', async (info) => {
    const { response } = await dialog.showMessageBox({
      type: 'info',
      title: 'Update ready',
      message: `Lea ${info.version} has been downloaded.`,
      detail: 'Restart now to install it, or it will be installed the next time you quit.',
      buttons: ['Restart now', 'Later'],
      defaultId: 0,
      cancelId: 1,
    });
    if (response === 0) autoUpdater.quitAndInstall();
  });

  async function check({ interactive = false } = {}) {
    try {
      const result = await autoUpdater.checkForUpdates();
      const latest = result?.updateInfo?.version;
      if (interactive && latest && latest === app.getVersion()) {
        await dialog.showMessageBox({ type: 'info', title: 'Updates', message: `Lea ${latest} is the latest version.` });
      }
    } catch (error) {
      log(`updater: check failed: ${terse(error)}`);
      if (interactive) {
        await dialog.showMessageBox({ type: 'warning', title: 'Updates', message: 'Could not check for updates.', detail: terse(error) });
      }
    }
  }

  setTimeout(() => check(), 15_000);
  return { enabled: true, check };
}
