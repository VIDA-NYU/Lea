import { useMemo } from 'react';
import { formatElapsed, userBubbleModel, type composerStatus } from '../../lib/chatModel.js';
import { Elapsed, useNow } from './Elapsed';
import { ChatIcon } from './icons';

/**
 * "Thinking · 1m 23s": the model is working and nothing is visible yet. With
 * `quietMs` it waits that long since `since` before showing, which is how a
 * streamed message that has stopped growing (the model is now writing a long
 * tool call) hands over to this row.
 */
export function ThinkingRow({
  since,
  quietMs = 0,
  label = 'Thinking',
}: {
  since: number | null;
  quietMs?: number;
  label?: string;
}) {
  const now = useNow(since != null);
  if (since == null) {
    return (
      <div className="chat-thinking">
        <span className="chat-live-dot" aria-hidden="true" />
        <span role="status">{label}</span>
      </div>
    );
  }
  if (quietMs && now - since < quietMs) return null;
  return (
    <div className="chat-thinking">
      <span className="chat-live-dot" aria-hidden="true" />
      <span>
        <span role="status">{label}</span>
        <span className="chat-thinking-time" aria-live="off">
          {' · '}
          {formatElapsed(Math.max(0, now - since))}
        </span>
      </span>
    </div>
  );
}

/** The composer's status line: a dot, the label, and the run's elapsed time. */
export function ComposerStatus({ status }: { status: ReturnType<typeof composerStatus> }) {
  if (!status) return null;
  return (
    <span className={`chat-status tone-${status.tone}`}>
      <span className="chat-status-dot" aria-hidden="true" />
      <span role="status" className="chat-status-label" title={status.label}>{status.label}</span>
      {status.since != null && (
        <span className="chat-status-time" aria-live="off">
          <span aria-hidden="true">· </span>
          <Elapsed since={status.since} />
        </span>
      )}
    </span>
  );
}

/**
 * A user message: the dark bubble, a leading slash command as a mono chip in
 * it, and the file App attached as context as a chip under it.
 */
export function UserBubble({ content }: { content: string }) {
  const model = useMemo(() => userBubbleModel(content), [content]);
  const hasText = Boolean(model.command || model.body);
  return (
    <div className="chat-user">
      <span className="chat-sr">You said:</span>
      {hasText && (
        <div className="chat-user-bubble">
          {model.command && <span className="chat-cmd">{model.command}</span>}
          {model.body && <span className="chat-user-text">{model.body}</span>}
        </div>
      )}
      {model.prompt && (
        <details className="chat-user-prompt">
          <summary>Prompt sent to Lea</summary>
          <pre>{model.prompt}</pre>
        </details>
      )}
      {model.context && (
        <span className="chat-user-context" title={model.context.path}>
          <ChatIcon name="file" size={12} />
          <span className="chat-mono">{model.context.chip}</span>
          <span className="chat-user-context-note">{model.context.note}</span>
        </span>
      )}
    </div>
  );
}

/** The Lea mark that heads each block of assistant output. */
export function LeaAvatar() {
  return (
    <span className="chat-avatar" aria-hidden="true">
      L
    </span>
  );
}
