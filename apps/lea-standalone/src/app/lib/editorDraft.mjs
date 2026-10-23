// A save base belongs to the loaded bytes, independently of language analysis
// and background snapshots. Reload and acknowledged publication alone move it.
export class EditorDraft {
  constructor(sessionId, formalizationId) {
    Object.assign(this, { sessionId, formalizationId, generation: 0 });
  }
  load(info) {
    if (!info.file_revision || (this.formalizationId && !info.formalization_revision)) {
      throw new Error('The published draft revision is unavailable. Refresh the services before saving.');
    }
    this.generation++;
    this.base = Object.freeze({ session_id: this.sessionId, path: info.path,
      formalization_id: this.formalizationId, file_revision: info.file_revision,
      formalization_revision: info.formalization_revision ?? undefined });
    this.pending = null;
  }
  prepare(content) {
    if (!this.base) throw new Error('Load a published revision before saving.');
    const pending = this.pending;
    if (pending && pending.content === content && pending.base === this.base) return pending;
    const submitted = Object.freeze({ content, base: this.base, generation: this.generation,
      draft_id: crypto.randomUUID() });
    this.pending = submitted;
    return submitted;
  }
  acknowledge(submitted, receipt) {
    if (submitted.generation !== this.generation || submitted.base !== this.base) return false;
    if (receipt.draft_id !== submitted.draft_id || !receipt.file_revision
        || (this.formalizationId && !receipt.revision_token)) {
      throw new Error('The publication receipt does not identify the submitted draft.');
    }
    this.base = Object.freeze({ ...this.base, file_revision: receipt.file_revision,
      formalization_revision: receipt.revision_token ?? this.base.formalization_revision });
    this.pending = null;
    return true;
  }
}
