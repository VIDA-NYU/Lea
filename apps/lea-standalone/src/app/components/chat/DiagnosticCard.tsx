import type { Diagnostic, DiagnosticAction } from '../../lib/api';
import { checkExecutionMessage } from '../../lib/leanCheckRuntime.mjs';

// v2.4 · one failure, rendered where it happened.
//
// The shape is deliberately uniform across every severity and every source (prover,
// adapter, sub-agent): a user learning to read one of these has learned to read all
// of them. `title` and `remedy` come from the adapter's code catalog, so the copy is
// not whatever an exception happened to stringify to — and `remedy` is simply absent
// when there is no honest advice to give, rather than padded with a guess.
const DIAGNOSTIC_LABEL: Record<string, string> = {
  fatal: 'Run failed',
  step_error: 'Step failed',
  degraded: 'Reduced capability',
  notice: 'Notice',
};

export function DiagnosticCard({
  diagnostic,
  onAction,
}: {
  diagnostic: Diagnostic;
  // One handler for every offer on the card; the card doesn't decide where an action
  // goes, it just reports which one was pressed.
  onAction?: (action: DiagnosticAction) => void;
}) {
  const { severity, title, detail, remedy, actions, context, code } = diagnostic;
  const message = code === 'lean.check_execution'
    ? checkExecutionMessage(context?.execution, context?.check_status) || diagnostic.message
    : diagnostic.message;
  const anchor = context?.path || context?.tool;
  return (
    <div className={`diag diag-${severity}`} data-code={code}>
      <div className="diag-head">
        <span className="diag-sev">{DIAGNOSTIC_LABEL[severity] ?? 'Notice'}</span>
        <span className="diag-title">{title}</span>
        {anchor && <span className="diag-anchor">{anchor}</span>}
      </div>
      {message && message !== title && <pre className="diag-msg">{message}</pre>}
      {/* The raw exception, folded away. Present for anyone who needs it, never the
          first thing read — `BadRequestError: litellm.BadRequestError: …{json}` is
          noise when the provider already said "your api key is invalid". */}
      {detail && (
        <details className="diag-detail">
          <summary>Technical detail</summary>
          <pre>{detail}</pre>
        </details>
      )}
      {remedy && <div className="diag-remedy">{remedy}</div>}
      {/* Where a failure has more than one plausible cause, offer every route rather
          than picking one — an auth rejection cannot tell a wrong key from a model
          belonging to another provider, and guessing sends people to fix the wrong
          thing. */}
      {actions && actions.length > 0 && onAction && (
        <div className="diag-actions">
          {actions.map((a) => (
            <button
              key={`${a.action}:${a.focus ?? ''}`}
              className="diag-action"
              onClick={() => onAction(a)}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
      {diagnostic.persisted === false && (
        // Honesty about our own failure: this one is on screen but was not written,
        // so it will not survive a reload. Better to say so than to let someone
        // trust a record that isn't there.
        <div className="diag-unsaved">Not saved — this notice will disappear on reload.</div>
      )}
    </div>
  );
}
