"""Public-contract rejection, invariants, and negative diagnostics using synthetic
materials.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from scoring_sample_helpers import INPUTS, load_bundle, sample_path, score_sample

from hvs_catalog.evaluation.scoring import (
    AuxiliaryScoringConfig,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    InputReference,
    PaperEvaluationScope,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
    ScoringInputError,
    ScoringPlan,
    build_measured_value,
    gold_input_digest,
    score_batch,
)

DIST = "observed_phase_space.distance"
RV = "observed_phase_space.radial_velocity"
STD = "as reported in the paper"


def _value(value_id, **kwargs):
    kwargs.setdefault("condition", STD)
    return build_measured_value(value_id=value_id, **kwargs)


def _simple_bundle(paper_key="synthetic-scoring-contract"):
    value_kwargs = {"value": "8.2", "error": "0.3", "unit": "kpc"}
    gold = [
        GoldPaperEntry(
            paper_key=paper_key,
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                GoldValueEntry(
                                    record_id="g1",
                                    value=_value("g1", **value_kwargs),
                                )
                            ],
                        )
                    ],
                )
            ],
        )
    ]
    predictions = [
        PredictionPaperEntry(
            paper_key=paper_key,
            delivered=True,
            format_valid=True,
            completion="complete",
            objects=[
                PredictionObjectEntry(
                    paper_key=paper_key,
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                PredictionValueEntry(
                                    record_id="p1",
                                    value=_value("p1", **value_kwargs),
                                )
                            ],
                        )
                    ],
                )
            ],
        )
    ]
    return gold, predictions


def _plan_for(paper_key, gold, auxiliary, paths=(DIST,)):
    def ref(name, seed):
        return InputReference(
            name=name, version="v1", fingerprint=hashlib.sha256(seed).hexdigest()
        )

    from hvs_catalog.evaluation.scoring import auxiliary_config_digest

    return ScoringPlan(
        paper_keys=[paper_key],
        scopes=[PaperEvaluationScope(paper_key=paper_key, quantity_paths=list(paths))],
        gold_ref=InputReference(
            name="synthetic-gold",
            version="v1",
            fingerprint=gold_input_digest(gold),
        ),
        materials_ref=ref("materials", b"m"),
        science_rules_ref=ref("rules", b"r"),
        scoring_scheme_ref=ref("scheme", b"s"),
        program_ref=ref("program", b"p"),
        auxiliary_ref=InputReference(
            name="auxiliary",
            version="v1",
            fingerprint=auxiliary_config_digest(auxiliary),
        ),
    )


def _auxiliary():
    from hvs_catalog.evaluation.scoring import protocol_only_config

    return protocol_only_config()


def test_reject_duplicate_gold_paper():
    gold, predictions = _simple_bundle()
    gold = gold + [gold[0].model_copy(update={})]
    plan = _plan_for("synthetic-scoring-contract", gold, _auxiliary())
    with pytest.raises(ScoringInputError, match="论文重复"):
        score_batch(plan, gold, predictions, auxiliary=_auxiliary())


def test_reject_out_of_plan_paper():
    gold, _ = _simple_bundle()
    extra_gold, extra_pred = _simple_bundle("synthetic-scoring-other")
    plan = _plan_for("synthetic-scoring-contract", gold, _auxiliary())
    with pytest.raises(ScoringInputError, match="计划外"):
        score_batch(plan, [extra_gold[0]], [extra_pred[0]], auxiliary=_auxiliary())


def test_reject_stale_gold_fingerprint():
    gold, predictions = _simple_bundle()
    plan = _plan_for("synthetic-scoring-contract", gold, _auxiliary())
    tampered_plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": hashlib.sha256(b"stale").hexdigest()}
            )
        }
    )
    with pytest.raises(ScoringInputError, match="Gold 指纹"):
        score_batch(tampered_plan, gold, predictions, auxiliary=_auxiliary())


def test_reject_stale_auxiliary_fingerprint():
    gold, predictions = _simple_bundle()
    plan = _plan_for("synthetic-scoring-contract", gold, _auxiliary())
    different_aux = _auxiliary().model_copy(
        update={"model_config_digest": hashlib.sha256(b"other").hexdigest()}
    )
    with pytest.raises(ScoringInputError, match="辅助配置指纹"):
        score_batch(plan, gold, predictions, auxiliary=different_aux)


def test_reject_wrong_object_paper_attribution():
    _, predictions = _simple_bundle()
    misattributed = (
        predictions[0]
        .objects[0]
        .model_copy(update={"paper_key": "synthetic-scoring-elsewhere"})
    )
    with pytest.raises(ValueError, match="论文归属"):
        PredictionPaperEntry(
            paper_key=predictions[0].paper_key,
            delivered=True,
            format_valid=True,
            completion="complete",
            objects=[misattributed],
        )


def test_reject_duplicate_internal_record_ids():
    with pytest.raises(ValueError, match="record_id 重复"):
        GoldPaperEntry(
            paper_key="synthetic-scoring-contract",
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                GoldValueEntry(
                                    record_id="g1", value=_value("g1", value="8.2")
                                ),
                                GoldValueEntry(
                                    record_id="g1", value=_value("g1b", value="9.9")
                                ),
                            ],
                        )
                    ],
                )
            ],
        )
    with pytest.raises(ValueError, match="label 重复"):
        objects = [
            GoldObjectEntry(label="A", identifiers=["SYN-A"], quantities=[]),
            GoldObjectEntry(label="A", identifiers=["SYN-B"], quantities=[]),
        ]
        GoldPaperEntry(
            paper_key="synthetic-scoring-contract",
            coverage="complete",
            objects=objects,
        )


def test_reject_inconsistent_l0_states():
    with pytest.raises(ValueError, match="未交付"):
        PredictionPaperEntry(
            paper_key="synthetic-scoring-contract",
            delivered=False,
            format_valid=True,
            completion="failed",
            objects=[],
        )
    with pytest.raises(ValueError, match="未交付"):
        PredictionPaperEntry(
            paper_key="synthetic-scoring-contract",
            delivered=False,
            format_valid=False,
            completion="complete",
            objects=[],
        )


def test_invalid_format_prediction_is_not_fake_empty_roster():
    paper_key = "synthetic-scoring-contract"
    gold, _ = _simple_bundle()
    pred = PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=False,
        completion="complete",
        errors=["prediction payload failed format validation"],
    )
    plan = _plan_for(paper_key, gold, _auxiliary())
    report = score_batch(plan, gold, [pred], auxiliary=_auxiliary())
    paper = report.papers[0]
    assert paper.l0.delivered == 1 and paper.l0.format_valid == 0
    assert paper.l0.valid_over_delivered == 0.0
    # Never claim a valid empty roster: preserve the Gold denominator and false correct_empty.
    assert paper.l1.fn == 1 and paper.l2.gold == 1
    assert paper.correct_empty is False
    assert paper.prediction_errors == ["prediction payload failed format validation"]


def test_missing_prediction_entry_counts_as_undelivered():
    paper_key = "synthetic-scoring-contract"
    gold, _ = _simple_bundle()
    plan = _plan_for(paper_key, gold, _auxiliary())
    report = score_batch(plan, gold, [], auxiliary=_auxiliary())
    paper = report.papers[0]
    assert paper.l0.delivered == 0 and paper.l0.completion_counts == {"unfinished": 1}
    assert paper.l1.fn == 1 and paper.l2.gold == 1
    assert paper.prediction_errors == ["missing prediction for planned paper"]
    assert report.micro.l0.delivered_over_planned == 0.0


def test_order_and_internal_ids_do_not_change_scores():
    baseline = score_sample("synthetic-s01")
    data = json.loads(sample_path("synthetic-s01").read_text("utf-8"))
    # Reverse every list and rename internal IDs.
    for obj in data["gold"][0]["objects"]:
        obj["label"] = "X" + obj["label"]
    data["gold"][0]["objects"].reverse()
    data["predictions"][0]["objects"].reverse()
    for obj in data["predictions"][0]["objects"]:
        obj["label"] = "Y" + obj["label"]
    from hvs_catalog.evaluation.scoring import (
        GoldPaperEntry,
        PredictionPaperEntry,
        ScoringPlan,
    )

    gold = [GoldPaperEntry.model_validate(entry) for entry in data["gold"]]
    predictions = [
        PredictionPaperEntry.model_validate(entry) for entry in data["predictions"]
    ]
    from hvs_catalog.evaluation.scoring import gold_input_digest

    original_plan = ScoringPlan.model_validate(data["plan"])
    shuffled_plan = original_plan.model_copy(
        update={
            "gold_ref": original_plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    shuffled = score_batch(shuffled_plan, gold, predictions, auxiliary=_auxiliary())
    first, second = baseline.papers[0], shuffled.papers[0]
    assert (first.l1.tp, first.l1.fp, first.l1.fn) == (
        second.l1.tp,
        second.l1.fp,
        second.l1.fn,
    )
    assert (first.l2.correct, first.l2.predicted, first.l2.gold) == (
        second.l2.correct,
        second.l2.predicted,
        second.l2.gold,
    )
    assert [q.metrics.model_dump() for q in first.quantity_breakdown] == [
        q.metrics.model_dump() for q in second.quantity_breakdown
    ]


def test_unit_loss_and_catalog_difference_are_attribution_or_content_mismatch():
    paper_key = "synthetic-scoring-contract"
    gold, _ = _simple_bundle()
    # Missing units are not wildcard null units.
    no_unit = PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion="complete",
        objects=[
            PredictionObjectEntry(
                paper_key=paper_key,
                label="A",
                identifiers=["SYN-A"],
                quantities=[
                    PredictionQuantityEntry(
                        quantity_path=DIST,
                        values=[
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="8.2", error="0.3"),
                            )
                        ],
                    )
                ],
            )
        ],
    )
    plan = _plan_for(paper_key, gold, _auxiliary())
    report = score_batch(plan, gold, [no_unit], auxiliary=_auxiliary())
    row = [r for r in report.papers[0].pairings if r.status == "value_mismatch"]
    assert row and "unit" in row[0].fields
    assert report.papers[0].l2.correct == 0

    # Catalog/source-version differences produce attribution mismatch diagnostics.
    other_version = _value(
        "p2", value="8.2", error="0.3", unit="kpc", source_note="from Gaia DR3"
    )
    gold_value = _value(
        "g2", value="8.2", error="0.3", unit="kpc", source_note="from Gaia DR2"
    )
    gold2 = [
        GoldPaperEntry(
            paper_key=paper_key,
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path=DIST,
                            values=[GoldValueEntry(record_id="g2", value=gold_value)],
                        )
                    ],
                )
            ],
        )
    ]
    pred2 = PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion="complete",
        objects=[
            PredictionObjectEntry(
                paper_key=paper_key,
                label="A",
                identifiers=["SYN-A"],
                quantities=[
                    PredictionQuantityEntry(
                        quantity_path=DIST,
                        values=[
                            PredictionValueEntry(record_id="p2", value=other_version)
                        ],
                    )
                ],
            )
        ],
    )
    plan2 = _plan_for(paper_key, gold2, _auxiliary())
    report2 = score_batch(plan2, gold2, [pred2], auxiliary=_auxiliary())
    assert any(
        r.status == "attribution_mismatch" and "source_note" in r.fields
        for r in report2.papers[0].pairings
    )
    assert report2.papers[0].l2.correct == 0


def test_prediction_without_gold_counterpart_stays_in_P_only():
    paper_key = "synthetic-scoring-contract"
    gold, _ = _simple_bundle()
    pred = PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion="complete",
        objects=[
            PredictionObjectEntry(
                paper_key=paper_key,
                label="A",
                identifiers=["SYN-A"],
                quantities=[
                    PredictionQuantityEntry(
                        quantity_path=DIST,
                        values=[
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value(
                                    "p1", value="8.2", error="0.3", unit="kpc"
                                ),
                            ),
                            PredictionValueEntry(
                                record_id="p2",
                                value=_value(
                                    "p2",
                                    value="600",
                                    error="20",
                                    unit="km s⁻¹",
                                ),
                            ),
                        ],
                    )
                ],
            )
        ],
    )
    plan = _plan_for(paper_key, gold, _auxiliary())
    report = score_batch(plan, gold, [pred], auxiliary=_auxiliary())
    paper = report.papers[0]
    assert paper.l2.predicted == 2 and paper.l2.gold == 1 and paper.l2.correct == 1
    assert any(r.status == "extra_value" for r in paper.pairings)


def test_ambiguous_alias_keeps_provisional_diagnosis_without_numeric_resolution():
    paper_key = "synthetic-scoring-contract"
    gold = [
        GoldPaperEntry(
            paper_key=paper_key,
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A", "SYN-SHARED"],
                    quantities=[],
                ),
                GoldObjectEntry(
                    label="B",
                    identifiers=["SYN-B", "SYN-SHARED"],
                    quantities=[],
                ),
            ],
        )
    ]
    pred = PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion="complete",
        objects=[
            PredictionObjectEntry(
                paper_key=paper_key,
                label="P",
                identifiers=["SYN-SHARED"],
                quantities=[],
            )
        ],
    )
    plan = _plan_for(paper_key, gold, _auxiliary())
    report = score_batch(plan, gold, [pred], auxiliary=_auxiliary())
    paper = report.papers[0]
    # Keep ambiguous candidates unresolved without FP/FN; relevant layer rates are null.
    assert paper.l1.tp == 0 and paper.l1.fp == 0 and paper.l1.fn == 0
    assert (
        paper.l1.pending == 3
    )  # One ambiguous prediction plus two pending Gold objects.
    assert paper.l1.precision is None and paper.l1.recall is None
    assert any(d.code == "ambiguous_object_alias" for d in paper.diagnostics)
    assert report.batch_provisional is True
    assert any("身份歧义" in note for note in report.notes)
    assert report.micro.l1.precision is None and report.micro.l1.pending == 3


def test_score_batch_without_agent_is_pure_and_json_roundtrips(tmp_path):
    # JSON round trips demonstrate that frozen input is a pure-data interface.
    for path in sorted(INPUTS.glob("*.json")):
        data = json.loads(path.read_text("utf-8"))
        from hvs_catalog.evaluation.scoring import ScoringPlan

        plan = ScoringPlan.model_validate(json.loads(json.dumps(data["plan"])))
        gold = [
            GoldPaperEntry.model_validate(json.loads(json.dumps(entry)))
            for entry in data["gold"]
        ]
        predictions = [
            PredictionPaperEntry.model_validate(json.loads(json.dumps(entry)))
            for entry in data["predictions"]
        ]
        aux = AuxiliaryScoringConfig.model_validate(data["auxiliary"])
        report = score_batch(plan, gold, predictions, auxiliary=aux)
        assert report.model_dump_json()
    assert not list(tmp_path.iterdir())


def test_scoped_coverage_validation_errors():
    paper_key = "synthetic-scoring-contract"
    gold, predictions = _simple_bundle()
    from hvs_catalog.evaluation.scoring import ScopedCoverage

    # Complete Gold cannot additionally declare a closed subset.
    plan = _plan_for(paper_key, gold, _auxiliary())
    scoped_plan = plan.model_copy(
        update={
            "scoped_coverages": [
                ScopedCoverage(
                    paper_key=paper_key,
                    object_identifiers=["SYN-A"],
                    quantity_paths=[DIST],
                )
            ]
        }
    )
    with pytest.raises(ScoringInputError, match="完整"):
        score_batch(scoped_plan, gold, predictions, auxiliary=_auxiliary())


def test_agent_technical_failure_marks_batch_provisional(tmp_path):
    plan, gold, predictions, _ = load_bundle("synthetic-s07u1e")
    from hvs_catalog.evaluation.scoring import MockAgentAdapter, protocol_only_config

    def boom(request):
        raise RuntimeError("synthetic agent crash")

    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    report = score_batch(
        plan,
        gold,
        predictions,
        auxiliary=auxiliary,
        agent_adapter=MockAgentAdapter(boom),
    )
    assert report.batch_provisional is True
    assert report.auxiliary.technical_failures == 1
    assert report.papers[0].l2.precision is None
    # Freeze failures too; reruns do not resample.
    report2 = score_batch(
        plan,
        gold,
        predictions,
        auxiliary=auxiliary,
        agent_adapter=MockAgentAdapter(boom),
    )
    assert report2.auxiliary.technical_failures == 1
    assert report2.auxiliary.judgments[0].reused is True
