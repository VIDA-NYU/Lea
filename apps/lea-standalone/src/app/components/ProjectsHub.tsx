import { useEffect, useMemo } from 'react';
import { ChevronLeft, Plus } from 'lucide-react';
import { useProjects } from '../stores/projects';

// D7: the Projects hub — a full-page card grid of every project (Claude-web style),
// opened by clicking the sidebar's "Projects" header (which itself shows only the 3
// most-recently-updated). Reuses the same view-switch + openProjectWindow the sidebar
// rows use; there is no live SSE feed for projects, so refresh on mount for fresh
// `updated_at` ordering.
export function ProjectsHub({
  onBack,
  onOpenProject,
  onNewProject,
}: {
  onBack: () => void;
  onOpenProject: (id: string) => void;
  onNewProject: () => void;
}) {
  const projects = useProjects((s) => s.projects);
  const refreshProjects = useProjects((s) => s.refreshProjects);

  useEffect(() => {
    refreshProjects().catch(() => {});
  }, [refreshProjects]);

  const sorted = useMemo(
    () => [...projects].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)),
    [projects],
  );

  return (
    <div className="lea-app">
      <div className="project-window ph">
        <div className="pw-bar">
          <button className="pw-back" onClick={onBack}>
            <ChevronLeft size={15} /> Chats
          </button>
          <span className="pw-crumbs">/ Projects</span>
        </div>

        <div className="pw-hero ph-hero">
          <div>
            <h1 className="pw-title">Projects</h1>
            <p className="pw-sub">
              {projects.length} project{projects.length === 1 ? '' : 's'} · newest first
            </p>
          </div>
          <button className="ph-new" onClick={onNewProject}>
            <Plus size={14} /> New project
          </button>
        </div>

        {sorted.length === 0 ? (
          <div className="ph-empty">
            No projects yet. Create one to group related proofs under a shared namespace.
          </div>
        ) : (
          <div className="ph-grid">
            {sorted.map((project) => (
              <button key={project.id} className="ph-card" onClick={() => onOpenProject(project.id)}>
                <div className="ph-card-top">
                  <span className="ph-card-icon">∑</span>
                  {typeof project.session_count === 'number' && project.session_count > 0 && (
                    <span className="ph-card-count">
                      {project.session_count} chat{project.session_count === 1 ? '' : 's'}
                    </span>
                  )}
                </div>
                <div className="ph-card-title">{project.title}</div>
                {project.description && <div className="ph-card-desc">{project.description}</div>}
                <div className="ph-card-meta">
                  <span className="ph-card-ns">{project.namespace}</span>
                  <span className="ph-card-when">{relativeTime(project.updated_at)}</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Compact "2h ago" / "3d ago" / a date for older, from an ISO timestamp.
function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
