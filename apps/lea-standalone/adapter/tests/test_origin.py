"""Session origin / providence (P-origin).

A session records where it was spawned from: 'ui' (interactive Lea UI, default) or
'overleaf' (the Overleaf extension), plus `origin_url` = the canonical Overleaf
document URL for an Overleaf-originated session. These pin: the schema columns live
on `sessions` (NOT `projects`), the default is 'ui', list/detail surface the fields,
and usage_stats() rolls usage up per origin for the Stats "By origin" tab.
"""

import sqlite3

from app import db, store


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_sessions_table_has_origin_columns_not_projects(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        session_cols = {row[1] for row in conn.execute("pragma table_info(sessions)").fetchall()}
        project_cols = {row[1] for row in conn.execute("pragma table_info(projects)").fetchall()}
    assert {"origin", "origin_url"} <= session_cols
    # Origin is deliberately kept off the projects table (future projects feature).
    assert "origin" not in project_cols
    assert "origin_url" not in project_cols


def test_create_session_defaults_to_ui_origin(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Interactive theorem")
    assert session["origin"] == "ui"
    assert session["origin_url"] is None

    summary = next(s for s in store.list_sessions() if s["id"] == session["id"])
    detail = store.session_detail(session["id"])
    assert summary["origin"] == "ui" and summary["origin_url"] is None
    assert detail["origin"] == "ui" and detail["origin_url"] is None


def test_overleaf_origin_persists_in_list_and_detail(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    url = "https://www.overleaf.com/project/abc123"
    session = store.create_session("Overleaf theorem", origin="overleaf", origin_url=url)
    assert session["origin"] == "overleaf"
    assert session["origin_url"] == url

    summary = next(s for s in store.list_sessions() if s["id"] == session["id"])
    detail = store.session_detail(session["id"])
    assert summary["origin"] == "overleaf" and summary["origin_url"] == url
    assert detail["origin"] == "overleaf" and detail["origin_url"] == url


def test_usage_stats_origin_rollup(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)

    # One UI session and two Overleaf sessions, each with a finished run carrying usage.
    ui = store.create_session("ui thm")
    r_ui = store.create_run(ui["id"], "gpt-4o", "openai", 3)
    store.update_run(r_ui["id"], "proved", input_tokens=100, output_tokens=25, cost_usd=0.10)

    url = "https://www.overleaf.com/project/doc-a"
    o1 = store.create_session("ov thm one", origin="overleaf", origin_url=url)
    r_o1 = store.create_run(o1["id"], "gpt-4o", "openai", 3)
    store.update_run(r_o1["id"], "proved", input_tokens=200, output_tokens=50, cost_usd=0.20)

    o2 = store.create_session("ov thm two", origin="overleaf", origin_url=url)
    r_o2 = store.create_run(o2["id"], "gpt-4o", "openai", 3)
    store.update_run(r_o2["id"], "proved", input_tokens=300, output_tokens=75, cost_usd=0.30)

    stats = store.usage_stats()
    by_origin = {row["origin"]: row for row in stats["origins"]}

    # Both rows always present (stable UI layout), ui first.
    assert [row["origin"] for row in stats["origins"][:2]] == ["ui", "overleaf"]

    assert by_origin["ui"]["session_count"] == 1
    assert by_origin["ui"]["input_tokens"] == 100
    assert by_origin["ui"]["total_tokens"] == 125
    assert abs(by_origin["ui"]["cost_usd"] - 0.10) < 1e-9

    assert by_origin["overleaf"]["session_count"] == 2
    assert by_origin["overleaf"]["input_tokens"] == 500
    assert by_origin["overleaf"]["output_tokens"] == 125
    assert by_origin["overleaf"]["total_tokens"] == 625
    assert abs(by_origin["overleaf"]["cost_usd"] - 0.50) < 1e-9

    # Rollup totals reconcile with the global all-time totals.
    assert abs(by_origin["ui"]["cost_usd"] + by_origin["overleaf"]["cost_usd"]
               - stats["global"]["cost_usd"]) < 1e-9


def test_origin_rollup_emits_both_rows_when_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    stats = store.usage_stats()
    assert [row["origin"] for row in stats["origins"]] == ["ui", "overleaf"]
    assert all(row["session_count"] == 0 for row in stats["origins"])
