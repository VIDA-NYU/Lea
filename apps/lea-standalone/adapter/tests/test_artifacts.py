from app.artifacts import (
    classify_lean_artifact,
    declaration_contains_sorry,
    scan_lean_declarations,
)


def test_classifies_pure_definition_artifact():
    code = """
import Mathlib

def Subadditive (a : Nat -> Int) : Prop := True
"""
    assert classify_lean_artifact(code) == "definition"


def test_classifies_theorem_artifact():
    code = """
import Mathlib

theorem t : True := by
  trivial
"""
    assert classify_lean_artifact(code) == "proof"


def test_classifies_mixed_artifact_as_not_pure_definition():
    code = """
def helper : Nat := 0

lemma helper_ok : helper = 0 := by
  rfl
"""
    assert classify_lean_artifact(code) == "mixed"


def test_ignores_declaration_words_in_comments_and_strings():
    code = '''
-- theorem fake : True := by trivial
def label : String := "lemma also fake"
'''
    assert classify_lean_artifact(code) == "definition"


def test_extract_declaration_name_basic_kinds():
    from app.artifacts import extract_declaration_name
    assert extract_declaration_name("theorem foo_bar : True := trivial") == "foo_bar"
    assert extract_declaration_name("def Subadditive (a : Nat) : Prop := True") == "Subadditive"
    assert extract_declaration_name("private lemma aux' : True := trivial") == "aux'"
    assert extract_declaration_name("noncomputable instance instFoo : Inhabited Nat := ⟨0⟩") == "instFoo"


def test_extract_declaration_name_skips_comments_and_none_cases():
    from app.artifacts import extract_declaration_name
    assert extract_declaration_name("-- theorem commented_out : True\ntheorem real_one : True := trivial") == "real_one"
    assert extract_declaration_name("/- theorem block_comment : True -/\nlemma survivor : True := trivial") == "survivor"
    assert extract_declaration_name("import Mathlib\nopen Nat\n") is None
    assert extract_declaration_name("") is None
    assert extract_declaration_name(None) is None


def test_scanner_indexes_nested_namespaces_and_scopes_sorry_to_one_declaration():
    code = """
namespace Lea.Sample
def helper : Nat := 1
namespace Inner
theorem unfinished : True := by sorry
theorem finished : True := by trivial
end Inner
end Lea.Sample
"""
    declarations = scan_lean_declarations(code)
    assert [row.full_name for row in declarations] == [
        "Lea.Sample.helper",
        "Lea.Sample.Inner.unfinished",
        "Lea.Sample.Inner.finished",
    ]
    assert declaration_contains_sorry(code, "Lea.Sample.Inner.unfinished")
    assert not declaration_contains_sorry(code, "Lea.Sample.Inner.finished")


def test_scanner_keeps_namespace_across_sections_and_ignores_nested_comment_sorry():
    code = """
namespace Lea.Sample
section Inputs
variable (n : Nat)
theorem inside : True := by
  /- outer /- sorry -/ comment -/
  trivial
end Inputs
theorem after_section : True := by trivial
end Lea.Sample
"""
    declarations = scan_lean_declarations(code)
    assert [row.full_name for row in declarations] == [
        "Lea.Sample.inside",
        "Lea.Sample.after_section",
    ]
    assert not declaration_contains_sorry(code, "inside")
