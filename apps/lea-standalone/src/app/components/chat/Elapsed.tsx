import { useSyncExternalStore } from 'react';
import { formatElapsed } from '../../lib/chatModel.js';

// One shared once-a-second clock for every live counter in the chat, so the
// counters tick together and only the counters re-render (not the thread,
// whose Markdown is expensive to redo every second).
const listeners = new Set<() => void>();
let now = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (!timer) {
    now = Date.now();
    timer = setInterval(() => {
      now = Date.now();
      for (const l of listeners) l();
    }, 1000);
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
}

// Stable between two reads in one render (React compares them), and refreshed
// when nothing is ticking so a first render never shows a stale clock.
const read = () => {
  if (!timer && Date.now() - now >= 1000) now = Date.now();
  return now;
};
const idle = () => () => {};

/** The shared clock while `active`; a reading that does not tick otherwise. */
export function useNow(active = true): number {
  return useSyncExternalStore(active ? subscribe : idle, read, read);
}

/**
 * A duration: ticking from `since` while `until` is null, frozen otherwise.
 * Renders nothing without a start.
 */
export function Elapsed({
  since,
  until = null,
  fine = false,
  prefix = '',
  className,
}: {
  since: number | null | undefined;
  until?: number | null;
  fine?: boolean;
  prefix?: string;
  className?: string;
}) {
  const ticking = since != null && until == null;
  const current = useNow(ticking);
  if (since == null) return null;
  const end = until ?? Math.max(current, since);
  return (
    <span className={className} aria-live={ticking ? 'off' : undefined}>
      {prefix}
      {formatElapsed(end - since, { fine })}
    </span>
  );
}
