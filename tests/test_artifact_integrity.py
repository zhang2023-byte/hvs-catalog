"""Saved rosters and fixed input archives cannot bypass source verification."""

import json

import pytest
from test_roster import run, submission

from hvs_catalog.pipeline import resume_extraction, start_roster_extraction
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.pipeline.roster import roster_targets, saved_roster
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import (
    CommitRequest,
    RecoveryInconsistentError,
    WorkspaceIntegrityError,
    backup_workspace,
)
from hvs_catalog.workspace.archives import read_json


@pytest.mark.parametrize(
    "field", ["notes", "stars", "indices", "source", "material", "response"]
)
def test_save_rejects_roster_content_not_in_original_submission(prepared, field):
    space = prepared[0]
    handle, report, _, _ = run(prepared, [submission()])
    assert report.outcome == "finished"
    record = saved_roster(space, handle.execution_id)
    update = {
        "notes": {"notes": "Invented note."},
        "stars": {"stars": []},
        "indices": {"source_indices": [99]},
        "source": {
            "source_ref": record.data.source_ref.model_copy(update={"revision": 999})
        },
        "material": {
            "material_ref": record.data.material_ref.model_copy(
                update={"sha256": "0" * 64}
            )
        },
        "response": {
            "response_ref": record.data.response_ref.model_copy(
                update={"sha256": "0" * 64}
            )
        },
    }[field]
    bad = record.model_copy(
        update={
            "record_id": "invalid-roster",
            "data": record.data.model_copy(update=update),
        }
    )
    with pytest.raises(
        (WorkspaceIntegrityError, RecoveryInconsistentError, ValueError)
    ):
        space.store().commit(
            CommitRequest(
                operation_key="invalid-save", kind="manual", new_records=[bad]
            )
        )
    assert space.store().find_receipt("invalid-save") is None
    assert space.store().read_record(record.ref()) == record


@pytest.mark.parametrize("which", ["response", "material", "result"])
@pytest.mark.parametrize("damage", ["delete", "change"])
def test_archive_damage_blocks_finished_resume_and_backup(
    prepared, tmp_path, which, damage
):
    space = prepared[0]
    handle, _, _, _ = run(prepared, [submission()])
    record = saved_roster(space, handle.execution_id)
    path = (
        space.root
        / (
            {
                "response": record.data.response_ref.path,
                "material": record.data.material_ref.path,
                "result": current_result(space, handle.execution_id)[0].path,
            }[which]
        )
    )
    if damage == "delete":
        path.unlink()
    else:
        path.write_text("{}")
    with pytest.raises(WorkspaceIntegrityError):
        backup_workspace(space, tmp_path / "backup")
    with pytest.raises(
        (WorkspaceIntegrityError, RecoveryInconsistentError, ValueError)
    ):
        resume_extraction(space, handle.execution_id)


def test_handoff_checks_source_and_preserves_all_identifiers(prepared):
    payload = submission()
    alias = json.loads(json.dumps(payload["stars"][0]["identifiers"][0]))
    alias["value"] = "Alias-A"
    payload["stars"][0]["identifiers"].append(alias)
    space = prepared[0]
    handle, _, _, _ = run(prepared, [payload])
    targets = roster_targets(space, handle.execution_id)
    target = targets["targets"][0]
    assert target["identifiers"] == ["A", "Alias-A"]
    assert target["source_index"] == 0
    assert target["roster_ref"] == targets["roster_ref"]
    record = saved_roster(space, handle.execution_id)
    source = space.store().read_record(record.data.source_ref)
    (space.root / source.data.files[0].path).write_bytes(b"different PDF")
    with pytest.raises(ValueError):
        roster_targets(space, handle.execution_id)


@pytest.mark.parametrize("which", ["prompt", "source", "material"])
def test_frozen_input_damage_stops_before_model_call(prepared, which):
    space, prep, config = prepared
    handle = start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="freeze"
    )
    snapshot = get_execution(space, handle.execution_id).input_snapshot
    source = space.store().read_record(snapshot.source_refs[0])
    path = {
        "prompt": snapshot.prompt_refs[0].path,
        "source": source.data.files[0].path,
        "material": json.loads(snapshot.options["prepared_prompt"])["path"],
    }[which]
    (space.root / path).write_bytes(b"corrupt")
    from pydantic import SecretStr
    from test_roster import ScriptedAdapter

    from hvs_catalog.llm.managed import CallService

    adapter = ScriptedAdapter([submission()])
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    try:
        report = resume_extraction(space, handle.execution_id, service=service)
        assert report.outcome == "failed"
    except (ValueError, WorkspaceIntegrityError, RecoveryInconsistentError):
        pass
    assert adapter.seen == []


def test_shared_response_reader_checks_reference(prepared):
    handle, _, _, _ = run(prepared, [submission()])
    space = prepared[0]
    ref = saved_roster(space, handle.execution_id).data.response_ref
    with pytest.raises(WorkspaceIntegrityError):
        read_json(space, ref.model_copy(update={"sha256": "0" * 64}))
