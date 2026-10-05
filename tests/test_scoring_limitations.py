"""Hand-calculated single scores and frozen scope decisions."""

import json

import pytest
from pydantic import ValidationError
from scoring_sample_helpers import DIST, load_bundle

from hvs_catalog.evaluation.bundle import (
    ScoringBundleError,
    load_scoring_bundle,
    save_scoring_bundle,
    score_scoring_bundle,
)
from hvs_catalog.evaluation.scoring import (
    EvaluationLimitation,
    GoldObjectEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    LimitationScope,
    PredictionObjectEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
    ScoringInputError,
    build_measured_value,
    gold_input_digest,
    score_batch,
)


def _inputs():
    plan, gold, predictions, auxiliary = load_bundle("synthetic-s06b8")
    gold[0].objects = [
        GoldObjectEntry(
            label=f"g{name}",
            identifiers=[f"SYN-{name}"],
            quantities=[
                GoldQuantityEntry(
                    quantity_path=DIST,
                    values=[
                        GoldValueEntry(
                            record_id=f"g{name}-value",
                            value=build_measured_value(
                                value_id=f"g{name}-value",
                                value="8.0",
                                unit="kpc",
                                condition="standard",
                            ),
                        )
                    ],
                )
            ],
        )
        for name in "ABC"
    ]
    predictions[0].objects = [
        PredictionObjectEntry(
            paper_key=gold[0].paper_key,
            label=f"p{name}",
            identifiers=[f"SYN-{name}"],
            quantities=[
                PredictionQuantityEntry(
                    quantity_path=DIST,
                    values=[
                        PredictionValueEntry(
                            record_id=f"p{name}-value",
                            value=build_measured_value(
                                value_id=f"p{name}-value",
                                value="8.0",
                                unit="kpc",
                                condition="standard",
                            ),
                        )
                    ],
                )
            ],
        )
        for name in "ACD"
    ]
    plan.gold_ref.fingerprint = gold_input_digest(gold)
    return plan, gold, predictions, auxiliary


def _limitation(
    plan, *, category="external_out_of_scope", identifier="SYN-C", **kwargs
):
    return EvaluationLimitation(
        limitation_id="issue-c",
        paper_key=plan.paper_keys[0],
        category=category,
        scope=LimitationScope(object_identifiers=[identifier]),
        basis="Synthetic human-reviewed scope decision before predictions",
        evidence=["synthetic PDF page 2"],
        **kwargs,
    )


def test_single_score_keeps_parser_omissions_and_does_not_mutate_inputs():
    plan, gold, predictions, auxiliary = _inputs()
    plan.limitations = [
        _limitation(plan, category="unsupported_material", identifier="SYN-B")
    ]
    original = [x.model_dump(mode="json") for x in gold + predictions]
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert (report.micro.l1.tp, report.micro.l1.fp, report.micro.l1.fn) == (2, 1, 1)
    assert (
        report.micro.l2.correct,
        report.micro.l2.predicted,
        report.micro.l2.gold,
    ) == (2, 3, 3)
    assert report.micro.l2.f1 == pytest.approx(2 / 3)
    assert "reference_score" not in report.model_dump(mode="json")
    assert original == [x.model_dump(mode="json") for x in gold + predictions]
    assert report.digests["input_gold"] == plan.gold_ref.fingerprint
    assert any(p.diagnostics or p.pairings for p in report.papers)


def test_quantity_scope_preserves_objects_and_removes_wrong_duplicates():
    plan, gold, predictions, auxiliary = _inputs()
    baseline = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    issue = _limitation(plan, exclude_from_default=True)
    issue.scope.quantity_path = DIST
    plan.limitations = [issue]
    duplicate = predictions[0].objects[1].model_copy(deep=True)
    duplicate.label = "pC-duplicate"
    duplicate.quantities[0].values[0].record_id = "pC-duplicate-value"
    duplicate.quantities[0].values[0].value = build_measured_value(
        value_id="pC-duplicate-value", value="99.0", unit="kpc", condition="standard"
    )
    predictions[0].objects.append(duplicate)
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert (
        report.micro.l2.correct,
        report.micro.l2.predicted,
        report.micro.l2.gold,
    ) == (1, 2, 2)
    assert report.micro.l1.tp == baseline.micro.l1.tp


def test_external_scope_is_symmetric():
    plan, gold, predictions, auxiliary = _inputs()
    plan.limitations = [
        _limitation(plan, identifier="SYN-D", exclude_from_default=True)
    ]
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert (report.micro.l1.tp, report.micro.l1.fp, report.micro.l1.fn) == (2, 0, 1)
    assert (
        report.micro.l2.correct,
        report.micro.l2.predicted,
        report.micro.l2.gold,
    ) == (2, 2, 3)
    assert report.limitation_effects[0].excluded_from_default


def test_unsupported_parser_cannot_remove_existing_pdf_gold():
    plan, gold, predictions, auxiliary = _inputs()
    plan.limitations = [
        _limitation(plan, category="unsupported_material", exclude_from_default=True)
    ]
    with pytest.raises(ScoringInputError, match="仍须默认评分"):
        score_batch(plan, gold, predictions, auxiliary=auxiliary)


def test_author_problem_retains_scores_and_existing_difference_association():
    plan, gold, predictions, auxiliary = _inputs()
    predictions[0].objects[1].quantities[0].values[0].value = build_measured_value(
        value_id="pC-value", value="99.0", unit="kpc", condition="standard"
    )
    expected = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    plan.limitations = [_limitation(plan, category="author_issue")]
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert report.micro == expected.micro
    assert report.limitation_effects[0].observed_error_refs


def test_unknown_scope_membership_keeps_prediction_and_marks_score_provisional():
    plan, gold, predictions, auxiliary = _inputs()
    gold[0].objects[2].quantities[0].values[
        0
    ].value.condition = "under Galactic potential M1"
    predictions[0].objects[1].quantities[0].values[
        0
    ].value.condition = "under Galactic potential"
    plan.gold_ref.fingerprint = gold_input_digest(gold)
    issue = _limitation(plan, exclude_from_default=True)
    issue.scope.quantity_path = DIST
    issue.scope.conditions = ["under Galactic potential M1"]
    plan.limitations = [issue]
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert report.micro.l2.predicted == 3
    assert report.batch_provisional and report.micro.l2.f1 is None
    assert any(
        item.startswith("scope_membership_unknown:")
        for item in report.provisional_reasons
    )


def test_predeclared_scope_distinguishes_historical_and_current_values():
    plan, gold, predictions, auxiliary = _inputs()
    for obj in (gold[0].objects[2], predictions[0].objects[1]):
        historical = obj.quantities[0].values[0].model_copy(deep=True)
        historical.record_id += "-historical"
        historical.value.value_id += "-historical"
        historical.value.source = "prior_work"
        obj.quantities[0].values.append(historical)
    plan.gold_ref.fingerprint = gold_input_digest(gold)
    baseline = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    issue = _limitation(plan, exclude_from_default=True)
    issue.scope.quantity_path = DIST
    issue.scope.attribution = {"source": ["prior_work"]}
    plan.limitations = [issue]
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert (
        report.micro.l2.correct,
        report.micro.l2.predicted,
        report.micro.l2.gold,
    ) == (2, 3, 3)
    assert report.micro.l1 == baseline.micro.l1


def test_old_reference_interfaces_are_rejected():
    plan, *_ = _inputs()
    with pytest.raises(ValidationError):
        _limitation(plan, exclude_from_reference=True)
    with pytest.raises(ValidationError):
        _limitation(plan, category="pdf_source_mismatch")
    with pytest.raises(ValidationError):
        _limitation(plan, category="author_issue", exclude_from_default=True)


def test_scope_uses_aliases_and_keeps_ambiguous_identity():
    plan, gold, predictions, auxiliary = _inputs()
    gold[0].objects[2].identifiers.append("ALIAS-C")
    plan.gold_ref.fingerprint = gold_input_digest(gold)
    predictions[0].objects[1].identifiers = ["ALIAS-C"]
    plan.limitations = [_limitation(plan, exclude_from_default=True)]
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert report.micro.l2.predicted == 2
    predictions[0].objects[1].identifiers.append("SYN-A")
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert report.micro.l2.predicted == 3
    assert report.batch_provisional and report.micro.l1.f1 is None


def test_failing_and_missing_predictions_keep_gold_denominator():
    plan, gold, _, auxiliary = _inputs()
    plan.limitations = [_limitation(plan, category="unsupported_material")]
    report = score_batch(plan, gold, [], auxiliary=auxiliary)
    assert report.micro.l0.delivered == 0
    assert report.micro.l2.gold == 3 and report.micro.l2.recall == 0


def test_bundle_freezes_scope_and_reproduces_single_score(tmp_path):
    plan, gold, predictions, auxiliary = _inputs()
    plan.limitations = [_limitation(plan, exclude_from_default=True)]
    path = tmp_path / "bundle.json"
    save_scoring_bundle(
        path, plan=plan, gold=gold, predictions=predictions, auxiliary=auxiliary
    )
    first = score_scoring_bundle(path)
    assert first == score_scoring_bundle(path)
    assert first.micro.l2.f1 == 0.5
    assert "reference_score" not in first.model_dump(mode="json")
    payload = json.loads(path.read_text())
    payload["plan"]["limitations"][0]["basis"] = "changed decision"
    path.write_text(json.dumps(payload))
    with pytest.raises(ScoringBundleError, match="摘要"):
        load_scoring_bundle(path)


def test_unchanged_plans_do_not_gain_empty_fields():
    plan, gold, predictions, auxiliary = _inputs()
    assert "limitations" not in plan.model_dump(mode="json")
    report = score_batch(plan, gold, predictions, auxiliary=auxiliary)
    assert "reference_score" not in report.model_dump(mode="json")
    assert "limitation_effects" not in report.model_dump(mode="json")
