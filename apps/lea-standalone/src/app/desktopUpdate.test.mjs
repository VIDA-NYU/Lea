import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  SKIPPED_VERSION_KEY,
  isUpdateSkipped,
  readSkippedVersion,
  updateButtonLabel,
  writeSkippedVersion,
} from './lib/desktopUpdate.js';

test('updateButtonLabel follows the four visible states and hides the rest', () => {
  assert.equal(updateButtonLabel(null), null);
  assert.equal(updateButtonLabel({ state: 'idle' }), null);
  assert.equal(updateButtonLabel({ state: 'checking' }), null);
  assert.equal(updateButtonLabel({ state: 'uptodate', version: '0.1.0' }), null);
  assert.equal(updateButtonLabel({ state: 'available', version: '0.2.0', downloadOnly: false }), 'Update to 0.2.0');
  assert.equal(updateButtonLabel({ state: 'available', version: '0.2.0', downloadOnly: true }), 'Download Lea 0.2.0');
  assert.equal(updateButtonLabel({ state: 'downloading', version: '0.2.0', percent: 43 }), 'Downloading… 43%');
  assert.equal(updateButtonLabel({ state: 'downloading', percent: 142.4 }), 'Downloading… 100%');
  assert.equal(updateButtonLabel({ state: 'downloading' }), 'Downloading… 0%');
  assert.equal(updateButtonLabel({ state: 'ready', version: '0.2.0' }), 'Restart to update');
  assert.equal(updateButtonLabel({ state: 'error', message: 'boom' }), 'Update failed · retry');
});

test('isUpdateSkipped hides only the skipped version while it is merely available', () => {
  assert.equal(isUpdateSkipped({ state: 'available', version: '0.2.0' }, '0.2.0'), true);
  assert.equal(isUpdateSkipped({ state: 'available', version: '0.2.1' }, '0.2.0'), false);
  assert.equal(isUpdateSkipped({ state: 'downloading', version: '0.2.0' }, '0.2.0'), false);
  assert.equal(isUpdateSkipped({ state: 'ready', version: '0.2.0' }, '0.2.0'), false);
  assert.equal(isUpdateSkipped({ state: 'error', version: '0.2.0' }, '0.2.0'), false);
  assert.equal(isUpdateSkipped({ state: 'available', version: '0.2.0' }, null), false);
  assert.equal(isUpdateSkipped(null, '0.2.0'), false);
});

test('the skipped version round-trips through storage and survives a broken store', () => {
  const backing = new Map();
  const storage = {
    getItem: (key) => (backing.has(key) ? backing.get(key) : null),
    setItem: (key, value) => backing.set(key, value),
    removeItem: (key) => backing.delete(key),
  };
  assert.equal(readSkippedVersion(storage), null);
  writeSkippedVersion(storage, '0.2.0');
  assert.equal(backing.get(SKIPPED_VERSION_KEY), '0.2.0');
  assert.equal(readSkippedVersion(storage), '0.2.0');
  writeSkippedVersion(storage, null);
  assert.equal(readSkippedVersion(storage), null);

  const broken = {
    getItem: () => { throw new Error('blocked'); },
    setItem: () => { throw new Error('blocked'); },
    removeItem: () => { throw new Error('blocked'); },
  };
  assert.equal(readSkippedVersion(broken), null);
  assert.doesNotThrow(() => writeSkippedVersion(broken, '0.2.0'));
  assert.equal(readSkippedVersion(null), null);
  assert.doesNotThrow(() => writeSkippedVersion(undefined, '0.2.0'));
});
