"""Current diagnostic isolation and historical read-only compatibility."""

import copy
from dataclasses import replace

import pytest
from records_helpers import fixture_by_name, load_fixture
from test_submission_conversion import gap_conflict_submission, make_context

from hvs_catalog.evaluation.adapt import _objects_from_paper
from hvs_catalog.pipeline.configuration import (
    ExtractionConfig,
    stored_configuration,
    validate_configuration_snapshot,
)
from hvs_catalog.records import convert_quantity_submission
from hvs_catalog.records.paper import PaperRecord
from hvs_catalog.records.submission import QuantitySubmission

RV = "observed_phase_space.radial_velocity"


def converted(*, alter=None):
    source = fixture_by_name(load_fixture("gap-conflict"), "source_package")
    context = replace(make_context(source), legacy_questions=False)
    payload = gap_conflict_submission()
    if alter:
        alter(payload)
    return convert_quantity_submission(
        context, QuantitySubmission.model_validate(payload)
    )


def assembled(result):
    original = fixture_by_name(load_fixture("gap-conflict"), "paper")
    obj = result.objects[0]
    data = original.model_dump(mode="json")
    contribution = data["data"]["object_contributions"][0]
    for field in (
        "quantities",
        "quantity_conflicts",
        "quantity_coverage",
        "author_errors",
    ):
        contribution[field] = [
            item.model_dump(mode="json") for item in getattr(obj, field)
        ]
    return PaperRecord.model_validate(data)


def test_passive_conflict_not_scored_but_independent_prior_measurement_remains():
    result = converted()
    assert result.questions == []
    obj = result.objects[0]
    assert obj.quantity_conflicts[0].issue_ref is None
    row = next(row for row in obj.quantity_coverage if row.quantity == RV)
    assert row.state == "partial" and not row.issue_refs
    prediction = _objects_from_paper(assembled(result))[0]
    assert [v.value.value for group in prediction.quantities for v in group.values] == [
        "550"
    ]


def test_historical_question_backed_conflict_retains_original_score_input():
    paper = fixture_by_name(load_fixture("gap-conflict"), "paper")
    assert all(
        c.issue_ref for c in paper.data.object_contributions[0].quantity_conflicts
    )
    prediction = _objects_from_paper(paper)[0]
    assert {v.value.value for group in prediction.quantities for v in group.values} >= {
        "520",
        "525",
    }


def test_different_conditions_are_not_passive_conflicts():
    def alter(payload):
        payload["objects"][0]["quantity_conflicts"][0]["alternatives"][1][
            "condition"
        ] = "A different reference state."

    with pytest.raises(ValueError, match="不同条件"):
        converted(alter=alter)


def test_conflicting_scope_rejects_even_third_formal_printing():
    def alter(payload):
        obj = payload["objects"][0]
        third = copy.deepcopy(obj["quantity_conflicts"][0]["alternatives"][0])
        third["value"] = "530"
        third["evidence"][0]["parts"][0]["raw_fragment"] = "530"
        obj["quantities"][0]["values"].append(third)

    with pytest.raises(ValueError, match="不能同时"):
        converted(alter=alter)


def test_new_conversion_rejects_human_question_and_precedent_inputs():
    for field, item in (
        (
            "questions",
            {
                "object_id": "contribution-A",
                "quantity": RV,
                "category": "interpretation",
                "prompt": "Which reference state is intended?",
            },
        ),
        (
            "precedent_uses",
            {"precedent_id": "old", "explanation": "Apply an old answer."},
        ),
    ):

        def alter(payload, field=field, item=item):
            if field == "questions":
                payload[field] = [item]
            else:
                payload["objects"][0][field] = [item]

        with pytest.raises(ValueError, match="retired"):
            converted(alter=alter)


def test_legacy_configuration_can_be_read_but_cannot_resume():
    from types import SimpleNamespace

    from hvs_catalog.records.questions import RulesRef
    from hvs_catalog.records.rules import current_rules_ref

    raw = ExtractionConfig().model_dump(mode="json")
    raw["expert_adjudication"] = True
    import json

    snapshot = SimpleNamespace(
        options={"extraction_config": json.dumps(raw)},
        expert_adjudication=True,
        rules=RulesRef(**current_rules_ref()),
    )
    assert stored_configuration(snapshot).expert_adjudication
    with pytest.raises(ValueError, match="read-only"):
        validate_configuration_snapshot(snapshot)
    # Conflicting legacy metadata must not restore expert execution by projecting
    # the active configuration to its single automatic request form.
    snapshot.expert_adjudication = False
    with pytest.raises(ValueError, match="read-only"):
        validate_configuration_snapshot(snapshot)
    raw["expert_adjudication"] = False
    snapshot.options["extraction_config"] = json.dumps(raw)
    snapshot.expert_adjudication = False
    snapshot.rules = RulesRef(version="0.2.1", sha256="a" * 64)
    with pytest.raises(ValueError, match="read-only"):
        validate_configuration_snapshot(snapshot)


@pytest.mark.parametrize("scope", ["quantity", "assessment", "roster"])
def test_diagnostic_evidence_requires_evidence_record(scope):
    from hvs_catalog.records import validate_collection

    records = load_fixture("gap-conflict")
    paper = fixture_by_name(records, "paper")
    roster = fixture_by_name(records, "roster")
    error = {
        "error_id": "author-error",
        "raw_values": ["contradictory source claim"],
        "reason": "The printed definitions contradict the statement.",
        "evidence_refs": [paper.ref().model_dump(mode="json")],
    }
    data = paper.model_dump(mode="json")
    target = data["data"]["object_contributions"][0]
    if scope == "quantity":
        target["author_errors"] = [
            {
                **error,
                "quantity": RV,
                "condition": "discarded source claim",
                "source": "this_paper",
            }
        ]
    elif scope == "assessment":
        target["paper_boundness"].update(status=None, author_errors=[error])
    else:
        data = roster.model_dump(mode="json")
        target = data["data"]["entries"][0]
        target.update(
            disposition="pending", contribution_id=None, author_errors=[error]
        )
    updated = (type(roster) if scope == "roster" else PaperRecord).model_validate(data)
    records = [
        updated if record.ref() == updated.ref() else record for record in records
    ]
    with pytest.raises(ValueError, match="原文错误证据"):
        validate_collection(records)


@pytest.mark.parametrize(
    "field,new_value",
    [("source", "prior_work"), ("frame", "Galactocentric"), ("epoch", "J2015.5")],
)
def test_independent_source_frame_or_epoch_survives_conflict(field, new_value):
    def alter(payload):
        obj = payload["objects"][0]
        independent = copy.deepcopy(obj["quantity_conflicts"][0]["alternatives"][0])
        independent[field] = new_value
        obj["quantities"][0]["values"] = [independent]

    result = converted(alter=alter)
    assert getattr(result.objects[0].quantities[0].values[0], field) == new_value


@pytest.mark.parametrize(
    "field,independent",
    [("source", "prior_work"), ("frame", "Galactocentric"), ("epoch", "J2015.5")],
)
def test_author_error_stops_only_matching_source_and_reference_state(
    field, independent
):
    from records_helpers import point_value

    from hvs_catalog.records.paper import QuantityAuthorError, author_error_blocks

    error = QuantityAuthorError(
        error_id="error",
        quantity=RV,
        condition="Same reported condition",
        source="this_paper",
        frame="heliocentric",
        epoch="J2000",
        raw_values=["bad value"],
        reason="Explicit source correction.",
        evidence_refs=[point_value().direct_evidence[0].evidence_ref],
    )
    value = point_value(
        condition=error.condition,
        source=error.source,
        frame=error.frame,
        epoch=error.epoch,
    )
    assert author_error_blocks(error, value)
    assert not author_error_blocks(error, value.model_copy(update={field: independent}))


@pytest.mark.parametrize(
    "field,other",
    [("source", "prior_work"), ("frame", "Galactocentric"), ("epoch", "J2015.5")],
)
def test_separate_source_or_reference_state_cannot_be_conflict_alternative(
    field, other
):
    def alter(payload):
        payload["objects"][0]["quantity_conflicts"][0]["alternatives"][1][field] = other

    with pytest.raises(ValueError, match="不同条件"):
        converted(alter=alter)
