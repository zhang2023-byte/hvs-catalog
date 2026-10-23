"""Binary scientific matching, fixed denominators and independent hand-counted cases."""

import copy
import json
from pathlib import Path

import pytest
from scoring_helpers import document, group, plan, prediction, reference, star

from hvs_catalog.evaluation.scoring import score_batch, value_differences
from hvs_catalog.evaluation.scoring.layers import maximum_matching
from hvs_catalog.records.literature_contribution import LiteratureValue

CASES = json.loads(
    (Path(__file__).parent / "fixtures/scoring/numeric_cases.json").read_text()
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_approved_numeric_boundaries(case):
    evidence = {"evidence": [{"location": "I:p1-b0"}]}
    gold = LiteratureValue.model_validate({**case["gold"], **evidence})
    pred = LiteratureValue.model_validate({**case["prediction"], **evidence})
    original = gold.model_dump(), pred.model_dump()
    assert (
        not value_differences(gold, pred, case.get("quantity", "distance"))
    ) == bool(case["correct"])
    assert (gold.model_dump(), pred.model_dump()) == original


def test_hand_counted_example_and_roster_details():
    gold = document(
        [
            star(
                "A",
                [
                    group(
                        "radial_velocity",
                        {"value": "520", "error": "10", "unit": "km/s"},
                    ),
                    group("distance", {"value": "8.2", "error": "0.6", "unit": "kpc"}),
                ],
                aliases=("Alias-A",),
            ),
            star(
                "B",
                [group("parallax", {"value": "0.5", "error": "0.1", "unit": "mas"})],
            ),
        ]
    )
    pred = document(
        [
            star(
                "A",
                [
                    group(
                        "radial_velocity",
                        {"value": "520", "error": "20", "unit": "km/s"},
                    ),
                    group("distance", {"value": "8.2", "error": "0.6", "unit": "kpc"}),
                ],
                category="follow_up",
            ),
            star(
                "C",
                [
                    group(
                        "radial_velocity",
                        {"value": "600", "error": "15", "unit": "km/s"},
                    )
                ],
            ),
        ]
    )
    report = score_batch(plan(), [reference(gold)], [prediction(pred)])
    row = report.papers[0]
    assert (row.l1.tp, row.l1.fp, row.l1.fn, row.l1.f1) == (1, 1, 1, 0.5)
    assert (row.l2.correct, row.l2.predicted, row.l2.gold) == (1, 3, 3)
    assert row.l2.f1 == pytest.approx(1 / 3)
    assert row.roster_details.missing_identifiers == 1
    assert row.roster_details.contribution_correct == 0
    assert row.roster_details.contribution_accuracy == 0
    assert row.l0.completion_counts == {"complete": 1}
    assert any(r.fields == ["error"] for r in row.pairings)
    assert "provisional" not in report.model_dump_json()


def test_duplicates_and_reordered_conditions_do_not_inflate_scores():
    gold = document(
        [
            star(
                "A",
                [
                    group(
                        "distance",
                        {"value": "8", "condition": "Model A"},
                        {"value": "9", "condition": "Model B"},
                    )
                ],
                aliases=("Alias-A",),
            )
        ]
    )
    pred = document(
        [
            star(
                "Alias-A",
                [
                    group(
                        "distance",
                        {"value": "9", "condition": "Model A"},
                        {"value": "8", "condition": "Model B"},
                    )
                ],
            ),
            star("A", [group("distance", {"value": "8"})]),
        ]
    )
    row = score_batch(plan(), [reference(gold)], [prediction(pred)]).papers[0]
    assert (row.l1.tp, row.l1.fp, row.l1.fn) == (1, 1, 0)
    assert (row.l2.correct, row.l2.predicted, row.l2.gold) == (2, 3, 2)
    assert (
        len([p for p in row.pairings if p.layer == "value" and p.status == "extra"])
        == 1
    )


def test_maximum_matching_reassigns_without_greedy_loss():
    assert maximum_matching({(0, 0), (0, 1), (1, 0)}, 2) == [(0, 1), (1, 0)]
    edges = {(i, i) for i in range(1500)} | {(i, i - 1) for i in range(1, 1500)}
    assert len(maximum_matching(edges, 1500)) == 1500


def test_failed_and_missing_predictions_keep_all_denominators():
    gold = document([star("A", [group("distance", {"value": "8"})])])
    ref = reference(gold)
    for preds in (
        [],
        [
            prediction(
                document(), delivered=False, format_valid=False, completion="failed"
            )
        ],
    ):
        row = score_batch(plan(), [ref], preds).papers[0]
        assert (row.l1.tp, row.l1.fn, row.l1.recall, row.l1.f1) == (0, 1, 0, 0)
        assert (row.l2.predicted, row.l2.gold, row.l2.recall, row.l2.f1) == (0, 1, 0, 0)
        assert row.l1.precision is None and row.l2.precision is None
    pred = prediction(
        gold, delivered=False, format_valid=False, completion="unfinished"
    )
    row = score_batch(plan(), [ref], [pred]).papers[0]
    assert row.l0.delivered == 0 and row.l1.tp == row.l2.correct == 1


@pytest.mark.parametrize(
    "completion,correct_empty", [("complete", True), ("failed", False)]
)
def test_empty_is_not_a_perfect_scientific_score(completion, correct_empty):
    full = completion == "complete"
    row = score_batch(
        plan(),
        [reference(document())],
        [
            prediction(
                document(), completion=completion, delivered=full, format_valid=full
            )
        ],
    ).papers[0]
    assert row.correct_empty is correct_empty
    assert row.l1.f1 is None and row.l2.f1 is None


def test_roster_scope_does_not_score_quantities():
    doc = document([star("A", [group("distance", {"value": "8"})])])
    result = score_batch(
        plan(scope="roster"),
        [reference(doc)],
        [prediction(document([star()]), scope="roster")],
    )
    assert result.micro.l2 is None and result.papers[0].quantities == {}


def test_identity_requires_one_unambiguous_match_and_preserves_release():
    gold = document([star("A", aliases=("Alias-A",)), star("B")])
    pred = document([star("A", aliases=("B",))])
    row = score_batch(plan(), [reference(gold)], [prediction(pred)]).papers[0]
    assert (row.l1.tp, row.l1.fp, row.l1.fn) == (0, 1, 2)
    for a, b, count in [
        ("Gaia DR3 1234567890123456789", "Gaia DR3 Source ID 1234567890123456789", 1),
        ("Gaia DR3 1234567890123456789", "Gaia DR2 1234567890123456789", 0),
        ("Gaia DR3 1234567890123456789", "1234567890123456789", 0),
        ("A-B", "AB", 0),
        ("A", "a", 0),
        ("A−B", "A-B", 1),
    ]:
        row = score_batch(
            plan(), [reference(document([star(a)]))], [prediction(document([star(b)]))]
        ).papers[0]
        assert row.l1.tp == count


def test_micro_uses_counts_not_average_paper_rates():
    first = document([star("A")])
    second = document([star("B"), star("C"), star("D")], paper="2601.99998")
    result = score_batch(
        plan("2601.99999", "2601.99998"),
        [reference(first), reference(second)],
        [prediction(first)],
    )
    assert result.micro.l1.recall == 0.25
    assert result.micro.l1.f1 == pytest.approx(0.4)
    assert result.micro.l0.completion_counts == {"complete": 1, "unfinished": 1}


def test_revalidate_modified_inputs_and_preserve_sources():
    ref = reference(document([star()]))
    pred = prediction(document([star()]))
    before = copy.deepcopy((ref.model_dump(), pred.model_dump()))
    score_batch(plan(), [ref], [pred])
    assert (ref.model_dump(), pred.model_dump()) == before
    ref.stars[0].identifiers[0].value = "B"
    changed = score_batch(plan(), [ref], [pred])
    assert changed.micro.l1.tp == 0
    assert (
        changed.input_sha256
        != score_batch(plan(), [reference(document([star()]))], [pred]).input_sha256
    )


@pytest.mark.parametrize(
    "change", ["paper", "version", "material", "rules", "scope", "duplicate"]
)
def test_reject_incompatible_inputs(change):
    gold = reference(document([star()]))
    pred = prediction(document([star()]))
    if change == "paper":
        pred.content.metadata.arxiv_id = "2601.99998"
    if change == "version":
        pred.content.metadata.version = "v2"
    if change == "material":
        pred.materials.files[0].sha256 = "3" * 64
    if change == "rules":
        pred.rules.version = "unknown"
    if change == "scope":
        pred.scope = "roster"
    with pytest.raises(ValueError):
        score_batch(plan(), [gold], [pred, pred] if change == "duplicate" else [pred])
