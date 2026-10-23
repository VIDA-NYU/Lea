import { create } from 'zustand';
import {
  listProjects,
  getProject,
  createProject,
  deleteProject,
  type Project,
  type ProjectDetail,
  type SessionSummary,
} from '../lib/api';

/**
 * The projects store (v2.1 F1) — the project list, which one is open, and its
 * loaded detail. The Sidebar reads the list + selection; App opens the project
 * window when `selectedProjectId` is set. Mirrors the sessions store shape.
 */
interface ProjectsState {
  projects: Project[];
  setProjects: (projects: Project[]) => void;
  applySnapshot: (projects: Project[], sessions: SessionSummary[]) => void;
  // Which project window is open (undefined = none / loose-chat view).
  selectedProjectId?: string;
  setSelectedProjectId: (id?: string) => void;
  // The open project's loaded detail (meta + sessions).
  currentProject?: ProjectDetail;
  currentProjectDeleted: boolean;
  projectError?: string;
  invalidateNavigation: () => void;
  refreshCurrentProject: () => Promise<void>;

  refreshProjects: () => Promise<Project[]>;
  openProject: (projectId: string) => Promise<ProjectDetail | null>;
  closeProject: () => void;
  createAndOpen: (title: string, description?: string) => Promise<Project | null>;
  remove: (projectId: string) => Promise<void>;
}

let navigationGeneration = 0;
let listGeneration = 0;
let projectInvalidation = 0;
let refreshFlight: Promise<void> | undefined;
let refreshDirty = false;
export const useProjects = create<ProjectsState>((set, get) => ({
  projects: [],
  setProjects: (projects) => set({ projects }),
  applySnapshot: (projects, _sessions) => {
    projectInvalidation++;
    set(state => {
      const summary = projects.find(p => p.id === state.selectedProjectId);
      return { projects, ...(summary && state.currentProject?.id === summary.id
        ? { currentProject: { ...state.currentProject, ...summary } } : {}) };
    });
    if (get().currentProject) void get().refreshCurrentProject();
  },
  selectedProjectId: undefined,
  setSelectedProjectId: (selectedProjectId) => { navigationGeneration++; set({ selectedProjectId }); },
  currentProject: undefined,
  currentProjectDeleted: false,
  projectError: undefined,
  invalidateNavigation: () => { navigationGeneration++; },

  refreshCurrentProject: () => {
    refreshDirty = true;
    if (refreshFlight) return refreshFlight;
    refreshFlight = (async () => {
      while (refreshDirty) {
        refreshDirty = false;
        const id = get().selectedProjectId, generation = navigationGeneration;
        if (!id || !get().currentProject) continue;
        try {
          const detail = await getProject(id);
          if (generation === navigationGeneration && get().selectedProjectId === id) {
            set({ currentProject: detail, currentProjectDeleted: false, projectError: undefined });
          }
        } catch (error: any) {
          if (generation === navigationGeneration && get().selectedProjectId === id) {
            set({ currentProjectDeleted: error.status === 404, projectError: error.message });
          }
        }
      }
    })().finally(() => { refreshFlight = undefined; });
    return refreshFlight;
  },

  refreshProjects: async () => {
    const generation = ++listGeneration;
    const loaded = await listProjects();
    if (generation === listGeneration) set({ projects: loaded });
    return loaded;
  },

  openProject: async (projectId) => {
    const generation = ++navigationGeneration;
    set({ selectedProjectId: projectId, currentProject: undefined, currentProjectDeleted: false, projectError: undefined });
    try {
      let detail: ProjectDetail;
      let observedInvalidation: number;
      do {
        observedInvalidation = projectInvalidation;
        detail = await getProject(projectId);
        if (generation !== navigationGeneration) return null;
      } while (observedInvalidation !== projectInvalidation);
      set({ currentProject: detail });
      return detail;
    } catch (error) { if (generation !== navigationGeneration) return null; throw error; }
  },

  closeProject: () => { navigationGeneration++; set({ selectedProjectId: undefined, currentProject: undefined, currentProjectDeleted: false }); },

  createAndOpen: async (title, description) => {
    const generation = navigationGeneration;
    const project = await createProject(title, description);
    set((s) => ({ projects: [project, ...s.projects.filter(p => p.id !== project.id)] }));
    if (generation !== navigationGeneration) return null;
    return await get().openProject(project.id) ? project : null;
  },

  remove: async (projectId) => {
    await deleteProject(projectId);
    set((s) => ({ projects: s.projects.filter((p) => p.id !== projectId) }));
    if (get().selectedProjectId === projectId) get().closeProject();
  },
}));
