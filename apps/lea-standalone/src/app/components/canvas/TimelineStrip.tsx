import { useEffect, useRef, type ReactNode } from 'react';
import { IconChevronLeft, IconChevronRight } from './icons';

export interface TimelineDot {
  index: number;
  id: string;
  path: string | null;
  failed: boolean;
  user: boolean;
  current: boolean;
  tone: 'error' | 'sorry' | 'proved' | 'neutral';
}

// The 36px strip under the header: previous / next, one dot per code step (red
// for a failed check, a ring on the current one), "Step n of m · turn t · Lea",
// and the +a −b delta for this step. `note` is the right-hand text ("Editable",
// "Opening editor…", "Step 8 was your edit").
export function TimelineStrip({
  dots,
  index,
  total,
  head,
  tail,
  delta,
  ring,
  note,
  onPrev,
  onNext,
  onPick,
}: {
  dots: TimelineDot[];
  index: number;
  total: number;
  head: string;
  tail: string;
  delta?: { added: number; removed: number } | null;
  /** Colour of the current dot's ring: running (blue), error, proved, sorry, neutral. */
  ring: 'running' | 'error' | 'proved' | 'sorry' | 'neutral';
  note?: ReactNode;
  onPrev: () => void;
  onNext: () => void;
  onPick: (index: number) => void;
}) {
  // Long sessions overflow the dot row; keep the current dot in view.
  const dotsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = dotsRef.current?.querySelector<HTMLElement>('[aria-current="step"]');
    el?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [index, total]);
  const multiFile = new Set(dots.map((d) => d.path)).size > 1;
  return (
    <div className="cv-timeline" role="group" aria-label={`Timeline, step ${index + 1} of ${total}`}>
      <button type="button" className="cv-ibtn sm" aria-label="Previous step" onClick={onPrev} disabled={index <= 0}>
        <IconChevronLeft />
      </button>
      <button type="button" className="cv-ibtn sm" aria-label="Next step" onClick={onNext} disabled={index >= total - 1}>
        <IconChevronRight />
      </button>
      <div className="cv-dots" ref={dotsRef}>
        {dots.map((d) => (
          <button
            key={d.id}
            type="button"
            className={`cv-dot ${d.failed ? 'failed' : ''} ${d.user ? 'user' : ''} ${d.current ? `current ring-${ring}` : ''}`}
            aria-label={`Step ${d.index + 1}${multiFile && d.path ? `, ${baseName(d.path)}` : ''}${d.failed ? ', check failed' : ''}${d.user ? ', your edit' : ''}`}
            aria-current={d.current ? 'step' : undefined}
            title={`Step ${d.index + 1}${multiFile && d.path ? ` · ${baseName(d.path)}` : ''}${d.failed ? ' · check failed' : ''}${d.user ? ' · your edit' : ''}`}
            onClick={() => onPick(d.index)}
          />
        ))}
      </div>
      <span className="cv-steplabel">
        {head}
        {tail ? <span className="cv-steplabel-tail"> · {tail}</span> : null}
      </span>
      <span className="cv-spacer" />
      {delta && (delta.added > 0 || delta.removed > 0) ? (
        <span className="cv-delta mono">
          <span className="plus">+{delta.added}</span> <span className="minus">−{delta.removed}</span> this step
        </span>
      ) : null}
      {note ? <span className="cv-timeline-note">{note}</span> : null}
    </div>
  );
}

function baseName(path: string): string {
  return path.split('/').pop() || path;
}
