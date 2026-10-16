/** @param {{catalog?: Array<Record<string, any>>, featured?: Array<Record<string, any>>, value?: string, query?: string, maxResults?: number}} options */
export function buildModelRows({ catalog = [], featured = [], value = '', query = '', maxResults = 60 } = {}) {
  const all = new Map();
  for (const model of [...catalog, ...featured]) {
    if (model?.value) all.set(model.value, { ...all.get(model.value), ...model,
      provider: model.provider || model.family || '' });
  }
  if (value && !all.has(value)) all.set(value, { value, label: value, provider: '' });
  const trimmed = query.trim();
  if (!trimmed) return [...new Set([value, ...featured.map(m => m.value)])]
    .filter(v => all.has(v)).slice(0, maxResults).map(v => all.get(v));
  const q = trimmed.toLowerCase();
  const matches = [...all.values()].filter(m => [m.value, m.label, m.provider].some(v => String(v || '').toLowerCase().includes(q)))
    .sort((a, b) => Number(b.value.toLowerCase() === q) - Number(a.value.toLowerCase() === q))
    .slice(0, maxResults);
  return all.has(trimmed) ? matches : [{ value: trimmed, label: trimmed, provider: '', custom: true }, ...matches];
}

export function modelStatus(model) {
  if (model.availability === 'retired') return 'Retired';
  if (model.availability === 'unavailable') return 'Not listed for this key';
  if (model.compatibility === 'unsupported') return 'Unsupported for proof runs';
  if (model.compatibility === 'unverified') return 'Compatibility unverified';
  if (model.suggestion_reason === 'new') return 'Newly discovered';
  return model.preview ? 'Preview' : '';
}

export function catalogStatus(payload) {
  if (!payload) return 'Model catalog has not loaded. You can enter a model ID.';
  const warnings = (payload.warnings || []).map(w => w.message).filter(Boolean);
  const updated = payload.updated_at ? `Last updated ${new Date(payload.updated_at).toLocaleString()}.` : 'Using bundled model data.';
  return [payload.refreshing ? 'Refreshing models…' : updated, ...warnings].join(' ');
}
