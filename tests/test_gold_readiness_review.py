"""Scoring preparation decisions bind current pairs without rewriting PDF Gold."""

import hashlib

import pytest
from evaluation_helpers import build_fresh_draft, write_synthetic_pdf

from hvs_catalog.evaluation import (
    GoldAdaptationError,
    GoldReadinessDecision,
    GoldReadinessReview,
    adapt_compact_gold_pair,
    gold_entries_from_compact_pair,
    gold_readiness_fingerprint,
)
from hvs_catalog.evaluation.compact import (
    compact_answer_to_json,
    compact_gold_annotation,
)
from hvs_catalog.evaluation.models import parse_gold_annotation


def _pair(tmp_path, *, partial=False):
    digest = write_synthetic_pdf(tmp_path / "materials/paper.pdf")
    draft = parse_gold_annotation(build_fresh_draft(pdf_sha256=digest))
    draft.material_correspondence.differences = ["One PDF-only printed row"]
    scope = "partial" if partial else "complete"
    return compact_gold_annotation(
        draft,
        title="Synthetic paper",
        original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review={
            "value": "checked",
            "basis": "Fixed synthetic pair",
            "record_sha256": "a" * 64,
            "differences": ["One PDF-only printed row"],
        },
        scope_review={
            "value": scope,
            "basis": "Synthetic scope",
            "record_sha256": "a" * 64,
        },
        scope_limitations=[
            {"subject": "external table", "value": "Outside fixed source package"}
        ]
        if partial
        else [],
    )


def _review(pair, *, partial=False):
    decisions = [
        GoldReadinessDecision(
            code="material_differences",
            subject="metadata/review/material/differences",
            outcome="retained_limitation",
            basis="Human retains PDF-only reference and records its score impact",
        )
    ]
    if partial:
        decisions.append(
            GoldReadinessDecision(
                code="partial_coverage",
                subject="coverage",
                outcome="outside_scoring_scope",
                basis="Only outside-package content is missing; supported scoring scope was fully reviewed",
            )
        )
    return GoldReadinessReview(
        pair_sha256=gold_readiness_fingerprint(pair),
        decision_source="Synthetic human decision record",
        decisions=decisions,
        complete_within_scoring_scope=partial,
    )


def test_reviewed_material_difference_retains_numerical_gold(tmp_path):
    pair = _pair(tmp_path)
    with pytest.raises(GoldAdaptationError, match="准备缺口"):
        gold_entries_from_compact_pair(pair)
    before = compact_answer_to_json(pair.answer)
    result = adapt_compact_gold_pair(pair, scoring_review=_review(pair))
    assert result.formal_readiness and not result.readiness_gaps
    assert result.binding["scoring_review"][
        "pair_sha256"
    ] == gold_readiness_fingerprint(pair)
    assert result.limitations
    assert compact_answer_to_json(pair.answer) == before


def test_only_outside_package_partial_coverage_can_be_complete_for_scoring(tmp_path):
    pair = _pair(tmp_path, partial=True)
    entries = gold_entries_from_compact_pair(
        pair, scoring_review=_review(pair, partial=True)
    )
    assert entries[0].coverage == "complete"
    assert pair.answer.coverage.value == "partial"
    assert pair.metadata.review.scope.value == "partial"


def test_changes_to_metadata_or_answer_invalidate_prior_review(tmp_path):
    pair = _pair(tmp_path)
    review = _review(pair)
    pair.metadata.review.material.differences.append("New unreviewed difference")
    with pytest.raises(GoldAdaptationError, match="指纹"):
        gold_entries_from_compact_pair(pair, scoring_review=review)


def test_missing_evidence_cannot_be_waived_by_human_name_or_old_review(tmp_path):
    pair = _pair(tmp_path)
    pair.answer.contributions[0].identifiers[0].evidence = []
    pair.metadata.review.answer_sha256 = hashlib.sha256(
        compact_answer_to_json(pair.answer).encode()
    ).hexdigest()
    review = _review(pair)
    review.decisions.append(
        GoldReadinessDecision(
            code="missing_identifier_evidence",
            subject=f"contributions/{pair.answer.contributions[0].contribution_id}/identifiers/SYN-A",
            outcome="defined_reference",
            basis="A name is not evidence",
        )
    )
    with pytest.raises(GoldAdaptationError, match="豁免"):
        gold_entries_from_compact_pair(pair, scoring_review=review)


def test_invented_resolution_is_rejected(tmp_path):
    pair = _pair(tmp_path)
    review = _review(pair)
    review.decisions.append(
        GoldReadinessDecision(
            code="unresolved_99",
            subject="none",
            outcome="defined_reference",
            basis="Nonexistent problem",
        )
    )
    with pytest.raises(GoldAdaptationError, match="不存在"):
        gold_entries_from_compact_pair(pair, scoring_review=review)
