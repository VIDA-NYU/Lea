import '../../../styles/files.css';
import { useWorkbench } from '../../stores/workbench';

// Work package "files" (mockup frame 7). ⌘P palette: fuzzy match over file
// names, then declarations, then other projects; prefixes `#` declarations,
// `>` commands, `:` line. App mounts it once; it renders nothing while closed.
export function QuickOpen({ projectId }: { projectId: string | null }) {
  const open = useWorkbench((s) => s.quickOpenOpen);
  const setOpen = useWorkbench((s) => s.setQuickOpenOpen);
  if (!open) return null;
  return (
    <div className="quick-open-backdrop" onClick={() => setOpen(false)}>
      <div className="quick-open" role="dialog" aria-label="Go to file or declaration" onClick={(e) => e.stopPropagation()}>
        <div className="quick-open-stub">Quick open (project {projectId ?? 'none'}) — not built yet</div>
      </div>
    </div>
  );
}
