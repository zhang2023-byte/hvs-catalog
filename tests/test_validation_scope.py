"""Ordinary paths verify exact reference closure and execution scope; backup still verifies
all history.
"""

from __future__ import annotations

import json

import pytest
from records_helpers import load_fixture
from shared_scenarios import direct_flow, open_service, start_execution
from workspace_helpers import publish_fixture_collection

from hvs_catalog.records import build_record
from hvs_catalog.records.runs import Attempt
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import (
    CommitRequest,
    RecoveryInconsistentError,
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
)
from hvs_catalog.workspace.backup import framework_final_key
from hvs_catalog.workspace.store import StoreTransaction


def _commit(space, records, key):
    return space.store().commit(
        CommitRequest(operation_key=key, kind="manual", new_records=records)
    )


@pytest.mark.parametrize(
    "damage", ["historical-response", "historical-record", "checkpoint-id"]
)
def test_unrelated_history_does_not_block_daily_operations_but_backup_rejects(
    tmp_path, damage, monkeypatch
):
    space = initialize_workspace(tmp_path / "space")
    service = open_service(tmp_path / "calls.jsonl")
    try:
        old, flow, _ = start_execution(space, paper="B", request_key="old")
        assert (
            resume_execution(space, old.execution_id, flow, service=service).outcome
            == "finished"
        )
        if damage == "historical-response":
            run = space.store().read_record_current(old.run_ref.record_id)
            artifact = space.publish_run_file(
                old.execution_id, "old-response.json", b"original"
            )
            step = run.data.steps[0].model_copy(
                update={
                    "attempts": [
                        Attempt(
                            request=None,
                            response=artifact,
                            state="succeeded",
                            started_at=None,
                            finished_at=None,
                            usage=None,
                            service_request_id=None,
                        )
                    ]
                }
            )
            changed = build_record(
                type(run),
                record_id=run.record_id,
                created_at=run.created_at,
                revision=run.revision + 1,
                execution_id=old.execution_id,
                data=run.data.model_copy(update={"steps": [step, *run.data.steps[1:]]}),
            )
            _commit(space, [changed], "add-response")
            # Even if the latest run no longer references this response, current execution must verify its historical revisions.
            clean = build_record(
                type(run),
                record_id=run.record_id,
                created_at=run.created_at,
                revision=changed.revision + 1,
                execution_id=old.execution_id,
                data=run.data,
            )
            _commit(space, [clean], "remove-current-response")
            (space.root / artifact.path).write_bytes(b"corrupt")
        elif damage == "historical-record":
            with space.store().transaction() as tx:
                payload = json.loads(tx.record_payload(old.run_ref.record_id, 1))
                payload["data"]["result_refs"] = [
                    {"record_id": "missing-history", "revision": 1}
                ]
                tx.conn.execute(
                    "UPDATE records SET payload = ? WHERE record_id = ? AND revision = 1",
                    (json.dumps(payload), old.run_ref.record_id),
                )
        else:
            space.store().set_meta(framework_final_key(old.execution_id), "wrong-id")

        # An accidental all-history scan fails even without reading data.
        with monkeypatch.context() as patch:
            patch.setattr(
                StoreTransaction,
                "all_records",
                lambda self: pytest.fail("daily full-history scan"),
            )
            new, flow, _ = start_execution(space, paper="B", request_key="new")
            assert (
                resume_execution(space, new.execution_id, flow, service=service).outcome
                == "finished"
            )
            assert (
                resume_execution(space, new.execution_id, flow, service=service).mode
                == "already-finished"
            )

        with pytest.raises(RecoveryInconsistentError):
            resume_execution(space, old.execution_id, direct_flow(), service=service)
        with pytest.raises(WorkspaceIntegrityError):
            backup_workspace(space, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    finally:
        service.close()
        space.close()


def test_exact_old_revision_used_not_corrupt_newest_and_dependency_damage_rejected(
    tmp_path, monkeypatch
):
    space = initialize_workspace(tmp_path / "space")
    try:
        records = publish_fixture_collection(space, load_fixture("numeric-forms"))
        _commit(space, records, "initial")
        source = next(r for r in records if r.RECORD_NAME == "source_package")
        evidence = next(r for r in records if r.RECORD_NAME == "evidence")
        old_ref = evidence.data.source_ref
        new_file = space.publish_source_file("new-version.tex", b"different version")
        file = source.data.files[0].model_copy(
            update={"path": new_file.path, "sha256": new_file.sha256}
        )
        newer = build_record(
            type(source),
            record_id=source.record_id,
            created_at=source.created_at,
            execution_id=source.execution_id,
            revision=source.revision + 1,
            data=source.data.model_copy(update={"files": [file]}),
        )
        _commit(space, [newer], "new-source")
        (space.root / new_file.path).write_bytes(b"corrupt latest version")
        next_evidence = build_record(
            type(evidence),
            record_id=evidence.record_id,
            created_at=evidence.created_at,
            execution_id=evidence.execution_id,
            revision=evidence.revision + 1,
            data=evidence.data,
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                StoreTransaction,
                "all_records",
                lambda self: pytest.fail("full-history scan"),
            )
            _commit(space, [next_evidence], "old-source-reference")
        assert space.store().read_record(next_evidence.ref()).data.source_ref == old_ref
        with pytest.raises(WorkspaceIntegrityError):
            backup_workspace(space, tmp_path / "backup")
        # Corruption of an exact old revision required by this batch rejects the entire commit.
        (space.root / source.data.files[0].path).write_bytes(b"corrupt pinned source")
        rejected = build_record(
            type(evidence),
            record_id=evidence.record_id,
            created_at=evidence.created_at,
            execution_id=evidence.execution_id,
            revision=next_evidence.revision + 1,
            data=evidence.data,
        )
        with pytest.raises(WorkspaceIntegrityError, match="校验|哈希"):
            _commit(space, [rejected], "damaged-dependency")
        assert space.store().find_receipt("damaged-dependency") is None
        assert (
            space.store().current_revision(evidence.record_id) == next_evidence.revision
        )
    finally:
        space.close()


def test_duplicate_or_nonincreasing_revision_rejected_without_history_scan(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    try:
        records = publish_fixture_collection(space, load_fixture("numeric-forms"))
        source = next(r for r in records if r.RECORD_NAME == "source_package")
        with pytest.raises(WorkspaceIntegrityError, match="未递增"):
            _commit(space, [source, source], "duplicate")
        high = build_record(
            type(source),
            record_id=source.record_id,
            created_at=source.created_at,
            execution_id=source.execution_id,
            revision=3,
            data=source.data,
        )
        _commit(space, [high], "high")
        with pytest.raises(WorkspaceIntegrityError, match="未递增"):
            _commit(space, [source], "old")
        assert space.store().current_revision(source.record_id) == 3
    finally:
        space.close()
