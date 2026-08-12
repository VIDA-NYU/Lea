"""B1 tests: per-session git repo init (architecture D7).

Each test points a GitStore at a fresh tmp_path root — no monkeypatching, no
dependence on the host's global git config (init sets identity repo-locally).
"""

import subprocess

import pytest

from app.gitstore import GitStore, GitStoreError, CO_AUTHOR_TRAILER, commit_message


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def test_init_creates_git_dir(tmp_path):
    store = GitStore(tmp_path)
    repo = store.init_session("s1")
    assert (repo / ".git").is_dir()
    assert repo == tmp_path / "s1"


def test_head_resolves_after_init(tmp_path):
    # the empty root commit means HEAD exists from the start (B3/B4 depend on this)
    repo = GitStore(tmp_path).init_session("s1")
    sha = _git(repo, "rev-parse", "HEAD")
    assert len(sha) == 40


def test_session_repo_is_pure(tmp_path):
    store = GitStore(tmp_path)
    path = store.session_repo("s1")
    assert path == tmp_path / "s1"
    assert not path.exists()  # pure path query — no side effects


def test_init_is_idempotent(tmp_path):
    store = GitStore(tmp_path)
    repo = store.init_session("s1")
    count_before = _git(repo, "rev-list", "--count", "HEAD")
    repo2 = store.init_session("s1")  # resume — must be a no-op
    assert repo2 == repo
    assert _git(repo, "rev-list", "--count", "HEAD") == count_before


def test_identity_is_repo_local(tmp_path):
    # proves init does not lean on a global git identity
    repo = GitStore(tmp_path).init_session("s1")
    assert _git(repo, "config", "user.name") == "lea"
    assert _git(repo, "config", "user.email") == "lea@nyu.edu"


def test_init_commit_has_co_author_trailer(tmp_path):
    repo = GitStore(tmp_path).init_session("s1")
    body = _git(repo, "log", "-1", "--format=%B")
    assert CO_AUTHOR_TRAILER in body
    assert "session init" in body


def test_commit_message_helper():
    msg = commit_message("did a thing")
    assert msg.startswith("did a thing")
    assert msg.endswith(CO_AUTHOR_TRAILER)
    assert "Co-authored-by: Lea <lea@nyu.edu>" == CO_AUTHOR_TRAILER


def test_git_error_raises(tmp_path):
    store = GitStore(tmp_path)
    repo = tmp_path / "not_a_repo"
    repo.mkdir()
    with pytest.raises(GitStoreError):
        store._git(repo, "rev-parse", "HEAD")  # no repo here -> non-zero exit


# --- B2: commit on every write (D8) -----------------------------------------

def _init_with_file(tmp_path, name="Proof.lean", content="theorem t : True := by trivial\n"):
    store = GitStore(tmp_path)
    repo = store.init_session("s1")
    (repo / name).write_text(content)
    return store, repo


def test_commit_write_returns_new_sha(tmp_path):
    store, repo = _init_with_file(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    sha = store.commit_write("s1", turn=1, tool="write_file")
    assert len(sha) == 40
    assert sha != head_before
    assert sha == _git(repo, "rev-parse", "HEAD")


def test_commit_write_content_is_readable(tmp_path):
    store, repo = _init_with_file(tmp_path, content="theorem t : True := by trivial\n")
    store.commit_write("s1", turn=1, tool="write_file")
    assert _git(repo, "show", "HEAD:Proof.lean") == "theorem t : True := by trivial"


def test_commit_subject_is_human_readable(tmp_path):
    # readable label for `git log` — mentions author, tool, file, turn. No machine trailers.
    store, repo = _init_with_file(tmp_path)
    store.commit_write("s1", turn=3, author="agent", tool="write_file")
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject == "agent write_file: Proof.lean (turn 3)"
    body = _git(repo, "log", "-1", "--format=%B")
    assert CO_AUTHOR_TRAILER in body
    assert "Lea-turn" not in body and "Lea-author" not in body  # no dead machine metadata


def test_commit_write_noop_when_unchanged(tmp_path):
    store, repo = _init_with_file(tmp_path)
    sha1 = store.commit_write("s1", turn=1, tool="write_file")
    count_after_first = _git(repo, "rev-list", "--count", "HEAD")
    sha2 = store.commit_write("s1", turn=2, tool="write_file")  # nothing changed
    assert sha2 == sha1
    assert _git(repo, "rev-list", "--count", "HEAD") == count_after_first


def test_commit_write_commits_non_compiling_state(tmp_path):
    # D8: failed/non-compiling states are committed too — git doesn't compile anything
    store, repo = _init_with_file(tmp_path, content="theorem broken : True := by\n  this is not lean\n")
    sha = store.commit_write("s1", turn=1, tool="write_file")
    assert len(sha) == 40
    assert "this is not lean" in _git(repo, "show", "HEAD:Proof.lean")


def test_commit_write_records_user_author(tmp_path):
    # D9: user edits reuse the same path with author="user"
    store, repo = _init_with_file(tmp_path)
    store.commit_write("s1", turn=None, author="user", tool="edit_file")
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject.startswith("user edit_file:")


# --- B3: read path — snapshot via git show, diff via git diff (D8) -----------

def test_snapshot_returns_exact_content(tmp_path):
    content = "theorem t : True := by trivial\n"  # note trailing newline
    store, repo = _init_with_file(tmp_path, content=content)
    sha = store.commit_write("s1", turn=1, tool="write_file")
    assert store.snapshot("s1", sha, "Proof.lean") == content


def test_snapshot_across_two_commits(tmp_path):
    store, repo = _init_with_file(tmp_path, content="v1\n")
    sha1 = store.commit_write("s1", turn=1, tool="write_file")
    (repo / "Proof.lean").write_text("v2\n")
    sha2 = store.commit_write("s1", turn=2, tool="edit_file")
    assert store.snapshot("s1", sha1, "Proof.lean") == "v1\n"
    assert store.snapshot("s1", sha2, "Proof.lean") == "v2\n"


def test_snapshot_absent_path_is_empty(tmp_path):
    # the root commit has no proof file yet -> empty canvas, not an error
    store = GitStore(tmp_path)
    store.init_session("s1")
    root = _git(tmp_path / "s1", "rev-parse", "HEAD")
    assert store.snapshot("s1", root, "Proof.lean") == ""


def test_snapshot_bad_sha_raises(tmp_path):
    store = GitStore(tmp_path)
    store.init_session("s1")
    with pytest.raises(GitStoreError):
        store.snapshot("s1", "deadbeef" * 5, "Proof.lean")  # no such commit


def test_diff_between_two_commits(tmp_path):
    store, repo = _init_with_file(tmp_path, content="old line\n")
    sha1 = store.commit_write("s1", turn=1, tool="write_file")
    (repo / "Proof.lean").write_text("new line\n")
    sha2 = store.commit_write("s1", turn=2, tool="edit_file")
    out = store.diff("s1", sha1, sha2)
    assert "-old line" in out and "+new line" in out


def test_diff_no_change_is_empty(tmp_path):
    store, repo = _init_with_file(tmp_path)
    sha = store.commit_write("s1", turn=1, tool="write_file")
    assert store.diff("s1", sha, sha) == ""


def test_diff_scoped_to_path(tmp_path):
    store, repo = _init_with_file(tmp_path, content="a\n")
    (repo / "Other.lean").write_text("x\n")
    sha1 = store.commit_write("s1", turn=1, tool="write_file")
    (repo / "Proof.lean").write_text("b\n")
    (repo / "Other.lean").write_text("y\n")
    sha2 = store.commit_write("s1", turn=2, tool="edit_file")
    out = store.diff("s1", sha1, sha2, path="Proof.lean")
    assert "Proof.lean" in out and "Other.lean" not in out


def test_diff_from_root_shows_file_added(tmp_path):
    store, repo = _init_with_file(tmp_path, content="theorem t : True := by trivial\n")
    root = _git(repo, "rev-parse", "HEAD")
    sha = store.commit_write("s1", turn=1, tool="write_file")
    out = store.diff("s1", root, sha, path="Proof.lean")
    assert "new file" in out and "+theorem t" in out


# --- B4: uncommitted_diff — capture a human edit before its Save commit (D12) -

def test_uncommitted_diff_captures_working_change(tmp_path):
    # the Save flow: agent state committed, then the human's edit lands in the tree
    store, repo = _init_with_file(tmp_path, content="agent line\n")
    store.commit_write("s1", turn=1, tool="write_file")    # HEAD = agent's last state
    (repo / "Proof.lean").write_text("human line\n")        # human edits the canvas
    out = store.uncommitted_diff("s1")                      # diff HEAD, before committing
    assert "-agent line" in out and "+human line" in out


def test_uncommitted_diff_empty_when_clean(tmp_path):
    store, repo = _init_with_file(tmp_path)
    store.commit_write("s1", turn=1, tool="write_file")     # tree clean after commit
    assert store.uncommitted_diff("s1") == ""


def test_uncommitted_diff_empty_after_its_commit(tmp_path):
    # the delta must be captured BEFORE commit_write; afterward it's gone
    store, repo = _init_with_file(tmp_path, content="a\n")
    store.commit_write("s1", turn=1, tool="write_file")
    (repo / "Proof.lean").write_text("b\n")
    captured = store.uncommitted_diff("s1")                 # capture first
    store.commit_write("s1", turn=None, author="user", tool="edit_file")  # then commit
    assert captured != ""
    assert store.uncommitted_diff("s1") == ""


# --- D34 / AUDIT-2026-07-24 S2: the token only ever goes to GitHub -------------

def test_inject_token_embeds_the_credential_for_github(tmp_path):
    from app.gitstore import _inject_token

    assert _inject_token("https://github.com/me/repo", "ghp_secret") == (
        "https://x-access-token:ghp_secret@github.com/me/repo"
    )
    # gist clones (ghimport's private-gist path) are the same credential domain
    assert "x-access-token:ghp_secret@gist.github.com" in _inject_token(
        "https://gist.github.com/abc123.git", "ghp_secret"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/me/repo",          # the S2 exfiltration target
        "https://github.com.evil.example.com/a/b",   # suffix-lookalike host
        "https://raw.githubusercontent.com/a/b",     # GitHub-owned, but not a clone host
        "http://github.com/me/repo",                 # cleartext: never send the token
        "git@github.com:me/repo.git",                # ssh: URL credentials don't apply
    ],
)
def test_inject_token_refuses_every_non_github_https_target(url):
    """The token is embedded in the URL, which git sends on its FIRST request — so a
    remote pointing anywhere else must come back untouched rather than authenticated."""
    from app.gitstore import _inject_token

    assert _inject_token(url, "ghp_secret") == url
    assert "ghp_secret" not in _inject_token(url, "ghp_secret")


def test_push_to_a_non_github_remote_sends_no_credential(tmp_path):
    """End-to-end at the gitstore seam: pushing to a non-GitHub https remote fails
    (nothing is listening), and the token appears nowhere in the attempted URL."""
    store, repo = _init_with_file(tmp_path)
    store.commit_write("s1", turn=1, tool="write_file")
    with pytest.raises(GitStoreError) as ei:
        store.push_to_github(repo, "https://127.0.0.1:9/steal/me", "ghp_secret")
    assert "ghp_secret" not in str(ei.value)


# --- AUDIT-2026-07-24 X2: one repo, many concurrent writers -------------------

def test_concurrent_commits_to_one_repo_all_succeed(tmp_path):
    """Project sessions SHARE a repo (D24) and up to LEA_MAX_CONCURRENT_RUNS write at
    once. Git takes .git/index.lock for an add/commit and a second arrival does not
    queue — it fails outright, surfacing as a GitStoreError out of a healthy run."""
    import threading

    store = GitStore(tmp_path)
    repo = tmp_path / "shared"
    store.init_repo(repo)

    errors: list[Exception] = []
    start = threading.Barrier(8)

    def writer(n: int) -> None:
        path = f"file{n}.txt"
        (repo / path).write_text(f"content {n}\n")
        start.wait(timeout=10)
        try:
            store.commit_all(repo, f"write {path}", paths=[path])
        except Exception as exc:  # noqa: BLE001 — the point of the test
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent commits failed: {errors[0]}"
    for n in range(8):
        assert (repo / f"file{n}.txt").read_text() == f"content {n}\n"
    log = store._git(repo, "log", "--oneline")
    assert log.count("\n") >= 8, "every writer should have produced a commit"


def test_a_scoped_commit_does_not_sweep_up_a_concurrent_run_s_file(tmp_path):
    """`git add -A` staged everything, so one run's commit carried another run's
    half-written proof under its message — which is what made `git log` useless as an
    audit trail."""
    store = GitStore(tmp_path)
    repo = tmp_path / "shared"
    store.init_repo(repo)

    (repo / "mine.lean").write_text("theorem mine : True := trivial\n")
    (repo / "theirs.lean").write_text("half written, another run is mid-write\n")

    store.commit_all(repo, "agent write_file: mine.lean", paths=["mine.lean"])

    committed = store._git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["mine.lean"]
    # The other run's file is untouched on disk and still uncommitted — its own
    # commit_all will pick it up.
    assert (repo / "theirs.lean").exists()
    assert "theirs.lean" in store._git(repo, "status", "--porcelain")


def test_a_scoped_commit_stages_a_deletion(tmp_path):
    """Pathspec-scoped staging must still record removals — `git add` without `-A`
    would skip them, so a delete would silently never be committed."""
    store = GitStore(tmp_path)
    repo = tmp_path / "shared"
    store.init_repo(repo)
    (repo / "gone.txt").write_text("here\n")
    store.commit_all(repo, "add", paths=["gone.txt"])

    (repo / "gone.txt").unlink()
    store.commit_all(repo, "delete gone.txt", paths=["gone.txt"])

    assert "gone.txt" not in store._git(repo, "ls-files")
    assert store._git(repo, "status", "--porcelain").strip() == ""


def test_unscoped_commit_all_still_takes_the_whole_tree(tmp_path):
    """Provisioning and namespace migration genuinely mean 'everything'."""
    store = GitStore(tmp_path)
    repo = tmp_path / "shared"
    store.init_repo(repo)
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")

    store.commit_all(repo, "seed everything")

    committed = set(store._git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == {"a.txt", "b.txt"}


def test_different_repos_are_not_serialized_against_each_other(tmp_path):
    """The lock is per repo path, so unrelated repos still commit in parallel."""
    from app.gitstore import _repo_lock

    store = GitStore(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    store.init_repo(one)
    store.init_repo(two)

    assert _repo_lock(one) is _repo_lock(one), "same repo shares one lock"
    assert _repo_lock(one) is not _repo_lock(two), "different repos must not share a lock"
    # ...and the identity survives a differently-spelled path for the same repo.
    assert _repo_lock(one) is _repo_lock(tmp_path / "." / "one")
