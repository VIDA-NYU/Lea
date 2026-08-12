import { useEffect, useMemo, useRef, useState } from 'react';
import { Search } from 'lucide-react';
import { searchSessions, type SearchResult } from '../lib/api';

// ⌘K global search overlay (F9 / D41). A spotlight modal over `GET /api/search`:
// debounced query, results sectioned into "Loose chats" vs "Inside projects"
// (project-tagged), fully keyboard-driven (↑/↓ to move, ↵ to open, esc to close).
// It's the only path to a project session — the sidebar hides those (D30). Opening
// is handled by the parent (App owns the ⌘K toggle); this owns everything else.

function statusDot(status: string): string {
  if (status === 'ok' || status === 'proved' || status === 'defined') return 'ok';
  if (status === 'disproved') return 'run';
  if (status === 'error') return 'fail';
  if (status === 'running') return 'run';
  return 'idle';
}

export function SearchOverlay({
  open,
  onClose,
  onOpenSession,
  onOpenFormalization,
}: {
  open: boolean;
  onClose: () => void;
  onOpenSession: (sessionId: string) => void;
  onOpenFormalization: (formalizationId: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset + focus on open.
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setResults([]);
    setActive(0);
    setLoading(false);
    const id = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(id);
  }, [open]);

  // Debounced search; an empty query clears results without a request.
  useEffect(() => {
    if (!open) return;
    const q = query.trim();
    if (!q) {
      setResults([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    let cancelled = false;
    const id = setTimeout(() => {
      searchSessions(q)
        .then((rows) => {
          if (cancelled) return;
          setResults(rows);
          setActive(0);
        })
        .catch(() => !cancelled && setResults([]))
        .finally(() => !cancelled && setLoading(false));
    }, 160);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [query, open]);

  // Loose chats first, then in-project — the flat order keyboard nav indexes into.
  const formalizations = useMemo(
    () => results.filter((r) => r.result_type === 'formalization'),
    [results],
  );
  const sessions = useMemo(
    () => results.filter((r) => r.result_type !== 'formalization'),
    [results],
  );
  const loose = useMemo(() => sessions.filter((r) => !r.project_id), [sessions]);
  const inProject = useMemo(() => sessions.filter((r) => r.project_id), [sessions]);
  const ordered = useMemo(
    () => [...formalizations, ...loose, ...inProject],
    [formalizations, loose, inProject],
  );

  if (!open) return null;

  const choose = (r: SearchResult) => {
    if (r.result_type === 'formalization') onOpenFormalization(r.id);
    else onOpenSession(r.id);
    onClose();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, ordered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const r = ordered[active];
      if (r) choose(r);
    }
  };

  // A render-time counter maps each row to its index in `ordered` (loose then
  // project), so the highlighted row matches the keyboard cursor.
  let flatIndex = -1;
  const renderRow = (r: SearchResult) => {
    flatIndex += 1;
    const i = flatIndex;
    return (
      <button
        key={r.id}
        className={`search-row ${i === active ? 'active' : ''}`}
        onMouseMove={() => setActive(i)}
        onClick={() => choose(r)}
      >
        <span className={`dot ${statusDot(r.status)}`} />
        <span className="search-row-title">
          {r.declaration_name || r.title}
          {r.result_type === 'formalization' && (
            <small>{r.formalization_kind} · {r.status}</small>
          )}
        </span>
        {r.project_id && <span className="search-tag" title={r.project_namespace ?? undefined}>{r.project_title}</span>}
        <span className="search-row-when">{new Date(r.updated_at).toLocaleDateString()}</span>
      </button>
    );
  };

  const q = query.trim();
  return (
    <div className="search-overlay" onMouseDown={onClose}>
      <div className="search-modal" onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKeyDown}>
        <div className="search-input-row">
          <Search size={16} className="search-input-icon" />
          <input
            ref={inputRef}
            className="search-input"
            placeholder="Search formalizations, declarations, files, and conversations…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            spellCheck={false}
          />
          <kbd className="search-esc">esc</kbd>
        </div>

        <div className="search-results">
          {!q ? (
            <div className="search-hint">Type to search across formalizations and conversations.</div>
          ) : loading && results.length === 0 ? (
            <div className="search-hint">Searching…</div>
          ) : ordered.length === 0 ? (
            <div className="search-hint">Nothing matches “{q}”.</div>
          ) : (
            <>
              {formalizations.length > 0 && (
                <div className="search-section">
                  <div className="search-section-label">Formalizations</div>
                  {formalizations.map(renderRow)}
                </div>
              )}
              {loose.length > 0 && (
                <div className="search-section">
                  <div className="search-section-label">Loose chats</div>
                  {loose.map(renderRow)}
                </div>
              )}
              {inProject.length > 0 && (
                <div className="search-section">
                  <div className="search-section-label">Inside projects</div>
                  {inProject.map(renderRow)}
                </div>
              )}
            </>
          )}
        </div>

        <div className="search-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> open</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
