"""Confidence follows source semantics and coverage, independently of compilation."""
import pytest

from lea.status_reporting import attention, merge_assessment, validate_assessment, validate_payload


def correspondence(**changes):
    return dict(
        scope_summary="The complex right-hand sum, with the coefficient identity still omitted.",
        omitted_source_parts=["Equality with the coefficient series involving M_a(n)."],
        context_status="reconciled",
        context_summary="The later convention q = exp(2*pi*i*tau), tau in the upper half-plane, "
                        "supports the analytic interpretation; the coefficient identity needs a proof.",
        context_evidence=[dict(side="latex", path="main.tex", start_line=326,
                              excerpt="q = exp(2*pi*i*tau), tau in the upper half-plane")],
    ) | changes


def ambiguity(**changes):
    return dict(key="q-domain", severity="warning", category="formalization_choice",
                semantic_impact="unresolved", title="The domain needs reconciliation",
                detail="Formal series and totalized complex tsum are not interchangeable by compilation.",
                lean_resolution="applied", source_resolution="open",
                correction="Use a complex tsum.") | changes


def update(**changes):
    return dict(kind="final", confidence="medium", scope="partial_artifact",
                summary="The right-hand sum is encoded; the source identity is not established.",
                confidence_reason="The chosen domain still needs semantic justification.",
                correspondence=correspondence(), finding_updates=[ambiguity()], matches=[], caveats=[],
                remaining_obligations=["Establish the coefficient identity."], limitations=[],
                recommended_next_action=dict(kind="inspect_result", detail="Inspect the partial encoding.")) | changes


def merge(previous=None, **changes):
    return merge_assessment(previous, validate_payload(update(**changes)), "source")


def test_ua_cannot_promote_a_conditional_complex_encoding_to_high_confidence():
    assessed = merge()
    assert assessed["scope"] == "partial_artifact"
    assert attention(assessed) == "source_issue"
    with pytest.raises(ValueError, match="unresolved semantic ambiguity"):
        merge(confidence="high")


@pytest.mark.parametrize("scope", ["definition", "statement_only", "statement_and_proof"])
def test_omitted_source_identity_cannot_claim_complete_scope(scope):
    with pytest.raises(ValueError, match="Omitted source parts require partial_artifact"):
        merge(scope=scope)


def test_a_lean_choice_and_an_omitted_finding_do_not_clear_semantic_uncertainty():
    assessed = merge()
    for changes in ({"finding_updates": []}, {"finding_updates": [ambiguity()]}):
        with pytest.raises(ValueError, match="unresolved semantic ambiguity"):
            merge(assessed, confidence="high", **changes)
    # An update cannot erase the retained impact merely by omitting that field.
    without_impact = ambiguity()
    del without_impact["semantic_impact"]
    assert merge(assessed, finding_updates=[without_impact])["findings"][0]["semantic_impact"] == "unresolved"


def test_semantics_can_be_reconciled_while_the_latex_correction_remains_open():
    assessed = merge()
    with pytest.raises(ValueError, match="clearing semantic uncertainty requires resolution_explanation"):
        merge(assessed, confidence="high", finding_updates=[ambiguity(semantic_impact="none")])
    reconciled = merge(assessed, confidence="high", finding_updates=[ambiguity(
        semantic_impact="none",
        resolution_explanation="The later upper-half-plane convention fixes the analytic interpretation; "
                               "the source should state the domain locally as well.",
    )])
    assert reconciled["confidence"] == "high"
    assert reconciled["scope"] == "partial_artifact"
    assert reconciled["correspondence"]["omitted_source_parts"]
    assert attention(reconciled) == "source_issue"


def test_resolving_semantics_requires_fresh_context_in_a_progress_update():
    assessed = merge()
    progress = dict(kind="progress", confidence="high", scope="partial_artifact",
                    summary="Context found.", confidence_reason="The domain convention was located.",
                    finding_updates=[ambiguity(semantic_impact="none", resolution_explanation="Located the convention.")])
    with pytest.raises(ValueError, match="resolving semantic uncertainty requires refreshed correspondence"):
        merge_assessment(assessed, validate_payload(progress), "source")


def test_legacy_semantic_findings_are_conservative_until_explicitly_reassessed():
    legacy = ambiguity()
    del legacy["semantic_impact"]
    with pytest.raises(ValueError, match="unresolved semantic ambiguity"):
        merge(confidence="high", finding_updates=[legacy])


def test_applied_lean_choices_cannot_hide_an_explicit_semantic_uncertainty():
    with pytest.raises(ValueError, match="unresolved semantic ambiguity"):
        merge(confidence="high", finding_updates=[ambiguity(source_resolution="not_applicable")])


def test_high_confidence_requires_reconciled_context_and_source_evidence():
    with pytest.raises(ValueError, match="reconciled document context"):
        merge(confidence="high", finding_updates=[], correspondence=correspondence(context_status="incomplete"))
    for references in ([], [dict(side="lean", path="Ua.lean", start_line=10)]):
        with pytest.raises(ValueError, match="cited LaTeX evidence"):
            merge(confidence="high", finding_updates=[], correspondence=correspondence(context_evidence=references))
    with pytest.raises(ValueError, match="reconciled document context"):
        merge_assessment(None, validate_payload(dict(kind="progress", confidence="high", scope="source_only",
                         summary="Compiles.", confidence_reason="Lean checked.")), "source")


def test_definition_scope_does_not_claim_a_theorem_proof_or_unfinished_relations():
    assessed = merge(scope="definition", finding_updates=[], remaining_obligations=[],
                     correspondence=correspondence(omitted_source_parts=[], scope_summary="The complete definition."))
    validate_assessment(assessed, "definition")
    with pytest.raises(ValueError, match="definition target"):
        validate_assessment(assessed, "theorem")
    with pytest.raises(ValueError, match="do not claim a theorem proof"):
        validate_assessment({**assessed, "scope": "statement_and_proof"}, "definition")
    with pytest.raises(ValueError, match="Unfinished target obligations"):
        merge(scope="definition", correspondence=correspondence(omitted_source_parts=[]))


def test_final_updates_refresh_correspondence_and_changed_source_or_scope_cannot_reuse_it():
    final = update()
    del final["correspondence"]
    with pytest.raises(ValueError, match="Final update must refresh"):
        validate_payload(final)
    assessed = merge(finding_updates=[])
    progress = dict(kind="progress", confidence="medium", scope="partial_artifact",
                    summary="Continuing.", confidence_reason="Same interpretation.")
    assert merge_assessment(assessed, validate_payload(progress), "source")["correspondence"] == assessed["correspondence"]
    with pytest.raises(ValueError, match="changed source or document context requires refreshed correspondence"):
        merge_assessment(assessed, validate_payload(progress), "new-source")
    with pytest.raises(ValueError, match="changed assessment scope requires refreshed correspondence"):
        merge_assessment(assessed, validate_payload(progress | {"scope": "source_only"}), "source")


def test_changed_surrounding_document_cannot_reuse_context_with_an_unchanged_target():
    assessed = merge_assessment(None, validate_payload(update()), "same-target", source_bundle_hash="old-document")
    progress = dict(kind="progress", confidence="medium", scope="partial_artifact",
                    summary="Continuing.", confidence_reason="The target text is unchanged.")
    with pytest.raises(ValueError, match="document context requires refreshed correspondence"):
        merge_assessment(assessed, validate_payload(progress), "same-target", source_bundle_hash="new-document")
    refreshed = merge_assessment(assessed, validate_payload(update()), "same-target", source_bundle_hash="new-document")
    assert refreshed["correspondence_context_hash"] == "new-document"


def test_a_repaired_proof_gap_does_not_automatically_lower_confidence():
    repaired = ambiguity(category="source_gap", semantic_impact="none", title="Missing boundary case",
                         detail="The boundary case is handled faithfully in Lean.")
    assessed = merge(confidence="high", scope="statement_and_proof", finding_updates=[repaired],
                     remaining_obligations=[], correspondence=correspondence(omitted_source_parts=[]))
    assert assessed["confidence"] == "high"
    assert attention(assessed) == "source_issue"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
