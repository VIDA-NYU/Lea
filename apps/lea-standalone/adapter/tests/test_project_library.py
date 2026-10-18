"""Canonical reuse without provider calls; real Lean inspection is tested below."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil

import pytest

from app import db, store, project_library as library, project_dependencies as deps
from app.routes import runs
from app.config import LeaConfig
from lea.events import CheckResult, VerifyResult

REAL_INSPECT_SPEC = library.inspect_specification


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "library.sqlite3")
    db.init_db()
    p = store.get_or_create_project("study", "Study")
    repo = tmp_path / "proofs/Lea/Study"
    repo.mkdir(parents=True)
    monkeypatch.setattr(library, "project_repo", lambda _: repo)
    monkeypatch.setattr(library, "inspect_specification", lambda _, s: {
        "kind": s.get("kind", "theorem"), "type": s["signature"],
        "type_expr": s["signature"], "body_expr": s.get("body"), "axioms": []})
    return p, repo


def spec(signature=": True", **changes):
    return {**dict(name="SharedFact", kind="theorem", statement="A precise shared result",
                   signature=signature, imports=[], sources=[]), **changes}


def target(p, name):
    return store.create_formalization(project_id=p["id"], loose_session_id=None,
        display_title=name, declaration_name=name, statement=name)


def active_run(p, name="A"):
    bundle = store.create_run_bundle(message=name, session_id=None, project_id=p["id"],
        session_origin="overleaf", session_origin_url=None, model="test", provider=None,
        max_turns=10, autonomous=True, new_formalization={"display_title": name, "declaration_name": name})
    store.claim_pending_run(bundle["run"]["id"])
    return store.get_run(bundle["run"]["id"])


def install_inspection(monkeypatch, dep, *, wrong=False):
    expected = dep["specification"]["expected"]
    declaration = dict(expected, full_name=dep["declaration_name"], user_name=dep["declaration_name"], visibility="public")
    if wrong:
        declaration["type_expr"] = ": False"
    monkeypatch.setattr(library, "inspect_module", lambda *_: [declaration])
    monkeypatch.setattr(library, "rebuild", lambda *_: CheckResult("module", "ok"))
    monkeypatch.setattr(library, "verify", lambda *_: VerifyResult("ok"))


def test_helpers_private_unicode_and_inventory_over_limit(project):
    p, repo = project
    for index in range(110):
        (repo / f"Module{index:03}.lean").write_text(f"namespace Lea.Study\ntheorem lemma{index} : True := by trivial\nend Lea.Study\n")
    (repo / "Last.lean").write_text("namespace Lea.Study\nprivate theorem α_helper : True := by trivial\nlemma β : True := by trivial\nend Lea.Study\n")
    (repo / ".lea/tmp").mkdir(parents=True)
    (repo / ".lea/tmp/Probe.lean").write_text("theorem probe : False := by sorry")
    records = library.refresh(p)
    assert len(records) == 112
    assert not any(d["path"].startswith(".lea") for d in records)
    assert next(d for d in records if d["user_name"] == "Lea.Study.α_helper")["visibility"] == "private"
    assert library.search(p, "lemma109", validate=False)["total"] == 1


def test_read_only_library_search_does_not_resolve_every_environment(project, monkeypatch):
    p, repo = project
    for name in ['Needle', 'Other']:
        (repo / (name + '.lean')).write_text('theorem ' + name + ' : True := by trivial\n')
    monkeypatch.setattr(library, 'environment_hash', lambda *_: pytest.fail('Search inventory must not resolve environments'))
    monkeypatch.setattr(library, 'validate_module', lambda *_: pytest.fail('Read-only search must not validate'))
    result = library.search(p, 'Needle', validate=False)
    assert [d['path'] for d in result['declarations']] == ['Needle.lean']
    assert not result['declarations'][0]['reusable']


def test_library_search_validates_only_returned_modules_and_keeps_freshness_checks(project, monkeypatch):
    p, repo = project
    for name in ['Needle', 'Other']:
        (repo / (name + '.lean')).write_text('theorem ' + name + ' : True := by trivial\n')
    hashes, audits = [], []
    def environment(_project, _repo, path):
        hashes.append(path.name)
        return library.digest(path.read_bytes())
    monkeypatch.setattr(library, 'environment_hash', environment)
    monkeypatch.setattr(library, 'rebuild', lambda path: CheckResult(path, 'ok'))
    monkeypatch.setattr(library, 'inspect_module', lambda *args: [{**library._lexical((repo / args[-1]).read_text())[0],
        'axioms': [], 'type_expr': 'True', 'body_expr': None}])
    monkeypatch.setattr(library, 'verify', lambda path: (audits.append(path) or VerifyResult('ok')))
    result = library.search(p, 'Needle')
    assert hashes and set(hashes) == {'Needle.lean'}
    assert len(audits) == 1 and result['declarations'][0]['reusable']
    hashes.clear()
    library.search(p, 'Needle')
    assert set(hashes) == {'Needle.lean'} and len(audits) == 1
    (repo / 'Needle.lean').write_text('theorem Needle : True := by exact True.intro\n')
    library.search(p, 'Needle')
    assert len(audits) == 2


def test_atomic_reservation_and_two_consumers(project):
    p, _ = project
    a, b = target(p, "A"), target(p, "B")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: deps.ensure(p, spec(), requester_id=[a,b][i % 2]["id"]), range(16)))
    assert len({r["dependency_id"] for r in results}) == 1
    dep = deps.get(results[0]["dependency_id"])
    assert len(dep["attempts"]) == 1
    assert set(dep["requesting_targets"]) == {a["id"], b["id"]}
    assert dep["path"].startswith("Shared/")


def test_definitions_with_same_type_different_bodies_remain_distinct(project):
    p, _ = project
    one = spec(": Nat"); one.update(kind="definition", body="1")
    two = {**one, "body": "2"}
    assert deps.ensure(p, one)["dependency_id"] != deps.ensure(p, two)["dependency_id"]


@pytest.mark.parametrize("kind", ["definition", "theorem"])
@pytest.mark.parametrize("mode", ["inline", "deferred"])
@pytest.mark.parametrize("qualified", [False, True])
def test_primary_target_cannot_reserve_itself(project, monkeypatch, kind, mode, qualified):
    p, repo = project
    run = active_run(p, "descent_energy")
    fid = run["focus_formalization_id"]
    store.update_formalization(fid, kind=kind)
    store.link_formalization_file(fid, "descent_energy.lean", "primary")
    requested = p["namespace"] + ".descent_energy" if qualified else "descent_energy"

    def unexpected(*args, **kwargs):
        pytest.fail("A self-reservation must be rejected before compiler work")

    monkeypatch.setattr(library, "specification_environment", unexpected)
    with pytest.raises(ValueError, match="current formalization target") as rejected:
        deps.ensure(p, spec(name=requested, kind=kind, body="1" if kind == "definition" else None),
                    run_id=run["id"], mode=mode)
    assert "descent_energy.lean" in str(rejected.value)
    with db.connect() as conn:
        for table in ("project_dependencies", "project_dependency_attempts", "project_dependency_edges"):
            assert conn.execute(f"select count(*) from {table}").fetchone()[0] == 0
        assert conn.execute("select count(*) from formalizations").fetchone()[0] == 1
    assert not (repo / "Shared").exists()
    assert json.loads(store.get_run(run["id"])["waiting_dependencies_json"] or "[]") == []


def test_primary_target_guard_handles_qualified_declaration_and_original_source_label(project):
    p, _ = project
    run = active_run(p, "descent_energy")
    fid = run["focus_formalization_id"]
    store.update_formalization(fid, declaration_name=p["namespace"] + ".renamed_energy")
    with db.write() as conn:
        conn.execute("update formalizations set origin='overleaf',origin_key=? where id=?",
                     ("document:definition:descent_energy", fid))
    for name in ("renamed_energy", p["namespace"] + ".renamed_energy", "descent_energy",
                 p["namespace"] + ".descent_energy", "_root_." + p["namespace"] + ".renamed_energy"):
        with pytest.raises(ValueError, match="current formalization target"):
            deps.ensure(p, spec(name=name), run_id=run["id"], mode="inline")
    # An explicit alternative requester must not bypass the run's focus.
    other = target(p, "Other")
    with pytest.raises(ValueError, match="current formalization target"):
        deps.ensure(p, spec(name="descent_energy"), run_id=run["id"], requester_id=other["id"], mode="inline")


def test_requester_without_run_cannot_reserve_itself(project):
    p, _ = project
    own = target(p, "Target")
    with pytest.raises(ValueError, match="current formalization target"):
        deps.ensure(p, spec(name="Target"), requester_id=own["id"])


def test_distinct_helper_with_same_contract_or_short_name_is_allowed(project):
    p, _ = project
    run = active_run(p, "energy")
    supporting = deps.ensure(p, spec(name="Other.energy"), run_id=run["id"], mode="inline")
    assert supporting["outcome"] == "inline"
    assert supporting["dependency"]["formalization_id"] != run["focus_formalization_id"]


def test_tool_rejects_primary_reservation_then_accepts_real_helper(project):
    p, _ = project
    run = active_run(p, "descent_energy")
    arguments = {"mode": "inline", "specification": spec(name="descent_energy", kind="definition", body="1")}
    with pytest.raises(ValueError, match="current formalization target"):
        deps.tool_request(run["id"], "self-reservation", "request_project_dependency", arguments)
    with db.connect() as conn:
        assert conn.execute("select count(*) from project_library_invocations").fetchone()[0] == 0
    supporting = {"mode": "inline", "specification": spec(name="SupportingEnergy", kind="definition", body="1")}
    result = deps.tool_request(run["id"], "supporting-result", "request_project_dependency", supporting)
    assert result["outcome"] == "inline"
    assert result["dependency"]["requesting_targets"] == [run["focus_formalization_id"]]
    assert deps.tool_request(run["id"], "supporting-result", "request_project_dependency", supporting) == result


def test_inline_handoff_preserves_draft_notes_and_attempt_identity(project):
    p, repo = project
    run = active_run(p)
    first = deps.ensure(p, spec(notes="A finite-cover route may work."), run_id=run["id"], mode="inline")
    path = repo / first["path"]
    path.parent.mkdir(parents=True)
    path.write_text(first["skeleton"])
    transferred = deps.ensure(p, spec(notes="The compactness step still needs investigation."), run_id=run["id"], mode="deferred")
    assert transferred["dependency_id"] == first["dependency_id"]
    assert transferred["dependency"]["generation"] == 1
    assert transferred["dependency"]["owner_run_id"] is None
    assert path.read_text() == first["skeleton"]
    assert "finite-cover" in transferred["dependency"]["specification"]["notes"]
    assert "compactness" in transferred["dependency"]["specification"]["notes"]


def test_failure_cached_until_retry_or_relevant_source_change(project):
    p, _ = project
    first = deps.ensure(p, spec())
    with db.write() as conn:
        conn.execute("update project_dependencies set state='failed',blocker_json=? where id=?",
                     (json.dumps({"detail": "Missing supporting mathematics"}), first["dependency_id"]))
    again = deps.ensure(p, spec())
    assert again["outcome"] == "blocked" and again["dependency"]["generation"] == 1
    retried = deps.retry(first["dependency_id"])
    assert retried["dependency"]["generation"] == 2
    with db.write() as conn:
        conn.execute("update project_dependencies set state='failed' where id=?", (first["dependency_id"],))
    changed = deps.ensure(p, spec(sources=[{"path": "proof.tex", "content": "new supporting argument"}]))
    assert changed["dependency"]["generation"] == 3


def test_citation_is_metadata_and_missing_statement_is_shared_blocker(project):
    p, _ = project
    a = deps.ensure(p, spec(citation={"key": "BPS", "locator": "Theorem 2"}))
    b = deps.ensure(p, spec(": False", citation={"key": "BPS", "locator": "Theorem 3"}))
    assert a["dependency_id"] != b["dependency_id"] and a["path"].startswith("External/BPS/")
    missing = deps.ensure(p, spec(signature=""))
    assert missing["outcome"] == "blocked" and missing["dependency"]["blocker"]["category"] == "source_gap"


def test_dependency_graph_and_import_cycles_rejected(project):
    p, repo = project
    a = target(p, "A")
    first = deps.ensure(p, spec(), requester_id=a["id"])["dependency"]
    with pytest.raises(ValueError, match="cycle"):
        deps.ensure(p, spec(), requester_id=first["formalization_id"])
    (repo / "A.lean").write_text("theorem A : True := by trivial")
    store.link_formalization_file(a["id"], "A.lean", "primary")
    with pytest.raises(ValueError, match="unfinished"):
        deps.ensure(p, spec(imports=["Lea.Study.A"]), requester_id=a["id"])


def test_inline_publication_survives_parent_failure_and_b_reuses(project, monkeypatch):
    p, repo = project
    run = active_run(p)
    allocation = deps.ensure(p, spec(), run_id=run["id"], mode="inline")
    dep = allocation["dependency"]
    path = repo / dep["path"]
    path.parent.mkdir(parents=True)
    path.write_text(allocation["skeleton"].replace("sorry", "trivial"))
    install_inspection(monkeypatch, dep)
    assert deps.publish_owned(run["id"], dep["path"])["ready"]
    store.update_run(run["id"], "failed", result_detail="A failed after L was proved")
    deps.settle_run(run["id"])
    b = target(p, "B")
    reused = deps.ensure(p, spec(), requester_id=b["id"])
    assert reused["outcome"] == "existing"
    assert reused["dependency_id"] == dep["id"]
    assert len(reused["dependency"]["attempts"]) == 1
    assert len(reused["dependency"]["requesting_targets"]) == 2
    assert store.list_artifacts_for_scope(p["id"])[0]["path"] == dep["path"]
    # Changed published bytes are certified again without another proof attempt.
    path.write_text(path.read_text() + "\n-- edited\n")
    assert deps.get(dep["id"])["ready"]
    assert len(deps.get(dep["id"])["attempts"]) == 1
    install_inspection(monkeypatch, dep, wrong=True)
    path.write_text(path.read_text() + "\n-- invalid revision\n")
    assert not deps.get(dep["id"])["ready"]
    assert deps.ensure(p, spec(), requester_id=b["id"])["outcome"] == "blocked"
    assert len(deps.get(dep["id"])["attempts"]) == 1
    path.unlink()
    assert deps.get(dep["id"])["state"] == "stale"


def test_weakened_statement_and_missing_audit_never_publish(project, monkeypatch):
    p, repo = project
    run = active_run(p)
    allocation = deps.ensure(p, spec(), run_id=run["id"], mode="inline")
    dep = allocation["dependency"]
    path = repo / dep["path"]; path.parent.mkdir(parents=True); path.write_text(allocation["skeleton"])
    install_inspection(monkeypatch, dep, wrong=True)
    assert deps.publish_owned(run["id"], dep["path"]) is None
    assert not deps.get(dep["id"])["ready"]
    path.write_text(path.read_text() + "\n-- new version\n")
    install_inspection(monkeypatch, dep)
    monkeypatch.setattr(library, "verify", lambda *_: VerifyResult("unavailable"))
    assert deps.publish_owned(run["id"], dep["path"]) is None


def test_stats_projects_dependency_evidence_without_refreshing_or_building(project, monkeypatch):
    p, repo = project
    run = active_run(p)
    allocation = deps.ensure(p, spec(), run_id=run["id"], mode="inline")
    dep = allocation["dependency"]
    path = repo / dep["path"]
    path.parent.mkdir(parents=True)
    path.write_text(allocation["skeleton"].replace("sorry", "trivial"))
    install_inspection(monkeypatch, dep)
    assert deps.publish_owned(run["id"], dep["path"])["ready"]
    store.update_run(run["id"], "completed")

    def unexpected(*args, **kwargs):
        pytest.fail("Reading stats must not refresh or compile project modules")
    monkeypatch.setattr(library, "refresh", unexpected)
    monkeypatch.setattr(library, "validate_module", unexpected)
    from app import formalizations
    assert formalizations.decorate([store.get_formalization(dep["formalization_id"])])[0]["check_current"]
    assert store.usage_stats()["global"]["session_count"] >= 1
    path.write_text(path.read_text() + "\n-- externally changed\n")
    current = formalizations.decorate([store.get_formalization(dep["formalization_id"])])[0]
    assert not current["check_current"]
    assert current["validity_status"] not in {"proved", "defined"}
    assert store.usage_stats()["global"]["session_count"] >= 1


@pytest.mark.parametrize("single", [False, True])
def test_dependency_inventory_reads_never_compile_or_wait_for_environment(project, monkeypatch, single):
    from app.routes import project_library as routes
    from lea.check_evidence import coordinator
    p, repo = project
    run = active_run(p)
    allocation = deps.ensure(p, spec(), run_id=run["id"], mode="inline")
    dep = allocation["dependency"]
    path = repo / dep["path"]
    path.parent.mkdir(parents=True)
    path.write_text(allocation["skeleton"].replace("sorry", "trivial"))
    install_inspection(monkeypatch, dep)
    assert deps.publish_owned(run["id"], dep["path"])["ready"]

    def unexpected(*args, **kwargs):
        pytest.fail("Inventory reads must not refresh or compile project modules")
    monkeypatch.setattr(library, "refresh", unexpected)
    monkeypatch.setattr(library, "validate_module", unexpected)
    def read():
        return routes.get_dependency.worker(p["slug"], dep["id"]) if single else routes.list_dependencies.worker(p["slug"])["dependencies"][0]

    # A running proof may hold the compiler environment exclusively. Metadata
    # must still render, using filesystem freshness of the recorded evidence.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with coordinator(repo).lease(exclusive=True):
            assert pool.submit(read).result(timeout=2)["ready"]
    path.write_text(path.read_text() + "\n-- external edit\n")
    current = read()
    assert not current["ready"] and current["state"] == "stale"
    with db.connect() as conn:
        assert conn.execute("select state from project_dependencies where id=?", (dep["id"],)).fetchone()[0] == "ready"
    path.unlink()
    assert not read()["ready"]


@pytest.mark.parametrize("final_reason,invalid", [("completed", None), ("completed", "signature"),
    ("completed", "audit"), ("completed", "axioms"), ("completed", "contents"), ("interrupted", None)])
def test_deferred_worker_certifies_final_segment_before_done(project, monkeypatch, tmp_path, final_reason, invalid):
    from app import bridge, projects
    from lea.interface import FileChanged, Finished, TurnStarted
    from lea.providers import Usage
    from queue import Queue
    p, repo = project
    parent = active_run(p)
    allocation = deps.ensure(p, spec(), run_id=parent["id"])
    dep = allocation["dependency"]
    bundle = store.create_run_bundle(message="Prove shared fact", session_id=None, project_id=p["id"],
        session_origin="overleaf", session_origin_url=None, model="test", provider=None,
        max_turns=10, autonomous=True, focus_formalization_id=dep["formalization_id"],
        purpose="project_dependency", dependency_id=dep["id"], initiating_run_id=parent["id"])
    worker = bundle["run"]
    install_inspection(monkeypatch, dep)
    monkeypatch.setattr(projects, "project_repo_dir", lambda *_: repo)
    queue = Queue()
    context = bridge.RunnerContext(session_id=worker["session_id"], run_id=worker["id"],
        task="Prove shared fact", config=LeaConfig(model="test", max_turns=10, lea_root=tmp_path),
        events=queue, autonomous=True, purpose="project_dependency")
    def provider(*args, **kwargs):
        path = repo / dep["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(allocation["skeleton"].replace("sorry", "trivial"))
        yield TurnStarted(1)
        yield FileChanged(str(path))
        yield CheckResult(str(path), "ok")
        # Even a consumer's library search cannot publish intermediate work.
        assert not deps.get(dep["id"])["ready"]
        assert not any(d["reusable"] for d in library.search(p)["declarations"])
        assert deps.get(dep["id"])["owner_run_id"] == worker["id"]
        path.write_text(path.read_text() + "\n-- import cleanup\n")
        yield FileChanged(str(path))
        yield CheckResult(str(path), "ok")
        assert not deps.get(dep["id"])["ready"]
        if invalid == "signature":
            install_inspection(monkeypatch, dep, wrong=True)
        if invalid == "audit":
            monkeypatch.setattr(library, "verify", lambda *_: VerifyResult("unavailable"))
        if invalid == "axioms":
            expected = dict(dep["specification"]["expected"], full_name=dep["declaration_name"],
                user_name=dep["declaration_name"], visibility="public", axioms=["badAxiom"])
            monkeypatch.setattr(library, "inspect_module", lambda *_: [expected])
        if invalid == "contents":
            path.write_text(path.read_text() + "\n-- unchecked change\n")
        yield Finished(final_reason, "Shared work settled.", 1, worker["session_id"], "test", Usage(), 0, {}, result_kind="proved")
    monkeypatch.setattr(bridge, "run_events", provider)
    bridge.run_lea(context)
    events = list(queue.queue)
    publication = [i for i, e in enumerate(events) if e["type"] == "project_dependency" and e["payload"].get("outcome") == "published"]
    ready = final_reason == "completed" and invalid is None
    assert deps.get(dep["id"])["ready"] is ready
    assert len(publication) == int(ready)
    assert events[-1]["type"] == "done"
    if ready:
        assert publication[0] < len(events) - 1
        assert deps.ensure(p, spec(), requester_id=target(p, "B")["id"])["outcome"] == "existing"
    assert len(deps.get(dep["id"])["attempts"]) == 1
    assert deps.get(dep["id"])["attempts"][0]["initiating_run_id"] == parent["id"]


def test_admission_preserves_initiator_across_worker_continuation(project):
    p, _ = project
    parent = active_run(p)
    dep = deps.ensure(p, spec(), run_id=parent["id"])["dependency"]
    with db.write() as conn:
        with pytest.raises(ValueError, match="initiating run"):
            deps.admit_worker(conn, dep["id"], p["id"], "worker", initiating_run_id="missing")
        deps.admit_worker(conn, dep["id"], p["id"], "worker", initiating_run_id=parent["id"])
        conn.execute("update project_dependencies set state='waiting' where id=?", (dep["id"],))
        deps.admit_worker(conn, dep["id"], p["id"], "continued", "worker")
        with pytest.raises(ValueError, match="already owned"):
            deps.admit_worker(conn, dep["id"], p["id"], "competing", initiating_run_id=parent["id"])
    attempt = deps.get(dep["id"])["attempts"][0]
    assert attempt["run_id"] == "continued"
    assert attempt["initiating_run_id"] == parent["id"]


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lean runtime unavailable")
def test_real_lean_inspection_alpha_binders_and_definition_body(tmp_path):
    (tmp_path / "lakefile.lean").write_text('import Lake\nopen Lake DSL\npackage inspection\n')
    toolchain = Path(__file__).resolve().parents[2] / "prover/workspace/lean-toolchain"
    (tmp_path / "lean-toolchain").write_text(toolchain.read_text())
    prefix = "import Lean\nnamespace Lea.Test\nvariable {α : Type}\ntheorem πρώτη (x : α) : x = x := rfl\ndef entropy : Nat := 1\nend Lea.Test"
    one = library.inspect_source(tmp_path, prefix, '#lea_inspect_decl "Lea.Test.πρώτη"')
    two = library.inspect_source(tmp_path, prefix.replace("(x : α) : x = x", "(y : α) : y = y"), '#lea_inspect_decl "Lea.Test.πρώτη"')
    assert one["type_expr"] == two["type_expr"]
    assert "α" in one["type"] and one["kind"] == "theorem"
    entropy = library.inspect_source(tmp_path, prefix, '#lea_inspect_decl "Lea.Test.entropy"')
    other = library.inspect_source(tmp_path, prefix.replace(":= 1", ":= 2"), '#lea_inspect_decl "Lea.Test.entropy"')
    assert entropy["type_expr"] == other["type_expr"] and entropy["body_expr"] != other["body_expr"]


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lean runtime unavailable")
def test_real_module_public_private_types_and_two_consumers(project, monkeypatch):
    p, repo = project
    root = repo.parents[2]
    (root / "lakefile.lean").write_text('import Lake\nopen Lake DSL\npackage librarytest\nlean_lib Lea where\n  srcDir := "proofs"\n')
    toolchain = Path(__file__).resolve().parents[2] / "prover/workspace/lean-toolchain"
    (root / "lean-toolchain").write_text(toolchain.read_text())
    monkeypatch.setattr(library, "inspect_specification", REAL_INSPECT_SPEC)
    # Real module builds and compiler inspection, deterministic audit boundary.
    monkeypatch.setattr(library, "verify", lambda *_: VerifyResult("ok"))
    a, b = active_run(p, "A"), active_run(p, "B")
    reservation = deps.ensure(p, spec(": True"), run_id=a["id"], mode="inline")
    dep = reservation["dependency"]
    path = repo / dep["path"]
    path.parent.mkdir(parents=True)
    path.write_text(reservation["skeleton"].replace("by\n  sorry", "by\n  exact True.intro") +
        f"\nnamespace {p['namespace']}.Nested\nprivate theorem κ {{α : Type}} (x : α) : x = x := rfl\n"
        "theorem η {α : Type} (x : α) : x = x := rfl\n"
        "def BPSDegreeReductionClaim : Prop := False\n"
        "theorem conditional (h : BPSDegreeReductionClaim) : BPSDegreeReductionClaim := h\nend " + p["namespace"] + ".Nested\n")
    published = deps.publish_owned(a["id"], dep["path"])
    assert published["ready"]
    records = library.search(p, "Nested", validate=False)["declarations"]
    private = next(d for d in records if d["user_name"].endswith(".κ"))
    assert private["visibility"] == "private" and private["full_name"].startswith("_private.")
    assert not private["reusable"] and private["outstanding_obligations"]
    public = next(d for d in records if d["user_name"].endswith(".η"))
    assert public["reusable"] and "α" in public["type"]
    conditional = next(d for d in records if d["user_name"].endswith(".conditional"))
    assert conditional["outstanding_obligations"]
    obligation = spec(f": {p['namespace']}.Nested.BPSDegreeReductionClaim", imports=[dep["module_name"]])
    expected = REAL_INSPECT_SPEC(p, obligation)
    assert library.semantic_key(conditional) != library.semantic_key(expected)
    claim = next(d for d in records if d["user_name"].endswith(".BPSDegreeReductionClaim"))
    assert claim["kind"] == "definition" and library.semantic_key(claim) != library.semantic_key(expected)
    reused = deps.ensure(p, spec(": True"), run_id=b["id"], mode="inline")
    assert reused["outcome"] == "existing" and reused["dependency_id"] == dep["id"]
    assert len(reused["dependency"]["attempts"]) == 1
    assert len(reused["dependency"]["requesting_targets"]) == 2
    # Current verification is cached; rereading does not invoke another audit.
    monkeypatch.setattr(library, "verify", lambda *_: pytest.fail("duplicate audit"))
    library.validate_module(p, dep["path"])


def test_dependency_continuation_retains_original_allowances(project, monkeypatch):
    from app import recovery, bridge
    p, repo = project
    run = active_run(p)
    reservation = deps.ensure(p, spec(), run_id=run["id"], mode="deferred")
    dependency = reservation["dependency"]
    cfg = LeaConfig(model="test", max_turns=50, max_cost_usd=20, lea_root=repo, max_spend_usd=None)
    monkeypatch.setattr(runs, "load_config", lambda: cfg)
    monkeypatch.setattr(bridge, "enqueue_run", lambda *_: None)
    monkeypatch.setattr(bridge, "validate_continuation_runtime", lambda *_: None)
    # This is a worker checkpoint: its source is pinned independently of LaTeX.
    with db.write() as conn:
        deps.admit_worker(conn, dependency["id"], p["id"], run["id"])
        conn.execute("""update runs set purpose='project_dependency',dependency_id=?,waiting_dependencies_json=?,
            automation_budget_json=? where id=?""", (dependency["id"], '["verified-child"]',
            json.dumps(dict(max_turns=10, max_cost_usd=5, cost_used=2, remaining_seconds=70)), run["id"]))
        conn.execute("update project_dependencies set state='waiting' where id=?", (dependency["id"],))
    store.update_run(run["id"], "cancelled", recoverable=True, stop_reason="dependency_wait")
    monkeypatch.setattr(recovery, "continuation_checkpoint", lambda *_: ({"turn": 3}, {}, 1))
    original_get = deps.get
    monkeypatch.setattr(deps, "get", lambda key, **kw: {"ready": True, "result": {"module": "Lea.Child"}}
                        if key == "verified-child" else original_get(key, **kw))
    from app.queries import core
    monkeypatch.setattr(core,'fact',lambda scope,kind,key: {'ready':True,'result':{'module':'Lea.Child'}}
        if kind=='dependency' and key=='verified-child' else None)
    captured = {}
    def bundle(**arguments):
        captured.update(arguments)
        return {"created": False, "run": {"id": "continuation", "resumes_run_id": run["id"], "resume_checkpoint_id": 1}}
    monkeypatch.setattr(store, "create_run_bundle", bundle)
    monkeypatch.setattr(runs, "_continuation_response", lambda value: value)
    old = store.get_run(run["id"])
    runs._continue_dependency(old, runs.ContinueRequest(idempotency_key="continuation", dependency_resume=True), "hash")
    assert captured["max_turns"] == 10
    assert captured["automation_budget"]["cost_used"] == 2
    assert captured["automation_budget"]["remaining_seconds"] == 70
    assert captured["resume_checkpoint_id"] == 1 and captured["source_bundle"] is None
    assert "Lea.Child" in captured["message"]


def test_readiness_uses_actual_canonical_imports_and_explicit_waits(project, monkeypatch):
    p, repo = project
    run = active_run(p)
    imported = deps.ensure(p, spec(name="Imported"), requester_id=run["focus_formalization_id"])
    unrelated = deps.ensure(p, spec(name="Unrelated", signature=": False"), requester_id=run["focus_formalization_id"])
    # Registry relationships alone do not force a wait on an unused obligation.
    path = repo / "Target.lean"
    path.write_text(f"-- import {unrelated['module']}\nimport {imported['module']}\ntheorem target : True := by sorry\n")
    real_get = deps.get
    prior_attempts = real_get(imported["dependency_id"], reconcile=False)["attempts"]
    monkeypatch.setattr(deps, "get", lambda id, **kw: {**real_get(id, reconcile=False), "ready": False})
    result = deps.readiness(run["id"], str(path))
    assert result["dependency_ids"] == [imported["dependency_id"]]
    assert deps.get(imported["dependency_id"], reconcile=False)["attempts"] == prior_attempts
    with db.connect() as conn:
        assert json.loads(conn.execute("select waiting_dependencies_json from runs where id=?", (run["id"],)).fetchone()[0]) == result["dependency_ids"]
    path.write_text("theorem target : True := by sorry\n")
    assert deps.readiness(run["id"], str(path))["dependency_ids"] == result["dependency_ids"]  # explicit durable wait
    monkeypatch.setattr(deps, "get", lambda id, **kw: {**real_get(id, reconcile=False), "ready": True})
    assert deps.readiness(run["id"], str(path))["dependency_ids"] == []


def test_readiness_never_retries_failed_dependency(project, monkeypatch):
    p, repo = project
    run = active_run(p)
    registered = deps.ensure(p, spec(), requester_id=run["focus_formalization_id"])
    dep_id = registered["dependency_id"]
    with db.write() as conn:
        conn.execute("update project_dependencies set state='failed' where id=?", (dep_id,))
    path = repo / "Target.lean"
    path.write_text(f"import {registered['module']}\ntheorem target : True := by sorry\n")
    monkeypatch.setattr(library, "refresh", lambda _: [])
    prior_attempts = deps.get(dep_id, reconcile=False)["attempts"]
    first = deps.readiness(run["id"], str(path))
    assert first["dependency_ids"] == [dep_id]
    assert deps.readiness(run["id"], str(path)) == first
    assert deps.get(dep_id, reconcile=False)["state"] == "failed"
    assert deps.get(dep_id, reconcile=False)["attempts"] == prior_attempts


# This module mocks compiler behavior; bind those mock verdicts to their inputs.
pytestmark = pytest.mark.usefixtures("certified_unit_inputs")
