import '../../../styles/files.css';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from 'react';
import type { TreeEntry } from '../../lib/api';
import { createProjectFile, useFlatFiles, useProjectTree } from '../../lib/filesData';
import { STATUS_NAMES, sessionFiles, statusLabel, type FileStatusInfo } from '../../lib/fileStatus.js';
import { ancestorDirs, dirPaths, isLeanPath } from '../../lib/filesTree.js';
import { useFiles } from '../../stores/files';
import { useProjects } from '../../stores/projects';
import { useWorkbench } from '../../stores/workbench';
import { FilesMenu, Icon, Kbd, StatusDot, menuPositionFor, type MenuItem, type MenuPosition } from './parts';
import { useFileStatuses } from './useFileStatuses';

// Work package "files" (docs/PLAN-ui-redesign.md, mockup frame 5). The rail at
// the canvas's left edge: project switcher, "Go to file", the files Lea touched
// this session, the project tree with per-file status dots and a "Lea" marker
// on the file a live run is editing, a legend, and a row context menu. Canvas
// mounts it when useWorkbench().filesRailOpen is true.

const ROW_INDENT = 14;
const ROW_BASE = 6;
const FILE_EXTRA = 18;

type ContextTarget = { entry: TreeEntry; position: MenuPosition };

function rightClass(status: FileStatusInfo['status']): string {
  switch (status) {
    case 'error':
      return 'c-red';
    case 'sorry':
      return 'c-amber';
    case 'running':
      return 'c-blue';
    default:
      return '';
  }
}

async function copyToClipboard(text: string) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard blocked (insecure context): nothing else to do without a prompt.
  }
}

export function FilesRail({ projectId }: { projectId: string }) {
  const openPath = useWorkbench((s) => s.openPath);
  const openFile = useWorkbench((s) => s.openFile);
  const setQuickOpenOpen = useWorkbench((s) => s.setQuickOpenOpen);
  const setComposerContext = useFiles((s) => s.setComposerContext);
  const projects = useProjects((s) => s.projects);
  const currentProject = useProjects((s) => s.currentProject);

  const { tree, error, loading, refresh } = useProjectTree(projectId);
  const files = useFlatFiles(tree);
  const { statuses, codeSteps, editing, isRunning } = useFileStatuses(projectId, files);
  const touched = useMemo(() => sessionFiles(codeSteps), [codeSteps]);

  const projectName =
    projects.find((p) => p.id === projectId)?.title ?? (currentProject?.id === projectId ? currentProject.title : 'Project');

  // ── Expanded folders: everything open once per project, the open file's
  // ancestors kept open, "Collapse folders" closes them all.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const seededFor = useRef<string | null>(null);
  useEffect(() => {
    if (!tree || seededFor.current === projectId) return;
    setExpanded(new Set(dirPaths(tree)));
    seededFor.current = projectId;
  }, [tree, projectId]);
  useEffect(() => {
    if (!openPath) return;
    const dirs = ancestorDirs(openPath);
    if (!dirs.length) return;
    setExpanded((prev) => {
      if (dirs.every((d) => prev.has(d))) return prev;
      const next = new Set(prev);
      dirs.forEach((d) => next.add(d));
      return next;
    });
  }, [openPath]);
  const toggleDir = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  // ── Keep the tree fresh while Lea writes: a new step may have created a
  // file, and a run ending may have committed several. Debounced.
  const stepCount = codeSteps.length;
  const refreshSignature = useRef<string | null>(null);
  useEffect(() => {
    const signature = `${projectId}|${stepCount}|${isRunning}`;
    const previous = refreshSignature.current;
    refreshSignature.current = signature;
    // First sight of a project: useProjectTree already loads it.
    if (previous === null || previous.split('|')[0] !== projectId || previous === signature) return;
    const timer = setTimeout(refresh, 800);
    return () => clearTimeout(timer);
  }, [projectId, stepCount, isRunning, refresh]);

  // ── Menus
  const [context, setContext] = useState<ContextTarget | null>(null);
  const [switcher, setSwitcher] = useState<MenuPosition | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const switcherRef = useRef<HTMLButtonElement>(null);
  const closeContext = useCallback(() => setContext(null), []);
  const closeSwitcher = useCallback(() => setSwitcher(null), []);

  const openContext = (entry: TreeEntry, event: ReactMouseEvent | null, anchor: HTMLElement | null) => {
    event?.preventDefault();
    setContext({ entry, position: menuPositionFor(event, anchor) });
  };

  const newFile = async (dir: string) => {
    const suggested = dir ? `${dir}/New.lean` : 'New.lean';
    const path = window.prompt('New file (path relative to the project)', suggested)?.trim();
    if (!path) return;
    setActionError(null);
    try {
      await createProjectFile(projectId, path);
      openFile(path);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  const contextItems = useMemo<MenuItem[]>(() => {
    if (!context) return [];
    const { entry } = context;
    const path = entry.path;
    if (entry.type === 'dir') {
      return [
        { key: 'new', label: `New file in ${entry.name}…`, icon: <Icon name="filePlus" size={13} />, onSelect: () => void newFile(path) },
        { key: 'copy', label: 'Copy path', hint: '⌥⌘C', onSelect: () => void copyToClipboard(path) },
      ];
    }
    const info = statuses.get(path);
    const lean = isLeanPath(path);
    const sorries = info?.sorries ?? 0;
    const desktop = typeof window !== 'undefined' ? window.leaDesktop : undefined;
    const items: MenuItem[] = [
      { key: 'open', label: 'Open', onSelect: () => openFile(path) },
      {
        key: 'side',
        label: 'Open to the side',
        disabled: true,
        title: 'Split view is not available yet: the canvas shows one file at a time.',
      },
      {
        key: 'ask',
        separatorBefore: true,
        icon: <Icon name="chat" size={13} className="c-blue" />,
        label: 'Ask Lea about this file',
        onSelect: () => setComposerContext({ path, intent: 'ask' }),
      },
      {
        key: 'prove',
        icon: <Icon name="sparkle" size={13} style={{ color: 'var(--amber)' }} />,
        label: sorries > 0 ? `Prove the ${sorries} ${sorries === 1 ? 'sorry' : 'sorries'} here` : 'Prove the sorries here',
        disabled: !lean || sorries === 0,
        title: !lean ? 'Not a Lean file' : sorries === 0 ? 'No sorry in this file' : undefined,
        onSelect: () => setComposerContext({ path, intent: 'prove-sorries' }),
      },
    ];
    if (desktop) {
      // TODO(desktop): the bridge needs `revealPath(absolutePath)` (shell.showItemInFolder)
      // and the repo root for the project; `openExternal` takes http(s) URLs only.
      items.push({
        key: 'reveal',
        separatorBefore: true,
        label: desktop.platform === 'darwin' ? 'Reveal in Finder' : 'Reveal in file manager',
        disabled: true,
        title: 'Needs window.leaDesktop.revealPath(path) in the desktop bridge.',
      });
    }
    items.push(
      { key: 'copy', separatorBefore: !desktop, label: 'Copy path', hint: '⌥⌘C', onSelect: () => void copyToClipboard(path) },
      // TODO(api): no move/delete endpoint for repo paths yet (deleteProjectFile is for
      // uploaded reference docs by id). Enable these once the adapter grows one.
      { key: 'rename', separatorBefore: true, label: 'Rename…', disabled: true, title: 'Renaming needs a project file move endpoint (not in the API yet).' },
      { key: 'delete', label: 'Delete…', danger: true, disabled: true, title: 'Deleting needs a project file delete endpoint (not in the API yet).' },
    );
    return items;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, statuses, openFile, setComposerContext, projectId]);

  const switcherItems = useMemo<MenuItem[]>(
    () =>
      projects.map((p) => ({
        key: p.id,
        label: p.title,
        icon: <Icon name="folder" size={13} />,
        current: p.id === projectId,
        hint: p.id === projectId ? <Icon name="check" size={13} /> : undefined,
        onSelect: () => {
          if (p.id !== projectId) void useProjects.getState().openProject(p.id);
        },
      })),
    [projects, projectId],
  );

  const onRailKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.metaKey && event.altKey && event.key.toLowerCase() === 'c') {
      const path = (event.target as HTMLElement | null)?.closest<HTMLElement>('[data-path]')?.dataset.path;
      if (path) {
        event.preventDefault();
        void copyToClipboard(path);
      }
    }
  };

  // ── Rows
  const renderFileRow = (entry: TreeEntry, depth: number): ReactNode => {
    const info = statuses.get(entry.path);
    const lean = isLeanPath(entry.path);
    const label = statusLabel(info, 'tree');
    const active = openPath === entry.path;
    const title = info?.status ? `${entry.path} — ${STATUS_NAMES[info.status]}` : entry.path;
    return (
      <button
        key={entry.path}
        type="button"
        className={['files-row', active ? 'is-active' : '', lean ? '' : 'is-muted', context?.entry.path === entry.path ? 'is-context' : '']
          .filter(Boolean)
          .join(' ')}
        style={{ paddingLeft: ROW_BASE + depth * ROW_INDENT + FILE_EXTRA }}
        data-path={entry.path}
        aria-current={active ? 'true' : undefined}
        title={title}
        onClick={() => openFile(entry.path)}
        onContextMenu={(event) => openContext(entry, event, event.currentTarget)}
        onKeyDown={(event) => {
          if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
            event.preventDefault();
            openContext(entry, null, event.currentTarget);
          }
        }}
      >
        {lean ? <StatusDot status={info?.status} /> : <Icon name="file" size={13} />}
        <span className="files-row-name files-mono">{entry.name}</span>
        {label && <span className={`files-row-right ${rightClass(info?.status ?? null)}`.trim()}>{label}</span>}
      </button>
    );
  };

  const renderEntries = (entries: TreeEntry[], depth: number): ReactNode =>
    entries.map((entry) => {
      if (entry.type !== 'dir') return renderFileRow(entry, depth);
      const open = expanded.has(entry.path);
      return (
        <div key={entry.path}>
          <button
            type="button"
            className={`files-row is-dir${context?.entry.path === entry.path ? ' is-context' : ''}`}
            style={{ paddingLeft: ROW_BASE + depth * ROW_INDENT }}
            data-path={entry.path}
            aria-expanded={open}
            title={entry.path}
            onClick={() => toggleDir(entry.path)}
            onContextMenu={(event) => openContext(entry, event, event.currentTarget)}
          >
            <Icon name={open ? 'chevronDown' : 'chevronRight'} size={12} />
            <Icon name={open ? 'folderOpen' : 'folder'} size={14} />
            <span className="files-row-name">{entry.name}</span>
          </button>
          {open && entry.children && entry.children.length > 0 && renderEntries(entry.children, depth + 1)}
        </div>
      );
    });

  return (
    <aside className="files-rail" aria-label="Files" onKeyDown={onRailKeyDown}>
      <div className="files-rail-head">
        <button
          ref={switcherRef}
          type="button"
          className="files-project-btn"
          aria-label={`Switch project, current project ${projectName}`}
          aria-haspopup="menu"
          aria-expanded={!!switcher}
          onClick={() => {
            if (switcher) {
              closeSwitcher();
              return;
            }
            void useProjects.getState().refreshProjects().catch(() => {});
            setSwitcher(menuPositionFor(null, switcherRef.current));
          }}
        >
          <Icon name="folder" size={14} />
          <span className="files-project-name">{projectName}</span>
          <Icon name="chevronDown" size={12} />
        </button>
        <span className="files-rail-spacer" />
        <button type="button" className="files-ibtn" aria-label="New file" title="New file…" onClick={() => void newFile('')}>
          <Icon name="filePlus" size={14} />
        </button>
        <button
          type="button"
          className="files-ibtn"
          aria-label="Collapse folders"
          title="Collapse folders"
          onClick={() => setExpanded(new Set())}
        >
          <Icon name="collapse" size={14} />
        </button>
      </div>

      <div className="files-goto-wrap">
        <button type="button" className="files-goto" onClick={() => setQuickOpenOpen(true)} aria-label="Go to file">
          <Icon name="search" size={13} />
          <span className="files-goto-text">Go to file</span>
          <Kbd>⌘P</Kbd>
        </button>
      </div>

      <div className="files-grp">This session</div>
      <div className="files-rows">
        {touched.length === 0 ? (
          <div className="files-empty">{isRunning ? 'Lea has not written a file yet.' : 'No files written in this session.'}</div>
        ) : (
          touched.map((file) => {
            const info = statuses.get(file.path);
            const isEditing = editing === file.path;
            return (
              <button
                key={file.path}
                type="button"
                className={`files-row${openPath === file.path ? ' is-active' : ''}`}
                data-path={file.path}
                title={`${file.path}${info?.status ? ` — ${STATUS_NAMES[info.status]}` : ''}`}
                onClick={() => openFile(file.path)}
                onContextMenu={(event) =>
                  openContext({ name: file.name, path: file.path, type: 'file' }, event, event.currentTarget)
                }
              >
                {isLeanPath(file.path) ? <StatusDot status={info?.status ?? 'unchecked'} /> : <Icon name="file" size={13} />}
                <span className="files-row-name files-mono">{file.name}</span>
                <span className="files-row-right">
                  {isEditing ? (
                    <span className="c-blue">Lea · editing</span>
                  ) : (
                    <>
                      {file.added > 0 && <span className="c-green">+{file.added}</span>}
                      {file.added > 0 && file.removed > 0 && ' '}
                      {file.removed > 0 && <span className="c-red">−{file.removed}</span>}
                      {file.added === 0 && file.removed === 0 && '±0'}
                    </>
                  )}
                </span>
              </button>
            );
          })
        )}
      </div>

      <div className="files-grp">Project</div>
      <div className="files-tree" aria-label="Project files">
        {actionError && <div className="files-empty files-err">{actionError}</div>}
        {loading && !tree ? (
          <div className="files-empty">Loading…</div>
        ) : error && !tree ? (
          <div className="files-empty files-err">{error}</div>
        ) : !tree || tree.length === 0 ? (
          <div className="files-empty">Empty repo.</div>
        ) : (
          renderEntries(tree, 0)
        )}
      </div>

      <div className="files-legend" aria-label="Legend">
        <span className="files-legend-item">
          <StatusDot status="proved" />
          proved
        </span>
        <span className="files-legend-item">
          <StatusDot status="sorry" />
          sorry
        </span>
        <span className="files-legend-item">
          <StatusDot status="error" />
          error
        </span>
        <span className="files-legend-item">
          <StatusDot status="unchecked" />
          unchecked
        </span>
        {editing && (
          <span className="files-legend-item">
            <StatusDot status="running" />
            Lea
          </span>
        )}
      </div>

      {context && (
        <FilesMenu
          ariaLabel={`${context.entry.name} actions`}
          items={contextItems}
          position={context.position}
          onClose={closeContext}
        />
      )}
      {switcher && (
        <FilesMenu
          ariaLabel="Switch project"
          items={switcherItems}
          position={switcher}
          width={260}
          initialIndex={Math.max(0, projects.findIndex((p) => p.id === projectId))}
          anchor={switcherRef.current}
          onClose={closeSwitcher}
        />
      )}
    </aside>
  );
}
