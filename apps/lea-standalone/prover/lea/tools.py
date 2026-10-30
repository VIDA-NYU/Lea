"""Lea's built-in tools — the minimum surface area for Lean formalization."""

import os
import re
import subprocess
from . import execution as command_executor
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path

from .imports import IMPORT_COMMAND_RE, direct_imports, without_comments
from .runctx import current_config, current_depth, current_working_dir

# Item 6 / D74 — bound the two fallbacks that each load their OWN full Mathlib
# in a fresh subprocess. The warm daemon (lsp_daemon.py) is a single process and
# needs no bound; these guard the paths that don't share it. Both bounds only
# bite once more than one run checks concurrently — at LEA_MAX_CONCURRENT_RUNS=1
# the run lock already caps concurrency at one — but they must exist before that
# cap is raised (Phase 3), or one daemon hiccup under N runs becomes N cold
# Mathlib processes.

# Cold `lake env lean <file>` fallback: caps concurrent full-Mathlib compiles
# (each ~GBs resident). Not a correctness bound — a memory one.
from .lean_checks import _COLD_CHECK_CONCURRENCY, _cold_check_sem

# `lake build` writes the shared workspace's `.lake` artifacts; two concurrent
# builds against one lake_root race on them (a documented non-goal). One lock
# per lake_root serializes builds. Daemon `lean_check` is a *different process*
# and deliberately does NOT take this lock, so checks stay parallel with builds.
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _build_lock_for(lake_root: str) -> threading.Lock:
    with _build_locks_guard:
        lk = _build_locks.get(lake_root)
        if lk is None:
            lk = threading.Lock()
            _build_locks[lake_root] = lk
        return lk


TOOLS_SCHEMA = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Optionally restrict to a line range (1-indexed, inclusive) to avoid pulling large files into context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."},
                "start_line": {"type": "integer", "description": "Optional 1-indexed first line to include."},
                "end_line": {"type": "integer", "description": "Optional 1-indexed last line to include (inclusive)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact substring in a file with new text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit."},
                "old_string": {"type": "string", "description": "Exact text to find."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "lean_check",
        "description": "Compile a .lean file and return diagnostics (errors, warnings, goals). Uses `lake env lean` if inside a Lake project, otherwise `lean` directly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .lean file to check."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "bash",
        "description": "Run a shell command and return stdout + stderr. Use for `lake build`, git, file I/O outside the dedicated tools, etc. Do NOT use for Lean compilation (use `lean_check`) or Mathlib search (use `search_mathlib`).",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120).",
                    "default": 120,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "search_mathlib",
        "description": "Find Mathlib declarations and their import modules before guessing an import. Identifier queries match whole identifiers (Even excludes Eventually); 'def Even' also finds generated declarations. Exact module basenames and declaration sites rank first. Other queries support grep extended patterns. Pass path to use the active Lake project's pinned Mathlib version. Search hints still require Lean checking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term — a lemma name fragment, type signature pattern, or keyword.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": 10,
                },
                "path": {
                    "type": "string",
                    "description": "Optional path to a .lean file or directory inside a Lake project. If provided, search Mathlib in that project's Lake packages instead of the default workspace Mathlib.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "suggest_imports",
        "description": (
            "Optional import advice for a compiling .lean file, with a 10-second "
            "analysis budget. Never required for completion. Uses targeted imports "
            "in a disposable copy; if skipped, keep current imports and finish "
            "using the passing lean_check. Never modifies the proof file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the compiling .lean proof file to analyze.",
                }
            },
            "required": ["path"],
        },
    },
]


def _find_lake_root(path: str) -> str | None:
    """Walk up from path looking for lakefile.lean or lakefile.toml."""
    p = Path(path).resolve()
    start = p if p.is_dir() else p.parent
    for parent in [start, *start.parents]:
        if (parent / "lakefile.lean").exists() or (parent / "lakefile.toml").exists():
            return str(parent)
    return None


def _readable_roots() -> list[Path] | None:
    """The directories a run may read from, or None outside any run context.

    Two roots, and both are needed:

      * the run's ``working_dir`` — its own proofs, the project's ``.lea/`` docs and
        uploads, and (for a project) its sibling sessions' files;
      * the enclosing **Lake root**, which is what makes Mathlib readable. Confining
        reads to the workspace alone would break the normal loop, since
        ``search_mathlib`` returns paths under ``.lake/packages/mathlib/`` and the
        model reads them next.

    Everything else is out of bounds. `None` (no run context — a standalone CLI call
    or a test) means unrestricted, matching `_sandboxed_write_path`.
    """
    wd = current_working_dir()
    if wd is None:
        return None
    root = Path(wd).expanduser().resolve()
    roots = [root]
    lake_root = _find_lake_root(str(root / "_"))
    if lake_root:
        roots.append(Path(lake_root).resolve())
    # H7: the materialized skills directory. A multi-file skill is advertised, not
    # injected — the agent opens `references/*.md` on demand — and those reads land
    # outside both roots above, so without this every one of them is refused.
    #
    # Narrow by construction: a directory Lea itself created, holding only skill files,
    # added to the READ roots only. `_sandboxed_write_path` is untouched, so nothing here
    # widens what a run may write. Reading it also works from a SUB-AGENT, whose
    # working_dir is its own scratch tree — which the alternative (copying skills into the
    # session repo) would not have.
    config = current_config()
    skills_root = getattr(config, "skills_root", None)
    if skills_root:
        path = Path(skills_root).expanduser()
        if path.is_dir():
            roots.append(path.resolve())
    return roots


def _within(target: Path, roots: list[Path]) -> bool:
    return any(target == root or root in target.parents for root in roots)


def _run_relative_path(path: str) -> Path:
    """Resolve a model path against this activation's working directory.

    The adapter process has a stable process cwd while concurrent activations each
    declare their own ``working_dir`` through ``run_context``. Project context paths
    such as ``.lea/files/overleaf/main.tex`` must therefore be anchored explicitly.

    This also has to match `_sandboxed_write_path`, or the two halves disagree about
    what a relative path means. They did: a sub-agent that wrote `candidate.lean`
    (landing in its scratch dir) and then checked `candidate.lean` was told the file
    did not exist, pointing at the adapter's own directory. Observed live — the child
    recovered with an absolute path, but its result envelope came back empty and a
    correct, compiling proof was silently discarded.

    Gated on the working dir being SET, not on depth: at depth 0 the process cwd is
    the adapter's own directory, where no proof ever lives, so relative paths were
    broken there too — just less visibly, because the main agent is handed absolute
    ones. No activation (CLI, eval, `interface.check`, tests) means unchanged
    behaviour, and that is the real safety boundary.
    """
    p = Path(path).expanduser()
    policy = command_executor.current() or {}
    if p.is_absolute() and policy.get("canonical_repo"):
        original = Path(policy["canonical_repo"]).resolve()
        if p.resolve().is_relative_to(original):
            p = Path(policy["attempt_repo"]) / p.resolve().relative_to(original)
    wd = current_working_dir()
    if wd is None or p.is_absolute():
        return p.resolve()
    return (Path(wd).expanduser().resolve() / p).resolve()


def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    p = _run_relative_path(path)
    # Reads are confined to the run's roots (AUDIT-2026-07-24 S4). `write_file` and
    # `edit_file` have been sandboxed since F3, but reads were not — so the model could
    # open anything the adapter process could: `~/.ssh/id_rsa`, the monorepo `.env`, and
    # `config/lea.local.toml`, which holds every provider key and the GitHub token in
    # plaintext. That matters most on the autonomous Overleaf path, where the task text
    # comes from a shared LaTeX document and no approval gate stands between a
    # prompt-injected instruction and the tool call.
    roots = _readable_roots()
    if roots is not None and not _within(p, roots):
        return (
            f"Error: {path!r} is outside this run's workspace. Read only within your "
            "session's directory or the Lake project (Mathlib included)."
        )
    if not p.exists():
        return f"Error: {p} does not exist."
    text = p.read_text()
    if start_line is None and end_line is None:
        return text
    lines = text.splitlines(keepends=True)
    s = max(0, (start_line or 1) - 1)
    e = end_line if end_line is not None else len(lines)
    sliced = "".join(lines[s:e])
    header = f"# lines {s + 1}-{min(e, len(lines))} of {len(lines)} in {p}\n"
    return header + sliced


class _SandboxViolation(ValueError):
    """A write path that escapes the run's working directory."""


def _sandboxed_write_path(path: str) -> Path:
    """Resolve a model-supplied write path and confine it to the run's working dir.

    Two reasons this matters, both sharpened by the v2.3 concurrency/hosting work:
      * **relative paths** must resolve against *this activation's* working_dir, not
        the one process-global cwd two concurrent runs share (that cwd is nobody's
        per-run identity);
      * **any** resolved path — relative or absolute — must stay inside working_dir,
        so a model path typo (seen in the wild: ``apps/lean-standalone`` for
        ``apps/lea-standalone``), a hallucination, or a prompt-injected path can't
        create/clobber files outside the session's workspace.

    When no run context is set (standalone CLI / tests), the path is returned
    unchanged so today's behavior is preserved — the confinement only engages once
    an activation has declared its working_dir (agent.run_events → run_context)."""
    wd = current_working_dir()
    p = _run_relative_path(path)
    if wd is None:
        return p
    root = Path(wd).expanduser().resolve()
    target = (p if p.is_absolute() else root / p).resolve()
    if target != root and root not in target.parents:
        # A SUBAGENT (depth > 0) works in an isolated scratch dir, and its candidate is
        # collated by the coordinator re-reading it — so a path the coordinator named in
        # the delegated task that points outside the scratch tree is *redirected into it*
        # (by basename), not rejected. The child physically cannot escape its scratch dir
        # this way (the rebase stays under `root`), and it can now fulfil "write the file
        # at <canonical path>" without knowing it's sandboxed. The MAIN agent (depth 0)
        # keeps the hard F3 rejection: its working_dir IS the real session workspace, so an
        # out-of-bounds path there is a typo/hallucination/injection to refuse, not redirect.
        if current_depth() > 0:
            return root / Path(path).name
        raise _SandboxViolation(
            f"path {path!r} resolves outside this run's workspace ({root}). "
            "Write only within your session's proofs directory."
        )
    config = current_config()
    relative = target.relative_to(root).as_posix()
    if (config and config.project_library and relative.split("/", 1)[0] in {"Shared", "External"}
            and relative not in config.project_context.get("reserved_paths", [])):
        raise _SandboxViolation("Reserve this shared module with request_project_dependency before writing it; another attempt may own it.")
    return target


_BROAD_IMPORT_OVERRIDE = "LEA_ALLOW_BROAD_MATHLIB_IMPORT"


def _broad_import_error(path: Path, content: str) -> str | None:
    """Policy diagnostic for model-authored Lean files that import the Mathlib barrel."""
    if path.suffix != ".lean" or "Mathlib" not in direct_imports(content):
        return None
    if os.environ.get(_BROAD_IMPORT_OVERRIDE, "").strip().lower() in {"1", "true", "yes"}:
        return None
    return (
        "Error: generated Lean files may not use the umbrella `import Mathlib`. "
        "Import targeted `Mathlib.<domain>.<module>` modules instead. If this is an "
        "existing compiling proof, call `suggest_imports` on it for an exact "
        "replacement block. Operators may temporarily bypass this generated-file "
        f"policy with {_BROAD_IMPORT_OVERRIDE}=1."
    )


def _protected_path_error(path: Path) -> str | None:
    """Refusal for a write to a file only the system renders (`LeaConfig.protected_paths`).

    The blueprint is written through blueprint_upsert / blueprint_remove so the host can
    validate it; a direct write would bypass that. Paths are relative to the run's
    working dir; with no run context or no protected paths, nothing is refused."""
    config = current_config()
    wd = current_working_dir()
    if not config or not wd or not getattr(config, "protected_paths", None):
        return None
    target = Path(path).resolve()
    root = Path(wd).expanduser().resolve()
    for rel in config.protected_paths:
        protected = (root / rel).resolve()
        # samefile also catches a case-variant spelling on a case-insensitive volume.
        if target == protected or (target.exists() and protected.exists() and target.samefile(protected)):
            return f"Error: {rel} is maintained by the system. Use blueprint_upsert / blueprint_remove instead."
    return None


def _mutating(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        from .check_evidence import mutation
        from .check_runtime import CheckBudget
        from .runctx import current_should_stop
        raw = args[0] if args else kwargs.get('path')
        if fn.__name__ in {'write_file', 'edit_file'}:
            try:
                path = _sandboxed_write_path(raw)
            except _SandboxViolation:
                return fn(*args, **kwargs)
        else:
            path = (current_working_dir() or os.getcwd()) if fn.__name__ == 'bash' else _run_relative_path(raw)
        with mutation(path, budget=CheckBudget(should_stop=current_should_stop())):
            return fn(*args, **kwargs)
    return guarded


@_mutating
def write_file(path: str, content: str) -> str:
    try:
        p = _sandboxed_write_path(path)
    except _SandboxViolation as exc:
        return f"Error: {exc}"
    if protected := _protected_path_error(p):
        return protected
    if policy_error := _broad_import_error(p, content):
        return policy_error
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    from .check_runtime import CheckText
    return CheckText(f"Wrote {len(content)} bytes to {p}", captured_content=content, snapshot_path=str(p.resolve()))


@_mutating
def edit_file(path: str, old_string: str, new_string: str) -> str:
    try:
        p = _sandboxed_write_path(path)
    except _SandboxViolation as exc:
        return f"Error: {exc}"
    if protected := _protected_path_error(p):
        return protected
    if not p.exists():
        return f"Error: {p} does not exist."
    text = p.read_text()
    count = text.count(old_string)
    if count == 0:
        return "Error: old_string not found in file."
    if count > 1:
        return f"Error: old_string appears {count} times. Provide more context to make it unique."
    updated = text.replace(old_string, new_string, 1)
    if policy_error := _broad_import_error(p, updated):
        return policy_error
    p.write_text(updated)
    from .check_runtime import CheckText
    return CheckText("OK", captured_content=updated, snapshot_path=str(p.resolve()))


def lean_check(path: str, *, use_lsp: bool = True, allow_cold: bool = True, budget=None) -> str:
    p = _run_relative_path(path)
    roots = _readable_roots()
    if roots is not None and not _within(p, roots):
        return (
            f"Error: {path!r} is outside this run's workspace. Check only within your "
            "session's directory or the Lake project."
        )
    if not p.exists():
        return f"Error: {p} does not exist."
    from .lean_checks import check_file
    return check_file(p, _find_lake_root(str(p)), use_lsp=use_lsp,
                      allow_cold=allow_cold, budget=budget).text()


def lean_check_cold(path: str) -> str:
    """`lean_check` with the LSP fast path forced off -- always a real, cold
    `lake env lean` / `lean` subprocess compile, in principle reading whatever
    `.olean`s are on disk right now with no in-process import cache to be
    stale.

    CAUTION: do not reach for this to work around the daemon's staleness
    (see `lean_check`'s `use_lsp` docstring) -- a live end-to-end test found
    it does NOT reliably do so for a project-local dependency. Prefer
    `lsp_daemon.mark_stale` (wired into `rebuild_module`, below) plus the
    normal warm `lean_check`, which that same test confirmed does work.
    """
    return lean_check(path, use_lsp=False)


def _generated_lean_check(path: str) -> str:
    """Agent-facing check with the generated-artifact import policy enforced.

    Adapter/manual checks call `lean_check` directly and remain able to inspect
    legacy user-authored files. Model tool calls use this wrapper, so writing a
    barrel import through `bash` cannot bypass the write/edit gate and receive a
    successful final verdict.
    """
    started = time.monotonic()
    p = _run_relative_path(path)
    if p.exists():
        try:
            if policy_error := _broad_import_error(p, p.read_text()):
                return policy_error
        except OSError:
            pass
    result = lean_check(path)
    config = current_config()
    selection = config.selected_result if config else {}
    if selection and (not selection.get("path") or p == _run_relative_path(selection["path"])) and not _lean_check_has_error(result):
        from .result_selection import inspect_selected
        from .check_runtime import CheckText
        inspection_started = time.monotonic()
        try:
            evidence = inspect_selected(p, selection["declaration"], selection.get("kind"))
            if evidence["content_sha256"] != (getattr(result, "execution", None) or {}).get("content_sha256"):
                raise ValueError("Source changed after the selected result check")
            result.selected_result = evidence
            result.execution['selected_result'] = evidence
        except (ValueError, OSError) as exc:
            result = CheckText('Error: selected result did not pass compiler inspection: ' + str(exc),
                getattr(result, 'execution', None), getattr(result, 'captured_content', None),
                getattr(result, 'snapshot_path', None))
            result.selected_result = None
            result.selected_result_error = str(exc)
            if result.execution is not None:
                result.execution['selected_result'] = None
        finally:
            if getattr(result, 'execution', None) is not None:
                timing = result.execution.setdefault('timings_ms', {})
                timing['result_inspection'] = round((time.monotonic() - inspection_started) * 1000, 2)
                timing['total'] = round((time.monotonic() - started) * 1000, 2)
    return result


@_mutating
def rebuild_module(path: str) -> str:
    """Force a real `lake build` of the Lean module at `path`, so its compiled
    `.olean` on disk reflects the file's *current* source.

    `lean_check`'s LSP fast path (above) never does this -- that's exactly what
    makes a same-file recheck ~420x faster than a cold compile, but it also means
    any *other* file that `import`s this module keeps resolving against whatever
    `.olean` was last built, no matter how many times the importing file itself
    is rechecked, until something does a real build. This is that something:
    called once per edited module, before cascade-verifying its dependents
    (docs/FEATURE-overleaf-lean-pane-manual-edit.md, "Cascade verification").

    Unlike `lean_check`, this always shells out -- there is no fast path for
    "make the compiled artifact on disk match the source," only a real one.
    """
    p = _run_relative_path(path)
    if not p.exists():
        return f"Error: {p} does not exist."

    lake_root = _find_lake_root(str(p))
    if not lake_root:
        return f"Error: no lakefile.lean/lakefile.toml found above {p}."

    # Mirrors the adapter's own `config.lea_root / "workspace" / "proofs"`
    # convention (routes/sessions.py `_resolve_proof_path`) and the companion's
    # `moduleNameFromProjectStep` (leanDependencyGraph.mjs): the Lean module name
    # is the file's path relative to the library's source root, dot-joined, no
    # extension. This project's `lean_lib Lea where srcDir := "proofs"`
    # (workspace/lakefile.lean) is exactly that source root.
    src_root = Path(lake_root) / "proofs"
    try:
        module_rel = p.relative_to(src_root.resolve())
    except ValueError:
        return f"Error: {p} is not under the Lean source root {src_root}."
    module_name = ".".join(module_rel.with_suffix("").parts)

    timeout = int(os.environ.get("LEAN_CHECK_TIMEOUT", "900"))
    # Serialize builds per lake_root: concurrent `lake build` against one shared
    # workspace races on its `.lake` artifacts. Reconcile the daemon before the
    # writer lease releases, including partial failures and timeouts.
    with _build_lock_for(lake_root):
        try:
            result = command_executor.run(
                ["lake", "build", module_name],
                # Explicit UTF-8: Lean's own output is full of non-ASCII (✖, ⊢,
                # Mathlib's unicode notation). `text=True` alone decodes with the
                # ambient locale's default encoding, which is not guaranteed to be
                # UTF-8 for a background-launched process -- a decoding failure
                # there would raise uncaught (not TimeoutExpired/FileNotFoundError)
                # and surface as an opaque 500 instead of a real verdict.
                # errors="replace" so a genuinely malformed byte can't crash the
                # rebuild outright either.
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, cwd=lake_root,
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            if result.returncode == 0:
                return output if output else "OK — rebuilt."
            # Lake's own build-failure report (e.g. "✗ [n/total] Building <module>
            # (Ns)" plus a `trace:` block showing the invocation it ran) is NOT the
            # same format `lean`'s direct CLI/LSP diagnostics use -- the
            # `file.lean:L:C: error: ...` line `_lean_check_has_error`/
            # `_first_error_line` (lea/interface.py `rebuild`) scan for may be
            # buried deep in Lake's own log, or, for some failure modes, absent
            # from the captured output entirely even though the build genuinely
            # failed. A non-zero exit code from `lake build` is authoritative on
            # its own -- don't make error detection depend on Lake's own log
            # happening to contain the literal word "error". Prefix unambiguously
            # so the downstream text-based classifiers can never miss a real
            # build failure regardless of how Lake chose to phrase it.
            label = output or f"lake build exited with code {result.returncode} (no output)."
            return f"error: lake build failed for {module_name} (exit {result.returncode}):\n{label}"
        except subprocess.TimeoutExpired:
            return f"Error: lake build timed out after {timeout}s."
        except FileNotFoundError:
            return "Error: `lake` not found. Is Lean 4 installed?"
        finally:
            from .check_evidence import reconcile_daemon
            reconcile_daemon(lake_root)


# --- output classifiers -----------------------------------------------------
# Pure helpers that read lean_check / tool output and classify it. Shared single
# source of truth: the agent's live events (agent._meaning_events,
# ProofVerificationState) and the standalone check() capability all use these, so
# a verdict can never drift between them.

def _lean_check_has_error(output: str) -> bool:
    return bool(re.search(r"(^|\n).*error[:\s]", output, re.IGNORECASE))


def _lean_check_has_sorry(output: str) -> bool:
    """True if lean_check's output shows the proof still relies on `sorry`/`admit`.

    Lean reports these as a *warning* (`declaration uses 'sorry'`), not an error,
    so `_lean_check_has_error` misses them — but a final proof with a `sorry` is
    NOT done. `admit` and a bare `sorry` both surface as "uses 'sorry'";
    `sorryAx` is the kernel axiom they elaborate to."""
    lowered = output.lower()
    return "uses 'sorry'" in lowered or "sorryax" in lowered


def _tool_result_ok(output: str) -> bool:
    return bool(output.strip()) and not output.strip().lower().startswith("error:")


def _first_error_line(output: str) -> str | None:
    """The first line that looks like a Lean error — a short label for the step
    badge. The full diagnostics still reach the model via ToolResulted."""
    for line in output.splitlines():
        if re.search(r"error[:\s]", line, re.IGNORECASE):
            return line.strip()
    return None


# Environment variables never handed to the agent's shell. `load_config` exports every
# configured provider key into this process so LiteLLM can read them, which also put
# them in the environment of every command the model ran — so "read the key" needed no
# filesystem access at all (AUDIT-2026-07-24 S4). The agent has no use for them: Lean,
# Lake, and git need none, and the adapter injects the GitHub token into its own push
# URL rather than via the environment.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")
_SECRET_ENV_NAMES = frozenset({"OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_SECRET_ACCESS_KEY"})


def _is_secret_env(name: str) -> bool:
    upper = name.upper()
    return upper in _SECRET_ENV_NAMES or upper.endswith(_SECRET_ENV_SUFFIXES)


def scrubbed_env() -> dict[str, str]:
    """The process environment minus anything that looks like a credential."""
    return {k: v for k, v in os.environ.items() if not _is_secret_env(k)}


@_mutating
def bash(command: str, timeout: int = 120) -> str:
    # Run in the active run's working dir (item 8) instead of the process-global
    # cwd, so under concurrent runs one run's shell command can't land in
    # another's tree. `None` (no run context — a standalone/test call) tells
    # subprocess to use the inherited cwd, i.e. today's behavior unchanged.
    cwd = current_working_dir()
    try:
        result = command_executor.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            env=scrubbed_env(),
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return f"(no output, exit code {result.returncode})"
        if len(output) > 10000:
            output = output[:10000] + "\n... (truncated)"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."


WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"


def _mathlib_for_lake_root(lake_root: Path) -> Path | None:
    for sub in (".lake/packages/mathlib/Mathlib", "lake-packages/mathlib/Mathlib"):
        candidate = lake_root / sub
        if candidate.exists():
            return candidate
    return None


def search_mathlib(query: str, max_results: int = 10, path: str | None = None) -> str:
    query = query.strip()
    if not query or len(query) > 256:
        return 'Error: provide a nonempty Mathlib query of at most 256 characters.'
    max_results = max(1, min(50, int(max_results)))
    symbol = re.sub(r'^(?:def|theorem|lemma|abbrev|class|structure)\s+', '', query)
    identifier = re.fullmatch(r"[^\W\d][\w'.]*", symbol)
    pattern = (r"(^|[^[:alnum:]_'])" + re.escape(symbol) + r"([^[:alnum:]_']|$)"
               if identifier else query)
    search_dir = None
    project_label = "default workspace"

    if path:
        resolved = _run_relative_path(path)
        roots = _readable_roots()
        if roots is not None and not _within(resolved, roots):
            return 'Error: Mathlib search path is outside the allowed read roots.'
        lake_root_str = _find_lake_root(str(resolved))
        if lake_root_str:
            mathlib = _mathlib_for_lake_root(Path(lake_root_str))
            if mathlib:
                search_dir = str(mathlib)
                project_label = lake_root_str
            else:
                return f"Error: Mathlib not found under Lake project at {lake_root_str}. Ensure Mathlib is a Lake dependency."
        else:
            return f"Error: no Lake project (lakefile.lean/lakefile.toml) found above {path}."

    if not search_dir:
        for candidate in (
            WORKSPACE / ".lake" / "packages" / "mathlib" / "Mathlib",
            WORKSPACE / "lake-packages" / "mathlib" / "Mathlib",
        ):
            if candidate.exists():
                search_dir = str(candidate)
                break

    if not search_dir:
        return "Error: Mathlib source not found. Ensure Mathlib is a Lake dependency."

    if identifier and symbol.startswith('Mathlib.'):
        parts = symbol.split('.')[1:]
        candidate = Path(search_dir).joinpath(*parts).with_suffix('.lean')
        if all(parts) and candidate.is_file():
            return f'Module found in {project_label}:\n  {candidate}\n  import {symbol}'
        return f'No Mathlib module named {symbol} in {project_label}; search its declaration before choosing an import.'

    try:
        result = command_executor.run(
            ["grep", "-r", "--include=*.lean", "-l", "-E", "-e", pattern, "--", search_dir],
            capture_output=True,
            text=True,
            timeout=30,
        )
        files = result.stdout.strip().split("\n")
        if result.returncode not in (0, 1):
            return 'Error: invalid Mathlib search pattern or unreadable source: ' + result.stderr[:300]
        files = [f for f in files if f]
        leaf = symbol.rsplit('.', 1)[-1].casefold() if identifier else ''
        files.sort(key=lambda f: (Path(f).stem.casefold() != leaf, len(Path(f).parts), f))
        files = files[:max_results]
        if not files:
            return f"No Mathlib results for '{query}' in {project_label}."

        # Bound each subprocess response, then rank declarations/derived names
        # ahead of prose. Preserve the existing confined subprocess boundary.
        lines = []
        imports = []
        for f in files[:max_results]:
            grep_result = command_executor.run(
                ["grep", "-n", "-m", "80", "-E", "-e", pattern, "--", f],
                capture_output=True,
                text=True,
                timeout=10,
            )
            matches = grep_result.stdout.strip().splitlines()
            def rank(line):
                text = line.partition(':')[2].strip()
                found = re.match(r'(?:protected\s+)?(?:def|theorem|lemma|abbrev|class|structure)\s+([\w\'.]+)', text)
                declaration = bool(found and identifier and found[1].rsplit('.',1)[-1] == symbol.rsplit('.',1)[-1])
                priority = 0 if declaration else 1 if 'to_additive' in text else 2
                return (priority, int(line.partition(':')[0]))
            matches.sort(key=rank)
            for line in matches[:2]:
                short_path = f.split("Mathlib/")[-1] if "Mathlib/" in f else f
                lines.append(f"  Mathlib/{short_path}:{line}")
                module = 'Mathlib.' + short_path.removesuffix('.lean').replace('/', '.')
                if module not in imports:
                    imports.append(module)
                if len(lines) >= max_results:
                    break
            if len(lines) >= max_results:
                break

        return (f"Found {len(lines)} matches in {project_label}:\n" + "\n".join(lines)
                + '\nImport candidates (ranked):\n' + '\n'.join('  import ' + name for name in imports))
    except subprocess.TimeoutExpired:
        return "Error: search timed out."


_IMPORT_ANALYSIS_MODULE = 'Mathlib.Tactic.Linter.MinImports'
IMPORT_ANALYSIS_TIMEOUT_SECONDS = 10


def _import_analysis_source(code: str) -> str:
    """Enable the linter without importing the entire Mathlib library."""
    lines = code.splitlines(keepends=True)
    clean_lines = without_comments(code).splitlines(keepends=True)
    import_lines = [
        i for i, line in enumerate(clean_lines)
        if IMPORT_COMMAND_RE.fullmatch(line.rstrip("\r\n"))
    ]

    if _IMPORT_ANALYSIS_MODULE not in direct_imports(code):
        insert_at = import_lines[0] if import_lines else 0
        if not import_lines:
            for i, line in enumerate(lines):
                if re.fullmatch(r"[ \t]*module(?:[ \t].*)?[ \t]*(?://.*)?\r?\n?", line):
                    insert_at = i + 1
                    break
        lines.insert(insert_at, f"import {_IMPORT_ANALYSIS_MODULE}\n")
        import_lines = [i + (1 if i >= insert_at else 0) for i in import_lines]
        import_lines.append(insert_at)

    bump_at = max(import_lines) + 1
    lines.insert(bump_at, "#import_bumps\n")
    return "".join(lines)


def _import_analysis_has_error(output: str) -> bool:
    return bool(re.search(r"(?mi)^.*\berror:", output)) or output.startswith("Error:")


def suggest_imports(path: str) -> str:
    """Suggest a targeted direct-import block without modifying the source file.

    Mathlib's incremental min-imports linter needs to read its own file at EOF, so
    the analysis runs on a short-lived sibling file rather than a purely virtual
    LSP document. Advice has an independent short budget and is never proof
    evidence or a completion requirement. The temporary document is closed.
    """
    p = _run_relative_path(path)
    roots = _readable_roots()
    if roots is not None and not _within(p, roots):
        return (
            f"Error: {path!r} is outside this run's workspace. Analyze imports only "
            "within your session's directory or the Lake project."
        )
    if not p.exists():
        return f"Error: {p} does not exist."
    if p.suffix != ".lean":
        return f"Error: {p} is not a .lean file."
    if not _find_lake_root(str(p)):
        return f"Error: no Lake project (lakefile.lean/lakefile.toml) found above {p}."

    code = p.read_text()
    if 'Mathlib' in direct_imports(code):
        return ('Import analysis skipped: replace the umbrella Mathlib import with targeted modules '
                'found using search_mathlib. Advice cannot certify a broad-import proof.')
    analysis = _import_analysis_source(code)
    output = ""
    from .check_runtime import CheckBudget
    from .runctx import current_should_stop
    budget = CheckBudget(timeout=IMPORT_ANALYSIS_TIMEOUT_SECONDS, should_stop=current_should_stop())
    with tempfile.TemporaryDirectory(dir=p.parent, prefix=".lea-imports-") as td:
        scratch_dir = Path(td)
        scratch = scratch_dir / p.name
        scratch.write_text(analysis)
        try:
            # Advice owns a separate compiler process. A timeout must not retire
            # the warm proof server or cancel another run's LSP document.
            output = lean_check(str(scratch), budget=budget, use_lsp=False, allow_cold=False)
        finally:
            try:
                from .lsp_daemon import close_documents_under

                close_documents_under(str(scratch_dir))
            except Exception:
                pass

    if _import_analysis_has_error(output):
        return (
            "Import analysis skipped: the optional analysis did not finish within its budget or could not elaborate. "
            "Keep the current targeted imports. A passing lean_check remains valid for its checked inputs; "
            "proceed to completion without retrying import advice.\n" + str(output)[:1500]
        )
    if p.read_text() != code:
        return 'Import analysis skipped: the proof changed. Check the current revision before completion.'

    unneeded = set(re.findall(r"unneeded import '([A-Za-z_][A-Za-z0-9_'.]*)'", output))
    reported = re.findall(
        r"(?m)^(?:public[ \t]+)?import[ \t]+([A-Za-z_][A-Za-z0-9_'.]*)[ \t]*$",
        output,
    )
    current = direct_imports(code)
    suggested = [
        module for module in current
        if module != "Mathlib" and module not in unneeded
    ]
    suggested.extend(module for module in reported if module not in suggested
        and (module != _IMPORT_ANALYSIS_MODULE or module in current))

    if not suggested and not unneeded:
        return (
            "Import analysis skipped: no reliable replacement was reported. "
            "Keep the current targeted imports and complete using the passing lean_check."
        )

    if suggested:
        block = "\n".join(f"import {module}" for module in suggested)
    else:
        block = "(no explicit imports required; Lean imports Init automatically)"
    return (
        f"Suggested replacement import block for {p}:\n{block}\n\n"
        "Replace only the file's import commands, then run `lean_check` again. "
        "The min-imports analysis is advisory and may miss unusual attribute dependencies."
    )


# Dispatch table
TOOL_HANDLERS = {
    "bash": lambda args: bash(args["command"], args.get("timeout", 120)),
    "read_file": lambda args: read_file(args["path"], args.get("start_line"), args.get("end_line")),
    "write_file": lambda args: write_file(args["path"], args["content"]),
    "edit_file": lambda args: edit_file(args["path"], args["old_string"], args["new_string"]),
    "lean_check": lambda args: _generated_lean_check(args["path"]),
    "search_mathlib": lambda args: search_mathlib(args["query"], args.get("max_results", 10), args.get("path")),
    "suggest_imports": lambda args: suggest_imports(args["path"]),
}


# Register the built-ins with the shared registry, in TOOLS_SCHEMA order. The
# loop selects from the registry (build_toolset); these globals remain the
# human-readable source of truth for the built-in tools and stay importable.
from .registry import Tool, register, tool  # noqa: E402

for _schema in TOOLS_SCHEMA:
    register(Tool(name=_schema["name"], schema=_schema, handler=TOOL_HANDLERS[_schema["name"]]))


def _select_result(args):
    from .check_runtime import CheckText
    from .result_selection import inspect_selected
    config = current_config()
    if not config or not (config.selected_result or config.target_declaration):
        return "Error: explicit result selection is unavailable for this run"
    try:
        path = _sandboxed_write_path(args["path"])
        outcome = args["outcome"]
        target = config.selected_result.get("target_declaration", config.selected_result.get("declaration")) or config.target_declaration
        if outcome != "disproved" and args["declaration"].rsplit(".", 1)[-1] != target.rsplit(".", 1)[-1]:
            raise ValueError("A proof must select the requested target; use disproved for a counterexample")
        evidence = inspect_selected(path, args["declaration"], outcome)
        if outcome != 'disproved' and '.' in target and evidence['user_name'] != target:
            raise ValueError('The selected declaration is not the requested qualified target')
        config.selected_result.update(declaration=evidence["user_name"], kind=outcome, path=str(path),
                                      target_declaration=target)
        result = CheckText("Selected result. Run lean_check on this file before finishing.")
        result.selection = dict(config.selected_result)
        return result
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


register(Tool(name="select_result", opt_in=True, handler=_select_result, schema={
    "name": "select_result",
    "description": "Explicitly associate the requested proof/definition or a checked negation/counterexample with this target. Then run lean_check. A different result name is allowed only for disproof.",
    "input_schema": {"type": "object", "properties": {
        "path": {"type": "string"}, "declaration": {"type": "string"},
        "outcome": {"type": "string", "enum": ["proved", "defined", "disproved"]}},
        "required": ["path", "declaration", "outcome"]}}))


# Opt-in SafeVerify tool: a kernel-level anti-cheat audit the interactive coordinator can
# run on a finished proof file. Registered `opt_in=True` (like `spawn_subagent`) so it stays
# off every default/eval run and a sub-agent's `tools=None` can never include it; the adapter
# composes it onto interactive coordinator runs. The handler LAZILY imports `interface.verify`
# so this module (imported by agent.py) doesn't form the tools ← agent ← interface cycle.
_SAFE_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to the .lean proof file to audit."},
    },
    "required": ["path"],
}


@tool(
    name="safe_verify",
    description=(
        "Kernel-level anti-cheat audit of a finished Lean proof FILE via SafeVerify. "
        "Stronger than lean_check: it replays the proof through the Lean kernel and rejects "
        "sorry, extra axioms, native_decide, partial/unsafe, and environment manipulation "
        "that a plain compile lets through. Audits EVERY theorem/lemma in the file in one "
        "pass. Run it to confirm a proof is genuinely complete before you finish. Note: it "
        "checks the PROOF against the file's own statements; it does not certify that the "
        "statements themselves are the intended ones."
    ),
    input_schema=_SAFE_VERIFY_SCHEMA,
    opt_in=True,
)
def safe_verify(args: dict) -> str:
    from .interface import verify as _verify  # lazy: avoid tools <- agent <- interface cycle

    path = args.get("path")
    if not isinstance(path, str) or not path:
        return "Error: safe_verify requires a 'path' to the .lean file."
    resolved = _run_relative_path(path)
    config = current_config()
    selection = config.selected_result if config else {}
    declaration = selection.get('declaration') if selection and str(resolved) == str(_run_relative_path(selection.get('path') or path)) else None
    result = _verify(str(resolved), declaration=declaration) if declaration else _verify(str(resolved))
    detail = (result.detail or "").strip()
    if result.status == "ok":
        return ("SafeVerify: OK — the proof passed the kernel audit (no sorry / extra axiom / "
                "native_decide / partial)." + (" The selected result verified." if declaration else " Every theorem in the file verified."))
    if result.status == "rejected":
        return f"SafeVerify: REJECTED — a cheat or type mismatch was caught.\n{detail}"
    if result.status == "unavailable":
        return ("SafeVerify: UNAVAILABLE — the SafeVerify binary is not built on this server, "
                f"so this proof could not be audited. {detail}").strip()
    return f"SafeVerify: ERROR — could not run the audit (this is not a pass).\n{detail}"

# Register the opt-in `spawn_subagent` tool (item 18). Imported here so it lands in
# the registry alongside the built-ins whenever tools are loaded, but it is
# `opt_in=True`, so build_toolset(None) never includes it — existing runs are
# byte-identical, and a subagent's own default toolset can't contain it.
from . import subagents as _subagents  # noqa: E402,F401

# Host-mediated, opt-in: unavailable to ordinary CLI/LeaChat runs.
from .status_reporting import TOOL_SCHEMA as _STATUS_SCHEMA, unsupported_handler as _status_handler
register(Tool(name="update_lea_status", schema=_STATUS_SCHEMA, handler=_status_handler, opt_in=True))

# Host-mediated, opt-in blueprint tools (blueprint_read/upsert/remove). Imported here so
# they land in the registry with the built-ins; project runs name them explicitly.
from . import blueprint_tools as _blueprint_tools  # noqa: E402,F401
