import { useEffect, useState } from 'react';
import { Bot, Plug, Sparkles } from 'lucide-react';
import { useFactories } from '../stores/factories';

// The project's Skills / MCP tab (v2.5 E0d).
//
// The Library pages answer "which projects use this skill/server?"; this answers the
// question a mathematician actually asks — "what does THIS project have?" — and it is
// the direction the two-tier model is really about: what you pick here applies to
// **every session in the project**, and a session can then add or drop items for
// itself (E0e).
//
// Both lists write through the SAME assignment endpoints the Library pages use
// (`project_ids` ± this project), so the two directions can never disagree — there is
// one join table and one writer.
//
// A GLOBAL item is shown as on-and-locked: it already applies everywhere, and letting
// the project un-tick it here would silently mean "make this no longer global", which
// is a Library-level decision with effects far outside this project.
export function SkillsMcpTab({ projectId }: { projectId: string }) {
  const skills = useFactories((s) => s.skills);
  const servers = useFactories((s) => s.mcpServers);
  const refreshSkills = useFactories((s) => s.refreshSkills);
  const refreshMcpServers = useFactories((s) => s.refreshMcpServers);
  const assignSkill = useFactories((s) => s.assignSkill);
  const assignMcpServer = useFactories((s) => s.assignMcpServer);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([refreshSkills(), refreshMcpServers()])
      .then(() => !cancelled && setError(null))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const toggle = async (
    item: { id: string; is_global: boolean; project_ids: string[] },
    assign: (id: string, a: { is_global: boolean; project_ids: string[] }) => Promise<unknown>,
  ) => {
    if (item.is_global || busyId) return;
    const on = item.project_ids.includes(projectId);
    const next = on
      ? item.project_ids.filter((id) => id !== projectId)
      : [...item.project_ids, projectId];
    setBusyId(item.id);
    setError(null);
    try {
      await assign(item.id, { is_global: false, project_ids: next });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  const row = (
    item: { id: string; name: string; is_global: boolean; project_ids: string[] },
    kind: 'skill' | 'server',
    extra?: string,
  ) => {
    const on = item.is_global || item.project_ids.includes(projectId);
    return (
      <label key={item.id} className={`smc-row ${item.is_global ? 'is-locked' : ''}`}>
        <input
          type="checkbox"
          checked={on}
          disabled={item.is_global || busyId === item.id}
          onChange={() => toggle(item, kind === 'skill' ? assignSkill : assignMcpServer)}
        />
        <span className="smc-name">{item.name}</span>
        {extra && <span className="smc-extra">{extra}</span>}
        {item.is_global && <span className="smc-badge">All projects</span>}
      </label>
    );
  };

  if (loading) return <div className="sf-muted" style={{ padding: '18px 28px' }}>Loading…</div>;

  return (
    <div className="smc-tab">
      <p className="smc-intro">
        What this project's proofs can use. Anything ticked here applies to{' '}
        <b>every session in the project</b>.
      </p>

      <div className="smc-group">
        <div className="smc-head">
          <Sparkles size={13} /> Skills
        </div>
        {skills.length === 0 ? (
          <div className="sf-muted">
            No skills yet — add one under Library → Skills.
          </div>
        ) : (
          skills.map((s) => row(s, 'skill'))
        )}
      </div>

      <div className="smc-group">
        <div className="smc-head">
          <Plug size={13} /> MCP servers
        </div>
        {servers.length === 0 ? (
          <div className="sf-muted">
            No MCP servers yet — add one under Library → MCP servers.
          </div>
        ) : (
          servers.map((s) => row(s, 'server', s.enabled ? undefined : 'turned off'))
        )}
      </div>

      <div className="smc-group">
        <div className="smc-head">
          <Bot size={13} /> Sub-agents
        </div>
        <div className="sf-muted">
          Sub-agent roles apply to every project. Tune them under Library → Sub-agents.
        </div>
      </div>

      {error && <div className="sf-detail-err">{error}</div>}
    </div>
  );
}
