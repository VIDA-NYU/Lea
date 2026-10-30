import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react';
import { getProjectFile, getProjectTree, putProjectFile, type TreeEntry } from './api';
import { flattenFiles, isLeanPath, scanDeclarations } from './filesTree.js';

/**
 * Shared, cached project data for the files package (rail, breadcrumbs, ⌘P).
 *
 * One module-level cache per project so the three components fetch the tree
 * once and share it; `.lean` contents are fetched lazily (for the sorry scan
 * and the declaration index) and keyed by path + size so a file the agent
 * rewrote is refetched after the next tree refresh. Nothing here touches the
 * stores; components combine this with useProofSession's code steps.
 */

export type FlatFile = { path: string; name: string; dir: string; size?: number };

export interface TreeState {
  tree: TreeEntry[] | null;
  error: string | null;
  loading: boolean;
  /** Bumps on every successful load, so dependants can refetch contents. */
  version: number;
}

const EMPTY_TREE: TreeState = { tree: null, error: null, loading: false, version: 0 };

const trees = new Map<string, TreeState>();
const treeInflight = new Map<string, Promise<void>>();

type ContentEntry = { size?: number; content: string | null };
const contents = new Map<string, Map<string, ContentEntry>>();
const contentInflight = new Set<string>();

const listeners = new Set<() => void>();
let version = 0;
let emitTimer: ReturnType<typeof setTimeout> | null = null;

function emit() {
  version += 1;
  listeners.forEach((listener) => listener());
}

/** Coalesce bursts (a batch of file fetches) into one re-render. */
function emitSoon() {
  if (emitTimer) return;
  emitTimer = setTimeout(() => {
    emitTimer = null;
    emit();
  }, 40);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getVersion() {
  return version;
}

function message(err: unknown) {
  return err instanceof Error ? err.message : String(err);
}

function patchTree(projectId: string, patch: Partial<TreeState>) {
  trees.set(projectId, { ...(trees.get(projectId) ?? EMPTY_TREE), ...patch });
  emit();
}

// ── Tree ──────────────────────────────────────────────────────────────────────

/** Load (or reload with `force`) a project's tree into the cache. */
export function loadProjectTree(projectId: string, force = false): Promise<void> {
  const inflight = treeInflight.get(projectId);
  if (inflight && !force) return inflight;
  if (!force && trees.get(projectId)?.tree) return Promise.resolve();
  patchTree(projectId, { loading: true });
  const promise = getProjectTree(projectId)
    .then((tree) => {
      patchTree(projectId, { tree, error: null, loading: false, version: (trees.get(projectId)?.version ?? 0) + 1 });
    })
    .catch((err) => {
      patchTree(projectId, { error: message(err), loading: false });
    })
    .finally(() => {
      if (treeInflight.get(projectId) === promise) treeInflight.delete(projectId);
    });
  treeInflight.set(projectId, promise);
  return promise;
}

export function getCachedTree(projectId: string): TreeEntry[] | null {
  return trees.get(projectId)?.tree ?? null;
}

/** The project's tree, loaded on first use and shared across components. */
export function useProjectTree(projectId: string | null): TreeState & { refresh: () => void } {
  useSyncExternalStore(subscribe, getVersion);
  const state = projectId ? trees.get(projectId) ?? EMPTY_TREE : EMPTY_TREE;
  useEffect(() => {
    if (projectId) void loadProjectTree(projectId);
  }, [projectId]);
  const refresh = useCallback(() => {
    if (projectId) void loadProjectTree(projectId, true);
  }, [projectId]);
  return { ...state, refresh };
}

/** Every file of the tree, memoised on the tree object. */
export function useFlatFiles(tree: TreeEntry[] | null): FlatFile[] {
  return useMemo(() => (tree ? (flattenFiles(tree) as FlatFile[]) : []), [tree]);
}

// ── File contents ─────────────────────────────────────────────────────────────

const CONTENT_CONCURRENCY = 4;

function contentKey(projectId: string, path: string) {
  return `${projectId}\n${path}`;
}

function storeContent(projectId: string, path: string, entry: ContentEntry) {
  let map = contents.get(projectId);
  if (!map) {
    map = new Map();
    contents.set(projectId, map);
  }
  map.set(path, entry);
  emitSoon();
}

async function fetchContents(projectId: string, files: FlatFile[]) {
  const queue = files.filter((f) => !contentInflight.has(contentKey(projectId, f.path)));
  queue.forEach((f) => contentInflight.add(contentKey(projectId, f.path)));
  const workers = Array.from({ length: Math.min(CONTENT_CONCURRENCY, queue.length) }, async () => {
    while (queue.length) {
      const file = queue.shift()!;
      try {
        const result = await getProjectFile(projectId, file.path);
        storeContent(projectId, file.path, { size: file.size, content: result.binary ? null : result.content });
      } catch {
        storeContent(projectId, file.path, { size: file.size, content: null });
      } finally {
        contentInflight.delete(contentKey(projectId, file.path));
      }
    }
  });
  await Promise.all(workers);
}

/**
 * The text of the project's `.lean` files (up to `limit`), fetched lazily and
 * cached by path + size. Returns only the files already loaded; re-renders as
 * more arrive. Pass `enabled: false` to defer the fetch (the palette while
 * closed).
 */
export function useLeanContents(
  projectId: string | null,
  files: FlatFile[],
  options: { enabled?: boolean; limit?: number } = {},
): Map<string, string> {
  const { enabled = true, limit = 300 } = options;
  const snapshot = useSyncExternalStore(subscribe, getVersion);
  useEffect(() => {
    if (!projectId || !enabled) return;
    const wanted = files.filter((f) => isLeanPath(f.path)).slice(0, limit);
    const cache = contents.get(projectId);
    const missing = wanted.filter((f) => {
      const entry = cache?.get(f.path);
      return !entry || entry.size !== f.size;
    });
    if (missing.length) void fetchContents(projectId, missing);
  }, [projectId, files, enabled, limit]);
  return useMemo(() => {
    const out = new Map<string, string>();
    if (!projectId) return out;
    for (const [path, entry] of contents.get(projectId) ?? []) {
      if (entry.content != null) out.set(path, entry.content);
    }
    return out;
    // `snapshot` is the cache version: the map is rebuilt when any content lands.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, snapshot]);
}

// ── Declarations ──────────────────────────────────────────────────────────────

export interface DeclarationHit {
  kind: string;
  name: string;
  qualified: string;
  line: number;
  path: string;
}

type DeclCache = Map<string, { content: string; decls: DeclarationHit[] }>;
const declCaches = new Map<string, DeclCache>();

/**
 * Named declarations across the loaded `.lean` files, from a regex scan cached
 * per file content. The LSP's document symbols would be exact; this is the
 * dependency-free fallback the palette ships with.
 */
export function useDeclarations(projectId: string | null, leanContents: Map<string, string>): DeclarationHit[] {
  return useMemo(() => {
    if (!projectId) return [];
    let cache = declCaches.get(projectId);
    if (!cache) {
      cache = new Map();
      declCaches.set(projectId, cache);
    }
    const out: DeclarationHit[] = [];
    for (const [path, content] of leanContents) {
      let entry = cache.get(path);
      if (!entry || entry.content !== content) {
        entry = { content, decls: scanDeclarations(content).map((d) => ({ ...d, path })) };
        cache.set(path, entry);
      }
      out.push(...entry.decls);
    }
    return out;
  }, [projectId, leanContents]);
}

// ── Other projects (⌘P "Other projects" section) ──────────────────────────────

/** Flat file lists for other projects, loaded into the shared cache on demand. */
export function useOtherProjectFiles(
  projectIds: string[],
  enabled: boolean,
): { projectId: string; files: FlatFile[] }[] {
  const snapshot = useSyncExternalStore(subscribe, getVersion);
  useEffect(() => {
    if (!enabled) return;
    projectIds.forEach((id) => void loadProjectTree(id));
  }, [projectIds, enabled]);
  return useMemo(
    () =>
      projectIds
        .map((projectId) => ({ projectId, files: flattenFiles(trees.get(projectId)?.tree ?? []) as FlatFile[] }))
        .filter((entry) => entry.files.length > 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [projectIds, snapshot],
  );
}

// ── Mutations ─────────────────────────────────────────────────────────────────

/** Create an empty file at `path` (PUT with empty content) and refresh the tree. */
export async function createProjectFile(projectId: string, path: string): Promise<void> {
  await putProjectFile(projectId, path, '');
  await loadProjectTree(projectId, true);
}
