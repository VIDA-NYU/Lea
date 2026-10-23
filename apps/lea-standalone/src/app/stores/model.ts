import { create } from 'zustand';
import {
  fetchModelCatalog,
  fetchModelRequirements,
  getSettings,
  saveSettings,
  type ModelCatalogEntry,
  type ModelOption,
  type ModelCatalogPayload,
} from '../lib/api';
import { useProofSession } from './proofSession';

/**
 * The model store (v2.0.1 R4) — the active model + pickers + key-missing nudge.
 *
 * ChatThread reads everything here (model, catalog, featured, keyMissing) and
 * calls `changeModel` directly, so none of it is prop-drilled through App. App
 * just kicks off `syncFromSettings` + `loadCatalog` on startup and re-syncs when
 * the user returns from the Settings page.
 */
interface ModelState {
  model?: string;
  modelCatalog: ModelCatalogEntry[];
  modelFeatured: ModelOption[];
  catalogPayload?: ModelCatalogPayload;
  catalogError?: string;
  catalogLoading: boolean;
  // True when the active model's provider key is not configured (M19 nudge).
  keyMissing: boolean;

  // Whether the given model's provider key is configured (M19).
  checkKey: (model?: string) => Promise<void>;
  // Load the active model + featured list from settings, then check the key.
  syncFromSettings: () => Promise<void>;
  // Load the full model catalog from the API.
  loadCatalog: (refresh?: boolean) => Promise<void>;
  // Change the active model (M9): optimistic, persists to the same config the
  // Settings page writes; rolls back + surfaces an error banner on failure.
  changeModel: (next: string) => Promise<void>;
}

export const useModel = create<ModelState>((set, get) => ({
  model: undefined,
  modelCatalog: [],
  modelFeatured: [],
  catalogLoading: false,
  keyMissing: false,

  checkKey: async (model) => {
    if (!model) {
      set({ keyMissing: false });
      return;
    }
    try {
      const r = await fetchModelRequirements(model);
      set({ keyMissing: !r.satisfied });
    } catch {
      set({ keyMissing: false });
    }
  },

  syncFromSettings: async () => {
    try {
      const s = await getSettings();
      const m = typeof s.model === 'string' ? s.model : undefined;
      set({
        model: m,
        modelFeatured: Array.isArray(s.model_options) ? (s.model_options as ModelOption[]) : [],
      });
      await get().checkKey(m);
    } catch {
      /* ignore — settings may be unreadable; leave defaults */
    }
  },

  loadCatalog: async (refresh = false) => {
    if (get().catalogLoading) return;
    set({ catalogLoading: true });
    try {
      const payload = await fetchModelCatalog(refresh);
      const changed = !payload.revision || payload.revision !== get().catalogPayload?.revision;
      set({ catalogPayload: payload, catalogError: undefined,
        ...(changed ? { modelCatalog: payload.models } : {}),
        ...(changed && payload.featured ? { modelFeatured: payload.featured } : {}) });
    } catch (error) {
      set({ catalogError: 'Could not refresh models. Showing saved data; try Refresh models.' });
    } finally {
      set({ catalogLoading: false });
    }
  },

  changeModel: async (next) => {
    const previous = get().model;
    if (!next || next === previous) return;
    set({ model: next });
    get().checkKey(next);
    try {
      await saveSettings({ model: next });
    } catch (err) {
      set({ model: previous });
      useProofSession
        .getState()
        .setError(err instanceof Error ? err.message : 'Could not change the model.');
    }
  },
}));
