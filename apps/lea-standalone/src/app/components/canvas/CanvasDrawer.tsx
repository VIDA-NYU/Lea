import type { SafeVerifyStatus } from '../../lib/api';
import type { DrawerTab } from '../../stores/workbench';
import { IconCheck, IconChevronDown, IconChevronUp } from './icons';

export interface Problem {
  path?: string | null;
  line: number | null;
  column: number | null;
  severity: 'error' | 'warning' | 'info';
  message: string;
  synthetic?: boolean;
}

export interface VerifyView {
  status: SafeVerifyStatus;
  detail?: string | null;
  current?: boolean;
}

// The drawer under the editor: a 40px bar with Goals / Problems / Verify tabs
// (from useWorkbench().drawerTab) and a one-line summary, expandable to the tab
// body. Problems jump to a line; Verify shows the SafeVerify verdict and audit.
export function CanvasDrawer({
  open,
  onToggle,
  tab,
  onTab,
  summary,
  meta,
  problems,
  onJump,
  goals,
  verify,
}: {
  open: boolean;
  onToggle: () => void;
  tab: DrawerTab;
  onTab: (tab: DrawerTab) => void;
  summary: string;
  meta?: string | null;
  problems: Problem[];
  onJump?: (line: number) => void;
  goals: { text: string | null; hint: string; canAsk: boolean; onAsk: () => void; note?: string | null };
  verify: {
    result: VerifyView | null;
    canRun: boolean;
    busy: boolean;
    onRun: () => void;
    disabledReason?: string | null;
    note?: string | null;
  };
}) {
  const errors = problems.filter((p) => p.severity === 'error').length;
  const pick = (next: DrawerTab) => {
    onTab(next);
    if (!open || next === tab) onToggle();
  };
  const verified = verify.result?.status === 'ok' && verify.result.current !== false;
  return (
    <div className={`cv-drawer ${open ? 'open' : ''}`}>
      <div className="cv-drawer-bar">
        <button type="button" className={`cv-tab ${open && tab === 'goals' ? 'on' : ''}`} onClick={() => pick('goals')} aria-pressed={open && tab === 'goals'}>
          Goals
        </button>
        <button type="button" className={`cv-tab ${open && tab === 'problems' ? 'on' : ''}`} onClick={() => pick('problems')} aria-pressed={open && tab === 'problems'}>
          Problems
          {problems.length > 0 && (
            <span
              className={`cv-count ${errors > 0 ? 'error' : 'warn'}`}
              aria-label={`${problems.length} ${problems.length === 1 ? 'problem' : 'problems'}${errors > 0 ? `, ${errors} ${errors === 1 ? 'error' : 'errors'}` : ''}`}
            >
              {problems.length}
            </span>
          )}
        </button>
        <button type="button" className={`cv-tab ${open && tab === 'verify' ? 'on' : ''}`} onClick={() => pick('verify')} aria-pressed={open && tab === 'verify'}>
          Verify
          {verified && <IconCheck size={13} className="cv-tab-check" aria-label="verified" />}
        </button>
        {!open && summary ? (
          <>
            <span className="cv-vsep" aria-hidden="true" />
            <span className="cv-drawer-summary mono">{summary}</span>
          </>
        ) : null}
        <span className="cv-spacer" />
        {meta ? <span className="cv-drawer-meta">{meta}</span> : null}
        <button type="button" className="cv-ibtn sm" aria-label={open ? 'Collapse drawer' : 'Expand drawer'} aria-expanded={open} onClick={onToggle}>
          {open ? <IconChevronDown /> : <IconChevronUp />}
        </button>
      </div>
      {open && (
        <div className="cv-drawer-body">
          {tab === 'problems' && (
            problems.length === 0 ? (
              <div className="cv-drawer-empty">No problems reported.</div>
            ) : (
              <div className="cv-problems">
                {problems.map((p, i) => {
                  const [first, ...rest] = String(p.message ?? '').split('\n');
                  return (
                    <div key={i} className={`cv-prob ${p.severity}`}>
                      <span className="cv-prob-pos mono">
                        {p.line != null ? `${p.line}:${p.column ?? 1}` : '—'}
                      </span>
                      <div className="cv-prob-body">
                        <span className="cv-prob-msg">
                          <span className="cv-prob-sev">{p.severity}</span> {first}
                        </span>
                        {rest.length > 0 && <pre className="cv-prob-ctx mono">{rest.join('\n')}</pre>}
                      </div>
                      {p.line != null && onJump ? (
                        <button type="button" className="cv-link" onClick={() => onJump(p.line as number)}>
                          Jump to line
                        </button>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )
          )}
          {tab === 'goals' && (
            <div className="cv-goals">
              {goals.text ? <pre className="cv-goals-text mono">{goals.text}</pre> : <div className="cv-drawer-empty">{goals.hint}</div>}
              <div className="cv-goals-actions">
                {goals.canAsk && (
                  <button type="button" className="cv-btn" onClick={goals.onAsk} title="Grab the goal state at the cursor and ask Lea about it">
                    Ask Lea about this goal
                  </button>
                )}
                {goals.note ? <span className="cv-note-inline">{goals.note}</span> : null}
              </div>
            </div>
          )}
          {tab === 'verify' && (
            <div className="cv-verify">
              <div className="cv-kv">
                <span className="k">SafeVerify</span>
                <span className={`v cv-verify-status ${verify.result ? verify.result.status : 'none'}`}>
                  {verify.result
                    ? verify.result.status === 'ok'
                      ? verify.result.current === false
                        ? 'passed · recheck required'
                        : 'passed'
                      : verify.result.status === 'running'
                        ? 'running…'
                        : verify.result.status
                    : 'not run on this step'}
                </span>
                <span className="k">What it checks</span>
                <span className="v">kernel replay · declaration type and body match · axiom whitelist (no sorryAx, no native_decide)</span>
                {verify.note ? (
                  <>
                    <span className="k">Step</span>
                    <span className="v">{verify.note}</span>
                  </>
                ) : null}
              </div>
              <div className="cv-verify-side">
                {verify.result?.detail ? (
                  <pre className="cv-audit mono">{verify.result.detail}</pre>
                ) : (
                  <div className="cv-drawer-empty">{verify.result ? 'No audit output.' : 'Run SafeVerify to audit the saved proof.'}</div>
                )}
                <div className="cv-verify-actions">
                  <button
                    type="button"
                    className="cv-btn primary"
                    onClick={verify.onRun}
                    disabled={!verify.canRun || verify.busy}
                    title={verify.disabledReason ?? 'Kernel-audit the saved proof (SafeVerify)'}
                  >
                    {verify.busy ? 'SafeVerify…' : verify.result ? 'Run SafeVerify again' : 'Run SafeVerify'}
                  </button>
                  {!verify.canRun && verify.disabledReason ? <span className="cv-note-inline">{verify.disabledReason}</span> : null}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// The drawer bar with nothing behind it (mockup frame 1): dimmed tabs and a note.
export function DrawerPlaceholder({ note }: { note: string }) {
  return (
    <div className="cv-drawer placeholder" aria-disabled="true">
      <div className="cv-drawer-bar">
        <span className="cv-tab">Goals</span>
        <span className="cv-tab">Problems</span>
        <span className="cv-tab">Verify</span>
        <span className="cv-spacer" />
        <span className="cv-drawer-meta">{note}</span>
      </div>
    </div>
  );
}
