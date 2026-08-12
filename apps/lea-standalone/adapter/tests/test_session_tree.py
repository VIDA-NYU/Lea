"""Session tree — parent_id / role / spawned_at_turn (v2.3 item 24, store + migration 0005).

A sub-agent run is a real session that is a CHILD of the coordinator that spawned it.
These pin the schema + the list filters the sidebar relies on:

  * the three tree columns exist after migration 0005;
  * a child carries parent_id / role / spawned_at_turn on its row;
  * children RIDE ALONG in the loose/project lists (the frontend splits roots from
    children, matching the design mock — so the sub-agents block has the data);
  * `list_child_sessions` returns exactly one coordinator's children;
  * a child's parent_id points back at its coordinator, so the frontend can filter.
"""

import sqlite3

from app import db, store


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_tree_columns_exist(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        cols = {row[1] for row in conn.execute("pragma table_info(sessions)").fetchall()}
    assert {"parent_id", "role", "spawned_at_turn"} <= cols


def test_child_carries_tree_fields(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("Prove sqrt2 irrational")
    child = store.create_session(
        "parity / infinite descent",
        parent_id=parent["id"],
        role="proof-candidate",
        spawned_at_turn=4,
    )
    assert child["parent_id"] == parent["id"]
    assert child["role"] == "proof-candidate"
    assert child["spawned_at_turn"] == 4
    # A root session has all three NULL — unchanged shape.
    assert parent["parent_id"] is None and parent["role"] is None and parent["spawned_at_turn"] is None


def test_children_ride_along_in_loose_list_with_parent_id(tmp_path, monkeypatch):
    # The frontend filters roots (parent_id is null) from children — so the loose list
    # must SHIP both, with parent_id set on the child so the split is possible.
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("root proof")
    child = store.create_session("a candidate", parent_id=parent["id"], role="proof-candidate")
    loose = {s["id"]: s for s in store.list_loose_sessions()}
    assert parent["id"] in loose and child["id"] in loose
    assert loose[parent["id"]]["parent_id"] is None
    assert loose[child["id"]]["parent_id"] == parent["id"]  # the frontend keys off this


def test_children_ride_along_in_project_list(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    project = store.create_project("analysis-i", "Analysis I")
    root = store.create_session("in-project root", project_id=project["id"])
    child = store.create_session(
        "in-project child", project_id=project["id"], parent_id=root["id"], role="premise-search"
    )
    proj = {s["id"]: s for s in store.list_project_sessions(project["id"])}
    assert root["id"] in proj and child["id"] in proj
    assert proj[child["id"]]["parent_id"] == root["id"]


def test_list_child_sessions_returns_children(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("coordinator")
    c1 = store.create_session("candidate A", parent_id=parent["id"], role="proof-candidate")
    c2 = store.create_session("candidate B", parent_id=parent["id"], role="premise-search")
    other = store.create_session("unrelated root")
    store.create_session("other's child", parent_id=other["id"], role="proof-candidate")
    kids = {s["id"] for s in store.list_child_sessions(parent["id"])}
    assert kids == {c1["id"], c2["id"]}


def test_unfiltered_list_sessions_includes_children(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("coordinator")
    child = store.create_session("candidate", parent_id=parent["id"], role="proof-candidate")
    all_ids = {s["id"] for s in store.list_sessions()}
    assert {parent["id"], child["id"]} <= all_ids  # stats/search see the whole tree


def test_child_final_summary_is_its_last_agent_message(tmp_path, monkeypatch):
    # The spawn box shows a child's final output without a second fetch: `final_summary`
    # is the child's last AGENT message, populated only for children (a root gets None so
    # a normal list row never carries a big prose blob).
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("coordinator")
    child = store.create_session("candidate", parent_id=parent["id"], role="proof-candidate")
    store.add_message(child["id"], "user", "prove the lemma")
    store.add_message(child["id"], "assistant", "first attempt notes")
    store.add_message(child["id"], "assistant", "FINAL: candidate compiles cleanly")
    rows = {s["id"]: s for s in store.list_sessions()}
    assert rows[child["id"]]["final_summary"] == "FINAL: candidate compiles cleanly"
    assert rows[parent["id"]].get("final_summary") is None  # roots carry no blob
