"""v2.5 H1–H4/H7: importing and using a real, multi-file skill.

The bug this closes is user-visible and was found by importing an actual skill repo:
`cameronfreer/lean4-skills` imported as its README — 8.9 KB of documentation ABOUT the
repo — while the real 29 KB SKILL.md and 41 reference files were ignored, because the
lookup only checked the repo's direct children.
"""

from pathlib import Path

import pytest

from app import db, ghimport, skills_catalog, store
from app.routes import skills as skills_route
from app.routes.skills import SkillCreate


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _fake_repo(root: Path) -> Path:
    """A repo shaped like the real one: a root README, and the skill several levels down."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Lean 4 Skills\n\nDocs ABOUT the repo.\n")
    skill = root / "plugins" / "lean4" / "skills" / "lean4"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: lean4\n"
        "description: Use when editing .lean files or searching Mathlib.\n"
        "license: MIT\n"
        "---\n\n"
        "# Lean 4 Theorem Proving\n\nSee [tactics](references/tactics.md).\n"
    )
    (skill / "references" / "tactics.md").write_text("# Tactics\n\nring, linarith…\n")
    (skill / "references" / "errors.md").write_text("# Errors\n\ntype mismatch…\n")
    return root


def test_a_nested_skill_wins_over_a_root_readme(tmp_path):
    """THE bug. A repo with a root README used to lose its real skill, silently."""
    repo = _fake_repo(tmp_path / "repo")
    found = ghimport._locate_md(repo, ghimport.ImportTarget(clone_url="", repo_name="r"))
    assert found.name == "SKILL.md"
    assert "plugins/lean4/skills/lean4" in str(found)


def test_frontmatter_is_parsed_not_injected():
    meta, body = ghimport.split_frontmatter(
        "---\nname: lean4\ndescription: Use when editing .lean files.\n---\n\n# Heading\n")
    assert meta["name"] == "lean4"
    assert meta["description"] == "Use when editing .lean files."
    assert body.startswith("# Heading")
    assert "---" not in body            # never lands in the prompt as literal YAML


def test_malformed_frontmatter_is_treated_as_text():
    raw = "---\nthis: is: not: yaml:\n---\nbody\n"
    meta, body = ghimport.split_frontmatter(raw)
    assert meta == {} and body == raw   # an import must not fail on a bad header


def test_resources_are_collected_from_the_skill_dir(tmp_path):
    repo = _fake_repo(tmp_path / "repo")
    skill_dir = repo / "plugins" / "lean4" / "skills" / "lean4"
    files = dict(ghimport._collect_resources(skill_dir))
    assert set(files) == {"references/tactics.md", "references/errors.md"}
    assert "ring, linarith" in files["references/tactics.md"]


def test_multifile_skill_materializes_as_a_tree(tmp_path, monkeypatch):
    """The tree shape is load-bearing: SKILL.md says `see references/x.md`, which only
    means anything if the layout survives materialization."""
    _setup(tmp_path, monkeypatch)
    skill = skills_route.create_skill(SkillCreate(
        name="lean4", body="# Lean 4\n\nSee [tactics](references/tactics.md).", is_global=True))
    store.set_skill_files(skill["id"], [("references/tactics.md", "# Tactics\n")])

    paths, tempdir = skills_catalog.materialize_run_skills(None, None)
    try:
        assert len(paths) == 1
        entry = Path(paths[0])
        assert entry.name == "SKILL.md"
        assert (entry.parent / "references" / "tactics.md").read_text() == "# Tactics\n"
    finally:
        skills_catalog.cleanup(tempdir)


def test_a_traversing_path_cannot_escape_the_skill_root(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    skill = skills_route.create_skill(SkillCreate(name="evil", body="x", is_global=True))
    store.set_skill_files(skill["id"], [("../../escaped.md", "nope")])
    paths, tempdir = skills_catalog.materialize_run_skills(None, None)
    try:
        assert not (Path(tempdir).parent / "escaped.md").exists()
    finally:
        skills_catalog.cleanup(tempdir)


def test_single_file_skills_are_unchanged(tmp_path, monkeypatch):
    """The common path must stay byte-identical — a flat `<slug>.md`, injected whole."""
    _setup(tmp_path, monkeypatch)
    skills_route.create_skill(SkillCreate(name="Ring tactics", body="# use ring", is_global=True))
    paths, tempdir = skills_catalog.materialize_run_skills(None, None)
    try:
        assert Path(paths[0]).name == "ring-tactics.md"
    finally:
        skills_catalog.cleanup(tempdir)


def test_the_prover_advertises_rather_than_injects_a_multifile_skill(tmp_path, monkeypatch):
    """H3 end to end: a single-file skill is injected whole; a multi-file one contributes
    its entry point plus a pointer to its references, never their contents."""
    _setup(tmp_path, monkeypatch)
    from lea.skills import load_skills

    solo = skills_route.create_skill(SkillCreate(name="Solo", body="SOLO BODY", is_global=True))
    multi = skills_route.create_skill(SkillCreate(name="Multi", body="ENTRY POINT", is_global=True))
    store.set_skill_files(multi["id"], [("references/deep.md", "DEEP CONTENT " * 500)])

    paths, tempdir = skills_catalog.materialize_run_skills(None, None)
    try:
        prompt = load_skills(paths)
        assert "SOLO BODY" in prompt            # single-file: injected
        assert "ENTRY POINT" in prompt          # multi-file: entry point injected
        assert "DEEP CONTENT" not in prompt     # ...but NOT its references
        assert "references/deep.md" in prompt   # ...which are advertised by path
        assert "read_file" in prompt
    finally:
        skills_catalog.cleanup(tempdir)


# --- H5/H6: bundled roles and foreign tool names -------------------------------

def test_foreign_tool_names_translate():
    from app.tool_names import translate
    lea, unmapped = translate([
        "Read", "Grep", "Glob", "Edit", "Bash",
        "mcp__lean-lsp__lean_goal", "mcp__lean-lsp__lean_loogle", "Frobnicate",
    ])
    assert lea == ["read_file", "edit_file", "bash", "lean_goal", "lean_loogle"]
    # Grep/Glob have no Lea equivalent and are incidental — reporting them would bury
    # the one name a human should actually look at.
    assert unmapped == ["Frobnicate"]


def test_an_unmapped_name_is_never_guessed():
    """Guessing that `Glob` means `bash` would hand a read-only role a shell."""
    from app.tool_names import translate
    lea, _ = translate(["Glob", "WebSearch"])
    assert lea == []


# --- H9: triggers ---------------------------------------------------------------

def test_a_skill_without_triggers_is_always_on(tmp_path, monkeypatch):
    """Every existing skill has none, so this changes nothing until asked for."""
    _setup(tmp_path, monkeypatch)
    s = skills_route.create_skill(SkillCreate(name="Always", body="x", is_global=True))
    assert store.matches_triggers(store.get_skill(s["id"]), "anything at all")


def test_triggers_gate_a_skill_by_whole_word(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    s = store.create_skill("Measure", "body", triggers=["measure", "integral"])
    store.set_skill_assignment(s["id"], is_global=True)
    row = store.get_skill(s["id"])

    assert store.matches_triggers(row, "Prove this measure is finite")
    assert store.matches_triggers(row, "compute the INTEGRAL")
    assert not store.matches_triggers(row, "Prove x + y = y + x")
    # Substring matching would fire "ring" on "bringing" — a keyword list has to be
    # something a mathematician can reason about.
    assert not store.matches_triggers(row, "measurements were taken")


def test_a_triggered_skill_is_filtered_out_of_a_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    always = store.create_skill("Always", "A")
    store.set_skill_assignment(always["id"], is_global=True)
    gated = store.create_skill("Measure", "M", triggers=["measure"])
    store.set_skill_assignment(gated["id"], is_global=True)

    off = [s["slug"] for s in store.skills_for_run(None, None, "prove 1+1=2")]
    on = [s["slug"] for s in store.skills_for_run(None, None, "prove this measure is finite")]
    assert off == ["always"]
    assert sorted(on) == ["always", "measure"]


def test_an_explicit_session_opt_in_beats_its_triggers(tmp_path, monkeypatch):
    """The user asking for a skill is a stronger signal than any keyword list."""
    _setup(tmp_path, monkeypatch)
    session = store.create_session("s", project_id=None)["id"]
    gated = store.create_skill("Measure", "M", triggers=["measure"])
    store.set_skill_assignment(gated["id"], is_global=True)

    assert store.skills_for_run(None, session, "prove 1+1=2") == []
    store.set_session_skill_mcp(session, "skill", gated["id"], "add")
    assert [s["slug"] for s in store.skills_for_run(None, session, "prove 1+1=2")] == ["measure"]
