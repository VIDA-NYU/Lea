import { useEffect, useMemo, useState } from 'react';
import { FileText, LoaderCircle } from 'lucide-react';
import type { ProjectGraph } from '../../lib/api';
import { acceptProposal, discardProposal, openOrDraftBlueprint } from '../../lib/blueprintActions';
import { byKey, displayFile, firstLine, parseBlueprintMarkdown, type BlueprintMdNode } from '../../lib/blueprintModel.js';
import { useWorkbench, type BlueprintProposal } from '../../stores/workbench';

// The proposal aside (mockup frame 8): "Lea proposes N nodes", one card per node with
// a checkbox, the kind, a one-line statement, the (resolved or suggested) file and
// its dependencies; Accept N nodes / Edit markdown / Discard. While the draft is
// still being produced it shows the drafting state instead.

export function ProposalPanel({
  projectId,
  proposal,
  markdown,
  graph,
  onEditMarkdown,
}: {
  projectId: string;
  proposal: BlueprintProposal;
  markdown: string;
  graph: ProjectGraph | null;
  onEditMarkdown: () => void;
}) {
  const setProposal = useWorkbench((s) => s.setBlueprintProposal);
  const setPendingSubmission = useWorkbench((s) => s.setPendingSubmission);
  const parsed = useMemo(() => parseBlueprintMarkdown(markdown), [markdown]);
  const graphIndex = useMemo(() => byKey(graph?.nodes ?? []), [graph]);
  const [checked, setChecked] = useState<Set<string>>(() => new Set(parsed.nodes.map((n) => n.key)));
  // Keys the user unticked, so a refetch (the agent still writing) does not re-tick them.
  const [removed] = useState(() => new Set<string>());
  const [busy, setBusy] = useState<'accept' | 'discard' | 'retry' | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A node that appears after a refetch starts checked.
  useEffect(() => {
    setChecked((cur) => {
      const next = new Set(cur);
      for (const n of parsed.nodes) if (!cur.has(n.key) && !removed.has(n.key)) next.add(n.key);
      return next;
    });
  }, [parsed, removed]);

  const toggle = (key: string) => {
    setChecked((cur) => {
      const next = new Set(cur);
      if (next.has(key)) {
        next.delete(key);
        removed.add(key);
      } else {
        next.add(key);
        removed.delete(key);
      }
      return next;
    });
  };

  const accept = async () => {
    setBusy('accept');
    setError(null);
    try {
      const keep = parsed.nodes.filter((n) => checked.has(n.key));
      await acceptProposal(projectId, keep, parsed.preamble);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const discard = async () => {
    setBusy('discard');
    setError(null);
    try {
      await discardProposal(projectId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const retry = async () => {
    setBusy('retry');
    setError(null);
    try {
      await openOrDraftBlueprint(projectId, { sessionId: proposal.draftingSessionId });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const cancelDrafting = () => {
    setPendingSubmission(null);
    setProposal(null);
  };

  if (proposal.phase === 'drafting') {
    return (
      <aside className="bpv-aside" aria-label="Drafting the blueprint">
        <div className="bpv-aside-head">
          <span className="bpv-aside-title">
            {proposal.error ? 'Drafting failed' : 'Lea is drafting the blueprint'}
          </span>
        </div>
        <div className="bpv-aside-body">
          {proposal.error ? (
            <p className="bpv-note is-error">{proposal.error}</p>
          ) : proposal.source === 'artifacts' ? (
            <p className="bpv-note">
              <LoaderCircle size={13} className="bpv-spin" /> Reading the project's formalized declarations…
            </p>
          ) : (
            <>
              <p className="bpv-note">
                <LoaderCircle size={13} className="bpv-spin" /> No proofs to seed from yet. A drafting prompt was handed to the
                composer; the nodes appear here as Lea writes <code>.lea/blueprint.md</code>, and you review them once the run
                settles.
              </p>
              {parsed.nodes.length > 0 && (
                <p className="bpv-note">
                  {parsed.nodes.length} {parsed.nodes.length === 1 ? 'node' : 'nodes'} so far: {parsed.nodes.map((n) => n.key).join(', ')}
                </p>
              )}
            </>
          )}
          {proposal.notes && proposal.notes.length > 0 && (
            <ul className="bpv-notes">
              {proposal.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
          {error && <p className="bpv-note is-error">{error}</p>}
        </div>
        <div className="bpv-aside-foot">
          {proposal.error && (
            <button className="bpv-btn is-primary" onClick={retry} disabled={busy !== null}>
              {busy === 'retry' ? 'Retrying…' : 'Retry'}
            </button>
          )}
          {proposal.source === 'agent' && parsed.nodes.length > 0 && !proposal.error && (
            <button className="bpv-btn" onClick={() => setProposal({ ...proposal, phase: 'review' })}>
              Review now
            </button>
          )}
          <span className="bpv-spacer" />
          <button className="bpv-btn is-ghost" onClick={cancelDrafting}>
            Cancel
          </button>
        </div>
      </aside>
    );
  }

  const count = checked.size;
  return (
    <aside className="bpv-aside" aria-label="Proposed nodes">
      <div className="bpv-aside-head">
        <span className="bpv-aside-title">
          Lea proposes {parsed.nodes.length} {parsed.nodes.length === 1 ? 'node' : 'nodes'}
        </span>
        <span className="bpv-aside-sub">
          {proposal.source === 'artifacts' ? 'drafted from your formalized declarations' : 'drafted by Lea'}
        </span>
      </div>
      <div className="bpv-aside-body bpv-proposal-list">
        {parsed.nodes.length === 0 && <p className="bpv-note">The draft has no nodes. Edit the markdown or discard it.</p>}
        {parsed.nodes.map((n: BlueprintMdNode, i: number) => {
          const live = graphIndex.get(n.key);
          const file = live?.file ?? displayFile(n);
          const suggested = !live?.file && !n.file && !!file;
          const id = `bpv-acc-${i}`;
          return (
            <div key={n.key} className={`bpv-card${checked.has(n.key) ? '' : ' is-off'}`}>
              <input type="checkbox" id={id} checked={checked.has(n.key)} onChange={() => toggle(n.key)} />
              <div className="bpv-card-body">
                <label htmlFor={id} className="bpv-card-title">
                  <span className="bpv-mono">{n.key}</span>
                  <span className="bpv-pill is-planned">{n.kind ?? 'node'}</span>
                </label>
                {n.statement && <div className="bpv-card-stmt">{firstLine(n.statement)}</div>}
                <div className="bpv-card-file">
                  <FileText size={12} />
                  {file ? (
                    <>
                      <span className="bpv-mono">{file}</span>
                      {suggested && <span className="is-muted">suggested</span>}
                    </>
                  ) : (
                    <span className="is-muted">no file yet</span>
                  )}
                </div>
                {n.uses.length > 0 && (
                  <div className="bpv-card-uses">
                    uses <span className="bpv-mono">{n.uses.join(', ')}</span>
                  </div>
                )}
              </div>
            </div>
          );
        })}
        {proposal.notes && proposal.notes.length > 0 && (
          <ul className="bpv-notes">
            {proposal.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        )}
        {error && <p className="bpv-note is-error">{error}</p>}
      </div>
      <div className="bpv-aside-foot">
        <button className="bpv-btn is-primary" onClick={accept} disabled={busy !== null || count === 0}>
          {busy === 'accept' ? 'Saving…' : `Accept ${count} ${count === 1 ? 'node' : 'nodes'}`}
        </button>
        <button className="bpv-btn" onClick={onEditMarkdown} disabled={busy !== null}>
          Edit markdown
        </button>
        <span className="bpv-spacer" />
        <button className="bpv-btn is-ghost" onClick={discard} disabled={busy !== null}>
          {busy === 'discard' ? 'Discarding…' : 'Discard'}
        </button>
      </div>
    </aside>
  );
}
