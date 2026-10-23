const canonical = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const tabStorage = () => { try { return globalThis.sessionStorage; } catch { return undefined; } };

// Only non-secret UI intent belongs here. Credential intake retains its existing
// ticket transport. Accepted work and receipts remain authoritative in SQL.
export function createIntentRegistry(namespace, { storage = tabStorage(), makeId = () => crypto.randomUUID() } = {}) {
  const name = `lea:pending-intents:${namespace}`;
  const records = new Map(Object.entries(JSON.parse(storage?.getItem(name) || '{}')));
  const flights = new Map();
  const persist = () => {
    if (!storage) return;
    if (records.size) storage.setItem(name, JSON.stringify(Object.fromEntries(records)));
    else storage.removeItem(name);
  };
  const prepare = payload => {
    const identity = canonical(payload);
    if (!records.has(identity)) {
      records.set(identity, { identity, id: makeId(), payload: JSON.parse(identity) });
      try { persist(); } // Retain identity before sending any bytes.
      catch (error) { records.delete(identity); throw error; }
    }
    return records.get(identity);
  };
  const complete = attempt => {
    if (records.get(attempt.identity)?.id !== attempt.id) return;
    records.delete(attempt.identity);
    try { persist(); }
    catch (error) { records.set(attempt.identity, attempt); throw error; }
  };
  return { prepare, save: persist, complete,
    async run(payload, send) {
      const attempt = prepare(payload);
      if (flights.has(attempt.id)) return flights.get(attempt.id);
      const work = Promise.resolve().then(() => send({ ...attempt.payload, idempotency_key: attempt.id }))
        .then(result => { complete(attempt); return result; })
        .catch(error => {
          // A conflicting key must be reviewed, never silently replaced.
          if (error.status >= 400 && error.status < 500 && error.status !== 409) complete(attempt);
          throw error;
        }).finally(() => flights.delete(attempt.id));
      flights.set(attempt.id, work);
      return work;
    },
  };
}
