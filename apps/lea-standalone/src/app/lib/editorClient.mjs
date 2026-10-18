// Immutable buffer publication + typed commands. All observations are SQL reads.
export class EditorStateError extends Error {
  constructor(state) { state = typeof state === 'string' ? state : 'invalid_editor_response'; super(state.replaceAll('_', ' ')); this.state = state; }
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
export class EditorClient {
  constructor(sessionId, path, { fetch: request = globalThis.fetch.bind(globalThis), onState = state => { void state; } } = {}) {
    this.sessionId = sessionId;
    Object.assign(this, { sessionId, path, fetch: request, onState, revision: 0, version: 0, sequence: 0, disposed: false, serial: Promise.resolve() });
  }
  async json(url, init = {}) {
    const response = await this.fetch(url, { ...init, signal: init.signal || AbortSignal.timeout(6000), headers: { 'Content-Type': 'application/json', ...(this.owner ? { Authorization: `Bearer ${this.owner.credential}` } : {}), ...init.headers } });
    const value = await response.json();
    if (!response.ok) throw new EditorStateError(value.detail?.error || value.detail || `editor_http_${response.status}`);
    return value;
  }
  async info() { return this.json(`/api/sessions/${encodeURIComponent(this.sessionId)}/editor${this.path ? `?path=${encodeURIComponent(this.path)}` : ''}`); }
  async open() {
    const info = await this.info(); this.path = info.path;
    this.owner = await this.json(`/api/sessions/${encodeURIComponent(this.sessionId)}/editor/clients`, { method: 'POST' });
    this.content = info.content; this.lastInfo = info; return info;
  }
  async refresh() {
    const info = await this.info();
    if (this.disposed) return false;
    const changed = Boolean(this.invalidated || (!this.activeRequests && this.retryKinds?.size) || (this.buffer && Date.parse(this.buffer.expires_at) <= Date.now())) || info.environment_revision !== this.lastInfo?.environment_revision || info.file_revision !== this.lastInfo?.file_revision || info.worker?.epoch !== this.lastInfo?.worker?.epoch || info.worker?.state !== this.lastInfo?.worker?.state;
    this.lastInfo = info; this.invalidated = !info.environment_revision;
    if (changed) { this.revision++; this.buffer = null; this.onState(info.environment_revision ? 'pending' : info.analysis_state || 'environment_unavailable'); }
    return changed;
  }
  invalidate(state = 'environment_unavailable') { this.invalidated = true; this.revision++; this.buffer = null; this.onState(state); }
  setContent(content) { if (content !== this.content) { this.content = content; this.revision++; this.onState('pending'); } }
  dispose() {
    if (this.disposed) return;
    this.disposed = true; this.revision++;
    if (this.owner) {
      const credential = this.owner.credential;
      Promise.resolve().then(() => this.fetch('/api/editor/clients/current', { method: 'DELETE', keepalive: true,
        headers: { Authorization: `Bearer ${credential}` }, signal: AbortSignal.timeout(2000) })).catch(() => {});
    }
  }
  assertCurrent(revision) { if (this.disposed || revision !== this.revision) throw new EditorStateError('superseded'); }
  async stablePost(url, value, lookup) {
    const init = { method: 'POST', body: JSON.stringify(value) };
    try { return await this.json(url, init); }
    catch (error) {
      if (error instanceof EditorStateError) throw error;
      if (lookup) { try { return await this.json(lookup); } catch { /* exact replay recovers a lost acknowledgement */ } }
      return this.json(url, init);
    }
  }
  async publish(revision) {
    const work = this.serial.catch(() => {}).then(async () => {
      this.assertCurrent(revision);
      const info = await this.info(); this.assertCurrent(revision); this.lastInfo = info;
      if (!info.environment_revision) throw new EditorStateError(info.analysis_state || 'environment_unavailable');
      if (this.buffer && this.publishedRevision === revision && this.buffer.base_revision === info.file_revision && this.buffer.environment_revision === info.environment_revision && Date.parse(this.buffer.expires_at) > Date.now()) return this.buffer;
      const value = { version: ++this.version, path: this.path, base_revision: info.file_revision, environment_revision: info.environment_revision, content: this.content };
      let buffer;
      try { buffer = await this.stablePost('/api/editor/buffers', value); }
      catch (error) {
        if (error.state !== 'editor_owner_expired') throw error;
        this.buffer = null;
        this.owner = await this.json(`/api/sessions/${encodeURIComponent(this.sessionId)}/editor/clients`, { method: 'POST' });
        this.assertCurrent(revision); buffer = await this.stablePost('/api/editor/buffers', value);
      }
      this.assertCurrent(revision); this.buffer = buffer; this.publishedRevision = revision; return buffer;
    });
    this.serial = work; return work;
  }
  async request(kind, position) {
    const revision = this.revision;
    let sequence;
    this.activeRequests = (this.activeRequests || 0) + 1;
    try {
      const buffer = await this.publish(revision); this.assertCurrent(revision);
      sequence = ++this.sequence; const key = crypto.randomUUID();
      const value = { version: 1, kind, buffer_id: buffer.id, environment_revision: buffer.environment_revision,
        idempotency_key: key, sequence, supersession_key: 'main', deadline: new Date(Date.now() + 4900).toISOString(), ...(position ? { position } : {}) };
      this.latest ??= new Map(); this.latest.set(kind, sequence); this.onState('pending');
      let result = await this.stablePost('/api/editor/requests', value, `/api/editor/receipts?idempotency_key=${encodeURIComponent(key)}`);
      while (['queued', 'claimed'].includes(result.state)) {
        this.assertCurrent(revision);
        if (this.latest.get(kind) !== sequence) throw new EditorStateError('superseded');
        if (Date.now() > Date.parse(value.deadline) + 1000) throw new EditorStateError('worker_unavailable');
        await pause(75);
        try { result = await this.json(`/api/editor/requests/${encodeURIComponent(result.id)}`); }
        catch (error) { if (error instanceof EditorStateError) throw error; await pause(100); }
      }
      this.assertCurrent(revision);
      if (this.latest.get(kind) !== sequence || result.buffer?.id !== buffer.id || this.buffer?.id !== buffer.id) throw new EditorStateError('superseded');
      if (!result.current) throw new EditorStateError(result.result_state || result.error || 'analysis_unavailable');
      this.retryKinds?.delete(kind); this.onState('available'); return result.result?.value ?? null;
    } catch (error) {
      if (!this.disposed && revision === this.revision && (!sequence || this.latest.get(kind) === sequence) && error.state !== 'superseded') {
        const state = error.state || 'worker_unavailable';
        if (!['unsupported_method', 'confined_editor_executor_unavailable', 'editor_scope_changed', 'scope_changed', 'editor_request_quota', 'editor_buffer_quota', 'editor_result_quota'].includes(state)) {
          this.retryKinds ??= new Set(); this.retryKinds.add(kind);
        }
        this.onState(state);
      }
      throw error;
    } finally { this.activeRequests--; }
  }
}
