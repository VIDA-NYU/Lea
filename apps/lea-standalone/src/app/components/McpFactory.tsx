import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, Plug, Plus, Trash2, Check, X, Loader2 } from 'lucide-react';
import {
  installMcpCatalogEntry,
  listMcpCatalog,
  getMcpKeyRequirements,
  getMcpServerDefaults,
  testMcpServer,
  type McpServer,
  type McpTestResult,
  type McpTransport,
  type McpKeyRequirement,
  type CatalogEntry,
} from '../lib/api';
import { useFactories } from '../stores/factories';
import { useProjects } from '../stores/projects';
import { ScopeAssignment, ScopeBadge, type Scope } from './ScopeAssignment';

// The MCP Factory page (v2.5 E0) — the long-deferred "MCP Factory" the factories
// store pointed at. Structure follows SkillFactory: a two-pane catalog with an Add
// panel above it, since an MCP server is the same kind of library item as a skill.
//
// The FORM follows OpenHands' `mcp-server-form.tsx`: a transport picker first (it
// switches which fields render), then plain text fields — no JSON anywhere. Two
// deliberate divergences from theirs:
//   * a Test button, because they have none and a broken server otherwise saves
//     fine and fails silently at the next run (E0b);
//   * credentials are NAMED, not typed in — the value lives in Settings → API keys
//     and is read at spawn, so nothing here ever stores a secret (A7).

const TRANSPORTS: { key: McpTransport; label: string; hint: string }[] = [
  { key: 'stdio', label: 'Local command', hint: 'Runs a program on this machine (most Lean servers)' },
  { key: 'http', label: 'Remote (HTTP)', hint: 'Connects to a URL' },
  { key: 'sse', label: 'Remote (SSE)', hint: 'Connects to a URL using server-sent events' },
];

// One arg / one KEY=value per line — the same choice OpenHands makes, and much
// easier to get right than a JSON array.
const linesToList = (text: string): string[] =>
  text.split('\n').map((l) => l.trim()).filter(Boolean);

const listToLines = (list: string[]): string => (list || []).join('\n');

function linesToEnv(text: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of linesToList(text)) {
    const eq = line.indexOf('=');
    if (eq > 0) env[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
  }
  return env;
}

const envToLines = (env: Record<string, string>): string =>
  Object.entries(env || {}).map(([k, v]) => `${k}=${v}`).join('\n');

function useLeanPath(): string | null {
  const [path, setPath] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    getMcpServerDefaults()
      .then((d) => !cancelled && setPath(d.lean_project_path))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  return path;
}

// E2/E3: the curated entries. One click installs a PINNED server with its Lean path
// already filled in — the two things a mathematician cannot supply. The paired skill is a
// separate, visible act, because importing it also brings sub-agents.
function Catalog() {
  const refreshMcpServers = useFactories((s) => s.refreshMcpServers);
  const [entries, setEntries] = useState<CatalogEntry[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const reload = () => listMcpCatalog().then(setEntries).catch(() => {});
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
                await installMcpCatalogEntry(e.id);
                await refreshMcpServers();
                await reload();
                setNote(e.skill_note ? `Installed. ${e.skill_note} Add it under Library → Skills: ${e.skill_url}` : 'Installed.');
              } catch (err) {
                setNote(err instanceof Error ? err.message : String(err));
              } finally {
                setBusy(null);
              }
            }}
          >
            {busy === e.id ? 'Installing…' : 'Install'}
          </button>
        </div>
      ))}
      {note && <div className="sf-add-ok">{note}</div>}
    </div>
  );
}

// D1/D2: a server whose declared key is unsaved starts fine and then 401s on first use —
// a delayed failure far from its cause. Surfacing the gap in the Library is the moment
// the user can actually act on it.
function MissingKeys() {
  const [missing, setMissing] = useState<McpKeyRequirement[]>([]);
  useEffect(() => {
    let cancelled = false;
    getMcpKeyRequirements()
      .then((r) => !cancelled && setMissing(r.filter((x) => !x.configured)))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  if (!missing.length) return null;
  return (
    <div className="mcp-keywarn">
      {missing.map((m) => (
        <div key={m.env}>
          <b>{m.env}</b> isn't saved yet, so {m.servers.join(', ')} will fail when used.
          Add it under <b>Settings → API keys</b>.
        </div>
      ))}
    </div>
  );
}

export function McpFactory({ onBack }: { onBack: () => void }) {
  const servers = useFactories((s) => s.mcpServers);
  const refreshMcpServers = useFactories((s) => s.refreshMcpServers);
  const selectedId = useFactories((s) => s.selectedMcpServerId);
  const setSelectedId = useFactories((s) => s.setSelectedMcpServerId);
  const refreshProjects = useProjects((s) => s.refreshProjects);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([refreshMcpServers(), refreshProjects().catch(() => [])])
      .then(() => !cancelled && setLoadError(null))
      .catch((err) => !cancelled && setLoadError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = useMemo(() => servers.find((s) => s.id === selectedId), [servers, selectedId]);

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
          <span className="pw-crumb">MCP servers</span>
        </div>

        <div className="pw-hero">
          <h1 className="pw-title">
            <span className="pw-sigma">✦</span> MCP servers
          </h1>
          <p className="pw-desc">
            Extra tools for Lea, provided by a program you've installed — Lean goal
            inspection, Mathlib search, and so on. Add one here and assign it to the
            projects that should use it.
          </p>
        </div>

        <Catalog />
        <AddServer />
        <MissingKeys />

        {loadError && <div className="sf-load-err">{loadError}</div>}

        <div className="sf-catalog">
          <div className="sf-list">
            {loading ? (
              <div className="sf-muted">Loading…</div>
            ) : servers.length === 0 ? (
              <div className="sf-muted">No MCP servers yet. Add one above.</div>
            ) : (
              servers.map((server) => (
                <button
                  key={server.id}
                  className={`sf-list-row ${selectedId === server.id ? 'active' : ''}`}
                  onClick={() => setSelectedId(server.id)}
                >
                  <span className="sf-list-name">
                    {server.name}
                    {!server.enabled && <span className="sf-detail-slug"> off</span>}
                  </span>
                  <ScopeBadge isGlobal={server.is_global} count={server.project_ids.length} />
                </button>
              ))
            )}
          </div>

          <div className="sf-detail">
            {selected ? (
              <ServerDetail key={selected.id} server={selected} />
            ) : (
              <div className="sf-empty">Select a server to view or edit it.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// The shared field set, used by both Add and Edit so the two can't drift.
function ServerFields({
  leanPath, transport, setTransport, command, setCommand, args, setArgs, env, setEnv,
  envFrom, setEnvFrom, url, setUrl, apiKeyName, setApiKeyName, disabled,
}: {
  leanPath: string | null;
  transport: McpTransport; setTransport: (t: McpTransport) => void;
  command: string; setCommand: (v: string) => void;
  args: string; setArgs: (v: string) => void;
  env: string; setEnv: (v: string) => void;
  envFrom: string; setEnvFrom: (v: string) => void;
  url: string; setUrl: (v: string) => void;
  apiKeyName: string; setApiKeyName: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <>
      <div className="sf-add-modes">
        {TRANSPORTS.map((t) => (
          <button
            key={t.key}
            className={`sf-add-mode ${transport === t.key ? 'active' : ''}`}
            onClick={() => setTransport(t.key)}
            disabled={disabled}
            title={t.hint}
          >
            {t.label}
          </button>
        ))}
      </div>

      {transport === 'stdio' ? (
        <>
          <label className="mcp-field">
            <span className="mcp-label">Command</span>
            <input
              className="sf-add-input"
              value={command}
              placeholder="uvx"
              onChange={(e) => setCommand(e.target.value)}
              spellCheck={false}
              disabled={disabled}
            />
            <span className="mcp-hint">
              Just the program name. Anything after it goes in Arguments below.
            </span>
          </label>

          <label className="mcp-field">
            <span className="mcp-label">Arguments <span className="mcp-opt">optional</span></span>
            <textarea
              className="sf-textarea mcp-mini"
              value={args}
              placeholder={'lean-lsp-mcp'}
              onChange={(e) => setArgs(e.target.value)}
              spellCheck={false}
              disabled={disabled}
              rows={3}
            />
            <span className="mcp-hint">One per line.</span>
          </label>

          <label className="mcp-field">
            <span className="mcp-label">Settings <span className="mcp-opt">optional</span></span>
            <textarea
              className="sf-textarea mcp-mini"
              value={env}
              placeholder={'LEAN_PROJECT_PATH=/path/to/your/lean/project'}
              onChange={(e) => setEnv(e.target.value)}
              spellCheck={false}
              disabled={disabled}
              rows={3}
            />
            <span className="mcp-hint">
              One <code>NAME=value</code> per line. Don't put passwords or API keys here —
              use the box below.
              {/* A4: the one field nobody can be expected to know. The adapter has always
                  had this path; offering it in a click is the whole fix. */}
              {leanPath && !env.includes('LEAN_PROJECT_PATH') && (
                <>
                  {' '}
                  <button
                    type="button"
                    className="mcp-inline-btn"
                    onClick={() =>
                      setEnv((env ? env.replace(/\n$/, '') + '\n' : '') + `LEAN_PROJECT_PATH=${leanPath}`)
                    }
                    disabled={disabled}
                  >
                    Use my Lean project
                  </button>
                </>
              )}
            </span>
          </label>
        </>
      ) : (
        <>
          <label className="mcp-field">
            <span className="mcp-label">URL</span>
            <input
              className="sf-add-input"
              value={url}
              placeholder="https://api.example.com/mcp"
              onChange={(e) => setUrl(e.target.value)}
              spellCheck={false}
              disabled={disabled}
            />
          </label>
          <label className="mcp-field">
            <span className="mcp-label">API key name <span className="mcp-opt">optional</span></span>
            <input
              className="sf-add-input"
              value={apiKeyName}
              placeholder="EXAMPLE_API_KEY"
              onChange={(e) => setApiKeyName(e.target.value)}
              spellCheck={false}
              disabled={disabled}
            />
          </label>
        </>
      )}

      <label className="mcp-field">
        <span className="mcp-label">
          Needs an API key? <span className="mcp-opt">optional</span>
        </span>
        <textarea
          className="sf-textarea mcp-mini"
          value={envFrom}
          placeholder={'WOLFRAM_API_KEY'}
          onChange={(e) => setEnvFrom(e.target.value)}
          spellCheck={false}
          disabled={disabled}
          rows={2}
        />
        <span className="mcp-hint">
          Name it here, one per line, and save the value under <b>Settings → API keys</b>.
          Lea passes it to the server when it starts, so the key is never stored with
          this server.
        </span>
      </label>
    </>
  );
}

// A Test result line. `detail` is the server's own error — the useful part.
function TestReport({ result }: { result: McpTestResult }) {
  if (result.ok) {
    return (
      <div className="mcp-test ok">
        <Check size={13} /> Connected — {result.tool_count} tool
        {result.tool_count === 1 ? '' : 's'} available
        {result.tools.length > 0 && (
          <div className="mcp-test-tools">{result.tools.slice(0, 12).join(', ')}
            {result.tools.length > 12 ? `, +${result.tools.length - 12} more` : ''}</div>
        )}
      </div>
    );
  }
  return (
    <div className="mcp-test bad">
      <div><X size={13} /> {result.error}</div>
      {result.reason && <div className="mcp-test-reason">{result.reason}</div>}
      {result.detail && result.detail !== result.reason && (
        <details className="mcp-test-more">
          <summary>Technical details</summary>
          <pre className="mcp-test-detail">{result.detail}</pre>
        </details>
      )}
    </div>
  );
}

function useTester() {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<McpTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (spec: Parameters<typeof testMcpServer>[0]) => {
    setTesting(true);
    setResult(null);
    setError(null);
    try {
      setResult(await testMcpServer(spec));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setTesting(false);
    }
  };
  return { testing, result, error, run, clear: () => setResult(null) };
}

function AddServer() {
  const projects = useProjects((s) => s.projects);
  const addMcpServer = useFactories((s) => s.addMcpServer);

  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [transport, setTransport] = useState<McpTransport>('stdio');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [env, setEnv] = useState('');
  const [envFrom, setEnvFrom] = useState('');
  const [url, setUrl] = useState('');
  const [apiKeyName, setApiKeyName] = useState('');
  const [scope, setScope] = useState<Scope>({ is_global: false, project_ids: [] });
  const leanPath = useLeanPath();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const tester = useTester();

  const spec = () => ({
    transport,
    command: command.trim() || null,
    args: linesToList(args),
    env: linesToEnv(env),
    env_from: linesToList(envFrom),
    url: url.trim() || null,
    api_key_name: apiKeyName.trim() || null,
  });

  const submit = async () => {
    setError(null);
    setOk(null);
    setBusy(true);
    try {
      if (!name.trim()) throw new Error('Give the server a name.');
      const server = await addMcpServer({ name: name.trim(), ...spec(), ...scope });
      setOk(`Added “${server.name}”.`);
      setName(''); setCommand(''); setArgs(''); setEnv(''); setEnvFrom('');
      setUrl(''); setApiKeyName('');
      setScope({ is_global: false, project_ids: [] });
      tester.clear();
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
          <Plus size={14} /> Add an MCP server
        </button>
      </div>
    );
  }

  return (
    <div className="sf-add">
      <label className="mcp-field">
        <span className="mcp-label">Name</span>
        <input
          className="sf-add-input"
          value={name}
          placeholder="Lean LSP"
          onChange={(e) => setName(e.target.value)}
          disabled={busy}
        />
      </label>

      <ServerFields
        leanPath={leanPath}
        transport={transport} setTransport={setTransport}
        command={command} setCommand={setCommand}
        args={args} setArgs={setArgs}
        env={env} setEnv={setEnv}
        envFrom={envFrom} setEnvFrom={setEnvFrom}
        url={url} setUrl={setUrl}
        apiKeyName={apiKeyName} setApiKeyName={setApiKeyName}
        disabled={busy}
      />

      <ScopeAssignment value={scope} projects={projects} onChange={setScope} disabled={busy} />

      {tester.result && <TestReport result={tester.result} />}
      {tester.error && <div className="sf-detail-err">{tester.error}</div>}

      <div className="sf-add-foot">
        {error && <span className="sf-add-err">{error}</span>}
        {ok && <span className="sf-add-ok">{ok}</span>}
        <button className="sf-cancel" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
        <button
          className="sf-cancel"
          onClick={() => tester.run(spec())}
          disabled={busy || tester.testing}
          title="Start the server once and see whether it works"
        >
          {tester.testing ? <><Loader2 size={12} className="mcp-spin" /> Testing…</> : <><Plug size={12} /> Test</>}
        </button>
        <button className="sf-add-btn" onClick={submit} disabled={busy}>
          {busy ? 'Adding…' : 'Add'}
        </button>
      </div>
    </div>
  );
}

function ServerDetail({ server }: { server: McpServer }) {
  const projects = useProjects((s) => s.projects);
  const editMcpServer = useFactories((s) => s.editMcpServer);
  const assignMcpServer = useFactories((s) => s.assignMcpServer);
  const removeMcpServer = useFactories((s) => s.removeMcpServer);

  const [transport, setTransport] = useState<McpTransport>(server.transport);
  const [command, setCommand] = useState(server.command || '');
  const [args, setArgs] = useState(listToLines(server.args));
  const [env, setEnv] = useState(envToLines(server.env));
  const [envFrom, setEnvFrom] = useState(listToLines(server.env_from));
  const [url, setUrl] = useState(server.url || '');
  const [apiKeyName, setApiKeyName] = useState(server.api_key_name || '');
  const leanPath = useLeanPath();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const tester = useTester();

  const scope: Scope = { is_global: server.is_global, project_ids: server.project_ids };

  const spec = () => ({
    transport,
    command: command.trim() || null,
    args: linesToList(args),
    env: linesToEnv(env),
    env_from: linesToList(envFrom),
    url: url.trim() || null,
    api_key_name: apiKeyName.trim() || null,
  });

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await editMcpServer(server.id, spec());
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = async () => {
    setBusy(true);
    setError(null);
    try {
      await editMcpServer(server.id, { enabled: !server.enabled });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Delete the MCP server “${server.name}”? This can't be undone.`)) return;
    setBusy(true);
    try {
      await removeMcpServer(server.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  return (
    <div className="sf-detail-pane">
      <div className="sf-detail-head">
        <div className="sf-detail-title">
          <span className="sf-detail-name">{server.name}</span>
          <span className="sf-detail-slug">{server.slug}</span>
        </div>
        <button className="sf-del" onClick={remove} disabled={busy} title="Delete server">
          <Trash2 size={13} /> Delete
        </button>
      </div>

      <div className="sf-section-label">
        {server.enabled ? 'Enabled' : 'Disabled'}
        <button className="sf-edit" onClick={toggleEnabled} disabled={busy}>
          {server.enabled ? 'Turn off' : 'Turn on'}
        </button>
      </div>
      <div className="mcp-hint">
        {server.enabled
          ? 'Lea starts this server for the projects below and offers its tools.'
          : 'Turned off — Lea ignores this server everywhere.'}
      </div>

      <div className="sf-section-label">Connection</div>
      <ServerFields
        leanPath={leanPath}
        transport={transport} setTransport={setTransport}
        command={command} setCommand={setCommand}
        args={args} setArgs={setArgs}
        env={env} setEnv={setEnv}
        envFrom={envFrom} setEnvFrom={setEnvFrom}
        url={url} setUrl={setUrl}
        apiKeyName={apiKeyName} setApiKeyName={setApiKeyName}
        disabled={busy}
      />

      {tester.result && <TestReport result={tester.result} />}
      {tester.error && <div className="sf-detail-err">{tester.error}</div>}

      <div className="sf-body-foot">
        <button
          className="sf-cancel"
          onClick={() => tester.run(spec())}
          disabled={busy || tester.testing}
        >
          {tester.testing ? <><Loader2 size={12} className="mcp-spin" /> Testing…</> : <><Plug size={12} /> Test</>}
        </button>
        <button className="sf-save" onClick={save} disabled={busy}>
          {busy ? 'Saving…' : saved ? 'Saved' : 'Save'}
        </button>
      </div>

      <div className="mcp-hint" style={{ marginTop: 10 }}>
        The first Lean check after a server starts can take up to a minute while Lean loads
        Mathlib. It stays fast after that.
      </div>

      <div className="sf-section-label">Scope</div>
      <ScopeAssignment
        value={scope}
        projects={projects}
        onChange={(next) => assignMcpServer(server.id, next).catch((err) =>
          setError(err instanceof Error ? err.message : String(err)))}
        disabled={busy}
      />

      {error && <div className="sf-detail-err">{error}</div>}
    </div>
  );
}
