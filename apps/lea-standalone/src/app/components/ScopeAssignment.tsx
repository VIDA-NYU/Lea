import type { Project } from '../lib/api';

// The shared scope control (v2.1.1 F11, D47/D55) — a Global toggle plus a project
// multi-select, emitting `{ is_global, project_ids }`. "Global" applies a unit to
// every project; otherwise it applies only to the checked projects. Reused by the
// Skill Factory now and the (deferred) MCP Factory later, so it's unit-agnostic.
export interface Scope {
  is_global: boolean;
  project_ids: string[];
}

export function ScopeAssignment({
  value,
  projects,
  onChange,
  disabled = false,
}: {
  value: Scope;
  projects: Project[];
  onChange: (next: Scope) => void;
  disabled?: boolean;
}) {
  const toggleProject = (id: string) => {
    const has = value.project_ids.includes(id);
    onChange({
      is_global: value.is_global,
      project_ids: has ? value.project_ids.filter((p) => p !== id) : [...value.project_ids, id],
    });
  };

  const nowhere = !value.is_global && value.project_ids.length === 0;

  return (
    <div className="scope">
      {/* The positive assertion, at the Library layer: nothing raises when a capability
          resolves nowhere, so something has to say so. Same reasoning as G3's run-time
          assertions — an absence has no exception to catch. */}
      {nowhere && (
        <div className="scope-nowhere">
          Not in use yet — pick <b>All projects</b> or at least one project below, or Lea
          will never load this.
        </div>
      )}
      <label className={`scope-global ${disabled ? 'is-disabled' : ''}`}>
        <input
          type="checkbox"
          checked={value.is_global}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, is_global: e.target.checked })}
        />
        <span className="scope-global-text">
          <b>All projects</b>
          <span className="scope-hint">Apply to every project (and future ones).</span>
        </span>
      </label>

      {!value.is_global && (
        <div className="scope-projects">
          <div className="scope-projects-label">…or pick specific projects</div>
          {projects.length === 0 ? (
            <div className="scope-empty">No projects yet.</div>
          ) : (
            <div className="scope-list">
              {projects.map((p) => (
                <label key={p.id} className="scope-proj">
                  <input
                    type="checkbox"
                    checked={value.project_ids.includes(p.id)}
                    disabled={disabled}
                    onChange={() => toggleProject(p.id)}
                  />
                  <span className="scope-proj-name">{p.title}</span>
                </label>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// A compact badge summarizing a skill's scope, for the catalog list.
export function ScopeBadge({ isGlobal, count }: { isGlobal: boolean; count: number }) {
  if (isGlobal) return <span className="scope-badge is-global">All projects</span>;
  if (count > 0) return <span className="scope-badge">{count} project{count === 1 ? '' : 's'}</span>;
  // "Unassigned" was accurate and useless: styled muted, it read as neutral metadata
  // rather than as "this does nothing". Three separate items — a skill, an MCP server and
  // a tool — were each installed, left unscoped, and silently never used, with the person
  // testing them unable to tell why. The badge now states the CONSEQUENCE, in amber.
  return <span className="scope-badge is-none">Not in use</span>;
}
