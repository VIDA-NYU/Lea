import '../../../styles/files.css';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { useDeclarations, useFlatFiles, useOtherProjectFiles, useProjectTree, type DeclarationHit, type FlatFile } from '../../lib/filesData';
import { STATUS_NAMES, statusLabel, type FileStatusInfo } from '../../lib/fileStatus.js';
import { isLeanPath } from '../../lib/filesTree.js';
import { highlightMatch, rankMatches } from '../../lib/fuzzy.js';
import { matchSlashCommands } from '../../lib/slashCommands.js';
import { useFiles, type ComposerContext } from '../../stores/files';
import { useProjects } from '../../stores/projects';
import { useWorkbench } from '../../stores/workbench';
import { Icon, Kbd, StatusDot, type IconName } from './parts';
import { useFileStatuses } from './useFileStatuses';

// Work package "files" (mockup frame 7). ⌘P palette: fuzzy match over file
// names, then declarations, then other projects' files; prefixes `#`
// declarations, `>` commands, `:` line. ⏎ opens the hit, ⌘⏎ adds it to the
// composer as context. App mounts it once; it renders nothing while closed
// and owns the ⌘P / Ctrl+P binding on `window`.

const FILE_LIMIT = 8;
const DECL_LIMIT = 8;
const OTHER_LIMIT = 5;

type Mode = 'all' | 'decls' | 'commands' | 'line';

function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.closest !== 'function') return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!el.closest('[contenteditable="true"]');
}

function parseQuery(raw: string): { mode: Mode; term: string } {
  const first = raw[0];
  if (first === '#') return { mode: 'decls', term: raw.slice(1).trim() };
  if (first === '>') return { mode: 'commands', term: raw.slice(1).trim() };
  if (first === ':') return { mode: 'line', term: raw.slice(1).trim() };
  return { mode: 'all', term: raw.trim() };
}

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

function Highlighted({ text, positions }: { text: string; positions: number[] }) {
  return (
    <>
      {highlightMatch(text, positions).map((segment, i) =>
        segment.hit ? (
          <span key={i} className="files-hl">
            {segment.text}
          </span>
        ) : (
          <span key={i}>{segment.text}</span>
        ),
      )}
    </>
  );
}

interface Hit {
  key: string;
  section: 'files' | 'decls' | 'other' | 'commands' | 'line';
  icon: IconName | ReactNode;
  name: ReactNode;
  sub?: ReactNode;
  right?: ReactNode;
  title?: string;
  disabled?: boolean;
  run: () => void | Promise<void>;
  /** What ⌘⏎ adds to the composer; undefined falls back to `run`. */
  context?: ComposerContext;
}

const SECTION_TITLES: Record<Hit['section'], string> = {
  files: 'Files',
  decls: 'Declarations',
  other: 'Other projects',
  commands: 'Commands',
  line: 'Go to line',
};

export function QuickOpen({ projectId }: { projectId: string | null }) {
  const open = useWorkbench((s) => s.quickOpenOpen);
  const setOpen = useWorkbench((s) => s.setQuickOpenOpen);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.shiftKey || event.altKey) return;
      if (event.key.toLowerCase() !== 'p') return;
      const isOpen = useWorkbench.getState().quickOpenOpen;
      if (!isOpen && isEditableTarget(event.target)) return;
      event.preventDefault();
      setOpen(!isOpen);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [setOpen]);

  const close = useCallback(() => setOpen(false), [setOpen]);
  if (!open) return null;
  return <Palette projectId={projectId} onClose={close} />;
}

function Palette({ projectId, onClose }: { projectId: string | null; onClose: () => void }) {
  const openPath = useWorkbench((s) => s.openPath);
  const openFile = useWorkbench((s) => s.openFile);
  const setFilesRailOpen = useWorkbench((s) => s.setFilesRailOpen);
  const setComposerContext = useFiles((s) => s.setComposerContext);
  const setPendingComposerText = useFiles((s) => s.setPendingComposerText);
  const setPendingLine = useFiles((s) => s.setPendingLine);
  const projects = useProjects((s) => s.projects);

  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const { mode, term } = parseQuery(query);

  const { tree, loading } = useProjectTree(projectId);
  const files = useFlatFiles(tree);
  const { statuses, codeSteps, contents } = useFileStatuses(projectId, files, { contents: true });
  const declarations = useDeclarations(projectId, contents);
  const otherIds = useMemo(() => projects.filter((p) => p.id !== projectId).map((p) => p.id), [projects, projectId]);
  const otherProjects = useOtherProjectFiles(otherIds, mode === 'all' && term.length > 0);
  const projectTitle = projects.find((p) => p.id === projectId)?.title;

  useEffect(() => {
    inputRef.current?.focus();
    void useProjects.getState().refreshProjects().catch(() => {});
  }, []);

  // Files the session touched come first for an empty query (recency), then
  // the tree in order; the fuzzy rank re-sorts once there is a term.
  const orderedFiles = useMemo<FlatFile[]>(() => {
    const touched = new Set(codeSteps.map((s) => s.path));
    return [...files.filter((f) => touched.has(f.path)), ...files.filter((f) => !touched.has(f.path))];
  }, [files, codeSteps]);

  const hits = useMemo<Hit[]>(() => {
    const out: Hit[] = [];
    const statusRight = (path: string, info: FileStatusInfo | undefined) => {
      const label = statusLabel(info, 'list');
      const openNow = path === openPath;
      if (!label && !openNow) return undefined;
      return (
        <span className={`quick-open-hit-right ${hintClass(info?.status ?? null)}`.trim()}>
          {isLeanPath(path) && <StatusDot status={info?.status} />}
          {[label, openNow ? 'open now' : ''].filter(Boolean).join(' · ')}
        </span>
      );
    };

    if (mode === 'commands') {
      for (const command of matchSlashCommands(term)) {
        out.push({
          key: `cmd:${command.name}`,
          section: 'commands',
          icon: 'command',
          name: <>/{command.name}</>,
          sub: command.description,
          right: command.argumentHint ? <span className="quick-open-hit-right files-mono">{command.argumentHint}</span> : undefined,
          run: () => setPendingComposerText(`/${command.name} `),
        });
      }
      return out;
    }

    if (mode === 'line') {
      const line = /^\d+$/.test(term) ? Number(term) : null;
      if (!openPath) {
        out.push({ key: 'line:none', section: 'line', icon: 'lines', name: 'No file open', sub: 'Open a file first, then : line jumps inside it.', disabled: true, run: () => {} });
      } else {
        out.push({
          key: 'line:go',
          section: 'line',
          icon: 'lines',
          name: line ? `Go to line ${line}` : 'Type a line number',
          sub: openPath,
          disabled: !line,
          run: () => {
            if (!line) return;
            openFile(openPath);
            setPendingLine(line);
          },
          context: line ? { path: openPath, intent: 'ask', line } : undefined,
        });
      }
      return out;
    }

    if (mode === 'all') {
      for (const { item, positions } of rankMatches(term, orderedFiles, (f) => f.path, { path: true, limit: FILE_LIMIT })) {
        const cut = item.path.length - item.name.length;
        const info = statuses.get(item.path);
        out.push({
          key: `file:${item.path}`,
          section: 'files',
          icon: 'file',
          name: <Highlighted text={item.name} positions={positions.filter((p) => p >= cut).map((p) => p - cut)} />,
          sub: <Highlighted text={item.path} positions={positions.filter((p) => p < cut)} />,
          right: statusRight(item.path, info),
          title: info?.status ? `${item.path} — ${STATUS_NAMES[info.status]}` : item.path,
          run: () => openFile(item.path),
          context: { path: item.path, intent: 'ask' },
        });
      }
    }

    if (mode === 'decls' || term) {
      const ranked = rankMatches(term, declarations, (d: DeclarationHit) => d.name, { limit: mode === 'decls' ? 12 : DECL_LIMIT });
      for (const { item, positions } of ranked) {
        const info = statuses.get(item.path);
        out.push({
          key: `decl:${item.path}:${item.line}:${item.name}`,
          section: 'decls',
          icon: 'lines',
          name: (
            <>
              <span className="files-kw">{item.kind}</span> <Highlighted text={item.name} positions={positions} />
            </>
          ),
          sub: `${item.path}:${item.line}${item.qualified !== item.name ? ` · ${item.qualified}` : ''}`,
          right: statusRight(item.path, info),
          run: () => {
            openFile(item.path);
            setPendingLine(item.line);
          },
          context: { path: item.path, intent: 'ask', line: item.line, label: item.name },
        });
      }
    }

    if (mode === 'all' && term) {
      const pool = otherProjects.flatMap(({ projectId: id, files: list }) =>
        list.map((f) => ({ ...f, projectId: id, projectTitle: projects.find((p) => p.id === id)?.title ?? id })),
      );
      for (const { item, positions } of rankMatches(term, pool, (f) => f.path, { path: true, limit: OTHER_LIMIT })) {
        const cut = item.path.length - item.name.length;
        out.push({
          key: `other:${item.projectId}:${item.path}`,
          section: 'other',
          icon: 'folder',
          name: <Highlighted text={item.name} positions={positions.filter((p) => p >= cut).map((p) => p - cut)} />,
          sub: `${item.projectTitle} / ${item.path}`,
          title: `Open ${item.path} in ${item.projectTitle}`,
          run: async () => {
            await useProjects.getState().openProject(item.projectId);
            openFile(item.path);
            setFilesRailOpen(true);
          },
        });
      }
    }
    return out;
  }, [mode, term, orderedFiles, statuses, declarations, otherProjects, projects, openPath, openFile, setPendingComposerText, setPendingLine, setFilesRailOpen]);

  useEffect(() => {
    setActive(0);
  }, [query, hits.length]);

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    el?.scrollIntoView?.({ block: 'nearest' });
  }, [active]);

  const runHit = (hit: Hit | undefined, asContext: boolean) => {
    if (!hit || hit.disabled) return;
    if (asContext && hit.context) {
      setComposerContext(hit.context);
    } else {
      void hit.run();
    }
    onClose();
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (hits.length) setActive((i) => (i + 1) % hits.length);
        return;
      case 'ArrowUp':
        event.preventDefault();
        if (hits.length) setActive((i) => (i - 1 + hits.length) % hits.length);
        return;
      case 'Enter':
        event.preventDefault();
        runHit(hits[active], event.metaKey || event.ctrlKey);
        return;
      case 'Escape':
        event.preventDefault();
        onClose();
        return;
      default:
    }
  };

  const sections: { section: Hit['section']; hits: { hit: Hit; index: number }[] }[] = [];
  hits.forEach((hit, index) => {
    const last = sections[sections.length - 1];
    if (last && last.section === hit.section) last.hits.push({ hit, index });
    else sections.push({ section: hit.section, hits: [{ hit, index }] });
  });

  const placeholder =
    mode === 'commands' ? 'Command…' : mode === 'line' ? 'Line number…' : mode === 'decls' ? 'Declaration…' : 'Go to file or declaration';
  const emptyText = !projectId
    ? 'Open a project session to search its files.'
    : loading && !tree
      ? 'Loading files…'
      : mode === 'decls' && declarations.length === 0
        ? 'Scanning declarations…'
        : 'No matches.';
  const activeId = hits[active] ? `quick-open-hit-${active}` : undefined;

  return (
    <div className="quick-open-backdrop" onMouseDown={onClose}>
      <div
        className="quick-open"
        role="dialog"
        aria-modal="true"
        aria-label="Go to file or declaration"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="quick-open-head">
          <Icon name="search" size={15} />
          <label htmlFor="quick-open-input" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>
            Go to file or declaration
          </label>
          <input
            id="quick-open-input"
            ref={inputRef}
            className="quick-open-input"
            type="text"
            value={query}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            role="combobox"
            aria-expanded={hits.length > 0}
            aria-controls="quick-open-list"
            aria-activedescendant={activeId}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
          />
          <span className="quick-open-prefixes">
            {([
              ['#', 'declarations', 'decls'],
              ['>', 'commands', 'commands'],
              [':', 'line', 'line'],
            ] as [string, string, Mode][]).map(([prefix, label, m]) => (
              <button
                key={prefix}
                type="button"
                className={`quick-open-prefix${mode === m ? ' is-on' : ''}`}
                title={`${prefix} searches ${label}`}
                onClick={() => {
                  setQuery(mode === m ? '' : prefix);
                  inputRef.current?.focus();
                }}
              >
                {prefix} {label}
              </button>
            ))}
          </span>
        </div>

        <div className="quick-open-body" id="quick-open-list" role="listbox" ref={listRef}>
          {hits.length === 0 ? (
            <div className="quick-open-empty">{emptyText}</div>
          ) : (
            sections.map(({ section, hits: group }) => (
              <div key={section}>
                <div className="quick-open-grp">
                  {section === 'files' && projectTitle ? `Files · ${projectTitle}` : SECTION_TITLES[section]}
                </div>
                {group.map(({ hit, index }) => (
                  <button
                    key={hit.key}
                    id={`quick-open-hit-${index}`}
                    type="button"
                    role="option"
                    aria-selected={index === active}
                    data-index={index}
                    className={['quick-open-hit', index === active ? 'is-active' : '', hit.disabled ? 'is-disabled' : ''].filter(Boolean).join(' ')}
                    title={hit.title}
                    onMouseEnter={() => setActive(index)}
                    onClick={(event) => runHit(hit, event.metaKey || event.ctrlKey)}
                  >
                    {typeof hit.icon === 'string' ? <Icon name={hit.icon as IconName} size={13} /> : hit.icon}
                    <span className="quick-open-hit-main">
                      <span className="quick-open-hit-name">{hit.name}</span>
                      {hit.sub && <span className="quick-open-hit-sub">{hit.sub}</span>}
                    </span>
                    {hit.right}
                  </button>
                ))}
              </div>
            ))
          )}
        </div>

        <div className="quick-open-foot">
          <span>
            <Kbd>↑ ↓</Kbd> move
          </span>
          <span>
            <Kbd>⏎</Kbd> open
          </span>
          <span>
            <Kbd>⌘⏎</Kbd> add to chat as context
          </span>
          <span>
            <Kbd>esc</Kbd> close
          </span>
          <span className="files-foot-right">
            {files.length} {files.length === 1 ? 'file' : 'files'} · {declarations.length}{' '}
            {declarations.length === 1 ? 'declaration' : 'declarations'} · {projects.length}{' '}
            {projects.length === 1 ? 'project' : 'projects'}
          </span>
        </div>
      </div>
    </div>
  );
}
