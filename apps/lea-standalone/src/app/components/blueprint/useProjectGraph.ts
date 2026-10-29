import { useCallback, useEffect, useReducer } from 'react';
import { getProjectGraph, type ProjectGraph } from '../../lib/api';
import { useWorkbench } from '../../stores/workbench';

// One cached graph per project, shared by BlueprintView, BlueprintStrip and NodeChip
// so the strip and the chip do not each refetch what the view already holds. The
// cache refetches when the workbench `blueprintRefresh` counter bumps (a save, a
// started session) and, optionally, on a polling interval while something is live.

interface Entry {
  graph: ProjectGraph | null;
  error: string | null;
  inflight: Promise<void> | null;
  listeners: Set<() => void>;
}

const cache = new Map<string, Entry>();

function entryFor(projectId: string): Entry {
  let e = cache.get(projectId);
  if (!e) {
    e = { graph: null, error: null, inflight: null, listeners: new Set() };
    cache.set(projectId, e);
  }
  return e;
}

/** Refetch a project's graph; concurrent callers share one request. */
export function refetchProjectGraph(projectId: string): Promise<void> {
  const e = entryFor(projectId);
  if (e.inflight) return e.inflight;
  e.inflight = getProjectGraph(projectId)
    .then((graph) => {
      e.graph = graph;
      e.error = null;
    })
    .catch((err) => {
      e.error = err instanceof Error ? err.message : String(err);
    })
    .finally(() => {
      e.inflight = null;
      for (const l of e.listeners) l();
    });
  return e.inflight;
}

export function useProjectGraph(
  projectId: string | null | undefined,
  options: { pollMs?: number } = {},
): { graph: ProjectGraph | null; error: string | null; loading: boolean; refetch: () => Promise<void> } {
  const refresh = useWorkbench((s) => s.blueprintRefresh);
  const [, rerender] = useReducer((x: number) => x + 1, 0);
  const pollMs = options.pollMs ?? 0;

  useEffect(() => {
    if (!projectId) return;
    const e = entryFor(projectId);
    e.listeners.add(rerender);
    if (!e.graph && !e.inflight) void refetchProjectGraph(projectId);
    return () => {
      e.listeners.delete(rerender);
    };
  }, [projectId]);

  useEffect(() => {
    if (projectId && refresh > 0) void refetchProjectGraph(projectId);
  }, [projectId, refresh]);

  useEffect(() => {
    if (!projectId || pollMs <= 0) return;
    const id = window.setInterval(() => void refetchProjectGraph(projectId), pollMs);
    return () => window.clearInterval(id);
  }, [projectId, pollMs]);

  const refetch = useCallback(() => (projectId ? refetchProjectGraph(projectId) : Promise.resolve()), [projectId]);
  const e = projectId ? entryFor(projectId) : null;
  return {
    graph: e?.graph ?? null,
    error: e?.error ?? null,
    loading: !!e && !e.graph && !e.error,
    refetch,
  };
}
