"""Unit tests for the blueprint parser, validator and renderer (T1, D28;
PLAN-blueprint-tools §2.1).

Pure functions over markdown text — no repo/DB needed. The contract under test: a
fence-aware line-scan that tolerates sloppy prose, a validator that returns coded
advisory warnings (never raises), and a canonical renderer. The shared parser
oracle is `test_blueprint_fixtures.py`; these cover the remaining corners."""

from app import blueprint


def test_parse_basic_node():
    text = (
        "# Blueprint — Epsilon\n\n"
        "## continuous_sq\n"
        "- kind: lemma\n"
        "- lean: `Lea.Epsilon.continuous_sq`\n"
        "- uses: tendsto_iff_eps, distance_lower\n"
        "\n"
        "The function x ↦ x² is continuous in the ε–δ sense.\n"
    )
    parsed = blueprint.parse(text)
    assert len(parsed["nodes"]) == 1
    node = parsed["nodes"][0]
    assert node["key"] == "continuous_sq"
    assert node["kind"] == "lemma"
    # Backticks around the decl are stripped.
    assert node["lean"] == "Lea.Epsilon.continuous_sq"
    assert node["uses"] == ["tendsto_iff_eps", "distance_lower"]
    assert "continuous in the ε–δ sense" in node["statement"]
    # The `# Blueprint` title is not a node and not in the statement.
    assert "Blueprint — Epsilon" not in node["statement"]


def test_parse_edges_from_uses():
    text = (
        "## a\n- kind: lemma\n- uses: b, c\n\nstatement a\n"
        "## b\n- kind: lemma\n\nstatement b\n"
        "## c\n- kind: definition\n\nstatement c\n"
    )
    parsed = blueprint.parse(text)
    assert [n["key"] for n in parsed["nodes"]] == ["a", "b", "c"]
    assert parsed["edges"] == [{"from": "a", "to": "b"}, {"from": "a", "to": "c"}]


def test_parse_missing_fields_are_none():
    # A planned node: named, no decl yet, no deps.
    parsed = blueprint.parse("## planned\n- kind: theorem\n\nTODO state it.\n")
    node = parsed["nodes"][0]
    assert node["kind"] == "theorem"
    assert node["lean"] is None
    assert node["uses"] == []
    assert parsed["edges"] == []


def test_parse_ignores_fenced_example():
    # A ```-fenced example (as in the seed) must NOT register as a real node.
    text = (
        "# Blueprint — Demo\n\n"
        "Here is the format:\n\n"
        "```\n"
        "## example_node\n"
        "- kind: lemma\n"
        "- uses: somewhere\n"
        "```\n\n"
        "## real_node\n"
        "- kind: lemma\n\n"
        "An actual node.\n"
    )
    parsed = blueprint.parse(text)
    assert [n["key"] for n in parsed["nodes"]] == ["real_node"]
    assert parsed["edges"] == []


def test_parse_tolerates_garbage_lines():
    # Prose, stray dashes, and non-header bullets never break the scan.
    text = (
        "## node\n"
        "- kind: lemma\n"
        "- some random bullet that is not a header\n"
        "Let $f : [0,1] \\to \\mathbb{R}$ be continuous: with a colon, even.\n"
        "- another: not-a-recognised-key\n"
    )
    parsed = blueprint.parse(text)
    assert len(parsed["nodes"]) == 1
    node = parsed["nodes"][0]
    assert node["kind"] == "lemma"
    # `- key:` lines after the header block are statement prose, not headers.
    assert "another: not-a-recognised-key" in node["statement"]
    assert "with a colon, even" in node["statement"]
    assert parsed["warnings"] == []


def test_unknown_header_inside_the_header_block_is_dropped_with_a_warning():
    parsed = blueprint.parse("## node\n- kind: lemma\n- status: PROVED\n- uses: none\n\nA leaf.\n")
    node = parsed["nodes"][0]
    assert node["uses"] == []  # the header block continues past the unknown line
    assert "PROVED" not in node["statement"]
    assert [(w["code"], w["node"]) for w in parsed["warnings"]] == [("unknown_header", "node")]
    assert "status" not in blueprint.render(parsed)


def test_section_without_a_recognised_header_is_prose_not_a_node():
    # The `## Goal` phantom node of the RealAnalysis run (PLAN-blueprint-tools §0).
    parsed = blueprint.parse("# T\n\n## Goal\n\nProve it.\n\n## a\n- kind: lemma\n\nA lemma.\n")
    assert [n["key"] for n in parsed["nodes"]] == ["a"]
    assert [b["type"] for b in parsed["blocks"]] == ["prose", "node"]
    assert parsed["blocks"][0] == {"type": "prose", "heading": "Goal", "body": "Prove it."}
    assert parsed["preamble"] == "# T"


def test_parse_empty_text():
    parsed = blueprint.parse("")
    assert parsed == {"preamble": "", "blocks": [], "nodes": [], "edges": [], "warnings": []}
    assert blueprint.render(parsed) == ""


def test_node_shape_has_type_and_file():
    node = blueprint.parse("## a\n- kind: lemma\n- file: `A.lean`\n\nS.\n")["nodes"][0]
    assert node == {"type": "node", "key": "a", "kind": "lemma", "lean": None,
                    "file": "A.lean", "uses": [], "statement": "S."}


def test_render_is_canonical():
    text = "#  Title \n\n\n## b\n- uses: a\n- kind: THEOREM\n- lean: Lea.X.b\nThe theorem.\n\n\n## a\n\n- kind: lemma\n"
    rendered = blueprint.render(blueprint.parse(text))
    assert rendered == (
        "#  Title \n\n"
        "## b\n- kind: theorem\n- lean: `Lea.X.b`\n- uses: a\n\nThe theorem.\n\n"
        "## a\n- kind: lemma\n"
    )
    assert blueprint.render(blueprint.parse(rendered)) == rendered


def test_render_closes_a_dangling_fence_so_later_sections_stay_live():
    parsed = blueprint.parse("# T\n\n```\nunterminated\n")
    parsed["blocks"].append({"type": "node", "key": "n", "kind": "lemma", "lean": None,
                             "file": None, "uses": [], "statement": "New."})
    rendered = blueprint.render(parsed)
    assert [n["key"] for n in blueprint.parse(rendered)["nodes"]] == ["n"]
    assert blueprint.render(blueprint.parse(rendered)) == rendered


def test_render_keeps_a_headerless_node_a_node():
    # An empty `- lean:` is a recognised header, so `a` is a node with no fields.
    parsed = blueprint.parse("## a\n- lean:\n\nBody.\n")
    assert [n["key"] for n in parsed["nodes"]] == ["a"]
    again = blueprint.parse(blueprint.render(parsed))
    assert [n["key"] for n in again["nodes"]] == ["a"]


def test_validate_clean_blueprint_has_no_warnings():
    text = (
        "## a\n- kind: lemma\n- uses: b\n\nstmt a\n"
        "## b\n- kind: definition\n\nstmt b\n"
    )
    assert blueprint.validate(text) == []


def test_validate_flags_dangling_edge():
    warnings = blueprint.validate("## a\n- kind: lemma\n- uses: ghost\n\nstmt\n")
    assert any(w["code"] == "dangling_dependency" and "ghost" in w["message"] and w["node"] == "a"
               for w in warnings)


def test_validate_flags_missing_and_unknown_kind():
    # `## a` needs a recognised header to be a node at all; without one it is prose.
    warnings = blueprint.validate("## a\n- uses: b\n\nno kind here\n## b\n- kind: corollary\n\nbad kind\n")
    assert {(w["code"], w["node"]) for w in warnings} == {("missing_kind", "a"), ("unknown_kind", "b")}
    messages = " ".join(w["message"] for w in warnings)
    assert "no `kind:`" in messages
    assert "unrecognised kind `corollary`" in messages
    assert blueprint.validate("## a\n\nno kind here\n") == []  # prose section, no node


def test_validate_flags_duplicate_keys():
    warnings = blueprint.validate("## a\n- kind: lemma\n\none\n## a\n- kind: lemma\n\ntwo\n")
    assert any(w["code"] == "duplicate_key" and "duplicate node key `a`" in w["message"] for w in warnings)


def test_validate_flags_cycles_but_not_self_loops_as_cycles():
    text = ("## a\n- kind: lemma\n- uses: b\n\n## b\n- kind: lemma\n- uses: c\n\n"
            "## c\n- kind: lemma\n- uses: a\n\n## d\n- kind: lemma\n- uses: d, a\n")
    pairs = {(w["code"], w["node"]) for w in blueprint.validate(text)}
    assert pairs == {("cycle", "a"), ("cycle", "b"), ("cycle", "c"), ("self_dependency", "d")}


def test_validate_flags_invalid_and_overlong_keys():
    long_key = "k" * 65
    text = f"## Bad Key\n- kind: lemma\n\n## {long_key}\n- kind: lemma\n\n## ok_key'\n- kind: lemma\n"
    pairs = {(w["code"], w["node"]) for w in blueprint.validate(text)}
    assert pairs == {("invalid_key", "Bad Key"), ("invalid_key", long_key)}


def test_uses_sentinel_is_a_real_dependency_when_a_node_has_that_key():
    parsed = blueprint.parse("## none\n- kind: lemma\n\n## a\n- kind: lemma\n- uses: none\n")
    assert parsed["nodes"][1]["uses"] == ["none"]
    assert parsed["warnings"] == []
