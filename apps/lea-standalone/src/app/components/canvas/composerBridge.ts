// The canvas package may not edit ChatThread, so the empty-state cards reach the
// composer through this bridge: a DOM event the integrator can subscribe to
// (`lea:compose`, detail `{ text }`), plus a best-effort fallback that writes into
// the composer's textarea through the native value setter so React sees the
// change. Replace the fallback with a store field once ChatThread exposes one.
export function composeInChat(text: string, { focus = true }: { focus?: boolean } = {}): boolean {
  window.dispatchEvent(new CustomEvent('lea:compose', { detail: { text } }));
  const el = document.querySelector<HTMLTextAreaElement>('.chat .composer textarea');
  if (!el) return false;
  if (text) {
    const next = el.value.trim() ? `${el.value.replace(/\s+$/, '')}\n${text}` : text;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    if (setter) setter.call(el, next);
    else el.value = next;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  if (focus) el.focus();
  return true;
}
