import { useEffect, useMemo, useState } from 'react';
import { FileText, LoaderCircle } from 'lucide-react';
import type { ProjectBlueprint, ProjectGraph } from '../../lib/api';
import {
  acceptDraft,
  acceptProposal,
  cancelDrafting,
  discardDraft,
  discardProposal,
  openOrDraftBlueprint,
} from '../../lib/blueprintActions';
import { byKey, displayFile, firstLine, parseBlueprintMarkdown } from '../../lib/blueprintModel.js';
import { useWorkbench, type BlueprintProposal } from '../../stores/workbench';

// The proposal aside (mockup frame 8): "Lea proposes N nodes", one card per node with
// a checkbox, the kind, a one-line statement, the Lean name and file and its
// dependencies; Accept N nodes / Edit markdown / Discard. While the draft is still
// being produced it shows the drafting state with the nodes so far.
//
// Two sources (stores/workbench BlueprintProposal):
// - agent: a `blueprint_draft` run wrote .lea/blueprint.draft.md through the
//   blueprint tools; the cards are `draft.nodes` from GET /blueprint, with the
//   Lean name and file the system assigned. Accept → POST draft/accept with the
//   checked keys; Discard → POST draft/discard; Edit markdown → the draft editor.
// - artifacts: the seeder wrote the live file; Accept writes the checked nodes
//   back, Discard empties it.

interface Row {
  key: string;
  kind: string | null;
  statement: string;
  lean: string | null;
  file: string | null;
  /** The file is a suggestion derived from the Lean name, not a recorded one. */
  suggested: boolean;
  uses: string[];
}

export function ProposalPanel({
  projectId,
  proposal,
  doc,
  graph,
  onEditMarkdown,
  onOpenSession,
}: {
  projectId: string;
  proposal: BlueprintProposal;
  doc: ProjectBlueprint;
  graph: ProjectGraph | null;
  onEditMarkdown: () => void;
  onOpenSession?: (sessionId: string) => void;
}) {
  const setProposal = useWorkbench((s) => s.setBlueprintProposal);
  const agent = proposal.source === 'agent';
  const parsedLive = useMemo(() => parseBlueprintMarkdown(doc.content), [doc.content]);
  const graphIndex = useMemo(() => byKey(graph?.nodes ?? []), [graph]);
  const rows: Row[] = useMemo(() => {
    if (agent) {
      return (doc.draft?.nodes ?? []).map((n) => ({
        key: n.key,
        kind: n.kind,
        statement: n.statement,
        lean: n.lean,
        file: n.file,
        suggested: false,
        uses: n.uses ?? [],
      }));
    }
    return parsedLive.nodes.map((n) => {
      const live = graphIndex.get(n.key);
      const file = live?.file ?? displayFile(n);
      return { key: n.key, kind: n.kind, statement: n.statement, lean: n.lean, file, suggested: !live?.file && !n.file && !!file, uses: n.uses };
    });
  }, [agent, doc.draft, parsedLive, graphIndex]);
  const keys = useMemo(() => [...new Set(rows.map((r) => r.key))], [rows]);
  const [checked, setChecked] = useState<Set<string>>(() => new Set(keys));
  // Keys the user unticked, so a refetch (the agent still writing) does not re-tick them.
  const [removed] = useState(() => new Set<string>());
  const [busy, setBusy] = useState<'accept' | 'discard' | 'retry' | 'cancel' | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A node that appears after a refetch starts checked; a node that left the draft is dropped.
  useEffect(() => {
    setChecked((cur) => {
      const next = new Set<string>();
      for (const k of keys) if (cur.has(k) || !removed.has(k)) next.add(k);
      return next;
    });
  }, [keys, removed]);

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

  const run = async (kind: 'accept' | 'discard' | 'retry' | 'cancel', action: () => Promise<unknown>) => {
    setBusy(kind);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const accept = () =>
    run('accept', async () => {
      const keep = keys.filter((k) => checked.has(k));
      if (agent) {
        const outcome = await acceptDraft(projectId, keep);
        if (outcome.applied.length === 0 && outcome.rejected.length > 0) {
          throw new Error(`Nothing was accepted: ${outcome.rejected.map((r) => `${r.key} (${r.message || r.code})`).join('; ')}`);
        }
        return;
      }
      await acceptProposal(projectId, parsedLive.nodes.filter((n) => checked.has(n.key)), parsedLive.preamble, parsedLive.blocks);
    });

  const discard = () => run('discard', () => (agent ? discardDraft(projectId) : discardProposal(projectId)));

  const retry = () =>
    run('retry', () => openOrDraftBlueprint(projectId, { sessionId: proposal.draftingSessionId, openSession: onOpenSession }));

  const cancel = () =>
    run('cancel', async () => {
      if (agent) await cancelDrafting(projectId, proposal.draftingRunId);
      else setProposal(null);
    });

  const warnings = agent ? doc.draft?.warnings ?? [] : [];
  const cards = (
    <>
      {rows.map((r, i) => {
        const id = `bpv-acc-${i}`;
        const on = checked.has(r.key);
        return (
          <div key={`${r.key}:${i}`} className={`bpv-card${on ? '' : ' is-off'}`}>
            <input type="checkbox" id={id} checked={on} onChange={() => toggle(r.key)} disabled={proposal.phase === 'drafting'} />
            <div className="bpv-card-body">
              <label htmlFor={id} className="bpv-card-title">
                <span className="bpv-mono">{r.key}</span>
                <span className="bpv-pill is-planned">{r.kind ?? 'node'}</span>
              </label>
              {r.statement && <div className="bpv-card-stmt">{firstLine(r.statement)}</div>}
              {agent && r.lean && (
                <div className="bpv-card-file" title="The Lean name the system assigned">
                  <span>declares</span>
                  <span className="bpv-mono">{r.lean}</span>
                </div>
              )}
              <div className="bpv-card-file">
                <FileText size={12} />
                {r.file ? (
                  <>
                    <span className="bpv-mono">{r.file}</span>
                    {r.suggested && <span className="is-muted">suggested</span>}
                  </>
                ) : (
                  <span className="is-muted">no file yet</span>
                )}
              </div>
              {r.uses.length > 0 && (
                <div className="bpv-card-uses">
                  uses <span className="bpv-mono">{r.uses.join(', ')}</span>
                </div>
              )}
            </div>
          </div>
        );
      })}
    </>
  );
  const notes = proposal.notes && proposal.notes.length > 0 && (
    <ul className="bpv-notes">
      {proposal.notes.map((n, i) => (
        <li key={i}>{n}</li>
      ))}
    </ul>
  );
  const warningList = warnings.length > 0 && (
    <ul className="bpv-notes">
      {warnings.map((w, i) => (
        <li key={i}>
          {w.node ? <span className="bpv-mono">{w.node}: </span> : null}
          {w.message}
        </li>
      ))}
    </ul>
  );

  if (proposal.phase === 'drafting') {
    return (
      <aside className="bpv-aside" aria-label="Drafting the blueprint">
        <div className="bpv-aside-head">
          <span className="bpv-aside-title">{proposal.error ? 'Drafting failed' : 'Lea is drafting the blueprint'}</span>
          {agent && rows.length > 0 && !proposal.error && (
            <span className="bpv-aside-sub">
              {rows.length} {rows.length === 1 ? 'node' : 'nodes'} so far
            </span>
          )}
        </div>
        <div className="bpv-aside-body bpv-proposal-list">
          {proposal.error ? (
            <p className="bpv-note is-error">{proposal.error}</p>
          ) : !agent ? (
            <p className="bpv-note">
              <LoaderCircle size={13} className="bpv-spin" /> Reading the project's formalized declarations…
            </p>
          ) : (
            <p className="bpv-note">
              <LoaderCircle size={13} className="bpv-spin" /> No proofs to seed from yet, so Lea is drafting the plan in a run.
              Its nodes appear here with the Lean names and files the system assigns; you review them once the run settles.
            </p>
          )}
          {agent && cards}
          {warningList}
          {notes}
          {error && <p className="bpv-note is-error">{error}</p>}
        </div>
        <div className="bpv-aside-foot">
          {proposal.error && (
            <button className="bpv-btn is-primary" onClick={() => void retry()} disabled={busy !== null}>
              {busy === 'retry' ? 'Retrying…' : 'Retry'}
            </button>
          )}
          {agent && rows.length > 0 && !proposal.error && (
            <button className="bpv-btn" onClick={() => setProposal({ ...proposal, phase: 'review' })} disabled={busy !== null}>
              Review now
            </button>
          )}
          {agent && proposal.draftingSessionId && onOpenSession && (
            <button className="bpv-btn is-ghost" onClick={() => onOpenSession(proposal.draftingSessionId!)} disabled={busy !== null}>
              Open the run
            </button>
          )}
          <span className="bpv-spacer" />
          <button
            className="bpv-btn is-ghost"
            onClick={() => void cancel()}
            disabled={busy !== null}
            title={agent ? 'Stop the drafting run and delete its draft' : undefined}
          >
            {busy === 'cancel' ? 'Cancelling…' : 'Cancel'}
          </button>
        </div>
      </aside>
    );
  }

  const count = keys.filter((k) => checked.has(k)).length;
  return (
    <aside className="bpv-aside" aria-label="Proposed nodes">
      <div className="bpv-aside-head">
        <span className="bpv-aside-title">
          Lea proposes {rows.length} {rows.length === 1 ? 'node' : 'nodes'}
        </span>
        <span className="bpv-aside-sub">
          {agent ? 'drafted by Lea · names and files assigned by the system' : 'drafted from your formalized declarations'}
        </span>
      </div>
      <div className="bpv-aside-body bpv-proposal-list">
        {rows.length === 0 && <p className="bpv-note">The draft has no nodes. Edit the markdown or discard it.</p>}
        {cards}
        {warningList}
        {notes}
        {error && <p className="bpv-note is-error">{error}</p>}
      </div>
      <div className="bpv-aside-foot">
        <button className="bpv-btn is-primary" onClick={() => void accept()} disabled={busy !== null || count === 0}>
          {busy === 'accept' ? 'Saving…' : `Accept ${count} ${count === 1 ? 'node' : 'nodes'}`}
        </button>
        <button className="bpv-btn" onClick={onEditMarkdown} disabled={busy !== null}>
          Edit markdown
        </button>
        <span className="bpv-spacer" />
        <button className="bpv-btn is-ghost" onClick={() => void discard()} disabled={busy !== null}>
          {busy === 'discard' ? 'Discarding…' : 'Discard'}
        </button>
      </div>
    </aside>
  );
}
