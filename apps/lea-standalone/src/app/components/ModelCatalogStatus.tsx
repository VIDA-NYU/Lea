import { useEffect } from 'react';
import { useModel } from '../stores/model';
import { catalogStatus } from '../lib/modelPickerRows.mjs';

export function useCatalogUpdates(active: boolean) {
  const refreshing = useModel(s => s.catalogPayload?.refreshing);
  useEffect(() => {
    if (!active) return;
    const reload = () => { void useModel.getState().loadCatalog(); };
    reload();
    window.addEventListener('focus', reload);
    const timer = window.setInterval(reload, refreshing ? 2000 : 5 * 60 * 1000);
    return () => { window.removeEventListener('focus', reload); window.clearInterval(timer); };
  }, [active, refreshing]);
}

export function ModelCatalogStatus() {
  const payload = useModel(s => s.catalogPayload);
  const error = useModel(s => s.catalogError);
  const loading = useModel(s => s.catalogLoading);
  return <div className="model-catalog-status">
    <span role="status">{error || catalogStatus(payload)}</span>
    <button type="button" disabled={loading || payload?.refreshing || payload?.updates_disabled}
      onClick={() => { void useModel.getState().loadCatalog(true); }}>Refresh models</button>
  </div>;
}
