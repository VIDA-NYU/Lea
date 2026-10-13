# Lean check recovery

Both front ends use the adapter's in-process prover check service. The editor's
WebSocket Lean server remains independent.

## Behavior

A check first uses the persistent daemon for its canonical Lake root. Transport,
protocol, or transient startup failures retire that generation and allow one
retry. If recovery fails, a normal check may use one cold compile with its
remaining budget. Compiler errors are ordinary results, not server outages.

`LEAN_CHECK_TIMEOUT` (default 900 seconds) is now the **whole check budget**:
queueing, initialization, checking, retry, and cold compilation. Initialization
also has a 120-second cap. Cancellation and expired budgets never start fallback.
Owned-process cleanup has a separate five-second grace period.

Recovery backoff is 30, 60, 120, 240, then 300 seconds, scoped to the Lake root.
After that delay the next check probes recovery; there is no background theorem
check. A completed LSP check, including a compiler error, resets backoff. Concurrent
callers share startup and recovery. A root has at most one serving and one retiring
generation; active checks retain leases until they finish.

- `LEA_COLD_CHECK_CONCURRENCY` retains its default of two.
- `LEA_LSP_RESTART_AFTER` retains its default of 500 checks.
- At most eight idle documents stay open; active and queued documents cannot be evicted.
- `LEA_DISABLE_LSP=1`, `true`, `yes`, or `on` explicitly disables LSP. `0` and `false` do not.
- Explicit cold checks remain supported and are distinguished from fallback.
- Dependency rebuilds invalidate cached daemons. Cascade checks disallow cold
  fallback until it is also verified against the product's Mathlib project layout.

## Evidence

Check results optionally contain `execution`: check ID, daemon generation,
backend (`lsp`, `cold`, `none`), cold reason, classified failure, attempt count
(including the cold attempt, when used),
content SHA-256, and phase/total timing in milliseconds. Tool results remain
strings; evidence belongs to the returned invocation, never a global last-result
slot. Changed files cannot receive a successful verdict for an earlier revision.

Diagnostics preserve recovery/fallback transitions and completed-check evidence.
Failures include the exception, request and document identity, and up to 32 KiB
of server stderr. Structured logs use `lean_check_fallback` for each fallback and
`lean_check_finished` for each completed invocation. Visible warnings deduplicate
within an outage episode; later episodes can be reported again.

`GET /api/sessions/{session_id}/lean-check-runtime` reads current shared runtime
health without starting Lean or creating files. Session details and manual check
responses also contain `lean_check_runtime`. Runtime state is in memory; historical
diagnostics remain in the existing timeline. No schema migration is required.

LeaChat and Overleaf show runtime health separately from proof validity. They poll
while work/degradation is visible, stop when hidden or settled, and refresh on
return. Historical fallback diagnostics remain available after recovery.

## Verification and activation

From `apps/lea-standalone/prover`, using the adapter/prover Python environment:

- `python -m tests.lsp.test_recovery`: deterministic transport, recovery, deadline,
  cancellation, concurrency, evidence, and shutdown tests.
- `python -m tests.lsp.test_recovery_integration`: installed Lean 4.29.0, disposable
  Lake projects; server termination, recovery, fallback, dependency rename,
  worker cleanup, and measured warm/cold timings. Process inspection is needed.
- Existing dispatch, lifecycle, tool-bound, event, interface, and agent suites.

The real-Lean fixtures use core Lean, not the user's large Mathlib project. Their
latencies are diagnostic measurements, not a promised speedup on that project.
Adapter pytest, frontend/Overleaf Node suites, and TypeScript checks cover the
cross-interface behavior. Run adapter tests with an initialized temporary default
DB as well as their per-test databases; URL-validation and SSE tests require DNS
and permission to bind a temporary localhost listener.

Activate the prover/adapter, companion, and frontend changes together. Restart the
adapter and companion, and reload the extension/page, when existing work can stop.
Do not reset local state, edit proof statements, or rewrite historical records.
