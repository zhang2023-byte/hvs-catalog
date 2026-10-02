"""Synthetic scoring regressions with independently constructed input-revalidation
counterexamples.

Contribution/author diagnostics cover entire definite identity pools independently of
list order. Public score_batch revalidates current nested structures before indexing,
Agent calls, or caches; reject mutated duplicate IDs and other violations without
overwriting or changing caller input. All cases use the full entry point.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from scoring_sample_helpers import load_bundle

from hvs_catalog.evaluation.scoring import (
    AgentResponse,
    MockAgentAdapter,
    gold_input_digest,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.errors import ScoringInputError

S08 = "synthetic-s08"


def _l2_rows(paper):
    return [row for row in paper.pairings if row.layer == "l2"]


def _s08_diagnostic_bundle(
    *,
    dup_contribution="previous_work",
    dup_author="excluded",
    matched_contribution="this_paper",
    matched_author="included",
    gold_contribution="this_paper",
    gold_author="included",
):
    """S08 variant: Gold matches the first prediction's diagnostic fields; parameterize the
    duplicate entry.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].contribution_type = gold_contribution
    gold[0].objects[0].author_judgment = gold_author
    preds[0].objects[0].contribution_type = matched_contribution
    preds[0].objects[0].author_judgment = matched_author
    preds[0].objects[1].contribution_type = dup_contribution
    preds[0].objects[1].author_judgment = dup_author
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    return plan, gold, preds, auxiliary


def _score(plan, gold, preds, auxiliary, **kwargs):
    return score_batch(plan, gold, preds, auxiliary=auxiliary, **kwargs)


# ---------------------------------------------------------------------------
# Diagnose contribution types and author assessments for every prediction in the pool.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_pool_diagnostics_complete_and_order_independent(reverse):
    """One prediction matches Gold and one differs; both list orders locate the same actual
    erroneous entry.
    """
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle()
    if reverse:
        preds = copy.deepcopy(preds)
        preds[0].objects = list(reversed(preds[0].objects))
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    mismatch = [d for d in paper.diagnostics if d.code.endswith("_mismatch")]
    codes = sorted(d.code for d in mismatch)
    assert codes == ["author_judgment_mismatch", "contribution_type_mismatch"]
    for diagnostic in mismatch:
        # Locate the erroneous duplicate prediction, not the first matching entry.
        assert diagnostic.refs == ["A", "A-dup"]
    # Independent diagnostics never affect quality counts.
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False


def test_duplicate_pool_diagnostics_survive_internal_renaming():
    """Renamed internal IDs/labels still identify the same erroneous entry."""
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle()
    preds = copy.deepcopy(preds)
    preds[0].objects[1].label = "Z-dup"
    preds[0].objects[1].quantities[0].values[0].record_id = "z9"
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    mismatch = [d for d in paper.diagnostics if d.code.endswith("_mismatch")]
    assert sorted(d.code for d in mismatch) == [
        "author_judgment_mismatch",
        "contribution_type_mismatch",
    ]
    assert all(d.refs == ["A", "Z-dup"] for d in mismatch)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)


def test_duplicate_pool_identical_fields_no_diagnostics():
    """A duplicate identical to Gold produces no diagnostics."""
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle(
        dup_contribution="this_paper", dup_author="included"
    )
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    assert not [d for d in paper.diagnostics if d.code.endswith("_mismatch")]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)


def test_single_field_or_missing_field_semantics_preserved():
    """One differing field yields only its diagnostic; missing prediction fields (None)
    yield none under established semantics.
    """
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle(dup_contribution="this_paper")
    report = _score(plan, gold, preds, auxiliary)
    mismatch = [d for d in report.papers[0].diagnostics if d.code.endswith("_mismatch")]
    assert [d.code for d in mismatch] == ["author_judgment_mismatch"]

    plan, gold, preds, auxiliary = _s08_diagnostic_bundle(
        dup_contribution=None, dup_author=None
    )
    report = _score(plan, gold, preds, auxiliary)
    mismatch = [d for d in report.papers[0].diagnostics if d.code.endswith("_mismatch")]
    assert mismatch == []


def test_no_duplicate_diagnostics_for_same_field():
    """At most one diagnostic per prediction/field, without duplicate reports for the same
    error.
    """
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle()
    report = _score(plan, gold, preds, auxiliary)
    codes = [d.code for d in report.papers[0].diagnostics]
    assert len(codes) == len(set(codes))


def test_ambiguity_not_diagnosed_as_field_mismatch():
    """Shared-alias ambiguity stays outside definite pools: no field diagnostics, still
    pending.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].identifiers = ["SYN-A", "SYN-SHARED"]
    second_object = copy.deepcopy(gold[0].objects[0])
    second_object.label = "B"
    second_object.identifiers = ["SYN-B", "SYN-SHARED"]
    second_object.quantities[0].values[0].record_id = "g3"
    gold[0].objects.append(second_object)
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    preds[0].objects[0].contribution_type = "this_paper"
    preds[0].objects[1].label = "P"
    preds[0].objects[1].identifiers = ["SYN-SHARED"]
    preds[0].objects[1].contribution_type = "previous_work"
    preds[0].objects[1].author_judgment = "excluded"
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    assert any(d.code == "ambiguous_object_alias" for d in paper.diagnostics)
    # Do not diagnose ambiguous identities or use diagnostic contents to disambiguate.
    assert not [d for d in paper.diagnostics if d.code.endswith("_mismatch")]


# ---------------------------------------------------------------------------
# Revalidate actual current input and reject post-construction mutation.
# ---------------------------------------------------------------------------


def test_post_construction_duplicate_record_id_rejected():
    """Mutate valid S08 so its second record_id becomes p1; reject it."""
    plan, gold, preds, auxiliary = load_bundle(S08)
    preds[0].objects[1].quantities[0].values[0].record_id = "p1"
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_prediction_list_append_duplicate_rejected():
    """Reject duplicate record_id entries appended directly to lists without field
    assignment.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    duplicate = copy.deepcopy(preds[0].objects[1].quantities[0].values[0])
    duplicate.record_id = "p1"
    preds[0].objects[1].quantities[0].values.append(duplicate)
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_prediction_duplicate_object_label_appended_rejected():
    plan, gold, preds, auxiliary = load_bundle(S08)
    clone = copy.deepcopy(preds[0].objects[0])
    preds[0].objects.append(
        clone
    )  # Duplicate paper-local labels violate parent constraints.
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_gold_duplicate_value_appended_rejected_even_with_recomputed_digest():
    """Reject appended duplicate Gold record_ids even when fingerprints are recomputed
    consistently.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    duplicate = copy.deepcopy(gold[0].objects[0].quantities[0].values[0])
    gold[0].objects[0].quantities[0].values.append(duplicate)
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_gold_duplicate_quantity_path_appended_rejected():
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].quantities.append(
        copy.deepcopy(gold[0].objects[0].quantities[0])
    )
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_gold_duplicate_identifiers_rejected():
    plan, gold, preds, auxiliary = load_bundle(S08)
    # List append bypasses field-assignment validation and can violate object-local identifier uniqueness.
    gold[0].objects[0].identifiers.append("SYN-A")
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_valid_duplicate_values_still_score_normally():
    """Normal globally unique duplicate values in S08 (5.0/9.9) still score normally."""
    plan, gold, preds, auxiliary = load_bundle(S08)
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False


def test_rejection_happens_before_agent_or_cache_side_effects(tmp_path: Path):
    """Before rejecting, make no Agent calls and publish no frozen entries."""
    plan, gold, preds, _auxiliary = load_bundle(S08)
    store_directory = tmp_path / "frozen"
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:g1"]
        )
    )
    preds[0].objects[1].quantities[0].values[0].record_id = "p1"
    with pytest.raises(ScoringInputError):
        score_batch(
            plan,
            gold,
            preds,
            auxiliary=protocol_only_config(store_directory=str(store_directory)),
            agent_adapter=mock,
        )
    assert mock.calls == []
    assert not list(store_directory.glob("*")) if store_directory.exists() else True


def test_caller_input_not_rewritten(tmp_path: Path):
    """Successful scoring never rewrites caller input or writes validation back."""
    plan, gold, preds, _auxiliary = load_bundle(S08)
    before = (
        plan.model_dump(mode="json"),
        [entry.model_dump(mode="json") for entry in gold],
        [entry.model_dump(mode="json") for entry in preds],
    )
    score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=str(tmp_path / "frozen")),
    )
    after = (
        plan.model_dump(mode="json"),
        [entry.model_dump(mode="json") for entry in gold],
        [entry.model_dump(mode="json") for entry in preds],
    )
    assert before == after


def test_frozen_reuse_and_idempotence_unaffected(tmp_path: Path):
    """Frozen reuse and idempotence remain intact for valid input."""
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = str(tmp_path / "frozen")

    def make_adapter():
        return MockAgentAdapter(
            lambda request: AgentResponse(
                verdict="equivalent",
                rationale="x",
                source_citations=["left:table-1"],
            )
        )

    first_adapter = make_adapter()
    score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=first_adapter,
    )
    assert len(first_adapter.calls) == 1
    second_adapter = make_adapter()
    second = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=second_adapter,
    )
    assert second_adapter.calls == []
    assert second.auxiliary.judgments[0].reused is True
    entries = list((tmp_path / "frozen").glob("*.json"))
    assert len(entries) == 1  # Idempotent: no duplicate publication.
