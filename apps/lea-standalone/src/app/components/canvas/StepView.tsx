import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { ensureHighlighter, highlightToLines, isHighlighterReady } from '../../lib/leanHighlight.mjs';
import type { DiffRow } from '../../lib/codeDiff';

export interface LineMarks {
  /** Lines this step changed (gutter mark). */
  changed?: number[];
  /** Lines with a lean_check / LSP error (red tint, red line number, gutter mark). */
  errors?: number[];
  /**
   * Where each error starts, for the squiggle (from the column to the line's
   * end). Leave empty when the editor already draws its own diagnostic markers.
   * The read-only view squiggles every error line whole.
   */
  errorAt?: { line: number; column: number | null }[];
  /** Lines containing `sorry` (amber tint). */
  sorries?: number[];
  /** The line Lea is editing while a run is live (caret marker). */
  caret?: number | null;
  caretLabel?: string;
}

export interface StepViewHandle {
  jumpToLine(line: number): void;
}

// Read-only, Shiki-highlighted view of one snapshot. Used for scrubbed (older)
// steps, as the stand-in while Monaco loads, and for project files. Marks are
// line-based so the same `LineMarks` drive the Monaco decorations.
export const StepView = forwardRef<
  StepViewHandle,
  { code: string; rows: DiffRow[]; marks: LineMarks; lean?: boolean; dim?: boolean }
>(function StepView({ code, rows, marks, lean = true, dim = false }, ref) {
  const [hlReady, setHlReady] = useState(isHighlighterReady());
  useEffect(() => {
    if (hlReady || !lean) return;
    let cancelled = false;
    ensureHighlighter()
      .then(() => !cancelled && setHlReady(true))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [hlReady, lean]);
  const tokenLines = useMemo(
    () => (lean && hlReady && code ? highlightToLines(code) : null),
    [lean, hlReady, code],
  );

  const rootRef = useRef<HTMLDivElement>(null);
  const [flash, setFlash] = useState<number | null>(null);
  useImperativeHandle(
    ref,
    () => ({
      jumpToLine(line: number) {
        const el = rootRef.current?.querySelector<HTMLElement>(`[data-line="${line}"]`);
        el?.scrollIntoView({ block: 'center' });
        setFlash(line);
        window.setTimeout(() => setFlash((f) => (f === line ? null : f)), 1400);
      },
    }),
    [],
  );

  const changed = useMemo(() => new Set(marks.changed ?? []), [marks.changed]);
  const errors = useMemo(() => new Set(marks.errors ?? []), [marks.errors]);
  const sorries = useMemo(() => new Set(marks.sorries ?? []), [marks.sorries]);

  return (
    <div ref={rootRef} className={`cv-step ${dim ? 'dim' : ''}`}>
      <pre className="cv-code">
        {rows.map((row, i) => {
          const n = row.newLineNumber ?? null;
          const isCaret = n != null && marks.caret === n;
          const cls = [
            'cv-ln',
            n != null && changed.has(n) ? 'changed' : '',
            n != null && errors.has(n) ? 'error' : '',
            n != null && sorries.has(n) ? 'sorry' : '',
            isCaret ? 'caret' : '',
            n != null && flash === n ? 'flash' : '',
          ]
            .filter(Boolean)
            .join(' ');
          return (
            <div key={i} className={cls} data-line={n ?? undefined}>
              <span className="cv-mark" aria-hidden="true" />
              <span className="cv-gut">{n ?? ''}</span>
              <span className={`cv-src ${n != null && errors.has(n) ? 'cv-squiggle' : ''}`}>
                {renderLineTokens(tokenLines, row)}
                {row.line === '' ? ' ' : ''}
              </span>
              {isCaret && (
                <>
                  <span className="cv-caret-bar" aria-hidden="true" />
                  <span className="cv-caret-chip">{marks.caretLabel ?? 'Lea · editing'}</span>
                </>
              )}
            </div>
          );
        })}
      </pre>
    </div>
  );
});

// Render one row's source using its Shiki tokens (colors inline from the theme).
// Tokens are keyed by the row's new-file line number so they stay aligned with the
// gutter; falls back to the raw text while the highlighter loads. Shiki fontStyle
// is a bitmask (Italic=1, Bold=2, Underline=4).
/* eslint-disable @typescript-eslint/no-explicit-any */
function renderLineTokens(tokenLines: any[] | null, row: DiffRow) {
  const toks = tokenLines && row.newLineNumber != null ? tokenLines[row.newLineNumber - 1] : null;
  if (!toks) return row.line;
  return toks.map((t: any, j: number) => (
    <span
      key={j}
      style={{
        color: t.color,
        fontStyle: t.fontStyle & 1 ? 'italic' : undefined,
        fontWeight: t.fontStyle & 2 ? 600 : undefined,
        textDecoration: t.fontStyle & 4 ? 'underline' : undefined,
      }}
    >
      {t.content}
    </span>
  ));
}
/* eslint-enable @typescript-eslint/no-explicit-any */

export function plainRows(code: string): DiffRow[] {
  const lines = String(code ?? '').split('\n');
  if (lines[lines.length - 1] === '' && lines.length > 1) lines.pop();
  return lines.map((line, index) => ({ kind: 'unchanged', line, newLineNumber: index + 1 }));
}
