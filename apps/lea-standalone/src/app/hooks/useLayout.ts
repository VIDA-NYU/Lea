import { useEffect, useRef, useState } from 'react';

export type View =
  | 'main'
  | 'stats'
  | 'settings'
  | 'project'
  | 'skills'
  | 'subagents'
  | 'mcp'
  | 'tools'
  | 'projects-hub';

/**
 * useLayout — the app's view/render UI state (v2.0.1 R5).
 *
 * Owns which page is showing, the collapsible sidebar (M27) + collapsible canvas,
 * and the draggable chat|canvas divider (M26), plus their localStorage
 * persistence and the drag listeners. Pure presentation state — App reads it back
 * for the shell layout and passes the two collapse flags down where needed.
 */
export function useLayout() {
  const [view, setView] = useState<View>('main');
  const [canvasCollapsed, setCanvasCollapsed] = useState(false);
  // Collapsible sidebar (M27) + draggable canvas width (M26), both remembered.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.localStorage.getItem('lea:sidebarCollapsed') === '1',
  );
  const [canvasWidth, setCanvasWidth] = useState(() => {
    const v = Number(window.localStorage.getItem('lea:canvasWidth'));
    return v >= 25 && v <= 70 ? v : 46;
  });
  const [dragging, setDragging] = useState(false);
  const mainAreaRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    window.localStorage.setItem('lea:sidebarCollapsed', sidebarCollapsed ? '1' : '0');
  }, [sidebarCollapsed]);
  useEffect(() => {
    window.localStorage.setItem('lea:canvasWidth', String(canvasWidth));
  }, [canvasWidth]);

  // Drag the chat|canvas divider to resize the canvas (M26).
  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const el = mainAreaRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const pct = ((rect.right - e.clientX) / rect.width) * 100;
      setCanvasWidth(Math.min(70, Math.max(25, pct)));
    };
    const onUp = () => setDragging(false);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [dragging]);

  return {
    view,
    setView,
    canvasCollapsed,
    setCanvasCollapsed,
    sidebarCollapsed,
    setSidebarCollapsed,
    canvasWidth,
    dragging,
    setDragging,
    mainAreaRef,
  };
}
