import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Braces, ChevronDown, Download, FileText, ImageIcon } from 'lucide-react';
import { getProjectDoc, type ProjectGraph } from '../../lib/api';
import { EXPORT_MIME, blueprintJsonText, exportFileName, type ExportFormat, type ExportProject } from '../../lib/blueprintExport.js';

// The toolbar's "Download ▾" menu: the blueprint as Markdown (the raw
// .lea/blueprint.md), as JSON (the derived graph: nodes with status and sessions,
// plus edges) or as a PNG of the rendered graph. Files are named after the
// project (`Sqrt2-blueprint.md`); the pure parts live in lib/blueprintExport.js.

/** Save a Blob through a temporary <a download>. Electron shows its save dialog. */
export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fileName;
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  // The download may read the blob after click() returns; free it later.
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

interface Item {
  format: ExportFormat;
  label: string;
  icon: typeof FileText;
  disabled: boolean;
  why?: string;
}

export function DownloadMenu({
  projectId,
  project,
  graph,
  hasMarkdown,
  pngReason,
  renderPng,
  onError,
}: {
  projectId: string;
  /** For the file names and the JSON's `project`; `{ id }` when the project is not loaded. */
  project: ExportProject;
  graph: ProjectGraph | null;
  /** Whether .lea/blueprint.md has content worth downloading. */
  hasMarkdown: boolean;
  /** Why the PNG is unavailable (no graph on screen), or null when it can be rendered. */
  pngReason: string | null;
  renderPng: () => Promise<Blob>;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = `bpv-download-${useId().replace(/[^A-Za-z0-9_-]/g, '')}`;

  const nodeCount = graph?.nodes.length ?? 0;
  const items: Item[] = [
    {
      format: 'md',
      label: 'Markdown',
      icon: FileText,
      disabled: !hasMarkdown,
      why: hasMarkdown ? undefined : 'The blueprint file is empty',
    },
    {
      format: 'json',
      label: 'JSON',
      icon: Braces,
      disabled: nodeCount === 0,
      why: nodeCount === 0 ? 'The graph has no nodes' : undefined,
    },
    { format: 'png', label: 'PNG image', icon: ImageIcon, disabled: pngReason !== null, why: pngReason ?? undefined },
  ];

  const enabledButtons = () =>
    Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []);

  const close = useCallback((focusButton: boolean) => {
    setOpen(false);
    if (focusButton) buttonRef.current?.focus();
  }, []);

  // Focus the first enabled item on open; close on a click outside.
  useEffect(() => {
    if (!open) return;
    enabledButtons()[0]?.focus();
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as globalThis.Node)) close(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open, close]);

  const run = async (format: ExportFormat) => {
    close(true);
    setBusy(format);
    try {
      const name = exportFileName(project, format);
      if (format === 'md') {
        const content = await getProjectDoc(projectId, 'blueprint');
        saveBlob(new Blob([content], { type: EXPORT_MIME.md }), name);
      } else if (format === 'json') {
        if (!graph) throw new Error('The graph is not loaded yet.');
        saveBlob(new Blob([blueprintJsonText(graph, { project })], { type: EXPORT_MIME.json }), name);
      } else {
        saveBlob(await renderPng(), name);
      }
    } catch (err) {
      onError(`Download failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  };

  const onMenuKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const buttons = enabledButtons();
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close(true);
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (buttons.length === 0) return;
      const step = e.key === 'ArrowDown' ? 1 : -1;
      buttons[(i + step + buttons.length) % buttons.length].focus();
    } else if (e.key === 'Home' || e.key === 'End') {
      e.preventDefault();
      (e.key === 'Home' ? buttons[0] : buttons[buttons.length - 1])?.focus();
    } else if (e.key === 'Tab') {
      close(false);
    }
  };

  return (
    <div className="bpv-menu-wrap" ref={wrapRef}>
      <button
        ref={buttonRef}
        className="bpv-btn is-small"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        disabled={busy !== null}
        title="Download the blueprint"
      >
        <Download size={13} /> {busy ? 'Exporting…' : 'Download'} <ChevronDown size={12} className="bpv-caret" />
      </button>
      {open && (
        <div className="bpv-menu" role="menu" id={menuId} aria-label="Download the blueprint as" ref={menuRef} onKeyDown={onMenuKeyDown}>
          {items.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.format}
                role="menuitem"
                className="bpv-menu-item"
                disabled={item.disabled}
                title={item.why}
                onClick={() => void run(item.format)}
              >
                <Icon size={14} />
                <span className="bpv-menu-text">
                  <span>{item.label}</span>
                  <span className="bpv-menu-hint">{item.disabled && item.why ? item.why : exportFileName(project, item.format)}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
