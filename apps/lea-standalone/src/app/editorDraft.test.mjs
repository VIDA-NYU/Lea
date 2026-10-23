import assert from 'node:assert/strict';
import test from 'node:test';
import { EditorDraft } from './lib/editorDraft.mjs';

const info = {path:'Proof.lean', file_revision:'file-A', formalization_revision:'proof-A'};
test('a dirty draft pins both revisions across metadata refresh and retries', () => {
  const draft = new EditorDraft('session', 'proof');
  const loaded = {...info};
  draft.load(loaded);
  const save = draft.prepare('my draft');
  loaded.file_revision = 'file-B'; // A background analysis observation is independent.
  assert.equal(draft.prepare('my draft'), save);
  assert.equal(save.base.file_revision, 'file-A');
  assert.equal(save.base.formalization_revision, 'proof-A');
  assert.equal(draft.prepare('new typing').base, save.base);
  assert.throws(() => draft.acknowledge(save, {draft_id:'another',file_revision:'file-C'}), /receipt/);
  assert.equal(draft.base, save.base);
});
test('only a matching publication or explicit reload moves the draft base', () => {
  const draft = new EditorDraft('session', 'proof');
  draft.load({...info,file_revision:'file-A'});
  const save = draft.prepare('published');
  draft.prepare('typed while publishing');
  assert.equal(draft.acknowledge(save, {draft_id:save.draft_id,file_revision:'file-C',revision_token:'proof-C'}), true);
  const next = draft.prepare('typed while publishing');
  assert.equal(next.base.file_revision,'file-C');
  assert.equal(next.base.formalization_revision,'proof-C');
  draft.load({...info,file_revision:'reloaded'});
  assert.equal(draft.acknowledge(next,{draft_id:next.draft_id,file_revision:'obsolete'}),false);
  assert.equal(draft.base.file_revision,'reloaded');
});
test('editable scoped content requires an atomic file and formalization revision', () => {
  assert.throws(() => new EditorDraft('s','f').load({path:'Proof.lean',file_revision:'A'}), /revision/);
});
