// LeaUI v2 frontend API client.
//
// One source of truth for the HTTP + SSE contract the v2 adapter exposes under
// /api. Git owns proof content; the DB is a pointer+verdict index. Both messages
// and code steps carry an authoritative `seq` (the shared timeline, C4), so the
// frontend never reconstructs ordering — it merges on `seq`.

// ── Session-level status (derived in store.list_sessions) ──────────────────────
// 'running' = a session with no code yet but an active run, so a freshly registered
// formalization (including an Overleaf-driven one) shows as in-progress immediately;
// once code exists the working-copy verdict takes over.
export type SessionStatus =
  | 'empty'
  | 'unchecked'
  | 'ok'
  | 'error'
  | 'running'
  | 'proved'
  | 'defined'
  | 'disproved';
// ── Run-level status (a single proof attempt) ─────────────────────────────────
// 'proved' / 'disproved' are checked-artifact outcomes; 'needs_review' is
// preserved as classifier metadata, not a primary session/code status.
// 'answered' = a chat / QA / sketch turn that finished cleanly but proved nothing.
export type RunStatus =
  | 'pending'
  | 'running'
  | 'proved'
  | 'disproved'
  | 'needs_review'
  | 'answered'
  | 'max_turns'
  | 'cancelled'
  | 'failed';

export interface RunSummary {
  id: string;
  status: RunStatus | string;
  focus_formalization_id?: string | null;
  focus_source_hash?: string | null;
  result_kind?: 'proved' | 'disproved' | 'needs_review' | string | null;
  result_detail?: string | null;
}
// ── Per-tool approval gate (D19) ──────────────────────────────────────────────
export type GatedTool = 'bash' | 'write_file' | 'edit_file';
export type ApprovalDecision = 'allow' | 'deny' | 'always_session';

export interface SessionSummary {
  id: string;
  project_id?: string | null;
  title: string;
  status: SessionStatus;
  // Session origin / providence: 'ui' = interactive Lea UI (default), 'overleaf' =
  // spawned from the Overleaf extension. `origin_url` is the canonical Overleaf
  // document URL for an Overleaf-originated session (used to open/focus the source).
  origin?: 'ui' | 'overleaf' | string;
  origin_url?: string | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  run_count: number;
  message_count: number;
  code_step_count: number;
  // v2.3 item 13: count of this session's pending/running runs. Non-zero means a
  // background run is live even when the derived `status` reads a settled verdict
  // (a re-run of an already-proved session), so the sidebar can show a running dot.
  active_run_count?: number;
  primary_model?: string | null;
  models: string[];
  latest_check_status?: 'ok' | 'error' | 'unchecked' | null;
  duration_seconds: number;
  // ── Sub-agent tree (item 24) ────────────────────────────────────────────────
  // A child sub-agent IS a session: `parent_id` is the coordinator that spawned it
  // (null for an ordinary/root session), `role` its subagent_type, `spawned_at_turn`
  // the coordinator turn it was delegated on. The list ships the whole tree; the
  // sidebar shows roots (`parent_id == null`) and a contextual Sub-agents block scoped
  // to `children(parent_id ?? id)`. A child renders read-only with a provenance bar.
  parent_id?: string | null;
  role?: string | null;
  spawned_at_turn?: number | null;
  // A child's final output (its last agent message) — populated only for children, so
  // the coordinator's spawn box can show a collapsed preview with expand/collapse.
  final_summary?: string | null;
  /** Child sessions only: the task the coordinator delegated, recorded at spawn — so a
   *  RUNNING child is judgeable (it has no summary yet, and its title is three words). */
  task?: string | null;
}

// ── Projects (v2.1) ───────────────────────────────────────────────────────────
// A project is a shared dir + git repo + this index row (D21). The slug is
// immutable and determines the namespace `Lea.<Project>` + repo path (D22).
// `session_count` is present on the list endpoint; `description`/`remote_url` are
// nullable metadata. Instructions/Memory/Blueprint are `.lea/*.md` files, not
// fields here.
export interface Project {
  id: string;
  slug: string;
  title: string;
  description?: string | null;
  namespace: string;
  repo_path: string;
  remote_url?: string | null;
  created_at: string;
  updated_at: string;
  session_count?: number;
}

// GET /api/projects/{id}: the project meta plus its sessions (the project window).
export interface ProjectDetail extends Project {
  sessions: SessionSummary[];
}

export type FormalizationValidity =
  | 'draft'
  | 'planned'
  | 'unchecked'
  | 'failing'
  | 'proved'
  | 'defined'
  | 'disproved'
  | 'needs_review'
  | 'stale';

export interface FormalizationFile {
  formalization_id: string;
  path: string;
  role: 'primary' | 'support' | 'generated';
}

export interface Formalization {
  id: string;
  project_id?: string | null;
  loose_session_id?: string | null;
  display_title: string;
  declaration_name?: string | null;
  kind: string;
  statement?: string | null;
  origin: string;
  origin_key?: string | null;
  source_hash?: string | null;
  validity_status: FormalizationValidity | string;
  activity: {
    status: 'idle' | 'queued' | 'running' | 'waiting_for_approval' | string;
    run_id?: string | null;
  };
  primary_path?: string | null;
  files: FormalizationFile[];
  sessions: Array<{ id: string; title: string; updated_at: string }>;
  safe_verify?: {
    id: string;
    status: SafeVerifyStatus;
    detail?: string | null;
    current: boolean;
  } | null;
  created_at: string;
  updated_at: string;
}

export interface CurrentFormalizationFile extends CodeStep {
  role: 'primary' | 'support' | 'generated';
  blob_id?: string | null;
  blob_sha256?: string | null;
  updating_session_title?: string | null;
}

export interface FormalizationCurrentSnapshot {
  formalization_id: string;
  project_id?: string | null;
  revision_token?: string | null;
  files: CurrentFormalizationFile[];
  last_updated_session?: { id: string; title: string } | null;
  last_updated_at?: string | null;
  conversation?: {
    session_id: string;
    revision_token?: string | null;
    files: CurrentFormalizationFile[];
    last_updated_at?: string | null;
    is_current: boolean;
  } | null;
  validity_status: FormalizationValidity | string;
  safe_verify?: Formalization['safe_verify'];
}

// An uploaded reference doc (D27). Bytes live in the project repo under
// `.lea/files/`; this row is the pointer + extraction metadata. `extracted_path`
// is the `.txt` sidecar for Tier-2 (pdf/docx); null for native text + images.
export interface ProjectFile {
  id: string;
  project_id: string;
  filename: string;
  stored_path: string;
  mime?: string | null;
  kind: string;
  extracted_path?: string | null;
  created_at: string;
}

// ── Skills (Skill Factory, v2.1.1) ────────────────────────────────────────────
// A skill is a DB row: a markdown `body` (procedural knowledge) injected into the
// prover's system prompt for the runs it resolves for. Scope (D47): `is_global`
// → every project; else the projects in `project_ids`; loose sessions get none.
// `source_url`/`source_ref` record GitHub provenance for an imported skill.
// The guided authoring answers (v2.5 C1). Stored alongside the compiled text so they
// stay editable; the server compiles them into what the model reads.
export interface AuthoringFieldValues {
  summary?: string;
  when_to_use?: string;
  when_not_to_use?: string;
  how?: string;
}

export interface Skill {
  id: string;
  name: string;
  slug: string;
  body: string;
  is_global: boolean;
  project_ids: string[];
  source_url?: string | null;
  source_ref?: string | null;
  authoring?: AuthoringFieldValues;
  // v2.5 H: a real skill is a directory. `file_paths` are its references (paths only —
  // the contents can run to hundreds of KB). `triggers` gate when it applies.
  description?: string | null;
  file_paths?: string[];
  triggers?: string[];
  created_at: string;
  updated_at: string;
}

// An MCP server the user has configured (v2.5 E0). Same library shape as Skill —
// both are things a project selects, so they share `is_global` / `project_ids`.
// NOTE: no field here ever holds a secret. `env` is non-secret literals; a
// credential is NAMED in `env_from` (stdio) or `api_key_name` (remote) and its
// value read from the environment at spawn (A7).
// A declarative HTTP tool (v2.5 F1). `auth_key_name` NAMES a key — the value lives in
// Settings and is read at call time, so nothing here ever holds a secret.
export interface CustomTool {
  id: string;
  name: string;
  slug: string;
  description: string;
  authoring?: AuthoringFieldValues;
  method: string;
  url: string;
  params: Record<string, unknown>;
  headers: Record<string, string>;
  auth_key_name: string | null;
  auth_header: string | null;
  timeout: number | null;
  enabled: boolean;
  is_global: boolean;
  project_ids: string[];
  created_at: string;
  updated_at: string;
}

export type McpTransport = 'stdio' | 'sse' | 'http';

export interface McpServer {
  id: string;
  name: string;
  slug: string;
  transport: McpTransport;
  command: string | null;
  args: string[];
  env: Record<string, string>;
  env_from: string[];
  url: string | null;
  api_key_name: string | null;
  enabled: boolean;
  is_global: boolean;
  project_ids: string[];
  created_at: string;
  updated_at: string;
}

// One row of a session's skills / MCP picker (E0e). `source` says WHERE the current
// state comes from, which is what makes the two tiers legible: 'global'/'project' are
// inherited, 'session'/'session-off' were toggled here. `locked` marks a global item —
// un-ticking it would mean "stop being global", a Library-level decision.
export interface SkillMcpItem {
  id: string;
  name: string;
  slug: string;
  kind: 'skill' | 'mcp_server';
  on: boolean;
  source: 'global' | 'project' | 'session' | 'session-off' | null;
  locked: boolean;
  enabled: boolean;
}

export interface SessionSkillsMcp {
  session_id: string;
  project_id: string | null;
  skills: SkillMcpItem[];
  mcp_servers: SkillMcpItem[];
}

// The result of dry-running a server spec (E0b). `detail` carries the child's real
// stderr on failure — the line that actually says what is wrong.
export interface McpTestResult {
  ok: boolean;
  tool_count: number;
  tools: string[];
  error: string | null;
  reason: string;   // the one line that says what to fix
  detail: string;   // the raw stderr tail, behind a disclosure
}

// ── Sub-agents (D6) ───────────────────────────────────────────────────────────
// A built-in role's tunable settings. `model: null` → inherit the coordinator's model;
// `max_turns: null` → the role's runaway ceiling; `max_cost: null` → uncapped spend.
export interface SubagentSettings {
  model: string | null;
  max_turns: number | null;
  max_cost: number | null;
  system_prompt: string;
  tools: string[];
}
// A role with its vendored `default`, the stored `override` (only the diff-from-default),
// and the `effective` settings actually used at spawn (default merged with override).
export interface SubagentProfile {
  // v2.5 B3: built-in roles can be retuned but not deleted; user roles can be edited
  // and removed outright. `id` is the row id for a user role, the name for a built-in.
  origin?: 'builtin' | 'user';
  id?: string;
  authoring?: AuthoringFieldValues;
  name: string;
  description?: string | null;
  default: SubagentSettings;
  override: Partial<SubagentSettings>;
  effective: SubagentSettings;
}

// ── Blueprint & derived graph (v2.1 Slice 5, D28/D29) ─────────────────────────
// The blueprint is `.lea/blueprint.md` (markdown-canonical); the graph is parsed +
// derived on read. Status is derived from live Lean state, never stored.
export type BlueprintStatus = 'planned' | 'stated' | 'ready' | 'proved' | 'failed';

// A structural warning from the validator (advisory — never blocks a save). `node`
// is the section key it concerns, or null for whole-file issues.
export interface BlueprintWarning {
  node: string | null;
  message: string;
}

// One session that committed a node's `lean:` file, newest first (D29).
export interface GraphNodeSession {
  session_id: string;
  title: string;
  last_at: string;
}

export interface GraphNode {
  key: string;
  kind: string | null;        // definition | lemma | theorem (shape)
  lean: string | null;        // the live decl, once named
  uses: string[];             // dependency keys (edges)
  statement: string;
  file: string | null;        // repo-relative file resolved for the decl, if any
  status: BlueprintStatus;    // derived from live state (color)
  verified: boolean;          // proved AND SafeVerify-audited (audit-grade, above proved)
  sessions: GraphNodeSession[];
  last_modified_by: string | null;
}

export interface GraphEdge {
  from: string;               // dependent node key
  to: string;                 // dependency node key
}

export interface ProjectGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

// ── Global search (v2.1 Slice 7, D41) ─────────────────────────────────────────
// A search hit is a session, tagged with its project (null for loose chats) so the
// ⌘K overlay can section "Loose chats" vs "Inside projects". The only path to a
// project session, which the sidebar hides.
export interface SearchResult {
  id: string;
  result_type?: 'session' | 'formalization';
  title: string;
  status: SessionStatus | FormalizationValidity | string;
  updated_at: string;
  project_id: string | null;
  project_title?: string | null;
  project_namespace?: string | null;
  declaration_name?: string | null;
  formalization_kind?: string | null;
  session_id?: string | null;
  primary_path?: string | null;
}

// ── Filesystem tab (v2.1 Slice 6, D34) ────────────────────────────────────────
// The project repo as a browsable tree. A dir carries `children`; a file carries
// `size`. `path` is repo-relative POSIX. `.git/`/`.lake/` are hidden server-side.
export interface TreeEntry {
  name: string;
  path: string;
  type: 'dir' | 'file';
  size?: number;
  children?: TreeEntry[];
}

export type GithubImportDisposition =
  | 'add'
  | 'already_present'
  | 'path_conflict'
  | 'declaration_conflict'
  | 'unsupported_module_layout'
  | 'excluded';

export interface GithubImportDeclarationMatch {
  declaration_name: string;
  formalization_id?: string | null;
  origin_key?: string | null;
  display_title: string;
  source_hash?: string | null;
}

export interface GithubImportPlannedDeclaration {
  short_name: string;
  full_name: string;
  keyword: string;
  kind: string;
  start_line: number;
  end_line: number;
  match?: GithubImportDeclarationMatch | null;
}

export interface GithubImportPlannedFile {
  source_path: string;
  destination_path?: string | null;
  disposition: GithubImportDisposition;
  reason: string;
  content_sha256?: string | null;
  module_name?: string | null;
  declarations?: GithubImportPlannedDeclaration[];
  code_step_id?: number | null;
  check_status?: 'pending' | 'ok' | 'error' | null;
  check_detail?: string | null;
}

export interface GithubImportPreview {
  preview_id: string;
  expires_in_seconds: number;
  source: {
    url: string;
    owner: string;
    repository: string;
    ref?: string | null;
    commit_sha: string;
  };
  project: { id: string; slug: string; namespace: string };
  plan: {
    source_namespace?: string | null;
    destination_namespace: string;
    destination_snapshot: string;
    files: GithubImportPlannedFile[];
    counts: Partial<Record<GithubImportDisposition, number>>;
    matched_declarations: number;
    reusable_declarations: number;
    blocking_error?: { code: string; message: string } | null;
  };
}

export interface GithubImportProgress {
  id: string;
  project_id: string;
  source_url: string;
  source_commit_sha: string;
  status: 'applying' | 'checking' | 'complete' | 'complete_with_issues' | 'failed';
  commit_sha?: string | null;
  error_detail?: string | null;
  reused?: boolean;
  files: GithubImportPlannedFile[];
  declarations: Array<{
    id: string;
    destination_path: string;
    declaration_name: string;
    full_name: string;
    kind: string;
    module_name: string;
    formalization_id?: string | null;
  }>;
  counts: {
    dispositions: Partial<Record<GithubImportDisposition, number>>;
    checks: Record<string, number>;
    matched_declarations: number;
    reusable_declarations: number;
  };
}

export interface ChatMessage {
  id: string;
  session_id: string;
  run_id?: string | null;
  formalization_id?: string | null;
  role: 'user' | 'assistant';
  content: string;
  kind?: 'assistant' | 'edit_note' | string;
  seq?: number;
  created_at: string;
  // Frontend-only marker for the streaming bubble before its persisted twin lands.
  live?: boolean;
}

export interface CodeStep {
  id: string;
  session_id: string;
  run_id?: string | null;
  formalization_id?: string | null;
  seq?: number;
  turn?: number | null;
  author: 'agent' | 'user' | 'environment';
  path: string;
  summary?: string | null;
  // True when the step's content couldn't be recovered (a pre-v2.3 row whose git
  // pointer didn't resolve). The step still exists — it says something happened.
  content_lost?: boolean;
  check_status?: 'ok' | 'error' | 'unchecked' | null;
  check_detail?: string | null;
  artifact_kind?: 'proof' | 'definition' | 'mixed' | 'unknown' | string | null;
  created_at: string;
  // The step's content: SQL owns it (v2.3), so it arrives with the row on read and
  // on the SSE `code_step` event alike. No hydrate step, no empty-canvas failure.
  code: string;
}

export interface StatusEvent {
  id: string;
  session_id?: string;
  run_id?: string;
  step_number?: number | null;
  status?: string | null;
  message: string;
  turn?: number | null;
  check_status?: 'ok' | 'error' | null;
  check_detail?: string | null;
  created_at: string;
}

// A paused tool call awaiting a human decision (live only).
export interface PendingApproval {
  approval_id: string;
  run_id: string;
  session_id: string;
  tool_name: GatedTool | string;
  args: Record<string, unknown>;
}

// An approval as kept in the thread history: gains a `decision` once resolved
// (so denied/allowed cards stay visible) + a synthetic `seq` for interleaving.
export type ApprovalRecord = PendingApproval & { decision?: string | null; seq?: number };

export interface ApprovalEvent {
  id: string;
  session_id?: string;
  run_id?: string;
  approval_id: string;
  tool_name?: string | null;
  args?: Record<string, unknown> | null;
  decision?: ApprovalDecision | string | null;
  resolved_at?: string | null;
}

export type SafeVerifyStatus = 'ok' | 'rejected' | 'error' | 'unavailable' | 'running' | 'pending';

export interface SafeVerifyResult {
  run_id?: string;
  status: SafeVerifyStatus;
  detail?: string | null;
}

export interface UsageBreakdownRow {
  id: string;
  session_id?: string;
  run_id?: string;
  run_number: number;
  ordinal: number;
  phase: string;
  label: string;
  turn?: number | null;
  candidate?: number | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  event_count: number;
  created_at?: string;
}

export interface ActiveRun {
  id: string;
  status: RunStatus | string;
  model?: string;
  pending_approval?: PendingApproval | null;
  result_kind?: string | null;
  result_detail?: string | null;
  focus_formalization_id?: string | null;
  focus_source_hash?: string | null;
}

/**
 * A failure surfaced to the human (v2.4). The one shape for every error channel:
 * streamed live as a `diagnostic` SSE, persisted on the timeline so it survives a
 * reload, and returned in a `warnings: Diagnostic[]` array by request/response
 * endpoints that can't stream (the graph, a rename, an upload, the model catalog).
 *
 * `severity` picks the SURFACE, not just the color:
 *   fatal      — the run ended here; shown as the run's outcome
 *   step_error — one step failed, run continued; renders on that step
 *   degraded   — a capability is reduced and STILL IS; a persistent indicator
 *   notice     — something silently didn't apply
 *
 * `context` is what anchors it. A diagnostic naming a `path`/`step_id` renders on
 * that code card, one naming a `child_id` on that sub-agent row, one naming a
 * `tool` on that tool step. With no anchor it falls back to the run-level block —
 * never dropped, which is what the old single `error` string did to every failure
 * after the first.
 */
export type DiagnosticSeverity = 'fatal' | 'step_error' | 'degraded' | 'notice';

/** An offer rendered as a button on a diagnostic card. A remedy tells you what to do;
 *  an action takes you there. */
export interface DiagnosticAction {
  label: string;
  // v2.5 G4: `open-library` points at Skills / Sub-agents / MCP servers, so a remedy
  // about a capability can take the user to it instead of describing where it lives.
  action: 'open-settings' | 'open-library';
  focus?: 'api-keys' | 'model' | 'skills' | 'subagents' | 'mcp' | string;
}

export interface Diagnostic {
  id?: string;
  session_id?: string;
  run_id?: string | null;
  severity: DiagnosticSeverity;
  code: string;
  title: string;
  message: string;
  /** The raw exception, when a friendlier provider message is leading. Shown
   *  collapsed — kept so nothing is hidden, demoted so it isn't read first. */
  detail?: string | null;
  remedy?: string | null;
  actions?: DiagnosticAction[];
  source?: string;
  turn?: number | null;
  seq?: number;
  created_at?: string;
  /** False when the row could not be written — shown, but not durable. */
  persisted?: boolean;
  context: {
    turn?: number | null;
    tool?: string;
    path?: string;
    step_id?: string;
    child_id?: string;
    child_result_id?: string;
    approval_id?: string;
    project_id?: string;
    project_slug?: string;
    [key: string]: unknown;
  };
}

export interface SessionDetail extends SessionSummary {
  messages: ChatMessage[];
  code_steps: CodeStep[];
  /** Persisted failures for this session, in timeline order (G1). */
  diagnostics?: Diagnostic[];
  status_events: StatusEvent[];
  approval_events: ApprovalEvent[];
  usage_breakdown: UsageBreakdownRow[];
  active_run?: ActiveRun | null;
  runs?: RunSummary[];
  safe_verify?: SafeVerifyResult | null;
  formalizations?: Formalization[];
  formalization_summary?: Record<string, number>;
  latest_focus_formalization_id?: string | null;
}

// ── SSE event payloads (GET /api/runs/{run_id}/events) ─────────────────────────
export interface AssistantDeltaEvent { text: string }
export interface RunStatusEventPayload {
  status: string;
  message: string;
  turn?: number;
  check_status?: 'ok' | 'error';
  check_detail?: string | null;
}
export interface ApprovalResolvedEvent { approval_id: string; decision: ApprovalDecision }
// A child sub-agent finished (item 24): the coordinator delegated a subtask and got a
// distilled result back. The adapter has already persisted the child as a session; this
// event tells the browser to pull it in live (so the Sub-agents block + spawn node appear
// without a reload).
export interface SubagentFinishedEvent {
  child_id: string;
  parent_id: string;
  run_id?: string;
  result_id: string;
  subagent_type: string;
  role: string;
  turn?: number | null;
  title: string;
  check_status?: 'ok' | 'error' | null;
  check_detail?: string | null;
  stop_reason: string;
  summary: string;
  candidate_path?: string | null;
}
export interface RunErrorEvent { message: string }
export interface DoneEvent {
  status: RunStatus;
  result_kind?: string | null;
  result_detail?: string | null;
}

// ── Frontend-derived timeline (built from messages + code steps by timeline.mjs) ──
export type TimelineItem =
  | { kind: 'message'; key: string; message: ChatMessage }
  | { kind: 'code'; key: string; step: CodeStep; codeIndex: number };
