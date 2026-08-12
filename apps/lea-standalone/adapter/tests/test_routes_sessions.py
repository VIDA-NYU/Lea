"""D2: the sessions route returns code straight from the DB (v2.3).

This file used to pin the opposite: "the DB stores each code_step as a pointer
(commit_sha + path); git owns the proof text (C1/D7) … so the route must hydrate
each step via `GitStore.snapshot`". SQL now owns the content, so `session_detail`
returns each step's bytes and there is no hydrate step to get wrong.

That deleted a whole failure class rather than moving it. Hydration could
half-fail — a missing repo, a bad pointer, a `git show` returning non-zero — and it
degraded to `code: ""`, i.e. a real proof rendering as an empty canvas with no
error. `test_code_survives_without_the_repo` is the direct inversion of the old
`test_hydration_is_graceful_on_a_bad_pointer`.
"""

import asyncio
import shutil

import pytest
from fastapi import HTTPException
from lea.interface import CheckResult, VerifyResult

from app import db, store
from app.config import LeaConfig
from app.gitstore import GitStore
from app.routes import sessions as sessions_route
from app.routes.sessions import FileWriteRequest, PathRequest


PROOF = "import Mathlib\n\ntheorem t : True := by trivial\n"


def _seed_session_with_code(tmp_path):
    """A session with one agent step. The file lands on disk (still
    filesystem-canonical, D3 — the prover compiles from disk) and its bytes are the
    step's content."""
    gs = GitStore(tmp_path / "workspace" / "proofs")
    session = store.create_session("S")
    repo = gs.init_session(session["id"])
    proof = repo / "Lea" / "Misc" / "p.lean"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(PROOF)
    run = store.create_run(session["id"], "m", None, 3)
    store.add_code_step(session["id"], run["id"], "Lea/Misc/p.lean",
                        content=PROOF, author="agent", turn=1, check_status="ok")
    return session, PROOF


def test_session_detail_returns_code_from_the_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, code = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config",
                        lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path))

    detail = sessions_route.session_detail(session["id"])

    assert detail["code_steps"][0]["code"] == code
    # the route is no longer a composition layer over DB + git — the store answers
    # the whole question, so there is nothing left for the route to fill in
    raw = store.session_detail(session["id"])["code_steps"][0]
    assert raw["code"] == code


def test_code_survives_without_the_repo(tmp_path, monkeypatch):
    """The inversion of the old `test_hydration_is_graceful_on_a_bad_pointer`.

    That test wiped the repo and asserted the read degraded to `code: ""` — the best
    a pointer store can do, and a silent one: a proof renders as an empty canvas with
    no error. Now the content is IN the row, so wiping git costs nothing. This is the
    property the whole migration is for."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, code = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config",
                        lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path))

    shutil.rmtree(tmp_path / "workspace" / "proofs")

    detail = sessions_route.session_detail(session["id"])
    assert detail["code_steps"][0]["code"] == code, "content must not depend on git"


def _config_for(tmp_path):
    return lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path)


def test_lean_check_backfills_verdict_onto_the_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)  # seeds a step with check_status="ok"
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_check",
                        lambda p, cold=False: CheckResult(p, "error", "p.lean:2:0: error: boom"))

    result = sessions_route.lean_check_session(session["id"], PathRequest(path="Lea/Misc/p.lean"))

    assert result["status"] == "error" and result["path"] == "Lea/Misc/p.lean"
    # the verdict is back-filled onto the file's latest code_step (canvas + status)
    step = store.latest_code_step_for_path(session["id"], "Lea/Misc/p.lean")
    assert step["check_status"] == "error"
    assert store.session_detail(session["id"])["status"] == "error"


def test_lean_check_with_author_records_a_new_cascade_step_instead_of_backfilling(tmp_path, monkeypatch):
    """docs/FEATURE-overleaf-lean-pane-manual-edit.md: a cascade re-check of a
    dependent file (triggered by an edit elsewhere in the project) gets its own
    code_step, distinct from the step the original write produced -- the file
    on disk didn't change, so the new step holds the same content (and, blobs being
    content-addressed, literally the same blob)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, code = _seed_session_with_code(tmp_path)  # seeds one step, check_status="ok"
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_check",
                        lambda p, cold=False: CheckResult(p, "error", "p.lean:3:0: error: unknown identifier"))

    result = sessions_route.lean_check_session(
        session["id"],
        PathRequest(path="Lea/Misc/p.lean", author="cascade", summary="Re-checked after edit to compactness_criterion"),
    )

    assert result["status"] == "error"
    # 'cascade' is a reason, not an author: the file is still the agent's work
    assert result["code_step"]["author"] == "agent"
    assert result["code_step"]["code"] == code  # no new content

    steps = store.session_detail(session["id"])["code_steps"]
    assert len(steps) == 2  # the original write's step, plus the new cascade step
    assert steps[0]["check_status"] == "ok"       # original step is untouched
    assert steps[-1]["check_status"] == "error"   # the new step carries the fresh verdict
    # same bytes on both steps, stored once
    assert steps[0]["code"] == steps[-1]["code"] == code
    assert steps[-1]["summary"] == "Re-checked after edit to compactness_criterion"


def test_lean_check_with_cascade_author_uses_the_warm_path_not_cold(tmp_path, monkeypatch):
    """A cascade re-check always runs right after a `/rebuild` of some *other*
    module in the project. It deliberately does NOT force `interface_check(...,
    cold=True)` -- a live end-to-end test (tests/lsp/test_cascade_rename_integration.py)
    found the cold subprocess path doesn't reliably see a just-rebuilt
    project-local module's fresh `.olean` either. Correctness instead comes
    from `rebuild_session_module` (tested separately) calling
    `lsp_daemon.mark_stale`, which runs strictly before this check in the
    cascade's own request order -- confirmed by that same test. See
    docs/FEATURE-overleaf-lean-pane-manual-edit.md ('Cascade verification')."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    calls = []

    def _fake_check(p, cold=False):
        calls.append(cold)
        return CheckResult(p, "ok", None)

    monkeypatch.setattr(sessions_route, "interface_check", _fake_check)

    sessions_route.lean_check_session(session["id"], PathRequest(path="Lea/Misc/p.lean", author="cascade"))
    assert calls == [False]

    calls.clear()
    sessions_route.lean_check_session(session["id"], PathRequest(path="Lea/Misc/p.lean"))
    assert calls == [False]

    calls.clear()
    sessions_route.lean_check_session(session["id"], PathRequest(path="Lea/Misc/p.lean", author="user"))
    assert calls == [False]  # every author uses the same (warm) path


def test_lean_check_without_author_still_backfills_as_before(tmp_path, monkeypatch):
    """Regression guard: omitting `author` (every existing caller) must be
    byte-for-byte the original back-fill-only behavior."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_check", lambda p, cold=False: CheckResult(p, "ok", None))

    result = sessions_route.lean_check_session(session["id"], PathRequest(path="Lea/Misc/p.lean"))

    assert "code_step" not in result
    steps = store.session_detail(session["id"])["code_steps"]
    assert len(steps) == 1


def test_verify_returns_the_verdict_for_the_default_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_verify", lambda p: VerifyResult("ok", None))

    result = sessions_route.verify_session(session["id"], PathRequest())  # default = latest step

    assert result["status"] == "ok"
    assert result["path"] == "Lea/Misc/p.lean"


def test_rebuild_returns_the_verdict_for_the_default_file(tmp_path, monkeypatch):
    """docs/FEATURE-overleaf-lean-pane-manual-edit.md, 'Cascade verification':
    unlike lean-check, rebuild forces a real `lake build` (via lea.interface.rebuild
    -> tools.rebuild_module) so a dependent's later lean-check resolves this
    module's current .olean, not a stale one. Route-level: just verify wiring."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_rebuild", lambda p: CheckResult(p, "ok", None))

    result = sessions_route.rebuild_session_module(session["id"], PathRequest(path="Lea/Misc/p.lean"))

    assert result == {"path": "Lea/Misc/p.lean", "status": "ok", "detail": None}


def test_rebuild_surfaces_a_real_compile_failure(tmp_path, monkeypatch):
    """A real `lake build` failure must be reported as-is -- callers (the
    Overleaf cascade) rely on this to distinguish 'safe to trust dependents'
    from 'nothing downstream can be verified right now'."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_rebuild",
                        lambda p: CheckResult(p, "error", "p.lean:2:0: error: boom"))

    result = sessions_route.rebuild_session_module(session["id"], PathRequest(path="Lea/Misc/p.lean"))

    assert result["status"] == "error"
    assert result["detail"] == "p.lean:2:0: error: boom"


def test_rebuild_404_when_session_has_no_proof_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("empty")
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    with pytest.raises(HTTPException) as exc:
        sessions_route.rebuild_session_module(session["id"], PathRequest())
    assert exc.value.status_code == 404


def test_check_404_when_session_has_no_proof_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("empty")
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    with pytest.raises(HTTPException) as exc:
        sessions_route.lean_check_session(session["id"], PathRequest())
    assert exc.value.status_code == 404


def test_write_file_creates_user_step_and_edit_note(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Canvas edit")
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    code = "import Mathlib\n\ntheorem mine : True := by trivial\n"

    result = sessions_route.write_file_session(
        session["id"], FileWriteRequest(path="Lea/Misc/Mine.lean", content=code, note="hand-wrote it"))

    assert result["unchanged"] is False
    step = result["code_step"]
    assert step["author"] == "user" and step["run_id"] is None  # run-less first-class step (D9)
    assert step["code"] == code
    # the edit landed on disk (filesystem-canonical) and is committed
    repo = GitStore(tmp_path / "workspace" / "proofs").session_repo(session["id"])
    assert (repo / "Lea" / "Misc" / "Mine.lean").read_text() == code
    # the note is a linked edit_note message (D11). It used to carry the edit's
    # commit_sha; there is no commit now, and the link that mattered was always
    # positional — the note sits right after the step it explains.
    assert result["note"]["kind"] == "edit_note"
    assert result["note"]["seq"] > step["seq"]
    # it shows up in the session timeline as a user step
    assert store.session_detail(session["id"])["code_steps"][-1]["author"] == "user"


def test_write_file_rejected_during_active_run(tmp_path, monkeypatch):
    # Modal lock (D62): a user write is refused while an agent run is active, so the
    # two never race on the same file. `create_run` starts a run 'pending' = active.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")
    store.create_run(session["id"], "m", None, 3)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    with pytest.raises(HTTPException) as exc:
        sessions_route.write_file_session(
            session["id"], FileWriteRequest(path="Lea/Misc/P.lean", content="x"))
    assert exc.value.status_code == 409


def test_write_file_coalesces_successive_user_edits(tmp_path, monkeypatch):
    # Auto-save (v2.2) writes on every debounced pause; successive user edits to the
    # same file collapse into ONE timeline step (same row, repointed) rather than
    # spraying History with a step per save.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")  # no run -> not locked
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    p = "Lea/Misc/P.lean"

    r1 = sessions_route.write_file_session(
        session["id"], FileWriteRequest(path=p, content="theorem p : True := by trivial\n"))
    r2 = sessions_route.write_file_session(
        session["id"], FileWriteRequest(path=p, content="theorem p : True := by\n  trivial\n"))

    assert r1["code_step"]["id"] == r2["code_step"]["id"]  # same row -> coalesced
    steps = store.session_detail(session["id"])["code_steps"]
    assert len(steps) == 1
    assert steps[-1]["author"] == "user"
    assert steps[-1]["code"] == "theorem p : True := by\n  trivial\n"  # holds the latest bytes


def test_user_edit_after_agent_step_starts_a_new_step(tmp_path, monkeypatch):
    # The human/agent boundary is preserved: a user edit that lands on top of an
    # agent step opens a fresh step (only successive *user* edits coalesce).
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)  # agent step for Lea/Misc/p.lean + a run
    # end the run so the modal lock doesn't block the user write
    run = store.session_detail(session["id"])["runs"][0]
    store.update_run(run["id"], "ok")
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    sessions_route.write_file_session(
        session["id"], FileWriteRequest(path="Lea/Misc/p.lean", content="import Mathlib\n\ntheorem t : True := by\n  trivial\n"))

    steps = store.session_detail(session["id"])["code_steps"]
    assert len(steps) == 2
    assert steps[0]["author"] == "agent" and steps[-1]["author"] == "user"


def test_write_file_no_op_save_creates_no_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    req = FileWriteRequest(path="Lea/Misc/P.lean", content="theorem p : True := by trivial\n")

    sessions_route.write_file_session(session["id"], req)
    again = sessions_route.write_file_session(session["id"], req)  # identical content

    assert again["unchanged"] is True and again["code_step"] is None
    # only the first save made a step
    assert len(store.session_detail(session["id"])["code_steps"]) == 1


def test_write_file_rejects_stale_cross_session_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    project = store.create_project(
        "shared",
        title="Shared",
        description=None,
        namespace="Lea.Shared",
        repo_path="Lea/Shared",
    )
    session_one = store.create_session("A first", project_id=project["id"])
    session_two = store.create_session("A revised", project_id=project["id"])
    theorem = store.create_formalization(
        project_id=project["id"], loose_session_id=None,
        display_title="A", declaration_name="a",
    )
    for session in (session_one, session_two):
        store.link_session_formalization(session["id"], theorem["id"])
    store.link_formalization_file(theorem["id"], "A.lean", "primary")
    store.add_code_step(
        session_one["id"], None, "A.lean",
        content="theorem a : True := by trivial",
        check_status="ok", formalization_id=theorem["id"],
    )
    stale = sessions_route.formalization_service.current_snapshot(theorem["id"])
    store.add_code_step(
        session_two["id"], None, "A.lean",
        content="theorem a : True := by\n  trivial",
        check_status="ok", formalization_id=theorem["id"],
    )
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    with pytest.raises(HTTPException) as exc:
        sessions_route.write_file_session(
            session_one["id"],
            FileWriteRequest(
                path="A.lean",
                content="theorem a : True := by\n  exact True.intro",
                formalization_id=theorem["id"],
                base_revision=stale["revision_token"],
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "revision_conflict"
    assert exc.value.detail["last_updated_session"]["id"] == session_two["id"]
    assert len(store.session_detail(session_one["id"])["code_steps"]) == 1


def test_session_list_events_emits_initial_sessions_changed(tmp_path, monkeypatch):
    # The feed fires `sessions_changed` on connect (digest goes None -> current), so
    # a client that connects mid-change still re-syncs. We pull only the first frame;
    # the generator yields it before its first sleep.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    async def first_frame():
        response = await sessions_route.session_list_events()
        return await response.body_iterator.__anext__()

    first = asyncio.run(first_frame())

    assert "event: sessions_changed" in first


def test_export_session_returns_a_zip_of_its_files(tmp_path, monkeypatch):
    # #14: a loose session's files download as a zip (its own proofs/<id> repo).
    import io
    import zipfile

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)  # writes Lea/Misc/p.lean
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    response = sessions_route.export_session(session["id"])

    assert response.media_type == "application/zip"
    assert response.headers["Content-Disposition"] == 'attachment; filename="s.zip"'  # slug of "S"
    names = zipfile.ZipFile(io.BytesIO(response.body)).namelist()
    assert any(n.endswith("Lea/Misc/p.lean") for n in names)
    assert not any("/.git/" in n for n in names)  # .git excluded by export_zip


def test_export_session_404_when_missing_or_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    # unknown session
    with pytest.raises(HTTPException) as exc:
        sessions_route.export_session("no-such-session")
    assert exc.value.status_code == 404

    # a session that has never written a file → no repo on disk → 404 (not a 500)
    empty = store.create_session("nothing here")
    with pytest.raises(HTTPException) as exc:
        sessions_route.export_session(empty["id"])
    assert exc.value.status_code == 404


def test_export_session_filename_falls_back_to_id(tmp_path, monkeypatch):
    # A title that slugifies to nothing falls back to a session-<id8> filename.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    gs = GitStore(tmp_path / "workspace" / "proofs")
    session = store.create_session("!!!")  # slug → "" → fallback
    repo = gs.init_session(session["id"])
    (repo / "note.txt").write_text("hi")
    gs.commit_write(session["id"], turn=None, author="user", tool="edit")
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    response = sessions_route.export_session(session["id"])
    expected = f'attachment; filename="session-{session["id"][:8]}.zip"'
    assert response.headers["Content-Disposition"] == expected


def test_write_file_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    with pytest.raises(HTTPException) as exc:
        sessions_route.write_file_session(
            session["id"], FileWriteRequest(path="../../escape.lean", content="x"))
    assert exc.value.status_code == 400


# --- G3: manual /compact endpoint -------------------------------------------

def _big_transcript(n_pairs=6, body=4000):
    """A finished-run transcript with `n_pairs` assistant(lean_check)+result pairs whose
    results are large + prunable. More than the default keep-recent-results window (so
    pruning actually masks some), but few enough turns that the summary stage no-ops —
    the endpoint prunes deterministically with no model call."""
    msgs = [{"role": "user", "content": "Prove that 2 + 2 = 4."}]
    for i in range(n_pairs):
        cid = f"c{i}"
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": f"Attempt {i}."},
            {"type": "tool_call", "name": "lean_check", "args": {"path": "P.lean"}, "id": cid}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_name": "lean_check",
             "tool_use_id": cid, "tool_call_id": cid, "content": f"error {i}: " + "x" * body}]})
    return msgs


def test_compact_prunes_the_stored_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")
    run = store.create_run(session["id"], "m", None, None)
    store.update_run(run["id"], "completed")
    store.set_run_transcript(run["id"], _big_transcript())
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    result = sessions_route.compact_session(session["id"])
    assert result["changed"] is True
    assert result["pruned"] >= 1
    assert result["freed_tokens"] > 0
    assert result["before_tokens"] > result["after_tokens"]
    # The Claude-Code-style "still referenced" list: the file the kept lean_check calls touch.
    assert "P.lean" in result["referenced_files"]
    # The condensed transcript was persisted back onto the same run: superseded
    # lean_check output now carries the placeholder instead of the 4k-char dump.
    latest = store.latest_transcript_run_for_session(session["id"])
    assert latest["run_id"] == run["id"]
    dumped = str(latest["messages"])
    assert "pruned to save context" in dumped
    # The marker is a DURABLE timeline message (kind='compaction'), so it survives a reload
    # — the bug where the box vanished on refresh. session_detail must carry it.
    assert result["message"] is not None and result["message"]["kind"] == "compaction"
    markers = [m for m in store.session_detail(session["id"])["messages"]
               if m.get("kind") == "compaction"]
    assert len(markers) == 1


def test_compact_is_a_noop_with_no_finished_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")  # no run, no transcript
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    result = sessions_route.compact_session(session["id"])
    assert result["changed"] is False and result["pruned"] == 0 and result["freed_tokens"] == 0


def test_compact_refuses_while_a_run_is_active(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")
    store.create_run(session["id"], "m", None, None)  # left 'running' → active
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    with pytest.raises(HTTPException) as exc:
        sessions_route.compact_session(session["id"])
    assert exc.value.status_code == 409


# --- AUDIT-2026-07-24 S3: the caller-supplied path is confined to the repo ------

@pytest.mark.parametrize(
    "escape",
    [
        "../../../../etc/passwd",              # classic traversal
        "Lea/Misc/../../../../etc/passwd",     # traversal through a legitimate prefix
        "/etc/passwd",                         # absolute path
        ".git/config",                         # repo internals
        ".lake/packages/mathlib/x.lean",       # build tree
    ],
)
def test_check_verify_and_rebuild_refuse_paths_outside_the_session_repo(
    tmp_path, monkeypatch, escape
):
    """`path` is caller-supplied and these three endpoints run a Lean toolchain over
    whatever it resolves to, handing the diagnostics back — so an unconfined join made
    every readable file on the host reachable through the API."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))

    def never(*args, **kwargs):
        raise AssertionError(f"a Lean toolchain was invoked on the escaping path {escape!r}")

    monkeypatch.setattr(sessions_route, "interface_check", never)
    monkeypatch.setattr(sessions_route, "interface_verify", never)
    monkeypatch.setattr(sessions_route, "interface_rebuild", never)

    for call in (
        sessions_route.lean_check_session,
        sessions_route.verify_session,
        sessions_route.rebuild_session_module,
    ):
        with pytest.raises(HTTPException) as ei:
            call(session["id"], PathRequest(path=escape))
        assert ei.value.status_code == 400, (call.__name__, escape)

    # Nothing was recorded against the escaping path either.
    assert len(store.session_detail(session["id"])["code_steps"]) == 1


def test_escaping_path_is_refused_before_a_cascade_step_is_written(tmp_path, monkeypatch):
    """The `author=` branch of lean-check writes a NEW code_step. Confinement has to
    happen before that, or a refused path still lands in the timeline."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_check",
                        lambda p, cold=False: CheckResult(p, "ok", None))

    with pytest.raises(HTTPException) as ei:
        sessions_route.lean_check_session(
            session["id"], PathRequest(path="../../../../etc/passwd", author="cascade")
        )
    assert ei.value.status_code == 400
    assert len(store.session_detail(session["id"])["code_steps"]) == 1


def test_a_legitimate_path_still_resolves_and_normalizes(tmp_path, monkeypatch):
    """The guard must not change the answer for in-repo paths: an equivalent but
    un-normalized spelling resolves to the same repo-relative key the store uses."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, _ = _seed_session_with_code(tmp_path)
    monkeypatch.setattr(sessions_route, "load_config", _config_for(tmp_path))
    monkeypatch.setattr(sessions_route, "interface_check",
                        lambda p, cold=False: CheckResult(p, "ok", None))

    plain = sessions_route.lean_check_session(session["id"], PathRequest(path="Lea/Misc/p.lean"))
    noisy = sessions_route.lean_check_session(
        session["id"], PathRequest(path="./Lea/Other/../Misc/p.lean")
    )

    assert plain["path"] == noisy["path"] == "Lea/Misc/p.lean"
    assert store.latest_code_step_for_path(session["id"], "Lea/Misc/p.lean")["check_status"] == "ok"
