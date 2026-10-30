// The canvas reaches the chat composer through a DOM event, `lea:compose` on
// window with detail `{ text }`. App owns the composer draft and listens: it
// appends the text to the draft (empty text only focuses the composer) and
// calls preventDefault() to say the composer took it.
export interface ComposeDetail {
  text: string;
}

/** Returns false when nothing took the text (no composer mounted). */
export function composeInChat(text: string): boolean {
  const event = new CustomEvent<ComposeDetail>('lea:compose', { detail: { text }, cancelable: true });
  return !window.dispatchEvent(event);
}
