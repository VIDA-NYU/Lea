import type { BlueprintStatus } from '../../lib/api';

// The status vocabulary the blueprint package paints with (docs/PLAN-ui-redesign.md
// §2): proved green · stated/sorry amber · failed red · running blue · planned grey
// outline · ready blue outline. A dot is never shown without a label next to it.
export type Tone = BlueprintStatus | 'running';

export function toneOf(node: { status?: BlueprintStatus | null } | null | undefined): Tone {
  return (node?.status ?? 'planned') as Tone;
}

export function StatusDot({ tone, title }: { tone: Tone; title?: string }) {
  return <span className={`bpv-dot is-${tone}`} aria-hidden={title ? undefined : true} title={title} />;
}

/** A pill: dot + text, coloured by tone. */
export function StatusPill({ tone, children, className = '' }: { tone: Tone; children: React.ReactNode; className?: string }) {
  return (
    <span className={`bpv-pill is-${tone} ${className}`.trim()}>
      <StatusDot tone={tone} />
      {children}
    </span>
  );
}
