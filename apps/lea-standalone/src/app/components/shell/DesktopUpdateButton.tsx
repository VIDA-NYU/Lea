import '../../../styles/shell.css';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useDesktop } from '../../stores/desktop';
import {
  isUpdateSkipped,
  readSkippedVersion,
  updateButtonLabel,
  writeSkippedVersion,
} from '../../lib/desktopUpdate.js';

// Desktop shell only (apps/lea-desktop; mockup frame 15). One button in the
// sidebar footer that follows the updater (stores/desktop, fed by App.tsx from
// the preload bridge):
//   available    "Update to x.y.z" downloads in the background — or, for a build
//                that cannot install in place, "Download Lea x.y.z" opens the DMG;
//   downloading  "Downloading… 43%";
//   ready        "Restart to update";
//   error        "Update failed · retry".
// A popover explains the three steps the first time a version shows up; Later
// keeps the button, Skip this version hides it for that version (localStorage).
// Renders nothing in a browser and when there is nothing to do.
export function DesktopUpdateButton() {
  const update = useDesktop((s) => s.update);
  const [skipped, setSkipped] = useState<string | null>(() => readSkippedVersion(storage()));
  // Version whose popover was dismissed (Later / the button itself) this launch.
  const [explained, setExplained] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const version = update?.version ?? null;
  const available = update?.state === 'available';
  const hidden = isUpdateSkipped(update, skipped);
  const label = updateButtonLabel(update);

  const dismiss = () => {
    setExplained(version);
    setOpen(false);
  };
  const act = () => {
    dismiss();
    window.leaDesktop?.installUpdate?.().catch(() => {});
  };
  const skip = () => {
    writeSkippedVersion(storage(), version);
    setSkipped(version);
    setOpen(false);
  };

  // The popover opens by itself once per version per launch, when the update
  // first shows up; it goes away with the available state.
  useEffect(() => {
    if (available && version && version !== explained && !hidden) setOpen(true);
    if (!available) setOpen(false);
  }, [available, version, explained, hidden]);

  // Escape and a click anywhere else close it (Later).
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismiss();
    };
    const onPointer = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) dismiss();
    };
    window.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onPointer);
    return () => {
      window.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onPointer);
    };
    // dismiss only closes over `version`, which is in the popover's own deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, version]);

  if (!update || !label || hidden) return null;

  const busy = update.state === 'downloading';
  const tone = update.state === 'ready' ? ' is-ready' : update.state === 'error' ? ' is-error' : busy ? ' is-busy' : '';
  return (
    <span className="desktop-update" ref={wrapRef}>
      <button
        ref={buttonRef}
        type="button"
        className={`desktop-update-btn${tone}`}
        onClick={act}
        disabled={busy}
        title={update.message}
        aria-describedby={open ? 'desktop-update-pop' : undefined}
      >
        {label}
      </button>
      {open && version && (
        <UpdatePopover anchor={buttonRef} update={update} version={version} onLater={dismiss} onSkip={skip} />
      )}
    </span>
  );
}

// Anchored above the button with fixed coordinates so it escapes the sidebar's
// overflow and survives whatever footer the shell package builds around it.
const POPOVER_WIDTH = 232;

function UpdatePopover({
  anchor,
  update,
  version,
  onLater,
  onSkip,
}: {
  anchor: React.RefObject<HTMLButtonElement | null>;
  update: LeaDesktopUpdateState;
  version: string;
  onLater: () => void;
  onSkip: () => void;
}) {
  const [pos, setPos] = useState<{ left: number; bottom: number; arrow: number } | null>(null);
  useLayoutEffect(() => {
    const place = () => {
      const rect = anchor.current?.getBoundingClientRect();
      if (!rect) return;
      const left = Math.max(8, Math.min(rect.right - POPOVER_WIDTH, window.innerWidth - POPOVER_WIDTH - 8));
      const arrow = Math.max(14, Math.min(POPOVER_WIDTH - 26, rect.left + rect.width / 2 - left - 6));
      setPos({ left, bottom: window.innerHeight - rect.top + 12, arrow });
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [anchor]);

  const notesUrl = update.notesUrl || update.releaseUrl;
  const openNotes = (event: React.MouseEvent) => {
    event.preventDefault();
    window.leaDesktop?.openExternal(notesUrl).catch(() => {});
  };
  return (
    <div
      id="desktop-update-pop"
      role="note"
      className="desktop-update-pop"
      style={pos ? { left: pos.left, bottom: pos.bottom } : { visibility: 'hidden' }}
    >
      <div className="desktop-update-pop-title">Lea {version} is available</div>
      <a className="desktop-update-pop-link" href={notesUrl} onClick={openNotes}>
        What&apos;s new →
      </a>
      {update.downloadOnly ? (
        <p className="desktop-update-pop-text">
          This build cannot update in place. <b>Download Lea {version}</b> opens the download for your Mac;
          drag it to Applications to replace this one. Your proofs and settings stay put.
        </p>
      ) : (
        <ol className="desktop-update-pop-steps">
          <li><b>Update to {version}</b> downloads in the background.</li>
          <li>The button becomes <b>Downloading… 43%</b>.</li>
          <li>Then <b>Restart to update</b>. One click; the backend and your data stay put.</li>
        </ol>
      )}
      {!update.downloadOnly && (
        <div className="desktop-update-pop-note">
          A build that cannot update in place (unsigned) opens the download instead and says so.
        </div>
      )}
      <div className="desktop-update-pop-actions">
        <button type="button" className="desktop-update-pop-btn" onClick={onLater}>Later</button>
        <button type="button" className="desktop-update-pop-btn is-quiet" onClick={onSkip}>Skip this version</button>
      </div>
      <span className="desktop-update-pop-arrow" aria-hidden="true" style={pos ? { left: pos.arrow } : undefined} />
    </div>
  );
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
