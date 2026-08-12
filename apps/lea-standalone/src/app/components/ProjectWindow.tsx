import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, Download } from 'lucide-react';
import {
  listProjectFormalizations,
  type Formalization,
  type ProjectDetail,
  type SessionStatus,
} from '../lib/api';
import { MarkdownDoc } from './MarkdownDoc';
import { FilesCard } from './FilesCard';
import { BlueprintTab } from './BlueprintTab';
import { FilesystemTab } from './FilesystemTab';
import { SkillsMcpTab } from './SkillsMcpTab';

type Tab = 'overview' | 'formalizations' | 'blueprint' | 'filesystem' | 'skills-mcp';

const TABS: { id: Tab; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'formalizations', label: 'Formalizations' },
  { id: 'blueprint', label: 'Blueprint' },
  { id: 'filesystem', label: 'Filesystem' },
  { id: 'skills-mcp', label: 'Skills / MCP' },
];

// The project window (v2.1 F2/F3). A full-page view — breadcrumb back to Chats, a
// hero (∑ title · namespace + description), and the tab strip. The Overview tab
// (F3) has the "new proof in this project" composer + the project's sessions list;
// opening a row loads the normal Chat+Canvas. Blueprint/Filesystem are later slices.
export function ProjectWindow({
  project,
  onBack,
  onStartProof,
  onStartFormalization,
  onOpenSession,
}: {
  project: ProjectDetail;
  onBack: () => void;
  onStartProof: (message: string) => Promise<void> | void;
  onStartFormalization: (formalization: Formalization, message?: string) => Promise<void> | void;
  onOpenSession: (sessionId: string) => void;
}) {
  const [tab, setTab] = useState<Tab>('overview');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [importSignal, setImportSignal] = useState(0);
  const allSessions = project.sessions ?? [];
  // ROOTS only. A sub-agent is a session row (`parent_id` = the coordinator that
  // spawned it), and the project payload returns children alongside roots so callers
  // can split them — the sidebar already does. This list did not, so a project's
  // Sessions read as a mix of work you started and internal children the coordinator
  // spawned ("certificate c2" sitting next to the formalization you actually asked
  // for). A child is reachable from its coordinator's thread, which is the only place
  // it means anything.
  const sessions = allSessions.filter((s) => !s.parent_id);
  // Memory is agent-written: a run advances its session's `updated_at`, so when the
  // project detail is re-fetched the Memory card re-loads memory.md (F4/D39).
  // Deliberately over ALL sessions, children included: a child's run advances the
  // project too, and missing that would leave the Memory card stale.
  const docSignal = allSessions.reduce(
    (max, s) => Math.max(max, Date.parse(s.updated_at) || 0),
    0,
  ) + importSignal;

  const submit = async () => {
    const message = draft.trim();
    if (!message || busy) return;
    setBusy(true);
    try {
      await onStartProof(message);
      setDraft('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="project-window">
      <div className="pw-bar">
        <button className="pw-back" onClick={onBack}>
          <ChevronLeft size={15} /> Chats
        </button>
        <span className="pw-crumb-sep">/</span>
        <span className="pw-crumb">{project.title}</span>
      </div>

      <div className="pw-hero">
        <h1 className="pw-title">
          <span className="pw-sigma">∑</span> {project.title}
          <span className="pw-ns">{project.namespace}</span>
        </h1>
        {project.description && <p className="pw-desc">{project.description}</p>}
      </div>

      <div className="pw-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`pw-tab ${tab === t.id ? 'active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="pw-body">
        {tab === 'overview' ? (
          <div className="pw-overview-grid">
          <div className="pw-overview">
            <div className="pw-composer">
              <div className="pw-sec-label">New proof in this project</div>
              <textarea
                className="pw-composer-input"
                placeholder={`Prove a theorem in ${project.namespace}… (it can import sibling lemmas)`}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit();
                }}
              />
              <div className="pw-composer-foot">
                <span className="pw-hint">⌘↵ to start</span>
                <button className="pw-start" onClick={submit} disabled={busy || !draft.trim()}>
                  {busy ? 'Starting…' : 'Prove in this project'}
                </button>
              </div>
            </div>

            <div className="pw-sec-label" style={{ marginTop: 22 }}>Sessions</div>
            {sessions.length === 0 ? (
              <div className="pw-empty">
                No proofs yet. Start one above — it lands in <code>{project.repo_path}</code> so
                its lemmas can chain.
              </div>
            ) : (
              <ul className="pw-session-list">
                {sessions.map((s) => (
                  <li key={s.id}>
                    <button className="pw-session-row" onClick={() => onOpenSession(s.id)}>
                      <span className={`dot ${sessionDotClass(s.status)}`} />
                      <span className="pw-session-title">{s.title}</span>
                      <span className="pw-session-when">{new Date(s.updated_at).toLocaleString()}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {/* Discoverability (#14): the project's files/download live under the
                Filesystem tab, not here — point there with a one-click jump. */}
            <div className="pw-download-hint">
              <Download size={13} /> Browse or download this project’s files under the{' '}
              <button className="pw-link" onClick={() => setTab('filesystem')}>Filesystem</button> tab.
            </div>
          </div>

          <aside className="pw-rail">
            <MarkdownDoc
              projectId={project.id}
              doc="instructions"
              title="Instructions"
              icon="📋"
              refreshSignal={docSignal}
              emptyHint="No instructions yet — add your project's goal and any conventions so Lea follows them on every run."
            />
            <MarkdownDoc
              projectId={project.id}
              doc="memory"
              title="Memory"
              icon="🧠"
              agentWritten
              refreshSignal={docSignal}
              emptyHint="No memory for this project yet — jot down facts, witnesses, and dead ends here; Lea reads it and adds to it as it works."
            />
            <FilesCard projectId={project.id} refreshSignal={docSignal} />
          </aside>
          </div>
        ) : tab === 'formalizations' ? (
          <FormalizationsTab
            projectId={project.id}
            refreshSignal={docSignal}
            onOpenSession={onOpenSession}
            onStartFormalization={onStartFormalization}
          />
        ) : tab === 'skills-mcp' ? (
          <SkillsMcpTab projectId={project.id} />
        ) : tab === 'blueprint' ? (
          <BlueprintTab projectId={project.id} onOpenSession={onOpenSession} refreshSignal={docSignal} />
        ) : (
          <FilesystemTab
            projectId={project.id}
            refreshSignal={docSignal}
            onProjectChanged={() => setImportSignal((value) => value + 1)}
          />
        )}
      </div>
    </div>
  );
}

function FormalizationsTab({
  projectId,
  refreshSignal,
  onOpenSession,
  onStartFormalization,
}: {
  projectId: string;
  refreshSignal: number;
  onOpenSession: (sessionId: string) => void;
  onStartFormalization: (formalization: Formalization) => Promise<void> | void;
}) {
  const [items, setItems] = useState<Formalization[]>([]);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('all');
  const [error, setError] = useState<string>();

  useEffect(() => {
    listProjectFormalizations(projectId)
      .then((result) => {
        setItems(result.formalizations);
        setSummary(result.summary);
        setError(undefined);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [projectId, refreshSignal]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return items.filter((item) => {
      if (status !== 'all' && item.validity_status !== status) return false;
      if (!needle) return true;
      return [
        item.display_title,
        item.declaration_name,
        item.primary_path,
        ...item.files.map((file) => file.path),
      ].some((value) => String(value || '').toLowerCase().includes(needle));
    });
  }, [items, query, status]);

  return (
    <div className="pw-formalizations">
      <div className="pw-form-summary">
        <span>{summary.formalization_count || 0} total</span>
        {!!summary.proved && <span className="ok">{summary.proved} proved</span>}
        {!!summary.failing && <span className="fail">{summary.failing} failing</span>}
        {!!summary.stale && <span className="warn">{summary.stale} stale</span>}
        {!!summary.active_run_count && <span>{summary.active_run_count} active</span>}
      </div>
      <div className="pw-form-controls">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search title, declaration, module, or path…"
        />
        <select value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="all">All statuses</option>
          {['draft', 'planned', 'unchecked', 'failing', 'proved', 'defined', 'disproved', 'needs_review', 'stale']
            .map((value) => <option key={value} value={value}>{value.replace('_', ' ')}</option>)}
        </select>
      </div>
      {error && <div className="pw-empty">{error}</div>}
      {!error && filtered.length === 0 && (
        <div className="pw-empty">No formalizations match this view.</div>
      )}
      <ul className="pw-form-list">
        {filtered.map((item) => {
          const recent = item.sessions[0];
          return (
            <li key={item.id} className="pw-form-row">
              <button
                className="pw-form-main"
                onClick={() => recent && onOpenSession(recent.id)}
                disabled={!recent}
              >
                <span className={`dot ${formalizationDotClass(item)}`} />
                <span>
                  <strong>{item.declaration_name || item.display_title}</strong>
                  <small>
                    {item.kind}
                    {item.primary_path ? ` · ${item.primary_path}` : ' · no file yet'}
                  </small>
                </span>
                <span className="pw-form-state">
                  {item.activity.status !== 'idle'
                    ? `${item.activity.status} · ${item.validity_status}`
                    : item.validity_status}
                </span>
              </button>
              <button
                className="pw-form-continue"
                onClick={() => onStartFormalization(item)}
              >
                New conversation
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function formalizationDotClass(item: Formalization): string {
  if (item.activity.status !== 'idle') return 'run';
  if (item.validity_status === 'proved' || item.validity_status === 'defined') return 'ok';
  if (item.validity_status === 'failing') return 'fail';
  return 'idle';
}

function sessionDotClass(status: SessionStatus | string): string {
  if (status === 'ok' || status === 'proved' || status === 'defined') return 'ok';
  if (status === 'error') return 'fail';
  if (status === 'running' || status === 'disproved') return 'run';
  return 'idle';
}
