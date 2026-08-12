import { create } from 'zustand';
import type { SubagentProfile, SubagentSettings } from '../lib/api';
import { listSubagentProfiles, updateSubagentProfile } from '../lib/api';

/**
 * Sub-agents store (D6) — the built-in role profiles for the Sub-agents page.
 *
 * Mirrors `stores/projects.ts`/`factories.ts`: a list + a selection + async actions
 * that call the adapter then update the list in place. Each role is keyed by its stable
 * `name` (there's a fixed set of built-ins, no create/delete here).
 */
interface SubagentsState {
  profiles: SubagentProfile[];
  selectedName?: string;
  setSelectedName: (name?: string) => void;
  refreshProfiles: () => Promise<SubagentProfile[]>;
  saveProfile: (name: string, settings: Partial<SubagentSettings>) => Promise<SubagentProfile>;
}

export const useSubagents = create<SubagentsState>((set) => ({
  profiles: [],
  selectedName: undefined,
  setSelectedName: (selectedName) => set({ selectedName }),

  refreshProfiles: async () => {
    const profiles = await listSubagentProfiles();
    set((s) => ({
      profiles,
      // keep the current selection if it still exists, else select the first role
      selectedName:
        s.selectedName && profiles.some((p) => p.name === s.selectedName)
          ? s.selectedName
          : profiles[0]?.name,
    }));
    return profiles;
  },

  saveProfile: async (name, settings) => {
    const saved = await updateSubagentProfile(name, settings);
    set((s) => ({ profiles: s.profiles.map((p) => (p.name === name ? saved : p)) }));
    return saved;
  },
}));
