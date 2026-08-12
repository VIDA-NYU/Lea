import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, Plus, Trash2, Wrench } from 'lucide-react';
import {
  installToolCatalogEntry,
  listToolCatalog,
  createCustomTool,
  deleteCustomTool,
  listCustomTools,
  setCustomToolAssignment,
  type AuthoringFieldValues,
  type CustomTool,
  type Project,
  type ToolCatalogEntry,
} from '../lib/api';
import { useProjects } from '../stores/projects';
import { ScopeAssignment, ScopeBadge, type Scope } from './ScopeAssignment';
import { AuthoringFields, EMPTY_AUTHORING, hasAuthoring } from './AuthoringFields';

// The Tools page (v2.5 F1) — a REST endpoint as a tool, with no code.
//
// The split that makes this usable by a mathematician: the ENDPOINT is engineering (a
// URL, a parameter list, a key name) and is written once — by a collaborator, or shipped
// in a pack. The four AUTHORING questions are what the mathematician owns, and they are
// what the model actually reads when deciding whether to reach for the tool.
//
// Only https public addresses are accepted, and that is enforced server-side at save AND
// again at call time: this is the first outbound request surface in Lea, and a tool spec
// can arrive inside an imported pack.

export function ToolFactory({ onBack }: { onBack: () => void }) {
  const projects = useProjects((s) => s.projects);
  const refreshProjects = useProjects((s) => s.refreshProjects);
  const [tools, setTools] = useState<CustomTool[]>([]);
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const reload = () => listCustomTools().then(setTools);

  useEffect(() => {
    let cancelled = false;
    Promise.all([reload(), refreshProjects().catch(() => [])])
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = useMemo(() => tools.find((t) => t.id === selectedId), [tools, selectedId]);

  return (
    <div className="lea-app">
      <div className="project-window sf">
        <div className="pw-bar">
          <button className="pw-back" onClick={onBack}>
            <ChevronLeft size={15} /> Chats
          </button>
          <span className="pw-crumb-sep">/</span>
          <span className="pw-crumb">Library</span>
          <span className="pw-crumb-sep">/</span>
          <span className="pw-crumb">Tools</span>
        </div>

        <div className="pw-hero">
          <h1 className="pw-title">
            <span className="pw-sigma">✦</span> Tools
          </h1>
          <p className="pw-desc">
            Connect Lea to a web service — a maths engine, a paper search — by describing
            its address and what it does. No installation and no code.
          </p>
        </div>

        <ToolCatalog onInstalled={async (id) => { await reload(); setSelectedId(id); }} />

        <AddTool
          open={adding}
          setOpen={setAdding}
          projects={projects}
          onAdded={async (id) => {
            await reload();
            setSelectedId(id);
          }}
        />

        {error && <div className="sf-load-err">{error}</div>}

        <div className="sf-catalog">
          <div className="sf-list">
            {loading ? (
              <div className="sf-muted">Loading…</div>
            ) : tools.length === 0 ? (
              <div className="sf-muted">No tools yet. Add one above.</div>
            ) : (
              tools.map((tool) => (
                <button
                  key={tool.id}
                  className={`sf-list-row ${selectedId === tool.id ? 'active' : ''}`}
                  onClick={() => setSelectedId(tool.id)}
                >
                  <span className="sf-list-name">
                    {tool.name}
                    {!tool.enabled && <span className="sf-detail-slug"> off</span>}
                  </span>
                  <ScopeBadge isGlobal={tool.is_global} count={tool.project_ids.length} />
                </button>
              ))
            )}
          </div>

          <div className="sf-detail">
            {selected ? (
              <div className="sf-detail-pane">
                <div className="sf-detail-head">
                  <div className="sf-detail-title">
                    <span className="sf-detail-name">{selected.name}</span>
                    <span className="sf-detail-slug">{selected.slug}</span>
                  </div>
                  <button
                    className="sf-del"
                    onClick={async () => {
                      if (!window.confirm(`Delete the tool “${selected.name}”?`)) return;
                      await deleteCustomTool(selected.id);
                      setSelectedId(undefined);
                      await reload();
                    }}
                  >
                    <Trash2 size={13} /> Delete
                  </button>
                </div>

                <div className="sf-section-label">Endpoint</div>
                <div className="sf-file">{selected.method} {selected.url}</div>
                {selected.auth_key_name && (
                  <div className="mcp-hint" style={{ marginTop: 6 }}>
                    Uses the key <b>{selected.auth_key_name}</b>, read from Settings when the
                    tool runs — it is not stored here.
                  </div>
                )}

                {selected.description && (
                  <>
                    <div className="sf-section-label">What Lea is told</div>
                    <div className="sf-body">
                      <pre className="mcp-test-detail">{selected.description}</pre>
                    </div>
                  </>
                )}

                <div className="sf-section-label">Scope</div>
                <ScopeAssignment
                  value={{ is_global: selected.is_global, project_ids: selected.project_ids }}
                  projects={projects}
                  onChange={async (next) => {
                    await setCustomToolAssignment(selected.id, next);
                    await reload();
                  }}
                />
              </div>
            ) : (
              <div className="sf-empty">Select a tool to view it.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// Suggested tools (E2). Installed UNSCOPED on purpose: a suggestion may duplicate
// something already configured, and T1 measured that giving the agent two ways to do one
// thing makes it choose by familiarity. Scoping stays a deliberate act.
function ToolCatalog({ onInstalled }: { onInstalled: (id: string) => void | Promise<void> }) {
  const [entries, setEntries] = useState<ToolCatalogEntry[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const reload = () => listToolCatalog().then(setEntries).catch(() => {});
  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const available = entries.filter((e) => !e.installed);
  if (!available.length) return null;

  return (
    <div className="sf-add">
      <div className="mcp-label" style={{ marginBottom: 4 }}>Suggested</div>
      {available.map((e) => (
        <div key={e.id} className="cat-row">
          <div className="cat-text">
            <b>{e.title}</b>
            <div className="mcp-hint">{e.summary}</div>
            {e.requires && <div className="mcp-hint">{e.requires}</div>}
          </div>
          <button
            className="sf-add-btn"
            disabled={busy === e.id}
            onClick={async () => {
              setBusy(e.id);
              setNote(null);
              try {
                const tool = await installToolCatalogEntry(e.id);
                await reload();
                await onInstalled(tool.id);
                setNote('Added. Choose which projects should use it below.');
              } catch (err) {
                setNote(err instanceof Error ? err.message : String(err));
              } finally {
                setBusy(null);
              }
            }}
          >
            {busy === e.id ? 'Adding…' : 'Add'}
          </button>
        </div>
      ))}
      {note && <div className="sf-add-ok">{note}</div>}
    </div>
  );
}

function AddTool({
  open,
  setOpen,
  projects,
  onAdded,
}: {
  open: boolean;
  setOpen: (v: boolean) => void;
  projects: Project[];
  onAdded: (id: string) => void | Promise<void>;
}) {
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [keyName, setKeyName] = useState('');
  const [params, setParams] = useState('');
  const [fields, setFields] = useState<AuthoringFieldValues>(EMPTY_AUTHORING);
  const [scope, setScope] = useState<Scope>({ is_global: false, project_ids: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    setBusy(true);
    try {
      if (!name.trim()) throw new Error('Give the tool a name.');
      if (!url.trim()) throw new Error('The tool needs a web address.');
      if (!hasAuthoring(fields)) throw new Error('Answer at least the first question.');
      // One parameter name per line becomes a minimal JSON Schema — the same
      // line-delimited choice the MCP form makes, for the same reason.
      const names = params.split('\n').map((p) => p.trim()).filter(Boolean);
      const properties: Record<string, unknown> = {};
      names.forEach((p) => {
        properties[p] = { type: 'string', description: `The ${p} to send.` };
      });
      const tool = await createCustomTool({
        name: name.trim(),
        url: url.trim(),
        authoring: fields,
        auth_key_name: keyName.trim() || null,
        params: names.length ? { type: 'object', properties, required: names } : {},
        ...scope,
      });
      setName(''); setUrl(''); setKeyName(''); setParams('');
      setFields(EMPTY_AUTHORING);
      setScope({ is_global: false, project_ids: [] });
      setOpen(false);
      await onAdded(tool.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <div className="sf-add">
        <button className="sf-add-mode active" onClick={() => setOpen(true)}>
          <Plus size={14} /> Add a tool
        </button>
      </div>
    );
  }

  return (
    <div className="sf-add">
      <label className="mcp-field">
        <span className="mcp-label">Name</span>
        <input className="sf-add-input" value={name} placeholder="Wolfram Alpha"
               onChange={(e) => setName(e.target.value)} disabled={busy} />
      </label>

      <label className="mcp-field">
        <span className="mcp-label">Web address</span>
        <input className="sf-add-input" value={url} spellCheck={false}
               placeholder="https://api.example.com/search?q={query}"
               onChange={(e) => setUrl(e.target.value)} disabled={busy} />
        <span className="mcp-hint">
          Must start with <code>https://</code>. Put <code>{'{name}'}</code> where a value
          should go, and list those names below.
        </span>
      </label>

      <label className="mcp-field">
        <span className="mcp-label">Values Lea fills in <span className="mcp-opt">optional</span></span>
        <textarea className="sf-textarea mcp-mini" rows={2} value={params}
                  placeholder={'query'} spellCheck={false}
                  onChange={(e) => setParams(e.target.value)} disabled={busy} />
        <span className="mcp-hint">One per line, matching the {'{names}'} in the address.</span>
      </label>

      <label className="mcp-field">
        <span className="mcp-label">Needs an API key? <span className="mcp-opt">optional</span></span>
        <input className="sf-add-input" value={keyName} placeholder="WOLFRAM_API_KEY"
               spellCheck={false} onChange={(e) => setKeyName(e.target.value)} disabled={busy} />
        <span className="mcp-hint">
          Name it here and save the value under <b>Settings → API keys</b>. Lea sends it when
          the tool runs, so the key is never stored with this tool.
        </span>
      </label>

      <AuthoringFields kind="tool" value={fields} onChange={setFields} disabled={busy} />
      <ScopeAssignment value={scope} projects={projects} onChange={setScope} disabled={busy} />

      <div className="sf-add-foot">
        {error && <span className="sf-add-err">{error}</span>}
        <button className="sf-cancel" onClick={() => setOpen(false)} disabled={busy}>Cancel</button>
        <button className="sf-add-btn" onClick={submit} disabled={busy}>
          {busy ? 'Adding…' : 'Add'}
        </button>
      </div>
    </div>
  );
}

export const ToolsIcon = Wrench;
