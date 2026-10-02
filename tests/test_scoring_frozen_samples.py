"""Frozen manually calculated examples: JSON inputs against independent expected results
(S01-S10).

samples.py deterministically generates frozen inputs under
tests/fixtures/scoring/inputs/ with descriptive filenames. Expected results are manually
calculated in expected/expected.json, never derived by running the scorer.
Loading/comparison helpers live in scoring_sample_helpers. All materials are fictional.
"""

from __future__ import annotations

import json

import pytest
from scoring_sample_helpers import (
    EXPECTED,
    SAMPLE_IDS,
    check_l0,
    check_metric,
    expected_rows,
    rows_of,
    score_sample,
)


@pytest.mark.parametrize("sample_id", SAMPLE_IDS)
def test_fixed_sample_matches_hand_computed_expectation(sample_id):
    assert sample_id in EXPECTED, f"缺少预期结果：{sample_id}"
    expected = EXPECTED[sample_id]
    report = score_sample(sample_id)

    assert report.batch_provisional is expected["batch_provisional"]
    assert report.correct_empty_papers == expected["correct_empty_papers"]

    assert sorted(paper.paper_key for paper in report.papers) == sorted(
        expected["papers"]
    )
    for paper in report.papers:
        paper_expected = expected["papers"][paper.paper_key]
        assert paper.readiness == paper_expected["readiness"]
        if "readiness_gaps" in paper_expected:
            assert sorted(paper.readiness_gaps) == sorted(
                paper_expected["readiness_gaps"]
            )
        check_l0(paper.l0, paper_expected["l0"])
        assert paper.correct_empty is paper_expected["correct_empty"]
        assert sorted(paper.prediction_errors) == sorted(
            paper_expected["prediction_errors"]
        )
        if paper_expected["l1"] is None:
            assert paper.l1 is None and paper.l2 is None
        else:
            check_metric(paper.l1, paper_expected["l1"], ("tp", "fp", "fn"))
            check_metric(
                paper.l2, paper_expected["l2"], ("correct", "predicted", "gold")
            )
            assert paper.l2_provisional is paper_expected["l2_provisional"]
        assert {
            breakdown.quantity_path: breakdown.metrics
            for breakdown in paper.quantity_breakdown
        }.keys() == paper_expected["quantities"].keys()
        for breakdown in paper.quantity_breakdown:
            quantity_expected = paper_expected["quantities"][breakdown.quantity_path]
            check_metric(
                breakdown.metrics,
                quantity_expected,
                ("correct", "predicted", "gold"),
            )
        assert rows_of(paper) == expected_rows(paper_expected)
        assert sorted(d.code for d in paper.diagnostics) == sorted(
            paper_expected["diagnostic_codes"]
        )
        assert sorted(d.code for d in paper.unresolved) == sorted(
            paper_expected["unresolved_codes"]
        )

    check_l0(report.micro.l0, expected["micro"]["l0"])
    check_metric(report.micro.l1, expected["micro"]["l1"], ("tp", "fp", "fn"))
    check_metric(
        report.micro.l2, expected["micro"]["l2"], ("correct", "predicted", "gold")
    )


def test_expected_file_covers_all_inputs_exactly():
    assert sorted(EXPECTED.keys() - {"_note"}) == SAMPLE_IDS


def test_report_is_json_serializable_and_deterministic():
    sample_id = "synthetic-s01"
    first = score_sample(sample_id)
    second = score_sample(sample_id)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert json.loads(first.model_dump_json())["batch_provisional"] is False


def test_s01_pairing_and_diagnosis_semantics():
    report = score_sample("synthetic-s01")
    paper = report.papers[0]
    mismatch = [
        row
        for row in paper.pairings
        if row.status == "value_mismatch" and row.fields == ["value"]
    ]
    assert len(mismatch) == 1
    assert mismatch[0].gold_ref.endswith("|g2")
    # An incorrectly copied A speed is never forgiven by within_gold_error; differing central values differ.
    assert paper.quantity_breakdown[1].metrics.correct == 0


def test_s09_scoped_report_does_not_claim_full_paper():
    report = score_sample("synthetic-s09")
    paper = report.papers[0]
    assert paper.readiness == "scoped" and paper.scoped is True
    assert paper.scope_note is not None and "SYN-A" in paper.scope_note
    assert any("不声称整论文准确率" in note for note in report.notes), report.notes


def test_s07_unresolved_keeps_counts_and_nulls_final_l2_rates():
    for sample_id in ("synthetic-s07u1", "synthetic-s07u2"):
        report = score_sample(sample_id)
        paper = report.papers[0]
        assert paper.l2.precision is None and paper.l2.recall is None
        assert paper.l2.f1 is None
        assert report.batch_provisional is True
        assert report.provisional_reasons
        expected_counts = (0, 1, 1) if sample_id.endswith("u1") else (0, 1, 2)
        assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == expected_counts
