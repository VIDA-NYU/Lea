import { useMemo } from 'react';
import type { CodeStep } from '../../lib/api';
import { useLeanContents, type FlatFile } from '../../lib/filesData';
import { deriveFileStatuses, editingPath, type FileStatusInfo } from '../../lib/fileStatus.js';
import { sortCodeSteps } from '../../lib/timeline.mjs';
import { useProofSession } from '../../stores/proofSession';
import { useSessions } from '../../stores/sessions';

/**
 * Per-file status for one project, shared by the rail, the breadcrumbs and
 * the palette: the selected session's code steps (only when that session
 * belongs to `projectId`) joined with the fetched `.lean` text and the live
 * run. See lib/fileStatus.js for the rules.
 */
export function useFileStatuses(
  projectId: string | null,
  files: FlatFile[],
  options: { contents?: boolean } = {},
): {
  statuses: Map<string, FileStatusInfo>;
  codeSteps: CodeStep[];
  editing: string | null;
  isRunning: boolean;
  contents: Map<string, string>;
} {
  const rawSteps = useProofSession((s) => s.codeSteps);
  const isRunning = useProofSession((s) => s.isRunning);
  const currentRunId = useProofSession((s) => s.currentRunId);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  const sessions = useSessions((s) => s.sessions);

  const sessionInProject = useMemo(() => {
    if (!projectId || !selectedSessionId) return false;
    const session = sessions.find((s) => s.id === selectedSessionId);
    return session?.project_id === projectId;
  }, [projectId, selectedSessionId, sessions]);

  const codeSteps = useMemo(
    () => (sessionInProject ? (sortCodeSteps(rawSteps) as CodeStep[]) : []),
    [rawSteps, sessionInProject],
  );

  const contents = useLeanContents(projectId, files, { enabled: options.contents !== false });

  const editing = useMemo(
    () => editingPath({ codeSteps, isRunning: isRunning && sessionInProject, currentRunId }),
    [codeSteps, isRunning, sessionInProject, currentRunId],
  );

  const paths = useMemo(() => {
    const set = new Set(files.map((f) => f.path));
    for (const step of codeSteps) if (step.path) set.add(step.path);
    return [...set];
  }, [files, codeSteps]);

  const statuses = useMemo(
    () => deriveFileStatuses({ paths, codeSteps, contents, editingPath: editing }) as Map<string, FileStatusInfo>,
    [paths, codeSteps, contents, editing],
  );

  return { statuses, codeSteps, editing, isRunning: isRunning && sessionInProject, contents };
}
