"""Hand-fixed numerical expectations; no real Gold, services, or prediction-derived answers."""

import copy
import json
from pathlib import Path

import pytest
from scoring_sample_helpers import build_single_quantity_bundle

from hvs_catalog.evaluation.bundle import save_scoring_bundle, score_scoring_bundle
from hvs_catalog.evaluation.scoring import (
    MockAgentAdapter,
    ScoringInputError,
    auxiliary_config_digest,
    gold_input_digest,
    numeric_scheme_reference,
    numeric_scoring_config,
    protocol_only_config,
    score_batch,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures/scoring/numeric_cases.json").read_text()
)


def bundle(golds, predictions):
    def entries(values, prefix):
        return [
            (
                f"{prefix}{i}",
                spec.get("condition", "Synthetic condition"),
                {k: v for k, v in spec.items() if k != "condition"},
                [],
            )
            for i, spec in enumerate(values)
        ]

    plan, gold, preds = build_single_quantity_bundle(
        entries(golds, "g"), entries(predictions, "p")
    )
    plan.comparison_mode = "numeric"
    plan.scoring_scheme_ref = numeric_scheme_reference()
    plan.auxiliary_ref.fingerprint = auxiliary_config_digest(numeric_scoring_config())
    return plan, gold, preds


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_fixed_complete_numeric_structures(case):
    plan, gold, preds = bundle([case["gold"]], [case["prediction"]])
    before = copy.deepcopy(
        [
            plan.model_dump(mode="json"),
            gold[0].model_dump(mode="json"),
            preds[0].model_dump(mode="json"),
        ]
    )
    result = score_batch(plan, gold, preds)
    p = result.papers[0]
    assert (p.l2.correct, p.l2.predicted, p.l2.gold) == (case["correct"], 1, 1)
    assert result.batch_provisional == case.get("provisional", False)
    assert p.l2.precision == (None if case.get("provisional") else case["correct"])
    assert not result.auxiliary.enabled and not result.auxiliary.protocol_only
    assert before == [
        plan.model_dump(mode="json"),
        gold[0].model_dump(mode="json"),
        preds[0].model_dump(mode="json"),
    ]
    assert p.value_records["gold"] and p.value_records["prediction"]


def test_maximum_numeric_matches_ignore_swapped_conditions_and_input_order():
    plan, gold, preds = bundle(
        [
            {"value": "8.2", "condition": "Model A"},
            {"value": "9.2", "condition": "Model B"},
        ],
        [
            {"value": "9.2", "condition": "Model A"},
            {"value": "8.2", "condition": "Model B"},
            {"value": "8.2"},
        ],
    )
    result = score_batch(plan, gold, preds)
    assert (
        result.micro.l2.correct,
        result.micro.l2.predicted,
        result.micro.l2.gold,
    ) == (2, 3, 2)
    pairs = [r for r in result.papers[0].pairings if r.layer == "l2"]
    assert len({r.gold_ref for r in pairs if r.gold_ref}) == 2
    assert len({r.prediction_ref for r in pairs if r.prediction_ref}) == 3
    assert sum(r.status == "extra_value" for r in pairs) == 1
    preds[0].objects[0].quantities[0].values.reverse()
    assert score_batch(plan, gold, preds).micro == result.micro


def test_residual_differences_are_one_to_one_and_do_not_raise_correct_count():
    plan, gold, preds = bundle(
        [{"value": "1"}, {"value": "2"}],
        [{"value": "3"}, {"value": "4"}, {"value": "5"}],
    )
    result = score_batch(plan, gold, preds)
    assert (
        result.micro.l2.correct,
        result.micro.l2.predicted,
        result.micro.l2.gold,
    ) == (0, 3, 2)
    pairs = [r for r in result.papers[0].pairings if r.layer == "l2"]
    assert sum(r.status == "value_mismatch" for r in pairs) == 2
    assert sum(r.status == "extra_value" for r in pairs) == 1
    assert len({r.gold_ref for r in pairs if r.gold_ref}) == 2


def test_missing_and_extra_objects_keep_all_numerical_denominators():
    plan, gold, preds = bundle([{"value": "8.2"}], [{"value": "8.2"}])
    preds[0].objects[0].identifiers = ["OTHER"]
    result = score_batch(plan, gold, preds)
    assert (result.micro.l1.tp, result.micro.l1.fp, result.micro.l1.fn) == (0, 1, 1)
    assert (
        result.micro.l2.correct,
        result.micro.l2.predicted,
        result.micro.l2.gold,
    ) == (0, 1, 1)
    assert {r.status for r in result.papers[0].pairings if r.layer == "l2"} == {
        "object_omitted_value",
        "extra_object_value",
    }


def test_duplicate_object_values_cannot_consume_gold_twice():
    plan, gold, preds = bundle([{"value": "8.2"}], [{"value": "8.2"}])
    duplicate = copy.deepcopy(preds[0].objects[0])
    duplicate.label = "duplicate"
    duplicate.quantities[0].values[0].record_id = "duplicate-value"
    preds[0].objects.append(duplicate)
    result = score_batch(plan, gold, preds)
    assert (result.micro.l1.tp, result.micro.l1.fp, result.micro.l1.fn) == (1, 1, 0)
    assert (
        result.micro.l2.correct,
        result.micro.l2.predicted,
        result.micro.l2.gold,
    ) == (1, 2, 1)


@pytest.mark.parametrize("state", ["missing", "failed", "partial"])
def test_failed_and_partial_predictions_retain_gold_and_delivery_facts(state):
    plan, gold, preds = bundle([{"value": "8.2"}], [{"value": "8.2"}])
    if state == "missing":
        preds = []
    elif state == "failed":
        preds[0].objects = []
        preds[0] = preds[0].model_copy(
            update={"delivered": False, "format_valid": False, "completion": "failed"}
        )
    else:
        preds[0].completion = "partial"
        preds[0].execution_state = "paused"
        preds[0].science_state = "pending"
        preds[0].pending_questions = 1
    result = score_batch(plan, gold, preds)
    assert result.micro.l2.gold == 1
    assert result.micro.l2.correct == (1 if state == "partial" else 0)
    if state == "partial":
        assert result.papers[0].pending_questions == 1
    else:
        assert result.micro.l2.recall == result.micro.l2.f1 == 0


def test_unknown_candidates_only_block_rates_when_correct_count_can_change():
    plan, gold, preds = bundle(
        [{"value": "8.2"}], [{"value": "8.2"}, {"value": "\\mystery{8.2}"}]
    )
    result = score_batch(plan, gold, preds)
    assert not result.batch_provisional
    assert (
        result.micro.l2.correct,
        result.micro.l2.predicted,
        result.micro.l2.gold,
    ) == (1, 2, 1)


def test_numeric_scoring_rejects_auxiliary_models_and_stale_scheme():
    plan, gold, preds = bundle([{"value": "8.2"}], [{"value": "8.2"}])
    with pytest.raises(ScoringInputError, match="辅助模型"):
        score_batch(plan, gold, preds, agent_adapter=MockAgentAdapter([]))
    with pytest.raises(ScoringInputError, match="辅助模型"):
        score_batch(plan, gold, preds, auxiliary=protocol_only_config())
    plan.scoring_scheme_ref.fingerprint = "a" * 64
    with pytest.raises(ScoringInputError, match="方案"):
        score_batch(plan, gold, preds)


def test_readiness_gaps_preserve_l0_and_block_final_metrics():
    plan, gold, preds = bundle([{"value": "8.2"}], [{"value": "8.2"}])
    plan.readiness_gaps = {plan.paper_keys[0]: ["PDF hash mismatch"]}
    result = score_batch(plan, gold, preds)
    assert result.papers[0].l0.delivered == 1
    assert result.papers[0].l1 is result.papers[0].l2 is None
    assert result.batch_provisional


def test_complete_empty_objects_are_separate_and_empty_quantities_add_no_credit():
    plan, gold, preds = bundle([{"value": "8.2"}], [{"value": "8.2"}])
    gold[0].objects = []
    preds[0].objects = []
    plan.gold_ref.fingerprint = gold_input_digest(gold)
    report = score_batch(plan, gold, preds)
    assert report.papers[0].correct_empty
    assert (
        report.micro.l2.correct,
        report.micro.l2.predicted,
        report.micro.l2.gold,
    ) == (0, 0, 0)
    assert (
        report.micro.l2.precision
        is report.micro.l2.recall
        is report.micro.l2.f1
        is None
    )


def test_numeric_bundle_replay_is_identical_with_frozen_context(tmp_path):
    plan, gold, preds = bundle([{"value": "8.2"}], [{"value": "8.2"}])
    plan.input_snapshot = {
        "synthetic_commit": "c" * 40,
        "run_facts": {"calls": 1, "tokens": None},
    }
    path = tmp_path / "bundle.json"
    save_scoring_bundle(
        path,
        plan=plan,
        gold=gold,
        predictions=preds,
        auxiliary=numeric_scoring_config(),
    )
    assert score_scoring_bundle(path) == score_batch(plan, gold, preds)
    payload = json.loads(path.read_text())
    payload["plan"]["input_snapshot"]["run_facts"]["calls"] = 2
    path.write_text(json.dumps(payload))
    from hvs_catalog.evaluation.bundle import ScoringBundleError

    with pytest.raises(ScoringBundleError, match="摘要"):
        score_scoring_bundle(path)
