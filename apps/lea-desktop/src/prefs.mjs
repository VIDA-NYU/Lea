// Small persistent preferences for the shell itself (not the backend's config):
// <userData>/preferences.json. Missing or unreadable means defaults.
import fs from 'node:fs';
import path from 'node:path';

const DEFAULTS = Object.freeze({
  // Show a system notification when a run finishes while the window is not
  // in front (see main.mjs `lea:notify`).
  notifications: true,
});

export function createPrefs(userData, { log = () => {} } = {}) {
  const file = path.join(userData, 'preferences.json');
  let values = { ...DEFAULTS };
  try {
    values = { ...DEFAULTS, ...JSON.parse(fs.readFileSync(file, 'utf8')) };
  } catch {
    // first run, or a file we cannot parse: defaults
  }

  function save() {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, `${JSON.stringify(values, null, 2)}\n`);
    } catch (error) {
      log(`could not save preferences: ${error.message}`);
    }
  }

  return {
    get: (key) => values[key],
    set: (key, value) => {
      values = { ...values, [key]: value };
      save();
    },
    file,
  };
}
