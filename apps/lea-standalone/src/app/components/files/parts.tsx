import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import type { FileStatus } from '../../stores/workbench';
import { STATUS_NAMES } from '../../lib/fileStatus.js';

// Shared pieces of the files package: inline icons (the mockup's 24-grid strokes),
// the status dot, and a small portal menu with keyboard navigation used by the
// rail's context menu, the project switcher and the breadcrumb folder menus.

// ── Icons ─────────────────────────────────────────────────────────────────────

const PATHS: Record<string, ReactNode> = {
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  folderOpen: (
    <>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      <path d="M3 11h18" />
    </>
  ),
  file: (
    <>
      <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <path d="M14 3v6h6" />
    </>
  ),
  filePlus: (
    <>
      <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <path d="M14 3v6h6M12 12v6M9 15h6" />
    </>
  ),
  chevronDown: <path d="M6 9l6 6 6-6" />,
  chevronRight: <path d="M9 6l6 6-6 6" />,
  collapse: <path d="M7 10l5-5 5 5M7 14l5 5 5-5" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20l-4-4" />
    </>
  ),
  chat: <path d="M21 12a8 8 0 0 1-8 8H8l-5 3 1-5a8 8 0 1 1 17-6z" />,
  sparkle: <path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8" />,
  check: <path d="M5 12l5 5 9-10" />,
  rail: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="3" />
      <path d="M9 3v18" />
    </>
  ),
  lines: <path d="M4 7h16M4 12h10M4 17h16" />,
  command: <path d="M9 6l6 6-6 6M4 20h16" />,
  hash: <path d="M5 9h14M5 15h14M10 3L8 21M16 3l-2 18" />,
};

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 14, className = '', style }: { name: IconName; size?: number; className?: string; style?: CSSProperties }) {
  return (
    <svg
      className={`files-ico ${className}`.trim()}
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      focusable="false"
      style={style}
    >
      {PATHS[name]}
    </svg>
  );
}

// ── Status dot ────────────────────────────────────────────────────────────────

/** A status dot; the hue is always paired with an accessible name and a label nearby. */
export function StatusDot({ status, title }: { status: FileStatus | null | undefined; title?: string }) {
  if (!status) return null;
  const name = title ?? STATUS_NAMES[status] ?? status;
  return <span className={`files-dot files-dot-${status}`} role="img" aria-label={name} title={name} />;
}

// ── Keyboard shortcut hint ────────────────────────────────────────────────────

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="files-kbd">{children}</kbd>;
}

// ── Portal menu ───────────────────────────────────────────────────────────────

export interface MenuItem {
  key: string;
  label: ReactNode;
  icon?: ReactNode;
  /** Right-aligned text: a shortcut, a status label, a tick. */
  hint?: ReactNode;
  disabled?: boolean;
  /** Tooltip; use it to say why an item is disabled. */
  title?: string;
  danger?: boolean;
  muted?: boolean;
  /** The item that represents the current selection (aria-current). */
  current?: boolean;
  separatorBefore?: boolean;
  className?: string;
  /** Leave the menu open after `onSelect` (drilling into a folder). */
  keepOpen?: boolean;
  onSelect?: () => void;
}

export interface MenuPosition {
  left: number;
  top: number;
}

interface FilesMenuProps {
  items: MenuItem[];
  ariaLabel: string;
  position: MenuPosition;
  width?: number;
  header?: ReactNode;
  footer?: ReactNode;
  className?: string;
  initialIndex?: number;
  /** The element that opened the menu: a mousedown on it does not count as "outside". */
  anchor?: HTMLElement | null;
  onClose: () => void;
  /** Keys the menu does not handle itself (← → for the breadcrumbs). */
  onKeyDown?: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
}

function nextEnabled(items: MenuItem[], from: number, step: 1 | -1): number {
  if (!items.length) return -1;
  let i = from;
  for (let n = 0; n < items.length; n++) {
    i = (i + step + items.length) % items.length;
    if (!items[i].disabled) return i;
  }
  return from;
}

/**
 * A menu rendered into document.body at a fixed viewport position, clamped to
 * the window. It takes focus on mount, moves with ↑ ↓ (wrapping, skipping
 * disabled items), Home/End, opens on ⏎ / Space, and closes on Esc, Tab or a
 * click outside. Hovering sets the active item, as the mockup's `.hov` row.
 */
export function FilesMenu({
  items,
  ariaLabel,
  position,
  width = 232,
  header,
  footer,
  className = '',
  initialIndex,
  anchor,
  onClose,
  onKeyDown,
}: FilesMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(() => {
    if (typeof initialIndex === 'number' && items[initialIndex] && !items[initialIndex].disabled) return initialIndex;
    return nextEnabled(items, -1, 1);
  });
  const [offset, setOffset] = useState<MenuPosition>(position);

  // Clamp to the viewport once the size is known.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const margin = 8;
    let { left, top } = position;
    if (left + rect.width > window.innerWidth - margin) left = Math.max(margin, window.innerWidth - margin - rect.width);
    if (top + rect.height > window.innerHeight - margin) top = Math.max(margin, window.innerHeight - margin - rect.height);
    setOffset({ left, top });
  }, [position, items.length]);

  useEffect(() => {
    ref.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (anchor && anchor.contains(target)) return;
      if (ref.current && !ref.current.contains(target)) onClose();
    };
    const onBlur = () => {
      // Focus left the window (the user switched app): close rather than linger.
      setTimeout(() => {
        if (ref.current && !ref.current.contains(document.activeElement)) onClose();
      }, 0);
    };
    document.addEventListener('mousedown', onPointerDown, true);
    window.addEventListener('blur', onBlur);
    return () => {
      document.removeEventListener('mousedown', onPointerDown, true);
      window.removeEventListener('blur', onBlur);
    };
  }, [onClose, anchor]);

  useEffect(() => {
    const el = ref.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    el?.scrollIntoView?.({ block: 'nearest' });
  }, [active]);

  const select = (index: number) => {
    const item = items[index];
    if (!item || item.disabled) return;
    item.onSelect?.();
    if (!item.keepOpen) onClose();
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        setActive((i) => nextEnabled(items, i, 1));
        return;
      case 'ArrowUp':
        event.preventDefault();
        setActive((i) => nextEnabled(items, i, -1));
        return;
      case 'Home':
        event.preventDefault();
        setActive(nextEnabled(items, -1, 1));
        return;
      case 'End':
        event.preventDefault();
        setActive(nextEnabled(items, 0, -1));
        return;
      case 'Enter':
      case ' ':
        event.preventDefault();
        select(active);
        return;
      case 'Escape':
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      case 'Tab':
        onClose();
        return;
      default:
        onKeyDown?.(event);
    }
  };

  const menu = (
    <div
      ref={ref}
      className={`files-menu ${className}`.trim()}
      role="menu"
      aria-label={ariaLabel}
      tabIndex={-1}
      style={{ left: offset.left, top: offset.top, width }}
      onKeyDown={handleKeyDown}
      onContextMenu={(event) => event.preventDefault()}
    >
      {header}
      {items.map((item, index) => (
        <div key={item.key} className="files-mi-wrap">
          {item.separatorBefore && <div className="files-hr" role="separator" />}
          <button
            type="button"
            role="menuitem"
            data-index={index}
            tabIndex={-1}
            className={[
              'files-mi',
              index === active ? 'is-active' : '',
              item.disabled ? 'is-disabled' : '',
              item.danger ? 'is-danger' : '',
              item.muted ? 'is-muted' : '',
              item.current ? 'is-current' : '',
              item.className ?? '',
            ]
              .filter(Boolean)
              .join(' ')}
            aria-disabled={item.disabled || undefined}
            aria-current={item.current ? 'true' : undefined}
            title={item.title}
            onMouseEnter={() => !item.disabled && setActive(index)}
            onClick={(event) => {
              event.preventDefault();
              select(index);
            }}
          >
            {item.icon && <span className="files-mi-icon">{item.icon}</span>}
            <span className="files-mi-label">{item.label}</span>
            {item.hint != null && <span className="files-mi-hint">{item.hint}</span>}
          </button>
        </div>
      ))}
      {footer}
    </div>
  );
  return createPortal(menu, document.body);
}

/** Where to open a menu for a mouse event or, for the keyboard, under an element. */
export function menuPositionFor(event: { clientX: number; clientY: number } | null, anchor: HTMLElement | null): MenuPosition {
  if (event && (event.clientX || event.clientY)) return { left: event.clientX, top: event.clientY };
  const rect = anchor?.getBoundingClientRect();
  return rect ? { left: rect.left, top: rect.bottom + 4 } : { left: 16, top: 16 };
}
