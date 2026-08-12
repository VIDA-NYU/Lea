"""Per-session git store — git owns proof-content history (architecture D7/D8).

Each session is its own git repo at ``<proofs_root>/<session-id>/`` (the proof
``.lean`` files live there so Lake/Lean can compile them; the ``.git`` dir is
invisible to Lake). The adapter — not the prover — owns this store: the prover is
a stateless library, and the adapter is the single source of truth for history.

This module grows across Group B:
  - B1 (here): per-session repo init.
  - B2: commit on every write, with turn/author/tool metadata.
  - B3: read path — snapshot via ``git show``, step diff via ``git diff``.
  - B4: diff-on-divergence — diff since the last agent commit.

A ``GitStore`` holds the proofs root so it can be pointed at a scratch dir in
tests without monkeypatching. The adapter constructs one at startup:
``GitStore(config.lea_root / "workspace" / "proofs")``.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# Identity for commits the adapter makes on the agent's behalf. Set repo-locally
# at init so the store never depends on the host's global git config (there is
# none in Docker/CI, where a global-less ``git commit`` would otherwise fail).
GIT_USER_NAME = "lea"
GIT_USER_EMAIL = "lea@nyu.edu"

# Co-author trailer appended to commit messages (Claude-Code style), so the proof
# history attributes work to Lea even when the committer is the adapter process.
CO_AUTHOR_TRAILER = f"Co-authored-by: Lea <{GIT_USER_EMAIL}>"


class GitStoreError(RuntimeError):
    """A git invocation failed. Carries the failing command's stderr."""


# One lock per repo path, so writers to the SAME repo serialize while unrelated repos
# still commit in parallel (AUDIT-2026-07-24 X2). Keyed by the resolved path rather
# than by `GitStore` instance: callers construct a fresh `GitStore(proofs_root)` per
# operation, so an instance-level lock would guard nothing.
#
# In-process only, and that is the honest scope: the adapter is one uvicorn process and
# its runs are threads inside it. It does NOT cover the agent running `git` through the
# `bash` tool against the same repo — nothing can, short of taking git's own lock — so
# that remains a (pre-existing, unbounded) race.
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _repo_lock(repo: Path) -> threading.Lock:
    key = str(Path(repo).resolve())
    with _repo_locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[key] = lock
        return lock


def commit_message(subject: str) -> str:
    """A commit subject plus the Lea co-author trailer (blank line between)."""
    return f"{subject}\n\n{CO_AUTHOR_TRAILER}"


# The only hosts the GitHub token may be embedded in a URL for (AUDIT-2026-07-24 S2).
# `_inject_token` puts the credential *in the URL*, which git sends on the very first
# request — so this set IS the blast radius of the token, and it belongs here, at the
# credential boundary, not only in whatever route happened to build the URL. Callers
# construct these hosts themselves (`ghimport` clones github.com/gist.github.com; the
# project push target is validated at the route), so nothing legitimate is excluded.
GITHUB_CREDENTIAL_HOSTS = frozenset({"github.com", "www.github.com", "gist.github.com"})


def _inject_token(remote_url: str, token: str) -> str:
    """Embed the token into an https GitHub URL for a single push/clone (D34).

    `https://github.com/owner/repo(.git)` → `https://x-access-token:<token>@github.com/owner/repo`.

    The URL is returned **unchanged** — i.e. the token is not sent at all — unless it
    is `https` *and* its host is in :data:`GITHUB_CREDENTIAL_HOSTS`. ssh/local-path
    remotes were always left alone (the token can't help there); the host check is
    the part that was missing (AUDIT-2026-07-24 S2): a remote pointing at any other
    server turned "push this project" into handing that server the user's PAT, since
    git offers URL credentials to the host before it knows whether the repo exists.
    `http` is excluded too — the credential must not travel in cleartext.

    The result is used as a one-shot push/clone target and is never written to
    `.git/config`, so the token never lands on disk."""
    parsed = urlparse(remote_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return remote_url
    if parsed.hostname.lower() not in GITHUB_CREDENTIAL_HOSTS:
        return remote_url
    netloc = f"x-access-token:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _scrub(text: str, token: str | None) -> str:
    """Remove the token from any git output before it's surfaced/logged — git echoes
    the (tokenized) URL in errors, which would otherwise leak the credential."""
    return text.replace(token, "***") if token else text


class GitStore:
    def __init__(self, proofs_root: Path | str):
        self.root = Path(proofs_root)

    def session_repo(self, session_id: str) -> Path:
        """The repo path for a session. Pure — no side effects, no I/O."""
        return self.root / session_id

    # ── Generalized primitives (D24) ──────────────────────────────────────────
    # A repo is just a directory with a `.git`; loose sessions resolve it from the
    # session id, projects from `proofs/Lea/<Project>` (the shared repo). These two
    # operate on an explicit path so a project session and a loose session share one
    # git implementation; the `session_id` methods below delegate to them.

    def init_repo(self, repo: Path, *, subject: str = "repo init") -> Path:
        """Create (or no-op if present) a git repo at ``repo`` and return its path.

        Idempotent: a second call leaves an existing repo untouched and adds no
        commit. The empty root commit guarantees ``HEAD`` resolves from the very
        start, so the read paths never hit the "no commits yet" edge."""
        if (repo / ".git").is_dir():
            return repo  # already initialised — re-init/resume is a no-op
        repo.mkdir(parents=True, exist_ok=True)
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", GIT_USER_NAME)
        self._git(repo, "config", "user.email", GIT_USER_EMAIL)
        self._git(repo, "commit", "--allow-empty", "-q", "-m", commit_message(subject))
        return repo

    def commit_all(self, repo: Path, subject: str, paths: list[str] | None = None) -> str:
        """Stage changes under ``repo`` and commit them with ``subject``; return the new
        SHA (or the unchanged HEAD when nothing is staged). The single commit primitive
        every write path funnels through (D8: commit on every write).

        ``paths`` limits what is staged to those repo-relative pathspecs. Pass it
        whenever the caller knows what it wrote (AUDIT-2026-07-24 X2): project sessions
        SHARE one repo (D24) and up to ``LEA_MAX_CONCURRENT_RUNS`` of them write at
        once, so an unscoped ``git add -A`` swept a *concurrent* run's half-written
        proof into this run's commit, under this run's message. Harmless for
        correctness — SQL owns proof content — but it makes ``git log`` useless as an
        audit trail, and it is what made the `.gitignore` guard in
        ``uploads.ensure_overleaf_gitignore`` load-bearing rather than belt-and-braces.
        Omit it only where the whole tree really is the unit of work (provisioning, a
        namespace migration).

        Serialized per repo. Git takes ``.git/index.lock`` for the duration of an
        ``add``/``commit``, and a second one arriving meanwhile does not queue — it
        fails outright, which surfaced as a ``GitStoreError`` out of an otherwise
        healthy run. The lock is per resolved repo path, so unrelated repos still
        commit in parallel.
        """
        with _repo_lock(repo):
            # `-A` *with* a pathspec means "all changes under these paths, including
            # deletions" — without it a deleted file's removal would not be staged.
            add_args = ["add", "-A"] + (["--", *paths] if paths else [])
            self._git(repo, *add_args)
            staged = self._git(repo, "diff", "--cached", "--name-only").strip()
            if not staged:
                return self._git(repo, "rev-parse", "HEAD").strip()
            self._git(repo, "commit", "-q", "-m", commit_message(subject))
            return self._git(repo, "rev-parse", "HEAD").strip()

    def commit_write(self, session_id: str, *, turn, author: str = "agent", tool: str) -> str:
        """DEPRECATED — dead as of v2.3; scheduled for deletion with the contract step.

        No application code calls this any more: SQL owns proof content, so a write
        stores its bytes (`store.add_code_step`) instead of committing and keeping a
        sha. Only `test_gitstore.py` still exercises it. **Do not reintroduce it as a
        write path.**

        It is kept only until the contract revision drops the old tables, because the
        rows it produced are still readable until then. The reason it should not come
        back is below.

        Commits the current state of the session repo and returns the new SHA. An
        identical write (nothing staged) makes no commit and returns the current HEAD.

        **That early return is the bug that motivated the migration.** The caller
        stored the returned sha as a pointer to the file it had just written — but
        when nothing was staged, the sha returned is whatever HEAD happens to be,
        an *unrelated* commit. Nothing verified that the sha's tree contained the
        path, so the pointer was wrong the moment it was written. One row in the real
        database says `RealLeAbsSelf.lean @ 51b6adf`, where 51b6adf is a commit
        subject "edit .lea/memory.md" whose tree does not contain that file — the one
        code step 0004's backfill could not recover. No amount of locking would have
        caught it: it was never a race, it was a pointer nobody checked.
        """
        repo = self.session_repo(session_id)
        # Build the subject first; commit_all formats files generically, but the
        # session label carries author/tool/turn for `git log` readability.
        self._git(repo, "add", "-A")
        staged = self._git(repo, "diff", "--cached", "--name-only").strip()
        if not staged:
            return self._git(repo, "rev-parse", "HEAD").strip()
        files = ", ".join(staged.splitlines())
        suffix = f" (turn {turn})" if turn is not None else ""  # user edits have no turn
        self._git(repo, "commit", "-q", "-m", commit_message(f"{author} {tool}: {files}{suffix}"))
        return self._git(repo, "rev-parse", "HEAD").strip()

    def head(self, session_id: str) -> str:
        """The session repo's current HEAD sha (the latest committed state)."""
        return self._git(self.session_repo(session_id), "rev-parse", "HEAD").strip()

    def push_to_github(self, repo: Path, remote_url: str, token: str | None, *, branch: str = "main") -> str:
        """Push ``repo``'s current HEAD to ``remote_url``'s ``branch`` (D34).

        The token (when given) is injected into the push URL for *this invocation
        only* — passed as the push target, never via ``git remote set-url`` — so it
        is never persisted to ``.git/config``. The token is scrubbed from both the
        success summary and any error before they leave this method, since git echoes
        the URL (with the token) in its messages. Raises ``GitStoreError`` (scrubbed)
        on a failed push (auth, non-fast-forward, unreachable, …)."""
        push_url = _inject_token(remote_url, token) if token else remote_url
        try:
            out = self._git(repo, "push", push_url, f"HEAD:refs/heads/{branch}")
        except GitStoreError as exc:
            raise GitStoreError(_scrub(str(exc), token)) from None
        return _scrub(out, token).strip()

    def init_session(self, session_id: str) -> Path:
        """Create (or no-op if present) the session's git repo and return its path.

        Thin wrapper over :meth:`init_repo` for the loose per-session repo."""
        return self.init_repo(self.session_repo(session_id), subject="session init")

    def snapshot(self, session_id: str, sha: str, path: str) -> str:
        """File content at a commit — `git show <sha>:<path>`. The canvas stepper.

        Reconstructs any historical step's content from the SHA + path the DB row
        stores. A path absent at this commit (e.g. the file didn't exist yet)
        returns `""` — a normal empty-canvas state. A bad SHA raises (ls-tree
        errors on an unknown rev), so "no such file" and "no such commit" stay
        distinct. Exact bytes, trailing newline preserved — the canvas sees
        precisely what was committed.
        """
        repo = self.session_repo(session_id)
        listed = self._git(repo, "ls-tree", "--name-only", sha, "--", path).strip()
        if not listed:
            return ""
        return self._git(repo, "show", f"{sha}:{path}")

    def uncommitted_diff(self, session_id: str) -> str:
        """The working-tree change not yet committed — `git diff HEAD`.

        Captures a human canvas edit at the moment of divergence: the Save endpoint
        writes the edited content to the file, calls this (working tree now differs
        from HEAD, which is the agent's last committed state), then commits via
        `commit_write(author="user")`. Must run *before* the commit — afterward the
        working tree is clean and this returns "". This is why we need neither a
        cached `last_agent_sha` nor a git-log boundary scan: the delta is captured
        exactly when it happens. `""` when nothing is uncommitted.
        """
        return self._git(self.session_repo(session_id), "diff", "HEAD")

    def diff(self, session_id: str, sha_a: str, sha_b: str, path: str | None = None) -> str:
        """Change between two steps — `git diff <a> <b> [-- <path>]`. The step-diff view.

        `path` scopes the diff to one file (the canvas passes it from the DB row);
        omitted, it diffs the whole session repo. Accepts any revspecs, so callers
        can diff against `HEAD`. Returns `""` when there is no change.
        """
        repo = self.session_repo(session_id)
        args = ["diff", sha_a, sha_b]
        if path is not None:
            args += ["--", path]
        return self._git(repo, *args)

    def _git(self, repo: Path, *args: str) -> str:
        """Run ``git <args>`` in ``repo`` and return stdout; raise on non-zero exit."""
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitStoreError(f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout
