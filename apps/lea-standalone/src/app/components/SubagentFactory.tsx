import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, Bot, Plus, RotateCcw, Trash2, X } from 'lucide-react';
import type { SubagentProfile, SubagentSettings } from '../lib/api';
import { useSubagents } from '../stores/subagents';
import { useModel } from '../stores/model';
import { ModelPicker } from './ModelPicker';
import { AuthoringFields, EMPTY_AUTHORING, hasAuthoring } from './AuthoringFields';
import { createSubagentRole, deleteSubagentRole, type AuthoringFieldValues } from '../lib/api';

// D6: the Sub-agents page — view/edit every role's settings (like the Skills page).
// Edits to a BUILT-IN persist as per-role OVERRIDES (never mutating the vendored
// lea/agents/*.yaml); the prover merges them over the role's defaults at spawn.
//
// v2.5 B2/B3: a user can now ADD a role too, described through the same guided questions
// the Skill Factory uses. "When should Lea use this?" becomes the line the coordinator
// reads while choosing whom to delegate to — so answering it well is what makes a new
// sub-agent actually get used, rather than existing and never running.
export function SubagentFactory({ onBack }: { onBack: () => void }) {
  const profiles = useSubagents((s) => s.profiles);
  const selectedName = useSubagents((s) => s.selectedName);
  const setSelectedName = useSubagents((s) => s.setSelectedName);
  const refreshProfiles = useSubagents((s) => s.refreshProfiles);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    let live = true;
    refreshProfiles()
      .catch((e) => live && setLoadError(e instanceof Error ? e.message : String(e)))
      .finally(() => live && setLoading(false));
    // Populate the model catalog + featured list so the per-role model picker can
    // search the full set (same source the chat-head picker uses).
    useModel.getState().loadCatalog().catch(() => {});
    useModel.getState().syncFromSettings().catch(() => {});
    return () => {
      live = false;
    };
  }, [refreshProfiles]);

  const selected = useMemo(
    () => profiles.find((p) => p.name === selectedName),
    [profiles, selectedName],
  );

  return (
    <div className="lea-app">
      <div className="project-window sf">
        <div className="pw-bar">
          <button className="pw-back" onClick={onBack}>
            <ChevronLeft size={15} /> Chats
          </button>
          <span className="pw-crumbs">/ Library / Sub-agents</span>
        </div>

        <div className="pw-hero">
          <h1 className="pw-title">Sub-agents</h1>
          <p className="pw-sub">
            The roles the coordinator delegates to. Add your own, or tune a built-in's
            model, limits and instructions — tuning is saved as an override, never
            touching the shipped defaults.
          </p>
        </div>

        <AddSubagent
          open={adding}
          setOpen={setAdding}
          onAdded={(name) => {
            refreshProfiles().catch(() => {});
            setSelectedName(name);
          }}
        />

        {loadError && <div className="sf-load-err">{loadError}</div>}

        <div className="sf-catalog">
          <div className="sf-list">
            {loading && profiles.length === 0 ? (
              <div className="sf-empty">Loading…</div>
            ) : (
              profiles.map((p) => (
                <button
                  key={p.name}
                  className={`sf-list-row ${p.name === selectedName ? 'active' : ''}`}
                  onClick={() => setSelectedName(p.name)}
                >
                  <span className="sf-list-name">{p.name}</span>
                  {Object.keys(p.override).length > 0 && <span className="sa-tag">customized</span>}
                </button>
              ))
            )}
          </div>
          <div className="sf-detail">
            {selected ? (
              <SubagentDetail key={selected.name} profile={selected} />
            ) : (
              <div className="sf-empty">Select a role to view and edit its settings.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function SubagentDetail({ profile }: { profile: SubagentProfile }) {
  // Deleting a user role touches the shared list, so read the store here rather than
  // threading two more props through the detail pane.
  const refreshProfiles = useSubagents((st) => st.refreshProfiles);
  const setSelectedName = useSubagents((st) => st.setSelectedName);

  const saveProfile = useSubagents((s) => s.saveProfile);
  const modelCatalog = useModel((s) => s.modelCatalog);
  const modelFeatured = useModel((s) => s.modelFeatured);
  const eff = profile.effective;

  const [model, setModel] = useState(eff.model ?? '');
  const [maxTurns, setMaxTurns] = useState(eff.max_turns != null ? String(eff.max_turns) : '');
  const [maxCost, setMaxCost] = useState(eff.max_cost != null ? String(eff.max_cost) : '');
  const [prompt, setPrompt] = useState(eff.system_prompt);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const dirty =
    (model.trim() || null) !== (eff.model ?? null) ||
    (maxTurns.trim() ? Number(maxTurns) : null) !== (eff.max_turns ?? null) ||
    (maxCost.trim() ? Number(maxCost) : null) !== (eff.max_cost ?? null) ||
    prompt !== eff.system_prompt;

  const commit = async (settings: Partial<SubagentSettings>) => {
    setBusy(true);
    setErr(null);
    try {
      await saveProfile(profile.name, settings);
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onSave = () =>
    commit({
      model: model.trim() || null,
      max_turns: maxTurns.trim() ? Number(maxTurns) : null,
      max_cost: maxCost.trim() ? Number(maxCost) : null,
      system_prompt: prompt,
      tools: eff.tools, // tool subset is read-only in this view; sent unchanged
    });

  // Reset = send the vendored defaults so the stored override diffs to empty (cleared).
  const onReset = () => commit(profile.default);

  const isOverridden = Object.keys(profile.override).length > 0;

  return (
    <div className="sa-detail">
      <div className="sa-head">
        <div className="sa-head-l">
          <Bot size={16} />
          <span className="sa-name">{profile.name}</span>
          {profile.origin === 'user' && <span className="sa-tag">yours</span>}
          {isOverridden && <span className="sa-tag">customized</span>}
        </div>
        {/* A built-in can be reset to its shipped defaults but never removed; one you
            wrote can be deleted outright. */}
        {profile.origin === 'user' ? (
          <button
            className="sf-del"
            disabled={busy}
            title="Delete this sub-agent"
            onClick={async () => {
              if (!profile.id) return;
              if (!window.confirm(`Delete the sub-agent “${profile.name}”? This can't be undone.`)) return;
              try {
                await deleteSubagentRole(profile.id);
                await refreshProfiles();
                setSelectedName(undefined);
              } catch (err) {
                window.setTimeout(() => undefined, 0);
                throw err;
              }
            }}
          >
            <Trash2 size={13} /> Delete
          </button>
        ) : isOverridden ? (
          <button className="sa-reset" onClick={onReset} disabled={busy} title="Reset to defaults">
            <RotateCcw size={12} /> Reset to defaults
          </button>
        ) : null}
      </div>
      {profile.description && <p className="sa-desc">{profile.description}</p>}

      <div className="sa-grid">
        <div className="sa-field">
          <span className="sa-label">Model</span>
          <div className="sa-model-row">
            <ModelPicker
              value={model}
              catalog={modelCatalog}
              featured={modelFeatured}
              onChange={setModel}
              placeholder="Inherit coordinator's model"
            />
            {model && (
              <button
                className="sa-inherit"
                onClick={() => setModel('')}
                title="Clear — inherit the coordinator's model"
                aria-label="Clear model (inherit the coordinator's)"
              >
                <X size={12} />
              </button>
            )}
          </div>
          <span className="sa-hint">
            Search the full catalog, or clear to inherit the coordinator's model.
          </span>
        </div>

        <label className="sa-field">
          <span className="sa-label">Max turns</span>
          <input
            className="sa-input"
            type="number"
            min={1}
            value={maxTurns}
            placeholder="role default"
            onChange={(e) => setMaxTurns(e.target.value)}
          />
          <span className="sa-hint">Turn budget before summarize-on-cap ends the run.</span>
        </label>

        <label className="sa-field">
          <span className="sa-label">Max cost (USD)</span>
          <input
            className="sa-input"
            type="number"
            min={0}
            step="0.01"
            value={maxCost}
            placeholder="uncapped"
            onChange={(e) => setMaxCost(e.target.value)}
          />
          <span className="sa-hint">Per-run spend ceiling; the run stops cleanly when crossed.</span>
        </label>

        <div className="sa-field">
          <span className="sa-label">Tools</span>
          <div className="sa-tools">
            {eff.tools.length === 0 ? (
              <span className="sa-hint">All of the coordinator's tools.</span>
            ) : (
              eff.tools.map((t) => (
                <span className="sa-tool" key={t}>
                  {t}
                </span>
              ))
            )}
          </div>
          <span className="sa-hint">The role's tool subset (read-only; always ⊆ the coordinator's).</span>
        </div>
      </div>

      <div className="sa-field sa-prompt-field">
        <span className="sa-label">System prompt</span>
        <textarea
          className="sa-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          spellCheck={false}
        />
        <span className="sa-hint">
          Appended after the shared Lean core — it composes onto the hard rules, never replaces them.
        </span>
      </div>

      {err && <div className="sf-load-err">{err}</div>}

      <div className="sa-actions">
        <button className="sa-save" onClick={onSave} disabled={busy || !dirty}>
          {busy ? 'Saving…' : saved ? 'Saved ✓' : 'Save changes'}
        </button>
        {saved && !busy && <span className="sa-saved-note">Applied to the next spawn.</span>}
      </div>
    </div>
  );
}


// v2.5 B2/B3 — authoring a new sub-agent. Deliberately the same four questions the Skill
// Factory asks: the answers compile into the role's instructions, and "when to use this"
// becomes the description the coordinator reads when choosing a role to delegate to.
function AddSubagent({
  open,
  setOpen,
  onAdded,
}: {
  open: boolean;
  setOpen: (v: boolean) => void;
  onAdded: (name: string) => void;
}) {
  const [name, setName] = useState('');
  const [fields, setFields] = useState<AuthoringFieldValues>(EMPTY_AUTHORING);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    setBusy(true);
    try {
      if (!name.trim()) throw new Error('Give the sub-agent a name.');
      if (!hasAuthoring(fields)) throw new Error('Answer at least the first question.');
      const role = await createSubagentRole({ name: name.trim(), authoring: fields });
      setName('');
      setFields(EMPTY_AUTHORING);
      setOpen(false);
      onAdded(role.name);
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
          <Plus size={14} /> Add a sub-agent
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
          placeholder="Counterexample hunter"
          onChange={(e) => setName(e.target.value)}
          disabled={busy}
        />
      </label>

      <AuthoringFields kind="role" value={fields} onChange={setFields} disabled={busy} />

      <div className="sf-add-foot">
        {error && <span className="sf-add-err">{error}</span>}
        <button className="sf-cancel" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
        <button className="sf-add-btn" onClick={submit} disabled={busy}>
          {busy ? 'Adding…' : 'Add'}
        </button>
      </div>
    </div>
  );
}
