import { useEffect, useMemo, useRef, useState } from 'react';
import { Search, Check } from 'lucide-react';
import type { ModelCatalogEntry, ModelOption } from '../lib/api';

const MAX_RESULTS = 60;

type Row = { value: string; provider?: string; custom?: boolean };

// A Spotlight-style model picker: the chip in the chat head opens a centered
// search overlay over the full LiteLLM catalog (type to filter, ↑/↓ + Enter,
// Esc to close), instead of a cramped popover. Any non-matching string can be
// used as a custom model ID.
export function ModelPicker({
  value,
  catalog,
  featured,
  onChange,
  placeholder = 'Select model',
  openSignal,
}: {
  value: string;
  catalog: ModelCatalogEntry[];
  featured: ModelOption[];
  onChange: (value: string) => void;
  // Button label when no model is set (e.g. "Inherit coordinator's model" on the
  // Sub-agents page, where an empty value means inherit).
  placeholder?: string;
  // Open the picker from OUTSIDE: bump this number and the overlay opens (v2.4, so a
  // diagnostic's "Change model" button lands on the picker itself rather than
  // dropping the user on the Settings page to go and find it). A counter rather than
  // a boolean so repeated requests re-open it — with a boolean, closing the picker
  // and asking again would be a no-op, since the prop never changed.
  openSignal?: number;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (open) {
      setQuery('');
      setActive(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const q = query.trim().toLowerCase();
  const rows = useMemo<Row[]>(() => {
    const trimmed = query.trim();
    const known =
      catalog.some((m) => m.value === trimmed) || featured.some((m) => m.value === trimmed);
    const matches: Row[] = q
      ? catalog
          .filter((m) => m.value.toLowerCase().includes(q) || m.provider.toLowerCase().includes(q))
          .slice(0, MAX_RESULTS)
          .map((m) => ({ value: m.value, provider: m.provider }))
      : featured.map((m) => ({ value: m.value, provider: m.family }));
    return trimmed && !known ? [{ value: trimmed, custom: true }, ...matches] : matches;
  }, [q, query, catalog, featured]);

  const choose = (v: string) => {
    if (v) onChange(v);
    setOpen(false);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') setOpen(false);
    else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, rows.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (rows[active]) choose(rows[active].value);
    }
  };

  // keep the active row in view
  useEffect(() => {
    listRef.current?.querySelector('.mm-row.active')?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  // An outside request to open (see `openSignal`). Guarded on a truthy value so the
  // initial mount — where the counter is 0/undefined — doesn't pop the picker open
  // at every page load.
  useEffect(() => {
    if (openSignal) setOpen(true);
  }, [openSignal]);

  return (
    <>
      <button className="chip model model-trigger" onClick={() => setOpen(true)} title="Change model">
        {value || placeholder} <span className="caret">▾</span>
      </button>

      {open && (
        <div
          className="model-overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          <div className="model-modal">
            <div className="mm-input">
              <Search size={17} className="mm-search-icon" />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActive(0);
                }}
                onKeyDown={onKeyDown}
                placeholder="Search models or type any model ID…"
              />
              <span className="mm-esc" onClick={() => setOpen(false)}>
                esc
              </span>
            </div>
            <div className="mm-section">{q ? 'Matches' : 'Featured'}</div>
            <div className="mm-results" ref={listRef}>
              {rows.map((row, i) => (
                <button
                  key={row.value}
                  className={`mm-row ${i === active ? 'active' : ''}`}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => choose(row.value)}
                >
                  <span className="mm-check">{row.value === value ? <Check size={15} /> : null}</span>
                  <span className="mm-name">{row.custom ? `Use “${row.value}”` : row.value}</span>
                  {row.provider && <span className="mm-prov">{row.provider}</span>}
                </button>
              ))}
              {rows.length === 0 && <div className="mm-empty">Type a model ID to use it directly.</div>}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
