"""记录读写往返、版本拒绝与专家裁决配置保存。"""

from __future__ import annotations

import pytest
from records_helpers import fixture_by_name, load_fixture

from hvs_catalog.records import (
    CollectionValidationError,
    RecordFormatError,
    load_records,
    parse_record,
    parse_records,
    record_to_json,
    records_to_json,
    save_records,
    validate_collection,
)


def test_fixture_round_trip_identical():
    for name in ("gap-conflict", "numeric-forms"):
        records = load_fixture(name)
        again = parse_records(records_to_json(records))
        assert records == again


def test_single_record_json_round_trip(tmp_path):
    records = load_fixture("gap-conflict")
    for record in records:
        again = parse_record(record_to_json(record))
        assert again == record


def test_file_round_trip(tmp_path):
    records = load_fixture("numeric-forms")
    path = tmp_path / "nested" / "records.json"
    save_records(path, records)
    assert load_records(path) == records


def test_numeric_strings_never_rewritten():
    paper = fixture_by_name(load_fixture("numeric-forms"), "paper")
    values = paper.data.object_contributions[0].quantities
    by_id = {value.value_id: value for group in values for value in group.values}
    assert by_id["distance-interval"].value is None
    assert by_id["distance-interval"].range_lower == "7"
    assert by_id["distance-interval"].range_upper == "10"
    assert by_id["distance-interval"].interval_kind == "credible"
    assert by_id["distance-interval"].coverage.value == "95"
    assert by_id["distance-single-error"].lower_error is None
    assert by_id["distance-single-error"].upper_error == "0.5"
    parallax = next(
        value
        for group in values
        if group.quantity == "observed_phase_space.parallax"
        for value in group.values
    )
    assert parallax.value == "-0.10"


def test_unknown_record_type_rejected():
    with pytest.raises(RecordFormatError):
        parse_record({"schema": {"name": "fictional_record", "version": 1}})
    with pytest.raises(ValueError, match="未知记录类型"):
        parse_records('[{"schema": {"name": "fictional_record", "version": 1}}]')


def test_unknown_record_version_rejected():
    records = load_fixture("gap-conflict")
    payload = records[0].model_dump(mode="json", by_alias=True)
    payload["schema"]["version"] = 2
    with pytest.raises(ValueError, match="未知记录格式版本"):
        parse_record(payload)


def test_unknown_envelope_field_rejected():
    records = load_fixture("gap-conflict")
    payload = records[0].model_dump(mode="json", by_alias=True)
    payload["mystery"] = True
    with pytest.raises(ValueError):
        parse_record(payload)


def test_expert_adjudication_config_round_trip():
    run = fixture_by_name(load_fixture("gap-conflict"), "run")
    assert run.data.input_snapshot.expert_adjudication is True
    again = parse_record(record_to_json(run))
    assert again.data.input_snapshot.expert_adjudication is True

    disabled = run.model_copy(deep=True)
    disabled.data.input_snapshot.expert_adjudication = False
    assert (
        parse_record(record_to_json(disabled)).data.input_snapshot.expert_adjudication
        is False
    )


def test_five_author_states_storable():
    paper = fixture_by_name(load_fixture("gap-conflict"), "paper")
    contribution = paper.data.object_contributions[0]
    for status in (
        "no_overall_conclusion",
        "unbound",
        "possibly_unbound",
        "possibly_bound",
        "bound",
    ):
        contribution.paper_boundness.status = status
        assert contribution.paper_boundness.status == status


def test_pending_author_state_needs_reason():
    from hvs_catalog.records import PaperBoundness

    with pytest.raises(ValueError, match="必须关联问题或材料缺口"):
        PaperBoundness.model_validate(
            {"status": None, "evidence_refs": [], "issue_refs": [], "gap_refs": []}
        )


def test_execution_id_required_in_envelope():
    records = load_fixture("gap-conflict")
    payload = records[0].model_dump(mode="json", by_alias=True)
    del payload["execution_id"]
    with pytest.raises(ValueError):
        parse_record(payload)


def test_technical_failure_not_recorded_as_success():
    records = load_fixture("gap-conflict")
    run = fixture_by_name(records, "run")
    failed = run.model_copy(deep=True)
    failed.data.errors = [
        {
            "error_id": "error-1",
            "kind": "transport_failure",
            "message": "Call failed before any response.",
        }
    ]
    failed.data.steps[0].error_refs = ["error-1"]
    failed.data.result_refs = []
    failed.data.state = "failed"
    replaced = [failed if r.record_id == run.record_id else r for r in records]
    validate_collection(replaced)
    assert failed.data.errors and not failed.data.result_refs


def test_failed_run_requires_error_record():
    run = fixture_by_name(load_fixture("gap-conflict"), "run")
    payload = run.data.model_dump(mode="json")
    payload["state"] = "failed"
    payload["errors"] = []
    with pytest.raises(ValueError, match="失败执行必须记录运行错误"):
        from hvs_catalog.records import RunData

        RunData.model_validate(payload)


def test_revision_must_increase():
    records = load_fixture("gap-conflict")
    positions = [
        index
        for index, record in enumerate(records)
        if record.record_id == "gap-conflict-question"
    ]
    assert [records[index].revision for index in positions] == [1, 2]
    swapped = list(records)
    swapped[positions[0]], swapped[positions[1]] = (
        swapped[positions[1]],
        swapped[positions[0]],
    )
    with pytest.raises(CollectionValidationError):
        validate_collection(swapped)
