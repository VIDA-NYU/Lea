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

  refreshProjects: () => Promise<Project[]>;
  openProject: (projectId: string) => Promise<ProjectDetail>;
  closeProject: () => void;
  createAndOpen: (title: string, description?: string) => Promise<Project>;
  remove: (projectId: string) => Promise<void>;
}

export const useProjects = create<ProjectsState>((set, get) => ({
  projects: [],
  setProjects: (projects) => set({ projects }),
  applySnapshot: (projects, sessions) => set(state => {
    const summary = projects.find(p => p.id === state.selectedProjectId);
    return { projects, ...(summary && state.currentProject
      ? { currentProject: { ...state.currentProject, ...summary, sessions: sessions.filter(s => s.project_id === summary.id) } }
      : {}) };
  }),
  selectedProjectId: undefined,
  setSelectedProjectId: (selectedProjectId) => set({ selectedProjectId }),
  currentProject: undefined,

  refreshProjects: async () => {
    const loaded = await listProjects();
    set({ projects: loaded });
    return loaded;
  },

  openProject: async (projectId) => {
    const detail = await getProject(projectId);
    set({ currentProject: detail, selectedProjectId: projectId });
    return detail;
  },

  closeProject: () => set({ selectedProjectId: undefined, currentProject: undefined }),

  createAndOpen: async (title, description) => {
    const project = await createProject(title, description);
    set((s) => ({ projects: [project, ...s.projects] }));
    await get().openProject(project.id);
    return project;
  },

  remove: async (projectId) => {
    await deleteProject(projectId);
    set((s) => ({ projects: s.projects.filter((p) => p.id !== projectId) }));
    if (get().selectedProjectId === projectId) get().closeProject();
  },
}));
