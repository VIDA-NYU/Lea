// Pure helpers for the desktop update button
// (components/shell/DesktopUpdateButton.tsx), unit-tested in desktopUpdate.test.mjs.

/** localStorage key holding the version the user chose to skip. */
export const SKIPPED_VERSION_KEY = 'lea.desktopUpdate.skippedVersion';

/**
 * @typedef {{ state: string, version?: string | null, percent?: number, downloadOnly?: boolean }} UpdateLike
 */

/**
 * What the footer button says for an updater state, or null when there is
 * nothing to show (idle, checking, up to date, or no shell at all).
 * @param {UpdateLike | null | undefined} update
 */
export function updateButtonLabel(update) {
  if (!update) return null;
  switch (update.state) {
    case 'available':
      return update.downloadOnly ? `Download Lea ${update.version}` : `Update to ${update.version}`;
    case 'downloading':
      return `Downloading… ${clampPercent(update.percent)}%`;
    case 'ready':
      return 'Restart to update';
    case 'error':
      return 'Update failed · retry';
    default:
      return null;
  }
}

/**
 * "Skip this version" hides the button while the available version is the one
 * skipped. A download in progress, a downloaded update and an error are never
 * hidden: the user started those.
 * @param {UpdateLike | null | undefined} update
 * @param {string | null | undefined} skippedVersion
 */
export function isUpdateSkipped(update, skippedVersion) {
  if (!update || !skippedVersion) return false;
  return update.state === 'available' && update.version === skippedVersion;
}

/** @param {{ getItem(key: string): string | null } | null | undefined} storage */
export function readSkippedVersion(storage) {
  try {
    return storage?.getItem(SKIPPED_VERSION_KEY) || null;
  } catch {
    return null;
  }
}

/**
 * @param {{ setItem(key: string, value: string): void, removeItem(key: string): void } | null | undefined} storage
 * @param {string | null | undefined} version  null clears the skip
 */
export function writeSkippedVersion(storage, version) {
  try {
    if (version) storage?.setItem(SKIPPED_VERSION_KEY, version);
    else storage?.removeItem(SKIPPED_VERSION_KEY);
  } catch {
    // private mode or a blocked store: the button simply shows again next launch
  }
}

function clampPercent(value) {
  const n = Math.round(Number(value) || 0);
  return Math.max(0, Math.min(100, n));
}
