import { useMemo, useState, type ReactNode } from 'react';
import type { ApprovalDecision, ApprovalRecord, CodeStep } from '../../lib/api';
import { buildInlineDiff, type DiffRow } from '../../lib/codeDiff';
import { checkExecutionMessage } from '../../lib/leanCheckRuntime.mjs';
import {
  approvalAsk,
  baseName,
  callResult,
  decisionLabel,
  formatElapsed,
  toolRowModel,
  toolTarget,
  type ToolCall,
  type ToolRowView,
} from '../../lib/chatModel.js';
import { ChatIcon } from './icons';
import { Elapsed } from './Elapsed';

/**
 * One compact row in the mockup's language: icon, verb, mono target, and the
 * result at the right. A live row swaps the icon for the blue dot and ticks;
 * a finished one shows its frozen duration next to the result. Clicking the
 * row expands it in place.
 */
export function ToolRow({
  view,
  state = 'done',
  startedAt = null,
  endedAt = null,
  approximate = false,
  meta,
  note,
  noteTone = 'error',
  ask,
  extra,
  active = false,
  expanded,
  onToggle,
  onOpen,
  title,
  children,
}: {
  view: ToolRowView;
  state?: string;
  startedAt?: number | null;
  endedAt?: number | null;
  /** The end is the run's next event, not a result: shown as an upper bound. */
  approximate?: boolean;
  meta?: ReactNode;
  note?: ReactNode;
  noteTone?: 'error' | 'muted' | 'running';
  ask?: ReactNode;
  extra?: ReactNode;
  active?: boolean;
  expanded: boolean;
  onToggle: () => void;
  /** When set, the row itself opens something (a session) and a chevron expands it. */
  onOpen?: () => void;
  title?: string;
  children?: ReactNode;
}) {
  const live = state === 'live';
  const duration =
    !live && startedAt != null && endedAt != null && endedAt >= startedAt ? (
      <span className="chat-tool-dur" title={approximate ? 'Ends at the next thing the run did; the tool itself may have finished sooner.' : undefined}>
        {view.result ? '· ' : ''}
        {approximate ? '≤ ' : ''}
        {formatElapsed(endedAt - startedAt, { fine: true })}
      </span>
    ) : null;
  const hasBody = Boolean(children);
  return (
    <div
      className={[
        'chat-tool',
        `tone-${view.tone}`,
        `state-${state}`,
        active ? 'active' : '',
        expanded && hasBody ? 'open' : '',
      ].join(' ')}
    >
      <div className="chat-tool-head">
        <button
          type="button"
          className="chat-tool-main"
          onClick={onOpen ?? onToggle}
          aria-expanded={onOpen || !hasBody ? undefined : expanded}
          title={title}
        >
          {live ? (
            <span className="chat-live-dot" aria-hidden="true" />
          ) : (
            <ChatIcon name={view.icon} className="chat-tool-ico" />
          )}
          <span className="chat-tool-verb">{view.verb}</span>
          {view.target && <span className="chat-tool-target">{view.target}</span>}
          <span className="chat-tool-gap" />
          {meta}
          {view.result && <span className={`chat-tool-result tone-${view.tone}`}>{view.result}</span>}
          {live && (
            <span className="chat-tool-clock">
              <span className="chat-sr">running for </span>
              <Elapsed since={startedAt} />
            </span>
          )}
          {duration}
        </button>
        {extra}
        {onOpen && hasBody && (
          <button
            type="button"
            className={`chat-tool-disclose ${expanded ? 'open' : ''}`}
            onClick={onToggle}
            aria-expanded={expanded}
            aria-label={expanded ? 'Hide details' : 'Show details'}
          >
            <ChatIcon name="chevron" size={12} />
          </button>
        )}
      </div>
      {note && <div className={`chat-tool-note ${noteTone}`}>{note}</div>}
      {ask}
      {expanded && hasBody && <div className="chat-tool-body">{children}</div>}
    </div>
  );
}

/** Approve / Always this session / Deny, with the "Lea wants to …" line. */
export function ApprovalAsk({
  approval,
  busy,
  onDecide,
}: {
  approval: ApprovalRecord;
  busy: boolean;
  onDecide: (decision: ApprovalDecision) => void;
}) {
  const preview = approvalPreview(approval);
  return (
    <>
      <div className="chat-tool-ask" role="group" aria-label={`Approval for ${approval.tool_name}`}>
        <span className="chat-tool-ask-text">{approvalAsk(approval)}</span>
        <span className="chat-tool-ask-actions">
          <button type="button" className="chat-btn pri" disabled={busy} onClick={() => onDecide('allow')}>
            Approve
          </button>
          <button type="button" className="chat-btn" disabled={busy} onClick={() => onDecide('always_session')}>
            Always this session
          </button>
          <button type="button" className="chat-btn danger" disabled={busy} onClick={() => onDecide('deny')}>
            Deny
          </button>
        </span>
      </div>
      {preview && <pre className="chat-tool-preview">{preview}</pre>}
    </>
  );
}

/** A tool call from the run stream, with whatever it produced. */
export function ToolCallRow({
  call,
  previousStep,
  codeIndexById,
  activeCodeIndex,
  onSelectStep,
  onDecide,
  approvalBusy,
}: {
  call: ToolCall;
  previousStep: CodeStep | null;
  codeIndexById: Map<string, number>;
  activeCodeIndex: number;
  onSelectStep: (codeIndex: number) => void;
  onDecide: (decision: ApprovalDecision) => void;
  approvalBusy: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const step = (call.step ?? null) as CodeStep | null;
  const checked = (call.checkedStep ?? null) as CodeStep | null;
  const fileStep = step ?? checked;
  const target = toolTarget(call.name, call.args) || (fileStep ? baseName(fileStep.path) : '');
  const view = toolRowModel({ name: call.name, args: call.args, target }, callResult(call, previousStep));
  const approval = (call.approval ?? null) as ApprovalRecord | null;
  const waiting = call.state === 'waiting' && approval;
  const codeIndex = fileStep ? codeIndexById.get(fileStep.id) : undefined;
  return (
    <ToolRow
      view={view}
      state={call.state}
      startedAt={call.startedAt}
      endedAt={call.synthetic ? null : call.endedAt}
      approximate={!call.exact}
      active={step != null && codeIndex === activeCodeIndex}
      note={view.firstError}
      ask={waiting ? <ApprovalAsk approval={approval} busy={approvalBusy} onDecide={onDecide} /> : null}
      expanded={expanded}
      onToggle={() => setExpanded((v) => !v)}
      title={fileStep?.path}
    >
      <CallDetail call={call} step={step} checked={checked} previousStep={previousStep} codeIndex={codeIndex} onSelectStep={onSelectStep} />
    </ToolRow>
  );
}

function CallDetail({
  call,
  step,
  checked,
  previousStep,
  codeIndex,
  onSelectStep,
}: {
  call: ToolCall;
  step: CodeStep | null;
  checked: CodeStep | null;
  previousStep: CodeStep | null;
  codeIndex: number | undefined;
  onSelectStep: (codeIndex: number) => void;
}) {
  const facts: string[] = [];
  if (call.turn != null) facts.push(`turn ${call.turn}`);
  if (call.startedAt != null) facts.push(`started ${clockTime(call.startedAt)}`);
  if (call.approval?.decision) facts.push(decisionLabel(call.approval.decision));
  if (call.state === 'queued') facts.push('waits for the call before it in this turn');
  if (call.state === 'stopped') facts.push('the run stopped before this call reported back');
  const args = call.args ?? null;
  const check = call.check;
  const execution = check?.execution;
  return (
    <>
      {facts.length > 0 && <div className="chat-detail-facts">{facts.join(' · ')}</div>}
      {step?.summary && <p className="chat-detail-intent">{step.summary}</p>}
      {check && (
        <div className="chat-detail-block">
          <div className="chat-detail-label">
            {execution ? checkExecutionMessage(execution, check.check_status) : check.check_status === 'ok' ? 'Lean check passed' : 'Lean check failed'}
          </div>
          {checked && (
            <div className="chat-detail-facts">
              newest snapshot at the time: <span className="chat-mono">{relPath(checked.path)}</span>
            </div>
          )}
          {check.check_detail && <pre className="chat-detail-pre">{check.check_detail}</pre>}
        </div>
      )}
      {step && (
        <div className="chat-detail-block">
          <div className="chat-detail-label">
            <span className="chat-mono">{relPath(step.path)}</span>
            {step.check_status === 'ok' ? ' · checked' : step.check_status === 'error' ? ' · check failed' : ' · not checked yet'}
            {step.content_lost ? ' · content could not be recovered' : ''}
          </div>
          <DiffPreview before={previousStep?.code ?? ''} after={step.code ?? ''} />
        </div>
      )}
      {!step && args && <ArgsPreview name={call.name} args={args} />}
      {!step && !check && !args && (
        <div className="chat-detail-facts">The run stream reports this tool's name only; its input and output are not recorded here.</div>
      )}
      {codeIndex != null && (
        <button type="button" className="chat-link" onClick={() => onSelectStep(codeIndex)}>
          Show this snapshot in the canvas →
        </button>
      )}
    </>
  );
}

/** A code snapshot no tool call claimed: your edit, or a file a command changed. */
export function StepRow({
  step,
  previousStep,
  codeIndex,
  active,
  onSelectStep,
}: {
  step: CodeStep;
  previousStep: CodeStep | null;
  codeIndex: number;
  active: boolean;
  onSelectStep: (codeIndex: number) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const base = toolRowModel(
    { name: step.author === 'user' ? 'edit_file' : 'write_file', target: baseName(step.path) },
    { code: step.code ?? '', previousCode: previousStep?.code ?? '' },
  );
  const view: ToolRowView = { ...base, verb: step.author === 'user' ? 'you edited' : 'snapshot' };
  return (
    <ToolRow
      view={view}
      active={active}
      expanded={expanded}
      onToggle={() => setExpanded((v) => !v)}
      title={step.path}
      note={step.check_status === 'error' ? step.check_detail : null}
    >
      {step.summary && <p className="chat-detail-intent">{step.summary}</p>}
      <div className="chat-detail-block">
        <div className="chat-detail-label">
          <span className="chat-mono">{relPath(step.path)}</span>
          {step.check_status === 'ok' ? ' · checked' : step.check_status === 'error' ? ' · check failed' : ' · not checked yet'}
        </div>
        <DiffPreview before={previousStep?.code ?? ''} after={step.code ?? ''} />
      </div>
      <button type="button" className="chat-link" onClick={() => onSelectStep(codeIndex)}>
        Show this snapshot in the canvas →
      </button>
    </ToolRow>
  );
}

/** An approval that no tool_call status claimed (an older run, a lost event). */
export function ApprovalRow({
  approval,
  busy,
  onDecide,
}: {
  approval: ApprovalRecord;
  busy: boolean;
  onDecide: (decision: ApprovalDecision) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const pending = !approval.decision && approval.actionable !== false;
  const view = toolRowModel(
    { name: approval.tool_name, args: approval.args },
    pending
      ? { waiting: true }
      : approval.decision === 'deny'
        ? { denied: true }
        : { label: approval.decision ? decisionLabel(approval.decision) : 'no longer pending', tone: 'muted' },
  );
  const preview = approvalPreview(approval);
  return (
    <ToolRow
      view={view}
      state={pending ? 'waiting' : 'done'}
      ask={pending ? <ApprovalAsk approval={approval} busy={busy} onDecide={onDecide} /> : null}
      expanded={expanded}
      onToggle={() => setExpanded((v) => !v)}
    >
      {!pending && preview ? <pre className="chat-tool-preview">{preview}</pre> : null}
    </ToolRow>
  );
}

function ArgsPreview({ name, args }: { name: string; args: Record<string, unknown> }) {
  const text = approvalPreview({ tool_name: name, args });
  if (!text) return null;
  return <pre className="chat-detail-pre">{text}</pre>;
}

const CONTEXT = 2;
const MAX_ROWS = 80;

/** The changed lines of a snapshot with a little context, capped. */
function DiffPreview({ before, after }: { before: string; after: string }) {
  const rows = useMemo(() => {
    if (!before) {
      const lines = after.split('\n');
      if (lines[lines.length - 1] === '') lines.pop();
      return lines.map((line, i) => ({ kind: 'added', line, newLineNumber: i + 1 }) as DiffRow);
    }
    const all = buildInlineDiff(before, after);
    const keep = new Set<number>();
    all.forEach((r, i) => {
      if (r.kind === 'unchanged') return;
      for (let j = Math.max(0, i - CONTEXT); j <= Math.min(all.length - 1, i + CONTEXT); j += 1) keep.add(j);
    });
    const out: (DiffRow | null)[] = [];
    let last = -1;
    for (const i of [...keep].sort((a, b) => a - b)) {
      if (last >= 0 && i > last + 1) out.push(null);
      out.push(all[i]);
      last = i;
    }
    return out;
  }, [before, after]);
  if (!rows.length) return <div className="chat-detail-facts">No change from the previous snapshot.</div>;
  const shown = rows.slice(0, MAX_ROWS);
  return (
    <div className="chat-diff" role="group" aria-label="Changes in this snapshot">
      {shown.map((r, i) =>
        r === null ? (
          <div key={i} className="chat-diff-gap">⋯</div>
        ) : (
          <div key={i} className={`chat-diff-row ${r.kind}`}>
            <span className="chat-diff-sign" aria-hidden="true">{r.kind === 'added' ? '+' : r.kind === 'removed' ? '−' : ' '}</span>
            <span className="chat-sr">{r.kind === 'added' ? 'added: ' : r.kind === 'removed' ? 'removed: ' : ''}</span>
            {r.line || ' '}
          </div>
        ),
      )}
      {rows.length > MAX_ROWS && <div className="chat-diff-gap">… {rows.length - MAX_ROWS} more lines in the canvas</div>}
    </div>
  );
}

/** Proof paths relative to the session repo (everything after `proofs/<session>/`). */
export function relPath(p: string): string {
  const m = p.match(/[/\\]proofs[/\\][^/\\]+[/\\](.+)$/);
  return m ? m[1].replace(/\\/g, '/') : p;
}

export function approvalPreview(approval: { tool_name: string; args?: Record<string, unknown> | null }): string {
  const args = approval.args || {};
  if (approval.tool_name === 'bash' && typeof args.command === 'string') return args.command;
  const path = typeof args.path === 'string' ? relPath(args.path) : '';
  const content =
    typeof args.content === 'string'
      ? args.content
      : typeof args.new_string === 'string'
        ? args.new_string
        : '';
  if (path || content) return [path, content].filter(Boolean).join('\n\n');
  return Object.keys(args).length ? JSON.stringify(args, null, 2) : '';
}

function clockTime(ms: number): string {
  try {
    return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return '';
  }
}
