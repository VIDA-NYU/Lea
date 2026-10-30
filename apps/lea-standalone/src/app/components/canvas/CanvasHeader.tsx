import { useState, type ReactNode } from 'react';
import { IconAlert, IconCheck, IconClose, IconMore, IconPlay, IconRail, IconShield } from './icons';

export type ChipKind = 'idle' | 'running' | 'error' | 'proved' | 'verified' | 'sorry' | 'unchecked';
export interface StatusChip {
  kind: ChipKind;
  label: string;
  line: number | null;
}
export interface MenuItem {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
}

function ChipIcon({ kind }: { kind: ChipKind }) {
  if (kind === 'error') return <IconAlert size={14} />;
  if (kind === 'verified') return <IconShield size={14} />;
  if (kind === 'proved') return <IconCheck size={14} />;
  return <span className="cv-chip-dot" aria-hidden="true" />;
}

// The 52px header: rail toggle, theorem name over the crumbs, the merged status
// chip, lean_check, SafeVerify, More, Hide canvas.
export function CanvasHeader({
  railAvailable,
  railOpen,
  onToggleRail,
  name,
  crumbs,
  filePicker,
  chip,
  onChipClick,
  check,
  verify,
  menu,
  onClose,
}: {
  railAvailable: boolean;
  railOpen: boolean;
  onToggleRail: () => void;
  name: string | null;
  crumbs: ReactNode;
  filePicker?: ReactNode;
  chip: StatusChip;
  onChipClick?: () => void;
  check: { enabled: boolean; title: string; onRun: () => void };
  verify: { enabled: boolean; busy: boolean; title: string; onRun: () => void; again?: boolean };
  menu: MenuItem[];
  onClose: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const chipBody = (
    <>
      <ChipIcon kind={chip.kind} />
      {chip.label}
    </>
  );
  return (
    <div className={`cv-head ${railAvailable ? 'with-rail' : ''}`}>
      {railAvailable && (
        <button
          type="button"
          className={`cv-ibtn ${railOpen ? 'on' : ''}`}
          aria-label={railOpen ? 'Hide files rail' : 'Show files rail'}
          aria-pressed={railOpen}
          onClick={onToggleRail}
        >
          <IconRail />
        </button>
      )}
      <div className="cv-title">
        <span className={`cv-name mono ${name ? '' : 'placeholder'}`} title={name ?? undefined}>
          {name ?? 'No theorem yet'}
        </span>
        <div className="cv-crumbs-row">
          {crumbs}
          {filePicker}
        </div>
      </div>
      <span className="cv-spacer" />
      {chip.kind !== 'idle' &&
        (onChipClick ? (
          <button
            type="button"
            className={`cv-chip ${chip.kind}`}
            onClick={onChipClick}
            title={chip.line ? `Show problems · line ${chip.line}` : 'Show details'}
          >
            {chipBody}
          </button>
        ) : (
          <span className={`cv-chip ${chip.kind}`}>{chipBody}</span>
        ))}
      <button type="button" className="cv-ibtn" aria-label="Run lean_check" title={check.title} disabled={!check.enabled} onClick={check.onRun}>
        <IconPlay />
      </button>
      <button
        type="button"
        className={`cv-ibtn ${verify.busy ? 'busy' : ''}`}
        aria-label={verify.again ? 'Run SafeVerify again' : 'Run SafeVerify'}
        title={verify.title}
        disabled={!verify.enabled || verify.busy}
        onClick={verify.onRun}
      >
        <IconShield />
      </button>
      <div className="cv-menu-anchor">
        <button
          type="button"
          className={`cv-ibtn ${menuOpen ? 'on' : ''}`}
          aria-label="More"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((o) => !o)}
        >
          <IconMore />
        </button>
        {menuOpen && (
          <>
            <div className="cv-menu-backdrop" onClick={() => setMenuOpen(false)} />
            <div className="cv-menu" role="menu">
              {menu.map((item) => (
                <button
                  type="button"
                  key={item.label}
                  role="menuitem"
                  className="cv-menu-item"
                  disabled={item.disabled}
                  onClick={() => {
                    setMenuOpen(false);
                    item.onSelect();
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </>
        )}
      </div>
      <button type="button" className="cv-ibtn" aria-label="Hide canvas" title="Hide canvas" onClick={onClose}>
        <IconClose />
      </button>
    </div>
  );
}
