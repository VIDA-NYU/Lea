"""Lea's built-in tools — the minimum surface area for Lean formalization."""

import os
import re
import subprocess
import tempfile
import threading
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
_COLD_CHECK_CONCURRENCY = max(1, int(os.environ.get("LEA_COLD_CHECK_CONCURRENCY", "2")))
_cold_check_sem = threading.BoundedSemaphore(_COLD_CHECK_CONCURRENCY)

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
        "description": "Search Mathlib for lemmas/theorems matching a query. Greps Mathlib source files for the query string. If you are proving in a specific Lake project (e.g., miniF2F, FormalQualBench), pass `path` so the search uses THAT project's Mathlib version — different projects pin different Mathlib versions, and a hit in the wrong version is worse than no hit.",
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
            "Analyze a compiling .lean file with Mathlib's min-imports linter and "
            "return a targeted replacement import block. Read-only: it checks a "
            "disposable copy and never modifies the proof file."
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
    for parent in [p.parent, *p.parent.parents]:
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
    p = Path(path).expanduser()
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


def write_file(path: str, content: str) -> str:
    try:
        p = _sandboxed_write_path(path)
    except _SandboxViolation as exc:
        return f"Error: {exc}"
    if policy_error := _broad_import_error(p, content):
        return policy_error
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} bytes to {p}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    try:
        p = _sandboxed_write_path(path)
    except _SandboxViolation as exc:
        return f"Error: {exc}"
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
    return "OK"


def lean_check(path: str, *, use_lsp: bool = True) -> str:
    p = _run_relative_path(path)
    roots = _readable_roots()
    if roots is not None and not _within(p, roots):
        return (
            f"Error: {path!r} is outside this run's workspace. Check only within your "
            "session's directory or the Lake project."
        )
    if not p.exists():
        return f"Error: {p} does not exist."

    lake_root = _find_lake_root(str(p))

    # Fast path: persistent LSP daemon (keeps Mathlib oleans warm). ~420×
    # speedup on in-place edits. See lea/lsp_daemon.py and tests/lsp/.
    #
    # `use_lsp=False` skips this deliberately (see `lean_check_cold` below):
    # the daemon caches every module it has ever imported for the life of its
    # process, so a check through it can silently resolve an `import` against
    # a stale in-memory copy even after a real `lake build` (`rebuild_module`,
    # below) refreshed that module's `.olean` on disk from a *different*
    # process.
    #
    # CAUTION: a live end-to-end test (tests/lsp/test_cascade_rename_integration.py)
    # found that `use_lsp=False` -- i.e. the plain `lake env lean <file>`
    # subprocess below -- does NOT reliably see a just-rebuilt project-local
    # module's fresh `.olean` either, unlike restarting the daemon (which does).
    # This is a real Lean/Lake behavior difference still under investigation,
    # not something to rely on for correctness yet. The Overleaf lean pane's
    # cascade re-check of a dependent (routes/sessions.py) does NOT use this --
    # it relies on `rebuild_module`'s `lsp_daemon.mark_stale` call instead,
    # confirmed correct by the same test. See
    # docs/FEATURE-overleaf-lean-pane-manual-edit.md ("Cascade verification").
    if use_lsp and lake_root and not os.environ.get("LEA_DISABLE_LSP"):
        try:
            from lea.lsp_daemon import check_via_lsp
            return check_via_lsp(str(p), p.read_text(), lake_root)
        except Exception as exc:  # noqa: BLE001 — fall through to subprocess
            # C4: the fallback is CORRECT but ~440x slower (~0.2s -> ~88s per check,
            # a cold Mathlib elaboration). Silently, this looked like the agent
            # thinking for a minute and a half, repeatedly, with no way for the user
            # to know the fast path was gone. `once=True`: the daemon being down is
            # one ongoing condition, not one fact per check.
            from lea import diagnostics
            diagnostics.report(
                "degraded", "lean.lsp_cold_fallback",
                f"The Lean language-server daemon is unavailable ({type(exc).__name__}); "
                "falling back to full compiles.",
                source="lean_check", once=True, path=str(p),
            )

    if lake_root:
        cmd = ["lake", "env", "lean", str(p)]
        cwd = lake_root
    else:
        cmd = ["lean", str(p)]
        cwd = str(p.parent)

    timeout = int(os.environ.get("LEAN_CHECK_TIMEOUT", "900"))
    # Semaphore-bound: this cold compile loads a full Mathlib. Under concurrent
    # runs a single daemon hiccup would otherwise let every run spawn one at
    # once (D74 / item 6). Held across the whole compile so waiters queue.
    with _cold_check_sem:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            if result.returncode == 0 and not output:
                return "OK — no errors, no warnings."
            return output if output else f"Exit code {result.returncode} (no output)."
        except subprocess.TimeoutExpired:
            return f"Error: lean timed out after {timeout}s."
        except FileNotFoundError:
            return "Error: `lean` or `lake` not found. Is Lean 4 installed?"


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
    p = _run_relative_path(path)
    if p.exists():
        try:
            if policy_error := _broad_import_error(p, p.read_text()):
                return policy_error
        except OSError:
            pass
    return lean_check(path)


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
    # workspace races on its `.lake` artifacts (a documented non-goal). mark_stale
    # is called INSIDE this lock (D74 / item 6) so the daemon is flagged before
    # the lock releases — no build completes without its stale flag, and no other
    # build interleaves between this build writing the olean and that flag.
    with _build_lock_for(lake_root):
        try:
            result = subprocess.run(
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
                # This module's .olean on disk just changed, but a persistent LSP
                # daemon (lsp_daemon.py) already holding it imported keeps its own
                # in-memory copy for the life of its process -- this subprocess
                # rebuild can't reach into that other process to fix it. Flag the
                # daemon (if one exists for this lake_root) so its *next* check
                # restarts it first, rather than silently serving the pre-rebuild
                # environment forever. Best-effort: a project with LEA_DISABLE_LSP
                # set, or no daemon started yet, has nothing to flag.
                try:
                    from lea.lsp_daemon import mark_stale
                    mark_stale(lake_root)
                except Exception:
                    pass
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


def bash(command: str, timeout: int = 120) -> str:
    # Run in the active run's working dir (item 8) instead of the process-global
    # cwd, so under concurrent runs one run's shell command can't land in
    # another's tree. `None` (no run context — a standalone/test call) tells
    # subprocess to use the inherited cwd, i.e. today's behavior unchanged.
    cwd = current_working_dir()
    try:
        result = subprocess.run(
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
    search_dir = None
    project_label = "default workspace"

    if path:
        lake_root_str = _find_lake_root(path)
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

    try:
        result = subprocess.run(
            ["grep", "-r", "-n", "--include=*.lean", "-l", query, search_dir],
            capture_output=True,
            text=True,
            timeout=30,
        )
        files = result.stdout.strip().split("\n")
        files = [f for f in files if f][:max_results]
        if not files:
            return f"No Mathlib results for '{query}' in {project_label}."

        # Get matching lines from each file
        lines = []
        for f in files[:max_results]:
            grep_result = subprocess.run(
                ["grep", "-n", query, f],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in grep_result.stdout.strip().split("\n")[:2]:
                short_path = f.split("Mathlib/")[-1] if "Mathlib/" in f else f
                lines.append(f"  Mathlib/{short_path}:{line}")
                if len(lines) >= max_results:
                    break
            if len(lines) >= max_results:
                break

        return f"Found {len(lines)} matches in {project_label}:\n" + "\n".join(lines)
    except subprocess.TimeoutExpired:
        return "Error: search timed out."


def _import_analysis_source(code: str) -> str:
    """Add a broad analysis import + `#import_bumps` to a disposable source copy."""
    lines = code.splitlines(keepends=True)
    clean_lines = without_comments(code).splitlines(keepends=True)
    import_lines = [
        i for i, line in enumerate(clean_lines)
        if IMPORT_COMMAND_RE.fullmatch(line.rstrip("\r\n"))
    ]

    if "Mathlib" not in direct_imports(code):
        insert_at = import_lines[0] if import_lines else 0
        if not import_lines:
            for i, line in enumerate(lines):
                if re.fullmatch(r"[ \t]*module(?:[ \t].*)?[ \t]*(?://.*)?\r?\n?", line):
                    insert_at = i + 1
                    break
        lines.insert(insert_at, "import Mathlib\n")
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
    LSP document. The persistent daemon still handles the check, keeping its broad
    analysis import warm; the temporary document is explicitly closed afterward.
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
    analysis = _import_analysis_source(code)
    output = ""
    with tempfile.TemporaryDirectory(dir=p.parent, prefix=".lea-imports-") as td:
        scratch_dir = Path(td)
        scratch = scratch_dir / p.name
        scratch.write_text(analysis)
        try:
            output = lean_check(str(scratch))
        finally:
            try:
                from .lsp_daemon import close_documents_under

                close_documents_under(str(scratch_dir))
            except Exception:
                pass

    if _import_analysis_has_error(output):
        return (
            "Error: import analysis could not elaborate the disposable copy. "
            "Make sure the original proof compiles before calling `suggest_imports`.\n"
            + output
        )

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
    suggested.extend(module for module in reported if module not in suggested)

    # A broad analysis of even a core-only theorem should report Mathlib as
    # unneeded. If it did not, avoid returning a dangerously empty block merely
    # because a future Mathlib diagnostic format stopped matching our parser.
    if not suggested and "Mathlib" not in unneeded:
        return (
            "Error: Mathlib import analysis completed but its suggestions could "
            "not be parsed. Keep the current targeted imports and run `lean_check`."
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
    result = _verify(path)
    detail = (result.detail or "").strip()
    if result.status == "ok":
        return ("SafeVerify: OK — the proof passed the kernel audit (no sorry / extra axiom / "
                "native_decide / partial), and every theorem in the file verified.")
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
