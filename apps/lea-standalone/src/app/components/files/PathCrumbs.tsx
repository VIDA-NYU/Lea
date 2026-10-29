import '../../../styles/files.css';
import { useCallback, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { createProjectFile, useFlatFiles, useProjectTree } from '../../lib/filesData';
import { STATUS_NAMES, statusLabel, type FileStatusInfo } from '../../lib/fileStatus.js';
import { childrenOf, isLeanPath, parentDir } from '../../lib/filesTree.js';
import { useWorkbench } from '../../stores/workbench';
import { FilesMenu, Icon, Kbd, StatusDot, menuPositionFor, type MenuItem, type MenuPosition } from './parts';
import { useFileStatuses } from './useFileStatuses';

// Work package "files" (mockup frame 6). The path under the canvas title: one
// button per segment; a folder crumb opens a menu of the files in that folder
// with status dots and the current file ticked; the file crumb lists its
// siblings. ← → move between crumbs (and between open menus), ↑ ↓ move inside
// a menu, ⏎ opens, Esc closes. `path` is repo-relative (null while the canvas
// shows the session's own proof and no project file is open).

function hintClass(status: FileStatusInfo['status']): string {
  switch (status) {
    case 'error':
      return 'c-red';
    case 'sorry':
      return 'c-amber';
    case 'running':
      return 'c-blue';
    case 'proved':
      return 'c-green';
    default:
      return '';
  }
}

export function PathCrumbs({ projectId, path }: { projectId: string | null; path: string | null }) {
  if (!projectId || !path) {
    return (
      <nav className="path-crumbs" aria-label="File path">
        <span className="path-crumb is-muted">proofs</span>
        <span className="path-sep">/</span>
        <span className="path-crumb is-muted" aria-label="No project file open">
          —
        </span>
      </nav>
    );
  }
  return <Crumbs projectId={projectId} path={path} />;
}

type OpenMenu = { index: number; dir: string; position: MenuPosition };

function Crumbs({ projectId, path }: { projectId: string; path: string }) {
  const openFile = useWorkbench((s) => s.openFile);
  const setFilesRailOpen = useWorkbench((s) => s.setFilesRailOpen);
  const { tree } = useProjectTree(projectId);
  const files = useFlatFiles(tree);
  const { statuses } = useFileStatuses(projectId, files);

  const segments = useMemo(() => path.split('/').filter(Boolean), [path]);
  const last = segments.length - 1;
  const buttonRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [focusIndex, setFocusIndex] = useState(last);
  const [menu, setMenu] = useState<OpenMenu | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  /** The directory a crumb lists: the folder itself, or the file's parent. */
  const dirForCrumb = useCallback(
    (index: number) => (index < last ? segments.slice(0, index + 1).join('/') : parentDir(path)),
    [segments, last, path],
  );

  const openMenuFor = useCallback(
    (index: number) => {
      const clamped = Math.max(0, Math.min(last, index));
      setFocusIndex(clamped);
      setMenu({ index: clamped, dir: dirForCrumb(clamped), position: menuPositionFor(null, buttonRefs.current[clamped]) });
    },
    [dirForCrumb, last],
  );

  const closeMenu = useCallback(() => {
    setMenu((current) => {
      if (current) buttonRefs.current[current.index]?.focus();
      return null;
    });
  }, []);

  const newFileIn = async (dir: string) => {
    const suggested = dir ? `${dir}/New.lean` : 'New.lean';
    const target = window.prompt('New file (path relative to the project)', suggested)?.trim();
    if (!target) return;
    setActionError(null);
    try {
      await createProjectFile(projectId, target);
      openFile(target);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  const menuItems = useMemo<MenuItem[]>(() => {
    if (!menu) return [];
    const entries = childrenOf(tree ?? [], menu.dir);
    const items: MenuItem[] = entries.map((entry) => {
      if (entry.type === 'dir') {
        return {
          key: entry.path,
          icon: <Icon name="folder" size={13} />,
          label: entry.name,
          hint: <Icon name="chevronRight" size={12} />,
          keepOpen: true,
          onSelect: () => {
            // Drill into the folder: the menu now lists it, still anchored to the crumb.
            setMenu((current) => (current ? { ...current, dir: entry.path } : current));
          },
        };
      }
      const info = statuses.get(entry.path);
      const current = entry.path === path;
      const lean = isLeanPath(entry.path);
      const label = statusLabel(info, 'list');
      return {
        key: entry.path,
        icon: lean ? <StatusDot status={info?.status} /> : <Icon name="file" size={13} />,
        label: <span className="files-mono">{entry.name}</span>,
        hint: current ? <Icon name="check" size={13} /> : label ? <span className={hintClass(info?.status ?? null)}>{label}</span> : undefined,
        current,
        muted: !lean,
        title: info?.status ? `${entry.path} — ${STATUS_NAMES[info.status]}` : entry.path,
        onSelect: () => openFile(entry.path),
      };
    });
    const dirName = menu.dir ? menu.dir.slice(menu.dir.lastIndexOf('/') + 1) : 'the project';
    items.push(
      {
        key: '__new',
        separatorBefore: true,
        muted: true,
        icon: <Icon name="filePlus" size={13} />,
        label: `New file in ${dirName}…`,
        onSelect: () => void newFileIn(menu.dir),
      },
      {
        key: '__rail',
        muted: true,
        icon: <Icon name="rail" size={13} />,
        label: 'Reveal in files rail',
        onSelect: () => setFilesRailOpen(true),
      },
    );
    // A folder drilled into from a crumb has no items when the menu just opened
    // onto a dir without children: say so rather than show an empty list.
    if (entries.length === 0) items.unshift({ key: '__empty', label: 'Empty folder', disabled: true });
    return items;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [menu, tree, statuses, path, openFile, setFilesRailOpen, projectId]);

  const fileCount = menu ? childrenOf(tree ?? [], menu.dir).filter((e) => e.type === 'file').length : 0;
  const initialIndex = menu ? Math.max(0, menuItems.findIndex((item) => item.current)) : 0;

  const onNavKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (menu) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      const next = Math.max(0, Math.min(last, focusIndex + (event.key === 'ArrowLeft' ? -1 : 1)));
      setFocusIndex(next);
      buttonRefs.current[next]?.focus();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      openMenuFor(focusIndex);
    }
  };

  const onMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!menu) return;
    if (event.key === 'ArrowLeft' && menu.index > 0) {
      event.preventDefault();
      openMenuFor(menu.index - 1);
    } else if (event.key === 'ArrowRight' && menu.index < last) {
      event.preventDefault();
      openMenuFor(menu.index + 1);
    }
  };

  return (
    <nav className="path-crumbs" aria-label="File path" onKeyDown={onNavKeyDown}>
      {segments.map((segment, index) => {
        const isFile = index === last;
        const open = menu?.index === index;
        return (
          <span key={`${index}:${segment}`} style={{ display: 'contents' }}>
            {index > 0 && <span className="path-sep">›</span>}
            <button
              ref={(el) => {
                buttonRefs.current[index] = el;
              }}
              type="button"
              className={['path-crumb', isFile ? 'is-file' : '', open ? 'is-open' : ''].filter(Boolean).join(' ')}
              tabIndex={focusIndex === index ? 0 : -1}
              aria-haspopup="menu"
              aria-expanded={open}
              title={isFile ? path : segments.slice(0, index + 1).join('/')}
              onFocus={() => setFocusIndex(index)}
              onClick={() => (open ? closeMenu() : openMenuFor(index))}
            >
              {segment}
              {isFile && <Icon name="chevronDown" size={11} />}
            </button>
          </span>
        );
      })}
      {actionError && (
        <span className="path-crumb is-muted files-err" role="alert">
          {actionError}
        </span>
      )}
      {menu && (
        <FilesMenu
          key={`${menu.index}:${menu.dir}`}
          ariaLabel={`Files in ${menu.dir || 'the project'}`}
          items={menuItems}
          position={menu.position}
          width={300}
          initialIndex={initialIndex}
          anchor={buttonRefs.current[menu.index]}
          onClose={closeMenu}
          onKeyDown={onMenuKeyDown}
          header={
            <div className="files-menu-head">
              <Icon name="folder" size={13} />
              <span className="files-mono">{menu.dir ? menu.dir.split('/').join(' / ') : 'project root'}</span>
              <span className="files-menu-count">
                {fileCount} {fileCount === 1 ? 'file' : 'files'}
              </span>
            </div>
          }
          footer={
            <div className="files-menu-foot">
              <span>
                <Kbd>← →</Kbd> folders
              </span>
              <span>
                <Kbd>↑ ↓</Kbd> files
              </span>
              <span>
                <Kbd>⏎</Kbd> open
              </span>
              <span className="files-foot-right">
                <Kbd>esc</Kbd>
              </span>
            </div>
          }
        />
      )}
    </nav>
  );
}
