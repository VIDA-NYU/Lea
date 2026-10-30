// Canvas package wrapper over fetch (docs/PLAN-ui-redesign.md §1: a package that
// needs an API shape lib/api.ts does not expose adds it here, same conventions).
//
// GET /api/projects/{id}/file already returns `editable`; lib/api.ts's
// getProjectFile drops it. The canvas needs it to decide whether a project file
// opened from the rail can be edited. Saving stays on lib/api.ts putProjectFile.

export interface ProjectDocument {
  path: string;
  content: string;
  lean: boolean;
  binary: boolean;
  /** False for observed (read-only) revisions; true otherwise. */
  editable: boolean;
  file_revision?: string;
}

async function detailMessage(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => ({}) as Record<string, unknown>);
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && typeof (detail as { message?: unknown }).message === 'string') {
    return (detail as { message: string }).message;
  }
  if (typeof (body as { message?: unknown }).message === 'string') return (body as { message: string }).message;
  return fallback;
}

export async function getProjectDocument(projectId: string, path: string): Promise<ProjectDocument> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}`,
  );
  // A binary/undecodable file comes back as 415 (lib/api.ts does the same).
  if (response.status === 415) return { path, content: '', lean: false, binary: true, editable: false };
  if (!response.ok) throw new Error(await detailMessage(response, `Failed to load ${path}: ${response.statusText}`));
  const data = await response.json();
  return {
    path: data.path ?? path,
    content: data.content ?? '',
    lean: !!data.lean || /\.lean$/i.test(path),
    binary: false,
    // Older adapters omit the flag; they accepted writes for any text file.
    editable: data.editable !== false,
    file_revision: data.file_revision,
  };
}
