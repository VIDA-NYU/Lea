import { create } from 'zustand';
import {
  listSkills,
  createSkill,
  importSkill,
  updateSkill,
  setSkillAssignment,
  deleteSkill,
  listMcpServers,
  createMcpServer,
  updateMcpServer,
  setMcpServerAssignment,
  deleteMcpServer,
  type Skill,
  type AuthoringFieldValues,
  type ImportedExtras,
  type McpServer,
  type McpServerInput,
} from '../lib/api';

/**
 * The factories store (v2.1.1 F10) — the Library's catalogs. Today it holds the
 * skills list + selection + CRUD/assignment/import actions, and (v2.5 E0) the same
 * for MCP servers — the "deferred Slice 9" this comment used to point at. Mirrors
 * `stores/projects.ts` so each Factory page reads its list + selection from here,
 * no prop-drilling.
 */
interface FactoriesState {
  skills: Skill[];
  selectedSkillId?: string;
  setSelectedSkillId: (id?: string) => void;

  refreshSkills: () => Promise<Skill[]>;
  addSkill: (input: { name: string; body?: string; authoring?: AuthoringFieldValues; is_global?: boolean; project_ids?: string[] }) => Promise<Skill>;
  addSkillFromGitHub: (input: { url: string; is_global?: boolean; project_ids?: string[] }) => Promise<Skill & ImportedExtras>;
  editSkill: (skillId: string, update: { name?: string; body?: string; authoring?: AuthoringFieldValues }) => Promise<Skill>;
  assignSkill: (skillId: string, assignment: { is_global: boolean; project_ids: string[] }) => Promise<Skill>;
  removeSkill: (skillId: string) => Promise<void>;

  // MCP servers (E0) — deliberately the same shape as the skills actions above.
  mcpServers: McpServer[];
  selectedMcpServerId?: string;
  setSelectedMcpServerId: (id?: string) => void;

  refreshMcpServers: () => Promise<McpServer[]>;
  addMcpServer: (input: McpServerInput) => Promise<McpServer>;
  editMcpServer: (serverId: string, update: Partial<McpServerInput>) => Promise<McpServer>;
  assignMcpServer: (
    serverId: string,
    assignment: { is_global: boolean; project_ids: string[] },
  ) => Promise<McpServer>;
  removeMcpServer: (serverId: string) => Promise<void>;

  // Which per-session picker `/skills` or `/mcp` opened (E0e); null = closed. Lives here
  // rather than in App so the slash-command handler can open it without prop-drilling.
  skillsMcpPicker: 'skills' | 'mcp' | null;
  setSkillsMcpPicker: (kind: 'skills' | 'mcp' | null) => void;
}

// Keep the local list consistent after a mutation, and re-select the touched skill.
function upsert(skills: Skill[], skill: Skill): Skill[] {
  const rest = skills.filter((s) => s.id !== skill.id);
  return [skill, ...rest];
}

function upsertServer(servers: McpServer[], server: McpServer): McpServer[] {
  const rest = servers.filter((s) => s.id !== server.id);
  return [server, ...rest];
}

export const useFactories = create<FactoriesState>((set, get) => ({
  skills: [],
  selectedSkillId: undefined,
  setSelectedSkillId: (selectedSkillId) => set({ selectedSkillId }),

  refreshSkills: async () => {
    const loaded = await listSkills();
    set({ skills: loaded });
    return loaded;
  },

  addSkill: async (input) => {
    const skill = await createSkill(input);
    set((s) => ({ skills: upsert(s.skills, skill), selectedSkillId: skill.id }));
    return skill;
  },

  addSkillFromGitHub: async (input) => {
    const skill = await importSkill(input);
    set((s) => ({ skills: upsert(s.skills, skill), selectedSkillId: skill.id }));
    return skill;
  },

  editSkill: async (skillId, update) => {
    const skill = await updateSkill(skillId, update);
    set((s) => ({ skills: upsert(s.skills, skill) }));
    return skill;
  },

  assignSkill: async (skillId, assignment) => {
    const skill = await setSkillAssignment(skillId, assignment);
    set((s) => ({ skills: upsert(s.skills, skill) }));
    return skill;
  },

  removeSkill: async (skillId) => {
    await deleteSkill(skillId);
    set((s) => ({
      skills: s.skills.filter((skill) => skill.id !== skillId),
      selectedSkillId: get().selectedSkillId === skillId ? undefined : get().selectedSkillId,
    }));
  },

  skillsMcpPicker: null,
  setSkillsMcpPicker: (skillsMcpPicker) => set({ skillsMcpPicker }),

  mcpServers: [],
  selectedMcpServerId: undefined,
  setSelectedMcpServerId: (selectedMcpServerId) => set({ selectedMcpServerId }),

  refreshMcpServers: async () => {
    const loaded = await listMcpServers();
    set({ mcpServers: loaded });
    return loaded;
  },

  addMcpServer: async (input) => {
    const server = await createMcpServer(input);
    set((s) => ({ mcpServers: upsertServer(s.mcpServers, server), selectedMcpServerId: server.id }));
    return server;
  },

  editMcpServer: async (serverId, update) => {
    const server = await updateMcpServer(serverId, update);
    set((s) => ({ mcpServers: upsertServer(s.mcpServers, server) }));
    return server;
  },

  assignMcpServer: async (serverId, assignment) => {
    const server = await setMcpServerAssignment(serverId, assignment);
    set((s) => ({ mcpServers: upsertServer(s.mcpServers, server) }));
    return server;
  },

  removeMcpServer: async (serverId) => {
    await deleteMcpServer(serverId);
    set((s) => ({
      mcpServers: s.mcpServers.filter((server) => server.id !== serverId),
      selectedMcpServerId:
        get().selectedMcpServerId === serverId ? undefined : get().selectedMcpServerId,
    }));
  },
}));
