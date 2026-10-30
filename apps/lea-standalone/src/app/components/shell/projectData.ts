import { create } from 'zustand';
import { getProjectGraph, getProjectTree, type ProjectGraph, type TreeEntry } from '../../lib/api';

/**
 * Per-project blueprint graphs and file trees for the shell (launch screen,
 * project home, sidebar). One cache so a project card, its sidebar row and its
 * home page share a single fetch; in-flight requests are de-duplicated and a
 * failed graph (no blueprint yet, or the endpoint unavailable) reads as empty.
 * Callers re-load when the session feed changes (`formalizationRefreshToken`).
 */
interface ProjectDataState {
  graphs: Record<string, ProjectGraph>;
  graphErrors: Record<string, string>;
  trees: Record<string, TreeEntry[]>;
  loadGraph: (projectId: string) => Promise<ProjectGraph>;
  loadGraphs: (projectIds: string[]) => void;
  loadTree: (projectId: string) => Promise<TreeEntry[]>;
  forget: (projectId: string) => void;
}

const EMPTY_GRAPH: ProjectGraph = { nodes: [], edges: [] };
const graphFlights = new Map<string, Promise<ProjectGraph>>();
const treeFlights = new Map<string, Promise<TreeEntry[]>>();

export const useProjectData = create<ProjectDataState>((set, get) => ({
  graphs: {},
  graphErrors: {},
  trees: {},

  loadGraph: (projectId) => {
    const inflight = graphFlights.get(projectId);
    if (inflight) return inflight;
    const flight = getProjectGraph(projectId)
      .then((graph) => {
        set((s) => {
          const graphErrors = { ...s.graphErrors };
          delete graphErrors[projectId];
          return { graphs: { ...s.graphs, [projectId]: graph }, graphErrors };
        });
        return graph;
      })
      .catch((err) => {
        set((s) => ({
          graphs: { ...s.graphs, [projectId]: s.graphs[projectId] ?? EMPTY_GRAPH },
          graphErrors: { ...s.graphErrors, [projectId]: err instanceof Error ? err.message : String(err) },
        }));
        return get().graphs[projectId] ?? EMPTY_GRAPH;
      })
      .finally(() => graphFlights.delete(projectId));
    graphFlights.set(projectId, flight);
    return flight;
  },

  loadGraphs: (projectIds) => {
    for (const id of projectIds) void get().loadGraph(id);
  },

  loadTree: (projectId) => {
    const inflight = treeFlights.get(projectId);
    if (inflight) return inflight;
    const flight = getProjectTree(projectId)
      .then((tree) => {
        set((s) => ({ trees: { ...s.trees, [projectId]: tree } }));
        return tree;
      })
      .catch(() => get().trees[projectId] ?? [])
      .finally(() => treeFlights.delete(projectId));
    treeFlights.set(projectId, flight);
    return flight;
  },

  forget: (projectId) =>
    set((s) => {
      const graphs = { ...s.graphs };
      const trees = { ...s.trees };
      const graphErrors = { ...s.graphErrors };
      delete graphs[projectId];
      delete trees[projectId];
      delete graphErrors[projectId];
      return { graphs, trees, graphErrors };
    }),
}));

/** The cached graph for a project (undefined until its first load resolves). */
export function useProjectGraph(projectId: string | undefined | null): ProjectGraph | undefined {
  return useProjectData((s) => (projectId ? s.graphs[projectId] : undefined));
}

/** The cached tree for a project (undefined until its first load resolves). */
export function useProjectTree(projectId: string | undefined | null): TreeEntry[] | undefined {
  return useProjectData((s) => (projectId ? s.trees[projectId] : undefined));
}
