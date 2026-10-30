import '../../../styles/shell.css';
import type { ReactNode } from 'react';
import type { BlueprintStatus } from '../../lib/api';
import { BLUEPRINT_LABELS, blueprintBar, blueprintTally } from '../../lib/shellModel.js';

/**
 * Small presentational pieces shared by the shell's screens. Status is never
 * hue alone: every dot sits next to a label, and the bar carries an aria-label
 * that spells its counts out.
 */

/** Colour kinds the shell's dots and pills use (see shell.css --shell-* tokens). */
export type DotKind = 'proved' | 'ready' | 'sorry' | 'error' | 'running' | 'planned' | 'idle';

export function nodeDotKind(status: BlueprintStatus | string | undefined): DotKind {
  switch (status) {
    case 'proved': return 'proved';
    case 'ready': return 'ready';
    case 'stated': return 'sorry';
    case 'failed': return 'error';
    default: return 'planned';
  }
}

export function sessionDotKind(state: string): DotKind {
  switch (state) {
    case 'running': return 'running';
    case 'proved': return 'proved';
    case 'failed': return 'error';
    case 'review':
    case 'disproved': return 'sorry';
    default: return 'idle';
  }
}

export function fileDotKind(status: string | undefined): DotKind {
  switch (status) {
    case 'proved': return 'proved';
    case 'sorry': return 'sorry';
    case 'error': return 'error';
    case 'ready': return 'ready';
    case 'running': return 'running';
    default: return 'planned';
  }
}

export function StatusDot({ kind, title }: { kind: DotKind; title?: string }) {
  return <span className={`sh-dot sh-dot-${kind}`} title={title} aria-hidden="true" />;
}

export function Pill({ kind, children, className = '' }: { kind: DotKind | 'neutral'; children: ReactNode; className?: string }) {
  return <span className={`sh-pill sh-pill-${kind} ${className}`}>{children}</span>;
}

export function RunningPill({ label = 'running' }: { label?: string }) {
  return (
    <Pill kind="running">
      <StatusDot kind="running" />
      {label}
    </Pill>
  );
}

/** The five-colour blueprint bar (proved · ready · sorry · failed · planned). */
export function BlueprintBar({ tally, className = '' }: { tally: ReturnType<typeof blueprintTally>; className?: string }) {
  const segments = blueprintBar(tally);
  const label = segments.length
    ? segments.map((s) => `${s.count} ${s.label}`).join(', ')
    : 'no blueprint yet';
  return (
    <div className={`sh-bar ${segments.length ? '' : 'sh-bar-empty'} ${className}`} role="img" aria-label={`Blueprint: ${label}`}>
      {segments.map((s) => (
        <span key={s.status} className={`sh-bar-${s.status}`} style={{ width: `${s.pct}%` }} title={`${s.count} ${s.label}`} />
      ))}
    </div>
  );
}

/** The legend under a progress bar: dot + count + label per status. */
export function BlueprintLegend({ tally }: { tally: ReturnType<typeof blueprintTally> }) {
  const entries: Array<[keyof typeof BLUEPRINT_LABELS, number]> = [
    ['proved', tally.proved],
    ['ready', tally.ready],
    ['stated', tally.stated],
    ['failed', tally.failed],
    ['planned', tally.planned],
  ];
  return (
    <>
      {entries
        .filter(([, count]) => count > 0)
        .map(([status, count]) => (
          <span key={status} className="sh-legend-item">
            <StatusDot kind={nodeDotKind(status)} />
            {count} {BLUEPRINT_LABELS[status]}
          </span>
        ))}
    </>
  );
}

/** Section heading in the shell's uppercase style, with an optional trailing slot. */
export function SectionHead({ children, trailing }: { children: ReactNode; trailing?: ReactNode }) {
  return (
    <div className="sh-h2">
      {children}
      {trailing !== undefined && <span className="sh-h2-trailing">{trailing}</span>}
    </div>
  );
}

/** Hidden-sidebar affordance for full-page shell views (mirrors ChatThread's). */
export function ExpandSidebarButton({ collapsed, onExpand }: { collapsed?: boolean; onExpand?: () => void }) {
  if (!collapsed || !onExpand) return null;
  return (
    <button className="sh-ibtn" onClick={onExpand} title="Show sidebar" aria-label="Show sidebar">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="3" y="3" width="18" height="18" rx="3" /><path d="M9 3v18" />
      </svg>
    </button>
  );
}
