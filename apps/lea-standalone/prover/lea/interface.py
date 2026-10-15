"""The prover's public surface — the one import the LeaUI adapter uses.

The prover exposes three capabilities (architecture D2):
  - run_events(...)  the agent loop                          (re-exported — A7)
  - check(path)      lean_check on a file, no model run      (A5, here)
  - verify(path)     SafeVerify on a file, no model run      (A6, here)
plus the typed event classes.

`check` / `verify` are standalone: give them a file path, they run the check and
hand back a structured verdict (a CheckResult / VerifyResult). No agent run — so the
adapter can verify a user-edited file on demand (the writeable canvas).
"""

import tempfile
from pathlib import Path

from . import safeverify
from .agent import run_events
from .events import (
    AgentEvent,
    AssistantTextDelta,
    CheckResult,
    Compacted,
    Diagnostic,
    DependencyReadinessRequested,
    DependencyReadinessAck,
    Error,
    FileChanged,
    Finished,
    SubagentFinished,
    SubagentProgress,
    SubagentStarted,
    LeaStatusUpdateRequested,
    LeaStatusUpdateAck,
    ProjectLibraryRequested,
    ProjectLibraryAck,
    ToolApprovalRequested,
    ToolCalled,
    ToolResulted,
    TurnStarted,
    UsageUpdated,
    VerifyResult,
)
from .subagents import request_child_stop
from .tools import lean_check, lean_check_cold, rebuild_module, _lean_check_has_error, _first_error_line

__all__ = [
    # the three capabilities (D2)
    "run_events",
    "check",
    "verify",
    "rebuild",
    # the typed event stream (D17) — one import for every event type
    "AgentEvent",
    "AssistantTextDelta",
    "TurnStarted",
    "ToolCalled",
    "ToolResulted",
    "ToolApprovalRequested",
    "LeaStatusUpdateRequested",
    "LeaStatusUpdateAck",
    "ProjectLibraryRequested",
    "ProjectLibraryAck",
    "UsageUpdated",
    "Compacted",
    "FileChanged",
    "CheckResult",
    "VerifyResult",
    "Error",
    "Diagnostic",
    "DependencyReadinessRequested",
    "DependencyReadinessAck",
    "SubagentStarted",
    "SubagentProgress",
    "SubagentFinished",
    "Finished",
    # per-child cooperative stop (D2)
    "request_child_stop",
]


def check(path: str, *, cold: bool = False, allow_cold: bool = True) -> CheckResult:
    """Run `lean_check` on a file and return a structured verdict.

    No agent run. Uses the same output classifiers as the agent's live CheckResult
    events (tools._lean_check_has_error / _first_error_line), so this verdict can
    never disagree with what a run would report for the same file.

    `cold=True` forces `tools.lean_check_cold`, bypassing the persistent LSP
    daemon entirely, instead of the normal fast path.

    Caution: a live end-to-end test (tests/lsp/test_cascade_rename_integration.py)
    found that `cold=True` does NOT reliably see a just-rebuilt project-local
    module's fresh `.olean` either -- a real Lean/Lake behavior difference
    between `lake env lean <file>` and `lake env lean --server` that's still
    under investigation, not something to build correctness on yet. The
    Overleaf lean pane's cascade re-check of a dependent
    (docs/FEATURE-overleaf-lean-pane-manual-edit.md, "Cascade verification"),
    which is what this parameter was originally added for, does NOT use it --
    it relies instead on `tools.rebuild_module`'s `lsp_daemon.mark_stale` call
    (see that function's docstring), confirmed correct by the same test.
    """
    out = lean_check_cold(path) if cold else (
        lean_check(path) if allow_cold else lean_check(path, allow_cold=False)
    )
    err = _lean_check_has_error(out)
    return CheckResult(
        path,
        "error" if err else "ok",
        _first_error_line(out) if err else None,
        execution=getattr(out, "execution", None),
        content=getattr(out, "captured_content", None),
    )


def rebuild(path: str) -> CheckResult:
    """Force a real `lake build` of a file's module (`tools.rebuild_module`) and
    return a verdict in the same shape as `check`.

    No agent run. `check`'s LSP fast path never updates the compiled `.olean`
    another file's `import` resolves against -- so before trusting a *different*
    file's re-check of something that imports this one, that other file's check
    needs this to have run first, once, for the edited module. Used by the
    Overleaf lean pane's manual-edit cascade
    (docs/FEATURE-overleaf-lean-pane-manual-edit.md, "Cascade verification").
    """
    out = rebuild_module(path)
    err = _lean_check_has_error(out)
    return CheckResult(
        path,
        "error" if err else "ok",
        _first_error_line(out) if err else None,
    )


def _verify_impl(path: str, declaration: str | None = None) -> VerifyResult:
    """Audit a finished proof with SafeVerify (kernel replay + axiom whitelist).

    No agent run. Derives the target from the proof's own main theorem, so it
    catches `sorry`/`axiom`/`native_decide`/shadowing tricks a plain compile
    misses. Expensive (~two compiles + a replay); concurrent calls are bounded by
    a semaphore in `safeverify.verify_proof` (D74/H9, `LEA_SAFEVERIFY_CONCURRENCY`)
    rather than by the adapter run lock, which v2.3 removes.

    status: 'ok' (passed) | 'rejected' (cheat caught) | 'error' (couldn't run /
    no theorem) | 'unavailable' (binary not built). 'error'/'unavailable' are
    deliberately distinct from 'rejected' — "couldn't verify" must never read as
    "verified bad."
    """
    if not safeverify.is_available():
        return VerifyResult("unavailable", "SafeVerify is not built on this server.")

    p = Path(path)
    if not p.exists():
        return VerifyResult("error", f"File not found: {path}")
    code = p.read_bytes().decode('utf-8')
    if declaration:
        from .check_evidence import lake_root, file_hash, file_version
        from .result_selection import inspect_file, select
        try:
            report = inspect_file(p)
            chosen = select(report, declaration)
            root = lake_root(p)
            if not root or report.get('inspection') != 'compiled_module':
                return VerifyResult('error', 'Selected replay requires an actual built Lake module.')
            relative = p.resolve().relative_to(root / 'proofs').with_suffix('.olean')
            olean = root / '.lake/build/lib/lean' / relative
            before_hash = file_hash(olean)
            sv_root = root / '.sv_scratch'
            sv_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=sv_root) as temp:
                ok, detail = safeverify.verify_selected_module(olean, chosen['full_name'], root, Path(temp))
            if file_hash(olean) != before_hash or p.read_bytes().decode('utf-8') != code:
                return VerifyResult('error', 'Selected replay inputs changed during verification.')
            record = {'path': str(olean), 'sha256': before_hash,
                      'version': file_version(olean), 'kind': 'compiled'}
            return VerifyResult('ok' if ok else 'rejected', None if ok else detail,
                {'selected_module': record, 'selected_result': chosen})
        except (ValueError, OSError) as exc:
            return VerifyResult('error', str(exc))

    signature = safeverify.theorem_signature(code)
    if not signature:
        return VerifyResult("error", "Could not find a theorem/lemma to verify in the proof.")

    from .check_evidence import lake_root
    workspace = lake_root(p) or safeverify.WORKSPACE
    # Per-call scratch dir (H2). The old code keyed target/submission by file
    # *stem* in one shared `.sv_scratch`, so two concurrent runs on `Div6.lean`
    # verified A's submission against B's target and the `finally` unlink deleted
    # files the peer was mid-compile on. A unique subdir per call is collision-
    # proof, and its RAII cleanup deletes only this call's files. Kept under
    # `.sv_scratch` (directly below `workspace/`, outside `proofs/`) so the Lake
    # `srcDir := "proofs"` glob never picks it up.
    sv_root = workspace / ".sv_scratch"
    sv_root.mkdir(parents=True, exist_ok=True)
    stem = p.stem or "proof"
    # Only TRUSTED direct imports go into the target. `import Mathlib` used to be
    # hardcoded here, and because SafeVerify requires the submission's transitive
    # imports to cover the target's closure, that made every targeted submission
    # unverifiable. An untrusted module must also never establish the meaning of a
    # name in the target's signatures — the target is the part we trust.
    import_prelude = safeverify.trusted_target_import_prelude(code)

    with tempfile.TemporaryDirectory(dir=sv_root, prefix=f"{stem}_") as td:
        scratch = Path(td)
        target = scratch / f"{stem}_sv_target.lean"
        submission = scratch / f"{stem}_sv_submission.lean"
        # The two halves of the target, composed (merge of two independent fixes):
        #   * a TRUSTED import prelude (above), not the submission's own imports and
        #     not the Mathlib barrel;
        #   * the WHOLE file with every theorem/lemma body `sorry`-ed and its `def`s
        #     kept — a bare signature target lost the file's own definitions and
        #     failed to compile for any project file, and audited only the LAST
        #     theorem where SafeVerify happily checks them all in one pass.
        # The namespace and fully-qualified names travel with the file, so no separate
        # namespace reconstruction is needed.
        target.write_text(
            import_prelude + "\n" + safeverify.sorry_target(code, imports=False)
        )
        submission.write_text(code if code.endswith("\n") else code + "\n")
        try:
            # Thread the per-call dir down to the olean/report scratch too — the
            # parameter existed but interface.verify never passed it, so the lower
            # layer (safeverify.py) fell back to the same shared, stem-keyed dir.
            ok, detail = safeverify.verify_proof(target, submission, workspace, scratch_dir=scratch)
            return VerifyResult("ok" if ok else "rejected", None if ok else detail)
        except Exception as exc:  # noqa: BLE001 — report any failure as an error verdict, not a crash
            return VerifyResult("error", f"{type(exc).__name__}: {exc}")


def verify(path: str, *, declaration: str | None = None) -> VerifyResult:
    from .check_evidence import lake_root, prepared_environment, environment_hash, freshness, PROTOCOL_VERSION
    from .check_runtime import CheckBudget, CheckFailure
    if not safeverify.is_available():
        return VerifyResult('unavailable', 'SafeVerify is not built on this server.')
    p = Path(path).resolve()
    budget = CheckBudget()
    try:
        content = p.read_bytes().decode('utf-8')
        from .check_evidence import digest
        from .check_evidence import source_identity
        budget.execution.update(content_sha256=digest(content.encode()), source_path=str(p), source_identity=source_identity(p))
        with prepared_environment(p, content, lake_root(p), budget) as environment:
            binary = safeverify.safe_verify_binary()
            if binary:
                from .check_evidence import file_hash, file_version
                environment = {**environment, 'files': {**environment['files'], 'verifier:safeverify': {
                    'path': str(binary), 'sha256': file_hash(binary), 'version': file_version(binary), 'kind': 'verifier'}}}
            budget.execution.update(evidence_version=PROTOCOL_VERSION, environment=environment,
                                    environment_sha256=environment_hash(environment))
            result = _verify_impl(str(p), declaration) if declaration else _verify_impl(str(p))
            if declaration:
                budget.execution['selected_declaration'] = declaration
                selected = result.execution or {}
                if selected.get('selected_module'):
                    environment = {**environment, 'files': {**environment['files'],
                        'audit:selected-module': selected['selected_module']}}
                    budget.execution.update(environment=environment, environment_sha256=environment_hash(environment),
                        selected_result=selected.get('selected_result'))
            if not freshness(budget.execution, p)[0]:
                return VerifyResult('error', 'Proof inputs changed during kernel verification.', budget.finish(), content)
            return VerifyResult(result.status, result.detail, budget.finish(), content)
    except (OSError, UnicodeError, CheckFailure) as exc:
        return VerifyResult('error', str(exc))
