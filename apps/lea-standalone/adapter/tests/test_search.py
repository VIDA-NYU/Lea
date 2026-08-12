"""V1 tests: global search across loose + project sessions (D31/D41).

`store.search_sessions` matches a session by its own title OR its project's title,
and tags each hit with its project so the ⌘K overlay can section loose vs in-project.
The route is a thin wrapper, exercised here too."""

from app import db, store
from app.routes.search import search


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _project(slug, title):
    namespace = store.project_namespace_for_slug(slug)
    return store.create_project(
        slug, title=title, description=None,
        namespace=namespace, repo_path=store.repo_path_for_namespace(namespace),
    )


def test_matches_loose_session_by_title(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    store.create_session("Irrationality of sqrt 2")
    store.create_session("Something unrelated")

    hits = store.search_sessions("sqrt")
    assert [h["title"] for h in hits] == ["Irrationality of sqrt 2"]
    assert hits[0]["project_id"] is None  # loose


def test_matches_project_session_and_tags_it(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    project = _project("real-analysis", "Real Analysis")
    store.create_session("continuous_sq lemma", project_id=project["id"])

    hits = store.search_sessions("continuous")
    assert len(hits) == 1
    assert hits[0]["project_id"] == project["id"]
    assert hits[0]["project_title"] == "Real Analysis"
    assert hits[0]["project_namespace"] == "Lea.RealAnalysis"


def test_matches_by_project_title(tmp_path, monkeypatch):
    # A session whose own title doesn't match, but whose project title does.
    _fresh_db(tmp_path, monkeypatch)
    project = _project("topology", "Topology")
    store.create_session("lemma A", project_id=project["id"])

    hits = store.search_sessions("topo")
    assert [h["title"] for h in hits] == ["lemma A"]
    assert hits[0]["project_title"] == "Topology"


def test_blank_query_returns_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    store.create_session("anything")
    assert store.search_sessions("") == []
    assert store.search_sessions("   ") == []


def test_no_match_returns_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    store.create_session("Irrationality of sqrt 2")
    assert store.search_sessions("zzz-no-such") == []


def test_like_wildcards_are_literal(tmp_path, monkeypatch):
    # A query with `%` must match the literal character, not act as a wildcard.
    _fresh_db(tmp_path, monkeypatch)
    store.create_session("100% complete")
    store.create_session("plain title")

    hits = store.search_sessions("100%")
    assert [h["title"] for h in hits] == ["100% complete"]


def test_route_returns_results_envelope(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    store.create_session("Irrationality of sqrt 2")

    payload = search("sqrt")
    assert [r["title"] for r in payload["results"]] == ["Irrationality of sqrt 2"]
    assert search("")["results"] == []


def test_route_unions_formalizations_with_conversations(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    project = _project("topology", "Topology")
    session = store.create_session("A conversation", project_id=project["id"])
    item = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="Compact image",
        declaration_name="compact_image",
    )
    store.link_session_formalization(session["id"], item["id"])
    store.link_formalization_file(item["id"], "compact_image.lean", "primary")

    payload = search("compact")

    assert len(payload["results"]) == 1
    hit = payload["results"][0]
    assert hit["result_type"] == "formalization"
    assert hit["id"] == item["id"]
    assert hit["session_id"] == session["id"]
    assert hit["primary_path"] == "compact_image.lean"
