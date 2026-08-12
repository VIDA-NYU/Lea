import { useEffect, useState } from 'react';
import { Plug, Sparkles, X } from 'lucide-react';
import {
  getSessionSkillsMcp,
  setSessionSkillMcp,
  type SessionSkillsMcp,
  type SkillMcpItem,
} from '../lib/api';

// The `/skills` and `/mcp` picker (v2.5 E0e) — the session tier of the two-tier model.
//
// Shows the WHOLE library with the project's picks pre-ticked, and lets this session add
// or drop items for itself. What is stored is the DIFF, so a later project-level change
// still reaches this session — the picker never writes an absolute list.
//
// A GLOBAL item is on-and-locked: un-ticking it here would mean "stop being global",
// which belongs in the Library, not in one conversation.
//
// Changes apply from the NEXT message, because MCP servers are acquired when a run
// starts. Saying so is the whole reason for the footer note — silence would read as
// "nothing happened".

const SOURCE_LABEL: Record<string, string> = {
  global: 'every project',
  project: 'this project',
  session: 'added here',
  'session-off': 'off for this chat',
};

export function SkillsMcpPicker({
  kind,
  sessionId,
  onClose,
}: {
  kind: 'skills' | 'mcp';
  sessionId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<SessionSkillsMcp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSessionSkillsMcp(sessionId)
      .then((d) => !cancelled && setData(d))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)));
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const items = data ? (kind === 'skills' ? data.skills : data.mcp_servers) : [];
  const itemKind = kind === 'skills' ? 'skill' : 'mcp_server';

  const toggle = async (item: SkillMcpItem) => {
    if (item.locked || busyId) return;
    // Clearing the override (null) is what "put it back how the project has it" means, so
    // re-ticking a project item doesn't leave a redundant 'add' behind.
    const action =
      item.source === 'session' ? null
      : item.source === 'session-off' ? null
      : item.on ? 'remove'
      : 'add';
    setBusyId(item.id);
    setError(null);
    try {
      setData(await setSessionSkillMcp(sessionId, { kind: itemKind, item_id: item.id, action }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="smc-modal-backdrop" onClick={onClose}>
      <div className="cappick" onClick={(e) => e.stopPropagation()}>
        <div className="smc-modal-head">
          {kind === 'skills' ? <Sparkles size={14} /> : <Plug size={14} />}
          <span className="smc-modal-title">
            {kind === 'skills' ? 'Skills for this conversation' : 'MCP servers for this conversation'}
          </span>
          <button className="smc-modal-x" onClick={onClose} aria-label="Close">
            <X size={14} />
          </button>
        </div>

        <div className="smc-modal-body">
          {error && <div className="sf-detail-err">{error}</div>}
          {!data ? (
            <div className="sf-muted">Loading…</div>
          ) : items.length === 0 ? (
            <div className="sf-muted">
              Nothing in your library yet — add {kind === 'skills' ? 'a skill' : 'a server'} under
              Library → {kind === 'skills' ? 'Skills' : 'MCP servers'}.
            </div>
          ) : (
            items.map((item) => (
              <label
                key={item.id}
                className={`smc-row ${item.locked ? 'is-locked' : ''}`}
                title={item.locked ? 'On for every project — change it in the Library' : undefined}
              >
                <input
                  type="checkbox"
                  checked={item.on}
                  disabled={item.locked || busyId === item.id}
                  onChange={() => toggle(item)}
                />
                <span className="smc-name">{item.name}</span>
                {!item.enabled && <span className="smc-extra">turned off</span>}
                {item.source && <span className="smc-badge">{SOURCE_LABEL[item.source]}</span>}
              </label>
            ))
          )}
        </div>

        <div className="smc-modal-foot">
          Applies from your next message.
        </div>
      </div>
    </div>
  );
}
