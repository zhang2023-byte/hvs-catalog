"""Numerical validation and the real paper flow use independent synthetic answers."""

import json

import pytest
from pydantic import SecretStr
from test_roster import ScriptedAdapter, entry, submission

from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.pipeline.results import current_result, saved_quantities
from hvs_catalog.pipeline.roster import saved_roster
from hvs_catalog.records.quantity_validation import (
    payload_issues,
)


def value(number="520", quote="A and Alias-A are one star."):
    return {
        "value": number,
        "unit": "km/s",
        "evidence": [{"location": "T:p1-b0", "quote": quote}],
    }


def quantities(values=None, notes=""):
    return {
        "quantities": [{"quantity": "radial_velocity", "values": values or [value()]}],
        "notes": notes,
    }


def test_full_pipeline(prepared):
    space, prep, config = prepared
    roster = submission([entry("A"), entry("B", 2, "follow_up")])
    roster["notes"] = "Roster note."
    adapter = ScriptedAdapter(
        [roster, quantities(notes="A note."), {"quantities": [], "notes": "B note."}]
    )
    handle = start_extraction(
        space, preparation_execution_id=prep, request_key="full", configuration=config
    )
    report = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic-key"), adapter=adapter
        ),
    )
    assert report.outcome == "finished", report.errors
    assert report.result_status == "complete"
    assert len(adapter.seen) == 3
    first, second = adapter.seen[0], adapter.seen[1]
    assert first["messages"][1] == second["messages"][1]
    assert json.loads(second["messages"][2]["content"]) == {"identifiers": ["A"]}
    _, run = current_result(space, handle.execution_id)
    result = json.loads(space.read_file(run.result_ref))
    assert result["metadata"]["title"] == "Synthetic paper"
    assert result["notes"] == ["Roster note.", "A: A note.", "B: B note."]
    assert result["stars"][0]["quantities"][0]["values"][0]["value"] == "520"
    assert result["stars"][1]["quantities"] == []
    assert run.usage.attempts == 3
    assert run.usage.total_tokens == 90


def test_partial_values_and_duplicate_groups(prepared):
    space, prep, config = prepared
    config = config.model_copy(update={"max_format_corrections": 0})
    payload = quantities([value(), value("bad", "not in paper")], notes="Partial note.")
    payload["quantities"][0]["values"][1]["evidence"][0]["location"] = "T:p9-b9"
    payload["quantities"] += [
        {"quantity": "distance", "values": [value("8")]},
        {"quantity": "distance", "values": [value("9")]},
    ]
    adapter = ScriptedAdapter([submission(), payload])
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="partial",
        configuration=config,
    )
    report = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic-key"), adapter=adapter
        ),
    )
    assert report.outcome == "finished", report.errors
    assert report.result_status == "partial"
    roster = saved_roster(space, handle.execution_id)
    numerical = saved_quantities(space, handle.execution_id, roster)[0]
    assert numerical.data.source_paths == ["/quantities/0/values/0"]
    assert set(numerical.data.rejected) == {
        "/quantities/0/values/1",
        "/quantities/1",
        "/quantities/2",
    }
    _, run = current_result(space, handle.execution_id)
    assert run.status.resumable
    result = json.loads(space.read_file(run.result_ref))
    assert len(result["stars"][0]["quantities"]) == 1


def test_missing_metadata_blocks_before_creation(prepared):
    from materials_helpers import PAPER

    from hvs_catalog.materials.metadata import PREFIX

    space, prep, config = prepared
    space.store().transaction_read(
        lambda tx: tx.conn.execute(
            "DELETE FROM meta WHERE key=?", (PREFIX + PAPER + "v1",)
        )
    )
    with pytest.raises(ValueError, match="metadata is missing"):
        start_extraction(
            space,
            preparation_execution_id=prep,
            request_key="missing",
            configuration=config,
        )


@pytest.mark.parametrize("bad", [None, 520, True, ""])
def test_numerical_component_types(bad):
    payload = quantities([value(bad)])
    errors = payload_issues(
        payload, {"T:p1-b0": {"text": "A and Alias-A are one star."}}
    )
    assert any(e.path == "/quantities/0/values/0/value" for e in errors)


def run_full(prepared, payloads, *, limit=2):
    space, prep, default = prepared
    config = default.model_copy(update={"max_format_corrections": limit})
    adapter = ScriptedAdapter(payloads)
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="full-case",
        configuration=config,
    )
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    return handle, report, adapter, service


@pytest.mark.parametrize(
    "last,expected_round,fallback,expected_value",
    [
        ("not JSON", 1, True, "700"),
        ({"quantities": [], "notes": 99}, 2, False, None),
    ],
)
def test_latest_readable_round_not_largest_valid_subset(
    prepared, last, expected_round, fallback, expected_value
):
    first = quantities([value("520"), value("530")], notes="first")
    first["extra"] = True
    second = quantities([value("700")], notes="selected")
    second["extra"] = True
    handle, report, adapter, service = run_full(
        prepared, [submission(), first, second, last]
    )
    space = prepared[0]
    snapshot = saved_quantities(
        space, handle.execution_id, saved_roster(space, handle.execution_id)
    )[0]
    assert report.result_status == "partial"
    assert snapshot.data.source_round == expected_round
    assert snapshot.data.fallback is fallback
    assert [v.value for q in snapshot.data.quantities for v in q.values] == (
        [expected_value] if expected_value else []
    )
    assert snapshot.data.notes == ("selected" if fallback else None)
    assert [len(r["messages"]) for r in adapter.seen] == [2, 3, 6, 9]
    assert (
        resume_extraction(space, handle.execution_id, service=service).result_status
        == "partial"
    )
    assert len(adapter.seen) == 4


def test_failed_quantity_keeps_star_and_legal_note(prepared):
    bad = quantities([value(4)], notes="No usable value.")
    handle, report, _, _ = run_full(prepared, [submission(), bad], limit=0)
    _, run = current_result(prepared[0], handle.execution_id)
    output = json.loads(prepared[0].read_file(run.result_ref))
    assert report.result_status == "partial"
    assert run.stages.quantities[0].status == "failed"
    assert output["stars"][0]["quantities"] == []
    assert output["notes"] == ["A: No usable value."]


@pytest.mark.parametrize(
    "roster,expected,science",
    [
        ({"stars": [], "notes": "No stars."}, "complete", True),
        ({"stars": [], "notes": 2}, "failed", False),
    ],
)
def test_empty_roster_complete_and_failed_are_distinct(
    prepared, roster, expected, science
):
    handle, report, adapter, _ = run_full(prepared, [roster], limit=0)
    _, run = current_result(prepared[0], handle.execution_id)
    assert report.result_status == expected
    assert bool(run.result_ref) is science
    assert len(adapter.seen) == 1
    assert run.stages.quantities == []


def test_explicit_roster_reuse_keeps_identity_and_counts_only_new_calls(
    prepared, tmp_path
):
    from test_roster import run

    from hvs_catalog.workspace import backup_workspace, restore_workspace

    space, _, config = prepared
    old, _, _, _ = run(prepared, [submission()])
    original = saved_roster(space, old.execution_id)
    handle = start_extraction(
        space, roster_ref=original.ref(), request_key="reuse", configuration=config
    )
    adapter = ScriptedAdapter([quantities()])
    report = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=adapter
        ),
    )
    assert report.result_status == "complete", report.errors
    assert len(adapter.seen) == 1
    _, result = current_result(space, handle.execution_id)
    assert result.stages.roster.snapshot_ref == original.ref()
    assert result.stages.roster.source_execution == old.execution_id
    assert result.usage.attempts == 1
    backup_workspace(space, tmp_path / "backup")
    restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
    try:
        assert (
            resume_extraction(restored, handle.execution_id).result_status == "complete"
        )
    finally:
        restored.close()


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"lower_bound": "4"}, "values/0"),
        ({"error": "1", "lower_error": "2"}, "values/0"),
        ({"lower_inclusive": True}, "lower_bound"),
    ],
)
def test_numeric_combination_feedback_names_fields(patch, field):
    payload = quantities([{**value(), **patch}])
    from hvs_catalog.records.quantity_validation import payload_issues

    issues = payload_issues(
        payload, {"T:p1-b0": {"text": "A and Alias-A are one star."}}
    )
    assert issues
    assert any(field in issue.path for issue in issues)
    assert all(
        "oneOf" not in issue.message and "not valid" not in issue.message
        for issue in issues
    )


def test_legal_note_survives_unusable_group_container(prepared):
    handle, report, _, _ = run_full(
        prepared,
        [submission(), {"quantities": 8, "notes": "Value collection is unusable."}],
        limit=0,
    )
    _, run = current_result(prepared[0], handle.execution_id)
    assert report.result_status == "partial"
    assert run.stages.quantities[0].notes_available
    assert json.loads(prepared[0].read_file(run.result_ref))["notes"] == [
        "A: Value collection is unusable."
    ]


@pytest.mark.parametrize(
    "field", ["source_index", "source_round", "notes", "value", "response_ref"]
)
def test_snapshot_save_rejects_wrong_binding_and_unsubmitted_content(prepared, field):
    from hvs_catalog.workspace import CommitRequest, WorkspaceIntegrityError

    space = prepared[0]
    handle, _, _, _ = run_full(
        prepared,
        [
            submission([entry("A"), entry("B", 2, "follow_up")]),
            quantities(),
            quantities(),
        ],
    )
    roster = saved_roster(space, handle.execution_id)
    records = saved_quantities(space, handle.execution_id, roster)
    record = records[0]
    data = record.data.model_dump(mode="json")
    if field == "value":
        data["quantities"][0]["values"][0]["value"] = "invented"
    elif field == "response_ref":
        data[field] = records[1].data.response_ref.model_dump()
    else:
        data[field] = {"source_index": 1, "source_round": 1, "notes": "invented"}[field]
    forged = record.model_copy(
        update={
            "record_id": "forged-numerical",
            "data": type(record.data).model_validate(data),
        }
    )
    with pytest.raises(WorkspaceIntegrityError):
        space.store().commit(
            CommitRequest(operation_key="forged", kind="test", new_records=[forged])
        )


def test_created_execution_publishes_only_summary_and_reentry_is_stable(prepared):
    space, prep, config = prepared
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="created",
        configuration=config,
    )
    ref, result = current_result(space, handle.execution_id)
    assert result.status.execution == "created"
    assert result.status.result is None and result.result_ref is None
    assert result.stages.roster.source_round is None
    assert result.stages.quantities == [] and result.usage.attempts == 0
    same = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="created",
        configuration=config,
    )
    assert same.execution_id == handle.execution_id
    assert current_result(space, handle.execution_id)[0] == ref
