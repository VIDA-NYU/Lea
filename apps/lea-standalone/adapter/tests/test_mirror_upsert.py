"""Tex-mirror sync modes (PLAN-system-hardening 3.2).

`reconcile` (default) treats the payload as the full truth set — absent files
are pruned and their index rows dropped. `upsert` is the active-buffer tier:
it writes only the provided files and must never delete anything, because the
extension sends just the buffer being edited.
"""

from app import db, projects, store, uploads


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs_root = tmp_path / "workspace" / "proofs"
    proofs_root.mkdir(parents=True)
    project = projects.ensure_project("doc-a", proofs_root)
    uploads.sync_overleaf_tex(project, proofs_root, [
        {"path": "main.tex", "content": "A"},
        {"path": "chapters/ch1.tex", "content": "B"},
    ], commit=False)
    return project, proofs_root


def test_upsert_writes_only_the_given_files_and_never_deletes(tmp_path, monkeypatch):
    project, proofs_root = _setup(tmp_path, monkeypatch)

    summary = uploads.sync_overleaf_tex(project, proofs_root, [
        {"path": "main.tex", "content": "A-edited"},
    ], commit=False, mode="upsert")

    assert summary["updated"] == 1
    assert summary["deleted"] == 0
    assert summary["pruned"] == 0
    base = uploads.overleaf_dir(project, proofs_root)
    assert (base / "main.tex").read_text() == "A-edited"
    assert (base / "chapters" / "ch1.tex").read_text() == "B", \
        "a file absent from the upsert payload must survive"
    rows = store.list_project_files_by_kind(project["id"], uploads.OVERLEAF_KIND)
    assert len(rows) == 2, "index rows for absent files must survive"


def test_upsert_is_a_noop_when_the_buffer_is_unchanged(tmp_path, monkeypatch):
    project, proofs_root = _setup(tmp_path, monkeypatch)

    summary = uploads.sync_overleaf_tex(project, proofs_root, [
        {"path": "main.tex", "content": "A"},
    ], commit=False, mode="upsert")

    assert summary["changed"] is False
    assert summary["unchanged"] == 1


def test_upsert_can_add_a_new_file(tmp_path, monkeypatch):
    project, proofs_root = _setup(tmp_path, monkeypatch)

    summary = uploads.sync_overleaf_tex(project, proofs_root, [
        {"path": "appendix.tex", "content": "C"},
    ], commit=False, mode="upsert")

    assert summary["written"] == 1
    rows = store.list_project_files_by_kind(project["id"], uploads.OVERLEAF_KIND)
    assert len(rows) == 3


def test_reconcile_still_prunes_absent_files(tmp_path, monkeypatch):
    project, proofs_root = _setup(tmp_path, monkeypatch)

    summary = uploads.sync_overleaf_tex(project, proofs_root, [
        {"path": "main.tex", "content": "A"},
    ], commit=False, mode="reconcile")

    assert summary["deleted"] == 1
    base = uploads.overleaf_dir(project, proofs_root)
    assert not (base / "chapters" / "ch1.tex").exists()
    rows = store.list_project_files_by_kind(project["id"], uploads.OVERLEAF_KIND)
    assert len(rows) == 1


def test_mirror_accepts_local_style_and_class_sources(tmp_path, monkeypatch):
    project, proofs_root = _setup(tmp_path, monkeypatch)

    summary = uploads.sync_overleaf_tex(project, proofs_root, [
        {"path": "main.tex", "content": "\\documentclass{local}"},
        {"path": "local.cls", "content": "\\ProvidesClass{local}"},
        {"path": "notation.sty", "content": "\\newcommand{\\NN}{\\mathbb{N}}"},
    ], commit=False)

    base = uploads.overleaf_dir(project, proofs_root)
    assert summary["written"] == 2
    assert (base / "local.cls").is_file()
    assert (base / "notation.sty").is_file()
