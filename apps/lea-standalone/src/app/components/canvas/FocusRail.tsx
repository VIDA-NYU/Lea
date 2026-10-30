import { IconChat, IconChevronRight } from './icons';

// Focus mode (mockup frame 4): the chat collapses to this 56px rail at the left
// edge of the canvas. The chat itself is hidden by `.main-area.focus` (App sets
// the class; canvas.css), so its state survives; this rail is the way back.
export function FocusRail({
  title,
  unread,
  onExit,
}: {
  title?: string;
  unread: number;
  onExit: () => void;
}) {
  return (
    <aside className="cv-focus-rail" aria-label="Chat, collapsed">
      <button
        type="button"
        className="cv-ibtn boxed"
        aria-label={unread > 0 ? `Expand chat, ${unread} new ${unread === 1 ? 'message' : 'messages'}` : 'Expand chat'}
        onClick={onExit}
      >
        <IconChat />
        {unread > 0 && <span className="cv-badge">{unread > 99 ? '99+' : unread}</span>}
      </button>
      {title ? <span className="cv-focus-title">{title}</span> : null}
      <span className="cv-spacer" />
      <button type="button" className="cv-ibtn" aria-label="Exit focus mode" title="Exit focus mode" onClick={onExit}>
        <IconChevronRight />
      </button>
    </aside>
  );
}
